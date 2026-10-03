from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from jev_trading_lab.core.manifest import Manifest, load_manifest, parse_manifest_bytes
from jev_trading_lab.core.observations import (
    ObservationMetadata,
    ObservationStatus,
    correct_observation,
)
from jev_trading_lab.core.run_window import RunWindow, resolve_run_window
from jev_trading_lab.core.time import (
    MAX_DATETIME_US,
    MIN_DATETIME_US,
    datetime_to_utc_us,
)
from jev_trading_lab.core.values import MicroDollars
from jev_trading_lab.persistence.core_repo import CoreRepository, CoreRepositoryError
from jev_trading_lab.persistence.db import connect_database
from jev_trading_lab.persistence.migrations import apply_migrations, load_migrations

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config/phase1.example.json"
TABLES = (
    "manifests",
    "run_windows",
    "runs",
    "accounts",
    "opportunities",
    "observations",
    "artifact_refs",
)
HEX_A = "a" * 64
HEX_B = "b" * 64
HEX_C = "c" * 64


def _connection(path: str | None = None) -> sqlite3.Connection:
    if path is None or path == ":memory:":
        descriptor, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(descriptor)
    connection = sqlite3.connect(path, isolation_level=None, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA trusted_schema = OFF")
    apply_migrations(connection, clock=lambda: 1_700_000_000_000_000)
    return connection


def _activation_value() -> dict[str, Any]:
    value = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    value["run"]["example_only"] = False
    value["models"]["openai"].update(
        sdk_version="1.93.0",
        expected_revision="rev-20261002-openai-a1",
        expected_fingerprint="fp_7f4d9a2c18b6e305",
    )
    value["models"]["jev"].update(
        sdk_version="3.12.0",
        request_model_snapshot="jev-systemone-phase1-2026-10-02",
        expected_returned_model_id="jev-systemone-phase1-2026-10-02",
        expected_revision="rev-20261002-jev-b2",
        expected_fingerprint="fp_5a8e1d7c92f403b6",
    )
    value["baselines"]["polymarket_control_artifact_id"] = "poly-control-20261002-a1-v1"
    value["baselines"]["stock_control_artifact_id"] = "stock-control-20261002-b2-v1"

    def replace_hashes(item: object, path: str = "") -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                child_path = f"{path}.{key}" if path else key
                if key.endswith("sha256") and child_path not in {
                    "calendar.lock_sha256",
                    "calendar.artifact_sha256",
                    "calendar.tm_rule_sha256",
                    "providers.nyse_calendar.contract_sha256",
                }:
                    item[key] = hashlib.sha256(child_path.encode()).hexdigest()
                else:
                    replace_hashes(child, child_path)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                replace_hashes(child, f"{path}.{index}")

    replace_hashes(value)
    return value


def _manifest(*, active: bool = True) -> Manifest:
    if not active:
        return load_manifest(EXAMPLE)
    return parse_manifest_bytes(json.dumps(_activation_value()).encode())


def _window(manifest: Manifest) -> RunWindow:
    return resolve_run_window(
        enrollment_start_us=datetime_to_utc_us(datetime(2026, 10, 5, tzinfo=UTC)),
        manifest=manifest,
    )


def _registered(connection: sqlite3.Connection) -> tuple[CoreRepository, str]:
    repository = CoreRepository(connection, expected_busy_timeout_ms=5000)
    manifest = _manifest()
    run_id = repository.insert_run(
        manifest=manifest,
        run_window=_window(manifest),
        dependency_lock_sha256=HEX_A,
        registered_at_us=1_800_000_000_000_000,
    )
    return repository, run_id


def test_isolated_arm_accounts() -> None:
    connection = _connection()
    repository, run_id = _registered(connection)
    try:
        ids = {
            repository.create_account(
                run_id=run_id,
                stream=stream,
                arm=arm,
                currency="USD",
                initial_capital=MicroDollars(10_000_000_000),
            )
            for stream, arm in (
                ("polymarket", "AI"),
                ("polymarket", "DETERMINISTIC"),
                ("polymarket", "CASH"),
                ("stocks", "AI"),
                ("stocks", "DETERMINISTIC"),
                ("stocks", "CASH"),
                ("stocks", "EXPOSURE_MATCHED_PASSIVE"),
            )
        }
        assert len(ids) == 7
        assert connection.execute("SELECT count(*) FROM accounts").fetchone()[0] == 7

        first = repository.create_account(
            run_id=run_id,
            stream="polymarket",
            arm="AI",
            currency="USD",
            initial_capital=MicroDollars(10_000_000_000),
        )
        assert first in ids
        with pytest.raises(ValueError, match="conflicts"):
            repository.create_account(
                run_id=run_id,
                stream="polymarket",
                arm="AI",
                currency="USD",
                initial_capital=MicroDollars(9_000_000_000),
            )
        with pytest.raises(ValueError, match="arm"):
            repository.create_account(
                run_id=run_id,
                stream="polymarket",
                arm="EXPOSURE_MATCHED_PASSIVE",
                currency="USD",
                initial_capital=MicroDollars(10_000_000_000),
            )
        with pytest.raises(TypeError, match="MicroDollars"):
            repository.create_account(
                run_id=run_id,
                stream="stocks",
                arm="AI",
                currency="USD",
                initial_capital=True,  # type: ignore[arg-type]
            )
    finally:
        connection.close()


def test_shared_opportunity_id_excludes_arm() -> None:
    connection = _connection()
    repository, run_id = _registered(connection)
    try:
        opportunity_id = repository.insert_opportunity(
            run_id=run_id,
            stream="stocks",
            subject_id="AAPL",
            scheduled_for_us=1_800_000_100_000_000,
        )
        assert opportunity_id == repository.insert_opportunity(
            run_id=run_id,
            stream="stocks",
            subject_id="AAPL",
            scheduled_for_us=1_800_000_100_000_000,
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(opportunities)")
        }
        assert "arm" not in columns
        with pytest.raises(TypeError, match="unexpected keyword"):
            repository.insert_opportunity(  # type: ignore[call-arg]
                run_id=run_id,
                stream="stocks",
                subject_id="AAPL",
                scheduled_for_us=1_800_000_100_000_000,
                arm="AI",
            )
    finally:
        connection.close()


def test_manifest_and_run_immutable() -> None:
    connection = _connection()
    repository, run_id = _registered(connection)
    try:
        row = repository.get_run(run_id)
        assert row is not None
        assert row["phase"] == "phase1"
        assert row["mode"] == "paper"
        assert row["dependency_lock_sha256"] == HEX_A
        assert connection.execute("SELECT count(*) FROM manifests").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM run_windows").fetchone()[0] == 1
        repository.create_account(
            run_id=run_id,
            stream="stocks",
            arm="AI",
            currency="USD",
            initial_capital=MicroDollars(10_000_000_000),
        )
        observation = ObservationMetadata(
            source_uri="https://example.test/immutable",
            published_at_us=None,
            retrieved_at_us=1_800_000_000_000_001,
            status=ObservationStatus.OK,
            content_sha256=HEX_B,
        )
        logical_key = hashlib.sha256(b"immutable").hexdigest()
        repository.insert_observation(
            observation, logical_key=logical_key, run_id=run_id
        )
        artifact_id = repository.insert_artifact_ref(
            kind="snapshot",
            content_sha256=HEX_C,
            byte_size=1,
            storage_relpath="sha256/cc/immutable.bin",
            created_at_us=1_800_000_000_000_002,
            source_observation_id=observation.observation_id,
        )
        repository.insert_opportunity(
            run_id=run_id,
            stream="stocks",
            subject_id="AAPL",
            scheduled_for_us=1_800_000_000_000_003,
            snapshot_id=artifact_id,
        )

        for table in TABLES:
            primary_key = connection.execute(f"PRAGMA table_info({table})").fetchone()[
                1
            ]
            with pytest.raises(sqlite3.IntegrityError, match=f"immutable:{table}"):
                connection.execute(f"UPDATE {table} SET {primary_key} = {primary_key}")
            with pytest.raises(sqlite3.IntegrityError, match=f"immutable:{table}"):
                connection.execute(f"DELETE FROM {table}")

        with pytest.raises(ValueError, match="example_only"):
            example = _manifest(active=False)
            repository.insert_run(
                manifest=example,
                run_window=_window(example),
                dependency_lock_sha256=HEX_A,
                registered_at_us=1_800_000_000_000_000,
            )
    finally:
        connection.close()


def test_migration_002_schema_contract_and_same_migration_guards() -> None:
    migrations = load_migrations()
    assert [(item.version, item.name) for item in migrations] == [
        (1, "001_migration_metadata.sql"),
        (2, "002_core_entities.sql"),
    ]
    connection = _connection()
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        assert set(TABLES) <= tables
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger'"
            )
        }
        assert {
            f"{table}_immutable_{operation}"
            for table in TABLES
            for operation in ("update", "delete")
        } <= triggers
        assert {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='index'"
            )
        } >= {
            "runs_manifest_idx",
            "runs_window_idx",
            "accounts_run_idx",
            "opportunities_run_idx",
            "observations_run_idx",
            "artifact_refs_observation_idx",
        }
        for table in TABLES:
            sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            assert "STRICT" in sql and "WITHOUT ROWID" in sql
        foreign_keys = {
            (table, row[2], row[3], row[4], row[6])
            for table in TABLES
            for row in connection.execute(f"PRAGMA foreign_key_list({table})")
        }
        assert (
            "runs",
            "manifests",
            "manifest_id",
            "manifest_id",
            "NO ACTION",
        ) in foreign_keys
        assert (
            "runs",
            "run_windows",
            "run_window_id",
            "run_window_id",
            "NO ACTION",
        ) in foreign_keys
        assert ("accounts", "runs", "run_id", "run_id", "NO ACTION") in foreign_keys
        assert (
            "opportunities",
            "artifact_refs",
            "snapshot_id",
            "artifact_id",
            "NO ACTION",
        ) in foreign_keys
    finally:
        connection.close()


def test_database_rejects_hostile_ids_types_bounds_and_fixedpoint() -> None:
    connection = _connection()
    try:
        valid = (HEX_A, b"{}", 1, 1)
        for bad_id in ("A" * 64, "g" * 64, "a" * 63, ""):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO manifests VALUES (?, ?, ?, ?)",
                    (bad_id, b"{}", 1, 1),
                )
        for canonical in (b"", b"x" * (256 * 1024 + 1), 7):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO manifests VALUES (?, ?, ?, ?)",
                    (HEX_B, canonical, 1, 1),
                )
        connection.execute("INSERT INTO manifests VALUES (?, ?, ?, ?)", valid)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?)",
                (HEX_B, HEX_C, "stocks", "AI", "USD", 0),
            )
        for index, timestamp in enumerate(
            (MIN_DATETIME_US - 1, MAX_DATETIME_US + 1, b"1")
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO observations VALUES "
                    "(?, NULL, ?, ?, NULL, ?, ?, ?, NULL)",
                    (
                        hashlib.sha256(f"bad-{index}".encode()).hexdigest(),
                        HEX_A,
                        "https://example.test/x",
                        timestamp,
                        "ok",
                        HEX_C,
                    ),
                )
    finally:
        connection.close()


def test_manifest_and_window_tampering_is_revalidated() -> None:
    connection = _connection()
    repository = CoreRepository(connection, expected_busy_timeout_ms=5000)
    manifest = _manifest()
    window = _window(manifest)
    try:
        object.__setattr__(manifest, "_manifest_id", "manifest:" + HEX_A)
        with pytest.raises(ValueError, match="revalidation failed"):
            repository.insert_manifest(manifest)

        fresh = _manifest()
        object.__setattr__(window, "_run_window_id", "run-window:" + HEX_A)
        with pytest.raises(ValueError, match="revalidation failed"):
            repository.insert_run_window(window, fresh)
    finally:
        connection.close()


def test_observation_supersession_constraints_are_authoritative() -> None:
    connection = _connection()
    repository = CoreRepository(connection, expected_busy_timeout_ms=5000)
    logical_key = hashlib.sha256(b"logical").hexdigest()
    original = ObservationMetadata(
        source_uri="https://example.test/item",
        published_at_us=100,
        retrieved_at_us=200,
        status=ObservationStatus.OK,
        content_sha256=HEX_A,
    )
    correction = correct_observation(
        original,
        retrieved_at_us=300,
        content_sha256=HEX_B,
        supersedes_id=original.observation_id,
    )
    try:
        first_id = repository.insert_observation(original, logical_key=logical_key)
        second_id = repository.insert_observation(correction, logical_key=logical_key)
        assert second_id != first_id
        assert (
            repository.insert_observation(correction, logical_key=logical_key)
            == second_id
        )

        base = (
            HEX_C,
            None,
            logical_key,
            "https://example.test/item",
            400,
            "ok",
            hashlib.sha256(b"third").hexdigest(),
        )
        cases = (
            (first_id, *base[1:]),
            (second_id, None, hashlib.sha256(b"other").hexdigest(), *base[3:]),
            (second_id, None, logical_key, base[3], 250, base[5], base[6]),
            (second_id, None, logical_key, base[3], 400, base[5], HEX_B),
        )
        for values in cases:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO observations "
                    "(observation_id,run_id,logical_key,source_uri,published_at_us,"
                    "retrieved_at_us,status,content_sha256,supersedes_id) "
                    "VALUES (?,NULL,?,?,NULL,?,?,?,?)",
                    (
                        HEX_C,
                        values[2],
                        values[3],
                        values[4],
                        values[5],
                        values[6],
                        values[0],
                    ),
                )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    hashlib.sha256(b"branch").hexdigest(),
                    None,
                    logical_key,
                    original.source_uri,
                    None,
                    500,
                    "ok",
                    hashlib.sha256(b"branch-content").hexdigest(),
                    first_id,
                ),
            )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "path",
    ("/absolute", "../escape", "a/../escape", "a\\b", "a\x00b", "", "./a"),
)
def test_artifact_path_and_metadata_validation(path: str) -> None:
    connection = _connection()
    repository = CoreRepository(connection, expected_busy_timeout_ms=5000)
    try:
        with pytest.raises((TypeError, ValueError), match="path"):
            repository.insert_artifact_ref(
                kind="provider_payload",
                content_sha256=HEX_A,
                byte_size=10,
                storage_relpath=path,
                created_at_us=1,
            )
    finally:
        connection.close()


def test_artifact_idempotency_and_conflict() -> None:
    connection = _connection()
    repository = CoreRepository(connection, expected_busy_timeout_ms=5000)
    try:
        artifact_id = repository.insert_artifact_ref(
            kind="provider_payload",
            content_sha256=HEX_A,
            byte_size=10,
            storage_relpath="sha256/aa/payload.bin",
            created_at_us=1,
        )
        assert artifact_id == repository.insert_artifact_ref(
            kind="provider_payload",
            content_sha256=HEX_A,
            byte_size=10,
            storage_relpath="sha256/aa/payload.bin",
            created_at_us=1,
        )
        with pytest.raises(ValueError, match="conflicts"):
            repository.insert_artifact_ref(
                kind="provider_payload",
                content_sha256=HEX_A,
                byte_size=11,
                storage_relpath="sha256/aa/payload.bin",
                created_at_us=1,
            )
        for bad_hash, bad_size in (("A" * 64, 1), (HEX_B, -1), (HEX_B, True)):
            with pytest.raises((TypeError, ValueError)):
                repository.insert_artifact_ref(
                    kind="provider_payload",
                    content_sha256=bad_hash,
                    byte_size=bad_size,  # type: ignore[arg-type]
                    storage_relpath="sha256/bb/payload.bin",
                    created_at_us=1,
                )
    finally:
        connection.close()


def test_repository_rejects_active_caller_transaction_without_rollback() -> None:
    connection = _connection()
    repository = CoreRepository(connection, expected_busy_timeout_ms=5000)
    try:
        connection.execute("BEGIN")
        connection.execute("CREATE TEMP TABLE caller_state(value TEXT)")
        connection.execute("INSERT INTO caller_state VALUES ('keep')")
        with pytest.raises(RuntimeError, match="active caller transaction"):
            repository.insert_manifest(_manifest())
        assert connection.in_transaction
        assert (
            connection.execute("SELECT value FROM caller_state").fetchone()[0] == "keep"
        )
        connection.execute("ROLLBACK")
        assert connection.execute("SELECT count(*) FROM manifests").fetchone()[0] == 0
    finally:
        connection.close()


def test_run_insert_failure_rolls_back_manifest_and_window() -> None:
    connection = _connection()
    repository = CoreRepository(connection, expected_busy_timeout_ms=5000)
    manifest = _manifest()
    try:
        connection.execute(
            "CREATE TRIGGER reject_run BEFORE INSERT ON runs "
            "BEGIN SELECT RAISE(ABORT, 'forced'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="forced"):
            repository.insert_run(
                manifest=manifest,
                run_window=_window(manifest),
                dependency_lock_sha256=HEX_A,
                registered_at_us=1_800_000_000_000_000,
            )
        assert connection.execute("SELECT count(*) FROM manifests").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM run_windows").fetchone()[0] == 0
        assert not connection.in_transaction
    finally:
        connection.close()


def _populated_core(
    connection: sqlite3.Connection,
) -> tuple[CoreRepository, dict[str, str]]:
    repository, run_id = _registered(connection)
    account_id = repository.create_account(
        run_id=run_id,
        stream="stocks",
        arm="AI",
        currency="USD",
        initial_capital=MicroDollars(10_000_000_000),
    )
    original = ObservationMetadata(
        source_uri="https://example.test/raw",
        published_at_us=None,
        retrieved_at_us=1_800_000_000_000_001,
        status=ObservationStatus.OK,
        content_sha256=HEX_B,
    )
    observation_id = repository.insert_observation(
        original, logical_key=HEX_A, run_id=run_id
    )
    artifact_id = repository.insert_artifact_ref(
        kind="snapshot",
        content_sha256=HEX_C,
        byte_size=1,
        storage_relpath="snapshots/core.bin",
        created_at_us=1_800_000_000_000_002,
        source_observation_id=observation_id,
    )
    opportunity_id = repository.insert_opportunity(
        run_id=run_id,
        stream="stocks",
        subject_id="AAPL",
        scheduled_for_us=1_800_000_000_000_003,
        snapshot_id=artifact_id,
    )
    manifest_id = connection.execute("SELECT manifest_id FROM runs").fetchone()[0]
    window_id = connection.execute("SELECT run_window_id FROM runs").fetchone()[0]
    return repository, {
        "manifests": manifest_id,
        "run_windows": window_id,
        "runs": run_id,
        "accounts": account_id,
        "opportunities": opportunity_id,
        "observations": observation_id,
        "artifact_refs": artifact_id,
    }


@pytest.mark.parametrize("table", TABLES)
def test_insert_or_replace_cannot_change_immutable_row(table: str) -> None:
    connection = _connection()
    try:
        _repository, keys = _populated_core(connection)
        connection.execute("PRAGMA recursive_triggers = OFF")
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        row = list(connection.execute(f"SELECT * FROM {table}").fetchone())
        changed_index = next(
            index
            for index, column in enumerate(columns)
            if index and column not in {"manifest_id", "run_window_id", "run_id"}
        )
        value = row[changed_index]
        row[changed_index] = (
            value + 1
            if type(value) is int
            else value + (b"-changed" if type(value) is bytes else "-changed")
        )
        placeholders = ",".join("?" for _ in columns)
        with pytest.raises(sqlite3.IntegrityError, match=f"immutable-conflict:{table}"):
            connection.execute(
                f"INSERT OR REPLACE INTO {table} VALUES ({placeholders})", row
            )
        assert (
            connection.execute(
                f"SELECT * FROM {table} WHERE {columns[0]} = ?", (keys[table],)
            ).fetchone()
            is not None
        )
    finally:
        connection.close()


def test_observation_replace_rejects_two_node_and_long_cycles() -> None:
    connection = _connection()
    try:
        repository, _keys = _populated_core(connection)
        original_row = connection.execute("SELECT * FROM observations").fetchone()
        original = ObservationMetadata(
            source_uri=original_row["source_uri"],
            published_at_us=original_row["published_at_us"],
            retrieved_at_us=original_row["retrieved_at_us"],
            status=ObservationStatus(original_row["status"]),
            content_sha256=original_row["content_sha256"],
        )
        second = correct_observation(
            original,
            retrieved_at_us=original.retrieved_at_us + 1,
            content_sha256=hashlib.sha256(b"second").hexdigest(),
            supersedes_id=original.observation_id,
        )
        repository.insert_observation(
            second, logical_key=HEX_A, run_id=original_row["run_id"]
        )
        third = correct_observation(
            second,
            retrieved_at_us=second.retrieved_at_us + 1,
            content_sha256=hashlib.sha256(b"third").hexdigest(),
            supersedes_id=second.observation_id,
        )
        repository.insert_observation(
            third, logical_key=HEX_A, run_id=original_row["run_id"]
        )
        connection.execute("PRAGMA recursive_triggers = OFF")
        for successor in (second.observation_id, third.observation_id):
            changed = list(original_row)
            changed[-1] = successor
            with pytest.raises(
                sqlite3.IntegrityError, match="immutable-conflict:observations"
            ):
                connection.execute(
                    "INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?,?,?,?)",
                    changed,
                )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("pragma", "drift"),
    (
        ("foreign_keys", "OFF"),
        ("recursive_triggers", "OFF"),
        ("trusted_schema", "ON"),
        ("journal_mode", "DELETE"),
        ("synchronous", "NORMAL"),
        ("busy_timeout", "1234"),
    ),
)
@pytest.mark.parametrize("after_success", (False, True))
def test_repository_rejects_complete_pragma_drift_without_side_effects(
    pragma: str, drift: str, after_success: bool
) -> None:
    connection = _connection()
    repository = CoreRepository(connection, expected_busy_timeout_ms=5000)
    manifest = _manifest()
    try:
        if after_success:
            repository.insert_manifest(manifest)
        connection.execute(f"PRAGMA {pragma} = {drift}")
        with pytest.raises(CoreRepositoryError, match=pragma):
            if after_success:
                repository.insert_run_window(_window(manifest), manifest)
            else:
                repository.insert_manifest(manifest)
        assert connection.execute("SELECT count(*) FROM manifests").fetchone()[
            0
        ] == int(after_success)
        assert connection.execute("SELECT count(*) FROM run_windows").fetchone()[0] == 0
        assert not connection.in_transaction
    finally:
        connection.close()


def test_plain_sqlite_repository_requires_explicit_expected_busy_timeout() -> None:
    connection = _connection()
    try:
        with pytest.raises(ValueError, match="expected_busy_timeout_ms"):
            CoreRepository(connection)
    finally:
        connection.close()


@pytest.mark.parametrize("invalid", (True, "5000", 0, -1, 2**31))
def test_plain_sqlite_repository_rejects_invalid_expected_busy_timeout(
    invalid: object,
) -> None:
    connection = _connection()
    try:
        with pytest.raises((TypeError, ValueError), match="expected_busy_timeout_ms"):
            CoreRepository(connection, expected_busy_timeout_ms=invalid)  # type: ignore[arg-type]
    finally:
        connection.close()


def test_secure_repository_rejects_explicit_timeout_override(tmp_path: Path) -> None:
    connection = connect_database(
        (tmp_path / "private" / "db.sqlite3").absolute(),
        busy_timeout_ms=5000,
        mount_resolver=lambda _path: "ext4",
    )
    try:
        connection.execute("PRAGMA busy_timeout = 1234")
        with pytest.raises(ValueError, match="expected_busy_timeout_ms"):
            CoreRepository(connection, expected_busy_timeout_ms=1234)
        repository = CoreRepository(connection, expected_busy_timeout_ms=5000)
        with pytest.raises(CoreRepositoryError, match="busy_timeout"):
            repository.insert_manifest(_manifest())
    finally:
        connection.close()


@pytest.mark.parametrize("pragma", ("foreign_keys", "recursive_triggers"))
def test_repository_rejects_pragma_drift_before_account_write(pragma: str) -> None:
    connection = _connection()
    try:
        repository, run_id = _registered(connection)
        connection.execute(f"PRAGMA {pragma} = OFF")
        with pytest.raises(CoreRepositoryError, match=pragma):
            repository.create_account(
                run_id=run_id,
                stream="stocks",
                arm="AI",
                currency="USD",
                initial_capital=MicroDollars(10_000_000_000),
            )
        assert connection.execute("SELECT count(*) FROM accounts").fetchone()[0] == 0
    finally:
        connection.close()


def test_repository_rejects_foreign_key_drift_before_opportunity_write() -> None:
    connection = _connection()
    try:
        repository, run_id = _registered(connection)
        connection.execute("PRAGMA foreign_keys = OFF")
        with pytest.raises(CoreRepositoryError, match="foreign_keys"):
            repository.insert_opportunity(
                run_id=run_id,
                stream="stocks",
                subject_id="AAPL",
                scheduled_for_us=1_800_000_000_000_001,
            )
        assert (
            connection.execute("SELECT count(*) FROM opportunities").fetchone()[0] == 0
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("attribute", "value"),
    (
        ("source_uri", "http://example.test/x"),
        ("source_uri", "https://user@example.test/x"),
        ("source_uri", "https://example.test/x#fragment"),
        ("status", "ok"),
        ("content_sha256", "A" * 64),
        ("retrieved_at_us", True),
        ("supersedes_id", HEX_C),
    ),
)
def test_repository_authoritatively_revalidates_tampered_observation(
    attribute: str, value: object
) -> None:
    connection = _connection()
    repository = CoreRepository(connection, expected_busy_timeout_ms=5000)
    observation = ObservationMetadata(
        source_uri="https://example.test/x",
        published_at_us=None,
        retrieved_at_us=200,
        status=ObservationStatus.OK,
        content_sha256=HEX_A,
    )
    object.__setattr__(observation, attribute, value)
    try:
        with pytest.raises((TypeError, ValueError), match="observation"):
            repository.insert_observation(observation, logical_key=HEX_B)
        assert (
            connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
        )
    finally:
        connection.close()


def test_repository_accepts_semantically_valid_forged_observation_wrapper() -> None:
    connection = _connection()
    repository = CoreRepository(connection, expected_busy_timeout_ms=5000)
    forged = object.__new__(ObservationMetadata)
    object.__setattr__(forged, "source_uri", "https://example.test/forged")
    object.__setattr__(forged, "published_at_us", None)
    object.__setattr__(forged, "retrieved_at_us", 200)
    object.__setattr__(forged, "status", ObservationStatus.OK)
    object.__setattr__(forged, "content_sha256", HEX_A)
    object.__setattr__(forged, "supersedes_id", None)
    try:
        observation_id = repository.insert_observation(forged, logical_key=HEX_B)
        row = connection.execute(
            "SELECT source_uri, content_sha256 FROM observations "
            "WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
        assert tuple(row) == ("https://example.test/forged", HEX_A)
    finally:
        connection.close()


def test_repository_rejects_simultaneous_observation_field_tampering() -> None:
    connection = _connection()
    repository = CoreRepository(connection, expected_busy_timeout_ms=5000)
    observation = ObservationMetadata(
        source_uri="https://example.test/original",
        published_at_us=100,
        retrieved_at_us=200,
        status=ObservationStatus.OK,
        content_sha256=HEX_A,
    )
    object.__setattr__(observation, "source_uri", "http://example.test/evil")
    object.__setattr__(observation, "status", "ok")
    object.__setattr__(observation, "content_sha256", "A" * 64)
    object.__setattr__(observation, "retrieved_at_us", True)
    object.__setattr__(observation, "supersedes_id", HEX_B)
    with pytest.raises(AttributeError):
        object.__setattr__(observation, "_constructor_proof", HEX_C)
    try:
        with pytest.raises((TypeError, ValueError), match="observation"):
            repository.insert_observation(observation, logical_key=HEX_B)
        assert (
            connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
        )
    finally:
        connection.close()


def test_repository_reconstructs_correction_predecessor_from_database() -> None:
    connection = _connection()
    repository = CoreRepository(connection, expected_busy_timeout_ms=5000)
    original = ObservationMetadata(
        source_uri="https://example.test/original",
        published_at_us=None,
        retrieved_at_us=200,
        status=ObservationStatus.OK,
        content_sha256=HEX_A,
    )
    repository.insert_observation(original, logical_key=HEX_C)
    correction = correct_observation(
        original,
        retrieved_at_us=300,
        content_sha256=HEX_B,
        supersedes_id=original.observation_id,
    )
    object.__setattr__(original, "content_sha256", HEX_C)
    try:
        assert repository.insert_observation(correction, logical_key=HEX_C)
    finally:
        connection.close()


def test_all_identifier_and_hash_columns_reject_nul_suffix() -> None:
    connection = _connection()
    try:
        _repository, _keys = _populated_core(connection)
        targets = {
            "manifests": ("manifest_id",),
            "run_windows": (
                "run_window_id",
                "calendar_lock_sha256",
                "calendar_artifact_sha256",
                "tm_rule_sha256",
            ),
            "runs": (
                "run_id",
                "manifest_id",
                "run_window_id",
                "dependency_lock_sha256",
            ),
            "accounts": ("account_id", "run_id"),
            "opportunities": ("opportunity_id", "run_id", "snapshot_id"),
            "observations": (
                "observation_id",
                "run_id",
                "logical_key",
                "content_sha256",
                "supersedes_id",
            ),
            "artifact_refs": ("artifact_id", "content_sha256", "source_observation_id"),
        }
        for table, column_names in targets.items():
            columns = [
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            ]
            source = list(
                connection.execute(f"SELECT * FROM {table} LIMIT 1").fetchone()
            )
            for column in column_names:
                row = source.copy()
                row[columns.index(column)] = HEX_A + "\x00suffix"
                placeholders = ",".join("?" for _ in columns)
                with pytest.raises(sqlite3.IntegrityError):
                    connection.execute(
                        f"INSERT OR REPLACE INTO {table} VALUES ({placeholders})", row
                    )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "path", (".", "..", "a//b", "a/", "/a", "a/./b", "a/../b", "a\\b", "a\x00b")
)
def test_artifact_path_database_check_rejects_noncanonical_paths(path: str) -> None:
    connection = _connection()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                "INSERT INTO artifact_refs VALUES (?,?,?,?,?,?,?)",
                (HEX_A, "snapshot", HEX_B, 1, 1, None, path),
            )
    finally:
        connection.close()


def test_artifact_path_is_unique_and_valid_nested_path_passes() -> None:
    connection = _connection()
    try:
        connection.execute(
            "INSERT INTO artifact_refs VALUES (?,?,?,?,?,?,?)",
            (HEX_A, "snapshot", HEX_B, 1, 1, None, "a/b/c.bin"),
        )
        with pytest.raises(
            sqlite3.IntegrityError, match="immutable-conflict:artifact_refs"
        ):
            connection.execute(
                "INSERT INTO artifact_refs VALUES (?,?,?,?,?,?,?)",
                (HEX_C, "snapshot", HEX_A, 1, 1, None, "a/b/c.bin"),
            )
    finally:
        connection.close()


@pytest.mark.parametrize("kind", ("report", "document"))
def test_opportunity_snapshot_must_reference_snapshot_artifact(kind: str) -> None:
    connection = _connection()
    try:
        repository, run_id = _registered(connection)
        artifact_id = repository.insert_artifact_ref(
            kind=kind,
            content_sha256=HEX_B,
            byte_size=1,
            storage_relpath=f"{kind}/item.bin",
            created_at_us=1,
        )
        with pytest.raises(sqlite3.IntegrityError, match="opportunity:snapshot-kind"):
            connection.execute(
                "INSERT INTO opportunities VALUES (?,?,?,?,?,?)",
                (HEX_C, run_id, "stocks", "AAPL", 100, artifact_id),
            )
    finally:
        connection.close()


def test_account_contract_is_loaded_from_persisted_manifest() -> None:
    connection = _connection()
    try:
        repository, run_id = _registered(connection)
        expected = {
            (stream, arm)
            for stream, arms in (
                ("polymarket", ("AI", "DETERMINISTIC", "CASH")),
                ("stocks", ("AI", "DETERMINISTIC", "CASH", "EXPOSURE_MATCHED_PASSIVE")),
            )
            for arm in arms
        }
        for stream, arm in expected:
            repository.create_account(run_id=run_id, stream=stream, arm=arm)
        assert {
            (row[0], row[1], row[2], row[3])
            for row in connection.execute(
                "SELECT stream,arm,currency,initial_capital_microdollars FROM accounts"
            )
        } == {(stream, arm, "USD", 10_000_000_000) for stream, arm in expected}
        with pytest.raises(ValueError, match="arm"):
            repository.create_account(run_id=run_id, stream="polymarket", arm="NOPE")
    finally:
        connection.close()


def test_account_rejects_wrong_compatibility_values_on_first_insert() -> None:
    connection = _connection()
    try:
        repository, run_id = _registered(connection)
        with pytest.raises(ValueError, match="manifest account contract"):
            repository.create_account(
                run_id=run_id,
                stream="stocks",
                arm="AI",
                currency="EUR",
                initial_capital=MicroDollars(9_000_000_000),
            )
        assert connection.execute("SELECT count(*) FROM accounts").fetchone()[0] == 0
    finally:
        connection.close()


def test_account_rejects_tampered_or_example_persisted_manifest() -> None:
    connection = _connection()
    try:
        repository, run_id = _registered(connection)
        connection.execute("DROP TRIGGER manifests_immutable_update")
        connection.execute("UPDATE manifests SET canonical_json = ?", (b"{}",))
        with pytest.raises(ValueError, match="persisted manifest"):
            repository.create_account(run_id=run_id, stream="stocks", arm="AI")
    finally:
        connection.close()


def test_opportunity_identity_has_no_eligibility_episode() -> None:
    connection = _connection()
    try:
        repository, run_id = _registered(connection)
        first = repository.insert_opportunity(
            run_id=run_id,
            stream="stocks",
            subject_id="AAPL",
            scheduled_for_us=100,
        )
        assert first == repository.insert_opportunity(
            run_id=run_id,
            stream="stocks",
            subject_id="AAPL",
            scheduled_for_us=100,
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(opportunities)")
        }
        assert "eligibility_episode" not in columns
        with pytest.raises(TypeError, match="unexpected keyword"):
            repository.insert_opportunity(
                run_id=run_id,
                stream="stocks",
                subject_id="AAPL",
                scheduled_for_us=100,
                eligibility_episode=1,  # type: ignore[call-arg]
            )
    finally:
        connection.close()


SECONDARY_UNIQUE_IDENTITIES = {
    "runs": (("manifest_id", "run_window_id", "dependency_lock_sha256"),),
    "accounts": (("run_id", "stream", "arm", "currency"),),
    "opportunities": (("run_id", "stream", "subject_id", "scheduled_for_us"),),
    "observations": (("supersedes_id",),),
    "artifact_refs": (("storage_relpath",),),
}


def test_every_secondary_unique_identity_has_insert_conflict_guard() -> None:
    connection = _connection()
    try:
        discovered: dict[str, set[tuple[str, ...]]] = {}
        for table in TABLES:
            identities = set()
            for index in connection.execute(f"PRAGMA index_list({table})"):
                if index[2] and index[3] == "u":
                    identities.add(
                        tuple(
                            row[2]
                            for row in connection.execute(
                                f"PRAGMA index_info({index[1]})"
                            )
                        )
                    )
            if identities:
                discovered[table] = identities
        assert discovered == {
            table: set(identities)
            for table, identities in SECONDARY_UNIQUE_IDENTITIES.items()
        }
        for table, identities in discovered.items():
            trigger = " ".join(
                connection.execute(
                    "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",
                    (f"{table}_immutable_conflict",),
                )
                .fetchone()[0]
                .split()
            )
            primary_key = connection.execute(f"PRAGMA table_info({table})").fetchone()[
                1
            ]
            assert f"old.{primary_key} = NEW.{primary_key}" in trigger
            for identity in identities:
                guard = " AND ".join(
                    f"old.{column} IS NEW.{column}" for column in identity
                )
                assert guard in trigger
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("table", "columns", "prepare"),
    (
        (
            "runs",
            ("run_id",),
            lambda connection, ids: (HEX_C,),
        ),
        (
            "accounts",
            ("account_id",),
            lambda connection, ids: (HEX_C,),
        ),
        (
            "opportunities",
            ("opportunity_id",),
            lambda connection, ids: (HEX_C,),
        ),
        (
            "artifact_refs",
            ("artifact_id",),
            lambda connection, ids: (
                hashlib.sha256(b"different-artifact").hexdigest(),
            ),
        ),
    ),
)
def test_insert_or_replace_rejects_different_primary_id_on_secondary_unique_identity(
    table: str,
    columns: tuple[str, ...],
    prepare: Any,
) -> None:
    connection = _connection()
    try:
        _repository, ids = _populated_core(connection)
        connection.execute("PRAGMA recursive_triggers = OFF")
        table_columns = [
            row[1] for row in connection.execute(f"PRAGMA table_info({table})")
        ]
        row = list(
            connection.execute(
                f"SELECT * FROM {table} WHERE {table_columns[0]} = ?", (ids[table],)
            ).fetchone()
        )
        for column, value in zip(columns, prepare(connection, ids), strict=True):
            row[table_columns.index(column)] = value
        placeholders = ",".join("?" for _ in table_columns)
        with pytest.raises(sqlite3.IntegrityError, match=f"immutable-conflict:{table}"):
            connection.execute(
                f"INSERT OR REPLACE INTO {table} VALUES ({placeholders})", row
            )
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
        assert connection.execute(
            f"SELECT 1 FROM {table} WHERE {table_columns[0]} = ?", (ids[table],)
        ).fetchone()
    finally:
        connection.close()


def test_replace_rejects_different_observation_id_for_existing_successor() -> None:
    connection = _connection()
    try:
        repository, ids = _populated_core(connection)
        original = repository._stored_observation(ids["observations"])
        successor = correct_observation(
            original,
            retrieved_at_us=original.retrieved_at_us + 1,
            content_sha256=hashlib.sha256(b"successor").hexdigest(),
            supersedes_id=original.observation_id,
        )
        successor_id = repository.insert_observation(
            successor, logical_key=HEX_A, run_id=ids["runs"]
        )
        replacement = list(
            connection.execute(
                "SELECT * FROM observations WHERE observation_id = ?", (successor_id,)
            ).fetchone()
        )
        replacement[0] = hashlib.sha256(b"different-successor-id").hexdigest()
        replacement[7] = hashlib.sha256(b"different-successor-content").hexdigest()
        connection.execute("PRAGMA recursive_triggers = OFF")
        with pytest.raises(
            sqlite3.IntegrityError, match="immutable-conflict:observations"
        ):
            connection.execute(
                "INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?,?,?,?)",
                replacement,
            )
        assert (
            connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 2
        )
        assert connection.execute(
            "SELECT 1 FROM observations WHERE observation_id = ?", (successor_id,)
        ).fetchone()
    finally:
        connection.close()


def test_concurrent_identical_insert_converges(tmp_path: Path) -> None:
    database = tmp_path / "core.sqlite3"
    setup = _connection(str(database))
    setup.execute("PRAGMA journal_mode=WAL")
    setup.close()
    barrier = threading.Barrier(2)

    def insert() -> str:
        connection = sqlite3.connect(database, isolation_level=None, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA recursive_triggers=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        try:
            barrier.wait(timeout=5)
            return CoreRepository(
                connection, expected_busy_timeout_ms=5000
            ).insert_manifest(_manifest())
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result() for future in (pool.submit(insert), pool.submit(insert))
        ]
    assert results[0] == results[1]
    check = sqlite3.connect(database)
    try:
        assert check.execute("SELECT count(*) FROM manifests").fetchone()[0] == 1
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()

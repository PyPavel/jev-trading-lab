from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from jev_trading_lab.core.ids import canonical_hash, canonical_json_bytes
from jev_trading_lab.persistence.db import connect_database
from jev_trading_lab.persistence.journal import (
    AppendOnlyJournal,
    JournalConflictError,
    JournalEntity,
    JournalEvent,
    JournalRepositoryError,
    make_journal_entity,
    make_journal_event,
    register_journal_sql_functions,
)
from jev_trading_lab.persistence.journal_sql import jev_journal_entity_id
from jev_trading_lab.persistence.migrations import apply_migrations, load_migrations

HEX_A = "a" * 64
HEX_B = "b" * 64
HEX_C = "c" * 64
TABLES = ("journal_entities", "journal_events")


def _hex(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _connection(path: str | None = None) -> sqlite3.Connection:
    if path is None:
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
    register_journal_sql_functions(connection)
    apply_migrations(connection, clock=lambda: 1_700_000_000_000_000)
    return connection


def _insert_run(connection: sqlite3.Connection, label: str = "one") -> str:
    manifest_id = _hex(f"manifest-{label}")
    window_id = _hex(f"window-{label}")
    run_id = _hex(f"run-{label}")
    connection.execute(
        "INSERT INTO manifests VALUES (?,?,?,?)", (manifest_id, b"{}", 1, 1)
    )
    connection.execute(
        "INSERT INTO run_windows VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            window_id,
            b"{}",
            0,
            7_257_600_000_000,
            7_257_600_000_001,
            "XNYS",
            "America/New_York",
            "test",
            _hex(f"lock-{label}"),
            _hex(f"artifact-{label}"),
            "test",
            _hex(f"rule-{label}"),
        ),
    )
    connection.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
        (run_id, manifest_id, window_id, "phase1", "paper", _hex(label), 1),
    )
    return run_id


def _entity(
    run_id: str,
    *,
    entity_type: str = "order_intent",
    logical_identity: str = HEX_A,
    created_at_us: int = 100,
    payload: object | None = None,
) -> JournalEntity:
    return make_journal_entity(
        run_id=run_id,
        entity_type=entity_type,
        logical_identity=logical_identity,
        created_at_us=created_at_us,
        payload={"value": 1} if payload is None else payload,
    )


def _event(
    entity_id: str,
    sequence: int,
    *,
    occurred_at_us: int | None = None,
    event_type: str = "recorded",
    payload: object | None = None,
    supersedes_event_id: str | None = None,
) -> JournalEvent:
    return make_journal_event(
        entity_id=entity_id,
        event_type=event_type,
        sequence=sequence,
        occurred_at_us=100 + sequence if occurred_at_us is None else occurred_at_us,
        payload={"sequence": sequence} if payload is None else payload,
        supersedes_event_id=supersedes_event_id,
    )


def _raw_entity(
    connection: sqlite3.Connection,
    run_id: str,
    label: str,
    *,
    entity_type: str = "order_intent",
    logical_identity: str | None = None,
) -> str:
    record = make_journal_entity(
        run_id=run_id,
        entity_type=entity_type,
        logical_identity=logical_identity or _hex(f"logical-{label}"),
        created_at_us=100,
        payload={"entity": label},
    )
    connection.execute(
        "INSERT INTO journal_entities VALUES (?,?,?,?,?,?,?)",
        tuple(getattr(record, field) for field in JournalEntity.__dataclass_fields__),
    )
    return record.entity_id


def _raw_event(
    connection: sqlite3.Connection,
    entity_id: str,
    sequence: int,
    *,
    label: str | None = None,
    supersedes_event_id: str | None = None,
) -> str:
    label = label or f"{entity_id}-{sequence}"
    record = make_journal_event(
        entity_id=entity_id,
        event_type="recorded",
        sequence=sequence,
        occurred_at_us=1_000 + sequence,
        payload={"event": label},
        supersedes_event_id=supersedes_event_id,
    )
    connection.execute(
        "INSERT INTO journal_events VALUES (?,?,?,?,?,?,?,?)",
        tuple(getattr(record, field) for field in JournalEvent.__dataclass_fields__),
    )
    return record.event_id


def _manual_entity_values(
    run_id: str, label: str, *, payload: bytes = b'{"value":1}'
) -> tuple[object, ...]:
    logical_identity = _hex(f"logical-{label}")
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    entity_id = canonical_hash(
        "journal-entity-v1",
        {
            "run_id": run_id,
            "entity_type": "order_intent",
            "logical_identity": logical_identity,
            "created_at_us": 100,
            "payload_sha256": payload_sha256,
        },
    )
    return (
        entity_id,
        run_id,
        "order_intent",
        logical_identity,
        100,
        payload_sha256,
        payload,
    )


def _manual_event_values(
    entity_id: str,
    sequence: int,
    *,
    payload: bytes,
    supersedes_event_id: str | None = None,
) -> tuple[object, ...]:
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    event_id = canonical_hash(
        "journal-event-v1",
        {
            "entity_id": entity_id,
            "event_type": "recorded",
            "sequence": sequence,
            "occurred_at_us": 1_000 + sequence,
            "payload_sha256": payload_sha256,
            "supersedes_event_id": supersedes_event_id,
        },
    )
    return (
        event_id,
        entity_id,
        "recorded",
        sequence,
        1_000 + sequence,
        payload_sha256,
        payload,
        supersedes_event_id,
    )


def test_registered_sql_functions_are_exact_and_fail_closed(tmp_path: Path) -> None:
    database = (tmp_path / "private" / "journal.sqlite3").absolute()
    for attempt in range(2):
        connection = connect_database(
            database,
            busy_timeout_ms=5000,
            mount_resolver=lambda _path: "ext4",
        )
        try:
            payload = canonical_json_bytes({"attempt": attempt})
            payload_sha256 = hashlib.sha256(payload).hexdigest()
            entity_id = canonical_hash(
                "journal-entity-v1",
                {
                    "run_id": HEX_A,
                    "entity_type": "entity",
                    "logical_identity": HEX_B,
                    "created_at_us": 0,
                    "payload_sha256": payload_sha256,
                },
            )
            assert (
                connection.execute("SELECT jev_sha256(?)", (payload,)).fetchone()[0]
                == payload_sha256
            )
            assert (
                connection.execute(
                    "SELECT jev_is_canonical_json(?)", (payload,)
                ).fetchone()[0]
                == 1
            )
            assert (
                connection.execute(
                    "SELECT jev_journal_entity_id(?,?,?,?,?)",
                    (HEX_A, "entity", HEX_B, 0, payload_sha256),
                ).fetchone()[0]
                == entity_id
            )
            assert connection.execute(
                "SELECT jev_journal_event_id(?,?,?,?,?,?)",
                (entity_id, "event", 0, 1, payload_sha256, None),
            ).fetchone()[0] == canonical_hash(
                "journal-event-v1",
                {
                    "entity_id": entity_id,
                    "event_type": "event",
                    "sequence": 0,
                    "occurred_at_us": 1,
                    "payload_sha256": payload_sha256,
                    "supersedes_event_id": None,
                },
            )
            assert (
                connection.execute("SELECT jev_sha256(?)", ("not-bytes",)).fetchone()[0]
                is None
            )
            assert (
                connection.execute(
                    "SELECT jev_is_canonical_json(?)", (b"not-json",)
                ).fetchone()[0]
                == 0
            )
            assert (
                jev_journal_entity_id(HEX_A, "entity", HEX_B, True, payload_sha256)
                is None
            )
            assert connection.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        finally:
            connection.close()


def test_reopened_raw_sqlite_connection_fails_closed_without_callbacks(
    tmp_path: Path,
) -> None:
    database = tmp_path / "raw-reopen.sqlite3"
    setup = _connection(str(database))
    run_id = _insert_run(setup)
    valid = _manual_entity_values(run_id, "raw-reopen")
    setup.close()

    raw = sqlite3.connect(database, isolation_level=None)
    try:
        raw.execute("PRAGMA foreign_keys = ON")
        raw.execute("PRAGMA trusted_schema = OFF")
        with pytest.raises(sqlite3.OperationalError, match="no such collation"):
            raw.execute("INSERT INTO journal_entities VALUES (?,?,?,?,?,?,?)", valid)
    finally:
        raw.close()


def test_direct_sql_authenticates_canonical_payload_hash_and_entity_id() -> None:
    connection = _connection()
    try:
        run_id = _insert_run(connection)
        valid = _manual_entity_values(run_id, "valid")
        connection.execute("INSERT INTO journal_entities VALUES (?,?,?,?,?,?,?)", valid)

        malformed = list(_manual_entity_values(run_id, "malformed"))
        malformed[-1] = b"not-json"
        malformed[-2] = hashlib.sha256(malformed[-1]).hexdigest()
        malformed[0] = _hex("arbitrary-malformed-id")
        with pytest.raises(sqlite3.IntegrityError, match="authentication:payload"):
            connection.execute(
                "INSERT INTO journal_entities VALUES (?,?,?,?,?,?,?)", malformed
            )

        wrong_hash = list(_manual_entity_values(run_id, "wrong-hash"))
        wrong_hash[-2] = HEX_A
        with pytest.raises(sqlite3.IntegrityError, match="authentication:hash"):
            connection.execute(
                "INSERT INTO journal_entities VALUES (?,?,?,?,?,?,?)", wrong_hash
            )

        wrong_id = list(_manual_entity_values(run_id, "wrong-id"))
        wrong_id[0] = HEX_B
        with pytest.raises(sqlite3.IntegrityError, match="authentication:id"):
            connection.execute(
                "INSERT INTO journal_entities VALUES (?,?,?,?,?,?,?)", wrong_id
            )
    finally:
        connection.close()


def test_direct_sql_authenticates_events_and_changed_payload_bytes() -> None:
    connection = _connection()
    try:
        run_id = _insert_run(connection)
        entity = _manual_entity_values(run_id, "events")
        connection.execute(
            "INSERT INTO journal_entities VALUES (?,?,?,?,?,?,?)", entity
        )
        entity_id = str(entity[0])
        first = _manual_event_values(entity_id, 0, payload=b'{"value":1}')
        connection.execute("INSERT INTO journal_events VALUES (?,?,?,?,?,?,?,?)", first)

        wrong_hash = list(
            _manual_event_values(
                entity_id,
                1,
                payload=b'{"value":2}',
                supersedes_event_id=str(first[0]),
            )
        )
        wrong_hash[-3] = HEX_A
        wrong_hash[0] = HEX_B
        with pytest.raises(sqlite3.IntegrityError, match="authentication:hash"):
            connection.execute(
                "INSERT INTO journal_events VALUES (?,?,?,?,?,?,?,?)", wrong_hash
            )

        wrong_id = list(
            _manual_event_values(
                entity_id,
                1,
                payload=b'{"value":2}',
                supersedes_event_id=str(first[0]),
            )
        )
        wrong_id[0] = HEX_C
        with pytest.raises(sqlite3.IntegrityError, match="authentication:id"):
            connection.execute(
                "INSERT INTO journal_events VALUES (?,?,?,?,?,?,?,?)", wrong_id
            )

        identical_payload_fake_hash = list(
            _manual_event_values(
                entity_id,
                1,
                payload=bytes(first[-2]),
                supersedes_event_id=str(first[0]),
            )
        )
        identical_payload_fake_hash[-3] = HEX_C
        identical_payload_fake_hash[0] = canonical_hash(
            "journal-event-v1",
            {
                "entity_id": entity_id,
                "event_type": "recorded",
                "sequence": 1,
                "occurred_at_us": 1_001,
                "payload_sha256": HEX_C,
                "supersedes_event_id": first[0],
            },
        )
        with pytest.raises(
            sqlite3.IntegrityError, match="(?:authentication:hash|supersession:payload)"
        ):
            connection.execute(
                "INSERT INTO journal_events VALUES (?,?,?,?,?,?,?,?)",
                identical_payload_fake_hash,
            )

        valid = _manual_event_values(
            entity_id,
            1,
            payload=b'{"value":2}',
            supersedes_event_id=str(first[0]),
        )
        connection.execute("INSERT INTO journal_events VALUES (?,?,?,?,?,?,?,?)", valid)
    finally:
        connection.close()


def test_update_delete_abort() -> None:
    connection = _connection()
    try:
        run_id = _insert_run(connection)
        entity_id = _raw_entity(connection, run_id, "immutable")
        _raw_event(connection, entity_id, 0)
        for table in TABLES:
            key = connection.execute(f"PRAGMA table_info({table})").fetchone()[1]
            with pytest.raises(sqlite3.IntegrityError, match=f"immutable:{table}"):
                connection.execute(f"UPDATE {table} SET {key} = {key}")
            with pytest.raises(sqlite3.IntegrityError, match=f"immutable:{table}"):
                connection.execute(f"DELETE FROM {table}")
    finally:
        connection.close()


def test_two_node_back_edge_rejected_by_immutable_conflict() -> None:
    connection = _connection()
    try:
        run_id = _insert_run(connection)
        entity_id = _raw_entity(connection, run_id, "cycle")
        first = _raw_event(connection, entity_id, 0, label="first")
        second = _raw_event(
            connection, entity_id, 1, label="second", supersedes_event_id=first
        )
        first_row = list(
            connection.execute(
                "SELECT * FROM journal_events WHERE event_id = ?", (first,)
            ).fetchone()
        )
        first_row[-1] = second
        connection.execute("PRAGMA recursive_triggers = OFF")
        with pytest.raises(
            sqlite3.IntegrityError, match="immutable-conflict:journal_events"
        ):
            connection.execute(
                "INSERT OR REPLACE INTO journal_events VALUES (?,?,?,?,?,?,?,?)",
                first_row,
            )
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' "
            "AND name='journal_events_validate_supersession'"
        ).fetchone()[0]
        assert "WITH RECURSIVE" not in trigger_sql

        journal = AppendOnlyJournal(connection, expected_busy_timeout_ms=5000)
        forged = object.__new__(JournalEvent)
        for field, value in zip(
            JournalEvent.__dataclass_fields__, first_row, strict=True
        ):
            object.__setattr__(forged, field, value)
        with pytest.raises((ValueError, JournalConflictError)):
            journal.append_event(forged)
    finally:
        connection.close()


def test_thousand_link_back_edge_rejected_by_immutable_conflict() -> None:
    connection = _connection()
    try:
        run_id = _insert_run(connection)
        entity_id = _raw_entity(connection, run_id, "long-cycle")
        first = _raw_event(connection, entity_id, 0, label="chain-0")
        predecessor = first
        for sequence in range(1, 1_001):
            predecessor = _raw_event(
                connection,
                entity_id,
                sequence,
                label=f"chain-{sequence}",
                supersedes_event_id=predecessor,
            )
        root = list(
            connection.execute(
                "SELECT * FROM journal_events WHERE event_id = ?", (first,)
            ).fetchone()
        )
        root[-1] = predecessor
        connection.execute("PRAGMA recursive_triggers = OFF")
        with pytest.raises(
            sqlite3.IntegrityError, match="immutable-conflict:journal_events"
        ):
            connection.execute(
                "INSERT OR REPLACE INTO journal_events VALUES (?,?,?,?,?,?,?,?)", root
            )
        assert (
            connection.execute("SELECT count(*) FROM journal_events").fetchone()[0]
            == 1001
        )
    finally:
        connection.close()


def test_one_successor_same_identity() -> None:
    connection = _connection()
    try:
        run_one = _insert_run(connection, "one")
        run_two = _insert_run(connection, "two")
        original_entity = _raw_entity(connection, run_one, "original")
        original = _raw_event(connection, original_entity, 0, label="original")
        _raw_event(
            connection,
            original_entity,
            1,
            label="successor",
            supersedes_event_id=original,
        )
        with pytest.raises(sqlite3.IntegrityError):
            _raw_event(
                connection,
                original_entity,
                2,
                label="branch",
                supersedes_event_id=original,
            )

        variants = (
            (run_one, "other_entity", _hex("logical-original"), "entity"),
            (run_two, "order_intent", _hex("logical-original"), "run"),
            (run_one, "other_type", _hex("logical-original"), "type"),
            (run_one, "order_intent", _hex("other-logical"), "logical"),
        )
        for index, (run_id, entity_type, logical, label) in enumerate(variants):
            source = _raw_entity(connection, run_one, f"source-{label}")
            predecessor = _raw_event(
                connection, source, 0, label=f"predecessor-{index}"
            )
            different = _raw_entity(
                connection,
                run_id,
                label,
                entity_type=entity_type,
                logical_identity=logical,
            )
            with pytest.raises(sqlite3.IntegrityError, match="supersession:entity"):
                _raw_event(
                    connection,
                    different,
                    1,
                    label=f"cross-{label}",
                    supersedes_event_id=predecessor,
                )
    finally:
        connection.close()


def test_replace_primary_and_secondary_unique_rejected_with_recursion_off() -> None:
    connection = _connection()
    try:
        run_id = _insert_run(connection)
        entity_id = _raw_entity(connection, run_id, "replace")
        first = _raw_event(connection, entity_id, 0, label="replace-0")
        second = _raw_event(
            connection,
            entity_id,
            1,
            label="replace-1",
            supersedes_event_id=first,
        )
        connection.execute("PRAGMA recursive_triggers = OFF")

        entity_row = list(
            connection.execute("SELECT * FROM journal_entities").fetchone()
        )
        entity_row[-1] = b'{"changed":true}'
        with pytest.raises(
            sqlite3.IntegrityError, match="immutable-conflict:journal_entities"
        ):
            connection.execute(
                "INSERT OR REPLACE INTO journal_entities VALUES (?,?,?,?,?,?,?)",
                entity_row,
            )
        entity_secondary = entity_row.copy()
        entity_secondary[0] = _hex("replacement-entity")
        entity_secondary[-2] = hashlib.sha256(entity_secondary[-1]).hexdigest()
        with pytest.raises(
            sqlite3.IntegrityError, match="immutable-conflict:journal_entities"
        ):
            connection.execute(
                "INSERT OR REPLACE INTO journal_entities VALUES (?,?,?,?,?,?,?)",
                entity_secondary,
            )

        event_row = list(
            connection.execute(
                "SELECT * FROM journal_events WHERE event_id = ?", (second,)
            ).fetchone()
        )
        event_row[-2] = b'{"changed":true}'
        with pytest.raises(
            sqlite3.IntegrityError, match="immutable-conflict:journal_events"
        ):
            connection.execute(
                "INSERT OR REPLACE INTO journal_events VALUES (?,?,?,?,?,?,?,?)",
                event_row,
            )
        for label, change in (
            ("sequence-unique", {}),
            ("successor-unique", {3: 2}),
        ):
            alternate = event_row.copy()
            alternate[0] = _hex(label)
            for index, value in change.items():
                alternate[index] = value
            alternate[-2] = json.dumps({"label": label}).encode()
            alternate[-3] = hashlib.sha256(alternate[-2]).hexdigest()
            with pytest.raises(
                sqlite3.IntegrityError, match="immutable-conflict:journal_events"
            ):
                connection.execute(
                    "INSERT OR REPLACE INTO journal_events VALUES (?,?,?,?,?,?,?,?)",
                    alternate,
                )
        assert (
            connection.execute("SELECT count(*) FROM journal_entities").fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT count(*) FROM journal_events").fetchone()[0] == 2
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("table", "row"),
    (
        (
            "journal_entities",
            (HEX_A + "\x00", HEX_B, "entity", HEX_C, 0, HEX_A, b"{}"),
        ),
        (
            "journal_entities",
            (HEX_A, HEX_B, "Bad-Slug", HEX_C, 0, HEX_A, b"{}"),
        ),
        (
            "journal_entities",
            (HEX_A, HEX_B, "entity", HEX_C, -1, HEX_A, b"{}"),
        ),
        (
            "journal_entities",
            (HEX_A, HEX_B, "entity", HEX_C, 0, HEX_A, b"x" * 262_145),
        ),
        (
            "journal_events",
            (HEX_A, HEX_B, "bad status", 0, 0, HEX_C, b"{}", None),
        ),
        (
            "journal_events",
            (HEX_A, HEX_B, "event", b"0", 0, HEX_C, b"{}", None),
        ),
        (
            "journal_events",
            (HEX_A, HEX_B, "event", 0, -62_135_596_800_000_001, HEX_C, b"{}", None),
        ),
    ),
)
def test_direct_sql_rejects_invalid_ids_hash_payload_type_slug_and_timestamp(
    table: str, row: tuple[object, ...]
) -> None:
    connection = _connection()
    try:
        run_id = _insert_run(connection)
        entity_id = _raw_entity(connection, run_id, "valid")
        values = list(row)
        if table == "journal_entities":
            values[1] = run_id
        else:
            values[1] = entity_id
        placeholders = ",".join("?" for _ in values)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f"INSERT INTO {table} VALUES ({placeholders})", values)
    finally:
        connection.close()


def test_factories_require_exact_builtin_types_and_canonical_payload_limit() -> None:
    with pytest.raises(TypeError):
        make_journal_entity(
            run_id=HEX_A,
            entity_type="entity",
            logical_identity=HEX_B,
            created_at_us=True,  # type: ignore[arg-type]
            payload={},
        )
    with pytest.raises(ValueError, match="payload"):
        make_journal_entity(
            run_id=HEX_A,
            entity_type="entity",
            logical_identity=HEX_B,
            created_at_us=0,
            payload={"large": "x" * 262_145},
        )
    with pytest.raises((TypeError, ValueError)):
        make_journal_event(
            entity_id=HEX_A,
            event_type="Bad-Type",
            sequence=0,
            occurred_at_us=0,
            payload={},
        )


def test_tampered_and_forged_records_are_semantically_reconstructed() -> None:
    connection = _connection()
    try:
        run_id = _insert_run(connection)
        journal = AppendOnlyJournal(connection, expected_busy_timeout_ms=5000)
        valid = _entity(run_id)
        forged = object.__new__(JournalEntity)
        for field in JournalEntity.__dataclass_fields__:
            object.__setattr__(forged, field, getattr(valid, field))
        assert journal.append_entity(forged) == valid.entity_id

        tampered = object.__new__(JournalEntity)
        for field in JournalEntity.__dataclass_fields__:
            object.__setattr__(tampered, field, getattr(valid, field))
        object.__setattr__(tampered, "payload_sha256", HEX_C)
        with pytest.raises(ValueError, match="record"):
            journal.append_entity(tampered)
        with pytest.raises(AttributeError):
            object.__setattr__(valid, "_proof", HEX_A)
    finally:
        connection.close()


def test_append_idempotency_and_conflicts() -> None:
    connection = _connection()
    try:
        run_id = _insert_run(connection)
        journal = AppendOnlyJournal(connection, expected_busy_timeout_ms=5000)
        entity = _entity(run_id)
        assert journal.append_entity(entity) == entity.entity_id
        assert journal.append_entity(entity) == entity.entity_id
        first = _event(entity.entity_id, 0)
        assert journal.append_event(first) == first.event_id
        assert journal.append_event(first) == first.event_id

        conflicting_entity = _entity(run_id, payload={"value": 2})
        with pytest.raises(JournalConflictError, match="conflict"):
            journal.append_entity(conflicting_entity)
        conflicting_event = _event(entity.entity_id, 0, payload={"other": True})
        with pytest.raises(JournalConflictError, match="conflict"):
            journal.append_event(conflicting_event)
    finally:
        connection.close()


def test_repository_uses_authoritative_database_predecessor() -> None:
    connection = _connection()
    try:
        run_id = _insert_run(connection)
        journal = AppendOnlyJournal(connection, expected_busy_timeout_ms=5000)
        entity = _entity(run_id)
        journal.append_entity(entity)
        first = _event(entity.entity_id, 0)
        journal.append_event(first)
        second = _event(
            entity.entity_id,
            1,
            supersedes_event_id=first.event_id,
            payload={"changed": True},
        )
        object.__setattr__(first, "payload_sha256", HEX_C)
        assert journal.append_event(second) == second.event_id
    finally:
        connection.close()


def test_repository_transaction_ownership_and_rollback() -> None:
    connection = _connection()
    try:
        run_id = _insert_run(connection)
        journal = AppendOnlyJournal(connection, expected_busy_timeout_ms=5000)
        entity = _entity(run_id)
        connection.execute("BEGIN")
        connection.execute("CREATE TEMP TABLE caller_state(value TEXT)")
        connection.execute("INSERT INTO caller_state VALUES ('keep')")
        with pytest.raises(JournalRepositoryError, match="active caller transaction"):
            journal.append_entity(entity)
        assert connection.in_transaction
        assert (
            connection.execute("SELECT value FROM caller_state").fetchone()[0] == "keep"
        )
        connection.execute("ROLLBACK")

        connection.execute(
            "CREATE TRIGGER reject_entity BEFORE INSERT ON journal_entities "
            "BEGIN SELECT RAISE(ABORT, 'forced'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="forced"):
            journal.append_entity(entity)
        assert not connection.in_transaction
        assert (
            connection.execute("SELECT count(*) FROM journal_entities").fetchone()[0]
            == 0
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
def test_repository_rejects_pragma_drift(pragma: str, drift: str) -> None:
    connection = _connection()
    try:
        run_id = _insert_run(connection)
        journal = AppendOnlyJournal(connection, expected_busy_timeout_ms=5000)
        connection.execute(f"PRAGMA {pragma} = {drift}")
        with pytest.raises(JournalRepositoryError, match=pragma):
            journal.append_entity(_entity(run_id))
        assert (
            connection.execute("SELECT count(*) FROM journal_entities").fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_secure_connection_timeout_is_authoritative(tmp_path: Path) -> None:
    connection = connect_database(
        (tmp_path / "private" / "journal.sqlite3").absolute(),
        busy_timeout_ms=5000,
        mount_resolver=lambda _path: "ext4",
    )
    try:
        apply_migrations(connection, clock=lambda: 1)
        run_id = _insert_run(connection)
        with pytest.raises(ValueError, match="expected_busy_timeout_ms"):
            AppendOnlyJournal(connection, expected_busy_timeout_ms=1234)
        journal = AppendOnlyJournal(connection)
        connection.execute("PRAGMA busy_timeout = 1234")
        with pytest.raises(JournalRepositoryError, match="busy_timeout"):
            journal.append_entity(_entity(run_id))
    finally:
        connection.close()


def test_concurrent_identical_appends_converge(tmp_path: Path) -> None:
    database = tmp_path / "identical.sqlite3"
    setup = _connection(str(database))
    run_id = _insert_run(setup)
    entity = _entity(run_id)
    setup.close()
    barrier = threading.Barrier(2)

    def append() -> str:
        connection = _connection(str(database))
        try:
            barrier.wait(timeout=5)
            return AppendOnlyJournal(
                connection, expected_busy_timeout_ms=5000
            ).append_entity(entity)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result(timeout=10)
            for future in (pool.submit(append), pool.submit(append))
        ]
    assert results == [entity.entity_id, entity.entity_id]
    check = sqlite3.connect(database)
    try:
        assert check.execute("SELECT count(*) FROM journal_entities").fetchone()[0] == 1
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()


def test_concurrent_conflicting_sequence_has_one_winner(tmp_path: Path) -> None:
    database = tmp_path / "conflict.sqlite3"
    setup = _connection(str(database))
    run_id = _insert_run(setup)
    journal = AppendOnlyJournal(setup, expected_busy_timeout_ms=5000)
    entity = _entity(run_id)
    journal.append_entity(entity)
    setup.close()
    events = (
        _event(entity.entity_id, 0, payload={"candidate": "a"}),
        _event(entity.entity_id, 0, payload={"candidate": "b"}),
    )
    barrier = threading.Barrier(2)

    def append(event: JournalEvent) -> tuple[str, str]:
        connection = _connection(str(database))
        try:
            barrier.wait(timeout=5)
            try:
                result = AppendOnlyJournal(
                    connection, expected_busy_timeout_ms=5000
                ).append_event(event)
            except JournalConflictError as error:
                return "conflict", str(error)
            return "ok", result
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [
            future.result(timeout=10)
            for future in (
                pool.submit(append, events[0]),
                pool.submit(append, events[1]),
            )
        ]
    assert sorted(item[0] for item in outcomes) == ["conflict", "ok"]


def test_migration_003_chain_guards_and_002_unchanged() -> None:
    migrations = load_migrations()
    assert [(item.version, item.name) for item in migrations] == [
        (1, "001_migration_metadata.sql"),
        (2, "002_core_entities.sql"),
        (3, "003_append_only_guards.sql"),
    ]
    migration_003 = migrations[2].sql
    assert "CREATE TRIGGER main.manifests" not in migration_003
    assert "CREATE TRIGGER main.observations" not in migration_003
    connection = _connection()
    try:
        trigger_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger'"
            )
        }
        assert {
            f"{table}_immutable_{operation}"
            for table in TABLES
            for operation in ("update", "delete")
        } <= trigger_names
        run_id = _insert_run(connection)
        with pytest.raises(sqlite3.IntegrityError, match="immutable:runs"):
            connection.execute(
                "UPDATE runs SET run_id=run_id WHERE run_id=?", (run_id,)
            )
    finally:
        connection.close()

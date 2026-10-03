"""Transaction-owning append-only repository for immutable core records."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import PurePosixPath

from jev_trading_lab.core.ids import canonical_hash
from jev_trading_lab.core.manifest import (
    Manifest,
    parse_manifest_bytes,
    revalidate_manifest,
)
from jev_trading_lab.core.observations import ObservationMetadata, ObservationStatus
from jev_trading_lab.core.run_window import RunWindow, revalidate_run_window
from jev_trading_lab.core.time import MAX_DATETIME_US, MIN_DATETIME_US, validate_utc_us
from jev_trading_lab.core.values import MAX_INT64, MicroDollars

_HEX = frozenset("0123456789abcdef")
_STREAM_ARMS = {
    "polymarket": frozenset({"AI", "DETERMINISTIC", "CASH"}),
    "stocks": frozenset({"AI", "DETERMINISTIC", "CASH", "EXPOSURE_MATCHED_PASSIVE"}),
}
_ARTIFACT_KINDS = frozenset(
    {
        "provider_payload",
        "snapshot",
        "document",
        "evidence_manifest",
        "calendar",
        "report",
        "other",
    }
)
_PRAGMA_CONTRACT = (
    ("foreign_keys", 1),
    ("recursive_triggers", 1),
    ("trusted_schema", 0),
)


class CoreRepositoryError(RuntimeError):
    """Raised when a repository connection violates its write contract."""


def _hex64(value: str, *, name: str) -> str:
    if type(value) is not str or len(value) != 64 or not set(value) <= _HEX:
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def _text(value: str, *, name: str, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty bounded string")
    return value


def _timestamp(value: int, *, name: str, post_epoch: bool = False) -> int:
    try:
        result = validate_utc_us(value)
    except (TypeError, ValueError) as error:
        raise type(error)(f"{name}: {error}") from error
    if post_epoch and result < 0:
        raise ValueError(f"{name} cannot predate the Unix epoch")
    return result


def _nonnegative_int(value: int, *, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be a built-in integer")
    if not 0 <= value <= MAX_INT64:
        raise ValueError(f"{name} must be between 0 and {MAX_INT64}")
    return value


def _bare_id(value: str, *, prefix: str, name: str) -> str:
    expected = f"{prefix}:"
    if type(value) is not str or not value.startswith(expected):
        raise ValueError(f"{name} has an invalid namespace")
    return _hex64(value.removeprefix(expected), name=name)


class CoreRepository:
    """Persist minimum immutable records using short owned transactions."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be sqlite3.Connection")
        if connection.isolation_level is not None:
            raise ValueError(
                "repository connection must use autocommit isolation_level=None"
            )
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._connection = connection
        self._clock = clock or (lambda: int(datetime.now(UTC).timestamp() * 1_000_000))

    def _created_at_us(self) -> int:
        return _timestamp(self._clock(), name="created_at_us", post_epoch=True)

    def _verify_write_boundary(self) -> None:
        if self._connection.isolation_level is not None:
            raise CoreRepositoryError(
                "repository connection isolation_level drift: expected autocommit"
            )
        if self._connection.in_transaction:
            raise CoreRepositoryError(
                "cannot write inside an active caller transaction"
            )
        for pragma, expected in _PRAGMA_CONTRACT:
            row = self._connection.execute(f"PRAGMA {pragma}").fetchone()
            actual = None if row is None else row[0]
            if actual != expected:
                raise CoreRepositoryError(
                    f"repository connection PRAGMA {pragma} drift: "
                    f"expected {expected}, received {actual!r}"
                )

    @contextmanager
    def _write(self) -> Iterator[None]:
        self._verify_write_boundary()
        owned = False
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            owned = True
            yield
            self._connection.execute("COMMIT")
            owned = False
        except BaseException:
            if owned and self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def _insert_exact(
        self,
        *,
        table: str,
        key_column: str,
        key: str,
        columns: Sequence[str],
        values: Sequence[object],
    ) -> None:
        existing = self._connection.execute(
            f"SELECT {', '.join(columns)} FROM {table} WHERE {key_column} = ?",
            (key,),
        ).fetchone()
        expected = tuple(values)
        if existing is not None:
            if tuple(existing) != expected:
                raise ValueError(
                    f"existing {table} record conflicts with requested values"
                )
            return
        placeholders = ", ".join("?" for _ in columns)
        self._connection.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
            expected,
        )

    def _insert_manifest(self, manifest: Manifest) -> str:
        trusted = revalidate_manifest(manifest)
        manifest_id = _bare_id(
            trusted.manifest_id, prefix="manifest", name="manifest_id"
        )
        existing = self._connection.execute(
            "SELECT canonical_json, schema_version FROM manifests "
            "WHERE manifest_id = ?",
            (manifest_id,),
        ).fetchone()
        identity = (trusted.canonical_bytes, trusted.schema_version)
        if existing is not None:
            if tuple(existing) != identity:
                raise ValueError(
                    "existing manifests record conflicts with requested values"
                )
            return manifest_id
        self._connection.execute(
            "INSERT INTO manifests "
            "(manifest_id, canonical_json, schema_version, created_at_us) "
            "VALUES (?, ?, ?, ?)",
            (manifest_id, *identity, self._created_at_us()),
        )
        return manifest_id

    def insert_manifest(self, manifest: Manifest) -> str:
        with self._write():
            return self._insert_manifest(manifest)

    def _insert_run_window(self, window: RunWindow, manifest: Manifest) -> str:
        trusted = revalidate_run_window(window, manifest)
        window_id = _bare_id(
            trusted.run_window_id, prefix="run-window", name="run_window_id"
        )
        columns = (
            "run_window_id",
            "canonical_json",
            "enrollment_start_us",
            "enrollment_end_us",
            "maturation_cutoff_us",
            "calendar_name",
            "calendar_timezone",
            "calendar_version",
            "calendar_lock_sha256",
            "calendar_artifact_sha256",
            "tm_rule_version",
            "tm_rule_sha256",
        )
        values = (
            window_id,
            trusted.canonical_bytes,
            trusted.enrollment_start_us,
            trusted.enrollment_end_us,
            trusted.maturation_cutoff_us,
            trusted.calendar_name,
            trusted.timezone,
            trusted.package_version,
            trusted.lock_sha256,
            trusted.artifact_sha256,
            trusted.tm_rule_version,
            trusted.tm_rule_sha256,
        )
        self._insert_exact(
            table="run_windows",
            key_column="run_window_id",
            key=window_id,
            columns=columns,
            values=values,
        )
        return window_id

    def insert_run_window(self, window: RunWindow, manifest: Manifest) -> str:
        trusted_manifest = revalidate_manifest(manifest)
        with self._write():
            return self._insert_run_window(window, trusted_manifest)

    def insert_run(
        self,
        *,
        manifest: Manifest,
        run_window: RunWindow,
        dependency_lock_sha256: str,
        registered_at_us: int,
    ) -> str:
        trusted_manifest = revalidate_manifest(manifest)
        trusted_manifest.assert_activation_candidate()
        lock_hash = _hex64(dependency_lock_sha256, name="dependency_lock_sha256")
        registered = _timestamp(
            registered_at_us, name="registered_at_us", post_epoch=True
        )
        trusted_window = revalidate_run_window(run_window, trusted_manifest)
        with self._write():
            manifest_id = self._insert_manifest(trusted_manifest)
            window_id = self._insert_run_window(trusted_window, trusted_manifest)
            run_id = canonical_hash(
                "run-v1",
                {
                    "manifest_id": manifest_id,
                    "run_window_id": window_id,
                    "phase": "phase1",
                    "mode": "paper",
                    "dependency_lock_sha256": lock_hash,
                },
            )
            columns = (
                "run_id",
                "manifest_id",
                "run_window_id",
                "phase",
                "mode",
                "dependency_lock_sha256",
                "registered_at_us",
            )
            values = (
                run_id,
                manifest_id,
                window_id,
                "phase1",
                "paper",
                lock_hash,
                registered,
            )
            self._insert_exact(
                table="runs",
                key_column="run_id",
                key=run_id,
                columns=columns,
                values=values,
            )
            return run_id

    def get_run(self, run_id: str) -> sqlite3.Row | tuple[object, ...] | None:
        identifier = _hex64(run_id, name="run_id")
        row = self._connection.execute(
            "SELECT * FROM runs WHERE run_id = ?", (identifier,)
        ).fetchone()
        if row is None or isinstance(row, sqlite3.Row):
            return row
        return tuple(row)

    def create_account(
        self,
        *,
        run_id: str,
        stream: str,
        arm: str,
        currency: str | None = None,
        initial_capital: MicroDollars | None = None,
    ) -> str:
        run = _hex64(run_id, name="run_id")
        if type(stream) is not str or stream not in _STREAM_ARMS:
            raise ValueError("stream must be polymarket or stocks")
        with self._write():
            row = self._connection.execute(
                "SELECT runs.manifest_id, manifests.canonical_json "
                "FROM runs JOIN manifests USING (manifest_id) WHERE runs.run_id = ?",
                (run,),
            ).fetchone()
            if row is None:
                raise ValueError("run does not reference a persisted manifest")
            try:
                trusted_manifest = parse_manifest_bytes(row[1])
                trusted_manifest.assert_activation_candidate()
            except (TypeError, ValueError) as error:
                raise ValueError("persisted manifest failed revalidation") from error
            manifest_id = _bare_id(
                trusted_manifest.manifest_id,
                prefix="manifest",
                name="manifest_id",
            )
            if manifest_id != row[0]:
                raise ValueError("persisted manifest ID does not match canonical bytes")
            contract = trusted_manifest.accounts
            allowed_arms = (
                contract.polymarket_arms
                if stream == "polymarket"
                else contract.stock_arms
            )
            if type(arm) is not str or arm not in allowed_arms:
                raise ValueError(f"arm is not allowed for {stream}")
            expected_currency = contract.currency
            expected_capital = contract.initial_capital_microdollars
            if currency is not None and currency != expected_currency:
                raise ValueError("currency conflicts with manifest account contract")
            if initial_capital is not None:
                if type(initial_capital) is not MicroDollars:
                    raise TypeError("initial_capital must be MicroDollars")
                if int(initial_capital) != expected_capital:
                    raise ValueError(
                        "initial_capital conflicts with manifest account contract"
                    )
            account_id = canonical_hash(
                "account-v1",
                {
                    "run_id": run,
                    "stream": stream,
                    "arm": arm,
                    "currency": expected_currency,
                },
            )
            columns = (
                "account_id",
                "run_id",
                "stream",
                "arm",
                "currency",
                "initial_capital_microdollars",
            )
            values = (
                account_id,
                run,
                stream,
                arm,
                expected_currency,
                expected_capital,
            )
            self._insert_exact(
                table="accounts",
                key_column="account_id",
                key=account_id,
                columns=columns,
                values=values,
            )
        return account_id

    def insert_opportunity(
        self,
        *,
        run_id: str,
        stream: str,
        subject_id: str,
        scheduled_for_us: int,
        snapshot_id: str | None = None,
    ) -> str:
        run = _hex64(run_id, name="run_id")
        if type(stream) is not str or stream not in _STREAM_ARMS:
            raise ValueError("stream must be polymarket or stocks")
        subject = _text(subject_id, name="subject_id", maximum=255)
        scheduled = _timestamp(scheduled_for_us, name="scheduled_for_us")
        snapshot = (
            None if snapshot_id is None else _hex64(snapshot_id, name="snapshot_id")
        )
        identity = {
            "run_id": run,
            "stream": stream,
            "subject_id": subject,
            "scheduled_for_us": scheduled,
        }
        opportunity_id = canonical_hash("opportunity-v1", identity)
        columns = (
            "opportunity_id",
            "run_id",
            "stream",
            "subject_id",
            "scheduled_for_us",
            "snapshot_id",
        )
        values = (
            opportunity_id,
            run,
            stream,
            subject,
            scheduled,
            snapshot,
        )
        with self._write():
            self._insert_exact(
                table="opportunities",
                key_column="opportunity_id",
                key=opportunity_id,
                columns=columns,
                values=values,
            )
        return opportunity_id

    def _stored_observation(self, observation_id: str) -> ObservationMetadata:
        pending: list[sqlite3.Row | tuple[object, ...]] = []
        current: str | None = observation_id
        seen: set[str] = set()
        while current is not None:
            if current in seen:
                raise ValueError("persisted observation lineage contains a cycle")
            seen.add(current)
            row = self._connection.execute(
                "SELECT source_uri,published_at_us,retrieved_at_us,status,"
                "content_sha256,supersedes_id FROM observations "
                "WHERE observation_id = ?",
                (current,),
            ).fetchone()
            if row is None:
                raise ValueError("correction predecessor is not persisted")
            pending.append(row)
            current = row[5]
        predecessor: ObservationMetadata | None = None
        for row in reversed(pending):
            predecessor = ObservationMetadata(
                source_uri=row[0],  # type: ignore[arg-type]
                published_at_us=row[1],  # type: ignore[arg-type]
                retrieved_at_us=row[2],  # type: ignore[arg-type]
                status=ObservationStatus(row[3]),
                content_sha256=row[4],  # type: ignore[arg-type]
                supersedes_id=row[5],  # type: ignore[arg-type]
                predecessor=predecessor,
            )
        assert predecessor is not None
        if predecessor.observation_id != observation_id:
            raise ValueError("persisted observation ID does not match stored fields")
        return predecessor

    def insert_observation(
        self,
        observation: ObservationMetadata,
        *,
        logical_key: str,
        run_id: str | None = None,
    ) -> str:
        if type(observation) is not ObservationMetadata:
            raise TypeError("observation must be ObservationMetadata")
        logical = _hex64(logical_key, name="logical_key")
        run = None if run_id is None else _hex64(run_id, name="run_id")
        try:
            constructor_proof = observation._constructor_proof
            supersedes = observation.supersedes_id
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError(
                "observation revalidation failed: wrapper state"
            ) from error
        with self._write():
            try:
                predecessor = (
                    None if supersedes is None else self._stored_observation(supersedes)
                )
                trusted = ObservationMetadata(
                    source_uri=observation.source_uri,
                    published_at_us=observation.published_at_us,
                    retrieved_at_us=observation.retrieved_at_us,
                    status=observation.status,
                    content_sha256=observation.content_sha256,
                    supersedes_id=supersedes,
                    predecessor=predecessor,
                )
            except (TypeError, ValueError) as error:
                raise ValueError("observation revalidation failed") from error
            if trusted.observation_id != constructor_proof:
                raise ValueError("observation revalidation failed: ID mismatch")
            columns = (
                "observation_id",
                "run_id",
                "logical_key",
                "source_uri",
                "published_at_us",
                "retrieved_at_us",
                "status",
                "content_sha256",
                "supersedes_id",
            )
            values = (
                trusted.observation_id,
                run,
                logical,
                trusted.source_uri,
                trusted.published_at_us,
                trusted.retrieved_at_us,
                trusted.status.value,
                trusted.content_sha256,
                trusted.supersedes_id,
            )
            self._insert_exact(
                table="observations",
                key_column="observation_id",
                key=trusted.observation_id,
                columns=columns,
                values=values,
            )
        return trusted.observation_id

    def insert_artifact_ref(
        self,
        *,
        kind: str,
        content_sha256: str,
        byte_size: int,
        storage_relpath: str,
        created_at_us: int,
        source_observation_id: str | None = None,
    ) -> str:
        if type(kind) is not str or kind not in _ARTIFACT_KINDS:
            raise ValueError("kind is not an approved artifact kind")
        content_hash = _hex64(content_sha256, name="content_sha256")
        size = _nonnegative_int(byte_size, name="byte_size")
        created = _timestamp(created_at_us, name="created_at_us", post_epoch=True)
        if type(storage_relpath) is not str:
            raise TypeError("storage path must be a string")
        path = PurePosixPath(storage_relpath)
        if (
            not storage_relpath
            or len(storage_relpath) > 4096
            or "\x00" in storage_relpath
            or "\\" in storage_relpath
            or path.is_absolute()
            or storage_relpath != path.as_posix()
            or any(part in {"", ".", ".."} for part in storage_relpath.split("/"))
        ):
            raise ValueError("storage path must be a normalized relative POSIX path")
        observation_id = (
            None
            if source_observation_id is None
            else _hex64(source_observation_id, name="source_observation_id")
        )
        artifact_id = canonical_hash(
            "artifact-ref-v1",
            {
                "kind": kind,
                "content_sha256": content_hash,
                "storage_relpath": storage_relpath,
                "source_observation_id": observation_id,
            },
        )
        columns = (
            "artifact_id",
            "kind",
            "content_sha256",
            "byte_size",
            "created_at_us",
            "source_observation_id",
            "storage_relpath",
        )
        values = (
            artifact_id,
            kind,
            content_hash,
            size,
            created,
            observation_id,
            storage_relpath,
        )
        with self._write():
            self._insert_exact(
                table="artifact_refs",
                key_column="artifact_id",
                key=artifact_id,
                columns=columns,
                values=values,
            )
        return artifact_id


__all__ = ["CoreRepository", "MAX_DATETIME_US", "MIN_DATETIME_US"]

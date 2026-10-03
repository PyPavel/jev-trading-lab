"""Transaction-owning append-only repository for immutable core records."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import PurePosixPath

from jev_trading_lab.core.ids import canonical_hash
from jev_trading_lab.core.manifest import Manifest, revalidate_manifest
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
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys is None or foreign_keys[0] != 1:
            raise ValueError("repository connection must enforce foreign keys")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._connection = connection
        self._clock = clock or (lambda: int(datetime.now(UTC).timestamp() * 1_000_000))

    def _created_at_us(self) -> int:
        return _timestamp(self._clock(), name="created_at_us", post_epoch=True)

    @contextmanager
    def _write(self) -> Iterator[None]:
        if self._connection.in_transaction:
            raise RuntimeError("cannot write inside an active caller transaction")
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
        currency: str,
        initial_capital: MicroDollars,
    ) -> str:
        run = _hex64(run_id, name="run_id")
        if type(stream) is not str or stream not in _STREAM_ARMS:
            raise ValueError("stream must be polymarket or stocks")
        if type(arm) is not str or arm not in _STREAM_ARMS[stream]:
            raise ValueError(f"arm is not allowed for {stream}")
        if currency != "USD" or type(currency) is not str:
            raise ValueError("currency must be USD")
        if type(initial_capital) is not MicroDollars:
            raise TypeError("initial_capital must be MicroDollars")
        capital = int(initial_capital)
        if capital <= 0:
            raise ValueError("initial_capital must be positive")
        account_id = canonical_hash(
            "account-v1",
            {"run_id": run, "stream": stream, "arm": arm, "currency": currency},
        )
        columns = (
            "account_id",
            "run_id",
            "stream",
            "arm",
            "currency",
            "initial_capital_microdollars",
        )
        values = (account_id, run, stream, arm, currency, capital)
        with self._write():
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
        eligibility_episode: int,
        snapshot_id: str | None = None,
    ) -> str:
        run = _hex64(run_id, name="run_id")
        if type(stream) is not str or stream not in _STREAM_ARMS:
            raise ValueError("stream must be polymarket or stocks")
        subject = _text(subject_id, name="subject_id", maximum=255)
        scheduled = _timestamp(scheduled_for_us, name="scheduled_for_us")
        episode = _nonnegative_int(eligibility_episode, name="eligibility_episode")
        snapshot = (
            None if snapshot_id is None else _hex64(snapshot_id, name="snapshot_id")
        )
        identity = {
            "run_id": run,
            "stream": stream,
            "subject_id": subject,
            "scheduled_for_us": scheduled,
            "eligibility_episode": episode,
        }
        opportunity_id = canonical_hash("opportunity-v1", identity)
        columns = (
            "opportunity_id",
            "run_id",
            "stream",
            "subject_id",
            "scheduled_for_us",
            "eligibility_episode",
            "snapshot_id",
        )
        values = (
            opportunity_id,
            run,
            stream,
            subject,
            scheduled,
            episode,
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
        source = _text(observation.source_uri, name="source_uri", maximum=4096)
        published = observation.published_at_us
        if published is not None:
            published = _timestamp(published, name="published_at_us")
        retrieved = _timestamp(observation.retrieved_at_us, name="retrieved_at_us")
        if published is not None and published > retrieved:
            raise ValueError("published_at_us cannot be after retrieved_at_us")
        if type(observation.status) is not ObservationStatus:
            raise TypeError("status must be an ObservationStatus")
        content_hash = _hex64(observation.content_sha256, name="content_sha256")
        supersedes = (
            None
            if observation.supersedes_id is None
            else _hex64(observation.supersedes_id, name="supersedes_id")
        )
        observation_id = canonical_hash(
            "observation-metadata-v1",
            {
                "source_uri": source,
                "published_at_us": published,
                "retrieved_at_us": retrieved,
                "status": observation.status.value,
                "content_sha256": content_hash,
                "supersedes_id": supersedes,
            },
        )
        if observation.observation_id != observation_id:
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
            observation_id,
            run,
            logical,
            source,
            published,
            retrieved,
            observation.status.value,
            content_hash,
            supersedes,
        )
        with self._write():
            self._insert_exact(
                table="observations",
                key_column="observation_id",
                key=observation_id,
                columns=columns,
                values=values,
            )
        return observation_id

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

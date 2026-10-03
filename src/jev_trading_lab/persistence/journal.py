"""Generic immutable entity and acyclic event journal."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from jev_trading_lab.core.ids import canonical_hash, canonical_json_bytes
from jev_trading_lab.core.time import MAX_DATETIME_US, MIN_DATETIME_US
from jev_trading_lab.persistence.db import (
    SecureConnection,
    _journal_write_scope,
    _reject_user_temp_schema,
)

_HEX = frozenset("0123456789abcdef")
_SLUG = re.compile(r"[a-z](?:[a-z0-9]|_(?=[a-z0-9])){0,63}")
_MAX_PAYLOAD_BYTES = 262_144
_MAX_INT64 = 2**63 - 1
_MAX_BUSY_TIMEOUT_MS = 2_147_483_647
_STATIC_PRAGMA_CONTRACT = (
    ("foreign_keys", 1),
    ("recursive_triggers", 1),
    ("trusted_schema", 0),
    ("journal_mode", "wal"),
    ("synchronous", 2),
)


class JournalRepositoryError(RuntimeError):
    """Raised when the journal connection violates its write contract."""


class JournalConflictError(ValueError):
    """Raised when an append conflicts with immutable stored history."""


@dataclass(frozen=True, slots=True)
class JournalEntity:
    entity_id: str
    run_id: str
    entity_type: str
    logical_identity: str
    created_at_us: int
    payload_sha256: str
    canonical_payload: bytes


@dataclass(frozen=True, slots=True)
class JournalEvent:
    event_id: str
    entity_id: str
    event_type: str
    sequence: int
    occurred_at_us: int
    payload_sha256: str
    canonical_payload: bytes
    supersedes_event_id: str | None


def _hex64(value: object, *, name: str) -> str:
    if type(value) is not str or len(value) != 64 or not set(value) <= _HEX:
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def _slug(value: object, *, name: str) -> str:
    if type(value) is not str or _SLUG.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase slug of at most 64 bytes")
    return value


def _integer(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be a built-in integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _payload(value: object) -> tuple[bytes, str]:
    canonical = canonical_json_bytes(value)
    if not 1 <= len(canonical) <= _MAX_PAYLOAD_BYTES:
        raise ValueError("canonical payload must be between 1 and 262144 bytes")
    return canonical, hashlib.sha256(canonical).hexdigest()


def _canonical_payload(value: object) -> bytes:
    if type(value) is not bytes or not 1 <= len(value) <= _MAX_PAYLOAD_BYTES:
        raise ValueError("canonical_payload must be bounded bytes")
    try:
        decoded = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("canonical_payload must be canonical JSON") from error
    if canonical_json_bytes(decoded) != value:
        raise ValueError("canonical_payload must be exact canonical JSON bytes")
    return value


def make_journal_entity(
    *,
    run_id: str,
    entity_type: str,
    logical_identity: str,
    created_at_us: int,
    payload: object,
) -> JournalEntity:
    """Build a deterministic entity record from canonical JSON semantics."""
    run = _hex64(run_id, name="run_id")
    kind = _slug(entity_type, name="entity_type")
    logical = _hex64(logical_identity, name="logical_identity")
    created = _integer(
        created_at_us, name="created_at_us", minimum=0, maximum=MAX_DATETIME_US
    )
    canonical, payload_hash = _payload(payload)
    entity_id = canonical_hash(
        "journal-entity-v1",
        {
            "run_id": run,
            "entity_type": kind,
            "logical_identity": logical,
            "created_at_us": created,
            "payload_sha256": payload_hash,
        },
    )
    return JournalEntity(
        entity_id,
        run,
        kind,
        logical,
        created,
        payload_hash,
        canonical,
    )


def make_journal_event(
    *,
    entity_id: str,
    event_type: str,
    sequence: int,
    occurred_at_us: int,
    payload: object,
    supersedes_event_id: str | None = None,
) -> JournalEvent:
    """Build a deterministic event record from canonical JSON semantics."""
    entity = _hex64(entity_id, name="entity_id")
    kind = _slug(event_type, name="event_type")
    ordered = _integer(sequence, name="sequence", minimum=0, maximum=_MAX_INT64)
    occurred = _integer(
        occurred_at_us,
        name="occurred_at_us",
        minimum=MIN_DATETIME_US,
        maximum=MAX_DATETIME_US,
    )
    supersedes = (
        None
        if supersedes_event_id is None
        else _hex64(supersedes_event_id, name="supersedes_event_id")
    )
    canonical, payload_hash = _payload(payload)
    event_id = canonical_hash(
        "journal-event-v1",
        {
            "entity_id": entity,
            "event_type": kind,
            "sequence": ordered,
            "occurred_at_us": occurred,
            "payload_sha256": payload_hash,
            "supersedes_event_id": supersedes,
        },
    )
    if supersedes == event_id:
        raise ValueError("event cannot supersede itself")
    return JournalEvent(
        event_id,
        entity,
        kind,
        ordered,
        occurred,
        payload_hash,
        canonical,
        supersedes,
    )


def _reconstruct_entity(record: JournalEntity) -> JournalEntity:
    if type(record) is not JournalEntity:
        raise TypeError("entity record must be JournalEntity")
    try:
        canonical = _canonical_payload(record.canonical_payload)
        payload_hash = hashlib.sha256(canonical).hexdigest()
        values = {
            "run_id": _hex64(record.run_id, name="run_id"),
            "entity_type": _slug(record.entity_type, name="entity_type"),
            "logical_identity": _hex64(
                record.logical_identity, name="logical_identity"
            ),
            "created_at_us": _integer(
                record.created_at_us,
                name="created_at_us",
                minimum=0,
                maximum=MAX_DATETIME_US,
            ),
            "payload_sha256": payload_hash,
        }
        expected_id = canonical_hash("journal-entity-v1", values)
        if record.payload_sha256 != payload_hash or record.entity_id != expected_id:
            raise ValueError("entity record hashes do not match canonical payload")
        return JournalEntity(
            expected_id,
            values["run_id"],  # type: ignore[arg-type]
            values["entity_type"],  # type: ignore[arg-type]
            values["logical_identity"],  # type: ignore[arg-type]
            values["created_at_us"],  # type: ignore[arg-type]
            payload_hash,
            canonical,
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("entity record failed semantic reconstruction") from error


def _reconstruct_event(record: JournalEvent) -> JournalEvent:
    if type(record) is not JournalEvent:
        raise TypeError("event record must be JournalEvent")
    try:
        canonical = _canonical_payload(record.canonical_payload)
        payload_hash = hashlib.sha256(canonical).hexdigest()
        entity = _hex64(record.entity_id, name="entity_id")
        kind = _slug(record.event_type, name="event_type")
        sequence = _integer(
            record.sequence, name="sequence", minimum=0, maximum=_MAX_INT64
        )
        occurred = _integer(
            record.occurred_at_us,
            name="occurred_at_us",
            minimum=MIN_DATETIME_US,
            maximum=MAX_DATETIME_US,
        )
        supersedes = (
            None
            if record.supersedes_event_id is None
            else _hex64(record.supersedes_event_id, name="supersedes_event_id")
        )
        expected_id = canonical_hash(
            "journal-event-v1",
            {
                "entity_id": entity,
                "event_type": kind,
                "sequence": sequence,
                "occurred_at_us": occurred,
                "payload_sha256": payload_hash,
                "supersedes_event_id": supersedes,
            },
        )
        if record.payload_sha256 != payload_hash or record.event_id != expected_id:
            raise ValueError("event record hashes do not match canonical payload")
        if supersedes == expected_id:
            raise ValueError("event cannot supersede itself")
        return JournalEvent(
            expected_id,
            entity,
            kind,
            sequence,
            occurred,
            payload_hash,
            canonical,
            supersedes,
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("event record failed semantic reconstruction") from error


class AppendOnlyJournal:
    """Validate and append immutable records through a sealed ``SecureConnection``.

    Consistency and controlled-writer change control assume callers use
    ``SecureConnection`` public instance methods. Code that deliberately invokes
    base-class descriptors, mutates object internals or process memory, opens raw
    SQLite connections, or writes files directly is out of scope. This is not
    keyed provenance or writer authentication against hostile same-process code.

    Supersession is acyclic without graph traversal: each new event may reference
    only an already persisted predecessor, event primary keys are immutable, and
    primary/alternate identity conflicts abort before ``INSERT OR REPLACE`` can
    delete a row. A future event therefore cannot become an ancestor, while a
    persisted event cannot later gain a back-edge.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        expected_busy_timeout_ms: int | None = None,
    ) -> None:
        if not isinstance(connection, SecureConnection):
            raise TypeError("journal connection must be SecureConnection")
        if connection.isolation_level is not None:
            raise ValueError(
                "journal connection must use autocommit isolation_level=None"
            )
        if not connection.authorizer_sealed:
            raise ValueError("journal connection runtime authorizer must be sealed")
        _reject_user_temp_schema(connection)
        configured = connection.busy_timeout_ms
        if (
            expected_busy_timeout_ms is not None
            and expected_busy_timeout_ms != configured
        ):
            raise ValueError(
                "expected_busy_timeout_ms cannot override SecureConnection"
            )
        expected_busy_timeout_ms = configured
        if type(expected_busy_timeout_ms) is not int:
            raise TypeError("expected_busy_timeout_ms must be a built-in integer")
        if not 0 < expected_busy_timeout_ms <= _MAX_BUSY_TIMEOUT_MS:
            raise ValueError(
                "expected_busy_timeout_ms must be between 1 and 2147483647"
            )
        self._connection = connection
        self._expected_busy_timeout_ms = expected_busy_timeout_ms

    def _verify_write_boundary(self) -> None:
        if self._connection.isolation_level is not None:
            raise JournalRepositoryError(
                "journal connection isolation_level drift: expected autocommit"
            )
        if self._connection.in_transaction:
            raise JournalRepositoryError(
                "cannot write inside an active caller transaction"
            )
        timeout = self._expected_busy_timeout_ms
        if isinstance(self._connection, SecureConnection):
            timeout = self._connection.busy_timeout_ms
        for pragma, expected in (*_STATIC_PRAGMA_CONTRACT, ("busy_timeout", timeout)):
            row = self._connection.execute(f"PRAGMA {pragma}").fetchone()
            actual = None if row is None else row[0]
            if pragma == "journal_mode" and isinstance(actual, str):
                actual = actual.casefold()
            if actual != expected:
                raise JournalRepositoryError(
                    f"journal connection PRAGMA {pragma} drift: "
                    f"expected {expected}, received {actual!r}"
                )

    @contextmanager
    def _write(self, table: str) -> Iterator[None]:
        self._verify_write_boundary()
        owned = False
        with _journal_write_scope(self._connection, table):
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

    def _append_exact(
        self,
        *,
        table: str,
        key_column: str,
        key: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        conflicts: tuple[tuple[str, tuple[object, ...]], ...],
    ) -> str:
        existing = self._connection.execute(
            f"SELECT {', '.join(columns)} FROM main.{table} WHERE {key_column} = ?",
            (key,),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != values:
                raise JournalConflictError(f"existing {table} record conflicts")
            return key
        for where, parameters in conflicts:
            if self._connection.execute(
                f"SELECT 1 FROM main.{table} WHERE {where}", parameters
            ).fetchone():
                raise JournalConflictError(f"existing {table} identity conflicts")
        placeholders = ", ".join("?" for _ in columns)
        try:
            self._connection.execute(
                f"INSERT INTO main.{table} ({', '.join(columns)}) "
                f"VALUES ({placeholders})",
                values,
            )
        except sqlite3.IntegrityError as error:
            if "immutable-conflict" in str(error) or "UNIQUE constraint failed" in str(
                error
            ):
                raise JournalConflictError(f"{table} append conflicts") from error
            raise
        return key

    def append_entity(self, record: JournalEntity) -> str:
        trusted = _reconstruct_entity(record)
        columns = tuple(JournalEntity.__dataclass_fields__)
        values = tuple(getattr(trusted, column) for column in columns)
        with self._write("journal_entities"):
            return self._append_exact(
                table="journal_entities",
                key_column="entity_id",
                key=trusted.entity_id,
                columns=columns,
                values=values,
                conflicts=(
                    (
                        "run_id = ? AND entity_type = ? AND logical_identity = ?",
                        (trusted.run_id, trusted.entity_type, trusted.logical_identity),
                    ),
                ),
            )

    def append_event(self, record: JournalEvent) -> str:
        trusted = _reconstruct_event(record)
        columns = tuple(JournalEvent.__dataclass_fields__)
        values = tuple(getattr(trusted, column) for column in columns)
        with self._write("journal_events"):
            if trusted.supersedes_event_id is not None:
                predecessor = self._connection.execute(
                    "SELECT entity_id, sequence, occurred_at_us, payload_sha256 "
                    "FROM main.journal_events WHERE event_id = ?",
                    (trusted.supersedes_event_id,),
                ).fetchone()
                if predecessor is None:
                    raise JournalConflictError(
                        "supersession predecessor is not persisted"
                    )
                if predecessor[0] != trusted.entity_id:
                    raise JournalConflictError("supersession entity conflicts")
                if predecessor[1] >= trusted.sequence:
                    raise JournalConflictError("supersession sequence conflicts")
                if predecessor[2] >= trusted.occurred_at_us:
                    raise JournalConflictError("supersession time conflicts")
                if predecessor[3] == trusted.payload_sha256:
                    raise JournalConflictError("supersession payload conflicts")
            conflicts: tuple[tuple[str, tuple[object, ...]], ...] = (
                (
                    "entity_id = ? AND sequence = ?",
                    (trusted.entity_id, trusted.sequence),
                ),
            )
            if trusted.supersedes_event_id is not None:
                conflicts += (
                    (
                        "supersedes_event_id = ?",
                        (trusted.supersedes_event_id,),
                    ),
                )
            return self._append_exact(
                table="journal_events",
                key_column="event_id",
                key=trusted.event_id,
                columns=columns,
                values=values,
                conflicts=conflicts,
            )


__all__ = [
    "AppendOnlyJournal",
    "JournalConflictError",
    "JournalEntity",
    "JournalEvent",
    "JournalRepositoryError",
    "make_journal_entity",
    "make_journal_event",
]

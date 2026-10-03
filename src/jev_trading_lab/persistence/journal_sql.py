"""Trusted SQLite callbacks for authenticating append-only journal rows."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from jev_trading_lab.core.ids import canonical_hash, canonical_json_bytes
from jev_trading_lab.core.time import MAX_DATETIME_US, MIN_DATETIME_US

_HEX = frozenset("0123456789abcdef")
_MAX_INT64 = 2**63 - 1


def _hex64(value: object) -> bool:
    return type(value) is str and len(value) == 64 and set(value) <= _HEX


def _slug(value: object) -> bool:
    return (
        type(value) is str
        and 1 <= len(value.encode("utf-8")) <= 64
        and value[0].islower()
        and value[0].isascii()
        and value[-1].isascii()
        and value[-1].isalnum()
        and all(
            character.isascii()
            and (character.islower() or character.isdigit() or character == "_")
            for character in value
        )
        and "__" not in value
    )


def _integer(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _canonical_payload(value: object) -> bytes | None:
    if type(value) is not bytes or not 1 <= len(value) <= 262_144:
        return None
    try:
        decoded = json.loads(value)
        canonical = canonical_json_bytes(decoded)
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return None
    return value if canonical == value else None


def jev_sha256(value: object) -> str | None:
    """Return lowercase SHA-256 only for an exact SQLite BLOB value."""
    return hashlib.sha256(value).hexdigest() if type(value) is bytes else None


def jev_is_canonical_json(value: object) -> int:
    """Return one only for bounded exact canonical JSON BLOB bytes."""
    return int(_canonical_payload(value) is not None)


def jev_journal_entity_id(
    run_id: object,
    entity_type: object,
    logical_identity: object,
    created_at_us: object,
    payload_sha256: object,
) -> str | None:
    """Derive an entity ID from exact validated SQLite scalar types."""
    if not (
        _hex64(run_id)
        and _slug(entity_type)
        and _hex64(logical_identity)
        and _integer(created_at_us, 0, MAX_DATETIME_US)
        and _hex64(payload_sha256)
    ):
        return None
    return canonical_hash(
        "journal-entity-v1",
        {
            "run_id": run_id,
            "entity_type": entity_type,
            "logical_identity": logical_identity,
            "created_at_us": created_at_us,
            "payload_sha256": payload_sha256,
        },
    )


def jev_journal_event_id(
    entity_id: object,
    event_type: object,
    sequence: object,
    occurred_at_us: object,
    payload_sha256: object,
    supersedes_event_id: object,
) -> str | None:
    """Derive an event ID from exact validated SQLite scalar types."""
    if not (
        _hex64(entity_id)
        and _slug(event_type)
        and _integer(sequence, 0, _MAX_INT64)
        and _integer(occurred_at_us, MIN_DATETIME_US, MAX_DATETIME_US)
        and _hex64(payload_sha256)
        and (supersedes_event_id is None or _hex64(supersedes_event_id))
    ):
        return None
    return canonical_hash(
        "journal-event-v1",
        {
            "entity_id": entity_id,
            "event_type": event_type,
            "sequence": sequence,
            "occurred_at_us": occurred_at_us,
            "payload_sha256": payload_sha256,
            "supersedes_event_id": supersedes_event_id,
        },
    )


def _decode_hex(value: object) -> bytes | None:
    if type(value) is not str:
        return None
    try:
        return bytes.fromhex(value)
    except ValueError:
        return None


def _canonical_collation(left: str, right: str) -> int:
    payload = _decode_hex(left)
    return 0 if right == "valid" and _canonical_payload(payload) is not None else 1


def _sha_collation(left: str, right: str) -> int:
    try:
        payload_hex, supplied = left.split(":", 1)
    except ValueError:
        return 1
    payload = _decode_hex(payload_hex)
    return 0 if right == "valid" and jev_sha256(payload) == supplied else 1


def _entity_id_collation(left: str, right: str) -> int:
    try:
        values = json.loads(left)
    except (TypeError, ValueError, json.JSONDecodeError):
        return 1
    if type(values) is not list or len(values) != 5:
        return 1
    return 0 if jev_journal_entity_id(*values) == right else 1


def _event_id_collation(left: str, right: str) -> int:
    try:
        values = json.loads(left)
    except (TypeError, ValueError, json.JSONDecodeError):
        return 1
    if type(values) is not list or len(values) != 6:
        return 1
    return 0 if jev_journal_event_id(*values) == right else 1


def register_journal_sql_functions(connection: sqlite3.Connection) -> None:
    """Register deterministic functions and trusted-schema-safe auth collations.

    CPython exposes ``SQLITE_DETERMINISTIC`` but not ``SQLITE_INNOCUOUS``.
    Consequently, SQLite rejects Python functions inside persistent triggers while
    ``trusted_schema=OFF``. The triggers use equivalent application-defined
    collations, which SQLite permits in that mode; direct SQL still fails closed
    when callbacks are missing. The named functions remain available for explicit
    behavior checks and non-schema callers.
    """
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be sqlite3.Connection")
    connection.create_function("jev_sha256", 1, jev_sha256, deterministic=True)
    connection.create_function(
        "jev_is_canonical_json", 1, jev_is_canonical_json, deterministic=True
    )
    connection.create_function(
        "jev_journal_entity_id", 5, jev_journal_entity_id, deterministic=True
    )
    connection.create_function(
        "jev_journal_event_id", 6, jev_journal_event_id, deterministic=True
    )
    connection.create_collation("jev_is_canonical_json", _canonical_collation)
    connection.create_collation("jev_sha256", _sha_collation)
    connection.create_collation("jev_journal_entity_id", _entity_id_collation)
    connection.create_collation("jev_journal_event_id", _event_id_collation)


__all__ = [
    "jev_is_canonical_json",
    "jev_journal_entity_id",
    "jev_journal_event_id",
    "jev_sha256",
    "register_journal_sql_functions",
]

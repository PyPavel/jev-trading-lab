"""Isolated fixed-protocol migration process entry point."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

from jev_trading_lab.persistence.db import (
    MIN_SQLITE_VERSION,
    DatabaseConfigurationError,
    _internal_connection,
    _validate_busy_timeout,
)
from jev_trading_lab.persistence.migrations import (
    MigrationError,
    _apply_packaged_migrations,
    _migration_authorizer,
)

PROTOCOL_VERSION = 1


def _arguments(argv: tuple[str, ...]) -> tuple[Path, int, tuple[int, int]]:
    if len(argv) != 6 or argv[1] != str(PROTOCOL_VERSION):
        raise MigrationError("invalid migration helper protocol")
    path = Path(argv[2])
    if not path.is_absolute() or os.path.normpath(argv[2]) != argv[2]:
        raise MigrationError(
            "migration helper database path must be absolute and lexically normalized"
        )
    try:
        timeout_ms = int(argv[3], 10)
    except ValueError as error:
        raise MigrationError("invalid migration helper busy timeout") from error
    try:
        identity = (int(argv[4], 10), int(argv[5], 10))
    except ValueError as error:
        raise MigrationError("invalid migration helper database identity") from error
    if min(identity) < 0:
        raise MigrationError("invalid migration helper database identity")
    return path, _validate_busy_timeout(timeout_ms), identity


def main(argv: tuple[str, ...]) -> int:
    try:
        path, timeout_ms, identity = _arguments(argv)
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise MigrationError("migration helper SQLite is too old")
        connection = _internal_connection(
            path,
            timeout_ms=timeout_ms,
            authorizer=_migration_authorizer,
            expected_identity=identity,
        )
        try:
            _apply_packaged_migrations(connection)
        finally:
            connection.close()
    except (
        DatabaseConfigurationError,
        MigrationError,
        OSError,
        TypeError,
        ValueError,
        sqlite3.DatabaseError,
    ):
        return 1
    sys.stdout.write(json.dumps({"protocol": PROTOCOL_VERSION, "status": "ok"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv)))

"""Test-only execution helper for synthetic migration packages."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from jev_trading_lab.persistence.db import SecureConnection, _reject_user_temp_schema
from jev_trading_lab.persistence.migrations import (
    MigrationError,
    _applied_rows,
    _migration_authorizer,
    _migration_metadata_insert,
    _require_main_only,
    _timestamp,
    _validate_applied,
    _validated_statements,
    load_migrations,
)


def apply_fixture_migrations(
    connection: sqlite3.Connection,
    *,
    package: str = "jev_trading_lab.persistence.schema",
    clock: Callable[[], int] = lambda: 1_000_000,
) -> None:
    """Execute synthetic test migrations on a caller-owned test connection."""
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be sqlite3.Connection")
    if connection.isolation_level is not None:
        raise MigrationError(
            "migration connection must use autocommit isolation_level=None"
        )
    if connection.in_transaction:
        raise MigrationError("cannot migrate inside an active caller transaction")
    if not callable(clock):
        raise TypeError("migration clock must be callable")
    _require_main_only(connection)
    if isinstance(connection, SecureConnection):
        _reject_user_temp_schema(connection)
    migrations = load_migrations(package)
    migration_statements = tuple(_validated_statements(item) for item in migrations)
    timestamps = tuple(_timestamp(clock) for _item in migrations)
    _validate_applied(_applied_rows(connection), migrations)

    sqlite3.Connection.set_authorizer(connection, _migration_authorizer)
    try:
        for migration, statements, applied_at_us in zip(
            migrations, migration_statements, timestamps, strict=True
        ):
            owned_transaction = False
            try:
                connection.execute("BEGIN IMMEDIATE")
                owned_transaction = True
                applied = _applied_rows(connection)
                _validate_applied(applied, migrations)
                if migration.version > len(applied):
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(
                        _migration_metadata_insert(migration, applied_at_us)
                    )
                connection.execute("COMMIT")
                owned_transaction = False
            except sqlite3.DatabaseError as error:
                if owned_transaction and connection.in_transaction:
                    connection.execute("ROLLBACK")
                if "authorized" in str(error).casefold():
                    raise MigrationError(
                        f"migration {migration.name!r} contains unauthorized SQL"
                    ) from error
                raise
            except BaseException:
                if owned_transaction and connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
    finally:
        sqlite3.Connection.set_authorizer(connection, None)

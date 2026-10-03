"""Load and apply immutable migrations from reviewed package resources.

Future migrations that create immutable tables must install their UPDATE/DELETE
abort triggers and lineage constraints in the same migration. Each migration is
committed independently, so no migration prefix may expose an unguarded table.
"""

from __future__ import annotations

import errno
import hashlib
import importlib.resources
import os
import re
import sqlite3
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources.abc import Traversable
from pathlib import Path

from jev_trading_lab.core.time import datetime_to_utc_us, validate_utc_us

MAX_MIGRATION_BYTES = 1_048_576
_DEFAULT_PACKAGE = "jev_trading_lab.persistence.schema"
_FILENAME = re.compile(r"(?P<version>[0-9]{3})_(?P<slug>[a-z][a-z0-9_]*)\.sql")
_FORBIDDEN_FIRST_KEYWORDS = frozenset(
    {
        "ATTACH",
        "BEGIN",
        "COMMIT",
        "DETACH",
        "END",
        "PRAGMA",
        "RELEASE",
        "ROLLBACK",
        "SAVEPOINT",
        "VACUUM",
    }
)


class MigrationError(RuntimeError):
    """Raised when the immutable migration contract is violated."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sha256: str
    sql: str


def _utc_now_us() -> int:
    return datetime_to_utc_us(datetime.now(UTC))


def _read_filesystem_resource(resource: Path) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(resource, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise MigrationError(
                f"migration resource {resource.name!r} is not a regular file"
            )
        if before.st_uid != os.geteuid():
            raise MigrationError(
                f"migration resource {resource.name!r} is not owned by effective UID"
            )
        if not before.st_mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH):
            raise MigrationError(f"migration resource {resource.name!r} is unreadable")
        chunks: list[bytes] = []
        remaining = MAX_MIGRATION_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise MigrationError(
                f"migration resource {resource.name!r} changed while reading"
            )
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise MigrationError(
                f"migration resource {resource.name!r} is a symlink"
            ) from error
        raise MigrationError(
            f"migration resource {resource.name!r} is unreadable"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return content


def _read_resource(resource: Traversable) -> bytes:
    if isinstance(resource, Path):
        content = _read_filesystem_resource(resource)
    else:
        try:
            with resource.open("rb") as stream:
                content = stream.read(MAX_MIGRATION_BYTES + 1)
        except OSError as error:
            raise MigrationError(
                f"migration resource {resource.name!r} is unreadable"
            ) from error
    if len(content) > MAX_MIGRATION_BYTES:
        raise MigrationError(
            f"migration resource {resource.name!r} exceeds maximum size "
            f"{MAX_MIGRATION_BYTES}"
        )
    return content


def load_migrations(package: str = _DEFAULT_PACKAGE) -> tuple[Migration, ...]:
    """Discover a contiguous migration chain without requiring filesystem paths."""
    if type(package) is not str or not package:
        raise TypeError("migration package must be a non-empty string")
    try:
        root = importlib.resources.files(package)
        resources = tuple(root.iterdir())
    except (ImportError, ModuleNotFoundError, OSError) as error:
        raise MigrationError(f"cannot load migration package {package!r}") from error

    migrations: list[Migration] = []
    versions: set[int] = set()
    for resource in resources:
        name = resource.name
        if name == "__init__.py":
            _read_resource(resource)
            continue
        if name == "__pycache__" and resource.is_dir():
            continue
        match = _FILENAME.fullmatch(name)
        if match is None:
            raise MigrationError(
                f"invalid migration filename or unexpected resource: {name!r}"
            )
        version = int(match.group("version"))
        if version == 0:
            raise MigrationError(f"invalid migration filename: {name!r}")
        if version in versions:
            raise MigrationError(f"duplicate migration version {version:03d}")
        versions.add(version)
        content = _read_resource(resource)
        try:
            sql = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise MigrationError(f"migration {name!r} is not valid UTF-8") from error
        migrations.append(
            Migration(
                version=version,
                name=name,
                sha256=hashlib.sha256(content).hexdigest(),
                sql=sql,
            )
        )

    migrations.sort(key=lambda migration: migration.version)
    if not migrations:
        raise MigrationError("migration package must contain at least migration 001")
    actual = [migration.version for migration in migrations]
    expected = list(range(1, len(migrations) + 1))
    if actual != expected:
        raise MigrationError(
            "migration versions must be contiguous starting at 001; "
            f"received {actual!r}"
        )
    return tuple(migrations)


def _only_whitespace_or_comments(value: str) -> bool:
    index = 0
    while index < len(value):
        if value[index].isspace():
            index += 1
        elif value.startswith("--", index):
            newline = value.find("\n", index + 2)
            if newline == -1:
                return True
            index = newline + 1
        elif value.startswith("/*", index):
            end = value.find("*/", index + 2)
            if end == -1:
                raise MigrationError("migration SQL contains an unterminated comment")
            index = end + 2
        else:
            return False
    return True


def split_sql_statements(script: str) -> tuple[str, ...]:
    """Split SQL using SQLite's parser, preserving trigger-body semicolons."""
    if type(script) is not str:
        raise TypeError("migration SQL must be a string")
    statements: list[str] = []
    pending: list[str] = []
    for character in script:
        pending.append(character)
        if character != ";":
            continue
        candidate = "".join(pending)
        if sqlite3.complete_statement(candidate):
            if not _only_whitespace_or_comments(candidate):
                statements.append(candidate)
            pending.clear()
    remainder = "".join(pending)
    if not _only_whitespace_or_comments(remainder):
        raise MigrationError("migration SQL ends with an incomplete statement")
    if not statements:
        raise MigrationError("migration SQL contains no statements")
    return tuple(statements)


def _first_keyword(statement: str) -> str:
    index = 0
    length = len(statement)
    while index < length:
        if statement[index].isspace():
            index += 1
        elif statement.startswith("--", index):
            newline = statement.find("\n", index + 2)
            if newline == -1:
                return ""
            index = newline + 1
        elif statement.startswith("/*", index):
            end = statement.find("*/", index + 2)
            if end == -1:
                raise MigrationError("migration SQL contains an unterminated comment")
            index = end + 2
        else:
            break
    match = re.match(r"[A-Za-z]+", statement[index:])
    return "" if match is None else match.group().upper()


def _validated_statements(migration: Migration) -> tuple[str, ...]:
    statements = split_sql_statements(migration.sql)
    for statement in statements:
        keyword = _first_keyword(statement)
        if not keyword:
            raise MigrationError(
                f"migration {migration.name!r} has no executable SQL statement"
            )
        if keyword in _FORBIDDEN_FIRST_KEYWORDS:
            raise MigrationError(
                f"migration {migration.name!r} contains forbidden SQL {keyword}"
            )
    return statements


def _metadata_table_exists(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM main.sqlite_schema "
            "WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        is not None
    )


def _applied_rows(connection: sqlite3.Connection) -> list[tuple[int, str, str]]:
    if not _metadata_table_exists(connection):
        return []
    try:
        rows = connection.execute(
            "SELECT version, name, sha256 FROM main.schema_migrations ORDER BY version"
        ).fetchall()
    except sqlite3.DatabaseError as error:
        raise MigrationError("schema_migrations metadata is invalid") from error
    result: list[tuple[int, str, str]] = []
    for row in rows:
        version, name, sha256 = row
        if type(version) is not int or type(name) is not str or type(sha256) is not str:
            raise MigrationError("applied migration metadata has invalid types")
        result.append((version, name, sha256))
    return result


def _validate_applied(
    applied: list[tuple[int, str, str]], migrations: tuple[Migration, ...]
) -> None:
    versions = [row[0] for row in applied]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(
            f"applied migration chain has a gap or unknown future version: {versions!r}"
        )
    if len(applied) > len(migrations):
        raise MigrationError(
            "applied migration is an unknown future or missing packaged migration"
        )
    for row, migration in zip(applied, migrations, strict=False):
        expected = (migration.version, migration.name, migration.sha256)
        if row != expected:
            raise MigrationError(
                f"applied migration {row[0]:03d} name or checksum differs from package"
            )


def _timestamp(clock: Callable[[], int]) -> int:
    value = clock()
    if type(value) is not int:
        raise TypeError(
            "migration clock must return a built-in integer UTC microsecond value"
        )
    return validate_utc_us(value)


def apply_migrations(
    connection: sqlite3.Connection,
    *,
    package: str = _DEFAULT_PACKAGE,
    clock: Callable[[], int] = _utc_now_us,
) -> None:
    """Apply migrations using an idle autocommit connection and owned transactions."""
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
    migrations = load_migrations(package)
    migration_statements = tuple(_validated_statements(item) for item in migrations)
    _validate_applied(_applied_rows(connection), migrations)

    for migration, statements in zip(migrations, migration_statements, strict=True):
        owned_transaction = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            owned_transaction = True
            applied = _applied_rows(connection)
            _validate_applied(applied, migrations)
            if migration.version <= len(applied):
                connection.execute("COMMIT")
                owned_transaction = False
                continue
            for statement in statements:
                connection.execute(statement)
            applied_at_us = _timestamp(clock)
            connection.execute(
                "INSERT INTO main.schema_migrations "
                "(version, name, sha256, applied_at_us) VALUES (?, ?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    migration.sha256,
                    applied_at_us,
                ),
            )
            connection.execute("COMMIT")
            owned_transaction = False
        except BaseException:
            if owned_transaction and connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

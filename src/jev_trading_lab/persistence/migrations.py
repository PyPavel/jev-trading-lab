"""Load and apply immutable migrations from reviewed package resources.

Future migrations that create immutable tables must install their UPDATE/DELETE
abort triggers and lineage constraints in the same migration. Each migration is
committed independently, so no migration prefix may expose an unguarded table.
Filesystem package resources require trusted parent metadata; archive Traversables
use bounded streams and rely on the exact locked, hash-protected wheel artifact.
Migration connections are dedicated: this runner owns and clears the authorizer.
The trusted fixed packaged helper is supervised for ordinary hangs, crashes, closed
pipes, same-group descendants, and output overflow. Exceptions during blocking
operations where cleanup guards already own resources trigger helper cleanup. Malicious
replacement/modification of the helper, including a setsid escape, is excluded.
"""

from __future__ import annotations

import errno
import hashlib
import importlib.resources
import json
import os
import re
import selectors
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources.abc import Traversable
from pathlib import Path

from jev_trading_lab.core.time import (
    MAX_DATETIME_US,
    datetime_to_utc_us,
    validate_utc_us,
)
from jev_trading_lab.persistence.db import (
    SecureConnection,
    _close_directory_handle,
    _connection_locator,
    _DirectoryHandle,
    _is_factory_connection,
    _reject_user_temp_schema,
    _verify_internal_connection_storage,
)

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
        "REINDEX",
        "RELEASE",
        "ROLLBACK",
        "SAVEPOINT",
        "VACUUM",
    }
)
_TEMP_SQL = re.compile(
    r"(?:\bCREATE\s+TEMP(?:ORARY)?\b|(?:\btemp\b|\"temp\"|\[temp\]|`temp`)\s*\.)",
    re.IGNORECASE,
)
_EXPLICIT_SCHEMA_SQL = re.compile(
    r"^\s*(?:CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX|TRIGGER|VIEW)|"
    r"DROP\s+(?:TABLE|INDEX|TRIGGER|VIEW)|ALTER\s+TABLE|INSERT\s+INTO|"
    r"UPDATE|DELETE\s+FROM|REINDEX)\s+"
    r"(?:(?:IF\s+(?:NOT\s+)?EXISTS)\s+)?"
    r"(?P<schema>[A-Za-z_][A-Za-z0-9_]*|\"[^\"]+\"|\[[^\]]+\]|`[^`]+`)\s*\.",
    re.IGNORECASE,
)
_WRITE_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_VIEW,
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
    }
)
_TEMP_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_CREATE_TEMP_INDEX,
        sqlite3.SQLITE_CREATE_TEMP_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
        sqlite3.SQLITE_CREATE_TEMP_VIEW,
        sqlite3.SQLITE_DROP_TEMP_INDEX,
        sqlite3.SQLITE_DROP_TEMP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_TRIGGER,
        sqlite3.SQLITE_DROP_TEMP_VIEW,
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


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _directory_snapshot(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_ctime_ns,
    )


def _validate_resource_directory(metadata: os.stat_result, path: Path) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise MigrationError(
            f"migration package directory {path} is a symlink or non-directory"
        )
    if metadata.st_uid not in {0, os.geteuid()}:
        raise MigrationError(
            f"migration package directory {path} has an untrusted owner"
        )
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise MigrationError(
            f"migration package directory {path} is group/other writable"
        )


def _open_resource_directories(
    root: Path, package: str
) -> list[tuple[_DirectoryHandle, tuple[int, ...], Path]]:
    package_parts = tuple(package.split("."))
    if tuple(root.parts[-len(package_parts) :]) != package_parts:
        raise MigrationError("migration package path does not match package name")
    base = root.parents[len(package_parts) - 1]
    try:
        current = _DirectoryHandle(os.open(base, _directory_flags()))
    except OSError as error:
        raise MigrationError(
            f"migration package directory {base} is unreadable"
        ) from error
    opened: list[tuple[_DirectoryHandle, tuple[int, ...], Path]] = []
    base_handle = current
    try:
        current_path = base
        for component in package_parts:
            current_path /= component
            child = _DirectoryHandle(
                os.open(component, _directory_flags(), dir_fd=current.descriptor)
            )
            try:
                metadata = os.fstat(child.descriptor)
                _validate_resource_directory(metadata, current_path)
            except BaseException:
                _close_directory_handle(child)
                raise
            opened.append((child, _directory_snapshot(metadata), current_path))
            current = child
        return opened
    except OSError as error:
        for handle, _snapshot, _path in opened:
            _close_directory_handle(handle)
        raise MigrationError(
            f"migration package directory {root} is a symlink or unreadable"
        ) from error
    except BaseException:
        for handle, _snapshot, _path in opened:
            _close_directory_handle(handle)
        raise
    finally:
        _close_directory_handle(base_handle)


def _revalidate_resource_directories(
    opened: list[tuple[_DirectoryHandle, tuple[int, ...], Path]],
) -> None:
    for handle, snapshot, path in opened:
        metadata = os.fstat(handle.descriptor)
        _validate_resource_directory(metadata, path)
        if _directory_snapshot(metadata) != snapshot:
            raise MigrationError(
                f"migration package directory {path} changed while loading"
            )


def _resource_snapshot(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
    )


def _read_filesystem_resource(
    resource: Path, parent_descriptor: int | None = None
) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    handle: _DirectoryHandle | None = None
    try:
        handle = _DirectoryHandle(
            os.open(
                resource.name if parent_descriptor is not None else resource,
                flags,
                dir_fd=parent_descriptor,
            )
        )
        before = os.fstat(handle.descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise MigrationError(
                f"migration resource {resource.name!r} is not a regular file"
            )
        if before.st_uid not in {0, os.geteuid()}:
            raise MigrationError(
                f"migration resource {resource.name!r} has an untrusted owner"
            )
        if before.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise MigrationError(
                f"migration resource {resource.name!r} is group/other writable"
            )
        if before.st_nlink != 1:
            raise MigrationError(
                f"migration resource {resource.name!r} must have one link"
            )
        if not before.st_mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH):
            raise MigrationError(f"migration resource {resource.name!r} is unreadable")
        chunks: list[bytes] = []
        remaining = MAX_MIGRATION_BYTES + 1
        while remaining:
            chunk = os.read(handle.descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(handle.descriptor)
        if _resource_snapshot(before) != _resource_snapshot(after):
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
        if handle is not None:
            _close_directory_handle(handle)
    if len(content) > MAX_MIGRATION_BYTES:
        raise MigrationError(
            f"migration resource {resource.name!r} exceeds maximum size "
            f"{MAX_MIGRATION_BYTES}"
        )
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
    except (ImportError, ModuleNotFoundError, OSError) as error:
        raise MigrationError(f"cannot load migration package {package!r}") from error

    directories: list[tuple[_DirectoryHandle, tuple[int, ...], Path]] = []
    filesystem_root: Path | None = root if isinstance(root, Path) else None
    filesystem_names: tuple[str, ...] | None = None
    archive_resources: tuple[Traversable, ...] = ()
    try:
        if filesystem_root is not None:
            directories = _open_resource_directories(filesystem_root, package)
            _revalidate_resource_directories(directories)
            filesystem_names = tuple(os.listdir(directories[-1][0].descriptor))
            _revalidate_resource_directories(directories)
        else:
            archive_resources = tuple(root.iterdir())
    except (ImportError, ModuleNotFoundError, OSError) as error:
        for handle, _snapshot, _path in directories:
            _close_directory_handle(handle)
        raise MigrationError(f"cannot load migration package {package!r}") from error
    except BaseException:
        for handle, _snapshot, _path in directories:
            _close_directory_handle(handle)
        raise

    migrations: list[Migration] = []
    versions: set[int] = set()
    resources: tuple[tuple[str, Traversable | None], ...]
    if filesystem_names is not None:
        resources = tuple((name, None) for name in filesystem_names)
    else:
        resources = tuple((resource.name, resource) for resource in archive_resources)
    try:
        for name, archive_resource in resources:
            if name == "__pycache__":
                if archive_resource is not None:
                    if archive_resource.is_dir():
                        continue
                else:
                    metadata = os.stat(
                        name,
                        dir_fd=directories[-1][0].descriptor,
                        follow_symlinks=False,
                    )
                    if stat.S_ISDIR(metadata.st_mode):
                        continue
            if name == "__init__.py":
                if archive_resource is None:
                    _revalidate_resource_directories(directories)
                    assert filesystem_root is not None
                    _read_filesystem_resource(
                        filesystem_root / name, directories[-1][0].descriptor
                    )
                    _revalidate_resource_directories(directories)
                else:
                    _read_resource(archive_resource)
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
            if archive_resource is None:
                _revalidate_resource_directories(directories)
                assert filesystem_root is not None
                content = _read_filesystem_resource(
                    filesystem_root / name, directories[-1][0].descriptor
                )
                _revalidate_resource_directories(directories)
            else:
                content = _read_resource(archive_resource)
            try:
                sql = content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise MigrationError(
                    f"migration {name!r} is not valid UTF-8"
                ) from error
            migrations.append(
                Migration(
                    version=version,
                    name=name,
                    sha256=hashlib.sha256(content).hexdigest(),
                    sql=sql,
                )
            )
        _revalidate_resource_directories(directories)
    except OSError as error:
        raise MigrationError(f"cannot load migration package {package!r}") from error
    finally:
        for handle, _snapshot, _path in directories:
            _close_directory_handle(handle)

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
        if _TEMP_SQL.search(statement):
            raise MigrationError(
                f"migration {migration.name!r} contains forbidden TEMP SQL"
            )
        schema = _EXPLICIT_SCHEMA_SQL.match(statement)
        if schema is not None:
            name = schema.group("schema").strip('"[]`').casefold()
            if name != "main":
                raise MigrationError(
                    f"migration {migration.name!r} contains non-main schema SQL"
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


def _applied_rows(connection: sqlite3.Connection) -> list[tuple[int, str, str, int]]:
    if not _metadata_table_exists(connection):
        return []
    try:
        rows = connection.execute(
            "SELECT version, name, sha256, applied_at_us "
            "FROM main.schema_migrations ORDER BY version"
        ).fetchall()
    except sqlite3.DatabaseError as error:
        raise MigrationError("schema_migrations metadata is invalid") from error
    result: list[tuple[int, str, str, int]] = []
    for row in rows:
        version, name, sha256, applied_at_us = row
        if (
            type(version) is not int
            or type(name) is not str
            or type(sha256) is not str
            or type(applied_at_us) is not int
            or not 0 <= applied_at_us <= MAX_DATETIME_US
        ):
            raise MigrationError(
                "applied migration metadata has invalid types or values"
            )
        result.append((version, name, sha256, applied_at_us))
    return result


def _validate_applied(
    applied: list[tuple[int, str, str, int]], migrations: tuple[Migration, ...]
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
        if row[:3] != expected:
            raise MigrationError(
                f"applied migration {row[0]:03d} name or checksum differs from package"
            )


def _timestamp(clock: Callable[[], int]) -> int:
    value = clock()
    if type(value) is not int:
        raise TypeError(
            "migration clock must return a built-in integer UTC microsecond value"
        )
    validate_utc_us(value)
    if value < 0:
        raise ValueError("migration timestamp cannot predate the Unix epoch")
    return value


def _require_main_only(connection: sqlite3.Connection) -> None:
    rows = connection.execute("PRAGMA database_list").fetchall()
    databases = [(row[1], row[2]) for row in rows]
    if not databases or databases[0][0] != "main":
        raise MigrationError("migration connection has an invalid main database")
    if databases[1:] not in ([], [("temp", "")]):
        raise MigrationError("migration connection has an auxiliary database attached")


def _migration_authorizer(
    action: int,
    _argument1: str | None,
    _argument2: str | None,
    database: str | None,
    source: str | None,
) -> int:
    if action in {
        sqlite3.SQLITE_ANALYZE,
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
    }:
        return sqlite3.SQLITE_DENY
    if action in _TEMP_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action in _WRITE_ACTIONS and (database != "main" or source is not None):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _migration_metadata_insert(migration: Migration, applied_at_us: int) -> str:
    name = migration.name.encode("utf-8").hex()
    sha256 = migration.sha256.encode("utf-8").hex()
    return (
        "INSERT INTO main.schema_migrations "
        "(version, name, sha256, applied_at_us) VALUES "
        f"({migration.version}, CAST(X'{name}' AS TEXT), "
        f"CAST(X'{sha256}' AS TEXT), {applied_at_us})"
    )


_HELPER_PROTOCOL_VERSION = 1
_HELPER_TIMEOUT_SECONDS = 30.0
_HELPER_OUTPUT_LIMIT = 16_384


def verify_migration_prefix(connection: sqlite3.Connection) -> None:
    """Read-verify the exact packaged migration prefix and checksums."""
    migrations = load_migrations()
    _validate_applied(_applied_rows(connection), migrations)
    if len(_applied_rows(connection)) != len(migrations):
        raise MigrationError(
            "database does not contain the full packaged migration prefix"
        )


def _helper_command(
    path: Path, timeout_ms: int, identity: tuple[int, int]
) -> tuple[str, ...]:
    return (
        sys.executable,
        "-I",
        "-B",
        "-m",
        "jev_trading_lab.persistence.migration_helper",
        str(_HELPER_PROTOCOL_VERSION),
        str(path),
        str(timeout_ms),
        str(identity[0]),
        str(identity[1]),
    )


def _kill_and_reap_helper_group(process: subprocess.Popen[bytes]) -> None:
    """Kill the helper session and reap its direct child without masking failure."""
    active = sys.exception()
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except BaseException:
        if active is None:
            raise
    try:
        process.wait()
    except BaseException:
        if active is None:
            raise


def _run_migration_helper(
    path: Path, timeout_ms: int, identity: tuple[int, int]
) -> subprocess.CompletedProcess[bytes]:
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    stdout = bytearray()
    stderr = bytearray()
    failure: MigrationError | None = None
    returncode = -1
    try:
        try:
            process = subprocess.Popen(
                _helper_command(path, timeout_ms, identity),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as error:
            raise MigrationError("cannot launch isolated migration helper") from error
        assert process.stdout is not None and process.stderr is not None
        output = {process.stdout.fileno(): stdout, process.stderr.fileno(): stderr}
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        selector.register(process.stderr, selectors.EVENT_READ)
        deadline = time.monotonic() + _HELPER_TIMEOUT_SECONDS
        total_output = 0
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = MigrationError("isolated migration helper timed out")
                break
            for key, _events in selector.select(remaining):
                chunk = os.read(
                    key.fd,
                    min(4096, _HELPER_OUTPUT_LIMIT + 1 - total_output),
                )
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output[key.fd].extend(chunk)
                total_output += len(chunk)
                if total_output > _HELPER_OUTPUT_LIMIT:
                    failure = MigrationError(
                        "isolated migration helper output exceeded limit"
                    )
                    break
            if failure is not None:
                break
        if failure is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = MigrationError("isolated migration helper timed out")
            else:
                try:
                    returncode = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    failure = MigrationError("isolated migration helper timed out")
        _kill_and_reap_helper_group(process)
    except BaseException:
        if process is not None:
            _kill_and_reap_helper_group(process)
        raise
    finally:
        active = sys.exception()
        cleanup_error: BaseException | None = None
        streams = () if process is None else (process.stdout, process.stderr)
        for stream in (selector, *streams):
            if stream is None:
                continue
            try:
                stream.close()
            except BaseException as error:
                if active is None and cleanup_error is None:
                    cleanup_error = error
        if cleanup_error is not None:
            raise cleanup_error
    if failure is not None:
        raise failure
    assert process is not None
    return subprocess.CompletedProcess(
        process.args, returncode, bytes(stdout), bytes(stderr)
    )


def _validate_helper_reply(completed: subprocess.CompletedProcess[bytes]) -> None:
    if (
        len(completed.stdout) > _HELPER_OUTPUT_LIMIT
        or len(completed.stderr) > _HELPER_OUTPUT_LIMIT
    ):
        raise MigrationError("isolated migration helper output exceeded limit")
    try:
        reply = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise MigrationError(
            "isolated migration helper returned malformed output"
        ) from error
    if completed.returncode != 0 or reply != {
        "protocol": _HELPER_PROTOCOL_VERSION,
        "status": "ok",
    }:
        raise MigrationError("isolated migration helper failed")


def apply_migrations(connection: sqlite3.Connection) -> None:
    """Apply packaged migrations in a fresh isolated fixed-role interpreter.

    Passive audit hooks are supported. Deliberate ``sys.addaudithook`` SQLite
    handle capture and capability theft are out of scope.
    """
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be sqlite3.Connection")
    if not _is_factory_connection(connection):
        raise MigrationError(
            "migration connection must be created and sealed by connect_database"
        )
    assert type(connection) is SecureConnection
    if connection.isolation_level is not None:
        raise MigrationError(
            "migration connection must use autocommit isolation_level=None"
        )
    if connection.in_transaction:
        raise MigrationError("cannot migrate inside an active caller transaction")
    _require_main_only(connection)
    _reject_user_temp_schema(connection)
    path, timeout_ms, identity = _connection_locator(connection)
    migrations = load_migrations()
    _validate_applied(_applied_rows(connection), migrations)

    try:
        completed = _run_migration_helper(path, timeout_ms, identity)
    except Exception:
        verify_migration_prefix(connection)
        return
    try:
        verify_migration_prefix(connection)
    except MigrationError:
        _validate_helper_reply(completed)
        raise
    # Helper replies are diagnostic; exact authoritative DB readback decides success.


def _apply_packaged_migrations(connection: sqlite3.Connection) -> None:
    """Child-only migration loop for a fixed migration-role connection."""
    migrations = load_migrations()
    migration_statements = tuple(_validated_statements(item) for item in migrations)
    timestamps = tuple(_timestamp(_utc_now_us) for _item in migrations)
    _validate_applied(_applied_rows(connection), migrations)
    for migration, statements, applied_at_us in zip(
        migrations, migration_statements, timestamps, strict=True
    ):
        owned_transaction = False
        try:
            _verify_internal_connection_storage(connection)
            connection.execute("BEGIN IMMEDIATE")
            owned_transaction = True
            _verify_internal_connection_storage(connection)
            applied = _applied_rows(connection)
            _validate_applied(applied, migrations)
            if migration.version > len(applied):
                for statement in statements:
                    connection.execute(statement)
                connection.execute(_migration_metadata_insert(migration, applied_at_us))
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
    verify_migration_prefix(connection)

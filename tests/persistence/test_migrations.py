from __future__ import annotations

import hashlib
import importlib
import importlib.resources
import os
import sqlite3
import stat
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from jev_trading_lab.persistence import db as db_module
from jev_trading_lab.persistence.db import (
    MIN_SQLITE_VERSION,
    DatabaseConfigurationError,
    connect_database,
)
from jev_trading_lab.persistence.migrations import (
    MAX_MIGRATION_BYTES,
    MigrationError,
    apply_migrations,
    load_migrations,
    split_sql_statements,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_SQL = (
    ROOT / "src/jev_trading_lab/persistence/schema/001_migration_metadata.sql"
).read_bytes()


def _package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    files: Mapping[str, bytes],
) -> tuple[str, Path]:
    package = f"migration_fixture_{uuid.uuid4().hex}"
    schema = tmp_path / package / "schema"
    schema.mkdir(parents=True)
    (tmp_path / package / "__init__.py").write_text("", encoding="utf-8")
    (schema / "__init__.py").write_text("", encoding="utf-8")
    for name, content in files.items():
        (schema / name).write_bytes(content)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    return f"{package}.schema", schema


def _connect(path: Path, *, timeout: int = 1_000) -> sqlite3.Connection:
    return connect_database(
        path,
        busy_timeout_ms=timeout,
        mount_resolver=lambda _path: "ext4",
    )


def _applied(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    rows = connection.execute(
        "SELECT version, name, sha256, applied_at_us "
        "FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [tuple(row) for row in rows]


def test_connection_pragmas(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    database = parent / "lab.sqlite3"

    for _ in range(2):
        connection = _connect(database, timeout=4_321)
        try:
            assert connection.row_factory is sqlite3.Row
            assert connection.isolation_level is None
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 4_321
            assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
            assert connection.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        finally:
            connection.close()

    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


@pytest.mark.parametrize("invalid", [True, "1000", 0, -1, 2**31])
def test_connection_rejects_invalid_busy_timeout(
    tmp_path: Path, invalid: object
) -> None:
    with pytest.raises((TypeError, ValueError), match="busy_timeout_ms"):
        connect_database(  # type: ignore[arg-type]
            tmp_path / "db.sqlite3",
            busy_timeout_ms=invalid,
            mount_resolver=lambda _path: "ext4",
        )


@pytest.mark.parametrize("invalid", ["db.sqlite3", True])
def test_connection_requires_path(tmp_path: Path, invalid: object) -> None:
    with pytest.raises(TypeError, match="Path"):
        connect_database(  # type: ignore[arg-type]
            invalid,
            busy_timeout_ms=1_000,
            mount_resolver=lambda _path: "ext4",
        )


def test_network_fs_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    real_connect = sqlite3.connect

    def recording_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal calls
        calls += 1
        return real_connect(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(db_module.sqlite3, "connect", recording_connect)
    network_filesystems = (
        "nfs",
        "nfs4",
        "cifs",
        "smbfs",
        "sshfs",
        "fuse.sshfs",
        "9p",
        "ceph",
        "glusterfs",
    )
    for filesystem in network_filesystems:
        with pytest.raises(DatabaseConfigurationError, match="network filesystem"):
            connect_database(
                tmp_path / filesystem / "db.sqlite3",
                busy_timeout_ms=1_000,
                mount_resolver=lambda _path, fs=filesystem: fs,
            )
    assert calls == 0

    connection = connect_database(
        tmp_path / "local" / "db.sqlite3",
        busy_timeout_ms=1_000,
        mount_resolver=lambda _path: "ext4",
    )
    connection.close()
    assert calls == 1


def test_mountinfo_uses_decoded_longest_mount_point(tmp_path: Path) -> None:
    mountinfo = (
        "1 0 8:1 / / rw - ext4 /dev/root rw\n"
        "2 1 0:2 / /srv/shared\\040data rw - nfs server:/share rw\n"
        "3 2 0:3 / /srv/shared\\040data/local rw - ext4 /dev/loop0 rw\n"
    )
    assert (
        db_module._filesystem_type_from_mountinfo(
            Path("/srv/shared data/file.sqlite3"), mountinfo
        )
        == "nfs"
    )
    assert (
        db_module._filesystem_type_from_mountinfo(
            Path("/srv/shared data/local/file.sqlite3"), mountinfo
        )
        == "ext4"
    )


def test_symlink_database_and_parent_are_rejected(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    symlink_parent = tmp_path / "linked"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(DatabaseConfigurationError, match="symlink"):
        _connect(symlink_parent / "db.sqlite3")

    target = real_parent / "target.sqlite3"
    target.touch()
    link = real_parent / "db.sqlite3"
    link.symlink_to(target)
    with pytest.raises(DatabaseConfigurationError, match="symlink"):
        _connect(link)


def test_sqlite_too_old_fails_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = False

    def forbidden_open(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        nonlocal opened
        opened = True
        raise AssertionError("database must not open")

    monkeypatch.setattr(db_module.sqlite3, "sqlite_version_info", (3, 36, 0))
    monkeypatch.setattr(db_module.sqlite3, "connect", forbidden_open)
    with pytest.raises(DatabaseConfigurationError, match=r"SQLite >= 3\.37\.0"):
        _connect(tmp_path / "db.sqlite3")
    assert MIN_SQLITE_VERSION == (3, 37, 0)
    assert not opened


def test_pragma_setup_failure_closes_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    monkeypatch.setattr(
        db_module.sqlite3,
        "connect",
        lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(DatabaseConfigurationError, match="WAL"):
        _connect(tmp_path / "db.sqlite3")

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_clean_database_applies_only_migration_metadata(tmp_path: Path) -> None:
    connection = _connect(tmp_path / "db.sqlite3")
    try:
        apply_migrations(connection, clock=lambda: 1_700_000_000_000_000)
        objects = connection.execute(
            "SELECT type, name FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        assert [tuple(row) for row in objects] == [("table", "schema_migrations")]
        expected_hash = hashlib.sha256(SCHEMA_SQL).hexdigest()
        assert _applied(connection) == [
            (
                1,
                "001_migration_metadata.sql",
                expected_hash,
                1_700_000_000_000_000,
            )
        ]
    finally:
        connection.close()


def test_repeated_migration_application_is_idempotent(tmp_path: Path) -> None:
    connection = _connect(tmp_path / "db.sqlite3")
    try:
        apply_migrations(connection, clock=lambda: 1_000_000)
        apply_migrations(connection, clock=lambda: 2_000_000)
        assert len(_applied(connection)) == 1
        assert _applied(connection)[0][3] == 1_000_000
    finally:
        connection.close()


@pytest.mark.parametrize("change", ["bytes", "name"])
def test_migration_checksum_change_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    package, schema = _package(
        tmp_path, monkeypatch, {"001_migration_metadata.sql": SCHEMA_SQL}
    )
    connection = _connect(tmp_path / "db.sqlite3")
    try:
        apply_migrations(connection, package=package, clock=lambda: 1_000_000)
        if change == "bytes":
            (schema / "001_migration_metadata.sql").write_bytes(SCHEMA_SQL + b"\n")
        else:
            (schema / "001_migration_metadata.sql").rename(
                schema / "001_renamed_metadata.sql"
            )
        before = _applied(connection)

        with pytest.raises(MigrationError, match="applied migration"):
            apply_migrations(connection, package=package, clock=lambda: 2_000_000)

        assert _applied(connection) == before
    finally:
        connection.close()


def test_installed_wheel_loads_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = f"wheel_fixture_{uuid.uuid4().hex}"
    archive_path = tmp_path / "fixture.zip"
    migration_name = "001_migration_metadata.sql"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(f"{package}/__init__.py", "")
        archive.writestr(f"{package}/schema/__init__.py", "")
        archive.writestr(f"{package}/schema/{migration_name}", SCHEMA_SQL)
    monkeypatch.syspath_prepend(str(archive_path))
    importlib.invalidate_caches()
    resource = importlib.resources.files(f"{package}.schema") / migration_name
    assert not isinstance(resource, Path)

    connection = _connect(tmp_path / "db.sqlite3")
    try:
        apply_migrations(
            connection, package=f"{package}.schema", clock=lambda: 1_234_567
        )
        assert _applied(connection) == [
            (
                1,
                migration_name,
                hashlib.sha256(SCHEMA_SQL).hexdigest(),
                1_234_567,
            )
        ]
    finally:
        connection.close()


@pytest.mark.parametrize(
    "filenames,match",
    [
        (["001_ok.sql", "003_gap.sql"], "contiguous"),
        (["001_ok.sql", "001_duplicate.sql"], "duplicate"),
        (["000_zero.sql"], "filename"),
        (["01_short.sql"], "filename"),
        (["001_Bad.sql"], "filename"),
        (["001_bad-name.sql"], "filename"),
    ],
)
def test_migration_filename_chain_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filenames: list[str],
    match: str,
) -> None:
    package, _schema = _package(
        tmp_path, monkeypatch, {name: b"SELECT 1;\n" for name in filenames}
    )
    with pytest.raises(MigrationError, match=match):
        load_migrations(package)


def test_runtime_pycache_does_not_break_resource_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, schema = _package(tmp_path, monkeypatch, {"001_ok.sql": b"SELECT 1;\n"})
    pycache = schema / "__pycache__"
    pycache.mkdir()
    (pycache / "__init__.cpython-312.pyc").write_bytes(b"runtime cache")

    assert [migration.name for migration in load_migrations(package)] == ["001_ok.sql"]


def test_non_utf8_and_oversize_migrations_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, schema = _package(tmp_path, monkeypatch, {"001_invalid.sql": b"\xff"})
    with pytest.raises(MigrationError, match="UTF-8"):
        load_migrations(package)

    (schema / "001_invalid.sql").write_bytes(b" " * (MAX_MIGRATION_BYTES + 1))
    with pytest.raises(MigrationError, match="maximum size"):
        load_migrations(package)


def test_symlink_special_unreadable_and_extra_resources_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, schema = _package(tmp_path, monkeypatch, {"001_ok.sql": b"SELECT 1;\n"})
    target = tmp_path / "target.sql"
    target.write_text("SELECT 1;\n", encoding="utf-8")
    symlink = schema / "002_link.sql"
    symlink.symlink_to(target)
    with pytest.raises(MigrationError, match="symlink"):
        load_migrations(package)
    symlink.unlink()

    fifo = schema / "002_fifo.sql"
    os.mkfifo(fifo)
    try:
        with pytest.raises(MigrationError, match="regular file"):
            load_migrations(package)
    finally:
        fifo.unlink()

    migration = schema / "001_ok.sql"
    migration.chmod(0)
    try:
        with pytest.raises(MigrationError, match="unreadable"):
            load_migrations(package)
    finally:
        migration.chmod(0o600)

    (schema / "notes.txt").write_text("not a migration", encoding="utf-8")
    with pytest.raises(MigrationError, match="unexpected resource"):
        load_migrations(package)


def test_failed_second_statement_rolls_back_entire_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, _schema = _package(
        tmp_path,
        monkeypatch,
        {
            "001_migration_metadata.sql": SCHEMA_SQL,
            "002_failure.sql": (
                b"CREATE TABLE must_rollback (id INTEGER PRIMARY KEY) STRICT;\n"
                b"INSERT INTO table_that_does_not_exist VALUES (1);\n"
            ),
        },
    )
    connection = _connect(tmp_path / "db.sqlite3")
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            apply_migrations(connection, package=package, clock=lambda: 1_000_000)
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'must_rollback'"
            ).fetchone()
            is None
        )
        assert [row[0] for row in _applied(connection)] == [1]
        assert not connection.in_transaction
    finally:
        connection.close()


def test_trigger_aware_statement_parsing_and_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trigger_sql = b"""
CREATE TABLE guarded (id INTEGER PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TRIGGER guarded_no_update
BEFORE UPDATE ON guarded
BEGIN
    SELECT RAISE(ABORT, 'immutable');
END;
INSERT INTO guarded (id, value) VALUES (1, 'original');
"""
    statements = split_sql_statements(trigger_sql.decode("utf-8"))
    assert len(statements) == 3
    assert "SELECT RAISE" in statements[1]

    package, _schema = _package(
        tmp_path,
        monkeypatch,
        {
            "001_migration_metadata.sql": SCHEMA_SQL,
            "002_guard.sql": trigger_sql,
        },
    )
    connection = _connect(tmp_path / "db.sqlite3")
    try:
        apply_migrations(connection, package=package, clock=lambda: 1_000_000)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE guarded SET value = 'changed' WHERE id = 1")
    finally:
        connection.close()


@pytest.mark.parametrize(
    "forbidden",
    [
        "BEGIN;",
        "BEGIN IMMEDIATE;",
        "COMMIT;",
        "ROLLBACK;",
        "SAVEPOINT nested;",
        "RELEASE nested;",
        "ATTACH DATABASE 'other.db' AS other;",
        "DETACH DATABASE other;",
        "PRAGMA journal_mode = DELETE;",
    ],
)
def test_transaction_control_and_attach_sql_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forbidden: str,
) -> None:
    package, _schema = _package(
        tmp_path,
        monkeypatch,
        {
            "001_migration_metadata.sql": SCHEMA_SQL,
            "002_forbidden.sql": forbidden.encode(),
        },
    )
    connection = _connect(tmp_path / "db.sqlite3")
    try:
        with pytest.raises(MigrationError, match="forbidden SQL"):
            apply_migrations(connection, package=package, clock=lambda: 1_000_000)
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'schema_migrations'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


def test_policy_words_in_comments_and_strings_are_not_false_positives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sql = b"""
-- BEGIN COMMIT ROLLBACK SAVEPOINT ATTACH PRAGMA journal_mode
CREATE TABLE policy_words (value TEXT NOT NULL) STRICT;
INSERT INTO policy_words VALUES (
    'BEGIN; COMMIT; ROLLBACK; SAVEPOINT; ATTACH; PRAGMA journal_mode;'
);
/* ATTACH DATABASE and BEGIN are text here. */
"""
    package, _schema = _package(
        tmp_path,
        monkeypatch,
        {"001_migration_metadata.sql": SCHEMA_SQL, "002_words.sql": sql},
    )
    connection = _connect(tmp_path / "db.sqlite3")
    try:
        apply_migrations(connection, package=package, clock=lambda: 1_000_000)
        row = connection.execute("SELECT count(*) FROM policy_words").fetchone()
        assert row[0] == 1
    finally:
        connection.close()


@pytest.mark.parametrize(
    "versions,match",
    [
        ([1, 3], "gap"),
        ([1, 99], "unknown future"),
        ([1, 2], "missing packaged"),
    ],
)
def test_invalid_applied_migration_chain_rejected(
    tmp_path: Path,
    versions: list[int],
    match: str,
) -> None:
    connection = _connect(tmp_path / "db.sqlite3")
    try:
        apply_migrations(connection, clock=lambda: 1_000_000)
        for version in versions[1:]:
            connection.execute(
                "INSERT INTO schema_migrations "
                "(version, name, sha256, applied_at_us) VALUES (?, ?, ?, ?)",
                (
                    version,
                    f"{version:03d}_unknown.sql",
                    "a" * 64,
                    1_000_000 + version,
                ),
            )
        with pytest.raises(MigrationError, match=match):
            apply_migrations(connection, clock=lambda: 2_000_000)
    finally:
        connection.close()


def test_clock_must_return_builtin_integer_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, _schema = _package(
        tmp_path, monkeypatch, {"001_migration_metadata.sql": SCHEMA_SQL}
    )
    connection = _connect(tmp_path / "db.sqlite3")
    try:
        with pytest.raises(TypeError, match="clock"):
            apply_migrations(connection, package=package, clock=lambda: True)
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'schema_migrations'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


def test_concurrent_migration_attempts_serialize(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    barrier = threading.Barrier(2)

    def migrate() -> list[tuple[object, ...]]:
        connection = _connect(database, timeout=5_000)
        try:
            barrier.wait(timeout=5)
            apply_migrations(connection, clock=lambda: 1_000_000)
            return _applied(connection)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: migrate(), range(2)))

    assert len(results[0]) == 1
    assert results[0] == results[1]

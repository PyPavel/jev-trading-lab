from __future__ import annotations

import hashlib
import importlib
import importlib.resources
import io
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

from jev_trading_lab.core.time import MAX_DATETIME_US
from jev_trading_lab.persistence import db as db_module
from jev_trading_lab.persistence import migrations as migration_module
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
        path if path.is_absolute() else path.absolute(),
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
        with pytest.raises(DatabaseConfigurationError, match="unsupported filesystem"):
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
        _connect(tmp_path / "private" / "db.sqlite3")
    assert MIN_SQLITE_VERSION == (3, 37, 0)
    assert not opened


def test_pragma_setup_failure_closes_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_connect = sqlite3.connect
    connection: sqlite3.Connection | None = None

    def memory_connect(*_args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal connection
        factory = kwargs["factory"]
        assert isinstance(factory, type)
        connection = real_connect(  # type: ignore[call-overload]
            ":memory:", isolation_level=None, factory=factory
        )
        assert connection is not None
        return connection

    monkeypatch.setattr(db_module.sqlite3, "connect", memory_connect)

    with pytest.raises(DatabaseConfigurationError, match="inode"):
        _connect(tmp_path / "private" / "db.sqlite3")

    assert connection is not None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_clean_database_applies_only_migration_metadata(tmp_path: Path) -> None:
    connection = _connect(tmp_path / "private" / "db.sqlite3")
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
    connection = _connect(tmp_path / "private" / "db.sqlite3")
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
    connection = _connect(tmp_path / "private" / "db.sqlite3")
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

    connection = _connect(tmp_path / "private" / "db.sqlite3")
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
    connection = _connect(tmp_path / "private" / "db.sqlite3")
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
    connection = _connect(tmp_path / "private" / "db.sqlite3")
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
    connection = _connect(tmp_path / "private" / "db.sqlite3")
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
    connection = _connect(tmp_path / "private" / "db.sqlite3")
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
    connection = _connect(tmp_path / "private" / "db.sqlite3")
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
    connection = _connect(tmp_path / "private" / "db.sqlite3")
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
    database = tmp_path / "private" / "db.sqlite3"
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


def test_database_path_must_be_absolute_and_lexically_normalized(
    tmp_path: Path,
) -> None:
    relative = Path("relative/db.sqlite3")
    with pytest.raises(ValueError, match="absolute"):
        connect_database(
            relative,
            busy_timeout_ms=1_000,
            mount_resolver=lambda _path: "ext4",
        )

    crossing = tmp_path / "private" / ".." / "escaped.sqlite3"
    with pytest.raises(ValueError, match="normalized"):
        _connect(crossing)
    assert not (tmp_path / "escaped.sqlite3").exists()


def test_mountinfo_last_stacked_mount_wins_and_matches_components() -> None:
    mountinfo = (
        "1 0 8:1 / / rw - ext4 /dev/root rw\n"
        "2 1 8:2 / /data rw - ext4 /dev/a rw\n"
        "3 1 0:3 / /database rw - nfs server:/database rw\n"
        "4 1 0:4 / /data rw - nfs server:/data rw\n"
    )
    assert (
        db_module._filesystem_type_from_mountinfo(Path("/data/db.sqlite3"), mountinfo)
        == "nfs"
    )
    assert (
        db_module._filesystem_type_from_mountinfo(
            Path("/database/db.sqlite3"), mountinfo
        )
        == "nfs"
    )


@pytest.mark.parametrize(
    "filesystem",
    ("ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "zfs", "overlay", "tmpfs"),
)
def test_reviewed_local_filesystems_are_allowed(
    tmp_path: Path, filesystem: str
) -> None:
    connection = connect_database(
        tmp_path / filesystem / "db.sqlite3",
        busy_timeout_ms=1_000,
        mount_resolver=lambda _path: filesystem,
    )
    connection.close()


@pytest.mark.parametrize(
    "filesystem",
    (
        "unknownfs",
        "fuse",
        "fuse.rclone",
        "fuse.s3fs",
        "nfs",
        "nfs4",
        "cifs",
        "smb3",
        "smbfs",
        "9p",
        "ceph",
        "glusterfs",
        "lustre",
        "afs",
        "davfs",
    ),
)
def test_unknown_remote_and_fuse_filesystems_fail_closed(
    tmp_path: Path, filesystem: str
) -> None:
    with pytest.raises(DatabaseConfigurationError, match="unsupported filesystem"):
        connect_database(
            tmp_path / filesystem / "db.sqlite3",
            busy_timeout_ms=1_000,
            mount_resolver=lambda _path: filesystem,
        )


@pytest.mark.parametrize("mode", (0o755, 0o777))
def test_existing_database_parent_requires_exact_private_mode(
    tmp_path: Path, mode: int
) -> None:
    parent = tmp_path / f"unsafe-{mode:o}"
    parent.mkdir(mode=mode)
    parent.chmod(mode)

    with pytest.raises(DatabaseConfigurationError, match="mode 0700"):
        _connect(parent / "db.sqlite3")
    assert stat.S_IMODE(parent.stat().st_mode) == mode


def test_existing_database_requires_owner_mode_and_single_link(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    database = parent / "db.sqlite3"
    database.touch(mode=0o644)
    with pytest.raises(DatabaseConfigurationError, match="mode 0600"):
        _connect(database)
    assert stat.S_IMODE(database.stat().st_mode) == 0o644

    database.chmod(0o600)
    hardlink = parent / "linked.sqlite3"
    hardlink.hardlink_to(database)
    with pytest.raises(DatabaseConfigurationError, match="hard links"):
        _connect(database)


def test_parent_replacement_during_connect_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    database = parent / "db.sqlite3"
    original = tmp_path / "original"
    real_connect = sqlite3.connect

    def replace_parent(*args: object, **kwargs: object) -> sqlite3.Connection:
        parent.rename(original)
        parent.mkdir(mode=0o700)
        return real_connect(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(db_module.sqlite3, "connect", replace_parent)
    with pytest.raises(DatabaseConfigurationError, match="validated parent"):
        _connect(database)


def test_database_path_replacement_during_connect_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    database = parent / "db.sqlite3"
    database.touch(mode=0o600)
    real_connect = sqlite3.connect

    def replace_database(*args: object, **kwargs: object) -> sqlite3.Connection:
        database.unlink()
        database.touch(mode=0o600)
        return real_connect(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(db_module.sqlite3, "connect", replace_database)
    with pytest.raises(DatabaseConfigurationError, match="validated inode"):
        _connect(database)


def test_database_sidecars_remain_private(tmp_path: Path) -> None:
    database = tmp_path / "private" / "db.sqlite3"
    connection = _connect(database)
    try:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY) STRICT")
        connection.execute("INSERT INTO sample VALUES (1)")
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{database}{suffix}")
            assert sidecar.is_file()
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    finally:
        connection.close()


def test_apply_rejects_caller_transaction_without_rollback(tmp_path: Path) -> None:
    connection = _connect(tmp_path / "private" / "db.sqlite3")
    try:
        connection.execute("CREATE TABLE caller_data (value TEXT NOT NULL) STRICT")
        connection.execute("BEGIN")
        connection.execute("INSERT INTO caller_data VALUES ('keep me')")

        with pytest.raises(MigrationError, match="active caller transaction"):
            apply_migrations(connection, clock=lambda: 1_000_000)

        assert connection.in_transaction
        assert (
            connection.execute("SELECT value FROM caller_data").fetchone()[0]
            == "keep me"
        )
        connection.execute("COMMIT")
        assert connection.execute("SELECT count(*) FROM caller_data").fetchone()[0] == 1
    finally:
        connection.close()


def test_apply_requires_autocommit_connection_contract() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        with pytest.raises(MigrationError, match="autocommit"):
            apply_migrations(connection)
    finally:
        connection.close()


def test_temp_schema_migrations_cannot_shadow_main(tmp_path: Path) -> None:
    connection = _connect(tmp_path / "private" / "db.sqlite3")
    try:
        connection.execute(
            "CREATE TEMP TABLE schema_migrations "
            "(version INTEGER, name TEXT, sha256 TEXT, applied_at_us INTEGER)"
        )
        connection.execute(
            "INSERT INTO temp.schema_migrations VALUES (99, 'shadow', 'bad', 1)"
        )

        apply_migrations(connection, clock=lambda: 1_000_000)

        assert (
            connection.execute(
                "SELECT count(*) FROM temp.schema_migrations"
            ).fetchone()[0]
            == 1
        )
        row = connection.execute(
            "SELECT version, name FROM main.schema_migrations"
        ).fetchone()
        assert tuple(row) == (1, "001_migration_metadata.sql")
    finally:
        connection.close()

    reopened = _connect(tmp_path / "private" / "db.sqlite3")
    try:
        apply_migrations(reopened, clock=lambda: 2_000_000)
        assert (
            reopened.execute("SELECT count(*) FROM main.schema_migrations").fetchone()[
                0
            ]
            == 1
        )
    finally:
        reopened.close()


def test_empty_migration_package_fails_before_database_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, _schema = _package(tmp_path, monkeypatch, {})
    connection = _connect(tmp_path / "private" / "db.sqlite3")
    try:
        with pytest.raises(MigrationError, match="at least migration 001"):
            apply_migrations(connection, package=package)
        assert (
            connection.execute(
                "SELECT 1 FROM main.sqlite_schema WHERE name = 'schema_migrations'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("version", "name"),
    (
        (2, "002_a/evil.sql"),
        (2, "002_a\\evil.sql"),
        (2, "002_A.sql"),
        (2, "002_.sql"),
        (2, "002_a-b.sql"),
        (2, "002_a.b.sql"),
        (2, "003_ok.sql"),
        (2, "002_bad\nname.sql"),
    ),
)
def test_schema_rejects_hostile_or_mismatched_migration_names(
    tmp_path: Path, version: int, name: str
) -> None:
    connection = _connect(tmp_path / "private" / "db.sqlite3")
    try:
        apply_migrations(connection, clock=lambda: 1_000_000)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO main.schema_migrations "
                "(version, name, sha256, applied_at_us) VALUES (?, ?, ?, ?)",
                (version, name, "a" * 64, 2_000_000),
            )
    finally:
        connection.close()


def test_filesystem_resource_replacement_with_symlink_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, schema = _package(tmp_path, monkeypatch, {"001_ok.sql": b"SELECT 1;\n"})
    migration = schema / "001_ok.sql"
    target = tmp_path / "replacement.sql"
    target.write_bytes(b"SELECT 2;\n")
    real_open = os.open
    replaced = False

    def replacing_open(
        path: object, flags: int, *args: object, **kwargs: object
    ) -> int:
        nonlocal replaced
        if path == migration.name and kwargs.get("dir_fd") is not None and not replaced:
            replaced = True
            migration.unlink()
            migration.symlink_to(target)
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(migration_module.os, "open", replacing_open)
    with pytest.raises(MigrationError, match="symlink|unreadable"):
        load_migrations(package)
    assert replaced


def test_archive_resource_read_is_bounded_to_maximum_plus_one() -> None:
    class RecordingStream(io.BytesIO):
        bytes_returned = 0

        def read(self, size: int | None = -1) -> bytes:
            result = super().read(size)
            self.bytes_returned += len(result)
            return result

    class ArchiveResource:
        name = "001_large.sql"

        def __init__(self) -> None:
            self.stream = RecordingStream(b"x" * (MAX_MIGRATION_BYTES + 100))
            self.read_bytes_called = False

        def open(self, mode: str) -> RecordingStream:
            assert mode == "rb"
            return self.stream

        def read_bytes(self) -> bytes:
            self.read_bytes_called = True
            return b"x" * (MAX_MIGRATION_BYTES + 100)

    resource = ArchiveResource()
    with pytest.raises(MigrationError, match="maximum size"):
        migration_module._read_resource(resource)  # type: ignore[arg-type]
    assert not resource.read_bytes_called
    assert resource.stream.bytes_returned == MAX_MIGRATION_BYTES + 1


def _proc_fd_targets() -> dict[int, str]:
    targets: dict[int, str] = {}
    for entry in Path("/proc/self/fd").iterdir():
        try:
            targets[int(entry.name)] = os.readlink(entry)
        except (FileNotFoundError, OSError, ValueError):
            continue
    return targets


def test_connection_retains_private_parent_descriptor_until_close(
    tmp_path: Path,
) -> None:
    database = tmp_path / "private" / "db.sqlite3"
    before = _proc_fd_targets()

    connection = _connect(database)
    during = _proc_fd_targets()
    parent_fds = [
        descriptor
        for descriptor, target in during.items()
        if descriptor not in before and target == str(database.parent)
    ]
    assert len(parent_fds) == 1

    connection.close()
    assert parent_fds[0] not in _proc_fd_targets()


def test_database_inode_swap_restored_before_path_recheck_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    database = parent / "db.sqlite3"
    database.touch(mode=0o600)
    replacement = tmp_path / "replacement.sqlite3"
    replacement.touch(mode=0o600)
    parked_original = tmp_path / "original.sqlite3"
    parked_replacement = tmp_path / "opened.sqlite3"
    real_connect = sqlite3.connect

    def swap_for_sqlite(*args: object, **kwargs: object) -> sqlite3.Connection:
        database.rename(parked_original)
        replacement.rename(database)
        connection = real_connect(*args, **kwargs)  # type: ignore[arg-type]
        database.rename(parked_replacement)
        parked_original.rename(database)
        return connection

    monkeypatch.setattr(db_module.sqlite3, "connect", swap_for_sqlite)
    before = set(_proc_fd_targets())
    with pytest.raises(DatabaseConfigurationError, match="inode"):
        _connect(database)
    assert set(_proc_fd_targets()) == before


def test_parent_path_swap_cannot_redirect_descriptor_relative_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    database = parent / "db.sqlite3"
    original = sqlite3.connect(database)
    original.execute("CREATE TABLE identity (value TEXT NOT NULL)")
    original.execute("INSERT INTO identity VALUES ('original')")
    original.commit()
    original.close()
    database.chmod(0o600)

    replacement_parent = tmp_path / "replacement"
    replacement_parent.mkdir(mode=0o700)
    replacement_database = replacement_parent / database.name
    replacement = sqlite3.connect(replacement_database)
    replacement.execute("CREATE TABLE identity (value TEXT NOT NULL)")
    replacement.execute("INSERT INTO identity VALUES ('replacement')")
    replacement.commit()
    replacement.close()
    replacement_database.chmod(0o600)

    parked_original = tmp_path / "parked-original"
    parked_replacement = tmp_path / "parked-replacement"
    real_connect = sqlite3.connect

    def swap_parent_for_sqlite(*args: object, **kwargs: object) -> sqlite3.Connection:
        parent.rename(parked_original)
        replacement_parent.rename(parent)
        connection = real_connect(*args, **kwargs)  # type: ignore[arg-type]
        parent.rename(parked_replacement)
        parked_original.rename(parent)
        return connection

    monkeypatch.setattr(db_module.sqlite3, "connect", swap_parent_for_sqlite)
    with pytest.raises(DatabaseConfigurationError, match="validated parent"):
        _connect(database)


@pytest.mark.parametrize("suffix", ("-wal", "-shm"))
def test_preexisting_unsafe_sidecar_mode_is_rejected(
    tmp_path: Path, suffix: str
) -> None:
    database = tmp_path / "private" / "db.sqlite3"
    database.parent.mkdir(mode=0o700)
    database.touch(mode=0o600)
    sidecar = Path(f"{database}{suffix}")
    sidecar.touch(mode=0o644)
    sidecar.chmod(0o644)

    with pytest.raises(DatabaseConfigurationError, match="sidecar.*0600"):
        _connect(database)
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o644


@pytest.mark.parametrize("suffix", ("-wal", "-shm"))
def test_preexisting_sidecar_symlink_is_rejected(tmp_path: Path, suffix: str) -> None:
    database = tmp_path / "private" / "db.sqlite3"
    database.parent.mkdir(mode=0o700)
    database.touch(mode=0o600)
    target = tmp_path / "target"
    target.touch(mode=0o600)
    Path(f"{database}{suffix}").symlink_to(target)

    with pytest.raises(DatabaseConfigurationError, match="sidecar.*symlink"):
        _connect(database)


@pytest.mark.parametrize("suffix", ("-wal", "-shm"))
def test_preexisting_sidecar_hardlink_is_rejected(tmp_path: Path, suffix: str) -> None:
    database = tmp_path / "private" / "db.sqlite3"
    database.parent.mkdir(mode=0o700)
    database.touch(mode=0o600)
    target = tmp_path / "target"
    target.touch(mode=0o600)
    Path(f"{database}{suffix}").hardlink_to(target)

    with pytest.raises(DatabaseConfigurationError, match="sidecar.*hard link"):
        _connect(database)


def test_sidecar_created_racing_wal_activation_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "private" / "db.sqlite3"
    real_connect = sqlite3.connect

    class RacingConnection(sqlite3.Connection):
        raced = False

        def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:
            if sql == "PRAGMA journal_mode = WAL" and not self.raced:
                self.raced = True
                sidecar = Path(f"{database}-wal")
                sidecar.touch(mode=0o644)
                sidecar.chmod(0o644)
            return super().execute(sql, parameters)  # type: ignore[arg-type]

    def racing_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        class SecureRacingConnection(RacingConnection, db_module.SecureConnection):
            pass

        kwargs["factory"] = SecureRacingConnection
        return real_connect(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(db_module.sqlite3, "connect", racing_connect)
    with pytest.raises(DatabaseConfigurationError, match="sidecar.*0600"):
        _connect(database)


def test_world_writable_migration_package_directory_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, schema = _package(tmp_path, monkeypatch, {"001_ok.sql": b"SELECT 1;\n"})
    schema.chmod(0o777)

    with pytest.raises(MigrationError, match="package directory.*writable"):
        load_migrations(package)


def test_group_writable_migration_file_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, schema = _package(tmp_path, monkeypatch, {"001_ok.sql": b"SELECT 1;\n"})
    migration = schema / "001_ok.sql"
    migration.chmod(0o660)

    with pytest.raises(MigrationError, match="resource.*writable"):
        load_migrations(package)


def test_equal_size_resource_rewrite_during_read_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resource = tmp_path / "001_race.sql"
    original = b"A" * 1024
    resource.write_bytes(original)
    real_read = os.read
    mutated = False

    def rewriting_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        content = real_read(descriptor, size)
        if content and not mutated:
            mutated = True
            resource.write_bytes(b"B" * len(original))
        return content

    monkeypatch.setattr(migration_module.os, "read", rewriting_read)
    with pytest.raises(MigrationError, match="changed while reading"):
        migration_module._read_filesystem_resource(resource)
    assert mutated


def test_mixed_resource_bytes_from_in_place_rewrite_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resource = tmp_path / "001_race.sql"
    resource.write_bytes(b"A" * (65_536 * 2))
    real_read = os.read
    reads = 0

    def rewriting_read(descriptor: int, size: int) -> bytes:
        nonlocal reads
        content = real_read(descriptor, size)
        reads += 1
        if reads == 1:
            with resource.open("r+b") as stream:
                stream.seek(65_536)
                stream.write(b"B" * 65_536)
        return content

    monkeypatch.setattr(migration_module.os, "read", rewriting_read)
    with pytest.raises(MigrationError, match="changed while reading"):
        migration_module._read_filesystem_resource(resource)
    assert reads >= 2


def test_migration_rejects_pre_attached_auxiliary_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, _schema = _package(
        tmp_path,
        monkeypatch,
        {
            "001_migration_metadata.sql": SCHEMA_SQL,
            "002_off_main.sql": b"CREATE TABLE aux.off_main (id INTEGER);\n",
        },
    )
    database = tmp_path / "private" / "db.sqlite3"
    auxiliary = tmp_path / "aux.sqlite3"
    connection = _connect(database)
    try:
        connection.execute("ATTACH DATABASE ? AS aux", (str(auxiliary),))
        with pytest.raises(MigrationError, match="auxiliary database"):
            apply_migrations(connection, package=package, clock=lambda: 1_000_000)
        assert not connection.in_transaction
        assert (
            connection.execute(
                "SELECT 1 FROM main.sqlite_schema WHERE name = 'schema_migrations'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM aux.sqlite_schema WHERE name = 'off_main'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()

    reopened = sqlite3.connect(auxiliary)
    try:
        assert (
            reopened.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'off_main'"
            ).fetchone()
            is None
        )
    finally:
        reopened.close()


def test_create_temp_migration_is_denied_and_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, _schema = _package(
        tmp_path,
        monkeypatch,
        {
            "001_migration_metadata.sql": SCHEMA_SQL,
            "002_temp.sql": b"CREATE TEMP TABLE off_main (id INTEGER);\n",
        },
    )
    database = tmp_path / "private" / "db.sqlite3"
    connection = _connect(database)
    try:
        with pytest.raises(MigrationError, match="TEMP|authorized"):
            apply_migrations(connection, package=package, clock=lambda: 1_000_000)
        assert not connection.in_transaction
        assert (
            connection.execute(
                "SELECT 1 FROM temp.sqlite_schema WHERE name = 'off_main'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM main.sqlite_schema WHERE name = 'schema_migrations'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()

    reopened = _connect(database)
    try:
        assert (
            reopened.execute(
                "SELECT 1 FROM temp.sqlite_schema WHERE name = 'off_main'"
            ).fetchone()
            is None
        )
        assert (
            reopened.execute(
                "SELECT 1 FROM main.sqlite_schema WHERE name = 'schema_migrations'"
            ).fetchone()
            is None
        )
    finally:
        reopened.close()


@pytest.mark.parametrize("invalid", (-1, 2**63 - 1))
def test_schema_rejects_invalid_applied_timestamp(tmp_path: Path, invalid: int) -> None:
    connection = _connect(tmp_path / "private" / "db.sqlite3")
    try:
        apply_migrations(connection, clock=lambda: 0)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO main.schema_migrations "
                "(version, name, sha256, applied_at_us) VALUES (?, ?, ?, ?)",
                (2, "002_valid.sql", "a" * 64, invalid),
            )
        assert _applied(connection)[0][3] == 0
    finally:
        connection.close()


@pytest.mark.parametrize("invalid", (-1, 2**63 - 1))
def test_reconnect_detects_corrupt_applied_timestamp(
    tmp_path: Path, invalid: int
) -> None:
    database = tmp_path / "private" / "db.sqlite3"
    connection = _connect(database)
    try:
        apply_migrations(connection, clock=lambda: 0)
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE main.schema_migrations SET applied_at_us = ? WHERE version = 1",
            (invalid,),
        )
        connection.execute("PRAGMA ignore_check_constraints = OFF")
    finally:
        connection.close()

    reopened = _connect(database)
    try:
        with pytest.raises(MigrationError, match="applied migration metadata"):
            apply_migrations(reopened, clock=lambda: MAX_DATETIME_US)
    finally:
        reopened.close()


def test_database_parent_rejects_unexpected_entry(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    (parent / "parked.sqlite3").touch(mode=0o600)

    with pytest.raises(DatabaseConfigurationError, match="unexpected entry"):
        _connect(parent / "db.sqlite3")


def test_decoy_fd_cannot_hide_sqlite_opening_replaced_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    database = parent / "db.sqlite3"
    database.touch(mode=0o600)
    replacement = tmp_path / "replacement.sqlite3"
    replacement.touch(mode=0o600)
    parked_original = tmp_path / "original.sqlite3"
    parked_replacement = tmp_path / "opened.sqlite3"
    held: list[int] = []
    real_connect = sqlite3.connect

    def swap_and_open_decoy(*args: object, **kwargs: object) -> sqlite3.Connection:
        database.rename(parked_original)
        replacement.rename(database)
        connection = real_connect(*args, **kwargs)  # type: ignore[arg-type]
        database.rename(parked_replacement)
        parked_original.rename(database)
        held.append(os.open(database, os.O_RDONLY))
        return connection

    monkeypatch.setattr(db_module.sqlite3, "connect", swap_and_open_decoy)
    before = set(_proc_fd_targets())
    try:
        with pytest.raises(DatabaseConfigurationError, match="inode|ambiguity"):
            _connect(database)
    finally:
        for descriptor in held:
            os.close(descriptor)
    assert set(_proc_fd_targets()) == before


def test_deleted_sqlite_inode_cannot_be_hidden_by_decoy_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    database = parent / "db.sqlite3"
    database.touch(mode=0o600)
    replacement = tmp_path / "replacement.sqlite3"
    replacement.touch(mode=0o600)
    parked_original = tmp_path / "original.sqlite3"
    parked_replacement = tmp_path / "opened.sqlite3"
    held: list[int] = []
    real_connect = sqlite3.connect

    def swap_unlink_and_open_decoy(
        *args: object, **kwargs: object
    ) -> sqlite3.Connection:
        database.rename(parked_original)
        replacement.rename(database)
        connection = real_connect(*args, **kwargs)  # type: ignore[arg-type]
        database.rename(parked_replacement)
        parked_original.rename(database)
        parked_replacement.unlink()
        held.append(os.open(database, os.O_RDONLY))
        return connection

    monkeypatch.setattr(db_module.sqlite3, "connect", swap_unlink_and_open_decoy)
    before = set(_proc_fd_targets())
    try:
        with pytest.raises(DatabaseConfigurationError, match="inode|ambiguity"):
            _connect(database)
    finally:
        for descriptor in held:
            os.close(descriptor)
    assert set(_proc_fd_targets()) == before


def test_unsafe_open_sidecar_renamed_away_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "private" / "db.sqlite3"
    held: list[int] = []
    real_connect = sqlite3.connect

    class RacingConnection(sqlite3.Connection):
        raced = False

        def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:
            if sql == "PRAGMA journal_mode = WAL" and not self.raced:
                self.raced = True
                sidecar = Path(f"{database}-wal")
                sidecar.touch(mode=0o666)
                sidecar.chmod(0o666)
                held.append(os.open(sidecar, os.O_RDONLY))
                parked = database.parent / "parked-wal"
                sidecar.rename(parked)
                parked.unlink()
            return super().execute(sql, parameters)  # type: ignore[arg-type]

    def racing_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        class SecureRacingConnection(RacingConnection, db_module.SecureConnection):
            pass

        kwargs["factory"] = SecureRacingConnection
        return real_connect(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(db_module.sqlite3, "connect", racing_connect)
    try:
        with pytest.raises(DatabaseConfigurationError, match="sidecar|descriptor"):
            _connect(database)
    finally:
        for descriptor in held:
            os.close(descriptor)


def test_unrelated_new_regular_fd_does_not_block_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "private" / "db.sqlite3"
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("safe", encoding="utf-8")
    held: list[int] = []
    real_connect = sqlite3.connect

    def open_unrelated_fd(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)  # type: ignore[arg-type]
        held.append(os.open(unrelated, os.O_RDONLY))
        return connection

    monkeypatch.setattr(db_module.sqlite3, "connect", open_unrelated_fd)
    connection = _connect(database)
    try:
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    finally:
        connection.close()
        for descriptor in held:
            os.close(descriptor)


def test_connection_open_close_cycles_do_not_leak_descriptors(tmp_path: Path) -> None:
    database = tmp_path / "private" / "db.sqlite3"
    before = set(_proc_fd_targets())
    for _ in range(100):
        connection = _connect(database)
        connection.close()
    assert set(_proc_fd_targets()) == before


def test_schema_directory_swap_restore_is_rejected_before_sql_executes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, schema = _package(
        tmp_path / "package",
        monkeypatch,
        {"001_safe.sql": SCHEMA_SQL},
    )
    attacker = tmp_path / "attacker-schema"
    attacker.mkdir()
    (attacker / "__init__.py").write_text("", encoding="utf-8")
    (attacker / "001_safe.sql").write_text(
        "CREATE TABLE main.attacker_ran (value TEXT);", encoding="utf-8"
    )
    parked = tmp_path / "parked-schema"
    returned_attacker = tmp_path / "returned-attacker"
    migration = schema / "001_safe.sql"
    real_open = os.open
    replaced = False

    def replacing_open(
        path: object, flags: int, *args: object, **kwargs: object
    ) -> int:
        nonlocal replaced
        if path == migration.name and kwargs.get("dir_fd") is not None and not replaced:
            replaced = True
            schema.rename(parked)
            attacker.rename(schema)
            descriptor = real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
            schema.rename(returned_attacker)
            parked.rename(schema)
            return descriptor
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(migration_module.os, "open", replacing_open)
    connection = _connect(tmp_path / "database" / "db.sqlite3")
    try:
        with pytest.raises(MigrationError, match="directory.*changed"):
            apply_migrations(connection, package=package, clock=lambda: 1_000_000)
        assert replaced
        assert (
            connection.execute(
                "SELECT 1 FROM main.sqlite_schema WHERE name = 'attacker_ran'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM main.sqlite_schema WHERE name = 'schema_migrations'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


def test_explicit_non_main_schema_is_rejected_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, _schema = _package(
        tmp_path,
        monkeypatch,
        {
            "001_migration_metadata.sql": SCHEMA_SQL,
            "002_aux.sql": b'CREATE TABLE "aux".off_main (value TEXT);\n',
        },
    )
    connection = _connect(tmp_path / "private" / "db.sqlite3")
    try:
        with pytest.raises(MigrationError, match="non-main schema"):
            apply_migrations(connection, package=package, clock=lambda: 1_000_000)
        assert not connection.in_transaction
        assert (
            connection.execute(
                "SELECT 1 FROM main.sqlite_schema WHERE name = 'schema_migrations'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


def test_quoted_temp_reindex_is_denied_without_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, _schema = _package(
        tmp_path,
        monkeypatch,
        {
            "001_migration_metadata.sql": SCHEMA_SQL,
            "002_reindex.sql": b'REINDEX "temp".idx;\n',
        },
    )
    connection = _connect(tmp_path / "database" / "db.sqlite3")
    try:
        connection.execute("CREATE TEMP TABLE sink (value TEXT)")
        connection.execute("CREATE INDEX temp.idx ON sink(value)")
        with pytest.raises(MigrationError, match="TEMP|authorized"):
            apply_migrations(connection, package=package, clock=lambda: 1_000_000)
        assert (
            connection.execute(
                "SELECT 1 FROM main.sqlite_schema WHERE name = 'schema_migrations'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


def test_temp_trigger_cannot_write_during_metadata_insert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, _schema = _package(
        tmp_path,
        monkeypatch,
        {"001_payload.sql": b"CREATE TABLE main.payload (value TEXT);\n"},
    )
    database = tmp_path / "database" / "db.sqlite3"
    connection = _connect(database)
    try:
        connection.executescript(SCHEMA_SQL.decode("utf-8"))
        connection.execute("CREATE TEMP TABLE sink (version INTEGER)")
        connection.execute(
            "CREATE TEMP TRIGGER capture_metadata "
            "AFTER INSERT ON main.schema_migrations "
            "BEGIN INSERT INTO sink VALUES (NEW.version); END"
        )

        with pytest.raises(MigrationError, match="authorized"):
            apply_migrations(connection, package=package, clock=lambda: 1_000_000)

        assert connection.execute("SELECT count(*) FROM temp.sink").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM main.schema_migrations"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT 1 FROM main.sqlite_schema WHERE name = 'payload'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()

    reopened = _connect(database)
    try:
        assert (
            reopened.execute("SELECT count(*) FROM main.schema_migrations").fetchone()[
                0
            ]
            == 0
        )
        assert (
            reopened.execute(
                "SELECT 1 FROM main.sqlite_schema WHERE name = 'payload'"
            ).fetchone()
            is None
        )
    finally:
        reopened.close()

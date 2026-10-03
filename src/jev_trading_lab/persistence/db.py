"""Create SQLite connections that satisfy the local persistence contract."""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from pathlib import Path
from typing import Protocol

MIN_SQLITE_VERSION = (3, 37, 0)
_MAX_BUSY_TIMEOUT_MS = 2_147_483_647
_NETWORK_FILESYSTEMS = frozenset(
    {
        "9p",
        "ceph",
        "cifs",
        "fuse.sshfs",
        "glusterfs",
        "nfs",
        "nfs4",
        "smbfs",
        "sshfs",
    }
)
_MOUNTINFO_ESCAPES = {
    "\\040": " ",
    "\\011": "\t",
    "\\012": "\n",
    "\\134": "\\",
}


class DatabaseConfigurationError(RuntimeError):
    """Raised when SQLite cannot meet the persistence contract."""


class MountResolver(Protocol):
    def __call__(self, path: Path) -> str: ...


def _decode_mountinfo_path(value: str) -> str:
    for escaped, decoded in _MOUNTINFO_ESCAPES.items():
        value = value.replace(escaped, decoded)
    return value


def _contains_path(mount_point: Path, path: Path) -> bool:
    try:
        path.relative_to(mount_point)
    except ValueError:
        return False
    return True


def _filesystem_type_from_mountinfo(path: Path, mountinfo: str) -> str:
    """Return the filesystem from the longest mount-point match."""
    absolute = path.absolute()
    matches: list[tuple[int, str]] = []
    for line in mountinfo.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
            mount_point = Path(_decode_mountinfo_path(fields[4]))
            filesystem = fields[separator + 1].casefold()
        except (IndexError, ValueError):
            continue
        if _contains_path(mount_point, absolute):
            matches.append((len(mount_point.parts), filesystem))
    if not matches:
        raise DatabaseConfigurationError(
            f"no mount information covers database path {absolute}"
        )
    return max(matches, key=lambda item: item[0])[1]


def _linux_mount_resolver(path: Path) -> str:
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise DatabaseConfigurationError(
            f"cannot verify local database filesystem: {error}"
        ) from error
    return _filesystem_type_from_mountinfo(path, mountinfo)


def _validate_busy_timeout(value: int) -> int:
    if type(value) is not int:
        raise TypeError("busy_timeout_ms must be a built-in integer")
    if not 0 < value <= _MAX_BUSY_TIMEOUT_MS:
        raise ValueError(
            f"busy_timeout_ms must be between 1 and {_MAX_BUSY_TIMEOUT_MS}"
        )
    return value


def _assert_not_symlink_or_special(path: Path, *, database: bool = False) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise DatabaseConfigurationError(f"cannot inspect {path}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise DatabaseConfigurationError(f"database path contains symlink: {path}")
    expected = stat.S_ISREG if database else stat.S_ISDIR
    if not expected(metadata.st_mode):
        kind = "database" if database else "parent"
        raise DatabaseConfigurationError(
            f"{kind} path is not the required file type: {path}"
        )


def _prepare_parent(parent: Path, resolver: MountResolver) -> None:
    filesystem = resolver(parent)
    if not isinstance(filesystem, str) or not filesystem:
        raise DatabaseConfigurationError(
            "mount resolver returned an invalid filesystem"
        )
    if filesystem.casefold() in _NETWORK_FILESYSTEMS:
        raise DatabaseConfigurationError(
            f"database path uses unsupported network filesystem {filesystem!r}"
        )

    for directory in reversed((parent, *parent.parents)):
        try:
            metadata = directory.lstat()
        except FileNotFoundError:
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError as error:
                raise DatabaseConfigurationError(
                    f"cannot create private database parent {directory}: {error}"
                ) from error
            _assert_not_symlink_or_special(directory)
            try:
                directory.chmod(0o700)
            except OSError as error:
                raise DatabaseConfigurationError(
                    f"cannot secure database parent {directory}: {error}"
                ) from error
        except OSError as error:
            raise DatabaseConfigurationError(
                f"cannot inspect database parent {directory}: {error}"
            ) from error
        else:
            if stat.S_ISLNK(metadata.st_mode):
                raise DatabaseConfigurationError(
                    f"database path contains symlink: {directory}"
                )
            if not stat.S_ISDIR(metadata.st_mode):
                raise DatabaseConfigurationError(
                    f"database parent is not a directory: {directory}"
                )

    filesystem = resolver(parent)
    if not isinstance(filesystem, str) or not filesystem:
        raise DatabaseConfigurationError(
            "mount resolver returned an invalid filesystem"
        )
    if filesystem.casefold() in _NETWORK_FILESYSTEMS:
        raise DatabaseConfigurationError(
            f"database path uses unsupported network filesystem {filesystem!r}"
        )


def _prepare_database_file(path: Path) -> tuple[int, int]:
    _assert_not_symlink_or_special(path, database=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise DatabaseConfigurationError(
                f"database path is not a regular file: {path}"
            )
        os.fchmod(descriptor, 0o600)
        return metadata.st_dev, metadata.st_ino
    except OSError as error:
        raise DatabaseConfigurationError(
            f"cannot securely open database file: {error}"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _verify_pragma(
    connection: sqlite3.Connection,
    pragma: str,
    expected: object,
    *,
    label: str,
) -> None:
    row = connection.execute(f"PRAGMA {pragma}").fetchone()
    actual = row[0] if row is not None else None
    if actual != expected:
        raise DatabaseConfigurationError(
            f"SQLite {label} setup failed: expected {expected!r}, received {actual!r}"
        )


def connect_database(
    path: Path,
    *,
    busy_timeout_ms: int,
    mount_resolver: MountResolver | None = None,
) -> sqlite3.Connection:
    """Open one non-URI local SQLite database with asserted safety PRAGMAs."""
    if not isinstance(path, Path):
        raise TypeError("database path must be a pathlib.Path")
    timeout = _validate_busy_timeout(busy_timeout_ms)
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        required = ".".join(str(part) for part in MIN_SQLITE_VERSION)
        actual_version = sqlite3.sqlite_version
        raise DatabaseConfigurationError(
            f"SQLite >= {required} is required; runtime is {actual_version}"
        )

    absolute = path.absolute()
    if absolute.name in {"", ".", ".."}:
        raise ValueError("database path must name a file")
    resolver = mount_resolver or _linux_mount_resolver
    _prepare_parent(absolute.parent, resolver)
    identity = _prepare_database_file(absolute)

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            str(absolute),
            timeout=timeout / 1_000,
            isolation_level=None,
            uri=False,
            check_same_thread=True,
        )
        metadata = absolute.lstat()
        file_identity = (metadata.st_dev, metadata.st_ino)
        if stat.S_ISLNK(metadata.st_mode) or file_identity != identity:
            raise DatabaseConfigurationError("database file changed while opening")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        _verify_pragma(connection, "foreign_keys", 1, label="foreign_keys")
        deadline = time.monotonic() + timeout / 1_000
        while True:
            try:
                journal = connection.execute("PRAGMA journal_mode = WAL").fetchone()
                break
            except sqlite3.OperationalError as error:
                if (
                    "locked" not in str(error).casefold()
                    or time.monotonic() >= deadline
                ):
                    raise
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        if journal is None or str(journal[0]).casefold() != "wal":
            actual = None if journal is None else journal[0]
            raise DatabaseConfigurationError(
                f"SQLite WAL setup failed: received {actual!r}"
            )
        connection.execute(f"PRAGMA busy_timeout = {timeout}")
        _verify_pragma(connection, "busy_timeout", timeout, label="busy_timeout")
        connection.execute("PRAGMA synchronous = FULL")
        _verify_pragma(connection, "synchronous", 2, label="synchronous")
        connection.execute("PRAGMA trusted_schema = OFF")
        _verify_pragma(connection, "trusted_schema", 0, label="trusted_schema")
        return connection
    except BaseException:
        if connection is not None:
            connection.close()
        raise

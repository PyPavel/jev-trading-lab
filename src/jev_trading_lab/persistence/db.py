"""Create SQLite connections that satisfy the local persistence contract."""

from __future__ import annotations

import errno
import os
import sqlite3
import stat
import time
from pathlib import Path
from typing import Protocol

MIN_SQLITE_VERSION = (3, 37, 0)
_MAX_BUSY_TIMEOUT_MS = 2_147_483_647
_LOCAL_FILESYSTEMS = frozenset(
    {
        "btrfs",
        "ext2",
        "ext3",
        "ext4",
        "f2fs",
        "overlay",
        "tmpfs",
        "xfs",
        "zfs",
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
    """Return the visible filesystem from the longest mount-point match."""
    matches: list[tuple[int, int, str]] = []
    for record, line in enumerate(mountinfo.splitlines()):
        fields = line.split()
        try:
            separator = fields.index("-")
            mount_point = Path(_decode_mountinfo_path(fields[4]))
            filesystem = fields[separator + 1].casefold()
        except (IndexError, ValueError):
            continue
        if _contains_path(mount_point, path):
            matches.append((len(mount_point.parts), record, filesystem))
    if not matches:
        raise DatabaseConfigurationError(
            f"no mount information covers database path {path}"
        )
    return max(matches, key=lambda item: (item[0], item[1]))[2]


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


def _validate_filesystem(path: Path, resolver: MountResolver) -> None:
    filesystem = resolver(path)
    if not isinstance(filesystem, str) or not filesystem:
        raise DatabaseConfigurationError(
            "mount resolver returned an invalid filesystem"
        )
    normalized = filesystem.casefold()
    if normalized not in _LOCAL_FILESYSTEMS:
        raise DatabaseConfigurationError(
            f"database path uses unsupported filesystem {filesystem!r}"
        )


def _directory_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _open_private_parent(parent: Path) -> int:
    """Traverse without symlinks and return an open descriptor for the 0700 parent."""
    flags = _directory_flags()
    current = os.open("/", flags)
    try:
        for component in parent.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=current)
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise DatabaseConfigurationError(
                        "database path contains symlink or non-directory "
                        f"component: {parent}"
                    ) from error
                raise DatabaseConfigurationError(
                    f"cannot securely traverse database parent {parent}: {error}"
                ) from error
            os.close(current)
            current = child

        metadata = os.fstat(current)
        if not stat.S_ISDIR(metadata.st_mode):
            raise DatabaseConfigurationError(
                f"database parent is not a directory: {parent}"
            )
        if metadata.st_uid != os.geteuid():
            raise DatabaseConfigurationError(
                f"database parent is not owned by effective UID: {parent}"
            )
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise DatabaseConfigurationError(
                f"database parent must have mode 0700: {parent}"
            )
        return current
    except BaseException:
        os.close(current)
        raise


def _validate_database_metadata(metadata: os.stat_result, path: Path) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise DatabaseConfigurationError(f"database path is not a regular file: {path}")
    if metadata.st_uid != os.geteuid():
        raise DatabaseConfigurationError(
            f"database file is not owned by effective UID: {path}"
        )
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise DatabaseConfigurationError(f"database file must have mode 0600: {path}")
    if metadata.st_nlink != 1:
        raise DatabaseConfigurationError(
            f"database file must not have hard links: {path}"
        )


def _prepare_database_file(path: Path, parent_descriptor: int) -> tuple[int, int]:
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path.name, flags, 0o600, dir_fd=parent_descriptor)
        metadata = os.fstat(descriptor)
        _validate_database_metadata(metadata, path)
        return metadata.st_dev, metadata.st_ino
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise DatabaseConfigurationError(
                f"database path contains symlink: {path}"
            ) from error
        raise DatabaseConfigurationError(
            f"cannot securely open database file: {error}"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _recheck_storage(
    path: Path,
    parent_descriptor: int,
    parent_identity: tuple[int, int],
    file_identity: tuple[int, int],
) -> None:
    descriptor_parent = os.fstat(parent_descriptor)
    try:
        pathname_parent = os.stat(path.parent, follow_symlinks=False)
        database = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError as error:
        raise DatabaseConfigurationError(
            f"database storage changed while opening: {error}"
        ) from error
    actual_parent = (pathname_parent.st_dev, pathname_parent.st_ino)
    if actual_parent != parent_identity or actual_parent != (
        descriptor_parent.st_dev,
        descriptor_parent.st_ino,
    ):
        raise DatabaseConfigurationError("database parent changed while opening")
    if (
        pathname_parent.st_uid != os.geteuid()
        or stat.S_IMODE(pathname_parent.st_mode) != 0o700
    ):
        raise DatabaseConfigurationError(
            "database parent security changed while opening"
        )
    _validate_database_metadata(database, path)
    if (database.st_dev, database.st_ino) != file_identity:
        raise DatabaseConfigurationError("database file changed while opening")


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
    """Open one local SQLite database inside an owned 0700 security boundary."""
    if not isinstance(path, Path):
        raise TypeError("database path must be a pathlib.Path")
    timeout = _validate_busy_timeout(busy_timeout_ms)
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        required = ".".join(str(part) for part in MIN_SQLITE_VERSION)
        raise DatabaseConfigurationError(
            f"SQLite >= {required} is required; runtime is {sqlite3.sqlite_version}"
        )
    if not path.is_absolute():
        raise ValueError("database path must be absolute")
    supplied = str(path)
    if os.path.normpath(supplied) != supplied or ".." in path.parts:
        raise ValueError("database path must be lexically normalized")
    if path.name in {"", ".", ".."}:
        raise ValueError("database path must name a file")

    resolver = mount_resolver or _linux_mount_resolver
    _validate_filesystem(path.parent, resolver)
    parent_descriptor = _open_private_parent(path.parent)
    parent_metadata = os.fstat(parent_descriptor)
    parent_identity = (parent_metadata.st_dev, parent_metadata.st_ino)
    connection: sqlite3.Connection | None = None
    try:
        _validate_filesystem(path.parent, resolver)
        file_identity = _prepare_database_file(path, parent_descriptor)
        connection = sqlite3.connect(
            supplied,
            timeout=timeout / 1_000,
            isolation_level=None,
            uri=False,
            check_same_thread=True,
        )
        _recheck_storage(path, parent_descriptor, parent_identity, file_identity)
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
        _recheck_storage(path, parent_descriptor, parent_identity, file_identity)
        return connection
    except BaseException:
        if connection is not None:
            connection.close()
        raise
    finally:
        os.close(parent_descriptor)

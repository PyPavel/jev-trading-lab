"""Create SQLite connections that satisfy the local persistence contract.

The supported lifetime boundary includes passive trace/profile/audit hooks, public API
callbacks using the public handle, cooperative cancellation at documented safe points
after ownership is established, and exceptions during blocking operations where
cleanup guards already own resources. Unsupported cases include asynchronous Python
signal/KeyboardInterrupt/SystemExit delivery during native resource acquisition or
before Python ownership/cleanup guard is established; a malicious trace/profile
callback raising at the exact bytecode between C resource acquisition and Python
ownership assignment; frame inspection; private helper import; base descriptors; and
process memory access. The implementation does not promise acquisition or bytecode
atomicity.
"""

from __future__ import annotations

import errno
import os
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

_Authorizer = Callable[[int, str | None, str | None, str | None, str | None], int]
_JOURNAL_TABLES = frozenset({"journal_entities", "journal_events"})
_TEMP_SCHEMA_ACTION_NAMES = (
    "SQLITE_CREATE_TEMP_INDEX",
    "SQLITE_CREATE_TEMP_TABLE",
    "SQLITE_CREATE_TEMP_TRIGGER",
    "SQLITE_CREATE_TEMP_VIEW",
    "SQLITE_DROP_TEMP_INDEX",
    "SQLITE_DROP_TEMP_TABLE",
    "SQLITE_DROP_TEMP_TRIGGER",
    "SQLITE_DROP_TEMP_VIEW",
)
_TEMP_SCHEMA_ACTIONS = frozenset(
    getattr(sqlite3, name)
    for name in _TEMP_SCHEMA_ACTION_NAMES
    if hasattr(sqlite3, name)
)
_SCHEMA_ACTIONS = (
    frozenset(
        {
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_CREATE_VTABLE,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_DROP_VTABLE,
        }
    )
    | _TEMP_SCHEMA_ACTIONS
)
_DANGEROUS_PRAGMAS = frozenset({"writable_schema", "wal_checkpoint"})
_SAFE_DBCONFIG = (
    ("SQLITE_DBCONFIG_WRITABLE_SCHEMA", False),
    ("SQLITE_DBCONFIG_ENABLE_TRIGGER", True),
    ("SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION", False),
    ("SQLITE_DBCONFIG_DEFENSIVE", True),
    ("SQLITE_DBCONFIG_TRUSTED_SCHEMA", False),
)

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
_UMASK_LOCK = threading.Lock()
_CONNECT_LOCK = threading.Lock()
_PUBLIC_CONNECTION = object()
_INTERNAL_CONNECTION = object()


class _DirectoryHandle:
    """Own one FD, relinquishing its number before an ambiguous ``os.close``."""

    __slots__ = ("_descriptor", "_lock")

    def __init__(self, descriptor: int) -> None:
        self._descriptor: int | None = descriptor
        self._lock = threading.RLock()

    @property
    def closed(self) -> bool:
        return self._descriptor is None

    @property
    def descriptor(self) -> int:
        descriptor = self._descriptor
        if descriptor is None:
            raise DatabaseConfigurationError("database directory handle is closed")
        return descriptor

    def close(self) -> None:
        with self._lock:
            descriptor = self._descriptor
            if descriptor is None:
                return
            self._descriptor = None
            os.close(descriptor)

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


def _close_directory_handle(handle: _DirectoryHandle) -> None:
    """Close while preserving an exception already active in the caller."""
    active = sys.exception()
    try:
        handle.close()
    except BaseException:
        if active is None:
            raise


def _close_sqlite_with_handle(
    close_sqlite: Callable[[], None], handle: _DirectoryHandle
) -> None:
    """Release provenance only after SQLite reports a successful close."""
    sqlite_closed = False
    try:
        close_sqlite()
        sqlite_closed = True
    finally:
        if sqlite_closed:
            _close_directory_handle(handle)


@contextmanager
def _connection_fd_activity() -> Iterator[None]:
    """Serialize supported library connection and FD activity.

    The descriptor census detects FDs created while this lock is held. Raw
    same-process code that bypasses this lock can close/reopen an FD with the
    same number and inode (ABA); that adversarial activity is outside the
    supported boundary. This library is not a sandbox for malicious in-process
    code.
    """
    with _CONNECT_LOCK:
        yield


class DatabaseConfigurationError(RuntimeError):
    """Raised when SQLite cannot meet the persistence contract."""


class DatabaseSafetyError(DatabaseConfigurationError):
    """Raised when database storage cannot satisfy the safety boundary."""


class MountResolver(Protocol):
    def __call__(self, path: Path) -> str: ...


class SecureConnection(sqlite3.Connection):
    """SQLite connection with sealed code callbacks and database mutation APIs.

    Supported callers may execute ordinary SQL but cannot replace trusted behavior
    through public instance callback, extension, deserialization, or blob methods
    after ``connect_database`` returns. Deliberate base-class descriptor dispatch,
    object-internal mutation, process-memory modification, and raw file writes are
    outside this same-process change-control boundary; this is not a sandbox.
    Passive audit hooks are supported. Deliberate ``sys.addaudithook`` SQLite
    handle capture and capability theft are out of scope.
    """

    __slots__ = (
        "_directory_handle",
        "_database_path",
        "_database_identity",
        "_busy_timeout_ms",
        "_authorizer_sealed",
        "_factory_token",
    )

    _directory_handle: _DirectoryHandle
    _database_path: Path
    _database_identity: tuple[int, int]
    _busy_timeout_ms: int
    _authorizer_sealed: bool
    _factory_token: object
    _close_sqlite = sqlite3.Connection.close
    _set_sqlite_authorizer = sqlite3.Connection.set_authorizer

    @property
    def authorizer_sealed(self) -> bool:
        """Return whether public mutation surfaces and runtime writes are sealed."""
        return getattr(self, "_authorizer_sealed", False)

    def _reject_if_sealed(self) -> None:
        if self.authorizer_sealed:
            raise RuntimeError(
                "secure connection authorizer, callback, and mutation APIs are sealed"
            )

    def set_authorizer(self, authorizer_callback: _Authorizer | None) -> None:
        """Set an authorizer only before the connection has been sealed."""
        self._reject_if_sealed()
        self._set_sqlite_authorizer(authorizer_callback)

    def set_trace_callback(self, trace_callback: Any) -> None:
        self._reject_if_sealed()
        super().set_trace_callback(trace_callback)

    def create_function(self, *args: Any, **kwargs: Any) -> None:
        self._reject_if_sealed()
        super().create_function(*args, **kwargs)

    def create_aggregate(self, *args: Any, **kwargs: Any) -> None:
        self._reject_if_sealed()
        super().create_aggregate(*args, **kwargs)

    def create_window_function(self, *args: Any, **kwargs: Any) -> None:
        self._reject_if_sealed()
        super().create_window_function(*args, **kwargs)

    def create_collation(self, *args: Any, **kwargs: Any) -> None:
        self._reject_if_sealed()
        super().create_collation(*args, **kwargs)

    def set_progress_handler(self, *args: Any, **kwargs: Any) -> None:
        self._reject_if_sealed()
        super().set_progress_handler(*args, **kwargs)

    def enable_load_extension(self, *args: Any, **kwargs: Any) -> None:
        self._reject_if_sealed()
        super().enable_load_extension(*args, **kwargs)

    if hasattr(sqlite3.Connection, "setconfig"):

        def setconfig(self, *args: Any, **kwargs: Any) -> bool:
            self._reject_if_sealed()
            return super().setconfig(*args, **kwargs)

    def load_extension(self, *args: Any, **kwargs: Any) -> None:
        self._reject_if_sealed()
        super().load_extension(*args, **kwargs)

    def deserialize(self, *args: Any, **kwargs: Any) -> None:
        self._reject_if_sealed()
        super().deserialize(*args, **kwargs)

    def blobopen(self, *args: Any, **kwargs: Any) -> sqlite3.Blob:
        self._reject_if_sealed()
        return super().blobopen(*args, **kwargs)

    def __getattribute__(self, name: str) -> object:
        if name in {
            "_factory_token",
            "_directory_handle",
            "_database_path",
            "_database_identity",
        }:
            raise AttributeError("connection provenance is internal")
        return super().__getattribute__(name)

    def __setattr__(self, name: str, value: object) -> None:
        if name in {
            "_busy_timeout_ms",
            "_authorizer_sealed",
            "_factory_token",
            "_directory_handle",
            "_database_path",
            "_database_identity",
        }:
            raise AttributeError(f"{name.removeprefix('_')} is immutable")
        if name in {"row_factory", "text_factory"} and self.authorizer_sealed:
            raise AttributeError(f"{name} is immutable after sealing")
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in {
            "_busy_timeout_ms",
            "_authorizer_sealed",
            "_factory_token",
            "_directory_handle",
            "_database_path",
            "_database_identity",
        }:
            raise AttributeError(f"{name.removeprefix('_')} is immutable")
        if name in {"row_factory", "text_factory"} and self.authorizer_sealed:
            raise AttributeError(f"{name} is immutable after sealing")
        super().__delattr__(name)

    @property
    def busy_timeout_ms(self) -> int:
        """Return the frozen busy timeout configured by ``connect_database``."""
        try:
            return self._busy_timeout_ms
        except AttributeError as error:
            raise DatabaseConfigurationError(
                "busy timeout has not been configured"
            ) from error

    def close(self) -> None:
        try:
            handle = _secure_slot(self, "_directory_handle")
        except AttributeError:
            self._close_sqlite()
            return
        assert isinstance(handle, _DirectoryHandle)
        _close_sqlite_with_handle(self._close_sqlite, handle)

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


class _InternalConnection(sqlite3.Connection):
    """Fixed-role connection retaining descriptor-verification state."""

    __slots__ = ("_before_fds", "_directory_handle", "_expected_identity", "_path")
    _before_fds: dict[int, tuple[int, int]]
    _directory_handle: _DirectoryHandle
    _expected_identity: tuple[int, int]
    _path: Path
    _close_sqlite = sqlite3.Connection.close

    def close(self) -> None:
        try:
            handle = self._directory_handle
        except AttributeError:
            self._close_sqlite()
            return
        _close_sqlite_with_handle(self._close_sqlite, handle)

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


def _runtime_authorizer(
    action: int,
    argument1: str | None,
    _argument2: str | None,
    database: str | None,
    _trigger: str | None,
) -> int:
    if action in {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}:
        return sqlite3.SQLITE_DENY
    if (
        action == sqlite3.SQLITE_PRAGMA
        and (argument1 or "").casefold() in _DANGEROUS_PRAGMAS
    ):
        return sqlite3.SQLITE_DENY
    if (
        action in _SCHEMA_ACTIONS
        or database == "temp"
        and action
        in {
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_DROP_VIEW,
        }
    ):
        return sqlite3.SQLITE_DENY
    if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
        if argument1 in _JOURNAL_TABLES or argument1 == "schema_migrations":
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _journal_authorizer(table: str) -> _Authorizer:
    if table not in _JOURNAL_TABLES:
        raise ValueError("journal write scope requires an exact journal table")

    allowed = frozenset(
        {
            sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_PRAGMA,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_TRANSACTION,
        }
    )

    def authorize(
        action: int,
        argument1: str | None,
        _argument2: str | None,
        database: str | None,
        trigger: str | None,
    ) -> int:
        if (
            action == sqlite3.SQLITE_INSERT
            and database == "main"
            and argument1 == table
            and trigger is None
        ):
            return sqlite3.SQLITE_OK
        if action in allowed and database != "temp":
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    return authorize


def _secure_slot(connection: SecureConnection, name: str) -> object:
    descriptor = SecureConnection.__dict__[name]
    return descriptor.__get__(connection, SecureConnection)


def _is_factory_connection(connection: object) -> bool:
    if type(connection) is not SecureConnection:
        return False
    try:
        sealed = _secure_slot(connection, "_authorizer_sealed")
        token = _secure_slot(connection, "_factory_token")
        path = _secure_slot(connection, "_database_path")
        identity = _secure_slot(connection, "_database_identity")
        handle = _secure_slot(connection, "_directory_handle")
    except AttributeError:
        return False
    return (
        sealed is True
        and token is _PUBLIC_CONNECTION
        and isinstance(path, Path)
        and isinstance(identity, tuple)
        and len(identity) == 2
        and isinstance(handle, _DirectoryHandle)
        and not handle.closed
    )


def _connection_locator(
    connection: SecureConnection,
) -> tuple[Path, int, tuple[int, int]]:
    if not _is_factory_connection(connection):
        raise DatabaseConfigurationError(
            "connection must be created and sealed by connect_database"
        )
    path = _secure_slot(connection, "_database_path")
    identity = _secure_slot(connection, "_database_identity")
    handle = _secure_slot(connection, "_directory_handle")
    assert isinstance(path, Path)
    assert isinstance(identity, tuple)
    assert isinstance(handle, _DirectoryHandle)
    metadata = os.stat(path.name, dir_fd=handle.descriptor, follow_symlinks=False)
    _validate_database_metadata(metadata, path)
    if (metadata.st_dev, metadata.st_ino) != identity:
        raise DatabaseSafetyError("database inode identity changed")
    _check_directory_entries(path, handle.descriptor)
    _check_sidecars(path, handle.descriptor)
    return path, connection.busy_timeout_ms, identity


def _reject_user_temp_schema(connection: SecureConnection) -> None:
    for row in connection.execute("SELECT type, name FROM temp.sqlite_schema"):
        object_type, name = row
        if type(name) is str and name.startswith("sqlite_"):
            continue
        raise DatabaseConfigurationError(
            f"TEMP schema contains unsupported user object {object_type} {name!r}"
        )


def _seal_runtime_authorizer(connection: SecureConnection) -> None:
    _reject_user_temp_schema(connection)
    connection._set_sqlite_authorizer(_runtime_authorizer)
    object.__setattr__(connection, "_authorizer_sealed", True)


def _verify_internal_connection_storage(connection: sqlite3.Connection) -> None:
    if type(connection) is not _InternalConnection:
        raise DatabaseConfigurationError("invalid internal database connection")
    path = connection._path
    identity = connection._expected_identity
    handle = connection._directory_handle
    _verify_sqlite_descriptors(
        connection._before_fds, identity, path, handle.descriptor
    )
    _verify_main_database_path(connection, path)
    metadata = os.stat(path.name, dir_fd=handle.descriptor, follow_symlinks=False)
    _validate_database_metadata(metadata, path)
    if (metadata.st_dev, metadata.st_ino) != identity:
        raise DatabaseSafetyError("database inode identity changed")
    _check_directory_entries(path, handle.descriptor)
    _check_sidecars(path, handle.descriptor)


def _internal_connection(
    path: Path,
    *,
    timeout_ms: int,
    authorizer: _Authorizer,
    expected_identity: tuple[int, int],
) -> sqlite3.Connection:
    """Open a fixed-role internal connection whose authorizer never changes."""
    if not path.is_absolute() or type(timeout_ms) is not int:
        raise DatabaseConfigurationError("invalid internal database locator")
    handle = _open_private_parent(path.parent)
    connection: _InternalConnection | None = None
    try:
        metadata = os.stat(path.name, dir_fd=handle.descriptor, follow_symlinks=False)
        _validate_database_metadata(metadata, path)
        if (metadata.st_dev, metadata.st_ino) != expected_identity:
            raise DatabaseSafetyError("database inode identity changed")
        _check_sidecars(path, handle.descriptor)
        before_fds = _open_fd_identities()
        connection = sqlite3.connect(
            f"/proc/self/fd/{handle.descriptor}/{path.name}",
            timeout=timeout_ms / 1_000,
            isolation_level=None,
            check_same_thread=True,
            factory=_InternalConnection,
        )
        connection._before_fds = before_fds
        connection._directory_handle = handle
        connection._expected_identity = expected_identity
        connection._path = path
        _verify_internal_connection_storage(connection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA recursive_triggers = ON")
        connection.execute(f"PRAGMA busy_timeout = {timeout_ms}")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.set_authorizer(authorizer)
        _verify_internal_connection_storage(connection)
        return connection
    except BaseException:
        if connection is not None:
            connection.close()
        else:
            _close_directory_handle(handle)
        raise


def _open_journal_writer(
    connection: SecureConnection, table: str
) -> sqlite3.Connection:
    path, timeout, identity = _connection_locator(connection)
    return _internal_connection(
        path,
        timeout_ms=timeout,
        authorizer=_journal_authorizer(table),
        expected_identity=identity,
    )


def _seal_if_migrated(connection: SecureConnection) -> None:
    row = connection.execute(
        "SELECT 1 FROM main.sqlite_schema WHERE type = 'table' "
        "AND name = 'journal_events'"
    ).fetchone()
    if row is not None and not connection.authorizer_sealed:
        _seal_runtime_authorizer(connection)


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


def _resolve_filesystem(path: Path, resolver: MountResolver) -> str:
    filesystem = resolver(path)
    if not isinstance(filesystem, str) or not filesystem:
        raise DatabaseSafetyError("mount resolver returned an invalid filesystem")
    normalized = filesystem.casefold()
    if normalized not in _LOCAL_FILESYSTEMS:
        raise DatabaseSafetyError(
            f"database path uses unsupported filesystem {filesystem!r}"
        )
    return normalized


def _validate_filesystem(path: Path, expected: str) -> None:
    filesystem = _resolve_filesystem(path, _linux_mount_resolver)
    if filesystem != expected:
        raise DatabaseSafetyError("database filesystem changed while opening")


def _directory_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _open_private_parent(parent: Path) -> _DirectoryHandle:
    """Traverse without symlinks and return an owned handle for the 0700 parent."""
    flags = _directory_flags()
    current = _DirectoryHandle(os.open("/", flags))
    try:
        for component in parent.parts[1:]:
            try:
                child = _DirectoryHandle(
                    os.open(component, flags, dir_fd=current.descriptor)
                )
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=current.descriptor)
                except FileExistsError:
                    pass
                child = _DirectoryHandle(
                    os.open(component, flags, dir_fd=current.descriptor)
                )
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise DatabaseConfigurationError(
                        "database path contains symlink or non-directory "
                        f"component: {parent}"
                    ) from error
                raise DatabaseConfigurationError(
                    f"cannot securely traverse database parent {parent}: {error}"
                ) from error
            current.close()
            current = child

        metadata = os.fstat(current.descriptor)
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
        _close_directory_handle(current)
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


def _prepare_database_file(
    path: Path, parent_descriptor: int
) -> tuple[_DirectoryHandle, tuple[int, int]]:
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    handle: _DirectoryHandle | None = None
    try:
        handle = _DirectoryHandle(
            os.open(path.name, flags, 0o600, dir_fd=parent_descriptor)
        )
        metadata = os.fstat(handle.descriptor)
        _validate_database_metadata(metadata, path)
        return handle, (metadata.st_dev, metadata.st_ino)
    except OSError as error:
        if handle is not None:
            _close_directory_handle(handle)
        if error.errno == errno.ELOOP:
            raise DatabaseConfigurationError(
                f"database path contains symlink: {path}"
            ) from error
        raise DatabaseConfigurationError(
            f"cannot securely open database file: {error}"
        ) from error
    except BaseException:
        if handle is not None:
            _close_directory_handle(handle)
        raise


def _open_fd_identities() -> dict[int, tuple[int, int]]:
    identities: dict[int, tuple[int, int]] = {}
    try:
        entries = os.scandir("/proc/self/fd")
    except OSError as error:
        raise DatabaseConfigurationError(
            f"cannot inspect SQLite file descriptors: {error}"
        ) from error
    with entries:
        for entry in entries:
            try:
                descriptor = int(entry.name)
                metadata = os.fstat(descriptor)
            except (OSError, ValueError):
                continue
            identities[descriptor] = (metadata.st_dev, metadata.st_ino)
    return identities


def _new_regular_descriptors(
    before: dict[int, tuple[int, int]],
) -> list[tuple[int, os.stat_result, str]]:
    descriptors: list[tuple[int, os.stat_result, str]] = []
    for descriptor, identity in _open_fd_identities().items():
        if before.get(descriptor) == identity:
            continue
        try:
            metadata = os.fstat(descriptor)
            target = os.readlink(f"/proc/self/fd/{descriptor}")
        except OSError:
            continue
        if stat.S_ISREG(metadata.st_mode):
            descriptors.append((descriptor, metadata, target))
    return sorted(descriptors)


def _target_path(target: str) -> Path:
    deleted = " (deleted)"
    return Path(target[: -len(deleted)] if target.endswith(deleted) else target)


def _verify_sqlite_descriptors(
    before: dict[int, tuple[int, int]],
    expected: tuple[int, int],
    path: Path,
    parent_descriptor: int,
) -> None:
    candidates = _new_regular_descriptors(before)
    main = [
        candidate
        for candidate in candidates
        if (candidate[1].st_dev, candidate[1].st_ino) == expected
    ]
    if len(main) > 1:
        raise DatabaseConfigurationError(
            "SQLite main database descriptor ambiguity or validated inode mismatch"
        )
    if main:
        _validate_database_metadata(main[0][1], path)
    elif expected not in _open_fd_identities().values():
        raise DatabaseConfigurationError(
            "SQLite main database descriptor ambiguity or validated inode mismatch"
        )

    try:
        parent_target = Path(os.readlink(f"/proc/self/fd/{parent_descriptor}"))
    except OSError as error:
        raise DatabaseConfigurationError(
            f"cannot classify SQLite descriptors: {error}"
        ) from error
    sidecars = set(_sidecar_names(path))
    for descriptor, metadata, target in candidates:
        if main and descriptor == main[0][0]:
            continue
        candidate_path = _target_path(target)
        if candidate_path == path:
            raise DatabaseConfigurationError(
                "SQLite main database descriptor validated inode mismatch"
            )
        if (
            candidate_path.parent != parent_target
            or candidate_path.name not in sidecars
        ):
            raise DatabaseConfigurationError(
                f"SQLite descriptor inode or role mismatch: {target!r}"
            )
        _validate_sidecar(metadata, candidate_path.name)
        try:
            visible = os.stat(
                candidate_path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as error:
            raise DatabaseConfigurationError(
                f"SQLite sidecar descriptor is not visible: {candidate_path.name!r}"
            ) from error
        if (visible.st_dev, visible.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise DatabaseConfigurationError(
                f"SQLite sidecar descriptor inode mismatch: {candidate_path.name!r}"
            )


def _check_directory_entries(path: Path, parent_descriptor: int) -> None:
    allowed = {path.name, *_sidecar_names(path)}
    try:
        entries = set(os.listdir(parent_descriptor))
    except OSError as error:
        raise DatabaseConfigurationError(
            f"cannot inspect database directory entries: {error}"
        ) from error
    unexpected = entries - allowed
    if unexpected:
        raise DatabaseConfigurationError(
            f"database directory contains unexpected entry: {min(unexpected)!r}"
        )


def _verify_main_database_path(connection: sqlite3.Connection, path: Path) -> None:
    rows = connection.execute("PRAGMA database_list").fetchall()
    main = [row[2] for row in rows if row[1] == "main"]
    if main != [str(path)]:
        raise DatabaseConfigurationError(
            "SQLite resolved main database outside the validated parent"
        )


def _sidecar_names(path: Path) -> tuple[str, str]:
    return f"{path.name}-wal", f"{path.name}-shm"


def _validate_sidecar(metadata: os.stat_result, name: str) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise DatabaseConfigurationError(f"database sidecar {name!r} is not regular")
    if metadata.st_uid != os.geteuid():
        raise DatabaseConfigurationError(
            f"database sidecar {name!r} is not owned by effective UID"
        )
    if metadata.st_nlink != 1:
        raise DatabaseConfigurationError(
            f"database sidecar {name!r} must not have a hard link"
        )
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise DatabaseConfigurationError(
            f"database sidecar {name!r} must have mode 0600"
        )


def _check_sidecars(path: Path, parent_descriptor: int) -> None:
    for name in _sidecar_names(path):
        try:
            metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise DatabaseConfigurationError(
                f"cannot inspect database sidecar {name!r}: {error}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise DatabaseConfigurationError(f"database sidecar {name!r} is a symlink")
        _validate_sidecar(metadata, name)


def _activate_wal(
    connection: sqlite3.Connection, timeout: int
) -> sqlite3.Row | tuple[object, ...] | None:
    deadline = time.monotonic() + timeout / 1_000
    with _UMASK_LOCK:
        previous_umask = os.umask(0o177)
        try:
            while True:
                try:
                    row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
                    return None if row is None else tuple(row)
                except sqlite3.OperationalError as error:
                    if (
                        "locked" not in str(error).casefold()
                        or time.monotonic() >= deadline
                    ):
                        raise
                    time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        finally:
            os.umask(previous_umask)


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
    if (descriptor_parent.st_dev, descriptor_parent.st_ino) != parent_identity or (
        pathname_parent.st_dev,
        pathname_parent.st_ino,
    ) != parent_identity:
        raise DatabaseConfigurationError("database parent changed while opening")
    if (
        descriptor_parent.st_uid != os.geteuid()
        or stat.S_IMODE(descriptor_parent.st_mode) != 0o700
    ):
        raise DatabaseConfigurationError(
            "database parent security changed while opening"
        )
    _validate_database_metadata(database, path)
    if (database.st_dev, database.st_ino) != file_identity:
        raise DatabaseConfigurationError("database file changed while opening")
    _check_sidecars(path, parent_descriptor)


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

    filesystem = ""
    if mount_resolver is not None:
        filesystem = _resolve_filesystem(path.parent, mount_resolver)
    with _connection_fd_activity():
        if mount_resolver is None:
            filesystem = _resolve_filesystem(path.parent, _linux_mount_resolver)
            _validate_filesystem(path.parent, filesystem)
        parent_handle = _open_private_parent(path.parent)
        parent_descriptor = parent_handle.descriptor
        parent_metadata = os.fstat(parent_descriptor)
        parent_identity = (parent_metadata.st_dev, parent_metadata.st_ino)
        connection: SecureConnection | None = None
        file_handle: _DirectoryHandle | None = None
        try:
            if mount_resolver is None:
                _validate_filesystem(path.parent, filesystem)
            file_handle, file_identity = _prepare_database_file(path, parent_descriptor)
            _check_directory_entries(path, parent_descriptor)
            _check_sidecars(path, parent_descriptor)
            descriptor_path = f"/proc/self/fd/{parent_descriptor}/{path.name}"
            before_fds = _open_fd_identities()
            connection = sqlite3.connect(
                descriptor_path,
                timeout=timeout / 1_000,
                isolation_level=None,
                uri=False,
                check_same_thread=True,
                factory=SecureConnection,
            )
            if not isinstance(connection, SecureConnection):
                raise DatabaseConfigurationError(
                    "SQLite did not create a secure connection"
                )
            object.__setattr__(connection, "_directory_handle", parent_handle)
            object.__setattr__(connection, "_database_path", path)
            object.__setattr__(connection, "_database_identity", file_identity)
            object.__setattr__(connection, "_factory_token", _PUBLIC_CONNECTION)
            object.__setattr__(connection, "_authorizer_sealed", False)
            _verify_sqlite_descriptors(
                before_fds, file_identity, path, parent_descriptor
            )
            _close_directory_handle(file_handle)
            file_handle = None
            _verify_main_database_path(connection, path)
            _check_directory_entries(path, parent_descriptor)
            _recheck_storage(path, parent_descriptor, parent_identity, file_identity)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            _verify_pragma(connection, "foreign_keys", 1, label="foreign_keys")
            connection.execute("PRAGMA recursive_triggers = ON")
            _verify_pragma(
                connection, "recursive_triggers", 1, label="recursive_triggers"
            )
            journal = _activate_wal(connection, timeout)
            if journal is None or str(journal[0]).casefold() != "wal":
                actual = None if journal is None else journal[0]
                raise DatabaseConfigurationError(
                    f"SQLite WAL setup failed: received {actual!r}"
                )
            _check_directory_entries(path, parent_descriptor)
            _verify_sqlite_descriptors(
                before_fds, file_identity, path, parent_descriptor
            )
            _check_sidecars(path, parent_descriptor)
            connection.execute(f"PRAGMA busy_timeout = {timeout}")
            _verify_pragma(connection, "busy_timeout", timeout, label="busy_timeout")
            object.__setattr__(connection, "_busy_timeout_ms", timeout)
            connection.execute("PRAGMA synchronous = FULL")
            _verify_pragma(connection, "synchronous", 2, label="synchronous")
            connection.execute("PRAGMA trusted_schema = OFF")
            _verify_pragma(connection, "trusted_schema", 0, label="trusted_schema")
            if hasattr(connection, "setconfig"):
                for name, value in _SAFE_DBCONFIG:
                    operation = getattr(sqlite3, name, None)
                    if operation is not None:
                        connection.setconfig(operation, value)
                        if connection.getconfig(operation) is not value:
                            raise DatabaseConfigurationError(
                                f"SQLite {name} setup failed"
                            )
            _recheck_storage(path, parent_descriptor, parent_identity, file_identity)
            _seal_runtime_authorizer(connection)
            return connection
        except BaseException:
            try:
                if file_handle is not None:
                    _close_directory_handle(file_handle)
                if connection is not None:
                    connection.close()
            finally:
                _close_directory_handle(parent_handle)
            raise

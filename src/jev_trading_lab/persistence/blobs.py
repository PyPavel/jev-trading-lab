"""Crash-durable, content-addressed storage for redacted provider artifacts."""

from __future__ import annotations

import base64
import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_TEMP_RE = re.compile(r"\.tmp-[0-9a-f]{32}\Z")
_REDACTED = "[REDACTED]"
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_CHUNK_SIZE = 1024 * 1024
_RENAME_NOREPLACE = 1
_MAX_URL_BYTES = 16 * 1024
_MAX_HEADERS = 256
_MAX_HEADER_NAME_BYTES = 1024
_MAX_HEADER_VALUE_BYTES = 16 * 1024
_MAX_HEADER_BYTES = 64 * 1024
_MAX_QUERY_FIELDS = 256

_SECRET_FIELDS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "cookie",
        "id_token",
        "password",
        "refresh_token",
        "secret",
        "session_token",
        "token",
    }
)


class BlobError(Exception):
    """Base class for stable blob-store failures."""


class BlobMissingError(BlobError):
    """A referenced blob does not exist."""


class BlobCorruptError(BlobError):
    """A blob or reference fails integrity or filesystem checks."""


class BlobSecurityError(BlobError):
    """The blob root or a shard violates the private-directory boundary."""


class BlobConfigurationError(BlobError):
    """The host cannot provide a required durability primitive."""


@dataclass(frozen=True, slots=True)
class BlobRef:
    """Immutable metadata sufficient to enumerate and verify a stored blob."""

    sha256: str
    byte_length: int
    relative_path: str

    def __post_init__(self) -> None:
        if not _valid_ref(self):
            raise ValueError("invalid blob reference")


@dataclass(frozen=True, slots=True)
class ArtifactEnvelope:
    """Provider bytes plus structured transport metadata eligible for redaction.

    Opaque ``body`` bytes are preserved. In declared JSON bodies, only string
    values of structurally named secret fields are replaced, without otherwise
    normalizing the original bytes. URL userinfo and fragments are removed;
    signed/auth query values and common credential headers are redacted.
    """

    body: bytes
    url: str | None = None
    headers: Mapping[str, str] | None = None

    def __repr__(self) -> str:
        return "ArtifactEnvelope(body=<redacted>, url=<redacted>, headers=<redacted>)"


Failpoint = Callable[[str], None]
ReferenceWriter = Callable[[BlobRef], None]
FilesystemResolver = Callable[[Path], str]
_LOCAL_FILESYSTEMS = frozenset(
    {"btrfs", "ext2", "ext3", "ext4", "f2fs", "overlay", "xfs", "zfs"}
)
_MOUNTINFO_ESCAPES = {
    "\\040": " ",
    "\\011": "\t",
    "\\012": "\n",
    "\\134": "\\",
}


def _linux_filesystem(path: Path) -> str:
    try:
        path = path.resolve(strict=True)
    except OSError as error:
        raise BlobSecurityError("blob filesystem cannot be verified") from error
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise BlobSecurityError("blob filesystem cannot be verified") from error
    matches: list[tuple[int, int, str]] = []
    for record, line in enumerate(mountinfo.splitlines()):
        fields = line.split()
        try:
            separator = fields.index("-")
            raw_mount = fields[4]
            for escaped, decoded in _MOUNTINFO_ESCAPES.items():
                raw_mount = raw_mount.replace(escaped, decoded)
            mount = Path(raw_mount)
            path.relative_to(mount)
            filesystem = fields[separator + 1].casefold()
        except (IndexError, ValueError):
            continue
        matches.append((len(mount.parts), record, filesystem))
    if not matches:
        raise BlobSecurityError("blob filesystem cannot be verified")
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def _require_local_filesystem(path: Path, resolver: FilesystemResolver) -> None:
    filesystem = resolver(path)
    if type(filesystem) is not str or filesystem.casefold() not in _LOCAL_FILESYSTEMS:
        raise BlobSecurityError("blob root uses unsupported filesystem")


def _valid_ref(ref: object) -> bool:
    if type(ref) is not BlobRef:
        return False
    digest = ref.sha256
    if type(digest) is not str or _SHA256_RE.fullmatch(digest) is None:
        return False
    if type(ref.byte_length) is not int or ref.byte_length < 0:
        return False
    return ref.relative_path == f"{digest[:2]}/{digest[2:4]}/{digest}"


def _normalized_secret_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", name)
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", normalized)
    return re.sub(r"[-_\s]+", "_", normalized.casefold()).strip("_")


def _compact_secret_name(name: str) -> str:
    return _normalized_secret_name(name).replace("_", "")


_SECRET_FIELD_COMPACT = frozenset(name.replace("_", "") for name in _SECRET_FIELDS)
_SECRET_HEADER_COMPACT = frozenset(
    {
        "apikey",
        "authorization",
        "cookie",
        "proxyauth",
        "proxyauthorization",
        "setcookie",
        "xapikey",
        "xauthtoken",
        "xamzsecuritytoken",
    }
)
_SIGNED_QUERY_COMPACT = frozenset(
    {
        "awsaccesskeyid",
        "googleaccessid",
        "keypairid",
        "policy",
        "signature",
        "xamzalgorithm",
        "xamzcredential",
        "xamzdate",
        "xamzexpires",
        "xamzsecuritytoken",
        "xamzsignature",
        "xamzsignedheaders",
        "xgoogalgorithm",
        "xgoogcredential",
        "xgoogdate",
        "xgoogexpires",
        "xgoogsecuritytoken",
        "xgoogsignature",
        "xgoogsignedheaders",
        "sig",
        "se",
        "sv",
        "sp",
        "sr",
        "st",
        "spr",
        "sip",
        "si",
        "token",
        "credential",
        "key",
        "apikey",
        "secret",
        "password",
        "session",
        "cookie",
    }
)


def _is_secret_field(name: str) -> bool:
    compact = _compact_secret_name(name)
    return compact in _SECRET_FIELD_COMPACT or compact.endswith(
        ("token", "secret", "password", "session", "cookie")
    )


def _json_string_end(data: bytes, start: int) -> int:
    index = start + 1
    while index < len(data):
        byte = data[index]
        if byte == 0x22:
            return index + 1
        if byte == 0x5C:
            index += 2
        else:
            if byte < 0x20:
                raise ValueError("declared JSON body is unsafe")
            index += 1
    raise ValueError("declared JSON body is unsafe")


def _contains_json_secret_marker(data: bytes) -> bool:
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        return True
    index = 0
    while index < len(data):
        if data[index] != 0x22:
            index += 1
            continue
        try:
            end = _json_string_end(data, index)
            value = json.loads(data[index:end])
        except (ValueError, UnicodeError, json.JSONDecodeError):
            break
        if type(value) is str and _is_secret_field(value):
            return True
        index = end
    return any(item in _compact_secret_name(text) for item in _SECRET_FIELD_COMPACT)


def _redact_json_bytes(data: bytes) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise ValueError("declared JSON body is unsafe") from None
    offsets: list[int] = [0]
    for character in text:
        offsets.append(offsets[-1] + len(character.encode("utf-8")))
    spans: list[tuple[int, int]] = []

    def skip(index: int) -> int:
        while index < len(data) and data[index] in b" \t\r\n":
            index += 1
        return index

    def string(index: int) -> tuple[str, int]:
        end = _json_string_end(data, index)
        try:
            value = json.loads(data[index:end])
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError("declared JSON body is unsafe") from None
        if type(value) is not str:
            raise ValueError("declared JSON body is unsafe")
        return value, end

    def value(index: int, secret: bool = False) -> int:
        index = skip(index)
        if index >= len(data):
            raise ValueError("declared JSON body is unsafe")
        if data[index] == 0x22:
            _item, end = string(index)
            if secret:
                spans.append((index, end))
            return end
        if secret:
            raise ValueError("declared JSON body is unsafe")
        if data[index] == 0x7B:
            index = skip(index + 1)
            if index < len(data) and data[index] == 0x7D:
                return index + 1
            while True:
                if index >= len(data) or data[index] != 0x22:
                    raise ValueError("declared JSON body is unsafe")
                key, index = string(index)
                index = skip(index)
                if index >= len(data) or data[index] != 0x3A:
                    raise ValueError("declared JSON body is unsafe")
                index = value(index + 1, _is_secret_field(key))
                index = skip(index)
                if index < len(data) and data[index] == 0x7D:
                    return index + 1
                if index >= len(data) or data[index] != 0x2C:
                    raise ValueError("declared JSON body is unsafe")
                index = skip(index + 1)
        if data[index] == 0x5B:
            index = skip(index + 1)
            if index < len(data) and data[index] == 0x5D:
                return index + 1
            while True:
                index = value(index)
                index = skip(index)
                if index < len(data) and data[index] == 0x5D:
                    return index + 1
                if index >= len(data) or data[index] != 0x2C:
                    raise ValueError("declared JSON body is unsafe")
                index = skip(index + 1)
        match = re.match(
            rb"(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)",
            data[index:],
        )
        if match is None:
            raise ValueError("declared JSON body is unsafe")
        return index + len(match.group())

    end = skip(value(0))
    if end != len(data):
        raise ValueError("declared JSON body is unsafe")
    result = data
    replacement = json.dumps(_REDACTED).encode("ascii")
    for start, end in reversed(spans):
        result = result[:start] + replacement + result[end:]
    return result


def _redact_url(value: str) -> str:
    if len(value.encode("utf-8")) > _MAX_URL_BYTES:
        raise ValueError("artifact URL exceeds maximum size")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        if hostname is None:
            raise ValueError
        host = f"[{hostname}]" if ":" in hostname else hostname
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        if parsed.query and parsed.query.count("&") >= _MAX_QUERY_FIELDS:
            raise ValueError
        fields = parse_qsl(parsed.query, keep_blank_values=True)
        if len(fields) > _MAX_QUERY_FIELDS:
            raise ValueError
        query = urlencode(
            [
                (
                    key,
                    _REDACTED
                    if (
                        _compact_secret_name(key) in _SIGNED_QUERY_COMPACT
                        or _compact_secret_name(key).startswith("xamz")
                    )
                    else item,
                )
                for key, item in fields
            ]
        )
        return urlunsplit((parsed.scheme, host, parsed.path, query, ""))
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("artifact URL is invalid") from None


def _redacted_payload(payload: bytes | ArtifactEnvelope, max_bytes: int) -> bytes:
    if type(payload) is bytes:
        if len(payload) > max_bytes:
            raise ValueError("payload exceeds maximum size")
        return payload
    if type(payload) is not ArtifactEnvelope:
        raise TypeError("payload must be bytes or ArtifactEnvelope")
    if type(payload.body) is not bytes:
        raise TypeError("envelope body must be bytes")
    if len(payload.body) > max_bytes:
        raise ValueError("payload exceeds maximum size")
    if payload.url is not None and type(payload.url) is not str:
        raise TypeError("envelope URL must be a string or None")
    if payload.headers is not None and not isinstance(payload.headers, Mapping):
        raise TypeError("envelope headers must be a string mapping or None")

    encoded_body_bytes = 4 * ((len(payload.body) + 2) // 3)
    minimum_envelope_bytes = (
        len(b'{"body_base64":"","headers":{},"url":""}') + encoded_body_bytes
    )
    if minimum_envelope_bytes > max_bytes:
        raise ValueError("payload exceeds maximum size")

    if payload.url is not None and len(payload.url.encode("utf-8")) > min(
        max_bytes, _MAX_URL_BYTES
    ):
        raise ValueError("artifact URL exceeds maximum size")
    if payload.headers is not None and len(payload.headers) > _MAX_HEADERS:
        raise ValueError("envelope has too many headers")
    headers: dict[str, str] = {}
    header_bytes = 0
    for key, value in (payload.headers or {}).items():
        if type(key) is not str or type(value) is not str:
            raise TypeError("envelope headers must contain strings")
        key_bytes = len(key.encode("utf-8"))
        value_bytes = len(value.encode("utf-8"))
        header_bytes += key_bytes + value_bytes
        if (
            key_bytes > _MAX_HEADER_NAME_BYTES
            or value_bytes > _MAX_HEADER_VALUE_BYTES
            or header_bytes > min(max_bytes, _MAX_HEADER_BYTES)
        ):
            raise ValueError("envelope headers exceed maximum size")
        headers[key] = (
            _REDACTED if _compact_secret_name(key) in _SECRET_HEADER_COMPACT else value
        )

    content_type = next(
        (value for key, value in headers.items() if key.casefold() == "content-type"),
        "",
    )
    document: dict[str, Any] = {
        "body_base64": base64.b64encode(payload.body).decode("ascii"),
        "headers": headers,
        "url": _redact_url(payload.url) if payload.url is not None else None,
    }
    if "json" in content_type.casefold():
        try:
            redacted_body = _redact_json_bytes(payload.body)
        except ValueError:
            if _contains_json_secret_marker(payload.body):
                raise ValueError("declared JSON body is unsafe") from None
        else:
            document["body_base64"] = base64.b64encode(redacted_body).decode("ascii")
    result = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    if len(result) > max_bytes:
        raise ValueError("payload exceeds maximum size")
    return result


def _reject_symlink_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise BlobSecurityError("blob root ancestor is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise BlobSecurityError("blob root ancestor must not be a symlink")


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError as error:
        raise BlobSecurityError("blob directory cannot be opened safely") from error


def _validate_directory(
    descriptor: int, *, label: str = "blob directory"
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise BlobSecurityError(f"{label} is not private")
    return metadata


def _ensure_child_directory(parent_fd: int, name: str) -> int:
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        raise BlobSecurityError("blob shard cannot be opened safely") from error
    try:
        _validate_directory(descriptor, label="blob shard")
    except BlobSecurityError:
        os.close(descriptor)
        raise
    if created:
        try:
            os.fsync(parent_fd)
        except BaseException:
            os.close(descriptor)
            raise
    return descriptor


def _rename_noreplace(source: str, target: str, directory_fd: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise BlobConfigurationError("renameat2(RENAME_NOREPLACE) is unavailable")
    result = renameat2(
        ctypes.c_int(directory_fd),
        os.fsencode(source),
        ctypes.c_int(directory_fd),
        os.fsencode(target),
        ctypes.c_uint(_RENAME_NOREPLACE),
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        raise BlobConfigurationError(
            "renameat2(RENAME_NOREPLACE) is unavailable"
        ) from None
    raise OSError(error_number, os.strerror(error_number))


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    offset = 0
    while offset < len(view):
        try:
            written = os.write(descriptor, view[offset:])
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError(errno.EIO, "blob write made no progress")
        offset += written


def _checked_metadata(descriptor: int, expected_size: int, *, label: str) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or metadata.st_size != expected_size
    ):
        raise BlobCorruptError(f"{label} verification failed")


def _verify_descriptor(
    descriptor: int, expected_hash: str, expected_size: int, max_bytes: int
) -> bytes:
    if expected_size > max_bytes:
        raise BlobCorruptError("blob verification failed")
    _checked_metadata(descriptor, expected_size, label="blob")
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = os.read(descriptor, min(_CHUNK_SIZE, max_bytes + 1 - total))
        except InterruptedError:
            continue
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise BlobCorruptError("blob verification failed")
        digest.update(chunk)
        chunks.append(chunk)
    if total != expected_size or digest.hexdigest() != expected_hash:
        raise BlobCorruptError("blob verification failed")
    return b"".join(chunks)


class BlobStore:
    """Private local content store with durable publication before references."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        failpoint: Failpoint | None = None,
        filesystem_resolver: FilesystemResolver = _linux_filesystem,
    ) -> None:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        if not callable(filesystem_resolver):
            raise TypeError("filesystem_resolver must be callable")
        root_path = Path(root)
        if not root_path.is_absolute():
            root_path = root_path.absolute()
        _reject_symlink_ancestors(root_path.parent)
        root_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _reject_symlink_ancestors(root_path.parent)
        parent_fd = _open_directory(root_path.parent)
        created = False
        try:
            try:
                os.mkdir(root_path.name, 0o700, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                pass
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                root_fd = os.open(root_path.name, flags, dir_fd=parent_fd)
            except OSError as error:
                raise BlobSecurityError(
                    "blob directory cannot be opened safely"
                ) from error
            try:
                root_metadata = _validate_directory(root_fd, label="blob root")
                if created:
                    os.fsync(parent_fd)
                _require_local_filesystem(
                    Path(f"/proc/self/fd/{root_fd}"), filesystem_resolver
                )
            except BaseException:
                os.close(root_fd)
                if created:
                    try:
                        os.rmdir(root_path.name, dir_fd=parent_fd)
                    except OSError:
                        pass
                raise
            os.close(root_fd)
        finally:
            os.close(parent_fd)
        self.root = root_path
        self._root_identity = (root_metadata.st_dev, root_metadata.st_ino)
        self.max_bytes = max_bytes
        self._failpoint = failpoint or (lambda _name: None)
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> BlobStore:
        if self._closed:
            raise BlobSecurityError("blob store is closed")
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _open_root(self) -> int:
        if self._closed:
            raise BlobSecurityError("blob store is closed")
        parent_fd = _open_directory(self.root.parent)
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.root.name, flags, dir_fd=parent_fd)
        except OSError as error:
            raise BlobSecurityError("blob directory cannot be opened safely") from error
        finally:
            os.close(parent_fd)
        try:
            metadata = _validate_directory(descriptor, label="blob root")
            if (metadata.st_dev, metadata.st_ino) != self._root_identity:
                raise BlobSecurityError("blob root identity changed")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _open_shard(self, digest: str) -> int:
        root_fd = self._open_root()
        try:
            first_fd = _ensure_child_directory(root_fd, digest[:2])
        finally:
            os.close(root_fd)
        try:
            return _ensure_child_directory(first_fd, digest[2:4])
        finally:
            os.close(first_fd)

    def _open_existing_shard(self, digest: str) -> int:
        root_fd = self._open_root()
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            first_fd = os.open(digest[:2], flags, dir_fd=root_fd)
        except FileNotFoundError as error:
            os.close(root_fd)
            raise BlobMissingError("blob is missing") from error
        except OSError as error:
            os.close(root_fd)
            raise BlobCorruptError("blob verification failed") from error
        os.close(root_fd)
        first_metadata = os.fstat(first_fd)
        if (
            first_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(first_metadata.st_mode) != 0o700
        ):
            os.close(first_fd)
            raise BlobCorruptError("blob verification failed")
        try:
            descriptor = os.open(digest[2:4], flags, dir_fd=first_fd)
        except FileNotFoundError as error:
            raise BlobMissingError("blob is missing") from error
        except OSError as error:
            raise BlobCorruptError("blob verification failed") from error
        finally:
            os.close(first_fd)
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            os.close(descriptor)
            raise BlobCorruptError("blob verification failed")
        return descriptor

    def _read_from_shard(self, shard_fd: int, ref: BlobRef) -> bytes:
        flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(ref.sha256, flags, dir_fd=shard_fd)
        except FileNotFoundError as error:
            raise BlobMissingError("blob is missing") from error
        except OSError as error:
            raise BlobCorruptError("blob verification failed") from error
        try:
            return _verify_descriptor(
                descriptor, ref.sha256, ref.byte_length, self.max_bytes
            )
        finally:
            os.close(descriptor)

    def publish(self, payload: bytes | ArtifactEnvelope) -> BlobRef:
        content = _redacted_payload(payload, self.max_bytes)
        digest = hashlib.sha256(content).hexdigest()
        ref = BlobRef(
            sha256=digest,
            byte_length=len(content),
            relative_path=f"{digest[:2]}/{digest[2:4]}/{digest}",
        )
        shard_fd = self._open_shard(digest)
        temporary = f".tmp-{secrets.token_hex(16)}"
        temp_owned = False
        descriptor = -1
        try:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, flags, 0o600, dir_fd=shard_fd)
            temp_owned = True
            self._failpoint("after_temp_create")
            _write_all(descriptor, content)
            self._failpoint("after_temp_write")
            os.fsync(descriptor)
            self._failpoint("after_file_fsync")
            self._failpoint("before_rename")
            _verify_descriptor(descriptor, digest, len(content), self.max_bytes)
            try:
                _rename_noreplace(temporary, digest, shard_fd)
            except FileExistsError:
                self._read_from_shard(shard_fd, ref)
                os.unlink(temporary, dir_fd=shard_fd)
                temp_owned = False
            except OSError as error:
                if error.errno in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
                    raise BlobConfigurationError(
                        "renameat2(RENAME_NOREPLACE) is unavailable"
                    ) from None
                raise
            else:
                temp_owned = False
            self._failpoint("after_rename")
            self._failpoint("before_dir_fsync")
            os.fsync(shard_fd)
            self._failpoint("after_dir_fsync")
            self._read_from_shard(shard_fd, ref)
            return ref
        except BaseException:
            if temp_owned:
                try:
                    os.unlink(temporary, dir_fd=shard_fd)
                except FileNotFoundError:
                    pass
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(shard_fd)

    def publish_and_reference(
        self, payload: bytes | ArtifactEnvelope, reference_writer: ReferenceWriter
    ) -> BlobRef:
        if not callable(reference_writer):
            raise TypeError("reference_writer must be callable")
        ref = self.publish(payload)
        self._failpoint("before_reference")
        reference_writer(ref)
        return ref

    def read_verified(self, ref: BlobRef) -> bytes:
        if not _valid_ref(ref) or ref.byte_length > self.max_bytes:
            raise BlobCorruptError("blob reference is invalid")
        shard_fd = self._open_existing_shard(ref.sha256)
        try:
            return self._read_from_shard(shard_fd, ref)
        finally:
            os.close(shard_fd)

    def scavenge_stale_temps(
        self, *, older_than_seconds: float, now: float | None = None
    ) -> int:
        if older_than_seconds < 0:
            raise ValueError("older_than_seconds must not be negative")
        cutoff = (time.time() if now is None else now) - older_than_seconds
        removed = 0
        root_fd = self._open_root()
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            with os.scandir(root_fd) as entries:
                first_names = [entry.name for entry in entries]
            for first_name in first_names:
                if re.fullmatch(r"[0-9a-f]{2}", first_name) is None:
                    continue
                try:
                    first_fd = os.open(first_name, flags, dir_fd=root_fd)
                except FileNotFoundError:
                    continue
                except OSError as error:
                    raise BlobSecurityError(
                        "blob shard cannot be opened safely"
                    ) from error
                try:
                    _validate_directory(first_fd, label="blob shard")
                except BaseException:
                    os.close(first_fd)
                    raise
                try:
                    with os.scandir(first_fd) as entries:
                        second_names = [entry.name for entry in entries]
                    for second_name in second_names:
                        if re.fullmatch(r"[0-9a-f]{2}", second_name) is None:
                            continue
                        try:
                            directory_fd = os.open(second_name, flags, dir_fd=first_fd)
                        except FileNotFoundError:
                            continue
                        except OSError as error:
                            raise BlobSecurityError(
                                "blob shard cannot be opened safely"
                            ) from error
                        try:
                            _validate_directory(directory_fd, label="blob shard")
                        except BaseException:
                            os.close(directory_fd)
                            raise
                        try:
                            with os.scandir(directory_fd) as entries:
                                names = [entry.name for entry in entries]
                            for name in names:
                                if _TEMP_RE.fullmatch(name) is None:
                                    continue
                                try:
                                    metadata = os.stat(
                                        name, dir_fd=directory_fd, follow_symlinks=False
                                    )
                                except FileNotFoundError:
                                    continue
                                if (
                                    stat.S_ISREG(metadata.st_mode)
                                    and metadata.st_uid == os.geteuid()
                                    and stat.S_IMODE(metadata.st_mode) == 0o600
                                    and metadata.st_nlink == 1
                                    and metadata.st_mtime < cutoff
                                ):
                                    try:
                                        os.unlink(name, dir_fd=directory_fd)
                                    except FileNotFoundError:
                                        continue
                                    removed += 1
                        finally:
                            os.close(directory_fd)
                finally:
                    os.close(first_fd)
        finally:
            os.close(root_fd)
        return removed

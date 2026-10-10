"""Crash-durable, content-addressed storage for redacted provider artifacts.

Supported guarantees are crash and power-loss durability under documented filesystem
semantics and descriptor-relative no-follow validation. The store fails closed on
preexisting or checkpoint-observed state drift. The reference callback runs only after
fsync and final canonical-state verification as observed at that checkpoint.

Concurrent same-user raw filesystem mutation is unsupported for the entire operation,
as are malicious process-wide or private hooks. Pure filesystem verification and a
later Python callback are not an atomic transaction or a sandbox against those actors.
"""

from __future__ import annotations

import base64
import codecs
import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import threading
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
_MAX_JSON_DEPTH = 256
_MAX_JSON_KEY_BYTES = 16 * 1024

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


_MAX_CREDENTIAL_NAME_CHARS = 16 * 1024


def _secret_name_forms(name: str) -> tuple[tuple[str, ...], str]:
    if len(name) > _MAX_CREDENTIAL_NAME_CHARS:
        raise ValueError("credential name exceeds maximum size")
    normalized = unicodedata.normalize("NFKC", name)
    if len(normalized) > _MAX_CREDENTIAL_NAME_CHARS:
        raise ValueError("credential name exceeds maximum size")

    words: list[str] = []
    start = 0
    for index, character in enumerate(normalized):
        if not character.isalnum():
            if start < index:
                words.append(normalized[start:index].casefold())
            start = index + 1
            continue
        if index == start:
            continue
        previous = normalized[index - 1]
        following = normalized[index + 1] if index + 1 < len(normalized) else ""
        if (
            (previous.islower() or previous.isdecimal())
            and character.isupper()
            or (previous.isupper() and character.isupper() and following.islower())
        ):
            words.append(normalized[start:index].casefold())
            start = index
    if start < len(normalized):
        words.append(normalized[start:].casefold())

    compact = "".join(
        character for character in normalized.casefold() if character.isalnum()
    )
    return tuple(filter(None, words)), compact


def _compact_secret_name(name: str) -> str:
    return _secret_name_forms(name)[1]


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
        "clientsecret",
        "accesstoken",
        "refreshtoken",
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
        "cookie",
        "clientsecret",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "code",
    }
)
_CREDENTIAL_TERMINAL_PHRASES = (
    "oauthtokensecret",
    "sessiontoken",
    "refreshtoken",
    "securitytoken",
    "bearertoken",
    "clientsecret",
    "accesstoken",
    "oauthtoken",
    "privatekey",
    "secretkey",
    "authtoken",
    "passphrase",
    "password",
    "apikey",
    "idtoken",
    "cookie",
)


def _strip_credential_versions(
    compact: str, exact: frozenset[str] = frozenset()
) -> str:
    end = len(compact)
    cursor = end
    while cursor:
        component_end = cursor
        while cursor and compact[cursor - 1].isdecimal():
            cursor -= 1
        if cursor < component_end:
            if any(cursor == len(item) and compact.startswith(item) for item in exact):
                end = cursor
                break
            if cursor and compact[cursor - 1] == "v":
                cursor -= 1
        elif compact[cursor - 1] == "v":
            cursor -= 1
        else:
            break
        end = cursor
        if any(end == len(item) and compact.startswith(item) for item in exact):
            break
    return compact[:end]


def _is_structured_credential_name(name: str, exact: frozenset[str]) -> bool:
    _words, compact = _secret_name_forms(name)
    candidate = _strip_credential_versions(compact, exact)
    return (
        compact in exact
        or candidate in exact
        or candidate.endswith(_CREDENTIAL_TERMINAL_PHRASES)
    )


def _is_secret_field(name: str) -> bool:
    return _is_structured_credential_name(name, _SECRET_FIELD_COMPACT)


def _json_string_end(data: bytes, start: int) -> int:
    index = start + 1
    while index < len(data):
        byte = data[index]
        if byte == 0x22:
            return index + 1
        if byte == 0x5C:
            index += 1
            if index >= len(data):
                break
            escape = data[index]
            if escape == 0x75:
                if index + 4 >= len(data) or any(
                    item not in b"0123456789abcdefABCDEF"
                    for item in data[index + 1 : index + 5]
                ):
                    raise ValueError("declared JSON body is unsafe")
                index += 5
                continue
            if escape not in b'"\\/bfnrt':
                raise ValueError("declared JSON body is unsafe")
        elif byte < 0x20:
            raise ValueError("declared JSON body is unsafe")
        index += 1
    raise ValueError("declared JSON body is unsafe")


def _redact_json_bytes(data: bytes) -> bytes:
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        for start in range(0, len(data), _CHUNK_SIZE):
            decoder.decode(memoryview(data)[start : start + _CHUNK_SIZE])
        decoder.decode(b"", final=True)
    except UnicodeError:
        raise ValueError("declared JSON body is unsafe") from None
    output = bytearray()
    scalar = re.compile(
        rb"(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    )

    def copy(start: int, end: int) -> None:
        output.extend(memoryview(data)[start:end])

    def skip(index: int) -> int:
        start = index
        while index < len(data) and data[index] in b" \t\r\n":
            index += 1
        copy(start, index)
        return index

    def string(index: int, *, decode: bool = False) -> tuple[str | None, int]:
        end = _json_string_end(data, index)
        value: str | None = None
        if decode:
            try:
                decoded = json.loads(data[index:end])
            except (UnicodeError, json.JSONDecodeError):
                raise ValueError("declared JSON body is unsafe") from None
            if type(decoded) is not str:
                raise ValueError("declared JSON body is unsafe")
            value = decoded
        copy(index, end)
        return value, end

    def value(index: int, depth: int = 0, secret: bool = False) -> int:
        if depth > _MAX_JSON_DEPTH:
            raise ValueError("declared JSON body is unsafe")
        index = skip(index)
        if index >= len(data):
            raise ValueError("declared JSON body is unsafe")
        if data[index] == 0x22:
            if secret:
                end = _json_string_end(data, index)
                output.extend(b'"[REDACTED]"')
                return end
            return string(index)[1]
        if secret:
            raise ValueError("declared JSON body is unsafe")
        if data[index] == 0x7B:
            copy(index, index + 1)
            index = skip(index + 1)
            if index < len(data) and data[index] == 0x7D:
                copy(index, index + 1)
                return index + 1
            while True:
                if index >= len(data) or data[index] != 0x22:
                    raise ValueError("declared JSON body is unsafe")
                key_start = index
                key, index = string(index, decode=True)
                if index - key_start > _MAX_JSON_KEY_BYTES:
                    raise ValueError("declared JSON body is unsafe")
                assert key is not None
                index = skip(index)
                if index >= len(data) or data[index] != 0x3A:
                    raise ValueError("declared JSON body is unsafe")
                copy(index, index + 1)
                index = value(index + 1, depth + 1, _is_secret_field(key))
                index = skip(index)
                if index < len(data) and data[index] == 0x7D:
                    copy(index, index + 1)
                    return index + 1
                if index >= len(data) or data[index] != 0x2C:
                    raise ValueError("declared JSON body is unsafe")
                copy(index, index + 1)
                index = skip(index + 1)
        if data[index] == 0x5B:
            copy(index, index + 1)
            index = skip(index + 1)
            if index < len(data) and data[index] == 0x5D:
                copy(index, index + 1)
                return index + 1
            while True:
                index = value(index, depth + 1)
                index = skip(index)
                if index < len(data) and data[index] == 0x5D:
                    copy(index, index + 1)
                    return index + 1
                if index >= len(data) or data[index] != 0x2C:
                    raise ValueError("declared JSON body is unsafe")
                copy(index, index + 1)
                index = skip(index + 1)
        match = scalar.match(data, index)
        if match is None:
            raise ValueError("declared JSON body is unsafe")
        copy(index, match.end())
        return match.end()

    end = skip(value(0))
    if end != len(data):
        raise ValueError("declared JSON body is unsafe")
    return bytes(output)


def _redact_url(value: str) -> str:
    try:
        if len(value.encode("utf-8")) > _MAX_URL_BYTES:
            raise ValueError
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
                        _is_structured_credential_name(key, _SIGNED_QUERY_COMPACT)
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

    if payload.url is not None:
        try:
            url_bytes = len(payload.url.encode("utf-8"))
        except UnicodeError:
            raise ValueError("artifact URL is invalid") from None
        if url_bytes > min(max_bytes, _MAX_URL_BYTES):
            raise ValueError("artifact URL exceeds maximum size")
    if payload.headers is not None and len(payload.headers) > _MAX_HEADERS:
        raise ValueError("envelope has too many headers")
    headers: dict[str, str] = {}
    header_bytes = 0
    for key, value in (payload.headers or {}).items():
        if type(key) is not str or type(value) is not str:
            raise TypeError("envelope headers must contain strings")
        try:
            key_bytes = len(key.encode("utf-8"))
            value_bytes = len(value.encode("utf-8"))
        except UnicodeError:
            raise ValueError("envelope headers are invalid") from None
        header_bytes += key_bytes + value_bytes
        if (
            key_bytes > _MAX_HEADER_NAME_BYTES
            or value_bytes > _MAX_HEADER_VALUE_BYTES
            or header_bytes > min(max_bytes, _MAX_HEADER_BYTES)
        ):
            raise ValueError("envelope headers exceed maximum size")
        headers[key] = (
            _REDACTED
            if _is_structured_credential_name(key, _SECRET_HEADER_COMPACT)
            else value
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
        except (UnicodeError, ValueError, json.JSONDecodeError):
            raise ValueError("declared JSON body is unsafe") from None
        document["body_base64"] = base64.b64encode(redacted_body).decode("ascii")
    result = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    if len(result) > max_bytes:
        raise ValueError("payload exceeds maximum size")
    return result


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


def _validate_ancestor_directory(descriptor: int) -> os.stat_result:
    metadata = os.fstat(descriptor)
    mode = stat.S_IMODE(metadata.st_mode)
    sticky_world_writable = mode & stat.S_ISVTX and mode & stat.S_IWOTH
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or (mode & 0o022 and not sticky_world_writable)
    ):
        raise BlobSecurityError("blob root ancestor is not safe")
    return metadata


def _ensure_child_directory(
    parent_fd: int,
    name: str,
    *,
    label: str = "blob shard",
    require_private: bool = True,
) -> tuple[int, bool]:
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
        raise BlobSecurityError(f"{label} cannot be opened safely") from error
    try:
        if created or require_private:
            _validate_directory(descriptor, label=label)
        else:
            _validate_ancestor_directory(descriptor)
        os.fsync(parent_fd)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, created


def _open_or_create_root(root: Path, resolver: FilesystemResolver | None = None) -> int:
    if not root.is_absolute() or root == Path(root.anchor):
        raise BlobSecurityError("blob root path is invalid")
    names = list(root.parts[1:])
    if any(name in {"", ".", ".."} for name in names):
        raise BlobSecurityError("blob root path is invalid")
    descriptors = [_open_directory(Path(root.anchor))]
    created: list[bool] = []
    try:
        _validate_ancestor_directory(descriptors[0])
        for index, name in enumerate(names):
            is_root = index == len(names) - 1
            descriptor, was_created = _ensure_child_directory(
                descriptors[-1],
                name,
                label="blob root" if is_root else "blob root ancestor",
                require_private=is_root,
            )
            descriptors.append(descriptor)
            created.append(was_created)
        root_fd = descriptors[-1]
        if resolver is not None:
            _require_local_filesystem(Path(f"/proc/self/fd/{root_fd}"), resolver)
        descriptors.pop()
    except BaseException:
        for index in range(len(created) - 1, -1, -1):
            if created[index]:
                try:
                    os.rmdir(names[index], dir_fd=descriptors[index])
                    os.fsync(descriptors[index])
                except OSError:
                    pass
        raise
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    return root_fd


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
        root_fd = _open_or_create_root(root_path, filesystem_resolver)
        self.root = root_path
        self.max_bytes = max_bytes
        self._failpoint = failpoint or (lambda _name: None)
        self._root_fd = root_fd
        self._state_lock = threading.Lock()
        self._closed = False

    def close(self) -> None:
        with self._state_lock:
            if not self._closed:
                os.close(self._root_fd)
                self._closed = True

    def __del__(self) -> None:
        try:
            self.close()
        except (AttributeError, OSError):
            pass

    def __enter__(self) -> BlobStore:
        with self._state_lock:
            if self._closed:
                raise BlobSecurityError("blob store is closed")
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _open_visible_root(self) -> int:
        names = list(self.root.parts[1:])
        descriptors = [_open_directory(Path(self.root.anchor))]
        try:
            _validate_ancestor_directory(descriptors[0])
            for index, name in enumerate(names):
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    descriptor = os.open(name, flags, dir_fd=descriptors[-1])
                except OSError as error:
                    raise BlobSecurityError(
                        "blob directory cannot be opened safely"
                    ) from error
                try:
                    if index == len(names) - 1:
                        _validate_directory(descriptor, label="blob root")
                    else:
                        _validate_ancestor_directory(descriptor)
                except BaseException:
                    os.close(descriptor)
                    raise
                descriptors.append(descriptor)
            root_fd = descriptors.pop()
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
        return root_fd

    def _open_root(self) -> int:
        with self._state_lock:
            if self._closed:
                raise BlobSecurityError("blob store is closed")
            pinned = _validate_directory(self._root_fd, label="blob root")
            visible_fd = self._open_visible_root()
            try:
                visible = _validate_directory(visible_fd, label="blob root")
                if not os.path.samestat(pinned, visible):
                    raise BlobSecurityError("blob root identity changed")
                return os.dup(self._root_fd)
            finally:
                os.close(visible_fd)

    def _open_shard(self, digest: str) -> tuple[int, int]:
        root_fd = self._open_root()
        try:
            first_fd, _created = _ensure_child_directory(root_fd, digest[:2])
        finally:
            os.close(root_fd)
        try:
            descriptor, _created = _ensure_child_directory(first_fd, digest[2:4])
            return first_fd, descriptor
        except BaseException:
            os.close(first_fd)
            raise

    def _open_existing_shard_pair(self, digest: str) -> tuple[int, int]:
        root_fd = self._open_root()
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            first_fd = os.open(digest[:2], flags, dir_fd=root_fd)
        except FileNotFoundError as error:
            raise BlobMissingError("blob is missing") from error
        except OSError as error:
            raise BlobCorruptError("blob verification failed") from error
        finally:
            os.close(root_fd)
        try:
            try:
                _validate_directory(first_fd, label="blob shard")
            except BlobSecurityError as error:
                raise BlobCorruptError("blob verification failed") from error
            try:
                descriptor = os.open(digest[2:4], flags, dir_fd=first_fd)
            except FileNotFoundError as error:
                raise BlobMissingError("blob is missing") from error
            except OSError as error:
                raise BlobCorruptError("blob verification failed") from error
            try:
                _validate_directory(descriptor, label="blob shard")
            except BlobSecurityError as error:
                os.close(descriptor)
                raise BlobCorruptError("blob verification failed") from error
            except BaseException:
                os.close(descriptor)
                raise
            return first_fd, descriptor
        except BaseException:
            os.close(first_fd)
            raise

    def _open_existing_shard(self, digest: str) -> int:
        first_fd, descriptor = self._open_existing_shard_pair(digest)
        os.close(first_fd)
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

    def _publish(
        self, payload: bytes | ArtifactEnvelope
    ) -> tuple[BlobRef, os.stat_result, os.stat_result]:
        content = _redacted_payload(payload, self.max_bytes)
        digest = hashlib.sha256(content).hexdigest()
        ref = BlobRef(
            sha256=digest,
            byte_length=len(content),
            relative_path=f"{digest[:2]}/{digest[2:4]}/{digest}",
        )
        first_fd, shard_fd = self._open_shard(digest)
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
            first_metadata = _validate_directory(first_fd, label="blob shard")
            shard_metadata = _validate_directory(shard_fd, label="blob shard")
            return ref, first_metadata, shard_metadata
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
            os.close(first_fd)

    def publish(self, payload: bytes | ArtifactEnvelope) -> BlobRef:
        ref, _first_metadata, _shard_metadata = self._publish(payload)
        return ref

    def _verify_canonical_publication(
        self,
        ref: BlobRef,
        first_metadata: os.stat_result,
        shard_metadata: os.stat_result,
    ) -> None:
        try:
            canonical_first_fd, canonical_shard_fd = self._open_existing_shard_pair(
                ref.sha256
            )
        except (BlobMissingError, BlobCorruptError) as error:
            raise BlobSecurityError("canonical blob path changed") from error
        try:
            if not os.path.samestat(first_metadata, os.fstat(canonical_first_fd)):
                raise BlobSecurityError("blob shard identity changed")
            if not os.path.samestat(shard_metadata, os.fstat(canonical_shard_fd)):
                raise BlobSecurityError("blob shard identity changed")
            self._read_from_shard(canonical_shard_fd, ref)
            _validate_directory(canonical_first_fd, label="blob shard")
            _validate_directory(canonical_shard_fd, label="blob shard")
        finally:
            os.close(canonical_shard_fd)
            os.close(canonical_first_fd)

    def publish_and_reference(
        self, payload: bytes | ArtifactEnvelope, reference_writer: ReferenceWriter
    ) -> BlobRef:
        if not callable(reference_writer):
            raise TypeError("reference_writer must be callable")
        ref, first_metadata, shard_metadata = self._publish(payload)
        self._failpoint("before_reference")
        self._verify_canonical_publication(ref, first_metadata, shard_metadata)
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

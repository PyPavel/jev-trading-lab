"""Immutable common metadata for provider observations.

Publication time may be absent or precede retrieval, but future publication times
are rejected. Retrieval time records the first successful retrieval. Corrections
are separate observations linked to the observation they supersede.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import InitVar, dataclass, field
from enum import Enum
from urllib.parse import urlsplit

from jev_trading_lab.core.ids import canonical_hash
from jev_trading_lab.core.time import validate_utc_us

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_DNS_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_INVALID_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_URI_PATH_CHARACTER_RE = re.compile(r"[A-Za-z0-9._~!$&'()*+,;=:@/%-]*\Z")
_URI_QUERY_CHARACTER_RE = re.compile(r"[A-Za-z0-9._~!$&'()*+,;=:@/?%-]*\Z")


class ObservationStatus(Enum):
    OK = "ok"
    MISSING = "missing"
    INVALID = "invalid"
    STALE = "stale"


def _validate_sha256(value: str, *, name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def _validate_hostname(hostname: str, *, bracketed: bool) -> None:
    if bracketed:
        if "%" in hostname:
            raise ValueError("source URI must not include an IPv6 scope identifier")
        try:
            ipaddress.IPv6Address(hostname)
        except ipaddress.AddressValueError as error:
            raise ValueError("source URI must include a valid IPv6 host") from error
        return
    labels = hostname.split(".")
    numeric_labels = all(label.isdecimal() for label in labels)
    if len(labels) == 4 and numeric_labels:
        if any(
            not 0 <= int(label) <= 255 or (len(label) > 1 and label.startswith("0"))
            for label in labels
        ):
            raise ValueError("source URI must include a canonical IPv4 host")
        if str(ipaddress.IPv4Address(hostname)) != hostname:
            raise ValueError("source URI must include a canonical IPv4 host")
        return
    if (
        len(hostname) > 253
        or numeric_labels
        or labels[-1].isdecimal()
        or any(label.lower().startswith("0x") for label in labels)
        or any(_DNS_LABEL_RE.fullmatch(label) is None for label in labels)
    ):
        raise ValueError("source URI must include a valid DNS hostname or IPv4 host")


def _validate_source_uri(value: str) -> str:
    """Return an exact, absolute lowercase-HTTPS source identity."""
    if type(value) is not str or not value or not value.isascii():
        raise ValueError("source URI must be non-empty ASCII")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise ValueError("source URI must not contain control or whitespace characters")
    if not value.startswith("https://"):
        raise ValueError("source URI must use exact lowercase https scheme")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ValueError("source URI must have valid syntax") from error
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("source URI must be an absolute HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("source URI must not include userinfo")
    if "#" in value:
        raise ValueError("source URI must not include a fragment")
    if (
        _INVALID_PERCENT_ESCAPE_RE.search(parsed.path)
        or _INVALID_PERCENT_ESCAPE_RE.search(parsed.query)
        or _URI_PATH_CHARACTER_RE.fullmatch(parsed.path) is None
        or _URI_QUERY_CHARACTER_RE.fullmatch(parsed.query) is None
    ):
        raise ValueError(
            "source URI path and query must use valid RFC 3986 ASCII syntax"
        )
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError("source URI must include a valid host and port") from error
    if not hostname:
        raise ValueError("source URI must include a host")
    bracketed = parsed.netloc.startswith("[")
    host_end = parsed.netloc.find("]") + 1 if bracketed else len(hostname)
    port_text = parsed.netloc[host_end:]
    if port_text and (not port_text.startswith(":") or not port_text[1:].isdigit()):
        raise ValueError("source URI port must be numeric")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("source URI port must be in 1..65535")
    _validate_hostname(hostname, bracketed=bracketed)
    return value


@dataclass(frozen=True, slots=True)
class ObservationMetadata:
    """Validated immutable provenance for one exact provider payload.

    ``source_uri`` is an exact, unnormalized lowercase-HTTPS URL without userinfo
    or fragments. ``predecessor`` is constructor-only context: pass it exactly
    when ``supersedes_id`` is present. It is validated but neither stored nor hashed.
    """

    source_uri: str
    published_at_us: int | None
    retrieved_at_us: int
    status: ObservationStatus
    content_sha256: str
    supersedes_id: str | None = None
    predecessor: InitVar[ObservationMetadata | None] = None
    _constructor_proof: str = field(init=False, repr=False, compare=False)

    def __post_init__(self, predecessor: ObservationMetadata | None) -> None:
        _validate_source_uri(self.source_uri)
        retrieved = validate_utc_us(self.retrieved_at_us)
        if self.published_at_us is not None:
            published = validate_utc_us(self.published_at_us)
            if published > retrieved:
                raise ValueError("published_at_us cannot be after retrieved_at_us")
        if type(self.status) is not ObservationStatus:
            raise TypeError("status must be an ObservationStatus")
        _validate_sha256(self.content_sha256, name="content_sha256")
        if self.supersedes_id is None:
            if predecessor is not None:
                raise ValueError("predecessor cannot be provided without supersedes_id")
            object.__setattr__(self, "_constructor_proof", self.observation_id)
            return
        _validate_sha256(self.supersedes_id, name="supersedes_id")
        if type(predecessor) is not ObservationMetadata:
            raise ValueError("correction requires an ObservationMetadata predecessor")
        if self.supersedes_id != predecessor.observation_id:
            raise ValueError("correction supersedes_id must identify the predecessor")
        if retrieved <= predecessor.retrieved_at_us:
            raise ValueError(
                "correction retrieval cannot be earlier than or equal to predecessor"
            )
        if self.content_sha256 == predecessor.content_sha256:
            raise ValueError("correction must use a different content hash")
        object.__setattr__(self, "_constructor_proof", self.observation_id)

    @property
    def observation_id(self) -> str:
        """Return the stable identity covering every immutable metadata field."""
        return canonical_hash(
            "observation-metadata-v1",
            {
                "source_uri": self.source_uri,
                "published_at_us": self.published_at_us,
                "retrieved_at_us": self.retrieved_at_us,
                "status": self.status.value,
                "content_sha256": self.content_sha256,
                "supersedes_id": self.supersedes_id,
            },
        )


def correct_observation(
    original: ObservationMetadata,
    *,
    retrieved_at_us: int,
    content_sha256: str,
    supersedes_id: str,
) -> ObservationMetadata:
    """Append a corrected observation without mutating or backdating the original."""
    if type(original) is not ObservationMetadata:
        raise TypeError("original must be ObservationMetadata")
    return ObservationMetadata(
        source_uri=original.source_uri,
        published_at_us=original.published_at_us,
        retrieved_at_us=retrieved_at_us,
        status=original.status,
        content_sha256=content_sha256,
        supersedes_id=supersedes_id,
        predecessor=original,
    )

"""Immutable common metadata for provider observations.

Publication time may be absent or precede retrieval, but future publication times
are rejected. Retrieval time records the first successful retrieval. Corrections
are separate observations linked to the observation they supersede.
"""

from __future__ import annotations

import re
from dataclasses import InitVar, dataclass
from enum import Enum
from urllib.parse import urlsplit

from jev_trading_lab.core.ids import canonical_hash
from jev_trading_lab.core.time import validate_utc_us

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*\Z")
_INVALID_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")


class ObservationStatus(Enum):
    OK = "ok"
    MISSING = "missing"
    INVALID = "invalid"
    STALE = "stale"


def _validate_sha256(value: str, *, name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def _validate_source_uri(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or any(
            character.isspace() or ord(character) < 32 or 127 <= ord(character) <= 159
            for character in value
        )
    ):
        raise ValueError(
            "source URI must be a non-empty absolute URI without control or whitespace"
        )
    if _INVALID_PERCENT_ESCAPE_RE.search(value):
        raise ValueError("source URI must contain only valid percent escapes")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ValueError("source URI must have valid syntax") from error
    if _SCHEME_RE.fullmatch(parsed.scheme) is None or not (
        parsed.netloc or parsed.path
    ):
        raise ValueError("source URI must include a valid scheme and location")
    if parsed.scheme.casefold() in {"http", "https"}:
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("source URI HTTP(S) form must not include userinfo")
        try:
            hostname = parsed.hostname
            parsed.port
        except ValueError as error:
            raise ValueError(
                "source URI HTTP(S) form has invalid host or port"
            ) from error
        if not hostname:
            raise ValueError("source URI HTTP(S) form must include a host")
    return value


@dataclass(frozen=True, slots=True)
class ObservationMetadata:
    """Validated immutable provenance for one exact provider payload.

    ``predecessor`` is constructor-only context: pass it exactly when
    ``supersedes_id`` is present. It is validated but neither stored nor hashed.
    """

    source_uri: str
    published_at_us: int | None
    retrieved_at_us: int
    status: ObservationStatus
    content_sha256: str
    supersedes_id: str | None = None
    predecessor: InitVar[ObservationMetadata | None] = None

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

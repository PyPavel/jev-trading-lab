"""Immutable common metadata for provider observations.

Publication time may be absent or precede retrieval, but future publication times
are rejected. Retrieval time records the first successful retrieval. Corrections
are separate observations linked to the observation they supersede.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

from jev_trading_lab.core.ids import canonical_hash
from jev_trading_lab.core.time import validate_utc_us

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*\Z")


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
        or any(character.isspace() for character in value)
    ):
        raise ValueError(
            "source URI must be a non-empty absolute URI without whitespace"
        )
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ValueError("source URI must have valid syntax") from error
    if _SCHEME_RE.fullmatch(parsed.scheme) is None or not (
        parsed.netloc or parsed.path
    ):
        raise ValueError("source URI must include a valid scheme and location")
    if parsed.scheme.casefold() in {"http", "https"} and not parsed.netloc:
        raise ValueError("source URI HTTP(S) form must include a host")
    return value


@dataclass(frozen=True, slots=True)
class ObservationMetadata:
    """Validated immutable provenance for one exact provider payload."""

    source_uri: str
    published_at_us: int | None
    retrieved_at_us: int
    status: ObservationStatus
    content_sha256: str
    supersedes_id: str | None = None

    def __post_init__(self) -> None:
        _validate_source_uri(self.source_uri)
        retrieved = validate_utc_us(self.retrieved_at_us)
        if self.published_at_us is not None:
            published = validate_utc_us(self.published_at_us)
            if published > retrieved:
                raise ValueError("published_at_us cannot be after retrieved_at_us")
        if type(self.status) is not ObservationStatus:
            raise TypeError("status must be an ObservationStatus")
        _validate_sha256(self.content_sha256, name="content_sha256")
        if self.supersedes_id is not None:
            _validate_sha256(self.supersedes_id, name="supersedes_id")

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
    if supersedes_id != original.observation_id:
        raise ValueError(
            "correction supersedes_id must identify the original observation"
        )
    if content_sha256 == original.content_sha256:
        raise ValueError("correction must use a different content hash")
    validate_utc_us(retrieved_at_us)
    if retrieved_at_us <= original.retrieved_at_us:
        raise ValueError(
            "correction retrieval cannot be earlier than or equal to original"
        )
    return ObservationMetadata(
        source_uri=original.source_uri,
        published_at_us=original.published_at_us,
        retrieved_at_us=retrieved_at_us,
        status=original.status,
        content_sha256=content_sha256,
        supersedes_id=supersedes_id,
    )

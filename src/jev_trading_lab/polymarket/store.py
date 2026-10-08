"""Exact blob freeze and append-only journals for Polymarket observations."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import stat
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.persistence.blobs import BlobError, BlobRef, BlobStore
from jev_trading_lab.polymarket.ai import (
    JEV_MODEL_ID,
    OPENAI_MODEL_ID,
    AdapterFailure,
    build_jev_request,
    build_openai_request,
    is_canonical_failure_message,
)
from jev_trading_lab.polymarket.models import (
    INT64_MAX,
    EvidencePacket,
    JevDecision,
    JevResponse,
    ObservationResult,
    OpenAIResponse,
    ResearchBrief,
    Sha256,
    StrictModel,
    UtcModel,
    jev_response_decision,
    load_strict_json,
    openai_response_brief,
)

_LABEL_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MAX_LINE_BYTES = 1024 * 1024
_MAX_JOURNAL_BYTES = 64 * 1024 * 1024
_SECRET_PATTERN = re.compile(
    r"(?i)(?:openai_api_key|typesafe_api_key|bearer\s+\S{4,}|basic\s+\S{4,}|"
    r"(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|"
    r"api[-_]?key)\s*[:=]\s*\S{4,})"
)
_CREDENTIAL_KEYS = frozenset(
    {
        "authorization",
        "proxyauthorization",
        "cookie",
        "setcookie",
        "xapikey",
        "apikey",
        "openaiapikey",
        "typesafeapikey",
        "authtoken",
    }
)


class ArtifactKind(StrEnum):
    RAW_GAMMA = "raw_gamma"
    SANITIZED_EVIDENCE = "sanitized_evidence"
    OPENAI_REQUEST = "openai_request"
    OPENAI_RESPONSE_SUCCESS = "openai_response_success"
    OPENAI_FAILURE = "openai_failure"
    RESEARCH_BRIEF = "research_brief"
    JEV_REQUEST = "jev_request"
    JEV_RESPONSE_SUCCESS = "jev_response_success"
    JEV_FAILURE = "jev_failure"
    JEV_DECISION = "jev_decision"


_SUCCESS_PATH = (
    ArtifactKind.RAW_GAMMA,
    ArtifactKind.SANITIZED_EVIDENCE,
    ArtifactKind.OPENAI_REQUEST,
    ArtifactKind.OPENAI_RESPONSE_SUCCESS,
    ArtifactKind.RESEARCH_BRIEF,
    ArtifactKind.JEV_REQUEST,
    ArtifactKind.JEV_RESPONSE_SUCCESS,
    ArtifactKind.JEV_DECISION,
)
_OPENAI_FAILURE_PATH = (
    ArtifactKind.RAW_GAMMA,
    ArtifactKind.SANITIZED_EVIDENCE,
    ArtifactKind.OPENAI_REQUEST,
    ArtifactKind.OPENAI_FAILURE,
)
_JEV_FAILURE_PATH = (
    ArtifactKind.RAW_GAMMA,
    ArtifactKind.SANITIZED_EVIDENCE,
    ArtifactKind.OPENAI_REQUEST,
    ArtifactKind.OPENAI_RESPONSE_SUCCESS,
    ArtifactKind.RESEARCH_BRIEF,
    ArtifactKind.JEV_REQUEST,
    ArtifactKind.JEV_FAILURE,
)
_STAGE_PATHS = (_SUCCESS_PATH, _OPENAI_FAILURE_PATH, _JEV_FAILURE_PATH)
_RESULT_HASH_KINDS = {
    ArtifactKind.RAW_GAMMA: "evidence_sha256",
    ArtifactKind.RESEARCH_BRIEF: "research_brief_sha256",
    ArtifactKind.JEV_DECISION: "jev_decision_sha256",
}
Label = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
ByteSize = Annotated[int, Field(strict=True, ge=0, le=INT64_MAX)]
TypedArtifact = StrictModel | Mapping[str, object] | None


class FailureArtifact(UtcModel):
    """Canonical sanitized adapter failure metadata; raw bodies are never retained."""

    provider: Literal["openai", "jev"]
    code: Literal[
        "INPUT_ERROR",
        "PROVIDER_ERROR",
        "TIMEOUT",
        "HTTP_ERROR",
        "PARSE_ERROR",
        "MODEL_MISMATCH",
        "CITATION_ERROR",
        "CREDENTIAL_REFLECTION",
    ]
    message: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    request_sha256: Sha256
    response_sha256: Sha256 | None
    response_byte_length: ByteSize
    retrieved_at: datetime | None
    billed_cost_microdollars: ByteSize
    status_code: Annotated[int, Field(strict=True, ge=100, le=599)] | None

    @model_validator(mode="after")
    def require_canonical_metadata(self) -> Self:
        if not is_canonical_failure_message(
            self.provider, self.code, self.message, self.status_code
        ):
            raise ValueError("failure message is not canonical adapter metadata")
        has_response = self.response_sha256 is not None
        if has_response != (self.response_byte_length > 0):
            raise ValueError("failure response hash and length are inconsistent")
        if self.code == "HTTP_ERROR":
            if (
                self.status_code is None
                or 200 <= self.status_code < 300
                or not has_response
                or self.retrieved_at is None
            ):
                raise ValueError("HTTP failure metadata is inconsistent")
        elif self.code in {
            "PARSE_ERROR",
            "MODEL_MISMATCH",
            "CITATION_ERROR",
        }:
            if (
                self.status_code is None
                or not 200 <= self.status_code < 300
                or not has_response
                or self.retrieved_at is None
            ):
                raise ValueError("post-response failure metadata is inconsistent")
        elif self.code == "CREDENTIAL_REFLECTION" and (
            self.status_code is None or not has_response or self.retrieved_at is None
        ):
            raise ValueError("credential-reflection metadata is inconsistent")
        elif self.code in {"INPUT_ERROR", "PROVIDER_ERROR", "TIMEOUT"}:
            if (
                self.status_code is not None
                or has_response
                or self.retrieved_at is not None
                or self.billed_cost_microdollars != 0
            ):
                raise ValueError("pre-response failure contains response metadata")
        return self


def _stage_path(kinds: tuple[str, ...]) -> tuple[ArtifactKind, ...]:
    for path in _STAGE_PATHS:
        if kinds == tuple(kind.value for kind in path):
            return path
    raise ValueError("artifact stage order must match one reviewed path")


def _stage_path_for_result(
    result: ObservationResult, kinds: tuple[str, ...]
) -> tuple[ArtifactKind, ...]:
    path = _stage_path(kinds)
    failed = result.ai.disposition == "MODEL_FAILURE"
    if failed != (path in {_OPENAI_FAILURE_PATH, _JEV_FAILURE_PATH}):
        raise ValueError("terminal AI disposition does not match provider outcome path")
    return path


def _stage_path_from_envelopes(
    result: ObservationResult, selected: list[ArtifactEnvelope]
) -> tuple[ArtifactKind, ...]:
    try:
        return _stage_path_for_result(result, tuple(item.kind for item in selected))
    except ValueError as error:
        raise StoreCorruptError(
            "artifact stage order is incomplete or invalid"
        ) from error


def _normalized_key(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if character.isalnum()
    )


def _contains_credentials(value: object, sentinels: tuple[str, ...]) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if type(key) is str and _normalized_key(key) in _CREDENTIAL_KEYS:
                return True
            if _contains_credentials(nested, sentinels):
                return True
        return False
    if isinstance(value, list | tuple):
        return any(_contains_credentials(item, sentinels) for item in value)
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFKC", value).casefold()
        return bool(_SECRET_PATTERN.search(normalized)) or any(
            sentinel in normalized for sentinel in sentinels
        )
    return False


def failure_artifact_from_adapter(failure: AdapterFailure) -> FailureArtifact:
    """Convert only typed adapter metadata; failed response bodies stay discarded."""

    if type(failure) is not AdapterFailure:
        raise TypeError("failure must be an AdapterFailure")
    return FailureArtifact(
        provider=failure.provider,
        code=failure.code,
        message=failure.message,
        request_sha256=hashlib.sha256(failure.request_bytes).hexdigest(),
        response_sha256=failure.response_sha256,
        response_byte_length=failure.response_byte_length,
        retrieved_at=failure.retrieved_at,
        billed_cost_microdollars=failure.billed_cost_microdollars,
        status_code=failure.status_code,
    )


def _failure_artifact(
    payload: bytes, provider: Literal["openai", "jev"]
) -> FailureArtifact:
    failure = FailureArtifact.model_validate_json(payload)
    if failure.provider != provider:
        raise ValueError("failure artifact provider does not match stage")
    return failure


def _validate_failure_provenance(
    path: tuple[ArtifactKind, ...],
    typed: dict[str, TypedArtifact],
    *,
    fail: Callable[[str], None],
) -> None:
    if path == _OPENAI_FAILURE_PATH:
        failure_kind = ArtifactKind.OPENAI_FAILURE
        request_kind = ArtifactKind.OPENAI_REQUEST
        label = "OpenAI"
    elif path == _JEV_FAILURE_PATH:
        failure_kind = ArtifactKind.JEV_FAILURE
        request_kind = ArtifactKind.JEV_REQUEST
        label = "Jev"
    else:
        return
    failure = typed[failure_kind]
    request = typed[request_kind]
    assert isinstance(failure, FailureArtifact)
    if (
        failure.request_sha256
        != hashlib.sha256(canonical_json_bytes(request)).hexdigest()
    ):
        fail(f"{label} failure request hash does not match frozen request")


def _expected_model_cost(
    path: tuple[ArtifactKind, ...], typed: dict[str, TypedArtifact]
) -> int:
    if path == _OPENAI_FAILURE_PATH:
        failure = typed[ArtifactKind.OPENAI_FAILURE]
        assert isinstance(failure, FailureArtifact)
        return failure.billed_cost_microdollars
    if path == _JEV_FAILURE_PATH:
        failure = typed[ArtifactKind.JEV_FAILURE]
        assert isinstance(failure, FailureArtifact)
        return failure.billed_cost_microdollars
    return 0


def _validate_failure_terminal(
    result: ObservationResult,
    path: tuple[ArtifactKind, ...],
    typed: dict[str, TypedArtifact],
    *,
    fail: Callable[[str], None],
) -> None:
    ai = result.ai
    if path == _OPENAI_FAILURE_PATH:
        expected = _expected_model_cost(path, typed)
    elif path == _JEV_FAILURE_PATH:
        jev_failure = typed[ArtifactKind.JEV_FAILURE]
        assert isinstance(jev_failure, FailureArtifact)
        expected = jev_failure.billed_cost_microdollars
        # A Jev request proves OpenAI completed; its cost is already in the result.
        if ai.model_cost_microdollars < expected:
            fail("terminal model cost is below Jev failure cost")
            return
        expected = ai.model_cost_microdollars
    else:
        return
    if (
        ai.disposition != "MODEL_FAILURE"
        or ai.signal_probability_micros is not None
        or ai.signal_action is not None
        or ai.forecast_probability_micros is not None
        or ai.quantity != 0
        or ai.trading_pnl_microdollars != 0
        or ai.model_cost_microdollars != expected
        or ai.economic_pnl_microdollars != -expected
    ):
        fail("terminal AI arm does not match provider failure outcome")


class StoreError(Exception):
    """Base class for observation-store failures."""


class StoreSecurityError(StoreError):
    """The output root or a journal violates the private-file boundary."""


class StoreCorruptError(StoreError):
    """Persisted journal or artifact state cannot be replayed safely."""


class StoreConflictError(StoreError):
    """An immutable identity already names different canonical bytes."""


class ArtifactEnvelope(UtcModel):
    """Canonical reference to one exact, verified artifact blob."""

    kind: Label
    sha256: Sha256
    byte_size: ByteSize
    storage_relpath: str
    source_uri_label: Label
    retrieved_at: datetime
    opportunity_id: Sha256
    observation_id: Sha256
    predecessor_stage_sha256: Sha256 | None

    @field_validator("retrieved_at", mode="after")
    @classmethod
    def require_utc_exactly(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("retrieval timestamp must use UTC")
        return value

    @model_validator(mode="after")
    def bind_storage_path(self) -> Self:
        expected = f"{self.sha256[:2]}/{self.sha256[2:4]}/{self.sha256}"
        if self.storage_relpath != expected:
            raise ValueError("storage path must be the canonical blob path")
        return self

    def blob_ref(self) -> BlobRef:
        return BlobRef(self.sha256, self.byte_size, self.storage_relpath)


@dataclass(frozen=True, slots=True)
class ArtifactToFreeze:
    """Exact bytes and non-secret provenance needed to freeze one stage."""

    kind: str
    payload: bytes
    source_uri_label: str
    retrieved_at: datetime
    predecessor_stage_sha256: str | None

    def __post_init__(self) -> None:
        if type(self.kind) is not str or _LABEL_RE.fullmatch(self.kind) is None:
            raise ValueError("artifact kind must be a safe label")
        if type(self.payload) is not bytes:
            raise TypeError("artifact payload must be exact bytes")
        if (
            type(self.source_uri_label) is not str
            or _LABEL_RE.fullmatch(self.source_uri_label) is None
        ):
            raise ValueError("source URI label must be a safe non-URL label")
        if type(self.retrieved_at) is not datetime or (
            self.retrieved_at.tzinfo is None
            or self.retrieved_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("artifact retrieval timestamp must be UTC")
        predecessor = self.predecessor_stage_sha256
        if predecessor is not None and (
            type(predecessor) is not str
            or re.fullmatch(r"[0-9a-f]{64}", predecessor) is None
        ):
            raise ValueError("predecessor stage SHA-256 is invalid")


@dataclass(frozen=True, slots=True)
class ObservationReplay:
    result: ObservationResult
    envelopes: dict[str, ArtifactEnvelope]
    artifacts: dict[str, bytes]
    typed_artifacts: dict[str, TypedArtifact]


def _open_output(output_dir: Path) -> int:
    path = output_dir.absolute()
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise StoreSecurityError("output directory cannot be created safely") from error
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise StoreSecurityError("output directory cannot be opened safely") from error
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        os.close(descriptor)
        raise StoreSecurityError(
            "output directory must be private and owner-controlled"
        )
    return descriptor


def _open_journal(root_fd: int, name: str) -> int:
    flags = (
        os.O_RDWR
        | os.O_APPEND
        | os.O_CREAT
        | os.O_CLOEXEC
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
    )
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=root_fd)
    except OSError as error:
        raise StoreSecurityError("journal cannot be opened safely") from error
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        os.close(descriptor)
        raise StoreSecurityError("journal must be a private owner-only regular file")
    return descriptor


def _lock(descriptor: int, operation: int) -> None:
    while True:
        try:
            fcntl.flock(descriptor, operation)
            return
        except InterruptedError:
            continue


def _read_journal(descriptor: int, *, label: str) -> list[bytes]:
    metadata = os.fstat(descriptor)
    if metadata.st_size > _MAX_JOURNAL_BYTES:
        raise StoreCorruptError(f"{label} exceeds maximum size")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = metadata.st_size
    while remaining:
        try:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
        except InterruptedError:
            continue
        if not chunk:
            raise StoreCorruptError(f"{label} changed while locked")
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if raw and not raw.endswith(b"\n"):
        raise StoreCorruptError(f"{label} has a truncated tail")
    lines = raw.splitlines()
    if any(not line or len(line) > _MAX_LINE_BYTES for line in lines):
        raise StoreCorruptError(f"{label} contains an invalid line")
    return lines


def _append_lines(descriptor: int, lines: list[bytes], *, label: str) -> None:
    for line in lines:
        framed = line + b"\n"
        if len(framed) > _MAX_LINE_BYTES:
            raise ValueError(f"{label} line exceeds maximum size")
        view = memoryview(framed)
        while view:
            try:
                written = os.write(descriptor, view)
            except InterruptedError:
                continue
            if written <= 0:
                raise OSError(errno.EIO, "journal write made no progress")
            view = view[written:]
    os.fsync(descriptor)


def _parse_results(
    lines: Iterable[bytes],
) -> dict[str, tuple[ObservationResult, bytes]]:
    parsed: dict[str, tuple[ObservationResult, bytes]] = {}
    for line in lines:
        try:
            result = ObservationResult.model_validate_json(line)
        except (ValueError, TypeError) as error:
            raise StoreCorruptError(
                "result journal contains invalid strict JSON"
            ) from error
        if result.observation_id in parsed:
            raise StoreCorruptError("result journal contains duplicate observation IDs")
        canonical = canonical_json_bytes(result.model_dump(mode="json"))
        if line != canonical:
            raise StoreCorruptError("result journal contains noncanonical JSON")
        _validate_identity(result, corruption=True)
        parsed[result.observation_id] = (result, line)
    return parsed


def _parse_envelopes(lines: Iterable[bytes]) -> list[ArtifactEnvelope]:
    parsed: list[ArtifactEnvelope] = []
    identities: set[tuple[str, str]] = set()
    for line in lines:
        try:
            envelope = ArtifactEnvelope.model_validate_json(line)
        except (ValueError, TypeError) as error:
            raise StoreCorruptError(
                "artifact journal contains invalid strict JSON"
            ) from error
        if line != canonical_json_bytes(envelope.model_dump(mode="json")):
            raise StoreCorruptError("artifact journal contains noncanonical JSON")
        try:
            ArtifactKind(envelope.kind)
        except ValueError as error:
            raise StoreCorruptError(
                "artifact journal contains an unknown stage"
            ) from error
        identity = (envelope.observation_id, envelope.kind)
        if identity in identities:
            raise StoreCorruptError(
                "artifact journal contains duplicate stage identities"
            )
        identities.add(identity)
        parsed.append(envelope)
    return parsed


def _validate_identity(result: ObservationResult, *, corruption: bool) -> None:
    cutoff = result.model_dump(mode="json")["cutoff_at"]
    opportunity = hashlib.sha256(
        canonical_json_bytes(
            {
                "policy_version": result.policy_version,
                "market_id": result.market_id,
                "cutoff_at": cutoff,
            }
        )
    ).hexdigest()
    observation = hashlib.sha256(
        canonical_json_bytes(
            {
                "opportunity_id": result.opportunity_id,
                "evidence_sha256": result.evidence_sha256,
                "execution_book_sha256": result.execution_book_sha256,
                "policy_version": result.policy_version,
            }
        )
    ).hexdigest()
    if opportunity != result.opportunity_id or observation != result.observation_id:
        error = "persisted observation identity is invalid"
        if corruption:
            raise StoreCorruptError(error)
        raise ValueError(error)


def _verified_result(result: ObservationResult) -> tuple[ObservationResult, bytes]:
    canonical = canonical_json_bytes(result.model_dump(mode="json"))
    verified = ObservationResult.model_validate_json(canonical)
    _validate_identity(verified, corruption=False)
    return verified, canonical


def _screen_payload(
    payload: bytes,
    forbidden_secret_values: tuple[bytes, ...],
    *,
    strict_json: bool = False,
) -> None:
    sentinels: list[str] = []
    for value in forbidden_secret_values:
        if type(value) is not bytes or not value:
            raise ValueError("forbidden secret values must be nonempty bytes")
        try:
            sentinels.append(
                unicodedata.normalize("NFKC", value.decode("utf-8")).casefold()
            )
        except UnicodeDecodeError:
            if value in payload:
                raise StoreSecurityError("artifact contains a forbidden secret value")
    try:
        text = unicodedata.normalize("NFKC", payload.decode("utf-8")).casefold()
    except UnicodeDecodeError:
        if strict_json:
            raise StoreSecurityError("structured artifact is not UTF-8") from None
        return
    if _SECRET_PATTERN.search(text) or any(sentinel in text for sentinel in sentinels):
        raise StoreSecurityError("artifact contains credential-like data")
    if strict_json:
        try:
            decoded = load_strict_json(payload)
        except (TypeError, ValueError):
            return
        if _contains_credentials(decoded, tuple(sentinels)):
            raise StoreSecurityError("artifact contains credential-like data")


def _canonical_stage_hash(envelope: ArtifactEnvelope) -> str:
    return hashlib.sha256(
        canonical_json_bytes(envelope.model_dump(mode="json"))
    ).hexdigest()


def _reconstruct_orphan(kind: ArtifactKind, payload: bytes) -> TypedArtifact:
    if kind is ArtifactKind.RAW_GAMMA:
        value = load_strict_json(payload)
        if not isinstance(value, Mapping):
            raise ValueError("Gamma artifact must be a JSON object")
        return value
    if kind is ArtifactKind.SANITIZED_EVIDENCE:
        return EvidencePacket.model_validate_json(payload)
    if kind is ArtifactKind.RESEARCH_BRIEF:
        return ResearchBrief.model_validate_json(payload)
    if kind is ArtifactKind.JEV_DECISION:
        return JevDecision.model_validate_json(payload)
    if kind is ArtifactKind.OPENAI_RESPONSE_SUCCESS:
        response = OpenAIResponse.model_validate_json(payload)
        if (
            response.status != "completed"
            or response.usage is None
            or response.error is not None
            or response.incomplete_details is not None
        ):
            raise ValueError("OpenAI response is not a completed success")
        return response
    if kind is ArtifactKind.JEV_RESPONSE_SUCCESS:
        return JevResponse.model_validate_json(payload)
    if kind is ArtifactKind.OPENAI_FAILURE:
        return _failure_artifact(payload, "openai")
    if kind is ArtifactKind.JEV_FAILURE:
        return _failure_artifact(payload, "jev")
    value = load_strict_json(payload)
    if not isinstance(value, Mapping):
        raise ValueError("request artifact must be a JSON object")
    return value


def _reconstruct(
    kind: ArtifactKind, payload: bytes, result: ObservationResult
) -> TypedArtifact:
    del result
    try:
        return _reconstruct_orphan(kind, payload)
    except (TypeError, ValueError) as error:
        label = "Gamma" if kind is ArtifactKind.RAW_GAMMA else kind.value
        raise ValueError(f"{label} artifact violates its strict contract") from error


def _validate_reconstructed(
    result: ObservationResult,
    typed: dict[str, TypedArtifact],
    path: tuple[ArtifactKind, ...],
    *,
    corruption: bool,
) -> None:
    def fail(message: str) -> None:
        if corruption:
            raise StoreCorruptError(message)
        raise ValueError(message)

    evidence = typed[ArtifactKind.SANITIZED_EVIDENCE]
    if not isinstance(evidence, EvidencePacket):
        fail("sanitized evidence artifact type is invalid")
        return
    if (
        evidence.market_id != result.market_id
        or evidence.source_sha256 != result.evidence_sha256
    ):
        fail("sanitized evidence does not match observation")
    openai_request = typed[ArtifactKind.OPENAI_REQUEST]
    if openai_request != load_strict_json(
        build_openai_request(evidence, OPENAI_MODEL_ID)
    ):
        fail("OpenAI request does not match evidence")
    if path == _OPENAI_FAILURE_PATH:
        _validate_failure_provenance(path, typed, fail=fail)
        _validate_failure_terminal(result, path, typed, fail=fail)
        return
    brief = typed[ArtifactKind.RESEARCH_BRIEF]
    openai_response = typed[ArtifactKind.OPENAI_RESPONSE_SUCCESS]
    if not isinstance(brief, ResearchBrief) or not isinstance(
        openai_response, OpenAIResponse
    ):
        fail("OpenAI success artifact type is invalid")
        return
    if brief.evidence_sha256 != result.evidence_sha256:
        fail("research brief does not match observation evidence")
    if openai_response_brief(openai_response) != brief:
        fail("OpenAI response does not match research brief")
    jev_request = typed[ArtifactKind.JEV_REQUEST]
    if jev_request != load_strict_json(
        build_jev_request(brief, evidence, JEV_MODEL_ID)
    ):
        fail("Jev request does not match evidence and brief")
    if path == _JEV_FAILURE_PATH:
        _validate_failure_provenance(path, typed, fail=fail)
        _validate_failure_terminal(result, path, typed, fail=fail)
        return
    decision = typed[ArtifactKind.JEV_DECISION]
    jev_response = typed[ArtifactKind.JEV_RESPONSE_SUCCESS]
    if not isinstance(decision, JevDecision) or not isinstance(
        jev_response, JevResponse
    ):
        fail("Jev success artifact type is invalid")
        return
    if jev_response_decision(jev_response) != decision:
        fail("Jev response does not match decision")
    if (
        result.jev_decision_sha256
        != hashlib.sha256(
            canonical_json_bytes(decision.model_dump(mode="json"))
        ).hexdigest()
    ):
        fail("Jev decision hash does not match observation")
    if (
        result.ai.signal_probability_micros != decision.p_yes_micros
        or result.ai.forecast_probability_micros != decision.p_yes_micros
        or result.ai.signal_action != decision.action
    ):
        fail("terminal AI signal does not match frozen Jev decision")


def _prepare(
    result: ObservationResult,
    artifact_refs: Iterable[ArtifactToFreeze],
    forbidden_secret_values: tuple[bytes, ...],
) -> tuple[list[ArtifactToFreeze], list[ArtifactEnvelope], dict[str, TypedArtifact]]:
    artifacts = list(artifact_refs)
    if any(type(item) is not ArtifactToFreeze for item in artifacts):
        raise TypeError("artifact references must be ArtifactToFreeze values")
    path = _stage_path_for_result(result, tuple(item.kind for item in artifacts))
    envelopes: list[ArtifactEnvelope] = []
    typed: dict[str, TypedArtifact] = {}
    predecessor: str | None = None
    for artifact, kind in zip(artifacts, path, strict=True):
        _screen_payload(
            artifact.payload,
            forbidden_secret_values,
            strict_json=True,
        )
        payload_hash = hashlib.sha256(artifact.payload).hexdigest()
        envelope = ArtifactEnvelope(
            kind=kind.value,
            sha256=payload_hash,
            byte_size=len(artifact.payload),
            storage_relpath=f"{payload_hash[:2]}/{payload_hash[2:4]}/{payload_hash}",
            source_uri_label=artifact.source_uri_label,
            retrieved_at=artifact.retrieved_at,
            opportunity_id=result.opportunity_id,
            observation_id=result.observation_id,
            predecessor_stage_sha256=artifact.predecessor_stage_sha256,
        )
        if envelope.predecessor_stage_sha256 != predecessor:
            raise ValueError("artifact predecessor chain is invalid")
        typed[kind.value] = _reconstruct(kind, artifact.payload, result)
        envelopes.append(envelope)
        predecessor = _canonical_stage_hash(envelope)
    for kind, field in _RESULT_HASH_KINDS.items():
        if kind not in path:
            continue
        envelope = envelopes[path.index(kind)]
        if envelope.sha256 != getattr(result, field):
            raise ValueError(f"{kind.value} artifact does not match observation hash")
    _validate_reconstructed(result, typed, path, corruption=False)
    return artifacts, envelopes, typed


def _publish(
    store: BlobStore,
    artifacts: list[ArtifactToFreeze],
    envelopes: list[ArtifactEnvelope],
) -> list[tuple[ArtifactEnvelope, bytes]]:
    frozen: list[tuple[ArtifactEnvelope, bytes]] = []
    for artifact, envelope in zip(artifacts, envelopes, strict=True):
        ref = store.publish(artifact.payload)
        verified = store.read_verified(ref)
        if (
            verified != artifact.payload
            or ref.sha256 != envelope.sha256
            or ref.byte_length != envelope.byte_size
            or ref.relative_path != envelope.storage_relpath
        ):
            raise StoreCorruptError("published artifact failed exact verification")
        frozen.append((envelope, verified))
    return frozen


def _validate_journals_locked(
    store: BlobStore,
    results: dict[str, tuple[ObservationResult, bytes]],
    envelopes: list[ArtifactEnvelope],
    forbidden_secret_values: tuple[bytes, ...] = (),
) -> dict[str, ObservationReplay]:
    grouped: dict[str, list[ArtifactEnvelope]] = {}
    for envelope in envelopes:
        grouped.setdefault(envelope.observation_id, []).append(envelope)

    replays: dict[str, ObservationReplay] = {}
    for observation_id, selected in grouped.items():
        kinds = tuple(item.kind for item in selected)
        matching_paths = [
            path
            for path in _STAGE_PATHS
            if kinds == tuple(kind.value for kind in path[: len(kinds)])
        ]
        if not matching_paths:
            raise StoreCorruptError(
                "artifact stages do not form a reviewed path prefix"
            )
        opportunity_id = selected[0].opportunity_id
        predecessor: str | None = None
        for envelope in selected:
            if (
                envelope.observation_id != observation_id
                or envelope.opportunity_id != opportunity_id
            ):
                raise StoreCorruptError(
                    "artifact envelope observation references mismatch"
                )
            if envelope.predecessor_stage_sha256 != predecessor:
                raise StoreCorruptError("artifact predecessor chain is invalid")
            try:
                store.read_verified(envelope.blob_ref())
            except BlobError as error:
                raise StoreCorruptError(
                    "artifact blob is missing or corrupt"
                ) from error
            predecessor = _canonical_stage_hash(envelope)

        found = results.get(observation_id)
        if found is not None:
            result = found[0]
            if result.opportunity_id != opportunity_id:
                raise StoreCorruptError(
                    "artifact envelope observation references mismatch"
                )
            replays[observation_id] = _verify_replay_locked(
                store, result, selected, forbidden_secret_values
            )
            continue

        typed: dict[str, TypedArtifact] = {}
        for envelope, kind in zip(selected, matching_paths[0], strict=False):
            try:
                payload = store.read_verified(envelope.blob_ref())
                _screen_payload(payload, forbidden_secret_values, strict_json=True)
                typed[kind.value] = _reconstruct_orphan(kind, payload)
            except StoreSecurityError:
                raise
            except (BlobError, TypeError, ValueError) as error:
                raise StoreCorruptError(
                    "artifact blob is missing or corrupt"
                ) from error
        failure_paths = [
            path
            for path in matching_paths
            if len(selected) == len(path)
            and path in {_OPENAI_FAILURE_PATH, _JEV_FAILURE_PATH}
        ]
        if failure_paths:

            def fail(message: str) -> None:
                raise StoreCorruptError(message)

            _validate_failure_provenance(failure_paths[0], typed, fail=fail)

    missing = results.keys() - grouped.keys()
    if missing:
        raise StoreCorruptError("terminal result has an incomplete artifact path")
    return replays


def _verify_replay_locked(
    store: BlobStore,
    result: ObservationResult,
    envelopes: list[ArtifactEnvelope],
    forbidden_secret_values: tuple[bytes, ...] = (),
) -> ObservationReplay:
    selected = [
        item for item in envelopes if item.observation_id == result.observation_id
    ]
    path = _stage_path_from_envelopes(result, selected)
    by_kind = {item.kind: item for item in selected}
    artifacts: dict[str, bytes] = {}
    typed: dict[str, TypedArtifact] = {}
    predecessor: str | None = None
    for envelope, kind in zip(selected, path, strict=True):
        if (
            envelope.opportunity_id != result.opportunity_id
            or envelope.observation_id != result.observation_id
        ):
            raise StoreCorruptError("artifact envelope observation references mismatch")
        if envelope.predecessor_stage_sha256 != predecessor:
            raise StoreCorruptError("artifact predecessor chain is invalid")
        field = _RESULT_HASH_KINDS.get(kind)
        if field is not None and envelope.sha256 != getattr(result, field):
            raise StoreCorruptError("artifact envelope does not match observation hash")
        try:
            payload = store.read_verified(envelope.blob_ref())
            _screen_payload(
                payload,
                forbidden_secret_values,
                strict_json=True,
            )
            typed[kind.value] = _reconstruct(kind, payload, result)
        except StoreSecurityError:
            raise
        except (BlobError, TypeError, ValueError) as error:
            raise StoreCorruptError("artifact blob is missing or corrupt") from error
        artifacts[kind.value] = payload
        predecessor = _canonical_stage_hash(envelope)
    _validate_reconstructed(result, typed, path, corruption=True)
    return ObservationReplay(
        result=result,
        envelopes=by_kind,
        artifacts=artifacts,
        typed_artifacts=typed,
    )


def record_observation(
    output_dir: str | os.PathLike[str],
    result: ObservationResult,
    artifact_refs: Iterable[ArtifactToFreeze],
    *,
    forbidden_secret_values: tuple[bytes, ...] = (),
) -> ObservationResult:
    """Validate all stages, publish blobs, then append envelopes and terminal result."""

    if type(result) is not ObservationResult:
        raise TypeError("result must be an ObservationResult")
    verified_result, result_line = _verified_result(result)
    _screen_payload(result_line, forbidden_secret_values)
    artifacts, envelopes, _typed = _prepare(
        verified_result, artifact_refs, forbidden_secret_values
    )
    envelope_lines = [
        canonical_json_bytes(envelope.model_dump(mode="json")) for envelope in envelopes
    ]
    for line in envelope_lines:
        _screen_payload(line, forbidden_secret_values)
    output = Path(output_dir)
    root_fd = _open_output(output)
    try:
        with BlobStore(output.absolute() / "blobs") as store:
            _publish(store, artifacts, envelopes)
            results_fd = _open_journal(root_fd, "results.jsonl")
            try:
                _lock(results_fd, fcntl.LOCK_EX)
                existing_results = _parse_results(
                    _read_journal(results_fd, label="result journal")
                )
                existing = existing_results.get(verified_result.observation_id)
                artifacts_fd = _open_journal(root_fd, "artifacts.jsonl")
                try:
                    _lock(artifacts_fd, fcntl.LOCK_EX)
                    existing_envelopes = _parse_envelopes(
                        _read_journal(artifacts_fd, label="artifact journal")
                    )
                    _validate_journals_locked(
                        store,
                        existing_results,
                        existing_envelopes,
                        forbidden_secret_values,
                    )
                    prior_artifacts = [
                        item
                        for item in existing_envelopes
                        if item.observation_id == verified_result.observation_id
                    ]
                    if prior_artifacts != envelopes[: len(prior_artifacts)]:
                        raise StoreConflictError(
                            "artifact stage identity already has different bytes"
                        )
                    if len(prior_artifacts) > len(envelopes):
                        raise StoreConflictError("artifact journal has extra stages")
                    if existing is not None:
                        if existing[1] != result_line:
                            raise StoreConflictError(
                                "observation identity already has different "
                                "canonical bytes"
                            )
                        if prior_artifacts != envelopes:
                            raise StoreCorruptError(
                                "terminal result has an incomplete artifact path"
                            )
                        _verify_replay_locked(
                            store,
                            existing[0],
                            existing_envelopes,
                            forbidden_secret_values,
                        )
                        os.fsync(artifacts_fd)
                        os.fsync(results_fd)
                        return existing[0]
                    missing_lines = envelope_lines[len(prior_artifacts) :]
                    if missing_lines:
                        _append_lines(
                            artifacts_fd, missing_lines, label="artifact journal"
                        )
                    else:
                        os.fsync(artifacts_fd)
                    _append_lines(results_fd, [result_line], label="result journal")
                    return verified_result
                finally:
                    os.close(artifacts_fd)
            finally:
                os.close(results_fd)
    finally:
        os.close(root_fd)


def load_observation(
    output_dir: str | os.PathLike[str],
    observation_id: str,
    *,
    forbidden_secret_values: tuple[bytes, ...] = (),
) -> ObservationReplay:
    """Replay one observation only from strict journals and verified exact blobs."""

    if (
        type(observation_id) is not str
        or re.fullmatch(r"[0-9a-f]{64}", observation_id) is None
    ):
        raise ValueError("observation ID must be a lowercase SHA-256")
    output = Path(output_dir)
    root_fd = _open_output(output)
    try:
        with BlobStore(output.absolute() / "blobs") as store:
            results_fd = _open_journal(root_fd, "results.jsonl")
            try:
                _lock(results_fd, fcntl.LOCK_SH)
                results = _parse_results(
                    _read_journal(results_fd, label="result journal")
                )
                found = results.get(observation_id)
                if found is None:
                    raise StoreCorruptError("observation result is missing")
                artifacts_fd = _open_journal(root_fd, "artifacts.jsonl")
                try:
                    _lock(artifacts_fd, fcntl.LOCK_SH)
                    envelopes = _parse_envelopes(
                        _read_journal(artifacts_fd, label="artifact journal")
                    )
                    replays = _validate_journals_locked(
                        store, results, envelopes, forbidden_secret_values
                    )
                    return replays[observation_id]
                finally:
                    os.close(artifacts_fd)
            finally:
                os.close(results_fd)
    finally:
        os.close(root_fd)

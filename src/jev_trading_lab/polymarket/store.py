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
from jev_trading_lab.persistence.blobs import (
    BlobError,
    BlobRef,
    BlobSecurityError,
    BlobStore,
    _open_or_create_root,
)
from jev_trading_lab.polymarket.ai import (
    JEV_MODEL_ID,
    OPENAI_MODEL_ID,
    AdapterFailure,
    FailureCode,
    build_jev_request,
    build_openai_request,
    has_canonical_failure_response,
    is_canonical_failure_message,
)
from jev_trading_lab.polymarket.client import reconstruct_book_snapshot
from jev_trading_lab.polymarket.models import (
    INT64_MAX,
    BookSnapshot,
    EvidencePacket,
    FixtureIdentity,
    JevDecision,
    JevResponse,
    NonEmptyStr,
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
    OPENAI_SUCCESS_METADATA = "openai_success_metadata"
    OPENAI_FAILURE = "openai_failure"
    RESEARCH_BRIEF = "research_brief"
    JEV_REQUEST = "jev_request"
    JEV_RESPONSE_SUCCESS = "jev_response_success"
    JEV_SUCCESS_METADATA = "jev_success_metadata"
    JEV_FAILURE = "jev_failure"
    JEV_DECISION = "jev_decision"
    DECISION_FROZEN = "decision_frozen"
    BOOK_UNAVAILABLE = "book_unavailable"
    ELIGIBILITY_REJECTION = "eligibility_rejection"
    EXECUTION_BOOK = "execution_book"


class DecisionFrozenArtifact(UtcModel):
    """Canonical terminal model state durably frozen before book economics."""

    opportunity_id: Sha256
    observation_id: Sha256
    terminal_model_state: Literal["OPENAI_FAILURE", "JEV_FAILURE", "JEV_SUCCESS"]
    failure_code: FailureCode | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    openai_request_sha256: Sha256
    openai_response_sha256: Sha256 | None
    jev_request_sha256: Sha256 | None
    jev_response_sha256: Sha256 | None
    jev_decision_sha256: Sha256 | None
    openai_cost_microdollars: Annotated[int, Field(strict=True, ge=0, le=INT64_MAX)]
    jev_cost_microdollars: Annotated[int, Field(strict=True, ge=0, le=INT64_MAX)]
    frozen_at: datetime

    @model_validator(mode="after")
    def require_state_fields(self) -> Self:
        if self.terminal_model_state == "OPENAI_FAILURE":
            if (
                self.failure_code is None
                or any(
                    value is not None
                    for value in (
                        self.jev_request_sha256,
                        self.jev_response_sha256,
                        self.jev_decision_sha256,
                    )
                )
                or self.jev_cost_microdollars != 0
            ):
                raise ValueError("OpenAI failure barrier fields are inconsistent")
        elif self.terminal_model_state == "JEV_FAILURE":
            if (
                self.failure_code is None
                or self.openai_response_sha256 is None
                or self.jev_request_sha256 is None
                or self.jev_decision_sha256 is not None
            ):
                raise ValueError("Jev failure barrier fields are inconsistent")
        elif self.failure_code is not None or any(
            value is None
            for value in (
                self.openai_response_sha256,
                self.jev_request_sha256,
                self.jev_response_sha256,
                self.jev_decision_sha256,
            )
        ):
            raise ValueError("Jev success barrier fields are inconsistent")
        return self


_LEGACY_SUCCESS_PATH = (
    ArtifactKind.RAW_GAMMA,
    ArtifactKind.SANITIZED_EVIDENCE,
    ArtifactKind.OPENAI_REQUEST,
    ArtifactKind.OPENAI_RESPONSE_SUCCESS,
    ArtifactKind.OPENAI_SUCCESS_METADATA,
    ArtifactKind.RESEARCH_BRIEF,
    ArtifactKind.JEV_REQUEST,
    ArtifactKind.JEV_RESPONSE_SUCCESS,
    ArtifactKind.JEV_SUCCESS_METADATA,
    ArtifactKind.JEV_DECISION,
)
_LEGACY_OPENAI_FAILURE_PATH = (
    ArtifactKind.RAW_GAMMA,
    ArtifactKind.SANITIZED_EVIDENCE,
    ArtifactKind.OPENAI_REQUEST,
    ArtifactKind.OPENAI_FAILURE,
)
_LEGACY_JEV_FAILURE_PATH = (
    ArtifactKind.RAW_GAMMA,
    ArtifactKind.SANITIZED_EVIDENCE,
    ArtifactKind.OPENAI_REQUEST,
    ArtifactKind.OPENAI_RESPONSE_SUCCESS,
    ArtifactKind.OPENAI_SUCCESS_METADATA,
    ArtifactKind.RESEARCH_BRIEF,
    ArtifactKind.JEV_REQUEST,
    ArtifactKind.JEV_FAILURE,
)
_SUCCESS_PATH = (*_LEGACY_SUCCESS_PATH, ArtifactKind.DECISION_FROZEN)
_UNIT6_SUCCESS_PATH = (*_SUCCESS_PATH, ArtifactKind.EXECUTION_BOOK)
_OPENAI_FAILURE_PATH = (*_LEGACY_OPENAI_FAILURE_PATH, ArtifactKind.DECISION_FROZEN)
_OPENAI_INPUT_FAILURE_PATH = (
    ArtifactKind.RAW_GAMMA,
    ArtifactKind.SANITIZED_EVIDENCE,
    ArtifactKind.OPENAI_FAILURE,
    ArtifactKind.DECISION_FROZEN,
)
_JEV_FAILURE_PATH = (*_LEGACY_JEV_FAILURE_PATH, ArtifactKind.DECISION_FROZEN)
_JEV_INPUT_FAILURE_PATH = (
    ArtifactKind.RAW_GAMMA,
    ArtifactKind.SANITIZED_EVIDENCE,
    ArtifactKind.OPENAI_REQUEST,
    ArtifactKind.OPENAI_RESPONSE_SUCCESS,
    ArtifactKind.OPENAI_SUCCESS_METADATA,
    ArtifactKind.RESEARCH_BRIEF,
    ArtifactKind.JEV_FAILURE,
    ArtifactKind.DECISION_FROZEN,
)
_ELIGIBILITY_REJECTION_PATH = (
    ArtifactKind.RAW_GAMMA,
    ArtifactKind.ELIGIBILITY_REJECTION,
)
_WRITE_BASE_PATHS = (
    _SUCCESS_PATH,
    _UNIT6_SUCCESS_PATH,
    _OPENAI_FAILURE_PATH,
    _OPENAI_INPUT_FAILURE_PATH,
    _JEV_FAILURE_PATH,
    _JEV_INPUT_FAILURE_PATH,
    _ELIGIBILITY_REJECTION_PATH,
)
_LATE_PATHS = (
    (*_SUCCESS_PATH, ArtifactKind.BOOK_UNAVAILABLE),
    *(
        (*path, ArtifactKind.BOOK_UNAVAILABLE)
        for path in _WRITE_BASE_PATHS
        if path not in {_SUCCESS_PATH, _UNIT6_SUCCESS_PATH, _ELIGIBILITY_REJECTION_PATH}
    ),
)
_WRITE_STAGE_PATHS = (*_WRITE_BASE_PATHS, *_LATE_PATHS)
_LEGACY_STAGE_PATHS = (
    _LEGACY_SUCCESS_PATH,
    _LEGACY_OPENAI_FAILURE_PATH,
    _LEGACY_JEV_FAILURE_PATH,
)
_REPLAY_STAGE_PATHS = (
    *_WRITE_STAGE_PATHS,
    *_LEGACY_STAGE_PATHS,
)
_WRITE_FAILURE_PATHS = frozenset(
    {
        _OPENAI_FAILURE_PATH,
        _OPENAI_INPUT_FAILURE_PATH,
        _JEV_FAILURE_PATH,
        _JEV_INPUT_FAILURE_PATH,
    }
)
_FAILURE_PATHS = _WRITE_FAILURE_PATHS | frozenset(
    {_LEGACY_OPENAI_FAILURE_PATH, _LEGACY_JEV_FAILURE_PATH}
)
_LATE_FAILURE_PATHS = frozenset(
    (*path, ArtifactKind.BOOK_UNAVAILABLE) for path in _WRITE_FAILURE_PATHS
)
_RESULT_HASH_KINDS = {
    ArtifactKind.RAW_GAMMA: "evidence_sha256",
    ArtifactKind.RESEARCH_BRIEF: "research_brief_sha256",
    ArtifactKind.JEV_DECISION: "jev_decision_sha256",
    ArtifactKind.EXECUTION_BOOK: "execution_book_sha256",
}
Label = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
ByteSize = Annotated[int, Field(strict=True, ge=0, le=INT64_MAX)]
TypedArtifact = StrictModel | Mapping[str, object] | None


class SuccessArtifact(UtcModel):
    """Canonical non-secret billing metadata for one successful provider reply."""

    provider: Literal["openai", "jev"]
    request_sha256: Sha256
    response_sha256: Sha256
    retrieved_at: datetime
    billed_cost_microdollars: ByteSize


class BookUnavailableArtifact(UtcModel):
    """Price-free explanation for refusing a planned execution book."""

    reason: Literal[
        "STALE_BOOK",
        "CROSSED_BOOK",
        "TOKEN_DRIFT",
        "MARKET_DRIFT",
        "BOOK_UNAVAILABLE",
        "STALE_PRECHECK",
    ]
    planned_retrieved_at: datetime
    planned_source_sha256: Sha256
    terminal_model_at: datetime


class EligibilityRejectionArtifact(UtcModel):
    """Canonical reason an exact raw Gamma market was not model-eligible."""

    market_id: NonEmptyStr
    reason: NonEmptyStr
    raw_gamma_sha256: Sha256
    rejected_at: datetime


class FailureArtifact(UtcModel):
    """Canonical sanitized adapter failure metadata; raw bodies are never retained."""

    provider: Literal["openai", "jev"]
    code: Literal[
        "INPUT_ERROR",
        "CONFIG_FAILURE",
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
        if not has_canonical_failure_response(
            self.response_sha256, self.response_byte_length
        ):
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
        elif self.code in {
            "INPUT_ERROR",
            "CONFIG_FAILURE",
            "PROVIDER_ERROR",
            "TIMEOUT",
        }:
            if (
                self.status_code is not None
                or has_response
                or self.retrieved_at is not None
                or self.billed_cost_microdollars != 0
            ):
                raise ValueError("pre-response failure contains response metadata")
        return self


def _stage_path(kinds: tuple[str, ...]) -> tuple[ArtifactKind, ...]:
    for path in _WRITE_STAGE_PATHS:
        if kinds == tuple(kind.value for kind in path):
            return path
    raise ValueError("artifact stage order must match one reviewed path")


def _stage_path_prefix(kinds: tuple[str, ...]) -> tuple[ArtifactKind, ...]:
    matches = [
        path
        for path in _WRITE_STAGE_PATHS
        if kinds == tuple(kind.value for kind in path[: len(kinds)])
    ]
    if not matches:
        raise ValueError("artifact stages must form one reviewed path prefix")
    return matches[0]


def _stage_path_for_result(
    result: ObservationResult, kinds: tuple[str, ...]
) -> tuple[ArtifactKind, ...]:
    path = _stage_path(kinds)
    if path == _ELIGIBILITY_REJECTION_PATH:
        if any(
            arm.disposition != "INELIGIBLE" for arm in (result.ai, result.deterministic)
        ):
            raise ValueError("eligibility path requires terminal INELIGIBLE arms")
        return path
    late = path in _LATE_PATHS
    if late:
        if result.ai.disposition not in {"BOOK_FAILURE", "COST_OVERFLOW"}:
            raise ValueError(
                "late provider path requires terminal BOOK_FAILURE or COST_OVERFLOW"
            )
    elif result.ai.disposition == "COST_OVERFLOW":
        if path not in _FAILURE_PATHS | {_SUCCESS_PATH, _UNIT6_SUCCESS_PATH}:
            raise ValueError("cost overflow does not match provider outcome path")
    elif (result.ai.disposition == "MODEL_FAILURE") != (path in _FAILURE_PATHS):
        raise ValueError("terminal AI disposition does not match provider outcome path")
    return path


def _stage_path_from_envelopes(
    result: ObservationResult, selected: list[ArtifactEnvelope]
) -> tuple[ArtifactKind, ...]:
    kinds = tuple(item.kind for item in selected)
    for path in _REPLAY_STAGE_PATHS:
        if kinds != tuple(kind.value for kind in path):
            continue
        if path in _LEGACY_STAGE_PATHS:
            failed = result.ai.disposition == "MODEL_FAILURE"
            if failed != (path in _FAILURE_PATHS):
                break
            return path
        try:
            return _stage_path_for_result(result, kinds)
        except ValueError:
            break
    raise StoreCorruptError("artifact stage order is incomplete or invalid")


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


def _success_artifact(
    payload: bytes, provider: Literal["openai", "jev"]
) -> SuccessArtifact:
    success = SuccessArtifact.model_validate_json(payload)
    if success.provider != provider:
        raise ValueError("success artifact provider does not match stage")
    return success


def _failure_artifact(
    payload: bytes, provider: Literal["openai", "jev"]
) -> FailureArtifact:
    failure = FailureArtifact.model_validate_json(payload)
    if failure.provider != provider:
        raise ValueError("failure artifact provider does not match stage")
    return failure


def _validate_success_provenance(
    typed: dict[str, TypedArtifact],
    envelopes: list[ArtifactEnvelope],
    *,
    fail: Callable[[str], None],
) -> None:
    envelope_by_kind = {item.kind: item for item in envelopes}
    for provider, request_kind, response_kind, metadata_kind in (
        (
            "OpenAI",
            ArtifactKind.OPENAI_REQUEST,
            ArtifactKind.OPENAI_RESPONSE_SUCCESS,
            ArtifactKind.OPENAI_SUCCESS_METADATA,
        ),
        (
            "Jev",
            ArtifactKind.JEV_REQUEST,
            ArtifactKind.JEV_RESPONSE_SUCCESS,
            ArtifactKind.JEV_SUCCESS_METADATA,
        ),
    ):
        metadata = typed.get(metadata_kind)
        if metadata is None:
            continue
        assert isinstance(metadata, SuccessArtifact)
        if metadata.request_sha256 != envelope_by_kind[request_kind].sha256:
            fail(f"{provider} success request hash does not match frozen request")
        response = envelope_by_kind[response_kind]
        if metadata.response_sha256 != response.sha256:
            fail(f"{provider} success response hash does not match frozen response")
        metadata_envelope = envelope_by_kind[metadata_kind]
        if (
            metadata.retrieved_at != response.retrieved_at
            or metadata.retrieved_at != metadata_envelope.retrieved_at
        ):
            fail(f"{provider} success retrieval time does not match frozen response")


def _base_path(path: tuple[ArtifactKind, ...]) -> tuple[ArtifactKind, ...]:
    return path[:-1] if path in _LATE_PATHS else path


def _validate_failure_provenance(
    path: tuple[ArtifactKind, ...],
    typed: dict[str, TypedArtifact],
    *,
    fail: Callable[[str], None],
) -> None:
    base = _base_path(path)
    if base in {
        _OPENAI_FAILURE_PATH,
        _OPENAI_INPUT_FAILURE_PATH,
        _LEGACY_OPENAI_FAILURE_PATH,
    }:
        failure_kind = ArtifactKind.OPENAI_FAILURE
        request_kind = ArtifactKind.OPENAI_REQUEST
        label = "OpenAI"
    elif base in {
        _JEV_FAILURE_PATH,
        _JEV_INPUT_FAILURE_PATH,
        _LEGACY_JEV_FAILURE_PATH,
    }:
        failure_kind = ArtifactKind.JEV_FAILURE
        request_kind = ArtifactKind.JEV_REQUEST
        label = "Jev"
    else:
        return
    failure = typed[failure_kind]
    assert isinstance(failure, FailureArtifact)
    request = typed.get(request_kind)
    if request is None:
        if (
            failure.code != "INPUT_ERROR"
            or failure.request_sha256 != hashlib.sha256(b"").hexdigest()
        ):
            fail(f"{label} missing request is allowed only for canonical INPUT_ERROR")
        return
    if failure.code == "INPUT_ERROR":
        fail(f"{label} INPUT_ERROR cannot include a request artifact")
    if (
        failure.request_sha256
        != hashlib.sha256(canonical_json_bytes(request)).hexdigest()
    ):
        fail(f"{label} failure request hash does not match frozen request")


def _add_model_costs(left: int, right: int) -> int:
    total = left + right
    if total > INT64_MAX:
        raise ValueError("cumulative model cost exceeds signed 64-bit range")
    return total


def _unit6_model_cost(left: int, right: int) -> int | None:
    total = left + right
    return None if total > INT64_MAX else total


def _expected_model_cost(
    path: tuple[ArtifactKind, ...], typed: dict[str, TypedArtifact]
) -> int | None:
    base = _base_path(path)
    if base in {
        _OPENAI_FAILURE_PATH,
        _OPENAI_INPUT_FAILURE_PATH,
        _LEGACY_OPENAI_FAILURE_PATH,
    }:
        failure = typed[ArtifactKind.OPENAI_FAILURE]
        assert isinstance(failure, FailureArtifact)
        return failure.billed_cost_microdollars
    openai = typed[ArtifactKind.OPENAI_SUCCESS_METADATA]
    assert isinstance(openai, SuccessArtifact)
    if base in {
        _JEV_FAILURE_PATH,
        _JEV_INPUT_FAILURE_PATH,
        _LEGACY_JEV_FAILURE_PATH,
    }:
        failure = typed[ArtifactKind.JEV_FAILURE]
        assert isinstance(failure, FailureArtifact)
        return _add_model_costs(
            openai.billed_cost_microdollars, failure.billed_cost_microdollars
        )
    jev = typed[ArtifactKind.JEV_SUCCESS_METADATA]
    assert isinstance(jev, SuccessArtifact)
    if base == _UNIT6_SUCCESS_PATH:
        return _unit6_model_cost(
            openai.billed_cost_microdollars, jev.billed_cost_microdollars
        )
    return _add_model_costs(
        openai.billed_cost_microdollars, jev.billed_cost_microdollars
    )


def _validate_success_terminal(
    result: ObservationResult,
    path: tuple[ArtifactKind, ...],
    typed: dict[str, TypedArtifact],
    *,
    fail: Callable[[str], None],
) -> None:
    if path not in {_SUCCESS_PATH, _UNIT6_SUCCESS_PATH, _LEGACY_SUCCESS_PATH}:
        return
    if result.deterministic.model_cost_microdollars != 0:
        fail("deterministic arm model cost must be zero")
    expected = _expected_model_cost(path, typed)
    if expected is None:
        if (
            result.ai.disposition != "COST_OVERFLOW"
            or result.ai.model_cost_microdollars is not None
            or result.ai.economic_pnl_microdollars is not None
        ):
            fail("terminal AI arm must disclose cumulative cost overflow")
        return
    if result.ai.model_cost_microdollars != expected:
        fail("terminal AI model cost does not match provider success metadata")


def _validate_failure_terminal(
    result: ObservationResult,
    path: tuple[ArtifactKind, ...],
    typed: dict[str, TypedArtifact],
    *,
    fail: Callable[[str], None],
) -> None:
    ai = result.ai
    if path not in _FAILURE_PATHS:
        return
    try:
        expected: int | None = _expected_model_cost(path, typed)
    except ValueError:
        if ai.disposition != "COST_OVERFLOW":
            raise
        expected = None
    if expected is None:
        if (
            ai.disposition != "COST_OVERFLOW"
            or ai.model_cost_microdollars is not None
            or ai.economic_pnl_microdollars is not None
        ):
            fail("terminal AI arm must disclose cumulative cost overflow")
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


def _validate_late_terminal(
    result: ObservationResult,
    path: tuple[ArtifactKind, ...],
    typed: dict[str, TypedArtifact],
    *,
    fail: Callable[[str], None],
) -> None:
    if path not in _LATE_PATHS:
        return
    unavailable = typed.get(ArtifactKind.BOOK_UNAVAILABLE)
    frozen = typed.get(ArtifactKind.DECISION_FROZEN)
    if not isinstance(unavailable, BookUnavailableArtifact) or not isinstance(
        frozen, DecisionFrozenArtifact
    ):
        fail("late path requires typed book-unavailable and decision barriers")
        return
    try:
        expected_cost: int | None = _expected_model_cost(path, typed)
    except ValueError:
        if result.ai.disposition != "COST_OVERFLOW":
            raise
        expected_cost = None
    if (
        unavailable.terminal_model_at != frozen.frozen_at
        or (
            unavailable.reason == "STALE_PRECHECK"
            and unavailable.terminal_model_at < unavailable.planned_retrieved_at
        )
        or (
            unavailable.reason != "STALE_PRECHECK"
            and unavailable.terminal_model_at >= unavailable.planned_retrieved_at
        )
        or result.execution_book_sha256 is not None
        or result.planned_execution_book_sha256 != unavailable.planned_source_sha256
        or result.baseline_probability_micros is not None
        or result.baseline_brier_micros is not None
        or result.ai.disposition
        != ("COST_OVERFLOW" if expected_cost is None else "BOOK_FAILURE")
        or result.deterministic.disposition != "BOOK_FAILURE"
        or any(
            arm.quantity != 0 or arm.trading_pnl_microdollars != 0
            for arm in (result.ai, result.deterministic)
        )
        or result.deterministic.model_cost_microdollars != 0
        or result.deterministic.economic_pnl_microdollars != 0
    ):
        fail("terminal arms do not match unavailable execution book")
    expected_economic = -expected_cost if expected_cost is not None else None
    if (
        result.ai.model_cost_microdollars != expected_cost
        or result.ai.economic_pnl_microdollars != expected_economic
    ):
        fail("late terminal AI arm does not match incurred provider costs")
    decision = typed.get(ArtifactKind.JEV_DECISION)
    expected_signal = (
        (decision.p_yes_micros, decision.action)
        if isinstance(decision, JevDecision)
        else (None, None)
    )
    if result.ai.disposition != "COST_OVERFLOW" and (
        (
            result.ai.signal_probability_micros,
            result.ai.signal_action,
        )
        != expected_signal
        or result.ai.forecast_probability_micros != expected_signal[0]
    ):
        fail("late terminal forecast does not match available model decision")
    if any(
        value is not None
        for value in (
            result.deterministic.signal_probability_micros,
            result.deterministic.signal_action,
            result.deterministic.forecast_probability_micros,
        )
    ):
        fail("late deterministic arm cannot infer a forecast without a book")


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
        return _open_or_create_root(path)
    except BlobSecurityError as error:
        raise StoreSecurityError("output ancestor or directory is not safe") from error


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
    observation_identity = {
        "opportunity_id": result.opportunity_id,
        "evidence_sha256": result.evidence_sha256,
        "execution_book_sha256": (
            result.execution_book_sha256 or result.planned_execution_book_sha256
        ),
        "policy_version": result.policy_version,
    }
    observation = hashlib.sha256(canonical_json_bytes(observation_identity)).hexdigest()
    if opportunity != result.opportunity_id or observation != result.observation_id:
        error = "persisted observation identity is invalid"
        if corruption:
            raise StoreCorruptError(error)
        raise ValueError(error)


def _verified_identity(result: ObservationResult) -> tuple[ObservationResult, bytes]:
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
    if kind is ArtifactKind.DECISION_FROZEN:
        return DecisionFrozenArtifact.model_validate_json(payload)
    if kind is ArtifactKind.BOOK_UNAVAILABLE:
        return BookUnavailableArtifact.model_validate_json(payload)
    if kind is ArtifactKind.ELIGIBILITY_REJECTION:
        return EligibilityRejectionArtifact.model_validate_json(payload)
    if kind is ArtifactKind.EXECUTION_BOOK:
        raise ValueError("execution book reconstruction requires observation identity")
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
    if kind is ArtifactKind.OPENAI_SUCCESS_METADATA:
        return _success_artifact(payload, "openai")
    if kind is ArtifactKind.JEV_RESPONSE_SUCCESS:
        return JevResponse.model_validate_json(payload)
    if kind is ArtifactKind.JEV_SUCCESS_METADATA:
        return _success_artifact(payload, "jev")
    if kind is ArtifactKind.OPENAI_FAILURE:
        return _failure_artifact(payload, "openai")
    if kind is ArtifactKind.JEV_FAILURE:
        return _failure_artifact(payload, "jev")
    value = load_strict_json(payload)
    if not isinstance(value, Mapping):
        raise ValueError("request artifact must be a JSON object")
    return value


def _reconstruct(
    kind: ArtifactKind, payload: bytes, result: ObservationResult | FixtureIdentity
) -> TypedArtifact:
    try:
        if kind is ArtifactKind.EXECUTION_BOOK:
            return reconstruct_book_snapshot(payload, market_id=result.market_id)
        return _reconstruct_orphan(kind, payload)
    except (TypeError, ValueError) as error:
        label = "Gamma" if kind is ArtifactKind.RAW_GAMMA else kind.value
        raise ValueError(f"{label} artifact violates its strict contract") from error


def _validate_reconstructed_path(
    result: ObservationResult | None,
    typed: dict[str, TypedArtifact],
    path: tuple[ArtifactKind, ...],
    envelopes: list[ArtifactEnvelope],
    *,
    corruption: bool,
    allow_cost_overflow_prefix: bool = False,
) -> None:
    """Validate every semantic binding available in one reviewed path prefix."""

    def fail(message: str) -> None:
        if corruption:
            raise StoreCorruptError(message)
        raise ValueError(message)

    envelope_by_kind = {item.kind: item for item in envelopes}
    evidence = typed.get(ArtifactKind.SANITIZED_EVIDENCE)
    if evidence is not None and not isinstance(evidence, EvidencePacket):
        fail("sanitized evidence artifact type is invalid")
        return
    if isinstance(evidence, EvidencePacket):
        raw = envelope_by_kind.get(ArtifactKind.RAW_GAMMA)
        if raw is not None and evidence.source_sha256 != raw.sha256:
            fail("sanitized evidence does not match frozen source")
        raw_gamma = typed.get(ArtifactKind.RAW_GAMMA)
        if isinstance(raw_gamma, Mapping):
            evidence_value = evidence.model_dump(mode="json")
            raw_bindings = {
                "market_id": raw_gamma.get("id"),
                "question": raw_gamma.get("question"),
                "description": raw_gamma.get("description"),
                "rules": raw_gamma.get("rules"),
                "category": raw_gamma.get("category"),
                "tags": raw_gamma.get("tags"),
                "resolution_source_text": raw_gamma.get("resolutionSource"),
                "closes_at": raw_gamma.get("endDate"),
            }
            if any(
                evidence_value[field] != value for field, value in raw_bindings.items()
            ):
                fail("sanitized evidence does not match raw Gamma identity")
        if result is not None and (
            evidence.market_id != result.market_id
            or evidence.cutoff_at != result.cutoff_at
            or evidence.source_sha256 != result.evidence_sha256
        ):
            fail("sanitized evidence does not match observation")

    if result is not None:
        for kind, field in _RESULT_HASH_KINDS.items():
            envelope = envelope_by_kind.get(kind)
            if envelope is not None and envelope.sha256 != getattr(result, field):
                fail(f"{kind.value} artifact does not match observation hash")
        expected_opportunity_id = hashlib.sha256(
            canonical_json_bytes(
                {
                    "policy_version": result.policy_version,
                    "market_id": result.market_id,
                    "cutoff_at": result.cutoff_at.isoformat().replace("+00:00", "Z"),
                }
            )
        ).hexdigest()
        observation_identity = {
            "opportunity_id": result.opportunity_id,
            "evidence_sha256": result.evidence_sha256,
            "execution_book_sha256": (
                result.execution_book_sha256 or result.planned_execution_book_sha256
            ),
            "policy_version": result.policy_version,
        }
        expected_observation_id = hashlib.sha256(
            canonical_json_bytes(observation_identity)
        ).hexdigest()
        if result.opportunity_id != expected_opportunity_id:
            fail("opportunity ID does not match observation identity")
        if result.observation_id != expected_observation_id:
            fail("observation ID does not match frozen evidence and execution book")

    rejection = typed.get(ArtifactKind.ELIGIBILITY_REJECTION)
    if rejection is not None:
        if not isinstance(rejection, EligibilityRejectionArtifact):
            fail("eligibility rejection artifact type is invalid")
            return
        raw = envelope_by_kind.get(ArtifactKind.RAW_GAMMA)
        if raw is None or rejection.raw_gamma_sha256 != raw.sha256:
            fail("eligibility rejection does not bind raw Gamma bytes")
        if result is not None and (
            result.market_id != rejection.market_id
            or result.evidence_sha256 != rejection.raw_gamma_sha256
            or result.observed_at < rejection.rejected_at
            or any(
                arm.disposition != "INELIGIBLE"
                or arm.quantity != 0
                or arm.trading_pnl_microdollars != 0
                or arm.economic_pnl_microdollars != 0
                or arm.model_cost_microdollars != 0
                or arm.initial_cash_microdollars != arm.final_cash_microdollars
                or arm.forecast_status != "UNAVAILABLE"
                for arm in (result.ai, result.deterministic)
            )
        ):
            fail("terminal result does not match eligibility rejection")

    execution_book = typed.get(ArtifactKind.EXECUTION_BOOK)
    if execution_book is not None:
        if not isinstance(execution_book, BookSnapshot):
            fail("execution book artifact type is invalid")
            return
        envelope = envelope_by_kind.get(ArtifactKind.EXECUTION_BOOK)
        frozen = typed.get(ArtifactKind.DECISION_FROZEN)
        if (
            envelope is None
            or not isinstance(frozen, DecisionFrozenArtifact)
            or execution_book.source_sha256 != envelope.sha256
            or execution_book.retrieved_at != envelope.retrieved_at
            or execution_book.retrieved_at <= frozen.frozen_at
            or (
                result is not None
                and (
                    execution_book.market_id != result.market_id
                    or result.execution_book_sha256 != envelope.sha256
                    or result.observed_at < execution_book.retrieved_at
                )
            )
        ):
            fail("execution book does not bind result identity and chronology")

    openai_request = typed.get(ArtifactKind.OPENAI_REQUEST)
    if isinstance(evidence, EvidencePacket) and openai_request is not None:
        if openai_request != load_strict_json(
            build_openai_request(evidence, OPENAI_MODEL_ID)
        ):
            fail("OpenAI request does not match evidence")

    _validate_success_provenance(typed, envelopes, fail=fail)

    base_path = _base_path(path)
    if (
        base_path
        in {
            _OPENAI_FAILURE_PATH,
            _OPENAI_INPUT_FAILURE_PATH,
            _LEGACY_OPENAI_FAILURE_PATH,
        }
        and ArtifactKind.OPENAI_FAILURE in typed
    ):
        _validate_failure_provenance(path, typed, fail=fail)
        if result is not None and path not in _LATE_PATHS:
            _validate_failure_terminal(result, base_path, typed, fail=fail)

    brief = typed.get(ArtifactKind.RESEARCH_BRIEF)
    openai_response = typed.get(ArtifactKind.OPENAI_RESPONSE_SUCCESS)
    if brief is not None and not isinstance(brief, ResearchBrief):
        fail("research brief artifact type is invalid")
        return
    if openai_response is not None and not isinstance(openai_response, OpenAIResponse):
        fail("OpenAI success artifact type is invalid")
        return
    if isinstance(brief, ResearchBrief):
        if isinstance(evidence, EvidencePacket) and (
            brief.evidence_sha256 != evidence.source_sha256
        ):
            fail("research brief does not match frozen evidence")
        if result is not None and brief.evidence_sha256 != result.evidence_sha256:
            fail("research brief does not match observation evidence")
        if (
            isinstance(openai_response, OpenAIResponse)
            and openai_response_brief(openai_response) != brief
        ):
            fail("OpenAI response does not match research brief")

    jev_request = typed.get(ArtifactKind.JEV_REQUEST)
    if (
        isinstance(brief, ResearchBrief)
        and isinstance(evidence, EvidencePacket)
        and jev_request is not None
        and jev_request
        != load_strict_json(build_jev_request(brief, evidence, JEV_MODEL_ID))
    ):
        fail("Jev request does not match evidence and brief")

    if (
        base_path
        in {
            _JEV_FAILURE_PATH,
            _JEV_INPUT_FAILURE_PATH,
            _LEGACY_JEV_FAILURE_PATH,
        }
        and ArtifactKind.JEV_FAILURE in typed
    ):
        _validate_failure_provenance(path, typed, fail=fail)
        try:
            _expected_model_cost(path, typed)
        except ValueError as error:
            terminal_overflow = (
                result is not None and result.ai.disposition == "COST_OVERFLOW"
            )
            if not (terminal_overflow or allow_cost_overflow_prefix):
                fail(str(error))
        if result is not None and path not in _LATE_PATHS:
            _validate_failure_terminal(result, base_path, typed, fail=fail)

    decision = typed.get(ArtifactKind.JEV_DECISION)
    jev_response = typed.get(ArtifactKind.JEV_RESPONSE_SUCCESS)
    if decision is not None and not isinstance(decision, JevDecision):
        fail("Jev decision artifact type is invalid")
        return
    if jev_response is not None and not isinstance(jev_response, JevResponse):
        fail("Jev success artifact type is invalid")
        return
    if isinstance(decision, JevDecision) and isinstance(jev_response, JevResponse):
        if jev_response_decision(jev_response) != decision:
            fail("Jev response does not match decision")
        if result is not None and (
            result.ai.disposition != "COST_OVERFLOW"
            and (
                result.ai.signal_probability_micros != decision.p_yes_micros
                or result.ai.forecast_probability_micros != decision.p_yes_micros
                or result.ai.signal_action != decision.action
            )
        ):
            fail("terminal AI signal does not match frozen Jev decision")

    frozen = typed.get(ArtifactKind.DECISION_FROZEN)
    if frozen is not None:
        if not isinstance(frozen, DecisionFrozenArtifact):
            fail("decision frozen artifact type is invalid")
            return
        if (
            frozen.opportunity_id != envelopes[0].opportunity_id
            or frozen.observation_id != envelopes[0].observation_id
        ):
            fail("decision frozen artifact identity is inconsistent")
        frozen_envelope = envelope_by_kind.get(ArtifactKind.DECISION_FROZEN)
        terminal_envelope = envelope_by_kind.get(ArtifactKind.DECISION_FROZEN)
        prior_terminal_envelope = envelopes[
            path.index(ArtifactKind.DECISION_FROZEN) - 1
        ]
        if (
            frozen_envelope is None
            or frozen.frozen_at != frozen_envelope.retrieved_at
            or frozen_envelope is not terminal_envelope
            or frozen.frozen_at < prior_terminal_envelope.retrieved_at
        ):
            fail("decision frozen timestamp is inconsistent")
        request_envelope = envelope_by_kind.get(ArtifactKind.OPENAI_REQUEST)
        expected_openai_request = (
            request_envelope.sha256
            if request_envelope is not None
            else hashlib.sha256(b"").hexdigest()
        )
        if frozen.openai_request_sha256 != expected_openai_request:
            fail("decision frozen artifact does not bind OpenAI request")
        openai_response_envelope = envelope_by_kind.get(
            ArtifactKind.OPENAI_RESPONSE_SUCCESS
        )
        openai_failure_response = (
            openai_failure.response_sha256
            if isinstance(
                openai_failure := typed.get(ArtifactKind.OPENAI_FAILURE),
                FailureArtifact,
            )
            else None
        )
        expected_openai_response = (
            openai_response_envelope.sha256
            if openai_response_envelope
            else openai_failure_response
        )
        if frozen.openai_response_sha256 != expected_openai_response:
            fail("decision frozen artifact does not bind OpenAI response")
        jev_request_envelope = envelope_by_kind.get(ArtifactKind.JEV_REQUEST)
        jev_failure_artifact = typed.get(ArtifactKind.JEV_FAILURE)
        expected_jev_request = (
            jev_request_envelope.sha256
            if jev_request_envelope is not None
            else hashlib.sha256(b"").hexdigest()
            if isinstance(jev_failure_artifact, FailureArtifact)
            and jev_failure_artifact.code == "INPUT_ERROR"
            else None
        )
        if frozen.jev_request_sha256 != expected_jev_request:
            fail("decision frozen artifact does not bind Jev request")
        jev_response_envelope = envelope_by_kind.get(ArtifactKind.JEV_RESPONSE_SUCCESS)
        jev_failure_response = (
            jev_failure.response_sha256
            if isinstance(
                jev_failure := typed.get(ArtifactKind.JEV_FAILURE), FailureArtifact
            )
            else None
        )
        expected_jev_response = (
            jev_response_envelope.sha256
            if jev_response_envelope
            else jev_failure_response
        )
        if frozen.jev_response_sha256 != expected_jev_response:
            fail("decision frozen artifact does not bind Jev response")
        decision_envelope = envelope_by_kind.get(ArtifactKind.JEV_DECISION)
        if frozen.jev_decision_sha256 != (
            decision_envelope.sha256 if decision_envelope else None
        ):
            fail("decision frozen artifact does not bind the Jev decision")
        openai_metadata = typed.get(ArtifactKind.OPENAI_SUCCESS_METADATA)
        openai_failure = typed.get(ArtifactKind.OPENAI_FAILURE)
        openai_cost = (
            openai_metadata.billed_cost_microdollars
            if isinstance(openai_metadata, SuccessArtifact)
            else openai_failure.billed_cost_microdollars
            if isinstance(openai_failure, FailureArtifact)
            else 0
        )
        jev_metadata = typed.get(ArtifactKind.JEV_SUCCESS_METADATA)
        jev_failure = typed.get(ArtifactKind.JEV_FAILURE)
        jev_cost = (
            jev_metadata.billed_cost_microdollars
            if isinstance(jev_metadata, SuccessArtifact)
            else jev_failure.billed_cost_microdollars
            if isinstance(jev_failure, FailureArtifact)
            else 0
        )
        expected_state = (
            "OPENAI_FAILURE"
            if ArtifactKind.OPENAI_FAILURE in typed
            else "JEV_FAILURE"
            if ArtifactKind.JEV_FAILURE in typed
            else "JEV_SUCCESS"
        )
        expected_failure_code = (
            openai_failure.code
            if isinstance(openai_failure, FailureArtifact)
            else jev_failure.code
            if isinstance(jev_failure, FailureArtifact)
            else None
        )
        if (
            frozen.terminal_model_state != expected_state
            or frozen.failure_code != expected_failure_code
            or frozen.openai_cost_microdollars != openai_cost
            or frozen.jev_cost_microdollars != jev_cost
        ):
            fail(
                "decision frozen artifact terminal state, failure code, or costs "
                "are inconsistent"
            )

    success_metadata_complete = all(
        kind in typed
        for kind in (
            ArtifactKind.OPENAI_SUCCESS_METADATA,
            ArtifactKind.JEV_SUCCESS_METADATA,
        )
    )
    if success_metadata_complete:
        try:
            _expected_model_cost(_SUCCESS_PATH, typed)
        except ValueError as error:
            terminal_overflow = (
                result is not None
                and result.ai.disposition == "COST_OVERFLOW"
                and (path == _UNIT6_SUCCESS_PATH or path in _LATE_PATHS)
            )
            if not (terminal_overflow or allow_cost_overflow_prefix):
                fail(str(error))
    if (
        path in {_SUCCESS_PATH, _UNIT6_SUCCESS_PATH, _LEGACY_SUCCESS_PATH}
        and result is not None
    ):
        _validate_success_terminal(result, path, typed, fail=fail)
    if result is not None:
        _validate_late_terminal(result, path, typed, fail=fail)


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
    _validate_reconstructed_path(result, typed, path, envelopes, corruption=False)
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
            for path in _REPLAY_STAGE_PATHS
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
        if any(
            kinds == tuple(kind.value for kind in path) for path in _LEGACY_STAGE_PATHS
        ) and kinds != tuple(kind.value for kind in _SUCCESS_PATH):
            raise StoreCorruptError("legacy artifact path requires a terminal result")

        typed: dict[str, TypedArtifact] = {}
        for envelope, kind in zip(selected, matching_paths[0], strict=False):
            try:
                payload = store.read_verified(envelope.blob_ref())
                _screen_payload(payload, forbidden_secret_values, strict_json=True)
                if kind is ArtifactKind.EXECUTION_BOOK:
                    evidence = typed.get(ArtifactKind.SANITIZED_EVIDENCE)
                    if not isinstance(evidence, EvidencePacket):
                        raise ValueError(
                            "execution book reconstruction requires frozen evidence"
                        )
                    typed[kind.value] = reconstruct_book_snapshot(
                        payload, market_id=evidence.market_id
                    )
                else:
                    typed[kind.value] = _reconstruct_orphan(kind, payload)
            except StoreSecurityError:
                raise
            except (BlobError, TypeError, ValueError) as error:
                raise StoreCorruptError(
                    "artifact blob is missing or corrupt"
                ) from error
        closed_path = next(
            (path for path in matching_paths if len(selected) == len(path)), None
        )
        validation_path = closed_path or matching_paths[0]
        _validate_reconstructed_path(
            None,
            typed,
            validation_path,
            selected,
            corruption=True,
        )

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
    _validate_reconstructed_path(result, typed, path, selected, corruption=True)
    return ObservationReplay(
        result=result,
        envelopes=by_kind,
        artifacts=artifacts,
        typed_artifacts=typed,
    )


def _is_legacy_replay(replay: ObservationReplay) -> bool:
    return tuple(replay.artifacts) in {
        tuple(kind.value for kind in path) for path in _LEGACY_STAGE_PATHS
    }


def freeze_observation_artifacts(
    output_dir: str | os.PathLike[str],
    identity: ObservationResult | FixtureIdentity,
    artifact_refs: Iterable[ArtifactToFreeze],
    *,
    forbidden_secret_values: tuple[bytes, ...] = (),
) -> tuple[ArtifactEnvelope, ...]:
    """Publish and journal a validated reviewed-path prefix without its result."""

    if type(identity) is ObservationResult:
        verified_identity: ObservationResult | FixtureIdentity = _verified_identity(
            identity
        )[0]
    elif type(identity) is FixtureIdentity:
        verified_identity = FixtureIdentity.model_validate_json(
            canonical_json_bytes(identity.model_dump(mode="json"))
        )
    else:
        raise TypeError("identity must be an ObservationResult or FixtureIdentity")
    artifacts = list(artifact_refs)
    path = _stage_path_prefix(tuple(item.kind for item in artifacts))
    envelopes: list[ArtifactEnvelope] = []
    typed: dict[str, TypedArtifact] = {}
    predecessor: str | None = None
    for artifact, kind in zip(artifacts, path, strict=False):
        _screen_payload(artifact.payload, forbidden_secret_values, strict_json=True)
        payload_hash = hashlib.sha256(artifact.payload).hexdigest()
        envelope = ArtifactEnvelope(
            kind=kind.value,
            sha256=payload_hash,
            byte_size=len(artifact.payload),
            storage_relpath=f"{payload_hash[:2]}/{payload_hash[2:4]}/{payload_hash}",
            source_uri_label=artifact.source_uri_label,
            retrieved_at=artifact.retrieved_at,
            opportunity_id=verified_identity.opportunity_id,
            observation_id=verified_identity.observation_id,
            predecessor_stage_sha256=artifact.predecessor_stage_sha256,
        )
        if envelope.predecessor_stage_sha256 != predecessor:
            raise ValueError("artifact predecessor chain is invalid")
        typed[kind.value] = _reconstruct(kind, artifact.payload, verified_identity)
        envelopes.append(envelope)
        predecessor = _canonical_stage_hash(envelope)
    _validate_reconstructed_path(
        None,
        typed,
        path,
        envelopes,
        corruption=False,
        allow_cost_overflow_prefix=True,
    )
    envelope_lines = [
        canonical_json_bytes(envelope.model_dump(mode="json")) for envelope in envelopes
    ]
    output = Path(output_dir)
    root_fd = _open_output(output)
    try:
        with BlobStore(output.absolute() / "blobs") as store:
            results_fd = _open_journal(root_fd, "results.jsonl")
            try:
                _lock(results_fd, fcntl.LOCK_EX)
                existing_results = _parse_results(
                    _read_journal(results_fd, label="result journal")
                )
                artifacts_fd = _open_journal(root_fd, "artifacts.jsonl")
                try:
                    _lock(artifacts_fd, fcntl.LOCK_EX)
                    existing_envelopes = _parse_envelopes(
                        _read_journal(artifacts_fd, label="artifact journal")
                    )
                    replays = _validate_journals_locked(
                        store,
                        existing_results,
                        existing_envelopes,
                        forbidden_secret_values,
                    )
                    existing_replay = replays.get(verified_identity.observation_id)
                    if existing_replay is not None and _is_legacy_replay(
                        existing_replay
                    ):
                        raise StoreConflictError(
                            "completed legacy observation is replay-only"
                        )
                    _publish(store, artifacts, envelopes)
                    prior = [
                        item
                        for item in existing_envelopes
                        if item.observation_id == verified_identity.observation_id
                    ]
                    common = min(len(prior), len(envelopes))
                    if prior[:common] != envelopes[:common]:
                        raise StoreConflictError(
                            "artifact stage identity already has different bytes"
                        )
                    missing = envelope_lines[len(prior) :]
                    if missing:
                        _append_lines(artifacts_fd, missing, label="artifact journal")
                    else:
                        os.fsync(artifacts_fd)
                    return tuple(envelopes)
                finally:
                    os.close(artifacts_fd)
            finally:
                os.close(results_fd)
    finally:
        os.close(root_fd)


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
    verified_identity, result_line = _verified_identity(result)
    _screen_payload(result_line, forbidden_secret_values)
    artifacts, envelopes, _typed = _prepare(
        verified_identity, artifact_refs, forbidden_secret_values
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
            results_fd = _open_journal(root_fd, "results.jsonl")
            try:
                _lock(results_fd, fcntl.LOCK_EX)
                existing_results = _parse_results(
                    _read_journal(results_fd, label="result journal")
                )
                existing = existing_results.get(verified_identity.observation_id)
                artifacts_fd = _open_journal(root_fd, "artifacts.jsonl")
                try:
                    _lock(artifacts_fd, fcntl.LOCK_EX)
                    existing_envelopes = _parse_envelopes(
                        _read_journal(artifacts_fd, label="artifact journal")
                    )
                    replays = _validate_journals_locked(
                        store,
                        existing_results,
                        existing_envelopes,
                        forbidden_secret_values,
                    )
                    existing_replay = replays.get(verified_identity.observation_id)
                    if existing_replay is not None and _is_legacy_replay(
                        existing_replay
                    ):
                        raise StoreConflictError(
                            "completed legacy observation is replay-only"
                        )
                    _publish(store, artifacts, envelopes)
                    prior_artifacts = [
                        item
                        for item in existing_envelopes
                        if item.observation_id == verified_identity.observation_id
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
                    return verified_identity
                finally:
                    os.close(artifacts_fd)
            finally:
                os.close(results_fd)
    finally:
        os.close(root_fd)


def load_existing_terminal(
    output_dir: str | os.PathLike[str],
    observation_id: str,
    *,
    forbidden_secret_values: tuple[bytes, ...] = (),
) -> ObservationReplay | None:
    """Return a verified current or legacy terminal observation, if present."""

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
                artifacts_fd = _open_journal(root_fd, "artifacts.jsonl")
                try:
                    _lock(artifacts_fd, fcntl.LOCK_SH)
                    envelopes = _parse_envelopes(
                        _read_journal(artifacts_fd, label="artifact journal")
                    )
                    replays = _validate_journals_locked(
                        store, results, envelopes, forbidden_secret_values
                    )
                    return replays.get(observation_id)
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

    replay = load_existing_terminal(
        output_dir,
        observation_id,
        forbidden_secret_values=forbidden_secret_values,
    )
    if replay is None:
        raise StoreCorruptError("observation result is missing")
    return replay

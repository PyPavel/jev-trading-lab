"""Bounded, price-blind model adapters with injectable transports."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from pydantic import ValidationError

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.polymarket.models import (
    EvidencePacket,
    JevDecision,
    JevResponse,
    OpenAIResponse,
    ResearchBrief,
    load_strict_json,
)

_OPENAI_DELIMITERS = ("<evidence>", "</evidence>")
_JEV_QUESTIONS = (
    "evidence_sufficiency",
    "ambiguity",
    "p_yes",
    "action (BUY_YES|BUY_NO|ABSTAIN)",
    "confidence",
)
_FORBIDDEN_NAMES = frozenset(
    {
        "ask",
        "asks",
        "bestask",
        "bestbid",
        "bid",
        "bids",
        "book",
        "booksnapshot",
        "midpoint",
        "outcomeprices",
        "price",
        "prices",
        "token",
        "tokenid",
    }
)
type FailureCode = Literal[
    "PROVIDER_ERROR",
    "TIMEOUT",
    "HTTP_ERROR",
    "PARSE_ERROR",
    "MODEL_MISMATCH",
    "CITATION_ERROR",
    "COST_MISMATCH",
]


@dataclass(frozen=True, slots=True)
class TransportResponse:
    """One provider reply; callers discard headers after boundary validation."""

    status_code: int
    headers: Mapping[str, str]
    raw_bytes: bytes
    retrieved_at: datetime
    billed_cost_microdollars: int


class Transport(Protocol):
    """Single-attempt byte transport implemented by fixtures now and HTTP later."""

    def __call__(self, request_bytes: bytes) -> TransportResponse: ...


@dataclass(frozen=True, slots=True)
class OpenAISuccess:
    brief: ResearchBrief
    request_bytes: bytes
    response_bytes: bytes
    retrieved_at: datetime
    billed_cost_microdollars: int


@dataclass(frozen=True, slots=True)
class JevSuccess:
    decision: JevDecision
    request_bytes: bytes
    response_bytes: bytes
    retrieved_at: datetime
    billed_cost_microdollars: int


@dataclass(frozen=True, slots=True)
class AdapterFailure:
    provider: Literal["openai", "jev"]
    code: FailureCode
    message: str
    request_bytes: bytes
    response_bytes: bytes
    retrieved_at: datetime | None
    billed_cost_microdollars: int
    status_code: int | None


type OpenAIResult = OpenAISuccess | AdapterFailure
type JevResult = JevSuccess | AdapterFailure


def _normalized_name(name: str) -> str:
    return "".join(character for character in name.lower() if character.isalnum())


def _is_forbidden_name(value: str) -> bool:
    normalized = _normalized_name(value)
    return normalized in _FORBIDDEN_NAMES or normalized.startswith(
        ("ask", "bid", "book", "midpoint", "outcomeprices", "price", "token")
    )


def _reject_forbidden_names(value: object, *, label: str) -> None:
    if isinstance(value, Mapping):
        forbidden = {str(key) for key in value if _is_forbidden_name(str(key))}
        if forbidden:
            raise ValueError(f"{label} contains forbidden fields: {sorted(forbidden)}")
        for nested in value.values():
            _reject_forbidden_names(nested, label=label)
    elif isinstance(value, list | tuple):
        for nested in value:
            _reject_forbidden_names(nested, label=label)
    elif isinstance(value, str) and _is_forbidden_name(value):
        raise ValueError(f"{label} contains forbidden value: {value}")


def _strict_research_schema() -> dict[str, object]:
    schema = ResearchBrief.model_json_schema()
    _reject_forbidden_names(schema, label="OpenAI response schema")
    return schema


def build_openai_request(evidence: EvidencePacket, model: str) -> bytes:
    """Build one canonical, tool-free request from an EvidencePacket only."""

    if not isinstance(evidence, EvidencePacket):
        raise TypeError("OpenAI request requires an EvidencePacket")
    if type(model) is not str or not model:
        raise ValueError("OpenAI model must be a non-empty immutable snapshot ID")
    evidence_value = evidence.model_dump(mode="json")
    _reject_forbidden_names(evidence_value, label="OpenAI evidence")
    evidence_bytes = canonical_json_bytes(evidence_value)
    evidence_input = (
        _OPENAI_DELIMITERS[0] + evidence_bytes.decode() + _OPENAI_DELIMITERS[1]
    )
    request = {
        "input": evidence_input,
        "model": model,
        "response_format": {
            "json_schema": {
                "name": "strict-research-brief-v1",
                "schema": _strict_research_schema(),
                "strict": True,
            },
            "type": "json_schema",
        },
    }
    _reject_forbidden_names(request, label="OpenAI request")
    return canonical_json_bytes(request)


def build_jev_request(
    brief: ResearchBrief, evidence: EvidencePacket, model: str
) -> bytes:
    """Build canonical Jev state from a frozen brief and safe evidence identity."""

    if not isinstance(brief, ResearchBrief):
        raise TypeError("Jev request requires a ResearchBrief")
    if not isinstance(evidence, EvidencePacket):
        raise TypeError("Jev request requires an EvidencePacket, not a BookSnapshot")
    if type(model) is not str or not model:
        raise ValueError("Jev model must be a non-empty immutable snapshot ID")
    if brief.evidence_sha256 != evidence.source_sha256:
        raise ValueError("research brief must bind the supplied evidence SHA-256")
    brief_value = brief.model_dump(mode="json")
    brief_sha256 = hashlib.sha256(canonical_json_bytes(brief_value)).hexdigest()
    request = {
        "model": model,
        "questions": list(_JEV_QUESTIONS),
        "state": {
            "brief": brief_value,
            "evidence_sha256": evidence.source_sha256,
            "research_brief_sha256": brief_sha256,
        },
    }
    _reject_forbidden_names(request, label="Jev request")
    return canonical_json_bytes(request)


def _transport_failure(
    *,
    provider: Literal["openai", "jev"],
    code: FailureCode,
    message: str,
    request_bytes: bytes,
    response: TransportResponse | None = None,
) -> AdapterFailure:
    return AdapterFailure(
        provider=provider,
        code=code,
        message=message,
        request_bytes=request_bytes,
        response_bytes=b"" if response is None else response.raw_bytes,
        retrieved_at=None if response is None else response.retrieved_at,
        billed_cost_microdollars=(
            0 if response is None else response.billed_cost_microdollars
        ),
        status_code=None if response is None else response.status_code,
    )


def _send_once(
    provider: Literal["openai", "jev"],
    request_bytes: bytes,
    transport: Callable[[bytes], TransportResponse],
) -> TransportResponse | AdapterFailure:
    try:
        response = transport(request_bytes)
    except TimeoutError:
        return _transport_failure(
            provider=provider,
            code="TIMEOUT",
            message=f"{provider} transport timed out",
            request_bytes=request_bytes,
        )
    except Exception:
        return _transport_failure(
            provider=provider,
            code="PROVIDER_ERROR",
            message=f"{provider} transport failed",
            request_bytes=request_bytes,
        )
    valid_retrieval = (
        isinstance(response.retrieved_at, datetime)
        and response.retrieved_at.tzinfo is not None
        and response.retrieved_at.utcoffset() == timedelta(0)
    )
    if (
        type(response.status_code) is not int
        or not isinstance(response.raw_bytes, bytes)
        or type(response.billed_cost_microdollars) is not int
        or response.billed_cost_microdollars < 0
        or not valid_retrieval
    ):
        return _transport_failure(
            provider=provider,
            code="PROVIDER_ERROR",
            message=f"{provider} transport returned invalid metadata",
            request_bytes=request_bytes,
        )
    if response.status_code < 200 or response.status_code >= 300:
        return _transport_failure(
            provider=provider,
            code="HTTP_ERROR",
            message=f"{provider} returned HTTP status {response.status_code}",
            request_bytes=request_bytes,
            response=response,
        )
    return response


def _decoded_response(
    provider: Literal["openai", "jev"],
    request_bytes: bytes,
    response: TransportResponse,
) -> Mapping[str, object] | AdapterFailure:
    try:
        decoded = load_strict_json(response.raw_bytes)
    except (TypeError, ValueError):
        return _transport_failure(
            provider=provider,
            code="PARSE_ERROR",
            message=f"{provider} response is not strict JSON",
            request_bytes=request_bytes,
            response=response,
        )
    if not isinstance(decoded, Mapping):
        return _transport_failure(
            provider=provider,
            code="PARSE_ERROR",
            message=f"{provider} response must be a JSON object",
            request_bytes=request_bytes,
            response=response,
        )
    return decoded


def call_openai(
    evidence: EvidencePacket,
    model: str,
    transport: Callable[[bytes], TransportResponse],
) -> OpenAIResult:
    """Perform exactly one OpenAI call and return a typed success or failure."""

    request_bytes = build_openai_request(evidence, model)
    sent = _send_once("openai", request_bytes, transport)
    if isinstance(sent, AdapterFailure):
        return sent
    decoded = _decoded_response("openai", request_bytes, sent)
    if isinstance(decoded, AdapterFailure):
        return decoded
    if decoded.get("model") != model:
        return _transport_failure(
            provider="openai",
            code="MODEL_MISMATCH",
            message="openai response model does not match requested snapshot",
            request_bytes=request_bytes,
            response=sent,
        )
    raw_output = decoded.get("output")
    citation_hashes: list[object] = []
    if isinstance(raw_output, Mapping):
        citations = raw_output.get("cited_facts")
        if isinstance(citations, list):
            citation_hashes = [
                citation.get("evidence_sha256")
                for citation in citations
                if isinstance(citation, Mapping) and "evidence_sha256" in citation
            ]
        if raw_output.get("evidence_sha256") not in {
            None,
            evidence.source_sha256,
        } or any(value != evidence.source_sha256 for value in citation_hashes):
            return _transport_failure(
                provider="openai",
                code="CITATION_ERROR",
                message="openai brief does not cite the supplied evidence",
                request_bytes=request_bytes,
                response=sent,
            )
    try:
        envelope = OpenAIResponse.model_validate(decoded)
    except ValidationError:
        return _transport_failure(
            provider="openai",
            code="PARSE_ERROR",
            message="openai response violates the strict envelope",
            request_bytes=request_bytes,
            response=sent,
        )
    if envelope.output.evidence_sha256 != evidence.source_sha256:
        return _transport_failure(
            provider="openai",
            code="CITATION_ERROR",
            message="openai brief does not cite the supplied evidence",
            request_bytes=request_bytes,
            response=sent,
        )
    if envelope.cost_microdollars != sent.billed_cost_microdollars:
        return _transport_failure(
            provider="openai",
            code="COST_MISMATCH",
            message="openai billed cost does not match the strict envelope",
            request_bytes=request_bytes,
            response=sent,
        )
    return OpenAISuccess(
        brief=envelope.output,
        request_bytes=request_bytes,
        response_bytes=sent.raw_bytes,
        retrieved_at=sent.retrieved_at,
        billed_cost_microdollars=sent.billed_cost_microdollars,
    )


def call_jev(
    brief: ResearchBrief,
    evidence: EvidencePacket,
    model: str,
    transport: Callable[[bytes], TransportResponse],
) -> JevResult:
    """Perform exactly one Jev call and return a typed success or failure."""

    request_bytes = build_jev_request(brief, evidence, model)
    sent = _send_once("jev", request_bytes, transport)
    if isinstance(sent, AdapterFailure):
        return sent
    decoded = _decoded_response("jev", request_bytes, sent)
    if isinstance(decoded, AdapterFailure):
        return decoded
    if decoded.get("model") != model:
        return _transport_failure(
            provider="jev",
            code="MODEL_MISMATCH",
            message="jev response model does not match requested snapshot",
            request_bytes=request_bytes,
            response=sent,
        )
    try:
        envelope = JevResponse.model_validate(decoded)
    except ValidationError:
        return _transport_failure(
            provider="jev",
            code="PARSE_ERROR",
            message="jev response violates the strict envelope",
            request_bytes=request_bytes,
            response=sent,
        )
    if envelope.cost_microdollars != sent.billed_cost_microdollars:
        return _transport_failure(
            provider="jev",
            code="COST_MISMATCH",
            message="jev billed cost does not match the strict envelope",
            request_bytes=request_bytes,
            response=sent,
        )
    return JevSuccess(
        decision=envelope.result,
        request_bytes=request_bytes,
        response_bytes=sent.raw_bytes,
        retrieved_at=sent.retrieved_at,
        billed_cost_microdollars=sent.billed_cost_microdollars,
    )

"""Bounded, price-blind model adapters with injectable transports."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal, Protocol

from pydantic import ValidationError

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.polymarket.models import (
    INT64_MAX,
    EvidencePacket,
    JevDecision,
    JevResponse,
    OpenAIResponse,
    ResearchBrief,
    jev_response_decision,
    load_strict_json,
    openai_response_brief,
)

OPENAI_MODEL_ID: Final = "gpt-5-2025-08-07"
JEV_MODEL_ID: Final = "jev-1.13.0"
_OPENAI_DELIMITERS = ("<evidence>", "</evidence>")
_MAX_PROSE_SCAN_BYTES = 20_000
_MAX_JSON_CANDIDATES = 128
_MAX_SENSITIVE_VALUES = 16
_MAX_SENSITIVE_VALUE_BYTES = 256
_SECRET_PATTERN = re.compile(
    rb"(?i)(?:bearer\s+[!#$%&'*+.^_`|~0-9a-z-]{4,}|basic\s+[a-z0-9+/=]{4,}|(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|api[-_]?key)\s*[:=]\s*[^\s,;]{4,})"
)
_SCORE_CRITERIA = ["No", "Yes"]
_JEV_QUESTIONS: Final[dict[str, object]] = {
    "evidence_sufficient": {
        "type": "noul",
        "instructions": (
            "Is the supplied research brief sufficient to estimate the event outcome "
            "without any market data?"
        ),
        "criteria": {
            "true": "The brief supports an outcome estimate.",
            "false": "Material evidence needed for an estimate is absent.",
        },
    },
    "materially_ambiguous": {
        "type": "noul",
        "instructions": (
            "Are the event rules or supplied evidence materially ambiguous about the "
            "outcome definition?"
        ),
        "criteria": {
            "true": "Ambiguity could change the resolved outcome.",
            "false": "The outcome definition is sufficiently clear.",
        },
    },
    "p_yes": {
        "type": "score",
        "instructions": (
            "Estimate the probability that the event resolves YES using only the state."
        ),
        "criteria": _SCORE_CRITERIA,
    },
    "action": {
        "type": "choice",
        "instructions": (
            "Select the research action justified by the state, without considering "
            "any market price or order book."
        ),
        "criteria": {
            "BUY_YES": "Evidence favors YES strongly enough to permit a YES signal.",
            "BUY_NO": "Evidence favors NO strongly enough to permit a NO signal.",
            "ABSTAIN": (
                "Evidence is insufficient, ambiguous, or lacks a justified side."
            ),
        },
    },
    "confidence": {
        "type": "score",
        "instructions": (
            "Estimate confidence that the selected action is supported by the "
            "supplied state."
        ),
        "criteria": _SCORE_CRITERIA,
    },
}
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
        "impliedprobability",
        "marketprice",
        "midpoint",
        "outcomeprices",
        "price",
        "prices",
        "token",
        "tokenid",
        "yesprice",
    }
)
type FailureCode = Literal[
    "INPUT_ERROR",
    "PROVIDER_ERROR",
    "TIMEOUT",
    "HTTP_ERROR",
    "PARSE_ERROR",
    "MODEL_MISMATCH",
    "CITATION_ERROR",
    "CREDENTIAL_REFLECTION",
]


@dataclass(frozen=True, slots=True)
class TransportResponse:
    """One provider reply; callers discard headers and sensitive sentinels."""

    status_code: int
    headers: Mapping[str, str] = field(repr=False)
    raw_bytes: bytes = field(repr=False)
    retrieved_at: datetime
    billed_cost_microdollars: int
    sensitive_values: tuple[bytes, ...] = field(default=(), repr=False)


class Transport(Protocol):
    """Single-attempt byte transport implemented by fixtures now and HTTP later."""

    def __call__(self, request_bytes: bytes) -> TransportResponse: ...


@dataclass(frozen=True, slots=True)
class OpenAISuccess:
    brief: ResearchBrief
    request_bytes: bytes = field(repr=False)
    response_bytes: bytes = field(repr=False)
    retrieved_at: datetime
    billed_cost_microdollars: int


@dataclass(frozen=True, slots=True)
class JevSuccess:
    decision: JevDecision
    request_bytes: bytes = field(repr=False)
    response_bytes: bytes = field(repr=False)
    retrieved_at: datetime
    billed_cost_microdollars: int


@dataclass(frozen=True, slots=True)
class AdapterFailure:
    provider: Literal["openai", "jev"]
    code: FailureCode
    message: str
    request_bytes: bytes = field(repr=False)
    response_sha256: str | None
    response_byte_length: int
    retrieved_at: datetime | None
    billed_cost_microdollars: int
    status_code: int | None


type OpenAIResult = OpenAISuccess | AdapterFailure
type JevResult = JevSuccess | AdapterFailure


def _normalized_name(name: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", name).casefold()
        if character.isalnum()
    )


def _is_forbidden_name(value: str) -> bool:
    normalized = _normalized_name(value)
    return normalized in _FORBIDDEN_NAMES or normalized.startswith(
        (
            "ask",
            "bid",
            "book",
            "impliedprobability",
            "marketprice",
            "midpoint",
            "outcomeprices",
            "price",
            "token",
            "yesprice",
        )
    )


def _embedded_json_values(text: str, *, label: str) -> list[object]:
    if len(text.encode("utf-8")) > _MAX_PROSE_SCAN_BYTES:
        raise ValueError(f"{label} prose exceeds embedded JSON scan limit")
    decoder = json.JSONDecoder(object_pairs_hook=lambda pairs: dict(pairs))
    decoded_values: list[object] = []
    candidates = 0
    for index, character in enumerate(text):
        if character not in "{[":
            continue
        candidates += 1
        if candidates > _MAX_JSON_CANDIDATES:
            raise ValueError(f"{label} exceeds embedded JSON candidate scan limit")
        try:
            decoded, _ = decoder.raw_decode(text, index)
        except (TypeError, ValueError):
            continue
        decoded_values.append(decoded)
    return decoded_values


def _reject_forbidden_names(value: object, *, label: str) -> None:
    """Scan structured names and bounded JSON embedded anywhere in prose."""

    if isinstance(value, Mapping):
        forbidden = {str(key) for key in value if _is_forbidden_name(str(key))}
        if forbidden:
            raise ValueError(f"{label} contains forbidden fields: {sorted(forbidden)}")
        for nested in value.values():
            _reject_forbidden_names(nested, label=label)
    elif isinstance(value, list | tuple):
        for nested in value:
            _reject_forbidden_names(nested, label=label)
    elif isinstance(value, str):
        for decoded in _embedded_json_values(value, label=label):
            _reject_forbidden_names(decoded, label=label)


def _reject_delimiters(value: object) -> None:
    if isinstance(value, Mapping):
        for nested in value.values():
            _reject_delimiters(nested)
    elif isinstance(value, list | tuple):
        for nested in value:
            _reject_delimiters(nested)
    elif isinstance(value, str) and any(token in value for token in _OPENAI_DELIMITERS):
        raise ValueError("OpenAI evidence contains a reserved delimiter")


def _semantic_bytes(value: str) -> bytes:
    return unicodedata.normalize("NFKC", value).encode("utf-8")


def _contains_credentials(value: object, sensitive_values: tuple[bytes, ...]) -> bool:
    normalized_sentinels: list[bytes] = []
    for sentinel in sensitive_values:
        try:
            normalized_sentinels.append(_semantic_bytes(sentinel.decode("utf-8")))
        except UnicodeDecodeError:
            continue

    def contains(nested: object) -> bool:
        if isinstance(nested, Mapping):
            return any(contains(item) for item in nested.values())
        if isinstance(nested, list | tuple):
            return any(contains(item) for item in nested)
        if isinstance(nested, str):
            semantic = _semantic_bytes(nested)
            if bool(_SECRET_PATTERN.search(semantic)) or any(
                sentinel in semantic for sentinel in normalized_sentinels
            ):
                return True
            return any(
                contains(decoded)
                for decoded in _embedded_json_values(nested, label="response")
            )
        return False

    return contains(value)


def _require_model(model: object, approved: str, provider: str) -> None:
    if type(model) is not str or model != approved:
        raise ValueError(f"{provider} model must be the approved pinned ID {approved}")


def _strict_research_schema() -> dict[str, object]:
    schema = ResearchBrief.model_json_schema()
    _reject_forbidden_names(schema, label="OpenAI response schema")
    return schema


def build_openai_request(evidence: EvidencePacket, model: str) -> bytes:
    """Build one canonical, tool-free Responses API request from evidence only."""

    if not isinstance(evidence, EvidencePacket):
        raise TypeError("OpenAI request requires an EvidencePacket")
    _require_model(model, OPENAI_MODEL_ID, "OpenAI")
    evidence_value = evidence.model_dump(mode="json")
    _reject_forbidden_names(evidence_value, label="OpenAI evidence")
    _reject_delimiters(evidence_value)
    evidence_input = (
        _OPENAI_DELIMITERS[0]
        + canonical_json_bytes(evidence_value).decode()
        + _OPENAI_DELIMITERS[1]
    )
    request = {
        "input": evidence_input,
        "model": model,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "strict-research-brief-v1",
                "schema": _strict_research_schema(),
                "strict": True,
            }
        },
    }
    _reject_forbidden_names(request, label="OpenAI request")
    return canonical_json_bytes(request)


def build_jev_request(
    brief: ResearchBrief, evidence: EvidencePacket, model: str
) -> bytes:
    """Build canonical System One state and independently answerable questions."""

    if not isinstance(brief, ResearchBrief):
        raise TypeError("Jev request requires a ResearchBrief")
    if not isinstance(evidence, EvidencePacket):
        raise TypeError("Jev request requires an EvidencePacket, not a BookSnapshot")
    _require_model(model, JEV_MODEL_ID, "Jev")
    if brief.evidence_sha256 != evidence.source_sha256:
        raise ValueError("research brief must bind the supplied evidence SHA-256")
    brief_value = brief.model_dump(mode="json")
    request = {
        "model": model,
        "questions": _JEV_QUESTIONS,
        "state": {
            "brief": brief_value,
            "evidence_sha256": evidence.source_sha256,
            "research_brief_sha256": hashlib.sha256(
                canonical_json_bytes(brief_value)
            ).hexdigest(),
        },
    }
    _reject_forbidden_names(request, label="Jev request")
    return canonical_json_bytes(request)


def _failure(
    *,
    provider: Literal["openai", "jev"],
    code: FailureCode,
    message: str,
    request_bytes: bytes,
    response_body: bytes | None = None,
    retrieved_at: datetime | None = None,
    billed_cost_microdollars: int = 0,
    status_code: int | None = None,
) -> AdapterFailure:
    """Return safe metadata without retaining failed bodies or headers."""

    return AdapterFailure(
        provider=provider,
        code=code,
        message=message,
        request_bytes=request_bytes,
        response_sha256=(
            hashlib.sha256(response_body).hexdigest()
            if response_body is not None
            else None
        ),
        response_byte_length=len(response_body) if response_body is not None else 0,
        retrieved_at=retrieved_at,
        billed_cost_microdollars=billed_cost_microdollars,
        status_code=status_code,
    )


def _send_once(
    provider: Literal["openai", "jev"],
    request_bytes: bytes,
    transport: Callable[[bytes], TransportResponse],
) -> TransportResponse | AdapterFailure:
    try:
        response = transport(request_bytes)
        status_code = response.status_code
        headers = response.headers
        raw_bytes = response.raw_bytes
        retrieved_at = response.retrieved_at
        billed_cost = response.billed_cost_microdollars
        sensitive_values = response.sensitive_values
        valid_retrieval = type(retrieved_at) is datetime and retrieved_at.tzinfo is UTC
        valid_headers = isinstance(headers, Mapping) and all(
            type(key) is str and type(value) is str for key, value in headers.items()
        )
        valid_sensitive = (
            type(sensitive_values) is tuple
            and len(sensitive_values) <= _MAX_SENSITIVE_VALUES
            and all(
                type(value) is bytes and 0 < len(value) <= _MAX_SENSITIVE_VALUE_BYTES
                for value in sensitive_values
            )
        )
        if (
            type(status_code) is not int
            or not (100 <= status_code <= 599)
            or type(raw_bytes) is not bytes
            or type(billed_cost) is not int
            or not (0 <= billed_cost <= INT64_MAX)
            or not valid_retrieval
            or not valid_headers
            or not valid_sensitive
        ):
            raise ValueError("invalid transport metadata")
    except TimeoutError:
        return _failure(
            provider=provider,
            code="TIMEOUT",
            message=f"{provider} transport timed out",
            request_bytes=request_bytes,
        )
    except Exception:
        return _failure(
            provider=provider,
            code="PROVIDER_ERROR",
            message=f"{provider} transport returned invalid data",
            request_bytes=request_bytes,
        )
    if _SECRET_PATTERN.search(raw_bytes) or any(
        sentinel in raw_bytes for sentinel in sensitive_values
    ):
        return _failure(
            provider=provider,
            code="CREDENTIAL_REFLECTION",
            message=f"{provider} response contained credential-like data",
            request_bytes=request_bytes,
            response_body=raw_bytes,
            retrieved_at=retrieved_at,
            billed_cost_microdollars=billed_cost,
            status_code=status_code,
        )
    if status_code < 200 or status_code >= 300:
        return _failure(
            provider=provider,
            code="HTTP_ERROR",
            message=f"{provider} returned HTTP status {status_code}",
            request_bytes=request_bytes,
            response_body=raw_bytes,
            retrieved_at=retrieved_at,
            billed_cost_microdollars=billed_cost,
            status_code=status_code,
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
        return _failure(
            provider=provider,
            code="PARSE_ERROR",
            message=f"{provider} response is not strict JSON",
            request_bytes=request_bytes,
            response_body=response.raw_bytes,
            retrieved_at=response.retrieved_at,
            billed_cost_microdollars=response.billed_cost_microdollars,
            status_code=response.status_code,
        )
    if not isinstance(decoded, Mapping):
        return _failure(
            provider=provider,
            code="PARSE_ERROR",
            message=f"{provider} response must be a JSON object",
            request_bytes=request_bytes,
            response_body=response.raw_bytes,
            retrieved_at=response.retrieved_at,
            billed_cost_microdollars=response.billed_cost_microdollars,
            status_code=response.status_code,
        )
    try:
        reflected = _contains_credentials(decoded, response.sensitive_values)
    except (TypeError, ValueError):
        reflected = True
    if reflected:
        return _failure(
            provider=provider,
            code="CREDENTIAL_REFLECTION",
            message=f"{provider} response contained credential-like data",
            request_bytes=request_bytes,
            response_body=response.raw_bytes,
            retrieved_at=response.retrieved_at,
            billed_cost_microdollars=response.billed_cost_microdollars,
            status_code=response.status_code,
        )
    return decoded


def _parse_failure(
    provider: Literal["openai", "jev"],
    request_bytes: bytes,
    response: TransportResponse,
    message: str,
) -> AdapterFailure:
    return _failure(
        provider=provider,
        code="PARSE_ERROR",
        message=message,
        request_bytes=request_bytes,
        response_body=response.raw_bytes,
        retrieved_at=response.retrieved_at,
        billed_cost_microdollars=response.billed_cost_microdollars,
        status_code=response.status_code,
    )


def call_openai(
    evidence: EvidencePacket,
    model: str,
    transport: Callable[[bytes], TransportResponse],
) -> OpenAIResult:
    """Perform exactly one OpenAI call and return a typed success or failure."""

    try:
        request_bytes = build_openai_request(evidence, model)
    except Exception:
        return _failure(
            provider="openai",
            code="INPUT_ERROR",
            message="openai request input is invalid",
            request_bytes=b"",
        )
    sent = _send_once("openai", request_bytes, transport)
    if isinstance(sent, AdapterFailure):
        return sent
    decoded = _decoded_response("openai", request_bytes, sent)
    if isinstance(decoded, AdapterFailure):
        return decoded
    if decoded.get("model") != OPENAI_MODEL_ID:
        return _failure(
            provider="openai",
            code="MODEL_MISMATCH",
            message="openai response model does not match the approved snapshot",
            request_bytes=request_bytes,
            response_body=sent.raw_bytes,
            retrieved_at=sent.retrieved_at,
            billed_cost_microdollars=sent.billed_cost_microdollars,
            status_code=sent.status_code,
        )
    try:
        envelope = OpenAIResponse.model_validate(decoded)
        raw_brief = load_strict_json(envelope.message().content[0].text)
    except (ValidationError, TypeError, ValueError):
        return _parse_failure(
            "openai",
            request_bytes,
            sent,
            "openai response violates the strict envelope",
        )
    if isinstance(raw_brief, Mapping):
        citations = raw_brief.get("cited_facts")
        citation_hashes = (
            [
                citation.get("evidence_sha256")
                for citation in citations
                if isinstance(citation, Mapping)
            ]
            if isinstance(citations, list)
            else []
        )
        if raw_brief.get("evidence_sha256") != evidence.source_sha256 or any(
            value != evidence.source_sha256 for value in citation_hashes
        ):
            return _failure(
                provider="openai",
                code="CITATION_ERROR",
                message="openai brief does not cite the supplied evidence",
                request_bytes=request_bytes,
                response_body=sent.raw_bytes,
                retrieved_at=sent.retrieved_at,
                billed_cost_microdollars=sent.billed_cost_microdollars,
                status_code=sent.status_code,
            )
    try:
        brief = openai_response_brief(envelope)
    except (ValidationError, TypeError, ValueError):
        return _parse_failure(
            "openai",
            request_bytes,
            sent,
            "openai output_text violates the research schema",
        )
    return OpenAISuccess(
        brief=brief,
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

    try:
        request_bytes = build_jev_request(brief, evidence, model)
    except Exception:
        return _failure(
            provider="jev",
            code="INPUT_ERROR",
            message="jev request input is invalid",
            request_bytes=b"",
        )
    sent = _send_once("jev", request_bytes, transport)
    if isinstance(sent, AdapterFailure):
        return sent
    decoded = _decoded_response("jev", request_bytes, sent)
    if isinstance(decoded, AdapterFailure):
        return decoded
    if decoded.get("model") != JEV_MODEL_ID:
        return _failure(
            provider="jev",
            code="MODEL_MISMATCH",
            message="jev response model does not match the approved snapshot",
            request_bytes=request_bytes,
            response_body=sent.raw_bytes,
            retrieved_at=sent.retrieved_at,
            billed_cost_microdollars=sent.billed_cost_microdollars,
            status_code=sent.status_code,
        )
    try:
        envelope = JevResponse.model_validate(decoded)
        decision = jev_response_decision(envelope)
    except (ValidationError, TypeError, ValueError):
        return _parse_failure(
            "jev", request_bytes, sent, "jev response violates the strict envelope"
        )
    return JevSuccess(
        decision=decision,
        request_bytes=request_bytes,
        response_bytes=sent.raw_bytes,
        retrieved_at=sent.retrieved_at,
        billed_cost_microdollars=sent.billed_cost_microdollars,
    )

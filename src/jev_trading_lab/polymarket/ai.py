"""Bounded, price-blind model adapters with injectable transports."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import ValidationError

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.polymarket.client import (
    is_safe_global_ip,
    read_bounded_body,
    remaining_seconds,
    resolve_public_addresses,
)
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
    "INPUT_CHANGED",
    "CONFIG_FAILURE",
    "UNKNOWN_IN_FLIGHT",
    "PROVIDER_ERROR",
    "TIMEOUT",
    "HTTP_ERROR",
    "PARSE_ERROR",
    "MODEL_MISMATCH",
    "CITATION_ERROR",
    "CREDENTIAL_REFLECTION",
]


def priced_cost_microdollars(
    provider: Literal["openai", "jev"],
    *,
    input_tokens: int,
    cached_input_tokens: int | None,
    output_tokens: int,
    input_rate: int,
    cached_input_rate: int | None,
    output_rate: int,
) -> int:
    """Price exact usage at exact per-million-token rates, rounding up."""

    if provider == "jev":
        return _ceil_cost([(input_tokens, input_rate), (output_tokens, output_rate)])
    assert cached_input_tokens is not None and cached_input_rate is not None
    return _ceil_cost(
        [
            (input_tokens - cached_input_tokens, input_rate),
            (cached_input_tokens, cached_input_rate),
            (output_tokens, output_rate),
        ]
    )


def is_cost_overflow_consistent(
    provider: Literal["openai", "jev"],
    *,
    input_tokens: int,
    cached_input_tokens: int | None,
    output_tokens: int,
    input_rate: int,
    cached_input_rate: int | None,
    output_rate: int,
) -> bool:
    """Whether exact usage and rates are well-formed and truly exceed signed int64."""

    def exact(value: object, maximum: int) -> bool:
        return type(value) is int and 0 <= value <= maximum

    if (
        not exact(input_tokens, MAX_USAGE_TOKENS)
        or not exact(output_tokens, MAX_USAGE_TOKENS)
        or not exact(input_rate, _RATE_MAX)
        or not exact(output_rate, _RATE_MAX)
    ):
        return False
    if provider == "jev":
        if cached_input_tokens is not None or cached_input_rate is not None:
            return False
    elif provider == "openai":
        if (
            cached_input_tokens is None
            or cached_input_rate is None
            or not exact(cached_input_tokens, MAX_USAGE_TOKENS)
            or not exact(cached_input_rate, _RATE_MAX)
            or cached_input_tokens > input_tokens
        ):
            return False
    else:
        return False
    return (
        priced_cost_microdollars(
            provider,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            input_rate=input_rate,
            cached_input_rate=cached_input_rate,
            output_rate=output_rate,
        )
        > INT64_MAX
    )


@dataclass(frozen=True, slots=True)
class CostOverflowEvidence:
    """Exact usage and configured rates whose priced total exceeds signed int64."""

    provider: Literal["openai", "jev"]
    input_tokens: int
    cached_input_tokens: int | None
    output_tokens: int
    input_rate: int
    cached_input_rate: int | None
    output_rate: int

    def exact_cost_microdollars(self) -> int:
        return priced_cost_microdollars(
            self.provider,
            input_tokens=self.input_tokens,
            cached_input_tokens=self.cached_input_tokens,
            output_tokens=self.output_tokens,
            input_rate=self.input_rate,
            cached_input_rate=self.cached_input_rate,
            output_rate=self.output_rate,
        )

    def is_consistent(self) -> bool:
        return is_cost_overflow_consistent(
            self.provider,
            input_tokens=self.input_tokens,
            cached_input_tokens=self.cached_input_tokens,
            output_tokens=self.output_tokens,
            input_rate=self.input_rate,
            cached_input_rate=self.cached_input_rate,
            output_rate=self.output_rate,
        )


@dataclass(frozen=True, slots=True)
class TransportResponse:
    """One provider reply; callers discard headers and sensitive sentinels.

    A billed cost is either an exact signed int64 or ``None`` with typed overflow
    evidence when the priced usage cannot be represented as int64 microdollars.
    """

    status_code: int
    headers: Mapping[str, str] = field(repr=False)
    raw_bytes: bytes = field(repr=False)
    retrieved_at: datetime
    billed_cost_microdollars: int | None
    cost_status: Literal["KNOWN", "UNKNOWN"] = "KNOWN"
    sensitive_values: tuple[bytes, ...] = field(default=(), repr=False)
    cost_overflow: CostOverflowEvidence | None = None


class Transport(Protocol):
    """Single-attempt byte transport implemented by fixtures now and HTTP later."""

    def __call__(self, request_bytes: bytes) -> TransportResponse: ...


@dataclass(frozen=True, slots=True)
class LiveHttpRequest:
    """One closed-world provider request; headers are never persisted."""

    method: str
    url: str
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class LiveHttpResponse:
    status_code: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    retrieved_at: datetime
    oversized: bool = False


LiveNetwork = Callable[[LiveHttpRequest], LiveHttpResponse]
OPENAI_ENDPOINT: Final = "https://api.openai.com/v1/responses"
JEV_ENDPOINT: Final = "https://api.typesafe.ai/v1/systemone"
_MAX_MODEL_RESPONSE_BYTES = 2 * 1024 * 1024
_RATE_MAX = 1_000_000_000_000
MAX_USAGE_TOKENS: Final = 10**4300 - 1


def _public_addresses(host: str, *, deadline: float) -> list[str]:
    try:
        return resolve_public_addresses(socket.getaddrinfo, host, deadline=deadline)
    except ValueError as error:
        raise ValueError("provider DNS answer is unsafe") from error


class _ProviderHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self, host: str, address: str, timeout: float, deadline: float
    ) -> None:
        self._tls_context = ssl.create_default_context()
        super().__init__(host, port=443, timeout=timeout, context=self._tls_context)
        self._address = address
        self._deadline = deadline

    def connect(self) -> None:
        raw = socket.create_connection(
            (self._address, 443), remaining_seconds(self._deadline)
        )
        try:
            peer = raw.getpeername()[0]
            if peer != self._address or not is_safe_global_ip(peer):
                raise ValueError("provider peer differs from validated address")
            raw.settimeout(remaining_seconds(self._deadline))
            self.sock = self._tls_context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def _provider_network(
    request: LiveHttpRequest, *, timeout_seconds: float = 30.0
) -> LiveHttpResponse:
    parsed = urlsplit(request.url)
    allowed = {
        ("api.openai.com", "/v1/responses"),
        ("api.typesafe.ai", "/v1/systemone"),
    }
    if (
        request.method != "POST"
        or parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or (parsed.hostname, parsed.path) not in allowed
    ):
        raise ValueError("provider route is outside the exact allowlist")
    host = parsed.hostname
    assert host is not None
    deadline = time.monotonic() + timeout_seconds
    connection = _ProviderHTTPSConnection(
        host,
        _public_addresses(host, deadline=deadline)[0],
        remaining_seconds(deadline),
        deadline,
    )
    try:
        connection.request(
            "POST", parsed.path, body=request.body, headers=dict(request.headers)
        )
        assert connection.sock is not None
        connection.sock.settimeout(remaining_seconds(deadline))
        response = connection.getresponse()
        body = read_bounded_body(
            response,
            max_bytes=_MAX_MODEL_RESPONSE_BYTES,
            deadline=deadline,
            set_timeout=connection.sock.settimeout,
        )
        oversized = len(body) > _MAX_MODEL_RESPONSE_BYTES
        return LiveHttpResponse(
            status_code=response.status,
            headers={key.lower(): value for key, value in response.getheaders()},
            body=body[:_MAX_MODEL_RESPONSE_BYTES],
            retrieved_at=datetime.now(UTC),
            oversized=oversized,
        )
    except TimeoutError:
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException) as error:
        raise ValueError("provider HTTPS request failed") from error
    finally:
        connection.close()


def _env_secret(name: str) -> str:
    value = os.environ.get(name, "")
    if not value or len(value.encode()) > _MAX_SENSITIVE_VALUE_BYTES:
        raise ValueError("provider configuration is incomplete")
    return value


def _env_rate(name: str) -> int:
    value = os.environ.get(name, "")
    if not value.isascii() or not value.isdecimal():
        raise ValueError("provider configuration is incomplete")
    rate = int(value)
    if not 0 < rate <= _RATE_MAX:
        raise ValueError("provider configuration is incomplete")
    return rate


def _ceil_cost(terms: list[tuple[int, int]]) -> int:
    numerator = sum(tokens * rate for tokens, rate in terms)
    return (numerator + 999_999) // 1_000_000


def _usage_terms(
    body: bytes, provider: Literal["openai", "jev"]
) -> tuple[int, int | None, int] | None:
    """Return exact input, cached-input, and output token counts, if well-formed.

    Any non-negative integer a JSON parser accepts is priced exactly. Usage without
    integer input and output counts cannot be priced and is billed as zero; the strict
    response envelope then rejects the reply as a typed failure.
    """

    try:
        decoded = load_strict_json(body)
        if not isinstance(decoded, Mapping) or not isinstance(
            decoded.get("usage"), Mapping
        ):
            return None
        usage = decoded["usage"]
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if type(input_tokens) is not int or type(output_tokens) is not int:
            return None
        if not (
            0 <= input_tokens <= MAX_USAGE_TOKENS
            and 0 <= output_tokens <= MAX_USAGE_TOKENS
        ):
            return None
        if provider == "jev":
            return input_tokens, None, output_tokens
        details = usage.get("input_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, Mapping) else None
        if type(cached) is not int or not 0 <= cached <= input_tokens:
            # The cached split is unavailable: price every input token at the
            # full input rate so the cost can never be under-reported.
            cached = 0
        return input_tokens, cached, output_tokens
    except (TypeError, ValueError, RecursionError):
        return None


def _usage_cost(
    body: bytes, provider: Literal["openai", "jev"], rates: tuple[int, ...]
) -> tuple[int | None, CostOverflowEvidence | None]:
    """Price authoritative usage; unavailable usage has unknown cost."""

    terms = _usage_terms(body, provider)
    if terms is None:
        return None, None
    input_tokens, cached, output_tokens = terms
    evidence = CostOverflowEvidence(
        provider=provider,
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        output_tokens=output_tokens,
        input_rate=rates[0],
        cached_input_rate=rates[1] if provider == "openai" else None,
        output_rate=rates[-1],
    )
    cost = evidence.exact_cost_microdollars()
    return cost, (evidence if cost > INT64_MAX else None)


def _live_transport(
    *, provider: Literal["openai", "jev"], network: LiveNetwork
) -> Transport:
    endpoint = OPENAI_ENDPOINT if provider == "openai" else JEV_ENDPOINT

    def send(request_bytes: bytes) -> TransportResponse:
        if type(request_bytes) is not bytes or len(request_bytes) > 1024 * 1024:
            raise ValueError("provider request is outside byte bounds")
        rates: tuple[int, ...]
        if provider == "openai":
            secret = _env_secret("OPENAI_API_KEY")
            rates = (
                _env_rate("OPENAI_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS"),
                _env_rate("OPENAI_CACHED_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS"),
                _env_rate("OPENAI_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS"),
            )
        else:
            secret = _env_secret("TYPESAFE_API_KEY")
            rates = (
                _env_rate("TYPESAFE_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS"),
                _env_rate("TYPESAFE_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS"),
            )
        response = network(
            LiveHttpRequest(
                method="POST",
                url=endpoint,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {secret}",
                    "Content-Type": "application/json",
                },
                body=request_bytes,
            )
        )
        if response.oversized or len(response.body) > _MAX_MODEL_RESPONSE_BYTES:
            raise ValueError("provider response exceeds byte limit")
        if 300 <= response.status_code < 400:
            raise ValueError("provider redirect is forbidden")
        content_type = (
            response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        )
        if content_type != "application/json":
            raise ValueError("provider response content type is invalid")
        cost, overflow = _usage_cost(response.body, provider, rates)
        return TransportResponse(
            status_code=response.status_code,
            headers=response.headers,
            raw_bytes=response.body,
            retrieved_at=response.retrieved_at,
            billed_cost_microdollars=None if overflow is not None else cost,
            cost_status=("UNKNOWN" if cost is None and overflow is None else "KNOWN"),
            sensitive_values=(secret.encode(),),
            cost_overflow=overflow,
        )

    return send


def live_openai_transport(*, network: LiveNetwork = _provider_network) -> Transport:
    """Create the one-attempt Responses API transport; environment is read on send."""

    return _live_transport(provider="openai", network=network)


def live_jev_transport(*, network: LiveNetwork = _provider_network) -> Transport:
    """Create one TypeSafe transport; environment is read only on send."""

    return _live_transport(provider="jev", network=network)


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


EMPTY_RESPONSE_SHA256: Final = hashlib.sha256(b"").hexdigest()


def has_canonical_failure_response(
    response_sha256: str | None, response_byte_length: int
) -> bool:
    """Bind a response digest to absent, empty, or nonempty response bytes."""

    has_response = response_sha256 is not None
    empty_response = response_sha256 == EMPTY_RESPONSE_SHA256
    return (
        type(response_byte_length) is int
        and response_byte_length >= 0
        and (has_response or response_byte_length == 0)
        and (empty_response == (has_response and response_byte_length == 0))
    )


@dataclass(frozen=True, slots=True)
class AdapterFailure:
    provider: Literal["openai", "jev"]
    code: FailureCode
    message: str
    request_bytes: bytes = field(repr=False)
    response_sha256: str | None
    response_byte_length: int
    retrieved_at: datetime | None
    billed_cost_microdollars: int | None
    status_code: int | None

    @property
    def cost_status(self) -> Literal["KNOWN", "UNKNOWN"]:
        return "UNKNOWN" if self.billed_cost_microdollars is None else "KNOWN"

    def __post_init__(self) -> None:
        if (
            not has_canonical_failure_response(
                self.response_sha256, self.response_byte_length
            )
            or self.code == "INPUT_ERROR"
            and self.billed_cost_microdollars is None
        ):
            raise ValueError("failure response or cost metadata is inconsistent")


@dataclass(frozen=True, slots=True)
class AdapterCostOverflow:
    """Typed result when billed usage cannot be represented as int64 microdollars.

    Only the response digest, length, status, and exact usage and configured rates
    survive; the response body is never retained.
    """

    provider: Literal["openai", "jev"]
    request_bytes: bytes = field(repr=False)
    response_sha256: str
    response_byte_length: int
    retrieved_at: datetime
    status_code: int
    evidence: CostOverflowEvidence

    def __post_init__(self) -> None:
        if (
            not has_canonical_failure_response(
                self.response_sha256, self.response_byte_length
            )
            or self.evidence.provider != self.provider
            or not self.evidence.is_consistent()
        ):
            raise ValueError("cost overflow metadata is inconsistent")


type OpenAIResult = OpenAISuccess | AdapterFailure | AdapterCostOverflow
type JevResult = JevSuccess | AdapterFailure | AdapterCostOverflow


def is_canonical_failure_message(
    provider: Literal["openai", "jev"],
    code: FailureCode,
    message: str,
    status_code: int | None,
) -> bool:
    """Return whether metadata matches one sanitized adapter diagnostic."""

    fixed: dict[FailureCode, set[str]] = {
        "INPUT_ERROR": {f"{provider} request input is invalid"},
        "INPUT_CHANGED": {f"{provider} frozen input differs from current evidence"},
        "CONFIG_FAILURE": {f"{provider} live configuration is incomplete"},
        "UNKNOWN_IN_FLIGHT": {f"{provider} call outcome is unknown after restart"},
        "PROVIDER_ERROR": {f"{provider} transport returned invalid data"},
        "TIMEOUT": {f"{provider} transport timed out"},
        "HTTP_ERROR": {
            f"{provider} returned HTTP status {status_code}"
            if status_code is not None
            else ""
        },
        "PARSE_ERROR": {
            f"{provider} response is not strict JSON",
            f"{provider} response must be a JSON object",
            f"{provider} response violates the strict envelope",
        },
        "MODEL_MISMATCH": {
            f"{provider} response model does not match the approved snapshot"
        },
        "CITATION_ERROR": {
            "openai brief does not cite the supplied evidence"
            if provider == "openai"
            else ""
        },
        "CREDENTIAL_REFLECTION": {
            f"{provider} response contained credential-like data"
        },
    }
    if provider == "openai" and code == "PARSE_ERROR":
        fixed[code].add("openai output_text violates the research schema")
    return message in fixed[code]


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
    normalized = unicodedata.normalize("NFKC", text)
    if len(normalized.encode("utf-8")) > _MAX_PROSE_SCAN_BYTES:
        raise ValueError(f"{label} prose exceeds embedded JSON scan limit")
    decoder = json.JSONDecoder(object_pairs_hook=lambda pairs: dict(pairs))
    decoded_values: list[object] = []
    candidates = 0
    for index, character in enumerate(normalized):
        if character not in "{[":
            continue
        candidates += 1
        if candidates > _MAX_JSON_CANDIDATES:
            raise ValueError(f"{label} exceeds embedded JSON candidate scan limit")
        try:
            decoded, _ = decoder.raw_decode(normalized, index)
        except (TypeError, ValueError, RecursionError):
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
    elif isinstance(value, str) and any(
        token in unicodedata.normalize("NFKC", value) for token in _OPENAI_DELIMITERS
    ):
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

    def contains(nested: object, *, mapping_key: bool = False) -> bool:
        if isinstance(nested, Mapping):
            return any(
                type(key) is str and contains(key, mapping_key=True) for key in nested
            ) or any(contains(item) for item in nested.values())
        if isinstance(nested, list | tuple):
            return any(contains(item) for item in nested)
        if isinstance(nested, str):
            semantic = _semantic_bytes(nested)
            if (
                bool(_SECRET_PATTERN.search(semantic))
                or (mapping_key and bool(_SECRET_PATTERN.search(semantic + b": safe")))
                or any(sentinel in semantic for sentinel in normalized_sentinels)
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
    billed_cost_microdollars: int | None = 0,
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
) -> TransportResponse | AdapterFailure | AdapterCostOverflow:
    try:
        response = transport(request_bytes)
        status_code = response.status_code
        headers = response.headers
        raw_bytes = response.raw_bytes
        retrieved_at = response.retrieved_at
        billed_cost = response.billed_cost_microdollars
        overflow = response.cost_overflow
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
        valid_cost = (
            (
                type(billed_cost) is int
                and 0 <= billed_cost <= INT64_MAX
                and response.cost_status == "KNOWN"
                and overflow is None
            )
            or (
                billed_cost is None
                and response.cost_status == "KNOWN"
                and type(overflow) is CostOverflowEvidence
                and overflow.provider == provider
                and overflow.is_consistent()
            )
            or (
                billed_cost is None
                and response.cost_status == "UNKNOWN"
                and overflow is None
            )
        )
        if (
            type(status_code) is not int
            or not (100 <= status_code <= 599)
            or type(raw_bytes) is not bytes
            or not valid_cost
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
            billed_cost_microdollars=None,
        )
    except ValueError as error:
        if str(error) == "provider configuration is incomplete":
            return _failure(
                provider=provider,
                code="CONFIG_FAILURE",
                message=f"{provider} live configuration is incomplete",
                request_bytes=request_bytes,
            )
        return _failure(
            provider=provider,
            code="PROVIDER_ERROR",
            message=f"{provider} transport returned invalid data",
            request_bytes=request_bytes,
            billed_cost_microdollars=None,
        )
    except Exception:
        return _failure(
            provider=provider,
            code="PROVIDER_ERROR",
            message=f"{provider} transport returned invalid data",
            request_bytes=request_bytes,
            billed_cost_microdollars=None,
        )
    if overflow is not None:
        return AdapterCostOverflow(
            provider=provider,
            request_bytes=request_bytes,
            response_sha256=hashlib.sha256(raw_bytes).hexdigest(),
            response_byte_length=len(raw_bytes),
            retrieved_at=retrieved_at,
            status_code=status_code,
            evidence=overflow,
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
    if billed_cost is None:
        return _failure(
            provider=provider,
            code="PARSE_ERROR",
            message=f"{provider} response violates the strict envelope",
            request_bytes=request_bytes,
            response_body=raw_bytes,
            retrieved_at=retrieved_at,
            billed_cost_microdollars=None,
            status_code=status_code,
        )
    return response


def _billed(response: TransportResponse) -> int:
    """The exact int64 cost of a response that `_send_once` already accepted."""

    cost = response.billed_cost_microdollars
    assert cost is not None
    return cost


def _decoded_response(
    provider: Literal["openai", "jev"],
    request_bytes: bytes,
    response: TransportResponse,
) -> Mapping[str, object] | AdapterFailure:
    try:
        decoded = load_strict_json(response.raw_bytes)
    except (TypeError, ValueError, RecursionError):
        return _failure(
            provider=provider,
            code="PARSE_ERROR",
            message=f"{provider} response is not strict JSON",
            request_bytes=request_bytes,
            response_body=response.raw_bytes,
            retrieved_at=response.retrieved_at,
            billed_cost_microdollars=_billed(response),
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
            billed_cost_microdollars=_billed(response),
            status_code=response.status_code,
        )
    try:
        reflected = _contains_credentials(decoded, response.sensitive_values)
    except (TypeError, ValueError, RecursionError):
        reflected = True
    if reflected:
        return _failure(
            provider=provider,
            code="CREDENTIAL_REFLECTION",
            message=f"{provider} response contained credential-like data",
            request_bytes=request_bytes,
            response_body=response.raw_bytes,
            retrieved_at=response.retrieved_at,
            billed_cost_microdollars=_billed(response),
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
        billed_cost_microdollars=_billed(response),
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
    if isinstance(sent, AdapterFailure | AdapterCostOverflow):
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
            billed_cost_microdollars=_billed(sent),
            status_code=sent.status_code,
        )
    try:
        envelope = OpenAIResponse.model_validate(decoded)
        if (
            envelope.status != "completed"
            or envelope.usage is None
            or envelope.error is not None
            or envelope.incomplete_details is not None
        ):
            raise ValueError("OpenAI response is not a completed success")
        raw_brief = load_strict_json(envelope.message().content[0].text)
    except (ValidationError, TypeError, ValueError, RecursionError):
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
                billed_cost_microdollars=_billed(sent),
                status_code=sent.status_code,
            )
    try:
        brief = openai_response_brief(envelope)
    except (ValidationError, TypeError, ValueError, RecursionError):
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
        billed_cost_microdollars=_billed(sent),
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
    if isinstance(sent, AdapterFailure | AdapterCostOverflow):
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
            billed_cost_microdollars=_billed(sent),
            status_code=sent.status_code,
        )
    try:
        envelope = JevResponse.model_validate(decoded)
        decision = jev_response_decision(envelope)
    except (ValidationError, TypeError, ValueError, RecursionError):
        return _parse_failure(
            "jev", request_bytes, sent, "jev response violates the strict envelope"
        )
    return JevSuccess(
        decision=decision,
        request_bytes=request_bytes,
        response_bytes=sent.raw_bytes,
        retrieved_at=sent.retrieved_at,
        billed_cost_microdollars=_billed(sent),
    )

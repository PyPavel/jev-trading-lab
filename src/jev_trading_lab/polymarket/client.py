"""Bounded, read-only Polymarket discovery and order-book normalization."""

from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
import unicodedata
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Final, Literal
from urllib.parse import parse_qs, urlencode, urlsplit

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.persistence.blobs import is_credential_name
from jev_trading_lab.polymarket.models import (
    BookLevel,
    BookSnapshot,
    EvidencePacket,
    load_strict_json,
)

GAMMA_ENDPOINT: Final = "https://gamma-api.polymarket.com/markets"
CLOB_ENDPOINT: Final = "https://clob.polymarket.com/book"
CLOB_HOST: Final = "clob.polymarket.com"
CLOB_PATH: Final = "/book"
MAX_PUBLIC_BYTES: Final = 2 * 1024 * 1024
MAX_CLOB_TOKEN_ID_CHARS: Final = 512
MAX_DECIMAL_MAGNITUDE: Final = 40
MAX_BOOK_MILLISECONDS: Final = 253_402_300_799_999  # 9999-12-31T23:59:59.999Z
MAX_JSON_DEPTH: Final = 16
MAX_BOOK_AGE: Final = timedelta(seconds=30)
MAX_PRECHECK_SPREAD_MICROS: Final = 40_000
PRECHECK_DEPTH_WINDOW_MICROS: Final = 20_000
MIN_PRECHECK_NEAR_DEPTH: Final = 5_000
_GAMMA_UTC: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|\+00:00)\Z"
)
_MEDIA_TYPE: Final = re.compile(
    r"[a-z0-9!#$%&'*+.^_`|~-]{1,64}/[a-z0-9!#$%&'*+.^_`|~-]{1,64}\Z"
)
type PrecheckTransportReason = Literal[
    "OK",
    "TIMEOUT",
    "TRANSPORT_ERROR",
    "HTTP_STATUS",
    "REDIRECT",
    "CONTENT_TYPE",
    "OVERSIZED",
    "NOT_JSON",
]
_SPORTS = frozenset(
    {
        "sports",
        "sport",
        "nba",
        "nfl",
        "nhl",
        "mlb",
        "ncaa",
        "cbb",
        "cfb",
        "soccer",
        "football",
        "basketball",
        "baseball",
        "hockey",
        "tennis",
        "esports",
        "ufc",
        "mma",
    }
)
_GAMMA_QUERY_KEYS = frozenset(
    {"active", "ascending", "closed", "limit", "offset", "order"}
)


class PublicDataError(ValueError):
    """A public response or route violated the bounded client contract."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "BOOK_UNAVAILABLE",
        raw_bytes: bytes = b"",
        retrieved_at: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.raw_bytes = raw_bytes
        self.retrieved_at = retrieved_at


class PublicDataTimeout(PublicDataError):
    """A bounded public request timed out before yielding response bytes."""


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    body: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    retrieved_at: datetime
    oversized: bool = False


ByteTransport = Callable[[HttpRequest], HttpResponse]
Resolver = Callable[..., list[tuple[Any, ...]]]
_RESOLVERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bounded-dns")


def remaining_seconds(
    deadline: float, *, clock: Callable[[], float] = time.monotonic
) -> float:
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError("end-to-end deadline exceeded")
    return remaining


def read_bounded_body(
    response: Any,
    *,
    max_bytes: int,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
    set_timeout: Callable[[float], object] | None = None,
) -> bytes:
    """Read through EOF under one monotonic deadline, rejecting slow drip."""

    chunks: list[bytes] = []
    total = 0
    while total <= max_bytes:
        remaining = remaining_seconds(deadline, clock=clock)
        if set_timeout is not None:
            set_timeout(remaining)
        chunk = response.read(min(65536, max_bytes + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def resolve_public_addresses(
    resolver: Resolver,
    host: str,
    *,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> list[str]:
    """Resolve on a bounded shared pool within the caller's deadline."""

    future = _RESOLVERS.submit(resolver, host, 443, 0, socket.SOCK_STREAM)
    try:
        answers = future.result(timeout=remaining_seconds(deadline, clock=clock))
    except FutureTimeout as error:
        future.cancel()
        raise TimeoutError("end-to-end deadline exceeded during DNS") from error
    addresses = sorted({str(answer[4][0]) for answer in answers if len(answer) >= 5})
    if not addresses or any(not is_safe_global_ip(value) for value in addresses):
        raise PublicDataError("DNS returned an unsafe or mixed answer set")
    return addresses


def is_safe_global_ip(value: str) -> bool:
    """Accept only globally routed addresses, including their mapped IPv4 value."""

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return (
        address.is_global
        and not address.is_multicast
        and (mapped is None or (mapped.is_global and not mapped.is_multicast))
    )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, *, timeout: float, deadline: float) -> None:
        self._tls_context = ssl.create_default_context()
        super().__init__(host, port=443, timeout=timeout, context=self._tls_context)
        self._ip = ip
        self._deadline = deadline

    def connect(self) -> None:
        raw = socket.create_connection(
            (self._ip, 443), remaining_seconds(self._deadline)
        )
        try:
            peer = raw.getpeername()[0]
            if peer != self._ip or not is_safe_global_ip(peer):
                raise PublicDataError("HTTPS peer differs from the validated address")
            raw.settimeout(remaining_seconds(self._deadline))
            self.sock = self._tls_context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


class SecureHttpTransport:
    """One-attempt HTTPS transport with exact routes, DNS vetting, and no redirects."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        max_bytes: int = MAX_PUBLIC_BYTES,
        resolver: Resolver = socket.getaddrinfo,
    ) -> None:
        if timeout_seconds <= 0 or max_bytes <= 0:
            raise ValueError("transport bounds must be positive")
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.resolver = resolver

    def validate(
        self, request: HttpRequest, *, deadline: float | None = None
    ) -> tuple[str, str, str]:
        if request.method != "GET" or request.body is not None:
            raise PublicDataError("Polymarket public transport permits GET only")
        parsed = urlsplit(request.url)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise PublicDataError("public endpoint must use HTTPS without userinfo")
        if parsed.port not in {None, 443}:
            raise PublicDataError("public endpoint must use port 443")
        host = (parsed.hostname or "").lower()
        route = (host, parsed.path)
        if route == ("gamma-api.polymarket.com", "/markets"):
            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
            if set(query) - _GAMMA_QUERY_KEYS or any(
                len(values) != 1 for values in query.values()
            ):
                raise PublicDataError("Gamma query is outside the bounded allowlist")
        elif route == ("clob.polymarket.com", "/book"):
            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
            if (
                set(query) != {"token_id"}
                or len(query["token_id"]) != 1
                or not query["token_id"][0]
            ):
                raise PublicDataError("CLOB query must contain one token_id")
        else:
            raise PublicDataError("public endpoint is outside the exact allowlist")
        if deadline is None:
            deadline = time.monotonic() + self.timeout_seconds
        addresses = resolve_public_addresses(self.resolver, host, deadline=deadline)
        return (
            host,
            parsed.path + (f"?{parsed.query}" if parsed.query else ""),
            addresses[0],
        )

    def __call__(self, request: HttpRequest) -> HttpResponse:
        deadline = time.monotonic() + self.timeout_seconds
        host, target, address = self.validate(request, deadline=deadline)
        connection = _PinnedHTTPSConnection(
            host,
            address,
            timeout=remaining_seconds(deadline),
            deadline=deadline,
        )
        try:
            connection.request(
                "GET", target, headers={"Accept": "application/json", "Host": host}
            )
            assert connection.sock is not None
            connection.sock.settimeout(remaining_seconds(deadline))
            response = connection.getresponse()
            body = read_bounded_body(
                response,
                max_bytes=self.max_bytes,
                deadline=deadline,
                set_timeout=connection.sock.settimeout,
            )
            oversized = len(body) > self.max_bytes
            if oversized:
                body = body[: self.max_bytes]
            return HttpResponse(
                status_code=response.status,
                headers={key.lower(): value for key, value in response.getheaders()},
                body=body,
                retrieved_at=datetime.now(UTC),
                oversized=oversized,
            )
        except TimeoutError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            raise PublicDataError("bounded HTTPS request failed") from error
        finally:
            connection.close()


@dataclass(frozen=True, slots=True)
class PrecheckAttempt:
    """One bounded CLOB precheck outcome, including failures before any response."""

    token_id: str
    attempted_at: datetime
    status_code: int | None
    content_type: str | None
    retrieved_at: datetime | None
    body: bytes = field(repr=False)
    truncated: bool
    transport_reason: PrecheckTransportReason


@dataclass(frozen=True, slots=True)
class MarketRejection:
    market_id: str
    reason: str
    raw_gamma_bytes: bytes = field(repr=False)
    raw_gamma_sha256: str
    evidence: EvidencePacket | None = None
    yes_token_id: str | None = None
    precheck_book_bytes: bytes | None = field(default=None, repr=False)
    precheck_book: BookSnapshot | None = None
    precheck_failure_sha256: str | None = None
    precheck_retrieved_at: datetime | None = None
    precheck_byte_length: int | None = None
    precheck_request_token_id: str | None = None
    precheck_request_market_id: str | None = None
    precheck_request_condition_id: str | None = None
    precheck_transport_failure: bool = False
    precheck_attempt: PrecheckAttempt | None = None


@dataclass(frozen=True, slots=True)
class MarketCandidate:
    market_id: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    slot_at: datetime
    raw_gamma_bytes: bytes = field(repr=False)
    raw_gamma_sha256: str
    evidence: EvidencePacket
    precheck_book_bytes: bytes = field(repr=False)
    precheck_book: BookSnapshot
    precheck_attempt: PrecheckAttempt
    precheck_history_bytes: tuple[bytes, bytes] = field(repr=False)
    precheck_history: tuple[BookSnapshot, BookSnapshot]
    precheck_attempts: tuple[PrecheckAttempt, PrecheckAttempt]

    def __post_init__(self) -> None:
        if (
            self.slot_at.tzinfo is not UTC
            or self.slot_at.second
            or self.slot_at.microsecond
        ):
            raise ValueError("candidate slot must be an exact UTC minute")


def _gamma_semantic_identity(
    item: Mapping[str, object],
) -> tuple[tuple[str, str, str, str], bytes]:
    """Return stable market identity plus canonical normalized candidate semantics."""

    market_identity = gamma_clob_identity(item)
    evidence = normalize_gamma_identity(item)
    semantics = canonical_json_bytes(
        {
            "condition_id": market_identity[1],
            "evidence": evidence,
            "market_id": market_identity[0],
            "no_token_id": market_identity[3],
            "yes_token_id": market_identity[2],
        }
    )
    return market_identity, semantics


@dataclass(frozen=True, slots=True)
class PollResult:
    eligible: tuple[MarketCandidate, ...]
    rejections: tuple[MarketRejection, ...]


def _json_depth(value: object, depth: int = 0) -> int:
    if depth > MAX_JSON_DEPTH:
        raise PublicDataError("public JSON exceeds depth limit")
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if type(key) is not str:
                raise PublicDataError("public JSON object key is not text")
            _json_depth(nested, depth + 1)
    elif isinstance(value, list):
        for nested in value:
            _json_depth(nested, depth + 1)
    return depth


def _strict_response(response: HttpResponse, *, label: str) -> object:
    if response.oversized or len(response.body) > MAX_PUBLIC_BYTES:
        raise PublicDataError(f"{label} response exceeds byte limit")
    if response.status_code < 200 or response.status_code >= 300:
        if 300 <= response.status_code < 400:
            raise PublicDataError(f"{label} redirect is forbidden")
        raise PublicDataError(f"{label} returned non-success status")
    content_type = (
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    if content_type != "application/json":
        raise PublicDataError(f"{label} response is not application/json")
    try:
        value = load_strict_json(response.body)
    except (TypeError, ValueError, RecursionError) as error:
        raise PublicDataError(f"{label} response is not strict JSON") from error
    _json_depth(value)
    return value


def _market_raw_items(raw: bytes) -> list[bytes]:
    """Extract exact top-level array item bytes without reserializing provider data."""

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PublicDataError("Gamma response is not UTF-8") from error
    decoder = json.JSONDecoder(object_pairs_hook=lambda pairs: dict(pairs))
    index = 0
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text) or text[index] != "[":
        raise PublicDataError("Gamma response must be an array")
    index += 1
    items: list[bytes] = []
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index < len(text) and text[index] == "]":
            return items
        start = index
        try:
            _value, end = decoder.raw_decode(text, index)
        except ValueError as error:
            raise PublicDataError("Gamma response item is malformed") from error
        items.append(text[start:end].encode("utf-8"))
        index = end
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text) or text[index] not in ",]":
            raise PublicDataError("Gamma array separator is malformed")
        if text[index] == "]":
            return items
        index += 1


def _text(value: object) -> str:
    return value.strip() if type(value) is str else ""


def _utc(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("timestamp must be text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC")
    return parsed.astimezone(UTC)


def normalize_gamma_utc(value: object) -> datetime:
    """Parse the only accepted Gamma UTC spellings: ``...Z`` or ``...+00:00``."""

    if type(value) is not str or _GAMMA_UTC.fullmatch(value) is None:
        raise ValueError("Gamma timestamp must be ISO-8601 UTC ending in Z or +00:00")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(text).astimezone(UTC)


def canonical_utc_text(value: datetime) -> str:
    """Render a UTC instant exactly as the strict models serialize it."""

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def normalize_content_type(value: object) -> str | None:
    """Return a bounded lowercase media type, ``None`` when absent, or ``invalid``."""

    if type(value) is not str:
        return None
    media_type = value.split(";", 1)[0].strip().lower()
    if not media_type:
        return None
    return media_type if _MEDIA_TYPE.fullmatch(media_type) else "invalid"


def _encoded_array(value: object, *, label: str) -> list[object]:
    if type(value) is str:
        try:
            value = load_strict_json(value)
        except RecursionError as error:
            raise ValueError(f"{label} nesting is too deep") from error
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{label} must contain exactly two values")
    return value


def _decimal(value: object, *, label: str) -> Decimal:
    if type(value) not in {str, int, float} or type(value) is bool:
        raise ValueError(f"{label} must be numeric")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{label} must be decimal") from error
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    if not -MAX_DECIMAL_MAGNITUDE <= result.adjusted() <= MAX_DECIMAL_MAGNITUDE:
        raise ValueError(f"{label} magnitude is out of bounds")
    return result


def _tags(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    tags: list[str] = []
    for item in value:
        if type(item) is str:
            if item.strip():
                tags.append(item.strip())
            continue
        if isinstance(item, Mapping):
            label = next(
                (
                    text
                    for key in ("label", "name", "slug", "id")
                    if (text := _text(item.get(key)))
                ),
                "",
            )
            if not label:
                raise ValueError("Gamma tag object has no recognized identity")
            tags.append(label)
            continue
        raise ValueError("Gamma tag has unexpected shape")
    return tags


def normalize_gamma_identity(value: Mapping[str, object]) -> dict[str, object]:
    """Normalize the exact Gamma fields that bind sanitized evidence."""

    return {
        "market_id": _text(value.get("id")),
        "question": _text(value.get("question")),
        "description": _text(value.get("description")),
        "rules": _text(value.get("rules")),
        "category": _text(value.get("category")) or "Uncategorized",
        "tags": _tags(value.get("tags")),
        "resolution_source_text": _text(value.get("resolutionSource")),
        "closes_at": canonical_utc_text(normalize_gamma_utc(value.get("endDate"))),
    }


_PUBLIC_CREDENTIAL_PATTERN = re.compile(
    r"(?i)(?:sk-[a-z0-9_-]{8,}|bearer\s+\S{4,}|basic\s+\S{4,}|"
    r"(?:[\w.-]{0,128})(?:authorization|proxy[-_\s]?authorization|cookie|"
    r"set[-_\s]?cookie|x[-_\s]?api[-_\s]?key|api[-_\s]?key|"
    r"access[-_\s]?token|refresh[-_\s]?token|client[-_\s]?secret|"
    r"session[-_\s]?token)"
    r"(?:[-_.\s]?v?\d+)*\s*[:=]\s*\S{4,})"
)
_PUBLIC_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<name>[\w.-]{1,256})\s*[:=]\s*(?P<value>\S{4,})"
)
_PUBLIC_IS_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?P<name>[\w.-]+(?:\s+[\w.-]+){0,4})\s+is\s+(?P<value>\S{4,})"
)
_PUBLIC_IS_PREDICATE_WORDS = frozenset(
    {
        "available",
        "configured",
        "documented",
        "needed",
        "optional",
        "required",
        "stored",
        "unavailable",
        "used",
    }
)


def _sanitize_public_prose(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    sanitized = _PUBLIC_CREDENTIAL_PATTERN.sub("[REDACTED]", normalized)
    sanitized = _PUBLIC_IS_ASSIGNMENT_PATTERN.sub(
        lambda match: (
            "[REDACTED]"
            if is_credential_name(match.group("name"))
            and match.group("value").casefold().rstrip(".,;:!?")
            not in _PUBLIC_IS_PREDICATE_WORDS
            else match.group(0)
        ),
        sanitized,
    )
    return _PUBLIC_ASSIGNMENT_PATTERN.sub(
        lambda match: (
            "[REDACTED]" if is_credential_name(match.group("name")) else match.group(0)
        ),
        sanitized,
    )


def sanitized_gamma_identity(value: Mapping[str, object]) -> dict[str, object]:
    """Redact credential-like public prose before it reaches provider artifacts."""

    identity = normalize_gamma_identity(value)
    for name in (
        "question",
        "description",
        "rules",
        "category",
        "resolution_source_text",
    ):
        identity[name] = _sanitize_public_prose(str(identity[name]))
    tags = identity["tags"]
    if not isinstance(tags, list):
        raise ValueError("normalized Gamma tags are invalid")
    identity["tags"] = [_sanitize_public_prose(str(tag)) for tag in tags]
    return identity


def gamma_clob_identity(item: Mapping[str, object]) -> tuple[str, str, str, str]:
    """Return the market, condition, YES, and NO token IDs one Gamma item binds."""

    market_id = _text(item.get("id"))
    condition_id = _text(item.get("conditionId")) or market_id
    tokens = _encoded_array(item.get("clobTokenIds"), label="token IDs")
    yes_token, no_token = _text(tokens[0]), _text(tokens[1])
    if (
        not market_id
        or not yes_token
        or not no_token
        or yes_token == no_token
        or max(len(yes_token), len(no_token)) > MAX_CLOB_TOKEN_ID_CHARS
    ):
        raise ValueError("Gamma market has an invalid CLOB identity")
    return market_id, condition_id, yes_token, no_token


def _sports(category: str, tags: list[str], question: str) -> bool:
    words = {
        word.strip(".,:;!?()[]{}").casefold()
        for word in f"{category} {' '.join(tags)} {question}".split()
    }
    return bool(words & _SPORTS)


def gamma_eligibility_reason(
    item: Mapping[str, object], *, cutoff: datetime
) -> (
    Literal[
        "INACTIVE",
        "CLOSED",
        "SPORTS",
        "NON_BINARY",
        "MISSING_EVIDENCE",
        "CLOSE_WINDOW",
        "LOW_VOLUME",
    ]
    | None
):
    """Return the sole normalized Gamma eligibility decision."""

    try:
        if type(item.get("active")) is not bool or type(item.get("closed")) is not bool:
            raise ValueError
        identity = normalize_gamma_identity(item)
        market_id, _condition_id, _yes_token, _no_token = gamma_clob_identity(item)
        close = normalize_gamma_utc(item.get("endDate"))
        volume = _decimal(item.get("volume24hr"), label="24h volume")
        outcomes = _encoded_array(item.get("outcomes"), label="outcomes")
        prices = _encoded_array(item.get("outcomePrices"), label="outcome prices")
        parsed_prices = [_decimal(value, label="outcome price") for value in prices]
        if any(not 0 <= value <= 1 for value in parsed_prices):
            raise ValueError
    except (TypeError, ValueError) as error:
        raise ValueError("Gamma eligibility input is malformed") from error
    question = str(identity["question"])
    description = str(identity["description"])
    rules = str(identity["rules"])
    source = str(identity["resolution_source_text"])
    category = str(identity["category"])
    tags_value = identity["tags"]
    assert isinstance(tags_value, list)
    tags = [str(tag) for tag in tags_value]
    if item["active"] is not True:
        return "INACTIVE"
    if item["closed"] is not False:
        return "CLOSED"
    if _sports(category, tags, question):
        return "SPORTS"
    if outcomes != ["Yes", "No"]:
        return "NON_BINARY"
    if not all((market_id, question, description, rules, source)):
        return "MISSING_EVIDENCE"
    if not timedelta(hours=1) <= close - cutoff <= timedelta(days=30):
        return "CLOSE_WINDOW"
    if volume < 10_000:
        return "LOW_VOLUME"
    return None


def _book_time(value: object) -> datetime:
    if type(value) is int or (type(value) is str and value.isdigit()):
        milliseconds = int(value)
        if not 0 <= milliseconds <= MAX_BOOK_MILLISECONDS:
            raise ValueError("book timestamp is outside the supported range")
        return datetime.fromtimestamp(milliseconds / 1000, UTC)
    return _utc(value)


def _levels(value: object, *, descending: bool) -> list[BookLevel]:
    if not isinstance(value, list) or not value or len(value) > 10_000:
        raise ValueError("book side must be a bounded nonempty list")
    by_price: dict[int, int] = {}
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != {"price", "size"}:
            raise ValueError("book level has unexpected shape")
        price = _decimal(raw["price"], label="book price") * 1_000_000
        size = _decimal(raw["size"], label="book size")
        if price != price.to_integral_value() or not 0 < price < 1_000_000 or size <= 0:
            raise ValueError("book level is outside fixed-point bounds")
        quantity = int(size.to_integral_value(rounding=ROUND_FLOOR))
        if quantity <= 0:
            continue
        by_price[int(price)] = by_price.get(int(price), 0) + quantity
    ordered = sorted(by_price.items(), reverse=descending)
    if not ordered:
        raise ValueError("book side has no whole-contract depth")
    return [
        BookLevel(price_micros=price, quantity=quantity) for price, quantity in ordered
    ]


def _book_snapshot(
    raw: bytes,
    value: object,
    *,
    expected_market_id: str,
    expected_condition_id: str,
    expected_token_id: str,
    now: datetime,
) -> BookSnapshot:
    if not isinstance(value, Mapping) or set(value) - {
        "asks",
        "asset_id",
        "bids",
        "hash",
        "last_trade_price",
        "market",
        "min_order_size",
        "neg_risk",
        "tick_size",
        "timestamp",
    }:
        raise ValueError("book response has unexpected shape")
    if _text(value.get("asset_id")) != expected_token_id:
        raise ValueError("TOKEN_DRIFT")
    raw_market = _text(value.get("market"))
    if raw_market not in {expected_market_id, expected_condition_id}:
        raise ValueError("MARKET_DRIFT")
    retrieved_at = _book_time(value.get("timestamp"))
    age = now - retrieved_at
    if not timedelta(0) <= age <= MAX_BOOK_AGE:
        raise ValueError("STALE_BOOK")
    bids = _levels(value.get("bids"), descending=True)
    asks = _levels(value.get("asks"), descending=False)
    try:
        return BookSnapshot(
            market_id=expected_market_id,
            token_id=expected_token_id,
            retrieved_at=retrieved_at,
            source_sha256=hashlib.sha256(raw).hexdigest(),
            bids=bids,
            asks=asks,
            response_retrieved_at=now,
        )
    except ValueError as error:
        raise ValueError("CROSSED_BOOK") from error


def _reason(error: Exception) -> str:
    message = str(error)
    for reason in ("TOKEN_DRIFT", "MARKET_DRIFT", "STALE_BOOK", "CROSSED_BOOK"):
        if reason in message:
            return reason
    return "MALFORMED_BOOK"


def reconstruct_book_snapshot(
    raw: bytes, *, market_id: str, response_retrieved_at: datetime | None = None
) -> BookSnapshot:
    """Reconstruct a strict normalized book from authoritative raw CLOB bytes."""

    try:
        value = load_strict_json(raw)
        if not isinstance(value, Mapping):
            raise ValueError("book must be an object")
        condition_id = _text(value.get("market"))
        token_id = _text(value.get("asset_id"))
        captured_at = _book_time(value.get("timestamp"))
        return _book_snapshot(
            raw,
            value,
            expected_market_id=market_id,
            expected_condition_id=condition_id,
            expected_token_id=token_id,
            now=response_retrieved_at or captured_at,
        )
    except (TypeError, ValueError, ArithmeticError) as error:
        raise PublicDataError("execution book artifact is invalid") from error


def precheck_eligibility_reason(book: BookSnapshot) -> str | None:
    """Return the shared spread or near-depth rejection reason for a valid book.

    The client and every replay validator call this one pure function: a spread above
    40,000 micros is ``WIDE_SPREAD``; otherwise each side needs at least 5,000
    contracts within 20,000 micros of its best price or the book is ``LOW_DEPTH``.
    """

    best_bid = book.bids[0].price_micros
    best_ask = book.asks[0].price_micros
    if best_ask - best_bid > MAX_PRECHECK_SPREAD_MICROS:
        return "WIDE_SPREAD"
    bid_depth = sum(
        level.quantity
        for level in book.bids
        if best_bid - level.price_micros <= PRECHECK_DEPTH_WINDOW_MICROS
    )
    ask_depth = sum(
        level.quantity
        for level in book.asks
        if level.price_micros - best_ask <= PRECHECK_DEPTH_WINDOW_MICROS
    )
    if bid_depth < MIN_PRECHECK_NEAR_DEPTH or ask_depth < MIN_PRECHECK_NEAR_DEPTH:
        return "LOW_DEPTH"
    return None


def derive_precheck_transport_reason(
    *,
    status_code: int,
    content_type: str | None,
    truncated: bool,
    body: bytes,
) -> PrecheckTransportReason:
    """Classify one received CLOB response; pre-response failures are typed upstream."""

    if truncated or len(body) > MAX_PUBLIC_BYTES:
        return "OVERSIZED"
    if 300 <= status_code < 400:
        return "REDIRECT"
    if not 200 <= status_code < 300:
        return "HTTP_STATUS"
    if content_type != "application/json":
        return "CONTENT_TYPE"
    try:
        _json_depth(load_strict_json(body.decode("utf-8")))
    except (TypeError, ValueError, RecursionError):
        return "NOT_JSON"
    return "OK"


def derive_precheck_rejection(
    *,
    transport_reason: PrecheckTransportReason,
    body: bytes,
    market_id: str,
    condition_id: str,
    token_id: str,
    now: datetime,
) -> tuple[str | None, BookSnapshot | None]:
    """Recompute the rejection reason, and the valid book if one was received."""

    if transport_reason == "TIMEOUT":
        return "PRECHECK_TIMEOUT", None
    if transport_reason == "TRANSPORT_ERROR":
        return "BOOK_UNAVAILABLE", None
    if transport_reason != "OK":
        return "MALFORMED_BOOK", None
    try:
        snapshot = _book_snapshot(
            body,
            load_strict_json(body.decode("utf-8")),
            expected_market_id=market_id,
            expected_condition_id=condition_id,
            expected_token_id=token_id,
            now=now,
        )
    except (TypeError, ValueError, ArithmeticError) as error:
        return _reason(error), None
    return precheck_eligibility_reason(snapshot), snapshot


class PolymarketClient:
    """Discover at most twenty candidates and fetch only their public YES books."""

    def __init__(
        self, transport: ByteTransport, *, page_size: int = 20, max_pages: int = 3
    ) -> None:
        if not 1 <= page_size <= 20 or not 1 <= max_pages <= 20:
            raise ValueError("page and page-count bounds must be in 1..20")
        self.transport = transport
        self.page_size = page_size
        self.max_pages = max_pages

    def _get(self, url: str, *, label: str) -> tuple[bytes, object, datetime]:
        try:
            response = self.transport(HttpRequest("GET", url))
        except TimeoutError as error:
            reason = "PRECHECK_TIMEOUT" if label == "CLOB" else "PUBLIC_DATA_TIMEOUT"
            raise PublicDataTimeout(
                f"{label} transport timed out", reason=reason
            ) from error
        except PublicDataError:
            raise
        except Exception as error:
            raise PublicDataError(f"{label} transport failed") from error
        try:
            value = _strict_response(response, label=label)
        except PublicDataError as error:
            raise PublicDataError(
                str(error),
                reason="MALFORMED_BOOK" if label == "CLOB" else error.reason,
                raw_bytes=response.body,
                retrieved_at=response.retrieved_at,
            ) from error
        return response.body, value, response.retrieved_at

    def _precheck_attempt(
        self, token_id: str, attempted_at: datetime
    ) -> PrecheckAttempt:
        """Make one precheck request and keep its exact bounded outcome."""

        def no_response(reason: PrecheckTransportReason) -> PrecheckAttempt:
            return PrecheckAttempt(
                token_id, attempted_at, None, None, None, b"", False, reason
            )

        url = f"{CLOB_ENDPOINT}?{urlencode({'token_id': token_id})}"
        try:
            response = self.transport(HttpRequest("GET", url))
        except (TimeoutError, PublicDataTimeout):
            return no_response("TIMEOUT")
        except Exception:
            return no_response("TRANSPORT_ERROR")
        if (
            not isinstance(response, HttpResponse)
            or type(response.status_code) is not int
            or not 100 <= response.status_code <= 599
            or not isinstance(response.headers, Mapping)
            or type(response.body) is not bytes
            or type(response.retrieved_at) is not datetime
            or response.retrieved_at.tzinfo is None
        ):
            return no_response("TRANSPORT_ERROR")
        truncated = bool(response.oversized) or len(response.body) > MAX_PUBLIC_BYTES
        body = response.body[:MAX_PUBLIC_BYTES]
        content_type = normalize_content_type(response.headers.get("content-type"))
        return PrecheckAttempt(
            token_id,
            attempted_at,
            response.status_code,
            content_type,
            response.retrieved_at.astimezone(UTC),
            body,
            truncated,
            derive_precheck_transport_reason(
                status_code=response.status_code,
                content_type=content_type,
                truncated=truncated,
                body=body,
            ),
        )

    def poll(
        self,
        *,
        limit: int = 1,
        now: datetime | None = None,
        history_points: Literal[1, 2] = 1,
    ) -> PollResult:
        if not 1 <= limit <= 20:
            raise ValueError("limit must be in 1..20")
        cutoff = now or datetime.now(UTC)
        if cutoff.tzinfo is not UTC:
            cutoff = cutoff.astimezone(UTC)
        normalized: list[
            tuple[datetime, str, bytes, Mapping[str, object], datetime]
        ] = []
        for page in range(self.max_pages):
            params = urlencode(
                {
                    "active": "true",
                    "closed": "false",
                    "order": "volume24hr",
                    "ascending": "false",
                    "limit": self.page_size,
                    "offset": page * self.page_size,
                }
            )
            raw, value, retrieved_at = self._get(
                f"{GAMMA_ENDPOINT}?{params}", label="Gamma"
            )
            if not isinstance(value, list):
                raise PublicDataError(
                    "Gamma response must be an array",
                    raw_bytes=raw,
                    retrieved_at=retrieved_at,
                )
            raw_items = _market_raw_items(raw)
            if len(raw_items) != len(value):
                raise PublicDataError("Gamma raw item count is inconsistent")
            for item_raw, item in zip(raw_items, value, strict=True):
                if not isinstance(item, Mapping):
                    raise PublicDataError("Gamma market must be an object")
                try:
                    close = normalize_gamma_utc(item.get("endDate"))
                except (TypeError, ValueError):
                    close = datetime.max.replace(tzinfo=UTC)
                normalized.append(
                    (close, _text(item.get("id")), item_raw, item, retrieved_at)
                )
            if len(value) < self.page_size:
                break
        normalized.sort(key=lambda row: (row[0], row[1], row[2]))
        unique: list[tuple[datetime, str, bytes, Mapping[str, object], datetime]] = []
        positions: dict[str, int] = {}
        semantics: dict[str, bytes | None] = {}
        conflict_rows: dict[str, list[bytes]] = {}
        malformed_rows: set[bytes] = set()
        for row in normalized:
            try:
                market_identity, semantic = _gamma_semantic_identity(row[3])
            except (TypeError, ValueError):
                market_id = row[1]
                if not market_id:
                    if row[2] not in malformed_rows:
                        malformed_rows.add(row[2])
                        unique.append(row)
                    continue
                position = positions.get(market_id)
                if position is None:
                    positions[market_id] = len(unique)
                    semantics[market_id] = None
                    unique.append(row)
                    continue
                if semantics[market_id] is None:
                    continue
                conflict_rows.setdefault(market_id, [unique[position][2]]).append(
                    row[2]
                )
                if row[2] < unique[position][2]:
                    unique[position] = row
                continue
            market_id = market_identity[0]
            position = positions.get(market_id)
            if position is None:
                positions[market_id] = len(unique)
                semantics[market_id] = semantic
                unique.append(row)
                continue
            if semantics[market_id] == semantic:
                continue
            conflict_rows.setdefault(market_id, [unique[position][2]]).append(row[2])
            if row[2] < unique[position][2]:
                unique[position] = row
        eligible: list[MarketCandidate] = []
        rejected: list[MarketRejection] = []
        for close, market_id, raw_market, item, retrieved_at in unique:
            if len(eligible) + len(rejected) >= limit:
                break
            reason: str | None = None
            try:
                normalize_gamma_identity(item)
                reason = (
                    "DUPLICATE_MARKET_DATA"
                    if market_id in conflict_rows
                    else gamma_eligibility_reason(item, cutoff=cutoff)
                )
                if reason == "DUPLICATE_MARKET_DATA":
                    raw_market = canonical_json_bytes(
                        {
                            "duplicate_market_data": [
                                base64.b64encode(value).decode()
                                for value in sorted(set(conflict_rows[market_id]))
                            ],
                            "market_id": market_id,
                        }
                    )
            except ValueError:
                reason = "MALFORMED_MARKET"
            condition_id = _text(item.get("conditionId")) or market_id
            if reason is not None:
                rejected.append(
                    MarketRejection(
                        market_id or "UNKNOWN",
                        reason,
                        raw_market,
                        hashlib.sha256(raw_market).hexdigest(),
                    )
                )
                continue
            assert market_id
            _, condition_id, yes_token, no_token = gamma_clob_identity(item)
            source_sha = hashlib.sha256(raw_market).hexdigest()
            safe_identity = sanitized_gamma_identity(item)
            safe_tags = safe_identity["tags"]
            if not isinstance(safe_tags, list):
                raise ValueError("sanitized Gamma tags are invalid")
            try:
                evidence = EvidencePacket(
                    market_id=market_id,
                    question=str(safe_identity["question"]),
                    description=str(safe_identity["description"]),
                    rules=str(safe_identity["rules"]),
                    category=str(safe_identity["category"]),
                    tags=[str(tag) for tag in safe_tags],
                    resolution_source_text=str(safe_identity["resolution_source_text"]),
                    closes_at=close,
                    retrieved_at=min(retrieved_at, cutoff),
                    cutoff_at=cutoff,
                    source_sha256=source_sha,
                )
            except ValueError:
                rejected.append(
                    MarketRejection(
                        market_id, "MALFORMED_MARKET", raw_market, source_sha
                    )
                )
                continue
            attempts: list[PrecheckAttempt] = []
            snapshots: list[BookSnapshot] = []
            reason = None
            for attempt_number in range(history_points):
                attempted_at = (
                    cutoff
                    if not attempts
                    else max(
                        cutoff,
                        attempts[-1].retrieved_at or attempts[-1].attempted_at,
                    )
                )
                attempt = self._precheck_attempt(yes_token, attempted_at)
                snapshot_reason, snapshot = derive_precheck_rejection(
                    transport_reason=attempt.transport_reason,
                    body=attempt.body,
                    market_id=market_id,
                    condition_id=condition_id,
                    token_id=yes_token,
                    now=attempt.retrieved_at or attempted_at,
                )
                attempts.append(attempt)
                if snapshot is not None:
                    snapshots.append(snapshot)
                if snapshot_reason is not None:
                    reason = snapshot_reason
                    break
                assert snapshot is not None
                if attempt_number == 1:
                    first_at = (
                        snapshots[0].response_retrieved_at or snapshots[0].retrieved_at
                    )
                    second_at = snapshot.response_retrieved_at or snapshot.retrieved_at
                    if second_at <= first_at:
                        reason = "STALE_PRECHECK"
            if reason is not None:
                attempt = attempts[-1]
                snapshot = snapshots[-1] if snapshots else None
                rejection_book = (
                    snapshot if reason in {"WIDE_SPREAD", "LOW_DEPTH"} else None
                )
                rejected.append(
                    MarketRejection(
                        market_id=market_id,
                        reason=reason,
                        raw_gamma_bytes=raw_market,
                        raw_gamma_sha256=source_sha,
                        evidence=evidence,
                        yes_token_id=yes_token,
                        precheck_book_bytes=attempt.body,
                        precheck_book=rejection_book,
                        precheck_failure_sha256=(
                            hashlib.sha256(attempt.body).hexdigest()
                            if rejection_book is None
                            else None
                        ),
                        precheck_retrieved_at=(attempt.retrieved_at or cutoff),
                        precheck_byte_length=len(attempt.body),
                        precheck_request_token_id=yes_token,
                        precheck_request_market_id=market_id,
                        precheck_request_condition_id=condition_id,
                        precheck_transport_failure=attempt.status_code is None,
                        precheck_attempt=attempt,
                    )
                )
                continue
            assert len(attempts) == len(snapshots) == history_points
            if history_points == 2:
                final_cutoff = max(
                    cutoff,
                    *(
                        attempt.retrieved_at or attempt.attempted_at
                        for attempt in attempts
                    ),
                ) + timedelta(microseconds=1)
                evidence = evidence.model_copy(update={"cutoff_at": final_cutoff})
                snapshots = [
                    book.model_copy(
                        update={
                            "retrieved_at": attempt.retrieved_at or book.retrieved_at
                        }
                    )
                    for book, attempt in zip(snapshots, attempts, strict=True)
                ]
            else:
                attempts.append(attempts[0])
                snapshots.append(snapshots[0])
            eligible.append(
                MarketCandidate(
                    market_id=market_id,
                    condition_id=condition_id,
                    yes_token_id=yes_token,
                    no_token_id=no_token,
                    slot_at=cutoff.replace(second=0, microsecond=0),
                    raw_gamma_bytes=raw_market,
                    raw_gamma_sha256=source_sha,
                    evidence=evidence,
                    precheck_book_bytes=attempts[0].body,
                    precheck_book=snapshots[0],
                    precheck_attempt=attempts[0],
                    precheck_history_bytes=(attempts[0].body, attempts[1].body),
                    precheck_history=(snapshots[0], snapshots[1]),
                    precheck_attempts=(attempts[0], attempts[1]),
                )
            )
        return PollResult(tuple(eligible), tuple(rejected))

    def execution_book(
        self, candidate: MarketCandidate, *, now: datetime | None = None
    ) -> tuple[bytes, BookSnapshot]:
        cutoff = now or datetime.now(UTC)
        if cutoff.tzinfo is not UTC:
            cutoff = cutoff.astimezone(UTC)
        raw, value, retrieved_at = self._get(
            f"{CLOB_ENDPOINT}?{urlencode({'token_id': candidate.yes_token_id})}",
            label="CLOB",
        )
        try:
            snapshot = _book_snapshot(
                raw,
                value,
                expected_market_id=candidate.market_id,
                expected_condition_id=candidate.condition_id,
                expected_token_id=candidate.yes_token_id,
                now=retrieved_at,
            )
        except (TypeError, ValueError, ArithmeticError) as error:
            raise PublicDataError(_reason(error)) from error
        return raw, snapshot

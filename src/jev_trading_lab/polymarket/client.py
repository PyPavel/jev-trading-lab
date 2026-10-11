"""Bounded, read-only Polymarket discovery and order-book normalization."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Final
from urllib.parse import parse_qs, urlencode, urlsplit

from jev_trading_lab.polymarket.models import (
    BookLevel,
    BookSnapshot,
    EvidencePacket,
    load_strict_json,
)

GAMMA_ENDPOINT: Final = "https://gamma-api.polymarket.com/markets"
CLOB_ENDPOINT: Final = "https://clob.polymarket.com/book"
MAX_PUBLIC_BYTES: Final = 2 * 1024 * 1024
MAX_JSON_DEPTH: Final = 16
MAX_BOOK_AGE: Final = timedelta(seconds=30)
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
    def __init__(self, host: str, ip: str, *, timeout: float) -> None:
        self._tls_context = ssl.create_default_context()
        super().__init__(host, port=443, timeout=timeout, context=self._tls_context)
        self._ip = ip

    def connect(self) -> None:
        raw = socket.create_connection((self._ip, 443), self.timeout)
        try:
            peer = raw.getpeername()[0]
            if peer != self._ip or not is_safe_global_ip(peer):
                raise PublicDataError("HTTPS peer differs from the validated address")
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

    def validate(self, request: HttpRequest) -> tuple[str, str, str]:
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
        answers = self.resolver(host, 443, 0, socket.SOCK_STREAM)
        addresses = {str(answer[4][0]) for answer in answers if len(answer) >= 5}
        if not addresses or any(
            not is_safe_global_ip(address) for address in addresses
        ):
            raise PublicDataError("DNS returned an unsafe or mixed answer set")
        return (
            host,
            parsed.path + (f"?{parsed.query}" if parsed.query else ""),
            sorted(addresses)[0],
        )

    def __call__(self, request: HttpRequest) -> HttpResponse:
        host, target, address = self.validate(request)
        connection = _PinnedHTTPSConnection(host, address, timeout=self.timeout_seconds)
        try:
            connection.request(
                "GET", target, headers={"Accept": "application/json", "Host": host}
            )
            response = connection.getresponse()
            body = response.read(self.max_bytes + 1)
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


@dataclass(frozen=True, slots=True)
class MarketCandidate:
    market_id: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    raw_gamma_bytes: bytes = field(repr=False)
    raw_gamma_sha256: str
    evidence: EvidencePacket
    precheck_book_bytes: bytes = field(repr=False)
    precheck_book: BookSnapshot


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
    except (TypeError, ValueError) as error:
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


def _encoded_array(value: object, *, label: str) -> list[object]:
    if type(value) is str:
        value = load_strict_json(value)
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
        "closes_at": value.get("endDate"),
    }


def _sports(category: str, tags: list[str], question: str) -> bool:
    words = {
        word.strip(".,:;!?()[]{}").casefold()
        for word in f"{category} {' '.join(tags)} {question}".split()
    }
    return bool(words & _SPORTS)


def _book_time(value: object) -> datetime:
    if type(value) is int or (type(value) is str and value.isdigit()):
        milliseconds = int(value)
        if milliseconds < 0:
            raise ValueError("book timestamp must be nonnegative")
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
    if retrieved_at > now + timedelta(seconds=5) or now - retrieved_at > MAX_BOOK_AGE:
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
        )
    except ValueError as error:
        raise ValueError("CROSSED_BOOK") from error


def _reason(error: Exception) -> str:
    message = str(error)
    for reason in ("TOKEN_DRIFT", "MARKET_DRIFT", "STALE_BOOK", "CROSSED_BOOK"):
        if reason in message:
            return reason
    return "MALFORMED_BOOK"


def reconstruct_book_snapshot(raw: bytes, *, market_id: str) -> BookSnapshot:
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
            now=captured_at,
        )
    except (TypeError, ValueError) as error:
        raise PublicDataError("execution book artifact is invalid") from error


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

    def poll(self, *, limit: int = 1, now: datetime | None = None) -> PollResult:
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
                raise PublicDataError("Gamma response must be an array")
            raw_items = _market_raw_items(raw)
            if len(raw_items) != len(value):
                raise PublicDataError("Gamma raw item count is inconsistent")
            for item_raw, item in zip(raw_items, value, strict=True):
                if not isinstance(item, Mapping):
                    raise PublicDataError("Gamma market must be an object")
                try:
                    close = _utc(item.get("endDate"))
                except (TypeError, ValueError):
                    close = datetime.max.replace(tzinfo=UTC)
                normalized.append(
                    (close, _text(item.get("id")), item_raw, item, retrieved_at)
                )
            if len(value) < self.page_size:
                break
        normalized.sort(key=lambda row: (row[0], row[1]))
        eligible: list[MarketCandidate] = []
        rejected: list[MarketRejection] = []
        for close, market_id, raw_market, item, retrieved_at in normalized:
            if len(eligible) + len(rejected) >= limit:
                break
            reason: str | None = None
            try:
                identity = normalize_gamma_identity(item)
            except ValueError:
                identity = {
                    "question": "",
                    "description": "",
                    "rules": "",
                    "resolution_source_text": "",
                    "category": "Uncategorized",
                    "tags": [],
                }
                reason = "MALFORMED_MARKET"
            question = str(identity["question"])
            description = str(identity["description"])
            rules = str(identity["rules"])
            resolution_source = str(identity["resolution_source_text"])
            category = str(identity["category"])
            raw_tags = identity["tags"]
            tags = [str(tag) for tag in raw_tags] if isinstance(raw_tags, list) else []
            condition_id = _text(item.get("conditionId")) or market_id
            if reason is not None:
                pass
            elif item.get("active") is not True:
                reason = "INACTIVE"
            elif item.get("closed") is not False:
                reason = "CLOSED"
            elif _sports(category, tags, question):
                reason = "SPORTS"
            elif not all((market_id, question, description, rules, resolution_source)):
                reason = "MISSING_EVIDENCE"
            elif not timedelta(hours=1) <= close - cutoff <= timedelta(days=30):
                reason = "CLOSE_WINDOW"
            else:
                try:
                    volume = _decimal(item.get("volume24hr"), label="24h volume")
                    outcomes = _encoded_array(item.get("outcomes"), label="outcomes")
                    prices = _encoded_array(
                        item.get("outcomePrices"), label="outcome prices"
                    )
                    tokens = _encoded_array(item.get("clobTokenIds"), label="token IDs")
                    if volume < 10_000:
                        reason = "LOW_VOLUME"
                    elif outcomes != ["Yes", "No"]:
                        reason = "NON_BINARY"
                    else:
                        parsed_prices = [
                            _decimal(value, label="outcome price") for value in prices
                        ]
                        parsed_tokens = [_text(value) for value in tokens]
                        if (
                            any(not 0 <= value <= 1 for value in parsed_prices)
                            or not all(parsed_tokens)
                            or parsed_tokens[0] == parsed_tokens[1]
                        ):
                            raise ValueError("invalid prices or token IDs")
                except (TypeError, ValueError):
                    reason = "MALFORMED_MARKET"
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
            assert market_id and isinstance(tokens, list)
            yes_token, no_token = str(tokens[0]), str(tokens[1])
            source_sha = hashlib.sha256(raw_market).hexdigest()
            evidence = EvidencePacket(
                market_id=market_id,
                question=question,
                description=description,
                rules=rules,
                category=category,
                tags=tags,
                resolution_source_text=resolution_source,
                closes_at=close,
                retrieved_at=min(retrieved_at, cutoff),
                cutoff_at=cutoff,
                source_sha256=source_sha,
            )
            book_url = f"{CLOB_ENDPOINT}?{urlencode({'token_id': yes_token})}"
            raw_book: bytes | None = None
            snapshot: BookSnapshot | None = None
            transport_failure = False
            try:
                raw_book, book_value, _ = self._get(book_url, label="CLOB")
                snapshot = _book_snapshot(
                    raw_book,
                    book_value,
                    expected_market_id=market_id,
                    expected_condition_id=condition_id,
                    expected_token_id=yes_token,
                    now=cutoff,
                )
                if (
                    snapshot.asks[0].price_micros - snapshot.bids[0].price_micros
                    > 40_000
                ):
                    reason = "WIDE_SPREAD"
                elif (
                    sum(
                        level.quantity
                        for level in snapshot.bids
                        if snapshot.bids[0].price_micros - level.price_micros <= 20_000
                    )
                    < 5_000
                    or sum(
                        level.quantity
                        for level in snapshot.asks
                        if level.price_micros - snapshot.asks[0].price_micros <= 20_000
                    )
                    < 5_000
                ):
                    reason = "LOW_DEPTH"
            except PublicDataTimeout as error:
                raw_book = error.raw_bytes
                reason = error.reason
                transport_failure = True
            except PublicDataError as error:
                raw_book = error.raw_bytes
                reason = error.reason
                transport_failure = error.retrieved_at is None
            except (TypeError, ValueError) as error:
                reason = _reason(error)
            if reason is not None:
                rejection_book = (
                    snapshot if reason in {"WIDE_SPREAD", "LOW_DEPTH"} else None
                )
                rejection_bytes = raw_book
                failure_hash = (
                    hashlib.sha256(raw_book).hexdigest()
                    if raw_book is not None and rejection_book is None
                    else None
                )
                rejected.append(
                    MarketRejection(
                        market_id=market_id,
                        reason=reason,
                        raw_gamma_bytes=raw_market,
                        raw_gamma_sha256=source_sha,
                        evidence=evidence,
                        yes_token_id=yes_token,
                        precheck_book_bytes=rejection_bytes,
                        precheck_book=rejection_book,
                        precheck_failure_sha256=failure_hash,
                        precheck_retrieved_at=(
                            snapshot.retrieved_at if snapshot is not None else cutoff
                        ),
                        precheck_byte_length=(
                            len(rejection_bytes)
                            if rejection_bytes is not None
                            else None
                        ),
                        precheck_request_token_id=yes_token,
                        precheck_request_market_id=market_id,
                        precheck_request_condition_id=condition_id,
                        precheck_transport_failure=transport_failure,
                    )
                )
                continue
            assert raw_book is not None and snapshot is not None
            eligible.append(
                MarketCandidate(
                    market_id=market_id,
                    condition_id=condition_id,
                    yes_token_id=yes_token,
                    no_token_id=no_token,
                    raw_gamma_bytes=raw_market,
                    raw_gamma_sha256=source_sha,
                    evidence=evidence,
                    precheck_book_bytes=raw_book,
                    precheck_book=snapshot,
                )
            )
        return PollResult(tuple(eligible), tuple(rejected))

    def execution_book(
        self, candidate: MarketCandidate, *, now: datetime | None = None
    ) -> tuple[bytes, BookSnapshot]:
        cutoff = now or datetime.now(UTC)
        if cutoff.tzinfo is not UTC:
            cutoff = cutoff.astimezone(UTC)
        raw, value, _ = self._get(
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
                now=cutoff,
            )
        except (TypeError, ValueError) as error:
            raise PublicDataError(_reason(error)) from error
        return raw, snapshot

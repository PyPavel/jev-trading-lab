from __future__ import annotations

import json
import socket
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from jev_trading_lab.polymarket.client import (
    HttpRequest,
    HttpResponse,
    PolymarketClient,
    PublicDataError,
    SecureHttpTransport,
)

NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


def market(market_id: str = "m1", **changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": market_id,
        "conditionId": f"condition-{market_id}",
        "question": f"Will {market_id} happen?",
        "description": "Resolution rules and relevant public context.",
        "rules": "Resolve YES when the stated event occurs.",
        "resolutionSource": "Official agency publication.",
        "endDate": (NOW + timedelta(days=2)).isoformat().replace("+00:00", "Z"),
        "active": True,
        "closed": False,
        "category": "Politics",
        "tags": ["Elections"],
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.45","0.55"]',
        "clobTokenIds": f'["yes-{market_id}","no-{market_id}"]',
        "volume24hr": 20_000,
        "acceptingOrders": True,
    }
    value.update(changes)
    return value


def book(
    market_id: str = "m1", token: str | None = None, **changes: Any
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "market": f"condition-{market_id}",
        "asset_id": token or f"yes-{market_id}",
        "timestamp": str(int(NOW.timestamp() * 1000)),
        "bids": [{"price": "0.48", "size": "3000"}, {"price": "0.47", "size": "2500"}],
        "asks": [{"price": "0.51", "size": "3000"}, {"price": "0.52", "size": "2500"}],
    }
    value.update(changes)
    return value


class CannedTransport:
    def __init__(self, pages: list[object], books: list[object]) -> None:
        self.pages = list(pages)
        self.books = list(books)
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        payload = self.pages.pop(0) if "gamma-api" in request.url else self.books.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        if isinstance(payload, HttpResponse):
            return payload
        return HttpResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=json.dumps(payload, separators=(",", ":")).encode(),
            retrieved_at=NOW,
        )


def test_pagination_normalization_and_deterministic_order() -> None:
    late = market("late", endDate=(NOW + timedelta(days=3)).isoformat())
    early = market("early", endDate=(NOW + timedelta(days=1)).isoformat())
    transport = CannedTransport([[late], [early], []], [book("early"), book("late")])
    result = PolymarketClient(transport, page_size=1, max_pages=3).poll(
        limit=2, now=NOW
    )
    assert [item.market_id for item in result.eligible] == ["early", "late"]
    assert [request.method for request in transport.requests] == ["GET"] * 5
    assert all(request.body is None for request in transport.requests)
    assert all("limit=1" in request.url for request in transport.requests[:3])
    assert [
        "offset=0" in transport.requests[0].url,
        "offset=1" in transport.requests[1].url,
    ] == [True, True]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"active": False}, "INACTIVE"),
        ({"closed": True}, "CLOSED"),
        ({"category": "Sports"}, "SPORTS"),
        ({"tags": ["NBA"]}, "SPORTS"),
        ({"description": ""}, "MISSING_EVIDENCE"),
        ({"rules": ""}, "MISSING_EVIDENCE"),
        ({"resolutionSource": ""}, "MISSING_EVIDENCE"),
        ({"endDate": (NOW + timedelta(minutes=59)).isoformat()}, "CLOSE_WINDOW"),
        ({"endDate": (NOW + timedelta(days=31)).isoformat()}, "CLOSE_WINDOW"),
        ({"volume24hr": 9_999.99}, "LOW_VOLUME"),
        ({"outcomes": '["A","B"]'}, "NON_BINARY"),
        ({"outcomePrices": '["nan","0.5"]'}, "MALFORMED_MARKET"),
        ({"clobTokenIds": '["same","same"]'}, "MALFORMED_MARKET"),
    ],
)
def test_gamma_eligibility_gates(changes: dict[str, Any], reason: str) -> None:
    transport = CannedTransport([[market(**changes)]], [])
    result = PolymarketClient(transport).poll(limit=1, now=NOW)
    assert not result.eligible
    assert result.rejections[0].reason == reason
    assert all(
        "clob.polymarket.com" not in request.url for request in transport.requests
    )


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (
            {
                "asks": [
                    {"price": "0.53", "size": "5000"},
                    {"price": "0.54", "size": "5000"},
                ]
            },
            "WIDE_SPREAD",
        ),
        (
            {
                "bids": [
                    {"price": "0.48", "size": "2000"},
                    {"price": "0.47", "size": "2999"},
                ]
            },
            "LOW_DEPTH",
        ),
        (
            {
                "asks": [
                    {"price": "0.48", "size": "3000"},
                    {"price": "0.52", "size": "3000"},
                ]
            },
            "CROSSED_BOOK",
        ),
        (
            {"timestamp": str(int((NOW - timedelta(seconds=31)).timestamp() * 1000))},
            "STALE_BOOK",
        ),
        ({"asset_id": "changed-token"}, "TOKEN_DRIFT"),
    ],
)
def test_precheck_book_gates(changes: dict[str, Any], reason: str) -> None:
    transport = CannedTransport([[market()]], [book(**changes)])
    result = PolymarketClient(transport).poll(limit=1, now=NOW)
    assert not result.eligible
    assert result.rejections[0].reason == reason
    rejection = result.rejections[0]
    if reason in {"WIDE_SPREAD", "LOW_DEPTH"}:
        assert rejection.precheck_book_bytes is not None
        assert rejection.precheck_book is not None
        assert rejection.precheck_failure_sha256 is None
    else:
        assert rejection.precheck_book is None
        assert rejection.precheck_failure_sha256 is not None


def test_price_free_evidence_uses_gamma_text_and_raw_market_bytes() -> None:
    raw_market = market(resolutionSource="https://official.example/rules")
    transport = CannedTransport([[raw_market]], [book()])
    candidate = PolymarketClient(transport).poll(limit=1, now=NOW).eligible[0]
    assert candidate.evidence.resolution_source_text == "https://official.example/rules"
    assert candidate.evidence.source_sha256 == candidate.raw_gamma_sha256
    assert json.loads(candidate.raw_gamma_bytes) == raw_market
    encoded = candidate.evidence.model_dump_json()
    for forbidden in ("outcomePrices", "token", "book", "price"):
        assert forbidden not in encoded


def test_malformed_timeout_redirect_and_oversize_are_bounded() -> None:
    malformed = HttpResponse(200, {"content-type": "application/json"}, b"{", NOW)
    redirect = HttpResponse(302, {"location": "https://evil.example"}, b"", NOW)
    oversized = HttpResponse(
        200, {"content-type": "application/json"}, b"[]", NOW, oversized=True
    )
    for reply in (malformed, TimeoutError(), redirect, oversized):
        client = PolymarketClient(CannedTransport([reply], []))
        with pytest.raises(PublicDataError):
            client.poll(limit=1, now=NOW)


def test_limit_and_transport_routes_are_closed_world() -> None:
    with pytest.raises(ValueError):
        PolymarketClient(CannedTransport([], [])).poll(limit=0, now=NOW)
    with pytest.raises(ValueError):
        PolymarketClient(CannedTransport([], [])).poll(limit=21, now=NOW)
    transport = SecureHttpTransport(
        resolver=lambda *_: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ]
    )
    for method, url in [
        ("POST", "https://gamma-api.polymarket.com/markets"),
        ("GET", "http://gamma-api.polymarket.com/markets"),
        ("GET", "https://evil.example/markets"),
        ("GET", "https://gamma-api.polymarket.com/evil"),
        ("GET", "https://user@gamma-api.polymarket.com/markets"),
        ("GET", "https://@gamma-api.polymarket.com/markets"),
        ("GET", "https://:pass@gamma-api.polymarket.com/markets"),
        ("GET", "https://gamma-api.polymarket.com/markets#fragment"),
        ("GET", "https://clob.polymarket.com/book?token_id=x&extra=y"),
        ("GET", "https://clob.polymarket.com:444/book?token_id=x"),
    ]:
        with pytest.raises(PublicDataError):
            transport.validate(HttpRequest(method=method, url=url))


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "169.254.1.1", "224.0.0.1", "0.0.0.0", "192.0.2.1"],
)
def test_dns_rejects_unsafe_or_reserved_answers(address: str) -> None:
    def resolver(*_: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    transport = SecureHttpTransport(resolver=resolver)
    with pytest.raises(PublicDataError):
        transport.validate(
            HttpRequest(
                "GET",
                "https://gamma-api.polymarket.com/markets?limit=1&offset=0&active=true&closed=false&order=volume24hr&ascending=false",
            )
        )


def test_dns_rejects_mixed_safe_and_unsafe_answers() -> None:
    def resolver(*_: object) -> list[tuple[object, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    with pytest.raises(PublicDataError):
        SecureHttpTransport(resolver=resolver).validate(
            HttpRequest("GET", "https://clob.polymarket.com/book?token_id=x")
        )


@pytest.mark.parametrize("address", ["100.64.0.1", "::ffff:127.0.0.1"])
def test_dns_requires_ipaddress_is_global_including_mapped_ipv4(address: str) -> None:
    def resolver(*_: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 443))]

    with pytest.raises(PublicDataError):
        SecureHttpTransport(resolver=resolver).validate(
            HttpRequest("GET", "https://clob.polymarket.com/book?token_id=x")
        )


def test_depth_sums_only_levels_within_twenty_thousand_micros() -> None:
    distant = book(
        bids=[
            {"price": "0.48", "size": "4999"},
            {"price": "0.45", "size": "1000000"},
        ],
        asks=[
            {"price": "0.51", "size": "4999"},
            {"price": "0.54", "size": "1000000"},
        ],
    )
    result = PolymarketClient(CannedTransport([[market()]], [distant])).poll(
        limit=1, now=NOW
    )
    assert result.rejections[0].reason == "LOW_DEPTH"

    near = book(
        bids=[
            {"price": "0.48", "size": "2000"},
            {"price": "0.47", "size": "1500"},
            {"price": "0.46", "size": "1500"},
            {"price": "0.459999", "size": "1000000"},
        ],
        asks=[
            {"price": "0.51", "size": "2000"},
            {"price": "0.52", "size": "1500"},
            {"price": "0.53", "size": "1500"},
            {"price": "0.530001", "size": "1000000"},
        ],
    )
    result = PolymarketClient(CannedTransport([[market()]], [near])).poll(
        limit=1, now=NOW
    )
    assert len(result.eligible) == 1


def test_execution_book_rejects_stale_crossed_and_token_drift() -> None:
    for changed in (
        book(timestamp=str(int((NOW - timedelta(seconds=31)).timestamp() * 1000))),
        book(
            asks=[{"price": "0.47", "size": "5000"}, {"price": "0.52", "size": "5000"}]
        ),
        book(asset_id="other"),
    ):
        transport = CannedTransport([[market()]], [book(), changed])
        candidate = PolymarketClient(transport).poll(limit=1, now=NOW).eligible[0]
        with pytest.raises(PublicDataError):
            PolymarketClient(transport).execution_book(candidate, now=NOW)

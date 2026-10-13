from __future__ import annotations

import hashlib
import json
import socket
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from jev_trading_lab.polymarket.client import (
    HttpRequest,
    HttpResponse,
    PolymarketClient,
    PublicDataError,
    PublicDataTimeout,
    SecureHttpTransport,
    contains_public_credential_semantics,
    normalize_gamma_identity,
    sanitized_gamma_identity,
)
from jev_trading_lab.polymarket.models import BookLevel, BookSnapshot

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
    transport = CannedTransport(
        [[late], [early], []],
        [book("early"), book("late")],
    )
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


def test_identical_normalized_gamma_rows_deduplicate_before_limit_and_precheck() -> (
    None
):
    duplicate = market()
    transport = CannedTransport(
        [[duplicate, dict(duplicate)]],
        [book()],
    )

    result = PolymarketClient(transport).poll(limit=2, now=NOW)

    assert [item.market_id for item in result.eligible] == ["m1"]
    assert result.rejections == ()
    assert len([r for r in transport.requests if "clob.polymarket.com" in r.url]) == 1


def test_conflicting_duplicate_market_rows_become_one_canonical_rejection() -> None:
    first = market()
    second = market(description="Conflicting evidence")
    transport = CannedTransport([[second, first]], [])

    result = PolymarketClient(transport).poll(limit=2, now=NOW)

    assert result.eligible == ()
    assert [(item.market_id, item.reason) for item in result.rejections] == [
        ("m1", "DUPLICATE_MARKET_DATA")
    ]
    assert len(transport.requests) == 1


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


@pytest.mark.parametrize("payload", ({}, [1]))
def test_gamma_attempt_records_failure_only_after_endpoint_validation(
    payload: object,
) -> None:
    attempts: list[tuple[str, str, str, str | None, int]] = []
    transport = CannedTransport([payload], [])
    client = PolymarketClient(
        transport,
        attempt_sink=lambda *attempt: attempts.append(attempt),
    )

    with pytest.raises(PublicDataError, match="array|object"):
        client.poll(limit=1, now=NOW)

    assert [attempt[:4] for attempt in attempts] == [
        ("gamma", "gamma_poll", "failure", "malformed")
    ]
    assert len(transport.requests) == 1


@pytest.mark.parametrize("payload", ([{}], [market(active="true")]))
def test_gamma_attempt_fails_before_success_for_malformed_market_items(
    payload: list[dict[str, Any]],
) -> None:
    attempts: list[tuple[str, str, str, str | None, int]] = []
    transport = CannedTransport([payload], [])

    result = PolymarketClient(
        transport,
        attempt_sink=lambda *attempt: attempts.append(attempt),
    ).poll(limit=1, now=NOW)

    assert [(item.market_id, item.reason) for item in result.rejections] == [
        ("UNKNOWN" if payload == [{}] else "m1", "MALFORMED_MARKET")
    ]
    assert [attempt[:4] for attempt in attempts] == [
        ("gamma", "gamma_poll", "failure", "malformed")
    ]
    assert len(transport.requests) == 1


def test_execution_attempt_records_failure_only_for_malformed_clob_payload() -> None:
    client = PolymarketClient(CannedTransport([[market()]], [book(), []]))
    candidate = client.poll(limit=1, now=NOW).eligible[0]
    attempts: list[tuple[str, str, str, str | None, int]] = []
    client.attempt_sink = lambda *attempt: attempts.append(attempt)

    with pytest.raises(PublicDataError, match="MALFORMED_BOOK"):
        client.execution_book(candidate, now=NOW)

    assert [attempt[:4] for attempt in attempts] == [
        ("clob", "execution_book", "failure", "malformed")
    ]


@pytest.mark.parametrize(
    "changed",
    (
        book(asset_id="other"),
        book(market="other"),
        book(timestamp=str(int((NOW - timedelta(seconds=31)).timestamp() * 1000))),
        book(asks=[{"price": "0.47", "size": "5000"}]),
        [],
    ),
)
def test_precheck_attempt_records_failure_only_after_book_validation(
    changed: object,
) -> None:
    attempts: list[tuple[str, str, str, str | None, int]] = []
    transport = CannedTransport([[market()]], [changed])

    result = PolymarketClient(
        transport,
        attempt_sink=lambda *attempt: attempts.append(attempt),
    ).poll(limit=1, now=NOW)

    assert len(result.rejections) == 1
    assert [attempt[:4] for attempt in attempts] == [
        ("gamma", "gamma_poll", "success", None),
        ("clob", "precheck_book", "failure", "malformed"),
    ]
    assert len(transport.requests) == 2


def test_precheck_validated_snapshot_records_one_success() -> None:
    attempts: list[tuple[str, str, str, str | None, int]] = []
    transport = CannedTransport([[market()]], [book()])

    result = PolymarketClient(
        transport,
        attempt_sink=lambda *attempt: attempts.append(attempt),
    ).poll(limit=1, now=NOW)

    assert len(result.eligible) == 1
    assert [attempt[:4] for attempt in attempts] == [
        ("gamma", "gamma_poll", "success", None),
        ("clob", "precheck_book", "success", None),
    ]
    assert len(transport.requests) == 2


def test_validated_attempt_records_one_success_with_monotonic_latency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[tuple[str, str, str, str | None, int]] = []
    ticks = iter((100, 175))
    monkeypatch.setattr(
        "jev_trading_lab.polymarket.client.time.monotonic_ns", lambda: next(ticks)
    )

    result = PolymarketClient(
        CannedTransport([[]], []),
        attempt_sink=lambda *attempt: attempts.append(attempt),
    ).poll(limit=1, now=NOW)

    assert result.eligible == result.rejections == ()
    assert attempts == [("gamma", "gamma_poll", "success", None, 75)]


def test_precheck_transport_failures_preserve_typed_reason_and_empty_bytes() -> None:
    for failure, reason in (
        (TimeoutError(), "PRECHECK_TIMEOUT"),
        (OSError("dns failed"), "BOOK_UNAVAILABLE"),
    ):
        result = PolymarketClient(CannedTransport([[market()]], [failure])).poll(
            limit=1, now=NOW
        )
        rejection = result.rejections[0]
        assert rejection.reason == reason
        assert rejection.precheck_book_bytes == b""
        assert rejection.precheck_failure_sha256 == hashlib.sha256(b"").hexdigest()
        assert rejection.precheck_byte_length == 0
        assert rejection.precheck_request_token_id == "yes-m1"
        assert rejection.precheck_request_market_id == "m1"
    with pytest.raises(PublicDataTimeout) as captured:
        PolymarketClient(CannedTransport([], [TimeoutError()]))._get(
            "https://clob.polymarket.com/book?token_id=yes-m1", label="CLOB"
        )
    assert captured.value.reason == "PRECHECK_TIMEOUT"


def test_gamma_identity_normalization_is_deterministic_and_fail_closed() -> None:
    normalized = normalize_gamma_identity(
        {
            "id": "m1",
            "question": " Q ",
            "description": " D ",
            "rules": " R ",
            "resolutionSource": " S ",
            "endDate": "2026-10-12T12:00:00Z",
            "tags": [
                {"id": "id", "slug": "slug", "name": "name", "label": "label"},
                {"name": "name-only"},
                {"slug": "slug-only"},
                {"id": "id-only"},
                " text ",
            ],
        }
    )
    assert normalized["category"] == "Uncategorized"
    assert normalized["tags"] == ["label", "name-only", "slug-only", "id-only", "text"]
    with pytest.raises(ValueError, match="tag object"):
        normalize_gamma_identity({**market(), "tags": [{"other": "unsafe"}]})


def test_object_tags_and_missing_category_share_candidate_normalization() -> None:
    raw_market = market()
    raw_market.pop("category")
    raw_market["tags"] = [{"slug": "public-policy"}, {"id": "tag-2"}]
    candidate = (
        PolymarketClient(CannedTransport([[raw_market]], [book()]))
        .poll(limit=1, now=NOW)
        .eligible[0]
    )
    assert candidate.evidence.category == "Uncategorized"
    assert candidate.evidence.tags == ["public-policy", "tag-2"]


def test_missing_rules_is_not_filled_from_description() -> None:
    raw_market = market()
    raw_market.pop("rules")
    result = PolymarketClient(CannedTransport([[raw_market]], [])).poll(
        limit=1, now=NOW
    )
    assert result.rejections[0].reason == "MISSING_EVIDENCE"


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


# --- Final Unit 6 adversarial review findings -------------------------------


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


_JSON_HEADERS = {"content-type": "application/json"}
_VALID_BOOK = _json_bytes(book())


@pytest.mark.parametrize(
    ("label", "reply", "status", "content_type", "transport_reason", "reason"),
    [
        (
            "ok",
            HttpResponse(
                200,
                {"content-type": "Application/JSON; charset=UTF-8"},
                _VALID_BOOK,
                NOW,
            ),
            200,
            "application/json",
            "OK",
            None,
        ),
        (
            "503 with a valid JSON book",
            HttpResponse(503, _JSON_HEADERS, _VALID_BOOK, NOW),
            503,
            "application/json",
            "HTTP_STATUS",
            "MALFORMED_BOOK",
        ),
        (
            "wrong content type with a valid JSON book",
            HttpResponse(
                200, {"content-type": "text/html; charset=utf-8"}, _VALID_BOOK, NOW
            ),
            200,
            "text/html",
            "CONTENT_TYPE",
            "MALFORMED_BOOK",
        ),
        (
            "missing content type",
            HttpResponse(200, {}, _VALID_BOOK, NOW),
            200,
            None,
            "CONTENT_TYPE",
            "MALFORMED_BOOK",
        ),
        (
            "unparseable content type is bounded and sanitized",
            HttpResponse(
                200, {"content-type": "\x00 not/a media type"}, _VALID_BOOK, NOW
            ),
            200,
            "invalid",
            "CONTENT_TYPE",
            "MALFORMED_BOOK",
        ),
        (
            "redirect",
            HttpResponse(302, {"location": "https://evil.example"}, b"", NOW),
            302,
            None,
            "REDIRECT",
            "MALFORMED_BOOK",
        ),
        (
            "truncated JSON",
            HttpResponse(200, _JSON_HEADERS, b"{", NOW),
            200,
            "application/json",
            "NOT_JSON",
            "MALFORMED_BOOK",
        ),
        (
            "empty body",
            HttpResponse(200, _JSON_HEADERS, b"", NOW),
            200,
            "application/json",
            "NOT_JSON",
            "MALFORMED_BOOK",
        ),
        (
            "non UTF-8 body",
            HttpResponse(200, _JSON_HEADERS, b"\xff\xfe\x00", NOW),
            200,
            "application/json",
            "NOT_JSON",
            "MALFORMED_BOOK",
        ),
        (
            "valid book encoded as UTF-16 is not UTF-8 JSON",
            HttpResponse(200, _JSON_HEADERS, json.dumps(book()).encode("utf-16"), NOW),
            200,
            "application/json",
            "NOT_JSON",
            "MALFORMED_BOOK",
        ),
        (
            "valid book behind a UTF-8 byte-order mark",
            HttpResponse(200, _JSON_HEADERS, b"\xef\xbb\xbf" + _VALID_BOOK, NOW),
            200,
            "application/json",
            "NOT_JSON",
            "MALFORMED_BOOK",
        ),
        (
            "excessive JSON depth",
            HttpResponse(200, _JSON_HEADERS, b"[" * 40 + b"]" * 40, NOW),
            200,
            "application/json",
            "NOT_JSON",
            "MALFORMED_BOOK",
        ),
        (
            "oversized",
            HttpResponse(200, _JSON_HEADERS, b"[]", NOW, oversized=True),
            200,
            "application/json",
            "OVERSIZED",
            "MALFORMED_BOOK",
        ),
        (
            "timeout with no bytes",
            TimeoutError(),
            None,
            None,
            "TIMEOUT",
            "PRECHECK_TIMEOUT",
        ),
        (
            "transport failure with no bytes",
            OSError("dns failed"),
            None,
            None,
            "TRANSPORT_ERROR",
            "BOOK_UNAVAILABLE",
        ),
        (
            "transport policy failure with no bytes",
            PublicDataError("unsafe peer"),
            None,
            None,
            "TRANSPORT_ERROR",
            "BOOK_UNAVAILABLE",
        ),
    ],
)
def test_finding_1_precheck_attempt_records_http_metadata_for_every_outcome(
    label: str,
    reply: object,
    status: int | None,
    content_type: str | None,
    transport_reason: str,
    reason: str | None,
) -> None:
    result = PolymarketClient(CannedTransport([[market()]], [reply])).poll(
        limit=1, now=NOW
    )
    item = result.rejections[0] if reason else result.eligible[0]
    attempt = item.precheck_attempt
    expected_body = (
        b"" if isinstance(reply, BaseException) else getattr(reply, "body", b"")
    )
    assert (item.reason if reason else None) == reason, label
    assert attempt.token_id == "yes-m1", label
    assert attempt.attempted_at == NOW, label
    assert attempt.status_code == status, label
    assert attempt.content_type == content_type, label
    assert attempt.transport_reason == transport_reason, label
    assert attempt.body == expected_body, label
    assert attempt.truncated is (transport_reason == "OVERSIZED"), label
    assert attempt.retrieved_at == (None if status is None else NOW), label


def test_finding_1_oversized_precheck_bytes_stay_bounded() -> None:
    from jev_trading_lab.polymarket.client import MAX_PUBLIC_BYTES

    body = b"x" * (MAX_PUBLIC_BYTES + 5)
    result = PolymarketClient(
        CannedTransport([[market()]], [HttpResponse(200, _JSON_HEADERS, body, NOW)])
    ).poll(limit=1, now=NOW)
    attempt = result.rejections[0].precheck_attempt
    assert attempt.transport_reason == "OVERSIZED"
    assert attempt.truncated is True
    assert attempt.body == body[:MAX_PUBLIC_BYTES]


def test_finding_1_pathologically_deep_public_json_is_a_typed_error_not_a_crash() -> (
    None
):
    deep = HttpResponse(200, _JSON_HEADERS, b"[" * 200_000 + b"]" * 200_000, NOW)
    candidate = (
        PolymarketClient(CannedTransport([[market()]], [book()]))
        .poll(limit=1, now=NOW)
        .eligible[0]
    )

    with pytest.raises(PublicDataError):
        PolymarketClient(CannedTransport([deep], [])).poll(limit=1, now=NOW)
    with pytest.raises(PublicDataError):
        PolymarketClient(CannedTransport([], [deep])).execution_book(candidate, now=NOW)


_NESTED = "[" * 100_000 + "]" * 100_000
_HOSTILE_STRINGS = [
    "1e999999999",
    "-1e999999999",
    "9" * 40,
    "0." + "0" * 60 + "1",
    "NaN",
    "Infinity",
    "",
    "0",
    "\ud800",
    "[" * 100_000 + "]" * 100_000,
]
_PROPERTY = settings(
    max_examples=250,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=list(HealthCheck),
)
_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**30), max_value=10**30),
    st.floats(),
    st.text(max_size=40),
    st.sampled_from(_HOSTILE_STRINGS),
)
_JSON = st.recursive(
    _SCALARS,
    lambda inner: st.one_of(
        st.lists(inner, max_size=4),
        st.dictionaries(st.text(max_size=8), inner, max_size=4),
    ),
    max_leaves=12,
)
_LEVELS = st.lists(
    st.fixed_dictionaries({"price": _SCALARS, "size": _SCALARS}), max_size=3
)
_HOSTILE_BOOKS = st.fixed_dictionaries(
    {
        "market": st.one_of(st.just("condition-m1"), _SCALARS),
        "asset_id": st.one_of(st.just("yes-m1"), _SCALARS),
        "timestamp": st.one_of(st.just("1791633600000"), _SCALARS),
        "bids": st.one_of(_LEVELS, _JSON),
        "asks": st.one_of(_LEVELS, _JSON),
    },
    optional={"hash": _SCALARS, "tick_size": _SCALARS, "extra": _JSON},
)
# Encoded-array fields are parsed before any other deep gate can reject the market.
_ENCODED_FIELDS = [
    "outcomes",
    "outcomePrices",
    "clobTokenIds",
    "volume24hr",
    "conditionId",
]
_TEXT_FIELDS = [
    "id",
    "question",
    "description",
    "rules",
    "resolutionSource",
    "tags",
    "category",
    "endDate",
]
_HOSTILE_VALUES = st.one_of(_JSON, st.sampled_from(_HOSTILE_STRINGS))


@pytest.mark.parametrize(
    ("description", "secret", "expected"),
    (
        (
            "The bearer token value is ValueSecret in the report.",
            "ValueSecret",
            "The [REDACTED] in the report.",
        ),
        (
            "The bearer token was PastSecret in the report.",
            "PastSecret",
            "The [REDACTED] in the report.",
        ),
        (
            "API key credential equals AlphabeticSecret!",
            "AlphabeticSecret",
            "[REDACTED]!",
        ),
        (
            "The ACCESS_TOKEN value = Mixed-123_Secret; retain evidence.",
            "Mixed-123_Secret",
            "The [REDACTED]; retain evidence.",
        ),
        (
            "The refresh-token credential: RefreshSecret.",
            "RefreshSecret",
            "The [REDACTED].",
        ),
        (
            "The vendorSessionTokenV4 was SessionSecret, per report.",
            "SessionSecret",
            "The [REDACTED], per report.",
        ),
        (
            "The client secret value equals ClientSecretValue?",
            "ClientSecretValue",
            "The [REDACTED]?",
        ),
        (
            "The ｂｅａｒｅｒ＿ｔｏｋｅｎ-v2 credential was UnicodeSecret.",
            "UnicodeSecret",
            "The [REDACTED].",
        ),
        (
            "The bearer_tokenVersion2 credential was VersionWordSecret.",
            "VersionWordSecret",
            "The [REDACTED].",
        ),
        (
            "The apiKey_version_2-v3 equals VersionChainSecret.",
            "VersionChainSecret",
            "The [REDACTED].",
        ),
        (
            "token value is GenericTokenSecret.",
            "GenericTokenSecret",
            "[REDACTED].",
        ),
        (
            "secret credential equals GenericSecretValue.",
            "GenericSecretValue",
            "[REDACTED].",
        ),
        (
            "key—value：GenericKeyValue.",
            "GenericKeyValue",
            "[REDACTED].",
        ),
        (
            "auth💥credential／GenericAuthValue.",
            "GenericAuthValue",
            "[REDACTED].",
        ),
        (
            "The API key is 'AlphabeticSecret' in the report.",
            "AlphabeticSecret",
            "The [REDACTED] in the report.",
        ),
        (
            'The client secret equals "DoubleQuotedSecret"; retain evidence.',
            "DoubleQuotedSecret",
            "The [REDACTED]; retain evidence.",
        ),
        (
            "Authorization: Bearer AlphabeticSecret, per report.",
            "AlphabeticSecret",
            "[REDACTED], per report.",
        ),
        (
            "Proxy-Authorization = Basic QWxwaGEvKz0=!",
            "QWxwaGEvKz0=",
            "[REDACTED]!",
        ),
        (
            "client_secret=abc:def/ghi next",
            "abc:def/ghi",
            "[REDACTED] next",
        ),
        (
            "Prefix: Ｖｅｎｄｏｒ－Ｃｌｉｅｎｔ＿Ｓｅｃｒｅｔ.v12\tvalue = "
            "Token-._~+/=: y",
            "Token-._~+/=:",
            "Prefix: [REDACTED] y",
        ),
        (
            "Bearer Direct-._~+/=:; Basic QWxwaGEvKz0=.",
            "Direct-._~+/=:",
            "[REDACTED]; [REDACTED].",
        ),
        (
            "api_key=FirstSecret, client_secret='Second/Secret+='; done.",
            "FirstSecret",
            "[REDACTED], [REDACTED]; done.",
        ),
    ),
)
def test_gamma_credential_assignment_redacts_semantic_variants(
    description: str, secret: str, expected: str
) -> None:
    sanitized = sanitized_gamma_identity(market(description=description))["description"]

    assert isinstance(sanitized, str)
    assert secret not in sanitized
    terminal = description[-1] if description[-1] in ".!?" else ""
    assert sanitized == "[REDACTED]" + terminal
    assert not contains_public_credential_semantics(sanitized)


@pytest.mark.parametrize(
    "description",
    (
        "The bearer token value is required for client setup.",
        "The api key was optional for client setup.",
        "The access token equals unavailable.",
        "The refresh token credential is documented in the guide.",
        "The client secret is configured for client setup.",
        "The session token value was needed during setup.",
        "The bearer token was unavailable during maintenance.",
        "The token is unavailable.",
        "Key value was required for setup.",
        "Secret credential equals documented in the guide.",
        "Auth credential is optional for clients.",
        "The token value remains unavailable.",
        "The secret credentialing is documented.",
    ),
)
def test_gamma_credential_assignment_preserves_harmless_near_misses(
    description: str,
) -> None:
    sanitized = sanitized_gamma_identity(market(description=description))["description"]

    assert sanitized == description


_CREDENTIAL_PHRASES = st.sampled_from(
    (
        "bearer token",
        "API_KEY-v2",
        "AccessTokenV3",
        "refresh-token",
        "vendorSessionToken_v4",
        "client secret",
    )
)
_CREDENTIAL_CONNECTORS = st.sampled_from(
    (" is ", " was ", " equals ", " value is ", " credential was ", ": ", " = ")
)
_CREDENTIAL_SECRET_VALUES = st.from_regex(
    r"[A-Za-z][A-Za-z0-9_-]{7,31}", fullmatch=True
).filter(lambda value: value.casefold() not in {"available", "documented", "required"})


@_PROPERTY
@given(_CREDENTIAL_PHRASES, _CREDENTIAL_CONNECTORS, _CREDENTIAL_SECRET_VALUES)
def test_gamma_credential_assignment_fuzz_never_retains_candidate(
    phrase: str, connector: str, secret: str
) -> None:
    description = f"Before. {phrase}{connector}{secret}! After."

    sanitized = sanitized_gamma_identity(market(description=description))["description"]

    assert isinstance(sanitized, str)
    assert secret not in sanitized
    assert sanitized == "Before. [REDACTED]! After."


@_PROPERTY
@given(
    _CREDENTIAL_PHRASES,
    _CREDENTIAL_CONNECTORS,
    st.from_regex(
        r"[A-Za-z][A-Za-z0-9._~+/=:-]{6,30}[A-Za-z0-9_~+/=:-]", fullmatch=True
    ),
    st.sampled_from(("", "Bearer ", "Basic ")),
    st.sampled_from(("", "'", '"')),
    st.sampled_from((".", ",", ";", "!", "?")),
)
def test_gamma_credential_assignment_fuzz_handles_token_punctuation_and_quotes(
    phrase: str,
    connector: str,
    secret: str,
    scheme: str,
    quote: str,
    boundary: str,
) -> None:
    description = (
        f"Before. {phrase}{connector}{quote}{scheme}{secret}{quote}{boundary} After."
    )

    sanitized = sanitized_gamma_identity(market(description=description))["description"]

    assert isinstance(sanitized, str)
    assert secret not in sanitized
    if boundary in ".!?":
        assert sanitized == f"Before. [REDACTED]{boundary} After."
    else:
        assert sanitized == "Before. [REDACTED]."


@pytest.mark.parametrize(
    ("description", "secret", "expected"),
    (
        (
            "Before. client_secret=abc$FULLSECRET; still secret! After.",
            "abc$FULLSECRET",
            "Before. [REDACTED]! After.",
        ),
        (
            "Before. client_secret=abcd%FULLSECRET; still secret! After.",
            "abcd%FULLSECRET",
            "Before. [REDACTED]! After.",
        ),
        (
            "Before. access token = «arbitrary🙂punctuation»! After.",
            "«arbitrary🙂punctuation»",
            "Before. [REDACTED]! After.",
        ),
        (
            "Before. refresh_token = 'odd|$%^&*秘密'! After.",
            "odd|$%^&*秘密",
            "Before. [REDACTED]! After.",
        ),
        (
            "Before. session token: value-with spaces and $punctuation! After.",
            "value-with spaces and $punctuation",
            "Before. [REDACTED]! After.",
        ),
        (
            "Before. password was any/value%at—all! After.",
            "any/value%at—all",
            "Before. [REDACTED]! After.",
        ),
        (
            "Before. private key = -----BEGIN秘密-----! After.",
            "-----BEGIN秘密-----",
            "Before. [REDACTED]! After.",
        ),
        (
            "Before. Authorization: Basic arbitrary$unicode秘密! After.",
            "arbitrary$unicode秘密",
            "Before. [REDACTED]! After.",
        ),
        (
            "Before. cookie = arbitrary$unicode秘密! After.",
            "arbitrary$unicode秘密",
            "Before. [REDACTED]! After.",
        ),
    ),
)
def test_gamma_credential_sentence_policy_redacts_whole_bounded_sentence(
    description: str, secret: str, expected: str
) -> None:
    sanitized = sanitized_gamma_identity(market(description=description))["description"]

    assert sanitized == expected
    assert secret not in str(sanitized)


@pytest.mark.parametrize(
    "description",
    (
        "The api key is required.",
        "The access token was optional.",
        "The refresh token is needed.",
        "The session token was documented.",
        "The bearer token is available.",
        "The client secret was unavailable.",
        "The password is configured.",
        "The private key was stored.",
    ),
)
def test_gamma_credential_sentence_policy_preserves_only_enumerated_predicates(
    description: str,
) -> None:
    assert (
        sanitized_gamma_identity(market(description=description))["description"]
        == description
    )


@_PROPERTY
@given(_HOSTILE_BOOKS)
def test_hardening_arbitrary_book_payloads_never_escape_the_shared_parsers(
    hostile: dict[str, Any],
) -> None:
    from jev_trading_lab.polymarket.client import (
        derive_precheck_rejection,
        derive_precheck_transport_reason,
        reconstruct_book_snapshot,
    )

    body = json.dumps(hostile).encode()
    transport = derive_precheck_transport_reason(
        status_code=200, content_type="application/json", truncated=False, body=body
    )
    derive_precheck_rejection(
        transport_reason=transport,
        body=body,
        market_id="m1",
        condition_id="condition-m1",
        token_id="yes-m1",
        now=NOW,
    )
    try:
        reconstruct_book_snapshot(body, market_id="m1")
    except PublicDataError:
        pass


@_PROPERTY
@given(st.binary(max_size=300))
def test_hardening_arbitrary_bytes_never_escape_the_precheck_derivations(
    body: bytes,
) -> None:
    from jev_trading_lab.polymarket.client import (
        derive_precheck_rejection,
        derive_precheck_transport_reason,
    )

    for status in (200, 503):
        transport = derive_precheck_transport_reason(
            status_code=status,
            content_type="application/json",
            truncated=False,
            body=body,
        )
        derive_precheck_rejection(
            transport_reason=transport,
            body=body,
            market_id="m1",
            condition_id="condition-m1",
            token_id="yes-m1",
            now=NOW,
        )


def _poll_never_raises(changes: dict[str, Any]) -> None:
    transport = CannedTransport([[market() | changes]], [book(), book()])
    try:
        PolymarketClient(transport).poll(limit=1, now=NOW)
    except PublicDataError:
        pass


@_PROPERTY
@given(
    st.fixed_dictionaries(
        {}, optional={field: _HOSTILE_VALUES for field in _ENCODED_FIELDS}
    )
)
def test_hardening_hostile_gamma_encoded_fields_are_rejected_never_raised(
    changes: dict[str, Any],
) -> None:
    _poll_never_raises(changes)


@_PROPERTY
@given(
    st.fixed_dictionaries(
        {}, optional={field: _HOSTILE_VALUES for field in _TEXT_FIELDS}
    )
)
def test_hardening_hostile_gamma_text_fields_are_rejected_never_raised(
    changes: dict[str, Any],
) -> None:
    _poll_never_raises(changes)


@pytest.mark.parametrize(
    "field", ["outcomes", "outcomePrices", "clobTokenIds"], ids=lambda value: value
)
def test_hardening_deeply_nested_gamma_encoded_arrays_are_malformed_markets(
    field: str,
) -> None:
    transport = CannedTransport([[market(**{field: _NESTED})]], [])

    result = PolymarketClient(transport).poll(limit=1, now=NOW)

    assert not result.eligible
    assert result.rejections[0].reason == "MALFORMED_MARKET"
    assert all("clob.polymarket.com" not in item.url for item in transport.requests)


def test_hardening_gamma_clob_identity_is_total_for_hostile_encoded_arrays() -> None:
    from jev_trading_lab.polymarket.client import gamma_clob_identity

    with pytest.raises(ValueError):
        gamma_clob_identity({"id": "m1", "clobTokenIds": _NESTED})


def test_hardening_lone_surrogate_gamma_text_is_a_malformed_market() -> None:
    raw = (
        json.dumps([market()]).encode().replace(b"Will m1 happen?", b"Will \\ud800 x?")
    )
    page = HttpResponse(200, _JSON_HEADERS, raw, NOW)

    result = PolymarketClient(CannedTransport([page], [])).poll(limit=1, now=NOW)

    assert not result.eligible
    assert result.rejections[0].reason == "MALFORMED_MARKET"
    assert result.rejections[0].raw_gamma_bytes.count(b"\\ud800") == 1


_HOSTILE_PARSER_SCRIPT = """
import json, sys
from datetime import UTC, datetime

from jev_trading_lab.polymarket.client import (
    PublicDataError,
    derive_precheck_rejection,
    reconstruct_book_snapshot,
)

book = {
    "market": "condition-m1",
    "asset_id": "yes-m1",
    "timestamp": "1791633600000",
    "bids": [{"price": "0.48", "size": "5000"}],
    "asks": [{"price": "0.51", "size": "5000"}],
}
book.update(json.loads(sys.argv[1]))
body = json.dumps(book).encode()
reason, snapshot = derive_precheck_rejection(
    transport_reason="OK",
    body=body,
    market_id="m1",
    condition_id="condition-m1",
    token_id="yes-m1",
    now=datetime(2026, 10, 10, 12, tzinfo=UTC),
)
try:
    reconstruct_book_snapshot(body, market_id="m1")
except PublicDataError:
    rebuilt = "rejected"
else:
    rebuilt = "accepted"
print(reason, snapshot is None, rebuilt)
"""


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"timestamp": "9" * 30}, id="timestamp-beyond-platform-time"),
        pytest.param({"timestamp": "9" * 5_000}, id="timestamp-digit-limit"),
        pytest.param({"timestamp": 10**40}, id="timestamp-integer-beyond-float"),
        pytest.param(
            {"bids": [{"price": "1e999999999", "size": "5000"}]},
            id="price-exponent-overflows-decimal-context",
        ),
        pytest.param(
            {"bids": [{"price": "0.48", "size": "1e999999999"}]},
            id="size-exponent-would-hang-integer-conversion",
        ),
        pytest.param(
            {"bids": [{"price": "0.48", "size": "1e5000"}]}, id="size-exponent-5000"
        ),
        pytest.param(
            {"asks": [{"price": "0.51", "size": "9" * 5_000}]}, id="size-digits-5000"
        ),
        pytest.param(
            {"bids": [{"price": "0." + "0" * 100_000 + "1", "size": "5000"}]},
            id="price-with-a-hundred-thousand-zeros",
        ),
    ],
)
def test_finding_1_hostile_book_numbers_are_malformed_never_hang_or_crash(
    changes: dict[str, Any],
) -> None:
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, "-B", "-c", _HOSTILE_PARSER_SCRIPT, json.dumps(changes)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr[-400:]
    assert completed.stdout.strip() == "MALFORMED_BOOK True rejected"


def test_finding_1_token_ids_beyond_the_artifact_bound_are_malformed_markets() -> None:
    from jev_trading_lab.polymarket.client import MAX_CLOB_TOKEN_ID_CHARS

    for token in ("9" * (MAX_CLOB_TOKEN_ID_CHARS + 1), "x" * 5_000):
        transport = CannedTransport(
            [[market(clobTokenIds=json.dumps([token, "no-token"]))]], []
        )
        result = PolymarketClient(transport).poll(limit=1, now=NOW)
        assert not result.eligible
        assert result.rejections[0].reason == "MALFORMED_MARKET"
        assert all(
            "clob.polymarket.com" not in request.url for request in transport.requests
        )
    boundary = "9" * MAX_CLOB_TOKEN_ID_CHARS
    result = PolymarketClient(
        CannedTransport(
            [[market(clobTokenIds=json.dumps([boundary, "no-token"]))]],
            [book(token=boundary)],
        )
    ).poll(limit=1, now=NOW)
    assert result.eligible[0].yes_token_id == boundary


def test_finding_1_replay_derivations_are_shared_pure_functions() -> None:
    from jev_trading_lab.polymarket import client as public

    assert (
        public.derive_precheck_transport_reason(
            status_code=503,
            content_type="application/json",
            truncated=False,
            body=_VALID_BOOK,
        )
        == "HTTP_STATUS"
    )
    outcome = public.derive_precheck_rejection(
        transport_reason="OK",
        body=_VALID_BOOK,
        market_id="m1",
        condition_id="condition-m1",
        token_id="yes-m1",
        now=NOW,
    )
    assert outcome[0] is None and outcome[1] is not None
    stale = public.derive_precheck_rejection(
        transport_reason="OK",
        body=_VALID_BOOK,
        market_id="m1",
        condition_id="condition-m1",
        token_id="yes-m1",
        now=NOW + timedelta(seconds=31),
    )
    assert stale[0] == "STALE_BOOK" and stale[1] is None
    skipped = public.derive_precheck_rejection(
        transport_reason="HTTP_STATUS",
        body=_VALID_BOOK,
        market_id="m1",
        condition_id="condition-m1",
        token_id="yes-m1",
        now=NOW,
    )
    assert skipped == ("MALFORMED_BOOK", None)


def _snapshot(bids: list[tuple[int, int]], asks: list[tuple[int, int]]) -> BookSnapshot:
    return BookSnapshot(
        market_id="m1",
        token_id="yes-m1",
        retrieved_at=NOW,
        source_sha256="0" * 64,
        bids=[BookLevel(price_micros=p, quantity=q) for p, q in bids],
        asks=[BookLevel(price_micros=p, quantity=q) for p, q in asks],
    )


@pytest.mark.parametrize(
    ("bids", "asks", "expected"),
    [
        ([(480_000, 5_000)], [(520_000, 5_000)], None),
        ([(480_000, 5_000)], [(520_001, 5_000)], "WIDE_SPREAD"),
        ([(480_000, 4_999)], [(500_000, 5_000)], "LOW_DEPTH"),
        ([(480_000, 5_000)], [(500_000, 4_999)], "LOW_DEPTH"),
        ([(480_000, 2_000), (460_000, 3_000)], [(500_000, 5_000)], None),
        ([(480_000, 2_000), (459_999, 3_000)], [(500_000, 5_000)], "LOW_DEPTH"),
        ([(480_000, 5_000)], [(500_000, 2_000), (520_000, 3_000)], None),
        ([(480_000, 5_000)], [(500_000, 2_000), (520_001, 3_000)], "LOW_DEPTH"),
        ([(480_000, 1)], [(530_000, 1)], "WIDE_SPREAD"),
    ],
)
def test_finding_2_central_precheck_eligibility_function_thresholds(
    bids: list[tuple[int, int]], asks: list[tuple[int, int]], expected: str | None
) -> None:
    from jev_trading_lab.polymarket.client import precheck_eligibility_reason

    assert precheck_eligibility_reason(_snapshot(bids, asks)) == expected


def test_finding_2_poll_delegates_to_the_central_eligibility_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jev_trading_lab.polymarket import client as public

    monkeypatch.setattr(
        public, "precheck_eligibility_reason", lambda _book: "LOW_DEPTH"
    )
    result = PolymarketClient(CannedTransport([[market()]], [book()])).poll(
        limit=1, now=NOW
    )
    assert not result.eligible
    assert result.rejections[0].reason == "LOW_DEPTH"
    assert result.rejections[0].precheck_book is not None


@pytest.mark.parametrize(
    "text",
    [
        "2026-10-12T12:00:00Z",
        "2026-10-12T12:00:00+00:00",
        "2026-10-12T12:00:00.000Z",
        "2026-10-12T12:00:00.000000+00:00",
    ],
)
def test_finding_5_gamma_utc_normalizer_accepts_z_and_zero_offset(text: str) -> None:
    from jev_trading_lab.polymarket import client as public

    parsed = public.normalize_gamma_utc(text)
    assert parsed == datetime(2026, 10, 12, 12, tzinfo=UTC)
    assert parsed.utcoffset() == timedelta(0)
    assert (
        public.normalize_gamma_identity({**market(), "endDate": text})["closes_at"]
        == "2026-10-12T12:00:00Z"
    )


def test_finding_5_gamma_utc_normalizer_keeps_fractional_seconds_canonical() -> None:
    from jev_trading_lab.polymarket import client as public

    assert (
        public.normalize_gamma_identity(
            {**market(), "endDate": "2026-10-12T12:00:00.5+00:00"}
        )["closes_at"]
        == "2026-10-12T12:00:00.500000Z"
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        5,
        b"2026-10-12T12:00:00Z",
        "",
        "2026-10-12",
        "2026-10-12T12:00:00",
        "2026-10-12T14:00:00+02:00",
        "2026-10-12T10:00:00-02:00",
        "2026-10-12T12:00:00-00:00",
        "2026-10-12T12:00:00+0000",
        "2026-10-12T12:00:00z",
        "2026-10-12 12:00:00Z",
        "2026-10-12T12:00:00Z\n",
        "٢٠٢٦-١٠-١٢T١٢:٠٠:٠٠Z",
        "2026-10-12T12:00:00.1234567Z",
        "2026-13-12T12:00:00Z",
        "2026-10-12T25:00:00Z",
        "not-a-date",
        " 2026-10-12T12:00:00Z",
        "2026-10-12T12:00:00Z ",
    ],
)
def test_finding_5_gamma_utc_normalizer_rejects_invalid_and_nonzero_offsets(
    value: object,
) -> None:
    from jev_trading_lab.polymarket import client as public

    with pytest.raises(ValueError):
        public.normalize_gamma_utc(value)
    with pytest.raises(ValueError):
        public.normalize_gamma_identity({**market(), "endDate": value})


@pytest.mark.parametrize(
    "end_date",
    ["2026-10-12T14:00:00+02:00", "2026-10-12", "garbage", None, 7],
)
def test_finding_5_poll_rejects_invalid_or_offset_end_date_as_malformed(
    end_date: object,
) -> None:
    result = PolymarketClient(CannedTransport([[market(endDate=end_date)]], [])).poll(
        limit=1, now=NOW
    )
    assert not result.eligible
    assert result.rejections[0].reason == "MALFORMED_MARKET"


@pytest.mark.parametrize(
    "end_date", ["2026-10-12T12:00:00+00:00", "2026-10-12T12:00:00.000Z"]
)
def test_finding_5_poll_evidence_uses_the_shared_normalized_close(
    end_date: str,
) -> None:
    result = PolymarketClient(
        CannedTransport([[market(endDate=end_date)]], [book()])
    ).poll(limit=1, now=NOW)
    assert result.eligible[0].evidence.closes_at == datetime(
        2026, 10, 12, 12, tzinfo=UTC
    )

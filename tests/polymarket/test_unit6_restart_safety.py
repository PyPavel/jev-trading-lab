from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jev_trading_lab.polymarket.call_journal import ProviderCallJournal
from jev_trading_lab.polymarket.client import (
    HttpRequest,
    HttpResponse,
    PolymarketClient,
    gamma_eligibility_reason,
    read_bounded_body,
    resolve_public_addresses,
)
from jev_trading_lab.polymarket.pipeline import observation_slot

NOW = datetime(2026, 10, 10, 12, 0, 45, tzinfo=UTC)


def _market(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "m1",
        "conditionId": "c1",
        "question": "Will the measure pass?",
        "description": "A public-policy measure.",
        "rules": "Resolves Yes if enacted.",
        "resolutionSource": "Official legislature record",
        "category": "Politics",
        "tags": "[]",
        "active": True,
        "closed": False,
        "endDate": "2026-10-11T12:00:00Z",
        "volume24hr": "10000",
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.49","0.51"]',
        "clobTokenIds": '["yes","no"]',
    }
    value.update(changes)
    return value


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"active": False}, "INACTIVE"),
        ({"closed": True}, "CLOSED"),
        ({"category": "Sports"}, "SPORTS"),
        ({"outcomes": '["A","B"]'}, "NON_BINARY"),
        ({"rules": ""}, "MISSING_EVIDENCE"),
        ({"endDate": "2026-10-10T12:30:00Z"}, "CLOSE_WINDOW"),
        ({"volume24hr": "9999.999"}, "LOW_VOLUME"),
        ({}, None),
    ],
)
def test_gamma_eligibility_has_one_normalized_reason_function(
    changes: dict[str, object], reason: str | None
) -> None:
    assert gamma_eligibility_reason(_market(**changes), cutoff=NOW) == reason


def test_gamma_eligibility_rejects_tampered_types() -> None:
    with pytest.raises(ValueError, match="Gamma eligibility input"):
        gamma_eligibility_reason(_market(active=1), cutoff=NOW)


def test_observation_slot_is_minute_stable_and_adjacent_minutes_are_distinct() -> None:
    first = observation_slot(NOW)
    same = observation_slot(NOW + timedelta(seconds=14))
    adjacent = observation_slot(NOW + timedelta(seconds=15))
    assert first == same == datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    assert adjacent == datetime(2026, 10, 10, 12, 1, tzinfo=UTC)


def test_precheck_freshness_uses_http_retrieved_at_inclusive() -> None:
    market = _market()
    gamma = json.dumps([market], separators=(",", ":")).encode()
    book_time = NOW - timedelta(seconds=30)
    book = json.dumps(
        {
            "market": "c1",
            "asset_id": "yes",
            "timestamp": str(int(book_time.timestamp() * 1000)),
            "bids": [{"price": "0.49", "size": "5000"}],
            "asks": [{"price": "0.51", "size": "5000"}],
        },
        separators=(",", ":"),
    ).encode()

    class Transport:
        def __call__(self, request: HttpRequest) -> HttpResponse:
            if "gamma-api" in request.url:
                return HttpResponse(
                    200, {"content-type": "application/json"}, gamma, NOW
                )
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                book,
                NOW,
            )

    result = PolymarketClient(Transport(), page_size=20, max_pages=1).poll(
        now=NOW - timedelta(minutes=10)
    )
    assert len(result.eligible) == 1
    assert result.eligible[0].precheck_attempt.retrieved_at == NOW


def test_provider_call_intent_is_durable_and_unknown_is_never_retryable(
    tmp_path: Path,
) -> None:
    root = tmp_path
    journal = ProviderCallJournal(root)
    call_id = journal.begin(
        provider="openai",
        opportunity_id="1" * 64,
        request_bytes=b'{"model":"gpt-5"}',
        created_at=NOW,
    )
    restarted = ProviderCallJournal(root)
    state = restarted.state(call_id)
    assert state is not None
    assert state["kind"] == "provider_call_intent"
    assert state["retryable"] is False
    assert (root / "provider_calls.jsonl").read_bytes().endswith(b"\n")


def test_provider_call_result_is_content_addressed_and_replayable(
    tmp_path: Path,
) -> None:
    root = tmp_path
    journal = ProviderCallJournal(root)
    call_id = journal.begin(
        provider="jev",
        opportunity_id="2" * 64,
        request_bytes=b"request",
        created_at=NOW,
    )
    journal.finish(
        call_id,
        outcome={"status": "success", "response_sha256": "3" * 64},
        finished_at=NOW + timedelta(seconds=1),
    )
    state = ProviderCallJournal(root).state(call_id)
    assert state is not None
    assert state["kind"] == "provider_call_result"
    assert state["outcome"] == {"response_sha256": "3" * 64, "status": "success"}
    artifact = root / state["storage_relpath"]
    assert artifact.read_bytes() == state["artifact_bytes"]


def test_full_body_deadline_rejects_slow_drip() -> None:
    ticks = iter([0.0, 0.4, 0.8, 1.2])

    class Response:
        def read(self, _size: int) -> bytes:
            return b"x"

    with pytest.raises(TimeoutError, match="deadline"):
        read_bounded_body(
            Response(), max_bytes=100, deadline=1.0, clock=lambda: next(ticks)
        )


def test_dns_saturation_stays_bounded_and_each_wait_obeys_its_deadline() -> None:
    release = threading.Event()

    def blocked_resolver(*_args: object) -> list[tuple[object, ...]]:
        release.wait(timeout=5)
        return []

    started = time.monotonic()
    try:
        for _ in range(8):
            with pytest.raises(TimeoutError, match="DNS"):
                resolve_public_addresses(
                    blocked_resolver,
                    "example.test",
                    deadline=time.monotonic() + 0.01,
                )
        dns_threads = [
            thread
            for thread in threading.enumerate()
            if thread.name.startswith("bounded-dns")
        ]
        assert len(dns_threads) <= 4
        assert time.monotonic() - started < 1
    finally:
        release.set()

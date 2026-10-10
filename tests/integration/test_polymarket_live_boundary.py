from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jev_trading_lab.polymarket.ai import TransportResponse
from jev_trading_lab.polymarket.client import (
    HttpRequest,
    HttpResponse,
    PolymarketClient,
)
from jev_trading_lab.polymarket.pipeline import observe_markets

FIXTURE = Path(__file__).parents[1] / "fixtures/polymarket/feasibility_cycle.json"
NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
FORBIDDEN = {
    "price",
    "prices",
    "outcomeprices",
    "token",
    "tokenid",
    "book",
    "bids",
    "asks",
}


@pytest.fixture(autouse=True)
def _private_store(monkeypatch: pytest.MonkeyPatch) -> None:
    from jev_trading_lab.polymarket import store

    defaults = dict(store.BlobStore.__init__.__kwdefaults__ or {})
    defaults["filesystem_resolver"] = lambda _path: "ext4"
    monkeypatch.setattr(store.BlobStore.__init__, "__kwdefaults__", defaults)


def _market() -> dict[str, Any]:
    return {
        "id": "live-m1",
        "conditionId": "condition-live-m1",
        "question": "Will the public event occur?",
        "description": "Public evidence describing the event.",
        "rules": "Resolve YES if the official publication confirms the event.",
        "resolutionSource": "Official public agency publication.",
        "endDate": (NOW + timedelta(days=2)).isoformat().replace("+00:00", "Z"),
        "active": True,
        "closed": False,
        "category": "Politics",
        "tags": ["Policy"],
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.45","0.55"]',
        "clobTokenIds": '["yes-live-m1","no-live-m1"]',
        "volume24hr": 20_000,
        "acceptingOrders": True,
    }


def _book(second: int) -> dict[str, Any]:
    at = NOW + timedelta(seconds=second)
    return {
        "market": "condition-live-m1",
        "asset_id": "yes-live-m1",
        "timestamp": str(int(at.timestamp() * 1000)),
        "bids": [{"price": "0.48", "size": "5000"}, {"price": "0.47", "size": "5000"}],
        "asks": [{"price": "0.51", "size": "5000"}, {"price": "0.52", "size": "5000"}],
    }


class PublicNetwork:
    def __init__(self) -> None:
        self.requests: list[HttpRequest] = []
        self.books = [_book(0), _book(3)]

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        body = [_market()] if "gamma-api" in request.url else self.books.pop(0)
        return HttpResponse(
            200,
            {"content-type": "application/json"},
            json.dumps(body, separators=(",", ":")).encode(),
            NOW,
        )


class ModelTransport:
    def __init__(
        self, provider: str, evidence_hash: str | None = None, fail: bool = False
    ) -> None:
        fixture = json.loads(FIXTURE.read_text())
        self.raw = fixture["model_artifacts"][f"{provider}_response_bytes"].encode()
        if provider == "openai" and evidence_hash is not None:
            envelope = json.loads(self.raw)
            output = next(
                item for item in envelope["output"] if item["type"] == "message"
            )
            brief = json.loads(output["content"][0]["text"])
            brief["evidence_sha256"] = evidence_hash
            for fact in brief["cited_facts"]:
                fact["evidence_sha256"] = evidence_hash
            output["content"][0]["text"] = json.dumps(
                brief, separators=(",", ":"), sort_keys=True
            )
            self.raw = json.dumps(
                envelope, separators=(",", ":"), sort_keys=True
            ).encode()
        self.fail = fail
        self.requests: list[bytes] = []

    def __call__(self, request: bytes) -> TransportResponse:
        self.requests.append(request)
        return TransportResponse(
            status_code=503 if self.fail else 200,
            headers={"content-type": "application/json"},
            raw_bytes=b'{"error":"unavailable"}' if self.fail else self.raw,
            retrieved_at=NOW
            + timedelta(seconds=1 if "openai" in request.decode() else 2),
            billed_cost_microdollars=17,
        )


def _assert_price_free(value: object) -> None:
    if isinstance(value, dict):
        assert not ({str(key).casefold().replace("_", "") for key in value} & FORBIDDEN)
        for nested in value.values():
            _assert_price_free(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_price_free(nested)


def test_observe_freezes_then_calls_models_then_fresh_book_and_records_both_arms(
    tmp_path: Path,
) -> None:
    public = PublicNetwork()
    client = PolymarketClient(public)
    raw_market = json.dumps(_market(), separators=(",", ":")).encode()
    openai = ModelTransport("openai", hashlib.sha256(raw_market).hexdigest())
    jev = ModelTransport("jev")
    events: list[str] = []
    summaries = observe_markets(
        limit=1,
        output_dir=tmp_path,
        client=client,
        openai_transport=openai,
        jev_transport=jev,
        now=NOW,
        event_sink=events.append,
    )
    assert len(summaries) == 1
    assert events.index("decision_frozen") < events.index("execution_book_requested")
    assert [request.method for request in public.requests] == ["GET", "GET", "GET"]
    assert all(
        "polymarket.com" not in request.decode()
        for request in openai.requests + jev.requests
    )
    assert all(request.body is None for request in public.requests)
    for request in openai.requests + jev.requests:
        _assert_price_free(json.loads(request))
    result = json.loads((tmp_path / "results.jsonl").read_text().strip())
    assert result["ai"]["initial_cash_microdollars"] == 10_000_000_000
    assert result["deterministic"]["initial_cash_microdollars"] == 10_000_000_000
    assert result["resolution"] is None
    assert result["ai"]["forecast_status"] == "PENDING_RESOLUTION"
    assert (tmp_path / "runtime-config.json").is_file()
    assert "Authorization" not in (tmp_path / "runtime-config.json").read_text()


def test_provider_failure_still_writes_synchronized_terminal_observation(
    tmp_path: Path,
) -> None:
    public = PublicNetwork()
    client = PolymarketClient(public)
    raw_market = json.dumps(_market(), separators=(",", ":")).encode()
    openai = ModelTransport("openai", hashlib.sha256(raw_market).hexdigest(), fail=True)
    jev = ModelTransport("jev")
    summaries = observe_markets(
        limit=1,
        output_dir=tmp_path,
        client=client,
        openai_transport=openai,
        jev_transport=jev,
        now=NOW,
    )
    assert summaries[0]["ai"]["disposition"] == "MODEL_FAILURE"
    assert summaries[0]["control"]["disposition"] == "ABSTAIN"
    assert len(openai.requests) == 1
    assert not jev.requests
    result = json.loads((tmp_path / "results.jsonl").read_text().strip())
    assert result["ai"]["model_cost_microdollars"] == 17
    assert result["deterministic"]["model_cost_microdollars"] == 0


def test_missing_live_configuration_is_sanitized_terminal_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
        "OPENAI_CACHED_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
        "OPENAI_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)
    summaries = observe_markets(
        limit=1,
        output_dir=tmp_path,
        client=PolymarketClient(PublicNetwork()),
        now=NOW,
    )
    assert summaries[0]["ai"]["disposition"] == "MODEL_FAILURE"
    persisted = (tmp_path / "results.jsonl").read_bytes() + (
        tmp_path / "artifacts.jsonl"
    ).read_bytes()
    blob_bytes = b"".join(
        path.read_bytes() for path in (tmp_path / "blobs").rglob("*") if path.is_file()
    )
    assert b'"code":"CONFIG_FAILURE"' in blob_bytes
    assert b"OPENAI_API_KEY" not in persisted + blob_bytes
    assert b"TYPESAFE_API_KEY" not in persisted + blob_bytes

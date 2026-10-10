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
from jev_trading_lab.polymarket.models import BookSnapshot
from jev_trading_lab.polymarket.pipeline import observe_markets
from jev_trading_lab.polymarket.store import StoreCorruptError, load_observation


def _corrupt_blob(output: Path, replay: Any, kind: str) -> None:
    blob = output / "blobs" / replay.envelopes[kind].storage_relpath
    blob.write_bytes(b"corrupt")


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
        self,
        provider: str,
        evidence_hash: str | None = None,
        fail: bool = False,
        cost: int = 17,
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
        self.cost = cost
        self.requests: list[bytes] = []

    def __call__(self, request: bytes) -> TransportResponse:
        self.requests.append(request)
        return TransportResponse(
            status_code=503 if self.fail else 200,
            headers={"content-type": "application/json"},
            raw_bytes=b'{"error":"unavailable"}' if self.fail else self.raw,
            retrieved_at=NOW
            + timedelta(seconds=1 if "openai" in request.decode() else 2),
            billed_cost_microdollars=self.cost,
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
    replay = load_observation(tmp_path, result["observation_id"])
    assert list(replay.artifacts)[-2:] == ["decision_frozen", "execution_book"]
    assert isinstance(replay.typed_artifacts["execution_book"], BookSnapshot)
    assert (
        replay.artifacts["execution_book"]
        == json.dumps(_book(3), separators=(",", ":")).encode()
    )
    assert replay.envelopes["execution_book"].sha256 == result["execution_book_sha256"]
    assert (
        replay.envelopes["decision_frozen"].retrieved_at
        < replay.envelopes["execution_book"].retrieved_at
    )
    assert not (tmp_path / "live-artifacts.jsonl").exists()


@pytest.mark.parametrize("provider", ["openai", "jev"])
def test_provider_failure_freezes_precheck_history_and_consumed_execution_book(
    tmp_path: Path, provider: str
) -> None:
    public = PublicNetwork()
    client = PolymarketClient(public)
    raw_market = json.dumps(_market(), separators=(",", ":")).encode()
    openai = ModelTransport(
        "openai",
        hashlib.sha256(raw_market).hexdigest(),
        fail=provider == "openai",
    )
    jev = ModelTransport("jev", fail=provider == "jev")
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
    result = json.loads((tmp_path / "results.jsonl").read_text().strip())
    replay = load_observation(tmp_path, result["observation_id"])
    kinds = list(replay.artifacts)
    assert kinds[2:4] == ["precheck_book", "causal_history"]
    assert kinds[-2:] == ["decision_frozen", "execution_book"]
    assert (
        replay.artifacts["precheck_book"]
        == json.dumps(_book(0), separators=(",", ":")).encode()
    )
    assert (
        replay.artifacts["execution_book"]
        == json.dumps(_book(3), separators=(",", ":")).encode()
    )
    assert replay.envelopes["execution_book"].sha256 == result["execution_book_sha256"]
    assert result["ai"]["model_cost_microdollars"] == 17 * (int(provider == "jev") + 1)
    assert result["deterministic"]["model_cost_microdollars"] == 0

    _corrupt_blob(tmp_path, replay, "execution_book")
    with pytest.raises(StoreCorruptError):
        load_observation(tmp_path, result["observation_id"])


def test_precheck_corruption_blocks_terminal_replay(tmp_path: Path) -> None:
    public = PublicNetwork()
    raw_market = json.dumps(_market(), separators=(",", ":")).encode()
    summary = observe_markets(
        limit=1,
        output_dir=tmp_path,
        client=PolymarketClient(public),
        openai_transport=ModelTransport(
            "openai", hashlib.sha256(raw_market).hexdigest()
        ),
        jev_transport=ModelTransport("jev"),
        now=NOW,
    )[0]
    replay = load_observation(tmp_path, str(summary["observation_id"]))
    history = json.loads(replay.artifacts["causal_history"])
    assert history == []
    _corrupt_blob(tmp_path, replay, "precheck_book")
    with pytest.raises(StoreCorruptError):
        load_observation(tmp_path, str(summary["observation_id"]))


@pytest.mark.parametrize(
    ("fresh", "reason"),
    [
        (
            {"timestamp": str(int((NOW - timedelta(seconds=31)).timestamp() * 1000))},
            "STALE_BOOK",
        ),
        ({"asks": [{"price": "0.47", "size": "5000"}]}, "CROSSED_BOOK"),
        ({"asset_id": "changed"}, "TOKEN_DRIFT"),
        (TimeoutError(), "BOOK_UNAVAILABLE"),
        (_book(1), "STALE_BOOK"),
    ],
)
def test_fresh_book_failures_terminalize_after_one_decision_barrier(
    tmp_path: Path, fresh: object, reason: str
) -> None:
    class FailingFreshBook(PublicNetwork):
        def __init__(self) -> None:
            super().__init__()
            self.books[1] = fresh

        def __call__(self, request: HttpRequest) -> HttpResponse:
            if "gamma-api" in request.url:
                return super().__call__(request)
            value = self.books.pop(0)
            if isinstance(value, BaseException):
                raise value
            if isinstance(value, dict) and set(value) != set(_book(0)):
                merged = _book(3)
                merged.update(value)
                value = merged
            self.requests.append(request)
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps(value, separators=(",", ":")).encode(),
                NOW,
            )

    public = FailingFreshBook()
    raw_market = json.dumps(_market(), separators=(",", ":")).encode()
    summaries = observe_markets(
        limit=1,
        output_dir=tmp_path,
        client=PolymarketClient(public),
        openai_transport=ModelTransport(
            "openai", hashlib.sha256(raw_market).hexdigest()
        ),
        jev_transport=ModelTransport("jev"),
        now=NOW,
    )
    result = json.loads((tmp_path / "results.jsonl").read_text())
    replay = load_observation(tmp_path, result["observation_id"])
    unavailable = json.loads(replay.artifacts["book_unavailable"])
    frozen = json.loads(replay.artifacts["decision_frozen"])
    assert summaries[0]["ai"]["disposition"] == "BOOK_FAILURE"
    assert unavailable["reason"] == reason
    assert unavailable["terminal_model_at"] == frozen["frozen_at"]
    assert unavailable["terminal_model_at"] < unavailable["planned_retrieved_at"]
    assert result["observed_at"] >= unavailable["terminal_model_at"]


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


def test_eligibility_rejection_is_canonical_terminal_observation(
    tmp_path: Path,
) -> None:
    rejected = _market()
    rejected["rules"] = ""

    class RejectedNetwork:
        def __call__(self, request: HttpRequest) -> HttpResponse:
            assert "gamma-api" in request.url
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps([rejected], separators=(",", ":")).encode(),
                NOW,
            )

    summaries = observe_markets(
        limit=1,
        output_dir=tmp_path,
        client=PolymarketClient(RejectedNetwork()),
        now=NOW,
    )
    result = json.loads((tmp_path / "results.jsonl").read_text())
    assert summaries[0]["terminal_status"] == "ELIGIBILITY_REJECTION"
    assert result["execution_book_sha256"] is None
    assert result.get("planned_execution_book_sha256") is None
    assert result["ai"]["disposition"] == "INELIGIBLE"
    assert result["deterministic"]["disposition"] == "INELIGIBLE"
    for arm in (result["ai"], result["deterministic"]):
        assert arm["quantity"] == 0
        assert arm["trading_pnl_microdollars"] == 0
        assert arm["economic_pnl_microdollars"] == 0
        assert arm["initial_cash_microdollars"] == 10_000_000_000
        assert arm["final_cash_microdollars"] == 10_000_000_000
        assert arm["model_cost_microdollars"] == 0
        assert arm["forecast_status"] == "UNAVAILABLE"
    kinds = [
        json.loads(line)["kind"]
        for line in (tmp_path / "artifacts.jsonl").read_text().splitlines()
    ]
    assert kinds == ["raw_gamma", "eligibility_rejection"]
    assert not (tmp_path / "rejections.jsonl").exists()


def test_cumulative_model_cost_overflow_is_barriered_without_fresh_book(
    tmp_path: Path,
) -> None:
    class FreshBookTrap(PublicNetwork):
        def __call__(self, request: HttpRequest) -> HttpResponse:
            if "clob.polymarket.com" in request.url and len(self.requests) >= 2:
                journal = tmp_path / "artifacts.jsonl"
                assert journal.is_file()
                assert json.loads(journal.read_text().splitlines()[-1])["kind"] == (
                    "decision_frozen"
                )
                raise AssertionError("cost overflow must not fetch an execution book")
            return super().__call__(request)

    public = FreshBookTrap()
    raw_market = json.dumps(_market(), separators=(",", ":")).encode()
    summaries = observe_markets(
        limit=1,
        output_dir=tmp_path,
        client=PolymarketClient(public),
        openai_transport=ModelTransport(
            "openai", hashlib.sha256(raw_market).hexdigest(), cost=2**63 - 1
        ),
        jev_transport=ModelTransport("jev", cost=1),
        now=NOW,
    )
    result = json.loads((tmp_path / "results.jsonl").read_text())
    assert summaries[0]["ai"]["disposition"] == "COST_OVERFLOW"
    assert len(public.requests) == 2
    assert result["execution_book_sha256"] is None
    assert result["baseline_probability_micros"] is None
    assert result["ai"]["model_cost_microdollars"] is None
    assert result["ai"]["economic_pnl_microdollars"] is None
    assert result["paired_economic_pnl_delta_microdollars"] is None
    replay = load_observation(tmp_path, result["observation_id"])
    assert list(replay.artifacts)[-2:] == ["decision_frozen", "book_unavailable"]
    barrier = json.loads(replay.artifacts["decision_frozen"])
    assert barrier["terminal_model_state"] == "COST_OVERFLOW"
    assert json.loads(replay.artifacts["book_unavailable"])["reason"] == "COST_OVERFLOW"
    assert "execution_book" not in replay.artifacts
    assert (
        json.loads(replay.artifacts["openai_success_metadata"])[
            "billed_cost_microdollars"
        ]
        == 2**63 - 1
    )
    assert (
        json.loads(replay.artifacts["jev_success_metadata"])["billed_cost_microdollars"]
        == 1
    )


def test_cost_overflow_crash_after_barrier_retries_without_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jev_trading_lab.polymarket import pipeline

    raw_market = json.dumps(_market(), separators=(",", ":")).encode()
    original = pipeline._finish_book_unavailable
    crashed = False

    def crash_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal crashed
        if not crashed:
            crashed = True
            kinds = [
                json.loads(line)["kind"]
                for line in (tmp_path / "artifacts.jsonl").read_text().splitlines()
            ]
            assert kinds[-1] == "decision_frozen"
            raise RuntimeError("simulated crash after overflow barrier")
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_finish_book_unavailable", crash_once)

    def kwargs() -> dict[str, Any]:
        return {
            "limit": 1,
            "output_dir": tmp_path,
            "client": PolymarketClient(PublicNetwork()),
            "openai_transport": ModelTransport(
                "openai", hashlib.sha256(raw_market).hexdigest(), cost=2**63 - 1
            ),
            "jev_transport": ModelTransport("jev", cost=1),
            "now": NOW,
        }

    with pytest.raises(RuntimeError, match="simulated crash"):
        observe_markets(**kwargs())
    assert not (tmp_path / "results.jsonl").read_text()

    summary = observe_markets(**kwargs())[0]
    replay = load_observation(tmp_path, str(summary["observation_id"]))
    assert replay.result.ai.disposition == "COST_OVERFLOW"
    assert "execution_book" not in replay.artifacts


@pytest.mark.parametrize("fresh_unavailable", [False, True])
def test_cumulative_cost_overflow_terminalizes_all_late_branches(
    tmp_path: Path, fresh_unavailable: bool
) -> None:
    public = PublicNetwork()
    if fresh_unavailable:
        public.books[1] = TimeoutError()
    raw_market = json.dumps(_market(), separators=(",", ":")).encode()
    summaries = observe_markets(
        limit=1,
        output_dir=tmp_path,
        client=PolymarketClient(public),
        openai_transport=ModelTransport(
            "openai", hashlib.sha256(raw_market).hexdigest(), cost=2**63 - 1
        ),
        jev_transport=ModelTransport("jev", fail=not fresh_unavailable, cost=1),
        now=NOW,
    )
    result = json.loads((tmp_path / "results.jsonl").read_text())
    assert summaries[0]["ai"]["disposition"] == "COST_OVERFLOW"
    assert result["ai"]["model_cost_microdollars"] is None
    assert result["ai"]["economic_pnl_microdollars"] is None
    assert result["ai"]["forecast_status"] == "UNAVAILABLE"

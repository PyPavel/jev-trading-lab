from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.persistence.blobs import BlobStore
from jev_trading_lab.polymarket.ai import (
    LiveHttpRequest,
    LiveHttpResponse,
    TransportResponse,
    live_jev_transport,
    live_openai_transport,
)
from jev_trading_lab.polymarket.call_journal import ProviderCallJournal
from jev_trading_lab.polymarket.client import (
    HttpRequest,
    HttpResponse,
    PolymarketClient,
    PublicDataTimeout,
)
from jev_trading_lab.polymarket.models import BookSnapshot
from jev_trading_lab.polymarket.paper import POLICY_VERSION
from jev_trading_lab.polymarket.pipeline import observe_markets
from jev_trading_lab.polymarket.store import (
    StoreCorruptError,
    StoreSecurityError,
    load_observation,
)


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
    def __init__(self, market: dict[str, Any] | None = None) -> None:
        self.requests: list[HttpRequest] = []
        self.books = [_book(0), _book(1), _book(3)]
        self.market = market or _market()

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        body = [self.market] if "gamma-api" in request.url else self.books.pop(0)
        retrieved_at = (
            NOW
            if isinstance(body, list)
            else datetime.fromtimestamp(int(body["timestamp"]) / 1000, UTC)
        )
        return HttpResponse(
            200,
            {"content-type": "application/json"},
            json.dumps(body, separators=(",", ":")).encode(),
            retrieved_at,
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
    assert [request.method for request in public.requests] == ["GET"] * 4
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
    assert not (tmp_path / "runtime-config.json").exists()
    replay = load_observation(tmp_path, result["observation_id"])
    assert list(replay.artifacts)[0] == "runtime_config"
    assert b"Authorization" not in replay.artifacts["runtime_config"]
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
    assert summaries[0]["control"]["disposition"] == "NO_EDGE"
    result = json.loads((tmp_path / "results.jsonl").read_text().strip())
    replay = load_observation(tmp_path, result["observation_id"])
    kinds = list(replay.artifacts)
    assert kinds[:7] == [
        "runtime_config",
        "raw_gamma",
        "sanitized_evidence",
        "precheck_book_raw",
        "precheck_book_http",
        "precheck_book",
        "causal_history",
    ]
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


def test_operator_summary_always_includes_known_and_deterministic_model_costs(
    tmp_path: Path,
) -> None:
    summary = _observe(tmp_path)[0]

    assert summary["ai"]["model_cost_microdollars"] == 34
    assert summary["control"]["model_cost_microdollars"] == 0


def test_live_equal_price_prechecks_freeze_two_points_and_enable_control(
    tmp_path: Path,
) -> None:
    summary = _observe(tmp_path)[0]
    replay = load_observation(tmp_path, str(summary["observation_id"]))
    history = json.loads(replay.artifacts["causal_history"])
    points = history["points"]

    assert len(points) == len(history["raw_books_b64"]) == 2
    assert points[0]["midpoint_micros"] == points[1]["midpoint_micros"]
    assert all(
        datetime.fromisoformat(point["observed_at"].replace("Z", "+00:00"))
        < replay.result.cutoff_at
        for point in points
    )
    assert summary["control"]["forecast_probability_micros"] is not None


def test_conflicting_duplicate_gamma_rows_record_one_replayable_terminal_rejection(
    tmp_path: Path,
) -> None:
    market = _market()
    conflicting = market | {"description": "Conflicting duplicate evidence."}

    class ConflictingPublic(PublicNetwork):
        def __call__(self, request: HttpRequest) -> HttpResponse:
            assert "gamma-api" in request.url
            self.requests.append(request)
            return HttpResponse(200, JSON_HEADERS, _wire([conflicting, market]), NOW)

    first = observe_markets(
        limit=2,
        output_dir=tmp_path,
        client=PolymarketClient(ConflictingPublic(market)),
        now=NOW,
    )
    second = observe_markets(
        limit=2,
        output_dir=tmp_path,
        client=PolymarketClient(ConflictingPublic(market)),
        now=NOW,
    )

    assert second == first
    assert len(first) == 1
    assert first[0]["reason"] == "DUPLICATE_MARKET_DATA"
    assert first[0]["terminal_status"] == "ELIGIBILITY_REJECTION"
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 1
    load_observation(tmp_path, str(first[0]["observation_id"]))


def test_duplicate_gamma_rows_produce_one_observation_and_one_provider_pair(
    tmp_path: Path,
) -> None:
    market = _market()

    class DuplicatePublic(PublicNetwork):
        def __call__(self, request: HttpRequest) -> HttpResponse:
            if "gamma-api" in request.url:
                self.requests.append(request)
                return HttpResponse(
                    200, JSON_HEADERS, _wire([market, dict(market)]), NOW
                )
            return super().__call__(request)

    public = DuplicatePublic(market)
    openai = ModelTransport("openai", _market_sha(market))
    jev = ModelTransport("jev")
    first = observe_markets(
        limit=2,
        output_dir=tmp_path,
        client=PolymarketClient(public),
        openai_transport=openai,
        jev_transport=jev,
        now=NOW,
    )
    restarted = observe_markets(
        limit=2,
        output_dir=tmp_path,
        client=PolymarketClient(DuplicatePublic(market)),
        openai_transport=ModelTransport("openai", _market_sha(market)),
        jev_transport=ModelTransport("jev"),
        now=NOW,
    )

    assert restarted == first
    assert len(first) == len({item["observation_id"] for item in first}) == 1
    assert len(openai.requests) == len(jev.requests) == 1


def test_public_poll_timeout_records_one_replayable_failure_without_downstream_calls(
    tmp_path: Path,
) -> None:
    class FailedPublic:
        def __init__(self) -> None:
            self.polls = 0
            self.books = 0

        def poll(self, **_kwargs: Any) -> object:
            self.polls += 1
            raise PublicDataTimeout(
                "Gamma transport timed out", reason="PUBLIC_DATA_TIMEOUT"
            )

        def execution_book(self, *_args: Any, **_kwargs: Any) -> object:
            self.books += 1
            raise AssertionError("execution book must not be requested")

    class ModelTrap:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, request_bytes: bytes) -> TransportResponse:
            del request_bytes
            self.calls += 1
            raise AssertionError("model provider must not be called")

    public = FailedPublic()
    openai = ModelTrap()
    jev = ModelTrap()
    first = observe_markets(
        limit=1,
        output_dir=tmp_path,
        client=public,  # type: ignore[arg-type]
        openai_transport=openai,
        jev_transport=jev,
        now=NOW,
    )
    replay_public = FailedPublic()
    second = observe_markets(
        limit=1,
        output_dir=tmp_path,
        client=replay_public,  # type: ignore[arg-type]
        openai_transport=ModelTrap(),
        jev_transport=ModelTrap(),
        now=NOW,
    )

    assert second == first
    assert len(first) == 1
    assert first[0]["reason"] == "PUBLIC_DATA_TIMEOUT"
    assert first[0]["terminal_status"] == "PUBLIC_DATA_FAILURE"
    assert public.polls == 1
    assert public.books == openai.calls == jev.calls == replay_public.polls == 0
    result = _single_result(tmp_path)
    replay = load_observation(tmp_path, result["observation_id"])
    failure = json.loads(replay.artifacts["raw_gamma"])
    assert failure == {
        "public_poll_failure": {"raw_response_b64": "", "reason": "PUBLIC_DATA_TIMEOUT"}
    }


def test_malformed_gamma_top_level_records_bounded_raw_failure(tmp_path: Path) -> None:
    raw = b'{"unexpected":"object"}'

    class MalformedPublic:
        def __init__(self) -> None:
            self.requests: list[HttpRequest] = []

        def __call__(self, request: HttpRequest) -> HttpResponse:
            self.requests.append(request)
            assert "gamma-api" in request.url
            return HttpResponse(200, JSON_HEADERS, raw, NOW)

    public = MalformedPublic()
    summaries = observe_markets(
        limit=1,
        output_dir=tmp_path,
        client=PolymarketClient(public),
        openai_transport=ModelTransport("openai"),
        jev_transport=ModelTransport("jev"),
        now=NOW,
    )

    assert len(summaries) == 1
    assert summaries[0]["reason"] == "MALFORMED_PUBLIC_DATA"
    assert summaries[0]["terminal_status"] == "PUBLIC_DATA_FAILURE"
    assert len(public.requests) == 1
    replay = load_observation(tmp_path, str(summaries[0]["observation_id"]))
    failure = json.loads(replay.artifacts["raw_gamma"])["public_poll_failure"]
    assert failure["reason"] == "MALFORMED_PUBLIC_DATA"
    assert base64.b64decode(failure["raw_response_b64"], validate=True) == raw


def test_distinct_malformed_rows_for_one_market_converge_and_replay_cleanly(
    tmp_path: Path,
) -> None:
    first = _market() | {"clobTokenIds": "not-json"}
    second = _market() | {"clobTokenIds": '["only-one"]'}

    class DuplicateMalformedMarket:
        def __call__(self, request: HttpRequest) -> HttpResponse:
            assert "gamma-api" in request.url
            return HttpResponse(200, JSON_HEADERS, _wire([second, first]), NOW)

    client = lambda: PolymarketClient(  # noqa: E731
        DuplicateMalformedMarket(), page_size=2, max_pages=1
    )
    observed = observe_markets(limit=2, output_dir=tmp_path, client=client(), now=NOW)
    replayed = observe_markets(limit=2, output_dir=tmp_path, client=client(), now=NOW)

    assert replayed == observed
    assert len(observed) == 1
    assert observed[0]["reason"] == "MALFORMED_MARKET"
    assert len({item["opportunity_id"] for item in observed}) == 1
    results = (tmp_path / "results.jsonl").read_text().splitlines()
    assert len(results) == 1
    assert json.loads(results[0])["market_id"] == "live-m1"
    load_observation(tmp_path, str(observed[0]["observation_id"]))


def test_duplicate_malformed_gamma_rows_converge_and_replay_cleanly(
    tmp_path: Path,
) -> None:
    class DuplicateMalformed:
        def __call__(self, request: HttpRequest) -> HttpResponse:
            assert "gamma-api" in request.url
            return HttpResponse(200, JSON_HEADERS, _wire([{"id": ""}, {"id": ""}]), NOW)

    client = lambda: PolymarketClient(  # noqa: E731
        DuplicateMalformed(), page_size=2, max_pages=1
    )
    first = observe_markets(limit=2, output_dir=tmp_path, client=client(), now=NOW)
    second = observe_markets(limit=2, output_dir=tmp_path, client=client(), now=NOW)

    assert second == first
    assert len(first) == len({item["observation_id"] for item in first}) == 1
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 1
    completed = json.loads((tmp_path / "completed_slots.jsonl").read_text())
    assert completed["observation_ids"] == [first[0]["observation_id"]]
    load_observation(tmp_path, str(first[0]["observation_id"]))


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
    assert len(history["points"]) == len(history["raw_books_b64"]) == 2
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
            self.books[2] = fresh  # type: ignore[assignment]

        def __call__(self, request: HttpRequest) -> HttpResponse:
            if "gamma-api" in request.url:
                return super().__call__(request)
            value = self.books.pop(0)
            if isinstance(value, BaseException):
                raise value
            partial = isinstance(value, dict) and set(value) != set(_book(0))
            if partial:
                merged = _book(3)
                merged.update(value)
                value = merged
            self.requests.append(request)
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps(value, separators=(",", ":")).encode(),
                datetime.fromtimestamp(int(value["timestamp"]) / 1000, UTC),
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


@pytest.mark.parametrize(
    "value",
    [" 1", "1 ", "1e3", "-1", "+1", "1.0", "1000000000001", "9223372036854775808"],
)
def test_invalid_billing_rate_fails_before_runtime_config_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    monkeypatch.setenv("OPENAI_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS", value)
    with pytest.raises(ValueError, match="runtime configuration is invalid"):
        observe_markets(
            limit=1,
            output_dir=tmp_path,
            client=PolymarketClient(PublicNetwork()),
            now=NOW,
        )
    assert not (tmp_path / "runtime-config.json").exists()


def test_runtime_config_normalizes_rates_and_rejects_secret_like_constants(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from jev_trading_lab.polymarket import pipeline

    monkeypatch.setenv("OPENAI_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS", "0001")
    config = json.loads(pipeline._runtime_config_bytes())
    assert (
        config["rates_microdollars_per_million_tokens"][
            "OPENAI_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS"
        ]
        == 1
    )

    sentinel = "sk-do-not-persist-sentinel"
    monkeypatch.setattr(pipeline, "OPENAI_MODEL_ID", sentinel)
    with pytest.raises(
        ValueError, match="runtime configuration is invalid"
    ) as captured:
        pipeline._runtime_config_bytes()
    assert sentinel not in str(captured.value)
    assert list(tmp_path.rglob("*")) == []


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


@pytest.mark.parametrize(
    ("reply", "reason", "expected_raw"),
    [
        (TimeoutError(), "PRECHECK_TIMEOUT", b""),
        (OSError("provider unavailable"), "BOOK_UNAVAILABLE", b""),
        (
            HttpResponse(200, {"content-type": "application/json"}, b"", NOW),
            "MALFORMED_BOOK",
            b"",
        ),
        (
            HttpResponse(200, {"content-type": "application/json"}, b"{", NOW),
            "MALFORMED_BOOK",
            b"{",
        ),
        (
            HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps(
                    _book(0)
                    | {
                        "timestamp": str(
                            int((NOW - timedelta(seconds=31)).timestamp() * 1000)
                        )
                    },
                    separators=(",", ":"),
                ).encode(),
                NOW,
            ),
            "STALE_BOOK",
            json.dumps(
                _book(0)
                | {
                    "timestamp": str(
                        int((NOW - timedelta(seconds=31)).timestamp() * 1000)
                    )
                },
                separators=(",", ":"),
            ).encode(),
        ),
        (
            HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps(
                    _book(0) | {"asset_id": "wrong-token"}, separators=(",", ":")
                ).encode(),
                NOW,
            ),
            "TOKEN_DRIFT",
            json.dumps(
                _book(0) | {"asset_id": "wrong-token"}, separators=(",", ":")
            ).encode(),
        ),
        (
            HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps(
                    _book(0) | {"market": "wrong-market"}, separators=(",", ":")
                ).encode(),
                NOW,
            ),
            "MARKET_DRIFT",
            json.dumps(
                _book(0) | {"market": "wrong-market"}, separators=(",", ":")
            ).encode(),
        ),
        (
            HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps(
                    _book(0) | {"asks": [{"price": "0.47", "size": "5000"}]},
                    separators=(",", ":"),
                ).encode(),
                NOW,
            ),
            "CROSSED_BOOK",
            json.dumps(
                _book(0) | {"asks": [{"price": "0.47", "size": "5000"}]},
                separators=(",", ":"),
            ).encode(),
        ),
    ],
)
def test_precheck_failure_is_content_addressed_terminal_and_idempotent(
    tmp_path: Path, reply: object, reason: str, expected_raw: bytes
) -> None:
    class FailedPrecheck:
        def __call__(self, request: HttpRequest) -> HttpResponse:
            if "gamma-api" in request.url:
                body = json.dumps([_market()], separators=(",", ":")).encode()
                return HttpResponse(
                    200, {"content-type": "application/json"}, body, NOW
                )
            if isinstance(reply, BaseException):
                raise reply
            assert isinstance(reply, HttpResponse)
            return reply

    for _ in range(2):
        summaries = observe_markets(
            limit=1,
            output_dir=tmp_path,
            client=PolymarketClient(FailedPrecheck()),
            now=NOW,
        )
        assert summaries[0]["reason"] == reason
    result = json.loads((tmp_path / "results.jsonl").read_text())
    replay = load_observation(tmp_path, result["observation_id"])
    assert list(replay.artifacts) == [
        "runtime_config",
        "raw_gamma",
        "sanitized_evidence",
        "precheck_book_raw",
        "precheck_book_http",
        "precheck_book_failure",
        "eligibility_rejection",
    ]
    assert replay.artifacts["precheck_book_raw"] == expected_raw
    failure = json.loads(replay.artifacts["precheck_book_failure"])
    assert failure["reason"] == reason
    assert failure["raw_book_sha256"] == hashlib.sha256(expected_raw).hexdigest()
    assert failure["raw_book_byte_length"] == len(expected_raw)
    assert failure["request_token_id"] == "yes-live-m1"
    assert failure["request_market_id"] == "live-m1"
    assert result["ai"]["disposition"] == "INELIGIBLE"
    assert result["deterministic"]["disposition"] == "INELIGIBLE"
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 1


def test_precheck_failure_replay_rejects_raw_corruption_and_reason_mismatch(
    tmp_path: Path,
) -> None:
    malformed = HttpResponse(200, {"content-type": "application/json"}, b"{", NOW)

    class FailedPrecheck:
        def __call__(self, request: HttpRequest) -> HttpResponse:
            if "gamma-api" in request.url:
                body = json.dumps([_market()], separators=(",", ":")).encode()
                return HttpResponse(
                    200, {"content-type": "application/json"}, body, NOW
                )
            return malformed

    summary = observe_markets(
        limit=1,
        output_dir=tmp_path,
        client=PolymarketClient(FailedPrecheck()),
        now=NOW,
    )[0]
    replay = load_observation(tmp_path, str(summary["observation_id"]))
    raw_path = (
        tmp_path / "blobs" / replay.envelopes["precheck_book_raw"].storage_relpath
    )
    original_raw = raw_path.read_bytes()
    raw_path.unlink()
    with pytest.raises(StoreCorruptError):
        load_observation(tmp_path, str(summary["observation_id"]))
    raw_path.write_bytes(original_raw)
    os.chmod(raw_path, 0o600)
    load_observation(tmp_path, str(summary["observation_id"]))

    _forge(
        tmp_path,
        str(summary["observation_id"]),
        "precheck_book_failure",
        _field(reason="STALE_BOOK"),
    )
    with pytest.raises(StoreCorruptError, match="precheck failure does not match"):
        load_observation(tmp_path, str(summary["observation_id"]))


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
    assert kinds == ["runtime_config", "raw_gamma", "eligibility_rejection"]
    assert not (tmp_path / "rejections.jsonl").exists()


@pytest.mark.parametrize(
    ("changes", "reason", "forged"),
    [
        ({"active": False}, "INACTIVE", "CLOSED"),
        ({"active": False}, "INACTIVE", "MALFORMED_MARKET"),
        ({"closed": True}, "CLOSED", "SPORTS"),
        ({"category": "Sports"}, "SPORTS", "NON_BINARY"),
        ({"outcomes": '["A","B"]'}, "NON_BINARY", "MISSING_EVIDENCE"),
        ({"rules": ""}, "MISSING_EVIDENCE", "CLOSE_WINDOW"),
        (
            {"endDate": (NOW + timedelta(minutes=30)).isoformat()},
            "CLOSE_WINDOW",
            "LOW_VOLUME",
        ),
        ({"volume24hr": "9999"}, "LOW_VOLUME", "INACTIVE"),
    ],
)
def test_gamma_rejection_replay_recomputes_exact_reason(
    tmp_path: Path, changes: dict[str, Any], reason: str, forged: str
) -> None:
    market = _market() | changes
    summary = _observe(tmp_path, market=market)[0]
    observation_id = str(summary["observation_id"])
    assert summary["reason"] == reason
    load_observation(tmp_path, observation_id)

    _forge(tmp_path, observation_id, "eligibility_rejection", _field(reason=forged))

    _assert_replay_rejected(
        tmp_path,
        observation_id,
        "eligibility rejection reason does not match frozen Gamma",
    )


def test_gamma_rejection_replay_binds_utc_spelling_and_exact_raw_bytes(
    tmp_path: Path,
) -> None:
    market = _market() | {
        "endDate": (NOW + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    }
    summary = _observe(tmp_path, market=market)[0]
    observation_id = str(summary["observation_id"])
    replacement = dict(market)
    replacement["endDate"] = str(replacement["endDate"]).replace("Z", "+00:00")
    replacement_bytes = _wire(replacement)
    replacement_sha = hashlib.sha256(replacement_bytes).hexdigest()

    _forge(tmp_path, observation_id, "raw_gamma", lambda _payload: replacement_bytes)
    _forge(
        tmp_path,
        observation_id,
        "eligibility_rejection",
        _field(raw_gamma_sha256=replacement_sha),
    )

    _assert_replay_rejected(
        tmp_path,
        observation_id,
        "artifact envelope does not match observation hash",
    )


def test_cumulative_model_cost_overflow_is_barriered_without_fresh_book(
    tmp_path: Path,
) -> None:
    class FreshBookTrap(PublicNetwork):
        def __call__(self, request: HttpRequest) -> HttpResponse:
            if "clob.polymarket.com" in request.url and len(self.requests) >= 3:
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
    assert len(public.requests) == 3
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
        public.books[2] = TimeoutError()  # type: ignore[assignment]
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


# --- Final Unit 6 adversarial review findings -------------------------------

JSON_HEADERS = {"content-type": "application/json"}


def _wire(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def _book_bytes(second: int = 0, **changes: Any) -> bytes:
    return _wire(_book(second) | changes)


def _market_sha(market: dict[str, Any] | None = None) -> str:
    return hashlib.sha256(_wire(market or _market())).hexdigest()


def _observe(
    output: Path,
    *,
    market: dict[str, Any] | None = None,
    public: Callable[[HttpRequest], HttpResponse] | None = None,
    openai: Any = None,
    jev: Any = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    market = market or _market()
    return observe_markets(
        limit=1,
        output_dir=output,
        client=PolymarketClient(public or PublicNetwork(market)),
        openai_transport=openai or ModelTransport("openai", _market_sha(market)),
        jev_transport=jev or ModelTransport("jev"),
        now=NOW,
        **kwargs,
    )


class ScriptedPrecheck:
    """Gamma succeeds; CLOB requests receive the scripted replies in order."""

    def __init__(
        self,
        reply: object,
        market: dict[str, Any] | None = None,
        *,
        then: tuple[HttpResponse, ...] = (),
    ) -> None:
        repeated = (
            HttpResponse(
                reply.status_code,
                reply.headers,
                reply.body,
                reply.retrieved_at + timedelta(seconds=1),
                reply.oversized,
            )
            if isinstance(reply, HttpResponse)
            else reply
        )
        self.replies: list[object] = [reply, repeated, *then]
        self.market = market or _market()
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if "gamma-api" in request.url:
            return HttpResponse(200, JSON_HEADERS, _wire([self.market]), NOW)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        assert isinstance(reply, HttpResponse)
        return reply


def _journal(output: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (output / "artifacts.jsonl").read_text().splitlines()
    ]


def _single_result(output: Path) -> dict[str, Any]:
    lines = (output / "results.jsonl").read_text().splitlines()
    assert len(lines) == 1
    value = json.loads(lines[0])
    assert isinstance(value, dict)
    return value


def _edit(update: Callable[[dict[str, Any]], object]) -> Callable[[bytes], bytes]:
    def rewrite(payload: bytes) -> bytes:
        value = json.loads(payload)
        update(value)
        return canonical_json_bytes(value)

    return rewrite


def _forge(
    output: Path,
    observation_id: str,
    kind: str,
    rewrite: Callable[[bytes], bytes],
) -> None:
    """Replace one stage with coherently re-addressed bytes and relink its chain."""

    entries = _journal(output)
    index = next(
        position
        for position, entry in enumerate(entries)
        if entry["observation_id"] == observation_id and entry["kind"] == kind
    )
    payload = (output / "blobs" / entries[index]["storage_relpath"]).read_bytes()
    with BlobStore(output / "blobs") as store:
        ref = store.publish(rewrite(payload))
    entries[index].update(
        sha256=ref.sha256, byte_size=ref.byte_length, storage_relpath=ref.relative_path
    )
    previous = entries[index]
    for later in entries[index + 1 :]:
        if later["observation_id"] != observation_id:
            continue
        later["predecessor_stage_sha256"] = hashlib.sha256(
            canonical_json_bytes(previous)
        ).hexdigest()
        previous = later
    journal = output / "artifacts.jsonl"
    journal.write_bytes(
        b"".join(canonical_json_bytes(entry) + b"\n" for entry in entries)
    )
    os.chmod(journal, 0o600)


def _forge_envelope_retrieved_at(
    output: Path, observation_id: str, kind: str, retrieved_at: datetime
) -> None:
    entries = _journal(output)
    index = next(
        position
        for position, entry in enumerate(entries)
        if entry["observation_id"] == observation_id and entry["kind"] == kind
    )
    entries[index]["retrieved_at"] = retrieved_at.isoformat().replace("+00:00", "Z")
    previous = entries[index]
    for later in entries[index + 1 :]:
        if later["observation_id"] != observation_id:
            continue
        later["predecessor_stage_sha256"] = hashlib.sha256(
            canonical_json_bytes(previous)
        ).hexdigest()
        previous = later
    journal = output / "artifacts.jsonl"
    journal.write_bytes(
        b"".join(canonical_json_bytes(entry) + b"\n" for entry in entries)
    )
    os.chmod(journal, 0o600)


def _error_text(error: BaseException) -> str:
    """The message of an exception and of every cause or context behind it."""

    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(str(current))
        current = current.__cause__ or current.__context__
    return " | ".join(parts)


def _assert_replay_rejected(
    output: Path,
    observation_id: str,
    why: str,
    errors: tuple[type[BaseException], ...] = (StoreCorruptError,),
) -> None:
    """Replay must fail closed for the stated reason, not merely fail."""

    with pytest.raises(errors) as captured:
        load_observation(output, observation_id)
    assert why in _error_text(captured.value), _error_text(captured.value)


_VALID_PRECHECK_BODY = _book_bytes()
_STALE_PRECHECK_BODY = _book_bytes(
    timestamp=str(int((NOW - timedelta(seconds=31)).timestamp() * 1000))
)
_WIDE_ASKS = [{"price": "0.53", "size": "5000"}, {"price": "0.54", "size": "5000"}]
_THIN_BIDS = [{"price": "0.48", "size": "2000"}, {"price": "0.47", "size": "2999"}]
_PRECHECK_REPLIES = [
    pytest.param(
        TimeoutError(),
        "PRECHECK_TIMEOUT",
        "TIMEOUT",
        None,
        None,
        b"",
        id="timeout-no-bytes",
    ),
    pytest.param(
        OSError("provider unavailable"),
        "BOOK_UNAVAILABLE",
        "TRANSPORT_ERROR",
        None,
        None,
        b"",
        id="transport-no-bytes",
    ),
    pytest.param(
        HttpResponse(503, JSON_HEADERS, _VALID_PRECHECK_BODY, NOW),
        "MALFORMED_BOOK",
        "HTTP_STATUS",
        503,
        "application/json",
        _VALID_PRECHECK_BODY,
        id="503-with-valid-json-book",
    ),
    pytest.param(
        HttpResponse(
            200, {"content-type": "text/html; charset=utf-8"}, _VALID_PRECHECK_BODY, NOW
        ),
        "MALFORMED_BOOK",
        "CONTENT_TYPE",
        200,
        "text/html",
        _VALID_PRECHECK_BODY,
        id="wrong-content-type-with-valid-body",
    ),
    pytest.param(
        HttpResponse(200, {}, _VALID_PRECHECK_BODY, NOW),
        "MALFORMED_BOOK",
        "CONTENT_TYPE",
        200,
        None,
        _VALID_PRECHECK_BODY,
        id="missing-content-type",
    ),
    pytest.param(
        HttpResponse(302, {"location": "https://evil.example"}, b"", NOW),
        "MALFORMED_BOOK",
        "REDIRECT",
        302,
        None,
        b"",
        id="redirect",
    ),
    pytest.param(
        HttpResponse(200, JSON_HEADERS, b"", NOW),
        "MALFORMED_BOOK",
        "NOT_JSON",
        200,
        "application/json",
        b"",
        id="empty-body",
    ),
    pytest.param(
        HttpResponse(200, JSON_HEADERS, b"{", NOW),
        "MALFORMED_BOOK",
        "NOT_JSON",
        200,
        "application/json",
        b"{",
        id="malformed-json",
    ),
    pytest.param(
        HttpResponse(200, JSON_HEADERS, b"\xff\xfe\x00", NOW),
        "MALFORMED_BOOK",
        "NOT_JSON",
        200,
        "application/json",
        b"\xff\xfe\x00",
        id="non-utf8-body",
    ),
    pytest.param(
        HttpResponse(200, JSON_HEADERS, b"[]", NOW, oversized=True),
        "MALFORMED_BOOK",
        "OVERSIZED",
        200,
        "application/json",
        b"[]",
        id="oversized",
    ),
    pytest.param(
        HttpResponse(200, JSON_HEADERS, _book_bytes(timestamp="9" * 30), NOW),
        "MALFORMED_BOOK",
        "OK",
        200,
        "application/json",
        _book_bytes(timestamp="9" * 30),
        id="timestamp-beyond-platform-time",
    ),
    pytest.param(
        HttpResponse(
            200,
            JSON_HEADERS,
            _book_bytes(bids=[{"price": "1e999999999", "size": "5000"}]),
            NOW,
        ),
        "MALFORMED_BOOK",
        "OK",
        200,
        "application/json",
        _book_bytes(bids=[{"price": "1e999999999", "size": "5000"}]),
        id="price-exponent-overflows-decimal-context",
    ),
    pytest.param(
        HttpResponse(200, JSON_HEADERS, _book_bytes().decode().encode("utf-16"), NOW),
        "MALFORMED_BOOK",
        "NOT_JSON",
        200,
        "application/json",
        _book_bytes().decode().encode("utf-16"),
        id="valid-book-encoded-as-utf16",
    ),
    pytest.param(
        HttpResponse(200, JSON_HEADERS, _STALE_PRECHECK_BODY, NOW),
        "STALE_BOOK",
        "OK",
        200,
        "application/json",
        _STALE_PRECHECK_BODY,
        id="stale-book",
    ),
    pytest.param(
        HttpResponse(200, JSON_HEADERS, _book_bytes(asset_id="wrong-token"), NOW),
        "TOKEN_DRIFT",
        "OK",
        200,
        "application/json",
        _book_bytes(asset_id="wrong-token"),
        id="token-drift",
    ),
    pytest.param(
        HttpResponse(200, JSON_HEADERS, _book_bytes(market="wrong-market"), NOW),
        "MARKET_DRIFT",
        "OK",
        200,
        "application/json",
        _book_bytes(market="wrong-market"),
        id="market-drift",
    ),
    pytest.param(
        HttpResponse(
            200,
            JSON_HEADERS,
            _book_bytes(asks=[{"price": "0.47", "size": "5000"}]),
            NOW,
        ),
        "CROSSED_BOOK",
        "OK",
        200,
        "application/json",
        _book_bytes(asks=[{"price": "0.47", "size": "5000"}]),
        id="crossed-book",
    ),
]
_FAILURE_ARTIFACT_KINDS = [
    "runtime_config",
    "raw_gamma",
    "sanitized_evidence",
    "precheck_book_raw",
    "precheck_book_http",
    "precheck_book_failure",
    "eligibility_rejection",
]


@pytest.mark.parametrize(
    ("reply", "reason", "transport_reason", "status", "content_type", "raw"),
    _PRECHECK_REPLIES,
)
def test_finding_1_precheck_http_metadata_is_persisted_replayed_and_idempotent(
    tmp_path: Path,
    reply: object,
    reason: str,
    transport_reason: str,
    status: int | None,
    content_type: str | None,
    raw: bytes,
) -> None:
    for _ in range(2):
        summaries = observe_markets(
            limit=1,
            output_dir=tmp_path,
            client=PolymarketClient(ScriptedPrecheck(reply)),
            now=NOW,
        )
        assert summaries[0]["reason"] == reason
    result = _single_result(tmp_path)
    replay = load_observation(tmp_path, result["observation_id"])

    assert list(replay.artifacts) == _FAILURE_ARTIFACT_KINDS
    assert replay.artifacts["precheck_book_raw"] == raw
    http = json.loads(replay.artifacts["precheck_book_http"])
    assert http == {
        "attempted_at": "2026-10-10T12:00:00Z",
        "content_type": content_type,
        "request_host": "clob.polymarket.com",
        "request_path": "/book",
        "request_token_id": "yes-live-m1",
        "response_byte_length": len(raw),
        "response_sha256": hashlib.sha256(raw).hexdigest(),
        "response_truncated": transport_reason == "OVERSIZED",
        "retrieved_at": None if status is None else "2026-10-10T12:00:00Z",
        "status_code": status,
        "transport_reason": transport_reason,
    }
    failure = json.loads(replay.artifacts["precheck_book_failure"])
    assert failure["reason"] == reason
    assert failure["http_sha256"] == replay.envelopes["precheck_book_http"].sha256
    assert failure["raw_book_sha256"] == http["response_sha256"]
    assert failure["raw_book_byte_length"] == len(raw)
    assert failure["transport_failure"] is (status is None)
    assert failure["request_token_id"] == "yes-live-m1"
    assert failure["request_market_id"] == "live-m1"
    assert result["ai"]["disposition"] == result["deterministic"]["disposition"]
    assert result["ai"]["disposition"] == "INELIGIBLE"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"asks": _WIDE_ASKS}, "WIDE_SPREAD"),
        ({"bids": _THIN_BIDS}, "LOW_DEPTH"),
        ({}, None),
    ],
)
def test_finding_1_valid_precheck_responses_also_persist_http_metadata(
    tmp_path: Path, changes: dict[str, Any], reason: str | None
) -> None:
    body = _book_bytes(**changes)
    headers = {"content-type": "application/json; charset=utf-8"}
    public = ScriptedPrecheck(
        HttpResponse(200, headers, body, NOW),
        then=(HttpResponse(200, JSON_HEADERS, _book_bytes(3), NOW),),
    )
    summary = _observe(tmp_path, public=public)[0]
    replay = load_observation(tmp_path, str(summary["observation_id"]))
    kinds = list(replay.artifacts)

    prefix = [
        "runtime_config",
        "raw_gamma",
        "sanitized_evidence",
        "precheck_book_raw",
        "precheck_book_http",
        "precheck_book",
    ]
    assert kinds[: len(prefix)] == prefix
    assert (kinds[-1] == "eligibility_rejection") == (reason is not None)
    http = json.loads(replay.artifacts["precheck_book_http"])
    assert http["status_code"] == 200
    assert http["content_type"] == "application/json"
    assert http["transport_reason"] == "OK"
    assert (
        http["response_sha256"]
        == hashlib.sha256(replay.artifacts["precheck_book_raw"]).hexdigest()
    )
    assert http["request_token_id"] == "yes-live-m1"
    assert summary.get("reason") == reason


_HTTP_503 = HttpResponse(503, JSON_HEADERS, _VALID_PRECHECK_BODY, NOW)
_HTTP_WRONG_TYPE = HttpResponse(
    200, {"content-type": "text/html"}, _VALID_PRECHECK_BODY, NOW
)


def _field(**changes: Any) -> Callable[[bytes], bytes]:
    return _edit(lambda value: value.update(changes))


_TRANSPORT_MISMATCH = "precheck transport reason does not match the frozen response"
_HTTP_BINDING = "precheck HTTP metadata does not bind the frozen request and bytes"
_HTTP_ROUTE = "precheck request must name the exact CLOB book route"
_HTTP_INCONSISTENT = "precheck HTTP metadata is inconsistent"
_FAILURE_MISMATCH = "precheck failure does not match the recomputed response outcome"
_REJECTION_BINDING = "eligibility rejection does not bind its precheck book"
_EXTRA_FIELD = "Extra inputs are not permitted"
_FORGED_PRECHECK_WHY = {
    "status-503-forged-200": _TRANSPORT_MISMATCH,
    "transport-reason-forged-ok": _TRANSPORT_MISMATCH,
    "transport-reason-swapped": _TRANSPORT_MISMATCH,
    "truncation-forged": _TRANSPORT_MISMATCH,
    "response-hash-forged": _HTTP_BINDING,
    "response-length-forged": _HTTP_BINDING,
    "request-token-forged": _HTTP_BINDING,
    "request-host-forged": _HTTP_ROUTE,
    "request-path-forged": _HTTP_ROUTE,
    "attempt-time-forged": _HTTP_BINDING,
    "retrieval-erased": _HTTP_INCONSISTENT,
    "status-erased": _HTTP_INCONSISTENT,
    "extra-field": _EXTRA_FIELD,
    "content-type-forged-json": _TRANSPORT_MISMATCH,
    "failure-reason-forged": _FAILURE_MISMATCH,
    "failure-reason-swapped-to-timeout": _FAILURE_MISMATCH,
    "failure-metadata-binding-forged": _FAILURE_MISMATCH,
    "failure-metadata-binding-erased": _FAILURE_MISMATCH,
    "failure-transport-flag-forged": _FAILURE_MISMATCH,
    "failure-token-forged": "precheck failure metadata does not bind rejection",
    "rejection-reason-forged": _FAILURE_MISMATCH,
    "rejection-binding-forged": _REJECTION_BINDING,
    "raw-bytes-replaced": _REJECTION_BINDING,
}


@pytest.mark.parametrize(
    ("reply", "kind", "rewrite"),
    [
        pytest.param(
            _HTTP_503,
            "precheck_book_http",
            _field(status_code=200),
            id="status-503-forged-200",
        ),
        pytest.param(
            _HTTP_503,
            "precheck_book_http",
            _field(transport_reason="OK"),
            id="transport-reason-forged-ok",
        ),
        pytest.param(
            _HTTP_503,
            "precheck_book_http",
            _field(transport_reason="CONTENT_TYPE"),
            id="transport-reason-swapped",
        ),
        pytest.param(
            _HTTP_503,
            "precheck_book_http",
            _field(response_truncated=True),
            id="truncation-forged",
        ),
        pytest.param(
            _HTTP_503,
            "precheck_book_http",
            _field(response_sha256="0" * 64),
            id="response-hash-forged",
        ),
        pytest.param(
            _HTTP_503,
            "precheck_book_http",
            _field(response_byte_length=1),
            id="response-length-forged",
        ),
        pytest.param(
            _HTTP_503,
            "precheck_book_http",
            _field(request_token_id="other-token"),
            id="request-token-forged",
        ),
        pytest.param(
            _HTTP_503,
            "precheck_book_http",
            _field(request_host="evil.example"),
            id="request-host-forged",
        ),
        pytest.param(
            _HTTP_503,
            "precheck_book_http",
            _field(request_path="/orders"),
            id="request-path-forged",
        ),
        pytest.param(
            _HTTP_503,
            "precheck_book_http",
            _field(attempted_at="2026-10-10T13:00:00Z"),
            id="attempt-time-forged",
        ),
        pytest.param(
            _HTTP_503,
            "precheck_book_http",
            _field(retrieved_at=None),
            id="retrieval-erased",
        ),
        pytest.param(
            _HTTP_503,
            "precheck_book_http",
            _field(status_code=None),
            id="status-erased",
        ),
        pytest.param(
            _HTTP_503,
            "precheck_book_http",
            _field(unexpected="field"),
            id="extra-field",
        ),
        pytest.param(
            _HTTP_WRONG_TYPE,
            "precheck_book_http",
            _field(content_type="application/json"),
            id="content-type-forged-json",
        ),
        pytest.param(
            _HTTP_503,
            "precheck_book_failure",
            _field(reason="STALE_BOOK"),
            id="failure-reason-forged",
        ),
        pytest.param(
            _HTTP_503,
            "precheck_book_failure",
            _field(reason="PRECHECK_TIMEOUT"),
            id="failure-reason-swapped-to-timeout",
        ),
        pytest.param(
            _HTTP_503,
            "precheck_book_failure",
            _field(http_sha256="0" * 64),
            id="failure-metadata-binding-forged",
        ),
        pytest.param(
            _HTTP_503,
            "precheck_book_failure",
            _field(http_sha256=None),
            id="failure-metadata-binding-erased",
        ),
        pytest.param(
            _HTTP_503,
            "precheck_book_failure",
            _field(transport_failure=True),
            id="failure-transport-flag-forged",
        ),
        pytest.param(
            _HTTP_503,
            "precheck_book_failure",
            _field(request_token_id="other"),
            id="failure-token-forged",
        ),
        pytest.param(
            _HTTP_503,
            "eligibility_rejection",
            _field(reason="STALE_BOOK"),
            id="rejection-reason-forged",
        ),
        pytest.param(
            _HTTP_503,
            "eligibility_rejection",
            _field(precheck_book_sha256="0" * 64),
            id="rejection-binding-forged",
        ),
        pytest.param(
            _HTTP_503,
            "precheck_book_raw",
            lambda _payload: _book_bytes(1),
            id="raw-bytes-replaced",
        ),
    ],
)
def test_finding_1_replay_rejects_coherently_forged_precheck_metadata(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    reply: HttpResponse,
    kind: str,
    rewrite: Callable[[bytes], bytes],
) -> None:
    summary = observe_markets(
        limit=1,
        output_dir=tmp_path,
        client=PolymarketClient(ScriptedPrecheck(reply)),
        now=NOW,
    )[0]
    observation_id = str(summary["observation_id"])
    assert load_observation(tmp_path, observation_id).result.ai.disposition == (
        "INELIGIBLE"
    )

    _forge(tmp_path, observation_id, kind, rewrite)

    _assert_replay_rejected(
        tmp_path, observation_id, _FORGED_PRECHECK_WHY[request.node.callspec.id]
    )


def test_finding_1_replay_rejects_raw_bytes_swapped_without_updating_http_metadata(
    tmp_path: Path,
) -> None:
    summary = observe_markets(
        limit=1,
        output_dir=tmp_path,
        client=PolymarketClient(ScriptedPrecheck(_HTTP_503)),
        now=NOW,
    )[0]
    observation_id = str(summary["observation_id"])
    other = _book_bytes(1)
    digest = hashlib.sha256(other).hexdigest()

    _forge(tmp_path, observation_id, "precheck_book_raw", lambda _payload: other)
    _forge(
        tmp_path,
        observation_id,
        "precheck_book_failure",
        _field(raw_book_sha256=digest, raw_book_byte_length=len(other)),
    )
    _forge(
        tmp_path,
        observation_id,
        "eligibility_rejection",
        _field(precheck_book_sha256=digest),
    )

    _assert_replay_rejected(tmp_path, observation_id, _HTTP_BINDING)


def test_replay_recomputes_precheck_age_from_response_retrieved_at(
    tmp_path: Path,
) -> None:
    summary = _observe(tmp_path)[0]
    observation_id = str(summary["observation_id"])
    delayed = NOW + timedelta(seconds=31)

    _forge(
        tmp_path,
        observation_id,
        "precheck_book_http",
        _field(retrieved_at=delayed.isoformat().replace("+00:00", "Z")),
    )
    _forge_envelope_retrieved_at(
        tmp_path, observation_id, "precheck_book_http", delayed
    )

    _assert_replay_rejected(
        tmp_path,
        observation_id,
        "precheck book does not match the recomputed response outcome",
    )


@pytest.mark.parametrize(
    ("changes", "reason", "forged"),
    [
        ({"asks": _WIDE_ASKS}, "WIDE_SPREAD", "LOW_DEPTH"),
        ({"bids": _THIN_BIDS}, "LOW_DEPTH", "WIDE_SPREAD"),
        ({"bids": _THIN_BIDS}, "LOW_DEPTH", "STALE_BOOK"),
        ({"asks": _WIDE_ASKS}, "WIDE_SPREAD", "MALFORMED_BOOK"),
    ],
)
def test_finding_2_replay_recomputes_precheck_rejection_reason(
    tmp_path: Path, changes: dict[str, Any], reason: str, forged: str
) -> None:
    reply = HttpResponse(200, JSON_HEADERS, _book_bytes(**changes), NOW)
    summary = observe_markets(
        limit=1,
        output_dir=tmp_path,
        client=PolymarketClient(ScriptedPrecheck(reply)),
        now=NOW,
    )[0]
    assert summary["reason"] == reason
    observation_id = str(summary["observation_id"])
    load_observation(tmp_path, observation_id)

    _forge(tmp_path, observation_id, "eligibility_rejection", _field(reason=forged))

    _assert_replay_rejected(
        tmp_path,
        observation_id,
        "eligibility rejection reason does not match the precheck book",
    )


def _rewrite_precheck_book(output: Path, observation_id: str, body: bytes) -> None:
    """Coherently swap the exact precheck bytes and every hash that names them."""

    digest = hashlib.sha256(body).hexdigest()
    kinds = {
        entry["kind"]
        for entry in _journal(output)
        if entry["observation_id"] == observation_id
    }
    for kind in ("precheck_book_raw", "precheck_book"):
        _forge(output, observation_id, kind, lambda _payload: body)
    if "precheck_book_http" in kinds:
        _forge(
            output,
            observation_id,
            "precheck_book_http",
            _field(response_sha256=digest, response_byte_length=len(body)),
        )
    if "eligibility_rejection" in kinds:
        _forge(
            output,
            observation_id,
            "eligibility_rejection",
            _field(precheck_book_sha256=digest),
        )


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ({"asks": _WIDE_ASKS}, {}),
        ({}, {"asks": _WIDE_ASKS}),
        ({}, {"bids": _THIN_BIDS}),
    ],
)
def test_finding_2_replay_rejects_coherent_book_that_contradicts_eligibility(
    tmp_path: Path, original: dict[str, Any], replacement: dict[str, Any]
) -> None:
    rejected = bool(original)
    reply = HttpResponse(200, JSON_HEADERS, _book_bytes(**original), NOW)
    public: Any = ScriptedPrecheck(reply) if rejected else PublicNetwork()
    summary = _observe(tmp_path, public=public)[0]
    observation_id = str(summary["observation_id"])
    assert (summary.get("reason") is not None) == rejected
    load_observation(tmp_path, observation_id)

    _rewrite_precheck_book(tmp_path, observation_id, _book_bytes(**replacement))

    _assert_replay_rejected(
        tmp_path,
        observation_id,
        "eligibility rejection reason does not match the precheck book"
        if rejected
        else "precheck book does not match the recomputed response outcome",
    )


@pytest.mark.parametrize("depth", [1, 2])
def test_finding_3_symlink_output_ancestor_is_rejected_with_zero_mutation(
    tmp_path: Path, depth: int
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    output = link.joinpath(*(["nested"] * (depth - 1)), "out")
    public = ScriptedPrecheck(OSError("must not be reached"))

    with pytest.raises(StoreSecurityError):
        observe_markets(
            limit=1, output_dir=output, client=PolymarketClient(public), now=NOW
        )

    assert list(outside.rglob("*")) == []
    assert public.requests == []


@pytest.mark.parametrize(
    "value",
    [" 1", "1 ", "1e3", "-1", "+1", "1.0", "1000000000001", "9223372036854775808"],
)
def test_finding_3_invalid_runtime_configuration_fails_before_any_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    monkeypatch.setenv("OPENAI_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS", value)
    output = tmp_path / "fresh" / "out"
    public = PublicNetwork()

    with pytest.raises(ValueError, match="runtime configuration is invalid"):
        observe_markets(
            limit=1, output_dir=output, client=PolymarketClient(public), now=NOW
        )

    assert not (tmp_path / "fresh").exists()
    assert public.requests == []


def test_finding_3_runtime_config_is_a_content_addressed_replay_bound_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS", "0001")
    summary = _observe(tmp_path)[0]
    replay = load_observation(tmp_path, str(summary["observation_id"]))

    assert list(replay.artifacts)[0] == "runtime_config"
    payload = replay.artifacts["runtime_config"]
    envelope = replay.envelopes["runtime_config"]
    assert envelope.sha256 == hashlib.sha256(payload).hexdigest()
    assert (tmp_path / "blobs" / envelope.storage_relpath).read_bytes() == payload
    config = json.loads(payload)
    assert payload == canonical_json_bytes(config)
    assert config == {
        "endpoints": {
            "clob": "https://clob.polymarket.com/book",
            "gamma": "https://gamma-api.polymarket.com/markets",
            "jev": "https://api.typesafe.ai/v1/systemone",
            "openai": "https://api.openai.com/v1/responses",
        },
        "models": {"jev": "jev-1.13.0", "openai": "gpt-5-2025-08-07"},
        "policy_version": "polymarket-feasibility-v1",
        "rates_microdollars_per_million_tokens": {
            "OPENAI_CACHED_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS": None,
            "OPENAI_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS": 1,
            "OPENAI_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS": None,
            "TYPESAFE_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS": None,
            "TYPESAFE_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS": None,
        },
    }
    assert not (tmp_path / "runtime-config.json").exists()
    config_hashes = {
        entry["sha256"]
        for entry in _journal(tmp_path)
        if entry["kind"] == "runtime_config"
    }
    assert config_hashes == {envelope.sha256}


_FORGED_CONFIG_WHY = {
    "endpoint-forged": "runtime endpoints differ from the approved routes",
    "endpoint-added": _EXTRA_FIELD,
    "model-forged": "runtime models differ from the approved snapshots",
    "policy-forged": "runtime policy differs from the approved policy",
    "field-added": _EXTRA_FIELD,
    "rate-negative": "Input should be greater than or equal to 0",
    "rate-too-large": "Input should be less than or equal to 1000000000000",
    "rate-name-added": "runtime billing rates must name the exact rate set",
    "rate-name-removed": "runtime billing rates must name the exact rate set",
    "non-canonical-encoding": "artifact must use canonical JSON",
    "credential-like-value": "artifact contains credential-like data",
}


@pytest.mark.parametrize(
    "rewrite",
    [
        pytest.param(
            _edit(
                lambda v: v["endpoints"].update(gamma="https://evil.example/markets")
            ),
            id="endpoint-forged",
        ),
        pytest.param(
            _edit(lambda v: v["endpoints"].update(extra="https://evil.example")),
            id="endpoint-added",
        ),
        pytest.param(
            _edit(lambda v: v["models"].update(openai="gpt-other")), id="model-forged"
        ),
        pytest.param(
            _edit(lambda v: v.update(policy_version="other-policy")), id="policy-forged"
        ),
        pytest.param(_edit(lambda v: v.update(extra=1)), id="field-added"),
        pytest.param(
            _edit(
                lambda v: v["rates_microdollars_per_million_tokens"].update(
                    OPENAI_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS=-1
                )
            ),
            id="rate-negative",
        ),
        pytest.param(
            _edit(
                lambda v: v["rates_microdollars_per_million_tokens"].update(
                    OPENAI_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS=10**12 + 1
                )
            ),
            id="rate-too-large",
        ),
        pytest.param(
            _edit(
                lambda v: v["rates_microdollars_per_million_tokens"].update(
                    UNKNOWN_RATE=1
                )
            ),
            id="rate-name-added",
        ),
        pytest.param(
            _edit(
                lambda v: v["rates_microdollars_per_million_tokens"].pop(
                    "TYPESAFE_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS"
                )
            ),
            id="rate-name-removed",
        ),
        pytest.param(
            lambda payload: json.dumps(json.loads(payload), indent=1).encode(),
            id="non-canonical-encoding",
        ),
        pytest.param(
            _edit(lambda v: v["models"].update(jev="sk-do-not-persist-sentinel")),
            id="credential-like-value",
        ),
    ],
)
def test_finding_3_replay_rejects_forged_runtime_config(
    request: pytest.FixtureRequest, tmp_path: Path, rewrite: Callable[[bytes], bytes]
) -> None:
    summary = _observe(tmp_path)[0]
    observation_id = str(summary["observation_id"])
    load_observation(tmp_path, observation_id)

    _forge(tmp_path, observation_id, "runtime_config", rewrite)

    _assert_replay_rejected(
        tmp_path,
        observation_id,
        _FORGED_CONFIG_WHY[request.node.callspec.id],
        (StoreCorruptError, StoreSecurityError),
    )


def _lock_is_held(path: Path) -> bool:
    if not path.exists():
        return False
    descriptor = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def test_finding_4_observe_lock_spans_polling_providers_and_terminal_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from jev_trading_lab.polymarket import pipeline

    lock = tmp_path / "observe.lock"
    held: list[tuple[str, bool]] = []

    class ProbedPublic(PublicNetwork):
        def __call__(self, request: HttpRequest) -> HttpResponse:
            held.append((request.url.split("?")[0], _lock_is_held(lock)))
            return super().__call__(request)

    class ProbedModel(ModelTransport):
        def __call__(self, request: bytes) -> TransportResponse:
            held.append(("model", _lock_is_held(lock)))
            return super().__call__(request)

    original = pipeline.record_observation

    def probed_record(*args: Any, **kwargs: Any) -> Any:
        held.append(("record", _lock_is_held(lock)))
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "record_observation", probed_record)
    market = _market()
    _observe(
        tmp_path,
        public=ProbedPublic(market),
        openai=ProbedModel("openai", _market_sha(market)),
        jev=ProbedModel("jev"),
    )

    assert held[0][0] == "https://gamma-api.polymarket.com/markets"
    assert all(flag for _label, flag in held), held
    assert {label for label, _flag in held} >= {
        "https://gamma-api.polymarket.com/markets",
        "https://clob.polymarket.com/book",
        "model",
        "record",
    }
    assert not _lock_is_held(lock)
    metadata = os.stat(lock)
    assert stat.S_ISREG(metadata.st_mode)
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_nlink == 1


@pytest.mark.parametrize("attack", ["symlink", "fifo", "hardlink", "weak_mode"])
def test_finding_4_unsafe_lock_file_is_rejected_before_polling(
    tmp_path: Path, attack: str
) -> None:
    output = tmp_path / "out"
    output.mkdir()
    os.chmod(output, 0o700)
    lock = output / "observe.lock"
    target = tmp_path / "target"
    if attack == "symlink":
        target.write_bytes(b"")
        lock.symlink_to(target)
    elif attack == "fifo":
        os.mkfifo(lock, 0o600)
    elif attack == "hardlink":
        target.write_bytes(b"")
        os.chmod(target, 0o600)
        os.link(target, lock)
    else:
        lock.write_bytes(b"")
        os.chmod(lock, 0o644)
    public = PublicNetwork()

    with pytest.raises(StoreSecurityError):
        _observe(output, public=public)

    assert public.requests == []
    assert not (output / "results.jsonl").exists()
    if attack == "symlink":
        assert target.read_bytes() == b""


def test_finding_4_second_identical_observe_replays_terminal_without_provider_calls(
    tmp_path: Path,
) -> None:
    market = _market()
    first = _observe(tmp_path, market=market)
    openai = ModelTransport("openai", _market_sha(market))
    jev = ModelTransport("jev")
    public = PublicNetwork(market)

    second = _observe(tmp_path, public=public, openai=openai, jev=jev)

    assert second == first
    assert openai.requests == [] and jev.requests == []
    assert public.requests == []
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 1


def test_finding_4_adjacent_observation_slots_poll_and_record_distinct_opportunities(
    tmp_path: Path,
) -> None:
    market = _market()
    first = _observe(tmp_path, market=market)[0]

    class AdjacentPublic(PublicNetwork):
        def __call__(self, request: HttpRequest) -> HttpResponse:
            response = super().__call__(request)
            body = response.body
            if "clob.polymarket.com" in request.url:
                value = json.loads(body)
                value["timestamp"] = str(int(value["timestamp"]) + 60_000)
                body = json.dumps(value, separators=(",", ":")).encode()
            return HttpResponse(
                response.status_code,
                response.headers,
                body,
                response.retrieved_at + timedelta(minutes=1),
            )

    public = AdjacentPublic(market)

    second = observe_markets(
        limit=1,
        output_dir=tmp_path,
        client=PolymarketClient(public),
        openai_transport=ModelTransport("openai", _market_sha(market)),
        jev_transport=ModelTransport("jev"),
        now=NOW + timedelta(minutes=1),
    )[0]

    assert len(public.requests) == 4
    assert second["opportunity_id"] != first["opportunity_id"]


def test_provider_call_journal_is_replay_bound_and_tamper_evident(
    tmp_path: Path,
) -> None:
    summary = _observe(tmp_path)[0]
    lines = [
        json.loads(line)
        for line in (tmp_path / "provider_calls.jsonl").read_text().splitlines()
    ]
    result = next(line for line in lines if line["kind"] == "provider_call_result")
    artifact = tmp_path / result["storage_relpath"]
    artifact.write_bytes(artifact.read_bytes() + b" ")

    with pytest.raises(StoreCorruptError, match="provider call"):
        load_observation(tmp_path, str(summary["observation_id"]))


@pytest.mark.parametrize("provider", ["openai", "jev"])
def test_timeout_after_durable_intent_keeps_unknown_cost_and_never_replays_call(
    tmp_path: Path, provider: str
) -> None:
    class TimeoutTransport(ModelTransport):
        def __call__(self, request: bytes) -> TransportResponse:
            self.requests.append(request)
            raise TimeoutError("ambiguous timeout")

    market = _market()
    openai = (
        TimeoutTransport("openai", _market_sha(market))
        if provider == "openai"
        else ModelTransport("openai", _market_sha(market))
    )
    jev = TimeoutTransport("jev") if provider == "jev" else ModelTransport("jev")
    first = _observe(tmp_path, market=market, openai=openai, jev=jev)[0]

    replay_openai = ModelTransport("openai", _market_sha(market))
    replay_jev = ModelTransport("jev")
    second = _observe(tmp_path, market=market, openai=replay_openai, jev=replay_jev)[0]

    assert second == first
    assert first["ai"]["disposition"] == "UNKNOWN_IN_FLIGHT"
    assert first["ai"]["model_cost_microdollars"] is None
    assert first["ai"]["economic_pnl_microdollars"] is None
    assert first["paired_economic_pnl_delta_microdollars"] is None
    assert replay_openai.requests == [] and replay_jev.requests == []


def test_crash_after_provider_intent_never_retries_unknown_call(tmp_path: Path) -> None:
    def crash(event: str) -> None:
        if event == "openai_intent_frozen":
            raise RuntimeError("failpoint after provider intent")

    with pytest.raises(RuntimeError, match="failpoint"):
        _observe(tmp_path, event_sink=crash)

    openai = ModelTransport("openai", _market_sha())
    jev = ModelTransport("jev")
    summary = _observe(tmp_path, openai=openai, jev=jev)[0]
    assert openai.requests == [] and jev.requests == []
    assert summary["ai"]["disposition"] == "UNKNOWN_IN_FLIGHT"
    assert summary["ai"]["model_cost_microdollars"] is None
    assert summary["ai"]["economic_pnl_microdollars"] is None
    assert summary["paired_economic_pnl_delta_microdollars"] is None
    replay = load_observation(tmp_path, summary["observation_id"])
    failure = json.loads(replay.artifacts["openai_failure"])
    assert failure["code"] == "UNKNOWN_IN_FLIGHT"


def test_completed_slot_replay_terminalizes_other_same_slot_pending_intent(
    tmp_path: Path,
) -> None:
    first_market = _market()
    second_market = first_market | {
        "id": "live-m2",
        "conditionId": "condition-live-m2",
        "question": "Will the second public event occur?",
        "clobTokenIds": '["yes-live-m2","no-live-m2"]',
        "endDate": (NOW + timedelta(days=3)).isoformat().replace("+00:00", "Z"),
    }

    class TwoMarketPublic:
        def __init__(self) -> None:
            self.requests: list[HttpRequest] = []
            self.book_numbers: dict[str, int] = {}

        def __call__(self, request: HttpRequest) -> HttpResponse:
            self.requests.append(request)
            if "gamma-api" in request.url:
                return HttpResponse(
                    200, JSON_HEADERS, _wire([first_market, second_market]), NOW
                )
            token = "yes-live-m2" if "yes-live-m2" in request.url else "yes-live-m1"
            number = self.book_numbers.get(token, 0)
            self.book_numbers[token] = number + 1
            market_id = "live-m2" if token.endswith("m2") else "live-m1"
            book = _book(number if number < 2 else 3) | {
                "market": f"condition-{market_id}",
                "asset_id": token,
            }
            retrieved_at = datetime.fromtimestamp(int(book["timestamp"]) / 1000, UTC)
            return HttpResponse(200, JSON_HEADERS, _wire(book), retrieved_at)

    first = observe_markets(
        limit=1,
        output_dir=tmp_path,
        client=PolymarketClient(TwoMarketPublic(), page_size=2, max_pages=1),
        openai_transport=ModelTransport("openai", _market_sha(first_market)),
        jev_transport=ModelTransport("jev"),
        now=NOW,
    )
    assert len(first) == 1

    def crash(event: str) -> None:
        if event == "openai_intent_frozen":
            raise RuntimeError("second market provider intent frozen")

    with pytest.raises(RuntimeError, match="second market"):
        observe_markets(
            limit=2,
            output_dir=tmp_path,
            client=PolymarketClient(TwoMarketPublic(), page_size=2, max_pages=1),
            openai_transport=ModelTransport("openai", _market_sha(second_market)),
            jev_transport=ModelTransport("jev"),
            now=NOW,
            event_sink=crash,
        )

    class NoCalls:
        def __init__(self) -> None:
            self.requests: list[object] = []

        def __call__(self, request: object) -> Any:
            self.requests.append(request)
            raise AssertionError("completed-slot recovery must not call transports")

    public = NoCalls()
    recovered = observe_markets(
        limit=1,
        output_dir=tmp_path,
        client=PolymarketClient(public),
        now=NOW,
    )

    assert len(recovered) == 2
    assert len({item["observation_id"] for item in recovered}) == 2
    assert recovered[0] == first[0]
    assert recovered[1]["ai"]["disposition"] == "UNKNOWN_IN_FLIGHT"
    assert recovered[1]["ai"]["model_cost_microdollars"] is None
    assert public.requests == []
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 2

    completed = [
        json.loads(line)
        for line in (tmp_path / "completed_slots.jsonl").read_text().splitlines()
    ]
    assert completed[-1]["limit"] == 1
    assert completed[-1]["observation_ids"] == [
        item["observation_id"] for item in recovered
    ]

    replay_public = NoCalls()
    replay_openai = NoCalls()
    replay_jev = NoCalls()
    assert (
        observe_markets(
            limit=1,
            output_dir=tmp_path,
            client=PolymarketClient(replay_public),
            openai_transport=replay_openai,
            jev_transport=replay_jev,
            now=NOW,
        )
        == recovered
    )
    assert replay_public.requests == replay_openai.requests == replay_jev.requests == []


def test_poll_failure_cannot_complete_slot_before_pending_provider_recovery(
    tmp_path: Path,
) -> None:
    market = _market()

    def crash(event: str) -> None:
        if event == "openai_intent_frozen":
            raise RuntimeError("failpoint after provider intent")

    with pytest.raises(RuntimeError, match="failpoint"):
        _observe(tmp_path, market=market, event_sink=crash)

    journal = ProviderCallJournal(tmp_path)
    opportunity_id = hashlib.sha256(
        canonical_json_bytes(
            {
                "market_id": market["id"],
                "policy_version": POLICY_VERSION,
                "slot_at": NOW.isoformat().replace("+00:00", "Z"),
            }
        )
    ).hexdigest()
    intent = journal.lookup(opportunity_id, "openai")
    assert intent is not None and intent["kind"] == "provider_call_intent"

    class FailedGamma(PublicNetwork):
        def __call__(self, request: HttpRequest) -> HttpResponse:
            if "gamma-api" in request.url:
                self.requests.append(request)
                return HttpResponse(500, JSON_HEADERS, b'{"error":"failed"}', NOW)
            return super().__call__(request)

    public = FailedGamma(market)
    openai = ModelTransport("openai", _market_sha(market))
    jev = ModelTransport("jev")
    summary = _observe(tmp_path, market=market, public=public, openai=openai, jev=jev)[
        0
    ]

    assert summary["opportunity_id"] == opportunity_id
    assert summary["ai"]["disposition"] == "UNKNOWN_IN_FLIGHT"
    assert summary["ai"]["model_cost_microdollars"] is None
    assert summary["ai"]["economic_pnl_microdollars"] is None
    assert summary["paired_economic_pnl_delta_microdollars"] is None
    assert len(public.requests) == 1
    assert openai.requests == [] and jev.requests == []
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 1
    result = journal.lookup(opportunity_id, "openai")
    assert result is not None and result["kind"] == "provider_call_result"
    assert result["outcome"]["code"] == "UNKNOWN_IN_FLIGHT"
    assert result["outcome"]["billed_cost_microdollars"] is None

    replay_public = FailedGamma(market)
    replay_openai = ModelTransport("openai", _market_sha(market))
    replay_jev = ModelTransport("jev")
    again = _observe(
        tmp_path,
        market=market,
        public=replay_public,
        openai=replay_openai,
        jev=replay_jev,
    )
    assert again == [summary]
    assert replay_public.requests == []
    assert replay_openai.requests == [] and replay_jev.requests == []


def test_missing_market_cannot_strand_unknown_in_flight_provider_cost(
    tmp_path: Path,
) -> None:
    market = _market()

    def crash(event: str) -> None:
        if event == "openai_intent_frozen":
            raise RuntimeError("failpoint after provider intent")

    with pytest.raises(RuntimeError, match="failpoint"):
        _observe(tmp_path, market=market, event_sink=crash)

    class EmptyGamma:
        def __init__(self) -> None:
            self.requests: list[HttpRequest] = []

        def __call__(self, request: HttpRequest) -> HttpResponse:
            self.requests.append(request)
            assert "gamma-api" in request.url
            return HttpResponse(200, JSON_HEADERS, b"[]", NOW)

    public = EmptyGamma()
    openai = ModelTransport("openai", _market_sha(market))
    jev = ModelTransport("jev")
    summary = _observe(tmp_path, market=market, public=public, openai=openai, jev=jev)[
        0
    ]

    assert summary["ai"]["disposition"] == "UNKNOWN_IN_FLIGHT"
    assert summary["ai"]["model_cost_microdollars"] is None
    assert openai.requests == [] and jev.requests == []
    assert len(public.requests) == 1
    completed = json.loads((tmp_path / "completed_slots.jsonl").read_text())
    assert completed["observation_ids"] == [summary["observation_id"]]

    replay_public = EmptyGamma()
    replay_openai = ModelTransport("openai", _market_sha(market))
    replay_jev = ModelTransport("jev")
    again = _observe(
        tmp_path,
        market=market,
        public=replay_public,
        openai=replay_openai,
        jev=replay_jev,
    )
    assert again == [summary]
    assert replay_public.requests == []
    assert replay_openai.requests == [] and replay_jev.requests == []


def test_eligibility_drift_cannot_erase_unknown_in_flight_provider_cost(
    tmp_path: Path,
) -> None:
    market = _market()

    def crash(event: str) -> None:
        if event == "openai_intent_frozen":
            raise RuntimeError("failpoint after provider intent")

    with pytest.raises(RuntimeError, match="failpoint"):
        _observe(tmp_path, market=market, event_sink=crash)

    journal_store = ProviderCallJournal(tmp_path)
    opportunity_id = hashlib.sha256(
        canonical_json_bytes(
            {
                "market_id": market["id"],
                "policy_version": POLICY_VERSION,
                "slot_at": NOW.isoformat().replace("+00:00", "Z"),
            }
        )
    ).hexdigest()
    intent = journal_store.lookup(opportunity_id, "openai")
    assert intent is not None and intent["kind"] == "provider_call_intent"
    original_opportunity_id = str(intent["opportunity_id"])

    inactive = market | {"active": False}
    public = PublicNetwork(inactive)
    openai = ModelTransport("openai", _market_sha(inactive))
    jev = ModelTransport("jev")
    summary = _observe(
        tmp_path,
        market=inactive,
        public=public,
        openai=openai,
        jev=jev,
    )[0]

    assert summary["opportunity_id"] == original_opportunity_id
    assert summary["ai"]["disposition"] == "UNKNOWN_IN_FLIGHT"
    assert summary["ai"]["model_cost_microdollars"] is None
    assert summary["ai"]["economic_pnl_microdollars"] is None
    assert summary["paired_economic_pnl_delta_microdollars"] is None
    assert openai.requests == [] and jev.requests == []
    assert len(public.requests) == 1
    replay = load_observation(tmp_path, str(summary["observation_id"]))
    assert replay.result.evidence_sha256 == _market_sha(market)
    failure = json.loads(replay.artifacts["openai_failure"])
    assert failure["code"] == "UNKNOWN_IN_FLIGHT"
    results = (tmp_path / "results.jsonl").read_text().splitlines()
    assert len(results) == 1
    result_state = journal_store.lookup(original_opportunity_id, "openai")
    assert result_state is not None and result_state["kind"] == "provider_call_result"
    outcome = result_state["outcome"]
    assert outcome["code"] == "UNKNOWN_IN_FLIGHT"
    assert outcome["billed_cost_microdollars"] is None
    lines = (tmp_path / "provider_calls.jsonl").read_text().splitlines()
    assert len(lines) == 2

    replay_public = PublicNetwork(inactive)
    replay_openai = ModelTransport("openai", _market_sha(inactive))
    replay_jev = ModelTransport("jev")
    again = _observe(
        tmp_path,
        market=inactive,
        public=replay_public,
        openai=replay_openai,
        jev=replay_jev,
    )[0]
    assert again == summary
    assert replay_public.requests == []
    assert replay_openai.requests == [] and replay_jev.requests == []
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 1


def test_same_slot_changed_evidence_never_calls_provider_again(
    tmp_path: Path,
) -> None:
    first_market = _market()

    def crash(event: str) -> None:
        if event == "openai_intent_frozen":
            raise RuntimeError("failpoint after provider intent")

    with pytest.raises(RuntimeError, match="failpoint"):
        _observe(tmp_path, market=first_market, event_sink=crash)

    changed_market = first_market | {"description": "Changed public evidence."}
    public = PublicNetwork(changed_market)
    openai = ModelTransport("openai", _market_sha(changed_market))
    jev = ModelTransport("jev")

    summary = _observe(
        tmp_path,
        public=public,
        openai=openai,
        jev=jev,
        market=changed_market,
    )[0]

    assert openai.requests == [] and jev.requests == []
    assert summary["ai"]["disposition"] == "UNKNOWN_IN_FLIGHT"
    assert summary["ai"]["model_cost_microdollars"] is None
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 1


def test_pending_recovery_ignores_same_market_gamma_token_drift(
    tmp_path: Path,
) -> None:
    market = _market()

    def crash(event: str) -> None:
        if event == "openai_intent_frozen":
            raise RuntimeError("failpoint after provider intent")

    with pytest.raises(RuntimeError, match="failpoint"):
        _observe(tmp_path, market=market, event_sink=crash)

    drifted = market | {"clobTokenIds": '["yes-drifted","no-drifted"]'}

    class DriftedPublic(PublicNetwork):
        def __call__(self, request: HttpRequest) -> HttpResponse:
            response = super().__call__(request)
            if "clob.polymarket.com" not in request.url:
                return response
            value = json.loads(response.body)
            value["asset_id"] = "yes-drifted"
            return HttpResponse(
                response.status_code,
                response.headers,
                _wire(value),
                response.retrieved_at,
            )

    public = DriftedPublic(drifted)
    openai = ModelTransport("openai", _market_sha(drifted))
    jev = ModelTransport("jev")
    summary = _observe(
        tmp_path,
        market=drifted,
        public=public,
        openai=openai,
        jev=jev,
    )[0]

    assert summary["ai"]["disposition"] == "UNKNOWN_IN_FLIGHT"
    assert summary["ai"]["model_cost_microdollars"] is None
    assert summary["ai"]["economic_pnl_microdollars"] is None
    assert summary["paired_economic_pnl_delta_microdollars"] is None
    assert openai.requests == [] and jev.requests == []
    replay = load_observation(tmp_path, str(summary["observation_id"]))
    assert replay.result.evidence_sha256 == _market_sha(market)
    raw_gamma = json.loads(replay.artifacts["raw_gamma"])
    assert raw_gamma["clobTokenIds"] == market["clobTokenIds"]
    http = json.loads(replay.artifacts["precheck_book_http"])
    assert http["request_token_id"] == "yes-live-m1"
    result = ProviderCallJournal(tmp_path).lookup(
        str(summary["opportunity_id"]), "openai"
    )
    assert result is not None and result["kind"] == "provider_call_result"
    assert result["outcome"]["code"] == "UNKNOWN_IN_FLIGHT"
    assert result["outcome"]["billed_cost_microdollars"] is None


def test_same_slot_changed_evidence_after_openai_result_terminalizes_without_jev(
    tmp_path: Path,
) -> None:
    first_market = _market()

    def crash(event: str) -> None:
        if event == "openai_result_frozen":
            raise RuntimeError("failpoint after provider result")

    with pytest.raises(RuntimeError, match="failpoint"):
        _observe(tmp_path, market=first_market, event_sink=crash)

    changed_market = first_market | {"description": "Changed public evidence."}
    openai = ModelTransport("openai", _market_sha(changed_market))
    jev = ModelTransport("jev")
    summary = _observe(
        tmp_path,
        public=PublicNetwork(changed_market),
        openai=openai,
        jev=jev,
        market=changed_market,
    )[0]

    assert openai.requests == [] and jev.requests == []
    assert summary["ai"]["disposition"] == "MODEL_FAILURE"
    assert summary["ai"]["model_cost_microdollars"] == 17
    replay = load_observation(tmp_path, summary["observation_id"])
    failure = json.loads(replay.artifacts["jev_failure"])
    assert failure["code"] == "INPUT_CHANGED"
    assert failure["message"] == "jev frozen input differs from current evidence"
    brief = json.loads(replay.artifacts["research_brief"])
    assert brief["evidence_sha256"] == _market_sha(first_market)
    assert replay.result.evidence_sha256 == _market_sha(first_market)
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 1

    again = _observe(
        tmp_path,
        public=PublicNetwork(changed_market),
        openai=openai,
        jev=jev,
        market=changed_market,
    )[0]
    assert again == summary
    assert openai.requests == [] and jev.requests == []


@pytest.mark.parametrize(
    ("boundary", "expected_jev_calls", "expected_intents"),
    [
        pytest.param("openai_intent_frozen", 0, 1, id="after-openai-intent"),
        pytest.param("openai_result_frozen", 0, 1, id="after-openai-result"),
        pytest.param("jev_intent_frozen", 0, 2, id="after-jev-intent"),
    ],
)
def test_same_slot_restart_with_fresh_retrieval_times_never_repeats_provider_call(
    tmp_path: Path,
    boundary: str,
    expected_jev_calls: int,
    expected_intents: int,
) -> None:
    def crash(event: str) -> None:
        if event == boundary:
            raise RuntimeError("provider failpoint")

    with pytest.raises(RuntimeError, match="provider failpoint"):
        _observe(tmp_path, event_sink=crash)

    class FreshRetrievalPublic(PublicNetwork):
        def __call__(self, request: HttpRequest) -> HttpResponse:
            response = super().__call__(request)
            body = response.body
            if "clob.polymarket.com" in request.url:
                value = json.loads(body)
                value["timestamp"] = str(int(value["timestamp"]) + 10_000)
                body = _wire(value)
            return HttpResponse(
                response.status_code,
                response.headers,
                body,
                response.retrieved_at + timedelta(seconds=10),
            )

    openai = ModelTransport("openai", _market_sha())
    jev = ModelTransport("jev")
    summary = _observe(
        tmp_path,
        public=FreshRetrievalPublic(),
        openai=openai,
        jev=jev,
    )[0]

    assert openai.requests == []
    assert len(jev.requests) == expected_jev_calls
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 1
    journal = [
        json.loads(line)
        for line in (tmp_path / "provider_calls.jsonl").read_text().splitlines()
    ]
    intents = [line for line in journal if line["kind"] == "provider_call_intent"]
    assert len(intents) == expected_intents
    assert len({line["call_id"] for line in intents}) == expected_intents
    replay = load_observation(tmp_path, str(summary["observation_id"]))
    assert replay.result.opportunity_id == summary["opportunity_id"]


def test_same_slot_changed_evidence_after_jev_intent_never_calls_provider_again(
    tmp_path: Path,
) -> None:
    first_market = _market()

    def crash(event: str) -> None:
        if event == "jev_intent_frozen":
            raise RuntimeError("failpoint after provider intent")

    with pytest.raises(RuntimeError, match="failpoint"):
        _observe(tmp_path, market=first_market, event_sink=crash)

    changed_market = first_market | {"description": "Changed public evidence."}
    openai = ModelTransport("openai", _market_sha(changed_market))
    jev = ModelTransport("jev")
    summary = _observe(
        tmp_path,
        public=PublicNetwork(changed_market),
        openai=openai,
        jev=jev,
        market=changed_market,
    )[0]

    assert openai.requests == [] and jev.requests == []
    assert summary["ai"]["disposition"] == "UNKNOWN_IN_FLIGHT"
    assert summary["ai"]["model_cost_microdollars"] is None
    assert summary["paired_economic_pnl_delta_microdollars"] is None
    replay = load_observation(tmp_path, summary["observation_id"])
    failure = json.loads(replay.artifacts["jev_failure"])
    assert failure["code"] == "UNKNOWN_IN_FLIGHT"
    assert replay.result.evidence_sha256 == _market_sha(first_market)


@pytest.mark.parametrize(
    "boundary",
    [
        "openai_intent_frozen",
        "openai_result_frozen",
        "jev_intent_frozen",
        "jev_result_frozen",
    ],
)
def test_provider_failpoints_recover_without_repeating_completed_or_unknown_calls(
    tmp_path: Path, boundary: str
) -> None:
    def crash(event: str) -> None:
        if event == boundary:
            raise RuntimeError("provider failpoint")

    with pytest.raises(RuntimeError, match="provider failpoint"):
        _observe(tmp_path, event_sink=crash)

    market = _market()
    openai = ModelTransport("openai", _market_sha(market))
    jev = ModelTransport("jev")
    summary = _observe(tmp_path, market=market, openai=openai, jev=jev)[0]

    assert openai.requests == []
    assert len(jev.requests) == (1 if boundary == "openai_result_frozen" else 0)
    if boundary == "jev_intent_frozen":
        assert summary["ai"]["disposition"] == "UNKNOWN_IN_FLIGHT"
        assert summary["ai"]["model_cost_microdollars"] is None
    replay = load_observation(tmp_path, summary["observation_id"])
    assert replay.result.model_dump(mode="json") == _single_result(tmp_path)


_CONCURRENT_CHILD = r"""
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

support, output, calls, gate, ident = sys.argv[1:6]
spec = importlib.util.spec_from_file_location("live_boundary_support", support)
module = importlib.util.module_from_spec(spec)
sys.modules["live_boundary_support"] = module
spec.loader.exec_module(module)

from jev_trading_lab.polymarket import store

defaults = dict(store.BlobStore.__init__.__kwdefaults__ or {})
defaults["filesystem_resolver"] = lambda _path: "ext4"
store.BlobStore.__init__.__kwdefaults__ = defaults


def record(name):
    descriptor = os.open(calls, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, (name + "\n").encode())
    finally:
        os.close(descriptor)


class Slow(module.ModelTransport):
    def __init__(self, provider, *args, **kwargs):
        super().__init__(provider, *args, **kwargs)
        self.provider = provider

    def __call__(self, request):
        record(self.provider)
        time.sleep(0.4)
        return super().__call__(request)


market = module._market()
sha = module._market_sha(market)
Path(gate + "." + ident).write_text("ready")
while not Path(gate).exists():
    time.sleep(0.01)
summaries = module.observe_markets(
    limit=1,
    output_dir=Path(output),
    client=module.PolymarketClient(module.PublicNetwork(market)),
    openai_transport=Slow("openai", sha),
    jev_transport=Slow("jev"),
    now=module.NOW,
)
sys.stdout.write(json.dumps(summaries, sort_keys=True, separators=(",", ":")))
"""


def test_finding_4_concurrent_identical_observe_processes_call_each_provider_once(
    tmp_path: Path,
) -> None:
    output = tmp_path / "out"
    calls = tmp_path / "calls.log"
    gate = tmp_path / "go"
    children = [
        subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                _CONCURRENT_CHILD,
                __file__,
                str(output),
                str(calls),
                str(gate),
                str(index),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(4)
    ]
    deadline = time.monotonic() + 90
    while len(list(tmp_path.glob("go.*"))) < len(children):
        assert time.monotonic() < deadline, "children did not become ready"
        time.sleep(0.01)
    gate.write_text("go")
    outputs = [child.communicate(timeout=180) for child in children]

    assert [child.returncode for child in children] == [0] * len(children), outputs
    assert len({stdout for stdout, _stderr in outputs}) == 1
    assert sorted(calls.read_text().split()) == ["jev", "openai"]
    result = _single_result(output)
    summary = json.loads(outputs[0][0])
    assert [item["observation_id"] for item in summary] == [result["observation_id"]]
    replay = load_observation(output, result["observation_id"])
    assert replay.result.observation_id == result["observation_id"]
    assert not _lock_is_held(output / "observe.lock")


@pytest.mark.parametrize(
    "end_date",
    [
        "2026-10-12T12:00:00Z",
        "2026-10-12T12:00:00+00:00",
        "2026-10-12T12:00:00.000Z",
        "2026-10-12T12:00:00.000000+00:00",
    ],
)
def test_finding_5_zero_offset_gamma_close_is_canonicalized_before_store_compare(
    tmp_path: Path, end_date: str
) -> None:
    market = _market() | {"endDate": end_date}
    summary = _observe(tmp_path, market=market)[0]
    replay = load_observation(tmp_path, str(summary["observation_id"]))

    evidence = json.loads(replay.artifacts["sanitized_evidence"])
    assert evidence["closes_at"] == "2026-10-12T12:00:00Z"
    assert json.loads(replay.artifacts["raw_gamma"])["endDate"] == end_date
    assert replay.result.ai.disposition != "INELIGIBLE"


@pytest.mark.parametrize(
    "end_date",
    [
        "2026-10-12T14:00:00+02:00",
        "2026-10-12T10:00:00-02:00",
        "garbage",
        "2026-10-12",
        None,
    ],
)
def test_finding_5_nonzero_or_invalid_gamma_close_is_a_malformed_market_rejection(
    tmp_path: Path, end_date: object
) -> None:
    market = _market() | {"endDate": end_date}
    public = PublicNetwork(market)

    summary = _observe(tmp_path, public=public, market=market)[0]

    assert summary["reason"] == "MALFORMED_MARKET"
    assert summary["terminal_status"] == "ELIGIBILITY_REJECTION"
    assert len(public.requests) == 1
    assert [entry["kind"] for entry in _journal(tmp_path)] == [
        "runtime_config",
        "raw_gamma",
        "eligibility_rejection",
    ]


def _previous_variant(replay: Any, *, reason: str | None = None) -> list[Any]:
    """Re-express one chain in the previous Unit 6 shape (no config or HTTP stage)."""

    from jev_trading_lab.polymarket import pipeline

    values = []
    for kind, payload in replay.artifacts.items():
        if kind in {"runtime_config", "precheck_book_http"}:
            continue
        if kind == "precheck_book_failure":
            value = json.loads(payload)
            value.pop("http_sha256")
            payload = canonical_json_bytes(value)
        if kind == "eligibility_rejection" and reason is not None:
            value = json.loads(payload)
            value["reason"] = reason
            payload = canonical_json_bytes(value)
        envelope = replay.envelopes[kind]
        values.append((kind, payload, envelope.source_uri_label, envelope.retrieved_at))
    return pipeline._chain(replay.result, values)


def _previous_variant_scenario(
    output: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> str:
    market = _market()
    sha = _market_sha(market)
    reply = {
        "wide_spread": HttpResponse(
            200, JSON_HEADERS, _book_bytes(asks=_WIDE_ASKS), NOW
        ),
        "malformed": HttpResponse(200, JSON_HEADERS, b"{", NOW),
        "timeout": TimeoutError(),
    }.get(scenario)
    if reply is not None:
        summary = observe_markets(
            limit=1,
            output_dir=output,
            client=PolymarketClient(ScriptedPrecheck(reply)),
            now=NOW,
        )[0]
    elif scenario == "cumulative_overflow":
        summary = _observe(
            output,
            openai=ModelTransport("openai", sha, cost=2**63 - 1),
            jev=ModelTransport("jev", cost=1),
        )[0]
    elif scenario == "openai_failure":
        summary = _observe(output, openai=ModelTransport("openai", sha, fail=True))[0]
    else:
        summary = _observe(output)[0]
    return str(summary["observation_id"])


_PREVIOUS_VARIANT_SCENARIOS = [
    "success",
    "openai_failure",
    "cumulative_overflow",
    "wide_spread",
    "malformed",
    "timeout",
]


@pytest.mark.parametrize("scenario", _PREVIOUS_VARIANT_SCENARIOS)
def test_previous_unit6_chain_variants_still_record_and_replay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scenario: str
) -> None:
    from jev_trading_lab.polymarket.store import record_observation

    observation_id = _previous_variant_scenario(
        tmp_path / "current", monkeypatch, scenario
    )
    replay = load_observation(tmp_path / "current", observation_id)

    record_observation(tmp_path / "previous", replay.result, _previous_variant(replay))

    previous = load_observation(tmp_path / "previous", observation_id)
    assert previous.result == replay.result
    assert "runtime_config" not in previous.artifacts
    assert "precheck_book_http" not in previous.artifacts


@pytest.mark.parametrize(
    ("scenario", "forged"),
    [("wide_spread", "LOW_DEPTH"), ("wide_spread", "SPORTS")],
)
def test_previous_unit6_variant_replay_also_recomputes_eligibility(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scenario: str, forged: str
) -> None:
    from jev_trading_lab.polymarket.store import record_observation

    observation_id = _previous_variant_scenario(
        tmp_path / "current", monkeypatch, scenario
    )
    replay = load_observation(tmp_path / "current", observation_id)

    with pytest.raises(ValueError, match="does not match the precheck book"):
        record_observation(
            tmp_path / "previous",
            replay.result,
            _previous_variant(replay, reason=forged),
        )
    assert not (tmp_path / "previous").exists()


def test_finding_1_overlong_token_id_is_a_malformed_market_rejection_not_a_crash(
    tmp_path: Path,
) -> None:
    market = _market() | {"clobTokenIds": json.dumps(["9" * 600, "no-live-m1"])}
    public = PublicNetwork(market)

    summary = _observe(tmp_path, public=public, market=market)[0]

    assert summary["reason"] == "MALFORMED_MARKET"
    assert len(public.requests) == 1
    assert [entry["kind"] for entry in _journal(tmp_path)] == [
        "runtime_config",
        "raw_gamma",
        "eligibility_rejection",
    ]


_SECRET_LIKE_BODIES = [
    pytest.param(200, b'{"error":"Bearer abcd1234"}', id="bearer-in-json-200"),
    pytest.param(503, b'{"error":"Bearer abcd1234"}', id="bearer-in-json-503"),
    pytest.param(
        200, b"Set-Cookie: session=abcd1234; api_key=abcdef12", id="cookie-and-key-text"
    ),
]


@pytest.mark.parametrize(("status", "body"), _SECRET_LIKE_BODIES)
def test_hardening_credential_like_public_precheck_bytes_are_recorded_exactly(
    tmp_path: Path, status: int, body: bytes
) -> None:
    reply = HttpResponse(status, JSON_HEADERS, body, NOW)

    summary = observe_markets(
        limit=1,
        output_dir=tmp_path,
        client=PolymarketClient(ScriptedPrecheck(reply)),
        now=NOW,
    )[0]

    assert summary["reason"] == "MALFORMED_BOOK"
    replay = load_observation(tmp_path, str(summary["observation_id"]))
    assert replay.artifacts["precheck_book_raw"] == body
    http = json.loads(replay.artifacts["precheck_book_http"])
    assert http["response_sha256"] == hashlib.sha256(body).hexdigest()


def test_hardening_quoted_bearer_prose_in_gamma_is_recorded_exactly(
    tmp_path: Path,
) -> None:
    market = _market() | {
        "description": "Public report quotes Bearer abcd1234 in an incident."
    }
    raw_gamma = _wire(market)

    summary = _observe(tmp_path, market=market)[0]

    replay = load_observation(tmp_path, str(summary["observation_id"]))
    assert replay.artifacts["raw_gamma"] == raw_gamma
    assert b"Bearer abcd1234" not in replay.artifacts["sanitized_evidence"]
    assert b"Bearer abcd1234" not in replay.artifacts["openai_request"]
    assert replay.result.ai.disposition != "INELIGIBLE"
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 1


def test_hardening_gamma_access_token_prose_never_reaches_model_or_artifacts(
    tmp_path: Path,
) -> None:
    secret = b"PROSE-SECRET"
    market = _market() | {"description": "leak access_token=PROSE-SECRET"}
    openai = ModelTransport("openai", _market_sha(market))

    summary = _observe(tmp_path, market=market, openai=openai)[0]

    replay = load_observation(tmp_path, str(summary["observation_id"]))
    assert secret not in replay.artifacts["sanitized_evidence"]
    assert secret not in replay.artifacts["openai_request"]
    assert all(secret not in request for request in openai.requests)
    assert replay.result.ai.disposition != "INELIGIBLE"


@pytest.mark.parametrize(
    "description",
    (
        "The leaked api key is SUPERSECRET123 in the report.",
        "The leaked access-token: SUPERSECRET123 in the report.",
        "The leaked refresh_token = SUPERSECRET123 in the report.",
        "The leaked clientSecret V2 is SUPERSECRET123 in the report.",
        "The leaked ａｐｉ＿ｋｅｙ-v3 is SUPERSECRET123 in the report.",
        "The leaked vendorSessionTokenV4 is SUPERSECRET123 in the report.",
    ),
)
def test_hardening_gamma_credential_is_prose_never_reaches_model_or_artifacts(
    tmp_path: Path, description: str
) -> None:
    secret = b"SUPERSECRET123"
    market = _market() | {"description": description}
    raw_gamma = _wire(market)
    openai = ModelTransport("openai", _market_sha(market))

    summary = _observe(tmp_path, market=market, openai=openai)[0]

    replay = load_observation(tmp_path, str(summary["observation_id"]))
    assert replay.artifacts["raw_gamma"] == raw_gamma
    assert secret not in replay.artifacts["sanitized_evidence"]
    assert secret not in replay.artifacts["openai_request"]
    assert all(secret not in request for request in openai.requests)
    assert replay.result.ai.disposition != "INELIGIBLE"


def test_hardening_harmless_credential_noun_prose_is_not_redacted(
    tmp_path: Path,
) -> None:
    description = "The api key is required for client setup."
    market = _market() | {"description": description}

    summary = _observe(tmp_path, market=market)[0]

    replay = load_observation(tmp_path, str(summary["observation_id"]))
    evidence = json.loads(replay.artifacts["sanitized_evidence"])
    assert evidence["description"] == description


@pytest.mark.parametrize(
    "field",
    (
        "access_token",
        "refresh-token",
        "clientSecret",
        "vendorSessionToken_v2",
        "ａｃｃｅｓｓ＿ｔｏｋｅｎV3",
    ),
)
def test_hardening_raw_gamma_rejects_structural_credential_fields(field: str) -> None:
    from jev_trading_lab.polymarket.store import _screen_artifact

    payload = _wire({field: "STRUCTURAL-SECRET"})
    with pytest.raises(StoreSecurityError):
        _screen_artifact("raw_gamma", payload, ())


def test_hardening_secret_like_hash_in_valid_public_books_does_not_abort_observe(
    tmp_path: Path,
) -> None:
    precheck = _book_bytes(0, hash="sk-aaaaaaaaaaaaaaaa")
    execution = _book_bytes(3, hash="Bearer abcdefgh12345678")
    public = ScriptedPrecheck(
        HttpResponse(200, JSON_HEADERS, precheck, NOW),
        then=(HttpResponse(200, JSON_HEADERS, execution, NOW + timedelta(seconds=3)),),
    )

    summary = _observe(tmp_path, public=public)[0]

    replay = load_observation(tmp_path, str(summary["observation_id"]))
    assert replay.artifacts["precheck_book"] == precheck
    assert replay.artifacts["execution_book"] == execution
    assert replay.result.ai.disposition != "INELIGIBLE"


def test_hardening_public_book_kinds_still_screen_explicit_secret_sentinels() -> None:
    from jev_trading_lab.polymarket.store import _screen_artifact

    sentinel = (b"unit6-secret-sentinel",)
    for kind in ("precheck_book_raw", "precheck_book", "execution_book"):
        _screen_artifact(kind, b'{"hash":"sk-aaaaaaaaaaaa"}', ())
        with pytest.raises(StoreSecurityError):
            _screen_artifact(kind, b"prefix unit6-secret-sentinel suffix", sentinel)
    _screen_artifact("raw_gamma", b'{"a":"Bearer abcd1234"}', ())
    for kind in ("openai_request", "runtime_config", "jev_request"):
        with pytest.raises(StoreSecurityError):
            _screen_artifact(kind, b'{"a":"Bearer abcd1234"}', ())


@pytest.mark.parametrize(
    "field", ["outcomes", "outcomePrices", "clobTokenIds"], ids=lambda value: value
)
def test_hardening_nested_gamma_encoded_arrays_are_recorded_not_aborted(
    tmp_path: Path, field: str
) -> None:
    market = _market() | {field: "[" * 100_000 + "]" * 100_000}
    public = PublicNetwork(market)

    summary = _observe(tmp_path, public=public, market=market)[0]

    assert summary["reason"] == "MALFORMED_MARKET"
    assert len(public.requests) == 1


def test_hardening_lone_surrogate_gamma_text_is_recorded_as_a_malformed_market(
    tmp_path: Path,
) -> None:
    class SurrogateGamma:
        def __init__(self) -> None:
            self.requests: list[HttpRequest] = []

        def __call__(self, request: HttpRequest) -> HttpResponse:
            self.requests.append(request)
            raw = _wire([_market()]).replace(
                b"Will the public event occur?", b"Will \\ud800 occur?"
            )
            return HttpResponse(200, JSON_HEADERS, raw, NOW)

    public = SurrogateGamma()

    summary = observe_markets(
        limit=1, output_dir=tmp_path, client=PolymarketClient(public), now=NOW
    )[0]

    assert summary["reason"] == "MALFORMED_MARKET"
    assert len(public.requests) == 1
    assert [entry["kind"] for entry in _journal(tmp_path)] == [
        "runtime_config",
        "raw_gamma",
        "eligibility_rejection",
    ]


def test_hardening_observation_lock_wait_is_bounded_and_typed(tmp_path: Path) -> None:
    from jev_trading_lab.polymarket.store import StoreBusyError, observation_lock

    output = tmp_path / "out"
    with observation_lock(output):
        started = time.monotonic()
        with pytest.raises(StoreBusyError, match="observe lock"):
            with observation_lock(output, timeout_seconds=0.25):
                pytest.fail("the lock must not be acquired while it is held")
        assert 0.2 <= time.monotonic() - started < 5
    with observation_lock(output, timeout_seconds=0.25):
        pass


class ScriptedProvider:
    """Closed-world provider network that returns one scripted JSON body."""

    def __init__(self, body: bytes, at: datetime) -> None:
        self.body = body
        self.at = at
        self.requests: list[LiveHttpRequest] = []

    def __call__(self, request: LiveHttpRequest) -> LiveHttpResponse:
        self.requests.append(request)
        return LiveHttpResponse(200, JSON_HEADERS, self.body, self.at)


_RATE_ENVIRONMENT = (
    "OPENAI_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
    "OPENAI_CACHED_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
    "OPENAI_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
    "TYPESAFE_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
    "TYPESAFE_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
)


def _billing_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-secret")
    monkeypatch.setenv("TYPESAFE_API_KEY", "typesafe-test-secret")
    for name in _RATE_ENVIRONMENT:
        monkeypatch.setenv(name, str(10**12))


def _overflowing_usage(provider: str, raw: bytes, input_tokens: int = 10**13) -> bytes:
    value = json.loads(raw)
    usage = value["usage"]
    usage["input_tokens"] = input_tokens
    if provider == "openai":
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


@pytest.mark.parametrize(
    ("provider", "mutate"),
    [
        pytest.param("openai", lambda value: value.pop("usage"), id="openai-missing"),
        pytest.param(
            "jev",
            lambda value: value["usage"].update(input_tokens="malformed"),
            id="typesafe-malformed",
        ),
    ],
)
def test_unknown_provider_usage_terminalizes_without_zero_cost_and_replays_exactly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
    mutate: Callable[[dict[str, Any]], object],
) -> None:
    _billing_environment(monkeypatch)
    fixture = json.loads(FIXTURE.read_text())
    body = json.loads(fixture["model_artifacts"][f"{provider}_response_bytes"])
    mutate(body)
    live = ScriptedProvider(_wire(body), NOW + timedelta(seconds=2))
    market = _market()
    openai = (
        live_openai_transport(network=live)
        if provider == "openai"
        else ModelTransport("openai", _market_sha(market))
    )
    jev = (
        live_jev_transport(network=live) if provider == "jev" else ModelTransport("jev")
    )

    summary = _observe(
        tmp_path,
        market=market,
        openai=openai,
        jev=jev,
    )[0]

    assert len(live.requests) == 1
    assert summary["ai"]["disposition"] == "MODEL_FAILURE"
    assert summary["ai"]["model_cost_microdollars"] is None
    assert summary["ai"]["economic_pnl_microdollars"] is None
    assert summary["paired_economic_pnl_delta_microdollars"] is None
    replay = load_observation(tmp_path, str(summary["observation_id"]))
    failure = json.loads(replay.artifacts[f"{provider}_failure"])
    assert failure["code"] == "PARSE_ERROR"
    assert failure["cost_status"] == "UNKNOWN"
    assert failure["billed_cost_microdollars"] is None
    assert load_observation(tmp_path, str(summary["observation_id"])) == replay


def test_timeout_after_intent_artifact_marks_cost_unknown(tmp_path: Path) -> None:
    class TimeoutTransport(ModelTransport):
        def __call__(self, request: bytes) -> TransportResponse:
            self.requests.append(request)
            raise TimeoutError("ambiguous timeout")

    summary = _observe(tmp_path, openai=TimeoutTransport("openai"))[0]

    replay = load_observation(tmp_path, str(summary["observation_id"]))
    failure = json.loads(replay.artifacts["openai_failure"])
    assert failure["code"] == "UNKNOWN_IN_FLIGHT"
    assert failure["cost_status"] == "UNKNOWN"
    assert failure["billed_cost_microdollars"] is None
    assert replay.result.ai.model_cost_microdollars is None
    assert replay.result.ai.economic_pnl_microdollars is None


_OVERFLOW_TAILS = {
    "openai_individual": [
        "openai_request",
        "openai_cost_overflow",
        "decision_frozen",
        "book_unavailable",
    ],
    "jev_individual": [
        "research_brief",
        "jev_request",
        "jev_cost_overflow",
        "decision_frozen",
        "book_unavailable",
    ],
    "cumulative": [
        "jev_decision",
        "decision_frozen",
        "book_unavailable",
    ],
}


def _overflow_observation(
    monkeypatch: pytest.MonkeyPatch,
    output: Path,
    scenario: str,
    *,
    input_tokens: int = 10**13,
) -> tuple[dict[str, Any], PublicNetwork, dict[str, Any]]:
    _billing_environment(monkeypatch)
    market = _market()
    sha = _market_sha(market)
    public = PublicNetwork(market)
    raw = {
        "openai": ModelTransport("openai", sha).raw,
        "jev": ModelTransport("jev").raw,
    }
    transports: dict[str, Any] = {}
    if scenario == "openai_individual":
        body = _overflowing_usage("openai", raw["openai"], input_tokens)
        transports["openai"] = live_openai_transport(
            network=ScriptedProvider(body, NOW + timedelta(seconds=1))
        )
        transports["jev"] = ModelTransport("jev")
        transports["body"] = body
    elif scenario == "jev_individual":
        body = _overflowing_usage("jev", raw["jev"], input_tokens)
        transports["openai"] = ModelTransport("openai", sha)
        transports["jev"] = live_jev_transport(
            network=ScriptedProvider(body, NOW + timedelta(seconds=2))
        )
        transports["body"] = body
    else:
        transports["openai"] = ModelTransport("openai", sha, cost=2**63 - 1)
        transports["jev"] = ModelTransport("jev", cost=1)
        transports["body"] = b""
    summary = observe_markets(
        limit=1,
        output_dir=output,
        client=PolymarketClient(public),
        openai_transport=transports["openai"],
        jev_transport=transports["jev"],
        now=NOW,
    )[0]
    return summary, public, transports


@pytest.mark.parametrize(
    "scenario", ["openai_individual", "jev_individual", "cumulative"]
)
def test_finding_6_cost_overflow_terminalizes_before_any_execution_book(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scenario: str
) -> None:
    summary, public, transports = _overflow_observation(monkeypatch, tmp_path, scenario)
    result = _single_result(tmp_path)

    assert summary["ai"]["disposition"] == "COST_OVERFLOW"
    assert summary["control"]["disposition"] == "BOOK_FAILURE"
    assert len(public.requests) == 3
    assert result["execution_book_sha256"] is None
    assert result["baseline_probability_micros"] is None
    assert result["ai"]["model_cost_microdollars"] is None
    assert result["ai"]["economic_pnl_microdollars"] is None
    assert result["ai"]["quantity"] == 0
    assert result["ai"]["trading_pnl_microdollars"] == 0
    assert result["ai"]["final_cash_microdollars"] == 10_000_000_000
    assert result["deterministic"]["quantity"] == 0
    assert result["deterministic"]["trading_pnl_microdollars"] == 0
    assert result["deterministic"]["model_cost_microdollars"] == 0
    assert result["paired_economic_pnl_delta_microdollars"] is None
    assert result["paired_trading_pnl_delta_microdollars"] == 0
    if scenario == "openai_individual":
        assert transports["jev"].requests == []

    replay = load_observation(tmp_path, result["observation_id"])
    kinds = list(replay.artifacts)
    tail = _OVERFLOW_TAILS[scenario]
    assert kinds[-len(tail) :] == tail
    assert "execution_book" not in kinds
    barrier = json.loads(replay.artifacts["decision_frozen"])
    assert barrier["terminal_model_state"] == "COST_OVERFLOW"
    assert json.loads(replay.artifacts["book_unavailable"])["reason"] == "COST_OVERFLOW"
    expected_costs = {
        "openai_individual": (None, 0),
        "jev_individual": (17, None),
        "cumulative": (2**63 - 1, 1),
    }[scenario]
    assert (
        barrier["openai_cost_microdollars"],
        barrier["jev_cost_microdollars"],
    ) == expected_costs


@pytest.mark.parametrize(
    ("scenario", "provider"),
    [("openai_individual", "openai"), ("jev_individual", "jev")],
)
@pytest.mark.parametrize("input_tokens", [2**63, 2**70])
def test_finding_6_overflow_artifact_encodes_token_counts_beyond_int64_exactly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scenario: str,
    provider: str,
    input_tokens: int,
) -> None:
    summary, _public, _transports = _overflow_observation(
        monkeypatch, tmp_path, scenario, input_tokens=input_tokens
    )

    assert summary["ai"]["disposition"] == "COST_OVERFLOW"
    replay = load_observation(tmp_path, str(summary["observation_id"]))
    overflow = json.loads(replay.artifacts[f"{provider}_cost_overflow"])
    assert overflow["usage"]["input_tokens"] == str(input_tokens)
    assert replay.result.ai.model_cost_microdollars is None


@pytest.mark.parametrize(
    ("provider", "scenario", "cached", "output", "at"),
    [
        ("openai", "openai_individual", 0, 250, "2026-10-10T12:00:01Z"),
        ("jev", "jev_individual", None, 40, "2026-10-10T12:00:02Z"),
    ],
)
def test_finding_6_overflow_artifact_preserves_exact_usage_and_configured_rates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
    scenario: str,
    cached: int | None,
    output: int,
    at: str,
) -> None:
    _summary, _public, transports = _overflow_observation(
        monkeypatch, tmp_path, scenario
    )
    result = _single_result(tmp_path)
    replay = load_observation(tmp_path, result["observation_id"])
    body = transports["body"]

    overflow = json.loads(replay.artifacts[f"{provider}_cost_overflow"])
    assert overflow == {
        "provider": provider,
        "request_sha256": hashlib.sha256(
            replay.artifacts[f"{provider}_request"]
        ).hexdigest(),
        "response_sha256": hashlib.sha256(body).hexdigest(),
        "response_byte_length": len(body),
        "retrieved_at": at,
        "status_code": 200,
        "usage": {
            "input_tokens": str(10**13),
            "cached_input_tokens": None if cached is None else str(cached),
            "output_tokens": str(output),
        },
        "rates": {
            "input_microdollars_per_million_tokens": 10**12,
            "cached_input_microdollars_per_million_tokens": (
                10**12 if provider == "openai" else None
            ),
            "output_microdollars_per_million_tokens": 10**12,
        },
    }
    persisted = b"".join(
        path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    )
    assert b"openai-test-secret" not in persisted
    assert b"typesafe-test-secret" not in persisted
    assert body not in persisted


def _swap_overflow_provider(
    provider: str, cached_tokens: int | None, cached_rate: int | None
) -> Callable[[bytes], bytes]:
    """Rewrite an overflow artifact so it is well-formed for the other provider."""

    def update(value: dict[str, Any]) -> None:
        value["provider"] = provider
        value["usage"]["cached_input_tokens"] = (
            None if cached_tokens is None else str(cached_tokens)
        )
        value["rates"]["cached_input_microdollars_per_million_tokens"] = cached_rate

    return _edit(update)


_OVERFLOW_PROOF = "cost overflow metadata does not prove an int64 overflow"
_OVERFLOW_STAGE = "cost overflow artifact provider does not match stage"
_OVERFLOW_BINDING = "cost overflow does not bind its request and frozen rates"
_OVERFLOW_BARRIER = "cost overflow barrier fields are inconsistent"
_BARRIER_COSTS = (
    "decision frozen artifact terminal state, failure code, or costs are inconsistent"
)
_FORGED_OVERFLOW_WHY = {
    "openai-usage-no-longer-overflows": _OVERFLOW_PROOF,
    "openai-cached-exceeds-input": _OVERFLOW_PROOF,
    "usage-count-encoded-as-integer": "Input should be a valid string",
    "usage-count-with-leading-zeros": "String should match pattern",
    "openai-rate-differs-from-frozen-config": _OVERFLOW_BINDING,
    "openai-provider-swapped": _OVERFLOW_PROOF,
    "openai-provider-swapped-with-jev-shape": _OVERFLOW_STAGE,
    "openai-request-binding-forged": _OVERFLOW_BINDING,
    "openai-cost-field-added": _EXTRA_FIELD,
    "openai-barrier-cost-forged-zero": _OVERFLOW_BARRIER,
    "openai-barrier-response-forged": (
        "decision frozen artifact does not bind OpenAI response"
    ),
    "jev-usage-no-longer-overflows": _OVERFLOW_PROOF,
    "jev-rate-differs-from-frozen-config": _OVERFLOW_BINDING,
    "jev-cached-field-forged": _OVERFLOW_PROOF,
    "jev-provider-swapped": _OVERFLOW_PROOF,
    "jev-provider-swapped-with-openai-shape": _OVERFLOW_STAGE,
    "jev-barrier-cost-forged-zero": _OVERFLOW_BARRIER,
    "jev-barrier-openai-cost-forged": _BARRIER_COSTS,
    "cumulative-barrier-cost-forged-below-overflow": _OVERFLOW_BARRIER,
    "cumulative-provider-cost-forged-below-overflow": _BARRIER_COSTS,
}


@pytest.mark.parametrize(
    ("scenario", "kind", "rewrite"),
    [
        pytest.param(
            "openai_individual",
            "openai_cost_overflow",
            _edit(lambda v: v["usage"].update(input_tokens="1")),
            id="openai-usage-no-longer-overflows",
        ),
        pytest.param(
            "openai_individual",
            "openai_cost_overflow",
            _edit(lambda v: v["usage"].update(cached_input_tokens=str(10**14))),
            id="openai-cached-exceeds-input",
        ),
        pytest.param(
            "openai_individual",
            "openai_cost_overflow",
            _edit(lambda v: v["usage"].update(input_tokens=10**13)),
            id="usage-count-encoded-as-integer",
        ),
        pytest.param(
            "openai_individual",
            "openai_cost_overflow",
            _edit(lambda v: v["usage"].update(input_tokens="0010000000000000")),
            id="usage-count-with-leading-zeros",
        ),
        pytest.param(
            "openai_individual",
            "openai_cost_overflow",
            _edit(
                lambda v: v["rates"].update(
                    input_microdollars_per_million_tokens=10**12 - 1
                )
            ),
            id="openai-rate-differs-from-frozen-config",
        ),
        pytest.param(
            "openai_individual",
            "openai_cost_overflow",
            _edit(lambda v: v.update(provider="jev")),
            id="openai-provider-swapped",
        ),
        pytest.param(
            "openai_individual",
            "openai_cost_overflow",
            _swap_overflow_provider("jev", None, None),
            id="openai-provider-swapped-with-jev-shape",
        ),
        pytest.param(
            "openai_individual",
            "openai_cost_overflow",
            _edit(lambda v: v.update(request_sha256="0" * 64)),
            id="openai-request-binding-forged",
        ),
        pytest.param(
            "openai_individual",
            "openai_cost_overflow",
            _edit(lambda v: v.update(billed_cost_microdollars=1)),
            id="openai-cost-field-added",
        ),
        pytest.param(
            "openai_individual",
            "decision_frozen",
            _edit(lambda v: v.update(openai_cost_microdollars=0)),
            id="openai-barrier-cost-forged-zero",
        ),
        pytest.param(
            "openai_individual",
            "decision_frozen",
            _edit(lambda v: v.update(openai_response_sha256="0" * 64)),
            id="openai-barrier-response-forged",
        ),
        pytest.param(
            "jev_individual",
            "jev_cost_overflow",
            _edit(lambda v: v["usage"].update(input_tokens="1")),
            id="jev-usage-no-longer-overflows",
        ),
        pytest.param(
            "jev_individual",
            "jev_cost_overflow",
            _edit(
                lambda v: v["rates"].update(
                    output_microdollars_per_million_tokens=10**12 - 1
                )
            ),
            id="jev-rate-differs-from-frozen-config",
        ),
        pytest.param(
            "jev_individual",
            "jev_cost_overflow",
            _edit(lambda v: v["usage"].update(cached_input_tokens="1")),
            id="jev-cached-field-forged",
        ),
        pytest.param(
            "jev_individual",
            "jev_cost_overflow",
            _edit(lambda v: v.update(provider="openai")),
            id="jev-provider-swapped",
        ),
        pytest.param(
            "jev_individual",
            "jev_cost_overflow",
            _swap_overflow_provider("openai", 0, 10**12),
            id="jev-provider-swapped-with-openai-shape",
        ),
        pytest.param(
            "jev_individual",
            "decision_frozen",
            _edit(lambda v: v.update(jev_cost_microdollars=0)),
            id="jev-barrier-cost-forged-zero",
        ),
        pytest.param(
            "jev_individual",
            "decision_frozen",
            _edit(lambda v: v.update(openai_cost_microdollars=18)),
            id="jev-barrier-openai-cost-forged",
        ),
        pytest.param(
            "cumulative",
            "decision_frozen",
            _edit(lambda v: v.update(jev_cost_microdollars=0)),
            id="cumulative-barrier-cost-forged-below-overflow",
        ),
        pytest.param(
            "cumulative",
            "jev_success_metadata",
            _edit(lambda v: v.update(billed_cost_microdollars=0)),
            id="cumulative-provider-cost-forged-below-overflow",
        ),
    ],
)
def test_finding_6_replay_rejects_forged_cost_overflow_evidence(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scenario: str,
    kind: str,
    rewrite: Callable[[bytes], bytes],
) -> None:
    summary, _public, _transports = _overflow_observation(
        monkeypatch, tmp_path, scenario
    )
    observation_id = str(summary["observation_id"])
    load_observation(tmp_path, observation_id)

    _forge(tmp_path, observation_id, kind, rewrite)

    _assert_replay_rejected(
        tmp_path, observation_id, _FORGED_OVERFLOW_WHY[request.node.callspec.id]
    )

from __future__ import annotations

import copy
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from jev_trading_lab.cli import app
from jev_trading_lab.stocks import slice as stock_slice
from jev_trading_lab.stocks.slice import replay_fixture

FIXTURE = Path(__file__).parents[1] / "fixtures/stocks/observation.json"


class _Response:
    status = 200

    def __init__(self, value: dict[str, object]) -> None:
        self._payload = json.dumps(value).encode()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self._payload


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_replay_writes_three_terminal_arms_and_integrated_metrics(
    tmp_path: Path,
) -> None:
    summary = replay_fixture(FIXTURE, tmp_path)

    rows = _rows(tmp_path / "results.jsonl")
    metrics = (tmp_path / "stocks.prom").read_text()
    assert summary == {
        "arms": {
            "deterministic": "completed",
            "jev": "abstained",
            "openai": "completed",
        },
        "metrics_path": str(tmp_path / "stocks.prom"),
        "observation_id": "stocks-fixture-2026-10-09",
        "result_path": str(tmp_path / "results.jsonl"),
        "terminal_rows": 3,
    }
    assert {row["arm"] for row in rows} == {"openai", "jev", "deterministic"}
    assert {row["terminal_status"] for row in rows} == {"completed", "abstained"}
    assert len({row["snapshot_sha256"] for row in rows}) == 1
    assert all(row["source_mode"] == "synthetic_replay" for row in rows)
    assert all(row["initial_cash_microdollars"] == 10_000_000_000 for row in rows)
    assert all(row["process"]["cycle_duration_ms"] >= 0 for row in rows)
    assert all(len(row["market_data"]["quotes"]) == 3 for row in rows)
    assert rows[0]["provider"]["status"] in {"synthetic_replay", "not_applicable"}
    assert rows[0]["realized_pnl_microdollars"] == 0
    required_metrics = {
        "jev_stocks_market_data_requests_total",
        "jev_stocks_market_data_request_latency_milliseconds",
        "jev_stocks_market_data_age_seconds",
        "jev_stocks_provider_calls_total",
        "jev_stocks_provider_status",
        "jev_stocks_provider_input_tokens_total",
        "jev_stocks_provider_output_tokens_total",
        "jev_stocks_provider_latency_milliseconds",
        "jev_stocks_provider_cost_microdollars_total",
        "jev_stocks_provider_unknown_cost",
        "jev_stocks_decisions_total",
        "jev_stocks_fill_price_microdollars",
        "jev_stocks_fill_fees_microdollars",
        "jev_stocks_fill_slippage_microdollars",
        "jev_stocks_fill_quantity",
        "jev_stocks_cash_microdollars",
        "jev_stocks_position_quantity",
        "jev_stocks_position_value_microdollars",
        "jev_stocks_trading_equity_microdollars",
        "jev_stocks_trading_pnl_microdollars",
        "jev_stocks_economic_equity_microdollars",
        "jev_stocks_economic_pnl_microdollars",
        "jev_stocks_failures_total",
        "jev_stocks_abstentions_total",
        "jev_stocks_cycle_status",
    }
    assert required_metrics <= {
        line.split("{")[0].split()[0]
        for line in metrics.splitlines()
        if line and not line.startswith("#")
    }
    assert "observation_id=" not in metrics
    assert "symbol=" not in metrics
    assert not (tmp_path / "stocks.prom.tmp").exists()


def test_replay_appends_complete_observations_and_cumulative_metrics(
    tmp_path: Path,
) -> None:
    replay_fixture(FIXTURE, tmp_path)
    first_metrics = (tmp_path / "stocks.prom").read_text()
    replay_fixture(FIXTURE, tmp_path)

    metrics = (tmp_path / "stocks.prom").read_text()
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 6
    assert metrics != first_metrics
    market_requests = (
        'jev_stocks_market_data_requests_total{provider="yahoo_chart_v8",'
        'status="success"} 6'
    )
    assert market_requests in metrics
    assert 'jev_stocks_decisions_total{arm="openai",action="BUY"} 2' in metrics
    assert 'jev_stocks_decisions_total{arm="jev",action="HOLD"} 2' in metrics
    assert 'jev_stocks_abstentions_total{arm="jev"} 2' in metrics
    assert 'jev_stocks_decisions_total{arm="deterministic",action="BUY"} 2' in metrics


def test_replay_accumulates_calls_and_failures_from_captured_rows(
    tmp_path: Path,
) -> None:
    fixture = json.loads(FIXTURE.read_text())
    fixture["source_mode"] = "captured_replay"
    provenance = "captured-provider-artifact-sha256:" + "a" * 64
    fixture["fixture_decisions"]["openai"].update(
        {
            "called": True,
            "cost_microdollars": 10,
            "input_tokens": 10,
            "latency_ms": 5,
            "output_tokens": 5,
            "source_provenance": provenance,
            "status": "success",
        }
    )
    fixture["fixture_decisions"]["jev"].update(
        {
            "called": True,
            "confidence_micros": 0,
            "source_provenance": provenance,
            "status": "provider_failure",
        }
    )
    path = tmp_path / "captured.json"
    path.write_text(json.dumps(fixture))

    replay_fixture(path, tmp_path / "output")
    replay_fixture(path, tmp_path / "output")

    metrics = (tmp_path / "output/stocks.prom").read_text()
    assert (
        'jev_stocks_provider_calls_total{provider="openai",'
        'model="gpt-5-2025-08-07"} 2'
        in metrics
    )
    assert 'jev_stocks_failures_total{arm="jev"} 2' in metrics


def test_decision_counters_preserve_prior_action_series(tmp_path: Path) -> None:
    replay_fixture(FIXTURE, tmp_path / "output")
    fixture = json.loads(FIXTURE.read_text())
    fixture["fixture_decisions"]["openai"].update(
        {"action": "HOLD", "symbol": None}
    )
    path = tmp_path / "hold.json"
    path.write_text(json.dumps(fixture))

    replay_fixture(path, tmp_path / "output")

    metrics = (tmp_path / "output/stocks.prom").read_text()
    assert 'jev_stocks_decisions_total{arm="openai",action="BUY"} 1' in metrics
    assert 'jev_stocks_decisions_total{arm="openai",action="HOLD"} 1' in metrics


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "jev"),
        ("model", "wrong-model"),
        ("action", "SELL"),
        ("symbol", "TSLA"),
        ("input_tokens", -1),
        ("output_tokens", -1),
        ("cost_microdollars", -1),
        ("latency_ms", -1),
        ("called", True),
        ("status", "success"),
    ],
)
def test_replay_rejects_invalid_provider_telemetry(
    tmp_path: Path, field: str, value: object
) -> None:
    fixture = json.loads(FIXTURE.read_text())
    fixture["fixture_decisions"]["openai"][field] = value
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(fixture))

    with pytest.raises(ValueError, match="fixture decision"):
        replay_fixture(path, tmp_path / "output")


def test_replay_rejects_unknown_source_mode(tmp_path: Path) -> None:
    fixture = json.loads(FIXTURE.read_text())
    fixture["source_mode"] = "live_public"
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(fixture))

    with pytest.raises(ValueError, match="source mode"):
        replay_fixture(path, tmp_path / "output")


def test_captured_replay_requires_provenance_for_each_model_arm(tmp_path: Path) -> None:
    fixture = json.loads(FIXTURE.read_text())
    fixture["source_mode"] = "captured_replay"
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(fixture))

    with pytest.raises(ValueError, match="fixture decision"):
        replay_fixture(path, tmp_path / "output")


@pytest.mark.parametrize("location", ["fixture", "quote", "decision"])
def test_replay_rejects_unknown_fields(tmp_path: Path, location: str) -> None:
    fixture = json.loads(FIXTURE.read_text())
    target = (
        fixture
        if location == "fixture"
        else fixture["quotes"][0]
        if location == "quote"
        else fixture["fixture_decisions"]["openai"]
    )
    target["unexpected"] = True
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(fixture))

    with pytest.raises(ValueError):
        replay_fixture(path, tmp_path / "output")


def test_replay_rejects_contradictory_provider_failure(tmp_path: Path) -> None:
    fixture = json.loads(FIXTURE.read_text())
    fixture["source_mode"] = "captured_replay"
    for decision in fixture["fixture_decisions"].values():
        decision.update(
            {
                "called": True,
                "status": "provider_failure",
                "action": "HOLD",
                "symbol": None,
                "confidence_micros": 0,
                "source_provenance": "captured-provider-artifact-sha256:" + "a" * 64,
            }
        )
    fixture["fixture_decisions"]["openai"].update(
        {"action": "BUY", "symbol": "AAPL", "confidence_micros": 900000}
    )
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(fixture))

    with pytest.raises(ValueError, match="fixture decision"):
        replay_fixture(path, tmp_path / "output")


@pytest.mark.parametrize(
    "provenance",
    ["", "captured-provider-artifact-sha256:test", "sha256:" + "a" * 64],
)
def test_captured_replay_rejects_invalid_provenance(
    tmp_path: Path, provenance: str
) -> None:
    fixture = json.loads(FIXTURE.read_text())
    fixture["source_mode"] = "captured_replay"
    for decision in fixture["fixture_decisions"].values():
        decision.update(
            {
                "called": True,
                "status": "success",
                "source_provenance": provenance,
            }
        )
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(fixture))

    with pytest.raises(ValueError, match="fixture decision"):
        replay_fixture(path, tmp_path / "output")


def test_failed_decision_never_fills() -> None:
    fixture = json.loads(FIXTURE.read_text())
    quotes = stock_slice._validate_quotes(fixture["quotes"])
    decision = copy.deepcopy(fixture["fixture_decisions"]["openai"])
    decision.update(
        {"called": True, "status": "provider_failure", "confidence_micros": 900000}
    )

    row = stock_slice._row(
        "id", "2026-10-09T20:00:00Z", "digest", quotes, "openai", decision, 1
    )

    assert row["terminal_status"] == "failed"
    assert row["fill"]["price_microdollars"] is None
    assert row["fill"]["quantity"] == 0
    assert row["position_quantity"] == 0


def test_stocks_replay_cli(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "stocks",
            "replay",
            "--fixture",
            str(FIXTURE),
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["terminal_rows"] == 3
    help_result = CliRunner().invoke(app, ["stocks", "observe", "--help"])
    assert help_result.exit_code == 0
    assert "--symbols" in help_result.stdout
    assert "broker" not in help_result.stdout.casefold()


def test_openai_response_accepts_one_message_after_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = json.loads(FIXTURE.read_text())
    quotes = stock_slice._validate_quotes(fixture["quotes"])
    envelope: dict[str, object] = {
        "output": [
            {"id": "reasoning", "status": "completed", "type": "reasoning"},
            {
                "content": [
                    {
                        "text": json.dumps(
                            {"action": "BUY", "symbol": "AAPL", "confidence": 0.8}
                        ),
                        "type": "output_text",
                    }
                ],
                "status": "completed",
                "type": "message",
            },
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    captured: dict[str, object] = {}

    def fake_urlopen(request: object, timeout: int) -> _Response:
        captured["body"] = json.loads(getattr(request, "data"))
        captured["timeout"] = timeout
        return _Response(envelope)

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("OPENAI_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS", "1")
    monkeypatch.setenv("OPENAI_CACHED_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS", "1")
    monkeypatch.setenv("OPENAI_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS", "1")
    monkeypatch.setattr(stock_slice.urllib.request, "urlopen", fake_urlopen)

    decision = stock_slice._provider_decision("openai", b"{}", quotes)

    assert decision["status"] == "success"
    assert decision["symbol"] == "AAPL"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["strict"] is True


def test_openai_response_rejects_multiple_completed_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = json.loads(FIXTURE.read_text())
    quotes = stock_slice._validate_quotes(fixture["quotes"])
    message = {
        "content": [
            {
                "text": json.dumps(
                    {"action": "HOLD", "symbol": None, "confidence": 0.5}
                ),
                "type": "output_text",
            }
        ],
        "status": "completed",
        "type": "message",
    }
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(
        stock_slice.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(
            {"output": [message, copy.deepcopy(message)]}
        ),
    )

    assert stock_slice._provider_decision("openai", b"{}", quotes)["status"] == (
        "provider_failure"
    )


def test_openai_response_rejects_extra_completed_message_without_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = json.loads(FIXTURE.read_text())
    quotes = stock_slice._validate_quotes(fixture["quotes"])
    message = {
        "content": [
            {
                "text": json.dumps(
                    {"action": "HOLD", "symbol": None, "confidence": 0.5}
                ),
                "type": "output_text",
            }
        ],
        "status": "completed",
        "type": "message",
    }
    empty_message = {"content": [], "status": "completed", "type": "message"}
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(
        stock_slice.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(
            {"output": [message, empty_message]}
        ),
    )

    assert stock_slice._provider_decision("openai", b"{}", quotes)["status"] == (
        "provider_failure"
    )


@pytest.mark.parametrize(
    "answer",
    [
        [],
        {"action": "HOLD", "symbol": None, "confidence": 0.5, "extra": True},
        {"action": "HOLD", "symbol": None},
        {"action": "HOLD", "symbol": None, "confidence": True},
        {"action": "HOLD", "symbol": None, "confidence": 1.1},
    ],
)
def test_openai_response_rejects_decisions_outside_exact_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: object
) -> None:
    del tmp_path
    fixture = json.loads(FIXTURE.read_text())
    quotes = stock_slice._validate_quotes(fixture["quotes"])
    envelope = {
        "output": [
            {
                "content": [{"text": json.dumps(answer), "type": "output_text"}],
                "status": "completed",
                "type": "message",
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(
        stock_slice.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(envelope),
    )

    decision = stock_slice._provider_decision("openai", b"{}", quotes)

    assert decision["status"] == "provider_failure"
    assert decision["action"] == "HOLD"
    assert decision["symbol"] is None


def test_openai_response_rejects_non_object_envelope_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = json.loads(FIXTURE.read_text())
    quotes = stock_slice._validate_quotes(fixture["quotes"])
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(
        stock_slice.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response([]),
    )

    decision = stock_slice._provider_decision("openai", b"{}", quotes)

    assert decision["status"] == "provider_failure"
    assert decision["action"] == "HOLD"
    assert decision["symbol"] is None


def test_openai_response_rejects_negative_token_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = json.loads(FIXTURE.read_text())
    quotes = stock_slice._validate_quotes(fixture["quotes"])
    envelope = {
        "output": [
            {
                "content": [
                    {
                        "text": json.dumps(
                            {"action": "HOLD", "symbol": None, "confidence": 0.5}
                        ),
                        "type": "output_text",
                    }
                ],
                "status": "completed",
                "type": "message",
            }
        ],
        "usage": {"input_tokens": -1, "output_tokens": 5},
    }
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(
        stock_slice.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(envelope),
    )

    assert stock_slice._provider_decision("openai", b"{}", quotes)["status"] == (
        "provider_failure"
    )


def test_observe_without_keys_fails_model_arms_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = json.loads(FIXTURE.read_text())
    by_symbol = {quote["symbol"]: quote for quote in fixture["quotes"]}
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(stock_slice, "_fetch_quote", by_symbol.__getitem__)

    summary = stock_slice.observe_public("AAPL,MSFT,NVDA", tmp_path)

    assert summary["arms"] == {
        "openai": "failed",
        "jev": "failed",
        "deterministic": "completed",
    }
    rows = _rows(tmp_path / "results.jsonl")
    assert [row["failure"] for row in rows[:2]] == [
        "configuration_failure",
        "configuration_failure",
    ]
    assert all(row["provider"]["called"] is False for row in rows[:2])
    assert all(row["source_mode"] == "live_public" for row in rows)


def test_observe_market_data_failure_writes_one_terminal_cycle_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = json.loads(FIXTURE.read_text())
    first_quote = fixture["quotes"][0]
    calls = 0

    def fail_second_request(_symbol: str) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise urllib.error.URLError("offline")
        return first_quote

    monkeypatch.setattr(stock_slice, "_fetch_quote", fail_second_request)

    summary = stock_slice.observe_public("AAPL,MSFT", tmp_path)

    rows = _rows(tmp_path / "results.jsonl")
    assert summary["arms"] == {"cycle": "failed"}
    assert summary["terminal_rows"] == 1
    assert len(rows) == 1
    assert rows[0]["arm"] == "cycle"
    assert rows[0]["failure"] == "market_data_failure"
    assert rows[0]["market_data"]["request_count"] == 2
    assert rows[0]["market_data"]["request_latency_ms"] >= 0
    assert rows[0]["market_data"]["quotes"] == []
    assert "decision" not in rows[0]
    assert "provider" not in rows[0]
    metrics = (tmp_path / "stocks.prom").read_text()
    assert 'jev_stocks_cycle_status{status="failure"} 1' in metrics
    failed_market_data = (
        'jev_stocks_market_data_requests_total{provider="yahoo_chart_v8",'
        'status="failure"} 1'
    )
    assert failed_market_data.replace("} 1", "} 2") in metrics
    assert "jev_stocks_market_data_request_latency_milliseconds" in metrics


def test_known_provider_cost_reduces_economic_not_trading_equity() -> None:
    fixture = json.loads(FIXTURE.read_text())
    quotes = stock_slice._validate_quotes(fixture["quotes"])
    decision = copy.deepcopy(fixture["fixture_decisions"]["openai"])
    decision.update(
        {
            "called": True,
            "cost_microdollars": 90,
            "input_tokens": 180,
            "latency_ms": 125,
            "output_tokens": 18,
            "source_provenance": "captured-provider-artifact-sha256:test",
            "status": "success",
        }
    )

    openai = stock_slice._row(
        "id", "2026-10-09T20:00:00Z", "digest", quotes, "openai", decision, 1
    )

    assert openai["trading_equity_microdollars"] == openai["equity_microdollars"]
    assert openai["economic_equity_microdollars"] == (
        openai["trading_equity_microdollars"] - 90
    )
    assert openai["economic_pnl_microdollars"] == (
        openai["trading_pnl_microdollars"] - 90
    )
    assert openai["economic_cost_unknown"] is False


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": -1, "output_tokens": 5},
        {"input_tokens": 5, "output_tokens": -1},
        {"input_tokens": 1.5, "output_tokens": 5},
        {"input_tokens": 5, "output_tokens": True},
    ],
)
def test_provider_cost_rejects_invalid_usage_without_credit(
    monkeypatch: pytest.MonkeyPatch, usage: dict[str, object]
) -> None:
    monkeypatch.setenv("OPENAI_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS", "1000000")
    monkeypatch.setenv(
        "OPENAI_CACHED_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS", "1000000"
    )
    monkeypatch.setenv("OPENAI_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS", "1000000")

    assert stock_slice._provider_cost("openai", usage) is None


def test_negative_provider_cost_cannot_increase_equity() -> None:
    fixture = json.loads(FIXTURE.read_text())
    quotes = stock_slice._validate_quotes(fixture["quotes"])
    decision = copy.deepcopy(fixture["fixture_decisions"]["openai"])
    decision.update(
        {"called": True, "cost_microdollars": -90, "status": "provider_failure"}
    )

    row = stock_slice._row(
        "id", "2026-10-09T20:00:00Z", "digest", quotes, "openai", decision, 1
    )

    assert row["economic_cost_unknown"] is True
    assert row["economic_equity_microdollars"] is None


def test_unknown_called_provider_cost_marks_economics_unknown() -> None:
    fixture = json.loads(FIXTURE.read_text())
    quotes = stock_slice._validate_quotes(fixture["quotes"])
    decision = copy.deepcopy(fixture["fixture_decisions"]["openai"])
    decision.update(
        {"called": True, "cost_microdollars": None, "status": "provider_failure"}
    )

    row = stock_slice._row(
        "id", "2026-10-09T20:00:00Z", "digest", quotes, "openai", decision, 1
    )

    assert row["economic_cost_unknown"] is True
    assert row["economic_equity_microdollars"] is None
    assert row["economic_pnl_microdollars"] is None

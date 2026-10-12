from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jev_trading_lab.cli import app
from jev_trading_lab.stocks import slice as stock_slice
from jev_trading_lab.stocks.slice import replay_fixture

FIXTURE = Path(__file__).parents[1] / "fixtures/stocks/observation.json"


def test_replay_writes_three_terminal_arms_and_metrics(tmp_path: Path) -> None:
    summary = replay_fixture(FIXTURE, tmp_path)

    rows = [
        json.loads(line)
        for line in (tmp_path / "results.jsonl").read_text().splitlines()
    ]
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
    assert all(row["initial_cash_microdollars"] == 10_000_000_000 for row in rows)
    assert all(row["process"]["cycle_duration_ms"] >= 0 for row in rows)
    assert all(len(row["market_data"]["quotes"]) == 3 for row in rows)
    assert rows[0]["provider"]["status"] in {"success", "not_applicable"}
    assert rows[0]["realized_pnl_microdollars"] == 0
    assert "jev_stocks_terminal" in (tmp_path / "stocks.prom").read_text()
    assert not (tmp_path / "stocks.prom.tmp").exists()


def test_replay_appends_complete_observations(tmp_path: Path) -> None:
    replay_fixture(FIXTURE, tmp_path)
    replay_fixture(FIXTURE, tmp_path)

    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 6


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
    rows = [
        json.loads(line)
        for line in (tmp_path / "results.jsonl").read_text().splitlines()
    ]
    assert [row["failure"] for row in rows[:2]] == [
        "configuration_failure",
        "configuration_failure",
    ]
    assert all(row["provider"]["called"] is False for row in rows[:2])

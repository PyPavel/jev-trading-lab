from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jev_trading_lab.cli import app

FIXTURE = Path(__file__).parents[1] / "fixtures/polymarket/feasibility_cycle.json"
ROOT = Path(__file__).parents[2]


def _source_bytecode() -> tuple[Path, ...]:
    return tuple(
        path
        for root in (ROOT / "src", ROOT / "tests")
        for pattern in ("__pycache__", "*.py[co]")
        for path in root.rglob(pattern)
    )


@pytest.fixture(autouse=True)
def _local_test_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    from jev_trading_lab.polymarket import store

    defaults = dict(store.BlobStore.__init__.__kwdefaults__ or {})
    defaults["filesystem_resolver"] = lambda _path: "ext4"
    monkeypatch.setattr(store.BlobStore.__init__, "__kwdefaults__", defaults)


def test_typer_replay_prints_exactly_one_stable_json_object(tmp_path: Path) -> None:
    output = tmp_path / "replay"
    runner = CliRunner()

    first = runner.invoke(
        app,
        [
            "polymarket",
            "replay",
            "--fixture",
            str(FIXTURE),
            "--output-dir",
            str(output),
        ],
    )
    second = runner.invoke(
        app,
        [
            "polymarket",
            "replay",
            "--fixture",
            str(FIXTURE),
            "--output-dir",
            str(output),
        ],
    )

    assert first.exit_code == second.exit_code == 0
    assert first.stderr == second.stderr == ""
    assert first.stdout == second.stdout
    assert first.stdout.count("\n") == 1
    summary = json.loads(first.stdout)
    assert (
        first.stdout
        == json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n"
    )
    assert set(summary) == {
        "ai",
        "artifact_count",
        "artifact_hashes",
        "baseline_brier_micros",
        "baseline_probability_micros",
        "control",
        "observation_id",
        "opportunity_id",
        "paired_brier_delta_micros",
        "paired_economic_pnl_delta_microdollars",
        "paired_trading_pnl_delta_microdollars",
    }


def test_actual_console_subprocess_needs_no_credentials() -> None:
    output = ROOT / ".tmp" / "pytest-polymarket-subprocess-replay"
    shutil.rmtree(output, ignore_errors=True)
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "OPENAI_API_KEY",
            "PYTHONDONTWRITEBYTECODE",
            "TYPESAFE_API_KEY",
        }
    }
    before = _source_bytecode()
    assert before == ()
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "jev_trading_lab.cli",
            "polymarket",
            "replay",
            "--fixture",
            str(FIXTURE),
            "--output-dir",
            str(output),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    try:
        assert completed.returncode == 0
        assert completed.stderr == ""
        assert json.loads(completed.stdout)["artifact_count"] == 11
        assert _source_bytecode() == before
    finally:
        shutil.rmtree(output, ignore_errors=True)


def test_help_exposes_public_observe_but_no_execution_capability() -> None:
    runner = CliRunner()
    root_help = runner.invoke(app, ["--help"])
    polymarket_help = runner.invoke(app, ["polymarket", "--help"])
    replay_help = runner.invoke(app, ["polymarket", "replay", "--help"])
    observe_help = runner.invoke(app, ["polymarket", "observe", "--help"])
    text = "".join(
        result.stdout
        for result in (root_help, polymarket_help, replay_help, observe_help)
    ).casefold()

    assert all(
        result.exit_code == 0
        for result in (root_help, polymarket_help, replay_help, observe_help)
    )
    assert "observe" in polymarket_help.stdout.casefold()
    assert "--limit" in observe_help.stdout.casefold()
    for forbidden in (
        "wallet",
        "sign",
        "approve",
        "deposit",
        "withdraw",
        "execute",
    ):
        assert forbidden not in text


def test_cli_error_is_sanitized_and_nonzero(tmp_path: Path) -> None:
    missing = tmp_path / "secret-token-fixture.json"
    result = CliRunner().invoke(
        app,
        [
            "polymarket",
            "replay",
            "--fixture",
            str(missing),
            "--output-dir",
            str(tmp_path / "output"),
        ],
    )

    assert result.exit_code != 0
    assert result.stdout == ""
    assert "secret-token-fixture" not in result.stderr
    assert "replay failed" in result.stderr

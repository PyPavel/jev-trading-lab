from __future__ import annotations

import json
import socket
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from jev_trading_lab.polymarket.models import load_strict_json
from jev_trading_lab.polymarket.pipeline import replay_fixture
from jev_trading_lab.polymarket.store import load_observation

FIXTURE = Path(__file__).parents[1] / "fixtures/polymarket/feasibility_cycle.json"


@pytest.fixture(autouse=True)
def _offline_private_store(monkeypatch: pytest.MonkeyPatch) -> None:
    from jev_trading_lab.polymarket import store

    defaults = dict(store.BlobStore.__init__.__kwdefaults__ or {})
    defaults["filesystem_resolver"] = lambda _path: "ext4"
    monkeypatch.setattr(store.BlobStore.__init__, "__kwdefaults__", defaults)

    def no_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("offline replay attempted network access")

    monkeypatch.setattr(socket, "socket", no_network)
    try:
        import urllib.request

        monkeypatch.setattr(urllib.request, "urlopen", no_network)
    except ImportError:  # pragma: no cover
        pass


def _fixture() -> dict[str, Any]:
    value = load_strict_json(FIXTURE.read_bytes())
    assert isinstance(value, dict)
    return value


def test_replay_is_offline_ordered_and_matches_recomputed_fixture(
    tmp_path: Path,
) -> None:
    output = tmp_path / "replay"
    events: list[str] = []

    first = replay_fixture(FIXTURE, output, event_sink=events.append)
    second = replay_fixture(FIXTURE, output)
    fixture = _fixture()

    assert first == second
    assert (
        first["observation_id"]
        == fixture["expected"]["observation_result"]["observation_id"]
    )
    assert first["ai"] == {
        "action": "BUY_YES",
        "brier_micros": 90000,
        "disposition": "FILLED",
        "economic_pnl_microdollars": 223500000,
        "forecast_probability_micros": 700000,
        "forecast_status": "SCORED",
        "quantity": 500,
        "trading_pnl_microdollars": 224000000,
    }
    assert first["control"]["disposition"] == "NO_EDGE"
    assert first["artifact_count"] == 11
    assert events.index("decision_frozen") < events.index("execution_book_accessed")

    result_lines = (output / "results.jsonl").read_text().splitlines()
    artifact_lines = (output / "artifacts.jsonl").read_text().splitlines()
    assert len(result_lines) == 1
    assert len(artifact_lines) == 11
    assert [json.loads(line)["kind"] for line in artifact_lines] == [
        "raw_gamma",
        "sanitized_evidence",
        "openai_request",
        "openai_response_success",
        "openai_success_metadata",
        "research_brief",
        "jev_request",
        "jev_response_success",
        "jev_success_metadata",
        "jev_decision",
        "decision_frozen",
    ]
    replay = load_observation(output, first["observation_id"])
    assert (
        replay.result.model_dump(mode="json")
        == fixture["expected"]["observation_result"]
    )
    assert first["artifact_hashes"] == {
        kind: envelope.sha256 for kind, envelope in replay.envelopes.items()
    }


def test_request_artifacts_are_persisted_before_transport(
    tmp_path: Path,
) -> None:
    from jev_trading_lab.polymarket.ai import TransportResponse

    fixture = _fixture()
    output = tmp_path / "request-order"

    def openai_transport(request_bytes: bytes) -> TransportResponse:
        kinds = [
            json.loads(line)["kind"]
            for line in (output / "artifacts.jsonl").read_text().splitlines()
        ]
        assert kinds[-1] == "openai_request"
        return TransportResponse(
            status_code=200,
            headers={},
            raw_bytes=fixture["model_artifacts"]["openai_response_bytes"].encode(),
            retrieved_at=datetime.fromisoformat(
                fixture["chronology"]["openai_response_at"].replace("Z", "+00:00")
            ),
            billed_cost_microdollars=fixture["model_costs_microdollars"]["openai"],
        )

    replay_fixture(FIXTURE, output, openai_transport=openai_transport)


def test_decision_artifact_is_persisted_before_execution_book_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jev_trading_lab.polymarket import pipeline

    original = pipeline.validate_feasibility_fixture

    def checked_validate(raw: dict[str, Any]) -> None:
        journal = tmp_path / "replay" / "artifacts.jsonl"
        assert journal.is_file()
        assert json.loads(journal.read_text().splitlines()[-1])["kind"] == (
            "decision_frozen"
        )
        original(raw)

    monkeypatch.setattr(pipeline, "validate_feasibility_fixture", checked_validate)
    replay_fixture(FIXTURE, tmp_path / "replay")


def test_openai_failure_records_both_terminal_arms(tmp_path: Path) -> None:
    calls = 0

    def timeout(_request: bytes) -> None:
        nonlocal calls
        calls += 1
        raise TimeoutError

    summary = replay_fixture(
        FIXTURE,
        tmp_path / "failure",
        openai_transport=timeout,
    )

    assert calls == 1
    assert summary["ai"]["disposition"] == "MODEL_FAILURE"
    assert summary["ai"]["quantity"] == 0
    assert summary["ai"]["economic_pnl_microdollars"] == 0
    assert summary["control"]["disposition"] == "NO_EDGE"
    assert summary["control"]["quantity"] == 0
    assert summary["artifact_count"] == 4


def test_corrupt_existing_artifact_fails_closed(tmp_path: Path) -> None:
    output = tmp_path / "replay"
    summary = replay_fixture(FIXTURE, output)
    replay = load_observation(output, summary["observation_id"])
    blob = output / "blobs" / replay.envelopes["research_brief"].storage_relpath
    blob.write_bytes(b"corrupt")

    with pytest.raises(Exception, match="corrupt|hash|blob"):
        replay_fixture(FIXTURE, output)


def test_fixture_must_be_regular_and_not_a_symlink(tmp_path: Path) -> None:
    link = tmp_path / "fixture.json"
    link.symlink_to(FIXTURE)

    with pytest.raises(ValueError, match="regular|symlink|fixture"):
        replay_fixture(link, tmp_path / "replay")

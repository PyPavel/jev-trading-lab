from __future__ import annotations

import json
import socket
from collections.abc import Iterator
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


class _AccessTrap(dict[str, Any]):
    def __init__(self, value: dict[str, Any], opened: list[bool], accesses: list[str]):
        super().__init__(value)
        self._opened = opened
        self._accesses = accesses

    def __getitem__(self, key: str) -> Any:
        if key in {
            "execution_book",
            "precheck_book",
            "economics",
            "expected",
            "resolution",
        }:
            assert self._opened[0], f"{key} accessed before durable decision barrier"
            self._accesses.append(key)
        value = super().__getitem__(key)
        if isinstance(value, dict):
            return _AccessTrap(value, self._opened, self._accesses)
        return value

    def __iter__(self) -> Iterator[str]:
        return super().__iter__()


def test_success_does_not_access_price_data_until_barrier_is_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jev_trading_lab.polymarket import pipeline

    raw = _fixture()
    opened = [False]
    accesses: list[str] = []
    monkeypatch.setattr(
        pipeline, "_load_fixture", lambda _path: _AccessTrap(raw, opened, accesses)
    )
    original_freeze = pipeline.freeze_observation_artifacts

    def durable_freeze(*args: Any, **kwargs: Any) -> Any:
        result = original_freeze(*args, **kwargs)
        artifacts = list(args[2])
        if artifacts[-1].kind == "decision_frozen":
            journal = tmp_path / "success" / "artifacts.jsonl"
            assert json.loads(journal.read_text().splitlines()[-1])["kind"] == (
                "decision_frozen"
            )
            opened[0] = True
        return result

    monkeypatch.setattr(pipeline, "freeze_observation_artifacts", durable_freeze)
    replay_fixture(FIXTURE, tmp_path / "success")

    assert accesses
    assert accesses[0] in {"execution_book", "precheck_book", "economics", "expected"}


@pytest.mark.parametrize("provider", ["openai", "jev"])
def test_failure_persists_decision_barrier_before_price_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    from jev_trading_lab.polymarket import pipeline

    raw = _fixture()
    opened = [False]
    accesses: list[str] = []
    monkeypatch.setattr(
        pipeline, "_load_fixture", lambda _path: _AccessTrap(raw, opened, accesses)
    )
    original_freeze = pipeline.freeze_observation_artifacts

    def durable_freeze(*args: Any, **kwargs: Any) -> Any:
        result = original_freeze(*args, **kwargs)
        artifacts = list(args[2])
        if artifacts[-1].kind == "decision_frozen":
            journal = tmp_path / provider / "artifacts.jsonl"
            kinds = [
                json.loads(line)["kind"] for line in journal.read_text().splitlines()
            ]
            assert kinds[-1] == "decision_frozen"
            opened[0] = True
        return result

    monkeypatch.setattr(pipeline, "freeze_observation_artifacts", durable_freeze)

    def timeout(_request: bytes) -> None:
        raise TimeoutError

    kwargs = {f"{provider}_transport": timeout}
    replay_fixture(FIXTURE, tmp_path / provider, **kwargs)

    assert accesses
    kinds = [
        json.loads(line)["kind"]
        for line in (tmp_path / provider / "artifacts.jsonl").read_text().splitlines()
    ]
    assert kinds[-1] == "decision_frozen"
    assert "openai_failure" in kinds or "jev_failure" in kinds


def test_crash_after_durable_barrier_recovers_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jev_trading_lab.polymarket import pipeline

    output = tmp_path / "crash"
    original_get = pipeline._ExecutionBookGate.get
    crashed = False

    def crash_once(gate: Any) -> Any:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("crash after barrier")
        return original_get(gate)

    monkeypatch.setattr(pipeline._ExecutionBookGate, "get", crash_once)
    with pytest.raises(RuntimeError, match="crash after barrier"):
        replay_fixture(FIXTURE, output)

    assert not (output / "results.jsonl").read_text()
    assert (
        json.loads((output / "artifacts.jsonl").read_text().splitlines()[-1])["kind"]
        == "decision_frozen"
    )
    summary = replay_fixture(FIXTURE, output)
    assert summary["artifact_count"] == 11
    assert len((output / "results.jsonl").read_text().splitlines()) == 1


def test_complete_fixture_validation_failure_leaves_only_durable_prefix(
    tmp_path: Path,
) -> None:
    raw = _fixture()
    raw["economics"]["starting_cash_microdollars"] += 1
    fixture_path = tmp_path / "invalid.json"
    fixture_path.write_text(json.dumps(raw))
    output = tmp_path / "invalid"

    with pytest.raises(ValueError, match="starting capital"):
        replay_fixture(fixture_path, output)

    assert not (output / "results.jsonl").read_text()
    kinds = [
        json.loads(line)["kind"]
        for line in (output / "artifacts.jsonl").read_text().splitlines()
    ]
    assert kinds[-1] == "decision_frozen"

    fixture_path.write_bytes(FIXTURE.read_bytes())
    summary = replay_fixture(fixture_path, output)
    assert summary["artifact_count"] == 11
    assert len((output / "results.jsonl").read_text().splitlines()) == 1


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
    assert summary["artifact_count"] == 5


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

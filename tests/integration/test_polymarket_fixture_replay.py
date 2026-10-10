from __future__ import annotations

import hashlib
import json
import socket
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
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


@pytest.mark.parametrize("body", (b'{"error":"unavailable"}', b""))
def test_openai_http_failure_barrier_binds_response_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    from jev_trading_lab.polymarket import pipeline
    from jev_trading_lab.polymarket.ai import TransportResponse

    output = tmp_path / "http-failure"
    opened = [False]
    accesses: list[str] = []
    monkeypatch.setattr(
        pipeline,
        "_load_fixture",
        lambda _path: _AccessTrap(_fixture(), opened, accesses),
    )
    original_freeze = pipeline.freeze_observation_artifacts

    def durable_freeze(*args: Any, **kwargs: Any) -> Any:
        result = original_freeze(*args, **kwargs)
        artifacts = list(args[2])
        if artifacts[-1].kind == "decision_frozen":
            barrier = json.loads(artifacts[-1].payload)
            assert barrier["openai_response_sha256"] == hashlib.sha256(body).hexdigest()
            opened[0] = True
        return result

    monkeypatch.setattr(pipeline, "freeze_observation_artifacts", durable_freeze)

    def failed(_request: bytes) -> TransportResponse:
        return TransportResponse(
            status_code=500,
            headers={},
            raw_bytes=body,
            retrieved_at=datetime(2026, 10, 3, 12, 0, 30, tzinfo=UTC),
            billed_cost_microdollars=17,
        )

    summary = replay_fixture(FIXTURE, output, openai_transport=failed)
    replay = load_observation(output, summary["observation_id"])
    failure = json.loads(replay.artifacts["openai_failure"])
    barrier = json.loads(replay.artifacts["decision_frozen"])
    digest = hashlib.sha256(body).hexdigest()

    assert failure["response_sha256"] == digest
    assert failure["response_byte_length"] == len(body)
    assert barrier["openai_response_sha256"] == digest
    assert barrier["failure_code"] == failure["code"]
    assert barrier["openai_request_sha256"] == failure["request_sha256"]
    assert barrier["openai_cost_microdollars"] == failure["billed_cost_microdollars"]
    if body:
        assert body not in b"".join(replay.artifacts.values())
    assert accesses


def test_jev_http_failure_barrier_binds_response_hash(tmp_path: Path) -> None:
    from jev_trading_lab.polymarket.ai import TransportResponse

    body = b'{"error":"jev unavailable"}'
    output = tmp_path / "jev-http-failure"

    def failed(_request: bytes) -> TransportResponse:
        return TransportResponse(
            status_code=503,
            headers={},
            raw_bytes=body,
            retrieved_at=datetime(2026, 10, 3, 12, 1, 20, tzinfo=UTC),
            billed_cost_microdollars=19,
        )

    summary = replay_fixture(FIXTURE, output, jev_transport=failed)
    replay = load_observation(output, summary["observation_id"])
    failure = json.loads(replay.artifacts["jev_failure"])
    barrier = json.loads(replay.artifacts["decision_frozen"])

    assert failure["response_sha256"] == hashlib.sha256(body).hexdigest()
    assert barrier["failure_code"] == failure["code"]
    assert barrier["jev_response_sha256"] == failure["response_sha256"]
    assert body not in b"".join(replay.artifacts.values())


@pytest.mark.parametrize(
    ("failure_code", "mutate"),
    (
        ("PARSE_ERROR", lambda _raw: b"not-json"),
        (
            "MODEL_MISMATCH",
            lambda raw: json.dumps(
                {**json.loads(raw), "model": "unapproved-model"}, separators=(",", ":")
            ).encode(),
        ),
        (
            "CITATION_ERROR",
            lambda raw: _openai_response_with_bad_citation(raw),
        ),
        (
            "CREDENTIAL_REFLECTION",
            lambda _raw: b"Authorization: Bearer reflected-secret",
        ),
    ),
)
def test_openai_response_failure_variants_bind_barrier_without_persisting_body(
    tmp_path: Path,
    failure_code: str,
    mutate: Callable[[bytes], bytes],
) -> None:
    from jev_trading_lab.polymarket.ai import TransportResponse

    fixture = _fixture()
    original = fixture["model_artifacts"]["openai_response_bytes"].encode()
    body = mutate(original)
    output = tmp_path / failure_code.lower()

    def failed(_request: bytes) -> TransportResponse:
        return TransportResponse(
            status_code=200,
            headers={},
            raw_bytes=body,
            retrieved_at=datetime(2026, 10, 3, 12, 0, 30, tzinfo=UTC),
            billed_cost_microdollars=23,
        )

    summary = replay_fixture(FIXTURE, output, openai_transport=failed)
    replay = load_observation(output, summary["observation_id"])
    failure = json.loads(replay.artifacts["openai_failure"])
    barrier = json.loads(replay.artifacts["decision_frozen"])

    assert failure["code"] == failure_code
    assert failure["response_sha256"] == hashlib.sha256(body).hexdigest()
    assert barrier["failure_code"] == failure_code
    assert barrier["openai_response_sha256"] == failure["response_sha256"]
    assert body not in b"".join(replay.artifacts.values())


@pytest.mark.parametrize("exception", (TimeoutError, RuntimeError))
def test_openai_pre_response_failures_leave_null_barrier_response_hash(
    tmp_path: Path, exception: type[Exception]
) -> None:
    output = tmp_path / exception.__name__

    def failed(_request: bytes) -> None:
        raise exception

    summary = replay_fixture(FIXTURE, output, openai_transport=failed)
    replay = load_observation(output, summary["observation_id"])
    failure = json.loads(replay.artifacts["openai_failure"])
    barrier = json.loads(replay.artifacts["decision_frozen"])

    assert failure["code"] == (
        "TIMEOUT" if exception is TimeoutError else "PROVIDER_ERROR"
    )
    assert failure["response_sha256"] is None
    assert barrier["failure_code"] == failure["code"]
    assert barrier["openai_response_sha256"] is None


def _openai_response_with_bad_citation(raw: bytes) -> bytes:
    value = json.loads(raw)
    message = next(item for item in value["output"] if item["type"] == "message")
    brief = json.loads(message["content"][0]["text"])
    brief["cited_facts"][0]["evidence_sha256"] = "0" * 64
    message["content"][0]["text"] = json.dumps(brief, separators=(",", ":"))
    return json.dumps(value, separators=(",", ":")).encode()


@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_request_input_failure_closes_without_request_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    from jev_trading_lab.polymarket import pipeline

    output = tmp_path / provider
    opened = [False]
    accesses: list[str] = []
    monkeypatch.setattr(
        pipeline,
        "_load_fixture",
        lambda _path: _AccessTrap(_fixture(), opened, accesses),
    )
    original_freeze = pipeline.freeze_observation_artifacts

    def durable_freeze(*args: Any, **kwargs: Any) -> Any:
        result = original_freeze(*args, **kwargs)
        artifacts = list(args[2])
        if artifacts[-1].kind == "decision_frozen":
            opened[0] = True
        return result

    monkeypatch.setattr(pipeline, "freeze_observation_artifacts", durable_freeze)
    monkeypatch.setattr(pipeline, f"{provider.upper()}_MODEL_ID", "invalid-model")

    first = replay_fixture(FIXTURE, output)
    second = replay_fixture(FIXTURE, output)
    replay = load_observation(output, first["observation_id"])
    kinds = list(replay.artifacts)
    failure = json.loads(replay.artifacts[f"{provider}_failure"])
    barrier = json.loads(replay.artifacts["decision_frozen"])
    empty_hash = hashlib.sha256(b"").hexdigest()

    assert first == second
    assert accesses
    assert f"{provider}_request" not in kinds
    assert failure["code"] == "INPUT_ERROR"
    assert failure["request_sha256"] == empty_hash
    assert barrier[f"{provider}_request_sha256"] == empty_hash
    assert kinds[-2:] == [f"{provider}_failure", "decision_frozen"]
    assert len((output / "results.jsonl").read_text().splitlines()) == 1
    assert replay.result.ai.disposition == "MODEL_FAILURE"
    assert replay.result.ai.quantity == replay.result.deterministic.quantity == 0


class _NoBookAccessTrap(dict[str, Any]):
    def __init__(self, value: dict[str, Any], accesses: list[str]):
        super().__init__(value)
        self._accesses = accesses

    def __getitem__(self, key: str) -> Any:
        if key in {
            "execution_book",
            "precheck_book",
            "economics",
            "expected",
            "resolution",
        }:
            self._accesses.append(key)
            raise AssertionError(f"late path accessed {key}")
        value = super().__getitem__(key)
        if isinstance(value, dict):
            return _NoBookAccessTrap(value, self._accesses)
        return value


@pytest.mark.parametrize(("provider", "status_code"), (("openai", 500), ("jev", 503)))
def test_late_provider_failure_terminalizes_without_book_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    status_code: int,
) -> None:
    from jev_trading_lab.polymarket import pipeline
    from jev_trading_lab.polymarket.ai import TransportResponse

    output = tmp_path / provider
    accesses: list[str] = []
    monkeypatch.setattr(
        pipeline,
        "_load_fixture",
        lambda _path: _NoBookAccessTrap(_fixture(), accesses),
    )

    def late_failure(_request: bytes) -> TransportResponse:
        return TransportResponse(
            status_code=status_code,
            headers={},
            raw_bytes=b"late failure body",
            retrieved_at=datetime(2026, 10, 3, 12, 2, tzinfo=UTC),
            billed_cost_microdollars=31,
        )

    events: list[str] = []
    kwargs: dict[str, Any] = {
        f"{provider}_transport": late_failure,
        "event_sink": events.append,
    }
    summary = replay_fixture(FIXTURE, output, **kwargs)
    replay = load_observation(output, summary["observation_id"])
    kinds = list(replay.artifacts)
    unavailable = json.loads(replay.artifacts["book_unavailable"])

    assert accesses == []
    assert events == ["decision_frozen"]
    assert kinds[-2:] == ["decision_frozen", "book_unavailable"]
    assert unavailable["reason"] == "STALE_PRECHECK"
    assert unavailable["planned_retrieved_at"] == "2026-10-03T12:02:00Z"
    assert replay.result.execution_book_sha256 is None
    assert replay.result.baseline_probability_micros is None
    assert replay.result.ai.disposition == "BOOK_FAILURE"
    assert replay.result.ai.quantity == replay.result.deterministic.quantity == 0
    assert replay.result.ai.trading_pnl_microdollars == 0
    assert replay.result.ai.economic_pnl_microdollars == -(
        31
        + (_fixture()["model_costs_microdollars"]["openai"] if provider == "jev" else 0)
    )
    assert replay.result.deterministic.economic_pnl_microdollars == 0
    assert len((output / "results.jsonl").read_text().splitlines()) == 1


def test_late_jev_success_preserves_forecast_without_book_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jev_trading_lab.polymarket import pipeline
    from jev_trading_lab.polymarket.ai import TransportResponse

    raw = _fixture()
    output = tmp_path / "late-success"
    accesses: list[str] = []
    monkeypatch.setattr(
        pipeline,
        "_load_fixture",
        lambda _path: _NoBookAccessTrap(raw, accesses),
    )

    def late_success(_request: bytes) -> TransportResponse:
        return TransportResponse(
            status_code=200,
            headers={},
            raw_bytes=raw["model_artifacts"]["jev_response_bytes"].encode(),
            retrieved_at=datetime(2026, 10, 3, 12, 2, tzinfo=UTC),
            billed_cost_microdollars=raw["model_costs_microdollars"]["jev"],
        )

    events: list[str] = []
    first = replay_fixture(
        FIXTURE, output, jev_transport=late_success, event_sink=events.append
    )
    second = replay_fixture(FIXTURE, output, jev_transport=late_success)
    replay = load_observation(output, first["observation_id"])

    assert first == second
    assert accesses == []
    assert events == ["decision_frozen"]
    assert list(replay.artifacts)[-2:] == ["decision_frozen", "book_unavailable"]
    assert replay.result.ai.disposition == "BOOK_FAILURE"
    assert (
        replay.result.ai.signal_probability_micros
        == raw["jev_decision"]["p_yes_micros"]
    )
    assert (
        replay.result.ai.forecast_probability_micros
        == raw["jev_decision"]["p_yes_micros"]
    )
    assert replay.result.ai.quantity == 0
    assert replay.result.deterministic.quantity == 0
    assert len((output / "results.jsonl").read_text().splitlines()) == 1


def test_late_path_crash_after_unavailable_artifact_recovers_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jev_trading_lab.polymarket import pipeline
    from jev_trading_lab.polymarket.ai import TransportResponse

    raw = _fixture()
    output = tmp_path / "late-crash"
    original_record = pipeline.record_observation
    crashed = False

    def crash_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("crash after book unavailable")
        return original_record(*args, **kwargs)

    def late_success(_request: bytes) -> TransportResponse:
        return TransportResponse(
            status_code=200,
            headers={},
            raw_bytes=raw["model_artifacts"]["jev_response_bytes"].encode(),
            retrieved_at=datetime(2026, 10, 3, 12, 2, tzinfo=UTC),
            billed_cost_microdollars=raw["model_costs_microdollars"]["jev"],
        )

    monkeypatch.setattr(pipeline, "record_observation", crash_once)
    with pytest.raises(RuntimeError, match="crash after book unavailable"):
        replay_fixture(FIXTURE, output, jev_transport=late_success)

    assert not (output / "results.jsonl").read_text()
    kinds = [
        json.loads(line)["kind"]
        for line in (output / "artifacts.jsonl").read_text().splitlines()
    ]
    assert kinds[-2:] == ["decision_frozen", "book_unavailable"]

    summary = replay_fixture(FIXTURE, output, jev_transport=late_success)
    assert summary["ai"]["disposition"] == "BOOK_FAILURE"
    assert len((output / "results.jsonl").read_text().splitlines()) == 1


def test_openai_failure_crash_after_barrier_recovers_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jev_trading_lab.polymarket import pipeline
    from jev_trading_lab.polymarket.ai import TransportResponse

    output = tmp_path / "failure-crash"
    original_get = pipeline._ExecutionBookGate.get
    crashed = False

    def crash_once(gate: Any) -> Any:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("crash after failure barrier")
        return original_get(gate)

    def failed(_request: bytes) -> TransportResponse:
        return TransportResponse(
            status_code=500,
            headers={},
            raw_bytes=b"temporary failure",
            retrieved_at=datetime(2026, 10, 3, 12, 0, 30, tzinfo=UTC),
            billed_cost_microdollars=29,
        )

    monkeypatch.setattr(pipeline._ExecutionBookGate, "get", crash_once)
    with pytest.raises(RuntimeError, match="crash after failure barrier"):
        replay_fixture(FIXTURE, output, openai_transport=failed)

    kinds = [
        json.loads(line)["kind"]
        for line in (output / "artifacts.jsonl").read_text().splitlines()
    ]
    assert kinds[-2:] == ["openai_failure", "decision_frozen"]
    assert not (output / "results.jsonl").read_text()

    summary = replay_fixture(FIXTURE, output, openai_transport=failed)
    assert summary["artifact_count"] == 5
    assert len((output / "results.jsonl").read_text().splitlines()) == 1


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

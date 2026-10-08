from __future__ import annotations

import errno
import hashlib
import json
import multiprocessing
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.persistence.blobs import BlobStore
from jev_trading_lab.polymarket.ai import (
    OPENAI_MODEL_ID,
    AdapterFailure,
    build_openai_request,
)
from jev_trading_lab.polymarket.models import (
    EvidencePacket,
    JevDecision,
    JevResponse,
    ObservationResult,
    OpenAIResponse,
    ResearchBrief,
    load_strict_json,
)
from jev_trading_lab.polymarket.store import (
    ArtifactEnvelope,
    ArtifactToFreeze,
    FailureArtifact,
    StoreConflictError,
    StoreCorruptError,
    StoreSecurityError,
    failure_artifact_from_adapter,
    load_observation,
    record_observation,
)

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures/polymarket/feasibility_cycle.json"


@pytest.fixture(autouse=True)
def _local_test_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    from jev_trading_lab.polymarket import store

    defaults = dict(store.BlobStore.__init__.__kwdefaults__ or {})
    defaults["filesystem_resolver"] = lambda _path: "ext4"
    monkeypatch.setattr(store.BlobStore.__init__, "__kwdefaults__", defaults)


@pytest.fixture
def cycle() -> dict[str, Any]:
    value = load_strict_json(FIXTURE_PATH.read_bytes())
    assert isinstance(value, dict)
    return value


def _result(cycle: dict[str, Any], suffix: str = "") -> ObservationResult:
    value = deepcopy(cycle["expected"]["observation_result"])
    if suffix:
        value["policy_version"] += f"-{suffix}"
        value["opportunity_id"] = hashlib.sha256(
            canonical_json_bytes(
                {
                    "policy_version": value["policy_version"],
                    "market_id": value["market_id"],
                    "cutoff_at": value["cutoff_at"],
                }
            )
        ).hexdigest()
        value["observation_id"] = hashlib.sha256(
            canonical_json_bytes(
                {
                    "opportunity_id": value["opportunity_id"],
                    "evidence_sha256": value["evidence_sha256"],
                    "execution_book_sha256": value["execution_book_sha256"],
                    "policy_version": value["policy_version"],
                }
            )
        ).hexdigest()
    return ObservationResult.model_validate_json(json.dumps(value))


def _stage_hash(
    result: ObservationResult, artifact: ArtifactToFreeze, predecessor: str | None
) -> str:
    payload_hash = hashlib.sha256(artifact.payload).hexdigest()
    envelope = {
        "byte_size": len(artifact.payload),
        "kind": artifact.kind,
        "observation_id": result.observation_id,
        "opportunity_id": result.opportunity_id,
        "predecessor_stage_sha256": predecessor,
        "retrieved_at": artifact.retrieved_at.isoformat().replace("+00:00", "Z"),
        "sha256": payload_hash,
        "source_uri_label": artifact.source_uri_label,
        "storage_relpath": f"{payload_hash[:2]}/{payload_hash[2:4]}/{payload_hash}",
    }
    return hashlib.sha256(canonical_json_bytes(envelope)).hexdigest()


def _artifacts(
    cycle: dict[str, Any], result: ObservationResult | None = None
) -> list[ArtifactToFreeze]:
    result = result or _result(cycle)
    evidence = EvidencePacket.model_validate_json(
        canonical_json_bytes(cycle["sanitized_evidence"])
    )
    brief = ResearchBrief.model_validate_json(
        canonical_json_bytes(cycle["research_brief"])
    )
    decision = JevDecision.model_validate_json(
        canonical_json_bytes(cycle["jev_decision"])
    )
    values = (
        (
            "raw_gamma",
            cycle["raw"]["gamma_bytes"].encode(),
            "gamma_public",
            evidence.retrieved_at,
        ),
        (
            "sanitized_evidence",
            canonical_json_bytes(evidence.model_dump(mode="json")),
            "gamma_sanitized",
            evidence.retrieved_at,
        ),
        (
            "openai_request",
            cycle["model_artifacts"]["openai_request_bytes"].encode(),
            "openai_request",
            datetime.fromisoformat(
                cycle["chronology"]["openai_request_at"].replace("Z", "+00:00")
            ),
        ),
        (
            "openai_response_success",
            cycle["model_artifacts"]["openai_response_bytes"].encode(),
            "openai_response",
            datetime.fromisoformat(
                cycle["chronology"]["openai_response_at"].replace("Z", "+00:00")
            ),
        ),
        (
            "openai_success_metadata",
            _success_metadata(
                provider="openai",
                cost=cycle["model_costs_microdollars"]["openai"],
                request_bytes=cycle["model_artifacts"]["openai_request_bytes"].encode(),
                response_bytes=cycle["model_artifacts"][
                    "openai_response_bytes"
                ].encode(),
                retrieved_at=datetime.fromisoformat(
                    cycle["chronology"]["openai_response_at"].replace("Z", "+00:00")
                ),
            ),
            "openai_success_metadata",
            datetime.fromisoformat(
                cycle["chronology"]["openai_response_at"].replace("Z", "+00:00")
            ),
        ),
        (
            "research_brief",
            canonical_json_bytes(brief.model_dump(mode="json")),
            "openai_validated",
            datetime.fromisoformat(
                cycle["chronology"]["openai_response_at"].replace("Z", "+00:00")
            ),
        ),
        (
            "jev_request",
            cycle["model_artifacts"]["jev_request_bytes"].encode(),
            "jev_request",
            datetime.fromisoformat(
                cycle["chronology"]["jev_request_at"].replace("Z", "+00:00")
            ),
        ),
        (
            "jev_response_success",
            cycle["model_artifacts"]["jev_response_bytes"].encode(),
            "jev_response",
            datetime.fromisoformat(
                cycle["chronology"]["jev_response_at"].replace("Z", "+00:00")
            ),
        ),
        (
            "jev_success_metadata",
            _success_metadata(
                provider="jev",
                cost=cycle["model_costs_microdollars"]["jev"],
                request_bytes=cycle["model_artifacts"]["jev_request_bytes"].encode(),
                response_bytes=cycle["model_artifacts"]["jev_response_bytes"].encode(),
                retrieved_at=datetime.fromisoformat(
                    cycle["chronology"]["jev_response_at"].replace("Z", "+00:00")
                ),
            ),
            "jev_success_metadata",
            datetime.fromisoformat(
                cycle["chronology"]["jev_response_at"].replace("Z", "+00:00")
            ),
        ),
        (
            "jev_decision",
            canonical_json_bytes(decision.model_dump(mode="json")),
            "jev_validated",
            datetime.fromisoformat(
                cycle["chronology"]["jev_response_at"].replace("Z", "+00:00")
            ),
        ),
        (
            "decision_frozen",
            canonical_json_bytes(
                {
                    "jev_decision_sha256": result.jev_decision_sha256,
                    "frozen_at": cycle["chronology"]["decision_frozen_at"],
                }
            ),
            "decision_frozen",
            datetime.fromisoformat(
                cycle["chronology"]["decision_frozen_at"].replace("Z", "+00:00")
            ),
        ),
    )
    artifacts: list[ArtifactToFreeze] = []
    predecessor: str | None = None
    for kind, payload, source, retrieved_at in values:
        artifact = ArtifactToFreeze(
            kind=kind,
            payload=payload,
            source_uri_label=source,
            retrieved_at=retrieved_at,
            predecessor_stage_sha256=predecessor,
        )
        artifacts.append(artifact)
        predecessor = _stage_hash(result, artifact, predecessor)
    assert result.research_brief_sha256 == hashlib.sha256(values[5][1]).hexdigest()
    assert result.jev_decision_sha256 == hashlib.sha256(values[9][1]).hexdigest()
    return artifacts


def _success_metadata(
    *,
    provider: str,
    cost: int,
    request_bytes: bytes,
    response_bytes: bytes,
    retrieved_at: datetime,
) -> bytes:
    return canonical_json_bytes(
        {
            "provider": provider,
            "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
            "response_sha256": hashlib.sha256(response_bytes).hexdigest(),
            "retrieved_at": retrieved_at.isoformat().replace("+00:00", "Z"),
            "billed_cost_microdollars": cost,
        }
    )


def _openai_success_cost(cycle: dict[str, Any]) -> int:
    response = json.loads(cycle["model_artifacts"]["openai_response_bytes"])
    usage = response["usage"]
    cached = usage["input_tokens_details"]["cached_tokens"]
    numerator = (
        (usage["input_tokens"] - cached) * 10 + cached + usage["output_tokens"] * 80
    )
    assert numerator % 8 == 0
    return numerator // 8


def _with_ai_model_cost(
    result: ObservationResult, model_cost_microdollars: int
) -> ObservationResult:
    value = result.model_dump(mode="json")
    trading = value["ai"]["trading_pnl_microdollars"]
    deterministic_economic = value["deterministic"]["economic_pnl_microdollars"]
    value["ai"]["model_cost_microdollars"] = model_cost_microdollars
    value["ai"]["economic_pnl_microdollars"] = trading - model_cost_microdollars
    value["paired_economic_pnl_delta_microdollars"] = (
        trading - model_cost_microdollars - deterministic_economic
    )
    return ObservationResult.model_validate_json(canonical_json_bytes(value))


def _replace_success_cost(
    artifacts: list[ArtifactToFreeze], provider: str, cost: int
) -> list[ArtifactToFreeze]:
    kind = f"{provider}_success_metadata"
    index = next(i for i, item in enumerate(artifacts) if item.kind == kind)
    value = json.loads(artifacts[index].payload)
    value["billed_cost_microdollars"] = cost
    item = artifacts[index]
    artifacts[index] = ArtifactToFreeze(
        kind=item.kind,
        payload=canonical_json_bytes(value),
        source_uri_label=item.source_uri_label,
        retrieved_at=item.retrieved_at,
        predecessor_stage_sha256=item.predecessor_stage_sha256,
    )
    return artifacts


def _failure_result(
    cycle: dict[str, Any], *, openai_failed: bool, model_cost: int
) -> ObservationResult:
    value = deepcopy(cycle["expected"]["observation_result"])
    value["research_brief_sha256"] = (
        "0" * 64 if openai_failed else value["research_brief_sha256"]
    )
    value["jev_decision_sha256"] = "0" * 64
    value["ai"] = {
        "arm": "AI",
        "signal_probability_micros": None,
        "signal_action": None,
        "disposition": "MODEL_FAILURE",
        "entry_side": None,
        "entry_vwap_micros": None,
        "entry_notional_microdollars": None,
        "quantity": 0,
        "fees_microdollars": 0,
        "initial_cash_microdollars": value["ai"]["initial_cash_microdollars"],
        "final_cash_microdollars": value["ai"]["initial_cash_microdollars"],
        "mark_micros": None,
        "payout_micros": None,
        "valuation_notional_microdollars": None,
        "trading_pnl_microdollars": 0,
        "economic_pnl_microdollars": -model_cost,
        "model_cost_microdollars": model_cost,
        "forecast_probability_micros": None,
        "forecast_status": "UNAVAILABLE",
        "brier_micros": None,
    }
    value["paired_trading_pnl_delta_microdollars"] = 0
    value["paired_economic_pnl_delta_microdollars"] = -model_cost
    value["paired_brier_delta_micros"] = None
    return ObservationResult.model_validate_json(canonical_json_bytes(value))


def _failure_metadata(
    *, provider: str, cost: int, request_bytes: bytes, response_body: bytes
) -> bytes:
    failure = AdapterFailure(
        provider=provider,  # type: ignore[arg-type]
        code="HTTP_ERROR",
        message=f"{provider} returned HTTP status 503",
        request_bytes=request_bytes,
        response_sha256=hashlib.sha256(response_body).hexdigest(),
        response_byte_length=len(response_body),
        retrieved_at=datetime(2026, 10, 3, 12, 0, 30, tzinfo=UTC),
        billed_cost_microdollars=cost,
        status_code=503,
    )
    artifact = failure_artifact_from_adapter(failure)
    return canonical_json_bytes(artifact.model_dump(mode="json"))


def _failure_artifacts(
    cycle: dict[str, Any], *, openai_failed: bool
) -> tuple[ObservationResult, list[ArtifactToFreeze], bytes]:
    success = _artifacts(cycle)
    openai_cost = _openai_success_cost(cycle)
    success = _replace_success_cost(success, "openai", openai_cost)
    by_kind = {item.kind: item for item in success}
    jev_cost = cycle["model_costs_microdollars"]["jev"]
    response_body = b"Authorization: Basic top-secret"
    if openai_failed:
        result = _failure_result(cycle, openai_failed=True, model_cost=openai_cost)
        path = ["raw_gamma", "sanitized_evidence", "openai_request"]
        failure_kind = "openai_failure"
        failure_cost = openai_cost
        request = by_kind["openai_request"].payload
        provider = "openai"
    else:
        result = _failure_result(
            cycle, openai_failed=False, model_cost=openai_cost + jev_cost
        )
        path = [
            "raw_gamma",
            "sanitized_evidence",
            "openai_request",
            "openai_response_success",
            "openai_success_metadata",
            "research_brief",
            "jev_request",
        ]
        failure_kind = "jev_failure"
        failure_cost = jev_cost
        request = by_kind["jev_request"].payload
        provider = "jev"
    values = [
        (
            kind,
            by_kind[kind].payload,
            by_kind[kind].source_uri_label,
            by_kind[kind].retrieved_at,
        )
        for kind in path
    ]
    values.append(
        (
            failure_kind,
            _failure_metadata(
                provider=provider,
                cost=failure_cost,
                request_bytes=request,
                response_body=response_body,
            ),
            failure_kind,
            datetime(2026, 10, 3, 12, 0, 30, tzinfo=UTC),
        )
    )
    artifacts: list[ArtifactToFreeze] = []
    predecessor: str | None = None
    for kind, payload, source, retrieved_at in values:
        artifact = ArtifactToFreeze(
            kind=kind,
            payload=payload,
            source_uri_label=source,
            retrieved_at=retrieved_at,
            predecessor_stage_sha256=predecessor,
        )
        artifacts.append(artifact)
        predecessor = _stage_hash(result, artifact, predecessor)
    return result, artifacts, response_body


def _retarget(
    artifacts: list[ArtifactToFreeze], result: ObservationResult
) -> list[ArtifactToFreeze]:
    retargeted: list[ArtifactToFreeze] = []
    predecessor: str | None = None
    for item in artifacts:
        payload = (
            canonical_json_bytes(result.model_dump(mode="json"))
            if item.kind == "observation_result"
            else item.payload
        )
        artifact = ArtifactToFreeze(
            kind=item.kind,
            payload=payload,
            source_uri_label=item.source_uri_label,
            retrieved_at=result.observed_at
            if item.kind == "observation_result"
            else item.retrieved_at,
            predecessor_stage_sha256=predecessor,
        )
        retargeted.append(artifact)
        predecessor = _stage_hash(result, artifact, predecessor)
    return retargeted


def _process_record(
    output: str, fixture: dict[str, Any], suffix: str, queue: multiprocessing.Queue[str]
) -> None:
    try:
        result = _result(fixture, suffix)
        record_observation(Path(output), result, _retarget(_artifacts(fixture), result))
    except BaseException as error:  # pragma: no cover - returned to parent
        queue.put(f"{type(error).__name__}: {error}")
    else:
        queue.put("ok")


def _journal_lines(path: Path) -> list[bytes]:
    return path.read_bytes().splitlines()


def test_publishes_exact_fixture_bytes_and_replays_verified_artifacts(
    tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)

    stored = record_observation(output, result, _artifacts(cycle))
    replay = load_observation(output, result.observation_id)

    assert stored == result
    assert replay.result == result
    assert replay.artifacts["raw_gamma"] == cycle["raw"]["gamma_bytes"].encode()
    assert (
        hashlib.sha256(replay.artifacts["raw_gamma"]).hexdigest()
        == (cycle["expected"]["artifact_sha256"]["gamma_bytes"])
    )
    assert set(replay.artifacts) == {
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
    }
    assert isinstance(replay.typed_artifacts["sanitized_evidence"], EvidencePacket)
    assert isinstance(replay.typed_artifacts["openai_response_success"], OpenAIResponse)
    assert isinstance(replay.typed_artifacts["research_brief"], ResearchBrief)
    assert isinstance(replay.typed_artifacts["jev_response_success"], JevResponse)
    assert isinstance(replay.typed_artifacts["jev_decision"], JevDecision)


@pytest.mark.parametrize("openai_failed", (True, False))
@pytest.mark.parametrize(
    "failure_code", ("HTTP_ERROR", "MODEL_MISMATCH", "PARSE_ERROR")
)
def test_failure_paths_record_and_replay_sanitized_metadata(
    tmp_path: Path,
    cycle: dict[str, Any],
    openai_failed: bool,
    failure_code: str,
) -> None:
    output = tmp_path / "output"
    result, artifacts, raw_failure = _failure_artifacts(
        cycle, openai_failed=openai_failed
    )
    failure = artifacts[-1]
    failure_value = json.loads(failure.payload)
    failure_value["code"] = failure_code
    provider = failure_value["provider"]
    if failure_code == "MODEL_MISMATCH":
        failure_value["message"] = (
            f"{provider} response model does not match the approved snapshot"
        )
        failure_value["status_code"] = 200
    elif failure_code == "PARSE_ERROR":
        failure_value["message"] = f"{provider} response is not strict JSON"
        failure_value["status_code"] = 200
    artifacts[-1] = ArtifactToFreeze(
        kind=failure.kind,
        payload=canonical_json_bytes(failure_value),
        source_uri_label=failure.source_uri_label,
        retrieved_at=failure.retrieved_at,
        predecessor_stage_sha256=failure.predecessor_stage_sha256,
    )

    assert record_observation(output, result, artifacts) == result
    replay = load_observation(output, result.observation_id)

    expected_kind = "openai_failure" if openai_failed else "jev_failure"
    assert tuple(replay.artifacts) == tuple(item.kind for item in artifacts)
    assert expected_kind in replay.typed_artifacts
    assert isinstance(replay.typed_artifacts[expected_kind], FailureArtifact)
    assert raw_failure not in b"".join(replay.artifacts.values())
    assert replay.result.ai.disposition == "MODEL_FAILURE"
    assert replay.result.ai.signal_probability_micros is None
    assert replay.result.ai.signal_action is None
    assert replay.result.ai.forecast_probability_micros is None
    assert replay.result.ai.trading_pnl_microdollars == 0
    assert (
        replay.result.ai.economic_pnl_microdollars
        == -replay.result.ai.model_cost_microdollars
    )


@pytest.mark.parametrize("openai_failed", (True, False))
@pytest.mark.parametrize("failure_code", ("HTTP_ERROR", "PARSE_ERROR"))
def test_empty_failure_response_records_and_replays_canonical_hash(
    tmp_path: Path,
    cycle: dict[str, Any],
    openai_failed: bool,
    failure_code: str,
) -> None:
    result, artifacts, _raw_failure = _failure_artifacts(
        cycle, openai_failed=openai_failed
    )
    item = artifacts[-1]
    value = json.loads(item.payload)
    value.update(
        code=failure_code,
        message=(
            f"{value['provider']} returned HTTP status 500"
            if failure_code == "HTTP_ERROR"
            else f"{value['provider']} response is not strict JSON"
        ),
        response_sha256=hashlib.sha256(b"").hexdigest(),
        response_byte_length=0,
        status_code=500 if failure_code == "HTTP_ERROR" else 200,
    )
    artifacts[-1] = ArtifactToFreeze(
        kind=item.kind,
        payload=canonical_json_bytes(value),
        source_uri_label=item.source_uri_label,
        retrieved_at=item.retrieved_at,
        predecessor_stage_sha256=item.predecessor_stage_sha256,
    )
    artifacts = _retarget(artifacts, result)

    assert record_observation(tmp_path / "output", result, artifacts) == result
    replay = load_observation(tmp_path / "output", result.observation_id)
    failure = replay.typed_artifacts[item.kind]
    assert isinstance(failure, FailureArtifact)
    assert failure.response_sha256 == hashlib.sha256(b"").hexdigest()
    assert failure.response_byte_length == 0


@pytest.mark.parametrize(
    ("response_sha256", "response_byte_length"),
    (
        ("0" * 64, 0),
        (hashlib.sha256(b"").hexdigest(), 1),
        (None, 1),
    ),
)
def test_failure_artifact_rejects_noncanonical_response_hash_length_pairs(
    response_sha256: str | None, response_byte_length: int
) -> None:
    with pytest.raises(ValidationError, match="response hash and length"):
        FailureArtifact(
            provider="openai",
            code="HTTP_ERROR",
            message="openai returned HTTP status 500",
            request_sha256=hashlib.sha256(b"request").hexdigest(),
            response_sha256=response_sha256,
            response_byte_length=response_byte_length,
            retrieved_at=datetime(2026, 10, 3, 12, 0, 30, tzinfo=UTC),
            billed_cost_microdollars=0,
            status_code=500,
        )


_FAILURE_VARIANTS = (
    "INPUT_ERROR",
    "PROVIDER_ERROR",
    "TIMEOUT",
    "HTTP_ERROR",
    "PARSE_ERROR",
    "MODEL_MISMATCH",
    "CREDENTIAL_REFLECTION",
)


@pytest.mark.parametrize("openai_failed", (True, False))
@pytest.mark.parametrize("failure_code", _FAILURE_VARIANTS)
def test_each_adapter_failure_variant_roundtrips_canonical_metadata(
    tmp_path: Path,
    cycle: dict[str, Any],
    openai_failed: bool,
    failure_code: str,
) -> None:
    result, artifacts, _raw_failure = _failure_artifacts(
        cycle, openai_failed=openai_failed
    )
    value = json.loads(artifacts[-1].payload)
    provider = value["provider"]
    value["code"] = failure_code
    if failure_code in {"INPUT_ERROR", "PROVIDER_ERROR", "TIMEOUT"}:
        messages = {
            "INPUT_ERROR": f"{provider} request input is invalid",
            "PROVIDER_ERROR": f"{provider} transport returned invalid data",
            "TIMEOUT": f"{provider} transport timed out",
        }
        value.update(
            message=messages[failure_code],
            response_sha256=None,
            response_byte_length=0,
            retrieved_at=None,
            billed_cost_microdollars=0,
            status_code=None,
        )
        prior_cost = 0 if openai_failed else _openai_success_cost(cycle)
        result = _failure_result(
            cycle, openai_failed=openai_failed, model_cost=prior_cost
        )
    elif failure_code == "HTTP_ERROR":
        value["message"] = f"{provider} returned HTTP status 503"
    elif failure_code == "PARSE_ERROR":
        value.update(message=f"{provider} response is not strict JSON", status_code=200)
    elif failure_code == "MODEL_MISMATCH":
        value.update(
            message=f"{provider} response model does not match the approved snapshot",
            status_code=200,
        )
    else:
        value["message"] = f"{provider} response contained credential-like data"
    item = artifacts[-1]
    artifacts[-1] = ArtifactToFreeze(
        kind=item.kind,
        payload=canonical_json_bytes(value),
        source_uri_label=item.source_uri_label,
        retrieved_at=item.retrieved_at,
        predecessor_stage_sha256=item.predecessor_stage_sha256,
    )
    artifacts = _retarget(artifacts, result)
    output = tmp_path / f"{provider}-{failure_code.lower()}"

    assert record_observation(output, result, artifacts) == result
    replay = load_observation(output, result.observation_id)

    failure = replay.typed_artifacts[artifacts[-1].kind]
    assert isinstance(failure, FailureArtifact)
    assert failure.code == failure_code
    assert failure.message == value["message"]


@pytest.mark.parametrize(
    "mutation", ("request_hash", "message", "response_consistency", "http_status")
)
def test_forged_failure_provenance_rejects_before_journal_write(
    tmp_path: Path, cycle: dict[str, Any], mutation: str
) -> None:
    output = tmp_path / "output"
    result, artifacts, _raw_failure = _failure_artifacts(cycle, openai_failed=True)
    value = json.loads(artifacts[-1].payload)
    if mutation == "request_hash":
        value["request_sha256"] = "0" * 64
    elif mutation == "message":
        value["message"] = "openai attacker supplied diagnostic"
    elif mutation == "response_consistency":
        value["response_sha256"] = None
    else:
        value["status_code"] = 200
    item = artifacts[-1]
    artifacts[-1] = ArtifactToFreeze(
        kind=item.kind,
        payload=canonical_json_bytes(value),
        source_uri_label=item.source_uri_label,
        retrieved_at=item.retrieved_at,
        predecessor_stage_sha256=item.predecessor_stage_sha256,
    )

    with pytest.raises(ValueError, match="failure|request|response|status"):
        record_observation(output, result, artifacts)

    assert not (output / "artifacts.jsonl").exists()
    assert not (output / "results.jsonl").exists()


@pytest.mark.parametrize("delta", (-1, 1))
def test_success_path_requires_exact_cumulative_model_cost(
    tmp_path: Path, cycle: dict[str, Any], delta: int
) -> None:
    result = _result(cycle)
    mismatched = _with_ai_model_cost(result, result.ai.model_cost_microdollars + delta)

    with pytest.raises(ValueError, match="model cost|provider success"):
        record_observation(
            tmp_path / "output", mismatched, _retarget(_artifacts(cycle), mismatched)
        )

    assert mismatched.ai.trading_pnl_microdollars == result.ai.trading_pnl_microdollars
    assert not (tmp_path / "output" / "artifacts.jsonl").exists()
    assert not (tmp_path / "output" / "results.jsonl").exists()


@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_success_path_cost_follows_frozen_provider_metadata(
    tmp_path: Path, cycle: dict[str, Any], provider: str
) -> None:
    result = _result(cycle)
    changed_cost = cycle["model_costs_microdollars"][provider] + 7
    artifacts = _replace_success_cost(_artifacts(cycle), provider, changed_cost)
    expected_cost = result.ai.model_cost_microdollars + 7
    changed = _with_ai_model_cost(result, expected_cost)
    artifacts = _retarget(artifacts, changed)

    stored = record_observation(tmp_path / "output", changed, artifacts)

    assert stored.ai.model_cost_microdollars == expected_cost
    assert stored.ai.trading_pnl_microdollars == result.ai.trading_pnl_microdollars
    assert (
        stored.ai.economic_pnl_microdollars
        == stored.ai.trading_pnl_microdollars - expected_cost
    )


def test_success_path_rejects_int64_cost_sum_overflow_without_persist(
    tmp_path: Path, cycle: dict[str, Any]
) -> None:
    result = _result(cycle)
    artifacts = _replace_success_cost(_artifacts(cycle), "openai", 2**63 - 1)
    artifacts = _replace_success_cost(artifacts, "jev", 1)

    with pytest.raises(ValueError, match="model cost|64-bit|overflow"):
        record_observation(tmp_path / "output", result, _retarget(artifacts, result))

    assert not (tmp_path / "output" / "artifacts.jsonl").exists()
    assert not (tmp_path / "output" / "results.jsonl").exists()


def test_success_path_rejects_nonzero_deterministic_model_cost(
    tmp_path: Path, cycle: dict[str, Any]
) -> None:
    value = _result(cycle).model_dump(mode="json")
    value["deterministic"]["model_cost_microdollars"] = 1
    value["deterministic"]["economic_pnl_microdollars"] -= 1
    value["paired_economic_pnl_delta_microdollars"] += 1
    result = ObservationResult.model_validate_json(canonical_json_bytes(value))

    with pytest.raises(ValueError, match="deterministic.*model cost"):
        record_observation(
            tmp_path / "output", result, _retarget(_artifacts(cycle), result)
        )


def test_replay_rejects_success_result_with_adjusted_model_cost(
    tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    record_observation(output, result, _artifacts(cycle))
    forged = _with_ai_model_cost(result, result.ai.model_cost_microdollars + 1)
    results = output / "results.jsonl"
    results.write_bytes(canonical_json_bytes(forged.model_dump(mode="json")) + b"\n")
    results.chmod(0o600)

    with pytest.raises(StoreCorruptError, match="model cost|provider success"):
        load_observation(output, result.observation_id)


@pytest.mark.parametrize("openai_failed", (True, False))
@pytest.mark.parametrize("delta", (-1, 1))
def test_failure_path_requires_exact_cumulative_model_cost(
    tmp_path: Path,
    cycle: dict[str, Any],
    openai_failed: bool,
    delta: int,
) -> None:
    result, artifacts, _raw_failure = _failure_artifacts(
        cycle, openai_failed=openai_failed
    )
    value = result.model_dump(mode="json")
    value["ai"]["model_cost_microdollars"] += delta
    value["ai"]["economic_pnl_microdollars"] -= delta
    value["paired_economic_pnl_delta_microdollars"] -= delta
    mismatched = ObservationResult.model_validate_json(canonical_json_bytes(value))

    with pytest.raises(ValueError, match="model cost|failure outcome"):
        record_observation(
            tmp_path / "output", mismatched, _retarget(artifacts, mismatched)
        )


@pytest.mark.parametrize("openai_failed", (True, False))
def test_failure_path_exact_cost_and_economics_pass(
    tmp_path: Path, cycle: dict[str, Any], openai_failed: bool
) -> None:
    result, artifacts, _raw_failure = _failure_artifacts(
        cycle, openai_failed=openai_failed
    )

    assert result.ai.model_cost_microdollars == (
        _openai_success_cost(cycle)
        + (0 if openai_failed else cycle["model_costs_microdollars"]["jev"])
    )
    assert result.ai.economic_pnl_microdollars == (
        result.ai.trading_pnl_microdollars - result.ai.model_cost_microdollars
    )
    assert result.deterministic.model_cost_microdollars == 0
    assert record_observation(tmp_path / "output", result, artifacts) == result
    assert load_observation(tmp_path / "output", result.observation_id).result == result


@pytest.mark.parametrize("openai_failed", (True, False))
@pytest.mark.parametrize("mutation", ("missing", "extra", "cost"))
def test_failure_path_rejects_stage_and_cost_mismatch(
    tmp_path: Path,
    cycle: dict[str, Any],
    openai_failed: bool,
    mutation: str,
) -> None:
    result, artifacts, _raw_failure = _failure_artifacts(
        cycle, openai_failed=openai_failed
    )
    if mutation == "missing":
        artifacts.pop(-2)
        artifacts = _retarget(artifacts, result)
    elif mutation == "extra":
        artifacts.append(_artifacts(cycle)[-1])
        artifacts = _retarget(artifacts, result)
    else:
        value = result.model_dump(mode="json")
        delta = 1 if openai_failed else -(value["ai"]["model_cost_microdollars"])
        value["ai"]["model_cost_microdollars"] += delta
        value["ai"]["economic_pnl_microdollars"] -= delta
        value["paired_economic_pnl_delta_microdollars"] -= delta
        result = ObservationResult.model_validate_json(canonical_json_bytes(value))
        artifacts = _retarget(artifacts, result)

    with pytest.raises(ValueError, match="stage order|model cost|failure outcome"):
        record_observation(tmp_path / "output", result, artifacts)


@pytest.mark.parametrize("field", ("signal_probability_micros", "signal_action"))
def test_success_result_must_match_frozen_jev_decision(
    tmp_path: Path, cycle: dict[str, Any], field: str
) -> None:
    result = _result(cycle)
    value = result.model_dump(mode="json")
    value["ai"][field] = 600_000 if field == "signal_probability_micros" else "BUY_NO"
    if field == "signal_probability_micros":
        value["ai"]["forecast_probability_micros"] = 600_000
        value["ai"]["brier_micros"] = 160_000
        value["paired_brier_delta_micros"] = -42_500
    else:
        value["ai"].update(
            disposition="NO_EDGE",
            entry_side=None,
            entry_vwap_micros=None,
            entry_notional_microdollars=None,
            quantity=0,
            fees_microdollars=0,
            final_cash_microdollars=value["ai"]["initial_cash_microdollars"],
            valuation_notional_microdollars=None,
            trading_pnl_microdollars=0,
            economic_pnl_microdollars=-value["ai"]["model_cost_microdollars"],
        )
        value["paired_trading_pnl_delta_microdollars"] = 0
        value["paired_economic_pnl_delta_microdollars"] = -value["ai"][
            "model_cost_microdollars"
        ]
    mutated = ObservationResult.model_validate_json(canonical_json_bytes(value))

    with pytest.raises(ValueError, match="Jev decision|AI signal"):
        record_observation(
            tmp_path / "output", mutated, _retarget(_artifacts(cycle), mutated)
        )


def test_journals_are_canonical_strict_and_private(
    tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    record_observation(output, result, _artifacts(cycle))

    result_line = _journal_lines(output / "results.jsonl")[0]
    assert result_line == canonical_json_bytes(result.model_dump(mode="json"))
    assert ObservationResult.model_validate_json(result_line) == result
    for line in _journal_lines(output / "artifacts.jsonl"):
        envelope = ArtifactEnvelope.model_validate_json(line)
        assert line == canonical_json_bytes(envelope.model_dump(mode="json"))
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE((output / "results.jsonl").stat().st_mode) == 0o600
    assert stat.S_IMODE((output / "artifacts.jsonl").stat().st_mode) == 0o600


def test_same_observation_is_idempotent_without_duplicate_lines(
    tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    artifacts = _artifacts(cycle)

    assert record_observation(output, result, artifacts) == result
    before = (
        (output / "artifacts.jsonl").read_bytes(),
        (output / "results.jsonl").read_bytes(),
    )
    assert record_observation(output, result, artifacts) == result

    assert (output / "artifacts.jsonl").read_bytes() == before[0]
    assert (output / "results.jsonl").read_bytes() == before[1]


def test_same_identity_with_different_canonical_result_conflicts(
    tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    record_observation(output, result, _artifacts(cycle))
    changed = result.model_copy(
        update={"observed_at": datetime(2026, 11, 1, 20, 0, 1, tzinfo=UTC)}
    )

    with pytest.raises(StoreConflictError, match="observation identity"):
        record_observation(output, changed, _artifacts(cycle, changed))

    assert len(_journal_lines(output / "results.jsonl")) == 1


def test_unknown_artifact_kind_rejects_without_journal_mutation(
    tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    artifacts = _artifacts(cycle)
    artifacts[-1] = ArtifactToFreeze(
        kind="provider_output",
        payload=artifacts[-1].payload,
        source_uri_label=artifacts[-1].source_uri_label,
        retrieved_at=artifacts[-1].retrieved_at,
        predecessor_stage_sha256=artifacts[-1].predecessor_stage_sha256,
    )

    with pytest.raises(ValueError, match="artifact kind|stage order"):
        record_observation(output, result, artifacts)

    assert not (output / "artifacts.jsonl").exists()
    assert not (output / "results.jsonl").exists()


@pytest.mark.parametrize("mutation", ("missing", "reordered", "broken"))
def test_invalid_complete_chain_rejects_before_journal_append(
    tmp_path: Path, cycle: dict[str, Any], mutation: str
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    artifacts = _artifacts(cycle)
    if mutation == "missing":
        artifacts.pop(3)
    elif mutation == "reordered":
        artifacts[3], artifacts[4] = artifacts[4], artifacts[3]
    else:
        item = artifacts[4]
        artifacts[4] = ArtifactToFreeze(
            kind=item.kind,
            payload=item.payload,
            source_uri_label=item.source_uri_label,
            retrieved_at=item.retrieved_at,
            predecessor_stage_sha256="0" * 64,
        )

    with pytest.raises(ValueError, match="stage order|predecessor"):
        record_observation(output, result, artifacts)

    assert not (output / "artifacts.jsonl").exists()
    assert not (output / "results.jsonl").exists()


def test_invalid_structured_artifact_rejects_before_journal_append(
    tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    artifacts = _artifacts(cycle)
    index = next(i for i, item in enumerate(artifacts) if item.kind == "research_brief")
    invalid = ArtifactToFreeze(
        kind="research_brief",
        payload=b"not a ResearchBrief",
        source_uri_label=artifacts[index].source_uri_label,
        retrieved_at=artifacts[index].retrieved_at,
        predecessor_stage_sha256=artifacts[index].predecessor_stage_sha256,
    )
    artifacts[index] = invalid

    with pytest.raises(ValueError, match="research_brief"):
        record_observation(output, result, artifacts)

    assert not (output / "artifacts.jsonl").exists()
    assert not (output / "results.jsonl").exists()


def test_incomplete_openai_response_rejects_before_journal_append(
    tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    artifacts = _artifacts(cycle)
    index = next(
        i for i, item in enumerate(artifacts) if item.kind == "openai_response_success"
    )
    response = json.loads(artifacts[index].payload)
    response["status"] = "incomplete"
    item = artifacts[index]
    artifacts[index] = ArtifactToFreeze(
        kind=item.kind,
        payload=json.dumps(response, separators=(",", ":"), sort_keys=True).encode(),
        source_uri_label=item.source_uri_label,
        retrieved_at=item.retrieved_at,
        predecessor_stage_sha256=item.predecessor_stage_sha256,
    )
    artifacts = _retarget(artifacts, result)

    with pytest.raises(ValueError, match="openai_response"):
        record_observation(output, result, artifacts)

    assert not (output / "artifacts.jsonl").exists()
    assert not (output / "results.jsonl").exists()


@pytest.mark.parametrize("journal", ("results.jsonl", "artifacts.jsonl"))
def test_truncated_tail_fails_closed(
    tmp_path: Path, cycle: dict[str, Any], journal: str
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    record_observation(output, result, _artifacts(cycle))
    path = output / journal
    path.write_bytes(path.read_bytes()[:-1])
    path.chmod(0o600)

    with pytest.raises(StoreCorruptError, match="truncated"):
        load_observation(output, result.observation_id)


def test_duplicate_keys_and_duplicate_ids_fail_closed(
    tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    record_observation(output, result, _artifacts(cycle))
    results = output / "results.jsonl"
    original = _journal_lines(results)[0]
    duplicate_key = (
        original[:-1]
        + b',"observation_id":"'
        + result.observation_id.encode()
        + b'"}\n'
    )
    results.write_bytes(duplicate_key)
    results.chmod(0o600)
    with pytest.raises(StoreCorruptError, match="result journal"):
        load_observation(output, result.observation_id)

    results.write_bytes(original + b"\n" + original + b"\n")
    results.chmod(0o600)
    with pytest.raises(StoreCorruptError, match="duplicate observation"):
        load_observation(output, result.observation_id)


def test_replay_rejects_missing_and_corrupt_blob(
    tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    record_observation(output, result, _artifacts(cycle))
    envelope = ArtifactEnvelope.model_validate_json(
        _journal_lines(output / "artifacts.jsonl")[0]
    )
    blob = output / "blobs" / envelope.storage_relpath

    blob.write_bytes(b"tampered")
    blob.chmod(0o600)
    with pytest.raises(StoreCorruptError, match="artifact blob"):
        load_observation(output, result.observation_id)

    blob.unlink()
    with pytest.raises(StoreCorruptError, match="artifact blob"):
        load_observation(output, result.observation_id)


@pytest.mark.parametrize("mutation", ("unknown", "duplicate", "missing", "mismatch"))
def test_replay_rejects_invalid_stage_journal(
    tmp_path: Path, cycle: dict[str, Any], mutation: str
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    record_observation(output, result, _artifacts(cycle))
    journal = output / "artifacts.jsonl"
    lines = _journal_lines(journal)
    if mutation == "unknown":
        value = json.loads(lines[0])
        value["kind"] = "unknown_stage"
        lines[0] = canonical_json_bytes(value)
    elif mutation == "duplicate":
        lines.insert(1, lines[0])
    elif mutation == "missing":
        lines.pop(4)
    else:
        value = json.loads(lines[0])
        value["opportunity_id"] = "0" * 64
        lines[0] = canonical_json_bytes(value)
    journal.write_bytes(b"\n".join(lines) + b"\n")
    journal.chmod(0o600)

    with pytest.raises(StoreCorruptError, match="artifact|stage"):
        load_observation(output, result.observation_id)


def test_replay_rejects_bad_envelope_hash(
    tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    record_observation(output, result, _artifacts(cycle))
    journal = output / "artifacts.jsonl"
    lines = _journal_lines(journal)
    value = json.loads(lines[0])
    value["sha256"] = "0" * 64
    value["storage_relpath"] = "00/00/" + "0" * 64
    lines[0] = canonical_json_bytes(value)
    journal.write_bytes(b"\n".join(lines) + b"\n")
    journal.chmod(0o600)

    with pytest.raises(StoreCorruptError, match="artifact"):
        load_observation(output, result.observation_id)


def test_output_symlink_is_rejected(tmp_path: Path, cycle: dict[str, Any]) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "output"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(StoreSecurityError, match="output"):
        record_observation(linked, _result(cycle), _artifacts(cycle))

    assert not (real / "results.jsonl").exists()


@pytest.mark.parametrize("attack", ("symlink", "fifo", "hardlink", "weak_mode"))
def test_unsafe_existing_result_journal_is_rejected(
    tmp_path: Path, cycle: dict[str, Any], attack: str
) -> None:
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    path = output / "results.jsonl"
    outside = tmp_path / "outside"
    outside.write_bytes(b"")
    outside.chmod(0o600)
    if attack == "symlink":
        path.symlink_to(outside)
    elif attack == "fifo":
        os.mkfifo(path, 0o600)
    elif attack == "hardlink":
        os.link(outside, path)
    else:
        path.write_bytes(b"")
        path.chmod(0o640)

    with pytest.raises(StoreSecurityError, match="journal"):
        record_observation(output, _result(cycle), _artifacts(cycle))


def test_failure_appending_artifacts_never_appends_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cycle: dict[str, Any]
) -> None:
    from jev_trading_lab.polymarket import store

    output = tmp_path / "output"
    real_append = store._append_lines

    def fail_artifacts(descriptor: int, lines: list[bytes], *, label: str) -> None:
        if label == "artifact journal":
            raise OSError("simulated artifact journal failure")
        real_append(descriptor, lines, label=label)

    monkeypatch.setattr(store, "_append_lines", fail_artifacts)
    with pytest.raises(OSError, match="simulated"):
        record_observation(output, _result(cycle), _artifacts(cycle))

    results = output / "results.jsonl"
    assert not results.exists() or results.read_bytes() == b""


def _leave_durable_artifact_orphan(
    monkeypatch: pytest.MonkeyPatch,
    output: Path,
    result: ObservationResult,
    artifacts: list[ArtifactToFreeze],
) -> None:
    from jev_trading_lab.polymarket import store

    real_append = store._append_lines

    def fail_result(descriptor: int, lines: list[bytes], *, label: str) -> None:
        if label == "result journal":
            raise OSError("simulated result journal failure")
        real_append(descriptor, lines, label=label)

    monkeypatch.setattr(store, "_append_lines", fail_result)
    with pytest.raises(OSError, match="simulated"):
        record_observation(output, result, artifacts)
    monkeypatch.setattr(store, "_append_lines", real_append)

    assert len(_journal_lines(output / "artifacts.jsonl")) == len(artifacts)
    assert (output / "results.jsonl").read_bytes() == b""


def _replace_artifact_payload(
    artifacts: list[ArtifactToFreeze], kind: str, value: dict[str, Any]
) -> list[ArtifactToFreeze]:
    return _replace_artifact_bytes(artifacts, kind, canonical_json_bytes(value))


def _replace_artifact_bytes(
    artifacts: list[ArtifactToFreeze], kind: str, payload: bytes
) -> list[ArtifactToFreeze]:
    index = next(i for i, item in enumerate(artifacts) if item.kind == kind)
    item = artifacts[index]
    artifacts[index] = ArtifactToFreeze(
        kind=item.kind,
        payload=payload,
        source_uri_label=item.source_uri_label,
        retrieved_at=item.retrieved_at,
        predecessor_stage_sha256=item.predecessor_stage_sha256,
    )
    return artifacts


def _install_durable_artifact_orphan(
    output: Path,
    result: ObservationResult,
    artifacts: list[ArtifactToFreeze],
) -> None:
    lines: list[bytes] = []
    with BlobStore(output / "blobs") as blob_store:
        for artifact in artifacts:
            ref = blob_store.publish(artifact.payload)
            envelope = ArtifactEnvelope(
                kind=artifact.kind,
                sha256=ref.sha256,
                byte_size=ref.byte_length,
                storage_relpath=ref.relative_path,
                source_uri_label=artifact.source_uri_label,
                retrieved_at=artifact.retrieved_at,
                opportunity_id=result.opportunity_id,
                observation_id=result.observation_id,
                predecessor_stage_sha256=artifact.predecessor_stage_sha256,
            )
            lines.append(canonical_json_bytes(envelope.model_dump(mode="json")))
    journal = output / "artifacts.jsonl"
    journal.write_bytes(b"\n".join(lines) + b"\n")
    journal.chmod(0o600)


def _forged_success_orphan(
    cycle: dict[str, Any], result: ObservationResult, mutation: str
) -> list[ArtifactToFreeze]:
    artifacts = _artifacts(cycle)
    if mutation == "evidence_source":
        raw = json.loads(artifacts[0].payload)
        raw["forged"] = True
        artifacts = _replace_artifact_payload(artifacts, "raw_gamma", raw)
    elif mutation == "evidence_identity":
        evidence = json.loads(
            next(
                item.payload for item in artifacts if item.kind == "sanitized_evidence"
            )
        )
        evidence["question"] = "Forged question?"
        artifacts = _replace_artifact_payload(artifacts, "sanitized_evidence", evidence)
        evidence_bytes = next(
            item.payload for item in artifacts if item.kind == "sanitized_evidence"
        )
        request = load_strict_json(
            build_openai_request(
                EvidencePacket.model_validate_json(evidence_bytes), OPENAI_MODEL_ID
            )
        )
        artifacts = _replace_artifact_payload(artifacts, "openai_request", request)
        request_bytes = next(
            item.payload for item in artifacts if item.kind == "openai_request"
        )
        metadata = json.loads(
            next(
                item.payload
                for item in artifacts
                if item.kind == "openai_success_metadata"
            )
        )
        metadata["request_sha256"] = hashlib.sha256(request_bytes).hexdigest()
        artifacts = _replace_artifact_payload(
            artifacts, "openai_success_metadata", metadata
        )
    elif mutation == "openai_metadata_time":
        index = next(
            i
            for i, item in enumerate(artifacts)
            if item.kind == "openai_success_metadata"
        )
        item = artifacts[index]
        artifacts[index] = ArtifactToFreeze(
            kind=item.kind,
            payload=item.payload,
            source_uri_label=item.source_uri_label,
            retrieved_at=item.retrieved_at + timedelta(seconds=1),
            predecessor_stage_sha256=item.predecessor_stage_sha256,
        )
    elif mutation in {"openai_request_hash", "openai_response_hash", "openai_cost"}:
        index = next(
            i
            for i, item in enumerate(artifacts)
            if item.kind == "openai_success_metadata"
        )
        metadata = json.loads(artifacts[index].payload)
        field = {
            "openai_request_hash": "request_sha256",
            "openai_response_hash": "response_sha256",
            "openai_cost": "billed_cost_microdollars",
        }[mutation]
        metadata[field] = 2**63 - 1 if mutation == "openai_cost" else "0" * 64
        artifacts = _replace_artifact_payload(
            artifacts, "openai_success_metadata", metadata
        )
    elif mutation == "openai_response_brief":
        response = json.loads(
            next(
                item.payload
                for item in artifacts
                if item.kind == "openai_response_success"
            )
        )
        message = next(item for item in response["output"] if item["type"] == "message")
        brief = json.loads(message["content"][0]["text"])
        brief["quality"] = "HIGH"
        message["content"][0]["text"] = canonical_json_bytes(brief).decode()
        artifacts = _replace_artifact_bytes(
            artifacts,
            "openai_response_success",
            json.dumps(response, separators=(",", ":"), sort_keys=True).encode(),
        )
        response_bytes = next(
            item.payload for item in artifacts if item.kind == "openai_response_success"
        )
        metadata = json.loads(
            next(
                item.payload
                for item in artifacts
                if item.kind == "openai_success_metadata"
            )
        )
        metadata["response_sha256"] = hashlib.sha256(response_bytes).hexdigest()
        artifacts = _replace_artifact_payload(
            artifacts, "openai_success_metadata", metadata
        )
    elif mutation == "research_brief_request":
        request = json.loads(
            next(item.payload for item in artifacts if item.kind == "jev_request")
        )
        request["state"]["research_brief_sha256"] = "0" * 64
        artifacts = _replace_artifact_payload(artifacts, "jev_request", request)
        request_bytes = next(
            item.payload for item in artifacts if item.kind == "jev_request"
        )
        metadata = json.loads(
            next(
                item.payload
                for item in artifacts
                if item.kind == "jev_success_metadata"
            )
        )
        metadata["request_sha256"] = hashlib.sha256(request_bytes).hexdigest()
        artifacts = _replace_artifact_payload(
            artifacts, "jev_success_metadata", metadata
        )
    elif mutation == "jev_request_books":
        request = json.loads(
            next(item.payload for item in artifacts if item.kind == "jev_request")
        )
        request["state"]["execution_book_sha256"] = result.execution_book_sha256
        artifacts = _replace_artifact_payload(artifacts, "jev_request", request)
        request_bytes = next(
            item.payload for item in artifacts if item.kind == "jev_request"
        )
        metadata = json.loads(
            next(
                item.payload
                for item in artifacts
                if item.kind == "jev_success_metadata"
            )
        )
        metadata["request_sha256"] = hashlib.sha256(request_bytes).hexdigest()
        artifacts = _replace_artifact_payload(
            artifacts, "jev_success_metadata", metadata
        )
    elif mutation == "jev_metadata_time":
        index = next(
            i for i, item in enumerate(artifacts) if item.kind == "jev_success_metadata"
        )
        item = artifacts[index]
        artifacts[index] = ArtifactToFreeze(
            kind=item.kind,
            payload=item.payload,
            source_uri_label=item.source_uri_label,
            retrieved_at=item.retrieved_at + timedelta(seconds=1),
            predecessor_stage_sha256=item.predecessor_stage_sha256,
        )
    elif mutation in {"jev_request_hash", "jev_response_hash", "jev_cost"}:
        index = next(
            i for i, item in enumerate(artifacts) if item.kind == "jev_success_metadata"
        )
        metadata = json.loads(artifacts[index].payload)
        field = {
            "jev_request_hash": "request_sha256",
            "jev_response_hash": "response_sha256",
            "jev_cost": "billed_cost_microdollars",
        }[mutation]
        metadata[field] = 2**63 - 1 if mutation == "jev_cost" else "0" * 64
        artifacts = _replace_artifact_payload(
            artifacts, "jev_success_metadata", metadata
        )
    else:
        decision = json.loads(
            next(item.payload for item in artifacts if item.kind == "jev_decision")
        )
        decision["p_yes_micros"] -= 1
        artifacts = _replace_artifact_payload(artifacts, "jev_decision", decision)
    return _retarget(artifacts, result)


@pytest.mark.parametrize(
    "mutation",
    (
        "evidence_source",
        "evidence_identity",
        "openai_request_hash",
        "openai_response_hash",
        "openai_metadata_time",
        "openai_cost",
        "openai_response_brief",
        "research_brief_request",
        "jev_request_books",
        "jev_request_hash",
        "jev_response_hash",
        "jev_metadata_time",
        "jev_cost",
        "jev_response_decision",
    ),
)
def test_forged_complete_success_orphan_blocks_unrelated_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cycle: dict[str, Any],
    mutation: str,
) -> None:
    from jev_trading_lab.polymarket import store

    output = tmp_path / "output"
    orphan = _result(cycle, "orphan-a")
    artifacts = _retarget(_artifacts(cycle), orphan)
    _leave_durable_artifact_orphan(monkeypatch, output, orphan, artifacts)
    forged = _forged_success_orphan(cycle, orphan, mutation)
    _install_durable_artifact_orphan(output, orphan, forged)
    before = (
        (output / "artifacts.jsonl").read_bytes(),
        (output / "results.jsonl").read_bytes(),
    )
    published = 0
    real_publish = store.BlobStore.publish

    def count_publish(blob_store: store.BlobStore, payload: bytes):
        nonlocal published
        published += 1
        return real_publish(blob_store, payload)

    monkeypatch.setattr(store.BlobStore, "publish", count_publish)
    unrelated = _result(cycle, "unrelated-b")

    with pytest.raises(StoreCorruptError):
        record_observation(output, unrelated, _retarget(_artifacts(cycle), unrelated))

    assert published == 0
    assert (output / "artifacts.jsonl").read_bytes() == before[0]
    assert (output / "results.jsonl").read_bytes() == before[1]


def test_success_prefix_rejects_cumulative_cost_overflow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    orphan = _result(cycle, "orphan-a")
    artifacts = _replace_success_cost(_artifacts(cycle), "openai", 2**63 - 1)
    artifacts = _replace_success_cost(artifacts, "jev", 1)
    prefix = _retarget(artifacts, orphan)[:9]
    valid = _retarget(_artifacts(cycle), orphan)
    _leave_durable_artifact_orphan(monkeypatch, output, orphan, valid)
    _install_durable_artifact_orphan(output, orphan, prefix)
    before = (output / "artifacts.jsonl").read_bytes()
    unrelated = _result(cycle, "unrelated-b")

    with pytest.raises(StoreCorruptError, match="model cost|64-bit|overflow"):
        record_observation(output, unrelated, _retarget(_artifacts(cycle), unrelated))

    assert (output / "artifacts.jsonl").read_bytes() == before
    assert (output / "results.jsonl").read_bytes() == b""


def test_forged_success_prefix_validates_all_available_bindings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    orphan = _result(cycle, "orphan-a")
    artifacts = _retarget(_artifacts(cycle), orphan)
    _leave_durable_artifact_orphan(monkeypatch, output, orphan, artifacts)
    forged = _forged_success_orphan(cycle, orphan, "openai_request_hash")[:5]
    _install_durable_artifact_orphan(output, orphan, forged)
    before = (output / "artifacts.jsonl").read_bytes()
    unrelated = _result(cycle, "unrelated-b")

    with pytest.raises(StoreCorruptError, match="OpenAI success request hash"):
        record_observation(output, unrelated, _retarget(_artifacts(cycle), unrelated))

    assert (output / "artifacts.jsonl").read_bytes() == before
    assert (output / "results.jsonl").read_bytes() == b""


def test_valid_complete_success_orphan_allows_unrelated_then_recovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    orphan = _result(cycle, "orphan-a")
    orphan_artifacts = _retarget(_artifacts(cycle), orphan)
    _leave_durable_artifact_orphan(monkeypatch, output, orphan, orphan_artifacts)
    unrelated = _result(cycle, "unrelated-b")

    assert (
        record_observation(output, unrelated, _retarget(_artifacts(cycle), unrelated))
        == unrelated
    )
    assert record_observation(output, orphan, orphan_artifacts) == orphan
    assert len(_journal_lines(output / "artifacts.jsonl")) == 22
    assert len(_journal_lines(output / "results.jsonl")) == 2


def test_retry_recovers_matching_artifact_journal_after_result_append_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    artifacts = _artifacts(cycle)
    _leave_durable_artifact_orphan(monkeypatch, output, result, artifacts)

    assert record_observation(output, result, artifacts) == result
    assert len(_journal_lines(output / "artifacts.jsonl")) == len(artifacts)
    assert len(_journal_lines(output / "results.jsonl")) == 1
    assert load_observation(output, result.observation_id).result == result


def test_concurrent_retries_recover_artifact_orphan_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    artifacts = _artifacts(cycle)
    _leave_durable_artifact_orphan(monkeypatch, output, result, artifacts)

    with ThreadPoolExecutor(max_workers=8) as pool:
        recovered = list(
            pool.map(
                lambda _index: record_observation(output, result, artifacts), range(8)
            )
        )

    assert recovered == [result] * 8
    assert len(_journal_lines(output / "artifacts.jsonl")) == len(artifacts)
    assert len(_journal_lines(output / "results.jsonl")) == 1


@pytest.mark.parametrize("mutation", ("mismatch", "incomplete", "duplicate", "unknown"))
def test_retry_rejects_invalid_artifact_orphan_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cycle: dict[str, Any],
    mutation: str,
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    artifacts = _artifacts(cycle)
    _leave_durable_artifact_orphan(monkeypatch, output, result, artifacts)
    journal = output / "artifacts.jsonl"
    lines = _journal_lines(journal)
    if mutation == "mismatch":
        value = json.loads(lines[0])
        value["source_uri_label"] = "different_public"
        lines[0] = canonical_json_bytes(value)
    elif mutation == "incomplete":
        lines.pop(3)
    elif mutation == "duplicate":
        lines.append(lines[-1])
    else:
        value = json.loads(lines[0])
        value["kind"] = "unknown_stage"
        lines[0] = canonical_json_bytes(value)
    journal.write_bytes(b"\n".join(lines) + b"\n")
    journal.chmod(0o600)
    before = journal.read_bytes()

    with pytest.raises((StoreConflictError, StoreCorruptError)):
        record_observation(output, result, artifacts)

    assert journal.read_bytes() == before
    assert (output / "results.jsonl").read_bytes() == b""


def _corrupt_second_stage_predecessor(output: Path) -> None:
    journal = output / "artifacts.jsonl"
    lines = _journal_lines(journal)
    value = json.loads(lines[1])
    value["predecessor_stage_sha256"] = "0" * 64
    lines[1] = canonical_json_bytes(value)
    journal.write_bytes(b"\n".join(lines) + b"\n")
    journal.chmod(0o600)


def test_corrupt_orphan_a_blocks_unrelated_observation_b_without_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    orphan = _result(cycle, "orphan-a")
    _leave_durable_artifact_orphan(
        monkeypatch, output, orphan, _retarget(_artifacts(cycle), orphan)
    )
    _corrupt_second_stage_predecessor(output)
    before = (
        (output / "artifacts.jsonl").read_bytes(),
        (output / "results.jsonl").read_bytes(),
    )
    unrelated = _result(cycle, "unrelated-b")

    with pytest.raises(StoreCorruptError, match="predecessor"):
        record_observation(output, unrelated, _retarget(_artifacts(cycle), unrelated))

    assert (output / "artifacts.jsonl").read_bytes() == before[0]
    assert (output / "results.jsonl").read_bytes() == before[1]


@pytest.mark.parametrize("corruption", ("artifact_predecessor", "truncated_results"))
def test_existing_corruption_blocks_candidate_blob_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cycle: dict[str, Any],
    corruption: str,
) -> None:
    from jev_trading_lab.polymarket import store

    output = tmp_path / "output"
    completed = _result(cycle, "completed-a")
    record_observation(output, completed, _retarget(_artifacts(cycle), completed))
    if corruption == "artifact_predecessor":
        _corrupt_second_stage_predecessor(output)
    else:
        results = output / "results.jsonl"
        results.write_bytes(results.read_bytes()[:-1])
        results.chmod(0o600)
    published = 0
    real_publish = store.BlobStore.publish

    def count_publish(blob_store: store.BlobStore, payload: bytes):
        nonlocal published
        published += 1
        return real_publish(blob_store, payload)

    monkeypatch.setattr(store.BlobStore, "publish", count_publish)
    unrelated = _result(cycle, "unrelated-b")

    with pytest.raises(StoreCorruptError, match="predecessor|truncated"):
        record_observation(output, unrelated, _retarget(_artifacts(cycle), unrelated))

    assert published == 0


def test_corrupt_completed_a_blocks_unrelated_observation_b_without_mutation(
    tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    completed = _result(cycle, "completed-a")
    record_observation(output, completed, _retarget(_artifacts(cycle), completed))
    _corrupt_second_stage_predecessor(output)
    before = (
        (output / "artifacts.jsonl").read_bytes(),
        (output / "results.jsonl").read_bytes(),
    )
    unrelated = _result(cycle, "unrelated-b")

    with pytest.raises(StoreCorruptError, match="predecessor"):
        record_observation(output, unrelated, _retarget(_artifacts(cycle), unrelated))

    assert (output / "artifacts.jsonl").read_bytes() == before[0]
    assert (output / "results.jsonl").read_bytes() == before[1]


def test_retry_fsyncs_existing_identical_result_after_prior_fsync_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cycle: dict[str, Any]
) -> None:
    from jev_trading_lab.polymarket import store

    output = tmp_path / "output"
    result = _result(cycle)
    artifacts = _artifacts(cycle)
    real_fsync = store.os.fsync
    failed = False

    def fail_result_fsync(descriptor: int) -> None:
        nonlocal failed
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        if not failed and target.endswith("results.jsonl"):
            failed = True
            raise OSError("simulated result fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(store.os, "fsync", fail_result_fsync)
    with pytest.raises(OSError, match="simulated"):
        record_observation(output, result, artifacts)
    assert len(_journal_lines(output / "results.jsonl")) == 1

    fsynced: list[str] = []

    def track_fsync(descriptor: int) -> None:
        fsynced.append(os.readlink(f"/proc/self/fd/{descriptor}"))
        real_fsync(descriptor)

    monkeypatch.setattr(store.os, "fsync", track_fsync)
    assert record_observation(output, result, artifacts) == result
    assert any(path.endswith("results.jsonl") for path in fsynced)
    assert any(path.endswith("artifacts.jsonl") for path in fsynced)
    artifact_index = next(
        index for index, path in enumerate(fsynced) if path.endswith("artifacts.jsonl")
    )
    result_index = next(
        index for index, path in enumerate(fsynced) if path.endswith("results.jsonl")
    )
    assert artifact_index < result_index


def test_retry_fsyncs_complete_artifacts_before_result_after_artifact_fsync_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cycle: dict[str, Any]
) -> None:
    from jev_trading_lab.polymarket import store

    output = tmp_path / "output"
    result = _result(cycle)
    artifacts = _artifacts(cycle)
    real_fsync = store.os.fsync
    failed = False

    def fail_artifact_fsync(descriptor: int) -> None:
        nonlocal failed
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        if not failed and target.endswith("artifacts.jsonl"):
            failed = True
            raise OSError("simulated artifact fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(store.os, "fsync", fail_artifact_fsync)
    with pytest.raises(OSError, match="simulated"):
        record_observation(output, result, artifacts)
    assert len(_journal_lines(output / "artifacts.jsonl")) == len(artifacts)
    assert (output / "results.jsonl").read_bytes() == b""

    fsynced: list[str] = []

    def track_fsync(descriptor: int) -> None:
        fsynced.append(os.readlink(f"/proc/self/fd/{descriptor}"))
        real_fsync(descriptor)

    monkeypatch.setattr(store.os, "fsync", track_fsync)
    assert record_observation(output, result, artifacts) == result
    journals = [
        path for path in fsynced if path.endswith(("artifacts.jsonl", "results.jsonl"))
    ]
    assert journals == [
        str(output / "artifacts.jsonl"),
        str(output / "results.jsonl"),
    ]


@pytest.mark.parametrize("prefix_length", (1, 4, 7))
def test_retry_completes_canonical_partial_artifact_prefix(
    tmp_path: Path, cycle: dict[str, Any], prefix_length: int
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    artifacts = _artifacts(cycle)
    expected = [
        ArtifactEnvelope(
            kind=item.kind,
            sha256=hashlib.sha256(item.payload).hexdigest(),
            byte_size=len(item.payload),
            storage_relpath=(
                f"{hashlib.sha256(item.payload).hexdigest()[:2]}/"
                f"{hashlib.sha256(item.payload).hexdigest()[2:4]}/"
                f"{hashlib.sha256(item.payload).hexdigest()}"
            ),
            source_uri_label=item.source_uri_label,
            retrieved_at=item.retrieved_at,
            opportunity_id=result.opportunity_id,
            observation_id=result.observation_id,
            predecessor_stage_sha256=item.predecessor_stage_sha256,
        )
        for item in artifacts
    ]
    output.mkdir(mode=0o700)
    with BlobStore(output / "blobs") as store:
        for artifact in artifacts[:prefix_length]:
            store.publish(artifact.payload)
    journal = output / "artifacts.jsonl"
    journal.write_bytes(
        b"\n".join(
            canonical_json_bytes(item.model_dump(mode="json"))
            for item in expected[:prefix_length]
        )
        + b"\n"
    )
    journal.chmod(0o600)

    assert record_observation(output, result, artifacts) == result
    assert len(_journal_lines(journal)) == len(artifacts)
    assert len(_journal_lines(output / "results.jsonl")) == 1
    assert load_observation(output, result.observation_id).result == result


@pytest.mark.parametrize("persisted_lines", (1, 4, 7))
def test_enospc_mid_artifact_append_retry_completes_suffix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cycle: dict[str, Any],
    persisted_lines: int,
) -> None:
    from jev_trading_lab.polymarket import store

    output = tmp_path / "output"
    result = _result(cycle)
    artifacts = _artifacts(cycle)
    real_write = store.os.write
    artifact_writes = 0

    def fail_mid_append(descriptor: int, data: bytes | memoryview) -> int:
        nonlocal artifact_writes
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        if target.endswith("artifacts.jsonl"):
            artifact_writes += 1
            if artifact_writes > persisted_lines:
                raise OSError(errno.ENOSPC, "simulated full disk")
        return real_write(descriptor, data)

    monkeypatch.setattr(store.os, "write", fail_mid_append)
    with pytest.raises(OSError) as caught:
        record_observation(output, result, artifacts)
    assert caught.value.errno == errno.ENOSPC
    assert len(_journal_lines(output / "artifacts.jsonl")) == persisted_lines
    assert (output / "results.jsonl").read_bytes() == b""

    monkeypatch.setattr(store.os, "write", real_write)
    assert record_observation(output, result, artifacts) == result
    assert len(_journal_lines(output / "artifacts.jsonl")) == len(artifacts)
    assert len(_journal_lines(output / "results.jsonl")) == 1


def test_concurrent_retries_converge_from_partial_artifact_prefix(
    tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    artifacts = _artifacts(cycle)
    output.mkdir(mode=0o700)
    first = artifacts[0]
    with BlobStore(output / "blobs") as store:
        store.publish(first.payload)
    digest = hashlib.sha256(first.payload).hexdigest()
    envelope = ArtifactEnvelope(
        kind=first.kind,
        sha256=digest,
        byte_size=len(first.payload),
        storage_relpath=f"{digest[:2]}/{digest[2:4]}/{digest}",
        source_uri_label=first.source_uri_label,
        retrieved_at=first.retrieved_at,
        opportunity_id=result.opportunity_id,
        observation_id=result.observation_id,
        predecessor_stage_sha256=None,
    )
    journal = output / "artifacts.jsonl"
    journal.write_bytes(canonical_json_bytes(envelope.model_dump(mode="json")) + b"\n")
    journal.chmod(0o600)

    with ThreadPoolExecutor(max_workers=8) as pool:
        recovered = list(
            pool.map(
                lambda _index: record_observation(output, result, artifacts), range(8)
            )
        )

    assert recovered == [result] * 8
    assert len(_journal_lines(journal)) == len(artifacts)
    assert len(_journal_lines(output / "results.jsonl")) == 1


def test_threads_append_distinct_results_and_same_identity_converges(
    tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    artifacts = _artifacts(cycle)
    results = [_result(cycle, str(index)) for index in range(8)]

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [
            pool.submit(
                record_observation, output, result, _retarget(artifacts, result)
            )
            for result in results
        ]
        futures.extend(
            pool.submit(
                record_observation, output, results[0], _retarget(artifacts, results[0])
            )
            for _ in range(4)
        )
        assert [future.result() for future in futures]

    lines = _journal_lines(output / "results.jsonl")
    parsed = [ObservationResult.model_validate_json(line) for line in lines]
    assert len(parsed) == len(results)
    assert {item.observation_id for item in parsed} == {
        item.observation_id for item in results
    }


def test_processes_append_intact_lines_and_same_identity_converges(
    tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    context = multiprocessing.get_context("fork")
    queue: multiprocessing.Queue[str] = context.Queue()
    suffixes = ("same", "same", "distinct-1", "distinct-2")
    processes = [
        context.Process(
            target=_process_record,
            args=(str(output), cycle, suffix, queue),
        )
        for suffix in suffixes
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    assert [queue.get(timeout=5) for _ in processes] == ["ok"] * len(processes)

    lines = _journal_lines(output / "results.jsonl")
    assert len(lines) == len(set(suffixes))
    assert len(
        {ObservationResult.model_validate_json(line).observation_id for line in lines}
    ) == len(set(suffixes))


@pytest.mark.parametrize(
    "secret",
    (
        b"OPENAI_API_KEY=top-secret",
        b"typesafe_api_key=top-secret",
        b"Authorization: Bearer top-secret",
        b"Cookie: session=top-secret",
        "ＡＰＩ＿ＫＥＹ＝top-secret".encode(),
    ),
)
def test_secret_artifact_rejects_before_durable_append(
    tmp_path: Path, cycle: dict[str, Any], secret: bytes
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    artifacts = _artifacts(cycle)
    index = next(i for i, item in enumerate(artifacts) if item.kind == "openai_request")
    item = artifacts[index]
    artifacts[index] = ArtifactToFreeze(
        kind=item.kind,
        payload=secret,
        source_uri_label=item.source_uri_label,
        retrieved_at=item.retrieved_at,
        predecessor_stage_sha256=item.predecessor_stage_sha256,
    )

    with pytest.raises(StoreSecurityError) as caught:
        record_observation(output, result, artifacts)

    assert b"top-secret" not in str(caught.value).encode()
    assert not (output / "artifacts.jsonl").exists()
    assert not (output / "results.jsonl").exists()


@pytest.mark.parametrize(
    "secret",
    (
        b'{"Authorization":"Basic top-secret"}',
        b'{"Cookie":"session=top-secret"}',
        b'{"x-api-key":"top-secret"}',
        b'{"\\u0041uthorization":"Basic top-secret"}',
        '{"Ａｕｔｈｏｒｉｚａｔｉｏｎ":"Basic top-secret"}'.encode(),
    ),
)
def test_semantic_json_secret_fields_reject_without_leaking(
    tmp_path: Path, cycle: dict[str, Any], secret: bytes
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    artifacts = _artifacts(cycle)
    index = next(i for i, item in enumerate(artifacts) if item.kind == "openai_request")
    item = artifacts[index]
    artifacts[index] = ArtifactToFreeze(
        kind=item.kind,
        payload=secret,
        source_uri_label=item.source_uri_label,
        retrieved_at=item.retrieved_at,
        predecessor_stage_sha256=item.predecessor_stage_sha256,
    )

    with pytest.raises(StoreSecurityError) as caught:
        record_observation(output, result, artifacts)

    assert "top-secret" not in repr(caught.value)
    assert not (output / "artifacts.jsonl").exists()
    assert not (output / "results.jsonl").exists()


def test_configured_fixture_secret_rejects_without_leaking(
    tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    artifacts = _artifacts(cycle)
    sentinel = b"FIXTURE_SECRET_SENTINEL"
    index = next(i for i, item in enumerate(artifacts) if item.kind == "openai_request")
    item = artifacts[index]
    artifacts[index] = ArtifactToFreeze(
        kind=item.kind,
        payload=b'{"input":"fixture_secret_sentinel"}',
        source_uri_label=item.source_uri_label,
        retrieved_at=item.retrieved_at,
        predecessor_stage_sha256=item.predecessor_stage_sha256,
    )

    with pytest.raises(StoreSecurityError) as caught:
        record_observation(
            output, result, artifacts, forbidden_secret_values=(sentinel,)
        )

    assert sentinel not in repr(caught.value).encode()
    assert not (output / "artifacts.jsonl").exists()
    assert not (output / "results.jsonl").exists()


@pytest.mark.parametrize(
    "gamma_payload,sentinel",
    (
        (b'{"\\u0041uthorization":"Basic top-secret"}', None),
        (b'{"auth_token":"top-secret"}', None),
        (b'{"Cookie":"session=top-secret"}', None),
        (b'{"api-key":"top-secret"}', None),
        (b'{"description":"\\u0053ECRET-SENTINEL"}', b"secret-sentinel"),
    ),
)
def test_raw_gamma_semantic_secrets_reject_before_publish_without_leaking(
    tmp_path: Path,
    cycle: dict[str, Any],
    gamma_payload: bytes,
    sentinel: bytes | None,
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    artifacts = _artifacts(cycle)
    first = artifacts[0]
    artifacts[0] = ArtifactToFreeze(
        kind=first.kind,
        payload=gamma_payload,
        source_uri_label=first.source_uri_label,
        retrieved_at=first.retrieved_at,
        predecessor_stage_sha256=None,
    )
    artifacts = _retarget(artifacts, result)

    with pytest.raises(StoreSecurityError) as caught:
        record_observation(
            output,
            result,
            artifacts,
            forbidden_secret_values=(sentinel,) if sentinel is not None else (),
        )

    assert "top-secret" not in repr(caught.value)
    assert "secret-sentinel" not in repr(caught.value).casefold()
    assert not (output / "blobs").exists()
    assert not (output / "artifacts.jsonl").exists()
    assert not (output / "results.jsonl").exists()


@pytest.mark.parametrize(
    "gamma_payload", (b"not json", b'{"id":1,"id":2}', b'["not", "an", "object"]')
)
def test_raw_gamma_requires_strict_json_object_before_publish(
    tmp_path: Path, cycle: dict[str, Any], gamma_payload: bytes
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    artifacts = _artifacts(cycle)
    first = artifacts[0]
    artifacts[0] = ArtifactToFreeze(
        kind=first.kind,
        payload=gamma_payload,
        source_uri_label=first.source_uri_label,
        retrieved_at=first.retrieved_at,
        predecessor_stage_sha256=None,
    )
    artifacts = _retarget(artifacts, result)

    with pytest.raises((ValueError, StoreSecurityError), match="Gamma|JSON|structured"):
        record_observation(output, result, artifacts)

    assert not (output / "blobs").exists()
    assert not (output / "artifacts.jsonl").exists()
    assert not (output / "results.jsonl").exists()


def test_ordinary_market_identifiers_are_not_secret_patterns(
    tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    record_observation(output, result, _artifacts(cycle))

    assert load_observation(output, result.observation_id).result == result


def test_same_opportunity_distinct_observations_coexist(
    tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    first = _result(cycle)
    value = first.model_dump(mode="json")
    value["execution_book_sha256"] = "0" * 64
    value["observation_id"] = hashlib.sha256(
        canonical_json_bytes(
            {
                "opportunity_id": first.opportunity_id,
                "evidence_sha256": first.evidence_sha256,
                "execution_book_sha256": value["execution_book_sha256"],
                "policy_version": first.policy_version,
            }
        )
    ).hexdigest()
    second = ObservationResult.model_validate_json(canonical_json_bytes(value))

    record_observation(output, first, _artifacts(cycle))
    record_observation(output, second, _retarget(_artifacts(cycle), second))

    assert load_observation(output, first.observation_id).result == first
    assert load_observation(output, second.observation_id).result == second
    assert len(_journal_lines(output / "results.jsonl")) == 2
    assert len(_journal_lines(output / "artifacts.jsonl")) == 22


def test_envelope_and_input_validation_is_strict(cycle: dict[str, Any]) -> None:
    artifact = _artifacts(cycle)[0]
    with pytest.raises(ValueError, match="source URI label"):
        ArtifactToFreeze(
            kind=artifact.kind,
            payload=artifact.payload,
            source_uri_label="https://example.test/?api_key=secret",
            retrieved_at=artifact.retrieved_at,
            predecessor_stage_sha256=None,
        )
    with pytest.raises(ValidationError):
        ArtifactEnvelope.model_validate(
            {
                "kind": "evidence",
                "sha256": "0" * 64,
                "byte_size": True,
                "storage_relpath": "00/00/" + "0" * 64,
                "source_uri_label": "gamma_public",
                "retrieved_at": "2026-10-03T12:00:00Z",
                "opportunity_id": "1" * 64,
                "predecessor_stage_sha256": None,
                "extra": 1,
            }
        )

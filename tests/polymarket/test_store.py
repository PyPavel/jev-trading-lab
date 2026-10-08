from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.polymarket.models import (
    BookSnapshot,
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
    StoreConflictError,
    StoreCorruptError,
    StoreSecurityError,
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
    precheck = BookSnapshot.model_validate_json(
        canonical_json_bytes(cycle["precheck_book"])
    )
    execution = BookSnapshot.model_validate_json(
        canonical_json_bytes(cycle["execution_book"])
    )
    brief = ResearchBrief.model_validate_json(
        canonical_json_bytes(cycle["research_brief"])
    )
    decision = JevDecision.model_validate_json(
        canonical_json_bytes(cycle["jev_decision"])
    )
    values = (
        (
            "gamma_raw",
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
            "precheck_book",
            cycle["raw"]["precheck_book_bytes"].encode(),
            "clob_public",
            precheck.retrieved_at,
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
            "openai_response",
            cycle["model_artifacts"]["openai_response_bytes"].encode(),
            "openai_response",
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
            "jev_response",
            cycle["model_artifacts"]["jev_response_bytes"].encode(),
            "jev_response",
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
            "execution_book",
            cycle["raw"]["execution_book_bytes"].encode(),
            "clob_public",
            execution.retrieved_at,
        ),
        (
            "observation_result",
            canonical_json_bytes(result.model_dump(mode="json")),
            "terminal_result",
            result.observed_at,
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
    assert result.jev_decision_sha256 == hashlib.sha256(values[8][1]).hexdigest()
    return artifacts


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
    assert replay.artifacts["gamma_raw"] == cycle["raw"]["gamma_bytes"].encode()
    assert (
        replay.artifacts["execution_book"]
        == cycle["raw"]["execution_book_bytes"].encode()
    )
    assert (
        hashlib.sha256(replay.artifacts["gamma_raw"]).hexdigest()
        == (cycle["expected"]["artifact_sha256"]["gamma_bytes"])
    )
    assert set(replay.artifacts) == {
        "gamma_raw",
        "sanitized_evidence",
        "precheck_book",
        "openai_request",
        "openai_response",
        "research_brief",
        "jev_request",
        "jev_response",
        "jev_decision",
        "execution_book",
        "observation_result",
    }
    assert isinstance(replay.typed_artifacts["sanitized_evidence"], EvidencePacket)
    assert isinstance(replay.typed_artifacts["precheck_book"], BookSnapshot)
    assert isinstance(replay.typed_artifacts["openai_response"], OpenAIResponse)
    assert isinstance(replay.typed_artifacts["research_brief"], ResearchBrief)
    assert isinstance(replay.typed_artifacts["jev_response"], JevResponse)
    assert isinstance(replay.typed_artifacts["jev_decision"], JevDecision)
    assert isinstance(replay.typed_artifacts["execution_book"], BookSnapshot)
    assert replay.typed_artifacts["observation_result"] == result


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
        i for i, item in enumerate(artifacts) if item.kind == "openai_response"
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

    assert len(_journal_lines(output / "artifacts.jsonl")) == 11
    assert (output / "results.jsonl").read_bytes() == b""


def test_retry_recovers_matching_artifact_journal_after_result_append_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cycle: dict[str, Any]
) -> None:
    output = tmp_path / "output"
    result = _result(cycle)
    artifacts = _artifacts(cycle)
    _leave_durable_artifact_orphan(monkeypatch, output, result, artifacts)

    assert record_observation(output, result, artifacts) == result
    assert len(_journal_lines(output / "artifacts.jsonl")) == 11
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
    assert len(_journal_lines(output / "artifacts.jsonl")) == 11
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
        lines.pop()
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
    execution_payload = cycle["raw"]["execution_book_bytes"].encode() + b" "
    execution_hash = hashlib.sha256(execution_payload).hexdigest()
    value["execution_book_sha256"] = execution_hash
    value["observation_id"] = hashlib.sha256(
        canonical_json_bytes(
            {
                "opportunity_id": first.opportunity_id,
                "evidence_sha256": first.evidence_sha256,
                "execution_book_sha256": execution_hash,
                "policy_version": first.policy_version,
            }
        )
    ).hexdigest()
    second = ObservationResult.model_validate_json(canonical_json_bytes(value))
    second_artifacts = _artifacts(cycle, second)
    execution_index = next(
        i for i, item in enumerate(second_artifacts) if item.kind == "execution_book"
    )
    item = second_artifacts[execution_index]
    second_artifacts[execution_index] = ArtifactToFreeze(
        kind=item.kind,
        payload=execution_payload,
        source_uri_label=item.source_uri_label,
        retrieved_at=item.retrieved_at,
        predecessor_stage_sha256=item.predecessor_stage_sha256,
    )
    second_artifacts = _retarget(second_artifacts, second)

    record_observation(output, first, _artifacts(cycle))
    record_observation(output, second, second_artifacts)

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

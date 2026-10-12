from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.polymarket.call_journal import ProviderCallJournal
from jev_trading_lab.polymarket.metrics_export import render_paper_metrics
from jev_trading_lab.polymarket.operations import (
    OperationEvent,
    OperationKind,
    append_operation,
    operation_event_id,
)

NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _local_test_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    from jev_trading_lab.persistence.blobs import BlobStore

    defaults = dict(BlobStore.__init__.__kwdefaults__ or {})
    defaults["filesystem_resolver"] = lambda _path: "ext4"
    monkeypatch.setattr(BlobStore.__init__, "__kwdefaults__", defaults)


def _event(**changes: Any) -> OperationEvent:
    kind = OperationKind(changes.pop("kind"))
    payload: dict[str, Any] = {
        "schema_version": 1,
        "occurred_at": changes.pop("occurred_at", NOW),
        "kind": kind,
        "run_id": changes.pop("run_id", "1" * 64),
        "semantic_id": None,
        "stage": changes.pop("stage", None),
        "provider": changes.pop("provider", None),
        "journal_kind": None,
        "status": changes.pop("status"),
        "reason": changes.pop("reason", None),
        "elapsed_nanoseconds": changes.pop("elapsed_nanoseconds", None),
        "exact_values": changes.pop("exact_values", {}),
        "evidence": None,
    }
    assert not changes
    payload.pop("journal_kind")
    payload["event_id"] = operation_event_id(payload)
    return OperationEvent.model_validate(payload)


def _append_run(
    root: Path,
    run_id: str,
    *,
    status: str,
    elapsed_nanoseconds: int,
    occurred_at: datetime,
    stages: tuple[OperationEvent, ...] = (),
) -> None:
    append_operation(
        root,
        _event(
            kind="run_started", run_id=run_id, status="started", occurred_at=occurred_at
        ),
    )
    for stage in stages:
        append_operation(root, stage)
    append_operation(
        root,
        _event(
            kind="process_sample",
            run_id=run_id,
            status="success",
            occurred_at=occurred_at + timedelta(seconds=1),
            exact_values={
                "user_cpu_nanoseconds": 0,
                "system_cpu_nanoseconds": 0,
                "probe_available": 0,
            },
        ),
    )
    append_operation(
        root,
        _event(
            kind="run_terminal",
            run_id=run_id,
            status=status,
            occurred_at=occurred_at + timedelta(seconds=2),
            elapsed_nanoseconds=elapsed_nanoseconds,
            reason="other" if status == "failure" else None,
        ),
    )


def test_market_metrics_reduce_only_persisted_attempt_events(tmp_path: Path) -> None:
    run_id = "1" * 64
    stages = (
        _event(
            kind="stage_terminal",
            run_id=run_id,
            stage="gamma_poll",
            provider="gamma",
            status="success",
            elapsed_nanoseconds=100_000_000,
        ),
        _event(
            kind="stage_terminal",
            run_id=run_id,
            stage="gamma_poll",
            provider="gamma",
            status="failure",
            reason="malformed",
            elapsed_nanoseconds=200_000_000,
            occurred_at=NOW + timedelta(microseconds=1),
        ),
        _event(
            kind="stage_terminal",
            run_id=run_id,
            stage="precheck_book",
            provider="clob",
            status="timeout",
            reason="timeout",
            elapsed_nanoseconds=300_000_000,
            occurred_at=NOW + timedelta(microseconds=2),
        ),
    )
    _append_run(
        tmp_path,
        run_id,
        status="success",
        elapsed_nanoseconds=1_000_000_000,
        occurred_at=NOW,
        stages=stages,
    )
    metrics = render_paper_metrics(tmp_path).read_text()
    assert (
        'jev_polymarket_market_data_calls_total{provider="gamma",status="success"} 1'
        in metrics
    )
    assert (
        'jev_polymarket_market_data_calls_total{provider="gamma",status="failure"} 1'
        in metrics
    )
    assert (
        'jev_polymarket_market_data_calls_total{provider="clob",status="timeout"} 1'
        in metrics
    )
    assert (
        'jev_polymarket_market_data_failures_total{provider="gamma",'
        'reason="malformed"} 1' in metrics
    )
    assert (
        'jev_polymarket_market_data_failures_total{provider="clob",reason="timeout"} 1'
        in metrics
    )
    assert (
        'jev_polymarket_market_data_call_duration_seconds_count{provider="gamma"} 2'
        in metrics
    )
    assert (
        'jev_polymarket_market_data_call_duration_seconds_sum{provider="gamma"} 0.3'
        in metrics
    )
    assert (
        'jev_polymarket_market_data_call_duration_seconds_count{provider="clob"} 1'
        in metrics
    )


def test_pipeline_metrics_count_invocations_and_use_only_latest_runtime(
    tmp_path: Path,
) -> None:
    _append_run(
        tmp_path,
        "1" * 64,
        status="failure",
        elapsed_nanoseconds=9_000_000_000,
        occurred_at=NOW,
    )
    _append_run(
        tmp_path,
        "2" * 64,
        status="success",
        elapsed_nanoseconds=2_500_000_000,
        occurred_at=NOW + timedelta(minutes=1),
    )
    metrics = render_paper_metrics(tmp_path).read_text()
    assert 'jev_polymarket_pipeline_runs_total{status="failure"} 1' in metrics
    assert 'jev_polymarket_pipeline_runs_total{status="success"} 1' in metrics
    assert (
        'jev_polymarket_process_last_run_runtime_seconds{status="success"} 2.5'
        in metrics
    )
    assert (
        'jev_polymarket_process_last_run_runtime_seconds{status="failure"}'
        not in metrics
    )


def test_pending_provider_intent_is_not_fabricated_as_terminal_call(
    tmp_path: Path,
) -> None:
    request = canonical_json_bytes({"model": "gpt-5-2025-08-07"})
    ProviderCallJournal(tmp_path).begin(
        provider="openai",
        opportunity_id=hashlib.sha256(b"pending").hexdigest(),
        request_bytes=request,
        created_at=NOW,
    )
    metrics = render_paper_metrics(tmp_path).read_text()
    assert (
        "jev_polymarket_llm_restart_recoveries_total{"
        'model_snapshot="gpt-5-2025-08-07",provider="openai",status="pending"} 1'
        in metrics
    )
    assert (
        "jev_polymarket_llm_unknown_cost_calls_total{"
        'model_snapshot="gpt-5-2025-08-07",provider="openai"} 1' in metrics
    )
    assert (
        'jev_polymarket_llm_calls_total{model_snapshot="gpt-5-2025-08-07",provider="openai"'
        not in metrics
    )


def test_render_inside_observation_lock_does_not_reacquire_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jev_trading_lab.polymarket import pipeline

    entered = 0
    real_lock = pipeline.observation_lock

    from collections.abc import Iterator
    from contextlib import contextmanager

    @contextmanager
    def counted_lock(output_dir: str | Path) -> Iterator[None]:
        nonlocal entered
        entered += 1
        with real_lock(output_dir):
            yield

    monkeypatch.setattr(pipeline, "observation_lock", counted_lock)
    fixture = Path(__file__).parents[1] / "fixtures/polymarket/feasibility_cycle.json"

    pipeline.replay_fixture(fixture, tmp_path)

    assert entered == 1
    assert (tmp_path / "polymarket.prom").is_file()

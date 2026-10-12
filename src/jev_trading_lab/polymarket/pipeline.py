"""Offline-only fixture replay orchestration for the Polymarket feasibility slice."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import stat
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.polymarket.ai import (
    JEV_ENDPOINT,
    JEV_MODEL_ID,
    OPENAI_ENDPOINT,
    OPENAI_MODEL_ID,
    AdapterCostOverflow,
    AdapterFailure,
    FailureCode,
    JevSuccess,
    OpenAISuccess,
    Transport,
    TransportResponse,
    build_jev_request,
    build_openai_request,
    call_jev,
    call_openai,
    live_jev_transport,
    live_openai_transport,
)
from jev_trading_lab.polymarket.call_journal import (
    ProviderCallJournal,
    adapter_from_outcome,
    adapter_outcome,
)
from jev_trading_lab.polymarket.client import (
    CLOB_ENDPOINT,
    CLOB_HOST,
    CLOB_PATH,
    GAMMA_ENDPOINT,
    MAX_PUBLIC_BYTES,
    MarketCandidate,
    MarketRejection,
    PolymarketClient,
    PrecheckAttempt,
    PublicDataError,
    SecureHttpTransport,
    gamma_clob_identity,
    reconstruct_book_snapshot,
)
from jev_trading_lab.polymarket.models import (
    ArmResult,
    CausalHistoryPoint,
    Economics,
    EvidencePacket,
    FeasibilityFixture,
    FixtureIdentity,
    JevDecision,
    ObservationResult,
    PriceFreeFixturePrefix,
    StrictModel,
    int64_delta,
    load_strict_json,
    validate_feasibility_fixture,
)
from jev_trading_lab.polymarket.operations import (
    OperationEvent,
    OperationKind,
    append_operation,
    operation_event_id,
    process_sample_exact_values,
    sample_process,
)
from jev_trading_lab.polymarket.paper import (
    POLICY_VERSION,
    STARTING_CASH_MICRODOLLARS,
    evaluate_ai_arm,
    evaluate_control_arm,
    evaluate_control_inputs,
    evaluate_fixture,
    midpoint_micros,
)
from jev_trading_lab.polymarket.store import (
    ArtifactEnvelope,
    ArtifactToFreeze,
    BookUnavailableArtifact,
    DecisionFrozenArtifact,
    EligibilityRejectionArtifact,
    PrecheckBookFailureArtifact,
    PrecheckHttpArtifact,
    RuntimeConfigArtifact,
    StoreSecurityError,
    SuccessArtifact,
    _screen_payload,
    cost_overflow_artifact_from_adapter,
    failure_artifact_from_adapter,
    freeze_observation_artifacts,
    load_completed_slot,
    load_existing_terminal,
    load_existing_terminal_for_inputs,
    load_frozen_artifacts_for_opportunity,
    load_observation,
    observation_lock,
    record_completed_slot,
    record_observation,
)

EventSink = Callable[[str], None]
Summary = dict[str, object]


def _operation_event(**values: object) -> OperationEvent:
    payload: dict[str, object] = {
        "schema_version": 1,
        "occurred_at": values.pop("occurred_at", datetime.now(UTC)),
        "kind": values.pop("kind"),
        "run_id": values.pop("run_id", None),
        "semantic_id": values.pop("semantic_id", None),
        "stage": values.pop("stage", None),
        "provider": values.pop("provider", None),
        "status": values.pop("status"),
        "reason": values.pop("reason", None),
        "elapsed_nanoseconds": values.pop("elapsed_nanoseconds", None),
        "exact_values": values.pop("exact_values", {}),
        "evidence": None,
    }
    assert not values
    payload["event_id"] = operation_event_id(payload)
    return OperationEvent.model_validate(payload)


def _run_id(kind: str, output_dir: str | os.PathLike[str]) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "kind": kind,
                "nonce": secrets.token_hex(16),
                "output": str(Path(output_dir).absolute()),
            }
        )
    ).hexdigest()


def _attempt_sink(
    output_dir: str | os.PathLike[str], run_id: str
) -> Callable[[str, str, str, str | None, int], None]:
    def record(
        provider: str, stage: str, status: str, reason: str | None, elapsed: int
    ) -> None:
        append_operation(
            Path(output_dir),
            _operation_event(
                kind=OperationKind.STAGE_TERMINAL,
                run_id=run_id,
                stage=stage,
                provider=provider,
                status=status,
                reason=reason,
                elapsed_nanoseconds=elapsed,
            ),
        )

    return record


def _finish_run(
    output_dir: str | os.PathLike[str], run_id: str, status: str, elapsed: int
) -> None:
    sample = sample_process()
    append_operation(
        Path(output_dir),
        _operation_event(
            kind=OperationKind.PROCESS_SAMPLE,
            run_id=run_id,
            status="success",
            exact_values=process_sample_exact_values(sample),
        ),
    )
    append_operation(
        Path(output_dir),
        _operation_event(
            kind=OperationKind.RUN_TERMINAL,
            run_id=run_id,
            status=status,
            reason="other" if status == "failure" else None,
            elapsed_nanoseconds=elapsed,
        ),
    )


@dataclass(slots=True)
class _ExecutionBookGate:
    """Make execution-book validation and economics impossible before the barrier."""

    raw_fixture: dict[str, Any]
    frozen: bool = False
    event_sink: EventSink | None = None

    def freeze(self) -> None:
        self.frozen = True
        if self.event_sink is not None:
            self.event_sink("decision_frozen")

    def get(self) -> FeasibilityFixture:
        if not self.frozen:
            raise RuntimeError(
                "execution book cannot be accessed before decision freeze"
            )
        self.raw_fixture["execution_book"]
        self.raw_fixture["precheck_book"]
        self.raw_fixture["economics"]
        self.raw_fixture["expected"]
        validate_feasibility_fixture(self.raw_fixture)
        if self.event_sink is not None:
            self.event_sink("execution_book_accessed")
        return FeasibilityFixture.model_validate_json(
            canonical_json_bytes(self.raw_fixture)
        )


def _load_fixture(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError("fixture cannot be opened") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("fixture must be a regular non-symlink file")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            current = os.fstat(descriptor)
            if not stat.S_ISREG(current.st_mode) or current.st_ino != metadata.st_ino:
                raise ValueError("fixture must remain a regular file")
            raw = os.read(descriptor, current.st_size + 1)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise ValueError("fixture cannot be opened safely") from error
    if len(raw) != metadata.st_size:
        raise ValueError("fixture changed while being read")
    value = load_strict_json(raw)
    if not isinstance(value, dict):
        raise ValueError("fixture must be a JSON object")
    return value


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _model_bytes(value: StrictModel) -> bytes:
    return canonical_json_bytes(value.model_dump(mode="json"))


def _stage_hash(
    identity: ObservationResult | FixtureIdentity, artifact: ArtifactToFreeze
) -> str:
    payload_hash = hashlib.sha256(artifact.payload).hexdigest()
    envelope = ArtifactEnvelope(
        kind=artifact.kind,
        sha256=payload_hash,
        byte_size=len(artifact.payload),
        storage_relpath=f"{payload_hash[:2]}/{payload_hash[2:4]}/{payload_hash}",
        source_uri_label=artifact.source_uri_label,
        retrieved_at=artifact.retrieved_at,
        opportunity_id=identity.opportunity_id,
        observation_id=identity.observation_id,
        predecessor_stage_sha256=artifact.predecessor_stage_sha256,
    )
    return hashlib.sha256(
        canonical_json_bytes(envelope.model_dump(mode="json"))
    ).hexdigest()


def _chain(
    identity: ObservationResult | FixtureIdentity,
    values: list[tuple[str, bytes, str, datetime]],
) -> list[ArtifactToFreeze]:
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
        predecessor = _stage_hash(identity, artifact)
    return artifacts


def _fixture_transport(
    expected_request: bytes,
    response: bytes,
    retrieved_at: datetime,
    cost: int,
) -> Transport:
    called = False

    def transport(request_bytes: bytes) -> TransportResponse:
        nonlocal called
        if called:
            raise RuntimeError("fixture transport may be called only once")
        called = True
        if request_bytes != expected_request:
            raise ValueError("generated provider request differs from fixture")
        return TransportResponse(
            status_code=200,
            headers={},
            raw_bytes=response,
            retrieved_at=retrieved_at,
            billed_cost_microdollars=cost,
        )

    return transport


def _publishing_transport(
    transport: Transport,
    publish_request: Callable[[bytes], None],
) -> Transport:
    def wrapped(request_bytes: bytes) -> TransportResponse:
        publish_request(request_bytes)
        return transport(request_bytes)

    return wrapped


def _success_metadata(
    provider: str,
    request: bytes,
    response: bytes,
    retrieved_at: datetime,
    cost: int,
) -> bytes:
    value = SuccessArtifact(
        provider=provider,  # type: ignore[arg-type]
        request_sha256=hashlib.sha256(request).hexdigest(),
        response_sha256=hashlib.sha256(response).hexdigest(),
        retrieved_at=retrieved_at,
        billed_cost_microdollars=cost,
    )
    return _model_bytes(value)


def _decision_barrier(
    identity: FixtureIdentity,
    prefix: list[tuple[str, bytes, str, datetime]],
    *,
    state: str,
    failure_code: FailureCode | None,
    frozen_at: datetime,
    openai_request: bytes,
    openai_response_sha256: str | None,
    jev_request: bytes | None,
    jev_response_sha256: str | None,
    jev_decision_sha256: str | None,
    openai_cost: int | None,
    jev_cost: int | None,
) -> tuple[list[tuple[str, bytes, str, datetime]], DecisionFrozenArtifact]:
    barrier = DecisionFrozenArtifact(
        opportunity_id=identity.opportunity_id,
        observation_id=identity.observation_id,
        terminal_model_state=state,  # type: ignore[arg-type]
        failure_code=failure_code,
        openai_request_sha256=hashlib.sha256(openai_request).hexdigest(),
        openai_response_sha256=openai_response_sha256,
        jev_request_sha256=(
            hashlib.sha256(jev_request).hexdigest() if jev_request is not None else None
        ),
        jev_response_sha256=jev_response_sha256,
        jev_decision_sha256=jev_decision_sha256,
        openai_cost_microdollars=openai_cost,
        jev_cost_microdollars=jev_cost,
        frozen_at=frozen_at,
    )
    values = prefix + [
        ("decision_frozen", _model_bytes(barrier), "decision_frozen", frozen_at)
    ]
    return values, barrier


def _failure_result(
    fixture: FeasibilityFixture,
    failure: AdapterFailure,
    prior_cost: int,
) -> ObservationResult:
    base = evaluate_fixture(fixture)
    cost = (
        None
        if failure.billed_cost_microdollars is None
        else _sum_model_cost(prior_cost, failure.billed_cost_microdollars)
    )
    if cost is None:
        ai = _unavailable_cost_arm(
            "UNKNOWN_IN_FLIGHT"
            if failure.code in {"TIMEOUT", "PROVIDER_ERROR", "UNKNOWN_IN_FLIGHT"}
            else "MODEL_FAILURE"
        )
    else:
        ai = evaluate_ai_arm(
            fixture.execution_book,
            None,
            fixture.economics,
            resolution=fixture.resolution.payout if fixture.resolution else None,
            model_cost_microdollars=cost,
        )
    control = evaluate_control_arm(
        fixture, resolution=fixture.resolution.payout if fixture.resolution else None
    )
    return base.model_copy(
        update={
            "research_brief_sha256": (
                base.research_brief_sha256 if failure.provider == "jev" else "0" * 64
            ),
            "jev_decision_sha256": "0" * 64,
            "ai": ai,
            "deterministic": control,
            "paired_trading_pnl_delta_microdollars": int64_delta(
                ai.trading_pnl_microdollars, control.trading_pnl_microdollars
            ),
            "paired_economic_pnl_delta_microdollars": (
                int64_delta(
                    ai.economic_pnl_microdollars, control.economic_pnl_microdollars
                )
                if ai.economic_pnl_microdollars is not None
                and control.economic_pnl_microdollars is not None
                else None
            ),
            "paired_brier_delta_micros": None,
        }
    )


def _book_failure_arm(
    *,
    arm: str,
    model_cost: int,
    decision: JevDecision | None = None,
) -> ArmResult:
    probability = getattr(decision, "p_yes_micros", None)
    action = getattr(decision, "action", None)
    return ArmResult(
        arm=arm,  # type: ignore[arg-type]
        signal_probability_micros=probability,
        signal_action=action,
        disposition="BOOK_FAILURE",
        entry_side=None,
        entry_vwap_micros=None,
        entry_notional_microdollars=None,
        quantity=0,
        fees_microdollars=0,
        initial_cash_microdollars=STARTING_CASH_MICRODOLLARS,
        final_cash_microdollars=STARTING_CASH_MICRODOLLARS,
        mark_micros=None,
        payout_micros=None,
        valuation_notional_microdollars=None,
        trading_pnl_microdollars=0,
        economic_pnl_microdollars=-model_cost,
        model_cost_microdollars=model_cost,
        forecast_probability_micros=probability,
        forecast_status=(
            "PENDING_RESOLUTION" if probability is not None else "UNAVAILABLE"
        ),
        brier_micros=None,
    )


def _sum_model_cost(left: int, right: int) -> int | None:
    total = left + right
    return total if total <= 2**63 - 1 else None


def _unavailable_cost_arm(
    disposition: Literal[
        "MODEL_FAILURE", "COST_OVERFLOW", "UNKNOWN_IN_FLIGHT"
    ] = "COST_OVERFLOW",
) -> ArmResult:
    return ArmResult(
        arm="AI",
        signal_probability_micros=None,
        signal_action=None,
        disposition=disposition,
        entry_side=None,
        entry_vwap_micros=None,
        entry_notional_microdollars=None,
        quantity=0,
        fees_microdollars=0,
        initial_cash_microdollars=STARTING_CASH_MICRODOLLARS,
        final_cash_microdollars=STARTING_CASH_MICRODOLLARS,
        mark_micros=None,
        payout_micros=None,
        valuation_notional_microdollars=None,
        trading_pnl_microdollars=0,
        economic_pnl_microdollars=None,
        model_cost_microdollars=None,
        forecast_probability_micros=None,
        forecast_status="UNAVAILABLE",
        brier_micros=None,
    )


def _book_unavailable_result(
    identity: FixtureIdentity,
    evidence_at: datetime,
    *,
    terminal_at: datetime,
    research_brief_sha256: str,
    decision: JevDecision | None,
    decision_sha256: str,
    model_cost: int | None,
    reason: str,
) -> ObservationResult:
    ai = (
        _unavailable_cost_arm(
            "UNKNOWN_IN_FLIGHT"
            if reason == "UNKNOWN_IN_FLIGHT"
            else "MODEL_FAILURE"
            if reason == "MODEL_FAILURE"
            else "COST_OVERFLOW"
        )
        if model_cost is None
        else _book_failure_arm(arm="AI", model_cost=model_cost, decision=decision)
    )
    control = _book_failure_arm(arm="DETERMINISTIC", model_cost=0)
    return ObservationResult(
        observation_id=identity.observation_id,
        opportunity_id=identity.opportunity_id,
        market_id=identity.market_id,
        policy_version=identity.policy_version,
        slot_at=identity.slot_at,
        cutoff_at=identity.cutoff_at,
        observed_at=max(terminal_at, evidence_at),
        evidence_sha256=identity.evidence_sha256,
        execution_book_sha256=None,
        planned_execution_book_sha256=identity.execution_book_sha256,
        research_brief_sha256=research_brief_sha256,
        jev_decision_sha256=decision_sha256,
        ai=ai,
        deterministic=control,
        baseline_probability_micros=None,
        resolution=None,
        baseline_brier_micros=None,
        paired_trading_pnl_delta_microdollars=0,
        paired_economic_pnl_delta_microdollars=(
            -model_cost if model_cost is not None else None
        ),
        paired_brier_delta_micros=None,
    )


def _finish_book_unavailable(
    output_dir: str | os.PathLike[str],
    identity: FixtureIdentity,
    values: list[tuple[str, bytes, str, datetime]],
    *,
    planned_at: datetime,
    terminal_at: datetime,
    observed_at: datetime,
    reason: str,
    evidence_at: datetime,
    research_brief_sha256: str,
    decision: JevDecision | None,
    decision_sha256: str,
    model_cost: int | None,
) -> Summary:
    unavailable = BookUnavailableArtifact(
        reason=reason,  # type: ignore[arg-type]
        planned_retrieved_at=planned_at,
        planned_source_sha256=identity.execution_book_sha256,
        terminal_model_at=terminal_at,
    )
    late_values = values + [
        (
            "book_unavailable",
            _model_bytes(unavailable),
            "book_unavailable",
            terminal_at,
        )
    ]
    artifacts = _chain(identity, late_values)
    freeze_observation_artifacts(output_dir, identity, artifacts)
    result = _book_unavailable_result(
        identity,
        evidence_at,
        terminal_at=max(observed_at, terminal_at),
        research_brief_sha256=research_brief_sha256,
        decision=decision,
        decision_sha256=decision_sha256,
        model_cost=model_cost,
        reason=reason,
    )
    record_observation(output_dir, result, artifacts)
    replay = load_observation(output_dir, result.observation_id)
    return _summary(replay.result, replay.envelopes)


def _arm_summary(arm: ArmResult) -> dict[str, object]:
    return {
        "action": arm.signal_action,
        "brier_micros": arm.brier_micros,
        "disposition": arm.disposition,
        "economic_pnl_microdollars": arm.economic_pnl_microdollars,
        "forecast_probability_micros": arm.forecast_probability_micros,
        "forecast_status": arm.forecast_status,
        "model_cost_microdollars": arm.model_cost_microdollars,
        "quantity": arm.quantity,
        "trading_pnl_microdollars": arm.trading_pnl_microdollars,
    }


def _summary(
    result: ObservationResult, envelopes: dict[str, ArtifactEnvelope]
) -> Summary:
    return {
        "ai": _arm_summary(result.ai),
        "artifact_count": len(envelopes),
        "artifact_hashes": {
            kind: envelope.sha256 for kind, envelope in sorted(envelopes.items())
        },
        "baseline_brier_micros": result.baseline_brier_micros,
        "baseline_probability_micros": result.baseline_probability_micros,
        "control": _arm_summary(result.deterministic),
        "observation_id": result.observation_id,
        "opportunity_id": result.opportunity_id,
        "paired_brier_delta_micros": result.paired_brier_delta_micros,
        "paired_economic_pnl_delta_microdollars": (
            result.paired_economic_pnl_delta_microdollars
        ),
        "paired_trading_pnl_delta_microdollars": (
            result.paired_trading_pnl_delta_microdollars
        ),
    }


def _identity(candidate: MarketCandidate, execution_hash: str) -> FixtureIdentity:
    slot = candidate.slot_at.isoformat().replace("+00:00", "Z")
    opportunity_id = hashlib.sha256(
        canonical_json_bytes(
            {
                "policy_version": POLICY_VERSION,
                "market_id": candidate.market_id,
                "slot_at": slot,
            }
        )
    ).hexdigest()
    observation_id = hashlib.sha256(
        canonical_json_bytes(
            {
                "opportunity_id": opportunity_id,
                "evidence_sha256": candidate.evidence.source_sha256,
                "execution_book_sha256": execution_hash,
                "policy_version": POLICY_VERSION,
            }
        )
    ).hexdigest()
    return FixtureIdentity(
        policy_version=POLICY_VERSION,
        market_id=candidate.market_id,
        slot_at=candidate.slot_at,
        cutoff_at=candidate.evidence.cutoff_at,
        opportunity_id=opportunity_id,
        observation_id=observation_id,
        evidence_sha256=candidate.evidence.source_sha256,
        execution_book_sha256=execution_hash,
    )


def observation_slot(value: datetime) -> datetime:
    """Return the deterministic UTC minute containing ``value``."""

    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("observation time must be UTC-aware")
    return value.astimezone(UTC).replace(second=0, microsecond=0)


def _unknown_provider_result(provider: str, request: bytes) -> AdapterFailure:
    return AdapterFailure(
        provider=provider,  # type: ignore[arg-type]
        code="UNKNOWN_IN_FLIGHT",
        message=f"{provider} call outcome is unknown after restart",
        request_bytes=request,
        response_sha256=None,
        response_byte_length=0,
        retrieved_at=None,
        billed_cost_microdollars=None,
        status_code=None,
    )


def _record_provider_recovery(
    *,
    root: str | os.PathLike[str],
    provider: str,
    call_id: str,
    occurred_at: datetime,
    status: str,
) -> None:
    append_operation(
        Path(root),
        _operation_event(
            occurred_at=occurred_at,
            kind=OperationKind.RECOVERY,
            semantic_id=call_id,
            stage=provider,
            provider=provider,
            status="success",
            reason=status,
        ),
    )


def _journaled_provider_call(
    *,
    journal: ProviderCallJournal,
    provider: str,
    opportunity_id: str,
    request_bytes: bytes,
    created_at: datetime,
    invoke: Callable[[], object],
    event_sink: EventSink | None,
    unknown_on_transport_failure: bool = True,
) -> object:
    existing = journal.lookup(opportunity_id, provider)  # type: ignore[arg-type]
    if existing is not None:
        if existing["kind"] == "provider_call_result":
            _record_provider_recovery(
                root=journal.root,
                provider=provider,
                call_id=existing["call_id"],
                occurred_at=created_at,
                status="reused_result",
            )
            return adapter_from_outcome(provider, existing["outcome"])  # type: ignore[arg-type]
        frozen_request = base64.b64decode(existing["request_b64"], validate=True)
        unknown = _unknown_provider_result(provider, frozen_request)
        journal.finish(
            existing["call_id"],
            outcome=adapter_outcome(unknown),
            finished_at=created_at,
        )
        _record_provider_recovery(
            root=journal.root,
            provider=provider,
            call_id=existing["call_id"],
            occurred_at=created_at,
            status="unknown_in_flight",
        )
        return unknown
    call_id = journal.begin(
        provider=provider,  # type: ignore[arg-type]
        opportunity_id=opportunity_id,
        request_bytes=request_bytes,
        created_at=created_at,
    )
    if event_sink is not None:
        event_sink(f"{provider}_intent_frozen")
    started_ns = time.monotonic_ns()
    result = invoke()
    elapsed_nanoseconds = time.monotonic_ns() - started_ns
    if (
        unknown_on_transport_failure
        and isinstance(result, AdapterFailure)
        and result.code
        in {
            "TIMEOUT",
            "PROVIDER_ERROR",
        }
    ):
        result = _unknown_provider_result(provider, result.request_bytes)
    journal.finish(
        call_id,
        outcome=adapter_outcome(result),
        finished_at=getattr(result, "retrieved_at", None) or datetime.now(UTC),
        elapsed_nanoseconds=elapsed_nanoseconds,
    )
    if event_sink is not None:
        event_sink(f"{provider}_result_frozen")
    return result


def _live_policy() -> Economics:
    return Economics(
        entry_fee_pp_micros=10_000,
        exit_fee_pp_micros=0,
        exit_slippage_pp_micros=10_000,
        sigma_p_micros=100_000,
        starting_cash_microdollars=STARTING_CASH_MICRODOLLARS,
    )


def _live_causal_history(candidate: MarketCandidate) -> list[CausalHistoryPoint]:
    return [
        CausalHistoryPoint(
            midpoint_micros=midpoint_micros(book),
            observed_at=book.response_retrieved_at or book.retrieved_at,
        )
        for book in candidate.precheck_history
    ]


def _causal_history_bytes(candidate: MarketCandidate) -> bytes:
    history = _live_causal_history(candidate)
    return canonical_json_bytes(
        {
            "points": [point.model_dump(mode="json") for point in history],
            "raw_books_b64": [
                base64.b64encode(raw).decode()
                for raw in candidate.precheck_history_bytes
            ],
            "response_retrieved_at": [
                (book.response_retrieved_at or book.retrieved_at)
                .isoformat()
                .replace("+00:00", "Z")
                for book in candidate.precheck_history
            ],
        }
    )


def _live_result(
    identity: FixtureIdentity,
    candidate: MarketCandidate,
    execution_book: Any,
    *,
    decision: JevDecision | None,
    research_brief_sha256: str,
    decision_sha256: str,
    model_cost: int | None,
) -> ObservationResult:
    history = _live_causal_history(candidate)
    policy = _live_policy()
    ai = (
        _unavailable_cost_arm()
        if model_cost is None
        else evaluate_ai_arm(
            execution_book,
            decision,
            policy,
            resolution=None,
            model_cost_microdollars=model_cost,
        )
    )
    control = evaluate_control_inputs(
        history,
        cutoff_at=candidate.evidence.cutoff_at,
        book=execution_book,
        policy=policy,
        resolution=None,
    )
    return ObservationResult(
        observation_id=identity.observation_id,
        opportunity_id=identity.opportunity_id,
        market_id=identity.market_id,
        policy_version=identity.policy_version,
        slot_at=identity.slot_at,
        cutoff_at=identity.cutoff_at,
        observed_at=max(datetime.now(UTC), execution_book.retrieved_at),
        evidence_sha256=identity.evidence_sha256,
        execution_book_sha256=execution_book.source_sha256,
        research_brief_sha256=research_brief_sha256,
        jev_decision_sha256=decision_sha256,
        ai=ai,
        deterministic=control,
        baseline_probability_micros=midpoint_micros(execution_book),
        resolution=None,
        baseline_brier_micros=None,
        paired_trading_pnl_delta_microdollars=int64_delta(
            ai.trading_pnl_microdollars, control.trading_pnl_microdollars
        ),
        paired_economic_pnl_delta_microdollars=(
            int64_delta(ai.economic_pnl_microdollars, control.economic_pnl_microdollars)
            if ai.economic_pnl_microdollars is not None
            and control.economic_pnl_microdollars is not None
            else None
        ),
        paired_brier_delta_micros=None,
    )


def _runtime_config_bytes() -> bytes:
    """Build the canonical non-secret runtime configuration; performs no I/O.

    The bytes become the first, content-addressed stage of every live observation
    chain, so they are validated here before any lock, directory, or file exists.
    """

    rate_names = (
        "OPENAI_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
        "OPENAI_CACHED_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
        "OPENAI_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
        "TYPESAFE_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
        "TYPESAFE_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
    )
    rates: dict[str, int | None] = {}
    for name in rate_names:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            rates[name] = None
            continue
        if not raw.isascii() or not raw.isdecimal():
            raise ValueError("runtime configuration is invalid")
        rate = int(raw)
        if not 0 <= rate <= 1_000_000_000_000:
            raise ValueError("runtime configuration is invalid")
        rates[name] = rate
    value = {
        "endpoints": {
            "clob": CLOB_ENDPOINT,
            "gamma": GAMMA_ENDPOINT,
            "jev": JEV_ENDPOINT,
            "openai": OPENAI_ENDPOINT,
        },
        "models": {"jev": JEV_MODEL_ID, "openai": OPENAI_MODEL_ID},
        "policy_version": POLICY_VERSION,
        "rates_microdollars_per_million_tokens": rates,
    }
    payload = canonical_json_bytes(value)
    try:
        _screen_payload(payload, (), strict_json=True)
        RuntimeConfigArtifact.model_validate_json(payload)
    except (StoreSecurityError, ValueError):
        raise ValueError("runtime configuration is invalid") from None
    return payload


def _precheck_http_artifact(attempt: PrecheckAttempt) -> PrecheckHttpArtifact:
    return PrecheckHttpArtifact(
        request_host=CLOB_HOST,
        request_path=CLOB_PATH,
        request_token_id=attempt.token_id,
        attempted_at=attempt.attempted_at,
        retrieved_at=attempt.retrieved_at,
        status_code=attempt.status_code,
        content_type=attempt.content_type,
        response_sha256=hashlib.sha256(attempt.body).hexdigest(),
        response_byte_length=len(attempt.body),
        response_truncated=attempt.truncated,
        transport_reason=attempt.transport_reason,
    )


def _http_stage(attempt: PrecheckAttempt) -> tuple[str, bytes, str, datetime]:
    return (
        "precheck_book_http",
        _model_bytes(_precheck_http_artifact(attempt)),
        "clob_precheck_http",
        attempt.retrieved_at or attempt.attempted_at,
    )


def _precheck_values(
    candidate: MarketCandidate, config: bytes
) -> list[tuple[str, bytes, str, datetime]]:
    """The frozen, price-blind prefix shared by every model-bearing observation."""

    return [
        ("runtime_config", config, "runtime_config", candidate.evidence.cutoff_at),
        (
            "raw_gamma",
            candidate.raw_gamma_bytes,
            "gamma_market",
            candidate.evidence.retrieved_at,
        ),
        (
            "sanitized_evidence",
            _model_bytes(candidate.evidence),
            "gamma_sanitized",
            candidate.evidence.retrieved_at,
        ),
        (
            "precheck_book_raw",
            candidate.precheck_book_bytes,
            "clob_precheck_raw",
            candidate.precheck_book.response_retrieved_at
            or candidate.precheck_book.retrieved_at,
        ),
        _http_stage(candidate.precheck_attempt),
        (
            "precheck_book",
            candidate.precheck_book_bytes,
            "clob_precheck_book",
            candidate.precheck_book.response_retrieved_at
            or candidate.precheck_book.retrieved_at,
        ),
        (
            "causal_history",
            _causal_history_bytes(candidate),
            "precheck_causal_history",
            candidate.precheck_book.retrieved_at,
        ),
    ]


def _live_artifacts(
    identity: FixtureIdentity,
    candidate: MarketCandidate,
    openai: OpenAISuccess | AdapterFailure | AdapterCostOverflow,
    jev: JevSuccess | AdapterFailure | AdapterCostOverflow | None,
    *,
    config: bytes,
    frozen_at: datetime,
) -> tuple[
    list[tuple[str, bytes, str, datetime]],
    JevDecision | None,
    str,
    str,
    int | None,
]:
    prefix = _precheck_values(candidate, config)
    if isinstance(openai, AdapterCostOverflow):
        prefix.extend(
            [
                (
                    "openai_request",
                    openai.request_bytes,
                    "openai_request",
                    candidate.evidence.retrieved_at,
                ),
                (
                    "openai_cost_overflow",
                    _model_bytes(cost_overflow_artifact_from_adapter(openai)),
                    "openai_cost_overflow",
                    openai.retrieved_at,
                ),
            ]
        )
        values, _ = _decision_barrier(
            identity,
            prefix,
            state="COST_OVERFLOW",
            failure_code=None,
            frozen_at=frozen_at,
            openai_request=openai.request_bytes,
            openai_response_sha256=openai.response_sha256,
            jev_request=None,
            jev_response_sha256=None,
            jev_decision_sha256=None,
            openai_cost=None,
            jev_cost=0,
        )
        return values, None, "0" * 64, "0" * 64, None
    if isinstance(openai, AdapterFailure):
        if openai.request_bytes:
            prefix.append(
                (
                    "openai_request",
                    openai.request_bytes,
                    "openai_request",
                    candidate.evidence.retrieved_at,
                )
            )
        prefix.append(
            (
                "openai_failure",
                _model_bytes(failure_artifact_from_adapter(openai)),
                "openai_failure",
                frozen_at,
            )
        )
        values, _ = _decision_barrier(
            identity,
            prefix,
            state="OPENAI_FAILURE",
            failure_code=openai.code,
            frozen_at=frozen_at,
            openai_request=openai.request_bytes,
            openai_response_sha256=openai.response_sha256,
            jev_request=None,
            jev_response_sha256=None,
            jev_decision_sha256=None,
            openai_cost=openai.billed_cost_microdollars,
            jev_cost=0,
        )
        return values, None, "0" * 64, "0" * 64, openai.billed_cost_microdollars
    brief_bytes = _model_bytes(openai.brief)
    brief_hash = hashlib.sha256(brief_bytes).hexdigest()
    prefix.extend(
        [
            (
                "openai_request",
                openai.request_bytes,
                "openai_request",
                candidate.evidence.retrieved_at,
            ),
            (
                "openai_response_success",
                openai.response_bytes,
                "openai_response",
                openai.retrieved_at,
            ),
            (
                "openai_success_metadata",
                _success_metadata(
                    "openai",
                    openai.request_bytes,
                    openai.response_bytes,
                    openai.retrieved_at,
                    openai.billed_cost_microdollars,
                ),
                "openai_success_metadata",
                openai.retrieved_at,
            ),
            ("research_brief", brief_bytes, "openai_validated", openai.retrieved_at),
        ]
    )
    assert jev is not None
    if isinstance(jev, AdapterCostOverflow):
        prefix.extend(
            [
                (
                    "jev_request",
                    jev.request_bytes,
                    "jev_request",
                    openai.retrieved_at,
                ),
                (
                    "jev_cost_overflow",
                    _model_bytes(cost_overflow_artifact_from_adapter(jev)),
                    "jev_cost_overflow",
                    jev.retrieved_at,
                ),
            ]
        )
        values, _ = _decision_barrier(
            identity,
            prefix,
            state="COST_OVERFLOW",
            failure_code=None,
            frozen_at=frozen_at,
            openai_request=openai.request_bytes,
            openai_response_sha256=hashlib.sha256(openai.response_bytes).hexdigest(),
            jev_request=jev.request_bytes,
            jev_response_sha256=jev.response_sha256,
            jev_decision_sha256=None,
            openai_cost=openai.billed_cost_microdollars,
            jev_cost=None,
        )
        return values, None, brief_hash, "0" * 64, None
    if isinstance(jev, AdapterFailure):
        if jev.request_bytes:
            prefix.append(
                ("jev_request", jev.request_bytes, "jev_request", openai.retrieved_at)
            )
        prefix.append(
            (
                "jev_failure",
                _model_bytes(failure_artifact_from_adapter(jev)),
                "jev_failure",
                frozen_at,
            )
        )
        aggregate_cost = (
            None
            if jev.billed_cost_microdollars is None
            else _sum_model_cost(
                openai.billed_cost_microdollars, jev.billed_cost_microdollars
            )
        )
        values, _ = _decision_barrier(
            identity,
            prefix,
            state=(
                "COST_OVERFLOW"
                if aggregate_cost is None and jev.billed_cost_microdollars is not None
                else "JEV_FAILURE"
            ),
            failure_code=jev.code,
            frozen_at=frozen_at,
            openai_request=openai.request_bytes,
            openai_response_sha256=hashlib.sha256(openai.response_bytes).hexdigest(),
            jev_request=jev.request_bytes,
            jev_response_sha256=jev.response_sha256,
            jev_decision_sha256=None,
            openai_cost=openai.billed_cost_microdollars,
            jev_cost=jev.billed_cost_microdollars,
        )
        return values, None, brief_hash, "0" * 64, aggregate_cost
    decision_bytes = _model_bytes(jev.decision)
    decision_hash = hashlib.sha256(decision_bytes).hexdigest()
    prefix.extend(
        [
            ("jev_request", jev.request_bytes, "jev_request", openai.retrieved_at),
            (
                "jev_response_success",
                jev.response_bytes,
                "jev_response",
                jev.retrieved_at,
            ),
            (
                "jev_success_metadata",
                _success_metadata(
                    "jev",
                    jev.request_bytes,
                    jev.response_bytes,
                    jev.retrieved_at,
                    jev.billed_cost_microdollars,
                ),
                "jev_success_metadata",
                jev.retrieved_at,
            ),
            ("jev_decision", decision_bytes, "jev_validated", jev.retrieved_at),
        ]
    )
    aggregate_cost = _sum_model_cost(
        openai.billed_cost_microdollars, jev.billed_cost_microdollars
    )
    values, _ = _decision_barrier(
        identity,
        prefix,
        state="JEV_SUCCESS" if aggregate_cost is not None else "COST_OVERFLOW",
        failure_code=None,
        frozen_at=frozen_at,
        openai_request=openai.request_bytes,
        openai_response_sha256=hashlib.sha256(openai.response_bytes).hexdigest(),
        jev_request=jev.request_bytes,
        jev_response_sha256=hashlib.sha256(jev.response_bytes).hexdigest(),
        jev_decision_sha256=decision_hash,
        openai_cost=openai.billed_cost_microdollars,
        jev_cost=jev.billed_cost_microdollars,
    )
    return values, jev.decision, brief_hash, decision_hash, aggregate_cost


def _rejection_terminal_status(reason: str) -> str:
    return (
        "PUBLIC_DATA_FAILURE"
        if reason in {"PUBLIC_DATA_TIMEOUT", "MALFORMED_PUBLIC_DATA"}
        else "ELIGIBILITY_REJECTION"
    )


def _public_poll_failure(
    output_dir: str | os.PathLike[str],
    error: PublicDataError,
    *,
    observed_at: datetime,
    config: bytes,
) -> list[Summary]:
    reason = (
        "PUBLIC_DATA_TIMEOUT"
        if error.reason == "PUBLIC_DATA_TIMEOUT"
        else "MALFORMED_PUBLIC_DATA"
    )
    raw = error.raw_bytes[:MAX_PUBLIC_BYTES]
    payload = canonical_json_bytes(
        {
            "public_poll_failure": {
                "raw_response_b64": base64.b64encode(raw).decode(),
                "reason": reason,
            }
        }
    )
    return _record_rejections(
        output_dir,
        (
            MarketRejection(
                market_id="PUBLIC_DATA",
                reason=reason,
                raw_gamma_bytes=payload,
                raw_gamma_sha256=hashlib.sha256(payload).hexdigest(),
            ),
        ),
        observed_at=observed_at,
        config=config,
    )


def _record_rejections(
    output_dir: str | os.PathLike[str],
    rejections: tuple[Any, ...],
    *,
    observed_at: datetime,
    config: bytes,
) -> list[Summary]:
    summaries: list[Summary] = []
    for rejection in rejections:
        assert isinstance(rejection, MarketRejection)
        cutoff_text = observed_at.isoformat().replace("+00:00", "Z")
        opportunity_id = hashlib.sha256(
            canonical_json_bytes(
                {
                    "policy_version": POLICY_VERSION,
                    "market_id": rejection.market_id,
                    "slot_at": cutoff_text,
                }
            )
        ).hexdigest()
        observation_id = hashlib.sha256(
            canonical_json_bytes(
                {
                    "opportunity_id": opportunity_id,
                    "evidence_sha256": rejection.raw_gamma_sha256,
                    "execution_book_sha256": None,
                    "policy_version": POLICY_VERSION,
                }
            )
        ).hexdigest()

        def arm(name: str) -> ArmResult:
            return ArmResult(
                arm=name,  # type: ignore[arg-type]
                signal_probability_micros=None,
                signal_action=None,
                disposition="INELIGIBLE",
                entry_side=None,
                entry_vwap_micros=None,
                entry_notional_microdollars=None,
                quantity=0,
                fees_microdollars=0,
                initial_cash_microdollars=STARTING_CASH_MICRODOLLARS,
                final_cash_microdollars=STARTING_CASH_MICRODOLLARS,
                mark_micros=None,
                payout_micros=None,
                valuation_notional_microdollars=None,
                trading_pnl_microdollars=0,
                economic_pnl_microdollars=0,
                model_cost_microdollars=0,
                forecast_probability_micros=None,
                forecast_status="UNAVAILABLE",
                brier_micros=None,
            )

        result = ObservationResult(
            observation_id=observation_id,
            opportunity_id=opportunity_id,
            market_id=rejection.market_id,
            policy_version=POLICY_VERSION,
            slot_at=observed_at,
            cutoff_at=observed_at,
            observed_at=observed_at,
            evidence_sha256=rejection.raw_gamma_sha256,
            execution_book_sha256=None,
            research_brief_sha256="0" * 64,
            jev_decision_sha256="0" * 64,
            ai=arm("AI"),
            deterministic=arm("DETERMINISTIC"),
            baseline_probability_micros=None,
            resolution=None,
            baseline_brier_micros=None,
            paired_trading_pnl_delta_microdollars=0,
            paired_economic_pnl_delta_microdollars=0,
            paired_brier_delta_micros=None,
        )
        rejection_artifact = EligibilityRejectionArtifact(
            market_id=rejection.market_id,
            reason=rejection.reason,
            raw_gamma_sha256=rejection.raw_gamma_sha256,
            precheck_book_sha256=(
                rejection.precheck_book.source_sha256
                if rejection.precheck_book is not None
                else rejection.precheck_failure_sha256
            ),
            rejected_at=observed_at,
        )
        rejection_values = [
            ("runtime_config", config, "runtime_config", observed_at),
            ("raw_gamma", rejection.raw_gamma_bytes, "gamma_market", observed_at),
        ]
        attempt = rejection.precheck_attempt
        if rejection.evidence is not None and attempt is not None:
            http_stage = _http_stage(attempt)
            rejection_values.append(
                (
                    "sanitized_evidence",
                    _model_bytes(rejection.evidence),
                    "gamma_sanitized",
                    rejection.evidence.retrieved_at,
                )
            )
            rejection_values.append(
                (
                    "precheck_book_raw",
                    attempt.body,
                    "clob_precheck_raw",
                    rejection.precheck_retrieved_at or observed_at,
                )
            )
            rejection_values.append(http_stage)
            if rejection.precheck_book is not None:
                rejection_values.append(
                    (
                        "precheck_book",
                        attempt.body,
                        "clob_precheck_book",
                        rejection.precheck_book.response_retrieved_at
                        or rejection.precheck_book.retrieved_at,
                    )
                )
            else:
                failure = PrecheckBookFailureArtifact(
                    market_id=rejection.market_id,
                    token_id=rejection.yes_token_id or "UNKNOWN",
                    request_market_id=(
                        rejection.precheck_request_market_id or rejection.market_id
                    ),
                    request_condition_id=rejection.precheck_request_condition_id,
                    request_token_id=(
                        rejection.precheck_request_token_id
                        or rejection.yes_token_id
                        or "UNKNOWN"
                    ),
                    reason=rejection.reason,
                    raw_book_sha256=hashlib.sha256(attempt.body).hexdigest(),
                    raw_book_byte_length=len(attempt.body),
                    transport_failure=rejection.precheck_transport_failure,
                    retrieved_at=rejection.precheck_retrieved_at or observed_at,
                    http_sha256=hashlib.sha256(http_stage[1]).hexdigest(),
                )
                rejection_values.append(
                    (
                        "precheck_book_failure",
                        _model_bytes(failure),
                        "clob_precheck_failure",
                        failure.retrieved_at,
                    )
                )
        rejection_values.append(
            (
                "eligibility_rejection",
                _model_bytes(rejection_artifact),
                "eligibility_rejection",
                observed_at,
            )
        )
        artifacts = _chain(result, rejection_values)
        record_observation(output_dir, result, artifacts)
        replay = load_observation(output_dir, observation_id)
        summary = _summary(replay.result, replay.envelopes)
        summary.update(
            reason=rejection.reason,
            terminal_status=_rejection_terminal_status(rejection.reason),
        )
        summaries.append(summary)
    return summaries


def _replay_summary(replay: Any) -> Summary:
    summary = _summary(replay.result, replay.envelopes)
    rejection = replay.artifacts.get("eligibility_rejection")
    if rejection is not None:
        value = load_strict_json(rejection)
        if not isinstance(value, dict) or type(value.get("reason")) is not str:
            raise ValueError("eligibility rejection replay is invalid")
        summary.update(
            reason=value["reason"],
            terminal_status=_rejection_terminal_status(value["reason"]),
        )
    return summary


def _observe_markets(
    *,
    limit: int,
    output_dir: str | os.PathLike[str],
    client: PolymarketClient | None = None,
    openai_transport: Transport | None = None,
    jev_transport: Transport | None = None,
    now: datetime | None = None,
    event_sink: EventSink | None = None,
    _lock_held: bool = False,
) -> list[Summary]:
    """Run one bounded, paper-only public polling pass.

    The configuration is validated before any write. One exclusive lock inside the
    validated private output root then spans polling, provider calls, and every
    terminal record, so concurrent identical passes bill each provider only once.
    "Identical" follows the observation identity: the same market, cutoff, policy, and
    frozen evidence. Every live pass samples a fresh cutoff, so only passes sharing an
    injected ``now`` (or duplicate markets inside one pass) deduplicate; distinct live
    passes are distinct opportunities that the lock merely serializes.
    """

    if not 1 <= limit <= 20:
        raise ValueError("limit must be in 1..20")
    config = _runtime_config_bytes()
    with nullcontext() if _lock_held else observation_lock(output_dir):
        slot = observation_slot(now or datetime.now(UTC))
        completed = load_completed_slot(
            output_dir, cutoff=slot, policy_version=POLICY_VERSION, limit=limit
        )
        if completed is not None:
            completed_opportunities = {item.result.opportunity_id for item in completed}
            journal = ProviderCallJournal(output_dir)
            pending = [
                opportunity_id
                for opportunity_id in journal.opportunity_ids_created_at(slot)
                if opportunity_id not in completed_opportunities
            ]
            recovered: list[MarketCandidate] = []
            for opportunity_id in pending:
                candidate = _frozen_candidate_for_opportunity(
                    opportunity_id, output_dir=output_dir, slot=slot
                )
                if candidate is None:
                    raise ValueError(
                        "durable provider intent lacks frozen observation input"
                    )
                recovered.append(candidate)
            if not recovered:
                return [_replay_summary(item) for item in completed]
            summaries = [_replay_summary(item) for item in completed]
            summaries.extend(
                _observe_locked(
                    limit=limit,
                    output_dir=output_dir,
                    config=config,
                    client=client,
                    openai_transport=openai_transport,
                    jev_transport=jev_transport,
                    now=slot,
                    event_sink=event_sink,
                    recovered_only=tuple(recovered),
                )
            )
            observation_ids = list(
                dict.fromkeys(str(item["observation_id"]) for item in summaries)
            )
            record_completed_slot(
                output_dir,
                cutoff=slot,
                policy_version=POLICY_VERSION,
                limit=limit,
                observation_ids=observation_ids,
            )
            return summaries
        summaries = _observe_locked(
            limit=limit,
            output_dir=output_dir,
            config=config,
            client=client,
            openai_transport=openai_transport,
            jev_transport=jev_transport,
            now=slot,
            event_sink=event_sink,
        )
        record_completed_slot(
            output_dir,
            cutoff=slot,
            policy_version=POLICY_VERSION,
            limit=limit,
            observation_ids=[str(item["observation_id"]) for item in summaries],
        )
        return summaries


def _frozen_candidate_for_opportunity(
    opportunity_id: str,
    *,
    output_dir: str | os.PathLike[str],
    slot: datetime,
    market_id: str | None = None,
) -> MarketCandidate | None:
    """Recover one started same-slot opportunity from its durable frozen inputs."""

    frozen = load_frozen_artifacts_for_opportunity(output_dir, opportunity_id)
    if frozen is None:
        return None
    intent = ProviderCallJournal(output_dir).lookup(opportunity_id, "openai")
    if intent is None:
        return None
    required = {
        "raw_gamma",
        "sanitized_evidence",
        "precheck_book_raw",
        "precheck_book_http",
        "causal_history",
    }
    if not required <= frozen.keys():
        raise ValueError("frozen observation inputs are incomplete")
    evidence = EvidencePacket.model_validate_json(frozen["sanitized_evidence"])
    raw_gamma = load_strict_json(frozen["raw_gamma"])
    history = load_strict_json(frozen["causal_history"])
    if (
        (market_id is not None and evidence.market_id != market_id)
        or not isinstance(raw_gamma, dict)
        or not isinstance(history, dict)
    ):
        raise ValueError("frozen observation inputs are invalid")
    market_id, condition_id, yes_token_id, no_token_id = gamma_clob_identity(raw_gamma)
    raw_books = [
        base64.b64decode(value, validate=True) for value in history["raw_books_b64"]
    ]
    retrieved = [_dt(value) for value in history["response_retrieved_at"]]
    if len(raw_books) != 2 or len(retrieved) != 2:
        raise ValueError("frozen causal history is invalid")
    books = tuple(
        reconstruct_book_snapshot(raw, market_id=market_id, response_retrieved_at=at)
        for raw, at in zip(raw_books, retrieved, strict=True)
    )
    http = PrecheckHttpArtifact.model_validate_json(frozen["precheck_book_http"])
    attempt = PrecheckAttempt(
        token_id=http.request_token_id,
        attempted_at=http.attempted_at,
        status_code=http.status_code,
        content_type=http.content_type,
        retrieved_at=http.retrieved_at,
        body=frozen["precheck_book_raw"],
        truncated=http.response_truncated,
        transport_reason=http.transport_reason,
    )
    return MarketCandidate(
        market_id=market_id,
        condition_id=condition_id,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        slot_at=slot,
        raw_gamma_bytes=frozen["raw_gamma"],
        raw_gamma_sha256=evidence.source_sha256,
        evidence=evidence,
        precheck_book_bytes=frozen["precheck_book_raw"],
        precheck_book=books[0],
        precheck_attempt=attempt,
        precheck_history_bytes=(raw_books[0], raw_books[1]),
        precheck_history=(books[0], books[1]),
        precheck_attempts=(attempt, attempt),
    )


def _frozen_candidate(
    candidate: MarketCandidate,
    openai_request: bytes,
    frozen_artifacts: dict[str, bytes],
) -> MarketCandidate:
    """Rebind current public transport data to the original durable provider input."""

    request = load_strict_json(openai_request)
    if not isinstance(request, dict) or type(request.get("input")) is not str:
        raise ValueError("frozen OpenAI request is invalid")
    payload = request["input"]
    opening, closing = "<evidence>", "</evidence>"
    if not payload.startswith(opening) or not payload.endswith(closing):
        raise ValueError("frozen OpenAI evidence is invalid")
    evidence = type(candidate.evidence).model_validate_json(
        payload[len(opening) : -len(closing)]
    )
    raw_gamma = frozen_artifacts.get("raw_gamma")
    precheck_raw = frozen_artifacts.get("precheck_book_raw")
    precheck_http = frozen_artifacts.get("precheck_book_http")
    history_raw = frozen_artifacts.get("causal_history")
    if (
        raw_gamma is None
        or precheck_raw is None
        or precheck_http is None
        or history_raw is None
    ):
        raise ValueError("frozen observation inputs are incomplete")
    raw_gamma_value = load_strict_json(raw_gamma)
    history = load_strict_json(history_raw)
    if not isinstance(raw_gamma_value, dict) or not isinstance(history, dict):
        raise ValueError("frozen observation inputs are invalid")
    market_id, condition_id, yes_token_id, no_token_id = gamma_clob_identity(
        raw_gamma_value
    )
    if evidence.market_id != market_id:
        raise ValueError("frozen observation inputs are invalid")
    raw_books = [
        base64.b64decode(value, validate=True) for value in history["raw_books_b64"]
    ]
    retrieved = [_dt(value) for value in history["response_retrieved_at"]]
    books = tuple(
        reconstruct_book_snapshot(raw, market_id=market_id, response_retrieved_at=at)
        for raw, at in zip(raw_books, retrieved, strict=True)
    )
    http = PrecheckHttpArtifact.model_validate_json(precheck_http)
    attempt = PrecheckAttempt(
        token_id=http.request_token_id,
        attempted_at=http.attempted_at,
        status_code=http.status_code,
        content_type=http.content_type,
        retrieved_at=http.retrieved_at,
        body=precheck_raw,
        truncated=http.response_truncated,
        transport_reason=http.transport_reason,
    )
    return MarketCandidate(
        market_id=market_id,
        condition_id=condition_id,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        slot_at=candidate.slot_at,
        raw_gamma_bytes=raw_gamma,
        raw_gamma_sha256=evidence.source_sha256,
        evidence=evidence,
        precheck_book_bytes=precheck_raw,
        precheck_book=books[0],
        precheck_attempt=attempt,
        precheck_history_bytes=(raw_books[0], raw_books[1]),
        precheck_history=(books[0], books[1]),
        precheck_attempts=(attempt, attempt),
    )


def _observe_locked(
    *,
    limit: int,
    output_dir: str | os.PathLike[str],
    config: bytes,
    client: PolymarketClient | None,
    openai_transport: Transport | None,
    jev_transport: Transport | None,
    now: datetime | None,
    event_sink: EventSink | None,
    recovered_only: tuple[MarketCandidate, ...] | None = None,
) -> list[Summary]:
    cutoff = observation_slot(now or datetime.now(UTC))
    public = client or PolymarketClient(SecureHttpTransport())
    openai_sender = openai_transport or live_openai_transport()
    jev_sender = jev_transport or live_jev_transport()
    call_journal = ProviderCallJournal(output_dir)
    recovered: list[MarketCandidate] = list(recovered_only or ())
    if recovered_only is None:
        for opportunity_id in call_journal.opportunity_ids_created_at(cutoff):
            candidate = _frozen_candidate_for_opportunity(
                opportunity_id, output_dir=output_dir, slot=cutoff
            )
            if candidate is not None:
                recovered.append(candidate)
    if recovered_only is not None:
        eligible: tuple[MarketCandidate, ...] = ()
        rejections: list[MarketRejection] = []
    else:
        try:
            polled = public.poll(limit=limit, now=cutoff, history_points=2)
        except PublicDataError as error:
            if not recovered:
                return _public_poll_failure(
                    output_dir, error, observed_at=cutoff, config=config
                )
            eligible = ()
            rejections = []
        else:
            recovered_markets = {candidate.market_id for candidate in recovered}
            eligible = polled.eligible
            rejections = [
                rejection
                for rejection in polled.rejections
                if rejection.market_id not in recovered_markets
            ]
    summaries = _record_rejections(
        output_dir, tuple(rejections), observed_at=cutoff, config=config
    )
    eligible_markets = {candidate.market_id for candidate in eligible}
    missing = [
        candidate
        for candidate in recovered
        if candidate.market_id not in eligible_markets
    ]
    for candidate in (*eligible, *missing):
        provisional = _identity(candidate, candidate.precheck_book.source_sha256)
        call_journal = ProviderCallJournal(output_dir)
        input_changed = False
        prior_openai = call_journal.lookup(provisional.opportunity_id, "openai")
        if prior_openai is not None:
            frozen_request = (
                base64.b64decode(prior_openai["request_b64"], validate=True)
                if prior_openai["kind"] == "provider_call_intent"
                else base64.b64decode(
                    prior_openai["outcome"]["request_b64"], validate=True
                )
            )
            if frozen_request != build_openai_request(
                candidate.evidence, OPENAI_MODEL_ID
            ):
                input_changed = True
                frozen_artifacts = load_frozen_artifacts_for_opportunity(
                    output_dir, provisional.opportunity_id
                )
                if frozen_artifacts is None:
                    raise ValueError(
                        "durable provider intent lacks frozen observation input"
                    )
                candidate = _frozen_candidate(
                    candidate, frozen_request, frozen_artifacts
                )
                provisional = _identity(
                    candidate, candidate.precheck_book.source_sha256
                )
        existing = load_existing_terminal_for_inputs(
            output_dir,
            opportunity_id=provisional.opportunity_id,
            evidence_sha256=candidate.evidence.source_sha256,
        )
        if existing is not None:
            summaries.append(_summary(existing.result, existing.envelopes))
            continue
        base = _precheck_values(candidate, config)
        freeze_observation_artifacts(output_dir, provisional, _chain(provisional, base))
        call_journal = ProviderCallJournal(output_dir)
        openai_request = build_openai_request(candidate.evidence, OPENAI_MODEL_ID)
        openai = _journaled_provider_call(
            journal=call_journal,
            provider="openai",
            opportunity_id=provisional.opportunity_id,
            request_bytes=openai_request,
            created_at=cutoff,
            invoke=lambda: call_openai(
                candidate.evidence, OPENAI_MODEL_ID, openai_sender
            ),
            event_sink=event_sink,
        )
        assert isinstance(openai, (OpenAISuccess, AdapterFailure, AdapterCostOverflow))
        jev: JevSuccess | AdapterFailure | AdapterCostOverflow | None = None
        if isinstance(openai, OpenAISuccess):
            openai_success = openai
            frozen_openai_request = load_strict_json(openai.request_bytes)
            current_openai_request = load_strict_json(
                build_openai_request(candidate.evidence, OPENAI_MODEL_ID)
            )
            if frozen_openai_request != current_openai_request:
                openai = AdapterFailure(
                    provider="openai",
                    code="INPUT_CHANGED",
                    message="openai frozen input differs from current evidence",
                    request_bytes=openai_success.request_bytes,
                    response_sha256=None,
                    response_byte_length=0,
                    retrieved_at=None,
                    billed_cost_microdollars=openai_success.billed_cost_microdollars,
                    status_code=None,
                )
            else:
                existing_jev = call_journal.lookup(provisional.opportunity_id, "jev")
                frozen_jev_request = (
                    existing_jev.get("request_b64")
                    if existing_jev is not None
                    else None
                )
                if frozen_jev_request is None and existing_jev is not None:
                    frozen_jev_request = existing_jev["outcome"]["request_b64"]
                jev_request = (
                    base64.b64decode(frozen_jev_request, validate=True)
                    if frozen_jev_request is not None
                    else build_jev_request(
                        openai_success.brief, candidate.evidence, JEV_MODEL_ID
                    )
                )
                if input_changed and existing_jev is None:
                    jev = AdapterFailure(
                        provider="jev",
                        code="INPUT_CHANGED",
                        message="jev frozen input differs from current evidence",
                        request_bytes=jev_request,
                        response_sha256=None,
                        response_byte_length=0,
                        retrieved_at=None,
                        billed_cost_microdollars=0,
                        status_code=None,
                    )
                else:
                    jev_value = _journaled_provider_call(
                        journal=call_journal,
                        provider="jev",
                        opportunity_id=provisional.opportunity_id,
                        request_bytes=jev_request,
                        created_at=openai_success.retrieved_at,
                        invoke=lambda: call_jev(
                            openai_success.brief,
                            candidate.evidence,
                            JEV_MODEL_ID,
                            jev_sender,
                        ),
                        event_sink=event_sink,
                    )
                    assert isinstance(
                        jev_value, (JevSuccess, AdapterFailure, AdapterCostOverflow)
                    )
                    jev = jev_value
        frozen_at = max(
            cutoff,
            openai.retrieved_at if openai.retrieved_at is not None else cutoff,
            jev.retrieved_at
            if jev is not None and jev.retrieved_at is not None
            else cutoff,
        )
        provisional_values, _, _, _, provisional_cost = _live_artifacts(
            provisional, candidate, openai, jev, config=config, frozen_at=frozen_at
        )
        freeze_observation_artifacts(
            output_dir, provisional, _chain(provisional, provisional_values)
        )
        if event_sink is not None:
            event_sink("decision_frozen")
        if provisional_cost is None:
            values, decision, brief_hash, decision_hash, _ = _live_artifacts(
                provisional, candidate, openai, jev, config=config, frozen_at=frozen_at
            )
            summaries.append(
                _finish_book_unavailable(
                    output_dir,
                    provisional,
                    values,
                    planned_at=max(cutoff, frozen_at + timedelta(seconds=1)),
                    terminal_at=frozen_at,
                    observed_at=max(datetime.now(UTC), frozen_at),
                    reason=(
                        "UNKNOWN_IN_FLIGHT"
                        if (
                            isinstance(openai, AdapterFailure)
                            and openai.code == "UNKNOWN_IN_FLIGHT"
                        )
                        or (
                            isinstance(jev, AdapterFailure)
                            and jev.code == "UNKNOWN_IN_FLIGHT"
                        )
                        else "MODEL_FAILURE"
                        if isinstance(openai, AdapterFailure)
                        and openai.billed_cost_microdollars is None
                        or isinstance(jev, AdapterFailure)
                        and jev.billed_cost_microdollars is None
                        else "COST_OVERFLOW"
                    ),
                    evidence_at=candidate.evidence.retrieved_at,
                    research_brief_sha256=brief_hash,
                    decision=decision,
                    decision_sha256=decision_hash,
                    model_cost=None,
                )
            )
            continue
        if event_sink is not None:
            event_sink("execution_book_requested")
        try:
            raw_execution, execution_book = public.execution_book(
                candidate, now=max(cutoff, frozen_at + timedelta(seconds=1))
            )
            if (
                execution_book.response_retrieved_at or execution_book.retrieved_at
            ) <= frozen_at:
                raise ValueError("STALE_BOOK")
        except Exception as error:
            reason = str(error)
            if reason not in {
                "STALE_BOOK",
                "CROSSED_BOOK",
                "TOKEN_DRIFT",
                "MARKET_DRIFT",
            }:
                reason = "BOOK_UNAVAILABLE"
            values, decision, brief_hash, decision_hash, model_cost = _live_artifacts(
                provisional, candidate, openai, jev, config=config, frozen_at=frozen_at
            )
            summaries.append(
                _finish_book_unavailable(
                    output_dir,
                    provisional,
                    values,
                    planned_at=max(cutoff, frozen_at + timedelta(seconds=1)),
                    terminal_at=frozen_at,
                    observed_at=max(datetime.now(UTC), frozen_at),
                    reason=reason,
                    evidence_at=candidate.evidence.retrieved_at,
                    research_brief_sha256=brief_hash,
                    decision=decision,
                    decision_sha256=decision_hash,
                    model_cost=model_cost,
                )
            )
            continue
        final = _identity(candidate, execution_book.source_sha256)
        values, decision, brief_hash, decision_hash, model_cost = _live_artifacts(
            final, candidate, openai, jev, config=config, frozen_at=frozen_at
        )
        values.append(
            (
                "execution_book",
                raw_execution,
                "clob_execution_book",
                execution_book.response_retrieved_at or execution_book.retrieved_at,
            )
        )
        artifacts = _chain(final, values)
        result = _live_result(
            final,
            candidate,
            execution_book,
            decision=decision,
            research_brief_sha256=brief_hash,
            decision_sha256=decision_hash,
            model_cost=model_cost,
        )
        record_observation(output_dir, result, artifacts)
        replay = load_observation(output_dir, result.observation_id)
        summaries.append(_summary(replay.result, replay.envelopes))
    return summaries


def _replay_fixture(
    fixture_path: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    *,
    event_sink: EventSink | None = None,
    openai_transport: Transport | None = None,
    jev_transport: Transport | None = None,
) -> Summary:
    """Replay one canned observation without credentials, network, or fallback."""

    raw = _load_fixture(Path(fixture_path))
    gate = _ExecutionBookGate(raw, event_sink=event_sink)
    required_keys = set(FeasibilityFixture.model_fields)
    if set(raw) != required_keys:
        raise ValueError(f"fixture must contain exactly {sorted(required_keys)}")
    raw_values = raw["raw"]
    chronology_value = dict(raw["chronology"])
    prefix_model = PriceFreeFixturePrefix.model_validate_json(
        canonical_json_bytes(
            {
                "schema_version": raw["schema_version"],
                "identity": raw["identity"],
                "gamma_bytes": raw_values["gamma_bytes"],
                "sanitized_evidence": raw["sanitized_evidence"],
                "chronology": chronology_value,
                "model_artifacts": raw["model_artifacts"],
                "model_costs_microdollars": raw["model_costs_microdollars"],
            }
        )
    )
    identity = prefix_model.identity
    expected_opportunity_id = hashlib.sha256(
        canonical_json_bytes(
            {
                "policy_version": identity.policy_version,
                "market_id": identity.market_id,
                "slot_at": identity.slot_at.isoformat().replace("+00:00", "Z"),
            }
        )
    ).hexdigest()
    expected_observation_id = hashlib.sha256(
        canonical_json_bytes(
            {
                "opportunity_id": identity.opportunity_id,
                "evidence_sha256": identity.evidence_sha256,
                "execution_book_sha256": identity.execution_book_sha256,
                "policy_version": identity.policy_version,
            }
        )
    ).hexdigest()
    if (
        identity.opportunity_id != expected_opportunity_id
        or identity.observation_id != expected_observation_id
    ):
        raise ValueError("price-free fixture identity hashes are invalid")
    existing = load_existing_terminal(output_dir, identity.observation_id)
    if existing is not None:
        return _summary(existing.result, existing.envelopes)
    evidence_model = prefix_model.sanitized_evidence
    model_values = prefix_model.model_artifacts
    chronology = prefix_model.chronology
    costs = prefix_model.model_costs_microdollars

    gamma = prefix_model.gamma_bytes.encode()
    if (
        hashlib.sha256(gamma).hexdigest() != evidence_model.source_sha256
        or identity.evidence_sha256 != evidence_model.source_sha256
        or identity.market_id != evidence_model.market_id
        or identity.cutoff_at != evidence_model.cutoff_at
    ):
        raise ValueError("Gamma bytes do not bind price-free fixture identity")

    prefix = [
        ("raw_gamma", gamma, "gamma_public", evidence_model.retrieved_at),
        (
            "sanitized_evidence",
            _model_bytes(evidence_model),
            "gamma_sanitized",
            evidence_model.retrieved_at,
        ),
    ]
    freeze_observation_artifacts(output_dir, identity, _chain(identity, prefix))

    openai_request = model_values.openai_request_bytes.encode()
    openai_response = model_values.openai_response_bytes.encode()
    openai_at = _dt(str(raw["chronology"]["openai_response_at"]))
    openai_base_transport = openai_transport or _fixture_transport(
        openai_request,
        openai_response,
        openai_at,
        costs.openai,
    )

    def publish_openai_request(request_bytes: bytes) -> None:
        prefix.append(
            (
                "openai_request",
                request_bytes,
                "openai_request",
                chronology.openai_request_at,
            )
        )
        freeze_observation_artifacts(output_dir, identity, _chain(identity, prefix))

    if OPENAI_MODEL_ID == "gpt-5-2025-08-07":
        publish_openai_request(openai_request)
        openai = _journaled_provider_call(
            journal=ProviderCallJournal(output_dir),
            provider="openai",
            opportunity_id=identity.opportunity_id,
            request_bytes=openai_request,
            created_at=chronology.openai_request_at,
            invoke=lambda: call_openai(
                evidence_model,
                OPENAI_MODEL_ID,
                openai_base_transport,
            ),
            event_sink=None,
            unknown_on_transport_failure=False,
        )
    else:
        openai = call_openai(
            evidence_model,
            OPENAI_MODEL_ID,
            _publishing_transport(openai_base_transport, publish_openai_request),
        )
    if isinstance(openai, AdapterCostOverflow):
        raise ValueError("fixture replay cannot record a billing overflow")
    if isinstance(openai, AdapterFailure):
        failure_at = openai.retrieved_at or chronology.openai_response_at
        failure_prefix = prefix + [
            (
                "openai_failure",
                _model_bytes(failure_artifact_from_adapter(openai)),
                "openai_failure",
                failure_at,
            )
        ]
        frozen_at = max(chronology.decision_frozen_at, failure_at)
        values, _barrier = _decision_barrier(
            identity,
            failure_prefix,
            state="OPENAI_FAILURE",
            failure_code=openai.code,
            frozen_at=frozen_at,
            openai_request=openai.request_bytes,
            openai_response_sha256=openai.response_sha256,
            jev_request=None,
            jev_response_sha256=None,
            jev_decision_sha256=None,
            openai_cost=openai.billed_cost_microdollars,
            jev_cost=0,
        )
        artifacts = _chain(identity, values)
        freeze_observation_artifacts(output_dir, identity, artifacts)
        gate.freeze()
        if frozen_at >= chronology.planned_execution_book_at:
            return _finish_book_unavailable(
                output_dir,
                identity,
                values,
                planned_at=chronology.planned_execution_book_at,
                terminal_at=frozen_at,
                observed_at=frozen_at,
                reason="STALE_PRECHECK",
                evidence_at=evidence_model.retrieved_at,
                research_brief_sha256="0" * 64,
                decision=None,
                decision_sha256="0" * 64,
                model_cost=openai.billed_cost_microdollars,
            )
        fixture = gate.get()
        result = _failure_result(fixture, openai, 0)
        record_observation(output_dir, result, artifacts)
        replay = load_observation(output_dir, result.observation_id)
        return _summary(replay.result, replay.envelopes)
    assert isinstance(openai, OpenAISuccess)

    prefix.extend(
        [
            (
                "openai_response_success",
                openai.response_bytes,
                "openai_response",
                openai.retrieved_at,
            ),
            (
                "openai_success_metadata",
                _success_metadata(
                    "openai",
                    openai.request_bytes,
                    openai.response_bytes,
                    openai.retrieved_at,
                    openai.billed_cost_microdollars,
                ),
                "openai_success_metadata",
                openai.retrieved_at,
            ),
            (
                "research_brief",
                _model_bytes(openai.brief),
                "openai_validated",
                openai.retrieved_at,
            ),
        ]
    )
    freeze_observation_artifacts(output_dir, identity, _chain(identity, prefix))

    jev_request = model_values.jev_request_bytes.encode()
    jev_response = model_values.jev_response_bytes.encode()
    jev_at = _dt(str(raw["chronology"]["jev_response_at"]))
    jev_base_transport = jev_transport or _fixture_transport(
        jev_request,
        jev_response,
        jev_at,
        costs.jev,
    )

    def publish_jev_request(request_bytes: bytes) -> None:
        prefix.append(
            (
                "jev_request",
                request_bytes,
                "jev_request",
                chronology.jev_request_at,
            )
        )
        freeze_observation_artifacts(output_dir, identity, _chain(identity, prefix))

    if JEV_MODEL_ID == "jev-1.13.0":
        publish_jev_request(jev_request)
        jev = _journaled_provider_call(
            journal=ProviderCallJournal(output_dir),
            provider="jev",
            opportunity_id=identity.opportunity_id,
            request_bytes=jev_request,
            created_at=chronology.jev_request_at,
            invoke=lambda: call_jev(
                openai.brief,
                evidence_model,
                JEV_MODEL_ID,
                jev_base_transport,
            ),
            event_sink=None,
            unknown_on_transport_failure=False,
        )
    else:
        jev = call_jev(
            openai.brief,
            evidence_model,
            JEV_MODEL_ID,
            _publishing_transport(jev_base_transport, publish_jev_request),
        )
    if isinstance(jev, AdapterCostOverflow):
        raise ValueError("fixture replay cannot record a billing overflow")
    if isinstance(jev, AdapterFailure):
        failure_at = jev.retrieved_at or jev_at
        failure_prefix = prefix + [
            (
                "jev_failure",
                _model_bytes(failure_artifact_from_adapter(jev)),
                "jev_failure",
                failure_at,
            )
        ]
        frozen_at = max(chronology.decision_frozen_at, failure_at)
        values, _barrier = _decision_barrier(
            identity,
            failure_prefix,
            state="JEV_FAILURE",
            failure_code=jev.code,
            frozen_at=frozen_at,
            openai_request=openai.request_bytes,
            openai_response_sha256=hashlib.sha256(openai.response_bytes).hexdigest(),
            jev_request=jev.request_bytes,
            jev_response_sha256=jev.response_sha256,
            jev_decision_sha256=None,
            openai_cost=openai.billed_cost_microdollars,
            jev_cost=jev.billed_cost_microdollars,
        )
        artifacts = _chain(identity, values)
        freeze_observation_artifacts(output_dir, identity, artifacts)
        gate.freeze()
        if frozen_at >= chronology.planned_execution_book_at:
            return _finish_book_unavailable(
                output_dir,
                identity,
                values,
                planned_at=chronology.planned_execution_book_at,
                terminal_at=frozen_at,
                observed_at=frozen_at,
                reason=(
                    "UNKNOWN_IN_FLIGHT"
                    if jev.code == "UNKNOWN_IN_FLIGHT"
                    else "STALE_PRECHECK"
                ),
                evidence_at=evidence_model.retrieved_at,
                research_brief_sha256=hashlib.sha256(
                    _model_bytes(openai.brief)
                ).hexdigest(),
                decision=None,
                decision_sha256="0" * 64,
                model_cost=(
                    None
                    if jev.billed_cost_microdollars is None
                    else openai.billed_cost_microdollars + jev.billed_cost_microdollars
                ),
            )
        fixture = gate.get()
        result = _failure_result(fixture, jev, openai.billed_cost_microdollars)
        record_observation(output_dir, result, artifacts)
        replay = load_observation(output_dir, result.observation_id)
        return _summary(replay.result, replay.envelopes)
    assert isinstance(jev, JevSuccess)

    frozen_at = max(chronology.decision_frozen_at, jev.retrieved_at)
    decision_bytes = _model_bytes(jev.decision)
    decision_hash = hashlib.sha256(decision_bytes).hexdigest()
    success_prefix = prefix + [
        (
            "jev_response_success",
            jev.response_bytes,
            "jev_response",
            jev.retrieved_at,
        ),
        (
            "jev_success_metadata",
            _success_metadata(
                "jev",
                jev.request_bytes,
                jev.response_bytes,
                jev.retrieved_at,
                jev.billed_cost_microdollars,
            ),
            "jev_success_metadata",
            jev.retrieved_at,
        ),
        ("jev_decision", decision_bytes, "jev_validated", jev.retrieved_at),
    ]
    values, _barrier = _decision_barrier(
        identity,
        success_prefix,
        state="JEV_SUCCESS",
        failure_code=None,
        frozen_at=frozen_at,
        openai_request=openai.request_bytes,
        openai_response_sha256=hashlib.sha256(openai.response_bytes).hexdigest(),
        jev_request=jev.request_bytes,
        jev_response_sha256=hashlib.sha256(jev.response_bytes).hexdigest(),
        jev_decision_sha256=decision_hash,
        openai_cost=openai.billed_cost_microdollars,
        jev_cost=jev.billed_cost_microdollars,
    )
    artifacts = _chain(identity, values)
    freeze_observation_artifacts(output_dir, identity, artifacts)
    gate.freeze()
    if frozen_at >= chronology.planned_execution_book_at:
        return _finish_book_unavailable(
            output_dir,
            identity,
            values,
            planned_at=chronology.planned_execution_book_at,
            terminal_at=frozen_at,
            observed_at=frozen_at,
            reason="STALE_PRECHECK",
            evidence_at=evidence_model.retrieved_at,
            research_brief_sha256=hashlib.sha256(
                _model_bytes(openai.brief)
            ).hexdigest(),
            decision=jev.decision,
            decision_sha256=decision_hash,
            model_cost=(openai.billed_cost_microdollars + jev.billed_cost_microdollars),
        )

    fixture = gate.get()
    result = evaluate_fixture(fixture)
    if result != fixture.expected.observation_result:
        raise ValueError(
            "fixture expected result differs from recomputed engine result"
        )
    artifacts = _chain(identity, values)
    record_observation(output_dir, result, artifacts)
    replay = load_observation(output_dir, result.observation_id)
    return _summary(replay.result, replay.envelopes)


def observe_markets(
    *,
    limit: int,
    output_dir: str | os.PathLike[str],
    client: PolymarketClient | None = None,
    openai_transport: Transport | None = None,
    jev_transport: Transport | None = None,
    now: datetime | None = None,
    event_sink: EventSink | None = None,
) -> list[Summary]:
    """Run one audited paper pass and publish metrics under the observation lock."""

    from jev_trading_lab.polymarket.metrics_export import render_paper_metrics

    _runtime_config_bytes()
    with observation_lock(output_dir):
        run_id = _run_id("observe", output_dir)
        append_operation(
            Path(output_dir),
            _operation_event(
                kind=OperationKind.RUN_STARTED, run_id=run_id, status="started"
            ),
        )
        started_ns = time.monotonic_ns()
        public = client or PolymarketClient(SecureHttpTransport())
        public.attempt_sink = _attempt_sink(output_dir, run_id)
        try:
            summaries = _observe_markets(
                limit=limit,
                output_dir=output_dir,
                client=public,
                openai_transport=openai_transport,
                jev_transport=jev_transport,
                now=now,
                event_sink=event_sink,
                _lock_held=True,
            )
        except BaseException:
            _finish_run(output_dir, run_id, "failure", time.monotonic_ns() - started_ns)
            render_paper_metrics(output_dir)
            raise
        _finish_run(output_dir, run_id, "success", time.monotonic_ns() - started_ns)
        render_paper_metrics(output_dir)
        return summaries


def replay_fixture(
    fixture_path: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    *,
    event_sink: EventSink | None = None,
    openai_transport: Transport | None = None,
    jev_transport: Transport | None = None,
) -> Summary:
    """Replay one fixture and publish after its durable audited terminal event."""

    from jev_trading_lab.polymarket.metrics_export import render_paper_metrics

    with observation_lock(output_dir):
        run_id = _run_id("replay", output_dir)
        append_operation(
            Path(output_dir),
            _operation_event(
                kind=OperationKind.RUN_STARTED, run_id=run_id, status="started"
            ),
        )
        started_ns = time.monotonic_ns()
        try:
            summary = _replay_fixture(
                fixture_path,
                output_dir,
                event_sink=event_sink,
                openai_transport=openai_transport,
                jev_transport=jev_transport,
            )
        except BaseException:
            _finish_run(output_dir, run_id, "failure", time.monotonic_ns() - started_ns)
            render_paper_metrics(output_dir)
            raise
        _finish_run(output_dir, run_id, "replayed", time.monotonic_ns() - started_ns)
        render_paper_metrics(output_dir)
        return summary

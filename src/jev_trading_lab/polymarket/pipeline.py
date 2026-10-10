"""Offline-only fixture replay orchestration for the Polymarket feasibility slice."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.persistence.blobs import BlobStore
from jev_trading_lab.polymarket.ai import (
    JEV_MODEL_ID,
    OPENAI_MODEL_ID,
    AdapterFailure,
    FailureCode,
    JevSuccess,
    OpenAISuccess,
    Transport,
    TransportResponse,
    call_jev,
    call_openai,
    live_jev_transport,
    live_openai_transport,
)
from jev_trading_lab.polymarket.client import (
    CLOB_ENDPOINT,
    GAMMA_ENDPOINT,
    MarketCandidate,
    MarketRejection,
    PolymarketClient,
    SecureHttpTransport,
)
from jev_trading_lab.polymarket.models import (
    ArmResult,
    CausalHistoryPoint,
    Economics,
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
    SuccessArtifact,
    failure_artifact_from_adapter,
    freeze_observation_artifacts,
    load_existing_terminal,
    load_observation,
    record_observation,
)

EventSink = Callable[[str], None]
Summary = dict[str, object]


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
    openai_cost: int,
    jev_cost: int,
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
    cost = _sum_model_cost(prior_cost, failure.billed_cost_microdollars)
    if cost is None:
        ai = _cost_overflow_arm()
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


def _cost_overflow_arm() -> ArmResult:
    return ArmResult(
        arm="AI",
        signal_probability_micros=None,
        signal_action=None,
        disposition="COST_OVERFLOW",
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
) -> ObservationResult:
    ai = (
        _cost_overflow_arm()
        if model_cost is None
        else _book_failure_arm(arm="AI", model_cost=model_cost, decision=decision)
    )
    control = _book_failure_arm(arm="DETERMINISTIC", model_cost=0)
    return ObservationResult(
        observation_id=identity.observation_id,
        opportunity_id=identity.opportunity_id,
        market_id=identity.market_id,
        policy_version=identity.policy_version,
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
    cutoff = candidate.evidence.cutoff_at.isoformat().replace("+00:00", "Z")
    opportunity_id = hashlib.sha256(
        canonical_json_bytes(
            {
                "policy_version": POLICY_VERSION,
                "market_id": candidate.market_id,
                "cutoff_at": cutoff,
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
        cutoff_at=candidate.evidence.cutoff_at,
        opportunity_id=opportunity_id,
        observation_id=observation_id,
        evidence_sha256=candidate.evidence.source_sha256,
        execution_book_sha256=execution_hash,
    )


def _live_policy() -> Economics:
    return Economics(
        entry_fee_pp_micros=10_000,
        exit_fee_pp_micros=0,
        exit_slippage_pp_micros=10_000,
        sigma_p_micros=100_000,
        starting_cash_microdollars=STARTING_CASH_MICRODOLLARS,
    )


def _live_causal_history(candidate: MarketCandidate) -> list[CausalHistoryPoint]:
    return (
        [
            CausalHistoryPoint(
                midpoint_micros=midpoint_micros(candidate.precheck_book),
                observed_at=candidate.precheck_book.retrieved_at,
            )
        ]
        if candidate.precheck_book.retrieved_at < candidate.evidence.cutoff_at
        else []
    )


def _causal_history_bytes(candidate: MarketCandidate) -> bytes:
    return canonical_json_bytes(
        [point.model_dump(mode="json") for point in _live_causal_history(candidate)]
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
        _cost_overflow_arm()
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


def _freeze_live_bytes(
    output_dir: str | os.PathLike[str],
    identity: FixtureIdentity,
    *,
    kind: str,
    payload: bytes,
    retrieved_at: datetime,
) -> None:
    """Freeze exact live bytes before the next causal stage."""

    allowed = {
        "raw_gamma",
        "sanitized_evidence",
        "precheck_book",
        "openai_request",
        "openai_response_success",
        "openai_failure",
        "research_brief",
        "jev_request",
        "jev_response_success",
        "jev_failure",
        "jev_decision",
        "decision_frozen",
        "execution_book",
    }
    if kind not in allowed:
        raise ValueError("unsupported live artifact kind")
    root = Path(output_dir)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with BlobStore(root.absolute() / "blobs") as store:
        ref = store.publish(payload)
    line = (
        canonical_json_bytes(
            {
                "byte_size": ref.byte_length,
                "kind": kind,
                "observation_id": identity.observation_id,
                "opportunity_id": identity.opportunity_id,
                "retrieved_at": retrieved_at.isoformat().replace("+00:00", "Z"),
                "sha256": ref.sha256,
                "source_uri_label": "live_causal_stage",
                "storage_relpath": ref.relative_path,
            }
        )
        + b"\n"
    )
    descriptor = os.open(
        root / "live-artifacts.jsonl",
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.write(descriptor, line)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _freeze_live_values(
    output_dir: str | os.PathLike[str],
    identity: FixtureIdentity,
    values: list[tuple[str, bytes, str, datetime]],
) -> None:
    for kind, payload, _source, retrieved_at in values:
        if kind in {"openai_success_metadata", "jev_success_metadata"}:
            continue
        _freeze_live_bytes(
            output_dir,
            identity,
            kind=kind,
            payload=payload,
            retrieved_at=retrieved_at,
        )


def _runtime_config(output_dir: str | os.PathLike[str]) -> None:
    """Freeze non-secret endpoint, model, policy, and explicit billing configuration."""

    root = Path(output_dir)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    value = {
        "endpoints": {
            "clob": CLOB_ENDPOINT,
            "gamma": GAMMA_ENDPOINT,
            "jev": "https://api.typesafe.ai/v1/systemone",
            "openai": "https://api.openai.com/v1/responses",
        },
        "models": {"jev": JEV_MODEL_ID, "openai": OPENAI_MODEL_ID},
        "policy_version": POLICY_VERSION,
        "rates_microdollars_per_million_tokens": {
            name: os.environ.get(name)
            for name in (
                "OPENAI_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
                "OPENAI_CACHED_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
                "OPENAI_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
                "TYPESAFE_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
                "TYPESAFE_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
            )
        },
    }
    payload = canonical_json_bytes(value) + b"\n"
    path = root / "runtime-config.json"
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError("runtime configuration differs from frozen artifact")
        return
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600
    )
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _live_artifacts(
    identity: FixtureIdentity,
    candidate: MarketCandidate,
    openai: OpenAISuccess | AdapterFailure,
    jev: JevSuccess | AdapterFailure | None,
    *,
    frozen_at: datetime,
) -> tuple[
    list[tuple[str, bytes, str, datetime]],
    JevDecision | None,
    str,
    str,
    int | None,
]:
    evidence_bytes = _model_bytes(candidate.evidence)
    prefix: list[tuple[str, bytes, str, datetime]] = [
        (
            "raw_gamma",
            candidate.raw_gamma_bytes,
            "gamma_market",
            candidate.evidence.retrieved_at,
        ),
        (
            "sanitized_evidence",
            evidence_bytes,
            "gamma_sanitized",
            candidate.evidence.retrieved_at,
        ),
        (
            "precheck_book",
            candidate.precheck_book_bytes,
            "clob_precheck_book",
            candidate.precheck_book.retrieved_at,
        ),
        (
            "causal_history",
            _causal_history_bytes(candidate),
            "precheck_causal_history",
            candidate.precheck_book.retrieved_at,
        ),
    ]
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
        aggregate_cost = _sum_model_cost(
            openai.billed_cost_microdollars, jev.billed_cost_microdollars
        )
        values, _ = _decision_barrier(
            identity,
            prefix,
            state="JEV_FAILURE" if aggregate_cost is not None else "COST_OVERFLOW",
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


def _record_rejections(
    output_dir: str | os.PathLike[str],
    rejections: tuple[Any, ...],
    *,
    observed_at: datetime,
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
                    "cutoff_at": cutoff_text,
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
            ("raw_gamma", rejection.raw_gamma_bytes, "gamma_market", observed_at)
        ]
        if rejection.evidence is not None:
            rejection_values.append(
                (
                    "sanitized_evidence",
                    _model_bytes(rejection.evidence),
                    "gamma_sanitized",
                    rejection.evidence.retrieved_at,
                )
            )
            if (
                rejection.precheck_book_bytes is not None
                and rejection.precheck_book is not None
            ):
                rejection_values.append(
                    (
                        "precheck_book",
                        rejection.precheck_book_bytes,
                        "clob_precheck_book",
                        rejection.precheck_book.retrieved_at,
                    )
                )
            elif rejection.precheck_failure_sha256 is not None:
                failure = PrecheckBookFailureArtifact(
                    market_id=rejection.market_id,
                    token_id=rejection.yes_token_id or "UNKNOWN",
                    reason=rejection.reason,
                    raw_book_sha256=rejection.precheck_failure_sha256,
                    retrieved_at=rejection.precheck_retrieved_at or observed_at,
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
            terminal_status="ELIGIBILITY_REJECTION",
        )
        summaries.append(summary)
    return summaries


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
    """Run one bounded, paper-only public polling pass."""

    if not 1 <= limit <= 20:
        raise ValueError("limit must be in 1..20")
    cutoff = now or datetime.now(UTC)
    public = client or PolymarketClient(SecureHttpTransport())
    openai_sender = openai_transport or live_openai_transport()
    jev_sender = jev_transport or live_jev_transport()
    _runtime_config(output_dir)
    polled = public.poll(limit=limit, now=cutoff)
    summaries = _record_rejections(output_dir, polled.rejections, observed_at=cutoff)
    for candidate in polled.eligible:
        provisional = _identity(candidate, candidate.precheck_book.source_sha256)
        base = [
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
                "precheck_book",
                candidate.precheck_book_bytes,
                "clob_precheck_book",
                candidate.precheck_book.retrieved_at,
            ),
            (
                "causal_history",
                _causal_history_bytes(candidate),
                "precheck_causal_history",
                candidate.precheck_book.retrieved_at,
            ),
        ]
        freeze_observation_artifacts(output_dir, provisional, _chain(provisional, base))
        openai = call_openai(candidate.evidence, OPENAI_MODEL_ID, openai_sender)
        jev: JevSuccess | AdapterFailure | None = None
        if isinstance(openai, OpenAISuccess):
            jev = call_jev(openai.brief, candidate.evidence, JEV_MODEL_ID, jev_sender)
        frozen_at = max(
            cutoff,
            openai.retrieved_at if openai.retrieved_at is not None else cutoff,
            jev.retrieved_at
            if jev is not None and jev.retrieved_at is not None
            else cutoff,
        )
        provisional_values, _, _, _, provisional_cost = _live_artifacts(
            provisional, candidate, openai, jev, frozen_at=frozen_at
        )
        freeze_observation_artifacts(
            output_dir, provisional, _chain(provisional, provisional_values)
        )
        if event_sink is not None:
            event_sink("decision_frozen")
        if provisional_cost is None:
            values, decision, brief_hash, decision_hash, _ = _live_artifacts(
                provisional, candidate, openai, jev, frozen_at=frozen_at
            )
            summaries.append(
                _finish_book_unavailable(
                    output_dir,
                    provisional,
                    values,
                    planned_at=max(cutoff, frozen_at + timedelta(seconds=1)),
                    terminal_at=frozen_at,
                    observed_at=max(datetime.now(UTC), frozen_at),
                    reason="COST_OVERFLOW",
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
            if execution_book.retrieved_at <= frozen_at:
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
                provisional, candidate, openai, jev, frozen_at=frozen_at
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
            final, candidate, openai, jev, frozen_at=frozen_at
        )
        values.append(
            (
                "execution_book",
                raw_execution,
                "clob_execution_book",
                execution_book.retrieved_at,
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


def replay_fixture(
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
                "cutoff_at": identity.cutoff_at.isoformat().replace("+00:00", "Z"),
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

    openai = call_openai(
        evidence_model,
        OPENAI_MODEL_ID,
        _publishing_transport(openai_base_transport, publish_openai_request),
    )
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

    jev = call_jev(
        openai.brief,
        evidence_model,
        JEV_MODEL_ID,
        _publishing_transport(jev_base_transport, publish_jev_request),
    )
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
                reason="STALE_PRECHECK",
                evidence_at=evidence_model.retrieved_at,
                research_brief_sha256=hashlib.sha256(
                    _model_bytes(openai.brief)
                ).hexdigest(),
                decision=None,
                decision_sha256="0" * 64,
                model_cost=(
                    openai.billed_cost_microdollars + jev.billed_cost_microdollars
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

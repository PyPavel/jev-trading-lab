"""Strict, paper-only contracts for the Polymarket feasibility slice."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from jev_trading_lab.core.ids import canonical_json_bytes

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Micros = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
SignedInt = Annotated[int, Field(strict=True)]
Action = Literal["BUY_YES", "BUY_NO", "ABSTAIN"]
Disposition = Literal[
    "FILLED",
    "PARTIAL_FILL",
    "ABSTAIN",
    "NO_EDGE",
    "INELIGIBLE",
    "MODEL_FAILURE",
    "BOOK_FAILURE",
]
MICROS_PER_UNIT = 1_000_000
_FAILURE_DISPOSITIONS = frozenset({"INELIGIBLE", "MODEL_FAILURE", "BOOK_FAILURE"})
_FILL_DISPOSITIONS = frozenset({"FILLED", "PARTIAL_FILL"})


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_strict_json(raw: str | bytes | bytearray) -> Any:
    """Decode JSON while rejecting duplicate keys at every nesting level."""

    return json.loads(raw, object_pairs_hook=_reject_duplicate_keys)


def _brier_micros(probability_micros: int, payout_micros: int) -> int:
    """Return squared error in micros, rounding down by integer division."""

    return (probability_micros - payout_micros) ** 2 // MICROS_PER_UNIT


class StrictModel(BaseModel):
    """Common closed-world validation for persisted feasibility contracts."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        extra: Literal["allow", "ignore", "forbid"] | None = None,
        context: Any | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        """Validate JSON after rejecting duplicate keys at every nesting level."""

        duplicate_safe = json.dumps(load_strict_json(json_data), separators=(",", ":"))
        return super().model_validate_json(
            duplicate_safe,
            strict=strict,
            extra=extra,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )

    @classmethod
    def model_validate_json_strict(cls, raw: str | bytes | bytearray) -> Self:
        """Compatibility alias for the standard duplicate-safe JSON API."""

        return cls.model_validate_json(raw)


class UtcModel(StrictModel):
    """Require every declared datetime field to identify UTC exactly."""

    @field_validator("*", mode="after")
    @classmethod
    def validate_utc_datetimes(cls, value: object) -> object:
        if isinstance(value, datetime) and (
            value.tzinfo is None
            or value.utcoffset() is None
            or value.utcoffset() != timedelta(0)
        ):
            raise ValueError("timestamp must be UTC-aware with offset zero")
        return value


class EvidencePacket(UtcModel):
    """Price-free public evidence that may be sent to research models."""

    market_id: NonEmptyStr
    question: NonEmptyStr
    description: NonEmptyStr
    rules: NonEmptyStr
    category: NonEmptyStr
    tags: list[NonEmptyStr]
    resolution_source_text: NonEmptyStr
    closes_at: datetime
    retrieved_at: datetime
    cutoff_at: datetime
    source_sha256: Sha256

    @model_validator(mode="after")
    def validate_times(self) -> Self:
        if self.retrieved_at > self.cutoff_at:
            raise ValueError("evidence retrieval cannot be after cutoff")
        return self


class BookLevel(StrictModel):
    """One fixed-point price level with positive captured quantity."""

    price_micros: Micros
    quantity: PositiveInt


class BookSnapshot(UtcModel):
    """Causal, best-first order book captured as exact integer values."""

    market_id: NonEmptyStr
    token_id: NonEmptyStr
    retrieved_at: datetime
    source_sha256: Sha256
    bids: Annotated[list[BookLevel], Field(min_length=1)]
    asks: Annotated[list[BookLevel], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_book(self) -> Self:
        bid_prices = [level.price_micros for level in self.bids]
        ask_prices = [level.price_micros for level in self.asks]
        if any(left <= right for left, right in zip(bid_prices, bid_prices[1:])):
            raise ValueError("bids must be in strictly decreasing best-first order")
        if any(left >= right for left, right in zip(ask_prices, ask_prices[1:])):
            raise ValueError("asks must be in strictly increasing best-first order")
        if bid_prices and ask_prices and bid_prices[0] >= ask_prices[0]:
            raise ValueError("book must not be locked or crossed")
        return self


class CitedFact(StrictModel):
    """A fact tied to the exact evidence packet that supports it."""

    fact: NonEmptyStr
    evidence_sha256: Sha256


class ResearchBrief(StrictModel):
    """Structured research whose citations all bind one evidence artifact."""

    evidence_sha256: Sha256
    cited_facts: list[CitedFact]
    arguments_for: list[NonEmptyStr]
    arguments_against: list[NonEmptyStr]
    contradictions: list[NonEmptyStr]
    missing_evidence: list[NonEmptyStr]
    quality: Literal["LOW", "MEDIUM", "HIGH"]

    @model_validator(mode="after")
    def bind_citations(self) -> Self:
        if any(
            citation.evidence_sha256 != self.evidence_sha256
            for citation in self.cited_facts
        ):
            raise ValueError("every citation must name the brief evidence SHA-256")
        return self


class JevDecision(StrictModel):
    """Bounded Jev forecast and paper action, without execution arithmetic."""

    p_yes_micros: Micros
    action: Action
    confidence_micros: Micros
    sufficiency_micros: Micros
    ambiguity_micros: Micros
    rationale: Annotated[str, StringConstraints(min_length=1, max_length=500)]


class ArmResult(StrictModel):
    """One arm result; mark/payout are always market-YES values in micros."""

    arm: Literal["AI", "DETERMINISTIC"]
    signal_probability_micros: Micros | None
    signal_action: Action | None
    disposition: Disposition
    entry_side: Literal["YES", "NO"] | None
    entry_vwap_micros: Micros | None
    quantity: NonNegativeInt
    fees_microdollars: NonNegativeInt
    initial_cash_microdollars: NonNegativeInt
    final_cash_microdollars: NonNegativeInt
    mark_micros: Micros | None
    payout_micros: Literal[0, 1_000_000] | None
    trading_pnl_microdollars: SignedInt
    economic_pnl_microdollars: SignedInt
    model_cost_microdollars: NonNegativeInt
    forecast_probability_micros: Micros | None
    forecast_status: Literal["SCORED", "PENDING_RESOLUTION", "UNAVAILABLE"]
    brier_micros: Micros | None

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        self._validate_signal()
        self._validate_execution()
        self._validate_economics()
        self._validate_forecast()
        return self

    def _validate_signal(self) -> None:
        signal = (self.signal_probability_micros, self.signal_action)
        if self.disposition in _FAILURE_DISPOSITIONS:
            if signal != (None, None):
                raise ValueError("failure disposition requires an unavailable signal")
            return
        if signal[0] is None or signal[1] is None:
            raise ValueError("disposition requires probability and action")
        expected_actions: dict[str, frozenset[str]] = {
            "FILLED": frozenset({"BUY_YES", "BUY_NO"}),
            "PARTIAL_FILL": frozenset({"BUY_YES", "BUY_NO"}),
            "ABSTAIN": frozenset({"ABSTAIN"}),
            "NO_EDGE": frozenset({"BUY_YES", "BUY_NO"}),
        }
        if signal[1] not in expected_actions[self.disposition]:
            raise ValueError("signal action is incompatible with disposition")

    def _validate_execution(self) -> None:
        filled = self.disposition in _FILL_DISPOSITIONS
        if filled != (self.quantity > 0):
            raise ValueError(
                "fill disposition and positive quantity must occur together"
            )
        if filled != (self.entry_side is not None):
            raise ValueError("fill disposition and entry side must occur together")
        if filled != (self.entry_vwap_micros is not None):
            raise ValueError("fill disposition and entry VWAP must occur together")
        if not filled:
            if self.fees_microdollars != 0:
                raise ValueError("an arm without an entry cannot have execution fees")
            if self.mark_micros is not None or self.payout_micros is not None:
                raise ValueError(
                    "an arm without an entry cannot carry a mark or payout"
                )
            return
        if self.signal_action == "BUY_YES" and self.entry_side != "YES":
            raise ValueError("BUY_YES entry side must be YES")
        if self.signal_action == "BUY_NO" and self.entry_side != "NO":
            raise ValueError("BUY_NO entry side must be NO")
        valuation_count = sum(
            value is not None for value in (self.mark_micros, self.payout_micros)
        )
        if valuation_count != 1:
            raise ValueError("filled arm requires exactly one mark or payout")

    def _validate_economics(self) -> None:
        if self.quantity == 0:
            expected_trading = 0
        else:
            market_yes_valuation = (
                self.payout_micros
                if self.payout_micros is not None
                else self.mark_micros
            )
            if market_yes_valuation is None or self.entry_vwap_micros is None:
                raise ValueError("filled economics require a valuation and entry VWAP")
            held_token_valuation = (
                market_yes_valuation
                if self.entry_side == "YES"
                else MICROS_PER_UNIT - market_yes_valuation
            )
            funded_notional = (
                self.quantity * self.entry_vwap_micros + self.fees_microdollars
            )
            if funded_notional > self.initial_cash_microdollars:
                raise ValueError("filled notional plus entry fees exceeds initial cash")
            expected_trading = (
                self.quantity * (held_token_valuation - self.entry_vwap_micros)
                - self.fees_microdollars
            )
        if self.trading_pnl_microdollars != expected_trading:
            raise ValueError("trading P&L does not match entry economics")
        if self.final_cash_microdollars != (
            self.initial_cash_microdollars + expected_trading
        ):
            raise ValueError("final cash must equal initial cash plus trading P&L")
        if self.economic_pnl_microdollars != (
            expected_trading - self.model_cost_microdollars
        ):
            raise ValueError("economic P&L must equal trading P&L minus model cost")

    def _validate_forecast(self) -> None:
        if self.forecast_probability_micros != self.signal_probability_micros:
            raise ValueError("forecast probability must equal signal probability")
        available = self.signal_probability_micros is not None
        if not available:
            if self.forecast_status != "UNAVAILABLE" or self.brier_micros is not None:
                raise ValueError("unavailable signal requires UNAVAILABLE forecast")
            return
        if self.forecast_status == "UNAVAILABLE":
            raise ValueError("available signal cannot have UNAVAILABLE forecast")
        scored = self.forecast_status == "SCORED"
        if scored != (self.brier_micros is not None):
            raise ValueError("SCORED and Brier value must occur together")
        if not scored:
            return
        if self.payout_micros is None:
            return
        forecast_probability = self.forecast_probability_micros
        if forecast_probability is None:
            raise ValueError("available forecast requires a probability")
        expected_brier = _brier_micros(forecast_probability, self.payout_micros)
        if self.brier_micros != expected_brier:
            raise ValueError("Brier value does not match probability and payout")


class ObservationResult(UtcModel):
    """One synchronized result containing exactly the AI and control arms."""

    observation_id: Sha256
    opportunity_id: Sha256
    market_id: NonEmptyStr
    policy_version: NonEmptyStr
    cutoff_at: datetime
    observed_at: datetime
    evidence_sha256: Sha256
    execution_book_sha256: Sha256
    research_brief_sha256: Sha256
    jev_decision_sha256: Sha256
    ai: ArmResult
    deterministic: ArmResult
    baseline_probability_micros: Micros
    resolution: Literal[0, 1] | None
    baseline_brier_micros: Micros | None
    paired_trading_pnl_delta_microdollars: SignedInt
    paired_economic_pnl_delta_microdollars: SignedInt
    paired_brier_delta_micros: SignedInt | None

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        if self.ai.arm != "AI" or self.deterministic.arm != "DETERMINISTIC":
            raise ValueError("observation requires one AI and one deterministic arm")
        if self.observed_at < self.cutoff_at:
            raise ValueError("observation cannot precede cutoff")
        expected_payout = (
            None if self.resolution is None else self.resolution * MICROS_PER_UNIT
        )
        for arm in (self.ai, self.deterministic):
            if arm.quantity > 0 and arm.payout_micros != expected_payout:
                raise ValueError("filled arm payout must match observation resolution")
            if arm.quantity == 0 and arm.payout_micros is not None:
                raise ValueError("nonfilled arm cannot carry a payout")
        if self.paired_trading_pnl_delta_microdollars != (
            self.ai.trading_pnl_microdollars
            - self.deterministic.trading_pnl_microdollars
        ):
            raise ValueError("paired trading P&L delta must be AI minus deterministic")
        if self.paired_economic_pnl_delta_microdollars != (
            self.ai.economic_pnl_microdollars
            - self.deterministic.economic_pnl_microdollars
        ):
            raise ValueError("paired economic P&L delta must be AI minus deterministic")
        if self.resolution is None:
            if (
                self.baseline_brier_micros is not None
                or self.paired_brier_delta_micros is not None
            ):
                raise ValueError("unresolved observation cannot carry Brier results")
            for arm in (self.ai, self.deterministic):
                expected_status = (
                    "PENDING_RESOLUTION"
                    if arm.forecast_probability_micros is not None
                    else "UNAVAILABLE"
                )
                if (
                    arm.forecast_status != expected_status
                    or arm.brier_micros is not None
                ):
                    raise ValueError("unresolved arm has inconsistent forecast state")
        else:
            payout_micros = self.resolution * MICROS_PER_UNIT
            expected_baseline = _brier_micros(
                self.baseline_probability_micros, payout_micros
            )
            if self.baseline_brier_micros != expected_baseline:
                raise ValueError("baseline Brier value does not match resolution")
            for arm in (self.ai, self.deterministic):
                probability = arm.forecast_probability_micros
                if probability is None:
                    if (
                        arm.forecast_status != "UNAVAILABLE"
                        or arm.brier_micros is not None
                    ):
                        raise ValueError("unavailable resolved arm cannot be scored")
                    continue
                if arm.forecast_status != "SCORED" or arm.brier_micros != _brier_micros(
                    probability, payout_micros
                ):
                    raise ValueError("available resolved arm has invalid Brier state")
            ai_brier = self.ai.brier_micros
            deterministic_brier = self.deterministic.brier_micros
            expected_paired = (
                ai_brier - deterministic_brier
                if ai_brier is not None and deterministic_brier is not None
                else None
            )
            if self.paired_brier_delta_micros != expected_paired:
                raise ValueError("paired Brier delta must be AI minus deterministic")
        return self


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _canonical_model_sha(model: StrictModel) -> str:
    return hashlib.sha256(
        canonical_json_bytes(model.model_dump(mode="json"))
    ).hexdigest()


def validate_feasibility_fixture(fixture: dict[str, Any]) -> None:
    """Validate Unit 1 fixture hashes and all cross-artifact bindings."""

    raw = fixture["raw"]
    model_artifacts = fixture["model_artifacts"]
    expected = fixture["expected"]
    artifacts = {**raw, **model_artifacts}
    if set(artifacts) != set(expected["artifact_sha256"]):
        raise ValueError("fixture artifact hash manifest is incomplete")
    for name, value in artifacts.items():
        if _sha256_text(value) != expected["artifact_sha256"][name]:
            raise ValueError(f"artifact hash mismatch: {name}")

    gamma_hash = _sha256_text(raw["gamma_bytes"])
    evidence = EvidencePacket.model_validate_json(
        json.dumps(fixture["sanitized_evidence"])
    )
    brief = ResearchBrief.model_validate_json(json.dumps(fixture["research_brief"]))
    observation = ObservationResult.model_validate_json(
        json.dumps(expected["observation_result"])
    )
    citation_hashes = {citation.evidence_sha256 for citation in brief.cited_facts}
    if not (
        evidence.source_sha256
        == brief.evidence_sha256
        == observation.evidence_sha256
        == gamma_hash
        and citation_hashes == {gamma_hash}
    ):
        raise ValueError("Gamma hash does not match evidence bindings")

    books = {
        "precheck_book": BookSnapshot.model_validate_json(
            json.dumps(fixture["precheck_book"])
        ),
        "execution_book": BookSnapshot.model_validate_json(
            json.dumps(fixture["execution_book"])
        ),
    }
    for name, book in books.items():
        raw_name = f"{name}_bytes"
        raw_hash = _sha256_text(raw[raw_name])
        if book.source_sha256 != raw_hash:
            raise ValueError(f"{name} hash does not match raw bytes")
    if observation.execution_book_sha256 != books["execution_book"].source_sha256:
        raise ValueError("observation execution book hash does not match raw bytes")

    decision = JevDecision.model_validate(fixture["jev_decision"])
    jev_response = load_strict_json(model_artifacts["jev_response_bytes"])
    if (
        not isinstance(jev_response, dict)
        or jev_response.get("result") != fixture["jev_decision"]
    ):
        raise ValueError("Jev response does not match decision")
    openai_response = load_strict_json(model_artifacts["openai_response_bytes"])
    if (
        not isinstance(openai_response, dict)
        or openai_response.get("output") != fixture["research_brief"]
    ):
        raise ValueError("OpenAI response does not match research brief")
    canonical_models: dict[str, StrictModel] = {
        "evidence": evidence,
        "precheck_book": books["precheck_book"],
        "execution_book": books["execution_book"],
        "research_brief": brief,
        "jev_decision": decision,
        "ai_arm": observation.ai,
        "deterministic_arm": observation.deterministic,
        "observation": observation,
    }
    for name, model in canonical_models.items():
        if _canonical_model_sha(model) != expected["canonical_sha256"][name]:
            raise ValueError(f"canonical hash mismatch: {name}")
    if (
        observation.research_brief_sha256
        != expected["canonical_sha256"]["research_brief"]
    ):
        raise ValueError("observation research brief hash mismatch")
    if observation.jev_decision_sha256 != expected["canonical_sha256"]["jev_decision"]:
        raise ValueError("observation Jev decision hash mismatch")

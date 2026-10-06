"""Strict, paper-only contracts for the Polymarket feasibility slice."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Micros = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
SignedInt = Annotated[int, Field(strict=True)]
Action = Literal["BUY_YES", "BUY_NO", "ABSTAIN"]


class StrictModel(BaseModel):
    """Common closed-world validation for persisted feasibility contracts."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


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
    """One arm's isolated signal, paper execution, economics, and forecast."""

    arm: Literal["AI", "DETERMINISTIC"]
    signal_probability_micros: Micros | None
    signal_action: Action | None
    disposition: Literal[
        "FILLED",
        "PARTIAL_FILL",
        "ABSTAIN",
        "NO_EDGE",
        "INELIGIBLE",
        "MODEL_FAILURE",
        "BOOK_FAILURE",
    ]
    entry_side: Literal["YES", "NO"] | None
    entry_vwap_micros: Micros | None
    quantity: NonNegativeInt
    fees_microdollars: NonNegativeInt
    initial_cash_microdollars: NonNegativeInt
    final_cash_microdollars: NonNegativeInt
    mark_micros: Micros | None
    payout: Literal[0, 1] | None
    trading_pnl_microdollars: SignedInt
    economic_pnl_microdollars: SignedInt
    model_cost_microdollars: NonNegativeInt
    forecast_probability_micros: Micros | None
    forecast_status: Literal["SCORED", "PENDING_RESOLUTION", "UNAVAILABLE"]
    brier_micros: Micros | None

    @model_validator(mode="after")
    def validate_paired_fields(self) -> Self:
        has_entry = self.quantity > 0
        if has_entry != (self.entry_side is not None):
            raise ValueError("positive quantity and entry side must occur together")
        if has_entry != (self.entry_vwap_micros is not None):
            raise ValueError("positive quantity and entry VWAP must occur together")
        if not has_entry and self.fees_microdollars != 0:
            raise ValueError("an arm without an entry cannot have execution fees")
        if self.signal_action == "ABSTAIN" and has_entry:
            raise ValueError("ABSTAIN cannot carry an entry side or quantity")
        if self.signal_action == "BUY_YES" and has_entry and self.entry_side != "YES":
            raise ValueError("BUY_YES entry side must be YES")
        if self.signal_action == "BUY_NO" and has_entry and self.entry_side != "NO":
            raise ValueError("BUY_NO entry side must be NO")
        if self.disposition == "ABSTAIN" and (
            self.signal_action != "ABSTAIN" or has_entry
        ):
            raise ValueError(
                "ABSTAIN disposition requires an entry-free ABSTAIN signal"
            )
        if self.disposition in {"FILLED", "PARTIAL_FILL"} and not has_entry:
            raise ValueError("fill disposition requires a positive entry")
        if has_entry and self.disposition not in {"FILLED", "PARTIAL_FILL"}:
            raise ValueError("positive entry requires a fill disposition")
        scored = self.forecast_status == "SCORED"
        if scored != (self.brier_micros is not None):
            raise ValueError("SCORED and Brier value must occur together")
        if scored and (self.forecast_probability_micros is None or self.payout is None):
            raise ValueError("scored forecast requires probability and payout")
        if not scored and self.brier_micros is not None:
            raise ValueError("unscored forecast cannot carry a Brier value")
        return self


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
        if (
            self.ai.payout != self.resolution
            or self.deterministic.payout != self.resolution
        ):
            raise ValueError("both arms must share the observation resolution")
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
        else:
            expected_baseline = (
                self.baseline_probability_micros - self.resolution * 1_000_000
            ) ** 2 // 1_000_000
            if self.baseline_brier_micros != expected_baseline:
                raise ValueError("baseline Brier value does not match resolution")
            if self.ai.brier_micros is None or self.deterministic.brier_micros is None:
                raise ValueError("resolved observation requires both arm Brier values")
            if self.paired_brier_delta_micros != (
                self.ai.brier_micros - self.deterministic.brier_micros
            ):
                raise ValueError("paired Brier delta must be AI minus deterministic")
        return self

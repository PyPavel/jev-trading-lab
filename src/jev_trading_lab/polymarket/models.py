"""Strict, paper-only contracts for the Polymarket feasibility slice."""

from __future__ import annotations

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
    def model_validate_json_strict(cls, raw: str | bytes | bytearray) -> Self:
        """Validate duplicate-safe JSON without changing Pydantic globally."""

        duplicate_safe = json.dumps(load_strict_json(raw), separators=(",", ":"))
        return cls.model_validate_json(duplicate_safe)


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
            valuation = (
                self.payout_micros
                if self.payout_micros is not None
                else self.mark_micros
            )
            if valuation is None or self.entry_vwap_micros is None:
                raise ValueError("filled economics require a valuation and entry VWAP")
            expected_trading = (
                self.quantity * (valuation - self.entry_vwap_micros)
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
        scored = self.forecast_status == "SCORED"
        if scored != (self.brier_micros is not None):
            raise ValueError("SCORED and Brier value must occur together")
        if not scored:
            return
        if self.forecast_probability_micros is None:
            raise ValueError("scored forecast requires a probability")
        if self.payout_micros is not None:
            expected_brier = _brier_micros(
                self.forecast_probability_micros, self.payout_micros
            )
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
        else:
            payout_micros = self.resolution * MICROS_PER_UNIT
            expected_baseline = _brier_micros(
                self.baseline_probability_micros, payout_micros
            )
            if self.baseline_brier_micros != expected_baseline:
                raise ValueError("baseline Brier value does not match resolution")
            for arm in (self.ai, self.deterministic):
                if arm.forecast_probability_micros is None or arm.brier_micros is None:
                    raise ValueError(
                        "resolved observation requires both arm Brier values"
                    )
                if arm.brier_micros != _brier_micros(
                    arm.forecast_probability_micros, payout_micros
                ):
                    raise ValueError("arm Brier value does not match resolution")
            ai_brier = self.ai.brier_micros
            deterministic_brier = self.deterministic.brier_micros
            if ai_brier is None or deterministic_brier is None:
                raise ValueError("resolved observation requires both arm Brier values")
            if self.paired_brier_delta_micros != ai_brier - deterministic_brier:
                raise ValueError("paired Brier delta must be AI minus deterministic")
        return self

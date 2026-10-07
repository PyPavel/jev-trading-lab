"""Strict, paper-only contracts for the Polymarket feasibility slice."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
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
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
Micros = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0, le=INT64_MAX)]
PositiveInt = Annotated[int, Field(strict=True, ge=1, le=INT64_MAX)]
SignedInt = Annotated[int, Field(strict=True, ge=INT64_MIN, le=INT64_MAX)]
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


class OpenAIUsage(StrictModel):
    """Exact token counts returned by the canned OpenAI response."""

    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    total_tokens: NonNegativeInt

    @model_validator(mode="after")
    def require_exact_total(self) -> Self:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens plus output_tokens")
        return self


class OpenAIResponse(StrictModel):
    """Closed-world canned OpenAI response with billed cost metadata."""

    cost_microdollars: NonNegativeInt
    model: NonEmptyStr
    output: ResearchBrief
    usage: OpenAIUsage


class JevDecision(StrictModel):
    """Bounded Jev forecast and paper action, without execution arithmetic."""

    p_yes_micros: Micros
    action: Action
    confidence_micros: Micros
    sufficiency_micros: Micros
    ambiguity_micros: Micros
    rationale: Annotated[str, StringConstraints(min_length=1, max_length=500)]


class JevResponse(StrictModel):
    """Closed-world canned Jev response with billed cost metadata."""

    cost_microdollars: NonNegativeInt
    model: Literal["jev-1-2026-09-15"]
    result: JevDecision


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
        if self.disposition == "ABSTAIN" and signal == (None, "ABSTAIN"):
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
            if self.mark_micros is not None and self.payout_micros is not None:
                raise ValueError("an arm cannot carry both a mark and payout")
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
        probability = self.forecast_probability_micros
        if probability != self.signal_probability_micros:
            raise ValueError("forecast probability must equal signal probability")
        if (
            self.disposition == "ABSTAIN"
            and self.signal_action == "ABSTAIN"
            and probability is None
        ):
            if self.forecast_status != "UNAVAILABLE" or self.brier_micros is not None:
                raise ValueError(
                    "probability-free abstain requires UNAVAILABLE forecast"
                )
            return
        if probability is None:
            if self.forecast_status != "UNAVAILABLE" or self.brier_micros is not None:
                raise ValueError("unavailable signal requires UNAVAILABLE forecast")
            return
        if self.payout_micros is not None:
            expected_brier = _brier_micros(probability, self.payout_micros)
            if self.forecast_status != "SCORED" or self.brier_micros != expected_brier:
                raise ValueError("payout requires exact SCORED forecast")
            return
        if (
            self.forecast_status != "PENDING_RESOLUTION"
            or self.brier_micros is not None
        ):
            raise ValueError("unresolved available forecast must be PENDING_RESOLUTION")


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
            if arm.payout_micros is not None and arm.payout_micros != expected_payout:
                raise ValueError("arm payout must match observation resolution")
            if arm.quantity > 0 and arm.payout_micros != expected_payout:
                raise ValueError("filled arm payout must match observation resolution")
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
                        or arm.payout_micros is not None
                    ):
                        raise ValueError("unavailable resolved arm cannot be scored")
                    continue
                if arm.payout_micros != payout_micros:
                    raise ValueError(
                        "available resolved arm payout must match resolution"
                    )
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


class CausalHistoryPoint(UtcModel):
    """One strictly pre-cutoff midpoint used by the deterministic control."""

    midpoint_micros: Micros
    observed_at: datetime


class Economics(StrictModel):
    """Frozen Unit 1 policy inputs."""

    entry_fee_pp_micros: Micros
    exit_fee_pp_micros: Micros
    exit_slippage_pp_micros: Micros
    sigma_p_micros: Micros
    starting_cash_microdollars: NonNegativeInt


class RawArtifacts(StrictModel):
    gamma_bytes: str
    precheck_book_bytes: str
    execution_book_bytes: str


class ModelArtifacts(StrictModel):
    openai_request_bytes: str
    openai_response_bytes: str
    jev_request_bytes: str
    jev_response_bytes: str


class ModelCosts(StrictModel):
    openai: NonNegativeInt
    jev: NonNegativeInt


class NormalizedTokens(StrictModel):
    """Gamma YES/NO token identity after deterministic normalization."""

    yes_token_id: NonEmptyStr
    no_token_id: NonEmptyStr

    @model_validator(mode="after")
    def require_distinct_tokens(self) -> Self:
        if self.yes_token_id == self.no_token_id:
            raise ValueError("YES and NO token IDs must differ")
        return self


class FixtureChronology(UtcModel):
    """Declared model and decision-freeze event times for Unit 1 binding."""

    openai_request_at: datetime
    openai_response_at: datetime
    jev_request_at: datetime
    jev_response_at: datetime
    decision_frozen_at: datetime

    @model_validator(mode="after")
    def require_strict_stage_order(self) -> Self:
        stages = (
            self.openai_request_at,
            self.openai_response_at,
            self.jev_request_at,
            self.jev_response_at,
            self.decision_frozen_at,
        )
        if any(left >= right for left, right in zip(stages, stages[1:])):
            raise ValueError("model chronology must be strictly increasing")
        return self


class Resolution(UtcModel):
    payout: Literal[0, 1]
    resolved_at: datetime


class ArtifactHashes(StrictModel):
    gamma_bytes: Sha256
    precheck_book_bytes: Sha256
    execution_book_bytes: Sha256
    openai_request_bytes: Sha256
    openai_response_bytes: Sha256
    jev_request_bytes: Sha256
    jev_response_bytes: Sha256


class CanonicalHashes(StrictModel):
    evidence: Sha256
    precheck_book: Sha256
    execution_book: Sha256
    research_brief: Sha256
    jev_decision: Sha256
    ai_arm: Sha256
    deterministic_arm: Sha256
    observation: Sha256


class ExpectedArms(StrictModel):
    ai: ArmResult
    deterministic: ArmResult


class FixtureExpected(StrictModel):
    artifact_sha256: ArtifactHashes
    canonical_sha256: CanonicalHashes
    arm_results: ExpectedArms
    observation_result: ObservationResult


class FeasibilityFixture(StrictModel):
    """Closed-world Unit 1 canned cycle envelope."""

    schema_version: Literal[1]
    raw: RawArtifacts
    sanitized_evidence: EvidencePacket
    causal_history: Annotated[list[CausalHistoryPoint], Field(min_length=1)]
    normalized_tokens: NormalizedTokens
    precheck_book: BookSnapshot
    execution_book: BookSnapshot
    chronology: FixtureChronology
    model_artifacts: ModelArtifacts
    research_brief: ResearchBrief
    jev_decision: JevDecision
    model_costs_microdollars: ModelCosts
    economics: Economics
    resolution: Resolution
    expected: FixtureExpected


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _canonical_model_sha(model: StrictModel) -> str:
    return hashlib.sha256(
        canonical_json_bytes(model.model_dump(mode="json"))
    ).hexdigest()


def _opportunity_id(*, policy_version: str, market_id: str, cutoff_at: str) -> str:
    """Hash canonical JSON of policy version, market ID, and UTC cutoff."""

    return hashlib.sha256(
        canonical_json_bytes(
            {
                "policy_version": policy_version,
                "market_id": market_id,
                "cutoff_at": cutoff_at,
            }
        )
    ).hexdigest()


def _observation_id(
    *,
    opportunity_id: str,
    evidence_sha256: str,
    execution_book_sha256: str,
    policy_version: str,
) -> str:
    """Hash canonical JSON of opportunity, evidence, book, and policy identity."""

    return hashlib.sha256(
        canonical_json_bytes(
            {
                "opportunity_id": opportunity_id,
                "evidence_sha256": evidence_sha256,
                "execution_book_sha256": execution_book_sha256,
                "policy_version": policy_version,
            }
        )
    ).hexdigest()


def _require_exact_keys(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} must contain exactly {sorted(keys)}")
    return value


def _raw_book_snapshot(raw: str, source_sha256: str) -> BookSnapshot:
    payload = _require_exact_keys(
        load_strict_json(raw),
        {"asks", "asset_id", "bids", "market", "timestamp"},
        "raw book",
    )

    def levels(side: str) -> list[dict[str, int]]:
        raw_levels = payload[side]
        if not isinstance(raw_levels, list) or not raw_levels:
            raise ValueError(f"raw book {side} must be a nonempty list")
        normalized: list[dict[str, int]] = []
        for raw_level in raw_levels:
            level = _require_exact_keys(raw_level, {"price", "size"}, "raw level")
            try:
                price = Decimal(level["price"])
                size = Decimal(level["size"])
            except (InvalidOperation, TypeError) as error:
                raise ValueError(
                    "raw book level must contain decimal strings"
                ) from error
            price_micros = price * MICROS_PER_UNIT
            if (
                price_micros != price_micros.to_integral_value()
                or size != size.to_integral_value()
            ):
                raise ValueError("raw book values must map exactly to integers")
            normalized.append(
                {"price_micros": int(price_micros), "quantity": int(size)}
            )
        return normalized

    return BookSnapshot.model_validate_json(
        json.dumps(
            {
                "market_id": payload["market"],
                "token_id": payload["asset_id"],
                "retrieved_at": payload["timestamp"],
                "source_sha256": source_sha256,
                "bids": levels("bids"),
                "asks": levels("asks"),
            }
        )
    )


def _reject_price_keys(value: object, label: str) -> None:
    forbidden = {
        "outcomePrices",
        "bestBid",
        "token_id",
        "bids",
        "asks",
        "midpoint",
        "price",
        "book",
    }
    if isinstance(value, dict):
        leaked = forbidden.intersection(value)
        if leaked:
            raise ValueError(f"{label} contains price/book fields: {sorted(leaked)}")
        for nested in value.values():
            _reject_price_keys(nested, label)
    elif isinstance(value, list):
        for nested in value:
            _reject_price_keys(nested, label)


def validate_feasibility_fixture(fixture: dict[str, Any]) -> None:
    """Validate the complete Unit 1 envelope and cross-artifact hash chain."""

    validated = FeasibilityFixture.model_validate_json(json.dumps(fixture))
    raw = validated.raw
    artifacts = validated.model_artifacts
    expected = validated.expected

    artifact_values = {**raw.model_dump(), **artifacts.model_dump()}
    expected_artifacts = expected.artifact_sha256.model_dump()
    for name, value in artifact_values.items():
        if _sha256_text(value) != expected_artifacts[name]:
            raise ValueError(f"artifact hash mismatch: {name}")

    gamma_hash = _sha256_text(raw.gamma_bytes)
    evidence = validated.sanitized_evidence
    brief = validated.research_brief
    observation = expected.observation_result
    citation_hashes = {citation.evidence_sha256 for citation in brief.cited_facts}
    if not (
        evidence.source_sha256
        == brief.evidence_sha256
        == observation.evidence_sha256
        == expected_artifacts["gamma_bytes"]
        == gamma_hash
        and citation_hashes == {gamma_hash}
    ):
        raise ValueError("Gamma hash does not match evidence bindings")

    gamma = _require_exact_keys(
        load_strict_json(raw.gamma_bytes),
        {
            "category",
            "clobTokenIds",
            "description",
            "endDate",
            "id",
            "outcomePrices",
            "question",
            "resolutionSource",
            "rules",
            "tags",
        },
        "Gamma record",
    )
    gamma_evidence = {
        "market_id": gamma["id"],
        "question": gamma["question"],
        "description": gamma["description"],
        "rules": gamma["rules"],
        "category": gamma["category"],
        "tags": gamma["tags"],
        "resolution_source_text": gamma["resolutionSource"],
        "closes_at": gamma["endDate"],
    }
    evidence_dump = evidence.model_dump(mode="json")
    if any(evidence_dump[key] != value for key, value in gamma_evidence.items()):
        raise ValueError("sanitized evidence does not match raw Gamma record")
    tokens = validated.normalized_tokens
    if gamma["clobTokenIds"] != [tokens.yes_token_id, tokens.no_token_id]:
        raise ValueError("normalized token identity does not match Gamma record")

    books = {
        "precheck_book": validated.precheck_book,
        "execution_book": validated.execution_book,
    }
    if any(book.market_id != evidence.market_id for book in books.values()) or (
        observation.market_id != evidence.market_id
    ):
        raise ValueError("market identity does not match evidence")
    if observation.cutoff_at != evidence.cutoff_at:
        raise ValueError("observation cutoff does not match evidence cutoff")
    expected_opportunity_id = _opportunity_id(
        policy_version=observation.policy_version,
        market_id=observation.market_id,
        cutoff_at=observation.cutoff_at.isoformat().replace("+00:00", "Z"),
    )
    if observation.opportunity_id != expected_opportunity_id:
        raise ValueError("opportunity_id does not match structural identity")
    expected_observation_id = _observation_id(
        opportunity_id=observation.opportunity_id,
        evidence_sha256=observation.evidence_sha256,
        execution_book_sha256=observation.execution_book_sha256,
        policy_version=observation.policy_version,
    )
    if observation.observation_id != expected_observation_id:
        raise ValueError("observation_id does not match structural identity")
    if any(book.token_id != tokens.yes_token_id for book in books.values()):
        raise ValueError("book token identity does not match normalized YES token")
    for name, book in books.items():
        raw_name = f"{name}_bytes"
        raw_hash = expected_artifacts[raw_name]
        parsed = _raw_book_snapshot(getattr(raw, raw_name), raw_hash)
        if book != parsed:
            raise ValueError(f"{name} does not match parsed raw bytes")
    if observation.execution_book_sha256 != expected_artifacts["execution_book_bytes"]:
        raise ValueError("observation execution book hash does not match raw bytes")

    canonical_evidence = canonical_json_bytes(evidence.model_dump(mode="json")).decode()
    openai_request = _require_exact_keys(
        load_strict_json(artifacts.openai_request_bytes),
        {"input", "model", "response_format"},
        "OpenAI request",
    )
    _reject_price_keys(openai_request, "OpenAI request")
    if openai_request != {
        "input": f"<evidence>{canonical_evidence}</evidence>",
        "model": "gpt-5-2026-09-30",
        "response_format": "strict-research-brief-v1",
    }:
        raise ValueError("OpenAI request does not match fixture evidence contract")

    openai_response = OpenAIResponse.model_validate_json(
        artifacts.openai_response_bytes
    )
    if (
        openai_response.model != openai_request["model"]
        or openai_response.output != brief
        or openai_response.cost_microdollars
        != validated.model_costs_microdollars.openai
    ):
        raise ValueError("OpenAI response does not match brief or billed cost")

    brief_hash = _canonical_model_sha(brief)
    jev_request = _require_exact_keys(
        load_strict_json(artifacts.jev_request_bytes),
        {"model", "questions", "state"},
        "Jev request",
    )
    _reject_price_keys(jev_request, "Jev request")
    if jev_request != {
        "model": "jev-1-2026-09-15",
        "questions": [
            "evidence_sufficiency",
            "ambiguity",
            "p_yes",
            "action",
            "confidence",
        ],
        "state": {
            "brief": brief.model_dump(mode="json"),
            "evidence_sha256": evidence.source_sha256,
            "research_brief_sha256": brief_hash,
        },
    }:
        raise ValueError("Jev request does not match fixture brief contract")

    jev_response = JevResponse.model_validate_json(artifacts.jev_response_bytes)
    if (
        jev_response.model != jev_request["model"]
        or jev_response.result != validated.jev_decision
        or jev_response.cost_microdollars != validated.model_costs_microdollars.jev
    ):
        raise ValueError("Jev response does not match decision or billed cost")

    model_cost = openai_response.cost_microdollars + jev_response.cost_microdollars
    if expected.arm_results.ai.model_cost_microdollars != model_cost:
        raise ValueError("AI arm model cost does not match provider costs")
    if expected.arm_results.deterministic.model_cost_microdollars != 0:
        raise ValueError("deterministic arm model cost must be zero")

    starting_cash = validated.economics.starting_cash_microdollars
    arms = expected.arm_results
    if any(
        arm.initial_cash_microdollars != starting_cash
        for arm in (
            arms.ai,
            arms.deterministic,
            observation.ai,
            observation.deterministic,
        )
    ):
        raise ValueError("starting capital does not match expected arms")
    if arms.ai != observation.ai or arms.deterministic != observation.deterministic:
        raise ValueError("expected arm results do not match observation arms")

    execution_retrieved_at = books["execution_book"].retrieved_at
    if observation.observed_at < execution_retrieved_at:
        raise ValueError("observation cannot precede execution book retrieval")

    resolution = validated.resolution
    if resolution.resolved_at <= execution_retrieved_at:
        raise ValueError("resolution must follow execution book retrieval")
    if (
        observation.resolution != resolution.payout
        or observation.observed_at != resolution.resolved_at
    ):
        raise ValueError("resolution does not match observation")
    if resolution.resolved_at < evidence.closes_at:
        raise ValueError("resolution cannot precede market close")

    cutoff = evidence.cutoff_at
    history_times = [point.observed_at for point in validated.causal_history]
    if any(point_at >= cutoff for point_at in history_times):
        raise ValueError("causal history timestamps must precede cutoff")
    if any(left >= right for left, right in zip(history_times, history_times[1:])):
        raise ValueError("causal history timestamps must be strictly increasing")

    chronology = validated.chronology
    if chronology.openai_request_at <= cutoff:
        raise ValueError("OpenAI request must follow cutoff")
    if chronology.decision_frozen_at >= books["execution_book"].retrieved_at:
        raise ValueError("decision must freeze before execution book retrieval")
    if books["precheck_book"].retrieved_at > cutoff:
        raise ValueError("precheck book must not follow cutoff")

    decision = validated.jev_decision
    if (
        arms.ai.signal_probability_micros != decision.p_yes_micros
        or arms.ai.signal_action != decision.action
    ):
        raise ValueError("AI arm signal does not match Jev decision")

    canonical_models: dict[str, StrictModel] = {
        "evidence": evidence,
        "precheck_book": books["precheck_book"],
        "execution_book": books["execution_book"],
        "research_brief": brief,
        "jev_decision": validated.jev_decision,
        "ai_arm": arms.ai,
        "deterministic_arm": arms.deterministic,
        "observation": observation,
    }
    expected_canonical = expected.canonical_sha256.model_dump()
    for name, model in canonical_models.items():
        if _canonical_model_sha(model) != expected_canonical[name]:
            raise ValueError(f"canonical hash mismatch: {name}")
    if observation.research_brief_sha256 != brief_hash:
        raise ValueError("observation research brief hash mismatch")
    decision_hash = _canonical_model_sha(validated.jev_decision)
    if observation.jev_decision_sha256 != decision_hash:
        raise ValueError("observation Jev decision hash mismatch")

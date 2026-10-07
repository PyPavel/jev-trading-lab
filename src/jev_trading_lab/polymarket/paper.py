"""Deterministic paper execution and scoring for one frozen YES-token book."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.polymarket.control import control_probability
from jev_trading_lab.polymarket.models import (
    INT64_MAX,
    ArmResult,
    BookLevel,
    BookSnapshot,
    Economics,
    FeasibilityFixture,
    JevDecision,
    ObservationResult,
    ResearchBrief,
    validate_feasibility_fixture,
)

MICROS = 1_000_000
STARTING_CASH_MICRODOLLARS = 10_000_000_000
POLICY_VERSION = "polymarket-feasibility-v1"
Side = Literal["YES", "NO"]
Liquidity = Literal["ASK", "BID"]


@dataclass(frozen=True, slots=True)
class Quote:
    """Integer book walk; VWAP rounds upward for entries and downward for marks."""

    requested_quantity: int
    filled_quantity: int
    captured_quantity: int
    notional_microdollars: int
    vwap_micros: int | None


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def _levels(book: BookSnapshot, side: Side, liquidity: Liquidity) -> list[BookLevel]:
    if (side, liquidity) in {("YES", "ASK"), ("NO", "BID")}:
        source = book.asks
    else:
        source = book.bids
    if side == "YES":
        return list(source)
    return [
        BookLevel(price_micros=MICROS - level.price_micros, quantity=level.quantity)
        for level in source
    ]


def quote_side(
    book: BookSnapshot, side: Side, quantity: int, *, liquidity: Liquidity
) -> Quote:
    """Walk best-first levels; executable-bid mark values uncovered units at zero."""

    if type(quantity) is not int or quantity < 0:
        raise ValueError("quantity must be a non-negative integer")
    levels = _levels(book, side, liquidity)
    captured = sum(level.quantity for level in levels)
    remaining = quantity
    notional = 0
    for level in levels:
        filled = min(remaining, level.quantity)
        notional += filled * level.price_micros
        remaining -= filled
        if remaining == 0:
            break
    filled_quantity = quantity - remaining
    denominator = quantity if liquidity == "BID" else filled_quantity
    if denominator == 0:
        vwap = None
    elif liquidity == "ASK":
        # Entry cost rounds against the arm; it can never understate funded notional.
        vwap = _ceil_div(notional, denominator)
    else:
        # Mark value rounds against the arm, including uncaptured quantity at zero.
        vwap = notional // denominator
    return Quote(quantity, filled_quantity, captured, notional, vwap)


def midpoint_micros(book: BookSnapshot) -> int:
    """Floor the shared YES best-bid/best-ask midpoint deterministically."""

    return (book.bids[0].price_micros + book.asks[0].price_micros) // 2


def brier_micros(probability_micros: int, resolution_micros: int) -> int:
    """Floor squared probability error to the Unit 1 millionths contract."""

    return (probability_micros - resolution_micros) ** 2 // MICROS


def gross_edge_micros(
    probability_micros: int, side: Side, entry_vwap_micros: int
) -> int:
    held_probability = (
        probability_micros if side == "YES" else MICROS - probability_micros
    )
    return held_probability - entry_vwap_micros


def edge_threshold_micros(policy: Economics) -> int:
    round_trip_cost = (
        policy.entry_fee_pp_micros
        + policy.exit_fee_pp_micros
        + policy.exit_slippage_pp_micros
    )
    return max(50_000, 2 * round_trip_cost + 30_000)


def fee_microdollars(quantity: int, fee_pp_micros: int) -> int:
    """Return the per-contract probability-point fee in microdollars."""

    if (
        type(quantity) is not int
        or type(fee_pp_micros) is not int
        or not 0 <= quantity <= INT64_MAX
        or not 0 <= fee_pp_micros <= MICROS
        or quantity > INT64_MAX // max(fee_pp_micros, 1)
    ):
        raise ValueError("fee inputs are outside their integer domains")
    return quantity * fee_pp_micros


def _volatility_cap(equity_microdollars: int, sigma_p_micros: int) -> int:
    # floor(0.005 * equity-dollars / max(sigma_p, 0.05)) contracts.
    return equity_microdollars * 5_000 // (max(sigma_p_micros, 50_000) * MICROS)


def _notional_cap(equity_microdollars: int, price_micros: int) -> int:
    # floor(0.10 * equity-dollars / token-price) contracts.
    return equity_microdollars * 100_000 // (price_micros * MICROS)


def _cash_cap(cash_microdollars: int, price_micros: int, fee_pp_micros: int) -> int:
    cost_per_contract = price_micros + fee_pp_micros
    if cost_per_contract == 0:
        return INT64_MAX
    return cash_microdollars // cost_per_contract


def sizing_caps(
    book: BookSnapshot,
    side: Side,
    policy: Economics,
    *,
    equity_microdollars: int | None = None,
    available_cash_microdollars: int | None = None,
) -> dict[str, int]:
    """Return each independently floored contract cap for audit and testing."""

    equity = (
        policy.starting_cash_microdollars
        if equity_microdollars is None
        else equity_microdollars
    )
    cash = (
        policy.starting_cash_microdollars
        if available_cash_microdollars is None
        else available_cash_microdollars
    )
    if equity < 0 or cash < 0:
        raise ValueError("equity and available cash must be non-negative")
    levels = _levels(book, side, "ASK")
    depth = sum(level.quantity for level in levels)
    best_price = levels[0].price_micros
    return {
        "volatility": _volatility_cap(equity, policy.sigma_p_micros),
        "notional": depth if best_price == 0 else _notional_cap(equity, best_price),
        "depth": depth * 50_000 // MICROS,
        "cash": min(
            depth,
            _cash_cap(cash, best_price, policy.entry_fee_pp_micros),
        ),
    }


def size_quantity(
    book: BookSnapshot,
    side: Side,
    probability_micros: int,
    policy: Economics,
    *,
    equity_microdollars: int | None = None,
    available_cash_microdollars: int | None = None,
) -> int:
    """Search integer quantity downward until edge and every frozen cap pass."""

    equity = (
        policy.starting_cash_microdollars
        if equity_microdollars is None
        else equity_microdollars
    )
    cash = (
        policy.starting_cash_microdollars
        if available_cash_microdollars is None
        else available_cash_microdollars
    )
    caps = sizing_caps(
        book,
        side,
        policy,
        equity_microdollars=equity,
        available_cash_microdollars=cash,
    )
    upper = min(caps.values())
    threshold = edge_threshold_micros(policy)

    def passes(quantity: int) -> bool:
        quote = quote_side(book, side, quantity, liquidity="ASK")
        if quote.filled_quantity != quantity or quote.vwap_micros is None:
            return False
        if quantity > _notional_cap(equity, quote.vwap_micros):
            return False
        entry_fee = fee_microdollars(quantity, policy.entry_fee_pp_micros)
        if quote.notional_microdollars + entry_fee > cash:
            return False
        return (
            gross_edge_micros(probability_micros, side, quote.vwap_micros) >= threshold
        )

    if upper == 0 or not passes(1):
        return 0
    lower = 1
    while lower < upper:
        candidate = (lower + upper + 1) // 2
        if passes(candidate):
            lower = candidate
        else:
            upper = candidate - 1
    return lower


def _preferred_side(book: BookSnapshot, probability_micros: int) -> Side:
    yes_quote = quote_side(book, "YES", 1, liquidity="ASK")
    no_quote = quote_side(book, "NO", 1, liquidity="ASK")
    yes_edge = gross_edge_micros(probability_micros, "YES", yes_quote.vwap_micros or 0)
    no_edge = gross_edge_micros(probability_micros, "NO", no_quote.vwap_micros or 0)
    return "YES" if yes_edge >= no_edge else "NO"


def _arm(
    *,
    arm: Literal["AI", "DETERMINISTIC"],
    probability_micros: int | None,
    action: Literal["BUY_YES", "BUY_NO", "ABSTAIN"] | None,
    disposition: Literal[
        "FILLED", "PARTIAL_FILL", "ABSTAIN", "NO_EDGE", "MODEL_FAILURE"
    ],
    book: BookSnapshot,
    policy: Economics,
    resolution: Literal[0, 1] | None,
    model_cost_microdollars: int,
) -> ArmResult:
    payout: Literal[0, 1_000_000] | None
    if resolution is None:
        payout = None
    elif resolution == 0:
        payout = 0
    else:
        payout = 1_000_000
    if disposition in {"ABSTAIN", "MODEL_FAILURE", "NO_EDGE"}:
        forecast_status: Literal["SCORED", "PENDING_RESOLUTION", "UNAVAILABLE"] = (
            "UNAVAILABLE"
            if probability_micros is None
            else ("SCORED" if payout is not None else "PENDING_RESOLUTION")
        )
        score = (
            brier_micros(probability_micros, payout)
            if probability_micros is not None and payout is not None
            else None
        )
        return ArmResult(
            arm=arm,
            signal_probability_micros=probability_micros,
            signal_action=action,
            disposition=disposition,
            entry_side=None,
            entry_vwap_micros=None,
            quantity=0,
            fees_microdollars=0,
            initial_cash_microdollars=policy.starting_cash_microdollars,
            final_cash_microdollars=policy.starting_cash_microdollars,
            mark_micros=None,
            payout_micros=payout if probability_micros is not None else None,
            trading_pnl_microdollars=0,
            economic_pnl_microdollars=-model_cost_microdollars,
            model_cost_microdollars=model_cost_microdollars,
            forecast_probability_micros=probability_micros,
            forecast_status=forecast_status,
            brier_micros=score,
        )

    if action not in {"BUY_YES", "BUY_NO"} or probability_micros is None:
        raise ValueError("filled arm requires a directional signal")
    side: Side = "YES" if action == "BUY_YES" else "NO"
    quantity = size_quantity(book, side, probability_micros, policy)
    if quantity == 0:
        return _arm(
            arm=arm,
            probability_micros=probability_micros,
            action=action,
            disposition="NO_EDGE",
            book=book,
            policy=policy,
            resolution=resolution,
            model_cost_microdollars=model_cost_microdollars,
        )
    entry = quote_side(book, side, quantity, liquidity="ASK")
    if entry.vwap_micros is None:
        raise ValueError("positive sized quantity requires an entry VWAP")
    entry_fee = fee_microdollars(quantity, policy.entry_fee_pp_micros)
    if payout is None:
        mark_quote = quote_side(book, side, quantity, liquidity="BID")
        held_valuation = mark_quote.vwap_micros or 0
        market_yes_mark = held_valuation if side == "YES" else MICROS - held_valuation
        exit_fee = fee_microdollars(quantity, policy.exit_fee_pp_micros)
    else:
        market_yes_mark = None
        held_valuation = payout if side == "YES" else MICROS - payout
        exit_fee = fee_microdollars(quantity, policy.exit_fee_pp_micros)
    fees = entry_fee + exit_fee
    trading_pnl = quantity * (held_valuation - entry.vwap_micros) - fees
    score = brier_micros(probability_micros, payout) if payout is not None else None
    return ArmResult(
        arm=arm,
        signal_probability_micros=probability_micros,
        signal_action=action,
        disposition="FILLED",
        entry_side=side,
        entry_vwap_micros=entry.vwap_micros,
        quantity=quantity,
        fees_microdollars=fees,
        initial_cash_microdollars=policy.starting_cash_microdollars,
        final_cash_microdollars=policy.starting_cash_microdollars + trading_pnl,
        mark_micros=market_yes_mark,
        payout_micros=payout,
        trading_pnl_microdollars=trading_pnl,
        economic_pnl_microdollars=trading_pnl - model_cost_microdollars,
        model_cost_microdollars=model_cost_microdollars,
        forecast_probability_micros=probability_micros,
        forecast_status="SCORED" if payout is not None else "PENDING_RESOLUTION",
        brier_micros=score,
    )


def evaluate_ai_arm(
    book: BookSnapshot,
    decision: JevDecision | None,
    policy: Economics,
    *,
    resolution: Literal[0, 1] | None,
    model_cost_microdollars: int,
) -> ArmResult:
    """Gate the frozen decision, then evaluate only its requested side."""

    if decision is None:
        return _arm(
            arm="AI",
            probability_micros=None,
            action=None,
            disposition="MODEL_FAILURE",
            book=book,
            policy=policy,
            resolution=resolution,
            model_cost_microdollars=model_cost_microdollars,
        )
    gated = (
        decision.action == "ABSTAIN"
        or decision.sufficiency_micros < 700_000
        or decision.ambiguity_micros > 300_000
    )
    return _arm(
        arm="AI",
        probability_micros=decision.p_yes_micros,
        action="ABSTAIN" if gated else decision.action,
        disposition="ABSTAIN" if gated else "FILLED",
        book=book,
        policy=policy,
        resolution=resolution,
        model_cost_microdollars=model_cost_microdollars,
    )


def evaluate_control_arm(
    fixture: FeasibilityFixture, *, resolution: Literal[0, 1] | None
) -> ArmResult:
    probability = control_probability(
        fixture.causal_history, cutoff_at=fixture.sanitized_evidence.cutoff_at
    )
    if probability is None:
        return _arm(
            arm="DETERMINISTIC",
            probability_micros=None,
            action="ABSTAIN",
            disposition="ABSTAIN",
            book=fixture.execution_book,
            policy=fixture.economics,
            resolution=resolution,
            model_cost_microdollars=0,
        )
    side = _preferred_side(fixture.execution_book, probability)
    action: Literal["BUY_YES", "BUY_NO"] = "BUY_YES" if side == "YES" else "BUY_NO"
    return _arm(
        arm="DETERMINISTIC",
        probability_micros=probability,
        action=action,
        disposition="FILLED",
        book=fixture.execution_book,
        policy=fixture.economics,
        resolution=resolution,
        model_cost_microdollars=0,
    )


def _model_sha(model: ResearchBrief | JevDecision) -> str:
    return hashlib.sha256(
        canonical_json_bytes(model.model_dump(mode="json"))
    ).hexdigest()


def _identity_sha(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def evaluate_fixture(fixture: FeasibilityFixture) -> ObservationResult:
    """Recompute the approved fixture result exclusively from frozen source inputs."""

    resolution = fixture.resolution.payout
    model_cost = (
        fixture.model_costs_microdollars.openai + fixture.model_costs_microdollars.jev
    )
    ai = evaluate_ai_arm(
        fixture.execution_book,
        fixture.jev_decision,
        fixture.economics,
        resolution=resolution,
        model_cost_microdollars=model_cost,
    )
    deterministic = evaluate_control_arm(fixture, resolution=resolution)
    evidence = fixture.sanitized_evidence
    cutoff_text = evidence.cutoff_at.isoformat().replace("+00:00", "Z")
    opportunity_id = _identity_sha(
        {
            "policy_version": POLICY_VERSION,
            "market_id": evidence.market_id,
            "cutoff_at": cutoff_text,
        }
    )
    observation_id = _identity_sha(
        {
            "opportunity_id": opportunity_id,
            "evidence_sha256": evidence.source_sha256,
            "execution_book_sha256": fixture.execution_book.source_sha256,
            "policy_version": POLICY_VERSION,
        }
    )
    baseline = midpoint_micros(fixture.execution_book)
    payout = resolution * MICROS
    return ObservationResult(
        observation_id=observation_id,
        opportunity_id=opportunity_id,
        market_id=evidence.market_id,
        policy_version=POLICY_VERSION,
        cutoff_at=evidence.cutoff_at,
        observed_at=fixture.resolution.resolved_at,
        evidence_sha256=evidence.source_sha256,
        execution_book_sha256=fixture.execution_book.source_sha256,
        research_brief_sha256=_model_sha(fixture.research_brief),
        jev_decision_sha256=_model_sha(fixture.jev_decision),
        ai=ai,
        deterministic=deterministic,
        baseline_probability_micros=baseline,
        resolution=resolution,
        baseline_brier_micros=brier_micros(baseline, payout),
        paired_trading_pnl_delta_microdollars=(
            ai.trading_pnl_microdollars - deterministic.trading_pnl_microdollars
        ),
        paired_economic_pnl_delta_microdollars=(
            ai.economic_pnl_microdollars - deterministic.economic_pnl_microdollars
        ),
        paired_brier_delta_micros=(
            ai.brier_micros - deterministic.brier_micros
            if ai.brier_micros is not None and deterministic.brier_micros is not None
            else None
        ),
    )


def validate_unit2_fixture(raw_fixture: dict[str, object]) -> None:
    """Reject structurally valid fixtures whose expected economics are detached."""

    validate_feasibility_fixture(raw_fixture)
    fixture = FeasibilityFixture.model_validate_json(json.dumps(raw_fixture))
    actual = evaluate_fixture(fixture)
    if (
        fixture.expected.arm_results.ai != actual.ai
        or (fixture.expected.arm_results.deterministic != actual.deterministic)
        or fixture.expected.observation_result != actual
    ):
        raise ValueError("Unit 2 expected results mismatch")

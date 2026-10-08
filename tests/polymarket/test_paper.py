from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.polymarket import paper
from jev_trading_lab.polymarket.models import (
    INT64_MAX,
    ArmResult,
    BookSnapshot,
    CausalHistoryPoint,
    Economics,
    FeasibilityFixture,
    JevDecision,
    ObservationResult,
    load_strict_json,
)
from jev_trading_lab.polymarket.paper import (
    STARTING_CASH_MICRODOLLARS,
    brier_micros,
    edge_threshold_micros,
    evaluate_ai_arm,
    evaluate_control_arm,
    evaluate_fixture,
    fee_microdollars,
    gross_edge_micros,
    midpoint_micros,
    quote_side,
    size_quantity,
    sizing_caps,
    validate_unit2_fixture,
)

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures/polymarket/feasibility_cycle.json"
NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
HASH = "0" * 64


def book(
    *,
    bids: list[tuple[int, int]],
    asks: list[tuple[int, int]],
) -> BookSnapshot:
    return BookSnapshot.model_validate(
        {
            "market_id": "m",
            "token_id": "yes",
            "retrieved_at": NOW,
            "source_sha256": HASH,
            "bids": [
                {"price_micros": price, "quantity": quantity}
                for price, quantity in bids
            ],
            "asks": [
                {"price_micros": price, "quantity": quantity}
                for price, quantity in asks
            ],
        }
    )


def economics(
    *,
    entry_fee: int = 10_000,
    exit_fee: int = 0,
    slippage: int = 10_000,
    sigma: int = 100_000,
    cash: int = STARTING_CASH_MICRODOLLARS,
) -> Economics:
    return Economics(
        entry_fee_pp_micros=entry_fee,
        exit_fee_pp_micros=exit_fee,
        exit_slippage_pp_micros=slippage,
        sigma_p_micros=sigma,
        starting_cash_microdollars=cash,
    )


@pytest.fixture(scope="module")
def fixture_dict() -> dict[str, object]:
    loaded = load_strict_json(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture(scope="module")
def fixture(fixture_dict: dict[str, object]) -> FeasibilityFixture:
    import json

    return FeasibilityFixture.model_validate_json(json.dumps(fixture_dict))


def test_yes_and_no_asks_are_interpreted_from_one_yes_book() -> None:
    snapshot = book(
        bids=[(490_000, 7), (480_000, 11)], asks=[(540_000, 5), (550_000, 13)]
    )

    yes = quote_side(snapshot, "YES", 6, liquidity="ASK")
    no = quote_side(snapshot, "NO", 8, liquidity="ASK")

    assert (yes.filled_quantity, yes.notional_microdollars, yes.vwap_micros) == (
        6,
        3_250_000,
        541_667,
    )
    assert (no.filled_quantity, no.notional_microdollars, no.vwap_micros) == (
        8,
        4_090_000,
        511_250,
    )


def test_walk_is_deterministic_partial_and_never_overfills_depth() -> None:
    snapshot = book(bids=[(490_000, 2)], asks=[(540_000, 2), (550_000, 3)])

    fill = quote_side(snapshot, "YES", 9, liquidity="ASK")

    assert fill.requested_quantity == 9
    assert fill.filled_quantity == 5
    assert fill.captured_quantity == 5
    assert fill.vwap_micros == 546_000


def test_bid_mark_includes_zero_value_for_quantity_beyond_captured_depth() -> None:
    snapshot = book(bids=[(490_000, 2)], asks=[(540_000, 2)])

    yes = quote_side(snapshot, "YES", 5, liquidity="BID")
    no = quote_side(snapshot, "NO", 5, liquidity="BID")

    assert yes.vwap_micros == 196_000
    assert no.vwap_micros == 184_000


@pytest.mark.parametrize(
    ("probability", "side", "vwap", "expected"),
    (
        (700_000, "YES", 540_000, 160_000),
        (300_000, "NO", 540_000, 160_000),
        (300_000, "YES", 540_000, -240_000),
        (700_000, "NO", 540_000, -240_000),
    ),
)
def test_gross_edge_signs_for_both_sides(
    probability: int, side: str, vwap: int, expected: int
) -> None:
    assert gross_edge_micros(probability, side, vwap) == expected


def test_edge_threshold_uses_cost_round_trip_floor_and_accepts_equality() -> None:
    policy = economics(entry_fee=10_000, exit_fee=5_000, slippage=5_000)

    assert edge_threshold_micros(policy) == 70_000
    assert gross_edge_micros(610_000, "YES", 540_000) >= edge_threshold_micros(policy)


@pytest.mark.parametrize("entry_price_micros", (50_000, 950_000))
def test_fee_is_per_contract_probability_point_and_price_independent(
    entry_price_micros: int,
) -> None:
    quantity = 500
    fee_pp_micros = 10_000
    notional_microdollars = quantity * entry_price_micros

    assert fee_microdollars(quantity, fee_pp_micros) == 5_000_000
    assert fee_microdollars(quantity, fee_pp_micros) != (
        notional_microdollars * fee_pp_micros // 1_000_000
    )


@pytest.mark.parametrize(
    ("quantity", "fee_pp_micros"),
    ((True, 10_000), (500, True), (False, 0), (0, False)),
)
def test_fee_rejects_bool_inputs(quantity: object, fee_pp_micros: object) -> None:
    with pytest.raises(ValueError, match="integer domains"):
        fee_microdollars(quantity, fee_pp_micros)  # type: ignore[arg-type]


def test_fee_enforces_int64_result_and_input_domains() -> None:
    int64_max = 2**63 - 1

    assert fee_microdollars(int64_max, 1) == int64_max
    for quantity, fee_pp_micros in (
        (-1, 1),
        (int64_max + 1, 0),
        (int64_max, 2),
        (1, -1),
        (1, 1_000_001),
        (1.0, 1),
        (1, 1.0),
        ("1", 1),
        (1, "1"),
    ):
        with pytest.raises(ValueError, match="integer domains"):
            fee_microdollars(quantity, fee_pp_micros)


def test_sizing_caps_report_volatility_notional_depth_and_cash_limits() -> None:
    snapshot = book(bids=[(490_000, 20_000)], asks=[(540_000, 20_000)])
    assert sizing_caps(snapshot, "YES", economics()) == {
        "volatility": 500,
        "notional": 1_851,
        "depth": 1_000,
        "cash": 18_181,
    }


def test_quantity_uses_smallest_frozen_cap() -> None:
    snapshot = book(bids=[(490_000, 20_000)], asks=[(540_000, 20_000)])
    assert size_quantity(snapshot, "YES", 700_000, economics()) == 500


@pytest.mark.parametrize(("cash", "expected"), ((55_000_000, 100), (54_999_999, 99)))
def test_cash_cap_uses_notional_plus_per_contract_fee_exactly(
    cash: int, expected: int
) -> None:
    snapshot = book(bids=[(490_000, 20_000)], asks=[(540_000, 20_000)])
    policy = economics(cash=cash)

    assert sizing_caps(snapshot, "YES", policy)["cash"] == expected
    assert (
        size_quantity(
            snapshot,
            "YES",
            700_000,
            policy,
            equity_microdollars=10_000_000_000,
        )
        == expected
    )


def test_threshold_equality_returns_exact_largest_quantity() -> None:
    snapshot = book(bids=[(490_000, 20_000)], asks=[(540_000, 20_000)])
    policy = economics(entry_fee=10_000, exit_fee=5_000, slippage=5_000)

    assert size_quantity(snapshot, "YES", 610_000, policy) == 500


def test_quantity_searches_down_when_deeper_vwap_loses_edge() -> None:
    snapshot = book(
        bids=[(490_000, 10_000)],
        asks=[(540_000, 10), (650_000, 1_990)],
    )
    policy = economics(sigma=50_000, entry_fee=0, slippage=0)

    assert size_quantity(snapshot, "YES", 590_000, policy) == 10


def test_huge_int64_quantity_search_has_bounded_candidate_evaluations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    int64_max = 2**63 - 1
    snapshot = book(bids=[(490_000, int64_max)], asks=[(540_000, int64_max)])
    policy = economics(cash=int64_max, sigma=50_000)
    quote_calls = 0
    original_quote_side = paper.quote_side

    def counted_quote_side(*args: object, **kwargs: object) -> object:
        nonlocal quote_calls
        quote_calls += 1
        return original_quote_side(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(paper, "quote_side", counted_quote_side)

    assert (
        size_quantity(
            snapshot,
            "YES",
            540_000,
            policy,
            equity_microdollars=int64_max,
            available_cash_microdollars=int64_max,
        )
        == 0
    )
    assert quote_calls <= 64


def test_caps_that_floor_to_zero_produce_no_quantity() -> None:
    snapshot = book(bids=[(490_000, 10)], asks=[(540_000, 10)])
    assert size_quantity(snapshot, "YES", 700_000, economics(cash=1)) == 0
    assert (
        size_quantity(snapshot, "YES", 700_000, economics(), equity_microdollars=1) == 0
    )


def test_ai_gating_prevents_orders() -> None:
    snapshot = book(bids=[(490_000, 20_000)], asks=[(540_000, 20_000)])
    base = {
        "p_yes_micros": 700_000,
        "action": "BUY_YES",
        "confidence_micros": 800_000,
        "sufficiency_micros": 700_000,
        "ambiguity_micros": 300_000,
        "rationale": "bounded",
    }
    for change in (
        {"sufficiency_micros": 699_999},
        {"ambiguity_micros": 300_001},
        {"action": "ABSTAIN"},
    ):
        decision = JevDecision.model_validate({**base, **change})
        result = evaluate_ai_arm(
            snapshot,
            decision,
            economics(),
            resolution=1,
            model_cost_microdollars=500_000,
        )
        assert result.disposition == "ABSTAIN"
        assert result.quantity == 0


def test_control_with_insufficient_history_abstains_without_model_failure(
    fixture: FeasibilityFixture,
) -> None:
    shortened = fixture.model_copy(
        update={"causal_history": fixture.causal_history[:1]}
    )

    result = evaluate_control_arm(shortened, resolution=1)

    assert result.disposition == "ABSTAIN"
    assert result.signal_probability_micros is None
    assert result.signal_action == "ABSTAIN"


def test_separate_arms_reuse_depth_and_model_cost_is_ai_only(
    fixture: FeasibilityFixture,
) -> None:
    observation = evaluate_fixture(fixture)

    assert observation.ai.initial_cash_microdollars == STARTING_CASH_MICRODOLLARS
    assert (
        observation.deterministic.initial_cash_microdollars
        == STARTING_CASH_MICRODOLLARS
    )
    assert observation.ai.model_cost_microdollars == 500_000
    assert observation.deterministic.model_cost_microdollars == 0
    assert (
        observation.deterministic.final_cash_microdollars == STARTING_CASH_MICRODOLLARS
    )


def test_fixture_recomputes_exact_resolved_economics_and_scores(
    fixture: FeasibilityFixture,
) -> None:
    observation = evaluate_fixture(fixture)

    assert observation == fixture.expected.observation_result
    assert observation.ai.entry_vwap_micros == 542_000
    assert observation.ai.quantity == 500
    assert observation.ai.fees_microdollars == 5_000_000
    assert observation.ai.trading_pnl_microdollars == 224_000_000
    assert observation.ai.economic_pnl_microdollars == 223_500_000
    assert observation.ai.brier_micros == 90_000
    assert observation.deterministic.brier_micros == 202_500
    assert observation.baseline_probability_micros == 515_000
    assert observation.baseline_brier_micros == 235_225
    assert observation.paired_brier_delta_micros == -112_500


def test_fixture_terminalizes_paired_economic_delta_below_int64(
    fixture: FeasibilityFixture,
) -> None:
    decision = fixture.jev_decision.model_copy(
        update={"p_yes_micros": 200_000, "action": "BUY_NO"}
    )
    costs = fixture.model_costs_microdollars.model_copy(
        update={"openai": INT64_MAX, "jev": 0}
    )
    history = [
        point.model_copy(update={"midpoint_micros": midpoint})
        for point, midpoint in zip(
            fixture.causal_history, (500_000, 600_000), strict=True
        )
    ]
    extreme = fixture.model_copy(
        update={
            "jev_decision": decision,
            "model_costs_microdollars": costs,
            "causal_history": history,
        }
    )

    observation = evaluate_fixture(extreme)

    assert isinstance(observation, ObservationResult)
    assert observation.paired_economic_pnl_delta_microdollars is None


def test_unit2_validation_rejects_detached_expected_economics(
    fixture_dict: dict[str, object],
) -> None:
    validate_unit2_fixture(fixture_dict)
    detached = deepcopy(fixture_dict)
    assert isinstance(detached["causal_history"], list)
    detached["causal_history"][0]["midpoint_micros"] = 440_000

    with pytest.raises(ValueError, match="Unit 2 expected results mismatch"):
        validate_unit2_fixture(detached)


@pytest.mark.parametrize("resolution", (1, None))
def test_resolved_and_marked_fills_charge_exit_fee_per_contract(
    resolution: int | None,
) -> None:
    snapshot = book(bids=[(490_000, 20_000)], asks=[(540_000, 20_000)])
    decision = JevDecision(
        p_yes_micros=700_000,
        action="BUY_YES",
        confidence_micros=800_000,
        sufficiency_micros=800_000,
        ambiguity_micros=200_000,
        rationale="bounded",
    )

    result = evaluate_ai_arm(
        snapshot,
        decision,
        economics(entry_fee=10_000, exit_fee=5_000, slippage=0),
        resolution=resolution,  # type: ignore[arg-type]
        model_cost_microdollars=0,
    )

    assert result.quantity == 500
    assert result.fees_microdollars == 7_500_000


def test_unresolved_fill_marks_executable_bids_and_scores_pending() -> None:
    snapshot = book(bids=[(490_000, 2)], asks=[(540_000, 100)])
    decision = JevDecision(
        p_yes_micros=700_000,
        action="BUY_YES",
        confidence_micros=800_000,
        sufficiency_micros=800_000,
        ambiguity_micros=200_000,
        rationale="bounded",
    )
    result = evaluate_ai_arm(
        snapshot,
        decision,
        economics(sigma=1_000, entry_fee=0, slippage=0),
        resolution=None,
        model_cost_microdollars=0,
    )

    assert result.quantity == 5
    assert result.mark_micros == 196_000
    assert result.payout_micros is None
    assert result.forecast_status == "PENDING_RESOLUTION"
    assert result.brier_micros is None


def test_buy_no_uses_derived_asks_and_held_no_payout() -> None:
    snapshot = book(bids=[(390_000, 20_000)], asks=[(410_000, 20_000)])
    decision = JevDecision(
        p_yes_micros=200_000,
        action="BUY_NO",
        confidence_micros=800_000,
        sufficiency_micros=800_000,
        ambiguity_micros=200_000,
        rationale="bounded",
    )

    result = evaluate_ai_arm(
        snapshot,
        decision,
        economics(entry_fee=0, slippage=0),
        resolution=0,
        model_cost_microdollars=0,
    )

    assert result.entry_side == "NO"
    assert result.entry_vwap_micros == 610_000
    assert result.payout_micros == 0
    assert result.trading_pnl_microdollars == result.quantity * 390_000


def test_midpoint_and_brier_round_down_deterministically() -> None:
    snapshot = book(bids=[(490_001, 2)], asks=[(540_000, 2)])
    assert midpoint_micros(snapshot) == 515_000
    assert brier_micros(333_333, 1_000_000) == 444_444


@pytest.mark.parametrize("cash", [1, 540_000, 5_400_000, 54_000_000, 10_000_000_000])
@pytest.mark.parametrize("depth", [1, 19, 100, 1_001])
def test_property_style_sizing_never_overfills_or_creates_negative_cash(
    cash: int, depth: int
) -> None:
    snapshot = book(bids=[(490_000, depth)], asks=[(540_000, depth)])
    policy = economics(cash=cash, entry_fee=10_001)
    quantity = size_quantity(snapshot, "YES", 700_000, policy)
    fill = quote_side(snapshot, "YES", quantity, liquidity="ASK")
    funded = fill.notional_microdollars + fee_microdollars(
        quantity, policy.entry_fee_pp_micros
    )

    assert 0 <= quantity <= depth
    assert fill.filled_quantity <= fill.captured_quantity
    assert funded <= cash


def _canonical_hash(model: Any) -> str:
    return hashlib.sha256(
        canonical_json_bytes(model.model_dump(mode="json"))
    ).hexdigest()


def test_fixture_engine_supports_unresolved_filled_arms(
    fixture: FeasibilityFixture,
) -> None:
    history = [
        CausalHistoryPoint(
            observed_at=point.observed_at,
            midpoint_micros=midpoint,
        )
        for point, midpoint in zip(fixture.causal_history, (500_000, 575_000))
    ]
    raw = fixture.model_dump(mode="json")
    raw.update(
        resolution=None,
        causal_history=[point.model_dump(mode="json") for point in history],
    )
    unresolved = FeasibilityFixture.model_validate_json(json.dumps(raw))

    observation = evaluate_fixture(unresolved)

    assert observation.observed_at > unresolved.execution_book.retrieved_at
    assert observation.resolution is None
    assert observation.ai.disposition == "FILLED"
    assert observation.deterministic.disposition == "FILLED"
    assert observation.ai.forecast_status == "PENDING_RESOLUTION"
    assert observation.deterministic.forecast_status == "PENDING_RESOLUTION"
    assert observation.ai.payout_micros is None
    assert observation.deterministic.payout_micros is None
    assert observation.ai.brier_micros is None
    assert observation.deterministic.brier_micros is None
    assert observation.baseline_brier_micros is None
    assert observation.paired_brier_delta_micros is None


def test_unresolved_abstain_and_unavailable_forecasts_are_coherent(
    fixture: FeasibilityFixture,
) -> None:
    gated = fixture.jev_decision.model_copy(update={"action": "ABSTAIN"})
    abstain = evaluate_ai_arm(
        fixture.execution_book,
        gated,
        fixture.economics,
        resolution=None,
        model_cost_microdollars=500_000,
    )
    unavailable = evaluate_ai_arm(
        fixture.execution_book,
        None,
        fixture.economics,
        resolution=None,
        model_cost_microdollars=INT64_MAX,
    )
    insufficient = evaluate_control_arm(
        fixture.model_copy(update={"causal_history": fixture.causal_history[:1]}),
        resolution=None,
    )

    assert (abstain.forecast_status, abstain.brier_micros) == (
        "PENDING_RESOLUTION",
        None,
    )
    for result in (unavailable, insufficient):
        assert result.forecast_status == "UNAVAILABLE"
        assert result.brier_micros is None
        assert result.payout_micros is None


def test_unresolved_buy_no_mark_is_stored_as_market_yes_value() -> None:
    snapshot = book(bids=[(390_000, 20_000)], asks=[(410_000, 20_000)])
    decision = JevDecision(
        p_yes_micros=200_000,
        action="BUY_NO",
        confidence_micros=800_000,
        sufficiency_micros=800_000,
        ambiguity_micros=200_000,
        rationale="bounded",
    )

    result = evaluate_ai_arm(
        snapshot,
        decision,
        economics(entry_fee=0, slippage=0),
        resolution=None,
        model_cost_microdollars=0,
    )

    assert result.entry_side == "NO"
    assert result.mark_micros == 410_000
    assert result.trading_pnl_microdollars == result.quantity * (590_000 - 610_000)


def test_unit2_rejects_regenerated_nine_thousand_dollar_fixture(
    fixture_dict: dict[str, object],
) -> None:
    mutated = deepcopy(fixture_dict)
    mutated["economics"]["starting_cash_microdollars"] = 9_000_000_000  # type: ignore[index]
    fixture = FeasibilityFixture.model_validate_json(json.dumps(mutated))
    assert fixture.resolution is not None
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
    original = fixture.expected.observation_result
    observation = original.model_copy(
        update={
            "ai": ai,
            "deterministic": deterministic,
            "paired_trading_pnl_delta_microdollars": (
                ai.trading_pnl_microdollars - deterministic.trading_pnl_microdollars
            ),
            "paired_economic_pnl_delta_microdollars": (
                ai.economic_pnl_microdollars - deterministic.economic_pnl_microdollars
            ),
            "paired_brier_delta_micros": (
                ai.brier_micros - deterministic.brier_micros
                if ai.brier_micros is not None
                and deterministic.brier_micros is not None
                else None
            ),
        }
    )
    expected = mutated["expected"]  # type: ignore[index]
    expected["arm_results"] = {  # type: ignore[index]
        "ai": ai.model_dump(mode="json"),
        "deterministic": deterministic.model_dump(mode="json"),
    }
    expected["observation_result"] = observation.model_dump(mode="json")  # type: ignore[index]
    expected["canonical_sha256"].update(  # type: ignore[index]
        ai_arm=_canonical_hash(ai),
        deterministic_arm=_canonical_hash(deterministic),
        observation=_canonical_hash(observation),
    )

    with pytest.raises(ValueError, match="starting capital"):
        validate_unit2_fixture(mutated)


@pytest.mark.parametrize(
    "call",
    (
        lambda snapshot, policy: quote_side(snapshot, "YES", True, liquidity="ASK"),
        lambda snapshot, policy: quote_side(snapshot, "BAD", 1, liquidity="ASK"),
        lambda snapshot, policy: quote_side(snapshot, "YES", 1, liquidity="BAD"),
        lambda snapshot, policy: brier_micros(True, 1_000_000),
        lambda snapshot, policy: brier_micros(500_000, 1_000_000.0),
        lambda snapshot, policy: gross_edge_micros("500000", "YES", 400_000),
        lambda snapshot, policy: gross_edge_micros(500_000, "BAD", 400_000),
        lambda snapshot, policy: sizing_caps(
            snapshot, "YES", policy, equity_microdollars=True
        ),
        lambda snapshot, policy: sizing_caps(
            snapshot, "BAD", policy, available_cash_microdollars=1
        ),
        lambda snapshot, policy: size_quantity(snapshot, "YES", True, policy),
        lambda snapshot, policy: size_quantity(
            snapshot, "YES", 700_000, policy, available_cash_microdollars=1.0
        ),
    ),
)
def test_public_integer_and_side_helpers_reject_inexact_runtime_types(
    call: Callable[[BookSnapshot, Economics], object],
) -> None:
    snapshot = book(bids=[(490_000, 10)], asks=[(540_000, 10)])

    with pytest.raises(ValueError):
        call(snapshot, economics())


@pytest.mark.parametrize(
    ("resolution", "model_cost"),
    ((True, 0), (0, True), (1.0, 0), (1, 1.0), ("1", 0), (1, "0")),
)
def test_arm_evaluators_reject_inexact_resolution_and_cost_types(
    fixture: FeasibilityFixture, resolution: object, model_cost: object
) -> None:
    with pytest.raises(ValueError):
        evaluate_ai_arm(
            fixture.execution_book,
            fixture.jev_decision,
            fixture.economics,
            resolution=resolution,  # type: ignore[arg-type]
            model_cost_microdollars=model_cost,  # type: ignore[arg-type]
        )


def test_extreme_model_cost_returns_terminal_no_fill_without_int64_underflow() -> None:
    snapshot = book(
        bids=[(0, INT64_MAX)],
        asks=[(2, INT64_MAX)],
    )
    decision = JevDecision(
        p_yes_micros=1_000_000,
        action="BUY_YES",
        confidence_micros=1_000_000,
        sufficiency_micros=1_000_000,
        ambiguity_micros=0,
        rationale="bounded",
    )

    result = evaluate_ai_arm(
        snapshot,
        decision,
        economics(cash=INT64_MAX, entry_fee=0, exit_fee=0, slippage=0, sigma=0),
        resolution=None,
        model_cost_microdollars=INT64_MAX,
    )

    assert result.disposition == "NO_EDGE"
    assert result.quantity == 0
    assert result.final_cash_microdollars == INT64_MAX
    assert result.trading_pnl_microdollars == 0
    assert result.economic_pnl_microdollars == -INT64_MAX


def test_extreme_cash_returns_terminal_no_fill_when_profit_would_overflow() -> None:
    snapshot = book(
        bids=[(1, INT64_MAX)],
        asks=[(2, INT64_MAX)],
    )
    decision = JevDecision(
        p_yes_micros=1_000_000,
        action="BUY_YES",
        confidence_micros=1_000_000,
        sufficiency_micros=1_000_000,
        ambiguity_micros=0,
        rationale="bounded",
    )

    result = evaluate_ai_arm(
        snapshot,
        decision,
        economics(cash=INT64_MAX, entry_fee=0, exit_fee=0, slippage=0, sigma=0),
        resolution=1,
        model_cost_microdollars=0,
    )

    assert result.disposition == "NO_EDGE"
    assert result.quantity == 0
    assert result.final_cash_microdollars == INT64_MAX
    assert result.trading_pnl_microdollars == 0


def test_terminal_bound_preserves_largest_passing_quantity() -> None:
    snapshot = book(
        bids=[(1, INT64_MAX)],
        asks=[(2, INT64_MAX)],
    )
    decision = JevDecision(
        p_yes_micros=1_000_000,
        action="BUY_YES",
        confidence_micros=1_000_000,
        sufficiency_micros=1_000_000,
        ambiguity_micros=0,
        rationale="bounded",
    )

    result = evaluate_ai_arm(
        snapshot,
        decision,
        economics(
            cash=INT64_MAX - 2_000_000,
            entry_fee=0,
            exit_fee=0,
            slippage=0,
            sigma=0,
        ),
        resolution=1,
        model_cost_microdollars=0,
    )

    assert result.disposition == "FILLED"
    assert result.quantity == 2
    assert result.final_cash_microdollars <= INT64_MAX


def test_extreme_contract_inputs_can_still_return_bounded_fill() -> None:
    snapshot = book(
        bids=[(1, INT64_MAX)],
        asks=[(2, INT64_MAX)],
    )
    decision = JevDecision(
        p_yes_micros=1_000_000,
        action="BUY_YES",
        confidence_micros=1_000_000,
        sufficiency_micros=1_000_000,
        ambiguity_micros=0,
        rationale="bounded",
    )

    result = evaluate_ai_arm(
        snapshot,
        decision,
        economics(cash=INT64_MAX - 1, entry_fee=0, exit_fee=0, slippage=0, sigma=0),
        resolution=1,
        model_cost_microdollars=INT64_MAX,
    )

    assert isinstance(result, ArmResult)
    assert -(2**63) <= result.trading_pnl_microdollars <= INT64_MAX
    assert 0 <= result.final_cash_microdollars <= INT64_MAX
    assert -(2**63) <= result.economic_pnl_microdollars <= INT64_MAX


def test_randomized_sizing_matches_exhaustive_small_reference() -> None:
    rng = random.Random(20261007)
    for _ in range(200):
        best_bid = rng.randint(50_001, 449_999)
        worse_bid = rng.randint(50_000, best_bid - 1)
        best_ask = rng.randint(max(best_bid + 1, 500_000), 899_999)
        worse_ask = rng.randint(best_ask + 1, 900_000)
        snapshot = book(
            bids=[
                (best_bid, rng.randint(1, 20)),
                (worse_bid, rng.randint(1, 20)),
            ],
            asks=[
                (best_ask, rng.randint(1, 20)),
                (worse_ask, rng.randint(1, 20)),
            ],
        )
        policy = economics(
            entry_fee=rng.randint(0, 20_000),
            exit_fee=rng.randint(0, 20_000),
            slippage=rng.randint(0, 20_000),
            sigma=rng.randint(0, 200_000),
            cash=rng.randint(1, 20_000_000),
        )
        probability = rng.randint(0, 1_000_000)
        side = rng.choice(("YES", "NO"))
        caps = sizing_caps(snapshot, side, policy)
        expected = 0
        for quantity in range(1, min(caps.values()) + 1):
            entry = quote_side(snapshot, side, quantity, liquidity="ASK")
            assert entry.vwap_micros is not None
            funded = entry.notional_microdollars + fee_microdollars(
                quantity, policy.entry_fee_pp_micros
            )
            if (
                quantity <= caps["notional"]
                and funded <= policy.starting_cash_microdollars
                and gross_edge_micros(probability, side, entry.vwap_micros)
                >= edge_threshold_micros(policy)
            ):
                expected = quantity
        assert size_quantity(snapshot, side, probability, policy) == expected


def test_terminal_bound_uses_exact_notional_for_rounding_counterexample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upper = 95_506
    snapshot = book(
        bids=[(1, upper)],
        asks=[(104_144, 30_413), (949_094, upper - 30_413)],
    )
    policy = economics(
        cash=INT64_MAX - 29_284_403_580,
        entry_fee=4_839,
        exit_fee=0,
        slippage=0,
        sigma=0,
    )
    decision = JevDecision(
        p_yes_micros=1_000_000,
        action="BUY_YES",
        confidence_micros=1_000_000,
        sufficiency_micros=1_000_000,
        ambiguity_micros=0,
        rationale="bounded",
    )
    quote_calls = 0
    original_quote_side = paper.quote_side

    def counted_quote_side(*args: object, **kwargs: object) -> object:
        nonlocal quote_calls
        quote_calls += 1
        return original_quote_side(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(paper, "size_quantity", lambda *args, **kwargs: upper)
    monkeypatch.setattr(paper, "quote_side", counted_quote_side)

    result = evaluate_ai_arm(
        snapshot,
        decision,
        policy,
        resolution=1,
        model_cost_microdollars=0,
    )

    rounded_island = quote_side(snapshot, "YES", 77_865, liquidity="ASK")
    assert rounded_island.vwap_micros is not None
    assert (
        policy.starting_cash_microdollars
        + 77_865 * (1_000_000 - rounded_island.vwap_micros - 4_839)
        == INT64_MAX
    )
    assert result.quantity == 77_863
    assert result.quantity != 77_865
    assert result.entry_notional_microdollars == 48_201_841_772
    assert result.trading_pnl_microdollars == 29_284_379_171
    assert result.final_cash_microdollars == 9_223_372_036_854_751_398
    next_entry = quote_side(snapshot, "YES", result.quantity + 1, liquidity="ASK")
    assert (
        policy.starting_cash_microdollars
        + (result.quantity + 1) * 1_000_000
        - next_entry.notional_microdollars
        - (result.quantity + 1) * policy.entry_fee_pp_micros
        > INT64_MAX
    )
    assert quote_calls <= 5


def test_randomized_terminal_bound_matches_exhaustive_contract_oracle() -> None:
    rng = random.Random(20261008)
    for _ in range(200):
        best_bid = rng.randint(50_001, 449_999)
        worse_bid = rng.randint(50_000, best_bid - 1)
        best_ask = rng.randint(max(best_bid + 1, 500_000), 899_999)
        worse_ask = rng.randint(best_ask + 1, 900_000)
        snapshot = book(
            bids=[(best_bid, rng.randint(1, 20)), (worse_bid, rng.randint(1, 20))],
            asks=[(best_ask, rng.randint(1, 20)), (worse_ask, rng.randint(1, 20))],
        )
        policy = economics(
            entry_fee=rng.randint(0, 20_000),
            exit_fee=rng.randint(0, 20_000),
            slippage=0,
            sigma=50_000,
            cash=rng.randint(1, 100_000_000),
        )
        side = rng.choice(("YES", "NO"))
        payout = rng.choice((0, 1_000_000, None))
        model_cost = rng.randint(0, INT64_MAX)
        upper = rng.randint(1, 40)
        expected = 0
        for quantity in range(1, upper + 1):
            entry = quote_side(snapshot, side, quantity, liquidity="ASK")
            if entry.filled_quantity != quantity or entry.vwap_micros is None:
                continue
            mark = quote_side(snapshot, side, quantity, liquidity="BID")
            valuation_notional = (
                mark.notional_microdollars
                if payout is None
                else quantity * (payout if side == "YES" else 1_000_000 - payout)
            )
            fees = quantity * (policy.entry_fee_pp_micros + policy.exit_fee_pp_micros)
            funded = entry.notional_microdollars + fees
            trading = valuation_notional - entry.notional_microdollars - fees
            final_cash = policy.starting_cash_microdollars + trading
            economic = trading - model_cost
            if (
                entry.notional_microdollars <= INT64_MAX
                and mark.notional_microdollars <= INT64_MAX
                and fees <= INT64_MAX
                and funded <= policy.starting_cash_microdollars
                and -(2**63) <= trading <= INT64_MAX
                and 0 <= final_cash <= INT64_MAX
                and -(2**63) <= economic <= INT64_MAX
            ):
                expected = quantity
        assert (
            paper._terminal_quantity(
                book=snapshot,
                side=side,
                upper=upper,
                policy=policy,
                payout=payout,
                model_cost_microdollars=model_cost,
            )
            == expected
        )


def test_randomized_terminal_bound_matches_exhaustive_int64_extremes() -> None:
    rng = random.Random(20261009)
    for _ in range(200):
        upper = rng.randint(1, 50)
        best_bid = rng.randint(0, 400_000)
        best_ask = rng.randint(max(best_bid + 1, 500_000), 900_000)
        snapshot = book(
            bids=[(best_bid, upper)],
            asks=[(best_ask, upper)],
        )
        policy = economics(
            entry_fee=rng.randint(0, 1_000_000),
            exit_fee=rng.randint(0, 1_000_000),
            slippage=0,
            sigma=0,
            cash=rng.choice((rng.randint(0, 10_000_000), INT64_MAX)),
        )
        side = rng.choice(("YES", "NO"))
        payout = rng.choice((0, 1_000_000, None))
        model_cost = rng.choice((0, rng.randint(0, 10_000_000), INT64_MAX))
        expected = 0
        for quantity in range(1, upper + 1):
            entry = quote_side(snapshot, side, quantity, liquidity="ASK")
            mark = quote_side(snapshot, side, quantity, liquidity="BID")
            valuation_notional = (
                mark.notional_microdollars
                if payout is None
                else quantity * (payout if side == "YES" else 1_000_000 - payout)
            )
            fees = quantity * (policy.entry_fee_pp_micros + policy.exit_fee_pp_micros)
            trading = valuation_notional - entry.notional_microdollars - fees
            if (
                entry.filled_quantity == quantity
                and entry.notional_microdollars <= INT64_MAX
                and valuation_notional <= INT64_MAX
                and fees <= INT64_MAX
                and entry.notional_microdollars + fees
                <= policy.starting_cash_microdollars
                and -(2**63) <= trading <= INT64_MAX
                and 0 <= policy.starting_cash_microdollars + trading <= INT64_MAX
                and -(2**63) <= trading - model_cost <= INT64_MAX
            ):
                expected = quantity
        assert (
            paper._terminal_quantity(
                book=snapshot,
                side=side,
                upper=upper,
                policy=policy,
                payout=payout,
                model_cost_microdollars=model_cost,
            )
            == expected
        )


def test_large_terminal_bound_finds_analytical_multi_level_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_level_quantity = 12_000
    first_price = 100_000
    second_price = 200_000
    economic_loss_limit = 1_800_000_000
    upper = 20_000
    snapshot = book(
        bids=[(0, upper)],
        asks=[
            (first_price, first_level_quantity),
            (second_price, upper - first_level_quantity),
        ],
    )
    policy = economics(
        cash=10_000_000_000,
        entry_fee=0,
        exit_fee=0,
        slippage=0,
        sigma=0,
    )
    model_cost = INT64_MAX - economic_loss_limit + 1
    expected = (
        first_level_quantity
        + (economic_loss_limit - first_level_quantity * first_price) // second_price
    )
    quote_calls = 0
    original_quote_side = paper.quote_side

    def counted_quote_side(*args: object, **kwargs: object) -> object:
        nonlocal quote_calls
        quote_calls += 1
        return original_quote_side(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(paper, "quote_side", counted_quote_side)

    actual = paper._terminal_quantity(
        book=snapshot,
        side="YES",
        upper=upper,
        policy=policy,
        payout=None,
        model_cost_microdollars=model_cost,
    )

    assert expected == 15_000
    assert actual == expected
    assert quote_calls < 200

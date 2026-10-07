from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jev_trading_lab.polymarket import paper
from jev_trading_lab.polymarket.models import (
    BookSnapshot,
    Economics,
    FeasibilityFixture,
    JevDecision,
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

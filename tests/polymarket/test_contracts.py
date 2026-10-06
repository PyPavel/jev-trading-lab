from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.polymarket import models as polymarket_models
from jev_trading_lab.polymarket.models import (
    ArmResult,
    BookSnapshot,
    EvidencePacket,
    JevDecision,
    ObservationResult,
    ResearchBrief,
)

MICROS_PER_DOLLAR = 1_000_000

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures/polymarket/feasibility_cycle.json"


@pytest.fixture(scope="module")
def cycle() -> dict[str, Any]:
    loaded = polymarket_models.load_strict_json(
        FIXTURE_PATH.read_text(encoding="utf-8")
    )
    assert isinstance(loaded, dict)
    return loaded


def _validate_json(model: type[BaseModel], value: object) -> BaseModel:
    return model.model_validate_json(json.dumps(value))


def _canonical_sha(model: BaseModel) -> str:
    return hashlib.sha256(
        canonical_json_bytes(model.model_dump(mode="json"))
    ).hexdigest()


def test_complete_fixture_validates_and_round_trips_canonically(
    cycle: dict[str, Any],
) -> None:
    sections: tuple[tuple[str, type[BaseModel], object], ...] = (
        ("evidence", EvidencePacket, cycle["sanitized_evidence"]),
        ("precheck_book", BookSnapshot, cycle["precheck_book"]),
        ("execution_book", BookSnapshot, cycle["execution_book"]),
        ("research_brief", ResearchBrief, cycle["research_brief"]),
        ("jev_decision", JevDecision, cycle["jev_decision"]),
        ("ai_arm", ArmResult, cycle["expected"]["arm_results"]["ai"]),
        (
            "deterministic_arm",
            ArmResult,
            cycle["expected"]["arm_results"]["deterministic"],
        ),
        (
            "observation",
            ObservationResult,
            cycle["expected"]["observation_result"],
        ),
    )

    for name, model_type, raw in sections:
        model = _validate_json(model_type, raw)
        canonical = canonical_json_bytes(model.model_dump(mode="json"))
        assert model_type.model_validate_json(canonical) == model
        assert _canonical_sha(model) == cycle["expected"]["canonical_sha256"][name]


def test_fixture_preserves_exact_raw_and_model_artifact_hashes(
    cycle: dict[str, Any],
) -> None:
    artifacts = {
        **cycle["raw"],
        **cycle["model_artifacts"],
    }
    expected = cycle["expected"]["artifact_sha256"]

    assert set(artifacts) == set(expected)
    for name, text in artifacts.items():
        assert hashlib.sha256(text.encode()).hexdigest() == expected[name]


def test_fixture_contains_inputs_needed_by_later_cycle(cycle: dict[str, Any]) -> None:
    assert len(cycle["causal_history"]) >= 2
    assert cycle["model_costs_microdollars"] == {"jev": 125_000, "openai": 375_000}
    assert cycle["resolution"] == {"resolved_at": "2026-10-04T20:00:00Z", "payout": 1}
    assert cycle["expected"]["observation_result"]["resolution"] == 1


@pytest.mark.parametrize(
    "forbidden",
    ("outcomePrices", "bestBid", "token_id", "bids", "asks", "midpoint"),
)
def test_evidence_packet_rejects_price_and_book_fields(
    cycle: dict[str, Any], forbidden: str
) -> None:
    evidence = {**cycle["sanitized_evidence"], forbidden: "leak"}

    with pytest.raises(ValidationError):
        _validate_json(EvidencePacket, evidence)


@pytest.mark.parametrize(
    ("section", "model_type"),
    (
        ("sanitized_evidence", EvidencePacket),
        ("precheck_book", BookSnapshot),
        ("research_brief", ResearchBrief),
        ("jev_decision", JevDecision),
    ),
)
def test_contracts_forbid_unknown_fields(
    cycle: dict[str, Any], section: str, model_type: type[BaseModel]
) -> None:
    value = {**cycle[section], "unexpected": True}

    with pytest.raises(ValidationError):
        _validate_json(model_type, value)


def test_observation_and_arm_contracts_forbid_unknown_fields(
    cycle: dict[str, Any],
) -> None:
    arm = {**cycle["expected"]["arm_results"]["ai"], "unexpected": True}
    observation = {**cycle["expected"]["observation_result"], "unexpected": True}

    with pytest.raises(ValidationError):
        _validate_json(ArmResult, arm)
    with pytest.raises(ValidationError):
        _validate_json(ObservationResult, observation)


@pytest.mark.parametrize("value", (-1, 1_000_001))
@pytest.mark.parametrize(
    "model_type,section,field",
    (
        (JevDecision, "jev_decision", "p_yes_micros"),
        (JevDecision, "jev_decision", "confidence_micros"),
        (JevDecision, "jev_decision", "sufficiency_micros"),
        (JevDecision, "jev_decision", "ambiguity_micros"),
        (BookSnapshot, "precheck_book", "bids.0.price_micros"),
    ),
)
def test_probabilities_and_prices_must_be_unit_interval(
    cycle: dict[str, Any],
    model_type: type[BaseModel],
    section: str,
    field: str,
    value: int,
) -> None:
    raw = deepcopy(cycle[section])
    if field == "bids.0.price_micros":
        raw["bids"][0]["price_micros"] = value
    else:
        raw[field] = value

    with pytest.raises(ValidationError):
        _validate_json(model_type, raw)


@pytest.mark.parametrize("quantity", (0, -1))
def test_book_rejects_nonpositive_quantities(
    cycle: dict[str, Any], quantity: int
) -> None:
    book = deepcopy(cycle["precheck_book"])
    book["asks"][0]["quantity"] = quantity

    with pytest.raises(ValidationError):
        _validate_json(BookSnapshot, book)


@pytest.mark.parametrize(
    ("side", "levels"),
    (
        ("bids", [(450_000, 100), (460_000, 100)]),
        ("asks", [(550_000, 100), (540_000, 100)]),
    ),
)
def test_book_levels_require_deterministic_best_first_order(
    cycle: dict[str, Any], side: str, levels: list[tuple[int, int]]
) -> None:
    book = deepcopy(cycle["precheck_book"])
    book[side] = [
        {"price_micros": price, "quantity": quantity} for price, quantity in levels
    ]

    with pytest.raises(ValidationError):
        _validate_json(BookSnapshot, book)


def test_book_rejects_crossed_or_locked_market(cycle: dict[str, Any]) -> None:
    book = deepcopy(cycle["precheck_book"])
    book["bids"][0]["price_micros"] = book["asks"][0]["price_micros"]

    with pytest.raises(ValidationError):
        _validate_json(BookSnapshot, book)


@pytest.mark.parametrize("side", ("bids", "asks"))
def test_book_requires_both_sides(cycle: dict[str, Any], side: str) -> None:
    book = deepcopy(cycle["precheck_book"])
    book[side] = []

    with pytest.raises(ValidationError):
        _validate_json(BookSnapshot, book)


@pytest.mark.parametrize("bad_hash", ("a" * 63, "A" * 64, "g" * 64))
def test_contracts_reject_malformed_or_nonlowercase_sha256(
    cycle: dict[str, Any], bad_hash: str
) -> None:
    evidence = {**cycle["sanitized_evidence"], "source_sha256": bad_hash}
    brief = {**cycle["research_brief"], "evidence_sha256": bad_hash}

    with pytest.raises(ValidationError):
        _validate_json(EvidencePacket, evidence)
    with pytest.raises(ValidationError):
        _validate_json(ResearchBrief, brief)


@pytest.mark.parametrize(
    ("section", "model_type", "field"),
    (
        ("sanitized_evidence", EvidencePacket, "retrieved_at"),
        ("precheck_book", BookSnapshot, "retrieved_at"),
        ("expected.observation_result", ObservationResult, "observed_at"),
    ),
)
def test_contracts_reject_non_utc_timestamps(
    cycle: dict[str, Any], section: str, model_type: type[BaseModel], field: str
) -> None:
    raw = deepcopy(cycle)
    for part in section.split("."):
        raw = raw[part]
    raw[field] = "2026-10-03T08:00:00-04:00"

    with pytest.raises(ValidationError):
        _validate_json(model_type, raw)


def test_research_citations_must_bind_evidence_sha(cycle: dict[str, Any]) -> None:
    brief = deepcopy(cycle["research_brief"])
    brief["cited_facts"][0]["evidence_sha256"] = "0" * 64

    with pytest.raises(ValidationError):
        _validate_json(ResearchBrief, brief)


def test_unknown_decision_action_fails(cycle: dict[str, Any]) -> None:
    decision = {**cycle["jev_decision"], "action": "HOLD"}

    with pytest.raises(ValidationError):
        _validate_json(JevDecision, decision)


def test_abstain_is_valid_without_entry(cycle: dict[str, Any]) -> None:
    decision = {**cycle["jev_decision"], "action": "ABSTAIN"}
    arm = _zero_fill_ai(cycle)

    assert _validate_json(JevDecision, decision)
    assert _validate_json(ArmResult, arm)


@pytest.mark.parametrize(
    "entry",
    (
        {"entry_side": "YES"},
        {"entry_vwap_micros": 550_000},
        {"quantity": 1},
    ),
)
def test_abstain_cannot_carry_entry_or_quantity(
    cycle: dict[str, Any], entry: dict[str, object]
) -> None:
    arm = {**_zero_fill_ai(cycle), **entry}

    with pytest.raises(ValidationError):
        _validate_json(ArmResult, arm)


def test_observation_requires_exactly_one_named_arm(cycle: dict[str, Any]) -> None:
    observation = deepcopy(cycle["expected"]["observation_result"])
    observation["ai"]["arm"] = "DETERMINISTIC"

    with pytest.raises(ValidationError):
        _validate_json(ObservationResult, observation)


def _filled_ai(cycle: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(cycle["expected"]["arm_results"]["ai"])


def _zero_fill_ai(
    cycle: dict[str, Any], disposition: str = "ABSTAIN"
) -> dict[str, Any]:
    arm = _filled_ai(cycle)
    model_cost = sum(cycle["model_costs_microdollars"].values())
    arm.update(
        signal_action="ABSTAIN" if disposition == "ABSTAIN" else "BUY_YES",
        disposition=disposition,
        entry_side=None,
        entry_vwap_micros=None,
        quantity=0,
        fees_microdollars=0,
        final_cash_microdollars=arm["initial_cash_microdollars"],
        mark_micros=None,
        payout_micros=None,
        trading_pnl_microdollars=0,
        economic_pnl_microdollars=-model_cost,
        forecast_status="PENDING_RESOLUTION",
        brier_micros=None,
    )
    return arm


def test_fixture_fill_economics_are_derived_from_frozen_book(
    cycle: dict[str, Any],
) -> None:
    quantity = cycle["expected"]["arm_results"]["ai"]["quantity"]
    remaining = quantity
    gross_entry = 0
    for level in cycle["execution_book"]["asks"]:
        filled = min(remaining, level["quantity"])
        gross_entry += filled * level["price_micros"]
        remaining -= filled
        if remaining == 0:
            break
    assert remaining == 0

    expected = cycle["expected"]["arm_results"]["ai"]
    vwap = gross_entry // quantity
    payout_micros = cycle["resolution"]["payout"] * MICROS_PER_DOLLAR
    trading_pnl = quantity * (payout_micros - vwap) - expected["fees_microdollars"]
    model_cost = sum(cycle["model_costs_microdollars"].values())

    assert expected["entry_vwap_micros"] == vwap
    assert expected["payout_micros"] == payout_micros
    assert expected["trading_pnl_microdollars"] == trading_pnl
    assert expected["final_cash_microdollars"] == (
        expected["initial_cash_microdollars"] + trading_pnl
    )
    assert expected["economic_pnl_microdollars"] == trading_pnl - model_cost
    observation = cycle["expected"]["observation_result"]
    assert observation["ai"] == expected
    assert observation["paired_trading_pnl_delta_microdollars"] == trading_pnl
    assert observation["paired_economic_pnl_delta_microdollars"] == (
        trading_pnl - model_cost
    )


def test_zero_fill_abstain_has_no_trading_profit(cycle: dict[str, Any]) -> None:
    arm = _zero_fill_ai(cycle)

    validated = ArmResult.model_validate(arm)

    assert validated.final_cash_microdollars == validated.initial_cash_microdollars
    assert validated.trading_pnl_microdollars == 0
    assert validated.economic_pnl_microdollars == -validated.model_cost_microdollars


@pytest.mark.parametrize(
    "changes",
    (
        {"signal_probability_micros": None},
        {"signal_action": None},
        {"signal_action": "BUY_NO"},
        {"entry_side": "NO"},
        {"mark_micros": 600_000},
        {"payout_micros": None},
        {"trading_pnl_microdollars": 1},
        {"final_cash_microdollars": 1},
        {"economic_pnl_microdollars": 1},
        {"brier_micros": 1},
    ),
)
def test_filled_arm_rejects_inconsistent_contract_fields(
    cycle: dict[str, Any], changes: dict[str, object]
) -> None:
    arm = {**_filled_ai(cycle), **changes}

    with pytest.raises(ValidationError):
        ArmResult.model_validate(arm)


@pytest.mark.parametrize(
    "changes",
    (
        {"entry_side": "YES"},
        {"entry_vwap_micros": 500_000},
        {"fees_microdollars": 1},
        {"final_cash_microdollars": 10_000_000_001},
        {"trading_pnl_microdollars": 1},
        {"economic_pnl_microdollars": 1},
    ),
)
def test_nonfilled_arm_rejects_entry_or_inconsistent_economics(
    cycle: dict[str, Any], changes: dict[str, object]
) -> None:
    arm = {**_zero_fill_ai(cycle), **changes}

    with pytest.raises(ValidationError):
        ArmResult.model_validate(arm)


@pytest.mark.parametrize(
    ("disposition", "probability", "action"),
    (
        ("FILLED", 700_000, "BUY_YES"),
        ("PARTIAL_FILL", 700_000, "BUY_YES"),
        ("ABSTAIN", 700_000, "ABSTAIN"),
        ("NO_EDGE", 700_000, "BUY_YES"),
        ("INELIGIBLE", None, None),
        ("MODEL_FAILURE", None, None),
        ("BOOK_FAILURE", None, None),
    ),
)
def test_disposition_has_one_allowed_signal_shape(
    cycle: dict[str, Any],
    disposition: str,
    probability: int | None,
    action: str | None,
) -> None:
    arm = _zero_fill_ai(cycle, disposition)
    arm.update(
        signal_probability_micros=probability,
        signal_action=action,
        forecast_probability_micros=probability,
        forecast_status="PENDING_RESOLUTION"
        if probability is not None
        else "UNAVAILABLE",
        brier_micros=None,
    )
    if disposition in {"FILLED", "PARTIAL_FILL"}:
        arm = _filled_ai(cycle)
        arm["disposition"] = disposition

    assert ArmResult.model_validate(arm)


@pytest.mark.parametrize(
    ("disposition", "probability", "action"),
    (
        ("FILLED", None, "BUY_YES"),
        ("NO_EDGE", 700_000, None),
        ("ABSTAIN", 700_000, "BUY_YES"),
        ("INELIGIBLE", 700_000, "BUY_YES"),
        ("MODEL_FAILURE", None, "ABSTAIN"),
        ("BOOK_FAILURE", 700_000, None),
    ),
)
def test_disposition_rejects_other_signal_shapes(
    cycle: dict[str, Any],
    disposition: str,
    probability: int | None,
    action: str | None,
) -> None:
    arm = _zero_fill_ai(cycle, disposition)
    arm.update(signal_probability_micros=probability, signal_action=action)
    if disposition == "FILLED":
        arm = _filled_ai(cycle)
        arm.update(signal_probability_micros=probability, signal_action=action)

    with pytest.raises(ValidationError):
        ArmResult.model_validate(arm)


def test_marked_fill_recomputes_economics(cycle: dict[str, Any]) -> None:
    arm = _filled_ai(cycle)
    mark_micros = 600_000
    trading_pnl = (
        arm["quantity"] * (mark_micros - arm["entry_vwap_micros"])
        - arm["fees_microdollars"]
    )
    arm.update(
        mark_micros=mark_micros,
        payout_micros=None,
        trading_pnl_microdollars=trading_pnl,
        final_cash_microdollars=arm["initial_cash_microdollars"] + trading_pnl,
        economic_pnl_microdollars=trading_pnl - arm["model_cost_microdollars"],
        forecast_status="PENDING_RESOLUTION",
        brier_micros=None,
    )

    assert ArmResult.model_validate(arm)
    arm["trading_pnl_microdollars"] += 1
    with pytest.raises(ValidationError):
        ArmResult.model_validate(arm)


@pytest.mark.parametrize(
    "field",
    (
        "paired_trading_pnl_delta_microdollars",
        "paired_economic_pnl_delta_microdollars",
        "paired_brier_delta_micros",
    ),
)
def test_observation_recomputes_paired_deltas(
    cycle: dict[str, Any], field: str
) -> None:
    observation = deepcopy(cycle["expected"]["observation_result"])
    observation[field] += 1

    with pytest.raises(ValidationError):
        _validate_json(ObservationResult, observation)


def test_observation_recomputes_arm_brier(cycle: dict[str, Any]) -> None:
    observation = deepcopy(cycle["expected"]["observation_result"])
    observation["deterministic"]["brier_micros"] += 1
    observation["paired_brier_delta_micros"] += 1

    with pytest.raises(ValidationError):
        _validate_json(ObservationResult, observation)


def test_strict_observation_json_loader_preserves_json_validation(
    cycle: dict[str, Any],
) -> None:
    raw = json.dumps(cycle["expected"]["observation_result"])

    assert ObservationResult.model_validate_json_strict(raw) == (
        ObservationResult.model_validate_json(raw)
    )


def test_strict_json_rejects_duplicate_top_level_arm(cycle: dict[str, Any]) -> None:
    observation = cycle["expected"]["observation_result"]
    raw = json.dumps({key: value for key, value in observation.items() if key != "ai"})
    duplicate_prefix = (
        '{"ai":'
        + json.dumps(observation["ai"])
        + ',"ai":'
        + json.dumps(observation["ai"])
        + ","
    )
    raw = duplicate_prefix + raw[1:]

    with pytest.raises(ValueError, match="duplicate JSON key: ai"):
        ObservationResult.model_validate_json_strict(raw)


def test_strict_json_rejects_duplicate_nested_field(cycle: dict[str, Any]) -> None:
    raw = json.dumps(cycle["expected"]["observation_result"])
    raw = raw.replace('"arm": "AI"', '"arm": "AI", "arm": "AI"', 1)

    with pytest.raises(ValueError, match="duplicate JSON key: arm"):
        ObservationResult.model_validate_json_strict(raw)


def test_fixture_loader_rejects_duplicate_raw_json_key() -> None:
    raw = '{"schema_version":1,"schema_version":1}'

    with pytest.raises(ValueError, match="duplicate JSON key: schema_version"):
        polymarket_models.load_strict_json(raw)


def _buy_no_arm(
    *, market_yes_payout_micros: int | None, market_yes_mark_micros: int | None
) -> dict[str, Any]:
    quantity = 10
    entry_vwap_micros = 400_000
    fees_microdollars = 100_000
    initial_cash_microdollars = 10_000_000
    market_yes_valuation = (
        market_yes_payout_micros
        if market_yes_payout_micros is not None
        else market_yes_mark_micros
    )
    assert market_yes_valuation is not None
    held_no_valuation = MICROS_PER_DOLLAR - market_yes_valuation
    trading_pnl_microdollars = (
        quantity * (held_no_valuation - entry_vwap_micros) - fees_microdollars
    )
    model_cost_microdollars = 500_000
    return {
        "arm": "AI",
        "signal_probability_micros": 300_000,
        "signal_action": "BUY_NO",
        "disposition": "FILLED",
        "entry_side": "NO",
        "entry_vwap_micros": entry_vwap_micros,
        "quantity": quantity,
        "fees_microdollars": fees_microdollars,
        "initial_cash_microdollars": initial_cash_microdollars,
        "final_cash_microdollars": (
            initial_cash_microdollars + trading_pnl_microdollars
        ),
        "mark_micros": market_yes_mark_micros,
        "payout_micros": market_yes_payout_micros,
        "trading_pnl_microdollars": trading_pnl_microdollars,
        "economic_pnl_microdollars": (
            trading_pnl_microdollars - model_cost_microdollars
        ),
        "model_cost_microdollars": model_cost_microdollars,
        "forecast_probability_micros": 300_000,
        "forecast_status": (
            "SCORED" if market_yes_payout_micros is not None else "PENDING_RESOLUTION"
        ),
        "brier_micros": (
            (300_000 - market_yes_payout_micros) ** 2 // MICROS_PER_DOLLAR
            if market_yes_payout_micros is not None
            else None
        ),
    }


@pytest.mark.parametrize(
    ("market_yes_payout_micros", "expected_trading", "expected_economic"),
    (
        (MICROS_PER_DOLLAR, -4_100_000, -4_600_000),
        (0, 5_900_000, 5_400_000),
    ),
)
def test_buy_no_resolved_economics_use_held_token_payout(
    market_yes_payout_micros: int,
    expected_trading: int,
    expected_economic: int,
) -> None:
    arm = ArmResult.model_validate(
        _buy_no_arm(
            market_yes_payout_micros=market_yes_payout_micros,
            market_yes_mark_micros=None,
        )
    )

    assert arm.payout_micros == market_yes_payout_micros
    assert arm.trading_pnl_microdollars == expected_trading
    assert arm.economic_pnl_microdollars == expected_economic
    assert arm.final_cash_microdollars == (
        arm.initial_cash_microdollars + expected_trading
    )


def test_buy_no_unresolved_economics_use_held_token_mark() -> None:
    arm = ArmResult.model_validate(
        _buy_no_arm(market_yes_payout_micros=None, market_yes_mark_micros=250_000)
    )

    assert arm.mark_micros == 250_000
    assert arm.trading_pnl_microdollars == 3_400_000
    assert arm.economic_pnl_microdollars == 2_900_000
    assert arm.final_cash_microdollars == 13_400_000


def _unresolved_observation(cycle: dict[str, Any]) -> dict[str, Any]:
    observation = deepcopy(cycle["expected"]["observation_result"])
    ai = observation["ai"]
    mark_micros = 600_000
    trading_pnl = (
        ai["quantity"] * (mark_micros - ai["entry_vwap_micros"])
        - ai["fees_microdollars"]
    )
    ai.update(
        payout_micros=None,
        mark_micros=mark_micros,
        trading_pnl_microdollars=trading_pnl,
        economic_pnl_microdollars=trading_pnl - ai["model_cost_microdollars"],
        final_cash_microdollars=ai["initial_cash_microdollars"] + trading_pnl,
        forecast_status="PENDING_RESOLUTION",
        brier_micros=None,
    )
    observation["deterministic"].update(
        payout_micros=None,
        forecast_status="PENDING_RESOLUTION",
        brier_micros=None,
    )
    observation.update(
        resolution=None,
        baseline_brier_micros=None,
        paired_trading_pnl_delta_microdollars=trading_pnl,
        paired_economic_pnl_delta_microdollars=(
            trading_pnl - ai["model_cost_microdollars"]
        ),
        paired_brier_delta_micros=None,
    )
    return observation


def test_unresolved_observation_requires_pending_available_forecasts(
    cycle: dict[str, Any],
) -> None:
    observation = _unresolved_observation(cycle)

    assert _validate_json(ObservationResult, observation)

    observation["deterministic"].update(forecast_status="SCORED", brier_micros=202_500)
    with pytest.raises(ValidationError):
        _validate_json(ObservationResult, observation)


def _unavailable_ai(cycle: dict[str, Any], disposition: str) -> dict[str, Any]:
    arm = _zero_fill_ai(cycle, disposition)
    arm.update(
        signal_probability_micros=None,
        signal_action=None,
        forecast_probability_micros=None,
        forecast_status="UNAVAILABLE",
        brier_micros=None,
    )
    return arm


@pytest.mark.parametrize("disposition", ("INELIGIBLE", "MODEL_FAILURE", "BOOK_FAILURE"))
def test_resolved_observation_allows_unavailable_terminal_arm(
    cycle: dict[str, Any], disposition: str
) -> None:
    observation = deepcopy(cycle["expected"]["observation_result"])
    observation["ai"] = _unavailable_ai(cycle, disposition)
    observation.update(
        paired_trading_pnl_delta_microdollars=0,
        paired_economic_pnl_delta_microdollars=-500_000,
        paired_brier_delta_micros=None,
    )

    validated = _validate_json(ObservationResult, observation)

    assert validated.ai.forecast_status == "UNAVAILABLE"
    assert validated.ai.brier_micros is None
    assert validated.deterministic.forecast_status == "SCORED"
    assert validated.baseline_brier_micros == 235_225


@pytest.mark.parametrize(
    ("disposition", "probability", "action", "status", "brier"),
    (
        ("ABSTAIN", 700_000, "ABSTAIN", "UNAVAILABLE", None),
        ("NO_EDGE", 700_000, "BUY_YES", "UNAVAILABLE", None),
        ("MODEL_FAILURE", None, None, "PENDING_RESOLUTION", None),
        ("BOOK_FAILURE", None, None, "SCORED", 1),
        ("INELIGIBLE", None, None, "UNAVAILABLE", 1),
    ),
)
def test_terminal_forecast_table_rejects_contradictions(
    cycle: dict[str, Any],
    disposition: str,
    probability: int | None,
    action: str | None,
    status: str,
    brier: int | None,
) -> None:
    arm = _zero_fill_ai(cycle, disposition)
    arm.update(
        signal_probability_micros=probability,
        signal_action=action,
        forecast_probability_micros=probability,
        forecast_status=status,
        brier_micros=brier,
    )

    with pytest.raises(ValidationError):
        ArmResult.model_validate(arm)


def test_scored_forecast_requires_exact_observation_brier(
    cycle: dict[str, Any],
) -> None:
    observation = deepcopy(cycle["expected"]["observation_result"])
    observation["ai"]["brier_micros"] += 1
    observation["paired_brier_delta_micros"] += 1

    with pytest.raises(ValidationError):
        ObservationResult.model_validate(observation)


def test_standard_json_api_rejects_duplicate_jev_probability(
    cycle: dict[str, Any],
) -> None:
    raw = json.dumps(cycle["jev_decision"])
    raw = raw.replace(
        '"p_yes_micros": 700000',
        '"p_yes_micros": 700000, "p_yes_micros": 1',
    )

    with pytest.raises(ValueError, match="duplicate JSON key: p_yes_micros"):
        JevDecision.model_validate_json(raw)


def test_standard_json_api_rejects_duplicate_observation_arm(
    cycle: dict[str, Any],
) -> None:
    observation = cycle["expected"]["observation_result"]
    raw = json.dumps(observation)
    encoded_ai = json.dumps(observation["ai"])
    raw = raw.replace(f'"ai": {encoded_ai}', f'"ai": {encoded_ai}, "ai": {encoded_ai}')

    with pytest.raises(ValueError, match="duplicate JSON key: ai"):
        ObservationResult.model_validate_json(raw)


def test_standard_json_api_rejects_nested_duplicate_cash(
    cycle: dict[str, Any],
) -> None:
    raw = json.dumps(cycle["expected"]["observation_result"])
    raw = raw.replace(
        '"final_cash_microdollars": 10224000000',
        ('"final_cash_microdollars": 10224000000, "final_cash_microdollars": 0'),
        1,
    )

    with pytest.raises(ValueError, match="duplicate JSON key: final_cash_microdollars"):
        ObservationResult.model_validate_json(raw)


def test_fixture_cross_hash_bindings_validate(cycle: dict[str, Any]) -> None:
    polymarket_models.validate_feasibility_fixture(cycle)


def test_fixture_cross_hash_rejects_raw_mutation_with_updated_self_hash(
    cycle: dict[str, Any],
) -> None:
    mutated = deepcopy(cycle)
    mutated["raw"]["gamma_bytes"] += " "
    mutated["expected"]["artifact_sha256"]["gamma_bytes"] = hashlib.sha256(
        mutated["raw"]["gamma_bytes"].encode()
    ).hexdigest()

    with pytest.raises(ValueError, match="Gamma hash"):
        polymarket_models.validate_feasibility_fixture(mutated)


def test_fixture_cross_hash_rejects_independent_canonical_literal(
    cycle: dict[str, Any],
) -> None:
    mutated = deepcopy(cycle)
    mutated["expected"]["canonical_sha256"]["jev_decision"] = "0" * 64

    with pytest.raises(ValueError, match="canonical hash"):
        polymarket_models.validate_feasibility_fixture(mutated)


def test_fixture_cross_hash_rejects_model_response_rebound_only_to_self_hash(
    cycle: dict[str, Any],
) -> None:
    mutated = deepcopy(cycle)
    response = json.loads(mutated["model_artifacts"]["jev_response_bytes"])
    response["result"]["action"] = "BUY_NO"
    mutated_response = json.dumps(response, separators=(",", ":"), sort_keys=True)
    mutated["model_artifacts"]["jev_response_bytes"] = mutated_response
    mutated["expected"]["artifact_sha256"]["jev_response_bytes"] = hashlib.sha256(
        mutated_response.encode()
    ).hexdigest()

    with pytest.raises(ValueError, match="Jev response"):
        polymarket_models.validate_feasibility_fixture(mutated)


@pytest.mark.parametrize("available_cash", (0, 4_099_999))
def test_filled_arm_rejects_unfunded_notional_and_fees(
    available_cash: int,
) -> None:
    arm = _buy_no_arm(market_yes_payout_micros=0, market_yes_mark_micros=None)
    arm["initial_cash_microdollars"] = available_cash
    arm["final_cash_microdollars"] = available_cash + arm["trading_pnl_microdollars"]

    with pytest.raises(ValidationError, match="notional"):
        ArmResult.model_validate(arm)


def test_filled_arm_accepts_exact_notional_and_fee_funding_boundary() -> None:
    arm = _buy_no_arm(market_yes_payout_micros=0, market_yes_mark_micros=None)
    required_cash = (
        arm["quantity"] * arm["entry_vwap_micros"] + arm["fees_microdollars"]
    )
    arm["initial_cash_microdollars"] = required_cash
    arm["final_cash_microdollars"] = required_cash + arm["trading_pnl_microdollars"]

    assert ArmResult.model_validate(arm)


@pytest.mark.parametrize(
    ("model_type", "raw", "mutate"),
    (
        (
            BookSnapshot,
            "precheck_book",
            lambda value: value["bids"][0].update(quantity=2**63),
        ),
        (
            BookSnapshot,
            "precheck_book",
            lambda value: value["bids"][0].update(quantity=2**63 + 1),
        ),
        (
            ArmResult,
            "expected.arm_results.ai",
            lambda value: value.update(trading_pnl_microdollars=2**63),
        ),
        (
            ArmResult,
            "expected.arm_results.ai",
            lambda value: value.update(trading_pnl_microdollars=-(2**63) - 1),
        ),
        (
            ObservationResult,
            "expected.observation_result",
            lambda value: value.update(paired_trading_pnl_delta_microdollars=2**63),
        ),
        (
            ObservationResult,
            "expected.observation_result",
            lambda value: value.update(
                paired_trading_pnl_delta_microdollars=-(2**63) - 1
            ),
        ),
    ),
)
def test_contract_integer_aliases_reject_values_outside_signed_int64(
    cycle: dict[str, Any],
    model_type: type[BaseModel],
    raw: str,
    mutate: Any,
) -> None:
    value: Any = deepcopy(cycle)
    for part in raw.split("."):
        value = value[part]
    mutate(value)

    with pytest.raises(ValidationError):
        model_type.model_validate(value)


def test_valid_contract_models_always_canonical_hash(cycle: dict[str, Any]) -> None:
    models = (
        BookSnapshot.model_validate_json(json.dumps(cycle["precheck_book"])),
        ArmResult.model_validate_json(
            json.dumps(cycle["expected"]["arm_results"]["ai"])
        ),
        ObservationResult.model_validate_json(
            json.dumps(cycle["expected"]["observation_result"])
        ),
    )

    for model in models:
        assert len(_canonical_sha(model)) == 64


@pytest.mark.parametrize(
    "changes",
    (
        {"forecast_status": "PENDING_RESOLUTION", "brier_micros": None},
        {"forecast_status": "UNAVAILABLE", "brier_micros": None},
        {"forecast_status": "SCORED", "brier_micros": 1},
    ),
)
def test_standalone_payout_arm_requires_exact_scored_forecast(
    cycle: dict[str, Any], changes: dict[str, object]
) -> None:
    arm = {**_filled_ai(cycle), **changes}

    with pytest.raises(ValidationError):
        ArmResult.model_validate(arm)


def test_standalone_marked_arm_rejects_scored_forecast(cycle: dict[str, Any]) -> None:
    arm = _buy_no_arm(market_yes_payout_micros=None, market_yes_mark_micros=250_000)
    arm.update(forecast_status="SCORED", brier_micros=2_500)

    with pytest.raises(ValidationError):
        ArmResult.model_validate(arm)


@pytest.mark.parametrize("valuation", ("mark_micros", "payout_micros"))
def test_standalone_unavailable_arm_accepts_terminal_valuation(
    cycle: dict[str, Any], valuation: str
) -> None:
    arm = _unavailable_ai(cycle, "MODEL_FAILURE")
    arm[valuation] = 1_000_000 if valuation == "payout_micros" else 500_000

    validated = ArmResult.model_validate(arm)

    assert validated.forecast_status == "UNAVAILABLE"
    assert validated.brier_micros is None


@pytest.mark.parametrize(
    "changes",
    (
        {"forecast_status": "SCORED", "brier_micros": 90_000},
        {"forecast_status": "UNAVAILABLE", "brier_micros": None},
    ),
)
def test_standalone_nonfilled_available_arm_requires_pending_forecast(
    cycle: dict[str, Any], changes: dict[str, object]
) -> None:
    arm = {**_zero_fill_ai(cycle), **changes}

    with pytest.raises(ValidationError):
        ArmResult.model_validate(arm)


@pytest.mark.parametrize(
    "section",
    (
        "schema_version",
        "raw",
        "sanitized_evidence",
        "causal_history",
        "precheck_book",
        "execution_book",
        "model_artifacts",
        "research_brief",
        "jev_decision",
        "model_costs_microdollars",
        "economics",
        "resolution",
        "expected",
    ),
)
def test_fixture_envelope_rejects_missing_required_root_sections(
    cycle: dict[str, Any], section: str
) -> None:
    mutated = deepcopy(cycle)
    del mutated[section]

    with pytest.raises((ValidationError, ValueError)):
        polymarket_models.validate_feasibility_fixture(mutated)


def test_fixture_envelope_rejects_unknown_root_section(cycle: dict[str, Any]) -> None:
    mutated = {**deepcopy(cycle), "unexpected": True}

    with pytest.raises((ValidationError, ValueError)):
        polymarket_models.validate_feasibility_fixture(mutated)


def _rehash_artifact(fixture: dict[str, Any], name: str) -> None:
    for section in ("raw", "model_artifacts"):
        if name in fixture[section]:
            fixture["expected"]["artifact_sha256"][name] = hashlib.sha256(
                fixture[section][name].encode()
            ).hexdigest()
            return
    raise AssertionError(name)


@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_fixture_rejects_detached_model_request_with_updated_self_hash(
    cycle: dict[str, Any], provider: str
) -> None:
    mutated = deepcopy(cycle)
    artifact = f"{provider}_request_bytes"
    request = json.loads(mutated["model_artifacts"][artifact])
    request["model"] += "-detached"
    mutated["model_artifacts"][artifact] = json.dumps(
        request, separators=(",", ":"), sort_keys=True
    )
    _rehash_artifact(mutated, artifact)

    with pytest.raises((ValidationError, ValueError)):
        polymarket_models.validate_feasibility_fixture(mutated)


@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_fixture_rejects_price_leakage_in_model_request(
    cycle: dict[str, Any], provider: str
) -> None:
    mutated = deepcopy(cycle)
    artifact = f"{provider}_request_bytes"
    request = json.loads(mutated["model_artifacts"][artifact])
    request["bids"] = [{"price_micros": 500_000}]
    mutated["model_artifacts"][artifact] = json.dumps(
        request, separators=(",", ":"), sort_keys=True
    )
    _rehash_artifact(mutated, artifact)

    with pytest.raises((ValidationError, ValueError), match="price/book|exactly"):
        polymarket_models.validate_feasibility_fixture(mutated)


@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_fixture_rejects_model_cost_detached_from_response_and_ai_arm(
    cycle: dict[str, Any], provider: str
) -> None:
    mutated = deepcopy(cycle)
    mutated["model_costs_microdollars"][provider] += 1

    with pytest.raises((ValidationError, ValueError)):
        polymarket_models.validate_feasibility_fixture(mutated)


def test_fixture_rejects_empty_causal_history(cycle: dict[str, Any]) -> None:
    mutated = deepcopy(cycle)
    mutated["causal_history"] = []

    with pytest.raises((ValidationError, ValueError)):
        polymarket_models.validate_feasibility_fixture(mutated)


def test_fixture_rejects_history_midpoint_outside_probability_bounds(
    cycle: dict[str, Any],
) -> None:
    mutated = deepcopy(cycle)
    mutated["causal_history"][0]["midpoint_micros"] = 1_000_001

    with pytest.raises((ValidationError, ValueError)):
        polymarket_models.validate_feasibility_fixture(mutated)


def test_fixture_rejects_noncausal_history(cycle: dict[str, Any]) -> None:
    mutated = deepcopy(cycle)
    mutated["causal_history"][0]["observed_at"] = mutated["sanitized_evidence"][
        "cutoff_at"
    ]

    with pytest.raises((ValidationError, ValueError)):
        polymarket_models.validate_feasibility_fixture(mutated)


@pytest.mark.parametrize("field", ("payout", "resolved_at"))
def test_fixture_rejects_resolution_detached_from_observation(
    cycle: dict[str, Any], field: str
) -> None:
    mutated = deepcopy(cycle)
    mutated["resolution"][field] = 0 if field == "payout" else "2026-10-04T20:00:01Z"

    with pytest.raises((ValidationError, ValueError)):
        polymarket_models.validate_feasibility_fixture(mutated)


def test_fixture_rejects_expected_arm_detached_from_observation(
    cycle: dict[str, Any],
) -> None:
    mutated = deepcopy(cycle)
    mutated["expected"]["arm_results"]["ai"]["brier_micros"] += 1

    with pytest.raises((ValidationError, ValueError)):
        polymarket_models.validate_feasibility_fixture(mutated)


def test_fixture_rejects_starting_capital_detached_from_arms(
    cycle: dict[str, Any],
) -> None:
    mutated = deepcopy(cycle)
    mutated["economics"]["starting_cash_microdollars"] += 1

    with pytest.raises((ValidationError, ValueError)):
        polymarket_models.validate_feasibility_fixture(mutated)


def test_fixture_rejects_openai_response_detached_from_research_brief(
    cycle: dict[str, Any],
) -> None:
    mutated = deepcopy(cycle)
    response = json.loads(mutated["model_artifacts"]["openai_response_bytes"])
    response["output"]["quality"] = "LOW"
    mutated["model_artifacts"]["openai_response_bytes"] = json.dumps(
        response, separators=(",", ":"), sort_keys=True
    )
    _rehash_artifact(mutated, "openai_response_bytes")

    with pytest.raises((ValidationError, ValueError), match="OpenAI response"):
        polymarket_models.validate_feasibility_fixture(mutated)


def test_fixture_rejects_book_bytes_detached_from_normalized_snapshot(
    cycle: dict[str, Any],
) -> None:
    mutated = deepcopy(cycle)
    raw_book = json.loads(mutated["raw"]["precheck_book_bytes"])
    raw_book["bids"][0]["size"] = "9999"
    mutated["raw"]["precheck_book_bytes"] = json.dumps(
        raw_book, separators=(",", ":"), sort_keys=True
    )
    _rehash_artifact(mutated, "precheck_book_bytes")
    new_hash = mutated["expected"]["artifact_sha256"]["precheck_book_bytes"]
    mutated["precheck_book"]["source_sha256"] = new_hash
    book = BookSnapshot.model_validate_json(json.dumps(mutated["precheck_book"]))
    mutated["expected"]["canonical_sha256"]["precheck_book"] = _canonical_sha(book)

    with pytest.raises((ValidationError, ValueError)):
        polymarket_models.validate_feasibility_fixture(mutated)


def test_fixture_integer_fields_reject_values_outside_signed_int64(
    cycle: dict[str, Any],
) -> None:
    too_large = deepcopy(cycle)
    too_large["model_costs_microdollars"]["openai"] = 2**63
    too_small = deepcopy(cycle)
    too_small["economics"]["starting_cash_microdollars"] = -(2**63) - 1

    with pytest.raises((ValidationError, ValueError)):
        polymarket_models.validate_feasibility_fixture(too_large)
    with pytest.raises((ValidationError, ValueError)):
        polymarket_models.validate_feasibility_fixture(too_small)

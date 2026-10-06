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
        {"mark_micros": 500_000},
        {"payout_micros": MICROS_PER_DOLLAR},
        {"trading_pnl_microdollars": 1},
        {"economic_pnl_microdollars": 1},
    ),
)
def test_nonfilled_arm_rejects_entry_or_profit_fields(
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
        forecast_status="SCORED" if probability is not None else "UNAVAILABLE",
        brier_micros=(probability - MICROS_PER_DOLLAR) ** 2 // MICROS_PER_DOLLAR
        if probability is not None
        else None,
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

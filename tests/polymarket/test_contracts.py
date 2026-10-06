from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.polymarket.models import (
    ArmResult,
    BookSnapshot,
    EvidencePacket,
    JevDecision,
    ObservationResult,
    ResearchBrief,
)

FIXTURE_PATH = (
    Path(__file__).parents[1] / "fixtures/polymarket/feasibility_cycle.json"
)


@pytest.fixture(scope="module")
def cycle() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


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
        {"price_micros": price, "quantity": quantity}
        for price, quantity in levels
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
    arm = {
        **cycle["expected"]["arm_results"]["ai"],
        "signal_action": "ABSTAIN",
        "disposition": "ABSTAIN",
        "entry_side": None,
        "entry_vwap_micros": None,
        "quantity": 0,
        "fees_microdollars": 0,
    }

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
    arm = {
        **cycle["expected"]["arm_results"]["ai"],
        "signal_action": "ABSTAIN",
        "disposition": "ABSTAIN",
        "entry_side": None,
        "entry_vwap_micros": None,
        "quantity": 0,
        "fees_microdollars": 0,
        **entry,
    }

    with pytest.raises(ValidationError):
        _validate_json(ArmResult, arm)


def test_observation_requires_exactly_one_named_arm(cycle: dict[str, Any]) -> None:
    observation = deepcopy(cycle["expected"]["observation_result"])
    observation["ai"]["arm"] = "DETERMINISTIC"

    with pytest.raises(ValidationError):
        _validate_json(ObservationResult, observation)

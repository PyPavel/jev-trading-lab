from __future__ import annotations

import importlib
from decimal import Decimal
from types import ModuleType

import pytest


def _values() -> ModuleType:
    try:
        return importlib.import_module("jev_trading_lab.core.values")
    except ModuleNotFoundError:
        pytest.fail("fixed-point values module must exist")


def test_money_roundtrip() -> None:
    values = _values()

    for source in (Decimal("1.234567"), "1.234567"):
        amount = values.micro_dollars(source)
        assert amount == 1_234_567
        assert values.decimal_dollars(amount) == Decimal("1.234567")


@pytest.mark.parametrize("source", ("-0.000001", "1.000001"))
def test_probability_bounds(source: str) -> None:
    values = _values()

    with pytest.raises(ValueError):
        values.probability_micros(source)

    assert values.probability_micros("0") == 0
    assert values.probability_micros("1") == values.MICRO


def test_rounding_policy() -> None:
    values = _values()

    assert values.ROUNDING_POLICY == "ROUND_HALF_EVEN"
    assert values.micro_dollars("0.0000005") == 0
    assert values.micro_dollars("0.0000015") == 2
    assert values.micro_dollars("1.0000025") == 1_000_002
    assert values.micro_dollars("1.0000035") == 1_000_004


def test_price_and_probability_are_distinct_tagged_values() -> None:
    values = _values()

    price = values.price_micros("0.25")
    probability = values.probability_micros("0.25")

    assert price == probability == 250_000
    assert type(price) is values.PriceMicros
    assert type(probability) is values.ProbabilityMicros
    assert type(price) is not type(probability)


def test_quantity_uses_positive_caller_declared_scale() -> None:
    values = _values()

    quantity = values.scaled_quantity("1.2345", scale=1_000)

    assert quantity.units == 1_234
    assert quantity.scale == 1_000
    with pytest.raises(ValueError):
        values.scaled_quantity("0", scale=1_000)
    with pytest.raises(ValueError):
        values.scaled_quantity("1", scale=0)


@pytest.mark.parametrize("source", (True, False, 1.25, "NaN", "Infinity"))
def test_trust_boundary_rejects_bool_float_and_non_finite(source: object) -> None:
    values = _values()

    with pytest.raises((TypeError, ValueError)):
        values.micro_dollars(source)


def test_money_and_price_reject_negative_values() -> None:
    values = _values()

    with pytest.raises(ValueError):
        values.micro_dollars("-0.000001")
    with pytest.raises(ValueError):
        values.price_micros("-0.000001")

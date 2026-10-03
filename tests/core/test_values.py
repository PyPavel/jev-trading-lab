from __future__ import annotations

import importlib
import sqlite3
from decimal import Decimal, localcontext
from types import ModuleType

import pytest


class _AlwaysInRangeInt(int):
    def __ge__(self, other: object) -> bool:
        return True

    def __le__(self, other: object) -> bool:
        return True


class _CustomInt(int):
    pass


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


def test_money_roundtrip_is_exact_under_low_decimal_precision() -> None:
    values = _values()
    maximum = values.MicroDollars(9_223_372_036_854_775_807)
    expected = Decimal("9223372036854.775807")

    for precision in (1, 2):
        with localcontext() as context:
            context.prec = precision
            assert values.decimal_dollars(maximum) == expected


def test_tagged_constructors_enforce_domain_invariants() -> None:
    values = _values()

    invalid_calls = (
        lambda: values.MicroDollars(-1),
        lambda: values.MicroDollars(True),
        lambda: values.MicroDollars(values.MAX_INT64 + 1),
        lambda: values.PriceMicros(-1),
        lambda: values.PriceMicros(False),
        lambda: values.ProbabilityMicros(-1),
        lambda: values.ProbabilityMicros(values.MICRO + 1),
        lambda: values.ProbabilityMicros(True),
        lambda: values.ScaledQuantity(units=0, scale=1),
        lambda: values.ScaledQuantity(units=1, scale=0),
        lambda: values.ScaledQuantity(units=True, scale=1),
        lambda: values.ScaledQuantity(units=1, scale=True),
        lambda: values.ScaledQuantity(units=values.MAX_INT64 + 1, scale=1),
        lambda: values.ScaledQuantity(units=1, scale=values.MAX_INT64 + 1),
    )
    for invalid_call in invalid_calls:
        with pytest.raises((TypeError, ValueError)):
            invalid_call()

    forged_invalid_money = int.__new__(values.MicroDollars, -1)
    with pytest.raises(ValueError):
        values.decimal_dollars(forged_invalid_money)


def test_tagged_constructors_reject_hostile_int_subclass() -> None:
    values = _values()

    with pytest.raises(TypeError):
        values.MicroDollars(_AlwaysInRangeInt(-1))


@pytest.mark.parametrize(
    ("constructor_name", "source_name"),
    (
        ("MicroDollars", "ProbabilityMicros"),
        ("MicroDollars", "PriceMicros"),
        ("ProbabilityMicros", "MicroDollars"),
        ("ProbabilityMicros", "PriceMicros"),
        ("PriceMicros", "MicroDollars"),
        ("PriceMicros", "ProbabilityMicros"),
    ),
)
def test_tagged_constructors_reject_cross_tag_inputs(
    constructor_name: str, source_name: str
) -> None:
    values = _values()
    constructor = getattr(values, constructor_name)
    source = getattr(values, source_name)(1)

    with pytest.raises(TypeError):
        constructor(source)


def test_scaled_quantity_rejects_custom_and_tagged_integer_inputs() -> None:
    values = _values()
    tagged = values.MicroDollars(1)

    for units, scale in (
        (_CustomInt(1), 1),
        (tagged, 1),
        (1, _CustomInt(1)),
        (1, tagged),
    ):
        with pytest.raises(TypeError):
            values.ScaledQuantity(units=units, scale=scale)

    with pytest.raises(TypeError):
        values.scaled_quantity("1", scale=_CustomInt(1))
    with pytest.raises(TypeError):
        values.scaled_quantity("1", scale=tagged)


def test_hostile_int_subclass_cannot_persist_negative_micro_dollars() -> None:
    values = _values()

    try:
        amount = values.MicroDollars(_AlwaysInRangeInt(-1))
    except TypeError:
        return

    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE amounts (value INTEGER NOT NULL)")
        connection.execute("INSERT INTO amounts VALUES (?)", (amount,))
        persisted = connection.execute("SELECT value FROM amounts").fetchone()

    assert persisted is not None
    assert persisted[0] >= 0, "constructor allowed a negative value to reach SQLite"


def test_sqlite_integer_limit_is_accepted_and_overflow_rejected() -> None:
    values = _values()

    assert values.MicroDollars(values.MAX_INT64) == values.MAX_INT64
    assert values.PriceMicros(values.MAX_INT64) == values.MAX_INT64
    assert (
        values.ScaledQuantity(values.MAX_INT64, values.MAX_INT64).units
        == values.MAX_INT64
    )
    assert values.micro_dollars("9223372036854.775807") == values.MAX_INT64
    assert values.price_micros("9223372036854.775807") == values.MAX_INT64
    assert (
        values.scaled_quantity(str(values.MAX_INT64), scale=1).units == values.MAX_INT64
    )

    with pytest.raises(ValueError):
        values.micro_dollars("9223372036854.775808")
    with pytest.raises(ValueError):
        values.price_micros("9223372036854.775808")
    with pytest.raises(ValueError):
        values.scaled_quantity(str(values.MAX_INT64 + 1), scale=1)


def test_decimal_input_size_and_exponent_are_bounded() -> None:
    values = _values()

    with pytest.raises(ValueError):
        values.micro_dollars("1e1000000")
    with pytest.raises(ValueError):
        values.micro_dollars("0." + "0" * values.MAX_DECIMAL_INPUT_LENGTH)
    with pytest.raises(ValueError):
        values.micro_dollars(Decimal((0, (1,), values.MAX_DECIMAL_EXPONENT + 1)))

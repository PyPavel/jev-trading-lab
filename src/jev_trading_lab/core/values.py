"""Deterministic fixed-point values for economic inputs.

Trust-boundary conversions round exact decimal inputs with ``ROUND_HALF_EVEN``.
Persisted integers use SQLite's non-negative signed 64-bit range. Decimal inputs
are limited to 128 characters/digits and an absolute exponent of 128.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Self

MICRO = 1_000_000
MIN_INT64 = 0
MAX_INT64 = 2**63 - 1
MAX_DECIMAL_INPUT_LENGTH = 128
MAX_DECIMAL_EXPONENT = 128
ROUNDING_POLICY = ROUND_HALF_EVEN


def _validate_integer(value: int, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


class MicroDollars(int):
    """A SQLite-safe non-negative count of millionths of a dollar."""

    def __new__(cls, value: int) -> Self:
        return super().__new__(
            cls,
            _validate_integer(
                value, name="micro-dollars", minimum=MIN_INT64, maximum=MAX_INT64
            ),
        )


class ProbabilityMicros(int):
    """A probability in integer millionths, inclusive from zero to one million."""

    def __new__(cls, value: int) -> Self:
        return super().__new__(
            cls,
            _validate_integer(
                value, name="probability micros", minimum=MIN_INT64, maximum=MICRO
            ),
        )


class PriceMicros(int):
    """A SQLite-safe non-negative price in integer millionths."""

    def __new__(cls, value: int) -> Self:
        return super().__new__(
            cls,
            _validate_integer(
                value, name="price micros", minimum=MIN_INT64, maximum=MAX_INT64
            ),
        )


@dataclass(frozen=True, slots=True)
class ScaledQuantity:
    """A positive SQLite-safe quantity and caller-declared venue scale."""

    units: int
    scale: int

    def __post_init__(self) -> None:
        _validate_integer(
            self.units, name="quantity units", minimum=1, maximum=MAX_INT64
        )
        _validate_integer(
            self.scale, name="quantity scale", minimum=1, maximum=MAX_INT64
        )


def _decimal(value: Decimal | str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str)):
        raise TypeError("fixed-point input must be Decimal or str")
    if isinstance(value, str) and len(value) > MAX_DECIMAL_INPUT_LENGTH:
        raise ValueError("fixed-point input is too long")
    try:
        result = Decimal(value)
    except Exception as error:
        raise ValueError("fixed-point input must be a decimal number") from error
    if not result.is_finite():
        raise ValueError("fixed-point input must be finite")
    _, digits, exponent = result.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError("fixed-point input must be finite")
    if len(digits) > MAX_DECIMAL_INPUT_LENGTH or abs(exponent) > MAX_DECIMAL_EXPONENT:
        raise ValueError("fixed-point input exceeds decimal size limits")
    return result


def _scaled_integer(value: Decimal, scale: int) -> int:
    numerator, denominator = value.as_integer_ratio()
    quotient, remainder = divmod(numerator * scale, denominator)
    if remainder * 2 > denominator or (remainder * 2 == denominator and quotient % 2):
        quotient += 1
    if quotient > MAX_INT64:
        raise ValueError("scaled value exceeds SQLite integer range")
    return quotient


def micro_dollars(value: Decimal | str) -> MicroDollars:
    """Convert a non-negative dollar amount to integer millionths."""
    amount = _decimal(value)
    if amount < 0:
        raise ValueError("money must be non-negative")
    return MicroDollars(_scaled_integer(amount, MICRO))


def decimal_dollars(value: MicroDollars) -> Decimal:
    """Convert integer millionths of a dollar to an exact decimal amount."""
    if not isinstance(value, MicroDollars):
        raise TypeError("value must be MicroDollars")
    integer = _validate_integer(
        value, name="micro-dollars", minimum=MIN_INT64, maximum=MAX_INT64
    )
    digits = tuple(int(digit) for digit in str(integer))
    return Decimal((0, digits, -6))


def probability_micros(value: Decimal | str) -> ProbabilityMicros:
    """Convert a probability in the inclusive interval [0, 1] to millionths."""
    probability = _decimal(value)
    if not Decimal(0) <= probability <= Decimal(1):
        raise ValueError("probability must be between zero and one")
    return ProbabilityMicros(_scaled_integer(probability, MICRO))


def price_micros(value: Decimal | str) -> PriceMicros:
    """Convert a non-negative price to integer millionths."""
    price = _decimal(value)
    if price < 0:
        raise ValueError("price must be non-negative")
    return PriceMicros(_scaled_integer(price, MICRO))


def scaled_quantity(value: Decimal | str, *, scale: int) -> ScaledQuantity:
    """Convert a positive quantity using a positive caller-declared venue scale."""
    scale = _validate_integer(
        scale, name="quantity scale", minimum=1, maximum=MAX_INT64
    )
    quantity = _decimal(value)
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    units = _scaled_integer(quantity, scale)
    if units <= 0:
        raise ValueError("quantity must be positive at the declared scale")
    return ScaledQuantity(units=units, scale=scale)

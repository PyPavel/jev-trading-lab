"""Deterministic fixed-point values for economic inputs.

Trust-boundary conversions round exact decimal inputs with ``ROUND_HALF_EVEN``.
Domain code receives tagged integer values rather than binary floating-point values.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

MICRO = 1_000_000
ROUNDING_POLICY = ROUND_HALF_EVEN


class MicroDollars(int):
    """A non-negative count of millionths of a dollar."""


class ProbabilityMicros(int):
    """A probability in integer millionths, inclusive from zero to one million."""


class PriceMicros(int):
    """A non-negative price in integer millionths, distinct from probability."""


@dataclass(frozen=True, slots=True)
class ScaledQuantity:
    """A positive quantity represented using its caller-declared venue scale."""

    units: int
    scale: int


def _decimal(value: Decimal | str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str)):
        raise TypeError("fixed-point input must be Decimal or str")
    try:
        result = Decimal(value)
    except Exception as error:
        raise ValueError("fixed-point input must be a decimal number") from error
    if not result.is_finite():
        raise ValueError("fixed-point input must be finite")
    return result


def _scaled_integer(value: Decimal, scale: int) -> int:
    numerator, denominator = value.as_integer_ratio()
    quotient, remainder = divmod(numerator * scale, denominator)
    if remainder * 2 > denominator or (remainder * 2 == denominator and quotient % 2):
        quotient += 1
    return quotient


def micro_dollars(value: Decimal | str) -> MicroDollars:
    """Convert a non-negative dollar amount to integer millionths."""
    amount = _decimal(value)
    if amount < 0:
        raise ValueError("money must be non-negative")
    return MicroDollars(_scaled_integer(amount, MICRO))


def decimal_dollars(value: MicroDollars) -> Decimal:
    """Convert integer millionths of a dollar to an exact decimal amount."""
    if isinstance(value, bool) or not isinstance(value, MicroDollars):
        raise TypeError("value must be MicroDollars")
    return Decimal(int(value)) / MICRO


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
    if isinstance(scale, bool) or not isinstance(scale, int):
        raise TypeError("quantity scale must be an integer")
    if scale <= 0:
        raise ValueError("quantity scale must be positive")
    quantity = _decimal(value)
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    units = _scaled_integer(quantity, scale)
    if units <= 0:
        raise ValueError("quantity must be positive at the declared scale")
    return ScaledQuantity(units=units, scale=scale)

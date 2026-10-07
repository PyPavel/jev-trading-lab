from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jev_trading_lab.polymarket.control import control_probability
from jev_trading_lab.polymarket.models import CausalHistoryPoint

CUTOFF = datetime(2026, 10, 3, 12, tzinfo=UTC)


def point(minutes_before: int, midpoint_micros: int) -> CausalHistoryPoint:
    return CausalHistoryPoint(
        observed_at=CUTOFF - timedelta(minutes=minutes_before),
        midpoint_micros=midpoint_micros,
    )


def test_control_abstains_with_fewer_than_two_pre_cutoff_points() -> None:
    assert control_probability([], cutoff_at=CUTOFF) is None
    assert control_probability([point(5, 500_000)], cutoff_at=CUTOFF) is None


def test_control_applies_clipped_trend_deterministically() -> None:
    history = [point(10, 450_000), point(5, 500_000)]

    assert control_probability(history, cutoff_at=CUTOFF) == 550_000
    assert control_probability(history, cutoff_at=CUTOFF) == 550_000


@pytest.mark.parametrize(
    ("first", "last", "expected"),
    (
        (100_000, 900_000, 950_000),
        (900_000, 100_000, 50_000),
        (400_000, 550_000, 650_000),
        (600_000, 450_000, 350_000),
    ),
)
def test_control_clips_trend_and_probability(
    first: int, last: int, expected: int
) -> None:
    assert (
        control_probability([point(10, first), point(5, last)], cutoff_at=CUTOFF)
        == expected
    )


def test_control_rejects_noncausal_or_unordered_history() -> None:
    with pytest.raises(ValueError, match="strictly before cutoff"):
        control_probability(
            [
                point(5, 450_000),
                CausalHistoryPoint(observed_at=CUTOFF, midpoint_micros=500_000),
            ],
            cutoff_at=CUTOFF,
        )
    with pytest.raises(ValueError, match="strictly increasing"):
        control_probability([point(5, 500_000), point(10, 450_000)], cutoff_at=CUTOFF)

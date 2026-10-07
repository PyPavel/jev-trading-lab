"""Price-only deterministic control for the Polymarket feasibility slice."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from jev_trading_lab.polymarket.models import CausalHistoryPoint

_MICROS = 1_000_000
_TREND_LIMIT_MICROS = 100_000
_PROBABILITY_FLOOR_MICROS = 50_000
_PROBABILITY_CEILING_MICROS = 950_000


def control_probability(
    history: Sequence[CausalHistoryPoint], *, cutoff_at: datetime
) -> int | None:
    """Return the causal trend forecast, or ``None`` with under two points."""

    times = [point.observed_at for point in history]
    if any(observed_at >= cutoff_at for observed_at in times):
        raise ValueError("control history must be strictly before cutoff")
    if any(left >= right for left, right in zip(times, times[1:])):
        raise ValueError("control history timestamps must be strictly increasing")
    if len(history) < 2:
        return None

    first = history[0].midpoint_micros
    last = history[-1].midpoint_micros
    trend = max(-_TREND_LIMIT_MICROS, min(_TREND_LIMIT_MICROS, last - first))
    probability = last + trend
    return max(
        _PROBABILITY_FLOOR_MICROS,
        min(_PROBABILITY_CEILING_MICROS, probability, _MICROS),
    )

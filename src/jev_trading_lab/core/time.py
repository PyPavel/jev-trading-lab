"""Strict UTC datetime conversion using integer microsecond arithmetic.

The supported timestamp range is exactly Python's UTC-aware ``datetime.min``
through ``datetime.max``. Pre-Unix-epoch values within that range are supported.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

_MICROSECONDS_PER_SECOND = 1_000_000
_SECONDS_PER_DAY = 86_400
_MIN_INT64 = -(2**63)
_MAX_INT64 = 2**63 - 1
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _timedelta_us(delta: timedelta) -> int:
    return (
        delta.days * _SECONDS_PER_DAY + delta.seconds
    ) * _MICROSECONDS_PER_SECOND + delta.microseconds


MIN_DATETIME_US = _timedelta_us(datetime.min.replace(tzinfo=UTC) - _EPOCH)
MAX_DATETIME_US = _timedelta_us(datetime.max.replace(tzinfo=UTC) - _EPOCH)


def datetime_to_utc_us(value: datetime) -> int:
    """Convert an aware zero-offset datetime to integer Unix microseconds exactly."""
    if type(value) is not datetime:
        raise TypeError("timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be UTC-aware")
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must have UTC offset zero")
    result = _timedelta_us(value.replace(tzinfo=UTC) - _EPOCH)
    if not _MIN_INT64 <= result <= _MAX_INT64:
        raise ValueError("timestamp exceeds signed 64-bit range")
    return result


def validate_utc_us(value: int) -> int:
    """Validate a built-in integer Unix timestamp in the supported datetime range."""
    if type(value) is not int:
        raise TypeError("timestamp must be a built-in integer")
    if not _MIN_INT64 <= value <= _MAX_INT64:
        raise ValueError("timestamp exceeds signed 64-bit range")
    if not MIN_DATETIME_US <= value <= MAX_DATETIME_US:
        raise ValueError("timestamp is outside the supported datetime range")
    return value


def utc_us_to_datetime(value: int) -> datetime:
    """Convert supported integer Unix microseconds to an aware UTC datetime."""
    return _EPOCH + timedelta(microseconds=validate_utc_us(value))

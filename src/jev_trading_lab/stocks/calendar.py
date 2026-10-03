"""Pinned, offline NYSE session adapter for run endpoint resolution."""

from __future__ import annotations

from datetime import UTC, date, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Final

from exchange_calendars.exchange_calendar_xnys import (  # type: ignore[import-untyped]
    XNYSExchangeCalendar,
)

from jev_trading_lab.core.ids import canonical_hash
from jev_trading_lab.core.time import datetime_to_utc_us

CALENDAR_PACKAGE_NAME: Final = "exchange-calendars"
CALENDAR_PACKAGE_VERSION: Final = "4.11.2"
# SHA-256 of the reviewed universal wheel pinned by uv.lock.
CALENDAR_PACKAGE_SHA256: Final = (
    "00b8672b567d52ff81ebfb0e9be8b3932f343de98496b65bbda4ee48540a2d36"
)
CALENDAR_NAME: Final = "XNYS"
CALENDAR_TIMEZONE: Final = "America/New_York"
TM_RULE: Final = "tenth_nyse_session_strictly_after_e_plus_30_calendar_days"
TM_RULE_VERSION: Final = "phase1-v1"
TM_RULE_SHA256: Final = canonical_hash(
    "tm-rule", {"rule": TM_RULE, "version": TM_RULE_VERSION}
)
CALENDAR_CONTENT_SHA256: Final = canonical_hash(
    "xnys-calendar-content",
    {
        "calendar_name": CALENDAR_NAME,
        "package_name": CALENDAR_PACKAGE_NAME,
        "package_sha256": CALENDAR_PACKAGE_SHA256,
        "package_version": CALENDAR_PACKAGE_VERSION,
        "timezone": CALENDAR_TIMEZONE,
        "tm_rule": TM_RULE,
        "tm_rule_version": TM_RULE_VERSION,
    },
)
PROVIDER_CONTRACT_SHA256: Final = canonical_hash(
    "provider-contract",
    {
        "calendar_content_sha256": CALENDAR_CONTENT_SHA256,
        "contract_version": "phase1-provider-contract-v1",
        "endpoint": "python:exchange_calendars:XNYS",
        "fallback_enabled": False,
        "precedence": 1,
        "source_name": "exchange-calendars",
        "timestamp_semantics": "pinned_package_session_schedule",
        "validation": "phase1_xnys_calendar_v1",
    },
)


def _installed_version() -> str:
    try:
        installed = version(CALENDAR_PACKAGE_NAME)
    except PackageNotFoundError as error:
        raise RuntimeError("exchange-calendars package is not installed") from error
    if installed != CALENDAR_PACKAGE_VERSION:
        raise RuntimeError(
            "exchange-calendars package version mismatch: "
            f"expected {CALENDAR_PACKAGE_VERSION}, installed {installed}"
        )
    return installed


def _stdlib_utc_datetime(value: object) -> datetime:
    """Convert a pandas-like timestamp at the adapter boundary without floats."""
    if type(value) is datetime:
        converted = value
    else:
        converter = getattr(value, "to_pydatetime", None)
        if not callable(converter):
            raise ValueError("malformed XNYS schedule timestamp")
        converted = converter(warn=False)
    if type(converted) is not datetime:
        raise ValueError("malformed XNYS schedule timestamp")
    if converted.tzinfo is None or converted.utcoffset() is None:
        raise ValueError(
            "malformed XNYS schedule timestamp: close must be timezone-aware"
        )
    return converted.astimezone(UTC)


def _schedule_close_us(schedule: Any, session: date) -> int:
    """Read one official close from a schedule-like object and fail closed."""
    if type(session) is not date:
        raise TypeError("session must be a built-in date")
    try:
        row = schedule.loc[session.isoformat()]
        close = row["close"]
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError(
            "malformed XNYS schedule: official close unavailable"
        ) from error
    if getattr(close, "ndim", 0) != 0:
        raise ValueError("malformed XNYS schedule: duplicate session")
    return datetime_to_utc_us(_stdlib_utc_datetime(close))


class XnysCalendar:
    """Minimal checked adapter over the pinned XNYS calendar."""

    __slots__ = ("_calendar",)

    def __init__(self, *, start: date | None = None, end: date | None = None) -> None:
        _installed_version()
        if start is not None and type(start) is not date:
            raise TypeError("calendar start must be a built-in date")
        if end is not None and type(end) is not date:
            raise TypeError("calendar end must be a built-in date")
        if (start is None) != (end is None):
            raise ValueError("calendar start and end must be provided together")
        if start is not None and end is not None and end < start:
            raise ValueError("calendar end cannot precede start")
        try:
            self._calendar = XNYSExchangeCalendar(start=start, end=end)
        except Exception as error:
            raise ValueError(
                "calendar sessions unavailable for requested range"
            ) from error
        if (
            self._calendar.name != CALENDAR_NAME
            or str(self._calendar.tz) != CALENDAR_TIMEZONE
        ):
            raise RuntimeError(
                "loaded calendar identity does not match pinned XNYS contract"
            )

    def session_dates_strictly_after(
        self, threshold: date, *, count: int
    ) -> tuple[date, ...]:
        if type(threshold) is not date:
            raise TypeError("threshold must be a built-in date")
        if type(count) is not int:
            raise TypeError("count must be a built-in integer")
        if count <= 0:
            raise ValueError("count must be positive")
        try:
            selected = tuple(
                self._calendar.sessions[
                    self._calendar.sessions > threshold.isoformat()
                ][:count]
            )
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ValueError(
                "calendar sessions unavailable for requested range"
            ) from error
        if len(selected) != count:
            raise ValueError("calendar sessions unavailable for requested range")
        result: list[date] = []
        for label in selected:
            converter = getattr(label, "date", None)
            session = converter() if callable(converter) else None
            if type(session) is not date or session <= threshold:
                raise ValueError("malformed XNYS schedule: invalid session label")
            result.append(session)
        if len(set(result)) != count or result != sorted(result):
            raise ValueError(
                "malformed XNYS schedule: sessions must be unique and ordered"
            )
        return tuple(result)

    def official_close_us(self, session: date) -> int:
        if type(session) is not date:
            raise TypeError("session must be a built-in date")
        try:
            close_us = _schedule_close_us(self._calendar.schedule, session)
        except ValueError as error:
            raise ValueError(f"{session.isoformat()} is not an XNYS session") from error
        return close_us

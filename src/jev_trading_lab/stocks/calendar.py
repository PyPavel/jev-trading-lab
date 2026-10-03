"""Frozen XNYS schedule artifact and runtime verification."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from typing import Final

from exchange_calendars.exchange_calendar_xnys import (  # type: ignore[import-untyped]
    XNYSExchangeCalendar,
)

from jev_trading_lab.core.ids import canonical_hash
from jev_trading_lab.core.time import datetime_to_utc_us, validate_utc_us

CALENDAR_PACKAGE_NAME: Final = "exchange-calendars"
CALENDAR_PACKAGE_VERSION: Final = "4.11.2"
CALENDAR_LOCK_SHA256: Final = (
    "4535dbd779b3c903f57235bb5f3a5c5ce12e3bf13f1b8e01da0576f97df212ac"
)
CALENDAR_ARTIFACT_NAME: Final = "xnys-schedule-2020-2036.json"
CALENDAR_ARTIFACT_SHA256: Final = (
    "ade5009ba938836a5d17e298b0e704fc3635b8b1eef329ba4b43cba5c40fc978"
)
CALENDAR_NAME: Final = "XNYS"
CALENDAR_TIMEZONE: Final = "America/New_York"
CALENDAR_TZDATA_VERSION: Final = "2026.5"
SUPPORTED_ENROLLMENT_START_DATE_MIN: Final = date(2020, 1, 1)
SUPPORTED_ENROLLMENT_START_DATE_MAX: Final = date(2035, 12, 31)
SCHEDULE_START_DATE: Final = date(2020, 1, 1)
SCHEDULE_END_DATE: Final = date(2036, 5, 31)
TM_RULE: Final = "tenth_nyse_session_strictly_after_e_plus_30_calendar_days"
TM_RULE_VERSION: Final = "phase1-v1"
TM_RULE_SHA256: Final = canonical_hash(
    "tm-rule", {"rule": TM_RULE, "version": TM_RULE_VERSION}
)
PROVIDER_CONTRACT_VERSION: Final = "phase1-provider-contract-v1"
PROVIDER_CONTRACT_SHA256: Final = canonical_hash(
    "provider-contract",
    {
        "calendar_artifact_sha256": CALENDAR_ARTIFACT_SHA256,
        "calendar_lock_sha256": CALENDAR_LOCK_SHA256,
        "contract_version": PROVIDER_CONTRACT_VERSION,
        "endpoint": "python:exchange_calendars:XNYS",
        "fallback_enabled": False,
        "precedence": 1,
        "source_name": "exchange-calendars",
        "timestamp_semantics": "frozen_schedule_artifact",
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


def _read_artifact_bytes() -> bytes:
    return (
        files("jev_trading_lab")
        .joinpath("artifacts", CALENDAR_ARTIFACT_NAME)
        .read_bytes()
    )


def _load_artifact() -> tuple[tuple[date, int, int], ...]:
    try:
        content = _read_artifact_bytes()
    except OSError as error:
        raise RuntimeError("frozen XNYS schedule artifact is unavailable") from error
    if hashlib.sha256(content).hexdigest() != CALENDAR_ARTIFACT_SHA256:
        raise RuntimeError("frozen XNYS schedule artifact SHA-256 mismatch")
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("frozen XNYS schedule artifact is invalid") from error
    if type(value) is not dict:
        raise RuntimeError("frozen XNYS schedule artifact is invalid")
    expected_metadata = {
        "calendar_name": CALENDAR_NAME,
        "exchange_calendars_version": CALENDAR_PACKAGE_VERSION,
        "provenance": (
            "generated from exchange-calendars XNYS schedule in the frozen "
            "uv.lock environment"
        ),
        "schedule_end_date": SCHEDULE_END_DATE.isoformat(),
        "schedule_start_date": SCHEDULE_START_DATE.isoformat(),
        "schema_version": 1,
        "supported_enrollment_start_date_max": (
            SUPPORTED_ENROLLMENT_START_DATE_MAX.isoformat()
        ),
        "supported_enrollment_start_date_min": (
            SUPPORTED_ENROLLMENT_START_DATE_MIN.isoformat()
        ),
        "timezone": CALENDAR_TIMEZONE,
        "tm_rule_version": TM_RULE_VERSION,
        "tzdata_version": CALENDAR_TZDATA_VERSION,
    }
    if {key: value.get(key) for key in expected_metadata} != expected_metadata:
        raise RuntimeError("frozen XNYS schedule artifact metadata mismatch")
    if (
        set(value) != set(expected_metadata) | {"rows"}
        or type(value["rows"]) is not list
    ):
        raise RuntimeError("frozen XNYS schedule artifact schema mismatch")
    result: list[tuple[date, int, int]] = []
    for row in value["rows"]:
        if (
            type(row) is not list
            or len(row) != 3
            or type(row[0]) is not str
            or type(row[1]) is not int
            or type(row[2]) is not int
        ):
            raise RuntimeError("frozen XNYS schedule artifact row is invalid")
        try:
            session = date.fromisoformat(row[0])
            validate_utc_us(row[1])
            validate_utc_us(row[2])
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "frozen XNYS schedule artifact row is invalid"
            ) from error
        if row[1] >= row[2]:
            raise RuntimeError("frozen XNYS schedule artifact row is invalid")
        result.append((session, row[1], row[2]))
    if (
        not result
        or result != sorted(result)
        or len({row[0] for row in result}) != len(result)
    ):
        raise RuntimeError("frozen XNYS schedule rows must be unique and ordered")
    return tuple(result)


def _runtime_schedule_rows() -> tuple[tuple[date, int, int], ...]:
    _installed_version()
    try:
        calendar = XNYSExchangeCalendar(
            start=SCHEDULE_START_DATE, end=SCHEDULE_END_DATE
        )
    except Exception as error:
        raise RuntimeError("runtime XNYS schedule mismatch") from error
    if calendar.name != CALENDAR_NAME or str(calendar.tz) != CALENDAR_TIMEZONE:
        raise RuntimeError("runtime XNYS schedule mismatch")
    result: list[tuple[date, int, int]] = []
    try:
        for session, row in calendar.schedule.iterrows():
            session_date = session.date()
            open_dt = row["open"].to_pydatetime(warn=False).astimezone(UTC)
            close_dt = row["close"].to_pydatetime(warn=False).astimezone(UTC)
            result.append(
                (
                    session_date,
                    datetime_to_utc_us(open_dt),
                    datetime_to_utc_us(close_dt),
                )
            )
    except Exception as error:
        raise RuntimeError("runtime XNYS schedule mismatch") from error
    return tuple(result)


def verify_runtime_calendar_against_artifact() -> None:
    """Reject package/calendar behavior that differs from the frozen schedule."""
    if _runtime_schedule_rows() != _load_artifact():
        raise RuntimeError("runtime XNYS schedule mismatch")


class XnysCalendar:
    """Read-only adapter over the authoritative packaged schedule artifact."""

    __slots__ = ("_closes", "_sessions")

    def __init__(self) -> None:
        _installed_version()
        rows = _load_artifact()
        self._sessions = tuple(row[0] for row in rows)
        self._closes = {row[0]: row[2] for row in rows}

    def assert_enrollment_start_supported(self, enrollment_start: date) -> None:
        if type(enrollment_start) is not date:
            raise TypeError("enrollment start must be a built-in date")
        if not (
            SUPPORTED_ENROLLMENT_START_DATE_MIN
            <= enrollment_start
            <= SUPPORTED_ENROLLMENT_START_DATE_MAX
        ):
            raise ValueError("calendar sessions unavailable for requested range")

    def session_dates_strictly_after(
        self, threshold: date, *, count: int
    ) -> tuple[date, ...]:
        if type(threshold) is not date:
            raise TypeError("threshold must be a built-in date")
        if type(count) is not int:
            raise TypeError("count must be a built-in integer")
        if count <= 0:
            raise ValueError("count must be positive")
        selected = tuple(session for session in self._sessions if session > threshold)[
            :count
        ]
        if len(selected) != count:
            raise ValueError("calendar sessions unavailable for requested range")
        return selected

    def official_close_us(self, session: date) -> int:
        if type(session) is not date:
            raise TypeError("session must be a built-in date")
        try:
            return self._closes[session]
        except KeyError as error:
            raise ValueError(f"{session.isoformat()} is not an XNYS session") from error

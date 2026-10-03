"""Immutable fixed enrollment and maturation endpoint contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from jev_trading_lab.core.ids import canonical_hash, canonical_json_bytes
from jev_trading_lab.core.manifest import Manifest, revalidate_manifest
from jev_trading_lab.core.time import utc_us_to_datetime, validate_utc_us
from jev_trading_lab.stocks.calendar import (
    CALENDAR_CONTENT_SHA256,
    CALENDAR_NAME,
    CALENDAR_PACKAGE_SHA256,
    CALENDAR_PACKAGE_VERSION,
    CALENDAR_TIMEZONE,
    PROVIDER_CONTRACT_SHA256,
    TM_RULE_SHA256,
    TM_RULE_VERSION,
    XnysCalendar,
)

_ENROLLMENT_WEEKS: Final = 12
_ENROLLMENT_DAYS: Final = 84
_MATURATION_CALENDAR_DAYS: Final = 30
_MATURATION_TRADING_DAYS: Final = 10
_US_PER_DAY: Final = 86_400_000_000


@dataclass(frozen=True, slots=True)
class RunWindow:
    """One run-wide enrollment interval and maturation cutoff."""

    enrollment_start_us: int
    enrollment_end_us: int
    maturation_cutoff_us: int
    timezone: str
    calendar_name: str
    package_version: str
    package_sha256: str
    content_sha256: str
    tm_rule_version: str
    tm_rule_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "enrollment_start_us",
            "enrollment_end_us",
            "maturation_cutoff_us",
        ):
            try:
                validate_utc_us(getattr(self, name))
            except TypeError as error:
                raise TypeError(f"{name} must be a built-in integer") from error
            except ValueError as error:
                raise ValueError(
                    f"{name} is outside the supported datetime range"
                ) from error
        if self.enrollment_end_us - self.enrollment_start_us != (
            _ENROLLMENT_DAYS * _US_PER_DAY
        ):
            raise ValueError("enrollment end must be exactly 12 weeks after start")
        if self.maturation_cutoff_us <= self.enrollment_end_us:
            raise ValueError("maturation cutoff must be after enrollment end")
        identities = (
            ("timezone", self.timezone, CALENDAR_TIMEZONE),
            ("calendar name", self.calendar_name, CALENDAR_NAME),
            ("package version", self.package_version, CALENDAR_PACKAGE_VERSION),
            ("package hash", self.package_sha256, CALENDAR_PACKAGE_SHA256),
            ("content hash", self.content_sha256, CALENDAR_CONTENT_SHA256),
            ("rule version", self.tm_rule_version, TM_RULE_VERSION),
            ("rule hash", self.tm_rule_sha256, TM_RULE_SHA256),
        )
        for label, actual, expected in identities:
            if type(actual) is not str:
                raise TypeError(f"calendar {label} must be a built-in string")
            if actual != expected:
                raise ValueError(f"calendar {label} does not match pinned identity")

    def entries_allowed_at(self, scheduled_for_us: int) -> bool:
        """Return whether an entry slot is before the exclusive endpoint E."""
        try:
            validate_utc_us(scheduled_for_us)
        except TypeError as error:
            raise TypeError("scheduled_for_us must be a built-in integer") from error
        return self.enrollment_start_us <= scheduled_for_us < self.enrollment_end_us

    def to_dict(self) -> dict[str, int | str]:
        return {
            "calendar_name": self.calendar_name,
            "content_sha256": self.content_sha256,
            "enrollment_end_us": self.enrollment_end_us,
            "enrollment_start_us": self.enrollment_start_us,
            "maturation_cutoff_us": self.maturation_cutoff_us,
            "package_sha256": self.package_sha256,
            "package_version": self.package_version,
            "timezone": self.timezone,
            "tm_rule_sha256": self.tm_rule_sha256,
            "tm_rule_version": self.tm_rule_version,
        }

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def run_window_id(self) -> str:
        return f"run-window:{canonical_hash('run-window', self.to_dict())}"


def _assert_manifest_calendar_identity(manifest: Manifest) -> None:
    calendar = manifest.calendar
    actual = (
        calendar.exchange,
        calendar.exchange_calendars_version,
        calendar.timezone,
        calendar.enrollment_weeks,
        calendar.maturation_calendar_days,
        calendar.maturation_trading_days,
        calendar.package_sha256,
        calendar.content_sha256,
        calendar.tm_rule_version,
        calendar.tm_rule_sha256,
    )
    expected = (
        "NYSE",
        CALENDAR_PACKAGE_VERSION,
        CALENDAR_TIMEZONE,
        _ENROLLMENT_WEEKS,
        _MATURATION_CALENDAR_DAYS,
        _MATURATION_TRADING_DAYS,
        CALENDAR_PACKAGE_SHA256,
        CALENDAR_CONTENT_SHA256,
        TM_RULE_VERSION,
        TM_RULE_SHA256,
    )
    if actual != expected:
        raise ValueError("calendar manifest identity mismatch")
    provider = manifest.providers.nyse_calendar
    if provider.contract_sha256 != PROVIDER_CONTRACT_SHA256:
        raise ValueError("calendar manifest identity mismatch")


def resolve_run_window(*, enrollment_start_us: int, manifest: Manifest) -> RunWindow:
    """Resolve E and one T_m from trusted manifest and pinned XNYS schedule."""
    validate_utc_us(enrollment_start_us)
    trusted = revalidate_manifest(manifest)
    _assert_manifest_calendar_identity(trusted)
    start = utc_us_to_datetime(enrollment_start_us)
    try:
        enrollment_end = start + timedelta(weeks=_ENROLLMENT_WEEKS)
        threshold = enrollment_end.date() + timedelta(days=_MATURATION_CALENDAR_DAYS)
        calendar = XnysCalendar(
            start=threshold + timedelta(days=1),
            end=threshold + timedelta(days=31),
        )
    except (OverflowError, ValueError) as error:
        raise ValueError("calendar sessions unavailable for requested range") from error
    sessions = calendar.session_dates_strictly_after(
        threshold, count=_MATURATION_TRADING_DAYS
    )
    return RunWindow(
        enrollment_start_us=enrollment_start_us,
        enrollment_end_us=enrollment_start_us + _ENROLLMENT_DAYS * _US_PER_DAY,
        maturation_cutoff_us=calendar.official_close_us(sessions[-1]),
        timezone=CALENDAR_TIMEZONE,
        calendar_name=CALENDAR_NAME,
        package_version=CALENDAR_PACKAGE_VERSION,
        package_sha256=CALENDAR_PACKAGE_SHA256,
        content_sha256=CALENDAR_CONTENT_SHA256,
        tm_rule_version=TM_RULE_VERSION,
        tm_rule_sha256=TM_RULE_SHA256,
    )

"""Resolver-only trust boundary for fixed enrollment and maturation endpoints."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any, Final

from jev_trading_lab.core.ids import canonical_hash, canonical_json_bytes
from jev_trading_lab.core.manifest import Manifest, revalidate_manifest
from jev_trading_lab.core.time import utc_us_to_datetime, validate_utc_us
from jev_trading_lab.stocks.calendar import (
    CALENDAR_ARTIFACT_SHA256,
    CALENDAR_LOCK_SHA256,
    CALENDAR_NAME,
    CALENDAR_PACKAGE_VERSION,
    CALENDAR_TIMEZONE,
    PROVIDER_CONTRACT_SHA256,
    PROVIDER_CONTRACT_VERSION,
    TM_RULE_SHA256,
    TM_RULE_VERSION,
    XnysCalendar,
)

_ENROLLMENT_WEEKS: Final = 12
_ENROLLMENT_DAYS: Final = 84
_MATURATION_CALENDAR_DAYS: Final = 30
_MATURATION_TRADING_DAYS: Final = 10
_US_PER_DAY: Final = 86_400_000_000
_CREATION_SENTINEL = object()


@dataclass(frozen=True, slots=True)
class _RunWindowData:
    enrollment_start_us: int
    enrollment_end_us: int
    maturation_cutoff_us: int
    timezone: str
    calendar_name: str
    package_version: str
    lock_sha256: str
    artifact_sha256: str
    tm_rule_version: str
    tm_rule_sha256: str


class RunWindow:
    """Read-only resolver-issued view over canonical run-window data."""

    __slots__ = ("_canonical_bytes", "_data", "_run_window_id")
    _canonical_bytes: bytes
    _data: _RunWindowData
    _run_window_id: str

    def __init__(
        self,
        sentinel: object = None,
        canonical_bytes: bytes = b"",
        data: _RunWindowData | None = None,
    ) -> None:
        if sentinel is not _CREATION_SENTINEL or data is None:
            raise TypeError(
                "RunWindow instances can only be created by the run-window resolver"
            )
        object.__setattr__(self, "_canonical_bytes", canonical_bytes)
        object.__setattr__(self, "_data", data)
        object.__setattr__(
            self,
            "_run_window_id",
            f"run-window:{canonical_hash('run-window', asdict(data))}",
        )

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("RunWindow is immutable")

    def __copy__(self) -> RunWindow:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> RunWindow:
        return self

    def __eq__(self, other: object) -> bool:
        return (
            type(other) is RunWindow
            and self.canonical_bytes == other.canonical_bytes
            and self.run_window_id == other.run_window_id
        )

    def __hash__(self) -> int:
        return hash((self.canonical_bytes, self.run_window_id))

    @property
    def enrollment_start_us(self) -> int:
        return self._data.enrollment_start_us

    @property
    def enrollment_end_us(self) -> int:
        return self._data.enrollment_end_us

    @property
    def maturation_cutoff_us(self) -> int:
        return self._data.maturation_cutoff_us

    @property
    def timezone(self) -> str:
        return self._data.timezone

    @property
    def calendar_name(self) -> str:
        return self._data.calendar_name

    @property
    def package_version(self) -> str:
        return self._data.package_version

    @property
    def lock_sha256(self) -> str:
        return self._data.lock_sha256

    @property
    def artifact_sha256(self) -> str:
        return self._data.artifact_sha256

    @property
    def tm_rule_version(self) -> str:
        return self._data.tm_rule_version

    @property
    def tm_rule_sha256(self) -> str:
        return self._data.tm_rule_sha256

    @property
    def canonical_bytes(self) -> bytes:
        return self._canonical_bytes

    @property
    def run_window_id(self) -> str:
        return self._run_window_id

    def entries_allowed_at(self, scheduled_for_us: int) -> bool:
        try:
            validate_utc_us(scheduled_for_us)
        except TypeError as error:
            raise TypeError("scheduled_for_us must be a built-in integer") from error
        return self.enrollment_start_us <= scheduled_for_us < self.enrollment_end_us

    def to_dict(self) -> dict[str, Any]:
        return asdict(self._data)


def _assert_manifest_calendar_identity(manifest: Manifest) -> None:
    calendar = manifest.calendar
    provider = manifest.providers.nyse_calendar
    actual = (
        calendar.exchange,
        calendar.exchange_calendars_version,
        calendar.timezone,
        calendar.enrollment_weeks,
        calendar.maturation_calendar_days,
        calendar.maturation_trading_days,
        calendar.lock_sha256,
        calendar.artifact_sha256,
        calendar.tm_rule_version,
        calendar.tm_rule_sha256,
        provider.contract_version,
        provider.contract_sha256,
    )
    expected = (
        "NYSE",
        CALENDAR_PACKAGE_VERSION,
        CALENDAR_TIMEZONE,
        _ENROLLMENT_WEEKS,
        _MATURATION_CALENDAR_DAYS,
        _MATURATION_TRADING_DAYS,
        CALENDAR_LOCK_SHA256,
        CALENDAR_ARTIFACT_SHA256,
        TM_RULE_VERSION,
        TM_RULE_SHA256,
        PROVIDER_CONTRACT_VERSION,
        PROVIDER_CONTRACT_SHA256,
    )
    if actual != expected:
        raise ValueError("calendar manifest identity mismatch")


def _issue_run_window(data: _RunWindowData) -> RunWindow:
    canonical = canonical_json_bytes(asdict(data))
    return RunWindow(_CREATION_SENTINEL, canonical, data)


def resolve_run_window(*, enrollment_start_us: int, manifest: Manifest) -> RunWindow:
    """Resolve E and T_m exclusively from trusted manifest and frozen schedule."""
    validate_utc_us(enrollment_start_us)
    trusted = revalidate_manifest(manifest)
    _assert_manifest_calendar_identity(trusted)
    start = utc_us_to_datetime(enrollment_start_us)
    calendar = XnysCalendar()
    calendar.assert_enrollment_start_supported(start.date())
    try:
        enrollment_end = start + timedelta(weeks=_ENROLLMENT_WEEKS)
        threshold = enrollment_end.date() + timedelta(days=_MATURATION_CALENDAR_DAYS)
    except (OverflowError, ValueError) as error:
        raise ValueError("calendar sessions unavailable for requested range") from error
    sessions = calendar.session_dates_strictly_after(
        threshold, count=_MATURATION_TRADING_DAYS
    )
    return _issue_run_window(
        _RunWindowData(
            enrollment_start_us=enrollment_start_us,
            enrollment_end_us=enrollment_start_us + _ENROLLMENT_DAYS * _US_PER_DAY,
            maturation_cutoff_us=calendar.official_close_us(sessions[-1]),
            timezone=CALENDAR_TIMEZONE,
            calendar_name=CALENDAR_NAME,
            package_version=CALENDAR_PACKAGE_VERSION,
            lock_sha256=CALENDAR_LOCK_SHA256,
            artifact_sha256=CALENDAR_ARTIFACT_SHA256,
            tm_rule_version=TM_RULE_VERSION,
            tm_rule_sha256=TM_RULE_SHA256,
        )
    )


def revalidate_run_window(window: RunWindow, manifest: Manifest) -> RunWindow:
    """Recompute from enrollment start and verify wrapper bytes, data, and ID."""
    if type(window) is not RunWindow:
        raise TypeError("revalidation requires an exact RunWindow instance")
    try:
        canonical = window._canonical_bytes
        stored_id = window._run_window_id
        data = window._data
    except (AttributeError, TypeError) as error:
        raise ValueError(
            "run-window revalidation failed: wrapper state is invalid"
        ) from error
    if (
        type(canonical) is not bytes
        or type(stored_id) is not str
        or type(data) is not _RunWindowData
    ):
        raise ValueError("run-window revalidation failed: wrapper state is invalid")
    fresh = resolve_run_window(
        enrollment_start_us=data.enrollment_start_us,
        manifest=manifest,
    )
    if (
        fresh.canonical_bytes != canonical
        or fresh.run_window_id != stored_id
        or fresh.to_dict() != asdict(data)
    ):
        raise ValueError(
            "run-window revalidation failed: canonical bytes, data, or ID mismatch"
        )
    return fresh

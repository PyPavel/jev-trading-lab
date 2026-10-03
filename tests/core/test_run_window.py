from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from enum import IntEnum
from importlib.metadata import version
from pathlib import Path

import pytest

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.core.manifest import load_manifest, parse_manifest_bytes
from jev_trading_lab.core.run_window import RunWindow, resolve_run_window
from jev_trading_lab.core.time import datetime_to_utc_us
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
    _schedule_close_us,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config/phase1.example.json"
US_PER_DAY = 86_400_000_000


def _us(value: datetime) -> int:
    return datetime_to_utc_us(value)


def _resolved_fixture() -> RunWindow:
    enrollment_end = datetime(2024, 10, 27, 12, tzinfo=UTC)
    enrollment_start = enrollment_end - timedelta(weeks=12)
    return resolve_run_window(
        enrollment_start_us=_us(enrollment_start),
        manifest=load_manifest(EXAMPLE),
    )


def _window_fields(**changes: object) -> dict[str, object]:
    start = _us(datetime(1960, 1, 1, tzinfo=UTC))
    fields: dict[str, object] = {
        "enrollment_start_us": start,
        "enrollment_end_us": start + 84 * US_PER_DAY,
        "maturation_cutoff_us": start + 85 * US_PER_DAY,
        "timezone": CALENDAR_TIMEZONE,
        "calendar_name": CALENDAR_NAME,
        "package_version": CALENDAR_PACKAGE_VERSION,
        "package_sha256": CALENDAR_PACKAGE_SHA256,
        "content_sha256": CALENDAR_CONTENT_SHA256,
        "tm_rule_version": TM_RULE_VERSION,
        "tm_rule_sha256": TM_RULE_SHA256,
    }
    fields.update(changes)
    return fields


def test_tm_is_tenth_nyse_session_strictly_after_e_plus_30() -> None:
    window = _resolved_fixture()
    calendar = XnysCalendar()
    threshold = date(2024, 11, 26)  # E UTC date + 30 calendar days; itself a session.
    sessions = calendar.session_dates_strictly_after(threshold, count=10)

    assert sessions == (
        date(2024, 11, 27),
        date(2024, 11, 29),
        date(2024, 12, 2),
        date(2024, 12, 3),
        date(2024, 12, 4),
        date(2024, 12, 5),
        date(2024, 12, 6),
        date(2024, 12, 9),
        date(2024, 12, 10),
        date(2024, 12, 11),
    )
    assert threshold not in sessions
    assert window.maturation_cutoff_us == _us(datetime(2024, 12, 11, 21, tzinfo=UTC))
    for session in sessions[:9]:
        assert calendar.official_close_us(session) != window.maturation_cutoff_us


def test_dst_holiday_early_close() -> None:
    calendar = XnysCalendar()

    assert calendar.official_close_us(date(2024, 3, 8)) == _us(
        datetime(2024, 3, 8, 21, tzinfo=UTC)
    )
    assert calendar.official_close_us(date(2024, 3, 11)) == _us(
        datetime(2024, 3, 11, 20, tzinfo=UTC)
    )
    with pytest.raises(ValueError, match="not an XNYS session"):
        calendar.official_close_us(date(2024, 7, 4))
    assert calendar.official_close_us(date(2024, 7, 3)) == _us(
        datetime(2024, 7, 3, 17, tzinfo=UTC)
    )
    with pytest.raises(ValueError, match="not an XNYS session"):
        calendar.official_close_us(date(2024, 11, 28))
    assert calendar.official_close_us(date(2024, 11, 29)) == _us(
        datetime(2024, 11, 29, 18, tzinfo=UTC)
    )


def test_same_tm_for_both_streams() -> None:
    window = _resolved_fixture()

    assert not hasattr(window, "stream")
    assert not hasattr(window, "polymarket_tm_us")
    assert not hasattr(window, "stock_tm_us")
    assert window.to_dict()["maturation_cutoff_us"] == window.maturation_cutoff_us
    assert canonical_json_bytes(window.to_dict()) == window.canonical_bytes
    assert window.run_window_id == _resolved_fixture().run_window_id


def test_enrollment_is_exactly_twelve_weeks_and_boundary_is_exclusive() -> None:
    window = _resolved_fixture()

    assert window.enrollment_end_us - window.enrollment_start_us == 84 * US_PER_DAY
    assert not window.entries_allowed_at(window.enrollment_start_us - 1)
    assert window.entries_allowed_at(window.enrollment_start_us)
    assert window.entries_allowed_at(window.enrollment_end_us - 1)
    assert not window.entries_allowed_at(window.enrollment_end_us)
    assert not window.entries_allowed_at(window.enrollment_end_us + 1)


class _IntSubclass(int):
    pass


class _IntValue(IntEnum):
    VALUE = 1


@pytest.mark.parametrize("bad", (True, _IntSubclass(1), _IntValue.VALUE, 1.0, "1"))
def test_constructor_rejects_non_builtin_integer_timestamps(bad: object) -> None:
    with pytest.raises(TypeError, match="built-in integer"):
        RunWindow(**_window_fields(enrollment_start_us=bad))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"enrollment_end_us": _us(datetime(1960, 1, 1, tzinfo=UTC))}, "12 weeks"),
        ({"enrollment_end_us": _us(datetime(1959, 12, 31, tzinfo=UTC))}, "12 weeks"),
        (
            {"maturation_cutoff_us": _us(datetime(1960, 1, 2, tzinfo=UTC))},
            "after enrollment",
        ),
        ({"timezone": "UTC"}, "timezone"),
        ({"calendar_name": "NYSE"}, "calendar name"),
        ({"package_version": "4.11.1"}, "package version"),
        ({"package_sha256": "0" * 64}, "package hash"),
        ({"content_sha256": "0" * 64}, "content hash"),
        ({"tm_rule_version": "phase1-v0"}, "rule version"),
        ({"tm_rule_sha256": "0" * 64}, "rule hash"),
    ),
)
def test_constructor_enforces_ordering_and_calendar_identity(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        RunWindow(**_window_fields(**changes))  # type: ignore[arg-type]


def test_package_and_calendar_identity_mismatch_with_manifest_is_rejected() -> None:
    manifest_data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    expected = manifest_data["calendar"]
    assert expected["exchange_calendars_version"] == CALENDAR_PACKAGE_VERSION
    assert expected["package_sha256"] == CALENDAR_PACKAGE_SHA256
    assert expected["content_sha256"] == CALENDAR_CONTENT_SHA256
    assert expected["tm_rule_version"] == TM_RULE_VERSION
    assert expected["tm_rule_sha256"] == TM_RULE_SHA256
    assert (
        manifest_data["providers"]["nyse_calendar"]["contract_sha256"]
        == PROVIDER_CONTRACT_SHA256
    )
    assert version("exchange-calendars") == CALENDAR_PACKAGE_VERSION

    for field in (
        "exchange_calendars_version",
        "package_sha256",
        "content_sha256",
        "tm_rule_version",
        "tm_rule_sha256",
    ):
        changed = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        changed["calendar"][field] = (
            "0.0.0" if field == "exchange_calendars_version" else "0" * 64
        )
        if field == "tm_rule_version":
            changed["calendar"][field] = "phase1-mismatch-v1"
        manifest = parse_manifest_bytes(json.dumps(changed).encode())
        with pytest.raises(ValueError, match="calendar manifest identity mismatch"):
            resolve_run_window(
                enrollment_start_us=_us(datetime(2024, 8, 4, 12, tzinfo=UTC)),
                manifest=manifest,
            )

    changed = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    changed["providers"]["nyse_calendar"]["contract_sha256"] = "0" * 64
    manifest = parse_manifest_bytes(json.dumps(changed).encode())
    with pytest.raises(ValueError, match="calendar manifest identity mismatch"):
        resolve_run_window(
            enrollment_start_us=_us(datetime(2024, 8, 4, 12, tzinfo=UTC)),
            manifest=manifest,
        )


def test_fresh_process_identity_is_stable() -> None:
    window = _resolved_fixture()
    code = (
        "from pathlib import Path; "
        "from jev_trading_lab.core.manifest import load_manifest; "
        "from jev_trading_lab.core.run_window import resolve_run_window; "
        f"m=load_manifest(Path({str(EXAMPLE)!r})); "
        "w=resolve_run_window("
        f"enrollment_start_us={window.enrollment_start_us},manifest=m); "
        "print(w.canonical_bytes.decode()); print(w.run_window_id)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "TZ": "Pacific/Auckland"},
    )

    assert result.stdout.splitlines() == [
        window.canonical_bytes.decode(),
        window.run_window_id,
    ]


def test_pre_epoch_domain_values_work_and_unavailable_calendar_range_fails() -> None:
    pre_epoch = RunWindow(**_window_fields())  # type: ignore[arg-type]
    assert pre_epoch.enrollment_start_us < 0
    assert pre_epoch.entries_allowed_at(pre_epoch.enrollment_start_us)

    with pytest.raises(ValueError, match="calendar sessions unavailable"):
        resolve_run_window(
            enrollment_start_us=_us(datetime(1, 1, 1, tzinfo=UTC)),
            manifest=load_manifest(EXAMPLE),
        )
    with pytest.raises(ValueError, match="supported datetime range"):
        RunWindow(**_window_fields(enrollment_start_us=-(2**63)))  # type: ignore[arg-type]


def test_calendar_missing_session_and_malformed_schedule_fail_closed() -> None:
    calendar = XnysCalendar()
    with pytest.raises(ValueError, match="not an XNYS session"):
        calendar.official_close_us(date(2024, 1, 6))

    import pandas as pd

    missing_close = pd.DataFrame(
        {"open": [pd.Timestamp("2024-01-02T14:30:00Z")]},
        index=pd.DatetimeIndex(["2024-01-02"]),
    )
    with pytest.raises(ValueError, match="malformed XNYS schedule"):
        _schedule_close_us(missing_close, date(2024, 1, 2))

    naive_close = pd.DataFrame(
        {"close": [pd.Timestamp("2024-01-02T21:00:00")]},
        index=pd.DatetimeIndex(["2024-01-02"]),
    )
    with pytest.raises(ValueError, match="malformed XNYS schedule"):
        _schedule_close_us(naive_close, date(2024, 1, 2))

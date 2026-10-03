from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, date, datetime
from importlib.metadata import version
from pathlib import Path

import pytest

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.core.manifest import load_manifest, parse_manifest_bytes
from jev_trading_lab.core.run_window import (
    RunWindow,
    resolve_run_window,
    revalidate_run_window,
)
from jev_trading_lab.core.time import datetime_to_utc_us
from jev_trading_lab.stocks import calendar as calendar_module
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
    verify_runtime_calendar_against_artifact,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config/phase1.example.json"
US_PER_DAY = 86_400_000_000


def _us(value: datetime) -> int:
    return datetime_to_utc_us(value)


def _start() -> int:
    return _us(datetime(2024, 8, 4, 12, tzinfo=UTC))


def _resolved_fixture() -> RunWindow:
    return resolve_run_window(
        enrollment_start_us=_start(), manifest=load_manifest(EXAMPLE)
    )


def test_run_window_is_resolver_issued_and_has_no_public_constructor() -> None:
    with pytest.raises(TypeError, match="only be created by the run-window resolver"):
        RunWindow()  # type: ignore[call-arg]
    assert not hasattr(_resolved_fixture(), "__dict__")


def test_tm_is_tenth_nyse_session_strictly_after_e_plus_30() -> None:
    window = _resolved_fixture()
    calendar = XnysCalendar()
    threshold = date(2024, 11, 26)
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
    assert window.maturation_cutoff_us == _us(datetime(2024, 12, 11, 21, tzinfo=UTC))


def test_dst_holiday_and_early_close_are_frozen_in_artifact() -> None:
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
    assert calendar.official_close_us(date(2024, 11, 29)) == _us(
        datetime(2024, 11, 29, 18, tzinfo=UTC)
    )


def test_run_window_revalidation_recomputes_and_rejects_forgery() -> None:
    manifest = load_manifest(EXAMPLE)
    window = _resolved_fixture()
    assert revalidate_run_window(window, manifest) == window
    assert revalidate_run_window(window, manifest) is not window

    forged = object.__new__(RunWindow)
    with pytest.raises(ValueError, match="wrapper state is invalid"):
        revalidate_run_window(forged, manifest)

    for field, value in (
        ("_canonical_bytes", b"{}"),
        ("_run_window_id", "run-window:" + "0" * 64),
        (
            "_data",
            replace(
                object.__getattribute__(window, "_data"),
                maturation_cutoff_us=window.maturation_cutoff_us + 1,
            ),
        ),
    ):
        tampered = _resolved_fixture()
        object.__setattr__(tampered, field, value)
        with pytest.raises(ValueError, match="revalidation failed"):
            revalidate_run_window(tampered, manifest)


@pytest.mark.parametrize("delta", (-US_PER_DAY, -1, 1, US_PER_DAY))
def test_arbitrary_maturation_cutoff_cannot_retain_identity(delta: int) -> None:
    window = _resolved_fixture()
    data = replace(
        object.__getattribute__(window, "_data"),
        maturation_cutoff_us=window.maturation_cutoff_us + delta,
    )
    object.__setattr__(window, "_data", data)
    with pytest.raises(ValueError, match="revalidation failed"):
        revalidate_run_window(window, load_manifest(EXAMPLE))


def test_same_tm_for_both_streams_and_enrollment_boundary() -> None:
    window = _resolved_fixture()
    assert not hasattr(window, "stream")
    assert canonical_json_bytes(window.to_dict()) == window.canonical_bytes
    assert window.run_window_id == _resolved_fixture().run_window_id
    assert window.enrollment_end_us - window.enrollment_start_us == 84 * US_PER_DAY
    assert not window.entries_allowed_at(window.enrollment_start_us - 1)
    assert window.entries_allowed_at(window.enrollment_start_us)
    assert window.entries_allowed_at(window.enrollment_end_us - 1)
    assert not window.entries_allowed_at(window.enrollment_end_us)


def test_manifest_calendar_and_provider_identities_are_exact() -> None:
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    expected = data["calendar"]
    assert expected["exchange_calendars_version"] == CALENDAR_PACKAGE_VERSION
    assert expected["lock_sha256"] == CALENDAR_LOCK_SHA256
    assert expected["artifact_sha256"] == CALENDAR_ARTIFACT_SHA256
    assert expected["tm_rule_version"] == TM_RULE_VERSION
    assert expected["tm_rule_sha256"] == TM_RULE_SHA256
    provider = data["providers"]["nyse_calendar"]
    assert provider["contract_version"] == PROVIDER_CONTRACT_VERSION
    assert provider["contract_sha256"] == PROVIDER_CONTRACT_SHA256
    assert version("exchange-calendars") == CALENDAR_PACKAGE_VERSION

    mismatches = (
        ("calendar", "exchange_calendars_version", "0.0.0"),
        ("calendar", "lock_sha256", "0" * 64),
        ("calendar", "artifact_sha256", "0" * 64),
        ("calendar", "tm_rule_version", "phase1-mismatch-v1"),
        ("calendar", "tm_rule_sha256", "0" * 64),
        ("providers.nyse_calendar", "contract_version", "phase1-provider-contract-v2"),
        ("providers.nyse_calendar", "contract_sha256", "0" * 64),
    )
    for section, field, replacement in mismatches:
        changed = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        target = changed
        for part in section.split("."):
            target = target[part]
        target[field] = replacement
        if field.startswith("contract_"):
            message = (
                "provider contract version"
                if field.endswith("version")
                else "provider contract hash"
            )
            with pytest.raises(ValueError, match=message):
                parse_manifest_bytes(json.dumps(changed).encode())
            continue
        manifest = parse_manifest_bytes(json.dumps(changed).encode())
        with pytest.raises(ValueError, match="calendar manifest identity mismatch"):
            resolve_run_window(enrollment_start_us=_start(), manifest=manifest)


def test_calendar_package_version_mismatch_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(calendar_module, "version", lambda _: "4.11.1")
    with pytest.raises(RuntimeError, match="package version mismatch"):
        XnysCalendar()
    with pytest.raises(RuntimeError, match="package version mismatch"):
        verify_runtime_calendar_against_artifact()


def test_resolver_uses_artifact_when_runtime_provider_is_monkeypatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _resolved_fixture()

    class BrokenCalendar:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("resolver consulted mutable runtime provider")

    monkeypatch.setattr(calendar_module, "XNYSExchangeCalendar", BrokenCalendar)
    assert _resolved_fixture() == expected
    with pytest.raises(RuntimeError, match="runtime XNYS schedule mismatch"):
        verify_runtime_calendar_against_artifact()


def test_tampered_artifact_and_hash_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = calendar_module._read_artifact_bytes()
    tampered = original.replace(b"2024-12-11", b"2024-12-12", 1)
    monkeypatch.setattr(calendar_module, "_read_artifact_bytes", lambda: tampered)
    with pytest.raises(RuntimeError, match="artifact SHA-256 mismatch"):
        XnysCalendar()


def test_schedule_outside_artifact_range_fails_closed() -> None:
    with pytest.raises(ValueError, match="calendar sessions unavailable"):
        resolve_run_window(
            enrollment_start_us=_us(datetime(2019, 1, 1, tzinfo=UTC)),
            manifest=load_manifest(EXAMPLE),
        )
    with pytest.raises(ValueError, match="calendar sessions unavailable"):
        resolve_run_window(
            enrollment_start_us=_us(datetime(2036, 1, 1, tzinfo=UTC)),
            manifest=load_manifest(EXAMPLE),
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


def test_window_identity_fields_are_read_only() -> None:
    window = _resolved_fixture()
    assert window.calendar_name == CALENDAR_NAME
    assert window.timezone == CALENDAR_TIMEZONE
    assert window.package_version == CALENDAR_PACKAGE_VERSION
    assert window.lock_sha256 == CALENDAR_LOCK_SHA256
    assert window.artifact_sha256 == CALENDAR_ARTIFACT_SHA256
    with pytest.raises(AttributeError):
        window.maturation_cutoff_us = 0  # type: ignore[misc]

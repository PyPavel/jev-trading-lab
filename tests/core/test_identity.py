from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _module(name: str) -> ModuleType:
    assert importlib.util.find_spec(name) is not None, f"{name} module must exist"
    return __import__(name, fromlist=["*"])


def test_canonical_hash_ignores_mapping_order() -> None:
    ids = _module("jev_trading_lab.core.ids")

    left = {"message": "café", "nested": {"b": 2, "a": 1}}
    right = {"nested": {"a": 1, "b": 2}, "message": "café"}

    assert ids.canonical_json_bytes(left) == ids.canonical_json_bytes(right)
    assert ids.canonical_json_bytes(left) == (
        '{"message":"café","nested":{"a":1,"b":2}}'.encode()
    )
    assert ids.canonical_hash("observation", left) == ids.canonical_hash(
        "observation", right
    )


def test_canonical_hash_preserves_list_order() -> None:
    ids = _module("jev_trading_lab.core.ids")

    assert ids.canonical_hash("ordered", [1, 2]) != ids.canonical_hash(
        "ordered", [2, 1]
    )


@pytest.mark.parametrize(
    "unsupported",
    (
        1.0,
        Decimal("1"),
        b"bytes",
        datetime(2026, 1, 1, tzinfo=UTC),
        {"set"},
        {1: "non-string key"},
    ),
)
def test_canonical_hash_rejects_unsupported_values(unsupported: object) -> None:
    ids = _module("jev_trading_lab.core.ids")

    with pytest.raises(TypeError, match="canonical JSON"):
        ids.canonical_hash("test", unsupported)


def test_canonical_hash_is_stable_across_fresh_processes() -> None:
    ids = _module("jev_trading_lab.core.ids")
    payload = {"text": "λ", "items": [None, True, -7]}
    expected = ids.canonical_hash("cross-process", payload)
    code = (
        "from jev_trading_lab.core.ids import canonical_hash; "
        f"print(canonical_hash('cross-process', {payload!r}))"
    )

    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={"PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert result.stdout.strip() == expected


def test_canonical_hash_separates_namespaces_without_delimiter_collisions() -> None:
    ids = _module("jev_trading_lab.core.ids")
    payload = {"value": "b:c"}

    assert ids.canonical_hash("a", payload) != ids.canonical_hash("a:b", payload)
    assert ids.canonical_hash("a", "b:c") != ids.canonical_hash("a:b", "c")


class IntSubclass(int):
    pass


def test_naive_datetime_rejected() -> None:
    time = _module("jev_trading_lab.core.time")

    with pytest.raises(ValueError, match="UTC-aware"):
        time.datetime_to_utc_us(datetime(2026, 1, 1))


def test_non_utc_datetime_rejected() -> None:
    time = _module("jev_trading_lab.core.time")

    with pytest.raises(ValueError, match="UTC offset"):
        time.datetime_to_utc_us(
            datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1)))
        )


def test_utc_datetime_roundtrips_exactly_including_pre_epoch() -> None:
    time = _module("jev_trading_lab.core.time")

    for value in (
        datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        datetime(1970, 1, 1, tzinfo=UTC),
        datetime(2026, 10, 3, 12, 34, 56, 789012, tzinfo=UTC),
    ):
        encoded = time.datetime_to_utc_us(value)
        assert type(encoded) is int
        assert time.utc_us_to_datetime(encoded) == value


def test_integer_timestamp_rejects_bool_and_int_subclasses() -> None:
    time = _module("jev_trading_lab.core.time")

    for value in (True, IntSubclass(0)):
        with pytest.raises(TypeError, match="built-in integer"):
            time.utc_us_to_datetime(value)


def test_datetime_supported_range_is_explicit_and_enforced() -> None:
    time = _module("jev_trading_lab.core.time")

    assert time.utc_us_to_datetime(time.MIN_DATETIME_US) == datetime.min.replace(
        tzinfo=UTC
    )
    assert time.utc_us_to_datetime(time.MAX_DATETIME_US) == datetime.max.replace(
        tzinfo=UTC
    )
    for value in (
        -(2**63),
        2**63 - 1,
        time.MIN_DATETIME_US - 1,
        time.MAX_DATETIME_US + 1,
    ):
        with pytest.raises(ValueError, match="supported datetime range"):
            time.utc_us_to_datetime(value)


def test_canonical_json_bytes_are_valid_utf8_json() -> None:
    ids = _module("jev_trading_lab.core.ids")

    encoded = ids.canonical_json_bytes({"emoji": "☃"})

    assert json.loads(encoded.decode("utf-8")) == {"emoji": "☃"}

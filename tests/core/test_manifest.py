from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from enum import IntEnum
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config/phase1.example.json"


def _manifest_module() -> Any:
    assert importlib.util.find_spec("jev_trading_lab.core.manifest") is not None
    import jev_trading_lab.core.manifest as manifest

    return manifest


def _example() -> dict[str, Any]:
    value = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _set_path(value: dict[str, Any], path: str, replacement: Any) -> None:
    parts = path.split(".")
    target: dict[str, Any] = value
    for part in parts[:-1]:
        child = target[part]
        assert isinstance(child, dict)
        target = child
    target[parts[-1]] = replacement


def _delete_path(value: dict[str, Any], path: str) -> None:
    parts = path.split(".")
    target: dict[str, Any] = value
    for part in parts[:-1]:
        child = target[part]
        assert isinstance(child, dict)
        target = child
    del target[parts[-1]]


def test_manifest_hash_stable() -> None:
    module = _manifest_module()
    original = EXAMPLE.read_bytes()
    parsed = module.parse_manifest_bytes(original)
    reordered = json.dumps(
        _example(), ensure_ascii=False, indent=7, sort_keys=True
    ).encode()
    equivalent = module.parse_manifest_bytes(reordered)

    assert parsed == equivalent
    assert parsed.manifest_hash == equivalent.manifest_hash
    assert parsed.manifest_id == f"manifest:{parsed.manifest_hash}"

    changed_data = _example()
    changed_data["quotas"]["daily_input_tokens"] += 1
    changed = module.parse_manifest_bytes(json.dumps(changed_data).encode())
    assert changed.manifest_hash != parsed.manifest_hash

    code = (
        "from pathlib import Path; "
        "from jev_trading_lab.core.manifest import load_manifest; "
        f"print(load_manifest(Path({str(EXAMPLE)!r})).manifest_hash)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.stdout.strip() == parsed.manifest_hash

    def assert_canonical(value: object) -> None:
        assert value is None or type(value) in {bool, int, str, list, dict}
        if type(value) is int:
            assert -(2**63) <= value <= 2**63 - 1
        elif type(value) is list:
            for item in value:
                assert_canonical(item)
        elif type(value) is dict:
            assert all(type(key) is str for key in value)
            for item in value.values():
                assert_canonical(item)

    assert_canonical(parsed.model_dump(mode="json"))


def test_alias_and_unknown_key_rejected() -> None:
    module = _manifest_module()

    invalid_cases: list[tuple[str, dict[str, Any]]] = []
    for label, path, replacement in (
        ("live mode", "run.mode", "live"),
        ("non-paper mode", "run.mode", "simulation"),
        ("model alias", "models.openai.request_model_snapshot", "gpt-5-latest"),
        ("tools", "models.jev.tools_enabled", True),
        ("model fallback", "models.openai.fallback_enabled", True),
        ("wrong integer type", "quotas.max_attempts_per_stage", "3"),
        ("wrong boolean type", "stocks.shorts_enabled", 0),
        ("bool as integer", "quotas.max_concurrent_requests", True),
        ("required null", "calendar.exchange", None),
    ):
        value = _example()
        _set_path(value, path, replacement)
        invalid_cases.append((label, value))

    alias = _example()
    alias["schemaVersion"] = alias.pop("schema_version")
    invalid_cases.append(("field alias", alias))

    for label, path in (
        ("root unknown", "unknown"),
        ("nested unknown", "run.unknown"),
        ("deep unknown", "models.openai.unknown"),
        ("fallback unknown", "providers.data_fallbacks.unregistered"),
    ):
        value = _example()
        _set_path(value, path, {})
        invalid_cases.append((label, value))

    for _label, value in invalid_cases:
        with pytest.raises(ValueError):
            module.Manifest.model_validate(value)

    duplicate = b'{"schema_version":1,"schema_version":1}'
    with pytest.raises(ValueError, match="duplicate JSON key.*schema_version"):
        module.parse_manifest_bytes(duplicate)

    for token in (b"NaN", b"Infinity", b"-Infinity"):
        payload = b'{"schema_version":1,"bad":' + token + b"}"
        with pytest.raises(ValueError, match="finite JSON numbers"):
            module.parse_manifest_bytes(payload)


def test_validation_policy_cannot_be_weakened_by_caller() -> None:
    module = _manifest_module()
    unknown = _example()
    unknown["unreviewed"] = True
    wrong_type = _example()
    wrong_type["quotas"]["max_attempts_per_stage"] = "3"

    with pytest.raises(ValueError):
        module.Manifest.model_validate(unknown, extra="allow")
    with pytest.raises(ValueError):
        module.Manifest.model_validate(wrong_type, strict=False)


@pytest.mark.parametrize(
    ("path", "expected"),
    (
        ("quotas.max_new_opportunities_per_cycle", 3),
        ("quotas.max_new_opportunities_per_utc_day", 20),
        ("quotas.max_concurrent_requests", 3),
        ("quotas.max_attempts_per_stage", 3),
        ("quotas.cycle_deadline_seconds", 600),
        ("quotas.daily_ai_budget_microdollars", 3_000_000),
        ("calendar.enrollment_weeks", 12),
        ("calendar.maturation_calendar_days", 30),
        ("calendar.maturation_trading_days", 10),
        ("accounts.initial_capital_microdollars", 10_000_000_000),
        ("accounts.risk_unit_microdollars", 50_000_000),
        ("ai_thresholds.sufficiency_probability_micros", 700_000),
        ("ai_thresholds.ambiguity_probability_micros", 300_000),
        ("ai_thresholds.action_probability_micros", 550_000),
        ("ai_thresholds.runner_up_margin_probability_micros", 150_000),
    ),
)
def test_required_limits_present(path: str, expected: int) -> None:
    module = _manifest_module()
    value = _example()
    target: Any = value
    for part in path.split("."):
        target = target[part]
    assert target == expected
    _delete_path(value, path)

    with pytest.raises(ValueError) as caught:
        module.Manifest.model_validate(value)

    assert path.split(".")[-1] in str(caught.value)


@pytest.mark.parametrize(
    "path",
    (
        "schema_version",
        "run.phase",
        "run.mode",
        "calendar.exchange",
        "calendar.exchange_calendars_version",
        "calendar.timezone",
        "calendar.tm_rule",
        "calendar.tm_rule_version",
        "accounts.currency",
        "accounts.polymarket_arms",
        "accounts.stock_arms",
        "accounts.cash_yield",
        "models.openai.provider",
        "models.openai.endpoint",
        "models.openai.api_version",
        "models.openai.request_model_snapshot",
        "models.openai.expected_returned_model_id",
        "models.openai.expected_revision",
        "models.openai.expected_fingerprint",
        "models.openai.schema_sha256",
        "models.openai.temperature_micros",
        "models.openai.top_p_micros",
        "models.openai.seed",
        "models.openai.tools_enabled",
        "models.jev.provider",
        "models.jev.endpoint",
        "providers.polymarket_catalog",
        "providers.stock_ohlcv_actions",
        "providers.data_fallbacks",
        "quotas.daily_provider_calls",
        "quotas.daily_input_tokens",
        "quotas.daily_output_tokens",
        "quotas.daily_evidence_bytes",
        "quotas.max_evidence_bytes_per_opportunity",
        "quotas.pool_allocation",
        "polymarket.cluster_algorithm",
        "polymarket.opportunity_rule",
        "polymarket.max_open_clusters",
        "polymarket.review_interval_minutes",
        "stocks.candidate_count",
        "stocks.shorts_enabled",
        "stocks.max_open_positions",
        "stocks.review_cadence",
        "costs.shared_ai_deterministic_split_micros",
        "costs.uncertain_cost_stress_multipliers_micros",
        "costs.zero_bid_valuation_rule",
        "baselines.cash_series",
        "baselines.polymarket_control_artifact_sha256",
        "baselines.stock_control_artifact_sha256",
        "baselines.stock_prevalence_artifact_sha256",
        "endpoint.tm_rule",
        "endpoint.daily_valuation_time_utc",
        "endpoint.no_entry_after_enrollment_end",
        "statistics.bootstrap_replicates",
        "statistics.expected_block_length_artifact_sha256",
        "statistics.confirmatory_min_weeks",
        "statistics.confirmatory_min_resolved_polymarket_clusters",
        "statistics.confirmatory_power_probability_micros",
        "statistics.terminal_slot_threshold_probability_micros",
        "statistics.weekly_loss_floor_probability_micros",
        "statistics.count_retention_probability_micros",
        "runtime.backup_rpo_hours",
        "runtime.restore_rto_hours",
        "runtime.restore_drill_cadence",
    ),
)
def test_complete_nested_required_sections(path: str) -> None:
    module = _manifest_module()
    value = _example()
    _delete_path(value, path)

    with pytest.raises(ValueError) as caught:
        module.Manifest.model_validate(value)

    assert path.split(".")[-1] in str(caught.value)


def test_manifest_and_all_nested_values_are_immutable() -> None:
    module = _manifest_module()
    manifest = module.load_manifest(EXAMPLE)

    with pytest.raises(Exception):
        manifest.schema_version = 2
    with pytest.raises(Exception):
        manifest.quotas.max_attempts_per_stage = 4
    with pytest.raises(TypeError):
        manifest.polymarket.accepted_categories[0] = "sports"
    with pytest.raises(TypeError):
        manifest.providers.data_fallbacks["new"] = manifest.providers.data_fallbacks[
            "treasury_last_published_prior"
        ]


class _IntSubclass(int):
    pass


class _IntEnum(IntEnum):
    VALUE = 3


@pytest.mark.parametrize("bad", (3.0, "3", True, _IntSubclass(3), _IntEnum.VALUE))
def test_strict_exact_builtin_integer_acceptance(bad: object) -> None:
    module = _manifest_module()
    value = _example()
    value["quotas"]["max_attempts_per_stage"] = bad

    with pytest.raises(ValueError):
        module.Manifest.model_validate(value)


def test_strict_builtin_primitives_accept_valid_json_values() -> None:
    module = _manifest_module()
    manifest = module.load_manifest(EXAMPLE)

    assert type(manifest.schema_version) is int
    assert type(manifest.run.mode) is str
    assert type(manifest.run.example_only) is bool
    assert type(manifest.polymarket.accepted_categories) is tuple
    assert type(manifest.providers.data_fallbacks) is module.DataFallbackContracts


def test_path_and_file_loader_failures(tmp_path: Path) -> None:
    module = _manifest_module()

    with pytest.raises(ValueError, match="manifest path must be a regular file"):
        module.load_manifest(tmp_path / "missing.json")
    with pytest.raises(ValueError, match="manifest path must be a regular file"):
        module.load_manifest(tmp_path)

    unreadable = tmp_path / "unreadable.json"
    unreadable.write_bytes(EXAMPLE.read_bytes())
    unreadable.chmod(0)
    with pytest.raises(ValueError, match="manifest file is not readable"):
        module.load_manifest(unreadable)


def test_loader_requires_strict_utf8_root_object_and_size() -> None:
    module = _manifest_module()

    with pytest.raises(ValueError, match="valid UTF-8"):
        module.parse_manifest_bytes(b"\xff\xfe")
    with pytest.raises(ValueError, match="JSON root must be an object"):
        module.parse_manifest_bytes(b"[]")
    with pytest.raises(ValueError, match="manifest exceeds 262144 bytes"):
        module.parse_manifest_bytes(b" " * (module.MAX_MANIFEST_BYTES + 1))
    with pytest.raises(TypeError, match="exact built-in bytes"):
        module.parse_manifest_bytes(bytearray(EXAMPLE.read_bytes()))


def test_validation_errors_are_deterministic_and_actionable() -> None:
    module = _manifest_module()
    value = _example()
    del value["stocks"]["candidate_count"]
    payload = json.dumps(value, sort_keys=True).encode()

    messages = []
    for _ in range(2):
        with pytest.raises(ValueError) as caught:
            module.parse_manifest_bytes(payload)
        messages.append(str(caught.value))

    assert messages[0] == messages[1]
    assert "stocks.candidate_count" in messages[0]


@pytest.mark.parametrize(
    ("path", "replacement"),
    (
        ("schema_version", 2),
        ("run.phase", "phase2"),
        ("calendar.exchange", "NASDAQ"),
        ("calendar.timezone", "UTC"),
        ("accounts.currency", "EUR"),
        ("accounts.polymarket_arms", ["AI", "CASH"]),
        ("models.openai.provider", "Other"),
        ("models.openai.endpoint", "https://example.test/v1/responses"),
        ("models.openai.expected_returned_model_id", "different-model"),
        ("models.jev.expected_returned_model_id", "jev-latest"),
        ("providers.polymarket_catalog.endpoint", "https://example.test/markets"),
        ("providers.polymarket_books.source_name", "Other book provider"),
        ("providers.stock_ohlcv_actions.endpoint", "https://example.test/chart"),
        ("providers.treasury_cash_yield.fallback_enabled", False),
        ("providers.borrow_locate.source_name", "example locate provider"),
        ("quotas.max_attempts_per_stage", 4),
        ("quotas.retry_jitter_micros", 1_000_001),
        ("quotas.pool_allocation.stock_review_micros", 149_999),
        ("polymarket.scan_interval_minutes", 5),
        ("polymarket.depth_haircut_micros", 400_000),
        ("stocks.shorts_enabled", True),
        ("stocks.half_spread_micros", 400),
        ("costs.shared_ai_deterministic_split_micros", 600_000),
        ("costs.joint_adverse_cost_multiplier_micros", 1_000_000),
        ("baselines.stock_passive_oef_weight_micros", 600_000),
        ("endpoint.no_entry_after_enrollment_end", False),
        ("statistics.risk_unit_microdollars", 40_000_000),
        ("statistics.theta_null_probability_micros", 40_000),
        ("statistics.theta_alt_probability_micros", 50_000),
        ("statistics.bootstrap_replicates", 100_000),
        ("statistics.confirmatory_min_weeks", 25),
        ("statistics.confirmatory_min_resolved_polymarket_clusters", 99),
        ("statistics.confirmatory_power_probability_micros", 799_999),
        ("statistics.terminal_slot_threshold_probability_micros", 989_999),
        ("statistics.max_drawdown_relative_tolerance_probability_micros", 30_000),
        ("statistics.max_drawdown_absolute_probability_micros", 110_000),
        ("statistics.weekly_loss_floor_probability_micros", -20_000),
        ("statistics.count_retention_probability_micros", 500_000),
        ("runtime.backup_rpo_hours", 48),
        ("runtime.restore_rto_hours", 4),
    ),
)
def test_validator_matrix_rejects_non_phase1_contract(
    path: str, replacement: object
) -> None:
    module = _manifest_module()
    value = _example()
    _set_path(value, path, replacement)

    with pytest.raises(ValueError) as caught:
        module.Manifest.model_validate(value)

    message = str(caught.value)
    assert path.split(".")[0] in message


def test_no_generic_dict_escape_hatches() -> None:
    module = _manifest_module()
    models = [
        module.Manifest,
        module.RunContract,
        module.CalendarContract,
        module.AccountsContract,
        module.ModelContracts,
        module.ModelContract,
        module.ProviderContracts,
        module.Quotas,
        module.PolymarketContract,
        module.StockContract,
        module.CostContract,
        module.BaselineContract,
        module.EndpointContract,
        module.StatisticsContract,
        module.RuntimeContract,
    ]
    for model in models:
        for field in model.model_fields.values():
            assert field.annotation is not dict

from __future__ import annotations

import hashlib
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


def _activation_value() -> dict[str, Any]:
    value = _example()
    value["run"]["example_only"] = False
    value["models"]["openai"].update(
        sdk_version="1.93.0",
        expected_revision="rev-20261002-openai-a1",
        expected_fingerprint="fp_7f4d9a2c18b6e305",
    )
    value["models"]["jev"].update(
        sdk_version="3.12.0",
        request_model_snapshot="jev-systemone-phase1-2026-10-02",
        expected_returned_model_id="jev-systemone-phase1-2026-10-02",
        expected_revision="rev-20261002-jev-b2",
        expected_fingerprint="fp_5a8e1d7c92f403b6",
    )
    value["baselines"]["polymarket_control_artifact_id"] = "poly-control-20261002-a1-v1"
    value["baselines"]["stock_control_artifact_id"] = "stock-control-20261002-b2-v1"

    def replace_hashes(item: object, path: str = "") -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                child_path = f"{path}.{key}" if path else key
                if key.endswith("sha256") and child_path not in {
                    "calendar.lock_sha256",
                    "calendar.artifact_sha256",
                    "calendar.tm_rule_sha256",
                    "providers.nyse_calendar.contract_sha256",
                }:
                    item[key] = hashlib.sha256(child_path.encode()).hexdigest()
                else:
                    replace_hashes(child, child_path)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                replace_hashes(child, f"{path}.{index}")

    replace_hashes(value)
    return value


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

    assert_canonical(parsed.to_dict())


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
            _parse_value(value)

    duplicate = b'{"schema_version":1,"schema_version":1}'
    with pytest.raises(ValueError, match="duplicate JSON key.*schema_version"):
        module.parse_manifest_bytes(duplicate)

    for token in (b"NaN", b"Infinity", b"-Infinity"):
        payload = b'{"schema_version":1,"bad":' + token + b"}"
        with pytest.raises(ValueError, match="finite JSON numbers"):
            module.parse_manifest_bytes(payload)


def test_validation_policy_cannot_be_weakened_by_caller() -> None:
    module = _manifest_module()
    assert not hasattr(module.Manifest, "model_validate")

    unknown = _example()
    unknown["unreviewed"] = True
    wrong_type = _example()
    wrong_type["quotas"]["max_attempts_per_stage"] = "3"
    with pytest.raises(ValueError):
        _parse_value(unknown)
    with pytest.raises(ValueError):
        _parse_value(wrong_type)


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
    value = _example()
    target: Any = value
    for part in path.split("."):
        target = target[part]
    assert target == expected
    _delete_path(value, path)

    with pytest.raises(ValueError) as caught:
        _parse_value(value)

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
        "model_input_policy",
        "model_input_policy.openai_prompt_template_version",
        "model_input_policy.openai_prompt_template_sha256",
        "model_input_policy.evidence_delimiter_version",
        "model_input_policy.evidence_delimiter_sha256",
        "model_input_policy.jev_state_template_version",
        "model_input_policy.jev_state_template_sha256",
        "model_input_policy.jev_question_set_version",
        "model_input_policy.jev_question_set_sha256",
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
    value = _example()
    _delete_path(value, path)

    with pytest.raises(ValueError) as caught:
        _parse_value(value)

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
        if type(bad) in {int, bool, str, float}:
            _parse_value(value)
        else:
            module._ManifestModel.model_validate(value)


def test_strict_builtin_primitives_accept_valid_json_values() -> None:
    module = _manifest_module()
    manifest = module.load_manifest(EXAMPLE)

    assert type(manifest.schema_version) is int
    assert type(manifest.run.mode) is str
    assert type(manifest.run.example_only) is bool
    assert type(manifest.polymarket.accepted_categories) is tuple
    assert type(manifest.providers.data_fallbacks) is module._DataFallbackContracts


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
    value = _example()
    _set_path(value, path, replacement)

    with pytest.raises(ValueError) as caught:
        _parse_value(value)

    message = str(caught.value)
    assert path.split(".")[0] in message


def test_no_generic_dict_escape_hatches() -> None:
    module = _manifest_module()
    models = [
        module._ManifestModel,
        module._RunContract,
        module._CalendarContract,
        module._AccountsContract,
        module._ModelContracts,
        module._ModelContract,
        module._ProviderContracts,
        module._Quotas,
        module._PolymarketContract,
        module._StockContract,
        module._CostContract,
        module._BaselineContract,
        module._EndpointContract,
        module._StatisticsContract,
        module._RuntimeContract,
    ]
    for model in models:
        for field in model.model_fields.values():
            assert field.annotation is not dict


def _parse_value(value: dict[str, Any]) -> Any:
    return _manifest_module().parse_manifest_bytes(
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    )


def test_public_manifest_has_parser_only_trust_boundary() -> None:
    import copy

    from pydantic import BaseModel, TypeAdapter

    module = _manifest_module()
    manifest = module.load_manifest(EXAMPLE)

    assert not issubclass(module.Manifest, BaseModel)
    assert not hasattr(manifest, "__dict__")
    for bypass in (
        "model_validate",
        "model_validate_json",
        "model_construct",
        "model_copy",
        "__pydantic_validator__",
    ):
        assert not hasattr(module.Manifest, bypass)
        assert not hasattr(manifest, bypass)
    with pytest.raises(TypeError):
        module.Manifest()
    with pytest.raises(Exception):
        TypeAdapter(module.Manifest)

    copied = copy.copy(manifest)
    assert copied.to_dict() == manifest.to_dict()
    assert module.revalidate_manifest(copied) == manifest


def test_canonical_bytes_round_trip_and_revalidation_is_authority() -> None:
    module = _manifest_module()
    manifest = module.load_manifest(EXAMPLE)

    assert module.parse_manifest_bytes(manifest.canonical_bytes) == manifest
    assert manifest.canonical_bytes == json.dumps(
        manifest.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert module.revalidate_manifest(manifest) == manifest

    object.__setattr__(manifest, "_manifest_id", "manifest:" + "0" * 64)
    with pytest.raises(ValueError, match="revalidation"):
        module.revalidate_manifest(manifest)

    manifest = module.load_manifest(EXAMPLE)
    object.__setattr__(manifest, "_canonical_bytes", b"{}")
    with pytest.raises(ValueError, match="revalidation"):
        module.revalidate_manifest(manifest)

    manifest = module.load_manifest(EXAMPLE)
    object.__setattr__(manifest, "_model", object())
    with pytest.raises(ValueError, match="revalidation"):
        module.revalidate_manifest(manifest)


def test_activation_candidate_rejects_examples_and_placeholders() -> None:
    module = _manifest_module()
    manifest = module.load_manifest(EXAMPLE)
    with pytest.raises(ValueError, match="example_only"):
        manifest.assert_activation_candidate()

    value = _example()
    value["run"]["example_only"] = False
    with pytest.raises(ValueError, match="placeholder"):
        _parse_value(value)

    candidate = _parse_value(_activation_value())
    candidate.assert_activation_candidate()


_STRONG_MARKERS = (
    "example",
    "xxx",
    "placeholder",
    "todo",
    "tbd",
    "changeme",
    "unknown",
    "dummy",
    "fake",
    "notapplicable",
    "notset",
    "unset",
    "replaceme",
    "pending",
)


@pytest.mark.parametrize(
    "token",
    tuple(
        candidate
        for marker in _STRONG_MARKERS
        for candidate in (marker, f"{marker}2026")
    ),
)
def test_activation_rejects_exact_or_digit_suffixed_unresolved_tokens(
    token: str,
) -> None:
    value = _activation_value()
    value["models"]["openai"]["expected_revision"] = f"rev-20261002-{token}-a1"
    with pytest.raises(ValueError, match="expected_revision.*placeholder"):
        _parse_value(value)


@pytest.mark.parametrize(
    "phrase",
    (
        "change-me",
        "change-me2026",
        "replace-me",
        "replace-me2026",
        "not-applicable",
        "not-applicable2026",
        "not-set",
        "not-set2026",
        "test-only",
        "test-only2026",
    ),
)
def test_activation_rejects_unresolved_token_phrases(phrase: str) -> None:
    value = _activation_value()
    value["calendar"]["tm_rule_version"] = f"phase1-{phrase}-v1"
    with pytest.raises(ValueError, match="tm_rule_version.*placeholder"):
        _parse_value(value)


@pytest.mark.parametrize(
    ("path", "replacement"),
    (
        ("models.jev.request_model_snapshot", "jev-phase1-example-2026-10-02"),
        ("models.openai.expected_fingerprint", "fp-replaceme2026-a91b7c4d"),
        ("models.openai.sdk_package", "todo2026-client"),
        ("baselines.polymarket_control_artifact_id", "control-unset2026-v1"),
        ("baselines.polymarket_control_artifact_version", "control-dummy2026-v1"),
        ("model_input_policy.openai_prompt_template_version", "prompt-fake2026-v1"),
        ("model_input_policy.evidence_delimiter_version", "delimiter-tbd2026-v1"),
        ("model_input_policy.jev_state_template_version", "state-unknown2026-v1"),
        ("model_input_policy.jev_question_set_version", "questions-pending2026-v1"),
        ("providers.polymarket_catalog.contract_version", "contract-notset2026-v1"),
        (
            "providers.data_fallbacks.treasury_last_published_prior.rule_version",
            "treasury-placeholder2026-v1",
        ),
        ("calendar.tm_rule_version", "phase1-changeme2026-v1"),
    ),
)
def test_activation_rejects_unresolved_markers_in_every_identity_class(
    path: str, replacement: str
) -> None:
    value = _activation_value()
    _set_path(value, path, replacement)
    if path == "models.jev.request_model_snapshot":
        value["models"]["jev"]["expected_returned_model_id"] = replacement
    with pytest.raises(ValueError, match=f"{path}.*placeholder"):
        _parse_value(value)


@pytest.mark.parametrize(
    ("path", "replacement"),
    (
        ("models.openai.expected_revision", "latest"),
        ("models.openai.sdk_version", "current"),
        ("calendar.tm_rule_version", "default"),
        ("models.openai.expected_fingerprint", "none"),
        ("baselines.stock_control_artifact_version", "null"),
        ("baselines.stock_control_artifact_id", "na"),
        ("models.openai.expected_revision", "n-a"),
        ("models.openai.expected_revision", "n_a"),
        ("models.openai.expected_revision", "N/A"),
    ),
)
def test_activation_rejects_separator_insensitive_generic_identity_values(
    path: str, replacement: str
) -> None:
    value = _activation_value()
    _set_path(value, path, replacement)
    with pytest.raises(ValueError, match=f"{path}.*generic identity"):
        _parse_value(value)


@pytest.mark.parametrize(
    ("path", "replacement"),
    (
        ("calendar.tm_rule_version", "phase1-permutation-test-v1"),
        ("stocks.candidate_rule_version", "phase1-sample-calendar-v1"),
        ("statistics.power_simulation_version", "phase1-change-detection-v1"),
        ("statistics.power_simulation_version", "phase1-current-regime-v1"),
        ("calendar.tm_rule_version", "phase1-sunset-policy-v1"),
        ("calendar.tm_rule_version", "phase1-depending-policy-v1"),
        ("calendar.tm_rule_version", "phase1-contest-policy-v1"),
        ("calendar.tm_rule_version", "phase1-changelog-policy-v1"),
    ),
)
def test_activation_accepts_legitimate_identity_tokens(
    path: str, replacement: str
) -> None:
    value = _activation_value()
    _set_path(value, path, replacement)
    _parse_value(value).assert_activation_candidate()


def test_activation_accepts_harmless_marker_substrings() -> None:
    value = _activation_value()
    value["models"]["openai"]["expected_revision"] = "rev-notional-contest-20261002"
    value["models"]["openai"]["expected_fingerprint"] = "fp_changelog2026_a91b7c4d"
    value["models"]["openai"]["sdk_package"] = "contest-client"
    value["models"]["openai"]["sdk_version"] = "2.1.0"
    _parse_value(value).assert_activation_candidate()


@pytest.mark.parametrize(
    ("path", "replacement"),
    (
        ("models.openai.sdk_version", "sdk-v2.1"),
        ("models.openai.expected_revision", "rev1"),
        ("models.openai.expected_fingerprint", "fingerprint-review"),
        ("calendar.tm_rule_version", "phase1-calendar"),
        ("baselines.stock_control_artifact_id", "stock-control-20261002-b2"),
    ),
)
def test_activation_rejects_malformed_field_specific_identities(
    path: str, replacement: str
) -> None:
    value = _activation_value()
    _set_path(value, path, replacement)
    with pytest.raises(ValueError, match=f"{path}.*placeholder"):
        _parse_value(value)


@pytest.mark.parametrize(
    "sdk_version",
    ("01.2.3", "1.02.3", "1.2.03"),
)
def test_activation_rejects_noncanonical_semver(sdk_version: str) -> None:
    value = _activation_value()
    value["models"]["openai"]["sdk_version"] = sdk_version
    with pytest.raises(ValueError, match="sdk_version.*malformed"):
        _parse_value(value)


@pytest.mark.parametrize("sdk_version", ("0.0.0", "3.12.0", "10.20.30"))
def test_activation_accepts_canonical_semver(sdk_version: str) -> None:
    value = _activation_value()
    value["models"]["openai"]["sdk_version"] = sdk_version
    _parse_value(value).assert_activation_candidate()


@pytest.mark.parametrize(
    "repeated_hash",
    (
        "a" * 64,
        "ab" * 32,
        "abcd" * 16,
        "01234567" * 8,
        "0123456789abcdef" * 4,
        "0123456789abcdef89abcdef01234567" * 2,
    ),
)
def test_activation_rejects_periodic_sha256_identities(repeated_hash: str) -> None:
    value = _activation_value()
    value["statistics"]["power_simulation_sha256"] = repeated_hash
    with pytest.raises(ValueError, match="power_simulation_sha256.*repeated hash"):
        _parse_value(value)


def test_activation_accepts_nonperiodic_sha256_identity() -> None:
    value = _activation_value()
    value["statistics"]["power_simulation_sha256"] = hashlib.sha256(
        b"reviewed-power-simulation"
    ).hexdigest()
    _parse_value(value).assert_activation_candidate()


@pytest.mark.parametrize(
    ("path", "replacement"),
    (
        ("models.openai.expected_fingerprint", "RePlAcE_Me"),
        ("baselines.polymarket_control_artifact_id", "sample"),
        ("baselines.stock_control_artifact_id", "bad artifact!"),
        ("baselines.stock_control_artifact_version", "test_only"),
        ("statistics.power_simulation_version", "bad version!"),
    ),
)
def test_activation_rejects_field_aware_identity_placeholders(
    path: str, replacement: str
) -> None:
    value = _activation_value()
    _set_path(value, path, replacement)
    with pytest.raises(ValueError, match=f"{path}.*placeholder"):
        _parse_value(value)


def test_provider_response_schema_identities_are_distinct() -> None:
    value = _activation_value()
    jev_questions = (
        value["model_input_policy"]["jev_question_set_version"],
        value["model_input_policy"]["jev_question_set_sha256"],
    )
    value["models"]["jev"]["schema_version"] = value["models"]["openai"][
        "schema_version"
    ]
    value["models"]["jev"]["schema_sha256"] = value["models"]["openai"]["schema_sha256"]

    with pytest.raises(ValueError, match="response schema.*distinct"):
        _parse_value(value)

    assert jev_questions == (
        value["model_input_policy"]["jev_question_set_version"],
        value["model_input_policy"]["jev_question_set_sha256"],
    )


def test_provider_response_schema_hashes_must_differ() -> None:
    value = _activation_value()
    value["models"]["jev"]["schema_sha256"] = value["models"]["openai"]["schema_sha256"]
    with pytest.raises(ValueError, match="response schema hashes must differ"):
        _parse_value(value)

    value = _activation_value()
    value["models"]["jev"]["schema_version"] = value["models"]["openai"][
        "schema_version"
    ]
    _parse_value(value).assert_activation_candidate()


def test_model_input_policy_is_required_content_addressed_and_identity_bound() -> None:
    policy = {
        "openai_prompt_template_version": "phase1-openai-prompt-v1",
        "openai_prompt_template_sha256": hashlib.sha256(
            b"openai-prompt-v1"
        ).hexdigest(),
        "evidence_delimiter_version": "phase1-evidence-delimiter-v1",
        "evidence_delimiter_sha256": hashlib.sha256(
            b"evidence-delimiter-v1"
        ).hexdigest(),
        "jev_state_template_version": "phase1-jev-state-v1",
        "jev_state_template_sha256": hashlib.sha256(b"jev-state-v1").hexdigest(),
        "jev_question_set_version": "phase1-jev-questions-v1",
        "jev_question_set_sha256": hashlib.sha256(b"jev-questions-v1").hexdigest(),
    }
    value = _example()
    assert value["model_input_policy"] == policy
    assert (
        value["models"]["openai"]["schema_version"]
        == "phase1-openai-response-schema-v1"
    )
    assert value["models"]["jev"]["schema_version"] == "phase1-jev-response-schema-v1"
    assert value["model_input_policy"]["jev_question_set_version"] == (
        "phase1-jev-questions-v1"
    )
    assert "openai_schema_version" not in value["model_input_policy"]
    assert "openai_schema_sha256" not in value["model_input_policy"]
    original = _parse_value(value)

    divergent = _example()
    divergent["model_input_policy"]["openai_schema_version"] = "other-schema-v2"
    divergent["model_input_policy"]["openai_schema_sha256"] = hashlib.sha256(
        b"other-schema-v2"
    ).hexdigest()
    with pytest.raises(ValueError, match="openai_schema"):
        _parse_value(divergent)

    for field in policy:
        missing = _example()
        del missing["model_input_policy"][field]
        with pytest.raises(ValueError, match=field):
            _parse_value(missing)

    changed = _example()
    changed["model_input_policy"]["jev_question_set_sha256"] = hashlib.sha256(
        b"changed"
    ).hexdigest()
    assert _parse_value(changed).manifest_id != original.manifest_id

    active = _activation_value()
    _parse_value(active).assert_activation_candidate()
    active["model_input_policy"]["openai_prompt_template_version"] = "pending"
    with pytest.raises(ValueError, match="openai_prompt_template_version.*placeholder"):
        _parse_value(active)


def test_required_review_contract_fields_and_exact_design_literals() -> None:
    expected: dict[str, object] = {
        "calendar.lock_sha256": (
            "4535dbd779b3c903f57235bb5f3a5c5ce12e3bf13f1b8e01da0576f97df212ac"
        ),
        "calendar.artifact_sha256": (
            "ade5009ba938836a5d17e298b0e704fc3635b8b1eef329ba4b43cba5c40fc978"
        ),
        "calendar.tm_rule_sha256": (
            "8f827b89de3ed73872c9935f4c3efb619b9fb922e47cc1009ef0820ce7597186"
        ),
        "accounts.cash_yield.rule_version": "phase1-cash-yield-v1",
        "accounts.cash_yield.rule_sha256": "1" * 64,
        "ai_thresholds.rule_version": "phase1-ai-thresholds-v1",
        "ai_thresholds.rule_sha256": "2" * 64,
        "quotas.rule_version": "phase1-quota-policy-v1",
        "quotas.rule_sha256": "6" * 64,
        "polymarket.policy_version": "phase1-polymarket-policy-v1",
        "polymarket.policy_sha256": "7" * 64,
        "stocks.policy_version": "phase1-stock-policy-v1",
        "stocks.policy_sha256": "8" * 64,
        "providers.data_fallbacks.treasury_last_published_prior.mapping": (
            "observation_date_to_observation_date",
            "value_to_yield_percent",
            "first_retrieved_at_to_available_at",
        ),
        "polymarket.cluster_metadata_fields": (
            "condition_id",
            "market_id",
            "token_ids",
            "categories",
            "tags",
            "question",
            "rules",
            "resolution_source",
            "end_at",
            "active",
            "closed",
            "volume_24h_usd",
            "liquidity",
        ),
        "polymarket.cluster_normalization_version": "phase1-cluster-normalization-v1",
        "polymarket.cluster_rule_sha256": "a" * 64,
        "polymarket.sigma_sampling_interval_minutes": 15,
        "polymarket.sigma_lookback_calendar_days": 7,
        "polymarket.sigma_missing_snapshot_rule": (
            "omit_missing_slots_no_forward_fill_minimum_48_observations"
        ),
        "polymarket.sigma_estimator_version": "phase1-sigma-v1",
        "polymarket.sigma_rule_sha256": "b" * 64,
        "costs.ai_direct_costs": (
            "openai",
            "jev",
            "evidence_retrieval",
            "paid_evidence_data",
            "incremental_ai_infrastructure",
        ),
        "costs.deterministic_direct_costs": ("deterministic_specific",),
        "costs.truly_shared_costs": (
            "market_data",
            "scheduler",
            "storage",
            "reporting",
        ),
        "costs.rule_version": "phase1-cost-allocation-v1",
        "costs.rule_sha256": "3" * 64,
        "endpoint.rule_version": "phase1-endpoint-v1",
        "endpoint.rule_sha256": "4" * 64,
        "statistics.rule_version": "phase1-statistics-v1",
        "statistics.rule_sha256": "5" * 64,
        "baselines.stock_passive_oef_ticker": "OEF",
        "baselines.stock_passive_oef_instrument": "iShares S&P 100 ETF total return",
        "baselines.stock_passive_oef_source_contract": (
            "yahoo_chart_v8_adjusted_for_explicit_actions"
        ),
        "runtime.sqlite_busy_timeout_ms": 5000,
        "quotas.retry_jitter_seed": 20261002,
        "quotas.retry_jitter_policy": (
            "seeded_uniform_plus_or_minus_fraction_of_backoff"
        ),
        "statistics.positive_cumulative_incremental_economic_pnl_required": True,
        "statistics.polymarket_cash_return_gate_required": True,
        "statistics.stock_cash_return_gate_required": True,
        "statistics.stock_passive_return_gate_required": True,
        "statistics.base_and_joint_adverse_must_pass_and_match": True,
        "statistics.zero_deterministic_retention_denominator_result": "INCONCLUSIVE",
        "statistics.missing_probability_rule": "substitute_frozen_baseline",
        "statistics.ece_bins": 10,
        "statistics.ece_bin_rule": "equal_width",
        "statistics.missing_label_bound_rule": (
            "joint_paired_score_difference_admissible_endpoint_bounds"
        ),
        "statistics.confirmatory_duration_rule": "max_26_weeks_and_b_min",
        "statistics.power_input_fields": (
            "paired_variance",
            "cross_stream_dependence",
            "serial_dependence",
            "abstention",
            "missingness",
        ),
        "statistics.alternative_theta_probability_micros": (60_000, 60_000),
    }
    value = _example()
    manifest = _manifest_module().load_manifest(EXAMPLE)
    as_dict = manifest.to_dict()
    for path, expected_value in expected.items():
        target: Any = as_dict
        for part in path.split("."):
            target = target[part]
        if isinstance(expected_value, tuple):
            target = tuple(target)
        assert target == expected_value, path

        changed = _example()
        _delete_path(changed, path)
        with pytest.raises(ValueError, match=path.split(".")[-1]):
            _parse_value(changed)

    assert value == _example()


def test_nyse_provider_contract_version_is_exact_independent_of_hash() -> None:
    value = _example()
    value["providers"]["nyse_calendar"]["contract_version"] = (
        "phase1-provider-contract-v2"
    )
    with pytest.raises(ValueError, match="provider contract version"):
        _parse_value(value)

    value = _example()
    value["providers"]["nyse_calendar"]["contract_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="provider contract hash"):
        _parse_value(value)


def test_provider_contracts_are_exact_and_sec_evidence_is_complete() -> None:
    manifest = _manifest_module().load_manifest(EXAMPLE)
    assert manifest.providers.sec_evidence.endpoints == (
        "https://data.sec.gov/submissions/CIK##########.json",
        "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/{primary_document}",
        "https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json",
    )
    assert manifest.providers.issuer_news.allowed_source_classes == (
        "issuer_investor_relations_rss_atom",
        "google_news_rss_discovery_only",
        "original_publisher_https",
    )
    assert manifest.providers.issuer_news.allowed_domains == (
        "news.google.com",
        "sec.gov",
        "www.sec.gov",
    )

    for path, replacement in (
        ("providers.polymarket_catalog.timestamp_semantics", "backdated"),
        ("providers.polymarket_books.precedence", 2),
        ("providers.sec_evidence.validation", "accept_anything"),
        (
            "providers.data_fallbacks.treasury_last_published_prior.endpoint",
            "https://evil.test",
        ),
        (
            "providers.data_fallbacks.treasury_last_published_prior.timestamp_semantics",
            "backdated",
        ),
        ("providers.data_fallbacks.treasury_last_published_prior.precedence", 1),
        ("providers.data_fallbacks.treasury_last_published_prior.validation", "none"),
        (
            "providers.data_fallbacks.treasury_last_published_prior.mapping",
            ["value_to_evil"],
        ),
    ):
        value = _example()
        _set_path(value, path, replacement)
        with pytest.raises(ValueError):
            _parse_value(value)


def test_categories_and_model_snapshots_reject_ambiguous_inputs() -> None:
    for categories in ([], ["business", "business"], ["Business"], ["Sports"]):
        value = _example()
        value["polymarket"]["accepted_categories"] = categories
        with pytest.raises(ValueError):
            _parse_value(value)

    for provider, bad_ids in (
        ("openai", ("gpt-5", "gpt-4o", "gpt-5-latest", "default")),
        ("jev", ("jev", "jev-latest", "systemone", "current")),
    ):
        for bad_id in bad_ids:
            value = _example()
            value["models"][provider]["request_model_snapshot"] = bad_id
            value["models"][provider]["expected_returned_model_id"] = bad_id
            with pytest.raises(ValueError):
                _parse_value(value)


def test_model_snapshots_reject_reserved_alias_components_and_invalid_dates() -> None:
    for provider, bad_ids in (
        (
            "openai",
            (
                "gpt-latest-2026-10-02",
                "gpt-5-current-preview-2026-10-02",
                "gpt-5-DEFAULT-2026-10-02",
                "gpt-5_latest_2026-10-02",
                "gpt-5-2026-99-99",
                "gpt-5-2026-02-30",
            ),
        ),
        (
            "jev",
            (
                "jev-latest-2026-10-02",
                "jev-phase1-current-build-2026-10-02",
                "jev-DEFAULT-2026-10-02",
                "jev-phase1.default.2026-10-02",
                "jev-phase1-2026-99-99",
                "jev-phase1-2026-02-30",
            ),
        ),
    ):
        for bad_id in bad_ids:
            value = _example()
            value["models"][provider]["request_model_snapshot"] = bad_id
            value["models"][provider]["expected_returned_model_id"] = bad_id
            with pytest.raises(ValueError):
                _parse_value(value)

    for provider, valid_id in (
        ("openai", "gpt-5-2025-08-07"),
        ("jev", "jev-systemone-phase1-2026-10-02"),
    ):
        value = _example()
        value["models"][provider]["request_model_snapshot"] = valid_id
        value["models"][provider]["expected_returned_model_id"] = valid_id
        _parse_value(value)


def test_statistics_information_contract_is_exact() -> None:
    manifest = _manifest_module().load_manifest(EXAMPLE)
    assert manifest.statistics.numeric_information_requirements == (
        "complete_enrollment_and_fixed_tm_analysis",
        "finite_theta_for_both_streams",
        "valid_base_and_joint_adverse_replays",
        "nonzero_deterministic_count_retention_denominator",
        "nonzero_deterministic_dollar_day_retention_denominator",
        "99999_valid_bootstrap_replicates_within_999990_draws",
        "power_simulation_artifact_with_b_min",
    )
    assert manifest.statistics.classification_precedence == (
        "OPERATIONALLY_INFEASIBLE",
        "INCONCLUSIVE",
        "PROMISING",
        "NO_EVIDENCE",
    )


def test_parser_rejects_excessive_structure_and_caps_sorted_errors() -> None:
    module = _manifest_module()
    nested = b"{" + b'"x":{' * 1100 + b'"y":1' + b"}" * 1101
    with pytest.raises(ValueError, match="nesting depth"):
        module.parse_manifest_bytes(nested)

    value = _example()
    value["polymarket"]["accepted_categories"] = [""] * 40_000
    with pytest.raises(ValueError, match="container items"):
        _parse_value(value)

    left = _example()
    left["z_unknown"] = 1
    left["a_unknown"] = 2
    right = _example()
    right["a_unknown"] = 2
    right["z_unknown"] = 1
    messages = []
    for value in (left, right):
        with pytest.raises(ValueError) as caught:
            _parse_value(value)
        messages.append(str(caught.value))
    assert messages[0] == messages[1]
    assert len(messages[0].splitlines()) <= module.MAX_VALIDATION_ERRORS + 2


def test_loader_reads_bounded_descriptor_once_and_detects_size_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _manifest_module()
    path = tmp_path / "manifest.json"
    path.write_bytes(EXAMPLE.read_bytes())
    original_fstat = module.os.fstat
    calls = 0

    def changing_fstat(fd: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        result = original_fstat(fd)
        if calls == 2:
            fields = list(result)
            fields[6] += 1
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(module.os, "fstat", changing_fstat)
    with pytest.raises(ValueError, match="changed while reading"):
        module.load_manifest(path)
    assert calls == 2

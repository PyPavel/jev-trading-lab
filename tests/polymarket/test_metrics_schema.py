from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
from types import MappingProxyType
from typing import Any

import pytest

from jev_trading_lab.polymarket import metrics_schema
from jev_trading_lab.polymarket.metrics_schema import (
    METRIC_FAMILIES,
    MetricFamily,
    MetricType,
    metric_family,
    validate_labels,
)

PREFIX = "jev_polymarket_"
SECTIONS = {
    "snapshot": (
        "metrics_schema_version",
        "metrics_snapshot_generation_timestamp_seconds",
        "metrics_snapshot_source_max_timestamp_seconds",
        "metrics_regenerations_total",
        "metrics_source_unknown_total",
        "metrics_series",
    ),
    "trading": (
        "opportunities_total",
        "arm_dispositions_total",
        "fills_total",
        "abstains_total",
        "rejections_total",
        "fill_quantity_contracts_total",
        "fill_entry_notional_usd_total",
        "fill_mean_quantity_contracts",
        "fill_entry_vwap_probability",
        "fees_usd_total",
        "slippage_usd_total",
        "spread_cost_usd_total",
        "realized_pnl_usd",
        "unrealized_pnl_usd",
        "trading_pnl_usd",
        "economic_pnl_usd",
        "capital_initial_usd",
        "capital_cash_usd",
        "capital_equity_usd",
        "capital_gross_exposure_usd",
        "forecasts_total",
        "forecast_brier_score_total",
        "forecast_mean_brier_score",
        "forecast_coverage_ratio",
        "baseline_mean_brier_score",
        "paired_trading_pnl_delta_usd",
        "paired_economic_pnl_delta_usd",
        "paired_mean_brier_delta",
    ),
    "llm": (
        "llm_model_snapshot_active",
        "llm_calls_total",
        "llm_failures_total",
        "llm_timeouts_total",
        "llm_call_duration_seconds",
        "llm_input_tokens_total",
        "llm_cached_input_tokens_total",
        "llm_output_tokens_total",
        "llm_token_usage_unknown_total",
        "llm_billed_cost_usd_total",
        "llm_unknown_cost_calls_total",
        "llm_parse_checks_total",
        "llm_citation_checks_total",
        "llm_restart_recoveries_total",
    ),
    "market_data": (
        "market_data_calls_total",
        "market_data_failures_total",
        "market_data_call_duration_seconds",
        "market_data_malformed_total",
        "market_data_stale_total",
        "market_eligibility_total",
        "market_book_spread_probability",
        "market_book_spread_probability_histogram",
        "market_book_bid_near_depth_contracts",
        "market_book_ask_near_depth_contracts",
        "market_book_bid_near_depth_contracts_histogram",
        "market_book_ask_near_depth_contracts_histogram",
        "market_data_last_poll_timestamp_seconds",
        "market_data_poll_age_seconds",
        "market_data_latest_book_age_seconds",
    ),
    "pipeline": (
        "pipeline_runs_total",
        "pipeline_terminal_results_total",
        "pipeline_stage_duration_seconds",
        "pipeline_stage_outcomes_total",
        "pipeline_last_success_timestamp_seconds",
        "pipeline_last_terminal_timestamp_seconds",
        "pipeline_pending_work_items",
        "pipeline_oldest_pending_age_seconds",
        "pipeline_unresolved_observations",
        "pipeline_decision_barriers_total",
        "pipeline_decision_barrier_age_seconds",
        "pipeline_terminal_ratio",
    ),
    "reliability": (
        "reliability_restarts_total",
        "reliability_replay_attempts_total",
        "reliability_recovery_actions_total",
        "reliability_unknown_in_flight_total",
        "reliability_deduplications_total",
        "reliability_journal_corruption_total",
        "reliability_blob_corruption_total",
        "reliability_fsync_failures_total",
        "reliability_write_failures_total",
        "reliability_journal_bytes",
        "reliability_verified_blobs",
    ),
    "experiment": (
        "experiment_paired_samples_total",
        "experiment_paired_wins_total",
        "experiment_ai_win_rate",
        "experiment_cumulative_pnl_usd",
        "experiment_cumulative_paired_pnl_delta_usd",
        "experiment_mean_brier_score",
        "experiment_forecast_coverage_ratio",
        "experiment_model_cost_usd_total",
        "experiment_model_cost_adjusted_edge_usd",
        "experiment_model_cost_adjusted_edge_per_pair_usd",
    ),
    "process": (
        "process_last_run_runtime_seconds",
        "process_resident_memory_bytes",
        "process_peak_resident_memory_bytes",
        "process_cpu_seconds_total",
        "process_open_fds",
        "process_build_version",
        "process_build_commit_timestamp_seconds",
        "process_build_dirty",
        "process_build_identity",
    ),
}

HISTOGRAMS = {
    "llm_call_duration_seconds",
    "market_data_call_duration_seconds",
    "market_book_spread_probability_histogram",
    "market_book_bid_near_depth_contracts_histogram",
    "market_book_ask_near_depth_contracts_histogram",
    "pipeline_stage_duration_seconds",
}

EXPECTED_LABELS = {
    "metrics_regenerations_total": ("status",),
    "metrics_source_unknown_total": ("reason",),
    "opportunities_total": ("status",),
    "arm_dispositions_total": ("arm", "status"),
    "fills_total": ("arm", "status"),
    "abstains_total": ("arm", "reason"),
    "rejections_total": ("arm", "reason"),
    "fill_quantity_contracts_total": ("arm",),
    "fill_entry_notional_usd_total": ("arm",),
    "fill_mean_quantity_contracts": ("arm",),
    "fill_entry_vwap_probability": ("arm",),
    "fees_usd_total": ("arm",),
    "slippage_usd_total": ("arm",),
    "spread_cost_usd_total": ("arm",),
    "realized_pnl_usd": ("arm",),
    "unrealized_pnl_usd": ("arm",),
    "trading_pnl_usd": ("arm",),
    "economic_pnl_usd": ("arm",),
    "capital_initial_usd": ("arm",),
    "capital_cash_usd": ("arm",),
    "capital_equity_usd": ("arm",),
    "capital_gross_exposure_usd": ("arm",),
    "forecasts_total": ("arm", "status"),
    "forecast_brier_score_total": ("arm",),
    "forecast_mean_brier_score": ("arm",),
    "forecast_coverage_ratio": ("arm",),
    "llm_model_snapshot_active": ("provider", "model_snapshot"),
    "llm_calls_total": ("provider", "model_snapshot", "status"),
    "llm_failures_total": ("provider", "model_snapshot", "reason"),
    "llm_timeouts_total": ("provider", "model_snapshot"),
    "llm_call_duration_seconds": ("provider", "model_snapshot"),
    "llm_input_tokens_total": ("provider", "model_snapshot"),
    "llm_cached_input_tokens_total": ("provider", "model_snapshot"),
    "llm_output_tokens_total": ("provider", "model_snapshot"),
    "llm_token_usage_unknown_total": ("provider", "model_snapshot"),
    "llm_billed_cost_usd_total": ("provider", "model_snapshot"),
    "llm_unknown_cost_calls_total": ("provider", "model_snapshot"),
    "llm_parse_checks_total": ("provider", "model_snapshot", "status"),
    "llm_citation_checks_total": ("provider", "model_snapshot", "status"),
    "llm_restart_recoveries_total": ("provider", "model_snapshot", "status"),
    "market_data_calls_total": ("provider", "status"),
    "market_data_failures_total": ("provider", "reason"),
    "market_data_call_duration_seconds": ("provider",),
    "market_data_malformed_total": ("provider", "reason"),
    "market_data_stale_total": ("provider", "stage"),
    "market_eligibility_total": ("status", "reason"),
    "market_book_spread_probability": ("stage",),
    "market_book_spread_probability_histogram": ("stage",),
    "market_book_bid_near_depth_contracts": ("stage",),
    "market_book_ask_near_depth_contracts": ("stage",),
    "market_book_bid_near_depth_contracts_histogram": ("stage",),
    "market_book_ask_near_depth_contracts_histogram": ("stage",),
    "market_data_last_poll_timestamp_seconds": ("status",),
    "market_data_poll_age_seconds": ("status",),
    "market_data_latest_book_age_seconds": ("stage",),
    "pipeline_runs_total": ("status",),
    "pipeline_terminal_results_total": ("status", "reason"),
    "pipeline_stage_duration_seconds": ("stage",),
    "pipeline_stage_outcomes_total": ("stage", "status"),
    "pipeline_last_terminal_timestamp_seconds": ("status",),
    "pipeline_pending_work_items": ("stage",),
    "pipeline_oldest_pending_age_seconds": ("stage",),
    "pipeline_unresolved_observations": ("status",),
    "pipeline_decision_barriers_total": ("status",),
    "pipeline_decision_barrier_age_seconds": ("status",),
    "reliability_restarts_total": ("status",),
    "reliability_replay_attempts_total": ("status",),
    "reliability_recovery_actions_total": ("stage", "status", "reason"),
    "reliability_unknown_in_flight_total": ("provider",),
    "reliability_deduplications_total": ("stage",),
    "reliability_journal_corruption_total": ("reason",),
    "reliability_blob_corruption_total": ("reason",),
    "reliability_fsync_failures_total": ("stage",),
    "reliability_write_failures_total": ("stage", "reason"),
    "reliability_journal_bytes": ("stage",),
    "reliability_verified_blobs": ("status",),
    "experiment_paired_samples_total": ("status",),
    "experiment_paired_wins_total": ("status",),
    "experiment_cumulative_pnl_usd": ("arm",),
    "experiment_mean_brier_score": ("arm",),
    "experiment_forecast_coverage_ratio": ("arm",),
    "experiment_model_cost_usd_total": ("provider",),
    "process_last_run_runtime_seconds": ("status",),
    "process_cpu_seconds_total": ("status",),
    "process_build_identity": ("status",),
}

EXPECTED_BUCKETS = {
    "llm_call_duration_seconds": "0.1 0.25 0.5 1 2.5 5 10 20 30 60",
    "market_data_call_duration_seconds": "0.05 0.1 0.25 0.5 1 2.5 5 10 20 30",
    "market_book_spread_probability_histogram": (
        "0.001 0.0025 0.005 0.01 0.02 0.03 0.04 0.05 0.1 0.25"
    ),
    "market_book_bid_near_depth_contracts_histogram": (
        "100 500 1000 2500 5000 10000 25000 50000 100000"
    ),
    "market_book_ask_near_depth_contracts_histogram": (
        "100 500 1000 2500 5000 10000 25000 50000 100000"
    ),
    "pipeline_stage_duration_seconds": (
        "0.001 0.01 0.05 0.1 0.25 0.5 1 2.5 5 10 30 60 300"
    ),
}


def _full(short_name: str) -> str:
    return PREFIX + short_name


def _family(short_name: str) -> MetricFamily:
    return metric_family(_full(short_name))


def test_registry_matches_exact_coverage_ledger_and_section_counts() -> None:
    expected_short_names = {name for names in SECTIONS.values() for name in names}
    actual_names = tuple(family.name for family in METRIC_FAMILIES)

    assert {name.removeprefix(PREFIX) for name in actual_names} == expected_short_names
    assert len(actual_names) == len(set(actual_names)) == 105
    assert {section: len(names) for section, names in SECTIONS.items()} == {
        "snapshot": 6,
        "trading": 28,
        "llm": 14,
        "market_data": 15,
        "pipeline": 12,
        "reliability": 11,
        "experiment": 10,
        "process": 9,
    }


def test_registry_is_lexical_and_obeys_name_type_suffix_rules() -> None:
    names = tuple(family.name for family in METRIC_FAMILIES)

    assert names == tuple(sorted(names))
    for family in METRIC_FAMILIES:
        assert family.name.startswith(PREFIX)
        assert family.name.replace("_", "a").isalnum()
        assert family.help and "\n" not in family.help
        short_name = family.name.removeprefix(PREFIX)
        expected_type = (
            MetricType.HISTOGRAM
            if short_name in HISTOGRAMS
            else MetricType.COUNTER
            if short_name.endswith("_total")
            else MetricType.GAUGE
        )
        assert family.type is expected_type
        assert family.name.endswith("_total") is (family.type is MetricType.COUNTER)
        assert (family.type is MetricType.HISTOGRAM) is bool(family.buckets)


def test_registry_has_exact_declared_labels_without_structural_le() -> None:
    for family in METRIC_FAMILIES:
        short_name = family.name.removeprefix(PREFIX)
        assert family.labels == EXPECTED_LABELS.get(short_name, ())
        assert set(family.label_values) == set(family.labels)
        assert "le" not in family.labels
        assert set(family.labels) <= {
            "arm",
            "provider",
            "model_snapshot",
            "status",
            "reason",
            "stage",
        }


def test_histogram_bucket_vectors_are_exact_finite_decimals() -> None:
    actual = {
        family.name.removeprefix(PREFIX): family.buckets
        for family in METRIC_FAMILIES
        if family.type is MetricType.HISTOGRAM
    }
    expected = {
        name: tuple(Decimal(value) for value in values.split())
        for name, values in EXPECTED_BUCKETS.items()
    }

    assert actual == expected
    assert all(value.is_finite() for buckets in actual.values() for value in buckets)


def test_metric_family_and_nested_label_values_are_immutable() -> None:
    mutable_values: Any = {"arm": {"ai", "deterministic"}}
    family = MetricFamily(
        name="jev_polymarket_test_total",
        help="Test family.",
        type=MetricType.COUNTER,
        labels=("arm",),
        label_values=mutable_values,
    )

    mutable_values["arm"].add("changed")

    assert isinstance(family.label_values, MappingProxyType)
    assert family.label_values["arm"] == frozenset({"ai", "deterministic"})
    with pytest.raises(FrozenInstanceError):
        family.name = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        family.label_values["arm"] = frozenset({"changed"})  # type: ignore[index]


def test_build_registry_rejects_duplicate_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    labels = dict(metrics_schema._LABELS)
    labels["metrics_regenerations_total"] = ("status", "status")
    monkeypatch.setattr(metrics_schema, "_LABELS", labels)

    with pytest.raises(RuntimeError, match="duplicate labels"):
        metrics_schema._build_registry()


@pytest.mark.parametrize(
    "buckets",
    (
        (Decimal("1"), Decimal("1")),
        (Decimal("2"), Decimal("1")),
        (Decimal("0"), Decimal("1")),
        (Decimal("-1"), Decimal("1")),
        (Decimal("NaN"), Decimal("1")),
        (Decimal("Infinity"),),
    ),
)
def test_build_registry_rejects_invalid_histogram_buckets(
    monkeypatch: pytest.MonkeyPatch, buckets: tuple[Decimal, ...]
) -> None:
    metric = "llm_call_duration_seconds"
    mutated_buckets = dict(metrics_schema._BUCKETS)
    mutated_buckets[metric] = buckets
    monkeypatch.setattr(metrics_schema, "_BUCKETS", mutated_buckets)

    with pytest.raises(RuntimeError, match="histogram buckets"):
        metrics_schema._build_registry()


def test_metric_family_rejects_an_unknown_name() -> None:
    with pytest.raises(ValueError, match="unknown metric family"):
        metric_family("jev_polymarket_not_registered")


def test_validate_labels_returns_lexically_sorted_pairs() -> None:
    family = _family("llm_calls_total")

    assert validate_labels(
        family,
        {
            "status": "success",
            "provider": "openai",
            "model_snapshot": "gpt-5-2025-08-07",
        },
    ) == (
        ("model_snapshot", "gpt-5-2025-08-07"),
        ("provider", "openai"),
        ("status", "success"),
    )


@pytest.mark.parametrize(
    ("metric", "labels", "message"),
    (
        ("fills_total", {"arm": "ai", "market_id": "123"}, "unexpected labels"),
        ("fills_total", {}, "missing labels"),
        ("fills_total", {"arm": "oracle", "status": "full"}, "invalid arm"),
        (
            "pipeline_stage_duration_seconds",
            {"stage": "dynamic_stage"},
            "invalid stage",
        ),
        (
            "pipeline_terminal_results_total",
            {"status": "failed", "reason": "exception text"},
            "invalid reason",
        ),
        (
            "pipeline_runs_total",
            {"status": "http_503"},
            "invalid status",
        ),
    ),
)
def test_validate_labels_rejects_unbounded_dimensions(
    metric: str, labels: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_labels(_family(metric), labels)


@pytest.mark.parametrize(
    "labels",
    (
        *(
            {"status": "eligible", "reason": reason}
            for reason in sorted(metrics_schema.TRADING_REASONS - {"none"})
        ),
        {"status": "rejected", "reason": "none"},
    ),
)
def test_validate_labels_rejects_invalid_market_eligibility_cross_products(
    labels: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="status/reason"):
        validate_labels(_family("market_eligibility_total"), labels)


@pytest.mark.parametrize(
    "labels",
    (
        {"status": "eligible", "reason": "none"},
        *(
            {"status": "rejected", "reason": reason}
            for reason in sorted(metrics_schema.TRADING_REASONS - {"none"})
        ),
    ),
)
def test_validate_labels_accepts_valid_market_eligibility_pairs(
    labels: dict[str, str],
) -> None:
    assert validate_labels(_family("market_eligibility_total"), labels)


@pytest.mark.parametrize(
    ("provider", "model_snapshot"),
    (
        ("openai", "jev-1.13.0"),
        ("jev", "gpt-5-2025-08-07"),
        ("openai", "gpt-latest"),
        ("gamma", "gpt-5-2025-08-07"),
        ("clob", "jev-1.13.0"),
    ),
)
def test_validate_labels_rejects_provider_model_cross_products(
    provider: str, model_snapshot: str
) -> None:
    with pytest.raises(ValueError, match="provider/model_snapshot"):
        validate_labels(
            _family("llm_model_snapshot_active"),
            {"provider": provider, "model_snapshot": model_snapshot},
        )


@pytest.mark.parametrize(
    ("provider", "model_snapshot"),
    (("openai", "gpt-5-2025-08-07"), ("jev", "jev-1.13.0")),
)
def test_validate_labels_accepts_only_frozen_provider_model_pairs(
    provider: str, model_snapshot: str
) -> None:
    assert validate_labels(
        _family("llm_model_snapshot_active"),
        {"provider": provider, "model_snapshot": model_snapshot},
    )


def test_gamma_and_clob_families_have_no_model_snapshot_dimension() -> None:
    for metric in (
        "market_data_calls_total",
        "market_data_failures_total",
        "market_data_call_duration_seconds",
        "market_data_malformed_total",
        "market_data_stale_total",
    ):
        family = _family(metric)
        assert "model_snapshot" not in family.labels
        labels = {
            label: next(iter(family.label_values[label])) for label in family.labels
        }
        labels["model_snapshot"] = "gpt-5-2025-08-07"
        with pytest.raises(ValueError, match="unexpected labels"):
            validate_labels(family, labels)


def test_metric_specific_status_reason_stage_and_provider_bounds() -> None:
    assert _family("metrics_regenerations_total").label_values["status"] == frozenset(
        {"success", "failure", "skipped"}
    )
    assert _family("abstains_total").label_values["reason"] == frozenset(
        {"abstain", "insufficient", "ambiguous", "control_unavailable", "other"}
    )
    assert _family("market_data_calls_total").label_values["provider"] == frozenset(
        {"gamma", "clob"}
    )
    assert _family("market_data_stale_total").label_values["provider"] == frozenset(
        {"clob"}
    )
    assert _family("llm_citation_checks_total").label_values["provider"] == frozenset(
        {"openai"}
    )
    assert _family("market_book_spread_probability").label_values["stage"] == frozenset(
        {"precheck_book", "execution_book"}
    )
    assert _family("reliability_deduplications_total").label_values[
        "stage"
    ] == frozenset({"openai", "jev", "terminal_commit", "completed_slot", "replay"})

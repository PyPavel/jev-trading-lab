"""Frozen bounded Prometheus metric-family schema for Polymarket observability."""

# Long catalog strings are exact HELP text from the approved specification.
# ruff: noqa: E501

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

_PREFIX = "jev_polymarket_"
_METRIC_NAME = re.compile(r"[a-z_:][a-z0-9_:]*\Z")

ARMS = frozenset({"ai", "deterministic"})
PROVIDERS = frozenset({"openai", "jev", "gamma", "clob"})
MODEL_SNAPSHOTS = frozenset({"gpt-5-2025-08-07", "jev-1.13.0"})
PROVIDER_MODEL_PAIRS = frozenset(
    {
        ("openai", "gpt-5-2025-08-07"),
        ("jev", "jev-1.13.0"),
    }
)
STAGES = frozenset(
    {
        "startup",
        "recovery",
        "gamma_poll",
        "eligibility",
        "precheck_book",
        "evidence_freeze",
        "openai",
        "jev",
        "decision_barrier",
        "execution_book",
        "paper_execution",
        "terminal_commit",
        "completed_slot",
        "metrics_render",
        "replay",
    }
)
EXPORTER_REASONS = frozenset(
    {
        "fees",
        "slippage",
        "realized_pnl",
        "unrealized_pnl",
        "capital",
        "brier",
        "latency",
        "tokens",
        "cost",
        "process",
        "other",
    }
)
TRADING_REASONS = frozenset(
    {
        "none",
        "inactive",
        "closed",
        "sports",
        "non_binary",
        "missing_evidence",
        "close_window",
        "low_volume",
        "wide_spread",
        "low_depth",
        "capacity",
        "risk",
        "no_edge",
        "abstain",
        "insufficient",
        "ambiguous",
        "control_unavailable",
        "book_failure",
        "model_failure",
        "public_data_failure",
        "other",
    }
)
MARKET_DATA_REASONS = frozenset(
    {
        "timeout",
        "transport",
        "http_status",
        "redirect",
        "content_type",
        "oversized",
        "not_json",
        "malformed",
        "stale",
        "crossed",
        "token_drift",
        "market_drift",
        "duplicate",
        "other",
    }
)
MODEL_REASONS = frozenset(
    {
        "input_error",
        "input_changed",
        "config_failure",
        "unknown_in_flight",
        "provider_error",
        "timeout",
        "http_error",
        "parse_error",
        "model_mismatch",
        "citation_error",
        "credential_reflection",
        "cost_overflow",
        "other",
    }
)
RELIABILITY_REASONS = frozenset(
    {
        "partial_tail",
        "reused_result",
        "unknown_in_flight",
        "stale_temporary",
        "invalid_line",
        "invalid_transition",
        "conflict",
        "missing_blob",
        "digest_mismatch",
        "unsafe_path",
        "lock_timeout",
        "write",
        "flush",
        "fsync",
        "rename",
        "directory_fsync",
        "other",
    }
)


class MetricType(StrEnum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


@dataclass(frozen=True, slots=True)
class MetricFamily:
    name: str
    help: str
    type: MetricType
    labels: tuple[str, ...] = ()
    label_values: Mapping[str, frozenset[str]] = field(default_factory=dict)
    buckets: tuple[Decimal, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "label_values",
            MappingProxyType(
                {
                    label: frozenset(values)
                    for label, values in self.label_values.items()
                }
            ),
        )


_LABELS: dict[str, tuple[str, ...]] = {
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

_LABEL_VALUES: dict[str, Mapping[str, frozenset[str]]] = {
    "abstains_total": {
        "arm": ARMS,
        "reason": frozenset(
            {"abstain", "insufficient", "ambiguous", "control_unavailable", "other"}
        ),
    },
    "arm_dispositions_total": {
        "arm": ARMS,
        "status": frozenset(
            {
                "model_failure",
                "partial_fill",
                "cost_overflow",
                "no_edge",
                "unknown_in_flight",
                "abstain",
                "ineligible",
                "book_failure",
                "filled",
            }
        ),
    },
    "capital_cash_usd": {
        "arm": ARMS,
    },
    "capital_equity_usd": {
        "arm": ARMS,
    },
    "capital_gross_exposure_usd": {
        "arm": ARMS,
    },
    "capital_initial_usd": {
        "arm": ARMS,
    },
    "economic_pnl_usd": {
        "arm": ARMS,
    },
    "experiment_cumulative_pnl_usd": {
        "arm": ARMS,
    },
    "experiment_forecast_coverage_ratio": {
        "arm": ARMS,
    },
    "experiment_mean_brier_score": {
        "arm": ARMS,
    },
    "experiment_model_cost_usd_total": {
        "provider": frozenset({"openai", "jev"}),
    },
    "experiment_paired_samples_total": {
        "status": frozenset({"unknown_cost", "resolved", "pending", "unscored"}),
    },
    "experiment_paired_wins_total": {
        "status": frozenset({"ai", "deterministic", "tie"}),
    },
    "fees_usd_total": {
        "arm": ARMS,
    },
    "fill_entry_notional_usd_total": {
        "arm": ARMS,
    },
    "fill_entry_vwap_probability": {
        "arm": ARMS,
    },
    "fill_mean_quantity_contracts": {
        "arm": ARMS,
    },
    "fill_quantity_contracts_total": {
        "arm": ARMS,
    },
    "fills_total": {
        "arm": ARMS,
        "status": frozenset({"full", "partial"}),
    },
    "forecast_brier_score_total": {
        "arm": ARMS,
    },
    "forecast_coverage_ratio": {
        "arm": ARMS,
    },
    "forecast_mean_brier_score": {
        "arm": ARMS,
    },
    "forecasts_total": {
        "arm": ARMS,
        "status": frozenset({"scored", "pending", "unavailable"}),
    },
    "llm_billed_cost_usd_total": {
        "provider": frozenset({"openai", "jev"}),
        "model_snapshot": MODEL_SNAPSHOTS,
    },
    "llm_cached_input_tokens_total": {
        "provider": frozenset({"openai"}),
        "model_snapshot": frozenset({"gpt-5-2025-08-07"}),
    },
    "llm_call_duration_seconds": {
        "provider": frozenset({"openai", "jev"}),
        "model_snapshot": MODEL_SNAPSHOTS,
    },
    "llm_calls_total": {
        "provider": frozenset({"openai", "jev"}),
        "model_snapshot": MODEL_SNAPSHOTS,
        "status": frozenset(
            {"failure", "success", "cost_overflow", "unknown_in_flight"}
        ),
    },
    "llm_citation_checks_total": {
        "provider": frozenset({"openai"}),
        "model_snapshot": frozenset({"gpt-5-2025-08-07"}),
        "status": frozenset({"success", "failure"}),
    },
    "llm_failures_total": {
        "provider": frozenset({"openai", "jev"}),
        "model_snapshot": MODEL_SNAPSHOTS,
        "reason": MODEL_REASONS,
    },
    "llm_input_tokens_total": {
        "provider": frozenset({"openai", "jev"}),
        "model_snapshot": MODEL_SNAPSHOTS,
    },
    "llm_model_snapshot_active": {
        "provider": frozenset({"openai", "jev"}),
        "model_snapshot": MODEL_SNAPSHOTS,
    },
    "llm_output_tokens_total": {
        "provider": frozenset({"openai", "jev"}),
        "model_snapshot": MODEL_SNAPSHOTS,
    },
    "llm_parse_checks_total": {
        "provider": frozenset({"openai", "jev"}),
        "model_snapshot": MODEL_SNAPSHOTS,
        "status": frozenset({"success", "failure"}),
    },
    "llm_restart_recoveries_total": {
        "provider": frozenset({"openai", "jev"}),
        "model_snapshot": MODEL_SNAPSHOTS,
        "status": frozenset({"reused_result", "pending", "unknown_in_flight"}),
    },
    "llm_timeouts_total": {
        "provider": frozenset({"openai", "jev"}),
        "model_snapshot": MODEL_SNAPSHOTS,
    },
    "llm_token_usage_unknown_total": {
        "provider": frozenset({"openai", "jev"}),
        "model_snapshot": MODEL_SNAPSHOTS,
    },
    "llm_unknown_cost_calls_total": {
        "provider": frozenset({"openai", "jev"}),
        "model_snapshot": MODEL_SNAPSHOTS,
    },
    "market_book_ask_near_depth_contracts": {
        "stage": frozenset({"precheck_book", "execution_book"}),
    },
    "market_book_ask_near_depth_contracts_histogram": {
        "stage": frozenset({"precheck_book", "execution_book"}),
    },
    "market_book_bid_near_depth_contracts": {
        "stage": frozenset({"precheck_book", "execution_book"}),
    },
    "market_book_bid_near_depth_contracts_histogram": {
        "stage": frozenset({"precheck_book", "execution_book"}),
    },
    "market_book_spread_probability": {
        "stage": frozenset({"precheck_book", "execution_book"}),
    },
    "market_book_spread_probability_histogram": {
        "stage": frozenset({"precheck_book", "execution_book"}),
    },
    "market_data_call_duration_seconds": {
        "provider": frozenset({"gamma", "clob"}),
    },
    "market_data_calls_total": {
        "provider": frozenset({"gamma", "clob"}),
        "status": frozenset({"timeout", "success", "failure"}),
    },
    "market_data_failures_total": {
        "provider": frozenset({"gamma", "clob"}),
        "reason": MARKET_DATA_REASONS,
    },
    "market_data_last_poll_timestamp_seconds": {
        "status": frozenset({"success", "failure"}),
    },
    "market_data_latest_book_age_seconds": {
        "stage": frozenset({"precheck_book", "execution_book"}),
    },
    "market_data_malformed_total": {
        "provider": frozenset({"gamma", "clob"}),
        "reason": frozenset(
            {"content_type", "oversized", "not_json", "malformed", "duplicate", "other"}
        ),
    },
    "market_data_poll_age_seconds": {
        "status": frozenset({"success", "failure"}),
    },
    "market_data_stale_total": {
        "stage": frozenset({"precheck_book", "execution_book"}),
        "provider": frozenset({"clob"}),
    },
    "market_eligibility_total": {
        "reason": TRADING_REASONS,
        "status": frozenset({"rejected", "eligible"}),
    },
    "metrics_regenerations_total": {
        "status": frozenset({"skipped", "success", "failure"}),
    },
    "metrics_source_unknown_total": {
        "reason": EXPORTER_REASONS,
    },
    "opportunities_total": {
        "status": frozenset(
            {
                "public_data_failure",
                "terminal",
                "model_failure",
                "ineligible",
                "book_failure",
                "eligible",
            }
        ),
    },
    "pipeline_decision_barrier_age_seconds": {
        "status": frozenset({"terminal", "pending_execution"}),
    },
    "pipeline_decision_barriers_total": {
        "status": frozenset(
            {"openai_failure", "jev_failure", "cost_overflow", "jev_success"}
        ),
    },
    "pipeline_last_terminal_timestamp_seconds": {
        "status": frozenset({"recovered", "replayed", "success", "failure"}),
    },
    "pipeline_oldest_pending_age_seconds": {
        "stage": STAGES,
    },
    "pipeline_pending_work_items": {
        "stage": STAGES,
    },
    "pipeline_runs_total": {
        "status": frozenset({"recovered", "replayed", "success", "failure"}),
    },
    "pipeline_stage_duration_seconds": {
        "stage": STAGES,
    },
    "pipeline_stage_outcomes_total": {
        "stage": STAGES,
        "status": frozenset({"skipped", "recovered", "success", "failure"}),
    },
    "pipeline_terminal_results_total": {
        "reason": TRADING_REASONS,
        "status": frozenset({"rejected", "unknown", "failed", "no_fill", "filled"}),
    },
    "pipeline_unresolved_observations": {
        "status": frozenset({"missing_terminal", "unknown_cost", "pending_resolution"}),
    },
    "process_build_identity": {
        "status": frozenset({"mismatch", "unknown", "valid", "dirty"}),
    },
    "process_cpu_seconds_total": {
        "status": frozenset({"system", "user"}),
    },
    "process_last_run_runtime_seconds": {
        "status": frozenset({"recovered", "replayed", "success", "failure"}),
    },
    "realized_pnl_usd": {
        "arm": ARMS,
    },
    "rejections_total": {
        "arm": ARMS,
        "reason": TRADING_REASONS,
    },
    "reliability_blob_corruption_total": {
        "reason": frozenset(
            {"missing_blob", "digest_mismatch", "unsafe_path", "other"}
        ),
    },
    "reliability_deduplications_total": {
        "stage": frozenset(
            {"openai", "jev", "terminal_commit", "completed_slot", "replay"}
        ),
    },
    "reliability_fsync_failures_total": {
        "stage": STAGES,
    },
    "reliability_journal_bytes": {
        "stage": frozenset(
            {"terminal_commit", "completed_slot", "openai", "jev", "recovery"}
        ),
    },
    "reliability_journal_corruption_total": {
        "reason": frozenset(
            {
                "partial_tail",
                "invalid_line",
                "invalid_transition",
                "conflict",
                "unsafe_path",
                "other",
            }
        ),
    },
    "reliability_recovery_actions_total": {
        "stage": STAGES,
        "reason": RELIABILITY_REASONS,
        "status": frozenset({"success", "failure"}),
    },
    "reliability_replay_attempts_total": {
        "status": frozenset({"deduplicated", "success", "failure"}),
    },
    "reliability_restarts_total": {
        "status": frozenset({"recovery_required", "recovery_failed", "clean"}),
    },
    "reliability_unknown_in_flight_total": {
        "provider": frozenset({"openai", "jev"}),
    },
    "reliability_verified_blobs": {
        "status": frozenset({"corrupt", "valid", "missing"}),
    },
    "reliability_write_failures_total": {
        "stage": STAGES,
        "reason": frozenset(
            {"write", "flush", "fsync", "rename", "directory_fsync", "other"}
        ),
    },
    "slippage_usd_total": {
        "arm": ARMS,
    },
    "spread_cost_usd_total": {
        "arm": ARMS,
    },
    "trading_pnl_usd": {
        "arm": ARMS,
    },
    "unrealized_pnl_usd": {
        "arm": ARMS,
    },
}

_BUCKETS = {
    "llm_call_duration_seconds": tuple(
        Decimal(value) for value in "0.1 0.25 0.5 1 2.5 5 10 20 30 60".split()
    ),
    "market_data_call_duration_seconds": tuple(
        Decimal(value) for value in "0.05 0.1 0.25 0.5 1 2.5 5 10 20 30".split()
    ),
    "market_book_spread_probability_histogram": tuple(
        Decimal(value)
        for value in "0.001 0.0025 0.005 0.01 0.02 0.03 0.04 0.05 0.1 0.25".split()
    ),
    "market_book_bid_near_depth_contracts_histogram": tuple(
        Decimal(value)
        for value in "100 500 1000 2500 5000 10000 25000 50000 100000".split()
    ),
    "market_book_ask_near_depth_contracts_histogram": tuple(
        Decimal(value)
        for value in "100 500 1000 2500 5000 10000 25000 50000 100000".split()
    ),
    "pipeline_stage_duration_seconds": tuple(
        Decimal(value)
        for value in "0.001 0.01 0.05 0.1 0.25 0.5 1 2.5 5 10 30 60 300".split()
    ),
}

_CATALOG: tuple[tuple[str, str, MetricType], ...] = (
    (
        "metrics_schema_version",
        "Numeric schema version 1 for this document.",
        MetricType.GAUGE,
    ),
    (
        "metrics_snapshot_generation_timestamp_seconds",
        "UTC time at which the published temporary file's source cut was completed.",
        MetricType.GAUGE,
    ),
    (
        "metrics_snapshot_source_max_timestamp_seconds",
        "Maximum committed event timestamp included in the snapshot; omitted for an empty source.",
        MetricType.GAUGE,
    ),
    (
        "metrics_regenerations_total",
        "Verified operational generation outcomes. The just-published success may appear on the next generation; the generation timestamp is immediate proof of the current publication.",
        MetricType.COUNTER,
    ),
    (
        "metrics_source_unknown_total",
        "Authoritative records that require a metric input but explicitly lack it. This is not emitted for a concept that had no applicable event.",
        MetricType.COUNTER,
    ),
    (
        "metrics_series",
        "Number of series in the current file, including histogram series.",
        MetricType.GAUGE,
    ),
    (
        "opportunities_total",
        "Count of unique terminal opportunity/observation semantics from results.jsonl, classified from validated artifacts. terminal counts every accepted terminal result and is the denominator sanity check.",
        MetricType.COUNTER,
    ),
    (
        "arm_dispositions_total",
        "Terminal arm result count. status is filled|partial_fill|abstain|no_edge|ineligible|model_failure|book_failure|cost_overflow|unknown_in_flight.",
        MetricType.COUNTER,
    ),
    ("fills_total", "Positive-quantity terminal arm results.", MetricType.COUNTER),
    (
        "abstains_total",
        "Explicit abstentions only; failures and no-edge outcomes do not count as abstains. Sufficiency/ambiguity comes from the verified Jev decision.",
        MetricType.COUNTER,
    ),
    (
        "rejections_total",
        "Terminal non-fill rejections, using the bounded trading/eligibility reason set. A single result contributes once per arm to its final reason.",
        MetricType.COUNTER,
    ),
    (
        "fill_quantity_contracts_total",
        "Sum of positive filled contract quantity.",
        MetricType.COUNTER,
    ),
    (
        "fill_entry_notional_usd_total",
        "Sum of exact entry notionals divided by 1_000_000.",
        MetricType.COUNTER,
    ),
    (
        "fill_mean_quantity_contracts",
        "Filled quantity divided by fill count; omitted with zero fills.",
        MetricType.GAUGE,
    ),
    (
        "fill_entry_vwap_probability",
        "Quantity-weighted mean entry VWAP, sum(entry_notional_microdollars) / sum(quantity) / 1_000_000; omitted with zero quantity.",
        MetricType.GAUGE,
    ),
    (
        "fees_usd_total",
        "Sum of explicit fee components only. It must not include modeled slippage unless the economic record identifies that component.",
        MetricType.COUNTER,
    ),
    (
        "slippage_usd_total",
        "Sum of explicit realized/modelled execution slippage postings. Omitted when the journal records only an aggregate fee or threshold assumption.",
        MetricType.COUNTER,
    ),
    (
        "spread_cost_usd_total",
        "Sum of explicitly persisted spread-cost decomposition. Omitted when spread is only implicit in entry VWAP.",
        MetricType.COUNTER,
    ),
    (
        "realized_pnl_usd",
        "Sum of trading P&L for resolved/payout observations plus any explicitly realized exits. Signed gauge, not counter.",
        MetricType.GAUGE,
    ),
    (
        "unrealized_pnl_usd",
        "Sum of mark-to-executable-book P&L for unresolved positions. Quantity beyond verified bid depth is valued at zero, matching the paper contract.",
        MetricType.GAUGE,
    ),
    (
        "trading_pnl_usd",
        "Realized plus unrealized trading P&L at the source cut.",
        MetricType.GAUGE,
    ),
    (
        "economic_pnl_usd",
        "Trading P&L minus known attributable operating/model cost. Omitted for an arm if any included event has unknown economic P&L.",
        MetricType.GAUGE,
    ),
    (
        "capital_initial_usd",
        "Frozen arm initial capital, currently $10,000, from the validated policy/account record; never summed per opportunity.",
        MetricType.GAUGE,
    ),
    (
        "capital_cash_usd",
        "Latest authoritative cash balance. It requires an account/cash record and is not inferred from marked final_cash fields.",
        MetricType.GAUGE,
    ),
    (
        "capital_equity_usd",
        "Initial capital plus cumulative economic P&L at the source cut, only when all economic P&L is known.",
        MetricType.GAUGE,
    ),
    (
        "capital_gross_exposure_usd",
        "Sum of absolute executable marked position notionals at the source cut.",
        MetricType.GAUGE,
    ),
    ("forecasts_total", "Count by strict forecast_status.", MetricType.COUNTER),
    (
        "forecast_brier_score_total",
        "Sum of non-negative scored Brier values in base units.",
        MetricType.COUNTER,
    ),
    (
        "forecast_mean_brier_score",
        "Brier sum divided by scored forecasts; omitted with no scored forecasts. Lower is better.",
        MetricType.GAUGE,
    ),
    (
        "forecast_coverage_ratio",
        "Forecasts with a valid probability divided by terminal eligible opportunities; range [0,1].",
        MetricType.GAUGE,
    ),
    (
        "baseline_mean_brier_score",
        "Mean scored execution-midpoint baseline Brier value; omitted with no scored baseline.",
        MetricType.GAUGE,
    ),
    (
        "paired_trading_pnl_delta_usd",
        "Cumulative AI minus deterministic trading P&L from exact paired deltas. Omitted if any included paired delta overflowed/was unknown.",
        MetricType.GAUGE,
    ),
    (
        "paired_economic_pnl_delta_usd",
        "Cumulative AI minus deterministic economic P&L. Omitted if any included paired economic delta is unknown.",
        MetricType.GAUGE,
    ),
    (
        "paired_mean_brier_delta",
        "Mean AI Brier - deterministic Brier over observations where both are scored. Negative favors AI.",
        MetricType.GAUGE,
    ),
    (
        "llm_model_snapshot_active",
        "1 for each approved pair in the validated runtime configuration represented by retained observations.",
        MetricType.GAUGE,
    ),
    (
        "llm_calls_total",
        "Count unique durable provider-call intents by terminal call outcome. An intent without a result is unknown_in_flight only after recovery classifies it; before recovery it is pending work.",
        MetricType.COUNTER,
    ),
    ("llm_failures_total", "Failures by bounded model reason.", MetricType.COUNTER),
    (
        "llm_timeouts_total",
        "Calls whose durable failure reason is timeout; unknown_in_flight remains separate.",
        MetricType.COUNTER,
    ),
    (
        "llm_call_duration_seconds",
        "Monotonic elapsed call duration from durable call start/result timing. Buckets: 0.1,0.25,0.5,1,2.5,5,10,20,30,60,+Inf. Unknown durations do not enter the histogram.",
        MetricType.HISTOGRAM,
    ),
    (
        "llm_input_tokens_total",
        "Sum of authoritative input token usage from verified responses.",
        MetricType.COUNTER,
    ),
    (
        "llm_cached_input_tokens_total",
        "OpenAI cached input tokens; no Jev series is emitted because Jev has no cached-input field.",
        MetricType.COUNTER,
    ),
    (
        "llm_output_tokens_total",
        "Sum of authoritative output token usage.",
        MetricType.COUNTER,
    ),
    (
        "llm_token_usage_unknown_total",
        "Completed attempts for which valid token usage is absent.",
        MetricType.COUNTER,
    ),
    (
        "llm_billed_cost_usd_total",
        "Sum of known billed microdollars divided by 1_000_000, including failed/abstained calls when billing is known.",
        MetricType.COUNTER,
    ),
    (
        "llm_unknown_cost_calls_total",
        "Attempts with unknown or unrepresentable billed cost. Cost overflow counts here and in call outcome cost_overflow; it never contributes a fabricated numeric cost.",
        MetricType.COUNTER,
    ),
    (
        "llm_parse_checks_total",
        "Strict envelope/schema parse outcomes. Transport/HTTP failures do not count as parse checks.",
        MetricType.COUNTER,
    ),
    (
        "llm_citation_checks_total",
        "OpenAI evidence-hash/citation validation outcomes.",
        MetricType.COUNTER,
    ),
    (
        "llm_restart_recoveries_total",
        "Provider call recovery decisions derived from intent/result journal state.",
        MetricType.COUNTER,
    ),
    (
        "market_data_calls_total",
        "One per persisted HTTP attempt, including attempts that yielded no candidate.",
        MetricType.COUNTER,
    ),
    (
        "market_data_failures_total",
        "Failures by bounded market-data reason.",
        MetricType.COUNTER,
    ),
    (
        "market_data_call_duration_seconds",
        "Monotonic request duration. Buckets: 0.05,0.1,0.25,0.5,1,2.5,5,10,20,30,+Inf.",
        MetricType.HISTOGRAM,
    ),
    (
        "market_data_malformed_total",
        "Structurally unusable responses or rows.",
        MetricType.COUNTER,
    ),
    (
        "market_data_stale_total",
        "Books rejected by the frozen freshness contract.",
        MetricType.COUNTER,
    ),
    (
        "market_eligibility_total",
        'One result per evaluated market candidate. Eligible uses reason="none"; rejected uses the bounded trading/eligibility reason set.',
        MetricType.COUNTER,
    ),
    (
        "market_book_spread_probability",
        "Latest verified best ask minus best bid in probability units.",
        MetricType.GAUGE,
    ),
    (
        "market_book_spread_probability_histogram",
        "Spread distribution. Buckets: 0.001,0.0025,0.005,0.01,0.02,0.03,0.04,0.05,0.1,0.25,+Inf.",
        MetricType.HISTOGRAM,
    ),
    (
        "market_book_bid_near_depth_contracts",
        "Latest sum of bid quantity within 0.02 of best bid.",
        MetricType.GAUGE,
    ),
    (
        "market_book_ask_near_depth_contracts",
        "Latest sum of ask quantity within 0.02 of best ask.",
        MetricType.GAUGE,
    ),
    (
        "market_book_bid_near_depth_contracts_histogram",
        "Bid near-depth distribution. Buckets: 100,500,1000,2500,5000,10000,25000,50000,100000,+Inf.",
        MetricType.HISTOGRAM,
    ),
    (
        "market_book_ask_near_depth_contracts_histogram",
        "Ask near-depth distribution with the same buckets.",
        MetricType.HISTOGRAM,
    ),
    (
        "market_data_last_poll_timestamp_seconds",
        "UTC completion time of the newest terminal Gamma poll by status.",
        MetricType.GAUGE,
    ),
    (
        "market_data_poll_age_seconds",
        "Snapshot generation time minus the corresponding latest poll timestamp. This is age as of generation, not continuously increasing time. Consumers that need current age use time() - jev_polymarket_market_data_last_poll_timestamp_seconds.",
        MetricType.GAUGE,
    ),
    (
        "market_data_latest_book_age_seconds",
        "For the newest verified book at each stage, response retrieval time minus exchange/book timestamp.",
        MetricType.GAUGE,
    ),
    (
        "pipeline_runs_total",
        "Terminal invocation outcomes from run audit events. A replayed completed slot is replayed, not another economic run.",
        MetricType.COUNTER,
    ),
    (
        "pipeline_terminal_results_total",
        "Unique accepted terminal observations.",
        MetricType.COUNTER,
    ),
    (
        "pipeline_stage_duration_seconds",
        "Monotonic elapsed duration for completed stages. Buckets: 0.001,0.01,0.05,0.1,0.25,0.5,1,2.5,5,10,30,60,300,+Inf.",
        MetricType.HISTOGRAM,
    ),
    (
        "pipeline_stage_outcomes_total",
        "Durable terminal stage outcomes.",
        MetricType.COUNTER,
    ),
    (
        "pipeline_last_success_timestamp_seconds",
        "UTC terminal time of the latest successful non-duplicated observation run; omitted before first success.",
        MetricType.GAUGE,
    ),
    (
        "pipeline_last_terminal_timestamp_seconds",
        "Latest terminal timestamp per run status.",
        MetricType.GAUGE,
    ),
    (
        "pipeline_pending_work_items",
        "Nonterminal durable intents at the source cut, including provider intents and started runs.",
        MetricType.GAUGE,
    ),
    (
        "pipeline_oldest_pending_age_seconds",
        "Generation time minus oldest pending intent time; omitted when none.",
        MetricType.GAUGE,
    ),
    (
        "pipeline_unresolved_observations",
        "Current count requiring resolution, cost recovery, or terminalization.",
        MetricType.GAUGE,
    ),
    (
        "pipeline_decision_barriers_total",
        "Validated DecisionFrozenArtifact.terminal_model_state count.",
        MetricType.COUNTER,
    ),
    (
        "pipeline_decision_barrier_age_seconds",
        "Age as of generation of the oldest decision barrier in that state.",
        MetricType.GAUGE,
    ),
    (
        "pipeline_terminal_ratio",
        "Terminal completed slots divided by scheduled/started slots represented in the audit trail; omitted with no denominator.",
        MetricType.GAUGE,
    ),
    (
        "reliability_restarts_total",
        "Startup count classified after journal scan/recovery.",
        MetricType.COUNTER,
    ),
    (
        "reliability_replay_attempts_total",
        "Explicit replay invocations and completed-slot replays. Economic observations remain deduplicated.",
        MetricType.COUNTER,
    ),
    (
        "reliability_recovery_actions_total",
        "Durable recovery actions such as tail truncation, reuse, terminalization, or cleanup.",
        MetricType.COUNTER,
    ),
    (
        "reliability_unknown_in_flight_total",
        "Provider intents terminalized as UNKNOWN_IN_FLIGHT.",
        MetricType.COUNTER,
    ),
    (
        "reliability_deduplications_total",
        "Duplicate-identical semantic work reused rather than appended/billed again.",
        MetricType.COUNTER,
    ),
    (
        "reliability_journal_corruption_total",
        "Detected journal damage. Recoverable partial_tail also counts as a recovery action.",
        MetricType.COUNTER,
    ),
    (
        "reliability_blob_corruption_total",
        "Missing, unsafe, or hash-invalid content-addressed artifacts.",
        MetricType.COUNTER,
    ),
    (
        "reliability_fsync_failures_total",
        "Failed fsync operations classified by the operation's bounded stage.",
        MetricType.COUNTER,
    ),
    (
        "reliability_write_failures_total",
        "Failed durable writes, including metric publication.",
        MetricType.COUNTER,
    ),
    (
        "reliability_journal_bytes",
        "Current validated journal byte size mapped to its logical stage.",
        MetricType.GAUGE,
    ),
    (
        "reliability_verified_blobs",
        "Blob verification count from the completed source scan. A corrupt scan is never published as a new economic snapshot; see Section 9.",
        MetricType.GAUGE,
    ),
    (
        "experiment_paired_samples_total",
        "Unique synchronized AI/control samples by current evaluability.",
        MetricType.COUNTER,
    ),
    (
        "experiment_paired_wins_total",
        "Resolved samples with known paired economic P&L, classified by sign.",
        MetricType.COUNTER,
    ),
    (
        "experiment_ai_win_rate",
        "AI wins / (AI wins + deterministic wins); ties excluded and separately counted. Omitted when no non-tie resolved pair exists.",
        MetricType.GAUGE,
    ),
    (
        "experiment_cumulative_pnl_usd",
        "Cumulative known economic P&L by arm. Omitted for an arm when included cost is unknown.",
        MetricType.GAUGE,
    ),
    (
        "experiment_cumulative_paired_pnl_delta_usd",
        "AI minus deterministic cumulative economic P&L over paired known samples.",
        MetricType.GAUGE,
    ),
    (
        "experiment_mean_brier_score",
        "Mean Brier score over scored forecasts.",
        MetricType.GAUGE,
    ),
    (
        "experiment_forecast_coverage_ratio",
        "Valid forecast probability count divided by registered eligible opportunity count.",
        MetricType.GAUGE,
    ),
    (
        "experiment_model_cost_usd_total",
        "Known cumulative model cost.",
        MetricType.COUNTER,
    ),
    (
        "experiment_model_cost_adjusted_edge_usd",
        "cumulative paired trading P&L delta - (AI model cost - deterministic model cost). In the current design deterministic model cost is zero. Omitted if either delta or required cost is unknown.",
        MetricType.GAUGE,
    ),
    (
        "experiment_model_cost_adjusted_edge_per_pair_usd",
        "Model-cost-adjusted edge divided by paired known-sample count; omitted with zero denominator.",
        MetricType.GAUGE,
    ),
    (
        "process_last_run_runtime_seconds",
        "Monotonic elapsed runtime of the latest terminal invocation with that status.",
        MetricType.GAUGE,
    ),
    (
        "process_resident_memory_bytes",
        "Resident set size sampled immediately before the latest terminal process audit event.",
        MetricType.GAUGE,
    ),
    (
        "process_peak_resident_memory_bytes",
        "Process peak RSS normalized to bytes for the latest invocation.",
        MetricType.GAUGE,
    ),
    (
        "process_cpu_seconds_total",
        "Cumulative audited CPU seconds across retained invocations. Per-run deltas are persisted so restart does not reset the reconstructed total.",
        MetricType.COUNTER,
    ),
    (
        "process_open_fds",
        "Open file descriptor count sampled at the latest terminal process event; Linux /proc/self/fd only. Omitted where unsupported.",
        MetricType.GAUGE,
    ),
    (
        "process_build_version",
        "Package SemVer encoded as major*1_000_000 + minor*1_000 + patch; current 0.1.0 emits 1000. Non-numeric prerelease/build details stay in audit records.",
        MetricType.GAUGE,
    ),
    (
        "process_build_commit_timestamp_seconds",
        "Commit author timestamp captured in validated startup identity; omitted when unavailable.",
        MetricType.GAUGE,
    ),
    (
        "process_build_dirty",
        "1 for a dirty source tree and 0 for clean, from startup identity.",
        MetricType.GAUGE,
    ),
    (
        "process_build_identity",
        "Exactly one series with value 1 for the latest audited runtime identity classification.",
        MetricType.GAUGE,
    ),
)


def _build_registry() -> tuple[MetricFamily, ...]:
    families = tuple(
        sorted(
            (
                MetricFamily(
                    name=_PREFIX + name,
                    help=help_text,
                    type=metric_type,
                    labels=_LABELS.get(name, ()),
                    label_values=_LABEL_VALUES.get(name, {}),
                    buckets=_BUCKETS.get(name, ()),
                )
                for name, help_text, metric_type in _CATALOG
            ),
            key=lambda family: family.name,
        )
    )
    names = tuple(family.name for family in families)
    if len(names) != 105 or len(set(names)) != len(names):
        raise RuntimeError("metric registry must contain 105 unique families")
    if names != tuple(sorted(names)):
        raise RuntimeError("metric registry must be lexically sorted")
    for family in families:
        if not _METRIC_NAME.fullmatch(family.name):
            raise RuntimeError(f"invalid metric name: {family.name}")
        if not family.help or "\n" in family.help:
            raise RuntimeError(f"invalid HELP text: {family.name}")
        if len(family.labels) != len(set(family.labels)):
            raise RuntimeError(f"duplicate labels: {family.name}")
        if set(family.labels) != set(family.label_values):
            raise RuntimeError(f"label allowlist mismatch: {family.name}")
        is_counter = family.type is MetricType.COUNTER
        if family.name.endswith("_total") is not is_counter:
            raise RuntimeError(f"counter suffix/type mismatch: {family.name}")
        if (family.type is MetricType.HISTOGRAM) is not bool(family.buckets):
            raise RuntimeError(f"histogram bucket/type mismatch: {family.name}")
        if any(not bucket.is_finite() for bucket in family.buckets):
            raise RuntimeError(f"histogram buckets must be finite: {family.name}")
        if any(bucket <= 0 for bucket in family.buckets):
            raise RuntimeError(f"histogram buckets must be positive: {family.name}")
        if any(
            current >= following
            for current, following in zip(family.buckets, family.buckets[1:])
        ):
            raise RuntimeError(
                f"histogram buckets must strictly increase: {family.name}"
            )
    return tuple(sorted(families, key=lambda family: family.name))


METRIC_FAMILIES = _build_registry()
_FAMILY_BY_NAME = MappingProxyType({family.name: family for family in METRIC_FAMILIES})


def metric_family(name: str) -> MetricFamily:
    """Return the registered family named by its full Prometheus name."""
    try:
        return _FAMILY_BY_NAME[name]
    except KeyError as error:
        raise ValueError(f"unknown metric family: {name}") from error


def validate_labels(
    family: MetricFamily, labels: Mapping[str, str]
) -> tuple[tuple[str, str], ...]:
    """Validate one complete bounded label set and return canonical ordering."""
    expected = set(family.labels)
    actual = set(labels)
    unexpected = actual - expected
    if unexpected:
        raise ValueError(f"unexpected labels for {family.name}: {sorted(unexpected)}")
    missing = expected - actual
    if missing:
        raise ValueError(f"missing labels for {family.name}: {sorted(missing)}")
    if "model_snapshot" in labels:
        pair = (labels["provider"], labels["model_snapshot"])
        if pair not in PROVIDER_MODEL_PAIRS:
            raise ValueError(f"invalid provider/model_snapshot pair: {pair!r}")
    if family.name == _PREFIX + "market_eligibility_total":
        pair = (labels["status"], labels["reason"])
        if (pair[0] == "eligible") is not (pair[1] == "none"):
            raise ValueError(f"invalid status/reason pair: {pair!r}")
    for label, value in labels.items():
        if value not in family.label_values[label]:
            raise ValueError(f"invalid {label} for {family.name}: {value!r}")
    return tuple(sorted(labels.items()))

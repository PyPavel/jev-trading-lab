"""Immutable, strict Phase-1 experiment manifest parsing and identity."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Annotated, Any, Literal, Self, get_args, get_origin

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from jev_trading_lab.core.ids import canonical_hash

MAX_MANIFEST_BYTES = 256 * 1024
_SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NonEmpty = Annotated[str, StringConstraints(min_length=1)]


class StrictModel(BaseModel):
    """Common closed, immutable, strict model contract."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    @classmethod
    def model_validate(
        cls,
        obj: Any,
        *,
        strict: bool | None = None,
        extra: Literal["allow", "ignore", "forbid"] | None = None,
        from_attributes: bool | None = None,
        context: Any | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        _check_json_primitives(obj)
        _check_model_input(cls, obj)
        return super().model_validate(
            obj,
            strict=True,
            extra="forbid",
            from_attributes=from_attributes,
            context=context,
            by_alias=False,
            by_name=True,
        )


def _check_json_primitives(item: object) -> None:
    if item is None or type(item) in {bool, int, str}:
        if type(item) is int and not -(2**63) <= item <= 2**63 - 1:
            raise ValueError("JSON integer must fit signed 64-bit range")
        return
    if type(item) is list:
        for child in item:
            _check_json_primitives(child)
        return
    if type(item) is dict:
        for key, child in item.items():
            if type(key) is not str:
                raise TypeError("JSON object keys must be exact built-in strings")
            _check_json_primitives(child)
        return
    raise ValueError("manifest accepts only exact built-in JSON primitives")


def _primitive_type(annotation: object) -> type[object] | None:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _primitive_type(get_args(annotation)[0])
    if origin is Literal:
        literal_values = get_args(annotation)
        return type(literal_values[0]) if literal_values else None
    if annotation is bool:
        return bool
    if annotation is int:
        return int
    if annotation is str:
        return str
    return None


def _check_model_input(model: type[BaseModel], value: object, path: str = "") -> None:
    if type(value) is not dict:
        return
    for name, item in value.items():
        field = model.model_fields.get(name)
        if field is None:
            continue
        location = f"{path}.{name}" if path else name
        expected = _primitive_type(field.annotation)
        if expected is not None and type(item) is not expected:
            raise ValueError(f"{location}: expected exact built-in {expected.__name__}")
        annotation = field.annotation
        nested = annotation if isinstance(annotation, type) else None
        if nested is not None and issubclass(nested, BaseModel):
            _check_model_input(nested, item, location)


def _tuple(value: object) -> object:
    return tuple(value) if type(value) is list else value


class RunContract(StrictModel):
    phase: Literal["phase1"]
    mode: Literal["paper"]
    example_only: bool


class CalendarContract(StrictModel):
    exchange: Literal["NYSE"]
    exchange_calendars_version: NonEmpty
    timezone: Literal["America/New_York"]
    enrollment_weeks: Literal[12]
    maturation_calendar_days: Literal[30]
    maturation_trading_days: Literal[10]
    tm_rule: Literal["tenth_nyse_session_strictly_after_e_plus_30_calendar_days"]
    tm_rule_version: NonEmpty


class CashYieldContract(StrictModel):
    series: Literal["DGS3MO"]
    source: Literal["FRED"]
    observation_rule: Literal["last_published_prior_observation"]
    first_retrieval_controls_availability: Literal[True]
    revision_backdating_allowed: Literal[False]


class AccountsContract(StrictModel):
    currency: Literal["USD"]
    initial_capital_microdollars: Literal[10_000_000_000]
    risk_unit_microdollars: Literal[50_000_000]
    polymarket_arms: tuple[Literal["AI", "DETERMINISTIC", "CASH"], ...]
    stock_arms: tuple[
        Literal["AI", "DETERMINISTIC", "CASH", "EXPOSURE_MATCHED_PASSIVE"], ...
    ]
    cash_yield: CashYieldContract

    @field_validator("polymarket_arms", mode="before")
    @classmethod
    def _poly_arms(cls, value: object) -> object:
        return _tuple(value)

    @field_validator("stock_arms", mode="before")
    @classmethod
    def _stock_arms(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _exact_arms(self) -> Self:
        if self.polymarket_arms != ("AI", "DETERMINISTIC", "CASH"):
            raise ValueError("polymarket_arms must contain the exact Phase-1 arms")
        if self.stock_arms != (
            "AI",
            "DETERMINISTIC",
            "CASH",
            "EXPOSURE_MATCHED_PASSIVE",
        ):
            raise ValueError("stock_arms must contain the exact Phase-1 arms")
        return self


class ModelContract(StrictModel):
    provider: NonEmpty
    endpoint: NonEmpty
    api_version: NonEmpty
    sdk_package: NonEmpty
    sdk_version: NonEmpty
    request_model_snapshot: NonEmpty
    expected_returned_model_id: NonEmpty
    expected_revision: NonEmpty
    expected_fingerprint: NonEmpty
    schema_sha256: _SHA256
    temperature_micros: Literal[0]
    top_p_micros: Literal[1_000_000]
    seed: int
    tools_enabled: Literal[False]
    fallback_enabled: Literal[False]

    @field_validator("request_model_snapshot", "expected_returned_model_id")
    @classmethod
    def _no_aliases(cls, value: str) -> str:
        if "latest" in value.casefold() or value.casefold() in {"default", "current"}:
            raise ValueError("model aliases are forbidden; pin an exact snapshot")
        return value

    @model_validator(mode="after")
    def _returned_model_matches_request(self) -> Self:
        if self.expected_returned_model_id != self.request_model_snapshot:
            raise ValueError(
                "expected_returned_model_id must equal request_model_snapshot"
            )
        return self


class ModelContracts(StrictModel):
    openai: ModelContract
    jev: ModelContract

    @model_validator(mode="after")
    def _phase1_providers(self) -> Self:
        expected = (
            (
                self.openai,
                "OpenAI",
                "https://api.openai.com/v1/responses",
                "v1",
            ),
            (
                self.jev,
                "TypeSafe",
                "https://api.typesafe.ai/v1/systemone",
                "v1",
            ),
        )
        for model, provider, endpoint, api_version in expected:
            if (model.provider, model.endpoint, model.api_version) != (
                provider,
                endpoint,
                api_version,
            ):
                raise ValueError(
                    f"{provider} provider, endpoint, and API version must be exact"
                )
        return self


class ProviderContract(StrictModel):
    source_name: NonEmpty
    endpoint: NonEmpty
    timestamp_semantics: NonEmpty
    precedence: Annotated[int, Field(ge=1)]
    validation: NonEmpty
    fallback_enabled: bool


class DataFallbackContract(StrictModel):
    source_name: NonEmpty
    endpoint: NonEmpty
    timestamp_semantics: NonEmpty
    precedence: Annotated[int, Field(ge=1)]
    validation: NonEmpty
    enabled: Literal[True]


class DataFallbackContracts(StrictModel):
    treasury_last_published_prior: DataFallbackContract


class ProviderContracts(StrictModel):
    polymarket_catalog: ProviderContract
    polymarket_books: ProviderContract
    polymarket_resolution: ProviderContract
    stock_ohlcv_actions: ProviderContract
    sp100_membership: ProviderContract
    treasury_cash_yield: ProviderContract
    sec_evidence: ProviderContract
    issuer_news: ProviderContract
    nyse_calendar: ProviderContract
    stock_spread_model: ProviderContract
    borrow_locate: ProviderContract
    data_fallbacks: DataFallbackContracts

    @model_validator(mode="after")
    def _fallback_and_borrow_policy(self) -> Self:
        expected = (
            (
                self.polymarket_catalog,
                "Polymarket Gamma",
                "https://gamma-api.polymarket.com/markets",
            ),
            (
                self.polymarket_books,
                "Polymarket CLOB",
                "https://clob.polymarket.com/book?token_id={token_id}",
            ),
            (
                self.polymarket_resolution,
                "Polymarket Gamma and stated resolution source",
                "https://gamma-api.polymarket.com/markets",
            ),
            (
                self.stock_ohlcv_actions,
                "Yahoo Finance chart v8",
                "https://query1.finance.yahoo.com/v8/finance/chart/"
                "{symbol}?interval=1d&events=div,splits",
            ),
            (
                self.sp100_membership,
                "Frozen Wikipedia S&P 100 artifact",
                "https://en.wikipedia.org/wiki/S%26P_100",
            ),
            (
                self.treasury_cash_yield,
                "FRED DGS3MO",
                "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS3MO",
            ),
            (
                self.sec_evidence,
                "SEC EDGAR",
                "https://data.sec.gov/submissions/CIK##########.json",
            ),
            (
                self.issuer_news,
                "Issuer RSS and Google News RSS discovery",
                "https://news.google.com/rss/search?q={query}",
            ),
            (
                self.nyse_calendar,
                "exchange-calendars",
                "python:exchange_calendars:XNYS",
            ),
            (
                self.stock_spread_model,
                "Frozen Phase-1 spread model",
                "artifact:stock_spread_model:v1",
            ),
            (self.borrow_locate, "none approved", "none"),
        )
        for provider, source_name, endpoint in expected:
            if (provider.source_name, provider.endpoint) != (source_name, endpoint):
                contract = f"source_name/endpoint: {source_name}"
                raise ValueError(f"provider contract must pin exact {contract}")
        providers = tuple(
            item[0] for item in expected if item[0] is not self.treasury_cash_yield
        )
        if any(provider.fallback_enabled for provider in providers):
            raise ValueError("unregistered data fallback is forbidden")
        if not self.treasury_cash_yield.fallback_enabled:
            raise ValueError("treasury prior-observation fallback must be registered")
        return self


class AiThresholds(StrictModel):
    sufficiency_probability_micros: Literal[700_000]
    ambiguity_probability_micros: Literal[300_000]
    action_probability_micros: Literal[550_000]
    runner_up_margin_probability_micros: Literal[150_000]


class PoolAllocation(StrictModel):
    polymarket_entry_micros: Annotated[int, Field(ge=0, le=1_000_000)]
    polymarket_review_micros: Annotated[int, Field(ge=0, le=1_000_000)]
    stock_entry_micros: Annotated[int, Field(ge=0, le=1_000_000)]
    stock_review_micros: Annotated[int, Field(ge=0, le=1_000_000)]
    priority_rule: Literal["scheduled_slot_then_semantic_subject_id"]

    @model_validator(mode="after")
    def _whole_pool(self) -> Self:
        if (
            sum(
                (
                    self.polymarket_entry_micros,
                    self.polymarket_review_micros,
                    self.stock_entry_micros,
                    self.stock_review_micros,
                )
            )
            != 1_000_000
        ):
            raise ValueError("pool allocation ratios must total 1,000,000 micros")
        return self


class Quotas(StrictModel):
    max_new_opportunities_per_cycle: Literal[3]
    max_new_opportunities_per_utc_day: Literal[20]
    max_concurrent_requests: Literal[3]
    max_attempts_per_stage: Literal[3]
    cycle_deadline_seconds: Literal[600]
    daily_ai_budget_microdollars: Literal[3_000_000]
    daily_provider_calls: Annotated[int, Field(gt=0)]
    daily_input_tokens: Annotated[int, Field(gt=0)]
    daily_output_tokens: Annotated[int, Field(gt=0)]
    daily_evidence_bytes: Annotated[int, Field(gt=0)]
    max_evidence_bytes_per_opportunity: Annotated[int, Field(gt=0)]
    retry_initial_backoff_milliseconds: Annotated[int, Field(gt=0)]
    retry_max_backoff_milliseconds: Annotated[int, Field(gt=0)]
    retry_jitter_micros: Annotated[int, Field(ge=0, le=1_000_000)]
    pool_allocation: PoolAllocation

    @model_validator(mode="after")
    def _quota_order(self) -> Self:
        if self.max_evidence_bytes_per_opportunity > self.daily_evidence_bytes:
            raise ValueError("per-opportunity evidence bytes exceed daily quota")
        if (
            self.retry_initial_backoff_milliseconds
            > self.retry_max_backoff_milliseconds
        ):
            raise ValueError("initial retry backoff exceeds maximum retry backoff")
        return self


class PolymarketContract(StrictModel):
    scan_interval_minutes: Literal[15]
    accepted_categories: tuple[str, ...]
    excluded_categories: tuple[Literal["sports"], ...]
    min_resolution_hours: Literal[1]
    max_resolution_days: Literal[30]
    min_trailing_24h_volume_microdollars: Literal[10_000_000_000]
    min_two_point_depth_contracts: Literal[5000]
    max_spread_probability_micros: Literal[40_000]
    cluster_algorithm: NonEmpty
    cluster_version: NonEmpty
    cluster_tie_breakers: tuple[str, ...]
    opportunity_rule: Literal["first_eligible_slot_per_cluster_no_reentry"]
    book_max_age_seconds: Literal[60]
    min_gross_edge_probability_micros: Literal[50_000]
    cost_edge_multiplier_micros: Literal[2_000_000]
    edge_reserve_probability_micros: Literal[30_000]
    risk_budget_equity_micros: Literal[5000]
    sigma_floor_microdollars_per_contract: Literal[50_000]
    max_cluster_cost_equity_micros: Literal[100_000]
    max_portfolio_gross_equity_micros: Literal[500_000]
    max_open_clusters: Literal[5]
    max_depth_participation_micros: Literal[50_000]
    max_volume_participation_micros: Literal[10_000]
    adverse_stop_probability_micros: Literal[150_000]
    favorable_target_probability_micros: Literal[100_000]
    max_holding_calendar_days: Literal[7]
    review_interval_minutes: Literal[15]
    depth_haircut_micros: Literal[500_000]
    quantity_beyond_depth_marks_zero: Literal[True]

    @field_validator(
        "accepted_categories",
        "excluded_categories",
        "cluster_tie_breakers",
        mode="before",
    )
    @classmethod
    def _tuples(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _categories(self) -> Self:
        if not self.accepted_categories or "sports" in self.accepted_categories:
            raise ValueError("accepted categories must be non-empty and non-sports")
        if self.excluded_categories != ("sports",):
            raise ValueError("sports must be the exact excluded category")
        if self.cluster_tie_breakers != (
            "trailing_24h_usd_volume_desc",
            "qualifying_depth_desc",
            "market_id_asc",
        ):
            raise ValueError("cluster tie-breakers must match Phase-1 order")
        return self


class StockContract(StrictModel):
    universe: Literal["S&P 100"]
    membership_artifact_sha256: _SHA256
    candidate_count: Literal[20]
    candidate_score_rule: Literal["abs_zscore_5d_return_plus_zscore_volume_surprise"]
    candidate_tie_breaker: Literal["ticker_ascending"]
    evidence_lookback_calendar_days: Literal[7]
    shorts_enabled: Literal[False]
    risk_budget_equity_micros: Literal[5000]
    stop_atr_multiplier_micros: Literal[2_000_000]
    target_atr_multiplier_micros: Literal[3_000_000]
    max_position_equity_micros: Literal[100_000]
    max_portfolio_gross_equity_micros: Literal[500_000]
    max_sector_gross_equity_micros: Literal[250_000]
    max_open_positions: Literal[5]
    entry_gap_limit_micros: Literal[100_000]
    half_spread_micros: Literal[500]
    adverse_slippage_micros: Literal[500]
    max_holding_trading_days: Literal[10]
    review_cadence: Literal["after_each_official_close"]
    common_exit_rules: tuple[str, ...]
    fill_rule: Literal["next_official_open_after_publication"]
    stop_target_collision_rule: Literal["stop_first"]
    gap_fill_rule: Literal["official_open"]

    @field_validator("common_exit_rules", mode="before")
    @classmethod
    def _exits(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _exact_exits(self) -> Self:
        if self.common_exit_rules != (
            "two_atr_stop",
            "three_atr_target",
            "ten_trading_days",
            "unmodelable_corporate_action",
            "invalid_pricing",
        ):
            raise ValueError("stock common exit rules must match Phase 1")
        return self


class CostContract(StrictModel):
    shared_ai_deterministic_split_micros: Literal[500_000]
    ai_direct_costs: tuple[str, ...]
    deterministic_direct_costs: tuple[str, ...]
    weekly_allocation_rule: Literal[
        "direct_to_opportunity_then_equal_within_utc_enrollment_week_residual_retained"
    ]
    uncertain_cost_stress_multipliers_micros: tuple[int, ...]
    joint_adverse_cost_multiplier_micros: Literal[1_500_000]
    joint_adverse_depth_haircut_micros: Literal[500_000]
    zero_bid_valuation_rule: Literal["quantity_beyond_executable_bid_depth_marks_zero"]
    statutory_fees_scale: Literal[False]
    cash_yield_scales: Literal[False]

    @field_validator(
        "ai_direct_costs",
        "deterministic_direct_costs",
        "uncertain_cost_stress_multipliers_micros",
        mode="before",
    )
    @classmethod
    def _tuples(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _stress_grid(self) -> Self:
        if self.uncertain_cost_stress_multipliers_micros != (
            500_000,
            1_000_000,
            1_500_000,
        ):
            raise ValueError("uncertain cost stress multipliers must be 0.5/1/1.5")
        return self


class BaselineContract(StrictModel):
    cash_series: Literal["DGS3MO"]
    stock_passive_oef_weight_micros: Literal[500_000]
    stock_passive_cash_weight_micros: Literal[500_000]
    stock_passive_rebalance: Literal["month_end_next_open_same_cost_rules"]
    polymarket_control_artifact_id: NonEmpty
    polymarket_control_artifact_sha256: _SHA256
    stock_control_artifact_id: NonEmpty
    stock_control_artifact_sha256: _SHA256
    stock_prevalence_artifact_sha256: _SHA256
    polymarket_forecast_baseline: Literal["common_t3_executable_midpoint"]
    stock_forecast_baseline: Literal["frozen_pre_run_candidate_prevalence"]


class EndpointContract(StrictModel):
    tm_rule: Literal["tenth_nyse_session_strictly_after_e_plus_30_calendar_days"]
    daily_valuation_time_utc: Literal["21:00:00"]
    no_entry_after_enrollment_end: Literal[True]
    classification_uses_fixed_tm: Literal[True]
    later_settlements_are_supplemental: Literal[True]


class StatisticsContract(StrictModel):
    risk_unit_microdollars: Literal[50_000_000]
    theta_null_probability_micros: Literal[30_000]
    theta_alt_probability_micros: Literal[60_000]
    bootstrap_replicates: Literal[99_999]
    bootstrap_seed: int
    expected_block_length_policy: Literal["pre_run_simulation_only"]
    expected_block_length_artifact_sha256: _SHA256
    holm_first_threshold_probability_micros: Literal[25_000]
    holm_second_threshold_probability_micros: Literal[50_000]
    confirmatory_min_weeks: Literal[26]
    confirmatory_min_resolved_polymarket_clusters: Literal[100]
    confirmatory_power_probability_micros: Literal[800_000]
    terminal_slot_threshold_probability_micros: Literal[990_000]
    max_drawdown_relative_tolerance_probability_micros: Literal[20_000]
    max_drawdown_absolute_probability_micros: Literal[100_000]
    weekly_loss_floor_probability_micros: Literal[-10_000]
    count_retention_probability_micros: Literal[600_000]
    dollar_day_retention_probability_micros: Literal[600_000]
    classification_precedence: tuple[str, ...]
    simultaneous_lower_bound_order_statistic: Literal[97_500]
    max_total_bootstrap_draws: Literal[999_990]

    @field_validator("classification_precedence", mode="before")
    @classmethod
    def _precedence_tuple(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _classification_order(self) -> Self:
        if self.classification_precedence != (
            "OPERATIONALLY_INFEASIBLE",
            "INCONCLUSIVE",
            "PROMISING",
            "NO_EVIDENCE",
        ):
            raise ValueError("classification precedence must match Phase 1")
        return self


class RuntimeContract(StrictModel):
    writer_processes: Literal[1]
    sqlite_synchronous: Literal["FULL"]
    backup_rpo_hours: Literal[24]
    restore_rto_hours: Literal[2]
    restore_drill_cadence: Literal["before_registration_and_monthly"]
    polymarket_review_cadence_minutes: Literal[15]
    stock_review_cadence: Literal["after_each_official_close"]


class Manifest(StrictModel):
    schema_version: Literal[1]
    run: RunContract
    calendar: CalendarContract
    accounts: AccountsContract
    models: ModelContracts
    providers: ProviderContracts
    ai_thresholds: AiThresholds
    quotas: Quotas
    polymarket: PolymarketContract
    stocks: StockContract
    costs: CostContract
    baselines: BaselineContract
    endpoint: EndpointContract
    statistics: StatisticsContract
    runtime: RuntimeContract

    @property
    def manifest_hash(self) -> str:
        """Hash the complete normalized model with the manifest namespace."""
        return canonical_hash("manifest", self.model_dump(mode="json"))

    @property
    def manifest_id(self) -> str:
        """Return a visibly typed stable manifest identity."""
        return f"manifest:{self.manifest_hash}"


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(token: str) -> None:
    raise ValueError(f"finite JSON numbers required; received {token}")


def _format_validation_error(error: ValidationError) -> ValueError:
    messages = []
    errors = error.errors(include_url=False, include_context=False, include_input=False)
    for item in errors:
        location = ".".join(str(part) for part in item["loc"]) or "<root>"
        messages.append(f"{location}: {item['msg']}")
    return ValueError("manifest validation failed:\n" + "\n".join(messages))


def parse_manifest_bytes(data: bytes) -> Manifest:
    """Parse strict UTF-8 JSON bytes into a frozen validated manifest."""
    if type(data) is not bytes:
        raise TypeError("manifest data must be exact built-in bytes")
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("manifest must be valid UTF-8") from error
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonfinite,
        )
    except json.JSONDecodeError as error:
        location = f"line {error.lineno} column {error.colno}"
        raise ValueError(
            f"manifest is invalid JSON at {location}: {error.msg}"
        ) from error
    if type(value) is not dict:
        raise ValueError("manifest JSON root must be an object")
    _check_json_primitives(value)
    try:
        return Manifest.model_validate(value)
    except ValidationError as error:
        raise _format_validation_error(error) from error


def load_manifest(path: str | os.PathLike[str]) -> Manifest:
    """Read and parse one regular, readable manifest file."""
    manifest_path = Path(path)
    try:
        metadata = manifest_path.stat()
    except OSError as error:
        raise ValueError("manifest path must be a regular file") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("manifest path must be a regular file")
    if not metadata.st_mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH):
        raise ValueError("manifest file is not readable")
    if metadata.st_size > MAX_MANIFEST_BYTES:
        raise ValueError(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes")
    try:
        return parse_manifest_bytes(manifest_path.read_bytes())
    except OSError as error:
        raise ValueError("manifest file is not readable") from error

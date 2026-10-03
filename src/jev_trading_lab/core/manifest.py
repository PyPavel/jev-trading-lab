"""Parser-only trust boundary for strict, immutable Phase-1 manifests.

Future registration code must accept canonical bytes or call ``revalidate_manifest``.
Never authorize a run from fields on an arbitrary ``Manifest`` object.
"""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import date
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

from jev_trading_lab.core.ids import canonical_hash, canonical_json_bytes

MAX_MANIFEST_BYTES = 256 * 1024
MAX_JSON_DEPTH = 100
MAX_CONTAINER_ITEMS = 10_000
MAX_STRING_LENGTH = 32_768
MAX_VALIDATION_ERRORS = 20
_SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_NonEmpty = Annotated[str, StringConstraints(min_length=1)]
_CREATION_SENTINEL = object()
_UNRESOLVED_IDENTITY_MARKERS = frozenset(
    {
        "changeme",
        "unknown",
        "dummy",
        "fake",
        "notset",
        "unset",
        "replaceme",
        "xxx",
        "pending",
        "sample",
        "testonly",
    }
)


def _tuple(value: object) -> object:
    return tuple(value) if type(value) is list else value


class _StrictModel(BaseModel):
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
        _check_json_domain(obj)
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


def _check_json_domain(root: object) -> None:
    pending: list[tuple[object, int]] = [(root, 1)]
    item_count = 0
    while pending:
        item, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            raise ValueError(f"manifest JSON nesting depth exceeds {MAX_JSON_DEPTH}")
        if item is None or type(item) in {bool, int, str}:
            if type(item) is int and not -(2**63) <= item <= 2**63 - 1:
                raise ValueError("JSON integer must fit signed 64-bit range")
            if type(item) is str and len(item) > MAX_STRING_LENGTH:
                raise ValueError(
                    f"manifest JSON string exceeds {MAX_STRING_LENGTH} characters"
                )
            continue
        if type(item) is list:
            item_count += len(item)
            if item_count > MAX_CONTAINER_ITEMS:
                raise ValueError(
                    f"manifest JSON container items exceed {MAX_CONTAINER_ITEMS}"
                )
            pending.extend((child, depth + 1) for child in item)
            continue
        if type(item) is dict:
            item_count += len(item)
            if item_count > MAX_CONTAINER_ITEMS:
                raise ValueError(
                    f"manifest JSON container items exceed {MAX_CONTAINER_ITEMS}"
                )
            for key, child in item.items():
                if type(key) is not str:
                    raise TypeError("JSON object keys must be exact built-in strings")
                if len(key) > MAX_STRING_LENGTH:
                    raise ValueError(
                        f"manifest JSON string exceeds {MAX_STRING_LENGTH} characters"
                    )
                pending.append((child, depth + 1))
            continue
        raise ValueError("manifest accepts only exact built-in JSON primitives")


def _primitive_type(annotation: object) -> type[object] | None:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _primitive_type(get_args(annotation)[0])
    if origin is Literal:
        values = get_args(annotation)
        return type(values[0]) if values else None
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
        nested = field.annotation if isinstance(field.annotation, type) else None
        if nested is not None and issubclass(nested, BaseModel):
            _check_model_input(nested, item, location)


class _RunContract(_StrictModel):
    phase: Literal["phase1"]
    mode: Literal["paper"]
    example_only: bool


class _CalendarContract(_StrictModel):
    exchange: Literal["NYSE"]
    exchange_calendars_version: _NonEmpty
    package_sha256: _SHA256
    content_sha256: _SHA256
    timezone: Literal["America/New_York"]
    enrollment_weeks: Literal[12]
    maturation_calendar_days: Literal[30]
    maturation_trading_days: Literal[10]
    tm_rule: Literal["tenth_nyse_session_strictly_after_e_plus_30_calendar_days"]
    tm_rule_version: _NonEmpty
    tm_rule_sha256: _SHA256


class _CashYieldContract(_StrictModel):
    series: Literal["DGS3MO"]
    source: Literal["FRED"]
    observation_rule: Literal["last_published_prior_observation"]
    rule_version: Literal["phase1-cash-yield-v1"]
    rule_sha256: _SHA256
    first_retrieval_controls_availability: Literal[True]
    revision_backdating_allowed: Literal[False]


class _AccountsContract(_StrictModel):
    currency: Literal["USD"]
    initial_capital_microdollars: Literal[10_000_000_000]
    risk_unit_microdollars: Literal[50_000_000]
    polymarket_arms: tuple[Literal["AI", "DETERMINISTIC", "CASH"], ...]
    stock_arms: tuple[
        Literal["AI", "DETERMINISTIC", "CASH", "EXPOSURE_MATCHED_PASSIVE"], ...
    ]
    cash_yield: _CashYieldContract

    @field_validator("polymarket_arms", "stock_arms", mode="before")
    @classmethod
    def _tuples(cls, value: object) -> object:
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


class _ModelContract(_StrictModel):
    provider: Literal["OpenAI", "TypeSafe"]
    endpoint: Literal[
        "https://api.openai.com/v1/responses",
        "https://api.typesafe.ai/v1/systemone",
    ]
    api_version: Literal["v1"]
    sdk_package: _NonEmpty
    sdk_version: _NonEmpty
    immutable_snapshot: Literal[True]
    request_model_snapshot: _NonEmpty
    expected_returned_model_id: _NonEmpty
    expected_revision: _NonEmpty
    expected_fingerprint: _NonEmpty
    schema_version: _NonEmpty
    schema_sha256: _SHA256
    temperature_micros: Literal[0]
    top_p_micros: Literal[1_000_000]
    seed: int
    tools_enabled: Literal[False]
    fallback_enabled: Literal[False]

    @model_validator(mode="after")
    def _returned_model_matches_request(self) -> Self:
        if self.expected_returned_model_id != self.request_model_snapshot:
            raise ValueError(
                "expected_returned_model_id must equal request_model_snapshot"
            )
        return self


class _ModelInputPolicyContract(_StrictModel):
    openai_prompt_template_version: _NonEmpty
    openai_prompt_template_sha256: _SHA256
    openai_schema_version: _NonEmpty
    openai_schema_sha256: _SHA256
    evidence_delimiter_version: _NonEmpty
    evidence_delimiter_sha256: _SHA256
    jev_state_template_version: _NonEmpty
    jev_state_template_sha256: _SHA256
    jev_question_set_version: _NonEmpty
    jev_question_set_sha256: _SHA256


class _ModelContracts(_StrictModel):
    openai: _ModelContract
    jev: _ModelContract

    @model_validator(mode="after")
    def _phase1_providers(self) -> Self:
        expected = (
            (
                self.openai,
                "OpenAI",
                "https://api.openai.com/v1/responses",
                r"^gpt-[a-z0-9]+(?:-[a-z0-9]+)*-\d{4}-\d{2}-\d{2}$",
            ),
            (
                self.jev,
                "TypeSafe",
                "https://api.typesafe.ai/v1/systemone",
                r"^jev-[a-z0-9]+(?:-[a-z0-9]+)*-\d{4}-\d{2}-\d{2}$",
            ),
        )
        for model, provider, endpoint, snapshot_pattern in expected:
            if (model.provider, model.endpoint) != (provider, endpoint):
                raise ValueError(f"{provider} provider and endpoint must be exact")
            snapshot = model.request_model_snapshot
            match = re.fullmatch(snapshot_pattern, snapshot)
            if match is None:
                raise ValueError(f"{provider} model must be a dated immutable snapshot")
            components = set(re.split(r"[^a-z0-9]+", snapshot.casefold()))
            if components & {"latest", "current", "default"}:
                raise ValueError(f"{provider} model snapshot contains a reserved alias")
            try:
                date.fromisoformat(snapshot[-10:])
            except ValueError as error:
                raise ValueError(
                    f"{provider} model snapshot date must be a real calendar date"
                ) from error
        return self


_SourceName = Literal[
    "Polymarket Gamma",
    "Polymarket CLOB",
    "Polymarket Gamma and stated resolution source",
    "Yahoo Finance chart v8",
    "Frozen Wikipedia S&P 100 artifact",
    "FRED DGS3MO",
    "SEC EDGAR",
    "Issuer RSS and Google News RSS discovery",
    "exchange-calendars",
    "Frozen Phase-1 spread model",
    "none approved",
]
_Endpoint = Literal[
    "https://gamma-api.polymarket.com/markets",
    "https://clob.polymarket.com/book?token_id={token_id}",
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&events=div,splits",
    "https://en.wikipedia.org/wiki/S%26P_100",
    "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS3MO",
    "https://data.sec.gov/submissions/CIK##########.json",
    "https://news.google.com/rss/search?q={query}",
    "python:exchange_calendars:XNYS",
    "artifact:stock_spread_model:v1",
    "none",
]
_TimestampSemantics = Literal[
    "first_successful_retrieval_utc",
    "server_and_first_successful_client_retrieval_utc",
    "official_resolution_and_first_successful_retrieval_utc",
    "exchange_date_and_first_successful_retrieval_utc",
    "frozen_first_successful_retrieval_utc",
    "observation_date_and_first_successful_retrieval_utc",
    "accepted_timestamp_and_first_successful_retrieval_utc",
    "original_publication_and_first_successful_retrieval_utc",
    "pinned_package_session_schedule",
    "registration_time_artifact",
    "not_applicable",
]
_ValidationRule = Literal[
    "phase1_gamma_market_schema_v1",
    "phase1_clob_book_schema_v1",
    "phase1_resolution_schema_v1",
    "phase1_yahoo_chart_schema_v1",
    "phase1_sp100_table_schema_v1",
    "phase1_fred_dgs3mo_schema_v1",
    "phase1_sec_evidence_schema_v1",
    "phase1_original_source_retrieval_v1",
    "phase1_xnys_calendar_v1",
    "half_spread_5bps_slippage_5bps",
    "shorts_disabled",
]


class _ProviderContract(_StrictModel):
    source_name: _SourceName
    endpoint: _Endpoint
    timestamp_semantics: _TimestampSemantics
    precedence: Literal[1, 2]
    validation: _ValidationRule
    contract_version: _NonEmpty
    contract_sha256: _SHA256
    fallback_enabled: bool


class _SecProviderContract(_ProviderContract):
    endpoints: tuple[str, ...]

    @field_validator("endpoints", mode="before")
    @classmethod
    def _endpoints_tuple(cls, value: object) -> object:
        return _tuple(value)


class _IssuerNewsProviderContract(_ProviderContract):
    allowed_source_classes: tuple[str, ...]
    allowed_domains: tuple[str, ...]
    allowlist_version: _NonEmpty
    allowlist_sha256: _SHA256

    @field_validator("allowed_source_classes", "allowed_domains", mode="before")
    @classmethod
    def _tuples(cls, value: object) -> object:
        return _tuple(value)


class _DataFallbackContract(_StrictModel):
    source_name: Literal["FRED DGS3MO prior observation"]
    endpoint: Literal["https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS3MO"]
    timestamp_semantics: Literal[
        "latest_published_observation_strictly_prior_to_valuation"
    ]
    precedence: Literal[2]
    validation: Literal["manifest_freshness_and_first_retrieval_v1"]
    mapping: tuple[
        Literal[
            "observation_date_to_observation_date",
            "value_to_yield_percent",
            "first_retrieved_at_to_available_at",
        ],
        ...,
    ]
    rule_version: _NonEmpty
    rule_sha256: _SHA256
    enabled: Literal[True]

    @field_validator("mapping", mode="before")
    @classmethod
    def _mapping_tuple(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _exact_mapping(self) -> Self:
        if self.mapping != (
            "observation_date_to_observation_date",
            "value_to_yield_percent",
            "first_retrieved_at_to_available_at",
        ):
            raise ValueError("Treasury fallback mapping must match Phase 1")
        return self


class _DataFallbackContracts(_StrictModel):
    treasury_last_published_prior: _DataFallbackContract


class _ProviderContracts(_StrictModel):
    polymarket_catalog: _ProviderContract
    polymarket_books: _ProviderContract
    polymarket_resolution: _ProviderContract
    stock_ohlcv_actions: _ProviderContract
    sp100_membership: _ProviderContract
    treasury_cash_yield: _ProviderContract
    sec_evidence: _SecProviderContract
    issuer_news: _IssuerNewsProviderContract
    nyse_calendar: _ProviderContract
    stock_spread_model: _ProviderContract
    borrow_locate: _ProviderContract
    data_fallbacks: _DataFallbackContracts

    @model_validator(mode="after")
    def _exact_provider_contracts(self) -> Self:
        expected = (
            (
                self.polymarket_catalog,
                "Polymarket Gamma",
                "https://gamma-api.polymarket.com/markets",
                "first_successful_retrieval_utc",
                1,
                "phase1_gamma_market_schema_v1",
                False,
            ),
            (
                self.polymarket_books,
                "Polymarket CLOB",
                "https://clob.polymarket.com/book?token_id={token_id}",
                "server_and_first_successful_client_retrieval_utc",
                1,
                "phase1_clob_book_schema_v1",
                False,
            ),
            (
                self.polymarket_resolution,
                "Polymarket Gamma and stated resolution source",
                "https://gamma-api.polymarket.com/markets",
                "official_resolution_and_first_successful_retrieval_utc",
                1,
                "phase1_resolution_schema_v1",
                False,
            ),
            (
                self.stock_ohlcv_actions,
                "Yahoo Finance chart v8",
                "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&events=div,splits",
                "exchange_date_and_first_successful_retrieval_utc",
                1,
                "phase1_yahoo_chart_schema_v1",
                False,
            ),
            (
                self.sp100_membership,
                "Frozen Wikipedia S&P 100 artifact",
                "https://en.wikipedia.org/wiki/S%26P_100",
                "frozen_first_successful_retrieval_utc",
                1,
                "phase1_sp100_table_schema_v1",
                False,
            ),
            (
                self.treasury_cash_yield,
                "FRED DGS3MO",
                "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS3MO",
                "observation_date_and_first_successful_retrieval_utc",
                1,
                "phase1_fred_dgs3mo_schema_v1",
                True,
            ),
            (
                self.sec_evidence,
                "SEC EDGAR",
                "https://data.sec.gov/submissions/CIK##########.json",
                "accepted_timestamp_and_first_successful_retrieval_utc",
                1,
                "phase1_sec_evidence_schema_v1",
                False,
            ),
            (
                self.issuer_news,
                "Issuer RSS and Google News RSS discovery",
                "https://news.google.com/rss/search?q={query}",
                "original_publication_and_first_successful_retrieval_utc",
                2,
                "phase1_original_source_retrieval_v1",
                False,
            ),
            (
                self.nyse_calendar,
                "exchange-calendars",
                "python:exchange_calendars:XNYS",
                "pinned_package_session_schedule",
                1,
                "phase1_xnys_calendar_v1",
                False,
            ),
            (
                self.stock_spread_model,
                "Frozen Phase-1 spread model",
                "artifact:stock_spread_model:v1",
                "registration_time_artifact",
                1,
                "half_spread_5bps_slippage_5bps",
                False,
            ),
            (
                self.borrow_locate,
                "none approved",
                "none",
                "not_applicable",
                1,
                "shorts_disabled",
                False,
            ),
        )
        for contract in expected:
            provider, *values = contract
            actual = [
                provider.source_name,
                provider.endpoint,
                provider.timestamp_semantics,
                provider.precedence,
                provider.validation,
                provider.fallback_enabled,
            ]
            if actual != values:
                raise ValueError(
                    "provider source/endpoint/timestamp/precedence/validation/"
                    "fallback contract must be exact"
                )
        if self.sec_evidence.endpoints != (
            "https://data.sec.gov/submissions/CIK##########.json",
            "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/{primary_document}",
            "https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json",
        ):
            raise ValueError(
                "SEC endpoints must cover submissions, archives, and companyfacts"
            )
        if self.issuer_news.allowed_source_classes != (
            "issuer_investor_relations_rss_atom",
            "google_news_rss_discovery_only",
            "original_publisher_https",
        ):
            raise ValueError("issuer evidence source classes must match Phase 1")
        if not self.issuer_news.allowed_domains or len(
            set(self.issuer_news.allowed_domains)
        ) != len(self.issuer_news.allowed_domains):
            raise ValueError("issuer evidence domains must be a non-empty unique tuple")
        if any(
            domain != domain.casefold() for domain in self.issuer_news.allowed_domains
        ):
            raise ValueError("issuer evidence domains must be lowercase")
        return self


class _AiThresholds(_StrictModel):
    rule_version: Literal["phase1-ai-thresholds-v1"]
    rule_sha256: _SHA256
    sufficiency_probability_micros: Literal[700_000]
    ambiguity_probability_micros: Literal[300_000]
    action_probability_micros: Literal[550_000]
    runner_up_margin_probability_micros: Literal[150_000]


class _PoolAllocation(_StrictModel):
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


class _Quotas(_StrictModel):
    rule_version: Literal["phase1-quota-policy-v1"]
    rule_sha256: _SHA256
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
    retry_jitter_seed: Literal[20261002]
    retry_jitter_policy: Literal["seeded_uniform_plus_or_minus_fraction_of_backoff"]
    pool_allocation: _PoolAllocation

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


class _PolymarketContract(_StrictModel):
    policy_version: Literal["phase1-polymarket-policy-v1"]
    policy_sha256: _SHA256
    scan_interval_minutes: Literal[15]
    accepted_categories: tuple[str, ...]
    excluded_categories: tuple[Literal["sports"], ...]
    min_resolution_hours: Literal[1]
    max_resolution_days: Literal[30]
    min_trailing_24h_volume_microdollars: Literal[10_000_000_000]
    min_two_point_depth_contracts: Literal[5000]
    max_spread_probability_micros: Literal[40_000]
    cluster_metadata_fields: tuple[str, ...]
    cluster_algorithm: Literal["normalized_resolution_source_question_v1"]
    cluster_version: Literal["phase1-v1"]
    cluster_normalization_version: Literal["phase1-cluster-normalization-v1"]
    cluster_rule_sha256: _SHA256
    cluster_tie_breakers: tuple[str, ...]
    opportunity_rule: Literal["first_eligible_slot_per_cluster_no_reentry"]
    book_max_age_seconds: Literal[60]
    min_gross_edge_probability_micros: Literal[50_000]
    cost_edge_multiplier_micros: Literal[2_000_000]
    edge_reserve_probability_micros: Literal[30_000]
    risk_budget_equity_micros: Literal[5000]
    sigma_sampling_interval_minutes: Literal[15]
    sigma_lookback_calendar_days: Literal[7]
    sigma_missing_snapshot_rule: Literal[
        "omit_missing_slots_no_forward_fill_minimum_48_observations"
    ]
    sigma_estimator_version: Literal["phase1-sigma-v1"]
    sigma_rule_sha256: _SHA256
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
        "cluster_metadata_fields",
        "cluster_tie_breakers",
        mode="before",
    )
    @classmethod
    def _tuples(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _exact_contract(self) -> Self:
        if not self.accepted_categories or any(
            not value or value != value.casefold() for value in self.accepted_categories
        ):
            raise ValueError("accepted categories must be non-empty lowercase strings")
        if len(set(self.accepted_categories)) != len(self.accepted_categories):
            raise ValueError("accepted categories must be unique")
        if any(value.casefold() == "sports" for value in self.accepted_categories):
            raise ValueError("sports is forbidden in accepted categories")
        if self.excluded_categories != ("sports",):
            raise ValueError("sports must be the exact excluded category")
        if self.cluster_metadata_fields != (
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
        ):
            raise ValueError("cluster metadata fields must match Phase 1")
        if self.cluster_tie_breakers != (
            "trailing_24h_usd_volume_desc",
            "qualifying_depth_desc",
            "market_id_asc",
        ):
            raise ValueError("cluster tie-breakers must match Phase-1 order")
        return self


class _StockContract(_StrictModel):
    policy_version: Literal["phase1-stock-policy-v1"]
    policy_sha256: _SHA256
    universe: Literal["S&P 100"]
    membership_artifact_version: _NonEmpty
    membership_artifact_sha256: _SHA256
    candidate_count: Literal[20]
    candidate_score_rule: Literal["abs_zscore_5d_return_plus_zscore_volume_surprise"]
    candidate_rule_version: _NonEmpty
    candidate_rule_sha256: _SHA256
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


class _CostContract(_StrictModel):
    rule_version: Literal["phase1-cost-allocation-v1"]
    rule_sha256: _SHA256
    shared_ai_deterministic_split_micros: Literal[500_000]
    ai_direct_costs: tuple[str, ...]
    deterministic_direct_costs: tuple[str, ...]
    truly_shared_costs: tuple[str, ...]
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
        "truly_shared_costs",
        "uncertain_cost_stress_multipliers_micros",
        mode="before",
    )
    @classmethod
    def _tuples(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _exact_costs(self) -> Self:
        if self.ai_direct_costs != (
            "openai",
            "jev",
            "evidence_retrieval",
            "paid_evidence_data",
            "incremental_ai_infrastructure",
        ):
            raise ValueError("AI direct costs must match Phase 1")
        if self.deterministic_direct_costs != ("deterministic_specific",):
            raise ValueError("deterministic direct costs must match Phase 1")
        if self.truly_shared_costs != (
            "market_data",
            "scheduler",
            "storage",
            "reporting",
        ):
            raise ValueError("truly shared costs must match Phase 1")
        if self.uncertain_cost_stress_multipliers_micros != (
            500_000,
            1_000_000,
            1_500_000,
        ):
            raise ValueError("uncertain cost stress multipliers must be 0.5/1/1.5")
        return self


class _BaselineContract(_StrictModel):
    cash_series: Literal["DGS3MO"]
    stock_passive_oef_ticker: Literal["OEF"]
    stock_passive_oef_instrument: Literal["iShares S&P 100 ETF total return"]
    stock_passive_oef_source_contract: Literal[
        "yahoo_chart_v8_adjusted_for_explicit_actions"
    ]
    stock_passive_oef_weight_micros: Literal[500_000]
    stock_passive_cash_weight_micros: Literal[500_000]
    stock_passive_rebalance: Literal["month_end_next_open_same_cost_rules"]
    polymarket_control_artifact_id: _NonEmpty
    polymarket_control_artifact_version: _NonEmpty
    polymarket_control_artifact_sha256: _SHA256
    stock_control_artifact_id: _NonEmpty
    stock_control_artifact_version: _NonEmpty
    stock_control_artifact_sha256: _SHA256
    stock_prevalence_artifact_version: _NonEmpty
    stock_prevalence_artifact_sha256: _SHA256
    polymarket_forecast_baseline: Literal["common_t3_executable_midpoint"]
    stock_forecast_baseline: Literal["frozen_pre_run_candidate_prevalence"]


class _EndpointContract(_StrictModel):
    rule_version: Literal["phase1-endpoint-v1"]
    rule_sha256: _SHA256
    tm_rule: Literal["tenth_nyse_session_strictly_after_e_plus_30_calendar_days"]
    daily_valuation_time_utc: Literal["21:00:00"]
    no_entry_after_enrollment_end: Literal[True]
    classification_uses_fixed_tm: Literal[True]
    later_settlements_are_supplemental: Literal[True]


class _StatisticsContract(_StrictModel):
    rule_version: Literal["phase1-statistics-v1"]
    rule_sha256: _SHA256
    risk_unit_microdollars: Literal[50_000_000]
    theta_null_probability_micros: Literal[30_000]
    theta_alt_probability_micros: Literal[60_000]
    bootstrap_replicates: Literal[99_999]
    bootstrap_seed: int
    expected_block_length_policy: Literal["pre_run_simulation_only"]
    expected_block_length_artifact_version: _NonEmpty
    expected_block_length_artifact_sha256: _SHA256
    holm_first_threshold_probability_micros: Literal[25_000]
    holm_second_threshold_probability_micros: Literal[50_000]
    confirmatory_min_weeks: Literal[26]
    confirmatory_min_resolved_polymarket_clusters: Literal[100]
    confirmatory_power_probability_micros: Literal[800_000]
    confirmatory_duration_rule: Literal["max_26_weeks_and_b_min"]
    b_min_weeks: Annotated[int, Field(ge=26)]
    power_input_fields: tuple[str, ...]
    alternative_theta_probability_micros: tuple[int, int]
    power_simulation_version: _NonEmpty
    power_simulation_sha256: _SHA256
    terminal_slot_threshold_probability_micros: Literal[990_000]
    max_drawdown_relative_tolerance_probability_micros: Literal[20_000]
    max_drawdown_absolute_probability_micros: Literal[100_000]
    weekly_loss_floor_probability_micros: Literal[-10_000]
    count_retention_probability_micros: Literal[600_000]
    dollar_day_retention_probability_micros: Literal[600_000]
    positive_cumulative_incremental_economic_pnl_required: Literal[True]
    polymarket_cash_return_gate_required: Literal[True]
    stock_cash_return_gate_required: Literal[True]
    stock_passive_return_gate_required: Literal[True]
    base_and_joint_adverse_must_pass_and_match: Literal[True]
    zero_deterministic_retention_denominator_result: Literal["INCONCLUSIVE"]
    numeric_information_requirements: tuple[str, ...]
    missing_probability_rule: Literal["substitute_frozen_baseline"]
    ece_bins: Literal[10]
    ece_bin_rule: Literal["equal_width"]
    missing_label_bound_rule: Literal[
        "joint_paired_score_difference_admissible_endpoint_bounds"
    ]
    classification_precedence: tuple[str, ...]
    simultaneous_lower_bound_order_statistic: Literal[97_500]
    max_total_bootstrap_draws: Literal[999_990]

    @field_validator(
        "power_input_fields",
        "alternative_theta_probability_micros",
        "numeric_information_requirements",
        "classification_precedence",
        mode="before",
    )
    @classmethod
    def _tuples(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _exact_statistics(self) -> Self:
        if self.power_input_fields != (
            "paired_variance",
            "cross_stream_dependence",
            "serial_dependence",
            "abstention",
            "missingness",
        ):
            raise ValueError("power inputs must match Phase 1")
        if self.alternative_theta_probability_micros != (60_000, 60_000):
            raise ValueError("alternative vector must match Phase 1")
        if self.numeric_information_requirements != (
            "complete_enrollment_and_fixed_tm_analysis",
            "finite_theta_for_both_streams",
            "valid_base_and_joint_adverse_replays",
            "nonzero_deterministic_count_retention_denominator",
            "nonzero_deterministic_dollar_day_retention_denominator",
            "99999_valid_bootstrap_replicates_within_999990_draws",
            "power_simulation_artifact_with_b_min",
        ):
            raise ValueError("numeric information requirements must match Phase 1")
        if self.classification_precedence != (
            "OPERATIONALLY_INFEASIBLE",
            "INCONCLUSIVE",
            "PROMISING",
            "NO_EVIDENCE",
        ):
            raise ValueError("classification precedence must match Phase 1")
        return self


class _RuntimeContract(_StrictModel):
    writer_processes: Literal[1]
    sqlite_synchronous: Literal["FULL"]
    sqlite_busy_timeout_ms: Literal[5000]
    backup_rpo_hours: Literal[24]
    restore_rto_hours: Literal[2]
    restore_drill_cadence: Literal["before_registration_and_monthly"]
    polymarket_review_cadence_minutes: Literal[15]
    stock_review_cadence: Literal["after_each_official_close"]


class _ManifestModel(_StrictModel):
    schema_version: Literal[1]
    run: _RunContract
    calendar: _CalendarContract
    accounts: _AccountsContract
    models: _ModelContracts
    model_input_policy: _ModelInputPolicyContract
    providers: _ProviderContracts
    ai_thresholds: _AiThresholds
    quotas: _Quotas
    polymarket: _PolymarketContract
    stocks: _StockContract
    costs: _CostContract
    baselines: _BaselineContract
    endpoint: _EndpointContract
    statistics: _StatisticsContract
    runtime: _RuntimeContract


class Manifest:
    """Read-only parser-issued view over canonical bytes and a private model."""

    __slots__ = ("_canonical_bytes", "_manifest_id", "_model")
    _canonical_bytes: bytes
    _manifest_id: str
    _model: _ManifestModel

    def __init__(
        self, sentinel: object, canonical_bytes: bytes, model: _ManifestModel
    ) -> None:
        if sentinel is not _CREATION_SENTINEL:
            raise TypeError(
                "Manifest instances can only be created by the manifest parser"
            )
        object.__setattr__(self, "_canonical_bytes", canonical_bytes)
        object.__setattr__(self, "_model", model)
        object.__setattr__(
            self,
            "_manifest_id",
            f"manifest:{canonical_hash('manifest', model.model_dump(mode='json'))}",
        )

    def __setattr__(self, name: str, value: object) -> None:
        raise TypeError("Manifest is immutable; reparse canonical bytes to create one")

    def __copy__(self) -> Manifest:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> Manifest:
        return self

    def __eq__(self, other: object) -> bool:
        return (
            type(other) is Manifest
            and self.canonical_bytes == other.canonical_bytes
            and self.manifest_id == other.manifest_id
        )

    def __hash__(self) -> int:
        return hash((self.canonical_bytes, self.manifest_id))

    @property
    def canonical_bytes(self) -> bytes:
        return self._canonical_bytes

    @property
    def manifest_id(self) -> str:
        return self._manifest_id

    @property
    def manifest_hash(self) -> str:
        return self._manifest_id.removeprefix("manifest:")

    @property
    def schema_version(self) -> int:
        return self._model.schema_version

    @property
    def run(self) -> _RunContract:
        return self._model.run

    @property
    def calendar(self) -> _CalendarContract:
        return self._model.calendar

    @property
    def accounts(self) -> _AccountsContract:
        return self._model.accounts

    @property
    def models(self) -> _ModelContracts:
        return self._model.models

    @property
    def model_input_policy(self) -> _ModelInputPolicyContract:
        return self._model.model_input_policy

    @property
    def providers(self) -> _ProviderContracts:
        return self._model.providers

    @property
    def ai_thresholds(self) -> _AiThresholds:
        return self._model.ai_thresholds

    @property
    def quotas(self) -> _Quotas:
        return self._model.quotas

    @property
    def polymarket(self) -> _PolymarketContract:
        return self._model.polymarket

    @property
    def stocks(self) -> _StockContract:
        return self._model.stocks

    @property
    def costs(self) -> _CostContract:
        return self._model.costs

    @property
    def baselines(self) -> _BaselineContract:
        return self._model.baselines

    @property
    def endpoint(self) -> _EndpointContract:
        return self._model.endpoint

    @property
    def statistics(self) -> _StatisticsContract:
        return self._model.statistics

    @property
    def runtime(self) -> _RuntimeContract:
        return self._model.runtime

    def to_dict(self) -> dict[str, Any]:
        """Return a detached deterministic JSON-domain representation."""
        return self._model.model_dump(mode="json")

    def assert_activation_candidate(self) -> None:
        """Reject examples/placeholders before registration performs I/O checks."""
        trusted = revalidate_manifest(self)
        if trusted.run.example_only:
            raise ValueError("example_only manifest cannot be activated")
        marker = _find_identity_placeholder(trusted.to_dict())
        if marker is not None:
            raise ValueError(
                "activation candidate contains placeholder or unverifiable marker: "
                f"{marker}"
            )


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
    messages: list[str] = []
    errors = sorted(
        error.errors(include_url=False, include_context=False, include_input=False),
        key=lambda item: tuple(str(part) for part in item["loc"]),
    )
    for item in errors[:MAX_VALIDATION_ERRORS]:
        location = ".".join(str(part) for part in item["loc"]) or "<root>"
        messages.append(f"{location}: {item['msg']}")
    if len(errors) > MAX_VALIDATION_ERRORS:
        messages.append(
            f"... {len(errors) - MAX_VALIDATION_ERRORS} additional errors omitted"
        )
    return ValueError("manifest validation failed:\n" + "\n".join(messages))


def _normalized_marker(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _has_unresolved_marker(value: str) -> bool:
    normalized = _normalized_marker(value)
    components = {
        _normalized_marker(component)
        for component in re.split(r"[^a-z0-9]+", value.casefold())
        if component
    }
    return (
        "example" in normalized
        or normalized.startswith(("placeholder", "todo", "tbd"))
        or any(marker in normalized for marker in _UNRESOLVED_IDENTITY_MARKERS)
        or bool(components & _UNRESOLVED_IDENTITY_MARKERS)
    )


def _is_repeated_hash(value: str) -> bool:
    return any(value == value[:width] * (64 // width) for width in range(1, 17))


def _find_identity_placeholder(root: object) -> str | None:
    pending: list[tuple[str, object]] = [("", root)]
    while pending:
        path, value = pending.pop()
        if type(value) is dict:
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else key
                pending.append((child_path, child))
        elif type(value) is list:
            pending.extend(
                (f"{path}[{index}]", child) for index, child in enumerate(value)
            )
        elif type(value) is str:
            field = path.rsplit(".", 1)[-1].casefold()
            identity_field = any(
                token in field
                for token in ("revision", "fingerprint", "artifact", "model", "hash")
            ) or field.endswith("version")
            if identity_field and _has_unresolved_marker(value):
                return f"{path} contains placeholder marker {value!r}"
            if field.endswith("sha256") and _is_repeated_hash(value):
                return f"{path} contains placeholder repeated hash {value!r}"
            structured_identity = (
                field in {"expected_revision", "expected_fingerprint"}
                or field.endswith("version")
                or ("artifact" in field and not field.endswith("sha256"))
            )
            if (
                structured_identity
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{1,}", value) is None
            ):
                return f"{path} contains placeholder or malformed identity {value!r}"
    return None


def parse_manifest_bytes(data: bytes) -> Manifest:
    """Parse strict UTF-8 JSON bytes through the sole trusted construction path."""
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
    except RecursionError as error:
        raise ValueError(
            f"manifest JSON nesting depth exceeds {MAX_JSON_DEPTH}"
        ) from error
    except json.JSONDecodeError as error:
        location = f"line {error.lineno} column {error.colno}"
        raise ValueError(
            f"manifest is invalid JSON at {location}: {error.msg}"
        ) from error
    if type(value) is not dict:
        raise ValueError("manifest JSON root must be an object")
    _check_json_domain(value)
    try:
        model = _ManifestModel.model_validate(value)
    except RecursionError as error:
        raise ValueError(
            f"manifest JSON nesting depth exceeds {MAX_JSON_DEPTH}"
        ) from error
    except ValidationError as error:
        raise _format_validation_error(error) from error
    canonical = canonical_json_bytes(model.model_dump(mode="json"))
    manifest = Manifest(_CREATION_SENTINEL, canonical, model)
    if not manifest.run.example_only:
        marker = _find_identity_placeholder(manifest.to_dict())
        if marker is not None:
            raise ValueError(
                "non-example manifest contains placeholder or unverifiable marker: "
                f"{marker}"
            )
    return manifest


def revalidate_manifest(manifest: Manifest) -> Manifest:
    """Reparse bytes and verify state; the returned value is authoritative."""
    if type(manifest) is not Manifest:
        raise TypeError("revalidation requires an exact Manifest instance")
    try:
        canonical = manifest._canonical_bytes
        stored_id = manifest._manifest_id
        stored_model = manifest._model
    except (AttributeError, TypeError) as error:
        raise ValueError(
            "manifest revalidation failed: wrapper state is invalid"
        ) from error
    if (
        type(canonical) is not bytes
        or type(stored_id) is not str
        or type(stored_model) is not _ManifestModel
    ):
        raise ValueError("manifest revalidation failed: wrapper state is invalid")
    try:
        reparsed = parse_manifest_bytes(canonical)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "manifest revalidation failed: canonical bytes are invalid"
        ) from error
    if (
        reparsed.canonical_bytes != canonical
        or reparsed.manifest_id != stored_id
        or reparsed.to_dict() != stored_model.model_dump(mode="json")
    ):
        raise ValueError(
            "manifest revalidation failed: canonical bytes, model, or ID mismatch"
        )
    return reparsed


def load_manifest(path: str | os.PathLike[str]) -> Manifest:
    """Open once, read at most MAX+1 bytes, and reject practical descriptor races."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    manifest_path = Path(path)
    try:
        metadata = manifest_path.lstat()
    except OSError as error:
        raise ValueError("manifest path must be a regular file") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("manifest path must be a regular file")
    if not metadata.st_mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH):
        raise ValueError("manifest file is not readable")
    try:
        descriptor = os.open(manifest_path, flags)
    except OSError as error:
        raise ValueError("manifest file is not readable") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("manifest path must be a regular file")
        if not before.st_mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH):
            raise ValueError("manifest file is not readable")
        if before.st_size > MAX_MANIFEST_BYTES:
            raise ValueError(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes")
        data = os.read(descriptor, MAX_MANIFEST_BYTES + 1)
        after = os.fstat(descriptor)
    except OSError as error:
        raise ValueError("manifest file is not readable") from error
    finally:
        os.close(descriptor)
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or len(data) != before.st_size:
        raise ValueError("manifest file changed while reading")
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes")
    return parse_manifest_bytes(data)

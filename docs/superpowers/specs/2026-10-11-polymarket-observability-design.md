# Polymarket Feasibility Observability — Design Specification

**Status:** Approved design  
**Date:** 2026-10-11  
**Scope:** ChatGPT + Jev Polymarket paper-feasibility slice  
**Related design:** `docs/superpowers/specs/2026-10-02-jev-trading-lab-design.md`

## 1. Purpose

Expose enough bounded, audit-derived metrics to answer four questions without opening the detailed research record:

1. Is the paper pipeline running and recovering correctly?
2. Are market data and model providers usable within the frozen policy?
3. Are the AI and deterministic arms receiving comparable opportunities and execution conditions?
4. Is the AI arm showing any model-cost-adjusted economic or forecast advantage?

Metrics are an operational projection, not a new source of truth. The verified append-only JSONL journals and content-addressed blobs remain authoritative. A metric must be reproducible from those records or be reported as unknown; it must never be guessed from logs, process-local counters, or a partially accepted journal.

This specification covers the current Polymarket feasibility slice only. It does not authorize live trading or make an alpha claim.

## 2. Decisions and non-goals

### 2.1 Approved architecture

- Produce one Prometheus text exposition snapshot named `jev_polymarket.prom`.
- Regenerate the complete snapshot from authoritative journals and verified blobs after startup recovery and after each terminal `observe` or `replay` run.
- Publish by same-directory temporary file, file `fsync`, atomic rename, then parent-directory `fsync`.
- Continue to store detailed events, identities, questions, prompts, responses, URLs, raw books, and diagnostics only in JSONL/blobs.
- Use Python 3.12 standard-library formatting and filesystem calls. Do not add a Prometheus client dependency.
- Expose no HTTP endpoint. Run no metrics daemon.
- Do not add Prometheus, Node Exporter, Grafana, alerting, or a dashboard to this application.

A host that already runs Node Exporter may configure its textfile collector to read the chosen directory. Direct inspection of the `.prom` file remains supported when no collector exists.

### 2.2 Deferred

The following remain separate operational work:

- Prometheus scrape configuration and retention;
- Grafana dashboards and panels;
- alert routing, paging policy, and SLOs;
- remote write and long-term metric storage;
- cross-host aggregation;
- tracing and log aggregation;
- per-market, per-question, per-prompt, or per-request drill-down.

Those features are not prerequisites for generating the snapshot and must not expand this design into a service.

## 3. Trust model and source hierarchy

The exporter reads the same private, local, owner-controlled output directory as replay. Its trust boundary is the existing one-host paper system: it detects accidental corruption and unsafe filesystem objects, but it does not defend against a malicious same-user process that rewrites all journals and blobs and recomputes unkeyed hashes.

Sources have this precedence:

1. `results.jsonl`: one strict terminal `ObservationResult` per accepted observation identity; authoritative for arm disposition, quantity, VWAP, fees, capital fields, marks or payouts, P&L, forecast status, Brier score, and paired deltas.
2. `artifacts.jsonl` plus `blobs/`: authoritative for stage order, runtime configuration, exact Gamma/CLOB data, model requests and responses, validated model outcomes, decision barriers, eligibility and book failures, token usage, and billed-cost evidence.
3. `provider_calls.jsonl` plus `provider-call-blobs/`: authoritative for provider call intent/result state and restart recovery of calls whose result was unknown in flight.
4. `completed_slots.jsonl`: authoritative for completed observation slots and replay/deduplication of a completed slot.
5. `operations.jsonl` plus `operation-blobs/`: authoritative for startup, run boundaries, monotonic stage durations, recovery actions, storage failures, and process samples that cannot be reconstructed from the four domain journals.

The operational journal uses the existing append-only, canonical-JSON, owner-only, `fsync`-on-append journal/blob mechanism. It is not metric state and cannot contain preaggregated counters. Each event has a stable semantic event identity, event schema version, UTC occurrence time, event kind, bounded status/reason/stage values, and exact numeric observations. Its closed event kinds are `startup`, `run_started`, `run_terminal`, `stage_terminal`, `recovery`, `deduplication`, `storage_failure`, `process_sample`, and `metrics_generation`. A `stage_terminal` event stores monotonic elapsed nanoseconds; a `process_sample` stores normalized source values and probe availability; a `storage_failure` identifies the failed durability boundary without exception text or paths. Large evidence remains blob-backed. Introducing mutable counter files, a metrics database, or log scraping is prohibited.

The current journals do not persist every measurement in this specification. In particular, exact market/provider elapsed durations, every token count on failed calls, process samples, and decomposed spread/slippage may be absent. Absence means unknown under Section 8; it does not permit inference or zero-filling. The metric contract applies as soon as the corresponding authoritative record exists.

## 4. Export location and configuration

The runtime configuration has one observability setting:

- `metrics_textfile_directory`: absolute local directory. Default: `<output_dir>/metrics`.

The filename is fixed as `<metrics_textfile_directory>/jev_polymarket.prom`; it is not configurable because one output directory represents one paper experiment. The producer refuses relative paths, network filesystems, symlinks, non-directory path components, and a destination that is not a regular file owned by the configured trust boundary. The directory must be pre-created with producer write access. If Node Exporter runs as a different user, the operator grants read access through the directory group rather than weakening journal permissions.

The snapshot contains no secrets, URLs, prompts, questions, market IDs, observation IDs, request IDs, hostnames, usernames, filesystem paths, or blob hashes. Mode `0644` is acceptable for the `.prom` file because its schema is intentionally non-sensitive; a deployment may use `0640`. Journals and blobs retain their existing private modes.

A Node Exporter deployment points `--collector.textfile.directory` at `metrics_textfile_directory`. The application neither installs nor starts Node Exporter.

## 5. Naming, units, and label contract

### 5.1 General rules

- Every metric starts with `jev_polymarket_`.
- Counters end in `_total` and are rebuilt as cumulative counts/sums over the retained authoritative journal.
- Current values, ratios, signed values, and aggregate means are gauges.
- Histograms use fixed buckets and emit Prometheus `_bucket`, `_sum`, and `_count` series.
- Seconds are SI seconds. Byte metrics end in `_bytes`. Currency ends in `_usd`. Probabilities, Brier values, ratios, and rates use base units, not percentages or millionths.
- Integer microdollars convert to USD by exact division by `1_000_000`.
- Integer probability/Brier micros convert by exact division by `1_000_000`.
- The renderer uses decimal arithmetic and a canonical finite decimal representation. It never emits `Inf`, `-Inf`, or `NaN` for domain data.
- Each family has exactly one `# HELP` and one `# TYPE` declaration. Families and samples are lexically sorted for deterministic output.

### 5.2 Allowed labels

Only these domain labels are permitted:

| Label | Allowed values |
|---|---|
| `arm` | `ai`, `deterministic` |
| `provider` | `openai`, `jev`, `gamma`, `clob` |
| `model_snapshot` | `gpt-5-2025-08-07`, `jev-1.13.0` |
| `status` | A metric-specific allowlist stated in this document |
| `reason` | A metric-specific bounded reason allowlist stated below |
| `stage` | `startup`, `recovery`, `gamma_poll`, `eligibility`, `precheck_book`, `evidence_freeze`, `openai`, `jev`, `decision_barrier`, `execution_book`, `paper_execution`, `terminal_commit`, `completed_slot`, `metrics_render`, `replay` |

Prometheus's required histogram label `le` is the only structural exception. No summary metrics are used, so `quantile` is absent.

The valid provider/model pairs are only:

- `provider="openai",model_snapshot="gpt-5-2025-08-07"`
- `provider="jev",model_snapshot="jev-1.13.0"`

The renderer rejects cross-products or an unapproved snapshot. A model change requires a specification update and cardinality review; arbitrary provider-returned model strings never become labels.

Bounded `reason` values are:

- exporter unknowns: `fees`, `slippage`, `realized_pnl`, `unrealized_pnl`, `capital`, `brier`, `latency`, `tokens`, `cost`, `process`, `other`;
- trading/eligibility: `none`, `inactive`, `closed`, `sports`, `non_binary`, `missing_evidence`, `close_window`, `low_volume`, `wide_spread`, `low_depth`, `capacity`, `risk`, `no_edge`, `abstain`, `insufficient`, `ambiguous`, `control_unavailable`, `book_failure`, `model_failure`, `public_data_failure`, `other`;
- market data: `timeout`, `transport`, `http_status`, `redirect`, `content_type`, `oversized`, `not_json`, `malformed`, `stale`, `crossed`, `token_drift`, `market_drift`, `duplicate`, `other`;
- model: `input_error`, `input_changed`, `config_failure`, `unknown_in_flight`, `provider_error`, `timeout`, `http_error`, `parse_error`, `model_mismatch`, `citation_error`, `credential_reflection`, `cost_overflow`, `other`;
- reliability/storage: `partial_tail`, `reused_result`, `unknown_in_flight`, `stale_temporary`, `invalid_line`, `invalid_transition`, `conflict`, `missing_blob`, `digest_mismatch`, `unsafe_path`, `lock_timeout`, `write`, `flush`, `fsync`, `rename`, `directory_fsync`, `other`.

A source reason outside the relevant allowlist maps to `other`; the original reason remains in the audit trail. The renderer never dynamically creates a label value.

### 5.3 Cardinality budget

The complete file has a hard budget of **800 time series**, including histogram buckets. The expected initial snapshot is below 500 series. Before rename, the renderer counts unique metric-name/label-set pairs and fails if the budget is exceeded.

Additional constraints:

- no labels from IDs, questions, prompts, URLs, symbols, token IDs, status codes, exception text, paths, commits, PIDs, or hosts;
- no unbounded reason or stage passthrough;
- no per-opportunity, per-observation, per-book-level, or per-call series;
- no histograms with runtime-selected buckets;
- no simultaneous retention of arbitrary historical model snapshots in labels.

Detailed cardinality belongs in JSONL/blobs, not Prometheus.

## 6. Metric catalog

Every source reference below means a fully validated source. A row is omitted when its required inputs are unknown unless the row explicitly defines a count of unknowns.

### 6.1 Snapshot and exporter health

| Metric | Type | Labels | Definition and authoritative source |
|---|---|---|---|
| `jev_polymarket_metrics_schema_version` | Gauge | none | Numeric schema version `1` for this document. |
| `jev_polymarket_metrics_snapshot_generation_timestamp_seconds` | Gauge | none | UTC time at which the published temporary file's source cut was completed. |
| `jev_polymarket_metrics_snapshot_source_max_timestamp_seconds` | Gauge | none | Maximum committed event timestamp included in the snapshot; omitted for an empty source. |
| `jev_polymarket_metrics_regenerations_total` | Counter | `status`=`success|failure|skipped` | Verified operational generation outcomes. The just-published success may appear on the next generation; the generation timestamp is immediate proof of the current publication. |
| `jev_polymarket_metrics_source_unknown_total` | Counter | `reason`=`fees|slippage|realized_pnl|unrealized_pnl|capital|brier|latency|tokens|cost|process|other` | Authoritative records that require a metric input but explicitly lack it. This is not emitted for a concept that had no applicable event. |
| `jev_polymarket_metrics_series` | Gauge | none | Number of series in the current file, including histogram series. |

`status` for regeneration is limited to `success`, `failure`, and `skipped`. A source-identical manual regeneration is `skipped`; startup and terminal-run audit events normally make startup/run regenerations source-distinct.

### 6.2 Trading and forecast economics

| Metric | Type | Labels | Definition and authoritative source |
|---|---|---|---|
| `jev_polymarket_opportunities_total` | Counter | `status`=`eligible|ineligible|public_data_failure|book_failure|model_failure|terminal` | Count of unique terminal opportunity/observation semantics from `results.jsonl`, classified from validated artifacts. `terminal` counts every accepted terminal result and is the denominator sanity check. |
| `jev_polymarket_arm_dispositions_total` | Counter | `arm`,`status` | Terminal arm result count. `status` is `filled|partial_fill|abstain|no_edge|ineligible|model_failure|book_failure|cost_overflow|unknown_in_flight`. |
| `jev_polymarket_fills_total` | Counter | `arm`,`status`=`full|partial` | Positive-quantity terminal arm results. |
| `jev_polymarket_abstains_total` | Counter | `arm`,`reason`=`abstain|insufficient|ambiguous|control_unavailable|other` | Explicit abstentions only; failures and no-edge outcomes do not count as abstains. Sufficiency/ambiguity comes from the verified Jev decision. |
| `jev_polymarket_rejections_total` | Counter | `arm`,`reason` | Terminal non-fill rejections, using the bounded trading/eligibility reason set. A single result contributes once per arm to its final reason. |
| `jev_polymarket_fill_quantity_contracts_total` | Counter | `arm` | Sum of positive filled contract quantity. |
| `jev_polymarket_fill_entry_notional_usd_total` | Counter | `arm` | Sum of exact entry notionals divided by `1_000_000`. |
| `jev_polymarket_fill_mean_quantity_contracts` | Gauge | `arm` | Filled quantity divided by fill count; omitted with zero fills. |
| `jev_polymarket_fill_entry_vwap_probability` | Gauge | `arm` | Quantity-weighted mean entry VWAP, `sum(entry_notional_microdollars) / sum(quantity) / 1_000_000`; omitted with zero quantity. |
| `jev_polymarket_fees_usd_total` | Counter | `arm` | Sum of explicit fee components only. It must not include modeled slippage unless the economic record identifies that component. |
| `jev_polymarket_slippage_usd_total` | Counter | `arm` | Sum of explicit realized/modelled execution slippage postings. Omitted when the journal records only an aggregate fee or threshold assumption. |
| `jev_polymarket_spread_cost_usd_total` | Counter | `arm` | Sum of explicitly persisted spread-cost decomposition. Omitted when spread is only implicit in entry VWAP. |
| `jev_polymarket_realized_pnl_usd` | Gauge | `arm` | Sum of trading P&L for resolved/payout observations plus any explicitly realized exits. Signed gauge, not counter. |
| `jev_polymarket_unrealized_pnl_usd` | Gauge | `arm` | Sum of mark-to-executable-book P&L for unresolved positions. Quantity beyond verified bid depth is valued at zero, matching the paper contract. |
| `jev_polymarket_trading_pnl_usd` | Gauge | `arm` | Realized plus unrealized trading P&L at the source cut. |
| `jev_polymarket_economic_pnl_usd` | Gauge | `arm` | Trading P&L minus known attributable operating/model cost. Omitted for an arm if any included event has unknown economic P&L. |
| `jev_polymarket_capital_initial_usd` | Gauge | `arm` | Frozen arm initial capital, currently `$10,000`, from the validated policy/account record; never summed per opportunity. |
| `jev_polymarket_capital_cash_usd` | Gauge | `arm` | Latest authoritative cash balance. It requires an account/cash record and is not inferred from marked `final_cash` fields. |
| `jev_polymarket_capital_equity_usd` | Gauge | `arm` | Initial capital plus cumulative economic P&L at the source cut, only when all economic P&L is known. |
| `jev_polymarket_capital_gross_exposure_usd` | Gauge | `arm` | Sum of absolute executable marked position notionals at the source cut. |
| `jev_polymarket_forecasts_total` | Counter | `arm`,`status`=`scored|pending|unavailable` | Count by strict `forecast_status`. |
| `jev_polymarket_forecast_brier_score_total` | Counter | `arm` | Sum of non-negative scored Brier values in base units. |
| `jev_polymarket_forecast_mean_brier_score` | Gauge | `arm` | Brier sum divided by scored forecasts; omitted with no scored forecasts. Lower is better. |
| `jev_polymarket_forecast_coverage_ratio` | Gauge | `arm` | Forecasts with a valid probability divided by terminal eligible opportunities; range `[0,1]`. |
| `jev_polymarket_baseline_mean_brier_score` | Gauge | none | Mean scored execution-midpoint baseline Brier value; omitted with no scored baseline. |
| `jev_polymarket_paired_trading_pnl_delta_usd` | Gauge | none | Cumulative AI minus deterministic trading P&L from exact paired deltas. Omitted if any included paired delta overflowed/was unknown. |
| `jev_polymarket_paired_economic_pnl_delta_usd` | Gauge | none | Cumulative AI minus deterministic economic P&L. Omitted if any included paired economic delta is unknown. |
| `jev_polymarket_paired_mean_brier_delta` | Gauge | none | Mean `AI Brier - deterministic Brier` over observations where both are scored. Negative favors AI. |

For current unresolved observations, marked trading P&L is unrealized even though the strict result field is named `trading_pnl_microdollars`. A payout makes it realized. Metrics must classify by resolution/exit evidence, not by field name alone.

A fee, spread, or slippage value is zero only when the authoritative record explicitly states that the applicable component is zero. Missing decomposition is unknown, not zero.

### 6.3 LLM/provider metrics

| Metric | Type | Labels | Definition and authoritative source |
|---|---|---|---|
| `jev_polymarket_llm_model_snapshot_active` | Gauge | `provider`,`model_snapshot` | `1` for each approved pair in the validated runtime configuration represented by retained observations. |
| `jev_polymarket_llm_calls_total` | Counter | `provider`,`model_snapshot`,`status`=`success|failure|unknown_in_flight|cost_overflow` | Count unique durable provider-call intents by terminal call outcome. An intent without a result is `unknown_in_flight` only after recovery classifies it; before recovery it is pending work. |
| `jev_polymarket_llm_failures_total` | Counter | `provider`,`model_snapshot`,`reason` | Failures by bounded model reason. |
| `jev_polymarket_llm_timeouts_total` | Counter | `provider`,`model_snapshot` | Calls whose durable failure reason is `timeout`; `unknown_in_flight` remains separate. |
| `jev_polymarket_llm_call_duration_seconds` | Histogram | `provider`,`model_snapshot` | Monotonic elapsed call duration from durable call start/result timing. Buckets: `0.1,0.25,0.5,1,2.5,5,10,20,30,60,+Inf`. Unknown durations do not enter the histogram. |
| `jev_polymarket_llm_input_tokens_total` | Counter | `provider`,`model_snapshot` | Sum of authoritative input token usage from verified responses. |
| `jev_polymarket_llm_cached_input_tokens_total` | Counter | `provider`,`model_snapshot` | OpenAI cached input tokens; no Jev series is emitted because Jev has no cached-input field. |
| `jev_polymarket_llm_output_tokens_total` | Counter | `provider`,`model_snapshot` | Sum of authoritative output token usage. |
| `jev_polymarket_llm_token_usage_unknown_total` | Counter | `provider`,`model_snapshot` | Completed attempts for which valid token usage is absent. |
| `jev_polymarket_llm_billed_cost_usd_total` | Counter | `provider`,`model_snapshot` | Sum of known billed microdollars divided by `1_000_000`, including failed/abstained calls when billing is known. |
| `jev_polymarket_llm_unknown_cost_calls_total` | Counter | `provider`,`model_snapshot` | Attempts with unknown or unrepresentable billed cost. Cost overflow counts here and in call outcome `cost_overflow`; it never contributes a fabricated numeric cost. |
| `jev_polymarket_llm_parse_checks_total` | Counter | `provider`,`model_snapshot`,`status`=`success|failure` | Strict envelope/schema parse outcomes. Transport/HTTP failures do not count as parse checks. |
| `jev_polymarket_llm_citation_checks_total` | Counter | `provider`=`openai`,`model_snapshot`,`status`=`success|failure` | OpenAI evidence-hash/citation validation outcomes. |
| `jev_polymarket_llm_restart_recoveries_total` | Counter | `provider`,`model_snapshot`,`status`=`reused_result|unknown_in_flight|pending` | Provider call recovery decisions derived from intent/result journal state. |

Call counts come from provider intents, not response artifacts, so a crash cannot erase a billable attempt. Token totals come from strict response usage, not local token estimation. Billed cost comes from validated usage and frozen rates or an authoritative billed amount. HTTP status codes remain audit details and are not labels.

### 6.4 Market-data metrics

| Metric | Type | Labels | Definition and authoritative source |
|---|---|---|---|
| `jev_polymarket_market_data_calls_total` | Counter | `provider`=`gamma|clob`,`status`=`success|failure|timeout` | One per persisted HTTP attempt, including attempts that yielded no candidate. |
| `jev_polymarket_market_data_failures_total` | Counter | `provider`=`gamma|clob`,`reason` | Failures by bounded market-data reason. |
| `jev_polymarket_market_data_call_duration_seconds` | Histogram | `provider`=`gamma|clob` | Monotonic request duration. Buckets: `0.05,0.1,0.25,0.5,1,2.5,5,10,20,30,+Inf`. |
| `jev_polymarket_market_data_malformed_total` | Counter | `provider`=`gamma|clob`,`reason`=`content_type|oversized|not_json|malformed|duplicate|other` | Structurally unusable responses or rows. |
| `jev_polymarket_market_data_stale_total` | Counter | `provider`=`clob`,`stage`=`precheck_book|execution_book` | Books rejected by the frozen freshness contract. |
| `jev_polymarket_market_eligibility_total` | Counter | `status`=`eligible|rejected`,`reason` | One result per evaluated market candidate. Eligible uses `reason="none"`; rejected uses the bounded trading/eligibility reason set. |
| `jev_polymarket_market_book_spread_probability` | Gauge | `stage`=`precheck_book|execution_book` | Latest verified best ask minus best bid in probability units. |
| `jev_polymarket_market_book_spread_probability_histogram` | Histogram | `stage`=`precheck_book|execution_book` | Spread distribution. Buckets: `0.001,0.0025,0.005,0.01,0.02,0.03,0.04,0.05,0.1,0.25,+Inf`. |
| `jev_polymarket_market_book_bid_near_depth_contracts` | Gauge | `stage`=`precheck_book|execution_book` | Latest sum of bid quantity within `0.02` of best bid. |
| `jev_polymarket_market_book_ask_near_depth_contracts` | Gauge | `stage`=`precheck_book|execution_book` | Latest sum of ask quantity within `0.02` of best ask. |
| `jev_polymarket_market_book_bid_near_depth_contracts_histogram` | Histogram | `stage`=`precheck_book|execution_book` | Bid near-depth distribution. Buckets: `100,500,1000,2500,5000,10000,25000,50000,100000,+Inf`. |
| `jev_polymarket_market_book_ask_near_depth_contracts_histogram` | Histogram | `stage`=`precheck_book|execution_book` | Ask near-depth distribution with the same buckets. |
| `jev_polymarket_market_data_last_poll_timestamp_seconds` | Gauge | `status`=`success|failure` | UTC completion time of the newest terminal Gamma poll by status. |
| `jev_polymarket_market_data_poll_age_seconds` | Gauge | `status`=`success|failure` | Snapshot generation time minus the corresponding latest poll timestamp. This is age **as of generation**, not continuously increasing time. Consumers that need current age use `time() - jev_polymarket_market_data_last_poll_timestamp_seconds`. |
| `jev_polymarket_market_data_latest_book_age_seconds` | Gauge | `stage`=`precheck_book|execution_book` | For the newest verified book at each stage, response retrieval time minus exchange/book timestamp. |

Gamma pagination must persist one attempt per page; candidate artifacts alone cannot reconstruct call count or latency. CLOB precheck and execution requests are separate stages even when they address the same endpoint.

### 6.5 Pipeline metrics

| Metric | Type | Labels | Definition and authoritative source |
|---|---|---|---|
| `jev_polymarket_pipeline_runs_total` | Counter | `status`=`success|failure|replayed|recovered` | Terminal invocation outcomes from run audit events. A replayed completed slot is `replayed`, not another economic run. |
| `jev_polymarket_pipeline_terminal_results_total` | Counter | `status`=`filled|no_fill|rejected|failed|unknown`,`reason` | Unique accepted terminal observations. |
| `jev_polymarket_pipeline_stage_duration_seconds` | Histogram | `stage` | Monotonic elapsed duration for completed stages. Buckets: `0.001,0.01,0.05,0.1,0.25,0.5,1,2.5,5,10,30,60,300,+Inf`. |
| `jev_polymarket_pipeline_stage_outcomes_total` | Counter | `stage`,`status`=`success|failure|skipped|recovered` | Durable terminal stage outcomes. |
| `jev_polymarket_pipeline_last_success_timestamp_seconds` | Gauge | none | UTC terminal time of the latest successful non-duplicated observation run; omitted before first success. |
| `jev_polymarket_pipeline_last_terminal_timestamp_seconds` | Gauge | `status`=`success|failure|replayed|recovered` | Latest terminal timestamp per run status. |
| `jev_polymarket_pipeline_pending_work_items` | Gauge | `stage` | Nonterminal durable intents at the source cut, including provider intents and started runs. |
| `jev_polymarket_pipeline_oldest_pending_age_seconds` | Gauge | `stage` | Generation time minus oldest pending intent time; omitted when none. |
| `jev_polymarket_pipeline_unresolved_observations` | Gauge | `status`=`pending_resolution|unknown_cost|missing_terminal` | Current count requiring resolution, cost recovery, or terminalization. |
| `jev_polymarket_pipeline_decision_barriers_total` | Counter | `status`=`openai_failure|jev_failure|jev_success|cost_overflow` | Validated `DecisionFrozenArtifact.terminal_model_state` count. |
| `jev_polymarket_pipeline_decision_barrier_age_seconds` | Gauge | `status`=`pending_execution|terminal` | Age as of generation of the oldest decision barrier in that state. |
| `jev_polymarket_pipeline_terminal_ratio` | Gauge | none | Terminal completed slots divided by scheduled/started slots represented in the audit trail; omitted with no denominator. |

Artifact `retrieved_at` timestamps establish causality but are not substitutes for elapsed stage duration. Stage histograms require persisted monotonic elapsed values measured around the stage. Replay validates original durations but does not add them again.

### 6.6 Reliability and durability metrics

| Metric | Type | Labels | Definition and authoritative source |
|---|---|---|---|
| `jev_polymarket_reliability_restarts_total` | Counter | `status`=`clean|recovery_required|recovery_failed` | Startup count classified after journal scan/recovery. |
| `jev_polymarket_reliability_replay_attempts_total` | Counter | `status`=`success|failure|deduplicated` | Explicit replay invocations and completed-slot replays. Economic observations remain deduplicated. |
| `jev_polymarket_reliability_recovery_actions_total` | Counter | `stage`,`status`=`success|failure`,`reason` | Durable recovery actions such as tail truncation, reuse, terminalization, or cleanup. |
| `jev_polymarket_reliability_unknown_in_flight_total` | Counter | `provider`=`openai|jev` | Provider intents terminalized as `UNKNOWN_IN_FLIGHT`. |
| `jev_polymarket_reliability_deduplications_total` | Counter | `stage`=`openai|jev|terminal_commit|completed_slot|replay` | Duplicate-identical semantic work reused rather than appended/billed again. |
| `jev_polymarket_reliability_journal_corruption_total` | Counter | `reason`=`partial_tail|invalid_line|invalid_transition|conflict|unsafe_path|other` | Detected journal damage. Recoverable `partial_tail` also counts as a recovery action. |
| `jev_polymarket_reliability_blob_corruption_total` | Counter | `reason`=`missing_blob|digest_mismatch|unsafe_path|other` | Missing, unsafe, or hash-invalid content-addressed artifacts. |
| `jev_polymarket_reliability_fsync_failures_total` | Counter | `stage` | Failed `fsync` operations classified by the operation's bounded stage. |
| `jev_polymarket_reliability_write_failures_total` | Counter | `stage`,`reason`=`write|flush|fsync|rename|directory_fsync|other` | Failed durable writes, including metric publication. |
| `jev_polymarket_reliability_journal_bytes` | Gauge | `stage`=`terminal_commit|completed_slot|openai|jev|recovery` | Current validated journal byte size mapped to its logical stage. |
| `jev_polymarket_reliability_verified_blobs` | Gauge | `status`=`valid|missing|corrupt` | Blob verification count from the completed source scan. A corrupt scan is never published as a new economic snapshot; see Section 9. |

An exporter failure is operational, not an economic result. It must not alter an already committed observation or delete the last good `.prom` file.

### 6.7 Experiment-health metrics

These are descriptive feasibility indicators, not promotion criteria. They do not replace the preregistered analysis.

| Metric | Type | Labels | Definition and authoritative source |
|---|---|---|---|
| `jev_polymarket_experiment_paired_samples_total` | Counter | `status`=`resolved|pending|unknown_cost|unscored` | Unique synchronized AI/control samples by current evaluability. |
| `jev_polymarket_experiment_paired_wins_total` | Counter | `status`=`ai|deterministic|tie` | Resolved samples with known paired economic P&L, classified by sign. |
| `jev_polymarket_experiment_ai_win_rate` | Gauge | none | `AI wins / (AI wins + deterministic wins)`; ties excluded and separately counted. Omitted when no non-tie resolved pair exists. |
| `jev_polymarket_experiment_cumulative_pnl_usd` | Gauge | `arm` | Cumulative known economic P&L by arm. Omitted for an arm when included cost is unknown. |
| `jev_polymarket_experiment_cumulative_paired_pnl_delta_usd` | Gauge | none | AI minus deterministic cumulative economic P&L over paired known samples. |
| `jev_polymarket_experiment_mean_brier_score` | Gauge | `arm` | Mean Brier score over scored forecasts. |
| `jev_polymarket_experiment_forecast_coverage_ratio` | Gauge | `arm` | Valid forecast probability count divided by registered eligible opportunity count. |
| `jev_polymarket_experiment_model_cost_usd_total` | Counter | `provider`=`openai|jev` | Known cumulative model cost. |
| `jev_polymarket_experiment_model_cost_adjusted_edge_usd` | Gauge | none | `cumulative paired trading P&L delta - (AI model cost - deterministic model cost)`. In the current design deterministic model cost is zero. Omitted if either delta or required cost is unknown. |
| `jev_polymarket_experiment_model_cost_adjusted_edge_per_pair_usd` | Gauge | none | Model-cost-adjusted edge divided by paired known-sample count; omitted with zero denominator. |

Marked unresolved P&L may appear in trading gauges but does not count as an experiment win. Brier metrics require an authoritative payout. Coverage includes valid forecasts from abstained/no-fill opportunities, consistent with intention-to-treat scoring.

### 6.8 Process and build metrics

These metrics describe the latest audited application invocation that generated or preceded the snapshot, not a continuously running daemon.

| Metric | Type | Labels | Definition and authoritative source |
|---|---|---|---|
| `jev_polymarket_process_last_run_runtime_seconds` | Gauge | `status`=`success|failure|replayed|recovered` | Monotonic elapsed runtime of the latest terminal invocation with that status. |
| `jev_polymarket_process_resident_memory_bytes` | Gauge | none | Resident set size sampled immediately before the latest terminal process audit event. |
| `jev_polymarket_process_peak_resident_memory_bytes` | Gauge | none | Process peak RSS normalized to bytes for the latest invocation. |
| `jev_polymarket_process_cpu_seconds_total` | Counter | `status`=`user|system` | Cumulative audited CPU seconds across retained invocations. Per-run deltas are persisted so restart does not reset the reconstructed total. |
| `jev_polymarket_process_open_fds` | Gauge | none | Open file descriptor count sampled at the latest terminal process event; Linux `/proc/self/fd` only. Omitted where unsupported. |
| `jev_polymarket_process_build_version` | Gauge | none | Package SemVer encoded as `major*1_000_000 + minor*1_000 + patch`; current `0.1.0` emits `1000`. Non-numeric prerelease/build details stay in audit records. |
| `jev_polymarket_process_build_commit_timestamp_seconds` | Gauge | none | Commit author timestamp captured in validated startup identity; omitted when unavailable. |
| `jev_polymarket_process_build_dirty` | Gauge | none | `1` for a dirty source tree and `0` for clean, from startup identity. |
| `jev_polymarket_process_build_identity` | Gauge | `status`=`valid|dirty|mismatch|unknown` | Exactly one series with value `1` for the latest audited runtime identity classification. |

Commit SHA, executable path, host, boot ID, PID, lock hash, and full version string remain in the startup audit record. They are prohibited as metric labels.

## 7. Source-to-metric reconstruction rules

### 7.1 Accepted source cut

A generation operates under the output-directory observation lock and defines one source cut after startup recovery or terminal commit. It must:

1. run canonical journal readers;
2. recover only an allowed torn final line;
3. validate journal transition ordering and duplicate identity rules;
4. resolve every referenced blob through canonical path, size, and SHA-256 verification;
5. validate every strict model and the artifact predecessor chain;
6. join terminal results to artifacts and provider calls by internal semantic IDs;
7. reject orphaned, conflicting, or cross-opportunity records;
8. capture the maximum committed source timestamp;
9. aggregate only the accepted cut.

Internal IDs are required for joins but never leave the exporter in labels or metric comments.

### 7.2 Deduplication and idempotence

- A terminal observation identity contributes economic metrics once.
- A provider call identity contributes call, token, latency, and cost metrics once.
- Duplicate-identical replay returns the stored terminal result and does not increment opportunities, fills, P&L, Brier, tokens, or cost.
- The replay invocation itself may increment `replay_attempts_total` through a distinct operational event.
- Duplicate-conflicting bytes are corruption and block publication.
- Completed-slot reuse increments a deduplication event but not a pipeline economic run.
- A correction, if later supported, must be an append-only typed successor. The projection uses the canonical active successor and separately audits the correction; it never sums both versions.

All counters are full-history reductions over retained records, not in-memory increments. Re-running generation over the same source cut produces identical domain samples. If no source event changed, manual generation records `skipped` and leaves the existing file untouched, preserving byte identity and modification time.

### 7.3 Aggregation details

- Signed sums use exact integers before conversion to base units.
- A sum that exceeds the declared exact source domain is unknown; it is not clamped.
- Ratios are calculated only after numerator and denominator are known and denominator is positive.
- Means are weighted exactly as named. VWAP is quantity weighted; mean Brier is observation weighted.
- Latest gauges choose the greatest authoritative event timestamp, then the canonical semantic event identity as a deterministic tie-breaker.
- Histograms count each unique authoritative duration/value once. Replays do not re-observe original values.
- Histogram `_sum` uses base units and `_count` is exact.
- No metric infers real executions: every fill is explicitly paper-only in HELP text.

## 8. Unknown, absent, and zero values

Unknown is distinct from zero.

1. **Known zero:** emit `0` only when the event applies and the authoritative record explicitly establishes zero.
2. **Not applicable:** omit the series for a provider/arm/state that cannot occur, such as Jev cached tokens.
3. **No observations yet:** counters may emit `0` for fixed bounded dimensions; means, ratios, last-event timestamps, and latest-value gauges are omitted when no denominator/event exists.
4. **Unknown input:** omit the affected aggregate gauge and increment the relevant unknown counter. Do not partially sum an aggregate that claims completeness.
5. **Unknown cost:** increment `llm_unknown_cost_calls_total`; omit affected economic P&L and cost-adjusted edge. Trading P&L may still be emitted because it excludes operating cost.
6. **Unresolved market:** emit pending forecast coverage and unrealized marked P&L when a verified executable mark exists; omit Brier and realized P&L for that observation.
7. **Unrepresentable cost overflow:** count the overflow outcome and unknown cost; never serialize the oversized value as infinity or a rounded float.
8. **Unsupported process probe:** omit that process gauge and count source unknown `reason="process"` once for the applicable process event.

The renderer may emit fixed zero-valued counters to stabilize basic queries, but it must not generate impossible provider/model pairs or reason/stage combinations.

## 9. Partial and corrupt journal behavior

### 9.1 Recoverable final partial line

A file that ends without a newline may have only its bytes after the last complete newline truncated, under the exclusive output lock, using the canonical journal recovery behavior. Recovery must:

- preserve and audit the discarded byte count and journal kind;
- `ftruncate`, `fsync` the journal, and `fsync` the parent directory where required by file-creation semantics;
- increment recovery action `reason="partial_tail"` on the next valid snapshot;
- re-read and validate the complete journal before aggregation.

A partial tail is never parsed, guessed, or included.

### 9.2 Fatal corruption

A malformed newline-terminated line, invalid canonical JSON, invalid transition, conflicting duplicate, unsafe path, missing blob, wrong byte size, digest mismatch, broken predecessor chain, or result/artifact mismatch is fatal.

On fatal corruption:

- do not publish a partial snapshot;
- do not replace or delete the last good `.prom` file;
- fail startup/run health and block new entry work under the existing fail-closed policy;
- record a bounded operational corruption event if the operational audit path itself remains safe;
- return a nonzero command result;
- require repair/recovery before the next successful generation.

The retained `.prom` file may therefore become stale. Consumers detect this from `metrics_snapshot_generation_timestamp_seconds`. The exporter does not overwrite trustworthy old metrics with a fresh file built from untrustworthy data.

If no prior valid snapshot exists, fatal corruption leaves no `.prom` file.

## 10. Timestamp and duration semantics

- Journal event timestamps are UTC instants and remain integer-microsecond/exact datetime values in the audit trail.
- Prometheus timestamp-valued gauges use Unix seconds, including fractions when source precision requires them.
- The exposition file contains no explicit sample timestamps. The scraper assigns scrape time; event-time gauges carry the actual domain time.
- Snapshot generation time is captured after the source cut and before temporary-file serialization.
- `*_age_seconds` gauges are `generation_time - event_time` and are non-negative. They are snapshots, not clocks. PromQL should derive current age from event timestamps.
- Network and stage durations use a monotonic clock. UTC start/finish timestamps are retained for causal ordering but are not subtracted to produce latency.
- A restart cannot continue a monotonic interval. An interrupted stage has no duration sample until recovery emits a terminal interrupted/recovered event; it increments unknown latency when no exact elapsed value survived.
- Provider latency begins immediately before transport invocation after durable intent and ends when transport returns or raises. Journal/lock wait is a separate stage duration.
- Market-data latency includes DNS, connect, TLS, response headers, and bounded body read for the one attempt.
- Pipeline run duration begins after startup recovery and lock acquisition for that invocation and ends after terminal/completed-slot commit, before post-run metric generation.

## 11. Snapshot generation and atomic publication

Generation is a deterministic, read-only reduction except for operational audit outcomes and publication itself.

1. Acquire the existing output-directory lock.
2. Perform startup recovery when generation is invoked at startup.
3. Establish and validate the source cut under Section 7.
4. Aggregate with standard-library code and enforce label allowlists, finite values, metric syntax, and the 800-series budget.
5. Serialize complete exposition bytes in deterministic order, ending with a newline.
6. Parse the generated bytes with the project's own strict text-exposition validator before touching the destination.
7. In `metrics_textfile_directory`, create an unpredictable same-directory temporary regular file using `O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC` and the configured mode.
8. Write all bytes with short-write/EINTR handling, flush, and `fsync` the temporary file.
9. Revalidate destination directory identity and reject a destination symlink or non-regular existing object.
10. Atomically replace `jev_polymarket.prom` with the temporary file using same-filesystem rename/replace.
11. `fsync` the parent directory.
12. Release the lock and append the bounded regeneration outcome to operational audit state. That outcome appears in the next generation; publication timestamp proves the current success.

If any step before rename fails, close and remove only the temporary file and retain the old snapshot. If rename succeeds but directory `fsync` fails, report a durability failure and retain the new visible file; do not attempt to roll back because crash durability is unknown. The next startup audits and regenerates it.

A temporary filename has the form `.jev_polymarket.prom.tmp.<pid>.<random>`, but it never appears in metrics. Startup removes only stale files that match this exact owned regular-file pattern after auditing recovery; it follows no links.

The renderer never edits the destination in place and never appends exposition fragments.

## 12. Integration points

### 12.1 Startup

Before network calls or new opportunity work:

1. validate configuration and destination;
2. acquire the output lock;
3. validate/recover journals and in-flight provider calls;
4. append the startup and recovery operational events;
5. regenerate and atomically publish the snapshot;
6. proceed only if domain recovery is valid.

A metrics publication failure does not rewrite domain journals or fabricate terminal observations. It does make the command operationally failed/nonzero so stale monitoring is visible to supervision.

### 12.2 `polymarket observe`

- Persist call/stage/process measurements as audit events at their normal durability boundaries.
- Do not update `.prom` between stages.
- After all terminal observations and `completed_slots.jsonl` are durable, append the terminal run/process event and regenerate once.
- If a run crashes, the prior snapshot remains valid; startup recovery publishes the recovered state.

### 12.3 `polymarket replay`

- Replay validates journals/blobs before aggregation.
- Replaying existing semantic work does not duplicate economic/provider metrics.
- Persist one bounded replay operational outcome.
- Regenerate after the replay terminal outcome, including failed replay outcomes when the authoritative source remains valid.

### 12.4 Decision barrier

`decision_frozen` is the mandatory boundary between model work and execution-book access. Every valid barrier contributes exactly once to `pipeline_decision_barriers_total`. A barrier without terminal execution/book-failure state appears as pending work. Model failure, unknown cost, and cost overflow remain distinct statuses.

## 13. Required verification

The observability implementation is acceptable only when the following tests pass. These are behavioral acceptance tests, not a delivery sequence.

### 13.1 Exposition and schema tests

- Golden snapshot covers all fixed metric families with exact HELP/TYPE lines, lexical order, final newline, and canonical decimal rendering.
- Every emitted metric name has the declared type and unit suffix.
- Label validator rejects IDs, questions, prompts, URLs, secrets, paths, dynamic status codes, unknown stages/reasons, invalid provider/model pairs, and unexpected labels.
- Values reject NaN and infinities.
- Prospective series count at 800 succeeds; 801 fails without replacing the old file.
- Histograms have cumulative buckets, `+Inf`, matching `_count`, and exact `_sum`.

### 13.2 Reconstruction tests

- Fixture observations reproduce exact fills, quantities, quantity-weighted VWAP, fees, marked/realized classification, P&L, capital, Brier, coverage, and paired deltas.
- Known zero, no event, not applicable, unknown, unresolved, and overflow each produce the specified distinct output.
- Failed and abstained LLM calls retain known billed cost.
- Provider intent without result becomes pending before recovery and `unknown_in_flight` after recovery.
- Token totals come only from verified strict usage fields.
- Gamma pages and CLOB precheck/execution attempts count independently.
- Spread and two-point near-depth are recomputed from verified book levels.
- Model-cost-adjusted edge reconciles exactly to paired trading P&L and model costs.

### 13.3 Idempotence and replay tests

- Two reductions of the same source cut produce byte-identical domain output.
- A source-identical manual regeneration leaves the file untouched and audits `skipped`.
- Duplicate-identical observation replay does not change opportunity, fill, token, cost, P&L, or Brier totals.
- Replay invocation and deduplication counters increase only from their distinct operational events.
- Conflicting duplicate identity fails closed.
- Reordered filesystem enumeration does not change output.

### 13.4 Crash and corruption tests

Inject failure before and after every write, flush, file `fsync`, rename, and directory `fsync` boundary.

Verify:

- old snapshot survives every pre-rename failure;
- no partial destination is visible;
- post-rename directory-`fsync` failure is reported honestly;
- a torn final JSONL line is truncated, audited, and excluded;
- malformed complete lines and middle corruption are fatal;
- missing/wrong-size/wrong-hash blobs are fatal;
- unsafe symlinks, FIFOs, foreign-owned files, and replaced directories are rejected;
- stale temporary cleanup follows no links and removes no unrelated file;
- fatal corruption retains the previous snapshot and returns nonzero.

### 13.5 Timing and process tests

- Wall-clock jumps do not affect monotonic duration histograms.
- Interrupted durations are unknown rather than negative or guessed.
- timestamp gauges preserve UTC event meaning and age is measured at generation.
- Linux RSS, CPU, and FD probes normalize documented units; unsupported probes omit metrics and record unknown process source.
- package version encoding for `0.1.0` is `1000`; dirty/mismatch/unknown build states are exclusive.

### 13.6 Integration tests

- Offline fixture replay generates a valid snapshot without network or credentials.
- `observe` terminal success, rejection, provider failure, book failure, unknown cost, and public poll failure each regenerate once after durable terminal commit.
- Startup with completed work regenerates before network access.
- Startup with in-flight intent recovers first, then publishes pending/unknown state consistently.
- Concurrent invocations serialize on the output lock and produce one valid destination.
- Generated output is accepted by `promtool check metrics` when `promtool` is available; the standard-library validator remains mandatory so verification does not depend on Prometheus installation.

## 14. Acceptance constraints

This design is satisfied only if the resulting exporter:

- remains paper-only and has no order/signing path;
- derives all domain values from verified append-only audit state;
- emits the full metric families above without high-cardinality detail;
- preserves unknowns instead of converting them to zero;
- is deterministic and replay-idempotent;
- fails closed on source corruption;
- preserves the last good snapshot on publication failure;
- adds no Prometheus client library, HTTP listener, daemon, or dashboard;
- keeps detailed observations in JSONL/blobs.

The `.prom` snapshot is disposable. Deleting it loses no experiment evidence; the next valid startup reconstructs it from the journals and blobs.

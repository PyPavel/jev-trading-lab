# Polymarket Prometheus Textfile Observability Implementation Plan

> **For Hermes:** Use `subagent-driven-development` to implement this plan task-by-task. Dispatch a fresh implementer, then a fresh specification reviewer, then a fresh quality/security reviewer for every task; do not advance until both reviewers approve.

**Goal:** Generate and atomically publish one deterministic `jev_polymarket.prom` snapshot containing all 105 approved bounded metric families, reconstructed only from verified append-only journals and content-addressed blobs.

**Architecture:** Add a standard-library Prometheus schema/renderer, an append-only operational audit journal, one verified source-cut loader, pure reducers, and a descriptor-relative atomic textfile publisher. Startup recovery and terminal `observe`/`replay` paths regenerate under the existing output lock; detailed evidence remains in JSONL/blobs, while unknown inputs omit affected aggregates and increment bounded unknown counters.

**Tech stack:** Python 3.12, stdlib (`dataclasses`, `decimal`, `hashlib`, `os`, `resource`, `time`), existing Pydantic 2 strict models and `BlobStore`, Typer, pytest. No Prometheus client, HTTP listener, daemon, Node Exporter installation, Grafana, alerts, or dashboard.

**Approved specification:** `docs/superpowers/specs/2026-10-11-polymarket-observability-design.md` at `1cbdb33`.

---

## Mandatory sequencing and ownership gate

A separate owner is changing credential-boundary runtime files. **Do not begin Task 1 until that owner has committed the active runtime fix and the worktree is free of their tracked modifications.** The plan itself may be committed while that work is active; implementation may not.

Run these commands separately before implementation:

```bash
git status --short
git log -1 --oneline
uv run pytest -q
```

Required gate:

- no tracked modifications remain from the credential-boundary work;
- `.hermes-tmp.W3Xrv5` remains untouched and untracked;
- the runtime-fix commit is visible at `HEAD` or in branch history;
- baseline tests pass before observability changes;
- each implementer rebases its understanding on the post-fix versions of `ai.py`, `store.py`, `pipeline.py`, and their tests.

If the gate is not met, stop. Do not stash, reset, restore, overwrite, stage, or commit the other owner's files.

## Scope and invariants

1. Emit the exact 105 families in design Section 6, even when a family has no samples; each family has exactly one lexical `# HELP` and `# TYPE` pair.
2. Build domain values only from accepted `results.jsonl`, `artifacts.jsonl`/`blobs`, `provider_calls.jsonl`/`provider-call-blobs`, `completed_slots.jsonl`, and `operations.jsonl`/`operation-blobs` cuts.
3. Never scrape logs or maintain mutable counter state. Operational events are raw facts, not preaggregated counters.
4. Preserve unknown versus known zero versus not-applicable versus no-observation semantics from design Section 8.
5. Allow only the approved bounded labels and provider/model pairs. Never expose IDs, questions, prompts, URLs, paths, hostnames, users, PIDs, status codes, exceptions, commits, or hashes.
6. Use exact integer/`Decimal` arithmetic until canonical finite decimal serialization. Never emit `NaN` or infinity for domain data.
7. Count unique metric-name/label-set series, including histogram `_bucket`, `_sum`, and `_count`; allow 800 and reject 801.
8. Validate the complete exposition before publication. Publish with same-directory unpredictable temporary file, short-write/EINTR handling, file `fsync`, atomic replace, and parent-directory `fsync`.
9. Fatal source corruption never replaces or deletes the last good file. A post-rename directory-`fsync` failure reports uncertain durability and does not roll back the visible file.
10. Startup recovery precedes publication and all network work. Terminal `observe` and `replay` publish once after durable terminal/run/process events.
11. Keep paper-only and offline replay boundaries intact. Add no order/signing/private Polymarket path.
12. The configured metrics directory is absolute, local, pre-created, non-symlinked, and descriptor-walk validated. The default is absolute `<output_dir>/metrics`; tests and examples must create it explicitly.
13. A manual regeneration whose non-generation source fingerprint is unchanged records `metrics_generation(status="skipped")` and leaves destination bytes and mtime unchanged. Generation audit events do not themselves make that fingerprint distinct; they are included when a later domain/run event causes a real publication.

## File map

Create:

- `src/jev_trading_lab/polymarket/metrics_schema.py` — all 105 family definitions, label allowlists, buckets, sample value types.
- `src/jev_trading_lab/polymarket/metrics_render.py` — strict validation, canonical decimal rendering, deterministic exposition parsing/rendering, series budget.
- `src/jev_trading_lab/polymarket/operations.py` — strict operational events, content-addressed evidence, recovery, process/build probes.
- `src/jev_trading_lab/polymarket/metrics_reconstruct.py` — accepted cut, pure aggregation, unknown propagation, all reducers.
- `src/jev_trading_lab/polymarket/metrics_publish.py` — config/path validation, local-filesystem check, atomic publication.
- `src/jev_trading_lab/polymarket/observability.py` — recovery/startup/terminal generation coordinator.
- `tests/polymarket/test_metrics_schema.py`
- `tests/polymarket/test_metrics_render.py`
- `tests/polymarket/test_operations.py`
- `tests/polymarket/test_metrics_reconstruct.py`
- `tests/polymarket/test_metrics_publish.py`
- `tests/integration/test_polymarket_metrics.py`
- `tests/fixtures/prometheus/jev_polymarket.prom`

Modify only after the sequencing gate:

- `src/jev_trading_lab/polymarket/store.py`
- `src/jev_trading_lab/polymarket/call_journal.py`
- `src/jev_trading_lab/polymarket/client.py`
- `src/jev_trading_lab/polymarket/ai.py`
- `src/jev_trading_lab/polymarket/pipeline.py`
- `src/jev_trading_lab/cli.py`
- `tests/polymarket/test_store.py`
- `tests/polymarket/test_unit6_restart_safety.py`
- `tests/polymarket/test_client.py`
- `tests/polymarket/test_ai.py`
- `tests/integration/test_polymarket_fixture_replay.py`
- `tests/integration/test_polymarket_live_boundary.py`
- `tests/integration/test_polymarket_cli.py`
- `README.md`
- `config/phase1.example.json`

Do not modify dependencies in `pyproject.toml` or `uv.lock`.

## Per-task execution and review protocol

Every task below uses this exact sequence:

1. **Implementer:** write the named failing tests first; run the RED command and confirm failure for the missing behavior; implement only the named behavior; run the GREEN and regression commands; inspect the diff; commit with the exact task commit message.
2. **Specification reviewer:** compare the commit to the task and approved design. Check source authority, unknown handling, bounded labels, file paths, and absence of scope additions. Return `PASS` or file/line findings. The implementer fixes findings in a focused follow-up commit; repeat until `PASS`.
3. **Quality/security reviewer:** only after specification `PASS`, review durability boundaries, symlink/ownership handling, finite/exact arithmetic, cardinality, secret/high-cardinality leakage, tests, and maintainability. Return `APPROVED` or file/line findings. The implementer fixes and reruns commands; repeat until `APPROVED`.
4. Record both verdicts before moving to the next task. Never let a reviewer implement its own findings, and never combine the two reviews.

For every Python task, the quality reviewer also runs:

```bash
uv run ruff check <task files>
uv run mypy src
```

## Metric coverage ledger

The schema test must compare the registry to this exact section allocation and total `105`:

- Snapshot/exporter health (6): `metrics_schema_version`, `metrics_snapshot_generation_timestamp_seconds`, `metrics_snapshot_source_max_timestamp_seconds`, `metrics_regenerations_total`, `metrics_source_unknown_total`, `metrics_series`.
- Trading/forecast economics (28): `opportunities_total`, `arm_dispositions_total`, `fills_total`, `abstains_total`, `rejections_total`, `fill_quantity_contracts_total`, `fill_entry_notional_usd_total`, `fill_mean_quantity_contracts`, `fill_entry_vwap_probability`, `fees_usd_total`, `slippage_usd_total`, `spread_cost_usd_total`, `realized_pnl_usd`, `unrealized_pnl_usd`, `trading_pnl_usd`, `economic_pnl_usd`, `capital_initial_usd`, `capital_cash_usd`, `capital_equity_usd`, `capital_gross_exposure_usd`, `forecasts_total`, `forecast_brier_score_total`, `forecast_mean_brier_score`, `forecast_coverage_ratio`, `baseline_mean_brier_score`, `paired_trading_pnl_delta_usd`, `paired_economic_pnl_delta_usd`, `paired_mean_brier_delta`.
- LLM/provider (14): `llm_model_snapshot_active`, `llm_calls_total`, `llm_failures_total`, `llm_timeouts_total`, `llm_call_duration_seconds`, `llm_input_tokens_total`, `llm_cached_input_tokens_total`, `llm_output_tokens_total`, `llm_token_usage_unknown_total`, `llm_billed_cost_usd_total`, `llm_unknown_cost_calls_total`, `llm_parse_checks_total`, `llm_citation_checks_total`, `llm_restart_recoveries_total`.
- Market data (15): `market_data_calls_total`, `market_data_failures_total`, `market_data_call_duration_seconds`, `market_data_malformed_total`, `market_data_stale_total`, `market_eligibility_total`, `market_book_spread_probability`, `market_book_spread_probability_histogram`, `market_book_bid_near_depth_contracts`, `market_book_ask_near_depth_contracts`, `market_book_bid_near_depth_contracts_histogram`, `market_book_ask_near_depth_contracts_histogram`, `market_data_last_poll_timestamp_seconds`, `market_data_poll_age_seconds`, `market_data_latest_book_age_seconds`.
- Pipeline (12): `pipeline_runs_total`, `pipeline_terminal_results_total`, `pipeline_stage_duration_seconds`, `pipeline_stage_outcomes_total`, `pipeline_last_success_timestamp_seconds`, `pipeline_last_terminal_timestamp_seconds`, `pipeline_pending_work_items`, `pipeline_oldest_pending_age_seconds`, `pipeline_unresolved_observations`, `pipeline_decision_barriers_total`, `pipeline_decision_barrier_age_seconds`, `pipeline_terminal_ratio`.
- Reliability/durability (11): `reliability_restarts_total`, `reliability_replay_attempts_total`, `reliability_recovery_actions_total`, `reliability_unknown_in_flight_total`, `reliability_deduplications_total`, `reliability_journal_corruption_total`, `reliability_blob_corruption_total`, `reliability_fsync_failures_total`, `reliability_write_failures_total`, `reliability_journal_bytes`, `reliability_verified_blobs`.
- Experiment health (10): `experiment_paired_samples_total`, `experiment_paired_wins_total`, `experiment_ai_win_rate`, `experiment_cumulative_pnl_usd`, `experiment_cumulative_paired_pnl_delta_usd`, `experiment_mean_brier_score`, `experiment_forecast_coverage_ratio`, `experiment_model_cost_usd_total`, `experiment_model_cost_adjusted_edge_usd`, `experiment_model_cost_adjusted_edge_per_pair_usd`.
- Process/build (9): `process_last_run_runtime_seconds`, `process_resident_memory_bytes`, `process_peak_resident_memory_bytes`, `process_cpu_seconds_total`, `process_open_fds`, `process_build_version`, `process_build_commit_timestamp_seconds`, `process_build_dirty`, `process_build_identity`.

Every registry name is prefixed at runtime with `jev_polymarket_`; the short names above exist only to keep this ledger readable.

---

### Task 1: Freeze the 105-family schema and bounded dimensions

**Objective:** Make metric names, types, HELP text, labels, statuses/reasons/stages, provider/model pairs, and histogram buckets executable data rather than scattered conditionals.

**Files:**
- Create: `src/jev_trading_lab/polymarket/metrics_schema.py`
- Create: `tests/polymarket/test_metrics_schema.py`

**Functions/types:**

```python
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

METRIC_FAMILIES: tuple[MetricFamily, ...]
def metric_family(name: str) -> MetricFamily
def validate_labels(family: MetricFamily, labels: Mapping[str, str]) -> tuple[tuple[str, str], ...]
```

Encode the full catalog from design Sections 5–6. Provider/model validation must accept only OpenAI/`gpt-5-2025-08-07` and Jev/`jev-1.13.0`; Gamma/CLOB cannot carry `model_snapshot`. Histograms append structural `le` only during rendering. Validate registry uniqueness, lexical names, suffix/type rules, and fixed buckets at import/test time.

**RED tests:** exact 105-name equality with the coverage ledger; exact section counts; unknown label/name/stage/reason/status and provider/model cross-products rejected; no forbidden labels; exact bucket vectors.

**Commands:**

```bash
uv run pytest tests/polymarket/test_metrics_schema.py -q
uv run ruff check src/jev_trading_lab/polymarket/metrics_schema.py tests/polymarket/test_metrics_schema.py
uv run mypy src
```

**Commit:** `feat(polymarket): define bounded metrics schema`

**Review gate:** Implementer → schema/spec reviewer → quality/security reviewer.

---

### Task 2: Render and parse strict deterministic Prometheus text

**Objective:** Render validated samples with canonical decimals and independently parse the complete bytes before publication.

**Files:**
- Create: `src/jev_trading_lab/polymarket/metrics_render.py`
- Create: `tests/polymarket/test_metrics_render.py`

**Functions/types:**

```python
@dataclass(frozen=True, slots=True)
class Sample:
    family: str
    labels: tuple[tuple[str, str], ...]
    value: int | Decimal

@dataclass(frozen=True, slots=True)
class Histogram:
    family: str
    labels: tuple[tuple[str, str], ...]
    observations: tuple[Decimal, ...]

class ExpositionError(ValueError): ...

def canonical_decimal(value: int | Decimal) -> str
def render_exposition(samples: Iterable[Sample], histograms: Iterable[Histogram]) -> bytes
def validate_exposition(payload: bytes) -> int
```

Render all 105 HELP/TYPE pairs in family lexical order and samples in `(metric_name, labels)` order. Escape label text per Prometheus text rules, reject unexpected labels before escaping, use cumulative fixed buckets plus `+Inf`, exact `_sum`/`_count`, no explicit sample timestamps, one final newline, and no blank/duplicate declarations. `validate_exposition` must parse only the supported 0.0.4 subset and return unique series count. Reject duplicate series, nonfinite values, malformed escapes, wrong histogram counts, and 801 series before publication; 800 succeeds.

**RED tests:** canonical values (`0`, signed values, micro-unit decimals, no exponent, no `-0`); lexical output; labels escaped; NaN/infinities rejected; 800/801 boundary; malformed exposition rejected; histogram bucket/sum/count invariants.

**Commands:**

```bash
uv run pytest tests/polymarket/test_metrics_render.py -q
uv run ruff check src/jev_trading_lab/polymarket/metrics_render.py tests/polymarket/test_metrics_render.py
uv run mypy src
```

**Commit:** `feat(polymarket): render strict prometheus text`

**Review gate:** Implementer → exposition spec reviewer → quality/security reviewer.

---

### Task 3: Add strict append-only operational events and blobs

**Objective:** Persist raw operational facts needed for reconstruction without introducing mutable metric state.

**Files:**
- Create: `src/jev_trading_lab/polymarket/operations.py`
- Create: `tests/polymarket/test_operations.py`

**Functions/types:**

```python
class OperationKind(StrEnum):
    STARTUP = "startup"
    RUN_STARTED = "run_started"
    RUN_TERMINAL = "run_terminal"
    STAGE_TERMINAL = "stage_terminal"
    RECOVERY = "recovery"
    DEDUPLICATION = "deduplication"
    STORAGE_FAILURE = "storage_failure"
    PROCESS_SAMPLE = "process_sample"
    METRICS_GENERATION = "metrics_generation"

class OperationEvent(StrictModel):
    schema_version: Literal[1]
    event_id: Sha256
    occurred_at: datetime
    kind: OperationKind
    run_id: Sha256 | None
    semantic_id: Sha256 | None
    stage: Literal[...] | None
    provider: Literal["openai", "jev", "gamma", "clob"] | None
    status: bounded str
    reason: bounded str | None
    elapsed_nanoseconds: int | None
    exact_values: dict[str, int]
    evidence: BlobRefModel | None

@dataclass(frozen=True, slots=True)
class OperationCut:
    events: tuple[OperationEvent, ...]
    journal_bytes: int
    valid_blobs: int

def operation_event_id(payload_without_id: Mapping[str, object]) -> str
def append_operation(root: Path, event: OperationEvent, evidence: bytes | None = None) -> None
def load_operation_cut(root: Path, *, recover_tail: bool = False) -> OperationCut
```

Use `operations.jsonl` mode `0600`, `operation-blobs/`, canonical JSON, `O_APPEND|O_NOFOLLOW|O_CLOEXEC`, descriptor locks, full-write/EINTR loops, file `fsync`, and directory `fsync` on creation. Enforce event-kind-specific fields, bounded statuses/reasons/stages, nonnegative elapsed/value domains, stable SHA-256 identity, duplicate-identical idempotence, and duplicate-conflicting failure. Exception text, paths, IDs intended for metric labels, secrets, and preaggregated counters are forbidden.

**RED tests:** strict round trip; invalid event transition/fields; duplicate behavior; missing/wrong-size/wrong-hash operation blobs; unsafe ancestors/symlinks/FIFOs/ownership/mode; append/write/fsync fault injection; complete-line corruption fatal; optional torn final tail recovery returns discarded byte count but never parses it.

**Commands:**

```bash
uv run pytest tests/polymarket/test_operations.py -q
uv run ruff check src/jev_trading_lab/polymarket/operations.py tests/polymarket/test_operations.py
uv run mypy src
```

**Commit:** `feat(polymarket): add operational audit journal`

**Review gate:** Implementer → audit-model spec reviewer → durability/security reviewer.

---

### Task 4: Capture bounded process and build facts

**Objective:** Persist per-run process/build samples that survive restart and support the nine process families.

**Files:**
- Modify: `src/jev_trading_lab/polymarket/operations.py`
- Modify: `tests/polymarket/test_operations.py`

**Functions/types:**

```python
@dataclass(frozen=True, slots=True)
class ProcessSample:
    resident_memory_bytes: int | None
    peak_resident_memory_bytes: int | None
    user_cpu_nanoseconds: int
    system_cpu_nanoseconds: int
    open_fds: int | None
    probe_available: bool

@dataclass(frozen=True, slots=True)
class BuildIdentity:
    version_code: int
    commit_timestamp_seconds: int | None
    dirty: bool | None
    status: Literal["valid", "dirty", "mismatch", "unknown"]

def sample_process() -> ProcessSample
def package_version_code(version: str) -> int
def capture_build_identity(expected_commit: str | None) -> BuildIdentity
```

Use `/proc/self/statm` plus `os.sysconf("SC_PAGE_SIZE")` for current Linux RSS, `resource.getrusage(RUSAGE_SELF).ru_maxrss * 1024` for Linux peak RSS, `resource` CPU values converted once to integer nanoseconds, and `/proc/self/fd` count without following entries. Unsupported probes return `None`, never zero. Encode `0.1.0` as `1000`. Git details stay only in startup audit evidence; metrics expose timestamp/dirty/status, never SHA.

**RED tests:** unit normalization, unsupported `/proc`, wall-clock irrelevance, CPU delta persistence, SemVer encoding and invalid version rejection, mutually exclusive build statuses, no commit/path/PID in metric-facing fields.

**Commands:**

```bash
uv run pytest tests/polymarket/test_operations.py -q
uv run ruff check src/jev_trading_lab/polymarket/operations.py tests/polymarket/test_operations.py
uv run mypy src
```

**Commit:** `feat(polymarket): audit process and build samples`

**Review gate:** Implementer → process semantics reviewer → portability/security reviewer.

---

### Task 5: Make journal tail recovery explicit and audited

**Objective:** Recover only allowed torn final lines under the exclusive output lock and keep all complete corruption fatal.

**Files:**
- Modify: `src/jev_trading_lab/polymarket/store.py`
- Modify: `src/jev_trading_lab/polymarket/call_journal.py`
- Modify: `src/jev_trading_lab/polymarket/operations.py`
- Modify: `tests/polymarket/test_store.py`
- Modify: `tests/polymarket/test_unit6_restart_safety.py`
- Modify: `tests/polymarket/test_operations.py`

**Functions/types:**

```python
@dataclass(frozen=True, slots=True)
class JournalRecovery:
    journal_kind: Literal["results", "artifacts", "provider_calls", "completed_slots", "operations"]
    discarded_bytes: int

# store.py
def recover_store_tails(output_dir: Path) -> tuple[JournalRecovery, ...]

# call_journal.py
def recover_tail(self) -> JournalRecovery | None
```

Refactor readers so ordinary reads remain fail-closed; only explicit startup recovery may truncate. Recovery locks each journal exclusively, validates every complete prefix line and transition before mutation, truncates bytes after the last newline, `fsync`s the file, and `fsync`s the parent where required. A newline-terminated malformed line, middle corruption, invalid transition, or conflicting duplicate is fatal and unchanged. Append one `recovery(reason="partial_tail")` event per recovered journal after its journal is safe; include discarded count in exact values, not labels.

**RED tests:** recover each journal tail; audit exact discarded bytes/kind; no recovery of complete corruption; no unrelated file changes; second recovery idempotent; prior tests expecting implicit provider truncation updated to call explicit startup recovery.

**Commands:**

```bash
uv run pytest tests/polymarket/test_store.py tests/polymarket/test_unit6_restart_safety.py tests/polymarket/test_operations.py -q
uv run ruff check src/jev_trading_lab/polymarket/store.py src/jev_trading_lab/polymarket/call_journal.py src/jev_trading_lab/polymarket/operations.py tests/polymarket/test_store.py tests/polymarket/test_unit6_restart_safety.py tests/polymarket/test_operations.py
uv run mypy src
```

**Commit:** `feat(polymarket): audit explicit journal recovery`

**Review gate:** Implementer → corruption-behavior reviewer → durability/security reviewer.

---

### Task 6: Expose one fully verified accepted source cut

**Objective:** Validate and join retained authoritative state once under lock, without N-squared per-observation replay.

**Files:**
- Modify: `src/jev_trading_lab/polymarket/store.py`
- Modify: `src/jev_trading_lab/polymarket/call_journal.py`
- Create: `src/jev_trading_lab/polymarket/metrics_reconstruct.py`
- Create: `tests/polymarket/test_metrics_reconstruct.py`

**Functions/types:**

```python
# store.py
@dataclass(frozen=True, slots=True)
class VerifiedObservationCut:
    observations: tuple[ObservationReplay, ...]
    journal_bytes: Mapping[str, int]
    valid_blobs: int

def load_verified_observation_cut(output_dir: Path) -> VerifiedObservationCut

# call_journal.py
@dataclass(frozen=True, slots=True)
class ProviderCallRecord: ...
def snapshot(self) -> tuple[ProviderCallRecord, ...]

# metrics_reconstruct.py
@dataclass(frozen=True, slots=True)
class AcceptedSourceCut:
    observations: tuple[ObservationReplay, ...]
    provider_calls: tuple[ProviderCallRecord, ...]
    completed_slots: tuple[CompletedSlotRecord, ...]
    operations: OperationCut
    source_max_timestamp: datetime | None
    source_fingerprint: str
    journal_bytes: Mapping[str, int]
    blob_counts: Mapping[str, int]

def load_accepted_source_cut(output_dir: Path) -> AcceptedSourceCut
```

Reuse canonical validators and verified blob reads. Sort by semantic IDs only internally. Validate all joins, orphan/conflict rules, provider-call opportunity binding, completed-slot binding, and artifact predecessors. Compute `source_fingerprint` from canonical accepted records excluding `metrics_generation` events, so generation does not trigger itself. Never expose that digest in exposition.

**RED tests:** valid complete and empty cuts; deterministic filesystem enumeration; orphaned/cross-opportunity records; provider conflicts; missing/wrong-size/wrong-hash blobs; unsafe paths; source max timestamp; generation-only event leaves fingerprint unchanged; any domain/run/recovery event changes it.

**Commands:**

```bash
uv run pytest tests/polymarket/test_metrics_reconstruct.py tests/polymarket/test_store.py tests/polymarket/test_unit6_restart_safety.py -q
uv run ruff check src/jev_trading_lab/polymarket/metrics_reconstruct.py src/jev_trading_lab/polymarket/store.py src/jev_trading_lab/polymarket/call_journal.py tests/polymarket/test_metrics_reconstruct.py
uv run mypy src
```

**Commit:** `feat(polymarket): load verified metrics source cut`

**Review gate:** Implementer → source-hierarchy reviewer → corruption/performance reviewer.

---

### Task 7: Implement aggregation primitives and unknown propagation

**Objective:** Centralize exact sums, latest selection, histograms, ratios, and completeness tracking before family reducers.

**Files:**
- Modify: `src/jev_trading_lab/polymarket/metrics_reconstruct.py`
- Modify: `tests/polymarket/test_metrics_reconstruct.py`

**Functions/types:**

```python
@dataclass(slots=True)
class Aggregate:
    samples: list[Sample]
    histograms: list[Histogram]
    unknowns: Counter[str]

class ExactAccumulator:
    def add(self, value: int | None, *, unknown_reason: str) -> None: ...
    def complete_sum(self) -> int | None: ...

def micros(value: int) -> Decimal
def ratio(numerator: int, denominator: int) -> Decimal | None
def latest(records: Iterable[T], key: Callable[[T], tuple[datetime, str]]) -> T | None
def aggregate_source_cut(cut: AcceptedSourceCut, generation_time: datetime) -> Aggregate
```

Sums remain integers until unit conversion; declared-domain overflow returns unknown rather than clamp. Means/ratios require known positive denominators. Latest uses event timestamp then canonical semantic ID. `unknowns` increments only for applicable authoritative records with missing required input. Fixed impossible combinations are never materialized.

**RED tests:** known zero/no event/not applicable/unknown/unresolved/overflow are distinct; signed sums; deterministic ties; no partial complete aggregate; nonnegative generated ages; replay identities deduplicate.

**Commands:**

```bash
uv run pytest tests/polymarket/test_metrics_reconstruct.py -q
uv run ruff check src/jev_trading_lab/polymarket/metrics_reconstruct.py tests/polymarket/test_metrics_reconstruct.py
uv run mypy src
```

**Commit:** `feat(polymarket): add exact metrics reducers`

**Review gate:** Implementer → aggregation semantics reviewer → arithmetic quality reviewer.

---

### Task 8: Reconstruct all 28 trading and forecast families

**Objective:** Derive synchronized arm economics and forecasts from terminal results plus verified artifacts.

**Files:**
- Modify: `src/jev_trading_lab/polymarket/metrics_reconstruct.py`
- Modify: `tests/polymarket/test_metrics_reconstruct.py`

**Function:** `reduce_trading(cut: AcceptedSourceCut, out: Aggregate) -> None`.

Cover all 28 families in the ledger. Classify realized P&L by payout/exit evidence and unresolved marked P&L as unrealized despite source field names. Recompute quantity-weighted VWAP from exact entry notionals. Emit fee/spread/slippage zero only when explicitly known; absent decomposition is unknown. Cash requires authoritative account/cash evidence and is not inferred from repeated `final_cash`; omit until such a record exists. Initial capital is one frozen value per arm, never summed. Economic P&L/equity and paired economic delta require complete known costs. Coverage denominator is terminal eligible opportunities; Brier requires payout.

**RED tests:** fixture exact quantities/notionals/VWAP/fees/realized and unrealized P&L/capital/Brier/coverage/paired deltas; no-fill and abstain; explicit zero versus missing decomposition; unknown cost omits economic values but retains trading P&L; replay does not double count; int64 delta overflow is unknown.

**Commands:**

```bash
uv run pytest tests/polymarket/test_metrics_reconstruct.py -q -k 'trading or forecast or economics'
uv run pytest tests/polymarket/test_contracts.py tests/polymarket/test_paper.py -q
uv run ruff check src/jev_trading_lab/polymarket/metrics_reconstruct.py tests/polymarket/test_metrics_reconstruct.py
uv run mypy src
```

**Commit:** `feat(polymarket): reconstruct trading metrics`

**Review gate:** Implementer → economics metric reviewer → financial arithmetic/security reviewer.

---

### Task 9: Reconstruct all 14 LLM/provider families

**Objective:** Count durable attempts once and derive outcome, usage, cost, parse/citation, latency, and recovery metrics.

**Files:**
- Modify: `src/jev_trading_lab/polymarket/metrics_reconstruct.py`
- Modify: `tests/polymarket/test_metrics_reconstruct.py`

**Function:** `reduce_providers(cut: AcceptedSourceCut, out: Aggregate) -> None`.

Use provider intents for attempts, strict verified responses for token usage, and operational `stage_terminal` records for monotonic duration. Pending intent is not `unknown_in_flight` until recovery classifies it. Failed/abstained calls retain known billing. Overflow counts `cost_overflow` and unknown cost without a fabricated value. Omit Jev cached-token series. Parse checks occur only after transport/HTTP success; citation checks apply only to OpenAI. Model snapshot active requires validated runtime configuration represented by retained observations.

**RED tests:** each terminal status/reason; pending before and unknown after recovery; failed call with known cost; usage absent/invalid; overflow; approved model pairs only; no Jev cached series; histogram boundaries; duplicate call/result rejection.

**Commands:**

```bash
uv run pytest tests/polymarket/test_metrics_reconstruct.py -q -k 'llm or provider or token or cost'
uv run pytest tests/polymarket/test_ai.py tests/polymarket/test_unit6_restart_safety.py -q
uv run ruff check src/jev_trading_lab/polymarket/metrics_reconstruct.py tests/polymarket/test_metrics_reconstruct.py
uv run mypy src
```

**Commit:** `feat(polymarket): reconstruct provider metrics`

**Review gate:** Implementer → provider metric reviewer → billing/security reviewer.

---

### Task 10: Reconstruct all 15 market-data families

**Objective:** Derive per-attempt market health and verified book distributions without market identifiers.

**Files:**
- Modify: `src/jev_trading_lab/polymarket/metrics_reconstruct.py`
- Modify: `tests/polymarket/test_metrics_reconstruct.py`

**Function:** `reduce_market_data(cut: AcceptedSourceCut, out: Aggregate) -> None`.

Count each persisted Gamma page and each CLOB precheck/execution attempt separately. Map dynamic source reasons to bounded values/`other`. Recompute spread and two-point near-depth from verified levels; use latest gauges by event time and all unique books for histograms. Poll age is generation time minus latest terminal Gamma completion. Book age is response retrieval minus exchange/book timestamp.

**RED tests:** multipage Gamma counts; CLOB stages separate; timeout/transport/HTTP/content/malformed mappings; duplicate rows; spread/depth exact buckets; stale stage; latest tie-break; no market/token/status-code labels.

**Commands:**

```bash
uv run pytest tests/polymarket/test_metrics_reconstruct.py -q -k 'market or book or gamma or clob'
uv run pytest tests/polymarket/test_client.py -q
uv run ruff check src/jev_trading_lab/polymarket/metrics_reconstruct.py tests/polymarket/test_metrics_reconstruct.py
uv run mypy src
```

**Commit:** `feat(polymarket): reconstruct market data metrics`

**Review gate:** Implementer → market-data metric reviewer → cardinality/security reviewer.

---

### Task 11: Reconstruct all 12 pipeline and 11 reliability families

**Objective:** Report run/stage/pending/recovery/durability state from operational facts and accepted journals.

**Files:**
- Modify: `src/jev_trading_lab/polymarket/metrics_reconstruct.py`
- Modify: `tests/polymarket/test_metrics_reconstruct.py`

**Functions:**

```python
def reduce_pipeline(cut: AcceptedSourceCut, generation_time: datetime, out: Aggregate) -> None
def reduce_reliability(cut: AcceptedSourceCut, out: Aggregate) -> None
```

Run counters come from terminal run events; completed-slot replay is `replayed`, not economic work. Pending work is unmatched started run/provider/stage intent at the cut. Durations use monotonic elapsed nanoseconds only; interrupted durations become unknown latency. Decision barriers count once and age by pending/terminal state. Reliability uses explicit startup/recovery/dedup/corruption/storage events and source scan blob counts. Map journal byte gauges only to approved logical stages.

**RED tests:** success/failure/replayed/recovered; pending ages; terminal ratio omitted without denominator; barrier states; restart classes; dedup does not change economics; corruption reason mapping; fsync/write boundary mapping; valid/missing/corrupt blob scan behavior; replay durations not duplicated.

**Commands:**

```bash
uv run pytest tests/polymarket/test_metrics_reconstruct.py -q -k 'pipeline or reliability or recovery or corruption'
uv run ruff check src/jev_trading_lab/polymarket/metrics_reconstruct.py tests/polymarket/test_metrics_reconstruct.py
uv run mypy src
```

**Commit:** `feat(polymarket): reconstruct pipeline reliability metrics`

**Review gate:** Implementer → reliability spec reviewer → failure-semantics/security reviewer.

---

### Task 12: Reconstruct all 10 experiment-health families

**Objective:** Produce descriptive paired feasibility indicators without turning them into promotion criteria.

**Files:**
- Modify: `src/jev_trading_lab/polymarket/metrics_reconstruct.py`
- Modify: `tests/polymarket/test_metrics_reconstruct.py`

**Function:** `reduce_experiment(cut: AcceptedSourceCut, out: Aggregate) -> None`.

Classify paired samples as resolved/pending/unknown-cost/unscored. Wins require resolved known paired economic P&L; ties stay separate and out of win-rate denominator. Model-cost-adjusted edge equals paired trading delta minus AI-minus-control model cost and is omitted on unknown inputs. Marked unresolved P&L never counts as a win. Coverage includes valid abstain/no-fill forecasts.

**RED tests:** AI/control/tie; zero denominator omission; unresolved marked pair; unknown model cost; exact cumulative and per-pair edge reconciliation; duplicate replay excluded.

**Commands:**

```bash
uv run pytest tests/polymarket/test_metrics_reconstruct.py -q -k experiment
uv run ruff check src/jev_trading_lab/polymarket/metrics_reconstruct.py tests/polymarket/test_metrics_reconstruct.py
uv run mypy src
```

**Commit:** `feat(polymarket): reconstruct experiment metrics`

**Review gate:** Implementer → experiment semantics reviewer → financial quality reviewer.

---

### Task 13: Reconstruct snapshot health and process/build families

**Objective:** Complete the remaining 6 snapshot/exporter and 9 process/build families.

**Files:**
- Modify: `src/jev_trading_lab/polymarket/metrics_reconstruct.py`
- Modify: `tests/polymarket/test_metrics_reconstruct.py`

**Functions:**

```python
def reduce_process(cut: AcceptedSourceCut, out: Aggregate) -> None
def finalize_snapshot_health(
    cut: AcceptedSourceCut,
    generation_time: datetime,
    prospective_series: int,
    out: Aggregate,
) -> None
```

Emit schema version `1`, generation time, optional source max timestamp, prior audited generation outcomes, bounded unknown totals, and final series count. Process runtime uses latest terminal invocation by status; RSS/peak/FD are latest audited sample; CPU totals sum per-run deltas. Unsupported probe increments process unknown once for the applicable event. Build status emits exactly one value-1 series.

Resolve the `metrics_series` self-reference by rendering once without that sample, computing the final unique count including `metrics_series` itself, adding it, then validating the count is stable.

**RED tests:** empty cut; generation timestamp precision; source max omission; regeneration statuses; process probe omission/unknown; CPU restart accumulation; version `1000`; one-hot build identity; stable series self-count.

**Commands:**

```bash
uv run pytest tests/polymarket/test_metrics_reconstruct.py -q -k 'snapshot or process or build or series'
uv run ruff check src/jev_trading_lab/polymarket/metrics_reconstruct.py tests/polymarket/test_metrics_reconstruct.py
uv run mypy src
```

**Commit:** `feat(polymarket): complete process snapshot metrics`

**Review gate:** Implementer → catalog-completeness reviewer → process/privacy reviewer.

---

### Task 14: Validate destination and publish atomically

**Objective:** Preserve the last good snapshot through unsafe paths and every pre-rename failure.

**Files:**
- Create: `src/jev_trading_lab/polymarket/metrics_publish.py`
- Create: `tests/polymarket/test_metrics_publish.py`

**Functions/types:**

```python
@dataclass(frozen=True, slots=True)
class MetricsConfig:
    textfile_directory: Path
    file_mode: int = 0o644

class MetricsPublishError(RuntimeError): ...
class MetricsDurabilityError(MetricsPublishError): ...
class MetricsPathError(MetricsPublishError): ...

def resolve_metrics_config(output_dir: Path, explicit: Path | None) -> MetricsConfig
def open_validated_metrics_directory(config: MetricsConfig) -> int
def publish_metrics(config: MetricsConfig, payload: bytes) -> Path
def remove_stale_metric_temporaries(config: MetricsConfig) -> int
```

Explicit paths must be absolute; default becomes `output_dir.absolute()/"metrics"`. Walk every component descriptor-relative with `O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`; require pre-existence, local filesystem, producer write access, and safe ownership. On Linux parse `/proc/self/mountinfo` and reject network types including `nfs`, `nfs4`, `cifs`, `smb3`, `sshfs`, `fuse.sshfs`, `9p`, `ceph`, `afs`, and `davfs`; fail closed if mount identity cannot be established.

Before writing, call `validate_exposition`. Create `.jev_polymarket.prom.tmp.<pid>.<random>` using `O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC` in the open directory, write all bytes with EINTR/short-write handling, `fsync`, revalidate directory identity and destination ownership/type, `os.replace` descriptor-relative, then `fsync` directory. Pre-rename errors remove only the owned temporary and retain old destination. Directory-`fsync` failure after rename raises `MetricsDurabilityError` and leaves the new visible file. Cleanup removes only exact-pattern, owner-owned, regular files and follows no links.

**RED tests:** relative/network/symlink/foreign/FIFO/hardlink/replaced-directory rejection; file mode; stale cleanup safety; each write/flush-equivalent/fsync/rename/directory-fsync fault; old-file preservation; no partial destination; post-rename semantics.

**Commands:**

```bash
uv run pytest tests/polymarket/test_metrics_publish.py -q
uv run ruff check src/jev_trading_lab/polymarket/metrics_publish.py tests/polymarket/test_metrics_publish.py
uv run mypy src
```

**Commit:** `feat(polymarket): publish metrics atomically`

**Review gate:** Implementer → publication spec reviewer → filesystem security reviewer.

---

### Task 15: Coordinate recovery, skip detection, generation, and failure audit

**Objective:** Provide one lock-aware entry point that converts a valid source cut into the file without recursive generation events.

**Files:**
- Create: `src/jev_trading_lab/polymarket/observability.py`
- Create: `tests/integration/test_polymarket_metrics.py`

**Functions/types:**

```python
class GenerationStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    SKIPPED = "skipped"

@dataclass(frozen=True, slots=True)
class GenerationResult:
    status: GenerationStatus
    path: Path
    series: int | None
    source_fingerprint: str

# Caller already holds observation_lock.
def recover_and_generate_locked(
    output_dir: Path,
    metrics_config: MetricsConfig,
    *,
    startup: bool,
    now: Callable[[], datetime] = utc_now,
) -> GenerationResult

def generate_locked(
    output_dir: Path,
    metrics_config: MetricsConfig,
    *,
    now: Callable[[], datetime] = utc_now,
) -> GenerationResult
```

Startup path removes audited stale temporaries, explicitly recovers tails/provider in-flight state, appends startup/recovery facts, then loads the accepted cut. Compare its non-generation fingerprint to the most recent successful/skipped generation audit. Identical manual generation appends `skipped` and does not render/replace. A changed cut aggregates, renders, validates, publishes, then appends success. Failure appends bounded failure/storage facts if safe and re-raises; never mutates domain results or old `.prom`. Do not call `observation_lock` from these functions; integration owns one lock and avoids deadlock.

**RED tests:** first success; source-identical skip preserves bytes/mtime; generation event alone does not force publication; later domain event includes prior generation count; invalid source retains old file; no prior file on first fatal cut; publication failure is operational only.

**Commands:**

```bash
uv run pytest tests/integration/test_polymarket_metrics.py -q -k generation
uv run ruff check src/jev_trading_lab/polymarket/observability.py tests/integration/test_polymarket_metrics.py
uv run mypy src
```

**Commit:** `feat(polymarket): coordinate metrics generation`

**Review gate:** Implementer → lifecycle spec reviewer → recursion/failure security reviewer.

---

### Task 16: Persist exact market/provider/stage timing facts

**Objective:** Make currently absent call and stage measurements authoritative without changing provider or market semantics.

**Files:**
- Modify: `src/jev_trading_lab/polymarket/client.py`
- Modify: `src/jev_trading_lab/polymarket/ai.py`
- Modify: `src/jev_trading_lab/polymarket/pipeline.py`
- Modify: `tests/polymarket/test_client.py`
- Modify: `tests/polymarket/test_ai.py`
- Modify: `tests/integration/test_polymarket_live_boundary.py`

**Interfaces:**

```python
AuditSink = Callable[[OperationEvent], None]

# Inject monotonic_ns and audit_sink into PolymarketClient and provider-call orchestration.
# Record one raw stage_terminal event per Gamma page, CLOB precheck/execution attempt,
# provider transport attempt, and completed pipeline stage.
```

Measure immediately around transport invocation with `time.monotonic_ns`; never subtract UTC. Gamma emits one event per requested page, including empty/failing pages. CLOB distinguishes `precheck_book` and `execution_book`. Provider duration joins to durable call semantic ID and records even known failures; a crash without a terminal duration becomes unknown latency after recovery. Persist status/reason and exact elapsed only; no URL/status code/body/error string in event labels or fields used as labels. Existing test-only `event_sink` ordering remains separate or is replaced with a typed adapter without weakening tests.

**RED tests:** fake monotonic clock exact duration; wall-clock jumps; every attempt counted; interrupted call unknown; event write occurs at normal durability boundary; no changes to request bytes, call count, price-blind input, or credential screening.

**Commands:**

```bash
uv run pytest tests/polymarket/test_client.py tests/polymarket/test_ai.py tests/integration/test_polymarket_live_boundary.py -q
uv run ruff check src/jev_trading_lab/polymarket/client.py src/jev_trading_lab/polymarket/ai.py src/jev_trading_lab/polymarket/pipeline.py tests/polymarket/test_client.py tests/polymarket/test_ai.py tests/integration/test_polymarket_live_boundary.py
uv run mypy src
```

**Commit:** `feat(polymarket): audit call and stage timings`

**Review gate:** Implementer → timing/source reviewer → credential/boundary reviewer.

---

### Task 17: Integrate startup and terminal observe generation

**Objective:** Publish recovered state before network and publish terminal state once after completed-slot durability.

**Files:**
- Modify: `src/jev_trading_lab/polymarket/pipeline.py`
- Modify: `tests/integration/test_polymarket_live_boundary.py`
- Modify: `tests/integration/test_polymarket_metrics.py`

**Signature:** add `metrics_textfile_directory: Path | None = None` to `observe_markets` and resolve config before any journal mutation.

Inside the existing `observation_lock`:

1. validate metrics destination;
2. append `run_started` and process-start facts;
3. call `recover_and_generate_locked(..., startup=True)` before `PolymarketClient.poll` or provider transport;
4. run/recover observation work;
5. durably write terminal observations and `completed_slots.jsonl`;
6. append `run_terminal` and `process_sample` with monotonic runtime;
7. call `generate_locked` exactly once;
8. return summaries only after publication succeeds.

Completed-slot reuse emits replay/dedup events and still has one terminal generation. Metrics failure makes the command nonzero but does not alter committed economic data.

**RED tests:** startup publication precedes a network trap; all observe terminal classes regenerate once; completed-slot reuse does not duplicate economics; concurrent observes serialize and leave one valid file; missing/unsafe metrics directory fails before network/provider/domain write.

**Commands:**

```bash
uv run pytest tests/integration/test_polymarket_live_boundary.py tests/integration/test_polymarket_metrics.py -q -k 'observe or startup or concurrent'
uv run ruff check src/jev_trading_lab/polymarket/pipeline.py tests/integration/test_polymarket_live_boundary.py tests/integration/test_polymarket_metrics.py
uv run mypy src
```

**Commit:** `feat(polymarket): publish metrics around observe`

**Review gate:** Implementer → observe-order reviewer → concurrency/security reviewer.

---

### Task 18: Integrate replay and CLI/configuration

**Objective:** Generate the same snapshot for offline replay and expose only the approved directory setting.

**Files:**
- Modify: `src/jev_trading_lab/polymarket/pipeline.py`
- Modify: `src/jev_trading_lab/cli.py`
- Modify: `config/phase1.example.json`
- Modify: `tests/integration/test_polymarket_fixture_replay.py`
- Modify: `tests/integration/test_polymarket_cli.py`
- Modify: `tests/integration/test_polymarket_metrics.py`

**Signature:** add `metrics_textfile_directory: Path | None = None` to `replay_fixture`; add `--metrics-textfile-directory` to both CLI commands. Explicit CLI values must be absolute. Default uses absolute `<output-dir>/metrics`. Tests create the directory before invoking commands.

Replay acquires `observation_lock`, performs startup recovery/generation before fixture work, remains offline/no-credential, appends one bounded replay terminal event, records process sample, and regenerates once after terminal success/failure when source remains valid. Update `config/phase1.example.json` with only `metrics_textfile_directory`; add no scrape/server/dashboard settings.

**RED tests:** offline socket traps; default and explicit absolute destination; relative rejection; CLI nonzero on publication failure; duplicate replay leaves domain totals unchanged while replay/dedup counters change; help contains no HTTP/listen/port/Grafana/order flags.

**Commands:**

```bash
uv run pytest tests/integration/test_polymarket_fixture_replay.py tests/integration/test_polymarket_cli.py tests/integration/test_polymarket_metrics.py -q -k replay
uv run ruff check src/jev_trading_lab/polymarket/pipeline.py src/jev_trading_lab/cli.py tests/integration/test_polymarket_fixture_replay.py tests/integration/test_polymarket_cli.py tests/integration/test_polymarket_metrics.py
uv run mypy src
```

**Commit:** `feat(polymarket): publish metrics around replay`

**Review gate:** Implementer → replay/config reviewer → offline/privacy reviewer.

---

### Task 19: Add golden 105-family and corruption/crash acceptance coverage

**Objective:** Prove catalog completeness, deterministic bytes, fail-closed reconstruction, and last-good preservation end to end.

**Files:**
- Create: `tests/fixtures/prometheus/jev_polymarket.prom`
- Modify: `tests/integration/test_polymarket_metrics.py`
- Modify: `tests/polymarket/test_metrics_publish.py`
- Modify: `tests/polymarket/test_metrics_reconstruct.py`

Build a deterministic synthetic accepted cut that exercises every fixed dimension required for all 105 family declarations, all histogram structures, and representative samples without IDs/secrets. Inject fixed generation time. Check exact fixture bytes, one HELP/TYPE per family, lexical order, final newline, exact values, and `<500` expected initial series.

Add end-to-end failures for every write/file-fsync/rename/directory-fsync boundary; torn tails; malformed complete lines/middle corruption; missing/wrong-size/wrong-hash blobs; conflicting identities; unsafe symlink/FIFO/foreign files; stale temporary cleanup; directory replacement; 800/801 series. Assert pre-rename failures preserve the exact old digest/bytes and post-rename directory-fsync failure is reported honestly.

**Commands:**

```bash
uv run pytest tests/polymarket/test_metrics_schema.py tests/polymarket/test_metrics_render.py tests/polymarket/test_metrics_reconstruct.py tests/polymarket/test_metrics_publish.py tests/integration/test_polymarket_metrics.py -q
uv run ruff check src/jev_trading_lab/polymarket tests/polymarket tests/integration/test_polymarket_metrics.py
uv run mypy src
```

**Commit:** `test(polymarket): cover metrics crash boundaries`

**Review gate:** Implementer → acceptance-matrix reviewer → adversarial filesystem/security reviewer.

---

### Task 20: Produce and verify an actual `.prom` artifact

**Objective:** Exercise the shipped CLI, inspect the real destination, and optionally validate with `promtool` without making it a dependency.

**Files:**
- Modify: `README.md`
- Modify: `tests/integration/test_polymarket_metrics.py`

Document direct inspection and optional Node Exporter textfile configuration only. Explicitly state the application does not install/run Node Exporter, Prometheus, Grafana, alerting, HTTP metrics, or a daemon.

Run each command separately:

```bash
rm -rf .tmp/metrics-acceptance
mkdir -p .tmp/metrics-acceptance
mkdir -m 700 .tmp/metrics-acceptance/output
mkdir -m 755 .tmp/metrics-acceptance/output/metrics
uv run jev-lab polymarket replay --fixture tests/fixtures/polymarket/feasibility_cycle.json --output-dir .tmp/metrics-acceptance/output
uv run python -c 'from pathlib import Path; from jev_trading_lab.polymarket.metrics_render import validate_exposition; p=Path(".tmp/metrics-acceptance/output/metrics/jev_polymarket.prom"); b=p.read_bytes(); assert b.endswith(b"\n"); assert validate_exposition(b) <= 800; print(p, len(b), validate_exposition(b))'
```

If `promtool` is available:

```bash
promtool check metrics .tmp/metrics-acceptance/output/metrics/jev_polymarket.prom
```

If unavailable, record `promtool unavailable; stdlib validator passed`; do not install Prometheus. Run the replay a second time and assert economic/provider totals are unchanged while distinct replay audit counters advance correctly.

**Commands:**

```bash
uv run pytest tests/integration/test_polymarket_metrics.py tests/integration/test_polymarket_fixture_replay.py tests/integration/test_polymarket_cli.py -q
uv run ruff check README.md src tests
uv run mypy src
```

**Commit:** `docs(polymarket): document metrics textfile`

**Review gate:** Implementer → final artifact/spec reviewer → operational security reviewer.

---

### Task 21: Run final regression and scope audit

**Objective:** Verify the implementation as one system and reject accidental dependencies, listeners, high-cardinality fields, or live-trading scope.

**Files:** no planned production changes. Fix only evidenced defects in their owning files; each fix gets a focused commit and repeats both reviews.

Run each command separately:

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src
uv run pip-audit
uv run python scripts/policy_preflight.py
```

Then inspect:

```bash
git diff 1cbdb33..HEAD --stat
git grep -nE 'prometheus_client|start_http_server|HTTPServer|Grafana|listen_port|wallet|signing|private.*order' -- src tests config README.md
git status --short
```

Acceptance requires:

- exactly 105 registered families and no undeclared emitted family;
- all source values trace to accepted journals/blobs or are omitted/unknown;
- all labels bounded and no secret/high-cardinality value in `.prom`;
- deterministic replay and source-cut output;
- explicit startup recovery and corruption fail-closed behavior;
- atomic publication and last-good preservation;
- startup, observe, and replay hooks each tested;
- process probes and build identity tested;
- actual `.tmp/metrics-acceptance/output/metrics/jev_polymarket.prom` exists and passes the stdlib validator;
- `promtool` result recorded when available;
- no Prometheus client, HTTP endpoint, daemon, dashboard, alerting, or live-order capability;
- `.hermes-tmp.W3Xrv5` remains unmodified and uncommitted.

**Commit:** none when clean. If a defect is found, use a focused `fix(polymarket): ...` commit and rerun the owning task's spec and quality/security reviews before repeating this gate.

**Review gate:** Final integration spec reviewer → final quality/security reviewer. Both must approve the complete commit range.

## Plan self-review checklist

- [x] Approved design is named and all 105 families are allocated exactly once.
- [x] Implementation is explicitly blocked until the concurrent runtime fix is committed.
- [x] Exact production/test files, public functions, commands, and commit messages are given.
- [x] Every coding task uses RED/GREEN TDD and implementer → spec review → quality/security review.
- [x] Authoritative source hierarchy, joins, deduplication, unknowns, corruption, and cardinality are explicit.
- [x] Same-directory temp, short writes, file `fsync`, rename, directory `fsync`, and post-rename failure semantics are explicit.
- [x] Startup/recovery, observe, replay, process metrics, and actual `.prom` verification are sequenced.
- [x] Every implementation decision needed by a task is resolved in this plan.
- [x] No HTTP daemon, Prometheus client, Grafana, Node Exporter installation, alerting, or dashboard entered scope.
- [x] `.hermes-tmp.W3Xrv5` preservation is explicit.

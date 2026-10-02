# Jev Trading Lab — Design Specification

**Status:** Approved direction; review candidate 1  
**Date:** 2026-10-02  
**Repository:** `PyPavel/jev-trading-lab`  
**Mode:** Paper trading only  
**Experiment duration:** 12 weeks, with no early promotion or confirmatory-stream tuning

## 1. Decision and objective

Build a clean, evidence-first feasibility lab with two independent paper-trading streams:

1. liquid, non-sports Polymarket markets;
2. liquid S&P 100 stocks traded as 2–10 trading-day swings.

Each stream starts with $10,000 of isolated paper capital. An OpenAI research model creates a timestamped evidence brief. Jev makes a bounded, typed decision. Deterministic code owns arithmetic, eligibility, edge calculation, portfolio risk, simulated execution, exits, reconciliation, and evaluation.

The project answers one falsifiable question:

> Does an LLM-evidence + Jev-decision arm produce better out-of-sample, net-of-cost, risk-adjusted results than synchronized cash, passive, and deterministic controls?

The objective is not to demonstrate that an AI can place orders. Public repositories already prove that. The objective is to determine whether this architecture adds measurable economic value under causal, forward-only conditions.

## 2. Non-goals

The first release will not contain:

- live broker, wallet, private-key, CLOB order-submission, or withdrawal code;
- autonomous prompt or strategy rewriting;
- reinforcement learning, vector memory, or self-modifying agents;
- confidence-based position sizing;
- high-frequency or sub-second execution;
- a generic multi-venue framework;
- retrospective tuning of the confirmation run;
- claims of profitability from screenshots, selected trades, or gross returns.

A live-execution interface is intentionally absent, not merely disabled by configuration.

## 3. Foundational rules from the previous trader system

These are design invariants, not optional implementation advice.

### 3.1 Causality and data integrity

1. Every external fact carries `observed_at`, `available_at`, `source_uri`, retrieval status, and content hash.
2. A decision may use only data whose `available_at <= decision_cutoff`.
3. Cross-asset daily data is aligned by exchange-local trading date. Raw timestamps and array positions are never used as implicit joins.
4. Inputs used at entry are frozen. Rolling breakout levels, market definitions, membership lists, and evidence briefs are not silently recomputed when replaying an old decision.
5. Revised data never overwrites the value originally seen. Corrections append a superseding record.
6. Historical backtests are exploratory unless the full input set is point-in-time. The 12-week forward run is the confirmatory test.

### 3.2 Model boundaries

1. The research model receives facts and research questions, not the current strategy action.
2. Jev receives factual state, not a sentence asserting the desired conclusion.
3. Polymarket prices and the deterministic control’s action are withheld from both AI models until Jev has produced its independent probability judgment.
4. Every bounded choice includes `ABSTAIN`. `ABSTAIN`, `HOLD/FLAT`, provider failure, validation failure, and stale data remain distinct states.
5. A malformed response or provider failure can never become a trade, hold, or exit by default.
6. Code calculates all arithmetic: dates, returns, indicators, probability edge, fees, slippage, size, exposure, P&L, and performance statistics.
7. Jev confidence is a model feature, not a win probability. It does not control size in the confirmation experiment.
8. Model aliases are forbidden in a confirmation run. The manifest pins exact returned model/version identifiers. A mismatch halts new decisions.

### 3.3 Execution and state

1. Opportunity IDs, decision IDs, simulated order IDs, and fill IDs are deterministic and unique.
2. Duplicate events are idempotent no-ops across retries and restarts.
3. SQLite runs in WAL mode with foreign keys enabled. Transactional writes replace mutable portfolio JSON files.
4. Market snapshots and evidence are immutable content-addressed blobs; SQLite stores hashes and indexes.
5. Portfolio state, dashboard values, and reports are derived from the journal. No separate mutable source of truth exists.
6. Startup rebuilds and reconciles positions, cash, pending orders, and equity from the journal before scheduling work.
7. Runtime health exposes the exact Git commit, experiment-manifest hash, prompt hashes, provider model IDs, last successful cycle, and data ages.
8. “Committed” and “running” are separate states. Verification requires process version/read-back, not only a successful commit.

### 3.4 Research discipline

1. Costs are first-class and reported separately: fees, spread, slippage, borrow/dividends, inference, and infrastructure.
2. Every strategy arm, model call, prompt revision, parameter trial, and exclusion is recorded.
3. Negative experiments stay visible. No winner-only dashboard or report is permitted.
4. A feature must beat an identical-execution control before being credited with alpha.
5. No prompt, threshold, universe, data source, sizing, fill, or exit change is allowed in a running confirmation manifest.

## 4. Architectural choice

Use a Python 3.12 modular monolith with SQLite. Two market adapters share a small experiment kernel. This is intentionally not a microservice system.

```text
Polymarket adapter ─┐
                    ├─> point-in-time snapshot ─> evidence retrieval
S&P 100 adapter ────┘                                  │
                                                       v
                                      OpenAI structured evidence brief
                                                       │
                                      evidence validation + freeze
                                                       │
                                                       v
                                             Jev typed decision
                                                       │
                                                       v
                          deterministic eligibility / edge / risk / cost gates
                                                       │
                                                       v
                                           paper execution simulator
                                                       │
                                                       v
                                   append-only journal + derived evaluation
```

### 4.1 Module boundaries

- `core`: immutable domain types, IDs, clocks, manifests, hashes, invariants.
- `data`: provider ports, timestamp normalization, snapshots, calendars, point-in-time validation.
- `research`: document retrieval, structured brief generation, citation/evidence validation.
- `decision`: Jev adapter, schemas, version pinning, confidence metadata.
- `polymarket`: universe discovery, event clustering, order-book snapshots, resolution ingestion.
- `stocks`: S&P 100 universe, prices, corporate actions, filings, stock candidate generation.
- `risk`: portfolio limits and deterministic sizing.
- `execution`: venue-specific paper fill simulators only.
- `journal`: SQLite schema, append-only repositories, idempotency, reconciliation.
- `evaluation`: controls, metrics, bootstrap/permutation tests, final report.
- `scheduler`: explicit CLI entry points used by systemd timers.
- `dashboard`: read-only views derived from journal queries.

Domain code does not import web frameworks, provider SDKs, or database implementations.

## 5. Shared decision contract

### 5.1 Evidence brief

The OpenAI Responses API with web search produces strict structured output:

- question being answered;
- `as_of` timestamp and cutoff;
- facts, each with source URI, publication/retrieval time, and a short exact supporting quote;
- bullish/YES evidence;
- bearish/NO evidence;
- contradictions and unresolved facts;
- upcoming catalysts with dates;
- evidence-quality labels;
- explicit list of missing evidence.

The brief may not contain current Polymarket probabilities, deterministic-arm actions, desired trade direction, or position size.

A deterministic validator rejects briefs when:

- any required field is absent;
- a citation cannot be retrieved or its quote does not match the retrieved text;
- publication/retrieval time exceeds the decision cutoff;
- the brief exceeds its freshness limit;
- a source is duplicated through syndication without being identified as duplicate;
- the provider returns a different model ID than the manifest.

Validation establishes traceability, not truth. The raw documents and raw model response remain in the audit record.

### 5.2 Jev questions

Every call batches bounded questions over the same state:

- evidence sufficient: binary `Noul`;
- evidence contradictory or materially ambiguous: binary `Noul`;
- direction/action: market-specific choice with explicit `ABSTAIN`;
- setup quality: ordered score used for analysis only;
- Polymarket only: independent `P(YES)` as a `Noul`.

The exact model ID, question schema, criteria, thresholds, and prompt hashes are frozen in the manifest. Raw provider responses, probabilities, confidence, latency, cost, errors, and retries are stored.

### 5.3 Shared gates

No simulated order is created unless all gates pass:

- fresh market snapshot;
- valid evidence brief;
- valid Jev response;
- evidence sufficiency at least 0.70;
- ambiguity at most 0.30;
- chosen action probability at least 0.55;
- chosen action exceeds the second-highest action by at least 0.15;
- deterministic market eligibility and risk limits;
- no duplicate opportunity or existing conflicting position;
- sufficient paper cash and gross exposure capacity.

Thresholds remain frozen for the confirmation run. Their sensitivity is reported after the run, never used to rewrite the confirmatory result.

## 6. Polymarket stream

### 6.1 Universe and schedule

Run every 15 minutes. A market is eligible only when all conditions hold at the cutoff:

- active, non-sports, and not already resolving or disputed;
- scheduled resolution between one hour and 30 days away;
- at least $10,000 trailing 24-hour volume;
- at least $5,000 visible depth within two probability points of the best quotes;
- maximum bid/ask spread of four probability points;
- unambiguous resolution criteria and an identified resolution source;
- not a duplicate or derivative of another included market in the same event cluster.

The scan ranks eligible event clusters by liquidity and evaluates at most 20 clusters per cycle. This budget and ranking are frozen.

### 6.2 Event clustering

Markets sharing an underlying real-world event, mutually dependent outcomes, or nested thresholds receive one `event_cluster_id`. Cluster-level limits stop many correlated contracts from evading position limits. Event clustering is deterministic where metadata supports it; ambiguous clusters are rejected rather than guessed.

### 6.3 AI state and action

The evidence state includes the exact market question, rules, resolution source, cutoff, end time, and validated evidence. It excludes YES/NO prices, spread, volume-derived direction, current position, and control-arm output.

Jev returns `YES`, `NO`, or `ABSTAIN`, plus independent `P(YES)`. Afterward, deterministic code reveals the executable order book and computes:

- `edge_yes = p_yes - executable_yes_ask`;
- `edge_no = (1 - p_yes) - executable_no_ask`;
- all-in cost margin from fees, spread/slippage allowance, and a fixed three-point uncertainty reserve.

A side is actionable only when its edge is at least the larger of five probability points or twice its all-in execution friction plus the uncertainty reserve.

### 6.4 Position sizing

No model confidence enters the formula.

For the selected side:

- estimate seven-day midpoint volatility from causal snapshots;
- set dollar cost to `0.5% of equity / max(volatility, 0.05)`;
- cap position cost at 10% of stream equity;
- cap total cost across one event cluster at 10% of equity;
- cap simulated participation at 5% of visible depth and 1% of trailing 24-hour volume;
- reject orders below venue minimum size.

This formula targets smaller positions in unstable markets while maintaining a hard worst-case capital cap.

### 6.5 Paper fills

A buy fills only against a captured executable ask and only up to captured depth. Slippage consumes successive captured levels. Unfilled quantity is cancelled at the end of the cycle; no optimistic partial fill is invented. A fill cannot use a quote timestamp later than the order timestamp.

### 6.6 Exit policy

Positions are reassessed every cycle using fresh data. Exit at the first applicable condition:

1. market resolution or invalidation;
2. current independently recomputed edge is zero or negative;
3. Jev produces the opposite directional action through a fresh validated brief;
4. midpoint has moved 15 probability points against entry;
5. midpoint has moved 10 probability points in favor of entry;
6. seven calendar days have elapsed;
7. liquidity no longer supports a modeled exit; in that case mark the position illiquid and value it conservatively at the executable bid rather than inventing a fill.

Exit simulation uses executable bid depth. Resolution uses official Polymarket outcome data and remains separate from trade fills.

### 6.7 Controls

Synchronized controls use the identical eligible event-cluster set and cutoff:

- **Cash:** no positions; P&L baseline.
- **Passive forecast:** executable midpoint/market probability as the no-model prediction benchmark, evaluated by Brier score and log loss. It is not presented as a tradable portfolio.
- **Deterministic arm:** chooses YES when causal price momentum over 24 hours is positive and NO when negative, abstaining inside a two-point dead band. It uses identical sizing, costs, fills, exits, and risk limits.
- **AI arm:** LLM evidence + Jev decision.

## 7. Stock swing stream

### 7.1 Universe and schedule

Freeze the current S&P 100 constituent snapshot at experiment registration and commit its source and hash. Because the confirmatory run is forward-only, this avoids retroactive membership leakage. Membership changes during the 12 weeks are logged but do not change the frozen universe.

Run once after the official US market close. Candidate generation uses only information available by the cutoff. Rank stocks by the sum of absolute standardized five-day return and standardized volume surprise; evaluate the top 20 each day. All arms receive the same candidates.

Primary market data is raw daily OHLCV plus corporate-action records. Adjusted series may be used for indicators only after point-in-time adjustment logic is tested. Simulated fills and cash flows always use raw prices with explicit splits and dividends.

### 7.2 Evidence and state

The evidence brief emphasizes the preceding seven calendar days:

- SEC filings and issuer releases;
- earnings and guidance;
- material industry or regulatory news;
- confirmed catalysts;
- arguments for and against the move;
- thesis breakers and missing evidence.

Jev also receives deterministic numeric facts: causal returns, ATR, trend position, volume surprise, realized volatility, next known earnings date, sector, and current exposure. It does not receive the deterministic arm’s action.

Jev returns `LONG`, `SHORT`, or `ABSTAIN`. `ABSTAIN` creates no position. The confirmation experiment allows paper shorts because S&P 100 names are generally liquid; the simulator nevertheless charges conservative borrow and dividend costs.

### 7.3 Position sizing and portfolio limits

- initial equity: $10,000;
- risk budget per position: 0.5% of current equity;
- stop distance: two 14-day ATRs from entry;
- shares: risk budget divided by stop distance;
- hard cap: 10% gross equity per position;
- hard cap: 50% gross portfolio exposure;
- hard cap: 25% gross exposure per GICS sector;
- maximum five concurrent positions;
- no confidence-based scaling or pyramiding.

Short positions accrue a conservative 0.50% annualized borrow charge plus dividend liability. A borrow-locatability failure in the data model causes `ABSTAIN`, not a free short.

### 7.4 Fill and exit policy

A decision made after day `D` close enters at day `D+1` official open plus adverse slippage. It never fills at the already-known close. Entries are cancelled if the next open is absent, stale, or gapped more than 10% from the decision close.

Exit at the first applicable condition:

1. two-ATR stop;
2. three-ATR profit target;
3. ten trading days elapsed;
4. fresh Jev direction flips;
5. security leaves trading, undergoes a corporate action that the simulator cannot model, or loses valid pricing.

Daily bars cannot establish intraday ordering when both stop and target cross. The simulator applies the adverse stop-first assumption. Gaps through stops fill at the open, not the stop price.

### 7.5 Transaction costs

Every entry and exit charges:

- half of the observed or modeled bid/ask spread;
- five basis points of additional slippage per side;
- SEC/TAF fees where applicable;
- borrow and dividend costs for shorts;
- zero commissions unless the selected paper broker later documents a commission.

Cost assumptions receive ±50% sensitivity analysis in the final report.

### 7.6 Controls

- **Cash:** zero-return baseline.
- **Passive:** SPY total return, with the same experiment dates and explicit dividends.
- **Deterministic arm:** `LONG` when close is above 50-day SMA and both 20-day and five-day returns are positive; `SHORT` for the exact inverse; otherwise `FLAT`. Rank by absolute 20-day return. It uses identical candidate set, sizing, costs, fills, exits, and portfolio limits.
- **AI arm:** LLM evidence + Jev decision.

## 8. Experiment protocol

### 8.1 Preregistration manifest

Before the first confirmatory decision, commit a machine-readable manifest containing:

- experiment start/end and timezone;
- universe snapshots and hashes;
- exact market filters and candidate limits;
- exact model IDs, question schemas, prompt hashes, and thresholds;
- data providers and fallback policy;
- cost, sizing, fill, and exit rules;
- random seeds;
- primary and secondary metrics;
- minimum sample requirements;
- statistical tests and promotion gates;
- code commit deployed at start.

Any change creates a new exploratory run ID. The original confirmation run continues unchanged or is formally invalidated; it is never silently migrated.

### 8.2 Units of analysis

- Polymarket: independent `event_cluster_id`, not repeated scans or related contracts.
- Stocks: position-level outcome, with uncertainty estimated using week-level blocks to preserve cross-sectional and serial dependence.
- Holds/abstentions are included as zero-trade outcomes for eligible opportunities in intention-to-treat comparisons.

### 8.3 Primary metrics

Per stream:

1. treatment-minus-deterministic net P&L per eligible opportunity, expressed in risk units (`R`);
2. treatment-minus-deterministic portfolio return over the exact run;
3. maximum drawdown and 95% expected shortfall;
4. profit factor and turnover;
5. total modeled market costs and model/API costs.

Polymarket additionally reports Brier score and log loss versus executable market probability. Stocks report directional Brier score for the frozen outcome definition: positive versus non-positive return from modeled entry to the earlier of modeled exit or day ten.

### 8.4 Statistical analysis

- Use stationary/block bootstrap confidence intervals; never treat observations as independent bars.
- Use paired comparisons because arms share opportunities and execution assumptions.
- Apply Holm correction to the two primary stream hypotheses.
- Report point estimates, 90% and 95% intervals, raw sample sizes, independent-cluster counts, and effective block counts.
- Run cost, fill, and threshold sensitivity only after the frozen result is computed; label it exploratory.
- Preserve all attempted exploratory variants to expose multiple testing.

### 8.5 Minimum evidence and promotion gate

A stream is **inconclusive** unless the 12 weeks complete and it records at least 100 independent resolved opportunities. No early promotion is allowed.

A stream passes feasibility only if all conditions hold:

1. AI-minus-deterministic net expectancy is at least +0.03R per eligible opportunity.
2. The Holm-adjusted 95% block-bootstrap interval for that difference is entirely above zero.
3. AI total return exceeds cash and its passive benchmark over the run.
4. Maximum drawdown is no more than two percentage points or 10% relatively worse than deterministic control, whichever is stricter.
5. 95% expected shortfall is no more than 0.02R per opportunity worse than control.
6. The AI arm retains at least 60% of deterministic-arm opportunity exposure; otherwise the result is classified as inactivity, not validated alpha.
7. At least 99% of scheduled cycles produce a terminal audited status, including explicit error/abstain states.
8. No causality, model-version, duplicate-fill, reconciliation, or risk-limit violation occurs.
9. Conclusions remain directionally unchanged under +50% modeled execution costs.

Passing authorizes only a separately designed, capped live pilot. It does not authorize live trading automatically.

Jev probabilities may influence future sizing only after at least 250 independent outcomes, expected calibration error no greater than 0.05, and Brier score at least 10% better than the relevant baseline on untouched data.

## 9. Persistence model

SQLite tables are append-oriented:

- `experiment_runs` and `manifests`;
- `provider_observations` and immutable `snapshot_blobs`;
- `opportunities` and `event_clusters`;
- `evidence_documents`, `evidence_briefs`, and `evidence_validation`;
- `model_calls` and `decisions`;
- `paper_orders`, `paper_fills`, and `position_events`;
- `cash_events`, `corporate_actions`, and `resolutions`;
- `equity_snapshots`;
- `cycle_runs`, `health_events`, and `violations`.

Corrections append a new event linked by `supersedes_id`. Destructive updates to economic events are forbidden. Derived tables or views may be rebuilt from the journal.

SQLite constraints enforce:

- unique opportunity/arm decision;
- unique provider fill identity;
- one active manifest per confirmation run;
- nonnegative costs and sizes;
- valid state transitions;
- hash presence for every model input and evidence artifact.

## 10. Scheduling and operation

Every scheduled action maps to a real version-controlled Python CLI entry point. systemd user timers call those scripts directly. No cron prompt acts as production logic.

- Polymarket scan: every 15 minutes.
- Stock scan: once after official close, using an exchange calendar rather than a weekday check.
- Reconciliation: at startup and after every cycle.
- Evaluation snapshot: daily.
- Final confirmatory report: after the fixed end date and resolution/sample gate.

A bounded retry policy handles 429/5xx responses with jitter. Authentication, validation, stale-data, model-version, and insufficient-funds failures are not blindly retried. Provider outages produce audited abstentions.

## 11. Dashboard and reporting

The read-only dashboard displays:

- exact run/commit/manifest/model versions;
- cycle health and data ages;
- opportunity funnel: discovered, eligible, researched, validated, decided, traded;
- every arm side-by-side;
- gross and net P&L with cost decomposition;
- equity, drawdown, exposure, and concentration;
- calibration and confidence buckets;
- abstentions, failures, stale rejections, and duplicate suppressions;
- all exploratory variants, not only the best result.

Dashboard values come only from journal queries. Every headline number links to its underlying opportunities, decisions, and fills.

## 12. Security and safety

- API keys live outside the repository in a mode-0600 environment file or OS credential store.
- Secrets, authorization headers, raw cookies, signed URLs, and private keys are prohibited in logs and evidence blobs.
- Network clients use host allowlists and bounded response sizes.
- Evidence and web content are untrusted data, never executable instructions.
- SQLite and blob directories use least-privilege file modes and daily backups.
- The initial dependency graph contains no exchange execution SDK requiring signing credentials.
- CI scans committed files for secrets and verifies that no live-order adapter exists.

## 13. Testing strategy

### Unit and property tests

- exact ID/hash stability;
- timezone and market-calendar boundaries;
- date-keyed multi-asset joins;
- causal cutoff enforcement;
- frozen feature baselines;
- Jev response parsing and explicit abstention;
- malformed/provider-error behavior;
- sizing, exposure, sector, and cluster limits;
- cost and adverse fill calculations;
- SQLite constraints and idempotency.

### Contract tests

Recorded fixtures validate provider schema adapters without live markets. Separate opt-in smoke tests verify current APIs and record model IDs, but never place orders.

### Replay and restart tests

The same immutable snapshot replayed twice must produce one economic event. Killing the process after any journal write and restarting must yield the same cash, positions, and equity as uninterrupted execution.

### No-lookahead tests

Fixtures place distinctive future values, filings, resolutions, and price bars after the cutoff. The resulting decision input and action must remain unchanged. Stock fills must occur no earlier than the next open. Polymarket fills must use only contemporaneous captured depth.

### Statistical tests

Port the block bootstrap, paired permutation, Brier/log-loss, and multiple-testing controls from the audited reference projects, retaining MIT attribution.

## 14. Reuse policy

Selective adaptation is preferred over whole-project forks. Exact copied or materially adapted code retains source comments and is listed in `THIRD_PARTY_NOTICES.md` with upstream URL, commit, file, and MIT notice.

Planned reuse:

- `jevymarket`: Jev adapter patterns, research brief schema concepts, SQLite decision logging, Polymarket discovery.
- `jev_bitcoin_backtest`: causal simulator patterns, explicit cost objects, no-lookahead tests, block bootstrap, permutation tests, multiple-testing correction.
- `jeeva`: stale-decision rejection, position recheck/reconciliation, decision lifecycle and audit persistence concepts.
- `jev-perp-bot`: WAL journal, duplicate-fill constraint, deterministic risk state, cost/P&L decomposition.
- `jev-trade-cc`: point-in-time SEC evidence, frozen baselines, jackknife robustness, prompt anti-anchoring rules.
- `jev-trader`: only the small provider-neutral decision interface pattern; none of its forced-action or simulated maker-fill policy.

Rejected upstream behavior is documented in `docs/research/reuse-audit.md`.

## 15. Review and change control

Before implementation planning, the design receives independent reviews from:

1. investment research;
2. financial analysis;
3. statistics/quant methodology;
4. risk/adversarial operations;
5. software architecture;
6. a second financial review after corrections.

Review findings are stored under `docs/reviews/`. Blocking findings must be fixed or explicitly rejected with evidence. The final design must contain no `TBD`, placeholder, contradictory threshold, or unassigned safety decision.

## 16. Acceptance criteria for the design phase

The design phase is complete when:

- the private GitHub repository exists and is cloneable;
- lessons and reuse decisions are documented with pinned upstream commits and licenses;
- independent review rounds have no unresolved blocking findings;
- the specification is internally consistent and free of placeholders;
- the revised specification and reviews are committed and pushed;
- the user reviews the written specification before implementation planning begins.

# Jev Trading Lab — Design Specification

**Status:** Review candidate 4  
**Date:** 2026-10-02  
**Mode:** Paper only; live-order/signing code prohibited  
**Phase 1:** 12-week preregistered feasibility pilot; no early promotion  
**Phase 2:** Separate confirmatory run only if Phase 1 justifies it

## 1. Objective and permitted claim

Build two independent paper streams: liquid non-sports Polymarket contracts and liquid S&P 100 stocks held 2–10 trading days. A deterministic retriever freezes evidence; OpenAI converts only those documents into a structured brief; Jev makes bounded judgments; code owns arithmetic, risk, fills, accounting, and evaluation.

For each stream Phase 1 asks: does the AI arm produce better paired intention-to-treat **economic P&L** than a synchronized deterministic arm under identical eligibility, capital limits, fills, and direct-cost accounting?

Phase 1 may conclude `PROMISING`, `NO_EVIDENCE`, `OPERATIONALLY_INFEASIBLE`, or `INCONCLUSIVE`. It cannot establish durable alpha or authorize live trading. A later confirmatory claim requires a new frozen manifest, at least 26 completed weekly blocks, and for Polymarket at least 100 resolved clusters observed over at least 26 weeks. Cutoff/extension rules are frozen before that run.

## 2. Non-goals

No broker, wallet, private-key, signed-order, withdrawal, or live CLOB adapter; no model-side browsing; no self-modification; no confidence sizing; no HFT; no distributed system or generic venue framework; no retrospective tuning; no winner-only reporting. Live execution is absent, not disabled.

## 3. Accounts, P&L, and risk unit

Each tradable arm gets an isolated synchronized $10,000 account keyed by `(run_id, stream, arm, currency)`. Arms never share cash, positions, fills, limits, or costs.

- Polymarket: `AI`, `DETERMINISTIC`, `CASH`.
- Stocks: `AI`, `DETERMINISTIC`, `CASH`, `EXPOSURE_MATCHED_PASSIVE`.
- Non-tradable forecast baselines have no account.

Uninvested cash accrues the same point-in-time three-month Treasury yield. Stock passive holds 50% OEF total-return exposure and 50% interest-bearing cash, rebalanced month-end with the same next-open/cost rules. Full OEF and SPY are context only.

**Trading P&L** deducts spread, slippage, statutory/venue fees, borrow, dividends owed, and execution costs. **Economic P&L** further deducts operating costs. Failed/abstained calls keep billed cost. Actual OpenAI, Jev, evidence retrieval, paid evidence data, and incremental AI infrastructure costs debit the AI arm only. Deterministic-specific costs debit the deterministic arm. Truly shared market-data, scheduler, storage, and reporting costs split 50:50 between AI and deterministic within each stream; the ratio is frozen before registration. Every posting carries `account_id`. Direct costs attach to their opportunity. Remaining costs attach to the UTC enrollment week, then equally to that week's registered opportunities. A zero-opportunity week's amount remains an arm-scoped weekly residual in account economic P&L and the weekly policy-value vector. For each arm, opportunity allocations plus weekly residuals reconcile exactly to its account economic P&L; stream totals reconcile across arms. No cost disappears. Development labor is reported separately with break-even AUM; it is not debited from paper capital.

`R0 = $50` (0.5% of initial capital), fixed for the run. Dynamic equity may change size, but paired outcomes always divide by R0.

Authoritative realized P&L:

- Polymarket: `q × (exit_or_payoff − entry_vwap) − entry_fees − exit_fees`.
- Stock long: `q × (exit − entry) + dividends − market_costs`.
- Stock short: `q × (entry − exit) − borrow − dividend_liability − market_costs`.

Unresolved Polymarket quantity marks at executable bid depth; quantity beyond depth marks at zero and is flagged illiquid. Reports separate realized/unrealized P&L, cash yield, dividends, borrow, spread/slippage, statutory fees, model, data, and infrastructure cost.

Total return is ending marked economic equity / initial equity − 1. Turnover is total absolute executed notional / average marked equity. Profit factor is gross positive realized trade P&L / absolute gross negative realized trade P&L; no losses yields `null`.

## 4. Mandatory invariants from prior systems

1. Every observation stores `published_at`, first successful `retrieved_at`, source URI, status, and hash. Availability means first retrieval; metadata never backdates it.
2. Cross-asset data joins by exchange-local date, never array position/raw timestamp.
3. Entry features, universe, cluster membership, rules, evidence, and model input are immutable. Corrections append supersession.
4. Models see facts, never desired/control action. Polymarket price is hidden until Jev emits independent P(YES).
5. `ABSTAIN`, operational errors, stale input, and `HOLD/FLAT` remain distinct.
6. Code owns all numerical calculations. Jev confidence is not win probability and never sizes Phase 1.
7. SQLite is the only economic source of truth; dashboards derive from it. No portfolio JSON.
8. Stable semantic IDs and database constraints enforce idempotency.
9. Runtime identity proves commit, dirty state, lockfile, schema, manifest, prompts, and executable. Commit alone is not deployment proof.
10. Costs, attempts, exclusions, and negative results remain visible. No run changes after registration.
11. Signals with leakage, degenerate labels, no cost model, or no consumer are retired.

## 5. Architecture

Python 3.12 modular monolith, one host, local SQLite, content-addressed artifacts.

Modules: `core` (fixed-point types/IDs/manifest), `application` (use cases, stages, transactions, leases), `data` (providers/calendars), `evidence` (safe retrieval/manifests), `research` (OpenAI, tools disabled), `decision` (Jev/pinning), `polymarket`, `stocks`, `controls` (forward-cycle control signals), `risk`, `execution` (paper only), `persistence` (journal/ledger/reconciliation), `evaluation` (read-only statistics), `reporting` (read-only static dashboard), and thin `cli` entry points. Core imports no provider SDK, DB, or web framework. Evaluation never backfills decisions.

## 6. Causal evidence pipeline

For each opportunity:

1. persist eligibility snapshot `t0`;
2. retrieve allowlisted documents/APIs and store exact bytes, normalized text, hashes, publication/retrieval times;
3. freeze ordered `evidence_input_manifest` and `input_cutoff=t1`;
4. call OpenAI with tools disabled and only frozen documents; completion `t2`;
5. validate brief; call Jev over same frozen brief; completion `t2j`;
6. capture one common executable snapshot for all arms at `t3`;
7. evaluate arm signals and append intents at `t4`.

Require `t0 <= retrieved_at <= t1 < t2 <= t2j <= t3 <= t4`. Retries reuse identical evidence/input hashes. New retrieval is a new opportunity/episode, never a retry.

Retriever permits HTTPS only. For every hop it reapplies scheme/port/hostname allowlist, credentials, redirect, byte, DNS/IP and MIME checks; rejects mixed public/private answers and IPv4-mapped private ranges; connects only to a validated resolved IP while preserving/verifying original Host, SNI and certificate; verifies connected peer IP remains in that set. It blocks loopback/private/link-local/multicast/metadata IPs, unsupported types, excess redirects/time/bytes/documents, and decompression bombs. Model input is explicitly delimited untrusted data.

OpenAI strict schema records facts with document hash, URI, publication/retrieval times and exact normalized quote span; arguments for/against, contradictions, missing evidence, catalysts, and quality. Quote validation uses stored document version and normalized Unicode spans; it proves provenance, not truth.

Jev batches: evidence sufficient (Noul), materially ambiguous (Noul), action including `ABSTAIN`, setup quality (analysis only), Polymarket P(YES), and stock P(D+1 common open to D+10 close total return > 0). Forecast probabilities are distinct from action confidence.

## 7. Model and failure policy

Manifest pins provider, endpoint/API version, SDK/lock version, request snapshot, expected returned ID, schema, temperature, top-p, supported seed, and tools. Store raw request/response and provider fingerprints. Aliases and model fallback are forbidden. Registration records a stable comparable provider deployment fingerprint/revision when available, and every response must match it. Changed/retired IDs or fingerprints, or a missing required fingerprint, append `VERSION_MISMATCH` and end/invalidate the run; historical outputs replay from storage. If a provider cannot expose stable comparable revision metadata, it is ineligible for a registered run.

Model result and final arm disposition are separate immutable records. Model results are `ACTION`, `HOLD_FLAT`, `MODEL_ABSTAIN`, `PROVIDER_ERROR`, `VALIDATION_ERROR`, `STALE_INPUT`, `VERSION_MISMATCH`, `DEADLINE_EXCEEDED`, or `BUDGET_EXHAUSTED`. After deterministic gates, every `(opportunity_id, arm)` gets exactly one final disposition: `ORDER_INTENT_CREATED`, `NO_ACTION`, `RISK_REJECTED`, `CAPACITY_REJECTED`, `OPERATIONAL_FAILURE`, or `BASELINE_RECORDED`. Fill/cancel/exit lifecycle belongs to order/position events. Only a valid `ACTION` may create an AI order intent. Non-trade dispositions contribute zero trading P&L and actual attributable cost.

`entry_enabled` and `position_management_enabled` are separate. Model/evidence failures block entries and model exits only. Stops, targets, time exits, resolution, corporate actions, accrual, marks, and reconciliation continue. Missing exit data creates `EXIT_PENDING_DATA` and conservative valuation, never a fill.

Common gates: fresh market snapshot, registered opportunity, arm-local capital/risk, no duplicate/conflict, complete cost inputs, valid runtime identity. AI-only gates: valid evidence/Jev; sufficiency >=0.70; ambiguity <=0.30; action probability >=0.55; margin over runner-up >=0.15. Each arm uses only its own signal for entry/signal exits.

## 8. Polymarket

### 8.1 Universe, identity, and budget

Scan each 15-minute slot. Eligibility: accepted frozen non-sports provider category; active; resolution 1 hour–30 days away; >=$10,000 trailing 24h USD volume; >=5,000 visible contracts within two points of best quote; <=4-point spread; required resolution fields/source; deterministic unambiguous cluster.

Manifest freezes categories/exclusions, normalized cluster inputs, algorithm/version, and tie-breakers. Cluster membership persists before model calls and is never rewritten. Choose one contract per cluster by highest 24h USD volume, then qualifying contract depth, then immutable market ID.

One cluster creates one Phase-1 opportunity at its first eligible slot; later scans manage it but create no reentry/observation. `opportunity_id=hash(run,stream,cluster,first_slot)`, never arm.

Budgets include entry and recurring position-review work: maximum 3 new opportunities/cycle, 20/day, 3 concurrent requests, 3 attempts/stage, frozen provider-call, input-token, output-token, evidence-byte, concurrency, and dollar quotas, a ten-minute cycle deadline, and $3/day variable AI spend. Before registration, quotas are deterministically partitioned by `(UTC day, stream, entry_or_review)` pool; immutable scheduled slot then semantic subject ID defines priority. Before retrieval/provider invocation, one short transaction atomically reserves worst-case units in every applicable dimension. Durable usage/billing converts reservations to actuals and releases only verified unused units. Unknown in-flight attempts retain worst-case reservations until recovery resolves them. Reservation failure yields AI `BUDGET_EXHAUSTED`; deterministic/baseline arms continue. Pre-run p99 rehearsal must fit deadline or limits decrease before registration.

### 8.2 Edge, sizing, fills

After Jev p_yes, capture one common book. Reject if older than 60 seconds at intent creation. For proposed quantity compute side-specific entry VWAP from captured levels. Gross edge is `p_yes − YES_VWAP` or `(1−p_yes) − NO_VWAP`. Entry VWAP already contains spread.

Convert entry fee, expected exit fee, and conservative exit slippage to probability points. Trade only if:

`gross_edge >= max(0.05, 2 × (entry_fee_pp + exit_fee_pp + exit_slippage_pp) + 0.03)`.

Solve size/edge jointly because VWAP changes with quantity.

Prices/probabilities are decimals on [0,1] for a $1 resolution payoff. `sigma_p` is frozen-estimator seven-day standard deviation in USD per contract; five percentage points is `0.05 = $0.05/contract`. Let B=0.5% current equity, and let `D_side,2pt` be side-specific integer contract quantity captured within the two-point price range. Define Q as positive integer quantities q satisfying: q <= floor(B/max(sigma_p,$0.05/contract)); the edge inequality above evaluated with `VWAP(q)` and explicit entry/exit fee/slippage probability-point functions; entry cost/cluster cost <=10% equity; portfolio gross <=50%; max five clusters; q <= floor(0.05×D_side,2pt); and entry notional+fees <=1% trailing 24h USD volume. Set q*=max(Q); no trade when Q is empty. Store raw/normalized units. Under a 50% adverse depth haircut, each captured level quantity becomes `floor(original_quantity/2)` before recomputing depth, VWAP, Q, fills, and exits.

Fills consume only captured ask levels and quantities; remainder cancels. Fill identity includes order, snapshot, level, sequence. Quote cannot postdate order.

Common exits: resolution/invalidation; 15 points adverse; 10 points favorable; seven days; liquidity failure without optimistic fill. AI signal exits use immutable recurring reviews every 15-minute Polymarket position-management slot: `position_review_id=hash(position_id,scheduled_review_slot,policy_version)`. A review is not an entry opportunity and never counts in the sample; it has its own frozen evidence manifest, cutoff, call attempts, deadline, and exactly one immutable management disposition (`NO_CHANGE`, `EXIT_INTENT_CREATED`, or an operational failure class), cannot reenter, and attributes costs and exit cash flow to the original opportunity. Deterministic exits use refreshed baseline probability/signal. Deterministic stops/targets/time/resolution continue when AI review fails or exhausts budget. Exits consume bid depth.

### 8.3 Controls

Deterministic arm uses a frozen probability-valued momentum model mapping causal 24h midpoint change/volatility to clipped probability, fitted only on committed pre-run point-in-time data. It uses the same edge, size, costs and common exits, but its own probability for signal exits.

Forecast benchmark is executable midpoint probability, evaluated by cluster-weighted Brier; log loss secondary. It is non-tradable. Economic performance compares AI with deterministic and interest-bearing cash.

## 9. Stock swings

Freeze S&P 100 membership/source/hash. After official close rank by frozen sum of absolute standardized 5-day return and standardized volume surprise; ticker breaks ties. Register top 20 `(run,session,symbol)` opportunities. Every arm uses this order; unavailable capacity records `CAPACITY_REJECTED`.

Evidence covers prior seven days: SEC/issuer filings, earnings/guidance, material industry/regulatory news, catalysts, opposition, thesis breakers, gaps. Jev also receives causal returns, ATR dollars/share, trend, volume surprise, realized volatility, known earnings date, sector, and arm exposure.

Shorts are enabled only if point-in-time borrow/locate source passes preflight; otherwise both arms are long/cash. Flat 0.50% borrow alone does not authorize shorts.

At modeled D+1 fill freeze decision-day ATR_D. B=0.5% current equity; d=2×ATR_D; integer `q=min(floor(B/d), floor(0.10×equity/fill_price), cash_or_collateral_limit)`. Stop is fill−d long/fill+d short. Recheck costs, 10% position, 50% gross, 25% sector, max five positions. Short proceeds are restricted; collateral covers market value plus liabilities.

Close job persists pending intent with decision, target session, reference close, 10% gap limit, manifest, expiry. `stock-open-execution` runs after official open publication. Unique key includes run, arm, symbol, target session, intent. Absent/stale/late or >10% gap cancels explicitly.

Fill: long entry `open×(1+half_spread+0.0005)`, long exit `reference×(1−half_spread−0.0005)`, adverse signs reversed for shorts. Freeze spread source/model. Apply date-effective SEC/TAF only where applicable. Borrow accrues daily; dividends post on ex-date.

Common exits: 2-ATR stop, 3-ATR target, ten trading days, unmodelable corporate action/invalid pricing. After every official close while a stock AI position is open, create `position_review_id=hash(position_id,session_date,policy_version)` and apply the same frozen-evidence, provider-attempt, deadline, immutable management-disposition, no-reentry, and cost-attribution contract as Polymarket reviews. AI flip and deterministic-rule flip are separate signal exits, executed next open. Daily stop+target collision assumes stop first; gaps fill at open.

Deterministic action: LONG when close>50d SMA and 20d and 5d returns positive; exact inverse SHORT only when shorts enabled; otherwise FLAT. Same candidate order/capital/cost/common exits.

Exogenous stock forecast label is total shareholder return from common modeled D+1 open reference to official D+10 close >0, including splits, dividends, delisting proceeds/distributions. Trade exits never change it. Brier uses separately elicited Jev probability, never action confidence.

## 10. Estimand and Phase-1 decision

For stream s with N_s registered opportunities, the policy-value estimand is `theta_s=(EconomicPnL_AI,s−EconomicPnL_DETERMINISTIC,s)/(N_s×R0)`. Account economic P&L uses the same fixed enrollment-through-T_m interval and includes realized cash flows, fixed-cutoff marks, cash interest, and every arm-scoped direct/allocated cost. Direct items retain opportunity IDs; weekly residuals remain in the owning arm's weekly policy contribution. Allocations and residuals reconcile separately to each arm account; their difference equals the numerator of theta_s. This estimates complete policy value, not an individual opportunity causal effect because portfolio limits create interference.

The two confirmatory nulls are H0_s: theta_s<=0.03 versus H1_s>0.03. Assign each opportunity and all later cash flows/marks/direct costs to enrollment week. For every calendar week b and stream s store synchronized `(X_sb,n_sb)`, where X is AI-minus-deterministic economic P&L including weekly residual differences and n is registered-opportunity count. Resample the same week indices jointly for both streams using a circular stationary bootstrap with expected block length L selected only from pre-run simulation, 99,999 valid replicates, and frozen RNG seed. Compute `theta*_s=sum(X*_sb)/(R0×sum(n*_sb))`; redraw a replicate when either stream denominator is zero, and declare analysis invalid if 999,990 total draws cannot produce 99,999 valid replicates. Let theta_hat be observed and `d*_r=theta*_r−theta_hat`. The one-sided null-centered p-value is `(1 + count(d*_r >= theta_hat−0.03))/(99,999+1)`. Order p1<=p2; reject both only when p1<=0.025 and p2<=0.05. For each stream, sort the 99,999 centered replicates `d*` ascending and define the fixed basic-bootstrap simultaneous lower bound as `L_s = theta_hat_s - d*_(97,500)`, where `d*_(97,500)` is the 97,500th 1-indexed order statistic (`ceil(0.975 × (99,999+1))`). Report these 97.5% one-sided lower bounds as simultaneous descriptive bounds rather than ordered generic intervals. Both rejections plus every safety gate are required for later confirmation. Phase-1 values are diagnostic only.

Forecast scoring covers all registered opportunities. Polymarket y is official payout in the frozen market's admissible resolution set within [0,1], baseline is common t3 executable midpoint. Stock y is the fixed binary D+1-to-D+10 label, baseline is pre-run prevalence from the identical candidate rule. Valid probabilities are scored even for hold/flat/abstain. Missing valid probability substitutes frozen baseline in full-cohort Brier; report coverage and valid-only Brier. Brier skill is `1−BS_full/BS_baseline`, target >0. Calibration on valid probabilities reports intercept target 0, slope target 1, and ten equal-width-bin ECE target 0 with bin counts/block-bootstrap intervals. Phase-1 forecast metrics are descriptive. For labels missing at T_m, bound the paired score difference `mean((p_model−y)^2−(p_baseline−y)^2)` jointly by choosing, for each missing label, the minimizing/maximizing value from its frozen admissible set; because the expression is linear in y, interval endpoints suffice for continuous [0,1] sets. Forecast superiority may be described only when the worst-case upper bound is below zero. Any Brier-skill bounds use the same joint label assignments, never separately bounded numerator/denominator.

Secondary: portfolio/control return, max drawdown, worst week, descriptive ES95 with CI, profit factor, turnover, filled-count and gross-dollar-day retention, forecast scores/calibration, reliability, latency, and costs.

Phase-1 precedence is exhaustive: (1) `OPERATIONALLY_INFEASIBLE` on any causality/version/duplicate-economic-event/accounting/reconciliation/risk violation or <99% terminal slots; (2) `INCONCLUSIVE` when enrollment/T_m analysis is incomplete or a manifest-frozen numeric information requirement fails; (3) `PROMISING` only when every numeric gate below passes under full base and joint-adverse replays; (4) otherwise `NO_EVIDENCE`. Numeric gates are: theta_poly>=0.03 and theta_stock>=0.03; positive cumulative AI-minus-deterministic economic P&L per stream; AI economic return above interest-bearing cash and stock AI above exposure-matched passive; AI maximum drawdown <= deterministic drawdown+0.02 and <=0.10 absolute; weekly-loss gate below; count and dollar-day retention each >=0.60. PROMISING is not alpha confirmation or live authorization. Before registration, the manifest freezes numeric information requirements and classification tree. Confirmatory duration is `max(26 weeks,B_min)` where B_min comes from preregistered simulation at >=80% joint-Holm power under a frozen alternative vector `(theta_alt_poly,theta_alt_stock)` with each theta_alt strictly >0.03 (default design target 0.06), paired variance, cross-stream/serial dependence, abstention and missingness. Completed zero-opportunity weeks remain analysis weeks. Report opportunities, clusters, weeks, and spectral effective weeks using a preregistered initial-positive-sequence autocorrelation truncation; effective weeks are diagnostic and never trigger post-result extension.

At the frozen daily valuation timestamp, maximum drawdown is `max_{t<=u}((peak_equity_t−equity_u)/peak_equity_t)` on synchronized marked economic-equity series. Weekly gate is `min_b[(AI economic P&L_b−deterministic economic P&L_b)/10000] >= -0.01` over identical frozen UTC enrollment weeks. Count retention is AI positive-fill opportunities / deterministic positive-fill opportunities. Dollar-day retention is summed AI daily absolute marked gross notional / corresponding deterministic sum at the daily valuation timestamp. A zero deterministic denominator makes the stream INCONCLUSIVE. All these gates must pass in base and named joint-adverse replay.

Uncertain spread/slippage/borrow/operating costs scale 0.5/1/1.5; depth does not. Named joint-adverse is 1.5x uncertain costs plus 50% haircut to every captured Polymarket level quantity and frozen zero-bid valuation. Replay the complete policy—edge, VWAP, sizing, fills, exits, endpoint, benchmarks and safety gates. Statutory fees/cash yield do not scale. Threshold/reserve/volatility grids are exploratory. Base and joint-adverse classifications must both pass and match.

Let E be the manifest's exact UTC end of fixed 12-week enrollment. Before enrollment, resolve and store T_m as the official NYSE close timestamp (America/New_York converted to UTC) on the tenth NYSE session strictly after the UTC calendar date E+30 calendar days, using the pinned NYSE calendar version. The same T_m applies to both streams. No entries occur after E. Primary economic endpoint/classification is fixed at T_m: realized cash flows plus conservative executable marks, with quantity beyond depth zero. Enrollment-cutoff marks are interim; later settlements/corrections are supplemental and never change classification. Forecast labels unavailable at T_m use prespecified best/worst-case bounds and are never silently excluded. Deterministic position management continues.

## 11. Journal and accounting

Store UTC timestamps as integer microseconds, money as integer micro-dollars, prices/probabilities as millionths, and quantities in declared venue-native integer scale.

Immutable tables cover runs/manifests/startups, opportunities/clusters/snapshots/documents/evidence manifests, provider call intents/attempts/results, decisions, order intents/events/fills, balanced ledger postings, position and position-review events, cycle intents/events, health/violations, artifacts. Mutable operational lease/quota-reservation rows are explicitly excluded from immutable history and append an immutable audit event for every mutation. `BEFORE UPDATE/DELETE` abort triggers protect economic/audit tables. Corrections append same-entity successor; no cross-run/type/identity, self-reference, branch, or cycle. Derived views are disposable.

Every fill, fee, cash yield, dividend, borrow, split, resolution, and operating cost writes balanced cash/inventory/expense postings transactionally. Enforce cumulative fill <= order and captured depth; temporal constraints; balanced postings; equity equation; restricted short proceeds; zero reconciliation difference before entries. Violation blocks entries but not exits.

Each stage key derives from run, semantic subject ID (`opportunity_id` or `position_review_id`), arm, stage, manifest hash, and input hash. Persist provider-call intent before network call; attempts separate; one validated result selected. On crash reuse completed result, resume explicit retryable failure, or mark unknown before retry. Unique indexes are final guard.

## 12. Scheduling, leases, recovery

A cycle intent stores `cycle_key=hash(run_id,mode,job_type,stream,scheduled_slot)` and `scheduled_for`, `input_cutoff`, and UTC deadline. Immutable cycle events append start, stage transitions, lease loss/recovery, terminal result and `finished_at`; no cycle row is updated. Recurring reviews have their own slot/ID. Stock uses exchange calendar including holidays/early closes. Persist UTC event/deadline times but enforce elapsed deadlines and latency with a monotonic clock. Missed slots append `MISSED_SLOT`; never backfill current data. Replay is distinct and frozen.

One advisory file lock plus `BEGIN IMMEDIATE` DB lease with owner, expiry, fencing token protects the writer; every stage write requires token. No DB transaction spans network. Startup recovers nonterminal cycles/pending intents before new slot; duplicate starts converge on cycle key.

One persistent `run-worker` process owns startup identity, heartbeat, writer lock, lease, deterministic scheduler, and all forward cycles. systemd supervises that service and contains no business logic. Thin CLI subcommands (`polymarket-cycle`, `stock-close-decision`, `stock-open-execution`, `manage-open-positions`, `recover-and-reconcile`, `evaluation-snapshot`, `final-report`, `backup-and-verify`, `deployment-verify`) are worker-internal handlers or explicit maintenance/replay commands, never independently timer-launched forward writers.

No model fallback. Allowed data fallbacks are manifest-listed with mapping/timestamp/precedence/validation. Retry identical input, max three attempts, bounded exponential backoff+jitter and absolute deadline; store every attempt.

## 13. Runtime, durability, security

Startup record: instance ID, PID, host/boot ID, start, absolute executable/package path, Git commit, dirty flag, manifest/prompt/lock hashes, schema, plus systemd unit/timer content hashes. Every cycle references it. Health includes last slot/start/terminal completion, stage, oldest pending work, failures, heartbeat, provider IDs, data ages. `deployment-verify` resolves non-expired heartbeats and requires exactly one live instance, compares expected identity with that live startup/latest stage, then separately verifies newest completed cycle references the same startup and fencing token. It verifies PID start identity and boot ID to prevent PID reuse, works before the first cycle from the unique live startup record, and fails on active mismatch, zero/multiple instances, stale heartbeat, superseded token, or historical-only match.

Connection factory asserts `foreign_keys=ON`, WAL, frozen busy timeout, `synchronous=FULL`. Startup rejects network filesystems. Daily backup uses online backup API or `VACUUM INTO`, includes referenced blobs, verifies hashes and `integrity_check`. RPO 24h, RTO 2h; restore drill before registration and monthly.

Secrets remain outside repo in mode-0600/OS store. Logs exclude auth/cookies/keys/signed secrets. Dashboard opens DB read-only with fixed parameterized queries. CI scans secrets and asserts no signing/live adapter.

## 14. Tests

Unit/property: fixed-point arithmetic, IDs, date joins/calendars, causal cutoffs, terminal states, parsing, sizing units, VWAP/costs, arm limits, accounting conservation, supersession.

No-lookahead/contract: future filings/resolutions/membership/prices cannot change input; stock fill no earlier than next open; Polymarket uses captured depth.

Crash/concurrency: failpoints before/after every commit/network boundary; fresh-interpreter recovery; N workers on one slot yield one semantic/economic event; test in-flight calls, locks, disk full, truncated WAL, corrupt/missing blob, timeout, rate errors, stale fallback.

Calendar/runtime: DST, holidays, early close, late/missed/catch-up suppression, next-open intents, version failure with deterministic exits, deployment identity read-back.

Accounting/backup: property-generated event sequences balance and reconstruct; golden journal hash; restore into empty directory and reconcile.

Statistics: attributed stationary bootstrap, paired permutation, Brier/log loss, multiplicity; synthetic known-effect tests and pinned block-length rule.

## 15. Reuse and review

Selective MIT adaptation only; copied/materially adapted code goes in `THIRD_PARTY_NOTICES.md` with URL, commit, file, license. Reuse audit holds exact accepted/rejected behavior.

Investment, financial/model-risk, statistical/adversarial, and architecture reviews live under `docs/reviews/`. Blocking findings are fixed or explicitly rejected with evidence. A second review validates corrections. No unresolved placeholders, contradictions, or discretionary post-result choices remain before implementation planning.

## 16. Design acceptance

Complete only when repo is cloneable; lessons/reuse are pinned and licensed; reviews have no blockers; formulas/gates are machine-decidable; spec/reviews/fixes are committed and pushed; and user reviews the written spec before an implementation plan.

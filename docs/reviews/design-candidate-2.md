# Jev Trading Lab — Design Specification

**Status:** Review candidate 2  
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

**Trading P&L** deducts spread, slippage, statutory/venue fees, borrow, dividends owed, and execution costs. **Economic P&L** further deducts actual OpenAI, Jev, paid-data, and incremental infrastructure costs. Failed/abstained calls keep billed cost. Shared run costs allocate 50% per stream, then to the AI arm unless opportunity-specific. Development labor is reported separately with break-even AUM; it is not debited from paper capital.

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

Retriever permits HTTPS only; rechecks DNS and every redirect; blocks loopback/private/link-local/multicast/metadata IPs, credentials in URLs, unsupported schemes/types, excess redirects/time/bytes/documents, and decompression bombs. Model input is explicitly delimited untrusted data.

OpenAI strict schema records facts with document hash, URI, publication/retrieval times and exact normalized quote span; arguments for/against, contradictions, missing evidence, catalysts, and quality. Quote validation uses stored document version and normalized Unicode spans; it proves provenance, not truth.

Jev batches: evidence sufficient (Noul), materially ambiguous (Noul), action including `ABSTAIN`, setup quality (analysis only), Polymarket P(YES), and stock P(D+1 common open to D+10 close total return > 0). Forecast probabilities are distinct from action confidence.

## 7. Model and failure policy

Manifest pins provider, endpoint/API version, SDK/lock version, request snapshot, expected returned ID, schema, temperature, top-p, supported seed, and tools. Store raw request/response and provider fingerprints. Aliases and model fallback are forbidden. Changed/retired models end or invalidate the run; historical outputs replay from storage.

Every `(opportunity_id, arm)` reaches one terminal state: `DECIDED`, `MODEL_ABSTAIN`, `PROVIDER_ERROR`, `VALIDATION_ERROR`, `STALE_INPUT`, `VERSION_MISMATCH`, `DEADLINE_EXCEEDED`, `BUDGET_EXHAUSTED`, `CAPACITY_REJECTED`, or `RISK_REJECTED`. Only `DECIDED` reaches trade gates. Other states contribute zero trading P&L and actual cost to intention-to-treat results.

`entry_enabled` and `position_management_enabled` are separate. Model/evidence failures block entries and model exits only. Stops, targets, time exits, resolution, corporate actions, accrual, marks, and reconciliation continue. Missing exit data creates `EXIT_PENDING_DATA` and conservative valuation, never a fill.

Common gates: fresh market snapshot, registered opportunity, arm-local capital/risk, no duplicate/conflict, complete cost inputs, valid runtime identity. AI-only gates: valid evidence/Jev; sufficiency >=0.70; ambiguity <=0.30; action probability >=0.55; margin over runner-up >=0.15. Each arm uses only its own signal for entry/signal exits.

## 8. Polymarket

### 8.1 Universe, identity, and budget

Scan each 15-minute slot. Eligibility: accepted frozen non-sports provider category; active; resolution 1 hour–30 days away; >=$10,000 trailing 24h USD volume; >=5,000 visible contracts within two points of best quote; <=4-point spread; required resolution fields/source; deterministic unambiguous cluster.

Manifest freezes categories/exclusions, normalized cluster inputs, algorithm/version, and tie-breakers. Cluster membership persists before model calls and is never rewritten. Choose one contract per cluster by highest 24h USD volume, then qualifying contract depth, then immutable market ID.

One cluster creates one Phase-1 opportunity at its first eligible slot; later scans manage it but create no reentry/observation. `opportunity_id=hash(run,stream,cluster,first_slot)`, never arm.

Budgets: maximum 3 new opportunities/cycle, 20/day, 3 concurrent requests, 3 attempts/stage, ten-minute cycle deadline, and $3/day variable AI spend across streams. Order candidates by depth, volume, market ID. Excess terminates `BUDGET_EXHAUSTED`. Pre-run p99 rehearsal must fit deadline or limits decrease before registration.

### 8.2 Edge, sizing, fills

After Jev p_yes, capture one common book. Reject if older than 60 seconds at intent creation. For proposed quantity compute side-specific entry VWAP from captured levels. Gross edge is `p_yes − YES_VWAP` or `(1−p_yes) − NO_VWAP`. Entry VWAP already contains spread.

Convert entry fee, expected exit fee, and conservative exit slippage to probability points. Trade only if:

`gross_edge >= max(0.05, 2 × (entry_fee_pp + exit_fee_pp + exit_slippage_pp) + 0.03)`.

Solve size/edge jointly because VWAP changes with quantity.

Let `sigma_p` be frozen-estimator seven-day standard deviation of midpoint probability-point changes per contract. Risk B=0.5% of current arm equity; `q=floor(B/max(sigma_p,0.05))`; entry cost `C=q×VWAP+fees`. Enforce C <=10% equity, cluster cost <=10%, portfolio gross cost <=50%, max five clusters, q <=5% captured executable contract depth, and notional+fees <=1% 24h USD volume. Store raw and normalized units.

Fills consume only captured ask levels and quantities; remainder cancels. Fill identity includes order, snapshot, level, sequence. Quote cannot postdate order.

Common exits: resolution/invalidation; 15 points adverse; 10 points favorable; seven days; liquidity failure without optimistic fill. AI signal exit uses fresh AI edge/action. Deterministic exit uses its own refreshed baseline probability/signal. Exits consume bid depth.

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

Common exits: 2-ATR stop, 3-ATR target, ten trading days, unmodelable corporate action/invalid pricing. AI flip and deterministic-rule flip are separate signal exits, executed next open. Daily stop+target collision assumes stop first; gaps fill at open.

Deterministic action: LONG when close>50d SMA and 20d and 5d returns positive; exact inverse SHORT only when shorts enabled; otherwise FLAT. Same candidate order/capital/cost/common exits.

Exogenous stock forecast label is total shareholder return from common modeled D+1 open reference to official D+10 close >0, including splits, dividends, delisting proceeds/distributions. Trade exits never change it. Brier uses separately elicited Jev probability, never action confidence.

## 10. Estimand and Phase-1 decision

For registered opportunity i:

`delta_i=(AI economic P&L_i − deterministic economic P&L_i)/R0`.

Abstention/failure/stale/budget/risk/capacity contributes zero trading P&L and attributable operating cost. Polymarket aggregates all cluster cash flows through exit/resolution. Stock unit is candidate-date; weekly blocks preserve dependence.

Two primary hypotheses only: mean(delta_poly)>0.03 and mean(delta_stock)>0.03. Paired stationary/block bootstrap uses preregistered block length; Holm applies only to these. A later confirmatory pass requires Holm-adjusted one-sided lower bound >0.03. Phase 1 reports this but cannot claim confirmatory alpha.

Secondary: portfolio/control return, max drawdown, worst week, descriptive ES95 with CI, profit factor, turnover, filled-count and gross-dollar-day retention, forecast scores/calibration, reliability, latency, and costs.

Phase 1 is `PROMISING` only if: 12-week enrollment finishes; base and joint 1.5x-cost mean economic delta >=0.03 for each stream; cumulative incremental economic P&L positive; AI beats cash and stock AI beats passive; AI max drawdown <= deterministic+2 percentage points and <=10% absolute; worst week no more than 1% initial capital worse; count and dollar-day retention >=60%; >=99% scheduled slots terminal; zero causality/version/duplicate/accounting/reconciliation/risk violation. Insufficient effective blocks remains `INCONCLUSIVE`.

Sensitivity scales uncertain spread/slippage/borrow/depth/operating costs at 0.5/1/1.5 individually and jointly; statutory fees and observed cash do not scale. Polymarket tests 25/50% depth haircut and zero-bid exits. Threshold/reserve/volatility grids are exploratory. Base and joint adverse classification must match.

Phase 1 enrolls 12 weeks, then no new entries. Maturation continues 30 calendar days plus ten trading days. Report enrollment-cutoff conservative marked return and post-maturation realized return separately. Deterministic position management continues.

## 11. Journal and accounting

Store UTC timestamps as integer microseconds, money as integer micro-dollars, prices/probabilities as millionths, and quantities in declared venue-native integer scale.

Immutable tables cover runs/manifests/startups, opportunities/clusters/snapshots/documents/evidence manifests, provider call intents/attempts/results, decisions, order intents/events/fills, balanced ledger postings, positions, cycles, health/violations, artifacts. `BEFORE UPDATE/DELETE` abort triggers protect economic/audit tables. Corrections append same-entity successor; no cross-run/type/identity, self-reference, branch, or cycle. Derived views are disposable.

Every fill, fee, cash yield, dividend, borrow, split, resolution, and operating cost writes balanced cash/inventory/expense postings transactionally. Enforce cumulative fill <= order and captured depth; temporal constraints; balanced postings; equity equation; restricted short proceeds; zero reconciliation difference before entries. Violation blocks entries but not exits.

Each stage key derives from run, shared opportunity, arm, stage, manifest hash, input hash. Persist provider-call intent before network call; attempts separate; one validated result selected. On crash reuse completed result, resume explicit retryable failure, or mark unknown before retry. Unique indexes are final guard.

## 12. Scheduling, leases, recovery

Every cycle stores `scheduled_for`, `started_at`, `input_cutoff`, `deadline_at`, `finished_at` UTC. Stock uses exchange calendar including holidays/early closes. Missed slots append `MISSED_SLOT`; never backfill with current data. Replay is distinct and uses frozen snapshots.

One advisory file lock plus `BEGIN IMMEDIATE` DB lease with owner, expiry, fencing token protects the writer; every stage write requires token. No DB transaction spans network. Startup recovers nonterminal cycles/pending intents before new slot; duplicate starts converge on cycle key.

CLI: `polymarket-cycle`, `stock-close-decision`, `stock-open-execution`, `manage-open-positions`, `recover-and-reconcile`, `evaluation-snapshot`, `final-report`, `backup-and-verify`, `deployment-verify`. systemd contains schedule only.

No model fallback. Allowed data fallbacks are manifest-listed with mapping/timestamp/precedence/validation. Retry identical input, max three attempts, bounded exponential backoff+jitter and absolute deadline; store every attempt.

## 13. Runtime, durability, security

Startup record: instance ID, PID, host/boot ID, start, absolute executable/package path, Git commit, dirty flag, manifest/prompt/lock hashes, schema. Every cycle references it. Health includes last slot/start/terminal completion, stage, oldest pending work, failures, heartbeat, provider IDs, data ages. `deployment-verify` compares expected identity with completed-cycle read-back.

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

# Round 1 — Investment Research Review

**Verdict:** REJECT AS CONFIRMATORY DESIGN PENDING REVISION  
**Blocking:** 4  
**Major:** 7

## Findings

- **IR-01 BLOCKER — Undefined hypotheses/estimand/R.** Define one confirmatory endpoint per stream, a fixed common $50 risk unit, intention-to-treat opportunity outcomes, and Holm only across those two tests.
- **IR-02 BLOCKER — Deterministic controls cannot pass AI-only gates or use AI exits.** Split common and AI-only gates; every arm uses its own signal for entry and signal exits.
- **IR-03 BLOCKER — Non-tradable Polymarket forecast has no portfolio return.** Compare economic return with interest-accruing cash; compare probability quality with market Brier/log loss separately.
- **IR-04 BLOCKER — Stock Brier score has neither a probability nor exogenous target.** Elicit `P(D+1 open to D+10 close total return > 0)` separately and freeze an arm-independent target.
- **IR-05 MAJOR — Capital/passive benchmarks inconsistent.** Give each tradable arm isolated $10,000; accrue point-in-time Treasury cash; use 50% OEF/50% cash as exposure-matched stock benchmark and report 100% OEF/SPY only contextually.
- **IR-06 MAJOR — Polymarket inclusion/clustering discretionary.** Freeze accepted categories, exclusion reasons, deterministic cluster keys, contract selection and tie-breakers before research/model output.
- **IR-07 MAJOR — Stock allocation order differs by arm.** Use one canonical candidate order and explicit `CAPACITY_REJECTED` outcomes.
- **IR-08 MAJOR — Cutoff sequence incomplete.** Persist eligibility, evidence cutoff, model completion, executable-book and order timestamps; refresh one common executable book after model completion and reject stale cycles.
- **IR-09 MAJOR — AI operating costs not part of promotion economics.** Maintain trading and economic P&L; deduct all attributable AI/data/infrastructure cost; require positive cumulative incremental economic P&L.
- **IR-10 MAJOR — Drawdown/exposure/stress rules ambiguous.** Make formulas machine-decidable, define count and dollar-day retention, and state exactly which gates must survive stress.
- **IR-11 MAJOR — Twelve weeks/100 nominal observations are underpowered.** Treat 12 weeks as a preregistered pilot; no alpha/live promotion. A later confirmatory run needs at least 26 completed weekly blocks and 100 resolved Polymarket clusters over at least 26 weeks.

## Final instruction

Revise before implementation planning. Preserve causal forward collection, but separate a 12-week feasibility pilot from any later confirmatory alpha claim.

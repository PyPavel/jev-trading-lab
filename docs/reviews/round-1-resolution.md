# Round 1 Review Resolution

**Status:** All round-1 blockers and material findings accepted and incorporated into design candidate 2.

## Investment review

- IR-01: one endpoint per stream; fixed R0=$50; opportunity ITT; Holm only across two hypotheses.
- IR-02: common and AI-only gates split; each arm owns signal entry/exit.
- IR-03: Polymarket forecast scoring separated from economic cash/control comparison.
- IR-04: separate stock fixed-horizon probability and exogenous D+1-to-D+10 label.
- IR-05: independent $10k accounts, cash yield, 50% OEF/50% cash passive.
- IR-06: categories, clustering, exclusions and tie-breakers frozen.
- IR-07: one stock candidate order with explicit capacity outcomes.
- IR-08: t0–t4 causal sequence and common post-model market snapshot.
- IR-09: trading/economic P&L separated; AI/data/infrastructure costs deducted.
- IR-10: machine-decidable drawdown, worst-week, retention and stress rules.
- IR-11: 12 weeks reclassified as pilot; later confirmation needs >=26 weeks/blocks.

## Financial/model-risk review

- FIN-01/02/03: corrected per-contract probability-volatility sizing, normalized units, size-dependent VWAP and non-duplicated friction.
- FIN-04/05: arm-specific signal exits; forecast benchmark has no portfolio-return comparison.
- FIN-06/07: economic P&L, R0 and shared opportunity denominator defined.
- FIN-08: arm-independent D+1-open to D+10-close stock forecast target.
- FIN-09: enrollment plus maturation; marked and realized reports.
- FIN-10/11: size at modeled fill; explicit gross, cluster, sector and short collateral limits.
- FIN-12/13: side-specific fills, point-in-time borrow requirement, explicit formulas and balanced ledger.
- FIN-14: interest-bearing cash and exposure-matched passive.
- FIN-15/16: pilot classification; later >=26 blocks; two hypotheses and Holm procedure.
- FIN-17: max drawdown/worst week hard gates; ES95 descriptive.
- FIN-18: frozen 0.5/1/1.5 uncertain-cost scenarios and market-specific stress.
- FIN-19: no probability sizing in Phase 1.
- FIN-20: return, turnover, profit factor and retention defined.

## Architecture/reliability review

- ARCH-001: isolated account key and economic P&L primary.
- ARCH-002: retrieval separated from generation; frozen evidence; tools disabled.
- ARCH-003: persistent next-open intents and dedicated executor.
- ARCH-004: cycle/day/concurrency/attempt/deadline/dollar budgets and rehearsal.
- ARCH-005: closed terminal enum; failures are not abstentions.
- ARCH-006: one first-eligible cluster opportunity and arm-free ID.
- ARCH-007/008: DB abort triggers, event tables, fixed-point balanced ledger.
- ARCH-009/010: durable provider call attempts, fenced writer, lease and recovery.
- ARCH-011/012: immutable slot timing/missed policy; controls execute forward.
- ARCH-013/014/015: full pinning, run invalidation, position management continues, no model fallback.
- ARCH-016: startup identity and completed-cycle deployment verification.
- ARCH-017: SSRF/redirect/IP/type/size defenses; no model browsing.
- ARCH-018: asserted PRAGMAs, local filesystem, online backup and restore drill.
- ARCH-019: pinned immutable clustering.
- ARCH-020: explicit application module.
- ARCH-021: fixed-point money/probability/time representation.
- ARCH-022: normalized stored document plus exact quote spans.
- ARCH-023: crash/concurrency/disk/WAL/blob/calendar/accounting/restore tests.
- ARCH-024: immutable manifests and schema identity; migration details move to plan.
- ARCH-025: dashboard reduced to static/read-only; distributed/generic infrastructure rejected.

No round-1 finding was rejected.

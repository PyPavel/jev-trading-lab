# Implementation Plan Review

## Candidate 1

35-task plan rejected: 8 blockers, 17 major findings. Material failures included self-matching safety scan, `git commit -am` omitting new files, unsafe migration edits, missing causal contract/gates/baselines/T_m, under-specified providers and formulas, oversized TDD tasks, incomplete evaluation/runtime/release paths.

## Candidate 2

Rewritten into 69 behavior-sized TDD tasks. Review still required corrections for delivery evidence, migration ownership, causal/orchestrator ordering, forecast baselines, cost routes, provider-local contracts, backup/worker order, durable preflight activation, evaluation vertical gate, and executable release push/review loop.

## Candidate 3

Expanded/reordered to 74 tasks. Final binary review found two issues:

- `MIG-01`: each immutable table must receive append-only/lineage guards in the same creating migration.
- `REL-01`: Task 74 delivery evidence must be committed before final push; CI/review must target that exact pushed HEAD and repeat after corrections.

Both were applied to the final plan.

## Final status

**APPROVED after MIG-01 and REL-01 corrections.** The plan has 74 TDD tasks, explicit two-commit task evidence, complete migration ownership, vertical phase gates, provider contracts, baselines, fixed endpoint valuation, full evaluation, durable preflight activation, and reviewed feature-branch release loop.

# Final Gate Resolution

Final gate did not approve candidate 3. All 1 blocker and 11 unique major findings were accepted into candidate 4.

## Finance/statistics
- GATE-01: restored every numeric PROMISING gate; drawdown formula/timestamp specified.
- POWER-01: power simulation now uses theta_alt > 0.03, default 0.06 vector; null boundary remains 0.03.
- BOOTSTRAP-01: weekly vectors, ratio recomputation, zero denominator rule, finite-replicate p-value and fixed simultaneous bounds specified.
- COST-01: AI-specific costs debit AI, deterministic-specific debit deterministic, truly shared costs use frozen 50:50 ratio; account reconciliations explicit.
- POLY-SIZE-01: feasible integer set Q includes edge, VWAP, fee, depth and all caps; adverse depth levels floor quantity/2.
- VALUATION-01: one run-wide T_m pinned to named NYSE calendar/timezone and stored as UTC.
- BRIER-01: admissible label set and joint paired-score-difference missing-label bounds specified.

## Architecture/adversarial
- ARCH2-002: every cost is arm/account scoped and reconciles by arm and stream.
- ARCH2-003: atomic reservations cover calls, tokens, evidence bytes, concurrency and dollars; unknown attempts retain reservations.
- ARCH2-004: both streams have manifest-frozen recurring review cadence/identity/disposition; stage keys accept position_review_id.
- ARCH-007/010/011: cycle intent/events are append-only; operational leases/reservations explicitly mutable with immutable audit events; canonical cycle key and monotonic deadlines specified.
- ARCH2-007: chose persistent-worker process model; one worker owns identity/heartbeat/lock/lease/scheduling; PID start/boot identity verified.

Candidate 4 requires one final concise verification before approval.

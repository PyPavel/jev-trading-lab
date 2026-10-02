# Upstream Reuse Audit

**Audit date:** 2026-10-02

All reviewed repositories use the MIT license. Copying remains selective. Adapted files must retain attribution in `THIRD_PARTY_NOTICES.md`.

| Project | Pinned commit | Reuse candidates | Explicit rejection |
|---|---|---|---|
| `markusbug/jevymarket` | `5aacd8dd26ba28838fca4c1c9ecd03910a0f3d7c` | Jev adapter concepts; research brief schema; SQLite decision/research records; Polymarket discovery and test fixtures | Live-by-default CLI; six-hour evidence cache; tolerant JSON parsing without citation verification; market probability in Jev state; 1.5× minimum-order cap exception; hold-to-resolution-only policy |
| `egrm07/jev_bitcoin_backtest` | `5d8e6d0450090c786b5c2d8aac3c3a643631ab4a` | Explicit cost object; next-bar causal simulator; no-lookahead fixtures; stationary bootstrap; paired permutation; AUC/Brier tests; multiple-testing correction | Crypto-specific market representations and policies |
| `SaratAngajalaoffl/jeeva` | `16cb10cad52a6dbf52164c4d7443a4849070b1e1` | Stale-decision price recheck; decision lifecycle; exchange-state reconciliation; failure auditing; persistent ambiguous-order handling concepts | Rust/Node/Postgres deployment; outdated direct TypeSafe API adapter; zero default confidence gate; permissive 5% slippage ceiling; provider-failure auto-flatten as a generic policy |
| `dijiclick/jev-perp-bot` | `8308be07c9660eb561febc4bce1eff71c727c8d3` | SQLite WAL journal; duplicate-fill identity; deterministic risk state; cost/P&L decomposition; honest negative-result reporting | Perpetual-futures strategy, one-minute cadence, venue integration and any claim that turnover creates edge |
| `michaelpersonal/jev-trade-cc` | `1f21aa3dcf89e641725a4b6dad998964e24c5f6c` | Point-in-time SEC facts; frozen entry baselines; jackknife robustness; prompt rules: facts not conclusions, explicit uncertainty, arithmetic in code | O’Neil strategy; selected +107.8% headline; any assumption that high Jev confidence implies predictive skill |
| `jarrodwatts/jev-trader` | `b587759e459ea049590102e54a0b07800864cdc3` | Small provider-neutral model interface; SSE feed patterns only if a dashboard later needs them | Forced BUY/SELL choice; no abstention; prompt/execution mismatch; simulated maker fills; reversing constrained actions while retaining original probabilities; HFT architecture |

## Lessons recovered from the previous local trader system

1. Align mixed calendars by explicit trading date. Array-length alignment produced false BTC/equity correlations.
2. Validate before wiring. Retire signals with leakage, degenerate labels, missing cost models, or no consumer.
3. Price, volume, ATR, news, and model state need freshness gates and last-good provenance.
4. Provider failure must fail loudly into an audited non-trade state. Do not disguise outages as model decisions.
5. Portfolio writes need transactional/atomic persistence. Stop writers before administrative resets and verify reconstructed state afterward.
6. Duplicate startup hooks, scanners, positions, logs, decisions, and fills require stable identities and uniqueness constraints.
7. Backtests must include realistic costs and no-lookahead guards. One favorable window is not feasibility.
8. Binary-event exits differ from trending-asset exits. Do not transfer trailing/tiered profit-taking assumptions across market structures.
9. Runtime code can lag or differ from committed code. Health endpoints must expose commit and manifest hashes; verification checks process start/read-back.
10. Dashboards must derive from the economic journal. Separate mutable summaries caused false zeroes and misleading P&L.
11. Numerical facts belong in code. Models should judge semantic evidence, not perform simple arithmetic.
12. Kill failed modules instead of preserving dead complexity. Negative findings remain visible.

# Jev Trading Lab Implementation Plan

> **For Hermes:** Use `subagent-driven-development` task-by-task with spec-compliance review, then code-quality/security review. No task advances with a BLOCKER or MAJOR finding.

**Goal:** Build and exercise a paper-only dual-stream feasibility lab for Polymarket and S&P 100 stocks.

**Architecture:** Python 3.12 modular monolith; one persistent worker; local SQLite WAL plus immutable blobs; paper fills only. OpenAI structures frozen evidence, Jev makes bounded judgments, deterministic code owns economics and evaluation.

**Stack:** Python 3.12, hatchling, stdlib SQLite/asyncio/hashlib/ssl, httpx, pydantic/settings, typer, numpy, exchange-calendars, pytest/pytest-asyncio/respx/hypothesis, ruff, mypy. Manifest format is JSON, not YAML.

**Contracts:** approved design; `docs/research/provider-contracts.md`; reuse audit; review resolutions.

## Mandatory execution protocol for every task

1. Work on a feature branch; never push directly to `main`.
2. For each listed RED behavior, run its exact pytest node and observe an assertion failure for the intended missing behavior. Collection/import errors do not count. For a new module, first create only the importable shell after a `find_spec` assertion fails, then write behavioral tests one at a time.
3. Implement only enough for that node to pass. Repeat RED/GREEN per behavior.
4. Run the focused test file, then the full gate:
   `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src`.
5. Before the production commit: `git diff --check`; `git status --short`; `git add -- <every listed task path>`; inspect `git diff --cached --name-status`; commit with `git commit -m ...`; verify `git show --stat --oneline HEAD`; fail if any task production file remains untracked/unstaged.
6. Run spec-compliance review, then quality/security review. Fix findings through additional RED/GREEN commits and rerun the full gate until both reviews report zero BLOCKER/MAJOR findings.
7. After reviews, create `docs/delivery/task-NNN.md` containing every RED command/expected assertion failure, GREEN/full-gate output, all production/fix commit IDs, review verdicts, and final tree status. Commit it separately: `git add -- docs/delivery/task-NNN.md && git commit -m "docs(delivery): record task NNN evidence"`; verify that commit and a clean tree before advancing.

No upstream production code may be copied before the local test is observed failing. No live/signing dependency or surface may be added.

---

### Task 1: Package, lockfile, and paper-only policy

**Objective:** Create an installable package whose resolved dependencies and runtime surfaces cannot place live orders.

**Files:**
- Create: `pyproject.toml`
- Create: `uv.lock`
- Create: `src/jev_trading_lab/__init__.py`
- Create: `src/jev_trading_lab/cli.py`
- Create: `tests/test_package_policy.py`
- Create: `.github/workflows/ci.yml`
- Create: `THIRD_PARTY_NOTICES.md`
- Modify: `README.md`

**RED behaviors (execute individually):**
- `tests/test_package_policy.py::test_package_imports` — find_spec is initially false, then package shell imports
- `tests/test_package_policy.py::test_no_live_execution_surface` — AST/dependency/entry-point scan over src,scripts,ops,pyproject,uv.lock rejects broker/wallet/signing surfaces without scanning rule literals
- `tests/test_package_policy.py::test_locked_build_configuration` — hatchling src layout, package-data globs, dependency groups and uv.lock are declared

**GREEN:** Use hatchling src layout and future SQL/template package-data globs; lock with `uv lock`; CI uses `uv sync --frozen --all-groups`; runtime allowlist excludes trading SDKs; add secret scan and format check. Clean-wheel install/migrate/render is deferred until resources exist and becomes mandatory in release verification.

**Focused verification:** `uv sync --all-groups && uv run pytest tests/test_package_policy.py -q && uv build`

**Commit:** `git add -- pyproject.toml uv.lock src/jev_trading_lab/__init__.py src/jev_trading_lab/cli.py tests/test_package_policy.py .github/workflows/ci.yml THIRD_PARTY_NOTICES.md README.md && git commit -m "build: initialize paper-only trading lab"`

### Task 2: Fixed-point economic values

**Objective:** Add deterministic integer money, probability, price, quantity scales and rounding.

**Files:**
- Create: `src/jev_trading_lab/core/values.py`
- Create: `tests/core/test_values.py`

**RED behaviors (execute individually):**
- `tests/core/test_values.py::test_money_roundtrip` — 1.234567 USD becomes exactly 1_234_567 micro-dollars
- `tests/core/test_values.py::test_probability_bounds` — [0,1] range enforced in millionths
- `tests/core/test_values.py::test_rounding_policy` — half-even boundary and venue quantity scale are deterministic

**GREEN:** Use Decimal only at trust boundaries; domain arithmetic uses tagged integers.

**Focused verification:** `uv run pytest tests/core/test_values.py -q`

**Commit:** `git add -- src/jev_trading_lab/core/values.py tests/core/test_values.py && git commit -m "feat(core): add fixed-point economic values"`

### Task 3: Canonical IDs, UTC time, and observation envelope

**Objective:** Create stable hashes, strict UTC timestamps, and the common provider metadata contract.

**Files:**
- Create: `src/jev_trading_lab/core/ids.py`
- Create: `src/jev_trading_lab/core/time.py`
- Create: `src/jev_trading_lab/core/observations.py`
- Create: `tests/core/test_identity.py`
- Create: `tests/core/test_observations.py`

**RED behaviors (execute individually):**
- `tests/core/test_identity.py::test_canonical_hash_ignores_mapping_order` — same payload order yields same cross-process SHA-256
- `tests/core/test_identity.py::test_naive_datetime_rejected` — naive datetime fails
- `tests/core/test_observations.py::test_first_retrieval_is_immutable` — later metadata cannot backdate/replace retrieved_at
- `tests/core/test_observations.py::test_corrections_append` — correction requires content hash and supersedes ID

**GREEN:** Implement canonical JSON, integer UTC microseconds, and frozen ObservationMetadata used by every adapter.

**Focused verification:** `uv run pytest tests/core/test_identity.py tests/core/test_observations.py -q`

**Commit:** `git add -- src/jev_trading_lab/core/ids.py src/jev_trading_lab/core/time.py src/jev_trading_lab/core/observations.py tests/core/test_identity.py tests/core/test_observations.py && git commit -m "feat(core): add identities time and observation metadata"`

### Task 4: Immutable JSON manifest models

**Objective:** Parse, validate, and hash every experiment-affecting setting without persistence.

**Files:**
- Create: `src/jev_trading_lab/core/manifest.py`
- Create: `config/phase1.example.json`
- Create: `tests/core/test_manifest.py`

**RED behaviors (execute individually):**
- `tests/core/test_manifest.py::test_manifest_hash_stable` — canonical JSON hash stable
- `tests/core/test_manifest.py::test_alias_and_unknown_key_rejected` — aliases/unknowns/live mode fail
- `tests/core/test_manifest.py::test_required_limits_present` — exact 3/cycle,20/day,3 concurrent,3 attempts,10m,$3/day and every allocation/model/calendar/cost threshold required

**GREEN:** Frozen Pydantic models; strict JSON only; include provider, quota, model, cost, calendar, review, endpoint, statistics and classification settings.

**Focused verification:** `uv run pytest tests/core/test_manifest.py -q`

**Commit:** `git add -- src/jev_trading_lab/core/manifest.py config/phase1.example.json tests/core/test_manifest.py && git commit -m "feat(core): define immutable run manifest"`

### Task 5: NYSE calendar and fixed enrollment endpoint

**Objective:** Resolve E and one run-wide T_m before registration.

**Files:**
- Create: `src/jev_trading_lab/stocks/calendar.py`
- Create: `src/jev_trading_lab/core/run_window.py`
- Create: `tests/core/test_run_window.py`

**RED behaviors (execute individually):**
- `tests/core/test_run_window.py::test_tm_is_tenth_nyse_session_strictly_after_e_plus_30` — strictly-after rule and UTC timestamp
- `tests/core/test_run_window.py::test_dst_holiday_early_close` — pinned calendar handles DST/holidays/early closes
- `tests/core/test_run_window.py::test_same_tm_for_both_streams` — one T_m shared

**GREEN:** Wrap pinned exchange-calendars; persistable RunWindow(E,T_m,calendar version/hash).

**Focused verification:** `uv run pytest tests/core/test_run_window.py -q`

**Commit:** `git add -- src/jev_trading_lab/stocks/calendar.py src/jev_trading_lab/core/run_window.py tests/core/test_run_window.py && git commit -m "feat(core): freeze experiment run window"`

### Task 6: SQLite connection and migration loader

**Objective:** Establish safe local SQLite and immutable migration checksums.

**Files:**
- Create: `src/jev_trading_lab/persistence/db.py`
- Create: `src/jev_trading_lab/persistence/migrations.py`
- Create: `src/jev_trading_lab/persistence/schema/001_migration_metadata.sql`
- Create: `tests/persistence/test_migrations.py`

**RED behaviors (execute individually):**
- `tests/persistence/test_migrations.py::test_connection_pragmas` — FK,WAL,busy timeout,FULL on every connection
- `tests/persistence/test_migrations.py::test_migration_checksum_change_rejected` — applied SQL hash cannot change
- `tests/persistence/test_migrations.py::test_installed_wheel_loads_sql` — package-resource migration applies from wheel
- `tests/persistence/test_migrations.py::test_network_fs_rejected` — unsupported mount blocks startup

**GREEN:** One connection factory; numbered package-resource migrations; schema version+SHA table; 001 contains metadata only. Every migration that creates an immutable table installs that table’s UPDATE/DELETE abort triggers and lineage constraints in the same migration, so every migration prefix is protected immediately; later persisted entities are introduced by new migrations before consumers. Applied SQL is never edited. A migration-chain gate tests clean install, every prefix upgrade, repeated application, checksum mismatch, and wheel resource loading.

**Focused verification:** `uv run pytest tests/persistence/test_migrations.py -q`

**Commit:** `git add -- src/jev_trading_lab/persistence/db.py src/jev_trading_lab/persistence/migrations.py src/jev_trading_lab/persistence/schema/001_migration_metadata.sql tests/persistence/test_migrations.py && git commit -m "feat(persistence): add checked SQLite migrations"`

### Task 7: Core immutable schema

**Objective:** Add run, manifest, account, opportunity, observation, and artifact tables.

**Files:**
- Create: `src/jev_trading_lab/persistence/schema/002_core_entities.sql`
- Create: `src/jev_trading_lab/persistence/core_repo.py`
- Create: `tests/persistence/test_core_schema.py`

**RED behaviors (execute individually):**
- `tests/persistence/test_core_schema.py::test_isolated_arm_accounts` — account identity includes run/stream/arm/currency
- `tests/persistence/test_core_schema.py::test_shared_opportunity_id_excludes_arm` — one opportunity joins all arms
- `tests/persistence/test_core_schema.py::test_manifest_and_run_immutable` — run references one manifest/window/lock hash

**GREEN:** Minimal immutable core tables with fixed-point CHECKs, unique identities, and same-migration append-only/lineage triggers. Migration 003 adds reusable guard infrastructure and repository tests; it never leaves 002 tables temporarily mutable.

**Focused verification:** `uv run pytest tests/persistence/test_core_schema.py -q`

**Commit:** `git add -- src/jev_trading_lab/persistence/schema/002_core_entities.sql src/jev_trading_lab/persistence/core_repo.py tests/persistence/test_core_schema.py && git commit -m "feat(persistence): add immutable core schema"`

### Task 8: Append-only guards and acyclic supersession

**Objective:** Enforce economic/audit immutability at the database layer.

**Files:**
- Create: `src/jev_trading_lab/persistence/schema/003_append_only_guards.sql`
- Create: `src/jev_trading_lab/persistence/journal.py`
- Create: `tests/persistence/test_append_only.py`

**RED behaviors (execute individually):**
- `tests/persistence/test_append_only.py::test_update_delete_abort` — immutable tables reject mutation
- `tests/persistence/test_append_only.py::test_supersession_two_node_cycle_rejected` — A→B→A fails
- `tests/persistence/test_append_only.py::test_supersession_long_cycle_rejected` — long cycle fails
- `tests/persistence/test_append_only.py::test_one_successor_same_identity` — branch/cross-run/type fails

**GREEN:** Abort triggers plus insertion-time recursive ancestry check; repository appends only.

**Focused verification:** `uv run pytest tests/persistence/test_append_only.py -q`

**Commit:** `git add -- src/jev_trading_lab/persistence/schema/003_append_only_guards.sql src/jev_trading_lab/persistence/journal.py tests/persistence/test_append_only.py && git commit -m "feat(persistence): enforce append-only lineage"`

### Task 9: Crash-durable blob publication

**Objective:** Store exact provider artifacts before DB references.

**Files:**
- Create: `src/jev_trading_lab/persistence/blobs.py`
- Create: `tests/persistence/test_blobs.py`

**RED behaviors (execute individually):**
- `tests/persistence/test_blobs.py::test_publication_order` — temp→fsync file→rename→fsync parent→verify then DB reference
- `tests/persistence/test_blobs.py::test_failpoints_leave_no_bad_reference` — kill/disk-full boundaries safe
- `tests/persistence/test_blobs.py::test_corrupt_missing_detected` — hash mismatch/missing fails

**GREEN:** Same-filesystem atomic content-addressed store with credential/signed-URL redaction.

**Focused verification:** `uv run pytest tests/persistence/test_blobs.py -q`

**Commit:** `git add -- src/jev_trading_lab/persistence/blobs.py tests/persistence/test_blobs.py && git commit -m "feat(persistence): add crash-durable artifact store"`

### Task 10: Chart of accounts and balanced postings

**Objective:** Define accounts and database constraints for double-entry events.

**Files:**
- Create: `src/jev_trading_lab/persistence/schema/004_ledger.sql`
- Create: `src/jev_trading_lab/persistence/ledger.py`
- Create: `tests/persistence/test_ledger_postings.py`

**RED behaviors (execute individually):**
- `tests/persistence/test_ledger_postings.py::test_each_transaction_balances` — debits equal credits
- `tests/persistence/test_ledger_postings.py::test_short_proceeds_restricted` — short cash unavailable
- `tests/persistence/test_ledger_postings.py::test_posting_amount_and_scale_constraints` — invalid sign, scale, or imbalance is rejected

**GREEN:** Fixed chart and transactional postings for cash, inventory, receivable, liability, expenses and equity.

**Focused verification:** `uv run pytest tests/persistence/test_ledger_postings.py -q`

**Commit:** `git add -- src/jev_trading_lab/persistence/schema/004_ledger.sql src/jev_trading_lab/persistence/ledger.py tests/persistence/test_ledger_postings.py && git commit -m "feat(accounting): add balanced ledger postings"`

### Task 11: Economic event projectors

**Objective:** Project fills, fees, yield, dividends, borrow, splits, resolutions and model costs.

**Files:**
- Create: `src/jev_trading_lab/persistence/projectors.py`
- Create: `tests/persistence/test_projectors.py`

**RED behaviors (execute individually):**
- `tests/persistence/test_projectors.py::test_long_short_pnl_examples` — approved long/short formulas match hand values
- `tests/persistence/test_projectors.py::test_resolution_and_split_idempotent` — duplicate economic events no-op
- `tests/persistence/test_projectors.py::test_failed_call_cost_posts` — failed/abstained billed calls debit owning arm

**GREEN:** One pure projector per event; opportunity/account dimensions required.

**Focused verification:** `uv run pytest tests/persistence/test_projectors.py -q`

**Commit:** `git add -- src/jev_trading_lab/persistence/projectors.py tests/persistence/test_projectors.py && git commit -m "feat(accounting): project economic events"`

### Task 12: Cost catalog and allocation

**Objective:** Make actual/direct/shared costs reconcile by arm, week, opportunity and residual.

**Files:**
- Create: `src/jev_trading_lab/persistence/schema/005_cost_allocations.sql`
- Create: `src/jev_trading_lab/accounting/costs.py`
- Create: `tests/accounting/test_cost_allocation.py`

**RED behaviors (execute individually):**
- `tests/accounting/test_cost_allocation.py::test_ai_routing_complete` — OpenAI, Jev, retrieval, paid evidence data, incremental AI infrastructure, and billed failures/abstentions debit AI only
- `tests/accounting/test_cost_allocation.py::test_deterministic_specific_debits_control` — control-specific costs debit deterministic only
- `tests/accounting/test_cost_allocation.py::test_shared_50_50_split` — truly shared market data, scheduler, storage, and reporting split 50:50 within stream
- `tests/accounting/test_cost_allocation.py::test_weekly_equal_opportunity_allocation` — remaining weekly cost splits equally across registered opportunities
- `tests/accounting/test_cost_allocation.py::test_zero_opportunity_week_residual` — cost remains owning-arm weekly residual
- `tests/accounting/test_cost_allocation.py::test_all_reconciliation_levels` — opportunities and weekly vectors reconcile arm accounts, stream total, and theta numerator

**GREEN:** Versioned catalog/usage ingestion; direct opportunity allocation; weekly residuals; report development labor/break-even AUM separately.

**Focused verification:** `uv run pytest tests/accounting/test_cost_allocation.py -q`

**Commit:** `git add -- src/jev_trading_lab/persistence/schema/005_cost_allocations.sql src/jev_trading_lab/accounting/costs.py tests/accounting/test_cost_allocation.py && git commit -m "feat(accounting): allocate full economic costs"`

### Task 13: Account reconstruction and property reconciliation

**Objective:** Rebuild exact portfolio state from journal prefixes.

**Files:**
- Create: `src/jev_trading_lab/persistence/reconcile.py`
- Create: `tests/persistence/test_reconcile_properties.py`

**RED behaviors (execute individually):**
- `tests/persistence/test_reconcile_properties.py::test_every_generated_prefix_balances` — Hypothesis event prefixes conserve
- `tests/persistence/test_reconcile_properties.py::test_restart_reconstruction_equal` — fresh interpreter equals uninterrupted
- `tests/persistence/test_reconcile_properties.py::test_violation_blocks_entries_not_management` — reconciliation difference gates entries only

**GREEN:** Pure state reducer and reconciliation equations from design.

**Focused verification:** `uv run pytest tests/persistence/test_reconcile_properties.py -q`

**Commit:** `git add -- src/jev_trading_lab/persistence/reconcile.py tests/persistence/test_reconcile_properties.py && git commit -m "feat(accounting): reconcile reconstructed accounts"`

### Task 14: Run registration after persistence

**Objective:** Transactionally register immutable manifest, lock, calendar, E and T_m.

**Files:**
- Create: `src/jev_trading_lab/application/register.py`
- Create: `src/jev_trading_lab/persistence/schema/006_run_registration.sql`
- Create: `tests/application/test_registration.py`

**RED behaviors (execute individually):**
- `tests/application/test_registration.py::test_registration_stores_all_hashes` — manifest/uv.lock/calendar/window/T_m stored
- `tests/application/test_registration.py::test_registration_is_immutable` — second different registration rejected
- `tests/application/test_registration.py::test_entry_after_e_rejected` — new entries stop after E

**GREEN:** This task creates the immutable registration model and rejection hooks only; it does not expose activation. Final activation CLI is added after all adapters and a durable fresh passing preflight result exist.

**Focused verification:** `uv run pytest tests/application/test_registration.py -q`

**Commit:** `git add -- src/jev_trading_lab/application/register.py src/jev_trading_lab/persistence/schema/006_run_registration.sql tests/application/test_registration.py && git commit -m "feat(application): register immutable experiment run"`

### Task 15: Cycle intent/event state machine

**Objective:** Represent slots append-only with canonical keys and terminal events.

**Files:**
- Create: `src/jev_trading_lab/persistence/schema/007_cycles.sql`
- Create: `src/jev_trading_lab/application/cycles.py`
- Create: `tests/application/test_cycles.py`

**RED behaviors (execute individually):**
- `tests/application/test_cycles.py::test_cycle_key_contract` — hash(run,mode,job,stream,slot)
- `tests/application/test_cycles.py::test_one_terminal_event` — append stages then one terminal
- `tests/application/test_cycles.py::test_missed_slot_not_backfilled` — MISSED_SLOT
- `tests/application/test_cycles.py::test_monotonic_deadline` — wall clock jump irrelevant

**GREEN:** Immutable intent/events; UTC audit times plus injected monotonic clock.

**Focused verification:** `uv run pytest tests/application/test_cycles.py -q`

**Commit:** `git add -- src/jev_trading_lab/persistence/schema/007_cycles.sql src/jev_trading_lab/application/cycles.py tests/application/test_cycles.py && git commit -m "feat(runtime): add immutable cycle events"`

### Task 16: Fenced writer lease

**Objective:** Ensure one persistent writer and reject stale tokens.

**Files:**
- Create: `src/jev_trading_lab/persistence/schema/008_leases.sql`
- Create: `src/jev_trading_lab/application/lease.py`
- Create: `tests/application/test_lease.py`

**RED behaviors (execute individually):**
- `tests/application/test_lease.py::test_n_workers_one_owner` — multiprocess contention
- `tests/application/test_lease.py::test_old_fence_rejected` — expired owner cannot write
- `tests/application/test_lease.py::test_lease_mutation_audited` — mutable row emits immutable event

**GREEN:** File lock + short BEGIN IMMEDIATE lease operations; never span network.

**Focused verification:** `uv run pytest tests/application/test_lease.py -q`

**Commit:** `git add -- src/jev_trading_lab/persistence/schema/008_leases.sql src/jev_trading_lab/application/lease.py tests/application/test_lease.py && git commit -m "feat(runtime): fence the persistent writer"`

### Task 17: Multidimensional quota reservations

**Objective:** Atomically bound calls, tokens, bytes, concurrency, attempts, time and dollars.

**Files:**
- Create: `src/jev_trading_lab/persistence/schema/009_quotas.sql`
- Create: `src/jev_trading_lab/application/budget.py`
- Create: `tests/application/test_budget.py`

**RED behaviors (execute individually):**
- `tests/application/test_budget.py::test_exact_manifest_caps` — 3/cycle,20/day,3 concurrent,3 attempts,10m,$3
- `tests/application/test_budget.py::test_reserve_all_dimensions_atomically` — partial reservation impossible
- `tests/application/test_budget.py::test_priority_slot_then_subject` — deterministic priority
- `tests/application/test_budget.py::test_unknown_attempt_retains_reservation` — restart cannot overshoot

**GREEN:** Pool `(day,stream,entry/review)`; worst-case reservation; actual usage conversion; AI-only exhaustion.

**Focused verification:** `uv run pytest tests/application/test_budget.py -q`

**Commit:** `git add -- src/jev_trading_lab/persistence/schema/009_quotas.sql src/jev_trading_lab/application/budget.py tests/application/test_budget.py && git commit -m "feat(runtime): reserve provider quotas"`

### Task 18: Semantic provider-call attempts

**Objective:** Persist intent before network and recover unknown attempts.

**Files:**
- Create: `src/jev_trading_lab/persistence/schema/010_provider_calls.sql`
- Create: `src/jev_trading_lab/application/calls.py`
- Create: `tests/application/test_calls.py`

**RED behaviors (execute individually):**
- `tests/application/test_calls.py::test_stage_key_accepts_opportunity_or_review` — semantic subject ID participates
- `tests/application/test_calls.py::test_intent_precedes_send` — failpoint proves durable intent
- `tests/application/test_calls.py::test_one_selected_result` — attempts cannot fork semantic result
- `tests/application/test_calls.py::test_retry_exact_input_backoff_deadline` — same hash, <=3 attempts, bounded jitter and deadline

**GREEN:** Call intent/attempt/result records tied to reservation and frozen input.

**Focused verification:** `uv run pytest tests/application/test_calls.py -q`

**Commit:** `git add -- src/jev_trading_lab/persistence/schema/010_provider_calls.sql src/jev_trading_lab/application/calls.py tests/application/test_calls.py && git commit -m "feat(runtime): add crash-safe provider calls"`

### Task 19: Persistence vertical gate

**Objective:** Prove migration→reserve→call→crash→recover→cost→reconcile.

**Files:**
- Create: `tests/integration/test_persistence_gate.py`
- Create: `tests/fixtures/persistence_gate/`

**RED behaviors (execute individually):**
- `tests/integration/test_persistence_gate.py::test_crash_recovery_journal_hash` — restarted and uninterrupted canonical hashes equal
- `tests/integration/test_persistence_gate.py::test_cost_and_quota_reconcile` — usage, residual and ledger agree

**GREEN:** Test-only task; no production special cases.

**Focused verification:** `uv run pytest tests/integration/test_persistence_gate.py -q`

**Commit:** `git add -- tests/integration/test_persistence_gate.py tests/fixtures/persistence_gate/ && git commit -m "test: add persistence recovery gate"`

### Task 20: URL and DNS safety policy

**Objective:** Validate scheme, host allowlist, and address sets before connect.

**Files:**
- Create: `src/jev_trading_lab/evidence/policy.py`
- Create: `tests/evidence/test_url_policy.py`

**RED behaviors (execute individually):**
- `tests/evidence/test_url_policy.py::test_private_and_mapped_ranges_blocked` — private/metadata/mixed/mapped fail
- `tests/evidence/test_url_policy.py::test_credentials_scheme_port_host_rules` — unsafe URL components fail
- `tests/evidence/test_url_policy.py::test_rebinding_change_rejected` — validated set immutable per hop

**GREEN:** Pure URL/DNS policy with injected resolver.

**Focused verification:** `uv run pytest tests/evidence/test_url_policy.py -q`

**Commit:** `git add -- src/jev_trading_lab/evidence/policy.py tests/evidence/test_url_policy.py && git commit -m "feat(evidence): validate retrieval destinations"`

### Task 21: Pinned TLS transport and peer verification

**Objective:** Connect to validated IP while preserving Host/SNI/certificate.

**Files:**
- Create: `src/jev_trading_lab/evidence/transport.py`
- Create: `tests/evidence/test_transport.py`

**RED behaviors (execute individually):**
- `tests/evidence/test_transport.py::test_valid_public_ip_tls` — Host/SNI cert verified
- `tests/evidence/test_transport.py::test_peer_ip_mismatch_rejected` — check-to-connect race fails

**GREEN:** Small transport interface; no redirects or parsing yet.

**Focused verification:** `uv run pytest tests/evidence/test_transport.py -q`

**Commit:** `git add -- src/jev_trading_lab/evidence/transport.py tests/evidence/test_transport.py && git commit -m "feat(evidence): pin safe HTTPS transport"`

### Task 22: Redirect, stream, decompression, and MIME limits

**Objective:** Complete bounded safe retrieval.

**Files:**
- Create: `src/jev_trading_lab/evidence/retriever.py`
- Create: `tests/evidence/test_retriever_limits.py`

**RED behaviors (execute individually):**
- `tests/evidence/test_retriever_limits.py::test_each_redirect_revalidated` — unapproved/private redirect fails
- `tests/evidence/test_retriever_limits.py::test_compressed_and_decompressed_caps` — bomb fails
- `tests/evidence/test_retriever_limits.py::test_mime_document_and_timeout_caps` — limits terminate

**GREEN:** Compose policy+transport; produce ObservationMetadata and immutable bytes.

**Focused verification:** `uv run pytest tests/evidence/test_retriever_limits.py -q`

**Commit:** `git add -- src/jev_trading_lab/evidence/retriever.py tests/evidence/test_retriever_limits.py && git commit -m "feat(evidence): add bounded safe retriever"`

### Task 23: Evidence manifests and quote provenance

**Objective:** Freeze ordered documents and exact normalized spans.

**Files:**
- Create: `src/jev_trading_lab/persistence/schema/011_evidence.sql`
- Create: `src/jev_trading_lab/evidence/models.py`
- Create: `src/jev_trading_lab/evidence/normalize.py`
- Create: `src/jev_trading_lab/evidence/validate.py`
- Create: `tests/evidence/test_validation.py`

**RED behaviors (execute individually):**
- `tests/evidence/test_validation.py::test_manifest_hash_order_and_cutoff` — order/doc/cutoff changes hash
- `tests/evidence/test_validation.py::test_unicode_html_span_roundtrip` — exact stored-version quote
- `tests/evidence/test_validation.py::test_late_or_wrong_version_rejected` — causal provenance fails

**GREEN:** Immutable evidence input manifest and normalized offset map.

**Focused verification:** `uv run pytest tests/evidence/test_validation.py -q`

**Commit:** `git add -- src/jev_trading_lab/persistence/schema/011_evidence.sql src/jev_trading_lab/evidence/models.py src/jev_trading_lab/evidence/normalize.py src/jev_trading_lab/evidence/validate.py tests/evidence/test_validation.py && git commit -m "feat(evidence): freeze cited document manifests"`

### Task 24: OpenAI evidence schema and adapter

**Objective:** Generate the complete structured brief from frozen documents with tools disabled.

**Files:**
- Create: `src/jev_trading_lab/research/models.py`
- Create: `src/jev_trading_lab/research/openai.py`
- Create: `tests/research/test_openai.py`

**RED behaviors (execute individually):**
- `tests/research/test_openai.py::test_complete_brief_schema` — facts(hash,URI,times,span),for,against,contradictions,missing,catalysts,quality required
- `tests/research/test_openai.py::test_tools_absent_and_untrusted_delimiters` — no browsing/tools
- `tests/research/test_openai.py::test_price_and_control_action_absent` — anti-anchoring
- `tests/research/test_openai.py::test_exact_model_fingerprint_usage_raw` — exact `/v1/responses` request/snapshot/version/usage/raw contract
- `tests/research/test_openai.py::test_no_model_or_endpoint_fallback` — fallback forbidden and timestamp semantics exact

**GREEN:** Direct httpx Responses call with strict Pydantic schema and injected transport.

**Focused verification:** `uv run pytest tests/research/test_openai.py -q`

**Commit:** `git add -- src/jev_trading_lab/research/models.py src/jev_trading_lab/research/openai.py tests/research/test_openai.py && git commit -m "feat(research): add frozen-input evidence briefs"`

### Task 25: Jev question schema and adapter

**Objective:** Return all bounded questions with separate probabilities/confidence.

**Files:**
- Create: `src/jev_trading_lab/decision/models.py`
- Create: `src/jev_trading_lab/decision/questions.py`
- Create: `src/jev_trading_lab/decision/jev.py`
- Create: `tests/decision/test_jev.py`
- Modify: `THIRD_PARTY_NOTICES.md`

**RED behaviors (execute individually):**
- `tests/decision/test_jev.py::test_complete_batched_questions` — sufficiency,ambiguity,action+ABSTAIN,quality,market forecast
- `tests/decision/test_jev.py::test_probability_confidence_separate` — never substitute
- `tests/decision/test_jev.py::test_model_revision_drift` — mismatch terminal
- `tests/decision/test_jev.py::test_malformed_and_retry_states` — distinct failures no trade
- `tests/decision/test_jev.py::test_exact_typesafe_contract_no_fallback` — `/v1/systemone`, auth/request/revision fields and fallback prohibition

**GREEN:** Direct TypeSafe contract from provider appendix; adapt only MIT question-builder concepts with attribution.

**Focused verification:** `uv run pytest tests/decision/test_jev.py -q`

**Commit:** `git add -- src/jev_trading_lab/decision/models.py src/jev_trading_lab/decision/questions.py src/jev_trading_lab/decision/jev.py tests/decision/test_jev.py THIRD_PARTY_NOTICES.md && git commit -m "feat(decision): add pinned Jev decisions"`

### Task 26: Frozen model vertical gate

**Objective:** Exercise document→OpenAI→validation→Jev→quota conversion using fixtures.

**Files:**
- Create: `tests/integration/test_model_gate.py`
- Create: `tests/fixtures/models/`

**RED behaviors (execute individually):**
- `tests/integration/test_model_gate.py::test_frozen_input_full_audit_chain` — raw artifacts and timestamps join
- `tests/integration/test_model_gate.py::test_each_failure_disposition_and_cost` — model failures explicit and billed

**GREEN:** Test-only gate; no live credentials.

**Focused verification:** `uv run pytest tests/integration/test_model_gate.py -q`

**Commit:** `git add -- tests/integration/test_model_gate.py tests/fixtures/models/ && git commit -m "test: add frozen model integration gate"`

### Task 27: Causal pipeline timestamp contract

**Objective:** Enforce persisted t0/retrieval/t1/t2/t2j/t3/t4 ordering.

**Files:**
- Create: `src/jev_trading_lab/application/causality.py`
- Create: `tests/application/test_causality.py`

**RED behaviors (execute individually):**
- `tests/application/test_causality.py::test_allowed_equal_and_strict_boundaries` — exact inequality
- `tests/application/test_causality.py::test_every_reordered_stage_rejected` — late evidence/model/snapshot/intent
- `tests/application/test_causality.py::test_retry_hash_and_crash_boundaries` — same frozen inputs

**GREEN:** Transactional validation before any order intent.

**Focused verification:** `uv run pytest tests/application/test_causality.py -q`

**Commit:** `git add -- src/jev_trading_lab/application/causality.py tests/application/test_causality.py && git commit -m "feat(application): enforce causal stage ordering"`

### Task 28: Common and AI-only gates with final dispositions

**Objective:** Map every model result and deterministic gate to exactly one arm disposition.

**Files:**
- Create: `src/jev_trading_lab/persistence/schema/012_decisions_dispositions.sql`
- Create: `src/jev_trading_lab/application/gates.py`
- Create: `src/jev_trading_lab/application/dispositions.py`
- Create: `tests/application/test_gates.py`

**RED behaviors (execute individually):**
- `tests/application/test_gates.py::test_each_common_gate_independently` — registered,fresh,costs,capital,risk,duplicate,runtime,reconciliation
- `tests/application/test_gates.py::test_each_ai_gate_boundary` — suff=.70,ambiguity=.30,action=.55,margin=.15
- `tests/application/test_gates.py::test_complete_result_disposition_table` — ACTION/HOLD/ABSTAIN/errors to reviewed dispositions
- `tests/application/test_gates.py::test_controls_continue_on_ai_failure` — synchronized control/baselines

**GREEN:** Deterministic ordered gate table; only valid ACTION creates AI intent.

**Focused verification:** `uv run pytest tests/application/test_gates.py -q`

**Commit:** `git add -- src/jev_trading_lab/persistence/schema/012_decisions_dispositions.sql src/jev_trading_lab/application/gates.py src/jev_trading_lab/application/dispositions.py tests/application/test_gates.py && git commit -m "feat(application): enforce arm decision gates"`

### Task 29: Generic opportunity orchestrator

**Objective:** Define a provider-neutral orchestrator over injected stream ports before either stream composes it.

**Files:**
- Create: `src/jev_trading_lab/application/orchestrator.py`
- Create: `tests/application/test_orchestrator.py`

**RED behaviors (execute individually):**
- `tests/application/test_orchestrator.py::test_prices_hidden_until_jev` — anti-anchor
- `tests/application/test_orchestrator.py::test_same_t3_all_arms` — synchronized execution
- `tests/application/test_orchestrator.py::test_all_arm_dispositions_and_costs` — AI/control/cash/passive complete

**GREEN:** Use fake discovery/evidence/decision/policy/execution ports. It enforces one t3 snapshot and all-arm dispositions but imports no stream package.

**Focused verification:** `uv run pytest tests/application/test_orchestrator.py -q`

**Commit:** `git add -- src/jev_trading_lab/application/orchestrator.py tests/application/test_orchestrator.py && git commit -m "feat(application): orchestrate synchronized opportunities"`

### Task 30: Polymarket public client contract

**Objective:** Normalize Gamma catalog and CLOB books through observation envelope.

**Files:**
- Create: `src/jev_trading_lab/polymarket/client.py`
- Create: `src/jev_trading_lab/polymarket/models.py`
- Create: `tests/polymarket/test_client.py`
- Create: `tests/fixtures/polymarket/README.md`

**RED behaviors (execute individually):**
- `tests/polymarket/test_client.py::test_market_fields_and_metadata` — IDs/categories/rules/end/volume retained
- `tests/polymarket/test_client.py::test_yes_no_books_and_units` — side mapping integer quantities/raw hash
- `tests/polymarket/test_client.py::test_fixture_provenance` — exact Gamma/CLOB URLs, retrieval/hash/license note
- `tests/polymarket/test_client.py::test_contract_drift_blocks_registration` — schema/timestamp/fallback failure is terminal

**GREEN:** Read-only documented endpoints from provider appendix; no authenticated SDK.

**Focused verification:** `uv run pytest tests/polymarket/test_client.py -q`

**Commit:** `git add -- src/jev_trading_lab/polymarket/client.py src/jev_trading_lab/polymarket/models.py tests/polymarket/test_client.py tests/fixtures/polymarket/README.md && git commit -m "feat(polymarket): add public data client"`

### Task 31: Polymarket eligibility and deterministic ordering

**Objective:** Apply every frozen cheap gate before model spend.

**Files:**
- Create: `src/jev_trading_lab/polymarket/universe.py`
- Create: `tests/polymarket/test_universe.py`

**RED behaviors (execute individually):**
- `tests/polymarket/test_universe.py::test_all_threshold_boundaries` — category,active,1h-30d,$10k,5k contracts,4pt,rules/source
- `tests/polymarket/test_universe.py::test_budget_order_depth_volume_id` — exact order
- `tests/polymarket/test_universe.py::test_first_eligible_only` — no later reentry

**GREEN:** Pure eligibility and opportunity ranking.

**Focused verification:** `uv run pytest tests/polymarket/test_universe.py -q`

**Commit:** `git add -- src/jev_trading_lab/polymarket/universe.py tests/polymarket/test_universe.py && git commit -m "feat(polymarket): select eligible markets"`

### Task 32: Polymarket clustering artifact

**Objective:** Freeze event-cluster membership and contract choice.

**Files:**
- Create: `src/jev_trading_lab/polymarket/clusters.py`
- Create: `src/jev_trading_lab/persistence/schema/013_polymarket_clusters.sql`
- Create: `tests/polymarket/test_clusters.py`

**RED behaviors (execute individually):**
- `tests/polymarket/test_clusters.py::test_versioned_inputs_and_tiebreak` — volume/depth/ID selection
- `tests/polymarket/test_clusters.py::test_ambiguous_rejected` — no guessed membership
- `tests/polymarket/test_clusters.py::test_history_never_rewritten` — new version appends

**GREEN:** Deterministic metadata clustering with immutable artifact.

**Focused verification:** `uv run pytest tests/polymarket/test_clusters.py -q`

**Commit:** `git add -- src/jev_trading_lab/polymarket/clusters.py src/jev_trading_lab/persistence/schema/013_polymarket_clusters.sql tests/polymarket/test_clusters.py && git commit -m "feat(polymarket): freeze event clusters"`

### Task 33: Polymarket edge and integer quantity set

**Objective:** Implement exact YES/NO economics and all independent caps.

**Files:**
- Create: `src/jev_trading_lab/polymarket/policy.py`
- Create: `tests/polymarket/test_policy.py`

**RED behaviors (execute individually):**
- `tests/polymarket/test_policy.py::test_hand_calculated_yes_no_edges` — VWAP and friction threshold exact
- `tests/polymarket/test_policy.py::test_sigma_floor_and_risk_budget` — B=.5%, floor .05 dollars/contract
- `tests/polymarket/test_policy.py::test_each_cap_binds` — 10% position/cluster,50% gross,5 clusters,5% two-point depth,1% volume,60s freshness
- `tests/polymarket/test_policy.py::test_q_empty_no_trade` — exhaustive integer set

**GREEN:** Use bounded exhaustive integer enumeration; no binary search unless a later proof/test establishes monotonicity.

**Focused verification:** `uv run pytest tests/polymarket/test_policy.py -q`

**Commit:** `git add -- src/jev_trading_lab/polymarket/policy.py tests/polymarket/test_policy.py && git commit -m "feat(polymarket): calculate causal edge and size"`

### Task 34: Polymarket paper fills and conservative marks

**Objective:** Consume captured levels and prohibit optimistic liquidity.

**Files:**
- Create: `src/jev_trading_lab/polymarket/execution.py`
- Create: `src/jev_trading_lab/persistence/schema/014_orders_fills.sql`
- Create: `tests/polymarket/test_execution.py`

**RED behaviors (execute individually):**
- `tests/polymarket/test_execution.py::test_partial_fill_by_levels` — never exceed captured depth
- `tests/polymarket/test_execution.py::test_adverse_depth_floors_each_level` — floor(quantity/2)
- `tests/polymarket/test_execution.py::test_beyond_bid_depth_marks_zero` — T_m conservative mark
- `tests/polymarket/test_execution.py::test_quote_temporal_constraint` — quote not after intent

**GREEN:** Paper order/fill events only.

**Focused verification:** `uv run pytest tests/polymarket/test_execution.py -q`

**Commit:** `git add -- src/jev_trading_lab/polymarket/execution.py src/jev_trading_lab/persistence/schema/014_orders_fills.sql tests/polymarket/test_execution.py && git commit -m "feat(polymarket): simulate book-depth fills"`

### Task 35: Polymarket deterministic probability control

**Objective:** Create frozen independent comparator with no AI imports.

**Files:**
- Create: `src/jev_trading_lab/polymarket/control.py`
- Create: `artifacts/polymarket-control.example.json`
- Create: `tests/polymarket/test_control.py`
- Create: `tests/architecture/test_control_boundaries.py`

**RED behaviors (execute individually):**
- `tests/polymarket/test_control.py::test_probability_from_frozen_coefficients` — 24h change/volatility mapping
- `tests/polymarket/test_control.py::test_control_uses_own_edge_and_exit` — never AI output
- `tests/architecture/test_control_boundaries.py::test_controls_cannot_import_models` — dependency boundary

**GREEN:** Frozen pre-run coefficients/provenance; same policy/fill caps.

**Focused verification:** `uv run pytest tests/polymarket/test_control.py tests/architecture/test_control_boundaries.py -q`

**Commit:** `git add -- src/jev_trading_lab/polymarket/control.py artifacts/polymarket-control.example.json tests/polymarket/test_control.py tests/architecture/test_control_boundaries.py && git commit -m "feat(polymarket): add deterministic comparator"`

### Task 36: Polymarket position reviews and mechanical exits

**Objective:** Separate entry and management permissions and exact exit states.

**Files:**
- Create: `src/jev_trading_lab/polymarket/manage.py`
- Create: `src/jev_trading_lab/persistence/schema/015_position_reviews.sql`
- Create: `tests/polymarket/test_management.py`

**RED behaviors (execute individually):**
- `tests/polymarket/test_management.py::test_review_id_and_one_disposition` — 15m position/slot/policy identity
- `tests/polymarket/test_management.py::test_common_exit_thresholds` — 15 adverse,10 favorable,7d exact
- `tests/polymarket/test_management.py::test_model_failure_mechanical_exits_continue` — entry/model exit block only
- `tests/polymarket/test_management.py::test_exit_pending_data` — no fill + conservative mark

**GREEN:** Explicit `entry_enabled` and `position_management_enabled`; NO_CHANGE/EXIT_INTENT/failure dispositions.

**Focused verification:** `uv run pytest tests/polymarket/test_management.py -q`

**Commit:** `git add -- src/jev_trading_lab/polymarket/manage.py src/jev_trading_lab/persistence/schema/015_position_reviews.sql tests/polymarket/test_management.py && git commit -m "feat(polymarket): manage paper positions"`

### Task 37: Polymarket resolution ingestion

**Objective:** Post official payouts without guessing disputes.

**Files:**
- Create: `src/jev_trading_lab/polymarket/resolution.py`
- Create: `tests/polymarket/test_resolution.py`

**RED behaviors (execute individually):**
- `tests/polymarket/test_resolution.py::test_payout_set_and_idempotent_posting` — [0,1] or frozen admissible payout
- `tests/polymarket/test_resolution.py::test_dispute_unavailable_remains_open` — no invented resolution
- `tests/polymarket/test_resolution.py::test_superseding_source_appends` — correction immutable

**GREEN:** Resolution source document plus catalog state; ledger posting projector.

**Focused verification:** `uv run pytest tests/polymarket/test_resolution.py -q`

**Commit:** `git add -- src/jev_trading_lab/polymarket/resolution.py tests/polymarket/test_resolution.py && git commit -m "feat(polymarket): ingest official resolution"`

### Task 38: Forecast baselines and baseline dispositions

**Objective:** Implement exact frozen forecast comparators used for Brier and missing-prediction substitution.

**Files:**
- Create: `src/jev_trading_lab/baselines/forecast.py`
- Create: `artifacts/stock-prevalence.example.json`
- Create: `tests/baselines/test_forecast.py`

**RED behaviors (execute individually):**
- `tests/baselines/test_forecast.py::test_polymarket_baseline_common_t3_midpoint` — cluster forecast uses executable common t3 midpoint
- `tests/baselines/test_forecast.py::test_stock_baseline_frozen_identical_candidate_prevalence` — pre-run prevalence provenance/hash
- `tests/baselines/test_forecast.py::test_baseline_recorded_and_missing_substitution` — BASELINE_RECORDED disposition and exact replacement

**GREEN:** Immutable forecast-baseline artifact/API used by opportunity pipeline and scoring; no account for forecast-only baseline.

**Focused verification:** `uv run pytest tests/baselines/test_forecast.py -q`

**Commit:** `git add -- src/jev_trading_lab/baselines/forecast.py artifacts/stock-prevalence.example.json tests/baselines/test_forecast.py && git commit -m "feat(baselines): add frozen forecast comparators"`

### Task 39: Polymarket opportunity composition

**Objective:** Compose the generic orchestrator with real Polymarket handlers before its vertical slice.

**Files:**
- Create: `src/jev_trading_lab/polymarket/pipeline.py`
- Create: `tests/polymarket/test_pipeline.py`

**RED behaviors (execute individually):**
- `tests/polymarket/test_pipeline.py::test_real_handlers_enforce_causality_and_gates` — t0-t4, reviewed gates, t3 snapshot
- `tests/polymarket/test_pipeline.py::test_all_arms_and_baselines_terminal` — AI/control/cash/forecast dispositions
- `tests/polymarket/test_pipeline.py::test_no_slice_bypass` — pipeline is only entry order-intent path

**GREEN:** Wire discovery, evidence, models, policies, execution, baseline and journal ports; no duplicate formulas.

**Focused verification:** `uv run pytest tests/polymarket/test_pipeline.py -q`

**Commit:** `git add -- src/jev_trading_lab/polymarket/pipeline.py tests/polymarket/test_pipeline.py && git commit -m "feat(polymarket): compose opportunity pipeline"`

### Task 40: Polymarket vertical slice

**Objective:** Run snapshot→arms→fill→review→resolution→ledger.

**Files:**
- Create: `tests/integration/test_polymarket_slice.py`
- Create: `tests/fixtures/polymarket_slice/`

**RED behaviors (execute individually):**
- `tests/integration/test_polymarket_slice.py::test_full_slice_canonical_hash` — all dispositions/economics expected
- `tests/integration/test_polymarket_slice.py::test_ai_budget_failure_control_continues` — control unaffected

**GREEN:** Test-only gate; all production handlers already exist.

**Focused verification:** `uv run pytest tests/integration/test_polymarket_slice.py -q`

**Commit:** `git add -- tests/integration/test_polymarket_slice.py tests/fixtures/polymarket_slice/ && git commit -m "test: add Polymarket vertical slice"`

### Task 41: Frozen S&P 100 universe and candidate scoring

**Objective:** Commit a provenance-bearing membership artifact and exact top-20 formula.

**Files:**
- Create: `src/jev_trading_lab/stocks/universe.py`
- Create: `artifacts/sp100-membership.example.json`
- Create: `tests/stocks/test_universe.py`

**RED behaviors (execute individually):**
- `tests/stocks/test_universe.py::test_membership_hash_frozen` — changes do not rewrite run
- `tests/stocks/test_universe.py::test_standardization_and_ticker_tie` — exact 5d/volume z-score formula
- `tests/stocks/test_universe.py::test_top20_shared_order` — all arms same candidates

**GREEN:** Artifact carries source/retrieval/hash/license note; pure causal scoring.

**Focused verification:** `uv run pytest tests/stocks/test_universe.py -q`

**Commit:** `git add -- src/jev_trading_lab/stocks/universe.py artifacts/sp100-membership.example.json tests/stocks/test_universe.py && git commit -m "feat(stocks): freeze universe and candidates"`

### Task 42: Stock OHLCV provider and date joins

**Objective:** Normalize Yahoo chart data under explicit preflight blocker.

**Files:**
- Create: `src/jev_trading_lab/stocks/market_data.py`
- Create: `tests/stocks/test_market_data.py`
- Create: `tests/fixtures/stocks/README.md`

**RED behaviors (execute individually):**
- `tests/stocks/test_market_data.py::test_raw_ohlcv_metadata` — raw values/envelope/timezone
- `tests/stocks/test_market_data.py::test_join_by_exchange_date` — no index/raw timestamp alignment
- `tests/stocks/test_market_data.py::test_schema_drift_blocks` — exact Yahoo v8 fields/timestamps mismatch blocks registration
- `tests/stocks/test_market_data.py::test_no_silent_fallback` — only registered provider allowed

**GREEN:** Provider contract appendix endpoint; raw data only; failure blocks registration/cycle.

**Focused verification:** `uv run pytest tests/stocks/test_market_data.py -q`

**Commit:** `git add -- src/jev_trading_lab/stocks/market_data.py tests/stocks/test_market_data.py tests/fixtures/stocks/README.md && git commit -m "feat(stocks): add causal OHLCV adapter"`

### Task 43: Corporate actions and total-return events

**Objective:** Model splits, dividends, delistings, and distributions explicitly.

**Files:**
- Create: `src/jev_trading_lab/stocks/corporate_actions.py`
- Create: `tests/stocks/test_corporate_actions.py`

**RED behaviors (execute individually):**
- `tests/stocks/test_corporate_actions.py::test_split_and_dividend_postings` — raw prices plus explicit cash/quantity events
- `tests/stocks/test_corporate_actions.py::test_delisting_distribution` — total shareholder return complete
- `tests/stocks/test_corporate_actions.py::test_ex_date_scheduler_idempotent` — long receipt/short liability once

**GREEN:** Observation-enveloped actions and ledger projectors.

**Focused verification:** `uv run pytest tests/stocks/test_corporate_actions.py -q`

**Commit:** `git add -- src/jev_trading_lab/stocks/corporate_actions.py tests/stocks/test_corporate_actions.py && git commit -m "feat(stocks): add corporate-action events"`

### Task 44: SEC and issuer evidence

**Objective:** Collect exact seven-calendar-day evidence with filing availability.

**Files:**
- Create: `src/jev_trading_lab/stocks/sec.py`
- Create: `src/jev_trading_lab/stocks/evidence.py`
- Create: `tests/stocks/test_evidence.py`

**RED behaviors (execute individually):**
- `tests/stocks/test_evidence.py::test_sec_acceptance_time_controls_availability` — future filing excluded
- `tests/stocks/test_evidence.py::test_exact_seven_day_boundary` — inclusive/exclusive cutoff fixed
- `tests/stocks/test_evidence.py::test_amendment_appends` — never overwrite
- `tests/stocks/test_evidence.py::test_sec_rate_user_agent` — exact SEC endpoints/contact/rate/backoff contract
- `tests/stocks/test_evidence.py::test_rss_discovery_then_original_source` — RSS/Google index leads to safe original-source retrieval, never evidence substitution
- `tests/stocks/test_evidence.py::test_provider_failure_blocks_registration` — timestamp/schema/fallback rules exact

**GREEN:** Safe retriever with provider appendix endpoints and manifest allowlist.

**Focused verification:** `uv run pytest tests/stocks/test_evidence.py -q`

**Commit:** `git add -- src/jev_trading_lab/stocks/sec.py src/jev_trading_lab/stocks/evidence.py tests/stocks/test_evidence.py && git commit -m "feat(stocks): collect point-in-time evidence"`

### Task 45: Stock deterministic control

**Objective:** Implement exact SMA/return comparator independent of AI.

**Files:**
- Create: `src/jev_trading_lab/stocks/control.py`
- Create: `tests/stocks/test_control.py`

**RED behaviors (execute individually):**
- `tests/stocks/test_control.py::test_long_rule_exact` — close>50SMA and 20d/5d positive
- `tests/stocks/test_control.py::test_short_disabled_phase1` — inverse returns FLAT in long/cash run
- `tests/stocks/test_control.py::test_no_ai_state_access` — approved DTO only

**GREEN:** Pure long/cash control following candidate order.

**Focused verification:** `uv run pytest tests/stocks/test_control.py -q`

**Commit:** `git add -- src/jev_trading_lab/stocks/control.py tests/stocks/test_control.py && git commit -m "feat(stocks): add deterministic swing control"`

### Task 46: Stock sizing and exposure gates

**Objective:** Calculate shares at modeled fill with all reviewed caps.

**Files:**
- Create: `src/jev_trading_lab/stocks/policy.py`
- Create: `tests/stocks/test_policy.py`

**RED behaviors (execute individually):**
- `tests/stocks/test_policy.py::test_integer_shares_and_atr_stop` — B=.5%,d=2ATR,fill-based q
- `tests/stocks/test_policy.py::test_position_gross_sector_five_caps` — each binds separately
- `tests/stocks/test_policy.py::test_canonical_capacity_rejection` — order determines explicit zero outcome

**GREEN:** Long/cash only; no confidence sizing/pyramiding.

**Focused verification:** `uv run pytest tests/stocks/test_policy.py -q`

**Commit:** `git add -- src/jev_trading_lab/stocks/policy.py tests/stocks/test_policy.py && git commit -m "feat(stocks): size paper swing positions"`

### Task 47: Next-open stock execution and costs

**Objective:** Persist intent at close and fill only from official D+1 open.

**Files:**
- Create: `src/jev_trading_lab/stocks/execution.py`
- Create: `src/jev_trading_lab/persistence/schema/016_stock_intents.sql`
- Create: `tests/stocks/test_execution.py`
- Create: `artifacts/stock-spread-model.example.json`

**RED behaviors (execute individually):**
- `tests/stocks/test_execution.py::test_no_fill_before_official_open` — causal next-open
- `tests/stocks/test_execution.py::test_gap_stale_late_cancel` — >10% and missing paths
- `tests/stocks/test_execution.py::test_long_fill_cost_signs` — 5bp half-spread+5bp slippage
- `tests/stocks/test_execution.py::test_date_effective_sale_fees` — SEC/TAF schedule only applicable sales

**GREEN:** No shorts until approved borrow provider; immutable pending intent/events.

**Focused verification:** `uv run pytest tests/stocks/test_execution.py -q`

**Commit:** `git add -- src/jev_trading_lab/stocks/execution.py src/jev_trading_lab/persistence/schema/016_stock_intents.sql tests/stocks/test_execution.py artifacts/stock-spread-model.example.json && git commit -m "feat(stocks): execute causal next-open paper fills"`

### Task 48: Stock reviews and mechanical exits

**Objective:** Review open AI positions after each close while preserving stops/targets/time exits.

**Files:**
- Create: `src/jev_trading_lab/stocks/manage.py`
- Create: `tests/stocks/test_management.py`

**RED behaviors (execute individually):**
- `tests/stocks/test_management.py::test_review_id_and_one_disposition` — position/session/policy identity
- `tests/stocks/test_management.py::test_stop_target_time_and_collision` — 2ATR/3ATR/10d/stop-first/gap-open
- `tests/stocks/test_management.py::test_failure_keeps_mechanical_management` — entry/model exit block only
- `tests/stocks/test_management.py::test_exit_pending_data` — no fill conservative mark

**GREEN:** Same causal review contract as Polymarket; signal flip creates next-open exit intent.

**Focused verification:** `uv run pytest tests/stocks/test_management.py -q`

**Commit:** `git add -- src/jev_trading_lab/stocks/manage.py tests/stocks/test_management.py && git commit -m "feat(stocks): manage swing positions"`

### Task 49: Fixed stock forecast outcomes

**Objective:** Create arm-independent common-reference D+1-to-D+10 labels.

**Files:**
- Create: `src/jev_trading_lab/stocks/outcomes.py`
- Create: `tests/stocks/test_outcomes.py`

**RED behaviors (execute individually):**
- `tests/stocks/test_outcomes.py::test_common_open_reference_not_arm_fill` — label independent of action/exit
- `tests/stocks/test_outcomes.py::test_total_shareholder_return` — split/dividend/delisting included
- `tests/stocks/test_outcomes.py::test_missing_at_tm_explicit` — no silent exclusion

**GREEN:** Binary outcome artifact with observation metadata.

**Focused verification:** `uv run pytest tests/stocks/test_outcomes.py -q`

**Commit:** `git add -- src/jev_trading_lab/stocks/outcomes.py tests/stocks/test_outcomes.py && git commit -m "feat(stocks): collect fixed forecast outcomes"`

### Task 50: Treasury yield and cash accrual baseline

**Objective:** Accrue equal causal cash yield in every account.

**Files:**
- Create: `src/jev_trading_lab/baselines/treasury.py`
- Create: `src/jev_trading_lab/baselines/cash.py`
- Create: `tests/baselines/test_cash.py`

**RED behaviors (execute individually):**
- `tests/baselines/test_cash.py::test_point_in_time_dgs3mo` — published prior observation only
- `tests/baselines/test_cash.py::test_daily_accrual_restart_idempotent` — weekend/missing/restart
- `tests/baselines/test_cash.py::test_equal_arm_treatment` — same uninvested cash earns same yield
- `tests/baselines/test_cash.py::test_exact_fred_contract_no_fallback` — endpoint/schema/timestamp drift blocks

**GREEN:** FRED contract from appendix; immutable daily rate artifact; balanced accrual postings.

**Focused verification:** `uv run pytest tests/baselines/test_cash.py -q`

**Commit:** `git add -- src/jev_trading_lab/baselines/treasury.py src/jev_trading_lab/baselines/cash.py tests/baselines/test_cash.py && git commit -m "feat(baselines): add interest-bearing cash"`

### Task 51: Exposure-matched OEF passive account

**Objective:** Implement 50% OEF/50% cash month-end next-open benchmark.

**Files:**
- Create: `src/jev_trading_lab/baselines/oef.py`
- Create: `tests/baselines/test_oef.py`

**RED behaviors (execute individually):**
- `tests/baselines/test_oef.py::test_month_end_target_and_next_open` — calendar/holiday behavior
- `tests/baselines/test_oef.py::test_costs_dividends_partial_period` — same execution model
- `tests/baselines/test_oef.py::test_unavailable_open_event` — explicit failure and later rule

**GREEN:** Isolated passive account, corporate actions, costs, reconciliation.

**Focused verification:** `uv run pytest tests/baselines/test_oef.py -q`

**Commit:** `git add -- src/jev_trading_lab/baselines/oef.py tests/baselines/test_oef.py && git commit -m "feat(baselines): add exposure-matched passive"`

### Task 52: Daily marks and fixed T_m valuation

**Objective:** Create synchronized marked economic equity and final endpoint snapshot.

**Files:**
- Create: `src/jev_trading_lab/persistence/schema/017_valuations.sql`
- Create: `src/jev_trading_lab/accounting/valuation.py`
- Create: `tests/accounting/test_valuation.py`

**RED behaviors (execute individually):**
- `tests/accounting/test_valuation.py::test_daily_synchronized_marks` — fixed timestamp and equity equation
- `tests/accounting/test_valuation.py::test_tm_same_both_streams` — persisted run window used
- `tests/accounting/test_valuation.py::test_polymarket_depth_excess_zero` — conservative
- `tests/accounting/test_valuation.py::test_no_entries_after_e_management_continues` — endpoint discipline

**GREEN:** Worker job posts daily marks and immutable T_m endpoint.

**Focused verification:** `uv run pytest tests/accounting/test_valuation.py -q`

**Commit:** `git add -- src/jev_trading_lab/persistence/schema/017_valuations.sql src/jev_trading_lab/accounting/valuation.py tests/accounting/test_valuation.py && git commit -m "feat(accounting): add synchronized valuation endpoint"`

### Task 53: Stock opportunity composition

**Objective:** Compose the generic orchestrator with real stock and baseline handlers before its vertical slice.

**Files:**
- Create: `src/jev_trading_lab/stocks/pipeline.py`
- Create: `tests/stocks/test_pipeline.py`

**RED behaviors (execute individually):**
- `tests/stocks/test_pipeline.py::test_close_to_next_open_real_path` — causality/gates/intents through handlers
- `tests/stocks/test_pipeline.py::test_all_accounts_dispositions` — AI/control/cash/OEF/forecast complete
- `tests/stocks/test_pipeline.py::test_no_slice_bypass` — pipeline is only stock entry intent path

**GREEN:** Wire stock candidates/evidence/models/policies/execution/cash/OEF/forecast/journal into orchestrator.

**Focused verification:** `uv run pytest tests/stocks/test_pipeline.py -q`

**Commit:** `git add -- src/jev_trading_lab/stocks/pipeline.py tests/stocks/test_pipeline.py && git commit -m "feat(stocks): compose opportunity pipeline"`

### Task 54: Stock vertical slice

**Objective:** Exercise close decision→next-open→management→action→D10/passive→ledger.

**Files:**
- Create: `tests/integration/test_stock_slice.py`
- Create: `tests/fixtures/stock_slice/`

**RED behaviors (execute individually):**
- `tests/integration/test_stock_slice.py::test_full_slice_canonical_hash` — expected economics/dispositions/outcome
- `tests/integration/test_stock_slice.py::test_restart_and_provider_failure` — same journal; baselines continue

**GREEN:** Test-only gate; all production handlers already exist.

**Focused verification:** `uv run pytest tests/integration/test_stock_slice.py -q`

**Commit:** `git add -- tests/integration/test_stock_slice.py tests/fixtures/stock_slice/ && git commit -m "test: add stock vertical slice"`

### Task 55: Scheduler kernel with fake handlers

**Objective:** Define manifest-driven dispatch independently of production jobs.

**Files:**
- Create: `src/jev_trading_lab/application/scheduler.py`
- Create: `tests/application/test_scheduler.py`

**RED behaviors (execute individually):**
- `tests/application/test_scheduler.py::test_dispatch_matrix_fields` — job,stream,cadence,deadline,priority,permissions,handler
- `tests/application/test_scheduler.py::test_calendar_slots_and_backpressure` — no overlap/catchup
- `tests/application/test_scheduler.py::test_sigterm_bounded_shutdown` — network/DB boundaries
- `tests/application/test_scheduler.py::test_lease_renewal_failure` — stop stale worker

**GREEN:** Injected clocks/fake handlers only; no production worker wiring yet.

**Focused verification:** `uv run pytest tests/application/test_scheduler.py -q`

**Commit:** `git add -- src/jev_trading_lab/application/scheduler.py tests/application/test_scheduler.py && git commit -m "feat(runtime): add deterministic scheduler kernel"`

### Task 56: Runtime identity and deployment verifier

**Objective:** Record and validate complete running identity before/after first cycle.

**Files:**
- Create: `src/jev_trading_lab/persistence/schema/018_runtime_health.sql`
- Create: `src/jev_trading_lab/application/runtime.py`
- Create: `tests/application/test_runtime_identity.py`

**RED behaviors (execute individually):**
- `tests/application/test_runtime_identity.py::test_every_identity_field` — path,commit,dirty,lock,schema,prompts,host,boot,PID start,units
- `tests/application/test_runtime_identity.py::test_zero_multiple_stale_mismatch` — all fail
- `tests/application/test_runtime_identity.py::test_before_first_and_newest_cycle` — live startup then startup/fence agreement

**GREEN:** Persistent-worker startup/heartbeat identity plus deployment-verify service.

**Focused verification:** `uv run pytest tests/application/test_runtime_identity.py -q`

**Commit:** `git add -- src/jev_trading_lab/persistence/schema/018_runtime_health.sql src/jev_trading_lab/application/runtime.py tests/application/test_runtime_identity.py && git commit -m "feat(runtime): verify deployed process identity"`

### Task 57: Economic return and P&L metrics

**Objective:** Compute authoritative trading/economic P&L, return, turnover and profit factor.

**Files:**
- Create: `src/jev_trading_lab/evaluation/economic.py`
- Create: `tests/evaluation/test_economic.py`

**RED behaviors (execute individually):**
- `tests/evaluation/test_economic.py::test_authoritative_formulas` — poly/long/short/cost examples
- `tests/evaluation/test_economic.py::test_total_return_turnover_profit_factor` — no-loss factor null
- `tests/evaluation/test_economic.py::test_theta_reconciles_accounts` — cost residuals and N×R0 exact

**GREEN:** Read-only account/journal calculations.

**Focused verification:** `uv run pytest tests/evaluation/test_economic.py -q`

**Commit:** `git add -- src/jev_trading_lab/evaluation/economic.py tests/evaluation/test_economic.py && git commit -m "feat(evaluation): add economic metrics"`

### Task 58: Risk and retention metrics

**Objective:** Compute daily drawdown, weekly loss, ES95 and activity denominators.

**Files:**
- Create: `src/jev_trading_lab/evaluation/risk.py`
- Create: `tests/evaluation/test_risk.py`

**RED behaviors (execute individually):**
- `tests/evaluation/test_risk.py::test_daily_drawdown_formula` — peak-to-subsequent-trough percentage
- `tests/evaluation/test_risk.py::test_weekly_loss_same_weeks` — exact -1% gate input
- `tests/evaluation/test_risk.py::test_retention_and_zero_denominator` — count/dollar-day; inconclusive
- `tests/evaluation/test_risk.py::test_es95_interval_short_sample` — descriptive/inconclusive explicit

**GREEN:** Frozen daily/week timestamps; ES95 descriptive only.

**Focused verification:** `uv run pytest tests/evaluation/test_risk.py -q`

**Commit:** `git add -- src/jev_trading_lab/evaluation/risk.py tests/evaluation/test_risk.py && git commit -m "feat(evaluation): add risk and retention metrics"`

### Task 59: Full-cohort forecast metrics

**Objective:** Score Brier/log loss/calibration without selective omission.

**Files:**
- Create: `src/jev_trading_lab/evaluation/scoring.py`
- Create: `tests/evaluation/test_scoring.py`

**RED behaviors (execute individually):**
- `tests/evaluation/test_scoring.py::test_missing_prediction_uses_baseline` — full cohort skill
- `tests/evaluation/test_scoring.py::test_valid_only_coverage_and_calibration` — intercept/slope/10-bin ECE counts
- `tests/evaluation/test_scoring.py::test_log_loss_clipping_frozen` — exact clipping
- `tests/evaluation/test_scoring.py::test_joint_missing_label_bounds` — paired score diff; skill same assignments

**GREEN:** Polymarket admissible payout set and stock binary label; no independent ratio bounds.

**Focused verification:** `uv run pytest tests/evaluation/test_scoring.py -q`

**Commit:** `git add -- src/jev_trading_lab/evaluation/scoring.py tests/evaluation/test_scoring.py && git commit -m "feat(evaluation): add full-cohort forecast scoring"`

### Task 60: Circular stationary bootstrap and Holm test

**Objective:** Implement exact synchronized-week inference formulas.

**Files:**
- Create: `src/jev_trading_lab/evaluation/bootstrap.py`
- Create: `tests/evaluation/test_bootstrap.py`
- Modify: `THIRD_PARTY_NOTICES.md`

**RED behaviors (execute individually):**
- `tests/evaluation/test_bootstrap.py::test_week_vectors_ratio_and_shared_indices` — X,n synchronized streams
- `tests/evaluation/test_bootstrap.py::test_zero_denominator_redraw_limit` — 999,990 cap
- `tests/evaluation/test_bootstrap.py::test_null_centered_pvalue_plus_one` — exact upper tail
- `tests/evaluation/test_bootstrap.py::test_97500_basic_bound` — exact order statistic
- `tests/evaluation/test_bootstrap.py::test_holm_both_required` — 025/.05 rule

**GREEN:** Adapt stationary block sampler from pinned MIT repo with attribution; normal tests reduced replicates, release marker runs 99,999.

**Focused verification:** `uv run pytest tests/evaluation/test_bootstrap.py -q`

**Commit:** `git add -- src/jev_trading_lab/evaluation/bootstrap.py tests/evaluation/test_bootstrap.py THIRD_PARTY_NOTICES.md && git commit -m "feat(evaluation): add preregistered bootstrap and Holm"`

### Task 61: Paired permutation diagnostic

**Objective:** Add synchronized paired permutation without substituting it for primary bootstrap.

**Files:**
- Create: `src/jev_trading_lab/evaluation/permutation.py`
- Create: `tests/evaluation/test_permutation.py`

**RED behaviors (execute individually):**
- `tests/evaluation/test_permutation.py::test_pairing_preserved` — week vectors swap signs jointly
- `tests/evaluation/test_permutation.py::test_known_null_and_effect` — deterministic p behavior

**GREEN:** Frozen RNG and replicate count in manifest; descriptive diagnostic.

**Focused verification:** `uv run pytest tests/evaluation/test_permutation.py -q`

**Commit:** `git add -- src/jev_trading_lab/evaluation/permutation.py tests/evaluation/test_permutation.py && git commit -m "feat(evaluation): add paired permutation diagnostic"`

### Task 62: Effective weeks, block length, and power simulation

**Objective:** Freeze L and B_min before confirmation.

**Files:**
- Create: `src/jev_trading_lab/evaluation/power.py`
- Create: `tests/evaluation/test_power.py`

**RED behaviors (execute individually):**
- `tests/evaluation/test_power.py::test_initial_positive_sequence_effective_weeks` — spectral formula
- `tests/evaluation/test_power.py::test_alt_strictly_above_null` — theta_alt>.03
- `tests/evaluation/test_power.py::test_power_increases_with_weeks` — >=80% joint Holm
- `tests/evaluation/test_power.py::test_confirmatory_eligibility` — max(26,Bmin),100 clusters/26 weeks,zero-opportunity weeks count

**GREEN:** Pre-run simulated paired variance/dependence/missingness; persist chosen L/B_min artifact.

**Focused verification:** `uv run pytest tests/evaluation/test_power.py -q`

**Commit:** `git add -- src/jev_trading_lab/evaluation/power.py tests/evaluation/test_power.py && git commit -m "feat(evaluation): add power and effective-week analysis"`

### Task 63: Full adverse-policy replay

**Objective:** Replay economics rather than revalue booked trades.

**Files:**
- Create: `src/jev_trading_lab/evaluation/replay.py`
- Create: `tests/evaluation/test_replay.py`

**RED behaviors (execute individually):**
- `tests/evaluation/test_replay.py::test_joint_adverse_full_policy` — 1.5x costs, floor levels/2,zero bid,edge,size,fills,exits
- `tests/evaluation/test_replay.py::test_statutory_fee_and_cash_unchanged` — not scaled
- `tests/evaluation/test_replay.py::test_threshold_grids_exploratory_only` — cannot alter base classification

**GREEN:** Replay frozen causal inputs through pure policies.

**Focused verification:** `uv run pytest tests/evaluation/test_replay.py -q`

**Commit:** `git add -- src/jev_trading_lab/evaluation/replay.py tests/evaluation/test_replay.py && git commit -m "feat(evaluation): replay adverse full policy"`

### Task 64: Exhaustive pilot classification

**Objective:** Encode every branch and numeric gate with precedence.

**Files:**
- Create: `src/jev_trading_lab/evaluation/classify.py`
- Create: `tests/evaluation/test_classify.py`

**RED behaviors (execute individually):**
- `tests/evaluation/test_classify.py::test_operational_precedence` — violation or <99% terminal wins
- `tests/evaluation/test_classify.py::test_inconclusive_conditions` — incomplete/min-info/zero denominators
- `tests/evaluation/test_classify.py::test_each_promising_gate_decisive` — theta,cumulative,cash/passive,drawdown+2pt/10%,weekly,60% ratios
- `tests/evaluation/test_classify.py::test_base_adverse_both_pass_match` — otherwise no evidence

**GREEN:** Table-driven decision tree with exact field/threshold/null handling.

**Focused verification:** `uv run pytest tests/evaluation/test_classify.py -q`

**Commit:** `git add -- src/jev_trading_lab/evaluation/classify.py tests/evaluation/test_classify.py && git commit -m "feat(evaluation): classify feasibility pilot"`

### Task 65: Evaluation vertical and statistics review gate

**Objective:** Run one persisted synthetic journal through valuation, costs, weekly vectors, inference, adverse replay and classification.

**Files:**
- Create: `tests/integration/test_evaluation_gate.py`
- Create: `tests/fixtures/evaluation_gate/`

**RED behaviors (execute individually):**
- `tests/integration/test_evaluation_gate.py::test_full_evaluation_path_expected_hash` — journal to final metrics/classification
- `tests/integration/test_evaluation_gate.py::test_base_and_adverse_traceability` — every output traces to inputs
- `tests/integration/test_evaluation_gate.py::test_no_selective_missingness` — full cohort preserved

**GREEN:** Test-only gate. Record independent statistics review in task delivery evidence and stop before reporting on any BLOCKER/MAJOR.

**Focused verification:** `uv run pytest tests/integration/test_evaluation_gate.py -q`

**Commit:** `git add -- tests/integration/test_evaluation_gate.py tests/fixtures/evaluation_gate/ && git commit -m "test: add evaluation vertical gate"`

### Task 66: Static read-only reports

**Objective:** Generate traceable dashboard/report with complete decomposition.

**Files:**
- Create: `src/jev_trading_lab/reporting/report.py`
- Create: `src/jev_trading_lab/reporting/dashboard.py`
- Create: `src/jev_trading_lab/reporting/templates/dashboard.html`
- Create: `tests/reporting/test_reporting.py`

**RED behaviors (execute individually):**
- `tests/reporting/test_reporting.py::test_all_cost_pnl_components` — realized/unrealized/yield/dividend/borrow/market/statutory/model/data/infra/labor
- `tests/reporting/test_reporting.py::test_every_headline_traceable` — opportunity/decision/fill IDs
- `tests/reporting/test_reporting.py::test_readonly_fixed_parameterized_sql` — no writes/injection
- `tests/reporting/test_reporting.py::test_secret_safe_negative_visible` — errors/abstentions not hidden

**GREEN:** Static HTML/JSON only; show bootstrap/permutation/log loss/ES/effective weeks and assumptions.

**Focused verification:** `uv run pytest tests/reporting/test_reporting.py -q`

**Commit:** `git add -- src/jev_trading_lab/reporting/report.py src/jev_trading_lab/reporting/dashboard.py src/jev_trading_lab/reporting/templates/dashboard.html tests/reporting/test_reporting.py && git commit -m "feat(reporting): add auditable static reports"`

### Task 67: Consistent database/blob backup and restore

**Objective:** Snapshot DB then exactly its referenced blobs and prove clean restore.

**Files:**
- Create: `src/jev_trading_lab/persistence/backup.py`
- Create: `src/jev_trading_lab/application/recovery.py`
- Create: `tests/persistence/test_backup_restore.py`

**RED behaviors (execute individually):**
- `tests/persistence/test_backup_restore.py::test_snapshot_then_referenced_blobs` — online backup enumerates exact hashes
- `tests/persistence/test_backup_restore.py::test_atomic_manifest_and_failpoints` — kill/disk/concurrent artifact safe
- `tests/persistence/test_backup_restore.py::test_empty_destination_restore` — integrity/hash/reconcile/RTO
- `tests/persistence/test_backup_restore.py::test_nonempty_requires_explicit_destructive_flag` — safe default

**GREEN:** SQLite backup API/VACUUM INTO; blob manifest atomic publish; RPO24h/RTO2h.

**Focused verification:** `uv run pytest tests/persistence/test_backup_restore.py -q`

**Commit:** `git add -- src/jev_trading_lab/persistence/backup.py src/jev_trading_lab/application/recovery.py tests/persistence/test_backup_restore.py && git commit -m "feat(ops): add consistent backup and restore"`

### Task 68: Migration-chain and prefix-upgrade gate

**Objective:** Verify all numbered migrations and append-only guards from clean DB and every prior prefix.

**Files:**
- Create: `tests/integration/test_migration_chain.py`

**RED behaviors (execute individually):**
- `tests/integration/test_migration_chain.py::test_clean_database_full_chain` — all packaged SQL applies
- `tests/integration/test_migration_chain.py::test_every_prefix_upgrades` — each version upgrades to latest
- `tests/integration/test_migration_chain.py::test_each_new_immutable_table_guarded` — UPDATE/DELETE abort
- `tests/integration/test_migration_chain.py::test_wheel_resources` — installed wheel contains/applies migrations/templates

**GREEN:** Test-only gate; fail on gaps, checksum edits, table ownership ambiguity, or missing guard.

**Focused verification:** `uv run pytest tests/integration/test_migration_chain.py -q`

**Commit:** `git add -- tests/integration/test_migration_chain.py && git commit -m "test: verify complete migration chain"`

### Task 69: Production worker dispatch and recovery order

**Objective:** Wire completed handlers into one persistent worker.

**Files:**
- Create: `src/jev_trading_lab/application/worker.py`
- Modify: `src/jev_trading_lab/cli.py`
- Create: `tests/application/test_worker.py`

**RED behaviors (execute individually):**
- `tests/application/test_worker.py::test_startup_order` — lock→lease→identity→recover→reconcile→entry gate→dispatch
- `tests/application/test_worker.py::test_every_design_command` — all CLI commands exist/test
- `tests/application/test_worker.py::test_sigterm_and_lease_loss` — bounded shutdown/no stale writes
- `tests/application/test_worker.py::test_entry_closes_at_e_management_to_tm` — window discipline

**GREEN:** Manifest dispatch matrix for scans, open execution, management, accrual, marks, resolutions, evaluation/reporting/backup.

**Focused verification:** `uv run pytest tests/application/test_worker.py -q`

**Commit:** `git add -- src/jev_trading_lab/application/worker.py src/jev_trading_lab/cli.py tests/application/test_worker.py && git commit -m "feat(runtime): wire persistent paper worker"`

### Task 70: Dual-stream golden replay

**Objective:** Verify uninterrupted and restarted runs produce identical economics and reports.

**Files:**
- Create: `tests/integration/test_golden_run.py`
- Create: `tests/integration/test_restart_equivalence.py`
- Create: `tests/fixtures/golden/`

**RED behaviors (execute individually):**
- `tests/integration/test_golden_run.py::test_expected_journal_account_tm_report_hashes` — full dual stream
- `tests/integration/test_restart_equivalence.py::test_every_boundary_restart_equal` — canonical equality

**GREEN:** Test-only; no missing production handler may be added here.

**Focused verification:** `uv run pytest tests/integration/test_golden_run.py tests/integration/test_restart_equivalence.py -q`

**Commit:** `git add -- tests/integration/test_golden_run.py tests/integration/test_restart_equivalence.py tests/fixtures/golden/ && git commit -m "test: add dual-stream golden replay"`

### Task 71: Failure matrix by boundary

**Objective:** Prove containment for infrastructure/provider/calendar failures.

**Files:**
- Create: `tests/integration/test_failure_matrix.py`
- Create: `tests/integration/test_calendar_failures.py`

**RED behaviors (execute individually):**
- `tests/integration/test_failure_matrix.py::test_disk_lock_wal_blob_failpoints` — exact terminal/postings
- `tests/integration/test_failure_matrix.py::test_timeout_rate_model_drift` — entries block; management continues
- `tests/integration/test_failure_matrix.py::test_duplicate_fence_dirty_runtime` — no duplicate events
- `tests/integration/test_calendar_failures.py::test_dst_holiday_early_late_missed` — explicit slot outcomes

**GREEN:** Write each scenario RED before adding only necessary handling; rerun whole matrix per GREEN.

**Focused verification:** `uv run pytest tests/integration/test_failure_matrix.py tests/integration/test_calendar_failures.py -q`

**Commit:** `git add -- tests/integration/test_failure_matrix.py tests/integration/test_calendar_failures.py && git commit -m "test: prove failure containment"`

### Task 72: Systemd service and restore-drill operations

**Objective:** Install persistent worker, daily backup, and monthly restore drill.

**Files:**
- Create: `ops/jev-trading-lab.service`
- Create: `ops/jev-trading-lab-backup.service`
- Create: `ops/jev-trading-lab-backup.timer`
- Create: `ops/jev-trading-lab-restore-drill.service`
- Create: `ops/jev-trading-lab-restore-drill.timer`
- Create: `scripts/install-user-units.sh`
- Create: `docs/operations.md`
- Create: `tests/test_systemd_policy.py`

**RED behaviors (execute individually):**
- `tests/test_systemd_policy.py::test_worker_only_forward_writer` — no business prompts/live flags
- `tests/test_systemd_policy.py::test_backup_and_monthly_restore_real_commands` — immutable result/RTO
- `tests/test_systemd_policy.py::test_unit_hashes_runtime_identity` — installed content verified

**GREEN:** Minimal user units; do not start registered experiment.

**Focused verification:** `uv run pytest tests/test_systemd_policy.py -q && systemd-analyze --user verify ops/*.service ops/*.timer`

**Commit:** `git add -- ops/jev-trading-lab.service ops/jev-trading-lab-backup.service ops/jev-trading-lab-backup.timer ops/jev-trading-lab-restore-drill.service ops/jev-trading-lab-restore-drill.timer scripts/install-user-units.sh docs/operations.md tests/test_systemd_policy.py && git commit -m "ops: add verified paper worker units"`

### Task 73: Durable provider preflight and activation

**Objective:** Persist all-or-nothing read-only provider eligibility and expose registration activation that requires it.

**Files:**
- Create: `src/jev_trading_lab/persistence/schema/019_preflight.sql`
- Create: `src/jev_trading_lab/application/preflight.py`
- Create: `src/jev_trading_lab/application/activate.py`
- Modify: `src/jev_trading_lab/cli.py`
- Create: `tests/integration/test_preflight_activation.py`
- Create: `docs/preflight-checklist.md`

**RED behaviors (execute individually):**
- `tests/integration/test_preflight_activation.py::test_each_provider_contract_local_case` — every appendix row has adapter-local and aggregate status
- `tests/integration/test_preflight_activation.py::test_missing_stale_failed_blocks_activation` — durable freshness/hash rules
- `tests/integration/test_preflight_activation.py::test_cli_nonzero_and_machine_report` — read-only preflight exact command
- `tests/integration/test_preflight_activation.py::test_activation_requires_passing_report` — immutable registration rejects otherwise

**GREEN:** CLI: `jev-trading-lab preflight --manifest ...` then `activate-run`; no network mutation or provider fallback.

**Focused verification:** `uv run pytest tests/integration/test_preflight_activation.py -q`

**Commit:** `git add -- src/jev_trading_lab/persistence/schema/019_preflight.sql src/jev_trading_lab/application/preflight.py src/jev_trading_lab/application/activate.py src/jev_trading_lab/cli.py tests/integration/test_preflight_activation.py docs/preflight-checklist.md && git commit -m "feat(ops): gate activation on durable preflight"`

### Task 74: Release verification and reviewed PR

**Objective:** Run every quality/safety/operational gate in a correction-review loop.

**Files:**
- Modify: `README.md`
- Modify: `THIRD_PARTY_NOTICES.md`
- Modify: `tests/test_package_policy.py`
- Create: `scripts/verify-release.sh`
- Create: `docs/verification.md`

**RED behaviors (execute individually):**
- `tests/test_package_policy.py::test_release_traceability` — design requirements map to files/tests
- `tests/integration/test_golden_run.py::test_expected_journal_account_tm_report_hashes` — release evidence current

**GREEN:** Implement `scripts/verify-release.sh` as one fail-fast command covering full suite/lint/format/mypy/build; clean-wheel install+CLI+migrate+render; safety/secret scans; actual 99,999-replicate marked inference; golden/failure/backup-restore/systemd gates; and read-only live preflight. Run it at candidate HEAD; record exact commands, exit codes/counts and wheel+lock+migration+golden+backup hashes; commit release artifacts. Complete and commit the mandatory Task 74 delivery-evidence file before the final push. Push the feature branch with `git push -u origin HEAD`; verify local HEAD equals configured upstream and tree is clean; open PR and require CI plus independent review against that exact pushed HEAD. Fix via RED/GREEN commits, rerun the entire release script, update and commit verification plus Task 74 delivery evidence, push again, reverify upstream parity/clean tree, and obtain CI plus independent correction review against the new exact HEAD. Exit only when the latest pushed HEAD has zero BLOCKER/MAJOR findings; never push main directly.

**Focused verification:** `scripts/verify-release.sh`

**Commit:** `git add -- README.md THIRD_PARTY_NOTICES.md tests/test_package_policy.py scripts/verify-release.sh docs/verification.md && git commit -m "docs: verify release candidate"`

---

## Phase gates

- After persistence gate: architecture/accounting review.
- After model gate: security/provider review.
- After each market slice: finance/causality review.
- After evaluation: independent statistics review.
- Before registration: full release loop and read-only provider preflight.

No phase starts until its gate and full suite pass. External provider failure is a blocker, not permission to substitute an unreviewed API.

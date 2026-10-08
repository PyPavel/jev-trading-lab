# Polymarket Feasibility Vertical Slice Implementation Plan

> **For Hermes:** Use `subagent-driven-development` to execute one vertical unit at a time. For every unit, dispatch a fresh implementer, then a fresh spec reviewer, then a fresh code-quality reviewer. Do not start the next unit until both reviews approve.

**Goal:** Produce the first synchronized, paper-only AI-versus-deterministic-control Polymarket observations in 2–4 days, with an offline fixture replay working before any live public-data polling.

**Architecture:** A narrow Polymarket pipeline freezes public evidence and causal books, asks OpenAI for a structured evidence brief with tools absent, asks Jev for a bounded probability/action including `ABSTAIN`, then lets deterministic code calculate control probability, edge, costs, quantity, paper fills, arm-local capital, P&L, and forecast output. Content-addressed blobs preserve exact inputs/outputs; a small append-only JSONL journal preserves observations. No wallet, signing, private Polymarket endpoint, or live order path exists.

**Tech stack:** Python 3.12, stdlib HTTP/JSON, Pydantic 2, Typer, existing fixed-point values and `BlobStore`, pytest.

---

## Replacement decision

This plan supersedes Tasks 10–74 of `docs/plans/2026-10-02-jev-trading-lab-implementation.md` for the feasibility milestone. Tasks 1–8 remain accepted foundations. Treat Task 9 as good enough; do not harden it further unless this slice exposes a blocking defect.

Build only the path below. Do not add a generic opportunity framework, stock stream, UI, daemon, leases, quotas, full ledger, backup/restore, generalized SSRF framework, position-management engine, confirmatory statistics, or production deployment. Do not fetch arbitrary resolution-source URLs in this slice; the frozen Gamma record is the evidence source. JSONL is the result store; existing SQLite is not on the critical path.

## Non-negotiable invariants

1. **Paper only:** no wallet package, key/signing field, order submission endpoint, allowance, deposit, withdrawal, or private Polymarket API.
2. **No price leakage:** OpenAI and Jev receive `EvidencePacket`/`ResearchBrief` only. Those schemas reject token IDs, outcome prices, bids, asks, midpoint, spread, volume-derived probability, and book history. The Jev result artifact is published before execution-book prices become scoring inputs.
3. **Synchronized arms:** AI and deterministic arms see the same eligible market, cutoff, execution book, costs, risk limits, and mark/resolution. Signals differ; capital and positions never mix.
4. **Independent control:** `control.py` imports neither `research` nor `decision`; it uses only causal pre-cutoff midpoint history and a frozen formula.
5. **Deterministic economics:** models never calculate edge, costs, quantity, fills, capital, P&L, or Brier score.
6. **Exact replay:** raw provider bytes, normalized records, model requests/responses, decisions, books, and final results are SHA-256-addressed. Replay verifies hashes and performs no network calls.
7. **Every candidate yields a record:** action, `ABSTAIN`, eligibility rejection, model failure, and no-edge outcomes all append an observation; zero-fill arms get zero trading P&L rather than disappearing.
8. **One-way live boundary:** public GETs are limited to hardcoded Gamma/CLOB hosts. Model POSTs are limited to the two configured model endpoints. No Polymarket POST exists.

## Execution and review protocol for every unit

1. Dispatch one fresh implementer with only the current unit, this plan, and relevant existing files. The implementer writes tests first, runs the unit commands, and commits one atomic commit.
2. Dispatch a fresh **spec reviewer**. It checks only this plan’s requirements and reports missing, extra, or misinterpreted behavior with file/line evidence. The implementer fixes findings; repeat spec review until approved.
3. Dispatch a fresh **quality reviewer** only after spec approval. It checks correctness, boundary validation, test quality, maintainability, and accidental live-trading capability. The implementer fixes findings; repeat quality review until approved.
4. Record implementer and reviewer verdicts before moving on. Never combine spec and quality review. Never let a reviewer implement its own requested changes.
5. Keep each unit’s diff minimal. If existing persistence resists the slice, keep the JSONL/blob path rather than expanding persistence scope.

---

## Unit 1: Freeze contracts and one complete canned cycle

**Objective:** Define the smallest strict data contract and a resolved fixture that can drive every later unit without network or credentials.

**Files:**
- Create: `src/jev_trading_lab/polymarket/__init__.py`
- Create: `src/jev_trading_lab/polymarket/models.py`
- Create: `tests/fixtures/polymarket/feasibility_cycle.json`
- Create: `tests/polymarket/test_contracts.py`

**Contract:**

- `EvidencePacket`: market ID, question, description, rules, category/tags, resolution source text, close time, retrieval/cutoff timestamps, and source SHA-256. Configure `extra="forbid"`; do not include token IDs or any price/book field.
- `BookSnapshot`: market/token IDs, retrieval time, source SHA-256, and integer-microprice/integer-quantity bid/ask levels in deterministic order.
- `ResearchBrief`: cited facts, arguments for/against, contradictions, missing evidence, and quality flag. Every citation names the evidence SHA-256.
- `JevDecision`: `p_yes_micros` in `[0, 1_000_000]`, action in `BUY_YES|BUY_NO|ABSTAIN`, confidence micros, sufficiency micros, ambiguity micros, and concise rationale.
- `ArmResult`: arm, signal probability/action, disposition, entry side/VWAP/quantity/fees, initial/final cash, mark or payout, trading/economic P&L, model cost, and forecast fields.
- `ObservationResult`: IDs/hashes/timestamps plus exactly one AI and one deterministic `ArmResult`, shared market baseline probability, resolution if known, and paired P&L/forecast deltas. Paired P&L deltas persist the exact AI-minus-deterministic signed int64 value, or `null` when that exact difference is outside signed int64; they are never clamped.
- Identity formulas are fixed and minimal: `opportunity_id` is SHA-256 of canonical JSON containing `policy_version`, `market_id`, and `cutoff_at`; `observation_id` is SHA-256 of canonical JSON containing `opportunity_id`, `evidence_sha256`, `execution_book_sha256`, and `policy_version`.
- The fixture contains raw Gamma bytes, sanitized evidence, causal pre-cutoff history, precheck and execution books, canned OpenAI/Jev request-response bytes, actual model costs, a resolution payout, and expected canonical hashes/results. One fixture is enough.
- Unit 1 validates the complete fixture shape and structural bindings across artifacts, hashes, market/token identity, chronology, model requests/responses, decisions, billed costs, arm capital, and resolution. Chronology requires `decision_frozen_at < execution_book.retrieved_at <= observation.observed_at`; a resolution requires `execution_book.retrieved_at < resolution.resolved_at`, and fixture `observed_at` equals that terminal result time. Expected arm/observation records are strict, canonical provisional expected values: Unit 1 verifies their exact duplication and hashes, while Unit 2 validates how those values are derived.
- Unit 1 does **not** derive control probability from causal history, walk books, or compute VWAP, fees, sizing, P&L, baseline probability, or Brier scores. Unit 2 is the single source of truth for those calculations and must reject fixture expected results detached from causal history, books, or frozen economics.

**Tests first:**

- Fixture validates under every strict model and round-trips canonically.
- Adding `outcomePrices`, `bestBid`, `token_id`, `bids`, `asks`, or `midpoint` to `EvidencePacket` fails validation.
- Prices/probabilities outside `[0, 1]`, nonpositive quantities, crossed books, malformed hashes, non-UTC timestamps, and unknown actions fail.
- `ABSTAIN` is valid and cannot carry an entry side/quantity.
- Structural validation rejects detached market/token identity, chronology, canonical request/response content, Jev decision, billed costs, capital, resolution, expected arm/observation duplication, or expected hashes without implementing Unit 2 arithmetic.

**Boundary:** Unit 1 owns canonical shape and binding only. Passing Unit 1 means the fixture is internally linked, not that its control or economics outputs are correctly derived.

**Commands:**

```bash
uv run pytest tests/polymarket/test_contracts.py -q
uv run ruff check src/jev_trading_lab/polymarket tests/polymarket/test_contracts.py
uv run mypy src
```

**Done when:** Fixture loads into strict contracts and the no-price evidence boundary is executable, not a comment.

---

## Unit 2: Deterministic control, edge, risk, quantity, and paper fill

**Objective:** Produce both arms’ economics from frozen inputs without model-owned arithmetic or shared capital.

**Files:**
- Create: `src/jev_trading_lab/polymarket/control.py`
- Create: `src/jev_trading_lab/polymarket/paper.py`
- Create: `tests/polymarket/test_control.py`
- Create: `tests/polymarket/test_paper.py`
- Create: `tests/architecture/test_polymarket_control_boundary.py`

**Frozen feasibility policy:**

- Control input is midpoint history with timestamps strictly earlier than the opportunity cutoff. With fewer than two points, action is `ABSTAIN`. Otherwise `p_control = clip(last_midpoint + clip(last_midpoint - first_midpoint, -0.10, 0.10), 0.05, 0.95)`; choose the better side only after edge calculation.
- AI probability is the frozen Jev `p_yes`; `ABSTAIN`, insufficient evidence below `0.70`, ambiguity above `0.30`, or invalid output produces no order.
- For quantity `q`, walk the shared execution asks to calculate side-specific VWAP. Gross edge is `p_yes - yes_vwap` or `(1-p_yes) - no_vwap`.
- Require `gross_edge >= max(0.05, 2 * (entry_fee_pp + exit_fee_pp + exit_slippage_pp) + 0.03)`.
- Search positive integer quantity downward from the minimum of: `floor(0.005 * equity / max(sigma_p, 0.05))`, `floor(0.10 * equity / side_vwap)`, `floor(0.05 * displayed_side_depth)`, and available arm cash. If no quantity passes edge after VWAP/costs, disposition is `NO_EDGE`.
- Fill only captured ask levels in order. Never exceed captured quantity; cancel remainder. Use exact integer/fixed-point arithmetic from `core.values`, with one explicit rounding direction per conversion.
- Give AI and deterministic arms separate `$10,000` starting capital. Charge OpenAI/Jev costs only to AI; charge no synthetic model cost to control. Report trading P&L separately from economic P&L.
- At resolution, `P&L = q * (payout - entry_vwap) - entry_fees - exit_fees`; on unresolved observations, conservatively mark from executable bid depth and mark excess quantity at zero.
- Forecast baseline is the shared execution-book midpoint. If payout is known, calculate AI/control/baseline Brier and paired deltas; otherwise emit `null` scores with `PENDING_RESOLUTION`.

**Tests first:**

- Control output is deterministic, rejects post-cutoff history, and has no AI/model imports.
- Both sides’ edge signs, depth-walk VWAP, threshold equality, each size cap, partial depth, rounding, and zero-quantity behavior are covered.
- AI costs cannot reduce deterministic capital; one arm’s fill cannot reduce the other’s cash/depth accounting.
- Fixture resolution produces exact cash, trading P&L, economic P&L, Brier values, and paired deltas.
- Recompute fixture expected control probability and every expected arm/observation economic value from causal history, captured books, and frozen economics; reject any fixture expected value that is detached from those source inputs even when Unit 1 structural hashes and duplicated records are internally consistent.
- Property-style parameter cases assert no negative cash, no overfill, and `filled_quantity <= captured_quantity`.

**Commands:**

```bash
uv run pytest tests/polymarket/test_control.py tests/polymarket/test_paper.py tests/architecture/test_polymarket_control_boundary.py -q
uv run ruff check src/jev_trading_lab/polymarket tests/polymarket tests/architecture/test_polymarket_control_boundary.py
uv run mypy src
```

**Done when:** The canned inputs generate exact, separate AI/control paper outcomes and forecast scores with no provider code involved.

---

## Unit 3: OpenAI brief and Jev bounded decision adapters

**Objective:** Convert only frozen, price-free evidence into a strict brief and bounded Jev probability/action, using injectable transports for replay.

**Files:**
- Create: `src/jev_trading_lab/polymarket/ai.py`
- Create: `tests/polymarket/test_ai.py`

**Adapter contract:**

- Implement small transport callables returning status, headers, raw bytes, retrieval time, and billed cost. Tests inject fixture transports; live HTTP is added only in Unit 6.
- OpenAI request uses the configured immutable model snapshot and strict JSON schema. Omit the `tools` key entirely; do not send an empty tools list. Send only canonical `EvidencePacket` bytes between fixed delimiters.
- Validate returned model ID and structured output before constructing `ResearchBrief`. Unknown fields, invalid citations, non-JSON, truncation, version mismatch, or provider error become typed failure results, not exceptions that lose the observation.
- Jev request is exactly `{model,state,questions}`. `state` contains the frozen brief and safe evidence metadata only. Questions elicit evidence sufficiency, ambiguity, `p_yes`, action including `ABSTAIN`, and confidence.
- Validate returned model ID, every bound, and action. No fallback model, retry model, OpenRouter route, or tool use.
- Preserve canonical request/response bytes and provider cost for publication by the caller.

**Tests first:**

- Serialized OpenAI request has no `tools` key and no forbidden market-price field at any nesting level.
- Serialized Jev state has no book/token/price field and cannot be built from a `BookSnapshot`.
- Canned responses map to the expected brief and decision.
- Malformed JSON, extra keys, wrong model, invalid citation, out-of-range probability, missing `ABSTAIN`, HTTP failure, and timeout produce explicit typed failures.
- Canned transport call count is exactly one per provider and no environment credential appears in artifacts or errors.

**Commands:**

```bash
uv run pytest tests/polymarket/test_ai.py -q
uv run ruff check src/jev_trading_lab/polymarket/ai.py tests/polymarket/test_ai.py
uv run mypy src
```

**Done when:** Fixture transports produce a validated Jev decision and leakage tests prove neither model sees market probability.

---

## Unit 4: Exact artifact freeze and append-only observation journal

**Objective:** Make fixture/live runs replayable without extending the existing database design.

**Files:**
- Create: `src/jev_trading_lab/polymarket/store.py`
- Create: `tests/polymarket/test_store.py`

**Storage contract:**

- Reuse `persistence.blobs.BlobStore.publish` for exact public/model bytes. Verify returned digest and byte size before appending references.
- Store a canonical JSON envelope per artifact: kind, SHA-256, byte size, relative blob path, source URI label, retrieval time, opportunity ID, and predecessor stage hash.
- Append one canonical line per terminal `ObservationResult` to `<output>/results.jsonl` using `O_APPEND`, restrictive permissions, flush, and `fsync`. Refuse an existing non-regular file or symlink.
- Observation identity uses the Unit 1 formula over opportunity ID, evidence hash, execution-book hash, and policy version. Replaying the same identity verifies and returns the existing result; conflicting bytes for the same identity fail closed.
- A replay loader resolves every referenced blob, verifies SHA-256/size, and reconstructs contracts. Missing/corrupt artifacts stop replay.
- Do not introduce migrations, mutable result rows, a repository abstraction, or backup machinery.

**Tests first:**

- Raw fixture bytes publish and read byte-for-byte with expected hash.
- Journal lines parse as strict `ObservationResult`; duplicate-identical replay is idempotent and duplicate-conflicting replay fails.
- Truncated JSONL tail, bad hash, missing blob, symlink output, and concurrent small appends are detected or handled without silently accepting corruption.
- Serialized artifacts contain no `OPENAI_API_KEY`, `TYPESAFE_API_KEY`, Authorization header, or fixture secret sentinel.

**Commands:**

```bash
uv run pytest tests/polymarket/test_store.py -q
uv run ruff check src/jev_trading_lab/polymarket/store.py tests/polymarket/test_store.py
uv run mypy src
```

**Done when:** Every stage can be reconstructed from verified artifacts and terminal results are append-only.

---

## Unit 5: Offline orchestrator and runnable fixture CLI

**Objective:** Complete the first end-to-end observation from the canned fixture before writing live polling code.

**Files:**
- Create: `src/jev_trading_lab/polymarket/pipeline.py`
- Modify: `src/jev_trading_lab/cli.py`
- Create: `tests/integration/test_polymarket_fixture_replay.py`
- Create: `tests/integration/test_polymarket_cli.py`

**Pipeline order:**

1. Load and hash raw Gamma bytes; validate/sanitize `EvidencePacket`; publish both.
2. Build and publish OpenAI request, canned response, and validated brief.
3. Build and publish Jev request, canned response, and validated `JevDecision`.
4. Publish a `decision_frozen` stage record. Only after this record exists may the engine load the execution book for edge/scoring.
5. Build control probability from pre-cutoff history, then apply AI and control independently to the same execution book and separate capital.
6. Publish final `ObservationResult`; append it to `results.jsonl`; print a compact JSON summary containing dispositions, quantities, trading/economic P&L, forecast scores, and paired deltas.

**CLI:**

```bash
uv run jev-lab polymarket replay --fixture tests/fixtures/polymarket/feasibility_cycle.json --output-dir .tmp/polymarket-replay
```

The command requires no API keys, performs no network access, exits `0`, and prints one JSON object. A second identical invocation is idempotent and prints the same observation ID/result.

**Tests first:**

- Patch socket/URL opening to fail if called; replay still succeeds.
- Assert artifact stage order and prove book-price access occurs after `decision_frozen`.
- Assert both arm records exist even when either arm abstains/no-fills.
- Assert printed summary and JSONL agree exactly with fixture expected values.
- Assert CLI has no wallet, sign, approve, deposit, withdraw, execute, or live-order command/option.

**Commands:**

```bash
uv run pytest tests/integration/test_polymarket_fixture_replay.py tests/integration/test_polymarket_cli.py -q
uv run jev-lab polymarket replay --fixture tests/fixtures/polymarket/feasibility_cycle.json --output-dir .tmp/polymarket-replay
uv run ruff check src tests/integration/test_polymarket_fixture_replay.py tests/integration/test_polymarket_cli.py
uv run mypy src
```

**Done when:** The CLI creates verified artifacts plus `results.jsonl` and emits paired P&L/forecast output entirely offline. Do not start Unit 6 before both reviews approve this gate.

---

## Unit 6: Bounded public polling and first live paper observations

**Objective:** Replace fixture input with bounded public Gamma/CLOB reads and real model calls while preserving the reviewed pipeline and paper-only boundary.

**Files:**
- Create: `src/jev_trading_lab/polymarket/client.py`
- Modify: `src/jev_trading_lab/polymarket/ai.py`
- Modify: `src/jev_trading_lab/polymarket/pipeline.py`
- Modify: `src/jev_trading_lab/cli.py`
- Create: `tests/polymarket/test_client.py`
- Create: `tests/integration/test_polymarket_live_boundary.py`

**Public-data behavior:**

- GET only hardcoded `https://gamma-api.polymarket.com/markets` and `https://clob.polymarket.com/book?token_id=...`. Bound pages, candidates (`--limit`, maximum 20), redirects (none), timeout, and response bytes. Reject host/scheme changes.
- Normalize deterministic market order by close time then market ID. Exclude closed/inactive, sports category/tag denylist, missing rules/resolution source, resolution outside 1 hour–30 days, trailing 24-hour volume below `$10,000`, spread above `0.04`, or visible two-point depth below 5,000 contracts.
- Freeze the precheck book for eligibility/history, but never serialize it into either model request. After `decision_frozen`, fetch and freeze one fresh execution book; both arms use it. If stale, changed token identity, crossed, or unavailable, append a synchronized no-fill observation.
- OpenAI POST only to `https://api.openai.com/v1/responses`; Jev POST only to `https://api.typesafe.ai/v1/systemone`. Read credentials from environment at send time and never persist headers. One attempt per provider for this slice; failures become observations.
- `observe` defaults to one market and paper output. It must contain no flag capable of enabling real orders.

**CLI:**

```bash
OPENAI_API_KEY=... TYPESAFE_API_KEY=... uv run jev-lab polymarket observe --limit 1 --output-dir var/polymarket-feasibility
```

For unresolved live markets, output arm-local fills/marks/economic P&L and probabilities with Brier fields `null/PENDING_RESOLUTION`. A later replay may supply a frozen resolution fixture; automated resolution polling is out of scope.

**Tests first:**

- Canned HTTP responses cover pagination, deterministic ordering, sports exclusion, every liquidity gate, malformed payloads, timeout, redirect, oversized body, wrong host, stale book, and provider failure.
- Capture both model request bodies and recursively assert absence of market/token/price/book fields.
- Capture all HTTP methods/hosts and assert Gamma/CLOB are GET-only and no Polymarket POST occurs.
- Assert a mixed AI action/control abstain still writes both arm records against separate `$10,000` capital.
- Existing package-policy test continues to reject wallet/signing/trading dependencies and entry points.

**Commands:**

```bash
uv run pytest tests/polymarket/test_client.py tests/integration/test_polymarket_live_boundary.py tests/test_package_policy.py -q
uv run pytest -q
uv run ruff check src tests
uv run mypy src
uv run pip-audit
```

**Manual smoke after tests and only when both API keys are available:**

```bash
uv run jev-lab polymarket observe --limit 1 --output-dir var/polymarket-feasibility
```

Inspect the emitted observation ID, both arm dispositions, artifact hashes, AI/control capital, P&L, model costs, baseline probability, and pending forecast status. Missing keys are an honest blocked smoke test, not permission to add fallback models or fabricate output.

**Done when:** At least one public market produces a synchronized terminal observation, or a truthful synchronized rejection/failure observation, through the same pipeline proven by fixture replay.

---

## Final acceptance gate

Run each command separately:

```bash
uv run jev-lab polymarket replay --fixture tests/fixtures/polymarket/feasibility_cycle.json --output-dir .tmp/final-replay
uv run pytest -q
uv run ruff check src tests
uv run mypy src
uv run pip-audit
```

Accept the slice only if:

- fixture replay works without network/keys and precedes any live smoke;
- exact artifacts replay with verified hashes;
- AI and control records share one opportunity/execution book but separate capital;
- OpenAI/Jev request-capture tests prove market probability is hidden through `decision_frozen`;
- deterministic code owns probability control, edge, costs, quantity, fills, P&L, and Brier arithmetic;
- every outcome, including abstain/failure/no-edge, reaches append-only JSONL;
- no wallet/signing/private-order capability exists;
- output shows paired trading/economic P&L and forecast values or explicit pending resolution;
- all tests, lint, types, package policy, and dependency audit pass.

## Timebox and stop rules

- **Day 1:** Units 1–2.
- **Day 2:** Units 3–4.
- **Day 3:** Unit 5 offline gate.
- **Day 4:** Unit 6 and one live smoke if credentials/network permit.

If schedule slips, keep one fixture, one market, one polling pass, one append-only result file, and one basic summary. Cut richer evidence sources, retries, automated settlement, multiple markets, and extra reporting first. Do not cut paper-only isolation, no-price leakage tests, separate capital, deterministic arithmetic, exact artifact hashes, or offline replay.

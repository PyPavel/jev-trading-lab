# Jev Trading Lab

Jev Trading Lab is an evidence-first feasibility lab for two independent simulated-market experiments:

- liquid non-sports Polymarket markets;
- liquid S&P 100 multi-day stock swings.

OpenAI builds timestamped evidence. Jev makes bounded decisions. Deterministic code owns arithmetic, risk, costs, simulated fills, accounting, and evaluation.

## Safety boundary

This package is paper-only. It has no broker, wallet, private-key, signing, withdrawal, or real-money order capability. Execution is simulated rather than disabled, and the dependency and entry-point policy is enforced by tests and CI.

## Development

Python 3.12 and `uv` are required.

```console
uv sync --all-groups
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv build
```

The CLI currently exposes only its safe research root:

```console
uv run jev-lab --help
```

## Restart-safe Polymarket observations

`polymarket observe` is an operational paper prototype using public data. It has no order,
wallet, or signing path. Each UTC-minute slot is serialized under a bounded filesystem
lock; a completed slot replays before any Gamma, CLOB, or model call. Adjacent slots
remain distinct. Provider intent is fsynced before invocation and its sanitized result is
content-addressed immediately afterward. A restart that finds intent without result
records terminal `UNKNOWN_IN_FLIGHT`, leaves model cost and economic P&L unavailable,
and never retries that opportunity/provider call—even if same-slot public evidence changed.

The canned fixture proves deterministic plumbing, not predictive edge. This repository
contains no resolved live sample, so it supports no promotion or economic-performance
claim. Feasibility remains unestablished until the preregistered live paper sample resolves.

The observation and provider journals trust a private local filesystem owned by the current
OS user and code running in that owner's process. They reject unsafe links, special files,
weak permissions, malformed transitions, and accidental corruption, but are not
cryptographically authenticated against deliberate same-user/process rewriting of all
journal/blob bytes and recomputation of unkeyed hashes. Keys, signatures, and a daemon are
outside this one-host paper-feasibility boundary.

Run a no-key public smoke (model calls fail closed when credentials are absent):

```console
env -u OPENAI_API_KEY -u JEV_API_KEY uv run jev-lab polymarket observe \
  --limit 1 --output-dir /tmp/jev-public-smoke
```

Run deterministic offline replay twice against the same private output directory to
verify idempotent recovery without network or credentials:

```console
uv run jev-lab polymarket replay \
  --fixture tests/fixtures/polymarket/feasibility_cycle.json \
  --output-dir /tmp/jev-offline-replay
uv run jev-lab polymarket replay \
  --fixture tests/fixtures/polymarket/feasibility_cycle.json \
  --output-dir /tmp/jev-offline-replay
```

## Stock paper observation

Replay the committed three-arm fixture without network access or credentials:

```console
uv run jev-lab stocks replay \
  --fixture tests/fixtures/stocks/observation.json \
  --output-dir /tmp/jev-stocks-replay
```

Observe one public Yahoo chart v8 snapshot. Missing model credentials fail only the
corresponding arm closed; the deterministic arm still reaches a terminal row:

```console
uv run jev-lab stocks observe --output-dir /tmp/jev-stocks-observe
```

Successful commands append one terminal JSONL row per arm and atomically replace a
bounded-label Prometheus textfile. A Yahoo failure appends one cycle-failure row without
fabricating quotes or decisions. Replay rows are explicitly synthetic unless every captured
provider artifact has provenance. Every arm has separate $10,000 paper capital, trading and
model-cost-adjusted economic P&L, and simulated long-only fills with a deterministic 10 bps
round-trip slippage assumption. Unknown provider cost leaves economic P&L unknown. No
brokerage or order surface exists.

Clean-wheel migration and report-render smoke checks are deferred until those resources exist; they become mandatory release checks when introduced.

## Project documents

- [Approved design](docs/superpowers/specs/2026-10-02-jev-trading-lab-design.md)
- [Provider contracts](docs/research/provider-contracts.md)
- [Reuse audit](docs/research/reuse-audit.md)

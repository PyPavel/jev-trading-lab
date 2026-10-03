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

Clean-wheel migration and report-render smoke checks are deferred until those resources exist; they become mandatory release checks when introduced.

## Project documents

- [Approved design](docs/superpowers/specs/2026-10-02-jev-trading-lab-design.md)
- [Provider contracts](docs/research/provider-contracts.md)
- [Reuse audit](docs/research/reuse-audit.md)

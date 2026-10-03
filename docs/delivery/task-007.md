# Task 007 Delivery Evidence

## Status

**PENDING INDEPENDENT REVIEWS**

Production implementation is committed at `24cbbff4e6a45cfd98bef9175eab9bb249b6b452`. No push was performed. Independent spec/adversarial and quality/security reviews remain pending.

## Production commit

```text
24cbbff4e6a45cfd98bef9175eab9bb249b6b452 feat(persistence): add immutable core schema
```

## Implemented scope

- Added migration `002_core_entities.sql` with strict `WITHOUT ROWID` tables for manifests, run windows, runs, isolated arm accounts, shared opportunities, observations, and artifact references.
- Added `BEFORE UPDATE` and `BEFORE DELETE` abort triggers for every migration-002 table in migration 002 itself. No migration prefix exposes those tables without immutability guards.
- Added immediate foreign keys without cascade deletion, exact lowercase SHA-256 checks, fixed-point/integer bounds, canonical JSON byte-size checks, exact 84-day enrollment ordering, account arm/stream constraints, and unique semantic identities.
- Added observation supersession checks for existing predecessor, one direct successor, same logical key/run, changed content hash, increasing retrieval time, and self-reference rejection.
- Added transaction-owning `CoreRepository` writes with caller-transaction rejection, rollback on failure, strict built-in/domain types, authoritative manifest/run-window revalidation, activation-candidate enforcement, deterministic IDs, exact idempotency comparison, normalized relative artifact paths, and no mutation APIs.
- Updated the migration authorizer so SQLite's `SQLITE_REINDEX` authorizer code can represent `CREATE INDEX`; explicit `REINDEX` SQL remains rejected by migration statement validation.
- Updated exact source/SQL/operational hashes, workflow preflight hash, package resource expectations, migration upgrade tests, and installed-wheel migration smoke for migrations 001 and 002.

## TDD RED evidence

The three mandatory behaviors were executed individually before production files existed. Each failed during test collection because `jev_trading_lab.persistence.core_repo` did not exist:

```text
ERROR tests/persistence/test_core_schema.py
ModuleNotFoundError: No module named 'jev_trading_lab.persistence.core_repo'
```

After the initial implementation, the focused suite exposed the migration authorizer conflict:

```text
18 failed
MigrationError: migration '002_core_entities.sql' contains unauthorized SQL
```

A diagnostic authorizer trace identified SQLite action code 27 (`SQLITE_REINDEX`, also `SQLITE_CREATE_INDEX`) for `runs_manifest_idx`. Removing the ambiguous action-level denial allowed `CREATE INDEX`; lexical migration validation still rejects explicit `REINDEX` statements.

Subsequent RED/GREEN iterations exposed and fixed test setup for `WITHOUT ROWID`, empty immutable tables, SQLite numeric affinity, migration-prefix expectations, and strict formatting/type gates.

## GREEN evidence

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_core_schema.py \
  tests/persistence/test_migrations.py \
  tests/test_package_policy.py -q
214 passed in 5.30s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
609 passed in 8.16s
```

## Verification gates

All final commands exited 0:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.60ms

env PYTHONDONTWRITEBYTECODE=1 uv run ruff check .
All checks passed!

env PYTHONDONTWRITEBYTECODE=1 uv run ruff format --check .
28 files already formatted

env PYTHONDONTWRITEBYTECODE=1 uv run mypy src
Success: no issues found in 15 source files

uv run pip-audit --strict \
  --requirement /tmp/jev-task-007-audit.txt --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

uv run python scripts/wheel_migration_smoke.py
Installed-wheel migration smoke passed.

env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/test_package_policy.py::test_migration_sql_is_packaged_in_wheel_and_sdist \
  tests/test_package_policy.py::test_built_artifacts_exclude_repository_and_host_junk -q
3 passed in 0.44s

git diff --check
exit 0
```

`pip-audit` emitted only its standard `--no-deps` advisory warnings. Design, plan, and review documents were unchanged. Dependencies and `uv.lock` were unchanged.

## Review handoff

Required independent reviews:

1. Spec/adversarial review of migration-002 immutability, FK/index/schema contract, supersession authority, repository transaction semantics, hostile direct SQL, idempotency, and concurrency.
2. Quality/security review of validation boundaries, SQL construction, package-policy hashes, migration authorizer behavior, build artifacts, and full verification gates.

Current status remains **PENDING INDEPENDENT REVIEWS** until both reviews report zero blocking findings.

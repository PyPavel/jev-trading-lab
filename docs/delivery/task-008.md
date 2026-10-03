# Task 008 Delivery Evidence

## Status

**PENDING INDEPENDENT REVIEWS**

Production implementation is committed at `05d5d10`. Local self-review found no blocking correctness, security, migration, or packaging issue. No push was performed.

## Production commit

```text
05d5d10 feat(persistence): enforce append-only lineage
```

## Delivered scope

- Added migration `003_append_only_guards.sql` with strict, run-bound `journal_entities` and `journal_events` tables.
- Added same-migration `BEFORE UPDATE`, `BEFORE DELETE`, and immutable `BEFORE INSERT` conflict guards for every new table and every primary/secondary UNIQUE identity.
- Added database-authoritative supersession validation: persisted predecessor, same entity, strictly increasing sequence/time, changed payload hash, one successor, and recursive CTE cycle rejection.
- Added `AppendOnlyJournal`, deterministic canonical factories, exact built-in type validation, semantic record reconstruction, idempotent appends, deterministic conflict errors, owned `BEGIN IMMEDIATE` transactions, full PRAGMA contract, and secure timeout authority.
- Added direct SQL, repository, 1,000-link cycle, REPLACE-with-recursion-off, malformed input, transaction, drift, concurrency, migration-chain, policy, package, and wheel tests.
- Preserved migration 002 unchanged. Migration 003 creates no triggers for migration 002 tables; post-003 tests verify migration 002 guards remain effective.
- Updated exact source/SQL hashes, workflow preflight hash, wheel migration smoke, package resource assertions, and migration-prefix expectations. Dependencies and `uv.lock` were unchanged.

## TDD RED evidence

The first required test was run before production files existed:

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_append_only.py::test_update_delete_abort -q

ERROR tests/persistence/test_append_only.py
ModuleNotFoundError: No module named 'jev_trading_lab.persistence.journal'
```

After the first implementation, the full Task 8 file exposed the cross-identity supersession assertion mismatch before its test setup was corrected to avoid colliding with the already-used successor:

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_append_only.py -q

1 failed, 26 passed
```

Migration and policy integration were also RED before updating expected prefix/resources and exact hashes:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py

policy preflight: src/jev_trading_lab/persistence/schema/003_append_only_guards.sql: file is not approved
policy preflight: src/jev_trading_lab/persistence/journal.py: file is not approved

env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_core_schema.py::test_migration_002_schema_contract_and_same_migration_guards \
  tests/persistence/test_migrations.py -q

5 failed, 140 passed
```

## GREEN evidence and exact gates

All listed commands exited 0.

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.68ms

/usr/bin/python3 -I -S scripts/policy_preflight.py && \
env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_append_only.py \
  tests/persistence/test_core_schema.py \
  tests/persistence/test_migrations.py \
  tests/test_package_policy.py -q
Policy preflight passed.
310 passed in 7.52s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
712 passed in 10.01s

uv run ruff check .
All checks passed!

uv run ruff format --check .
30 files already formatted

uv run mypy src
Success: no issues found in 16 source files

uv export --frozen --all-groups --no-emit-project \
  --format requirements-txt --output-file audit-requirements.txt
exit 0

uv run pip-audit --strict --requirement audit-requirements.txt \
  --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

uv run python scripts/wheel_migration_smoke.py
Installed-wheel migration smoke passed.

env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_package_policy.py -q
58 passed in 1.38s

git diff 2bd43e8..HEAD --check
exit 0
```

`pip-audit` emitted only its standard `--no-deps` advisory warnings. The shell emitted an unrelated existing `.zshrc` warning for a missing OpenClaw completion file during background commands; all commands exited 0. Build artifacts remain ignored. Design, plan, and review documents were unchanged.

## Exact production hashes

```text
src/jev_trading_lab/persistence/journal.py 133d3ef50acc9c2207e696fa6f04faf8dd2c53fd3c32195c039652205a49d03d
src/jev_trading_lab/persistence/schema/003_append_only_guards.sql c1dac6ccfd4d96c1157f872683473f2f18994416a21447e07387ad882e870aa2
scripts/policy_preflight.py 3f6d7374cc708fbcdcc3260e3b306c4ba71a9054af1ccd4176b627de8ff9cad0
scripts/wheel_migration_smoke.py cbb3e3ee84bbddd62cd4f0321f5f4675503cf0721c1bd06beaf9d2216c62b4a4
.github/workflows/ci.yml 217d7c50107a0772184eb487e3f4bae26d54b27259bad0a03c1d6e87ddf55016
```

## Self-review

No BLOCKER or MAJOR finding remained after reviewing the production diff against Task 8 requirements. Database triggers remain authoritative for direct SQL, including recursion disabled; Python validation adds deterministic boundary errors without replacing database enforcement. The generic journal is intentionally separate from Task 7 observations and does not retrofit or duplicate migration 002 protections.

## Review state

**PENDING INDEPENDENT REVIEWS.** This artifact records local implementation and gate evidence only; it does not claim independent approval.

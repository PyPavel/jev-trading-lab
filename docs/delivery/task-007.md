# Task 007 Delivery Evidence

## Status

**PENDING INDEPENDENT RE-REVIEW**

Production review fixes are committed at `434f33e280a43653cf7379a01098d5afe9e2809f`. No push was performed. Independent spec/adversarial and quality/security re-review remain pending.

## Production commits

```text
24cbbff4e6a45cfd98bef9175eab9bb249b6b452 feat(persistence): add immutable core schema
434f33e280a43653cf7379a01098d5afe9e2809f fix(persistence): enforce immutable core invariants
```

## Review findings resolved

- Added conflicting-row `BEFORE INSERT` guards for every immutable table so `INSERT OR REPLACE` cannot bypass `BEFORE UPDATE`/`BEFORE DELETE`, including with `recursive_triggers` disabled. Connection setup now enables and verifies `recursive_triggers`; every repository write rejects drift in `foreign_keys`, `recursive_triggers`, `trusted_schema`, transaction state, or autocommit mode.
- Made observation persistence authoritative: reconstructs predecessor lineage from stored rows, re-runs `ObservationMetadata` validation, rejects forged wrappers/tampered fields or IDs, detects corrupt cycles, and no longer trusts a caller-supplied predecessor object.
- Hardened every persisted ID/hash field with exact text type, exact 64-byte length, lowercase hexadecimal alphabet, and explicit NUL rejection.
- Enforced normalized relative POSIX artifact paths in SQLite, unique path mapping, and `snapshot` artifact kind for opportunity snapshot references.
- Made account arm, currency, and initial capital derive from the persisted, reparsed, activation-valid manifest; compatibility arguments cannot override that contract.
- Removed `eligibility_episode` from opportunity identity, schema, insert API, and uniqueness mapping.
- Added an explicit lexical `REINDEX` ban while retaining `CREATE INDEX` support despite SQLite's shared authorizer action code.
- Updated exact source, migration SQL, preflight, and workflow hashes. No dependency or lock-file changes.

## TDD RED evidence

Original Task 007 RED runs occurred before `core_repo.py` existed:

```text
ERROR tests/persistence/test_core_schema.py
ModuleNotFoundError: No module named 'jev_trading_lab.persistence.core_repo'
```

Initial migration implementation then exposed the SQLite authorizer alias:

```text
18 failed
MigrationError: migration '002_core_entities.sql' contains unauthorized SQL
```

The review-fix tests were added before the corresponding production fixes. They exposed REPLACE immutability bypasses, pragma drift, trusted caller observation state, NUL-suffixed identifiers, noncanonical/duplicate artifact paths, wrong snapshot kinds, caller-controlled account capital, residual `eligibility_episode`, and executable `REINDEX`. The preserved review-fix RED evidence also covered `WITHOUT ROWID` setup, empty immutable tables, numeric affinity, and migration-prefix expectations.

After targeted cache cleanup, the resumed focused run found one unfinished authoritative-observation behavior:

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_core_schema.py tests/persistence/test_migrations.py -q
1 failed, 197 passed in 4.58s
Expected regex: 'observation'
Actual message: 'correction predecessor is not persisted'
```

The repository now wraps stored-predecessor reconstruction failures as observation revalidation failures. The same focused command then passed `198 passed in 5.25s`.

## GREEN evidence and exact gates

All listed commands exited 0:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.61ms

env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_core_schema.py \
  tests/persistence/test_migrations.py \
  tests/test_package_policy.py -q
252 passed, 3 skipped in 6.73s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
647 passed, 3 skipped in 8.14s

env PYTHONDONTWRITEBYTECODE=1 uv run ruff check .
All checks passed!

env PYTHONDONTWRITEBYTECODE=1 uv run ruff format --check .
28 files already formatted

env PYTHONDONTWRITEBYTECODE=1 uv run mypy src
Success: no issues found in 15 source files

uv export --frozen --all-groups --no-emit-project \
  --format requirements-txt --output-file /tmp/jev-task-007-audit.txt
exit 0

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
3 passed in 0.48s

git diff --check
exit 0
```

`pip-audit` emitted only its standard `--no-deps` advisory warnings. Generated `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, and `dist` were removed with targeted path deletion after verification; `.venv` and tracked edits were preserved. Design, plan, and review documents were unchanged.

## Re-review handoff

Required independent re-reviews:

1. Spec/adversarial re-review of REPLACE behavior with recursive triggers off, pragma drift, observation lineage authority, exact ID/hash checks, artifact mapping/snapshot kind, manifest-authoritative accounts, opportunity identity, and explicit `REINDEX` rejection.
2. Quality/security re-review of validation boundaries, trigger ordering, SQL construction, exact hash chain, package artifacts, and all verification gates above.

Current status remains **PENDING INDEPENDENT RE-REVIEW** until both re-reviews report zero blocking findings.

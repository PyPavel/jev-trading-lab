# Task 007 Delivery Evidence

## Status

**PENDING INDEPENDENT RE-REVIEW**

Production review fixes are committed at `a490a0b54e18b3fc5c358f63270147821c1e6b04`. No push was performed. Independent spec/adversarial and quality/security re-review remain pending.

## Production commits

```text
24cbbff4e6a45cfd98bef9175eab9bb249b6b452 feat(persistence): add immutable core schema
434f33e280a43653cf7379a01098d5afe9e2809f fix(persistence): enforce immutable core invariants
a490a0b54e18b3fc5c358f63270147821c1e6b04 fix(persistence): close core immutability gaps
```

## Remaining review findings resolved

- Extended same-migration `BEFORE INSERT` immutable-conflict triggers to guard every discovered secondary UNIQUE identity: runs, accounts, opportunities, non-null observation successors, and artifact storage paths. Direct `INSERT OR REPLACE` with a different primary ID now aborts before SQLite deletes the existing row even when `recursive_triggers` is off. A `PRAGMA index_list`/`index_info` meta-test asserts that every secondary UNIQUE index is represented in its table trigger.
- Removed caller-writable `_constructor_proof`. `revalidate_observation()` exact-type checks the wrapper, reconstructs fresh `ObservationMetadata` from semantic fields plus authoritative predecessor context, and compares the canonical field tuple and observation ID. The repository rebuilds correction predecessors from stored rows and persists only the fresh object. This boundary validates semantics, not secret provenance: an `object.__new__` instance with complete semantically valid exact fields is intentionally accepted after reconstruction.
- Completed the write PRAGMA contract. `SecureConnection` retains an immutable configured busy timeout. `CoreRepository` reads it from secure connections or requires it explicitly for plain SQLite connections, then checks `foreign_keys=1`, `recursive_triggers=1`, `trusted_schema=0`, `journal_mode=wal`, `synchronous=2`, and exact `busy_timeout` before every `BEGIN IMMEDIATE`.
- Added before-first-write and after-successful-write drift tests for every PRAGMA, asserting no new rows and no open transaction. Added direct SQL tests for all secondary identities, observation successors, and two-node/long-cycle attempts.
- Preserved prior account capital, artifact path/snapshot, NUL, and `REINDEX` fixes. Updated exact source, migration SQL, preflight, and workflow hashes. No dependency or lock-file changes.

## TDD RED evidence

The new tests were run before their production fixes.

Secondary UNIQUE reproductions:

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_core_schema.py::test_every_secondary_unique_identity_has_insert_conflict_guard \
  tests/persistence/test_core_schema.py::test_insert_or_replace_rejects_different_primary_id_on_secondary_unique_identity \
  tests/persistence/test_core_schema.py::test_insert_or_replace_rejects_different_observation_id_for_existing_successor -q
6 failed

- trigger meta-test found no secondary identity predicates
- accounts and opportunities REPLACE operations did not raise
- runs and artifact refs reached later foreign-key failure after replacement deletion
- observation successor replacement did not raise
```

Observation trust-boundary reproductions:

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/core/test_observations.py::test_revalidation_returns_fresh_semantically_equivalent_observation \
  tests/core/test_observations.py::test_revalidation_accepts_semantically_valid_forged_exact_wrapper \
  tests/core/test_observations.py::test_revalidation_rejects_malicious_field_and_proof_mutation \
  tests/persistence/test_core_schema.py::test_repository_accepts_semantically_valid_forged_observation_wrapper \
  tests/persistence/test_core_schema.py::test_repository_rejects_simultaneous_tampering_even_with_matching_proof -q
8 failed, 1 passed

- `revalidate_observation` did not exist
- repository rejected a semantically valid forged exact wrapper because `_constructor_proof` was absent
```

Complete PRAGMA-contract reproductions:

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_migrations.py::test_secure_connection_retains_immutable_busy_timeout \
  tests/persistence/test_core_schema.py::test_repository_rejects_complete_pragma_drift_without_side_effects \
  tests/persistence/test_core_schema.py::test_plain_sqlite_repository_requires_explicit_expected_busy_timeout -q
14 failed

- `SecureConnection` had no retained timeout
- `CoreRepository` had no explicit timeout API
- plain SQLite construction did not require an expected timeout
```

The immutable-attribute follow-up was also RED before the final secure-connection change:

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_migrations.py::test_secure_connection_retains_immutable_busy_timeout -q
1 failed
Failed: DID NOT RAISE AttributeError for `_busy_timeout_ms` reassignment
```

## GREEN evidence and exact gates

All listed commands exited 0:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.60ms

env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/core/test_observations.py \
  tests/persistence/test_core_schema.py \
  tests/persistence/test_migrations.py \
  tests/test_package_policy.py -q
368 passed in 7.18s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
678 passed in 9.12s

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
3 passed in 0.42s

git diff --check
exit 0
```

`pip-audit` emitted only its standard `--no-deps` advisory warnings. Build artifacts and caches remain ignored and untracked. Design, plan, and review documents were unchanged.

## Exact reviewed hashes

```text
src/jev_trading_lab/core/observations.py 5c6f9d3c76c7f766b3170513dae374873b1df5970b4590e1ffa9c66367be2252
src/jev_trading_lab/persistence/core_repo.py 3de4542058bc385e8006c9fc466cd26fd407dc319fc566e10b9bd7e9b0e7c7c9
src/jev_trading_lab/persistence/db.py aa239d8dce02f30db46961e069650717c9fbe232a7bc88157647e9af6161585d
src/jev_trading_lab/persistence/schema/002_core_entities.sql 9026b984313d972dec6c3db8b442e4324f2a7f02ec71bd555c0c17f8ed362c54
scripts/policy_preflight.py ab681b792b7de90ad11306cef1d0f54e334b21b9612554b4a69bef45b2fd5925
.github/workflows/ci.yml a48e218a87ae3aabc96ba0247fce52731f221f7fe60fe0f9670f0551053071c8
```

## Re-review handoff

Required independent re-reviews:

1. Spec/adversarial re-review of secondary UNIQUE `INSERT OR REPLACE` behavior with recursive triggers off, observation semantic revalidation and stored predecessor authority, and complete pre-write PRAGMA drift rejection.
2. Quality/security re-review of trigger predicates, exact-type reconstruction, immutable timeout state, SQL construction, policy hash chain, package artifacts, and all verification gates above.

Current status remains **PENDING INDEPENDENT RE-REVIEW** until both re-reviews report zero blocking findings.

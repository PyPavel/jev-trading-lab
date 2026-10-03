# Task 008 Delivery Evidence

## Status

**PENDING INDEPENDENT RE-REVIEW**

Review findings are fixed in production commit `5de3a28`. This evidence update records local TDD and gate results only. No push was performed.

## Commits

```text
5de3a28 fix(persistence): authenticate journal records
05d5d10 feat(persistence): enforce append-only lineage
```

The evidence update is committed separately from production.

## Review findings resolved

- SQLite now authenticates journal payload bytes, SHA-256, and deterministic entity/event IDs on direct SQL inserts.
- `connect_database()` registers fixed-arity deterministic functions on every `SecureConnection`: `jev_sha256/1`, `jev_is_canonical_json/1`, `jev_journal_entity_id/5`, and `jev_journal_event_id/6`.
- CPython 3.12 exposes `SQLITE_DETERMINISTIC` but not `SQLITE_INNOCUOUS`. A minimal probe confirmed persistent triggers calling Python functions fail under `trusted_schema=OFF` with `OperationalError: unsafe use of f()`. The schema therefore retains `trusted_schema=OFF` and calls equivalent trusted application-defined collations from persistent triggers. Unsupported raw SQLite connections fail closed with `no such collation sequence`; named deterministic functions remain registered and directly tested.
- Direct SQL tests reject malformed/noncanonical JSON, valid canonical payload with wrong hash, valid hash with wrong entity/event ID, and an identical-payload successor carrying a fabricated hash. Manually derived valid rows pass.
- Supersession compares both verified payload hashes and canonical payload bytes.
- The unreachable recursive ancestry CTE was removed. Back-edge tests now assert the actual immutable-conflict guard and do not claim recursive CTE coverage.
- CI now runs `JEV_REQUIRE_BUILT_ARTIFACTS=1 uv run pytest tests/test_package_policy.py -q` after `uv build --no-build-isolation` and before installed-wheel smoke. Required mode fails instead of skipping when either artifact is missing or stale.
- Wheel smoke now proves the installed package registers journal authentication callbacks, accepts a valid manually derived entity, and rejects an unauthenticated direct-SQL row.
- Exact source, SQL, operational-script, workflow, and package inventory policies include `journal_sql.py`. Dependencies and `uv.lock` remain unchanged.
- Migration 002 remains byte-identical at SHA-256 `9026b984313d972dec6c3db8b442e4324f2a7f02ec71bd555c0c17f8ed362c54`; its guards remain green after migration 003.

## Acyclicity proof

Every accepted supersession references an already persisted predecessor. A new event cannot reference a future event because the predecessor-existence trigger aborts. Existing journal rows cannot be updated or deleted. `INSERT OR REPLACE` cannot rewrite a primary key, `(entity_id, sequence)`, or `supersedes_event_id` identity because `BEFORE INSERT` immutable-conflict triggers abort before SQLite deletes the conflicting row, including with `recursive_triggers=OFF`. Therefore a future node cannot become an ancestor and a persisted node cannot later gain a back-edge. The two-node and 1,000-link attempted back-edge tests exercise `immutable-conflict:journal_events`, not a recursive ancestry CTE.

## TDD RED evidence

Reviewer direct-SQL reproductions were run before production changes:

```text
RED malformed ACCEPTED
RED wrong-hash ACCEPTED
```

New authentication tests initially failed at collection because the registration boundary did not exist:

```text
ImportError: cannot import name 'register_journal_sql_functions'
1 error during collection
```

The CI order test was RED before the workflow update:

```text
FAILED test_ci_builds_before_required_artifact_policy_and_wheel_smoke
assert 'run: JEV_REQUIRE_BUILT_ARTIFACTS=1 uv run pytest tests/test_package_policy.py -q' in workflow
1 failed, 1 passed, 57 deselected
```

The required-artifact fresh-check test independently passed while the workflow-order assertion remained RED, proving missing artifacts fail rather than skip when `JEV_REQUIRE_BUILT_ARTIFACTS=1` is set.

## GREEN evidence and exact gates

All commands exited 0. Focused/full pre-build runs intentionally skipped four artifact-presence cases because `dist/` had been removed. The mandatory post-build artifact gate ran with no skips.

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 64 packages in 0.80ms

uv sync --frozen --all-groups
Checked 63 packages in 0.63ms

env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_append_only.py \
  tests/persistence/test_core_schema.py \
  tests/persistence/test_migrations.py \
  tests/test_package_policy.py -q
311 passed, 4 skipped in 7.75s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
713 passed, 4 skipped in 9.83s

uv run ruff check .
All checks passed!

uv run ruff format --check .
31 files already formatted

uv run mypy src
Success: no issues found in 17 source files

uv export --frozen --all-groups --no-emit-project \
  --format requirements-txt --output-file audit-requirements.txt
exit 0

uv run pip-audit --strict --requirement audit-requirements.txt \
  --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

JEV_REQUIRE_BUILT_ARTIFACTS=1 uv run pytest tests/test_package_policy.py -q
59 passed in 1.78s

uv run python scripts/wheel_migration_smoke.py
Installed-wheel migration and journal authentication smoke passed.

git diff --check
exit 0
```

`pip-audit` emitted only its standard `--no-deps` advisory warnings. Build artifacts remain ignored.

## Exact reviewed hashes

```text
src/jev_trading_lab/persistence/db.py 9adfb7e40f1d1be9016e2436f4d9f9c70d502daaf0bdd44fa5d2e6715192bb6f
src/jev_trading_lab/persistence/journal.py eb24c43d268cc4d399aa567d8928103b146192aac1e9b63a40cd046739d18c3b
src/jev_trading_lab/persistence/journal_sql.py f948ec7a0ecc413082b121f461187a828d1ebea4e9e725081af2316845ba1967
src/jev_trading_lab/persistence/schema/002_core_entities.sql 9026b984313d972dec6c3db8b442e4324f2a7f02ec71bd555c0c17f8ed362c54
src/jev_trading_lab/persistence/schema/003_append_only_guards.sql 929b3ee2eafef744d0f457d23b139ef4b1d081e3402c0d9f0fb3ba797dcb1d7f
scripts/policy_preflight.py df82b6c3d8bf25038cb6c02067e527ef44011d493f928f42c8466af0a5dfd5fe
scripts/wheel_migration_smoke.py e86e1baedf82f85d16af36e95a6d303714fc2a7c2b7997d8fc6ce2ba9ed14dc1
.github/workflows/ci.yml 1878d3f8f60aa2ae06e8853c8604499119c75af19ca4ceba31260e4da11cdb70
```

## Scope controls

Design, plan, and review documents were unchanged. No dependency changes were made. No push was performed.

## Review state

**PENDING INDEPENDENT RE-REVIEW.**

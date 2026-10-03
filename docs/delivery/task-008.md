# Task 008 Delivery Evidence

## Status

**PENDING INDEPENDENT RE-REVIEW**

Controlled journal writes are implemented in production commit `197047d`. This evidence update records local TDD and gate results only. No push was performed.

## Commits

```text
197047d fix(persistence): enforce controlled journal writes
5de3a28 fix(persistence): authenticate journal records
05d5d10 feat(persistence): enforce append-only lineage
```

The evidence update is committed separately from production.

## Review findings resolved

- Unsafe predicate-collation authentication triggers and their JSON1 `json_array()` dependency were removed from migration 003.
- `journal_sql.py` and its connection-level SQL functions/collations were removed; exact source, package, preflight, and artifact inventories were updated.
- `SecureConnection` installs a persistent runtime authorizer after the journal schema exists. Direct journal `INSERT`, `UPDATE`, and `DELETE`, main/temp schema DDL, `ATTACH`/`DETACH`, `writable_schema`, and `wal_checkpoint` are denied.
- `SecureConnection.set_authorizer()` cannot replace or clear the sealed runtime authorizer.
- `AppendOnlyJournal` rejects plain `sqlite3.Connection` objects. It opens only a table-specific `INSERT` scope, owns the transaction, and restores denial after success or error.
- Migrations use the existing migration authorizer through an internal setter, then restore/seal the runtime authorizer after successful, failed, or repeated application.
- `trusted_schema=OFF` and the full repository PRAGMA contract remain enforced.
- CI retains the mandatory post-build `JEV_REQUIRE_BUILT_ARTIFACTS=1` wheel+sdist policy gate with no skips, followed by installed-wheel smoke.
- Migration 002 remains byte-identical at SHA-256 `9026b984313d972dec6c3db8b442e4324f2a7f02ec71bd555c0c17f8ed362c54`.

## Trust boundary

The supported guarantee is controlled-writer consistency. Records created through `AppendOnlyJournal` are reconstructed and validated before a narrowly scoped insert; ordinary callers using the same `SecureConnection` cannot mutate journal rows or schema.

This is not protection against malicious same-process Python code, private-attribute/introspection bypasses, separately opened raw SQLite connections, or direct filesystem writers. It is not keyed provenance or cryptographic authentication of the writer.

## Acyclicity proof

Every accepted supersession references an already persisted predecessor. The predecessor-existence trigger prevents references to future events. Existing journal rows cannot be updated or deleted through the supported connection. `INSERT OR REPLACE` cannot rewrite a primary key, `(entity_id, sequence)`, or `supersedes_event_id` identity because `BEFORE INSERT` immutable-conflict triggers abort before SQLite deletes the conflicting row, including with `recursive_triggers=OFF`. Therefore a future node cannot become an ancestor and a persisted node cannot later gain a back-edge.

The two-node and 1,000-link attempted back-edge tests assert the actual `immutable-conflict:journal_events` error. No recursive ancestry CTE coverage is claimed.

## TDD RED evidence

The preserved focused test's loop hid which schema operation escaped. Parameterizing the statements isolated `ALTER TABLE`: `_runtime_authorizer` denied schema actions only when the authorizer's database argument equaled `main`, and this operation did not satisfy that condition. The actionable RED was:

```text
FAILED test_secure_connection_seals_authorizer_and_schema[ALTER TABLE journal_events RENAME TO replaced_events]
Failed: DID NOT RAISE DatabaseError
1 failed, 4 passed
```

Denying schema actions independent of SQLite's inconsistent schema-name argument made all five cases green.

A migration failure regression was then added. Before the fix, the migration `finally` block cleared the internal authorizer and left the sealed connection writable:

```text
FAILED test_failed_migration_restores_sealed_runtime_authorizer
Failed: DID NOT RAISE DatabaseError
1 failed
```

Restoring `_seal_runtime_authorizer` in the migration `finally` path made the regression green.

## GREEN evidence and exact gates

All commands exited 0. Focused/full pre-build runs intentionally skipped four artifact-presence cases because `dist/` had been removed. The mandatory post-build artifact gate ran with no skips.

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.64ms

env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_append_only.py \
  tests/persistence/test_core_schema.py \
  tests/persistence/test_migrations.py \
  tests/test_package_policy.py -q
317 passed, 4 skipped in 7.92s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
719 passed, 4 skipped in 10.18s

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

JEV_REQUIRE_BUILT_ARTIFACTS=1 uv run pytest tests/test_package_policy.py -q
59 passed in 1.93s

uv run python scripts/wheel_migration_smoke.py
Installed-wheel migration and controlled journal write smoke passed.

git diff --check
exit 0
```

`pip-audit` emitted only its standard `--no-deps` advisory warnings. Build artifacts remain ignored. The transient audit requirements export was removed.

## Exact reviewed hashes

```text
src/jev_trading_lab/persistence/db.py c6c71737f943b75331d852582029b2ca00d7ba4b9da160891bb8ab5b36fae6a7
src/jev_trading_lab/persistence/journal.py 8a435bf395ec78dbecd8baa422cc1f44010d47567277201528dee91a93171334
src/jev_trading_lab/persistence/migrations.py 01feca8145f7d9dd49c0e4eb8854b02543883c2623934b416eacb6cee63d67f1
src/jev_trading_lab/persistence/schema/002_core_entities.sql 9026b984313d972dec6c3db8b442e4324f2a7f02ec71bd555c0c17f8ed362c54
src/jev_trading_lab/persistence/schema/003_append_only_guards.sql d18492f31e6c0948fa87f11927f2bc4fb2a5a41ffb45c27c9e966339d5e35995
scripts/policy_preflight.py a406fb14193d3f567e1dd63e3e78c2e29264b4e428275edda00dafd348a8b65a
scripts/wheel_migration_smoke.py 4b066a3d6abcaaa2526b3b388eaeed579682d63d48f33fce154f32af6d5c1e9c
.github/workflows/ci.yml 947a6f28053c9c023840fefc7b7aea45eb5779a6043e81171abfebfbac97ae87
```

## Scope controls

Design, plan, and review documents were unchanged. Dependencies and `uv.lock` were unchanged. No push was performed.

## Review state

**PENDING INDEPENDENT RE-REVIEW.**

# Task 008 Delivery Evidence

## Status

**PENDING INDEPENDENT RE-REVIEW**

TEMP-schema isolation and the narrowed Python authorizer boundary are implemented in production commit `87522f3`. This evidence update records local TDD and gate results only. No push was performed.

## Commits

```text
87522f3 fix(persistence): isolate journal writes from temp schema
197047d fix(persistence): enforce controlled journal writes
5de3a28 fix(persistence): authenticate journal records
05d5d10 feat(persistence): enforce append-only lineage
```

The evidence update is committed separately from production.

## Review findings resolved

- Every `AppendOnlyJournal` read and insert names `main.journal_entities` or `main.journal_events`, including idempotency reads, alternate-identity conflict reads, and authoritative predecessor reads. Matching TEMP table names cannot redirect repository SQL.
- The runtime authorizer denies journal-table `INSERT`, `UPDATE`, and `DELETE` for every database value. The scoped authorizer permits only a direct `INSERT` into the selected main journal table when SQLite reports no trigger source.
- All available Python `sqlite3.SQLITE_CREATE_TEMP_*` and `SQLITE_DROP_TEMP_*` constants for table, index, trigger, and view are included in the schema deny set. TEMP schema DDL is also denied by database name for the generic schema action variants.
- Sealing, migration entry, and `AppendOnlyJournal` construction fail closed when `temp.sqlite_schema` contains any user object. SQLite internal TEMP objects remain allowed.
- A TEMP trigger created before sealing cannot survive the seal/journal boundary. A sealed connection cannot create the reviewer trigger or other TEMP schema objects. Trigger-originated journal inserts are independently denied by the scoped authorizer.
- Ordinary `connection.set_authorizer(...)` calls remain rejected after sealing. Success and failure write scopes restore the runtime denial authorizer.
- Unsafe predicate collations/functions and JSON1 remain absent. `AppendOnlyJournal` still accepts only `SecureConnection`; migrations restore the runtime seal; immutable record comparisons retain exact payload bytes; and the non-recursive acyclicity proof is unchanged.
- CI retains the mandatory post-build `JEV_REQUIRE_BUILT_ARTIFACTS=1` wheel+sdist policy gate with no skips, followed by installed-wheel smoke.
- Migration 002 remains byte-identical at SHA-256 `9026b984313d972dec6c3db8b442e4324f2a7f02ec71bd555c0c17f8ed362c54`. Migration 003 and dependencies were unchanged.

## Trust boundary

The supported guarantee is controlled-writer consistency for callers that use `SecureConnection` public instance methods and `AppendOnlyJournal`. Records created through that API are reconstructed and validated before a narrowly scoped direct insert into the selected main table; ordinary callers using the same connection cannot mutate journal rows, journal schema, or TEMP schema.

Python cannot prevent malicious in-process code holding the connection from deliberately invoking the base-class descriptor, for example `sqlite3.Connection.set_authorizer(connection, None)`. Base-class descriptor dispatch, `object.__setattr__` or other object-internal manipulation, process-memory mutation, raw separately opened SQLite connections, direct filesystem writers, and FD ABA attacks are outside the supported boundary. This is not a sandbox, keyed provenance, or cryptographic writer authentication. Tests use base-class dispatch only to construct pre-existing hostile TEMP state and to prove the supported boundary rejects that state when sealing or constructing the journal; no enforcement claim is made against the bypass itself.

## Acyclicity proof

Every accepted supersession references an already persisted predecessor. The predecessor-existence trigger prevents references to future events. Existing journal rows cannot be updated or deleted through the supported connection. `INSERT OR REPLACE` cannot rewrite a primary key, `(entity_id, sequence)`, or `supersedes_event_id` identity because `BEFORE INSERT` immutable-conflict triggers abort before SQLite deletes the conflicting row, including with `recursive_triggers=OFF`. Therefore a future node cannot become an ancestor and a persisted node cannot later gain a back-edge.

The two-node and 1,000-link attempted back-edge tests assert the actual `immutable-conflict:journal_events` error. No recursive ancestry CTE coverage is claimed.

## TDD RED evidence

The exact TEMP table reproduction created matching `temp.journal_entities` and `temp.journal_events`, then appended an entity and event. Before schema qualification the repository reported success while main stayed empty:

```text
FAILED test_append_ignores_matching_temp_journal_shadows
E assert 0 == 1
```

The exact reviewer TEMP trigger, created before append, injected a forged main row through the scoped repository insert. Before the trigger-source check the append executed the trigger and failed only because the forged row collided with the repository row:

```text
FAILED test_temp_trigger_cannot_inject_forged_main_journal_row
E jev_trading_lab.persistence.journal.JournalConflictError:
E journal_entities append conflicts
```

The authorizer and seal tests then exposed the remaining boundary defects:

```text
FAILED test_runtime_authorizer_denies_journal_dml_in_every_database
E assert 0 == 1
FAILED test_journal_authorizer_only_allows_direct_selected_main_insert
E assert 0 == 1
FAILED test_runtime_authorizer_denies_all_available_temp_schema_actions
E assert 0 == 1
FAILED test_sealed_connection_denies_temp_schema_creation[CREATE TEMP TABLE ...]
Failed: DID NOT RAISE DatabaseError
FAILED test_sealing_rejects_preexisting_user_temp_objects[...]
Failed: DID NOT RAISE DatabaseConfigurationError
FAILED test_append_only_journal_documents_supported_python_boundary
E assert 'public instance methods' in boundary
```

The focused RED run had 15 failures. Main qualification, database-independent journal DML denial, all supported TEMP action constants, direct-source-only scoped inserts, TEMP-object rejection, and precise boundary wording made these regressions green. Existing success- and failure-scope restoration tests remained green.

## GREEN evidence and exact gates

All commands exited 0. Current build artifacts existed for the focused and full pre-build test runs, so those runs had no artifact-presence skips. The mandatory post-build artifact gate also ran with no skips.

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.77ms

env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_append_only.py \
  tests/persistence/test_core_schema.py \
  tests/persistence/test_migrations.py \
  tests/test_package_policy.py -q
335 passed in 8.32s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
737 passed in 10.11s

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

env JEV_REQUIRE_BUILT_ARTIFACTS=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run pytest tests/test_package_policy.py -q
59 passed in 1.75s

env PYTHONDONTWRITEBYTECODE=1 uv run python scripts/wheel_migration_smoke.py
Installed-wheel migration and controlled journal write smoke passed.

git diff --check
exit 0
```

`pip-audit` emitted only its standard `--no-deps` advisory warnings. Build artifacts remain ignored. The transient audit requirements export was removed.

## Exact reviewed hashes

```text
src/jev_trading_lab/persistence/db.py 465998b818065eb34ea437c466081061e8fc486f8c324315abeae798a97b89fa
src/jev_trading_lab/persistence/journal.py f0397339a3ecd78c93f746b6ddc07375d451ccfc6a2ee89f86f81c5117287441
src/jev_trading_lab/persistence/migrations.py 1d5eaf29d0548d71b9c619b425d4615378e7707ea83b4c89622433cfd571595d
src/jev_trading_lab/persistence/schema/002_core_entities.sql 9026b984313d972dec6c3db8b442e4324f2a7f02ec71bd555c0c17f8ed362c54
src/jev_trading_lab/persistence/schema/003_append_only_guards.sql d18492f31e6c0948fa87f11927f2bc4fb2a5a41ffb45c27c9e966339d5e35995
scripts/policy_preflight.py 31c4690c9523d2bdbd40941e95c12f7290f5f9f9ead15ffaf650dfacc0694b1d
scripts/wheel_migration_smoke.py 4b066a3d6abcaaa2526b3b388eaeed579682d63d48f33fce154f32af6d5c1e9c
.github/workflows/ci.yml bb19bb26d57bb0c2911fb45927886fddb4d19443ce54f825f03f4852ef3c5bfa
```

## Scope controls

Design, plan, and review documents were unchanged. Dependencies, migration SQL, and `uv.lock` were unchanged. No push was performed.

## Review state

**PENDING INDEPENDENT RE-REVIEW.**

# Task 008 Delivery Evidence

## Status

**PENDING INDEPENDENT RE-REVIEW**

SQLite callback-property sealing, approved-package-only public migration execution, and load-stable append concurrency tests are implemented in production commit `bafb2ae`. This evidence update records local TDD and gate results only. No push was performed.

## Commits

```text
bafb2ae fix(persistence): close journal trust-boundary gaps
4af1cb5 fix(persistence): seal SQLite callback surfaces
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
- `row_factory` and `text_factory`, the remaining callback/conversion properties, become immutable when `SecureConnection` seals. `sqlite3.Row` and built-in `str` behavior remain intact. Regression coverage attempts assignment both before and after `AppendOnlyJournal` construction across entity/event idempotent and conflict reads; no caller callback can run while journal insertion is authorized.
- Public `apply_migrations` rejects every package except the reviewed `jev_trading_lab.persistence.schema` resource. Arbitrary-package execution remains private for migration-runner tests, preserving filesystem/archive resource coverage without exposing it as supported production API. An adversarial migration 004 that drops the delete guard, deletes journal events, and recreates the guard is rejected before mutation.
- Concurrent append tests now open both serialized `SecureConnection` instances before signaling readiness, then use an explicit release event with 30-second diagnostics. This reliably starts the append race only after setup completes.
- Unsafe predicate collations/functions and JSON1 remain absent. `AppendOnlyJournal` still accepts only `SecureConnection`; migrations restore the runtime seal; immutable record comparisons retain exact payload bytes; and the non-recursive acyclicity proof is unchanged.
- CI retains the mandatory post-build `JEV_REQUIRE_BUILT_ARTIFACTS=1` wheel+sdist policy gate with no skips, followed by installed-wheel smoke.
- Migration 002 remains byte-identical at SHA-256 `9026b984313d972dec6c3db8b442e4324f2a7f02ec71bd555c0c17f8ed362c54`. Migration 003 and dependencies were unchanged.

## Trust boundary

The supported guarantee is controlled-writer consistency for callers that use `SecureConnection` public instance methods and `AppendOnlyJournal`. Records created through that API are reconstructed and validated before a narrowly scoped direct insert into the selected main table; ordinary callers using the same connection cannot mutate journal rows, journal schema, or TEMP schema, replace callback-bearing properties, or select an arbitrary migration package.

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

The callback-property regressions were run before production changes and failed in all 10 parameterized cases:

```text
FAILED test_row_factory_cannot_reenter_journal_read_paths[...] (8 cases)
E assert False  # row_factory assignment was not rejected
FAILED test_text_factory_is_sealed_and_rows_remain_sqlite_rows[...] (2 cases)
E Failed: DID NOT RAISE AttributeError
10 failed in 0.36s
```

The hostile migration regression was then run before the public API restriction:

```text
FAILED test_public_apply_rejects_hostile_later_migration_package
E Failed: DID NOT RAISE MigrationError
1 failed in 0.19s
```

After the minimal fixes, the exact callback test selection passed `10 passed in 0.25s`; the hostile migration test passed `1 passed in 0.16s`; and the readiness/release concurrency selection passed `2 passed in 0.15s`.

## GREEN evidence and exact gates

All final commands exited 0. The mandatory post-build artifact gate ran with no skips.

```text
python3 -I scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.57ms

env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_append_only.py \
  tests/persistence/test_migrations.py -q
225 passed in 2.73s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
767 passed in 13.00s

uv run ruff check .
All checks passed!

uv run ruff format --check .
32 files already formatted

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
59 passed in 2.58s

env PYTHONDONTWRITEBYTECODE=1 uv run python scripts/wheel_migration_smoke.py
Installed-wheel migration and controlled journal write smoke passed.

git diff --check
exit 0
```

`pip-audit` emitted only its standard `--no-deps` advisory warnings. Build artifacts remain ignored. The transient audit requirements export was removed.

## Exact reviewed hashes

```text
src/jev_trading_lab/persistence/db.py 5afcb39908c4d8924a0f11d7e68e51fbef761f8d901e2c0c4427ca591161c641
src/jev_trading_lab/persistence/journal.py 24ee6f8bd4cec72a26ea2477d7b980c0a95d04d1c2fd51e189a231b6b7a46a35
src/jev_trading_lab/persistence/migrations.py 6ce446b8790d1a3ae2555cac85bd4ea955f96360078ec2c94d3cfdfda102b791
src/jev_trading_lab/persistence/schema/002_core_entities.sql 9026b984313d972dec6c3db8b442e4324f2a7f02ec71bd555c0c17f8ed362c54
src/jev_trading_lab/persistence/schema/003_append_only_guards.sql d18492f31e6c0948fa87f11927f2bc4fb2a5a41ffb45c27c9e966339d5e35995
scripts/policy_preflight.py 9d335b44436dc86fc6d356311eaca0828b5669e35ee539d20ed438109159e200
scripts/wheel_migration_smoke.py 4b066a3d6abcaaa2526b3b388eaeed579682d63d48f33fce154f32af6d5c1e9c
.github/workflows/ci.yml 5aa1da76f121a42818942de2df975cdec5b9b9ded076d18afd6ec9652ea99244
```

## Scope controls

Design, plan, review documents, dependencies, migration SQL, and `uv.lock` were unchanged. Production code, tests, policy hashes, workflow preflight hash, and this evidence document changed. No push was performed.

## Review state

**PENDING INDEPENDENT RE-REVIEW.**

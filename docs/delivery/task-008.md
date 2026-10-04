# Task 008 Delivery Evidence

## Status

**PENDING INDEPENDENT RE-REVIEW**

The interpreter-hook transition and journal provenance blockers are resolved in production commit `135d63f`. This records local TDD and gate evidence only. No push or independent approval occurred.

## Commits

```text
135d63f fix(persistence): harden privileged transitions
c08a7f6 fix(persistence): require trusted migration connections
8bfb961 fix(persistence): eliminate privileged callback execution
bafb2ae fix(persistence): close journal trust-boundary gaps
4af1cb5 fix(persistence): seal SQLite callback surfaces
87522f3 fix(persistence): isolate journal writes from temp schema
197047d fix(persistence): enforce controlled journal writes
5de3a28 fix(persistence): authenticate journal records
05d5d10 feat(persistence): enforce append-only lineage
```

Evidence is committed separately from production.

## Findings resolved

- `apply_migrations` and journal writes reject active `sys.settrace`, `threading.settrace_all_threads` hooks on the current thread, and `sys.setprofile` before supported-boundary state or SQL.
- Privileged authorizer installation now occurs inside an established context-manager restoration guard. Outermost `finally` restoration remains in migration and journal callers, so a trace-raised `BaseException` during transition or yield cannot strand privilege.
- `AppendOnlyJournal` requires exact `SecureConnection` factory provenance at construction and immediately before every write, using non-overrideable slot-token validation before property reads, callbacks, or SQL. Direct subclasses and spoofed properties are rejected.
- Public `apply_migrations` now rejects every connection except a factory-created, already-sealed `SecureConnection` that completed `connect_database` setup. Rejection occurs before connection property reads, transaction checks, authorizer changes, callbacks, or SQL.
- Direct `SecureConnection` construction and ordinary public setup cannot forge migration readiness. The internal slot token is inaccessible and immutable through the supported public surface; migration validation reads exact class slot descriptors, not overrideable instance properties.
- Migration authorizer transitions no longer reseal the connection. Migration temporarily installs the internal authorizer and restores the runtime authorizer while the public sealed state remains true.
- Raw trace callbacks and direct-connection trace/progress/scalar/collation/aggregate/window callbacks stay dormant on rejection. Existing caller transactions and authorizers remain untouched, and no schema metadata leaks.
- Existing raw-connection schema behavior tests now create schema through the supported factory migration boundary, close it, then reopen raw only to exercise downstream compatibility behavior.
- Public `apply_migrations(connection)` accepts no package or clock. Production no longer exports `_apply_migrations` or another callable accepting arbitrary package plus privileged connection.
- Production migration resources, statements, and timestamps are resolved and validated before installing the migration authorizer. Synthetic package execution moved to `tests/persistence/migration_test_support.py`; its caller clock is fully evaluated before privilege.
- Migration metadata insertion binds no Python object while privileged.
- Journal read predicates and inserts bind no Python object during `_journal_write_scope`. Exact `str`, `bytes`, `int`, and `None` values become internally generated SQL literals: UTF-8 blob cast to TEXT, blob hex, validated decimal integer, and fixed NULL. Table names and predicate templates remain fixed internal constants.
- Adversarial process-global adapters for every SQLite primitive used by journal rows (`str`, `int`, `bytes`, `NoneType`) do not execute during append and cannot forge a row. NUL, quote, Unicode, blob, integer-boundary, and NULL semantics are covered.
- Where Python exposes `Connection.setconfig`, setup enforces supported `WRITABLE_SCHEMA=false`, `ENABLE_TRIGGER=true`, `ENABLE_LOAD_EXTENSION=false`, `DEFENSIVE=true`, and `TRUSTED_SCHEMA=false`. Public `setconfig` is rejected after sealing. Older runtimes skip unavailable APIs/constants.
- Installed-wheel smoke asserts the public migration signature and absence of `_apply_migrations`, applies packaged migrations, and performs a controlled journal write.
- Prior hostile package, row/text factory, TEMP trigger/schema, authorizer restoration, and concurrent append regressions remain green.

## TDD RED evidence

### Migration entry callback boundary

```text
FAILED test_apply_rejects_raw_connection_before_trace_callback_or_sql
E Failed: DID NOT RAISE MigrationError
FAILED test_apply_rejects_direct_secure_connection_before_callbacks_or_sql
E sqlite3.IntegrityError: CHECK constraint failed ...
FAILED test_public_secure_connection_setup_cannot_grant_migration_trust
E Failed: DID NOT RAISE MigrationError
FAILED test_rejected_connection_preserves_transaction_and_authorizer_state
E Actual message: 'cannot migrate inside an active caller transaction'
4 failed, 1 passed, 152 deselected in 0.56s
```

The old entry boundary executed callback-visible SQL and inspected caller transaction state before rejecting unsupported connections.

### Migration callback and production helper

```text
FAILED test_public_migration_api_accepts_no_callback_or_package_capability
E AssertionError: assert ('connection', 'package', 'clock') == ('connection',)
FAILED test_public_migration_clock_cannot_run_sql_under_privilege
E Failed: DID NOT RAISE TypeError
2 failed in 0.52s
```

The old public clock ran after migration privilege was installed. New API rejects the argument without invocation, and `_apply_migrations` is absent from production.

### Process-global adapters and trusted literals

```text
FAILED test_global_adapters_cannot_run_during_privileged_journal_insert[str]
FAILED test_global_adapters_cannot_run_during_privileged_journal_insert[int]
FAILED test_global_adapters_cannot_run_during_privileged_journal_insert[bytes]
E RecursionError: maximum recursion depth exceeded
FAILED test_privileged_journal_literals_preserve_sqlite_boundaries[...] (4 cases)
E AttributeError: module ...journal has no attribute '_trusted_insert_sql'
7 failed, 1 passed in 0.60s
```

`NoneType` was absent from entity inserts; final coverage routes it through event append and verifies all four callbacks remain dormant.

### SQLite DB configuration

```text
FAILED test_secure_connection_seals_sqlite_dbconfig_after_safe_setup
E Failed: DID NOT RAISE RuntimeError
1 failed in 0.55s
```

The runtime exposed `setconfig`, and sealed callers could mutate DB configuration.


### Interpreter hooks, asynchronous restoration, and journal provenance

```text
PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_migrations.py \
  tests/persistence/test_append_only.py -q \
  -k 'active_trace or transition_restores or active_interpreter_hook or interpreter_trace or journal_scope_restores or spoofed_secure_subclass'
9 failed, 1 passed, 241 deselected in 0.80s
```

Failures reproduced migration trace injection, migration privilege stranding after a trace-raised `BaseException`, journal trace injection, journal privilege stranding at transition, active trace/profile acceptance, and acceptance of a spoofed `SecureConnection` subclass. The yield-point restoration reproduction already passed because the pre-existing inner `finally` covered that later boundary; transition failures proved the uncovered interval before that guard.

## Focused GREEN evidence

```text
PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/persistence/test_migrations.py -q \
  -k 'apply_rejects_raw_connection_before_trace_callback_or_sql or apply_rejects_direct_secure_connection_before_callbacks_or_sql or public_secure_connection_setup_cannot_grant_migration_trust or rejected_connection_preserves_transaction_and_authorizer_state or factory_created_sealed_connection_is_migration_ready'
5 passed, 152 deselected in 0.50s

PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_migrations.py \
  tests/persistence/test_append_only.py \
  tests/persistence/test_core_schema.py -q
322 passed in 6.65s
```

```text
PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_migrations.py::test_public_migration_api_accepts_no_callback_or_package_capability \
  tests/persistence/test_migrations.py::test_public_migration_clock_cannot_run_sql_under_privilege \
  tests/persistence/test_migrations.py::test_secure_connection_seals_sqlite_dbconfig_after_safe_setup -q
3 passed in 0.43s

PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_append_only.py::test_global_adapters_cannot_run_during_privileged_journal_insert \
  tests/persistence/test_append_only.py::test_privileged_journal_literals_preserve_sqlite_boundaries -q
8 passed in 0.41s

PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_migrations.py \
  tests/persistence/test_append_only.py \
  tests/persistence/test_core_schema.py -q
317 passed in 6.59s
```

Focused set includes prior row_factory/text_factory, hostile package, migration trigger/TEMP, and concurrency regressions.


```text
PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_migrations.py \
  tests/persistence/test_append_only.py -q \
  -k 'active_trace or transition_restores or active_interpreter_hook or interpreter_trace or journal_scope_restores or spoofed_secure_subclass or factory_created_sealed_connection_is_migration_ready or threading_trace'
12 passed, 240 deselected in 0.59s

PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_migrations.py \
  tests/persistence/test_append_only.py \
  tests/persistence/test_core_schema.py -q
333 passed in 7.24s
```

## Final gates

All commands exited 0. Mandatory built-artifact gate had no skips.

```text
PYTHONDONTWRITEBYTECODE=1 python3 -I scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.84ms

PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
794 passed in 12.96s

uv run ruff check .
All checks passed!

uv run ruff format --check .
33 files already formatted

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

JEV_REQUIRE_BUILT_ARTIFACTS=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run pytest tests/test_package_policy.py -q
59 passed in 2.63s

PYTHONDONTWRITEBYTECODE=1 uv run python scripts/wheel_migration_smoke.py
Installed-wheel migration and controlled journal write smoke passed.

git diff --check
exit 0
```

`pip-audit` emitted only standard `--no-deps` advisories. Transient audit requirements were removed. Build artifacts remain ignored.

## Exact reviewed hashes

```text
src/jev_trading_lab/persistence/db.py 23e863411e21a2f2b73dfab95b9b21cf082ef42d5db4249e6a0ca6be603b5229
src/jev_trading_lab/persistence/journal.py d74367ddae2402031bde8caafe1f669edfe8ec73b89ec22b7df7d0debace8176
src/jev_trading_lab/persistence/migrations.py 3c759241ae0737e23de20a03977d703a24c7339ac50c35aad12d19ff4962f1d0
src/jev_trading_lab/persistence/schema/002_core_entities.sql 9026b984313d972dec6c3db8b442e4324f2a7f02ec71bd555c0c17f8ed362c54
src/jev_trading_lab/persistence/schema/003_append_only_guards.sql d18492f31e6c0948fa87f11927f2bc4fb2a5a41ffb45c27c9e966339d5e35995
scripts/policy_preflight.py 7662c9586ce21bbb0f44acb15e5e86f5ddf4396e7e6df74664927ab401a2f05b
scripts/wheel_migration_smoke.py a9792c0e8dc9d7a9ed8a1ea57e1caf6f0b5eea03ecf8930bdef0ba394190568c
.github/workflows/ci.yml b6a044e6dbd465798fa70319e7d2b39bfe5918bad4513a89bc8ffc7bc1aa7dde
```

Migration SQL, dependencies, `pyproject.toml`, and `uv.lock` were unchanged.

## Trust boundary

Supported guarantee: controlled-writer consistency through factory-created, already-sealed `SecureConnection` instances and `AppendOnlyJournal`. Unsupported migration connections are rejected before callback-visible SQL or state inspection. No caller-controlled Python callback or mutable SQLite DB configuration can execute while a migration or journal authorizer grants privileged writes through this API.

Base-class descriptor dispatch, `object.__setattr__`, process-memory modification, raw separately opened SQLite connections, direct filesystem writes, and FD ABA attacks remain outside this same-process boundary. This is not a sandbox, keyed provenance, or cryptographic writer authentication.

## Review state

**PENDING INDEPENDENT RE-REVIEW.**


## Interpreter-transition production snapshot

```text
135d63f39c62e63c25644d11cf45b649601be8dd  production commit
f11fb9e2a81f5696ca30235098cc276da317f788977243b0c9312c7b8968c300  src/jev_trading_lab/persistence/db.py
f5dfe8f961b9bac375aeb20eb9a6b2085ded74a349efe27a27339ae41b9e6fc8  src/jev_trading_lab/persistence/migrations.py
ad0e05b6981559a8f2feb362fa0651eed4b1c95765f5248c39b514b129ed6447  src/jev_trading_lab/persistence/journal.py
f0ebbb9b1206565a059facba248096d4222de1f34c95886aae3c183e744ea296  tests/persistence/test_migrations.py
2156b54469a7c4dce07824569d99ff1971c4632c99ababf75d0e640d84ab4a07  tests/persistence/test_append_only.py
b161e4590f7959308cee43f338f153bebf668cf50d5bc7f781930464979415ec  scripts/policy_preflight.py
9a6f52386bfc94d0455bffb339fc581e50173fde9e960f834ae051838323db6d  tests/test_package_policy.py
dc05bf474b56042cd25e6e66e4678136f7507468febe6de811d768ef74d8930f  .github/workflows/ci.yml
```

Status remains pending independent re-review; this agent did not independently approve the change.

# Task 008 Delivery Evidence

## Status

**PENDING INDEPENDENT RE-REVIEW**

Remaining privileged callback/configuration findings are resolved in production commit `8bfb961`. This records local TDD and gate evidence only. No push or independent approval occurred.

## Commits

```text
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

- Public `apply_migrations(connection)` accepts no package or clock. Production no longer exports `_apply_migrations` or another callable accepting arbitrary package plus privileged connection.
- Production migration resources, statements, and timestamps are resolved and validated before installing the migration authorizer. Synthetic package execution moved to `tests/persistence/migration_test_support.py`; its caller clock is fully evaluated before privilege.
- Migration metadata insertion binds no Python object while privileged.
- Journal read predicates and inserts bind no Python object during `_journal_write_scope`. Exact `str`, `bytes`, `int`, and `None` values become internally generated SQL literals: UTF-8 blob cast to TEXT, blob hex, validated decimal integer, and fixed NULL. Table names and predicate templates remain fixed internal constants.
- Adversarial process-global adapters for every SQLite primitive used by journal rows (`str`, `int`, `bytes`, `NoneType`) do not execute during append and cannot forge a row. NUL, quote, Unicode, blob, integer-boundary, and NULL semantics are covered.
- Where Python exposes `Connection.setconfig`, setup enforces supported `WRITABLE_SCHEMA=false`, `ENABLE_TRIGGER=true`, `ENABLE_LOAD_EXTENSION=false`, `DEFENSIVE=true`, and `TRUSTED_SCHEMA=false`. Public `setconfig` is rejected after sealing. Older runtimes skip unavailable APIs/constants.
- Installed-wheel smoke asserts the public migration signature and absence of `_apply_migrations`, applies packaged migrations, and performs a controlled journal write.
- Prior hostile package, row/text factory, TEMP trigger/schema, authorizer restoration, and concurrent append regressions remain green.

## TDD RED evidence

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

## Focused GREEN evidence

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
778 passed in 13.37s

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
src/jev_trading_lab/persistence/db.py f89c0301ed79b5efdacebc683ca6bf202f2bfd14417acbddd90eec2a9991c302
src/jev_trading_lab/persistence/journal.py d74367ddae2402031bde8caafe1f669edfe8ec73b89ec22b7df7d0debace8176
src/jev_trading_lab/persistence/migrations.py fd0f0dec6d291c31ee7e06fa90983b2b0eaa29d1eb5d0b9fff4707397079e46a
src/jev_trading_lab/persistence/schema/002_core_entities.sql 9026b984313d972dec6c3db8b442e4324f2a7f02ec71bd555c0c17f8ed362c54
src/jev_trading_lab/persistence/schema/003_append_only_guards.sql d18492f31e6c0948fa87f11927f2bc4fb2a5a41ffb45c27c9e966339d5e35995
scripts/policy_preflight.py dafc65f1e5ce773410c21543d812e41c27cb84f0a28afce70316d9935c597823
scripts/wheel_migration_smoke.py a9792c0e8dc9d7a9ed8a1ea57e1caf6f0b5eea03ecf8930bdef0ba394190568c
.github/workflows/ci.yml 004f1985c5fd9c2448d6293d78b4c40570f78dcf034f2022a9c3730d3de7a7ea
```

Migration SQL, dependencies, `pyproject.toml`, and `uv.lock` were unchanged.

## Trust boundary

Supported guarantee: controlled-writer consistency through `SecureConnection` public instance methods and `AppendOnlyJournal`. No caller-controlled Python callback or mutable SQLite DB configuration can execute while a migration or journal authorizer grants privileged writes through this API.

Base-class descriptor dispatch, `object.__setattr__`, process-memory modification, raw separately opened SQLite connections, direct filesystem writes, and FD ABA attacks remain outside this same-process boundary. This is not a sandbox, keyed provenance, or cryptographic writer authentication.

## Review state

**PENDING INDEPENDENT RE-REVIEW.**

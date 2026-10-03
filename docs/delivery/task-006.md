# Task 006 Delivery Evidence

## Status

**PENDING INDEPENDENT REVIEWS**

Production implementation is committed and locally verified. Independent spec/adversarial and quality/model-risk reviews have not yet been performed.

## Production commit

```text
847cbc1 feat(persistence): add checked SQLite migrations
```

## Scope delivered

- Added one `Path`-only SQLite connection factory with exact built-in integer busy-timeout validation, SQLite `>=3.37.0`, explicit autocommit mode, `sqlite3.Row`, default thread affinity, and non-URI paths.
- Verified `foreign_keys=ON`, `journal_mode=WAL`, exact `busy_timeout`, `synchronous=FULL`, and `trusted_schema=OFF` on every connection. Setup failures close the connection.
- Created private parents as `0700` and the database file as `0600`; rejected symlink database paths, symlink parent components, special files, and the required network-filesystem denylist using injectable `/proc/self/mountinfo` resolution with decoded paths and longest mount-point matching.
- Added immutable package-resource migration discovery with exact `NNN_slug.sql` names, contiguous versions from 001, duplicate/gap checks, UTF-8 and size bounds, regular-file/symlink/readability checks for filesystem resources, and SHA-256 over exact bytes. Zip-import resources are covered without assuming filesystem `Path` objects.
- Added trigger-aware SQL statement splitting through `sqlite3.complete_statement`; transaction control, `ATTACH`/`DETACH`, `VACUUM`, and migration PRAGMAs are rejected before any migration applies. Comments and string literals do not trigger false positives.
- Applied each migration under `BEGIN IMMEDIATE`, inserted version/name/hash/timestamp in the same transaction, rolled back failed multi-statement migrations, validated all applied metadata before pending work, serialized concurrent migrators, and made repeated application idempotent.
- Added migration 001 with only the STRICT, WITHOUT ROWID `schema_migrations` table and metadata constraints. No business tables were introduced.
- Added exact source/package policy hashes, SQL artifact contents, wheel/sdist assertions, preflight hash, and workflow hash. `pyproject.toml`, `uv.lock`, approved design, implementation plan, and review files were unchanged.

## TDD RED evidence

The four mandatory nodes were executed individually before implementation and failed for the intended missing behavior:

```text
test_connection_pragmas
FAILED: connection.row_factory was None, expected sqlite3.Row

test_migration_checksum_change_rejected
2 failed: DID NOT RAISE MigrationError after applied SQL bytes/name changed

test_installed_wheel_loads_sql
FAILED: schema_migrations contained no recorded migration

test_network_fs_rejected
FAILED: DID NOT RAISE DatabaseConfigurationError for injected nfs
```

Exact source policy failed closed after the new production tree appeared and before approval hashes were updated:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
exit 1
policy preflight: src/jev_trading_lab/persistence: directory is not approved
policy preflight: src/jev_trading_lab/persistence/schema/001_migration_metadata.sql: file is not approved
policy preflight: src/jev_trading_lab/persistence/migrations.py: file is not approved
policy preflight: src/jev_trading_lab/persistence/db.py: file is not approved
```

A self-review regression was also witnessed RED before its fix:

```text
test_runtime_pycache_does_not_break_resource_discovery
FAILED: MigrationError: unexpected resource in migration package: '__pycache__'
```

The loader now ignores only the normal runtime `__pycache__` directory while rejecting other unexpected resources.

## GREEN evidence

Focused tests after implementation:

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/persistence/test_migrations.py tests/test_package_policy.py -q
97 passed, 1 skipped in 1.35s
```

After the build gate refreshed distributions, artifact-specific inspection passed:

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_package_policy.py::test_migration_sql_is_packaged_in_wheel_and_sdist tests/test_package_policy.py::test_built_artifacts_exclude_repository_and_host_junk -q
2 passed in 0.41s
```

Final full test run after the production commit and self-review fix:

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
494 passed in 3.56s
```

Coverage includes connection PRAGMAs and permissions, input types, mountinfo parsing, full network filesystem denylist, symlink paths, old SQLite, setup-close behavior, clean/repeated migration application, checksum/name drift, zip-import resources, malformed/duplicate/gapped filenames, non-UTF-8/oversize/unreadable/symlink/FIFO/unexpected resources, transaction rollback, trigger bodies, forbidden control SQL, comment/string false positives, applied future/gap/missing package metadata, strict timestamps, and concurrent migrators.

## Verification gates

All commands exited 0:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.65ms

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
494 passed in 3.56s

uv run ruff check .
All checks passed!

uv run ruff format --check .
25 files already formatted

uv run mypy src
Success: no issues found in 14 source files

uv run pip-audit --strict --requirement /tmp/jev-task-006-audit.txt --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

git diff --check HEAD^ HEAD
exit 0
```

`pip-audit` emitted only its standard `--no-deps` advisory warnings. Built wheel and sdist both contain `jev_trading_lab/persistence/schema/001_migration_metadata.sql`; the clean artifact file-set test passed.

## Self-review

- Confirmed migration 001 creates no business table.
- Confirmed forbidden SQL validation occurs across the full package before migration 001 can bootstrap metadata.
- Confirmed applied resource name/hash drift fails before pending changes.
- Confirmed network filesystem rejection occurs before `sqlite3.connect` in injected tests.
- Confirmed runtime bytecode cache directories do not make source-tree resource loading fail.
- Confirmed no dependency or lock changes and no design/plan/review edits.
- No push performed.

## Independent reviews

- Spec/adversarial review: **PENDING**
- Quality/model-risk review: **PENDING**

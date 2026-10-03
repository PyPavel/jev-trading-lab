# Task 006 Delivery Evidence

## Status

**PENDING INDEPENDENT RE-REVIEW**

All remaining Task 6 database and migration trust findings were addressed and verified locally. No independent re-review has approved this revision.

## Production commits

```text
847cbc1 feat(persistence): add checked SQLite migrations
40dcc21 fix(persistence): harden database and migrations
5e05789 fix(persistence): bind database and migration trust
```

## Review findings resolved

- **Database inode and parent trust:** `connect_database` now opens SQLite through `/proc/self/fd/<retained-parent-fd>/<filename>`. `SecureConnection` owns that validated parent descriptor until `close()`. The connect path snapshots `/proc/self/fd`, verifies the newly opened SQLite main descriptor by `fstat` device/inode against the descriptor-relative `O_NOFOLLOW` pre-open, verifies SQLite's resolved main path, and closes connection/descriptors on every rejection path. Exact A/B inode restoration, parent replacement, retained-fd lifetime, and leak probes are covered.
- **WAL/SHM trust:** descriptor-relative checks reject pre-existing sidecar symlinks, special files, wrong owner, multiple links, and modes other than exact `0600`. WAL activation runs under a serialized, narrowly scoped `0177` umask, restores the prior umask, and rechecks sidecars immediately after activation and during final storage validation. Pre-existing `0644`, symlink, hardlink, and activation-race probes are covered.
- **Migration resource trust:** filesystem package directories must be non-symlink directories owned by root/effective UID and not group/other writable. Migration files use one `O_NOFOLLOW` descriptor, require root/effective-UID ownership, one link, and no group/other writes, use a bounded chunk read, and compare device, inode, size, mtime_ns, ctime_ns, mode, UID, and link count before/after. Equal-size and mixed-byte in-place rewrites are rejected. Archive `Traversable` resources retain bounded streaming and rely on the exact lock/hash-protected wheel artifact.
- **Main-only migration writes:** migration connections reject attached databases before loading/applying. A scoped SQLite authorizer denies ATTACH/DETACH, TEMP object operations, and schema/data writes outside `main`; it is cleared in `finally` under the documented dedicated-connection contract. Parser defense also rejects `CREATE TEMP`, `CREATE TEMPORARY`, and explicit TEMP schema statements. TEMP and auxiliary migration failures roll back without metadata or off-main objects and remain absent after reconnect.
- **Timestamp integrity:** migration 001 now requires built-in SQLite integer storage with `applied_at_us` in the applied-migration domain `0..253402300799999999`. Application metadata reads validate exact integer type and range. `0` is accepted; `-1` and signed-64 maximum are rejected by direct constraints and detected as corruption on reconnect.
- **Prior fixes preserved:** caller transactions are rejected before owned transaction handling; metadata remains `main`-qualified; empty chains fail; local filesystems fail closed; private parents remain exact `0700`; migration filename checks and installed-wheel smoke remain green.
- **Policy updates:** exact source, migration SQL, preflight, and workflow hashes were deliberately updated only after behavior stabilized.

## TDD RED evidence

Each reviewer reproduction was added and run individually before production changes. Every probe exited 1 with the intended failure:

```text
test_connection_retains_private_parent_descriptor_until_close: 1 failed
test_database_inode_swap_restored_before_path_recheck_is_rejected: 1 failed
test_parent_path_swap_cannot_redirect_descriptor_relative_connect: 1 failed
test_preexisting_unsafe_sidecar_mode_is_rejected: 2 failed
test_preexisting_sidecar_symlink_is_rejected: 2 failed
test_preexisting_sidecar_hardlink_is_rejected: 2 failed
test_sidecar_created_racing_wal_activation_is_rejected: 1 failed
test_world_writable_migration_package_directory_is_rejected: 1 failed
test_group_writable_migration_file_is_rejected: 1 failed
test_equal_size_resource_rewrite_during_read_is_rejected: 1 failed
test_mixed_resource_bytes_from_in_place_rewrite_are_rejected: 1 failed
test_migration_rejects_pre_attached_auxiliary_database: 1 failed
test_create_temp_migration_is_denied_and_rolled_back: 1 failed
test_schema_rejects_invalid_applied_timestamp: 2 failed
test_reconnect_detects_corrupt_applied_timestamp: 2 failed
```

The exact source-policy preflight was also witnessed RED after source/SQL changes and before hash updates:

```text
policy preflight: src/jev_trading_lab/persistence/schema/001_migration_metadata.sql: SHA-256 does not match approved content
policy preflight: src/jev_trading_lab/persistence/migrations.py: SHA-256 does not match approved content
policy preflight: src/jev_trading_lab/persistence/db.py: SHA-256 does not match approved content
```

## GREEN evidence

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/persistence/test_migrations.py -q
110 passed in 0.26s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_migrations.py tests/test_package_policy.py -q
166 passed in 1.63s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
561 passed in 3.83s
```

## Verification gates

All commands exited 0:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.61ms

env PYTHONDONTWRITEBYTECODE=1 uv run ruff check .
All checks passed!

env PYTHONDONTWRITEBYTECODE=1 uv run ruff format --check .
26 files already formatted

env PYTHONDONTWRITEBYTECODE=1 uv run mypy src
Success: no issues found in 14 source files

uv export --frozen --all-groups --no-emit-project \
  --format requirements-txt --output-file /tmp/jev-task-006-final-audit.txt
exit 0

uv run pip-audit --strict \
  --requirement /tmp/jev-task-006-final-audit.txt --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

uv run python scripts/wheel_migration_smoke.py
Successfully installed jev-trading-lab-0.1.0
Installed-wheel migration smoke passed.

env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/test_package_policy.py::test_migration_sql_is_packaged_in_wheel_and_sdist \
  tests/test_package_policy.py::test_built_artifacts_exclude_repository_and_host_junk -q
2 passed in 0.43s

100 connection open/close cycles: no fd leak

git diff --check
exit 0
```

`pip-audit` emitted only its standard `--no-deps` advisory warnings. `uv.lock` and `pyproject.toml` were unchanged. Design, plan, and review documents were unchanged. No push was performed.

## Independent reviews

- Spec/adversarial re-review: **PENDING**
- Quality/model-risk re-review: **PENDING**

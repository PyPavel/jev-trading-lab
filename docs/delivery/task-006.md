# Task 006 Delivery Evidence

## Status

**PENDING INDEPENDENT RE-REVIEW**

All reported Task 6 adversarial findings were addressed and verified locally. No independent re-review has approved this revision.

## Production commits

```text
847cbc1 feat(persistence): add checked SQLite migrations
40dcc21 fix(persistence): harden database and migrations
5e05789 fix(persistence): bind database and migration trust
88dc9fd fix(persistence): close descriptor and authorizer races
```

## Review findings resolved

- **Database descriptor and dedicated-directory trust:** `connect_database` retains an effective-UID-owned exact-`0700` parent descriptor and connects through `/proc/self/fd/<dirfd>/<basename>`. Descriptor-relative scans require the physical directory to contain only the database basename and its `-wal`/`-shm` sidecars. A `/proc/self/fd` census made before connect verifies the lowest new regular descriptor as SQLite's main database, requires its device/inode to equal the securely prepared inode, rejects duplicate/ambiguous database candidates, validates every new sidecar descriptor, and rejects any extra new regular descriptor in the retained directory. The census is repeated after WAL activation while connect setup is serialized. Exact A/B swap/restore with a decoy FD, deleted replacement inode with a decoy FD, renamed-away unsafe sidecar, unrelated-FD acceptance, and 100-cycle leak probes are covered.
- **Security boundary:** this is a strong dedicated-directory invariant, not a claim that stdlib SQLite can defeat every same-UID hostile process. A same-UID actor that swaps, renames, or unlinks an opened database/sidecar is detected through retained-directory state plus actual new-FD metadata. Any descriptor ambiguity fails closed. The parent descriptor remains retained for connection lifetime.
- **Migration parent TOCTOU:** filesystem package directories are traversed component-by-component with `O_DIRECTORY|O_NOFOLLOW` and `dir_fd`; each descriptor is retained through all resource reads. Every component requires root/effective-UID ownership and no group/world write bit, and its device/inode/mode/UID/ctime snapshot is revalidated after loading. Migration files are opened by basename with `O_NOFOLLOW` relative to the retained schema descriptor; no absolute pathname reopen occurs. Descriptor cleanup is unconditional. A schema-directory swap/restore race fails before attacker SQL executes or metadata is recorded. Archive `Traversable` resources retain bounded streaming and rely on exact locked/hash-protected wheel bytes.
- **Main-only authorizer:** `PRAGMA database_list` must be exactly `main`, optionally followed only by fileless `temp`; attached databases remain rejected. The authorizer stays active from before migration statements through timestamp generation, metadata insertion, and `COMMIT`, and is cleared only after commit or rollback. It denies all TEMP create/drop actions, ATTACH/DETACH, REINDEX, ANALYZE, and write-capable actions whose database is not `main`. Parser defense recognizes quoted and unquoted TEMP schema qualification plus `CREATE TEMP`/`CREATE TEMPORARY`; SQLite's authorizer remains authoritative. Exact `REINDEX "temp".idx` and a pre-existing TEMP trigger attempting to write `temp.sink` during metadata insertion both fail with no metadata row or main payload after reconnect.
- **Timestamp integrity independently preserved:** migration SQL requires `typeof(applied_at_us) = 'integer'` and range `0..253402300799999999`; direct `-1` and signed-64 maximum inserts fail. `_applied_rows` requires exact built-in `int` and the same current-era range. Reconnect corruption probes remain green.
- **Policy updates:** exact source, preflight, and workflow hashes were updated after behavior stabilized. Migration SQL was unchanged because its existing timestamp constraint was already correct.

## TDD RED evidence

The exact new reviewer reproductions were run before production changes. The combined command exited 1 with seven intended failures and two controls already passing:

```text
FAILED test_database_parent_rejects_unexpected_entry
FAILED test_decoy_fd_cannot_hide_sqlite_opening_replaced_inode
FAILED test_deleted_sqlite_inode_cannot_be_hidden_by_decoy_fd
FAILED test_unsafe_open_sidecar_renamed_away_is_rejected
FAILED test_schema_directory_swap_restore_is_rejected_before_sql_executes
FAILED test_quoted_temp_reindex_is_denied_without_metadata
FAILED test_temp_trigger_cannot_write_during_metadata_insert
7 failed, 2 passed in 0.31s
```

The two passing controls were `test_unrelated_new_regular_fd_does_not_block_connection` and `test_connection_open_close_cycles_do_not_leak_descriptors`. The first migration-race RED reached attacker SQL and then failed with `sqlite3.OperationalError: no such table: main.schema_migrations`, proving the untrusted replacement resource was read before the descriptor-relative fix. The explicit quoted non-main schema parser reproduction also failed RED with `sqlite3.OperationalError: unknown database "aux"` before the parser defense was added.

Exact source-policy preflight was witnessed RED before hash updates because `db.py` and `migrations.py` no longer matched approved SHA-256 values. Hashes were updated only after focused behavior was green.

## GREEN evidence

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/persistence/test_migrations.py -q
119 passed in 0.33s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_migrations.py tests/test_package_policy.py -q
176 passed in 1.63s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
571 passed in 3.68s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_migrations.py::test_connection_open_close_cycles_do_not_leak_descriptors \
  tests/persistence/test_migrations.py::test_schema_rejects_invalid_applied_timestamp \
  tests/persistence/test_migrations.py::test_reconnect_detects_corrupt_applied_timestamp -q
5 passed in 0.21s
```

## Verification gates

All commands exited 0:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.59ms

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
2 passed in 0.40s

git diff --check
exit 0
```

`pip-audit` emitted only its standard `--no-deps` advisory warnings. `uv.lock` and `pyproject.toml` were unchanged. Design, plan, and review documents were unchanged. No push was performed.

## Independent reviews

- Spec/adversarial re-review: **PENDING**
- Quality/model-risk re-review: **PENDING**

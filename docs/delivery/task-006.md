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
ba6329e fix(persistence): validate every SQLite descriptor
```

## Final findings resolved

- **Fail-closed process descriptor census:** `connect_database` snapshots `/proc/self/fd` immediately before `sqlite3.connect` while holding a module process-wide lock. The lock remains held through SQLite connect, main-FD validation, WAL activation, the second complete census, storage rechecks, and pragma setup. Every newly opened regular descriptor is classified; none are ignored because of descriptor number, renamed pathname, deletion, or location outside the retained database directory.
- **Main database identity:** exactly one new regular descriptor must match the securely prepared database device/inode. The census no longer chooses the lowest FD. An expected-inode decoy opened before SQLite followed by SQLite opening a replacement inode creates an unclassified mismatch and is rejected. Replacement/deletion/restore variants remain rejected and failed construction closes the SQLite connection plus retained parent descriptor.
- **Sidecar identity and metadata:** every non-main new regular descriptor must still resolve to the exact `db.sqlite3-wal` or `db.sqlite3-shm` basename under the retained parent descriptor. Its `fstat` owner, regular-file type, exact `0600` mode, and link count are checked, then its device/inode is correlated with the descriptor-relative visible entry. Renamed, moved outside, unlinked, unsafe `0666`, hard-linked, ambiguous, and otherwise unprovable descriptors fail closed.
- **Concurrent unrelated descriptors:** any unrelated regular FD opened by another thread during this critical section causes safe rejection. This deliberate fail-closed behavior prevents unrelated same-process descriptor activity from being mistaken for SQLite state. Legitimate `connect_database` setup overlap is serialized by the module lock.
- **GC descriptor lifecycle:** `SecureConnection.__del__` directly detaches and closes `_parent_descriptor`, then invokes `sqlite3.Connection.close`, suppressing shutdown-time exceptions. Explicit close and finalization are idempotent. Tests cover dropping the last reference plus `gc.collect`, explicit close followed by FD-number reuse and GC, constructor failure, 100 GC cycles, and 100 explicit close cycles.
- **Prior migration protections preserved:** descriptor-relative migration resource loading, retained package-directory descriptors, main-only authorizer enforcement through metadata insertion/commit, TEMP/ATTACH/DETACH/REINDEX/ANALYZE denial, and timestamp integrity tests remain green.
- **Policy updates:** exact `db.py`, preflight, and workflow hashes were updated after behavior stabilized. `pyproject.toml`, `uv.lock`, dependencies, and migration SQL were unchanged.

## Security boundary

This is a same-process fail-closed descriptor census plus an effective-UID-owned dedicated `0700` directory and retained dirfd boundary. It detects and rejects the exact same-process monkeypatch/thread reproductions reviewed for Task 6. It is **not** protection against a malicious same-UID process capable of manipulating another process's namespace or `/proc` view; Python stdlib SQLite cannot establish that stronger cross-process boundary.

## TDD RED evidence

The final exact reproductions were added before production changes. The focused command exited 1:

```text
FAILED test_decoy_opened_before_sqlite_cannot_hide_replaced_inode
FAILED test_unsafe_open_sidecar_renamed_away_is_rejected
FAILED test_unrelated_regular_fd_opened_during_connect_fails_closed
FAILED test_dropped_connection_releases_parent_descriptor
FAILED test_explicit_close_then_gc_does_not_double_close_reused_fd
FAILED test_dropped_connection_cycles_do_not_leak_descriptors
6 failed, 1 passed in 0.30s
```

`test_failed_connection_construction_does_not_leak_descriptors` was the passing control: the pre-existing exception cleanup already released the retained parent descriptor when `sqlite3.connect` raised before returning a connection. The GC-cycle RED later failed while leaked prior connections accumulated, independently confirming missing finalization.

Exact source-policy preflight was also witnessed RED before hash updates. It reported `src/jev_trading_lab/persistence/db.py: SHA-256 does not match approved content`; generated `__pycache__` entries from an isolated diagnostic were removed, then approved hashes were updated only after focused behavior was green.

## GREEN evidence

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_migrations.py::test_connection_pragmas \
  tests/persistence/test_migrations.py::test_decoy_opened_before_sqlite_cannot_hide_replaced_inode \
  tests/persistence/test_migrations.py::test_unsafe_open_sidecar_renamed_away_is_rejected \
  tests/persistence/test_migrations.py::test_unrelated_regular_fd_opened_during_connect_fails_closed \
  tests/persistence/test_migrations.py::test_dropped_connection_releases_parent_descriptor \
  tests/persistence/test_migrations.py::test_explicit_close_then_gc_does_not_double_close_reused_fd \
  tests/persistence/test_migrations.py::test_failed_connection_construction_does_not_leak_descriptors \
  tests/persistence/test_migrations.py::test_dropped_connection_cycles_do_not_leak_descriptors -q
8 passed in 0.68s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/persistence/test_migrations.py -q
124 passed in 0.93s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_package_policy.py -q
56 passed in 1.56s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
575 passed in 6.60s
```

## Verification gates

All commands exited 0:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.71ms

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
2 passed in 0.39s

git diff --check
exit 0
```

`pip-audit` emitted only its standard `--no-deps` advisory warnings. Design, plan, and review documents were unchanged. No push was performed.

## Independent reviews

- Spec/adversarial re-review: **PENDING**
- Quality/model-risk re-review: **PENDING**

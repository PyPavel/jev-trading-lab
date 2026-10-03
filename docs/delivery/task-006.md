# Task 006 Delivery Evidence

## Status

**PENDING INDEPENDENT RE-REVIEW**

The migration-enumeration TOCTOU finding is fixed. The descriptor-census boundary is now explicit and tested without claiming complete detection of hostile same-process FD ABA. No independent re-review has approved this revision.

## Production commits

```text
847cbc1 feat(persistence): add checked SQLite migrations
40dcc21 fix(persistence): harden database and migrations
5e05789 fix(persistence): bind database and migration trust
88dc9fd fix(persistence): close descriptor and authorizer races
ba6329e fix(persistence): validate every SQLite descriptor
dea5a97 fix(persistence): bind migration enumeration
```

## Findings resolved

- **Descriptor-bound migration enumeration:** filesystem package loading now opens and validates the package-directory chain descriptor-relatively before enumeration. It enumerates with `os.listdir(final_dirfd)`, opens every accepted filename through the same retained final dirfd with `O_NOFOLLOW`, and revalidates all retained directory identities before and after enumeration and each read. Enumeration, reads, and final checks therefore remain bound to one directory inode.
- **Exact swap/restore omission:** the RED reproduction replaced a trusted schema containing `001` and `002` with an attacker schema containing only `001` during old `Path.iterdir()` enumeration, then restored the original before reads. Old code silently returned only `001`. The fixed code never calls pathname enumeration and returns trusted `001` plus `002` from the retained dirfd.
- **Injection/replacement variants:** deterministic `os.listdir(dirfd)` swap/injection reproductions either enumerate the retained trusted inode or fail when retained package-directory metadata changes; injected `003` and attacker `001` are never loaded. Existing file-open and package-directory swap/restore attacks remain fail-closed.
- **Archive resources:** non-filesystem `Traversable` packages still enumerate and read from the same trusted package `Traversable`; reads remain bounded to `MAX_MIGRATION_BYTES + 1` and rely on the exact locked, hash-protected package artifact.
- **Chain integrity:** at least migration `001` is required and versions must remain contiguous from `001`.
- **Cooperative FD census:** `_connection_fd_activity()` now names and documents the supported synchronization boundary. `connect_database()` acquires it before filesystem validation, parent-directory traversal, database preparation, the initial census, SQLite connect, WAL activation, the final census, and all storage/pragma checks. Concurrent library connection attempts cannot overlap this FD-critical section.
- **No hostile in-process ABA overclaim:** tests reproduce close/reopen of the same FD number to the same inode and confirm stdlib `(fd, st_dev, st_ino)` snapshots cannot distinguish it. This is documented as outside the supported boundary instead of falsely claiming rejection. Cooperative new/decoy FDs created inside library-controlled setup remain classified and rejected when mismatched.
- **Prior protections preserved:** exact main/sidecar descriptor classification, private parent retention, GC cleanup, migration authorizer enforcement, TEMP/ATTACH/DETACH/REINDEX/ANALYZE denial, timestamp integrity, migration checksums, and exact package policy remain green.
- **Policy updates:** exact `db.py`, `migrations.py`, preflight, workflow, and mirrored policy-test hashes were updated after behavior stabilized. `pyproject.toml`, `uv.lock`, dependencies, and migration SQL were unchanged.

## Security boundary

`_CONNECT_LOCK` serializes all supported connection and relevant FD activity performed by this library. Within that cooperative boundary, the descriptor census detects regular FDs created during the critical section and rejects unclassified or mismatched SQLite descriptors. All library connection setup that opens relevant FDs runs under the same lock.

A malicious or buggy same-process thread that bypasses `_CONNECT_LOCK` can close an existing FD and reopen the same inode at the same FD number between snapshots. Python stdlib snapshot metadata cannot reliably distinguish that ABA sequence. That activity is explicitly outside the supported boundary. This library is not a sandbox against malicious in-process code. The existing same-effective-UID cross-process limitation also remains: stdlib SQLite and `/proc` inspection do not establish a stronger hostile-process boundary.

## TDD RED evidence

Migration race tests were added before production changes. The focused command exited 1:

```text
FAILED test_migration_enumeration_swap_restore_cannot_omit_trusted_file
  assert [1] == [1, 2]
FAILED test_migration_enumeration_uses_retained_directory_fd_during_injection
  assert swapped
2 failed in 0.21s
```

The first failure is the exact reviewer omission: old pathname enumeration saw attacker-only `001`, restoration occurred before reads, and loading silently omitted trusted `002`. The second established that enumeration was not yet performed through a retained directory descriptor.

The ABA boundary tests were also added before production changes. The focused command exited 1:

```text
PASSED test_raw_same_process_fd_aba_bypassing_connect_lock_is_outside_boundary
FAILED test_descriptor_census_documents_cooperative_lock_boundary
  AttributeError: module ...db has no attribute '_connection_fd_activity'
FAILED test_concurrent_library_connection_attempts_serialize_fd_activity
  assert not True
PASSED test_cooperative_new_fd_during_connect_fails_closed
2 failed, 2 passed in 0.23s
```

The passing ABA reproduction demonstrates the honest limitation: same-number/same-inode close/reopen bypassing the library lock is invisible to the census. The failures captured the missing encoded boundary and that the old lock began too late to serialize all library FD activity.

Exact policy preflight was witnessed RED before hash updates:

```text
policy preflight: src/jev_trading_lab/persistence/migrations.py: SHA-256 does not match approved content
policy preflight: src/jev_trading_lab/persistence/db.py: SHA-256 does not match approved content
```

Approved source hashes after stabilization:

```text
d4702087efc2a6aa99691b8d002509ba211f9c896289a47d59eff72858696d82  src/jev_trading_lab/persistence/db.py
14ef2054cc27d54090d0f05de00cb9a8b511e592d6d35ec09980984f89582840  src/jev_trading_lab/persistence/migrations.py
b5d4b1e2d8b5895f8ee97e3672e6723dea662ceedfa6838e3cde997eecc31475  scripts/policy_preflight.py
2614aaa2a1e1daae2007d5d7df6e60c90124b0736fc5051320ad86c4aff6b206  .github/workflows/ci.yml
```

## GREEN evidence

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_migrations.py::test_migration_enumeration_swap_restore_cannot_omit_trusted_file \
  tests/persistence/test_migrations.py::test_migration_enumeration_uses_retained_directory_fd_during_injection \
  tests/persistence/test_migrations.py::test_raw_same_process_fd_aba_bypassing_connect_lock_is_outside_boundary \
  tests/persistence/test_migrations.py::test_descriptor_census_documents_cooperative_lock_boundary \
  tests/persistence/test_migrations.py::test_concurrent_library_connection_attempts_serialize_fd_activity \
  tests/persistence/test_migrations.py::test_cooperative_new_fd_during_connect_fails_closed -q
6 passed in 0.35s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/persistence/test_migrations.py -q
129 passed in 1.07s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_package_policy.py -q
56 passed in 1.28s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
580 passed in 6.56s
```

## Verification gates

All final commands exited 0:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.60ms

env PYTHONDONTWRITEBYTECODE=1 uv run ruff check .
All checks passed!

env PYTHONDONTWRITEBYTECODE=1 uv run ruff format --check .
26 files already formatted

env PYTHONDONTWRITEBYTECODE=1 uv run mypy src
Success: no issues found in 14 source files

uv export --frozen --all-groups --no-emit-project \
  --format requirements-txt --output-file /tmp/jev-task-006-enumeration-audit.txt
exit 0

uv run pip-audit --strict \
  --requirement /tmp/jev-task-006-enumeration-audit.txt --no-deps --disable-pip
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
2 passed in 0.45s

git diff --check
exit 0
```

`pip-audit` emitted only its standard `--no-deps` advisory warnings. Design, plan, and review documents were unchanged. No dependency or SQL changes were made. No push was performed.

## Independent reviews

- Spec/adversarial re-review: **PENDING**
- Quality/model-risk re-review: **PENDING**

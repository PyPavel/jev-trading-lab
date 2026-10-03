# Task 006 Delivery Evidence

## Status

**PENDING INDEPENDENT RE-REVIEW**

All Task 6 review findings were addressed in the production commit below and verified locally. No independent re-review has yet approved this revision.

## Production commits

```text
847cbc1 feat(persistence): add checked SQLite migrations
40dcc21 fix(persistence): harden database and migrations
```

## Review findings resolved

- **Filesystem/path blocker:** database paths must be absolute and lexically normalized before mount lookup. Descriptor-relative `O_NOFOLLOW` traversal rejects symlink components. Mountinfo escapes are decoded, containment is component-aware, longest mount points win, and the last equal-depth stacked record is visible.
- **Filesystem policy:** replaced the remote denylist with a reviewed local allowlist: `ext2`, `ext3`, `ext4`, `xfs`, `btrfs`, `f2fs`, `zfs`, `overlay`, and `tmpfs`. Unknown types plus all reviewed NFS/CIFS/SMB/FUSE/9p/Ceph/Gluster/Lustre/AFS/DAV types fail closed. `overlay` and `tmpfs` are required for common local/container and temporary test environments and retain the same private-directory boundary.
- **Private SQLite boundary:** the final parent is created as `0700` or must already be a non-symlink directory owned by the effective UID with exact mode `0700`. Existing database files must be regular, effective-UID-owned, exact `0600`, and single-linked. Parent and file identity/owner/mode are rechecked after connect. WAL/SHM sidecars are verified `0600`. This is a pathname/WAL/SHM TOCTOU boundary, not an atomic fd binding claim.
- **Adversarial path tests:** unsafe `0755`/`0777` parents, hard links, path replacement, parent replacement, symlinks, dot/dotdot spellings, stacked overmounts, and sidecar modes are covered.
- **Caller transaction blocker:** migration application requires `isolation_level=None` and an idle connection. An active caller transaction raises deterministic `MigrationError` before migration work, remains active, retains unrelated inserts, and can be committed by the caller. Rollback is performed only for a transaction successfully started by the migration runner.
- **TEMP shadowing:** all metadata reads/inserts use `main.schema_migrations`; existence checks use `main.sqlite_schema`. A populated TEMP shadow remains untouched while main metadata applies and survives reconnect consistently.
- **Empty chain:** `load_migrations` requires at least one migration and a contiguous chain starting at 001; empty packages fail before database mutation.
- **Schema name constraints:** migration metadata rejects separators, controls, uppercase, punctuation outside lowercase alphanumeric/underscore, empty or invalid slugs, bad suffixes, and version/name prefix mismatches.
- **Resource TOCTOU/bounds:** filesystem resources are opened once with `O_RDONLY|O_CLOEXEC|O_NOFOLLOW|O_NONBLOCK`, checked as regular and effective-UID-owned, read in chunks to `MAX+1`, and fstat-rechecked for inode/size changes. Archive resources use one bounded binary stream read. Symlink replacement, FIFO/special/unreadable input, invalid UTF-8, and oversized streams are covered.
- **Actual wheel CI:** added `scripts/wheel_migration_smoke.py`. CI builds first, then creates a clean Python 3.12 venv, installs the actual wheel without dependencies, runs outside the source tree, loads packaged migration 001, applies it, and validates main metadata. Script content/hash, workflow order/hash, source/SQL hashes, and preflight hash are exact-policy protected.
- Trigger-aware splitting, forbidden SQL checks, per-migration owned transactions, idempotence, rollback, and concurrent migration serialization remain covered.

## TDD RED evidence

Reviewer probes were added before production changes. The initial focused run exited 1 with **33 failed, 57 passed**. Representative intended failures:

```text
test_database_path_must_be_absolute_and_lexically_normalized
FAILED: DID NOT RAISE ValueError

test_mountinfo_last_stacked_mount_wins_and_matches_components
FAILED: expected nfs from last stacked /data record, received ext4

test_unknown_remote_and_fuse_filesystems_fail_closed
FAILED: unknownfs/fuse variants did not raise

test_existing_database_parent_requires_exact_private_mode
FAILED: unsafe 0755/0777 parents were accepted

test_existing_database_requires_owner_mode_and_single_link
FAILED: existing 0644 file was silently changed instead of rejected

test_apply_rejects_caller_transaction_without_rollback
FAILED: sqlite3.OperationalError: cannot start a transaction within a transaction

test_apply_requires_autocommit_connection_contract
FAILED: DID NOT RAISE MigrationError

test_temp_schema_migrations_cannot_shadow_main
FAILED: TEMP row count became 2

test_empty_migration_package_fails_before_database_mutation
FAILED: DID NOT RAISE MigrationError

test_schema_rejects_hostile_or_mismatched_migration_names
FAILED: hostile/mismatched direct inserts were accepted

test_archive_resource_read_is_bounded_to_maximum_plus_one
FAILED: read_bytes was called
```

The wheel policy probe was also witnessed RED individually:

```text
test_ci_builds_before_actual_wheel_migration_smoke
FAILED: expected `run: uv run python scripts/wheel_migration_smoke.py` in workflow
```

Exact preflight then failed closed while production/SQL/script/workflow hashes differed; hashes were updated only after the reviewed content stabilized.

## GREEN evidence

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/persistence/test_migrations.py tests/test_package_policy.py -q
146 passed in 1.75s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
541 passed in 3.69s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/test_package_policy.py::test_migration_sql_is_packaged_in_wheel_and_sdist \
  tests/test_package_policy.py::test_built_artifacts_exclude_repository_and_host_junk -q
2 passed in 0.42s
```

## Verification gates

All commands exited 0:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.87ms

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
541 passed in 3.69s

env PYTHONDONTWRITEBYTECODE=1 uv run ruff check .
All checks passed!

env PYTHONDONTWRITEBYTECODE=1 uv run ruff format --check .
26 files already formatted

env PYTHONDONTWRITEBYTECODE=1 uv run mypy src
Success: no issues found in 14 source files

uv export --frozen --all-groups --no-emit-project \
  --format requirements-txt --output-file /tmp/jev-task-006-review-audit.txt
exit 0

uv run pip-audit --strict \
  --requirement /tmp/jev-task-006-review-audit.txt --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

uv run python scripts/wheel_migration_smoke.py
Successfully installed jev-trading-lab-0.1.0
Installed-wheel migration smoke passed.

git diff --check
exit 0
```

`pip-audit` emitted only its standard `--no-deps` advisory warnings. `uv.lock` and `pyproject.toml` were unchanged. Design, plan, and review documents were unchanged. No push was performed.

## Independent reviews

- Spec/adversarial re-review: **PENDING**
- Quality/model-risk re-review: **PENDING**

# Task 006 Delivery Evidence

## Status

**PENDING INDEPENDENT RE-REVIEW**

The final migration-directory descriptor leak and mount-resolver reentrancy deadlock are fixed. No independent re-review has approved this revision.

## Production commits

```text
847cbc1 feat(persistence): add checked SQLite migrations
40dcc21 fix(persistence): harden database and migrations
5e05789 fix(persistence): bind database and migration trust
88dc9fd fix(persistence): close descriptor and authorizer races
ba6329e fix(persistence): validate every SQLite descriptor
dea5a97 fix(persistence): bind migration enumeration
805a7f1 fix(persistence): close rejected descriptors and resolver deadlock
```

## Findings resolved

- **Rejected directory descriptor ownership:** `_open_resource_directories()` now owns each child descriptor immediately after `os.open()`. Validation or `fstat()` failure closes that child before outer cleanup closes previously retained descriptors. Successful children transfer to `opened` only after validation, preventing both leaks and double-close.
- **Depth and repetition coverage:** mode-`0777` package and schema directories are rejected 100 times at each package depth with an unchanged `/proc/self/fd` set. Successful migration loads are also repeated 100 times with unchanged FD sets.
- **Retained read descriptors:** the success reproduction asserts the final package directory descriptor remains valid during every filesystem resource read, then verifies all retained descriptors are closed when `load_migrations()` returns.
- **Resolver reentrancy:** caller-provided `mount_resolver` callbacks execute before `_CONNECT_LOCK`. A callback may recursively call `connect_database()`; the subprocess reproduction now terminates with deterministic `DatabaseSafetyError` instead of hanging on the non-reentrant lock.
- **Untrusted callback result:** resolver output is normalized and allowlisted before any connection setup. Invalid, empty, non-string, unknown, remote, and FUSE results fail with `DatabaseSafetyError`, a `DatabaseConfigurationError` subtype.
- **Resolver exception cleanup:** a resolver that raises is exercised 100 times with an unchanged `/proc/self/fd` set.
- **Serialization preserved:** the existing concurrent normal-call test confirms parent traversal and all descriptor-census/SQLite setup work remain serialized under `_CONNECT_LOCK`.
- **TOCTOU boundary:** arbitrary callback code is outside the lock. Default mount classification is repeated inside the lock and compared with the pre-lock classification; custom resolvers are test injection only and are not reinvoked while locked. Parent/file/sidecar identities, ownership, modes, links, directory entries, SQLite descriptors, and storage remain revalidated inside the locked descriptor-bound setup. This does not claim protection against hostile same-process code bypassing the lock or same-effective-UID cross-process mutation.
- **Prior protections preserved:** descriptor-bound migration enumeration, exact main/sidecar classification, private parent retention, GC cleanup, migration authorizer enforcement, SQL restrictions, timestamp integrity, checksums, and exact package policy remain green.
- **Policy updates:** exact `db.py`, `migrations.py`, preflight, workflow, and mirrored policy-test hashes were updated after behavior stabilized. `pyproject.toml`, `uv.lock`, dependencies, and migration SQL were unchanged.

## TDD RED evidence

The exact leak and deadlock reproductions were added before production changes. The focused command exited 1:

```text
FAILED test_rejected_migration_package_directories_do_not_leak_descriptors
  Extra items in the left set: 13, 14, 15, 16, 17, ...
FAILED test_recursive_mount_resolver_fails_without_deadlock
  subprocess.TimeoutExpired: ... timed out after 3 seconds
FAILED test_mount_resolver_result_is_validated[None]
FAILED test_mount_resolver_result_is_validated[]
FAILED test_mount_resolver_result_is_validated[7]
FAILED test_mount_resolver_result_is_validated[invalid3]
6 failed, 2 passed in 3.35s
```

The descriptor failure grew `/proc/self/fd` after repeated mode-`0777` package rejection. The timeout was the exact recursive callback deadlock. Resolver exception cleanup and the successful-load retention/cleanup reproduction already passed; invalid resolver values still raised the broader pre-existing exception class rather than the requested deterministic safety subtype.

Exact policy preflight was witnessed RED before hash updates:

```text
policy preflight: src/jev_trading_lab/persistence/migrations.py: SHA-256 does not match approved content
policy preflight: src/jev_trading_lab/persistence/db.py: SHA-256 does not match approved content
```

Approved hashes after stabilization:

```text
1cbe162d8dfa474f6c01885a1be9c26be3ad7999fee5b7aaa55888df8cde1862  src/jev_trading_lab/persistence/db.py
c4fcd77f969af50024bf3e0c5bd4c1337f0af4874b82de124b32c1e4715ece9b  src/jev_trading_lab/persistence/migrations.py
e49a219b00d3545a40f10b8d72489e5697442290ff06d0e42c9a36a020a26b3c  scripts/policy_preflight.py
39dfed5a35bb62412439f61babf974fb7083fa0da9f5df929ce954c9ff9bffa3  .github/workflows/ci.yml
```

## GREEN evidence

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_migrations.py::test_rejected_migration_package_directories_do_not_leak_descriptors \
  tests/persistence/test_migrations.py::test_successful_migration_load_does_not_leak_or_close_retained_directories \
  tests/persistence/test_migrations.py::test_recursive_mount_resolver_fails_without_deadlock \
  tests/persistence/test_migrations.py::test_mount_resolver_exception_does_not_leak_descriptors \
  tests/persistence/test_migrations.py::test_mount_resolver_result_is_validated \
  tests/persistence/test_migrations.py::test_concurrent_library_connection_attempts_serialize_fd_activity -q
9 passed in 0.46s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/persistence/test_migrations.py -q
137 passed in 1.36s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_package_policy.py -q
56 passed in 1.55s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
588 passed in 6.41s
```

## Verification gates

All final commands exited 0:

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
  --format requirements-txt --output-file /tmp/jev-task-006-cleanup-audit.txt
exit 0

uv run pip-audit --strict \
  --requirement /tmp/jev-task-006-cleanup-audit.txt --no-deps --disable-pip
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

`pip-audit` emitted only its standard `--no-deps` advisory warnings. Design, plan, and review documents were unchanged. No dependency or SQL changes were made. No push was performed.

## Independent reviews

- Spec/adversarial re-review: **PENDING**
- Quality/model-risk re-review: **PENDING**

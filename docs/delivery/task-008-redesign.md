# Task 008 Architectural Redesign Evidence

## Status

**PENDING INDEPENDENT RE-REVIEW**

Production commit: `c36a1d8 fix(persistence): close final isolated writer gaps`

No push or independent approval occurred. This document records implementation and local evidence only.

## Delivered architecture

- Factory-created public `SecureConnection` installs the restrictive runtime authorizer once during initialization. Production contains no temporary journal/migration authorizer scope, restoration helper, or trace/profile preflight.
- Runtime policy denies journal DML, `schema_migrations` DML, schema/TEMP DDL, ATTACH/DETACH, and hardened dangerous configuration surfaces.
- `AppendOnlyJournal` retains the exact public factory connection only as provenance, path/configuration, and liveness. Each append opens a fresh fixed-role internal writer for exactly one selected journal table.
- Journal writer policy permits reads and direct main INSERT only into the selected table. It denies other inserts, UPDATE/DELETE, trigger-originated writes, schema/TEMP operations, and ATTACH/DETACH. Each private writer binds the SQLite-opened main FD to the expected `(st_dev, st_ino)`, classifies every SQLite main/sidecar FD, revalidates the restored path, and repeats storage validation around `BEGIN IMMEDIATE` before schema-prefix/checksum, idempotency, conflict, predecessor, and exact-literal checks.
- `apply_migrations(connection)` validates the parent connection and invokes a packaged helper through `sys.executable -I -B -m jev_trading_lab.persistence.migration_helper`, with `close_fds=True`, disabled stdin, fixed argv protocol, bounded timeout, and one combined bounded stdout/stderr budget. Every `BaseException` after `Popen` kills the helper process group, reaps the child, closes selector/pipes, and preserves the original exception.
- The helper independently loads packaged migrations, opens a fresh fixed-role migration connection, validates its opened descriptors/path around every `BEGIN IMMEDIATE`, serializes each migration, and closes. Parent independently verifies the exact packaged prefix/checksums. Complete authoritative DB readback is success regardless of a missing, malformed, or nonzero post-commit helper reply; incomplete/corrupt readback remains an error.
- `_DirectoryHandle` relinquishes numeric descriptor ownership before `os.close`; explicit close, constructor cleanup, and finalizer cleanup never retry an ambiguously closed FD and therefore cannot close a reused unrelated descriptor. If `os.close` raises before the kernel closes the FD, the handle may leak rather than risk unrelated-FD closure.
- Migration SQL files 001-003 remained byte-for-byte unchanged.

## TDD RED evidence

Each final defect reproduced before its production edit:

```text
writer swap-restore FD binding:
1 failed in 0.53s — append returned success instead of rejecting the decoy

successful malformed/missing helper reply after real commit:
3 failed in 0.64s — malformed output raised despite three authoritative rows

ambiguous DirectoryHandle close with forced FD reuse:
1 failed, 2 passed in 0.44s — ownership remained and risked reused-FD close

selector KeyboardInterrupt after Popen:
failed — helper PID remained until test cleanup

combined stdout/stderr budget:
4 failed, 2 passed in 2.50s — combined, interleaved, and trailing overflow passed
```

## Focused GREEN evidence

```text
PYTHONDONTWRITEBYTECODE=1 uv run pytest -q \
  tests/persistence/test_architectural_redesign.py \
  tests/persistence/test_append_only.py \
  tests/persistence/test_migrations.py \
  tests/test_package_policy.py
322 passed in 10.48s

uv run ruff check .
All checks passed!

uv run ruff format --check .
35 files already formatted

uv run mypy src
Success: no issues found in 17 source files

PYTHONDONTWRITEBYTECODE=1 python3 -I scripts/policy_preflight.py
Policy preflight passed.

git diff --check
exit 0
```

Replacement coverage assertions remain executable. The default and focused suites report zero skips.

## Full final gates

All commands exited 0. Default and mandatory built-artifact modes had zero skips.

```text
uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.56ms

PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
805 passed in 21.89s

uv run ruff check .
All checks passed!

uv run ruff format --check .
35 files already formatted

uv run mypy src
Success: no issues found in 17 source files

PYTHONDONTWRITEBYTECODE=1 python3 -I scripts/policy_preflight.py
Policy preflight passed.

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
59 passed in 2.45s

PYTHONDONTWRITEBYTECODE=1 uv run python scripts/wheel_migration_smoke.py
Installed-wheel migration and controlled journal write smoke passed.

git diff --check
exit 0

git diff --exit-code c36a1d8 -- \
  src/jev_trading_lab/persistence/schema/001_migration_metadata.sql \
  src/jev_trading_lab/persistence/schema/002_core_entities.sql \
  src/jev_trading_lab/persistence/schema/003_append_only_guards.sql
exit 0
```

The focused gate executed 322 tests, and the fresh mandatory artifact-policy gate executed all 59 package-policy cases; neither reported skips. Migrations 001-003 remain byte-for-byte unchanged from production commit `c36a1d8` and its parent.

`pip-audit` emitted only its standard `--no-deps` advisories. The transient audit requirements file was removed. Build artifacts remain ignored.

## Trust boundary

Supported guarantee: public factory connections remain permanently restrictive throughout journal append and migration operations. Privileged work occurs only on fresh private fixed-role connections, and migration privilege additionally runs in a fresh isolated interpreter. Public callbacks, signals, traces, profiles, and concurrent threads cannot borrow those roles through the supported API.

Deliberate base-class descriptor dispatch, object-internal mutation, frame inspection or private writer theft, importing private helpers, process-memory modification, raw separately opened SQLite connections, direct filesystem writes, and adversarial FD ABA outside the cooperative connection lock remain out of boundary. This is not an in-process sandbox or cryptographic writer authentication.

Passive CPython audit hooks remain supported. A malicious hook installed with
`sys.addaudithook` can deliberately capture a newly created SQLite handle before
Python installs its fixed authorizer; that capability-theft attack is explicitly
outside the controlled-API boundary and is not claimed to be sandboxed.

## Review state

**PENDING INDEPENDENT RE-REVIEW.** This agent did not independently approve the change.

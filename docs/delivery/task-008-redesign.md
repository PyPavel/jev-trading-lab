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

## Remaining descriptor/lifetime hardening

Production commit: `c668b60 fix(persistence): close remaining descriptor lifetime gaps`

The final review findings after `c36a1d8` were reproduced before production edits:

```text
helper EOF/lifetime reproduction:
FAILED — helper returned after 1.47s without timeout instead of the 0.1s deadline

private-parent traversal close/reuse reproduction:
FAILED — cleanup closed the sentinel FD reused after the real traversal close

foreign-thread SecureConnection.close reproduction:
FAILED — SQLite raised ProgrammingError, but factory provenance was already invalidated
```

Resolved behavior:

- Helper supervision now applies the original monotonic deadline to child lifetime after stdout/stderr EOF. `wait(timeout=remaining)` expiry kills the process group and reaps the direct child; the regression also verifies a forked descendant that closed both pipes is gone.
- `_open_private_parent` now gives every traversal descriptor explicit `_DirectoryHandle` ownership and transfers the final live descriptor through `release()`. Trace interruption after real close cannot make cleanup close a reused sentinel.
- Raw owned-FD transitions in `db.py`, `migrations.py`, and `migration_helper.py` were audited. Database-file preparation, migration directory traversal, migration resource reads, success/failure cleanup, and final transfer use idempotent ownership. An AST regression permits raw `os.close` only inside `_DirectoryHandle.close`.
- `SecureConnection.close` and the analogous internal connection close retained directory provenance only after SQLite close succeeds. A failed foreign-thread close leaves the connection queryable and factory provenance valid; owner-thread close later releases SQLite and the directory descriptor.
- Migration SQL 001-003 remained unchanged.

Focused and full verification:

```text
PYTHONDONTWRITEBYTECODE=1 uv run pytest -q \
  tests/persistence/test_architectural_redesign.py \
  tests/persistence/test_append_only.py \
  tests/persistence/test_migrations.py \
  tests/test_package_policy.py
332 passed in 11.56s

PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
815 passed in 24.82s
```

Final gates all exited 0:

```text
uv run ruff check .
All checks passed!

uv run ruff format --check .
35 files already formatted

uv run mypy src
Success: no issues found in 17 source files

PYTHONDONTWRITEBYTECODE=1 python3 -I scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.57ms

uv run pip-audit --strict --requirement audit-requirements.txt \
  --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

JEV_REQUIRE_BUILT_ARTIFACTS=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run pytest tests/test_package_policy.py -q
59 passed in 2.92s

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

The default, focused, and mandatory built-artifact suites reported zero skips. The transient audit requirements file was removed. Build artifacts remain ignored.

Exact production snapshot:

```text
c668b60d132a8be13f2915b2eef727552ebc06ba  production commit
addc3c88a66c52099db1a7ae9e9255e4cf8e9932a57ddc5248591b0fb711daea  src/jev_trading_lab/persistence/db.py
3dac817b7a2d52e35b40d79a50cd5880b2b4b7b123f6b7167bedb5b8bcfa8a98  src/jev_trading_lab/persistence/migrations.py
d3187180b1319fd6100c8c96d002acf0e5134a87eea87f5ae49b3aec37977263  src/jev_trading_lab/persistence/migration_helper.py
6fabb2c59f17e424211a9228e634ebbf4f2cec7f4f4440e35978892f5cfa890f  tests/persistence/test_architectural_redesign.py
10fe3175b2f0c739d67b8aa5b11d0cb681f347908da89e6d6ffc1bae1881870d  scripts/policy_preflight.py
69cdd7c9c88d031b62f7b6878ab2980336123115732b7cdd28fd529461368025  tests/test_package_policy.py
47ebea0cc43c2aa703dd215b7b116c6f2a74bc6cfb44d2b7920a6c3818aacd5e  .github/workflows/ci.yml
```

## Review state

**PENDING INDEPENDENT RE-REVIEW.** This agent did not independently approve the change.

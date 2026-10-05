# Task 008 Architectural Redesign Evidence

## Status

**PENDING INDEPENDENT RE-REVIEW**

Production commit: `049ae1b refactor(persistence): isolate privileged writers`

No push or independent approval occurred. This document records implementation and local evidence only.

## Delivered architecture

- Factory-created public `SecureConnection` installs the restrictive runtime authorizer once during initialization. Production contains no temporary journal/migration authorizer scope, restoration helper, or trace/profile preflight.
- Runtime policy denies journal DML, `schema_migrations` DML, schema/TEMP DDL, ATTACH/DETACH, and hardened dangerous configuration surfaces.
- `AppendOnlyJournal` retains the exact public factory connection only as provenance, path/configuration, and liveness. Each append opens a fresh fixed-role internal writer for exactly one selected journal table.
- Journal writer policy permits reads and direct main INSERT only into the selected table. It denies other inserts, UPDATE/DELETE, trigger-originated writes, schema/TEMP operations, and ATTACH/DETACH. Schema prefix/checksums, idempotency, conflicts, predecessors, exact literal insertion, and commit execute in one `BEGIN IMMEDIATE` transaction on that writer.
- `apply_migrations(connection)` validates the parent connection and invokes a packaged helper through `sys.executable -I -B -m jev_trading_lab.persistence.migration_helper`, with `close_fds=True`, disabled stdin, fixed argv protocol, bounded timeout, and bounded output.
- The helper independently loads packaged migrations, opens a fresh fixed-role migration connection, serializes each migration with `BEGIN IMMEDIATE`, and closes. Parent independently verifies the exact packaged prefix/checksums. Authoritative DB readback resolves lost/malformed post-commit replies.
- `_DirectoryHandle` atomically marks descriptor ownership closed before `os.close`; explicit close, constructor cleanup, and finalizer cleanup are idempotent and cannot double-close a reused FD.
- Migration SQL files 001-003 remained byte-for-byte unchanged.

## TDD RED evidence

Before production edits:

```text
PYTHONDONTWRITEBYTECODE=1 uv run pytest -q tests/persistence/test_architectural_redesign.py
12 failed in 0.55s
```

Failures showed the current architecture rejected active hooks instead of safely operating, exposed temporary authorizer helpers and `sys.gettrace/getprofile` guards, allowed non-selected table INSERT in the journal authorizer, lacked schema-prefix verification, lacked `_DirectoryHandle`, lacked the packaged helper, and lacked the helper in the built wheel.

## Focused GREEN evidence

```text
PYTHONDONTWRITEBYTECODE=1 uv run pytest -q \
  tests/persistence/test_architectural_redesign.py \
  tests/persistence/test_append_only.py \
  tests/persistence/test_migrations.py \
  tests/test_package_policy.py
303 passed, 26 skipped in 9.65s

uv run ruff check .
All checks passed!

uv run ruff format --check .
35 files already formatted

uv run mypy src
Success: no issues found in 17 source files

python3 -I scripts/policy_preflight.py
Policy preflight passed.

git diff --check
exit 0
```

The 26 skips are legacy transition/trace-rejection and direct public `schema_migrations` corruption tests whose premises were removed by the redesign. Replacement tests exercise permanent public denial while trace/profile/signal/all-thread hooks run, fixed-role truth tables, isolated helper protocol/output/readback, descriptor ownership, packaging, and existing append/migration behavior.

## Full final gates

All commands exited 0. Mandatory built-artifact mode had no skips.

```text
uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.58ms

PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
786 passed, 26 skipped in 21.08s

uv run ruff check .
All checks passed!

uv run ruff format --check .
35 files already formatted

uv run mypy src
Success: no issues found in 17 source files

PYTHONDONTWRITEBYTECODE=1 python3 -I scripts/policy_preflight.py
Policy preflight passed.

uv run pip-audit --strict --requirement audit-requirements.txt \
  --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

JEV_REQUIRE_BUILT_ARTIFACTS=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run pytest tests/test_package_policy.py -q
59 passed in 2.72s

PYTHONDONTWRITEBYTECODE=1 uv run python scripts/wheel_migration_smoke.py
Installed-wheel migration and controlled journal write smoke passed.

git diff --check
exit 0
```

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

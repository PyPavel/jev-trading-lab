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
- `apply_migrations(connection)` validates the parent connection and invokes a packaged helper through `sys.executable -I -B -m jev_trading_lab.persistence.migration_helper`, with `close_fds=True`, disabled stdin, fixed argv protocol, bounded timeout, and one combined bounded stdout/stderr budget. Once helper ownership and cleanup guards are established, exceptions during blocking supervision kill the same process group, reap the direct child, close selector/pipes, and preserve the original exception.
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

Supported guarantee: public factory connections remain permanently restrictive throughout journal append and migration operations. Privileged work occurs only on fresh private fixed-role connections, and migration privilege additionally runs in a fresh isolated interpreter.

Supported lifetime cases are passive trace/profile/audit hooks; public API callbacks using the public handle; cooperative cancellation at documented safe points after ownership is established; exceptions during blocking operations where cleanup guards already own resources; and the trusted fixed packaged helper's ordinary hangs, crashes, closed pipes, same-group descendants, and output overflow. A successful direct helper exit is followed by same-process-group descendant cleanup before return. Public callbacks and concurrent threads cannot borrow privileged roles through the supported API.

Unsupported cases include asynchronous Python signal/`KeyboardInterrupt`/`SystemExit` delivery during native resource acquisition or before Python ownership and cleanup guards are established; a malicious `sys.settrace`/profile callback intentionally raising at the exact bytecode between C resource acquisition and Python ownership assignment; frame inspection or private writer theft; private helper import; base descriptors or object-internal mutation; process memory modification; raw separately opened SQLite connections; direct filesystem writes; and adversarial FD ABA outside the cooperative connection lock. Malicious replacement or modification of the fixed packaged migration helper, including a `setsid` escape from its process group, is also excluded. This boundary promises neither acquisition nor bytecode atomicity and is not an in-process sandbox or cryptographic writer authentication.

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
- `_open_private_parent` now gives every traversal descriptor immediate `_DirectoryHandle` ownership and returns the final live handle itself. It no longer uses `release()` or returns an unowned numeric descriptor across the function boundary.
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

## Final actionable ownership/lifetime closure

Production commit: `2eebf34 fix(persistence): finalize ownership lifetimes`

TDD RED evidence captured before production edits:

```text
private-parent ownership boundary:
2 failed — `_open_private_parent` returned raw FD 12 and still used `release()`

child traversal transition:
1 failed — source retained an unowned `child_descriptor` between `os.open` and wrapper construction

connection cleanup helper:
1 failed — `_close_sqlite_with_handle` did not exist

successful direct helper exit with same-group descendant:
1 failed — descendant PID remained alive after helper return

precise source boundary policy:
1 failed — passive trace/profile/audit and bytecode-atomicity boundary text was absent

package policy after source edits:
1 failed — reviewed source hashes rejected changed `db.py` and `migrations.py`
```

Resolved behavior:

- `_open_private_parent` returns `_DirectoryHandle`; public and internal connection paths retain the object without `release()` or raw-FD rewrapping. Each traversed child is wrapped directly at `os.open` before validation/transition work.
- One `_close_sqlite_with_handle` helper gates directory/provenance cleanup on successful SQLite close and performs handle cleanup from `finally`. Foreign-thread failure preserves the live connection; deterministic cooperative cancellation at the owned close safe point leaves the handle relinquished.
- Helper launch is inside the outer cleanup region. Once helper ownership and cleanup guards are established, selector/wait `BaseException` kills/reaps the same group, and successful direct-child exit also cleans remaining same-group descendants before return. The existing deadline remains active after pipe EOF.
- The malicious exact-bytecode trace sabotage test was removed. Source and delivery policy state supported and excluded cases without promising acquisition/bytecode atomicity or containment of a modified helper using `setsid`.
- Migration SQL 001-003 remained unchanged.

Focused GREEN evidence:

```text
PYTHONDONTWRITEBYTECODE=1 uv run pytest -q \
  tests/persistence/test_architectural_redesign.py \
  tests/persistence/test_append_only.py \
  tests/persistence/test_migrations.py \
  tests/test_package_policy.py
339 passed in 11.75s

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

Full final gates all exited 0; default and required-artifact suites reported zero skips:

```text
uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.58ms

PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
822 passed in 25.83s

uv run ruff check .
All checks passed!

uv run ruff format --check .
35 files already formatted

uv run mypy src
Success: no issues found in 17 source files

PYTHONDONTWRITEBYTECODE=1 python3 -I scripts/policy_preflight.py
Policy preflight passed.

uv run pip-audit --strict --requirement /tmp/jev-task8-audit-requirements.txt \
  --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

JEV_REQUIRE_BUILT_ARTIFACTS=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run pytest tests/test_package_policy.py -q
59 passed in 2.97s

PYTHONDONTWRITEBYTECODE=1 uv run python scripts/wheel_migration_smoke.py
Installed-wheel migration and controlled journal write smoke passed.

git diff --check
exit 0

git diff --exit-code b5b0fa4 -- \
  src/jev_trading_lab/persistence/schema/001_migration_metadata.sql \
  src/jev_trading_lab/persistence/schema/002_core_entities.sql \
  src/jev_trading_lab/persistence/schema/003_append_only_guards.sql
exit 0
```

`pip-audit` emitted only its standard `--no-deps` advisories. The transient audit requirements file was removed. Build artifacts remain ignored.

Exact production snapshot:

```text
2eebf34  production commit
607b79fe6431dab082408026b9e9ec6017f7d432f5b6b197ad29aa8ac5753f38  src/jev_trading_lab/persistence/db.py
d6dbb2ddb602c2beae239656867fb414362578434185e3890850c76c74f251e1  src/jev_trading_lab/persistence/migrations.py
d3187180b1319fd6100c8c96d002acf0e5134a87eea87f5ae49b3aec37977263  src/jev_trading_lab/persistence/migration_helper.py
46ac00ad3f5f469377985c6e4294e15c9e9c953fecaaccc069c4babdd4406d91  tests/persistence/test_architectural_redesign.py
bb4a0d9bb9bb55ea5ec2efa151a5e4409eabc1e8b3676f39bd8891ad2dcdf6bc  scripts/policy_preflight.py
852d960ae7f2011fe600d21fa87d54834118f6393dc98dab5069b9150eef0988  tests/test_package_policy.py
49ed3c38d72fd46e5dcd1c9d35d3b7d1f996d7d18d21523d5448f7fb6f9ef9f9  .github/workflows/ci.yml
```

## Cooperative cancellation boundary correction

Production commit: `acf5782 fix(persistence): narrow cancellation boundary`

The final adversarial review established that pure Python cannot make ownership atomic between a native resource acquisition returning and the next Python bytecode. The public contract therefore supports cooperative cancellation only after ownership and cleanup guards are established. Asynchronous Python signal/`KeyboardInterrupt`/`SystemExit` delivery during native acquisition or before that ownership boundary is unsupported. Malicious exact-bytecode trace/profile sabotage, private capability theft, and malicious helper replacement or `setsid` escape remain unsupported.

The previous SIGALRM-based close test was replaced with a deterministic safe-point test that injects cooperative cancellation only when `_DirectoryHandle.close` already owns the descriptor and has relinquished its numeric ownership. The source-policy regression requires the acquisition exclusion and rejects the broad phrase `ordinary signal cancellation`. No flaky SIGALRM stress is a passing guarantee; the adversarial stress is evidence for the exclusion.

Focused verification after the correction:

```text
PYTHONDONTWRITEBYTECODE=1 uv run pytest -q \
  tests/persistence/test_architectural_redesign.py \
  tests/persistence/test_append_only.py \
  tests/persistence/test_migrations.py \
  tests/test_package_policy.py
339 passed in 11.71s
```

Mandatory final gates all exited 0; default and mandatory built-artifact suites reported zero skips:

```text
uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.58ms

PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
822 passed in 24.38s

uv run ruff check .
All checks passed!

uv run ruff format --check .
35 files already formatted

uv run mypy src
Success: no issues found in 17 source files

PYTHONDONTWRITEBYTECODE=1 python3 -I scripts/policy_preflight.py
Policy preflight passed.

uv run pip-audit --strict \
  --requirement /tmp/jev-task8-boundary-audit-requirements.txt \
  --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

JEV_REQUIRE_BUILT_ARTIFACTS=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run pytest tests/test_package_policy.py -q
59 passed in 2.41s

PYTHONDONTWRITEBYTECODE=1 uv run python scripts/wheel_migration_smoke.py
Installed-wheel migration and controlled journal write smoke passed.

git diff --check HEAD
exit 0

git diff --exit-code 35c8a5f -- \
  src/jev_trading_lab/persistence/schema/001_migration_metadata.sql \
  src/jev_trading_lab/persistence/schema/002_core_entities.sql \
  src/jev_trading_lab/persistence/schema/003_append_only_guards.sql
exit 0
```

The fresh audit requirements file was removed. Build artifacts remain ignored. Migration SQL 001-003 is unchanged.

Exact corrected production snapshot:

```text
acf5782  production commit
ac6765cb29cc6e21ea2d0c2aed1495295d038d466717165f3ce7f3f803080da9  src/jev_trading_lab/persistence/db.py
6848e5a2c83d91867649079acc32fbe0df6f6059e3d0e03c274f3d86c7d82255  src/jev_trading_lab/persistence/migrations.py
d3187180b1319fd6100c8c96d002acf0e5134a87eea87f5ae49b3aec37977263  src/jev_trading_lab/persistence/migration_helper.py
991fc1edfe0a4a69b913df27e05d5699e2c32ee0e6d73e0e56469108653b75cc  tests/persistence/test_architectural_redesign.py
0fcd66e67ae4c8f53e25da2d46816f08c9b071b07b9795a80e88dcf998763f14  scripts/policy_preflight.py
c376a55384a7aea29b61eb290f7eeef5015146c6afc00803f5751e346b15b0c5  tests/test_package_policy.py
6e69bdf14d494954a690e62f92b33ec6fc003c73c2f089c35b701f09f19e0a30  .github/workflows/ci.yml
```

## Review state

**PENDING INDEPENDENT RE-REVIEW.** No independent approval is claimed.

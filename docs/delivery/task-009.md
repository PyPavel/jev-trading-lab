# Task 009 Delivery Evidence

## Status

**PENDING INDEPENDENT RE-REVIEW**

Production commits `101dfd8` and `3c8cbdb` implement and harden crash-durable,
content-addressed artifact publication. This document records local TDD and gate
evidence only. No independent approval or push occurred.

## Review remediation

All Task 9 review findings received exact regression coverage before production
was edited. The regression suite used real directories, descriptors, fsyncs,
subprocess crashes, FIFO targets, filesystem identity changes, and scavenger
replacement races. Migrations `001`-`003` were unchanged.

### RED evidence

```text
PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/persistence/test_blobs.py -q
exit=1
50 failed, 44 passed in 4.33s
```

Expected failures covered missing ancestor directory fsync ordering, unsafe
path-based scavenging, incomplete normalized redaction and signed-query coverage,
JSON reserialization, unsafe invalid-JSON preservation, absent
`BlobConfigurationError`, tmpfs acceptance, root identity/mode drift, blocking
FIFO opens, late aggregate bounds, and ENOSPC/EIO cleanup. The FIFO subprocess
hit its three-second timeout, directly reproducing the blocking defect.

A second exact RED was added for escaped JSON secret markers:

```text
FAILED test_invalid_declared_json_with_escaped_secret_marker_fails_closed
E Failed: DID NOT RAISE ValueError
1 failed in 0.53s
```

### GREEN evidence

```text
TMPDIR=/home/pavel/.pytest-local PYTHONDONTWRITEBYTECODE=1 \
  uv run pytest tests/persistence/test_blobs.py -q
95 passed in 2.66s

TMPDIR=/mnt/storage/.jev-task9-pytest PYTHONDONTWRITEBYTECODE=1 \
  uv run pytest -q -rs
918 passed in 175.56s (0:02:55)
```

The complete zero-skip run used btrfs because tmpfs is intentionally rejected and
the available ext filesystem can immediately reuse an inode in a pre-existing
migration replacement regression. No migration production code or tests changed.

### Final gates

```text
uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

uv run ruff check .
All checks passed!

uv run ruff format --check .
37 files already formatted

uv run mypy src
Success: no issues found in 18 source files

PYTHONDONTWRITEBYTECODE=1 python3 -I scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 64 packages

uv sync --frozen --all-groups
Checked 63 packages

uv run pip-audit --strict --requirement /home/pavel/audit-requirements-task9.txt \
  --no-deps --disable-pip
No known vulnerabilities found

JEV_REQUIRE_BUILT_ARTIFACTS=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run pytest tests/test_package_policy.py -q
60 passed in 2.41s

PYTHONDONTWRITEBYTECODE=1 uv run python scripts/wheel_migration_smoke.py
Installed-wheel migration and controlled journal write smoke passed.

git diff --check
exit 0
```

Package inventory, approved source hashes, preflight self-hash, and exact workflow
hash were refreshed. No dependency or migration SQL changed.

### Supported boundary and exclusions

Observable root path replacement, symlink substitution, mode drift, filesystem
type, owner/type/mode violations, shard races, corrupt/special final targets,
and durability failures fail closed. Same-user raw mutation through an already
held private capability is outside the trust boundary. Deliberate same-user
sabotage of private descriptors/capabilities is likewise excluded. A malicious
test failpoint that deletes an already published final is not a supported crash
model; failpoints model process interruption and I/O failures, not an attacker
executing arbitrary mutation callbacks. No claim is made beyond those bounds.

## Commit

```text
101dfd8 feat(persistence): add crash-durable artifact store
3c8cbdb fix(persistence): harden crash-durable blob storage
```

Delivery evidence is committed separately from production.

## Delivered behavior

- `BlobRef` is immutable and contains SHA-256, byte length, and deterministic `aa/bb/<64-lowercase-hex>` relative path metadata suitable for later referenced-hash enumeration.
- `BlobStore` rejects unsupported filesystems, symlinked root ancestors, non-private root/shards, malformed refs, oversize input, and non-exact byte payloads.
- Publication creates a unique mode-0600 temp file in the final shard with `O_CREAT|O_EXCL|O_NOFOLLOW`, completes short/EINTR writes, fsyncs, descriptor-verifies hash/size/type/owner/mode/link count, publishes without replacement, fsyncs the parent shard, and descriptor-verifies the final blob before returning.
- Same-content concurrent publication converges. Existing targets are accepted only after verification; corrupt targets are never overwritten.
- `publish_and_reference` invokes its callback only after durable final verification and passes only `BlobRef`. Callback failure leaves an acceptable verified orphan.
- Failpoints cover after temp create/write/file fsync, before/after rename, before/after directory fsync, and before reference. `BaseException` cleanup removes owned temps; fresh-store reopen verifies any published orphan.
- Recovery scavenging removes only stale, owned, mode-0600, link-count-one regular files matching the private `.tmp-<32-lowercase-hex>` namespace; valid blobs and symlinks remain untouched.
- `read_verified` uses descriptor-bound no-follow opens, bounded streaming verification, and stable `BlobMissingError` / `BlobCorruptError` failures.
- `ArtifactEnvelope` defines the only structured redaction surface. URL userinfo/fragments, auth/signed query values, common credential headers, and structurally named JSON secret fields are redacted before hashing. Opaque body bytes and non-secret JSON content remain exact.
- Source, package inventory, CI workflow, and policy hashes approve and package `blobs.py`. No dependency or migration SQL changed.

## Required TDD RED evidence

Each frozen-plan test was executed individually before `blobs.py` existed. Failures were due to the missing production module, not syntax errors.

### Publication order

```text
PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_blobs.py::test_publication_order -q

FAILED tests/persistence/test_blobs.py::test_publication_order
E ImportError: cannot import name 'blobs' from 'jev_trading_lab.persistence'
1 failed in 0.39s
```

### Failpoint safety

```text
PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_blobs.py::test_failpoints_leave_no_bad_reference -q

FAILED ... [after_temp_create]
FAILED ... [after_temp_write]
FAILED ... [after_file_fsync]
FAILED ... [before_publish]
FAILED ... [after_publish]
FAILED ... [before_dir_fsync]
FAILED ... [after_dir_fsync]
FAILED ... [before_reference]
E ImportError: cannot import name 'blobs' from 'jev_trading_lab.persistence'
8 failed in 0.52s
```

The implemented failpoint names use the more precise `before_rename` and `after_rename`; semantic boundaries are unchanged.

### Corrupt/missing detection

```text
PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_blobs.py::test_corrupt_missing_detected -q

FAILED tests/persistence/test_blobs.py::test_corrupt_missing_detected
E ImportError: cannot import name 'blobs' from 'jev_trading_lab.persistence'
1 failed in 0.44s
```

### Additional RED cycles

```text
PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_blobs.py::test_rejects_nonlocal_filesystem_before_creating_root -q

FAILED ...
E TypeError: BlobStore.__init__() got an unexpected keyword argument 'filesystem_resolver'
1 failed in 0.45s

PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_blobs.py::test_rejects_symlinked_root_ancestor -q

FAILED ...
E Failed: DID NOT RAISE BlobSecurityError
1 failed in 0.49s

PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/persistence/test_blobs.py::test_read_rejects_malformed_ref_symlink_hardlink_and_weak_mode -q

FAILED ...
E Failed: DID NOT RAISE BlobCorruptError
1 failed in 0.45s
```

These follow-up cycles added local-filesystem rejection, symlink-ancestor rejection, and first-shard permission verification.

## Focused GREEN evidence

```text
PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/persistence/test_blobs.py -q
30 passed in 0.61s
```

Coverage includes publication ordering, fresh-store failpoint recovery, ENOSPC/EIO after short write, EINTR/short-write completion, corrupt/missing targets, dedup race, callback failure/orphan semantics, structured redaction, opaque-body preservation, path/mode/symlink/hardlink rejection, stale-temp scavenging, filesystem boundaries, and threaded convergence.

## Full final gates

All commands exited 0. The final default suite had zero skips after rebuilding current artifacts; the prior single skip was the intentional stale-artifact guard for the new `blobs.py` package-inclusion test and disappeared after `uv build`.

```text
uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

PYTHONDONTWRITEBYTECODE=1 python3 -I scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.62ms

PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/persistence/test_blobs.py -q
30 passed in 0.61s

PYTHONDONTWRITEBYTECODE=1 uv run pytest -q -rs
853 passed in 25.61s

uv run ruff check .
All checks passed!

uv run ruff format --check .
37 files already formatted

uv run mypy src
Success: no issues found in 18 source files

JEV_REQUIRE_BUILT_ARTIFACTS=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run pytest tests/test_package_policy.py -q
60 passed in 2.98s

PYTHONDONTWRITEBYTECODE=1 uv run python scripts/wheel_migration_smoke.py
Installed-wheel migration and controlled journal write smoke passed.

git diff --check
exit 0
```

Fresh dependency audit:

```text
uv export --frozen --all-groups --no-emit-project \
  --format requirements-txt --output-file audit-requirements.txt
exit 0

uv run pip-audit --strict --requirement audit-requirements.txt \
  --no-deps --disable-pip
No known vulnerabilities found
```

`pip-audit` emitted only its standard `--no-deps` advisories. The transient export was removed.

## Exact production snapshot

```text
101dfd8 production commit

e681215701c1064f3bf95d9bfb782898ea3a48867e4c346bf28aace8bbe853e4  src/jev_trading_lab/persistence/blobs.py
defe6606502a0d9eef2d4e2f7dc680bd0d6f619d8647a1990c75a01a43620468  tests/persistence/test_blobs.py
acae422c7b210c766f2917e4fb8b6a23fb9ea1239483d6a53a50c2efd0fda658  scripts/policy_preflight.py
cde27b29bbafc17de8aacd3949ca7c5c125da6bbdb26314a49f5d3ad49aeddae  tests/test_package_policy.py
37067edb3ae2bb4adf1e5b89f61fe7fd9e4ea005ee38b81550e4f17c3a405eb8  .github/workflows/ci.yml
```

Migration SQL remained byte-for-byte unchanged:

```text
1be6dbbcb20a6ab8efe016172d786280470fc7a450f403825e2a61e199a1cb48  src/jev_trading_lab/persistence/schema/001_migration_metadata.sql
9026b984313d972dec6c3db8b442e4324f2a7f02ec71bd555c0c17f8ed362c54  src/jev_trading_lab/persistence/schema/002_core_entities.sql
d18492f31e6c0948fa87f11927f2bc4fb2a5a41ffb45c27c9e966339d5e35995  src/jev_trading_lab/persistence/schema/003_append_only_guards.sql
```

Dependencies, `pyproject.toml`, and `uv.lock` were unchanged.

## Trust boundary

Supported guarantee: one-host local-filesystem durability through the provided API, assuming OS/filesystem fsync and atomic rename/link semantics. The store rejects non-allowlisted filesystem types and common path/link attacks. It prevents bad references through `publish_and_reference`; it does not control callbacks that independently write a DB reference before invocation or malicious same-process code with raw filesystem/process-memory access.

Publication after rename but before directory fsync may leave either no directory entry or a valid orphan after real power loss; it cannot leave a callback-created dangling reference because reference invocation occurs only after directory fsync and final descriptor verification. Test failpoints model boundary exceptions and fresh-store reopen, not fabricated power-loss guarantees.

## Task 8 replacement-race regression remediation

The intermittent migration regression was inode reuse, not monkeypatch ordering or
test contamination. A minimal diagnostic showed unlink plus create on
`/home/pavel/.pytest-local` preserving `(st_dev, st_ino)` while `st_ctime_ns`
changed; `/tmp` allocated a different inode. No descriptor pinned the validated
inode at the SQLite-open boundary, allowing immediate inode-number recycling to
defeat the two-field identity.

Commit `1c44e04` retains the validated database descriptor until SQLite's main
descriptor is identified and verified. The deterministic regression asserts the
validated inode is descriptor-pinned before replacement and that replacement gets
a distinct identity. Public and internal writers continue using the same descriptor
census and descriptor/path relationship checks.

Fresh verification after the fix:

```text
replacement regression, TMPDIR=/home/pavel/.pytest-local: 100/100 passed
replacement regression, default TMPDIR: 100/100 passed
Task 8 focused: 195 passed in 6.91s
Task 9 focused: 95 passed in 3.16s
full suite, zero skips: 918 passed in 175.79s (0:02:55)
package policy: 60 passed in 2.45s
pip-audit: No known vulnerabilities found
wheel smoke: Installed-wheel migration and controlled journal write smoke passed.
```

Build, policy, lock, frozen-sync, Ruff, mypy, package, wheel, and audit gates passed.
The audit input was freshly exported after code stabilization and then removed.
Migrations `001`-`003` remain byte-for-byte unchanged.

## Review state

**PENDING INDEPENDENT RE-REVIEW.**


## Task 9 re-review remediation at `171944d`

Status remains **PENDING INDEPENDENT RE-REVIEW**. Production commit `171944d`
closes the seven remaining re-review findings without changing Task 8 migration SQL.

### RED and root-cause evidence

The new focused regressions initially failed 25 cases: malformed declared JSON failed
open, OAuth query/header credentials remained stored, lone-surrogate exceptions exposed
original input, nested ancestor fsyncs were absent, and the 1 MiB JSON case peaked at
45,143,424 bytes (>10x). The exact malformed inputs from review reproduced directly.

Bytecode tracing isolated unguarded child interpreter launches in blob crash/FIFO,
migration-helper adversarial, recursive-resolver, artifact-policy, and wheel-smoke test
paths. `conftest.py` only sets `sys.dont_write_bytecode` in the pytest parent; that
runtime flag does not propagate to child interpreters. Running each affected test file
without the CI environment reproduced source-package `.pyc` creation under
`src/jev_trading_lab`. Every Python child now uses `-B`; CI still exports
`PYTHONDONTWRITEBYTECODE=1` as a second independent control. The package-policy
regression removes that environment variable for its child and proves `-B` alone
leaves `src` free of `__pycache__`.

### Delivered remediation

- Any malformed declared JSON now fails closed with stable generic `ValueError` text.
  Invalid UTF-8, invalid escapes, truncated nesting, and the two exact escaped-key
  attacks cannot reach stored bytes or exception text/repr.
- OAuth query fields (`client_secret`, `access_token`, `refresh_token`, `id_token`,
  `code`) and normalized credential header variants redact before hashing. Generic
  body field `code` remains exact because only structural query context treats it as
  a credential.
- Root ancestry is created descriptor-relatively from the nearest trusted existing
  directory. Each parent is fsynced immediately after child creation; observed
  existing entries are also fsynced before descent to cover concurrent `EEXIST`.
- `BlobStore` retains its validated root descriptor for its lifetime. Every operation
  validates pinned owner/type/mode, compares the visible path to the pinned descriptor,
  and duplicates only that descriptor. Idempotent `close`, context management, and
  finalization release it; 200 replacement attempts never publish to replacements.
- URL/header/body Unicode and parser failures become generic errors without reflected
  input.
- JSON lexical redaction now emits in one forward pass with bounded key decoding and
  no offsets/spans lists. The 1 MiB check stays below 10x; the 64 MiB default-limit
  check stays below 4x.
- Python subprocess invocations use `-B`; workflow and exact policy hashes were
  refreshed. `.pytest-tmp` contains test-generated caches without polluting package
  source, and is ignored as generated state.

### Fresh gates

```text
focused blobs: 127 passed in 97.01s
Task 8 local/storage: 195 passed in 6.61s
package policy after build: 61 passed in 3.14s
required built-artifact policy: 61 passed in 3.15s

exact CI command, clean generated state, run 1:
PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
951 passed in 126.72s (0:02:06); source __pycache__ = []

exact CI command, clean generated state, run 2:
PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
951 passed in 122.07s (0:02:02); source __pycache__ = []

policy preflight: Policy preflight passed.
uv lock --check: Resolved 64 packages
uv sync --frozen --all-groups: Checked 63 packages
ruff check: All checks passed!
ruff format --check: 37 files already formatted
mypy: Success: no issues found in 18 source files
build: wheel and sdist built successfully
pip-audit: No known vulnerabilities found
wheel smoke: Installed-wheel migration and controlled journal write smoke passed.
git diff --check: exit 0
```

Migration files `001`-`003` remain byte-for-byte unchanged. No dependency or lockfile
change occurred. Evidence is committed separately from production; no push or
independent approval occurred.


## Final shard/ancestry/durability/OAuth remediation at `8d250a7`

Status remains **PENDING INDEPENDENT RE-REVIEW**. Production commit `8d250a7`
closes the final four review findings without changing migrations `001`-`003`.

### Exact RED evidence

Before production edits, focused regressions failed exactly on the reported gaps:

```text
raced second-shard replacement/mode drift: 2 failed
nested existing and higher lexical ancestor symlinks: 2 failed
EEXIST parent fsync-before-descent: 2 failed
OAuth/OIDC structured query/header variants: 12 failed, 7 passed
```

The callback ran after a visible second-shard replacement or mode drift; root creation
opened a resolved multi-component prefix; existing shard creation skipped the parent
fsync; and `oauth_token`, `oauth_token_secret`, `Id-Token`, `OAuth-Token`, and
`X-Client-Secret` variants reached stored bytes.

### Delivered remediation

- Publication retains the exact first- and second-shard descriptor identities. Just
  before callback invocation, it reopens the canonical root/`aa`/`bb` chain with
  descriptor-relative `O_DIRECTORY|O_NOFOLLOW`, validates owner/type/mode, compares
  both shard identities, verifies the final blob through the canonical `bb`
  descriptor, and revalidates both shard modes. Replacement, disappearance, and mode
  drift fail closed before the callback.
- Every absolute lexical root component is traversed from a pinned `/` descriptor.
  Existing components are opened one at a time without following symlinks and checked
  for safe owner/type/mode; missing components are created one at a time. Filesystem
  root and `..` root paths are rejected.
- `_ensure_child_directory` fsyncs the parent after every successful child open and
  validation, including the `EEXIST` observer path, before returning for descent.
  Root ancestors and both shard levels use the same rule.
- Structured credential names use NFKC/camelCase/separator normalization plus bounded
  OAuth/OIDC credential suffix recognition. Full-width, prefixed, camelCase,
  hyphenated, and underscored query/header variants redact before hashing. Exact safe
  names such as `token_count`, `secretary`, `password_policy`, and `code_version`
  remain unchanged. Stored-byte and exception/repr assertions remain in coverage.
- A real 12-process dedup regression proves one verified blob survives concurrent
  same-content publication.
- Policy source/preflight/workflow hashes were refreshed after stabilization. No
  dependency or lockfile change occurred.

### Fresh verification

```text
focused blob suite: 154 passed in 95.94s
Task 8 local/storage plus blobs: 513 passed in 115.93s

exact CI command after mandatory artifacts, run 1:
PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
978 passed in 120.55s (0:02:00); zero skips; source bytecode = []

exact CI command after mandatory artifacts, run 2:
PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
978 passed in 121.20s (0:02:01); zero skips; source bytecode = []

mandatory built-artifact full suite:
JEV_REQUIRE_BUILT_ARTIFACTS=1 PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
978 passed in 121.12s (0:02:01); zero skips

policy preflight: Policy preflight passed.
uv lock --check: Resolved 64 packages
uv sync --frozen --all-groups: Checked 63 packages
ruff check: All checks passed!
ruff format --check: 37 files already formatted
mypy: Success: no issues found in 18 source files
build: wheel and sdist built successfully
required built-artifact policy: 61 passed
pip-audit: No known vulnerabilities found
wheel smoke: Installed-wheel migration and controlled journal write smoke passed.
git diff --check: exit 0
source/package bytecode outside generated test state: none
```

The zero-skip post-build full-suite result is recorded from the mandatory artifact
mode. Migrations `001`-`003` remain byte-for-byte unchanged at hashes
`1be6dbbcb20a6ab8efe016172d786280470fc7a450f403825e2a61e199a1cb48`,
`9026b984313d972dec6c3db8b442e4324f2a7f02ec71bd555c0c17f8ed362c54`, and
`d18492f31e6c0948fa87f11927f2bc4fb2a5a41ffb45c27c9e966339d5e35995`.
No push or independent approval occurred.

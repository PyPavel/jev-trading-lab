# Task 009 Delivery Evidence

## Status

**PENDING INDEPENDENT RE-REVIEW**

Production commit `101dfd8` implements crash-durable, content-addressed artifact publication. This document records local TDD and gate evidence only. No independent approval or push occurred.

## Commit

```text
101dfd8 feat(persistence): add crash-durable artifact store
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

## Review state

**PENDING INDEPENDENT RE-REVIEW.**

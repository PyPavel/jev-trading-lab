# Task 003 Delivery Evidence

## Status

**PENDING INDEPENDENT REVIEWS**

Task 3 production implementation and local verification are complete. Independent spec/adversarial and quality reviews remain required before Task 4 may begin.

## Scope

Added deterministic canonical JSON SHA-256 identities, strict UTC integer-microsecond conversion, and immutable common provider observation metadata. No runtime dependency, lock file, design, plan, or review document changed.

Production commit:

```text
24f9a5a27a05d48a58c6d4ed5950e4fb0acdee4e feat(core): add identities time and observation metadata
```

## RED evidence

Mandatory behaviors were written before production modules and run individually. Each failed for the intended missing-feature reason:

```text
uv run pytest tests/core/test_identity.py::test_canonical_hash_ignores_mapping_order -q
1 failed: AssertionError: jev_trading_lab.core.ids module must exist

uv run pytest tests/core/test_identity.py::test_naive_datetime_rejected -q
1 failed: AssertionError: jev_trading_lab.core.time module must exist

uv run pytest tests/core/test_observations.py::test_first_retrieval_is_immutable -q
1 failed: AssertionError: jev_trading_lab.core.observations module must exist

uv run pytest tests/core/test_observations.py::test_corrections_append -q
1 failed: AssertionError: jev_trading_lab.core.observations module must exist
```

Additional canonical-ID, UTC boundary, metadata validation, correction, and stable observation-ID tests were also exercised against absent modules before implementation.

After production files were added but before exact source-policy approval, both controls failed closed:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
exit 1
policy preflight: src/jev_trading_lab/core/ids.py: file is not approved
policy preflight: src/jev_trading_lab/core/observations.py: file is not approved
policy preflight: src/jev_trading_lab/core/time.py: file is not approved

uv run pytest tests/test_package_policy.py::test_no_live_execution_surface -q
1 failed
src/jev_trading_lab/core/ids.py: production file is not approved
src/jev_trading_lab/core/observations.py: production file is not approved
src/jev_trading_lab/core/time.py: production file is not approved
```

## GREEN implementation

Implemented:

- Canonical UTF-8 JSON with sorted mapping keys and preserved list order.
- Explicit canonical domain: null, booleans, exact built-in integers, strings, lists, and string-keyed mappings.
- Rejection of floats, `Decimal`, bytes, datetime objects, sets, non-string keys, and unsupported objects rather than string coercion.
- Namespace-separated SHA-256 IDs using a canonical envelope, avoiding delimiter ambiguity.
- Exact integer UTC microsecond conversion with no float timestamps.
- Rejection of naive datetimes, non-zero UTC offsets, booleans, and custom integer subclasses.
- Supported range defined as UTC-aware `datetime.min` through `datetime.max`, including pre-epoch values; signed 64-bit checks remain explicit.
- Frozen `ObservationMetadata` with exact status enum, minimally validated absolute source URI, valid timestamps, lowercase SHA-256 content hash, optional supersession, and stable canonical observation ID.
- Publication time may be absent or precede retrieval; publication after retrieval is rejected with no clock-skew allowance.
- `correct_observation()` appends a new immutable object, requires the original observation ID, a different content hash, and a later retrieval time.
- Exact source hashes, policy-preflight hash, workflow-embedded preflight hash, workflow hash, approved fixture tree, and wheel contents updated without weakening dependency, lock, workflow-order, or operational policy.

Focused verification:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py && uv run pytest tests/core/test_identity.py tests/core/test_observations.py tests/test_package_policy.py -q
Policy preflight passed.
76 passed in 1.26s
```

## Full gate evidence

Commands ran sequentially against the final production tree and exited 0:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 51 packages in 1ms

uv sync --frozen --all-groups
Checked 50 packages in 0.63ms

uv run pytest -q
101 passed in 1.18s

uv run ruff check .
All checks passed!

uv run ruff format --check .
14 files already formatted

uv run mypy src
Success: no issues found in 6 source files

uv export --frozen --all-groups --no-emit-project --format requirements-txt --output-file /tmp/jev-task-003-audit-requirements.txt
exit 0

uv run pip-audit --strict --requirement /tmp/jev-task-003-audit-requirements.txt --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

git diff --check
exit 0
```

## Self-review

- Every Task 3 behavior has direct tests, including hostile unsupported values, mapping/list semantics, fresh-process stability, namespace separation, strict UTC boundaries, pre-epoch behavior, immutable metadata, correction rules, enum exactness, and observation-ID field coverage.
- Canonical serialization remains intentionally narrow; it is not a generic serializer and performs no implicit normalization.
- URI validation is deliberately minimal. SSRF, DNS, address, redirect, and transport security remain owned by later evidence tasks.
- Correction validation covers one direct predecessor only. Repository-backed chain acyclicity and branch prevention remain persistence responsibilities in later tasks.
- No dependency or lock file changed. No approved design, plan, or review file changed.
- Production/tests/policy changes and delivery evidence are isolated in separate commits.

## Review state

**PENDING INDEPENDENT REVIEWS.** Task 4 remains blocked until independent reviews report zero blocking findings and this delivery record is updated through the established review cycle.

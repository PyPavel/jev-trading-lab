# Task 002 Delivery Evidence

## Status

**PENDING INDEPENDENT RE-REVIEW**

This document records implementation and self-review evidence only. It does not claim independent approval.

## Scope

Added deterministic fixed-point conversions for non-negative money and price values, bounded probabilities, and positive quantities using a caller-declared venue scale. Trust-boundary inputs accept only `Decimal` or decimal strings, reject booleans, floats, non-finite values, and invalid domain ranges, and use the documented `ROUND_HALF_EVEN` policy.

## RED evidence

The mandatory behavior tests were created before production code and run individually. After making missing-module detection an assertion, each failed for the intended missing-feature reason rather than collection failure:

```text
uv run pytest tests/core/test_values.py::test_money_roundtrip -q
1 failed: AssertionError: fixed-point values module must exist

uv run pytest tests/core/test_values.py::test_probability_bounds -q
2 failed: AssertionError: fixed-point values module must exist

uv run pytest tests/core/test_values.py::test_rounding_policy -q
1 failed: AssertionError: fixed-point values module must exist
```

After `values.py` was created but before source-policy approval was updated, the required source-policy control failed:

```text
uv run pytest tests/test_package_policy.py::test_no_live_execution_surface -q
1 failed
src/jev_trading_lab/core: production directory is not approved
src/jev_trading_lab/core/values.py: production file is not approved

/usr/bin/python3 -I -S scripts/policy_preflight.py
exit 1
policy preflight: src/jev_trading_lab/core: directory is not approved
policy preflight: src/jev_trading_lab/core/values.py: file is not approved
```

## GREEN implementation

Production commit:

```text
30e9ebddf429663f057e883edf14de49342a7c3e feat(core): add fixed-point economic values
```

Implemented:

- `MICRO = 1_000_000` and one documented `ROUND_HALF_EVEN` policy.
- Distinct runtime integer tags: `MicroDollars`, `ProbabilityMicros`, and `PriceMicros`.
- Frozen `ScaledQuantity` carrying integer units plus the caller-declared scale.
- Exact decimal parsing and integer-ratio rounding, independent of ambient decimal context precision.
- Exact micro-dollar round-trip conversion.
- Probability `[0, 1]` validation and non-negative money/price validation.
- Positive quantity and positive integer scale validation.
- Exact source-tree/hash approval for the new directory and file, with corresponding preflight hash and workflow-embedded hash updates. Dependency, lock, entry-point, tool, and workflow ordering contracts were not weakened.

Focused verification:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py && uv run pytest tests/core/test_values.py tests/test_package_policy.py -q
Policy preflight passed.
63 passed in 1.07s
```

## Full gate evidence

Commands ran sequentially after the final production commit and all exited 0:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 51 packages in 1ms

uv sync --frozen --all-groups
Checked 50 packages in 0.53ms

uv run pytest -q
63 passed in 1.35s

uv run ruff check .
All checks passed!

uv run ruff format --check .
9 files already formatted

uv run mypy src
Success: no issues found in 3 source files

uv export --frozen --all-groups --no-emit-project --format requirements-txt --output-file /tmp/jev-task-002-audit-requirements.txt
exit 0

uv run pip-audit --strict --requirement /tmp/jev-task-002-audit-requirements.txt --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

git diff --check
exit 0
```

## Self-review

- Production commit contains only the fixed-point implementation, focused tests, and strictly required reviewed source-policy/hash updates.
- No dependency or lock file changed.
- No approved design, plan, or review file changed.
- No speculative currency, venue, arithmetic, or execution abstractions were added.
- Delivery evidence is isolated in this separate documentation commit.
- Independent controller reviews remain required before approval.

## Review remediation

Status: **PENDING INDEPENDENT RE-REVIEW**. Task 3 remains blocked.

Review findings fixed in production commit:

```text
6ea6629 fix(core): enforce fixed-point invariants
```

- `decimal_dollars()` now constructs its exact decimal tuple directly, so ambient `Decimal` precision cannot round the result.
- Public tagged constructors enforce their domain ranges and reject booleans. `ScaledQuantity.__post_init__` enforces positive SQLite-safe units and scale.
- Persisted values use the documented inclusive `MIN_INT64 = 0` through `MAX_INT64 = 2**63 - 1` policy.
- Decimal trust-boundary inputs are limited to 128 characters/digits and absolute exponent 128 before integer scaling.
- Factory scaling rejects results above `MAX_INT64`; no arithmetic framework was added.

### Remediation RED evidence

Each mandatory regression test was run individually before production changes and failed for its intended assertion:

```text
uv run pytest tests/core/test_values.py::test_money_roundtrip_is_exact_under_low_decimal_precision -q
1 failed
AssertionError: assert Decimal('9E+12') == Decimal('9223372036854.775807')

uv run pytest tests/core/test_values.py::test_tagged_constructors_enforce_domain_invariants -q
1 failed
Failed: DID NOT RAISE any of (TypeError, ValueError)

uv run pytest tests/core/test_values.py::test_sqlite_integer_limit_is_accepted_and_overflow_rejected -q
1 failed
AttributeError: module 'jev_trading_lab.core.values' has no attribute 'MAX_INT64'

uv run pytest tests/core/test_values.py::test_decimal_input_size_and_exponent_are_bounded -q
1 failed
Failed: DID NOT RAISE ValueError
```

After production behavior changed but before reviewed source hashes were updated, both source-policy controls failed closed:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
exit 1
policy preflight: src/jev_trading_lab/core/values.py: SHA-256 does not match approved content

uv run pytest tests/test_package_policy.py::test_no_live_execution_surface -q
1 failed
src/jev_trading_lab/core/values.py: SHA-256 does not match approved source
```

### Remediation gate evidence

Commands ran sequentially against production commit `6ea6629` and exited 0:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 51 packages in 1ms

uv sync --frozen --all-groups
Checked 50 packages in 0.52ms

uv run pytest -q
67 passed in 1.16s

uv run ruff format --check .
9 files already formatted

uv run ruff check .
All checks passed!

uv run mypy src
Success: no issues found in 3 source files

uv export --frozen --all-groups --no-emit-project --format requirements-txt --output-file /tmp/jev-task-002-review-audit-requirements.txt
exit 0

uv run pip-audit --strict --requirement /tmp/jev-task-002-review-audit-requirements.txt --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

git diff --check
exit 0
```

No dependency or lock file changed. No approved design, plan, or review file changed. Exact source/preflight/workflow contracts remain fail-closed. Task 002 remains pending independent re-review; Task 3 must not start.

## Exact-integer review remediation

Status: **PENDING INDEPENDENT RE-REVIEW**. Task 3 remains blocked.

Quality review found that `_validate_integer()` accepted `int` subclasses. Tagged integer values and hostile subclasses could therefore cross public constructor boundaries, and overridden comparisons could bypass range validation. A reproduced `MicroDollars` bypass persisted `-1` to SQLite.

Production commit:

```text
c304e83facb7d62cbc5f619978bed1acf6c8ddd0 fix(core): reject tagged and subclass integer inputs
```

Implemented:

- Public persisted-integer constructors and `scaled_quantity(..., scale=...)` require exact built-in integers before comparisons.
- `MicroDollars`, `ProbabilityMicros`, and `PriceMicros` reject cross-tag inputs and all custom `int` subclasses.
- `ScaledQuantity` rejects custom and tagged `units` or `scale` values.
- `decimal_dollars()` retains its exact `MicroDollars` boundary and converts the validated tag to a plain integer before range validation.
- Internal factory scaling still yields plain built-in integers. No arithmetic framework was added.
- Exact source, preflight, and workflow hashes were updated under the fail-closed policy.

### Exact-integer RED evidence

Each regression was run individually before the production change and failed for the intended bypass:

```text
uv run pytest tests/core/test_values.py::test_tagged_constructors_reject_hostile_int_subclass -q
1 failed
Failed: DID NOT RAISE TypeError

uv run pytest tests/core/test_values.py::test_tagged_constructors_reject_cross_tag_inputs -q
6 failed
Failed: DID NOT RAISE TypeError

uv run pytest tests/core/test_values.py::test_scaled_quantity_rejects_custom_and_tagged_integer_inputs -q
1 failed
Failed: DID NOT RAISE TypeError

uv run pytest tests/core/test_values.py::test_hostile_int_subclass_cannot_persist_negative_micro_dollars -q
1 failed
AssertionError: constructor allowed a negative value to reach SQLite
assert -1 >= 0
```

After production behavior changed but before reviewed source hashes were updated, both source-policy controls failed closed:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
exit 1
policy preflight: src/jev_trading_lab/core/values.py: SHA-256 does not match approved content

uv run pytest tests/test_package_policy.py::test_no_live_execution_surface -q
1 failed
src/jev_trading_lab/core/values.py: SHA-256 does not match approved source
```

### Exact-integer gate evidence

Commands ran sequentially before production commit `c304e83` and exited 0:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 51 packages in 1ms

uv sync --frozen --all-groups
Checked 50 packages in 0.53ms

uv run pytest -q
76 passed in 1.17s

uv run ruff check .
All checks passed!

uv run ruff format --check .
9 files already formatted

uv run mypy src
Success: no issues found in 3 source files

uv export --frozen --all-groups --no-emit-project --format requirements-txt --output-file /tmp/jev-task-002-exact-int-audit-requirements.txt
exit 0

uv run pip-audit --strict --requirement /tmp/jev-task-002-exact-int-audit-requirements.txt --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

git diff --check
exit 0
```

No dependency or lock file changed. No approved design, plan, or review file changed. Task 002 remains pending independent re-review; Task 3 must not start.

# Task 005 Delivery Evidence

## Status

**IMPLEMENTED / PENDING INDEPENDENT REVIEWS**

## Code commit

```text
75addb4 feat(core): freeze experiment run window
```

## Delivered contract

- Added immutable, slotted `RunWindow` with exact built-in integer UTC-microsecond validation, supported datetime-range checks, exact 12-week enrollment invariant, `T_m > E`, and pinned calendar identity fields.
- Defined enrollment as `[E_start, E)`: scheduled entry slots before `E_start`, at `E`, or after `E` are rejected.
- Resolved one shared `T_m` as the official close on the tenth XNYS session strictly after the UTC calendar date `E + 30 calendar days`.
- Added a minimal offline XNYS adapter backed by `exchange-calendars==4.11.2`; official schedule data handles holidays, DST, and early closes.
- Converted pandas schedule timestamps to stdlib UTC datetimes at the adapter boundary and then to integer microseconds without float seconds.
- Pinned the reviewed universal-wheel SHA-256, deterministic calendar content/rule identities, and NYSE provider-contract identity in the manifest example.
- Added exact manifest/runtime identity mismatch rejection for package version/hash, calendar content hash, T_m rule version/hash, and NYSE provider contract hash.
- Added the exact runtime dependency and transitive `uv.lock` entries. Updated source/config/lock/preflight/workflow policy hashes and artifact allowlists only for Task 5 surfaces.

## TDD RED evidence

Mandatory named tests were written before production modules existed. Individual runs failed at collection as intended:

```text
uv run pytest tests/core/test_run_window.py::test_tm_is_tenth_nyse_session_strictly_after_e_plus_30 -q
ModuleNotFoundError: No module named 'jev_trading_lab.core.run_window'

uv run pytest tests/core/test_run_window.py::test_dst_holiday_early_close -q
ModuleNotFoundError: No module named 'jev_trading_lab.core.run_window'

uv run pytest tests/core/test_run_window.py::test_same_tm_for_both_streams -q
ModuleNotFoundError: No module named 'jev_trading_lab.core.run_window'
```

Source/dependency policy also failed before approvals were updated:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
policy preflight: uv.lock: SHA-256 does not match exact reviewed dependency lock
policy preflight: pyproject.toml: content differs from exact approved build, dependency, entry-point, and tool contract
```

Later test-first corrections reproduced two boundary/identity gaps before implementation:

```text
uv run pytest tests/core/test_run_window.py::test_enrollment_is_exactly_twelve_weeks_and_boundary_is_exclusive -q
1 failed: entries_allowed_at(E_start - 1) returned True

uv run pytest tests/core/test_run_window.py::test_package_and_calendar_identity_mismatch_with_manifest_is_rejected -q
1 failed: providers.nyse_calendar.contract_sha256 mismatch did not raise
```

Both focused cases passed after minimal production changes.

## Focused GREEN evidence

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/core/test_run_window.py tests/test_package_policy.py -q
76 passed in 3.19s
```

Task tests cover:

- Hand-counted 2024-11-27 through 2024-12-11 sessions, excluding the threshold session itself; first through ninth sessions are not selected.
- DST transition close movement from 21:00 UTC to 20:00 UTC.
- July 4 and Thanksgiving closures plus July 3 and day-after-Thanksgiving 17:00/18:00 UTC early closes.
- One run-wide cutoff with no stream override, stable canonical serialization/hash, and fresh-process stability under a different host `TZ`.
- Exact `[E_start, E)` entry boundary and exact 84-day enrollment duration.
- Boolean, integer-subclass, enum, float, string, ordering, calendar identity, pre-epoch, out-of-range, missing-session, malformed-schedule, and unavailable-calendar-range rejection.
- Installed version, manifest identities, lock pin, package policy, and build artifact contents.

## Full gate evidence

All commands ran after final production changes and exited 0:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.63ms

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
454 passed in 4.01s

uv run ruff check .
All checks passed!

uv run ruff format --check .
20 files already formatted

uv run mypy src
Success: no issues found in 10 source files

uv export --frozen --all-groups --no-emit-project --format requirements-txt --output-file /tmp/jev-task-005-audit.txt --quiet
exit 0

uv run pip-audit --strict --requirement /tmp/jev-task-005-audit.txt --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

git diff --check
exit 0
```

`pip-audit` emitted its standard `--no-deps` advisory warnings; the audit result was clean. Artifact inspection confirmed `core/run_window.py`, `stocks/__init__.py`, and `stocks/calendar.py` in the wheel, and both Task 5 production modules in the sdist.

## Scope

No approved design, implementation plan, provider-contract research, or review file changed. No push performed. Task 6 remains blocked pending independent Task 5 reviews.

## Independent reviews

**PENDING INDEPENDENT REVIEWS**

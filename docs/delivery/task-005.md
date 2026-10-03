# Task 005 Delivery Evidence

## Status

**FINAL APPROVED / COMPLETE**

This document records implementation, remediation, verification, and final independent approval evidence.

## Code commits

```text
75addb4 feat(core): freeze experiment run window
3ff6a02 fix(core): make run window calendar authoritative
```

## Review findings resolved

- Replaced publicly constructible `RunWindow` dataclass with a slotted, immutable resolver-issued wrapper over private frozen `_RunWindowData`, canonical bytes, and `run_window_id`.
- Added `revalidate_run_window(window, manifest)`: recomputes from `enrollment_start_us`, reparses the manifest, checks canonical bytes/data/ID, rejects `object.__new__` forgeries and tampered internals, and returns a fresh authoritative wrapper.
- Made `resolve_run_window` the sole issuer. Arbitrary `T_m` changes of ±1 microsecond and ±1 day fail revalidation. Normal construction fails and instances expose no `__dict__`.
- Enforced exact NYSE provider `contract_version` and `contract_sha256` independently in manifest semantic validation and runtime calendar identity checks.
- Removed misleading wheel-hash/content-label semantics. Manifest now pins the exact `uv.lock` SHA-256 and the authoritative schedule artifact SHA-256.
- Added packaged canonical `src/jev_trading_lab/artifacts/xnys-schedule-2020-2036.json`, generated from `exchange-calendars==4.11.2` and `tzdata==2026.5`. It freezes 4,122 ordered XNYS session labels, opens, and official UTC closes, with explicit enrollment support from 2020-01-01 through 2035-12-31.
- Resolver now derives results only from the packaged artifact. `verify_runtime_calendar_against_artifact()` independently compares the installed provider schedule with every frozen row and rejects version, identity, or content drift.
- Artifact SHA, metadata, schema, row ordering/uniqueness, UTC integer timestamps, and supported range fail closed. Runtime package behavior can be monkeypatched without changing resolver output, while verification rejects that mutation.
- Added the runtime artifact to exact source policy, wheel/sdist package data, artifact-content inspection, preflight hashes, and workflow hash. `uv.lock` was unchanged.

## TDD RED evidence

Reviewer probes were added before production changes. Initial focused collection failed because the required trust-boundary and calendar APIs did not exist:

```text
env PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest tests/core/test_run_window.py tests/core/test_manifest.py::test_nyse_provider_contract_version_is_exact_independent_of_hash -q
ImportError: cannot import name 'revalidate_run_window' from 'jev_trading_lab.core.run_window'
```

Source/config/artifact/package-data policy also failed before approvals were updated:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
policy preflight: pyproject.toml: content differs from exact approved build, dependency, entry-point, and tool contract
policy preflight: src/jev_trading_lab/stocks/calendar.py: SHA-256 does not match approved content
policy preflight: src/jev_trading_lab/core/manifest.py: SHA-256 does not match approved content
policy preflight: src/jev_trading_lab/core/run_window.py: SHA-256 does not match approved content
policy preflight: src/jev_trading_lab/artifacts: directory is not approved
policy preflight: src/jev_trading_lab/artifacts/xnys-schedule-2020-2036.json: file is not approved
policy preflight: config/phase1.example.json: SHA-256 does not match approved content
```

Artifact packaging test then exposed the repository-wide `artifacts/` ignore rule:

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_package_policy.py::test_calendar_schedule_artifact_is_packaged_in_wheel_and_sdist -q
1 failed: jev_trading_lab/artifacts/xnys-schedule-2020-2036.json not in wheel
```

The ignore rule was narrowed to root `/artifacts/`; rebuilt wheel and sdist both contain the runtime artifact.

## GREEN evidence

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/core/test_run_window.py tests/core/test_manifest.py tests/test_package_policy.py -q
317 passed in 3.37s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_package_policy.py::test_calendar_schedule_artifact_is_packaged_in_wheel_and_sdist -q
1 passed in 0.13s
```

Review-focused coverage includes constructor/forgery rejection, private-state/canonical-byte/ID tampering, arbitrary cutoff probes, fresh revalidation, independent provider version/hash mismatches, package-version drift, artifact mutation, runtime calendar monkeypatch drift, deterministic artifact resolution, range rejection, and wheel/sdist inclusion.

## Final gates after code commit

All commands exited 0:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 64 packages in 1ms

uv sync --frozen --all-groups
Checked 63 packages in 0.59ms

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
449 passed in 3.60s

uv run ruff check .
All checks passed!

uv run ruff format --check .
20 files already formatted

uv run mypy src
Success: no issues found in 10 source files

uv run pip-audit --strict --requirement /tmp/jev-task-005-review-audit.txt --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

git diff --check HEAD^ HEAD
exit 0
```

`pip-audit` emitted only its standard `--no-deps` advisory warnings. Artifact inspection passed against both built distributions.

## Scope

No design, implementation plan, provider-contract research, or review files changed. No push performed.

## Final independent review verdicts

### Spec and adversarial review: PASS

The independent spec/adversarial reviewer reported **PASS**, with zero findings. The reviewer verified the complete 449-test suite, 28 adversarial probes, and all 4,122 authoritative calendar rows.

### Quality and model-risk review: APPROVED

The independent quality/model-risk reviewer reported **APPROVED**, with zero findings at `8cf694e`. The reviewer verified the authoritative artifact, trust boundary, package bytes, and required gates.

## Final reviewed commit chain

```text
75addb455908e21b81183636d9e4857556c4fbd8 feat(core): freeze experiment run window
88e82be05d86cd27a0ab3596b727e5be6dc67316 docs(delivery): record task 005 evidence
3ff6a02979e46eab1c176895565742e9a78f0ea8 fix(core): make run window calendar authoritative
8cf694efc172625b96d41c6ea013c28b0523062a docs(delivery): update task 005 review evidence
```

The approval evidence commit containing this section follows `8cf694e` and changes only `docs/delivery/task-005.md`.

## Final status

**FINAL APPROVED / COMPLETE.** Both required independent reviews report zero findings. Task 5 is complete; Task 6 may proceed under its own reviewed change-control cycle.

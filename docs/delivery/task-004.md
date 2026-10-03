# Task 004 Delivery Evidence

## Status

**PENDING INDEPENDENT REVIEWS**

## Scope

Implemented strict immutable Phase-1 JSON manifest parsing, complete nested experiment contracts, canonical identity, reviewed Pydantic dependency/lock changes, and exact source/config/workflow policy approvals. No persistence, environment loading, secrets, live execution, approved design, provider-contract, plan, or review file changed.

Production commit:

```text
3a2ede39d5cc4572f9a36ee4f3173fe4953c97e1 feat(core): define immutable run manifest
```

## RED evidence

Mandatory nodes were run separately before `manifest.py` existed. Each failed at the intended missing-module boundary:

```text
uv run pytest tests/core/test_manifest.py::test_manifest_hash_stable -q
1 failed: AssertionError: find_spec("jev_trading_lab.core.manifest") is None

uv run pytest tests/core/test_manifest.py::test_alias_and_unknown_key_rejected -q
1 failed: AssertionError: find_spec("jev_trading_lab.core.manifest") is None

uv run pytest tests/core/test_manifest.py::test_required_limits_present -q
15 failed: AssertionError: find_spec("jev_trading_lab.core.manifest") is None
```

Focused immutability, strict primitive, loader, UTF-8/size, deterministic-error, nested-required, and validator-matrix tests were authored before implementation and initially failed at the same missing-module boundary.

Provider exactness was strengthened by an additional red-green cycle during self-review:

```text
uv run pytest tests/core/test_manifest.py::test_validator_matrix_rejects_non_phase1_contract -q
3 failed, 37 passed
DID NOT RAISE for changed Polymarket catalog endpoint, changed Polymarket book source,
and changed stock OHLCV endpoint.
```

Caller-overrides were also tested before the fix:

```text
uv run pytest tests/core/test_manifest.py::test_validation_policy_cannot_be_weakened_by_caller -q
1 failed: DID NOT RAISE when model_validate(..., extra="allow") was requested
```

The implementation now pins provider source/endpoint pairs and always enforces strict/name-only/extra-forbid validation regardless of caller arguments.

### Source/config/dependency policy RED

After production/config/dependency files were added but before reviewed approvals, fail-closed preflight reported every changed trust surface:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
exit 1
policy preflight: uv.lock: SHA-256 does not match exact reviewed dependency lock
policy preflight: pyproject.toml: content differs from exact approved build, dependency, entry-point, and tool contract
policy preflight: src/jev_trading_lab/core/manifest.py: file is not approved
```

Package-policy pytest independently rejected the unapproved dependency and source:

```text
uv run pytest tests/test_package_policy.py::test_no_live_execution_surface -q
1 failed
pyproject.toml: runtime dependency not allowlisted: pydantic
pyproject.toml: runtime dependency source or requirement differs from exact approved strings
uv.lock: runtime dependency not allowlisted: pydantic
src/jev_trading_lab/core/manifest.py: production file is not approved
```

The exact config physical tree/hash, source hash, runtime requirement string, complete lock SHA-256, preflight hash, workflow-embedded hash, and workflow hash were then approved. Existing dependency-source, workflow-order, source-tree, operational-tree, lock, and live-execution gates remain intact.

## GREEN implementation

- Added frozen Pydantic 2 models using `ConfigDict(frozen=True, extra="forbid", strict=True)` and explicit semantic sections rather than generic dict plugin structures.
- Added `parse_manifest_bytes()` and `load_manifest()` with a 256 KiB limit, exact bytes input, strict UTF-8, root-object requirement, duplicate-key rejection, non-finite-number rejection, regular/readable-file checks, and deterministic field-path errors.
- Required exact Phase-1 run/mode, calendar/maturation, account/cash-yield, model/provider, fallback, quota, Polymarket, stock, cost, baseline, endpoint, statistics/classification, runtime/backup, and review-cadence contracts.
- Rejected model aliases, tools, model fallback, live/non-paper mode, unregistered data fallback, unknown nested keys, coercion, bool-as-int, null required fields, and caller attempts to weaken strict validation.
- Converted JSON lists to immutable tuples only after exact JSON-domain validation; all nested models are frozen.
- Derived `manifest_hash` from the complete normalized JSON-mode model dump via existing `canonical_hash("manifest", ...)`; no field is excluded and no mutable cache exists. `manifest_id` is `manifest:<hash>`.
- Added structurally valid `config/phase1.example.json` with `example_only=true`, deterministic placeholder IDs/hashes, and no secrets.
- Added only `pydantic>=2.12,<3` as the new runtime dependency; did not add `pydantic-settings` or environment loading.
- Extended stdlib preflight and package policy to validate the exact physical `config` tree and file hash.

Focused verification:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv run pytest tests/core/test_manifest.py tests/test_package_policy.py -q
186 passed in 1.72s
```

## Full gate evidence

Commands ran sequentially against the production tree and exited 0:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 55 packages in 1ms

uv sync --frozen --all-groups
Checked 54 packages in 0.52ms

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
318 passed in 1.58s

uv run ruff check .
All checks passed!

uv run ruff format --check .
16 files already formatted

uv run mypy src
Success: no issues found in 7 source files

uv export --frozen --all-groups --no-emit-project --format requirements-txt --output-file /tmp/jev-task-004-audit-requirements.txt --quiet
exit 0

uv run pip-audit --strict --requirement /tmp/jev-task-004-audit-requirements.txt --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

git diff --check
exit 0
```

`pip-audit` emitted its standard advisory warnings for `--no-deps`; audit result was clean.

Artifact inspection passed: wheel and sdist contain `jev_trading_lab/core/manifest.py`; wheel metadata pins `pydantic<3,>=2.12`; the repository-only example config is excluded from both artifacts.

## Self-review

- Manifest code imports no database, provider SDK, web framework, settings loader, or persistence layer.
- Exact built-in JSON primitives are validated before Pydantic tuple normalization. Signed-64-bit integer compatibility is preserved for canonical hashing.
- Provider contracts freeze the reviewed Phase-1 source names and endpoints. Only the individually enumerated Treasury prior-observation fallback is enabled.
- `example_only` remains part of the canonical hash. Activation rejection is intentionally deferred to the later registration/preflight task.
- Model revision/fingerprint and artifact hash fields remain required even though the checked-in example uses clearly marked deterministic placeholders.
- No secrets appear in the example manifest.
- Exact package policy now includes config tree/hash mutation controls without weakening prior trust boundaries.
- Production/tests/config/dependency/policy changes and this evidence document are isolated in separate commits.

## Independent reviews

Independent spec/adversarial and code-quality reviews have not yet been recorded. Status remains **PENDING INDEPENDENT REVIEWS**.

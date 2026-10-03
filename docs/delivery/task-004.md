# Task 004 Delivery Evidence

## Status

**PENDING INDEPENDENT RE-REVIEW**

Task 5 remains blocked until reviewers accept this correction.

## Commits

Initial implementation:

```text
3a2ede39d5cc4572f9a36ee4f3173fe4953c97e1 feat(core): define immutable run manifest
```

Review-finding correction:

```text
0476dd3 fix(core): close manifest trust and policy gaps
```

## Review findings resolved

- Replaced public Pydantic `Manifest` with a slotted, immutable parser-issued wrapper. The private `_ManifestModel` and nested models are implementation details. Public `Manifest` exposes no Pydantic construction/copy/validator surface and cannot be normally constructed.
- Added canonical bytes, detached `to_dict()`, canonical identity, and authoritative `revalidate_manifest()`. Future registration must accept canonical bytes or use `revalidate_manifest()`; arbitrary wrapper fields are not trusted.
- Added `assert_activation_candidate()`. It rejects `example_only`, placeholder markers, repeated placeholder hashes, and malformed/tampered wrapper state. Non-example manifests containing placeholders fail during parsing. Task 14 registration must call this method after revalidation.
- Added all reviewed required experiment settings: exact fallback mapping/timestamp/precedence/validation; normalized cluster metadata and version/hash; sigma estimator inputs/rules/version/hash; exact direct/shared cost routing; classification, scoring, information, power, and adverse-replay gates; OEF contract; SQLite busy timeout; all SEC endpoints; issuer source/domain allowlists; calendar package/content hashes; deterministic retry jitter; rule/artifact versions and SHA-256 fields.
- Tightened provider categories to lowercase, nonempty, unique tuples with case-insensitive sports rejection. Provider and Treasury contracts now freeze source, endpoint, timestamp, precedence, validation, and fallback behavior.
- Added provider-specific dated immutable model snapshot validation plus stable expected revision/fingerprint fields. Known OpenAI and Jev moving aliases fail.
- Hardened parsing with iterative depth/item/string limits, deterministic sorted/capped validation errors, and deterministic `RecursionError` conversion.
- Hardened file loading to open once, `fstat()` the descriptor before/after, read only `MAX_MANIFEST_BYTES + 1`, and reject practical size/identity races.
- Preserved exact source/config policy. Updated only reviewed hashes, preflight hash, workflow-embedded preflight hash, and workflow hash. Dependencies and `uv.lock` are unchanged.

## TDD RED evidence

Focused tests were added before production changes and failed with intended assertions:

```text
uv run pytest tests/core/test_manifest.py::test_public_manifest_has_parser_only_trust_boundary -q
1 failed
AssertionError: issubclass(Manifest, BaseModel) was True

uv run pytest tests/core/test_manifest.py::test_categories_and_model_snapshots_reject_ambiguous_inputs -q
1 failed
Failed: DID NOT RAISE ValueError for duplicate accepted categories

uv run pytest tests/core/test_manifest.py::test_parser_rejects_excessive_structure_and_caps_sorted_errors -q
1 failed
RecursionError from 1,100-level JSON nesting instead of deterministic ValueError
```

Additional trust/schema/activation/file-race/design-trace tests initially failed because the wrapper API, `revalidate_manifest`, `assert_activation_candidate`, required fields, exact provider contracts, parser limits, and descriptor-race checks did not exist.

A final artifact-version audit added rule hash/version expectations first; its focused RED was:

```text
uv run pytest tests/core/test_manifest.py::test_required_review_contract_fields_and_exact_design_literals -q
1 failed
KeyError: 'rule_version'
```

The production schema and example were then extended minimally to satisfy the new contract.

## Focused GREEN evidence

```text
uv run pytest tests/core/test_manifest.py -q
142 passed in 0.60s

uv run pytest tests/core/test_manifest.py tests/test_package_policy.py -q
195 passed in 1.63s
```

Adversarial coverage includes Pydantic inherited bypass absence, constructor rejection, `TypeAdapter(Manifest)` rejection, no `__dict__`, object-copy safety, canonical round-trip, deliberate `object.__setattr__` tampering caught by revalidation, example activation rejection, moving model aliases, case/duplicate categories, exact Treasury/provider contracts, 1,100-level nesting, 40,000 invalid categories within the byte cap, deterministic key-order-independent errors, capped error output, and simulated descriptor-size race.

## Full gate evidence

All commands ran after the final production changes and exited 0:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 55 packages in 1ms

uv sync --frozen --all-groups
Checked 54 packages in 0.52ms

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
327 passed in 1.70s

uv run ruff check .
All checks passed!

uv run ruff format --check .
16 files already formatted

uv run mypy src
Success: no issues found in 7 source files

uv export --frozen --all-groups --no-emit-project --format requirements-txt --output-file /tmp/jev-task-004-review-audit.txt --quiet
exit 0

uv run pip-audit --strict --requirement /tmp/jev-task-004-review-audit.txt --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

git diff --check
exit 0
```

`pip-audit` emitted its standard `--no-deps` advisory warnings; the audit result was clean. Artifact inspection confirmed both wheel and sdist contain `core/manifest.py`, and the repository-only example config remains excluded.

## Scope and handoff

No design, plan, provider-research, or review file changed. No dependency changed. The checked-in example remains `example_only=true` and structurally valid with explicit placeholder values. Task 14 registration must revalidate canonical bytes and call `assert_activation_candidate()` before persistence or activation.

Independent spec/adversarial and quality/security re-reviews have not yet been recorded. Status remains **PENDING INDEPENDENT RE-REVIEW**.

# Task 004 Delivery Evidence

## Status

**PENDING INDEPENDENT RE-REVIEW**

Task 5 remains blocked until reviewers accept this correction.

## Commits

Initial implementation:

```text
3a2ede39d5cc4572f9a36ee4f3173fe4953c97e1 feat(core): define immutable run manifest
```

Review-finding corrections:

```text
0476dd3 fix(core): close manifest trust and policy gaps
be86be2cf9a764b7b9120c5008ac46fc6451707c fix(core): freeze model input and activation identities
b85d8fc00942730c2819c9583c48574da1b5ff46 fix(core): close activation and schema identity gaps
bdfda7566dfd94e6409e92d7c54e36287c089600 fix(core): make activation identity checks field-aware
```

## Review findings resolved

- Replaced public Pydantic `Manifest` with a slotted, immutable parser-issued wrapper. The private `_ManifestModel` and nested models are implementation details. Public `Manifest` exposes no Pydantic construction/copy/validator surface and cannot be normally constructed.
- Added canonical bytes, detached `to_dict()`, canonical identity, and authoritative `revalidate_manifest()`. Future registration must accept canonical bytes or use `revalidate_manifest()`; arbitrary wrapper fields are not trusted.
- Added `assert_activation_candidate()`. It rejects `example_only`, placeholder markers, repeated placeholder hashes, and malformed/tampered wrapper state. Non-example manifests containing placeholders fail during parsing. Task 14 registration must call this method after revalidation.
- Added all reviewed required experiment settings: exact fallback mapping/timestamp/precedence/validation; normalized cluster metadata and version/hash; sigma estimator inputs/rules/version/hash; exact direct/shared cost routing; classification, scoring, information, power, and adverse-replay gates; OEF contract; SQLite busy timeout; all SEC endpoints; issuer source/domain allowlists; calendar package/content hashes; deterministic retry jitter; rule/artifact versions and SHA-256 fields.
- Tightened provider categories to lowercase, nonempty, unique tuples with case-insensitive sports rejection. Provider and Treasury contracts now freeze source, endpoint, timestamp, precedence, validation, and fallback behavior.
- Added provider-specific dated immutable model snapshot validation plus stable expected revision/fingerprint fields. Snapshot IDs now reject `latest`, `current`, and `default` as case-insensitive token components anywhere in the ID, and validate the required suffix with `date.fromisoformat()` so impossible dates fail.
- Replaced the broad activation placeholder scan with field-aware identity validation. Revision, fingerprint, artifact, model, hash, and version fields reject normalized unresolved markers; hashes reject trivial/repeated patterns; revision, fingerprint, version, and artifact identities require structured values. The checked-in example remains parser-valid only under `example_only=true`, while a sanitized non-example fixture passes activation.
- Added required root `model_input_policy` identities for the OpenAI prompt template, evidence delimiter, Jev state template, and Jev question set. Every artifact has a required version and lowercase SHA-256; removal fails validation, mutation changes `manifest_id`, and activation rejects placeholder identities.
- Closed activation marker bypasses with deterministic lowercase, non-alphanumeric component parsing. Strong unresolved components and explicit phrases are rejected anywhere in revisions, fingerprints, SDK identities, artifact IDs/versions, model-input policy versions, provider contract versions, and rule versions; real-shaped identities with marker substrings remain valid.
- Made each model contract the sole response-schema identity owner. Removed duplicate OpenAI schema identity from `model_input_policy`, rejected legacy/divergent duplicate fields, named OpenAI and Jev response schemas distinctly, and retained the independently frozen Jev question-set identity.
- Hardened parsing with iterative depth/item/string limits, deterministic sorted/capped validation errors, and deterministic `RecursionError` conversion.
- Hardened file loading to open once, `fstat()` the descriptor before/after, read only `MAX_MANIFEST_BYTES + 1`, and reject practical size/identity races.
- Corrected the identity classifier's sole major review finding with field-aware rules. Strong unresolved markers are now detected after lowercase alphanumeric compaction, so separator-free and suffixed forms such as `placeholder2026`, `changeme123`, `todo2026`, and `not_applicable_2026` fail across activation identity fields. Generic moving values (`latest`, `current`, `default`, `none`, `null`, `na`) fail only when they are the complete normalized value.
- Removed weak global `test`, `sample`, and `change` bans. Versioned rule identities such as `phase1-permutation-test-v1`, `phase1-sample-calendar-v1`, `phase1-change-detection-v1`, and `phase1-current-regime-v1` now pass. Field contracts independently require strict semantic SDK/calendar package versions, reviewed lowercase version/artifact slugs ending in `-vN`, structurally nontrivial provider revisions/fingerprints, exact SHA-256 shape, and existing provider-specific dated model snapshots. These regexes establish structure and minimum entropy only; provider metadata truth remains the later `assert_activation_candidate`/registration comparison.
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

The final identity review was also test-first. Focused RED runs failed as intended:

```text
uv run pytest tests/core/test_manifest.py::test_model_snapshots_reject_reserved_alias_components_and_invalid_dates -q
1 failed
Failed: DID NOT RAISE ValueError

uv run pytest tests/core/test_manifest.py::test_activation_candidate_rejects_examples_and_placeholders tests/core/test_manifest.py::test_activation_rejects_normalized_revision_markers tests/core/test_manifest.py::test_activation_rejects_field_aware_identity_placeholders -q
19 failed, 1 passed
Unresolved revision/fingerprint/artifact/version/hash markers were accepted.

uv run pytest tests/core/test_manifest.py::test_model_input_policy_is_required_content_addressed_and_identity_bound tests/core/test_manifest.py::test_complete_nested_required_sections -q
11 failed, 64 passed
KeyError: 'model_input_policy'

uv run pytest 'tests/core/test_manifest.py::test_activation_rejects_normalized_revision_markers[rev-change_me-2026]' -q
1 failed
Failed: DID NOT RAISE ValueError
```

Each focused case passed after its minimal implementation, including adversarial embedded normalized markers and malformed version/artifact identities.

The remaining activation/schema identity correction was also test-first:

```text
uv run pytest tests/core/test_manifest.py::test_activation_rejects_unresolved_marker_components_anywhere tests/core/test_manifest.py::test_activation_rejects_unresolved_markers_in_every_identity_class tests/core/test_manifest.py::test_activation_accepts_real_shaped_identity_components tests/core/test_manifest.py::test_model_input_policy_is_required_content_addressed_and_identity_bound -q
35 failed, 45 passed
Missing failures covered embedded/suffixed `placeholder`, `todo`, and `tbd`; `none`, `null`, `na`, and `notapplicable`; SDK package/version and other activation identity classes; the duplicate OpenAI policy schema still existed.

uv run pytest tests/core/test_manifest.py::test_model_input_policy_is_required_content_addressed_and_identity_bound -q
1 failed
Expected distinct authoritative `phase1-openai-response-schema-v1`; found ambiguous `phase1-schema-v1`.
```

The field-aware classifier correction was likewise added test-first. The mandatory focused RED was:

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/core/test_manifest.py::test_activation_rejects_compacted_unresolved_markers tests/core/test_manifest.py::test_activation_rejects_unresolved_markers_in_every_identity_class tests/core/test_manifest.py::test_activation_rejects_exact_generic_identity_values tests/core/test_manifest.py::test_activation_accepts_legitimate_rule_identity_components tests/core/test_manifest.py::test_activation_accepts_harmless_marker_substrings tests/core/test_manifest.py::test_activation_rejects_malformed_field_specific_identities -q
26 failed, 4 passed in 0.64s
```

Failures reproduced both sides of the major finding: embedded/concatenated unresolved markers and exact generic values were accepted, while legitimate `test`, `sample`, and `change` rule-name components were rejected. SDK, revision/fingerprint, rule-version, and artifact-ID structure checks also failed before production changes.

## Focused GREEN evidence

```text
uv run pytest tests/core/test_manifest.py::test_activation_rejects_unresolved_marker_components_anywhere tests/core/test_manifest.py::test_activation_rejects_unresolved_markers_in_every_identity_class tests/core/test_manifest.py::test_activation_accepts_real_shaped_identity_components tests/core/test_manifest.py::test_model_input_policy_is_required_content_addressed_and_identity_bound -q
92 passed in 0.43s

uv run pytest tests/test_package_policy.py -q
53 passed in 1.11s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/core/test_manifest.py::test_activation_rejects_compacted_unresolved_markers tests/core/test_manifest.py::test_activation_rejects_unresolved_markers_in_every_identity_class tests/core/test_manifest.py::test_activation_rejects_exact_generic_identity_values tests/core/test_manifest.py::test_activation_accepts_legitimate_rule_identity_components tests/core/test_manifest.py::test_activation_accepts_harmless_marker_substrings tests/core/test_manifest.py::test_activation_rejects_malformed_field_specific_identities -q
30 passed in 0.27s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/core/test_manifest.py -q
202 passed in 0.87s
```

Adversarial coverage includes Pydantic inherited bypass absence, constructor rejection, `TypeAdapter(Manifest)` rejection, no `__dict__`, object-copy safety, canonical round-trip, deliberate `object.__setattr__` tampering caught by revalidation, parser-valid/example-only activation rejection, a valid sanitized activation fixture, reserved alias components under mixed case/separators, impossible snapshot dates, every requested unresolved token/phrase in prefix/embedded/suffix positions, revisions/fingerprints/SDK identities/artifact identities/model-input versions/provider revisions/rule versions, valid real-shaped substring controls, rejected legacy/divergent OpenAI policy-schema keys, distinct model response-schema identities, retained Jev question-set identity, manifest identity mutation, case/duplicate categories, exact Treasury/provider contracts, 1,100-level nesting, 40,000 invalid categories within the byte cap, deterministic key-order-independent errors, capped error output, and simulated descriptor-size race.

## Full gate evidence

All commands ran after the final production changes and exited 0:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 55 packages in 1ms

uv sync --frozen --all-groups
Checked 54 packages in 0.60ms

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
387 passed in 2.03s

uv run ruff check .
All checks passed!

uv run ruff format --check .
16 files already formatted

uv run mypy src
Success: no issues found in 7 source files

uv export --frozen --all-groups --no-emit-project --format requirements-txt --output-file /tmp/jev-task-004-identity-classifier-audit.txt --quiet
exit 0

uv run pip-audit --strict --requirement /tmp/jev-task-004-identity-classifier-audit.txt --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

artifact inspection
wheel manifest True example_config False
sdist manifest True example_config False

git diff --check
exit 0
```

`pip-audit` emitted its standard `--no-deps` advisory warnings; the audit result was clean. Artifact inspection confirmed both wheel and sdist contain `core/manifest.py`, and the repository-only example config remains excluded.

## Scope and handoff

No design, plan, provider-research, or review file changed. No dependency changed. The checked-in example remains `example_only=true` and structurally valid with explicit placeholder values; its Jev SDK version was normalized to strict semantic form. Task 14 registration must revalidate canonical bytes and call `assert_activation_candidate()` before persistence or activation.

Independent spec/adversarial and quality/security re-reviews have not yet been recorded. Status remains **PENDING INDEPENDENT RE-REVIEW**.

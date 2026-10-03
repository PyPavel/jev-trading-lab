# Task 004 Delivery Evidence

## Status

**FINAL APPROVED / COMPLETE**

This document records implementation, remediation, verification, and final independent approval evidence.

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
f8c901811ff7634f8ffb1e650ce9d0f1ab7ec2ee fix(core): make activation identities precise
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
- Preserved exact source/config policy. Updated only reviewed hashes, preflight hash, workflow-embedded preflight hash, and workflow hash. Dependencies and `uv.lock` are unchanged.
- Replaced the remaining compaction/substr heuristic with explicit lowercase alphanumeric tokens. A token now fails only when it exactly equals a strong marker or equals that marker plus digits. Required multi-token phrases fail only as adjacent token sequences with an optional numeric suffix on the final token. This covers `example`, `xxx`, model snapshots, provider metadata, SDK identities, schemas, rules, artifacts, and model-input identities without banning legitimate containing words.
- Normalized complete generic values by removing separators before exact comparison, so `n-a`, `n_a`, and `N/A` reject as `na` while versioned identities containing `current` remain governed by their field contract.
- Restricted repeated-hash detection to every proper divisor of 64 (`1, 2, 4, 8, 16, 32`) and added the reviewer's nonperiodic 32-character half repeated twice.
- Enforced canonical SemVer core for SDK/calendar versions: no leading zeroes and no prerelease/build suffix. Added explicit RED/GREEN matrices for rejected `01.2.3`, `1.02.3`, `1.2.03` and accepted `0.0.0`, `3.12.0`, `10.20.30`.
- Added root model-contract validation requiring OpenAI and Jev response-schema hashes to differ and their full `(schema_version, schema_sha256)` identities not to be identical. Jev question-set identity remains independently frozen.

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

The precise token-aware correction added one explicit table-driven matrix before production changes:

| Invariant | RED cases | Intended result |
|---|---|---|
| Exact/digit-suffixed strong tokens | every required marker, including `example`, `example2026`, `xxx`, `xxx2026` | reject |
| Adjacent phrases | `change me`, `replace me`, `not applicable`, `not set`, `test only`; numeric final-token forms | reject |
| All activation identities | Jev snapshot `jev-phase1-example-...`, revisions/fingerprints, SDK package, artifacts, model-input versions, provider/rule versions | reject |
| Complete generic values | `latest`, `current`, `default`, `none`, `null`, `na`, `n-a`, `n_a`, `N/A` | reject |
| Legitimate containing words | `sunset`, `depending`, `contest`, `changelog` and four required versioned IDs | accept |
| Strict SemVer | `01.2.3`, `1.02.3`, `1.2.03` / `0.0.0`, `3.12.0`, `10.20.30` | reject / accept |
| Repeated SHA-256 | periods `1,2,4,8,16,32`, including the nonperiodic half repeated twice | reject |
| Distinct provider schemas | exact OpenAI-to-Jev schema replacement and same hash with a differing version | reject |

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/core/test_manifest.py::test_activation_rejects_exact_or_digit_suffixed_unresolved_tokens tests/core/test_manifest.py::test_activation_rejects_unresolved_token_phrases tests/core/test_manifest.py::test_activation_rejects_unresolved_markers_in_every_identity_class tests/core/test_manifest.py::test_activation_rejects_separator_insensitive_generic_identity_values tests/core/test_manifest.py::test_activation_accepts_legitimate_identity_tokens tests/core/test_manifest.py::test_activation_accepts_harmless_marker_substrings tests/core/test_manifest.py::test_activation_rejects_noncanonical_semver tests/core/test_manifest.py::test_activation_accepts_canonical_semver tests/core/test_manifest.py::test_activation_rejects_periodic_sha256_identities tests/core/test_manifest.py::test_activation_accepts_nonperiodic_sha256_identity tests/core/test_manifest.py::test_provider_response_schema_identities_are_distinct tests/core/test_manifest.py::test_provider_response_schema_hashes_must_differ -q
18 failed, 65 passed in 0.67s
```

Observed failures exactly covered missing `example`/`xxx`, `test only`, Jev snapshot activation scanning, separator-insensitive generic values, false positives for `sunset`/`depending`, leading-zero SemVer, period-32 repeated hashes, and both provider response-schema authority checks.

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

# Final precise identity correction

env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/core/test_manifest.py -q
246 passed in 0.93s

env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_package_policy.py -q
53 passed in 1.08s
```

Adversarial coverage includes Pydantic inherited bypass absence, constructor rejection, `TypeAdapter(Manifest)` rejection, no `__dict__`, object-copy safety, canonical round-trip, deliberate `object.__setattr__` tampering caught by revalidation, parser-valid/example-only activation rejection, a valid sanitized activation fixture, reserved alias components under mixed case/separators, impossible snapshot dates, every required exact/digit-suffixed unresolved token and phrase across revisions/fingerprints/SDK/model snapshots/artifacts/model-input/provider/rule/schema identities, separator-normalized complete generic values, legitimate substring controls (`sunset`, `depending`, `contest`, `changelog`), strict canonical SemVer, every proper 64-character hash period, the nonperiodic 32-character half-repeat fixture, exact provider schema replacement, same-hash/different-version rejection, retained Jev question-set identity, rejected legacy/divergent OpenAI policy-schema keys, manifest identity mutation, case/duplicate categories, exact Treasury/provider contracts, 1,100-level nesting, 40,000 invalid categories within the byte cap, deterministic key-order-independent errors, capped error output, and simulated descriptor-size race.

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
431 passed in 2.16s

uv run ruff check .
All checks passed!

uv run ruff format --check .
16 files already formatted

uv run mypy src
Success: no issues found in 7 source files

uv export --frozen --all-groups --no-emit-project --format requirements-txt --output-file /tmp/jev-task-004-precise-identity-audit.txt --quiet
exit 0

uv run pip-audit --strict --requirement /tmp/jev-task-004-precise-identity-audit.txt --no-deps --disable-pip
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

## Final independent review verdicts

### Spec and adversarial review: PASS

The independent spec/adversarial reviewer reported **PASS**, with zero BLOCKER and zero MAJOR findings. The reviewer completed 4,134 identity/trust checks and verified the complete 431-test suite.

### Quality and model-risk review: APPROVED

The independent quality/model-risk reviewer reported **APPROVED**, with zero BLOCKER and zero MAJOR findings. The reviewer verified 299 Task 4 tests and 193 supplemental adversarial checks.

## Final reviewed commit chain

```text
3a2ede39d5cc4572f9a36ee4f3173fe4953c97e1 feat(core): define immutable run manifest
62ca131b3899cbd0c3a66785964613fe23852d4c docs(delivery): record task 004 evidence
0476dd3ad9f1bfef9978bce6f9c6c4b30ff41ead fix(core): close manifest trust and policy gaps
611549406a5500ef388214b93d3e3a77d0432314 docs(delivery): update task 004 review evidence
be86be2cf9a764b7b9120c5008ac46fc6451707c fix(core): freeze model input and activation identities
d1b76688ab3c94491ecb67a8a040b4bf08becc9d docs(delivery): update task 004 identity review
b85d8fc00942730c2819c9583c48574da1b5ff46 fix(core): close activation and schema identity gaps
a3cd848bede7837b3aae01adee88d5ab6c3778e7 docs(delivery): update task 004 activation review
bdfda7566dfd94e6409e92d7c54e36287c089600 fix(core): make activation identity checks field-aware
2f7773ffd1fe254087afb046b33bd02c562292ad docs(delivery): update task 004 identity classifier review
f8c901811ff7634f8ffb1e650ce9d0f1ab7ec2ee fix(core): make activation identities precise
34baba296bf283ebb98f021d3f86a25c9968af7a docs(delivery): update task 004 precise identity review
```

The approval evidence commit containing this section follows `34baba2` and changes only `docs/delivery/task-004.md`.

## Final status

**FINAL APPROVED / COMPLETE.** Both required independent reviews report zero BLOCKER and zero MAJOR findings. Task 4 is complete; Task 5 may proceed under its own reviewed change-control cycle.

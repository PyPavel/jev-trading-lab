# Task 003 Delivery Evidence

## Status

**FINAL APPROVED / COMPLETE**

This document records implementation, remediation, verification, and final independent approval evidence.

## Scope

Hardened the existing Task 3 canonical identity and observation metadata implementation after review. No runtime dependency, lock file, design, plan, or review document changed.

Production commits:

```text
24f9a5a27a05d48a58c6d4ed5950e4fb0acdee4e feat(core): add identities time and observation metadata
45f8069260dd5de3a3ca8bafeac01357b982e771 fix(core): enforce observation and ID invariants
5a6c81c71dc6a995baaa056b46c35feca7a25ddc fix(core): restrict observation sources to valid HTTPS URLs
bd425a7d843a06c20301318a39d92a1c2e3c650d fix(core): reject ambiguous numeric observation hosts
```

## Review findings resolved

1. Correction invariants now live at the `ObservationMetadata` constructor boundary. A correction must provide constructor-only `predecessor` context; `supersedes_id` must equal that predecessor's observation ID, retrieval must be strictly later, and content hash must differ. A predecessor without `supersedes_id` is rejected. `correct_observation()` delegates to this constructor path.
2. Source URI validation is deliberately limited to exact lowercase `https` URLs because every Phase-1 provider contract uses HTTPS. It requires a valid DNS reg-name, IPv4 address, or bracketed IPv6 address; accepts only numeric ports in `1..65535`; rejects userinfo, fragments, non-ASCII, controls/whitespace, raw RFC-disallowed characters, and malformed percent escapes; and preserves the exact input string without normalization. Arbitrary `ftp`, `file`, `urn`, and custom schemes are unsupported.
3. Canonical JSON now accepts only exact built-in integers in the signed 64-bit range. Boundary values are accepted; out-of-range and huge integers are rejected before `json.dumps`, independent of Python's integer string digit limit. Booleans and integer subclasses retain their existing distinct/rejected behavior.
4. HTTPS hosts now reject resolver-ambiguous numeric forms. IPv4 is accepted only as four canonical ASCII decimal labels in `0..255` without leading zeroes and with exact `IPv4Address` round-trip. All-numeric hosts, numeric TLDs, hex-prefixed labels, and non-four-label numeric forms are rejected while alphanumeric DNS names remain valid. Bracketed IPv6 is accepted only without raw or percent-encoded scope identifiers.

## RED evidence

Each mandatory review regression was run before its production fix and failed for the intended reason.

### Direct constructor bypass

```text
uv run pytest tests/core/test_observations.py::test_direct_correction_constructor_requires_predecessor -q
1 failed: Failed: DID NOT RAISE ValueError

uv run pytest tests/core/test_observations.py::test_direct_correction_constructor_rejects_backdating -q
1 failed: TypeError: ObservationMetadata.__init__() got an unexpected keyword argument 'predecessor'

uv run pytest tests/core/test_observations.py::test_direct_correction_constructor_rejects_same_content_hash -q
1 failed: TypeError: ObservationMetadata.__init__() got an unexpected keyword argument 'predecessor'

uv run pytest tests/core/test_observations.py::test_direct_correction_constructor_rejects_unrelated_predecessor -q
1 failed: TypeError: ObservationMetadata.__init__() got an unexpected keyword argument 'predecessor'

uv run pytest tests/core/test_observations.py::test_direct_non_correction_constructor_rejects_predecessor -q
1 failed: TypeError: ObservationMetadata.__init__() got an unexpected keyword argument 'predecessor'
```

### Hostile URIs

```text
uv run pytest tests/core/test_observations.py::test_hostile_source_uris_are_rejected -q
5 failed, 2 passed
DID NOT RAISE for malformed percent escape, non-numeric port, :443 without host,
userinfo with missing host, and embedded NUL. Malformed IPv6 and query-only custom
URI already failed and remained covered.

uv run pytest tests/core/test_observations.py::test_hostile_source_uris_are_rejected -q
1 failed, 7 passed
DID NOT RAISE for embedded DEL control character.
```

Valid IPv6 and percent-encoded HTTP(S) cases were initially added as positive controls. The later URI-domain review showed that the broad non-HTTP claim was unsafe and unnecessary for Phase 1.

### URI-domain MAJOR follow-up

Independent review found the generic URI validator still accepted raw RFC-disallowed characters, malformed non-HTTP authorities/ports, and invalid DNS labels. The boundary was narrowed to exact lowercase HTTPS rather than extending unused arbitrary-scheme parsing.

Mandatory nodes were run individually before the fix. Existing behavior already rejected malformed percent escapes and userinfo, and already accepted the required valid HTTPS fixtures; every missing behavior failed by `DID NOT RAISE ValueError`:

```text
uv run pytest tests/core/test_observations.py::test_source_uri_requires_exact_lowercase_https_scheme -q
4 failed: accepted ftp URL, ftp invalid port, ftp missing host, and custom scheme

uv run pytest tests/core/test_observations.py::test_source_uri_rejects_non_lowercase_https_scheme -q
2 failed: accepted uppercase and mixed-case schemes

uv run pytest tests/core/test_observations.py::test_source_uri_rejects_invalid_dns_hosts -q
5 failed, 1 passed: accepted raw `|`, leading/trailing hyphen, empty label, and underscore

uv run pytest tests/core/test_observations.py::test_source_uri_rejects_malformed_or_misplaced_ipv6_brackets -q
1 failed, 3 passed: accepted brackets in a path

uv run pytest tests/core/test_observations.py::test_source_uri_rejects_invalid_ports -q
2 failed, 2 passed: accepted empty port and port 0

uv run pytest tests/core/test_observations.py::test_source_uri_rejects_invalid_percent_escapes -q
4 passed (existing guard retained)

uv run pytest tests/core/test_observations.py::test_source_uri_rejects_userinfo -q
3 passed (existing guard retained)

uv run pytest tests/core/test_observations.py::test_source_uri_rejects_fragments -q
1 failed: accepted fragment

uv run pytest tests/core/test_observations.py::test_source_uri_rejects_raw_disallowed_path_characters -q
11 failed: accepted raw `<`, `>`, `"`, `{`, `}`, `|`, `\\`, `^`, backtick, `[`, and `]` in paths

uv run pytest tests/core/test_observations.py::test_source_uri_rejects_controls_whitespace_and_non_ascii -q
1 failed, 4 passed: accepted raw non-ASCII

uv run pytest tests/core/test_observations.py::test_valid_https_source_uris_are_accepted -q
7 passed

uv run pytest tests/core/test_observations.py::test_source_uri_is_not_silently_normalized -q
1 passed
```

A follow-up RED for dotted-numeric host ambiguity also failed before its guard:

```text
uv run pytest tests/core/test_observations.py::test_source_uri_rejects_invalid_dns_hosts -q
1 failed, 6 passed: accepted `https://999.1.1.1/data`
```

### Ambiguous numeric hosts and scoped IPv6

The remaining reviewer cases were added before the production fix. Numeric host RED rejected only the two already-covered non-canonical four-label forms and exposed ten accepted ambiguous forms. Scoped IPv6 RED exposed both raw and percent-encoded zones:

```text
uv run pytest tests/core/test_observations.py::test_source_uri_rejects_ambiguous_numeric_hosts -q
10 failed, 2 passed
DID NOT RAISE for `127.1`, `127.0.1`, `2130706433`, `0x7f000001`, `999`,
`1.2.3`, `1.2.3.4.5`, `0`, `00`, and `0xffffffff`; existing validation
already rejected `0177.0.0.1` and `127.000.000.001`.

uv run pytest tests/core/test_observations.py::test_source_uri_rejects_scoped_ipv6_hosts -q
2 failed
DID NOT RAISE for `https://[fe80::1%eth0]/data` or
`https://[fe80::1%25eth0]/data`.
```

A final adversarial extension then proved that hex-prefixed dotted labels and numeric TLD ambiguity remained before the matching guard:

```text
env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/core/test_observations.py::test_source_uri_rejects_ambiguous_numeric_hosts -q
2 failed, 12 passed
DID NOT RAISE for `127.0x0.0.1` and `example.999`.
```

### Canonical integer domain

```text
uv run pytest tests/core/test_identity.py::test_canonical_json_accepts_signed_int64_boundaries -q
2 passed

uv run pytest tests/core/test_identity.py::test_canonical_json_rejects_integers_outside_signed_int64 -q
2 failed: Failed: DID NOT RAISE ValueError

uv run pytest tests/core/test_identity.py::test_canonical_json_rejects_huge_integer_before_json_encoding -q
1 failed: expected "signed 64-bit"; received Python's default 4300-digit integer conversion limit error
```

The boundary test passed before implementation, confirming the accepted endpoints while the outside-range tests demonstrated the missing guard.

### Source-policy RED

After production files changed but before exact source-policy approval, both controls failed closed:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
exit 1
policy preflight: src/jev_trading_lab/core/ids.py: SHA-256 does not match approved content
policy preflight: src/jev_trading_lab/core/observations.py: SHA-256 does not match approved content

uv run pytest tests/test_package_policy.py::test_no_live_execution_surface -q
1 failed
src/jev_trading_lab/core/ids.py: SHA-256 does not match approved source
src/jev_trading_lab/core/observations.py: SHA-256 does not match approved source
```

The URI-domain follow-up independently reproduced the same fail-closed source-policy behavior before approving its production source change:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
exit 1
policy preflight: src/jev_trading_lab/core/observations.py: SHA-256 does not match approved content

uv run pytest tests/test_package_policy.py::test_no_live_execution_surface -q
1 failed
src/jev_trading_lab/core/observations.py: SHA-256 does not match approved source
```

Exact source hashes, policy-preflight hash, workflow-embedded preflight hash, and approved workflow hash were then updated without weakening dependency, lock, workflow-order, or operational policy.

The numeric/scoped-host production change independently reproduced fail-closed source policy before hash approval. Focused pytest RED also generated unapproved interpreter cache artifacts; both policy controls reported those artifacts plus the expected observation-source hash mismatch. The cache artifacts were removed before exact hash approval.

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
exit 1
policy preflight: src/jev_trading_lab/__pycache__: directory is not approved
policy preflight: src/jev_trading_lab/core/__pycache__: directory is not approved
policy preflight: src/jev_trading_lab/core/observations.py: SHA-256 does not match approved content

uv run pytest tests/test_package_policy.py::test_no_live_execution_surface -q
1 failed
runtime policy violations included unapproved `__pycache__` paths and
`src/jev_trading_lab/core/observations.py: SHA-256 does not match approved source`
```

## GREEN implementation

- Added documented `InitVar[ObservationMetadata | None]` predecessor context. It is validated during construction but is not stored and does not affect `observation_id` hashing.
- Centralized correction predecessor, identity, retrieval ordering, and changed-content checks in `ObservationMetadata.__post_init__()`.
- Kept `correct_observation()` as a minimal convenience API delegating to the authoritative constructor.
- Narrowed `source_uri` to exact lowercase HTTPS and validated DNS labels, IPv4, bracketed IPv6, ports, userinfo, fragments, ASCII path/query character sets, and percent escapes with stdlib `urlsplit` and `ipaddress`; retained the exact input as canonical identity.
- Added exact built-in signed int64 validation before canonical JSON serialization.
- Added direct bypass, hostile URI, valid URI, int64 boundary, huge integer, and integer-subclass regression tests.
- Restricted IPv4 to canonical four-label dotted decimal with ASCII-decimal labels, range and leading-zero checks, and exact stdlib round-trip.
- Rejected all-numeric hosts, numeric TLDs, hex-prefixed labels, and raw or percent-encoded IPv6 scope identifiers; retained alphanumeric DNS and unscoped IPv6 positive controls.

Focused verification:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv run pytest tests/core/test_identity.py tests/core/test_observations.py tests/test_package_policy.py -q
98 passed in 1.17s
```

## Numeric/scoped-host follow-up full gate evidence

Commands ran sequentially against production commit `bd425a7` and exited 0:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 51 packages in 1ms

uv sync --frozen --all-groups
Checked 50 packages in 0.51ms

env PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
183 passed in 1.22s

uv run ruff check .
All checks passed!

uv run ruff format --check .
14 files already formatted

uv run mypy src
Success: no issues found in 6 source files

uv export --frozen --all-groups --no-emit-project --format requirements-txt --output-file /tmp/jev-task-003-host-audit-requirements.txt --quiet
exit 0

uv run pip-audit --strict --requirement /tmp/jev-task-003-host-audit-requirements.txt --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

git diff --check
exit 0
```

`pip-audit` emitted its standard advisory warnings for `--no-deps`; audit result was clean.

## URI-domain follow-up full gate evidence

Commands ran sequentially against production commit `5a6c81c` and exited 0:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 51 packages in 1ms

uv sync --frozen --all-groups
Checked 50 packages in 0.70ms

uv run pytest -q
166 passed in 1.20s

uv run ruff check .
All checks passed!

uv run ruff format --check .
14 files already formatted

uv run mypy src
Success: no issues found in 6 source files

uv export --frozen --all-groups --no-emit-project --format requirements-txt --output-file /tmp/jev-task-003-uri-audit-requirements.txt
exit 0

uv run pip-audit --strict --requirement /tmp/jev-task-003-uri-audit-requirements.txt --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

git diff --check
exit 0
```

`pip-audit` emitted its standard advisory warnings for `--no-deps`; audit result was clean.

## Earlier full gate evidence

Commands ran sequentially against the earlier review-fix production tree and exited 0:

```text
/usr/bin/python3 -I -S scripts/policy_preflight.py
Policy preflight passed.

uv lock --check
Resolved 51 packages in 1ms

uv sync --frozen --all-groups
Checked 50 packages in 0.49ms

uv run pytest -q
123 passed in 1.24s

uv run ruff check .
All checks passed!

uv run ruff format --check .
14 files already formatted

uv run mypy src
Success: no issues found in 6 source files

uv export --frozen --all-groups --no-emit-project --format requirements-txt --output-file /tmp/jev-task-003-review-audit-requirements.txt
exit 0

uv run pip-audit --strict --requirement /tmp/jev-task-003-review-audit-requirements.txt --no-deps --disable-pip
No known vulnerabilities found

uv build --no-build-isolation
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl

git diff --check
exit 0
```

`pip-audit` emitted its standard advisory warnings for `--no-deps`; audit result was clean.

## Self-review

- Constructor callers cannot create superseding metadata without the exact predecessor object and all correction invariants.
- `predecessor` is intentionally ephemeral constructor context, not an immutable metadata field and not part of canonical identity.
- URI validation establishes an HTTPS source-identity syntax boundary only. DNS resolution, public-address policy, redirects, TLS, and transport controls remain later evidence-transport responsibilities.
- Canonical IPv4 and unscoped IPv6 remain accepted. Numeric-looking resolver aliases and scoped IPv6 are rejected before any later transport can reinterpret them.
- Correction validation covers one direct predecessor. Repository-backed chain acyclicity and branch prevention remain persistence responsibilities in later tasks.
- Canonical serialization remains deliberately narrow and performs no implicit normalization.
- No dependency or lock file changed. No approved design, plan, or review file changed.
- Production/tests/policy changes and this delivery evidence are isolated in separate commits.

## Final independent review verdicts

### Spec and adversarial review: PASS

The independent spec/adversarial reviewer reported **PASS**, with zero BLOCKER and zero MAJOR findings. The reviewer verified rejection of all 20 tested ambiguous numeric-host forms and all 4 tested scoped-IPv6 forms, acceptance of the original valid URI matrix, and the complete 183-test suite.

### Quality review: APPROVED

The independent quality reviewer reported **APPROVED**, with zero BLOCKER and zero MAJOR findings. The reviewer ran a parser differential across `urllib`, `httpx`, `urllib3`, and WHATWG implementations: 54,677 valid generated cases were accepted with no semantic host or port differences.

## Final reviewed commit chain

```text
24f9a5a27a05d48a58c6d4ed5950e4fb0acdee4e feat(core): add identities time and observation metadata
877c37573df3702f1d941a785614eede4ee89348 docs(delivery): record task 003 evidence
45f8069260dd5de3a3ca8bafeac01357b982e771 fix(core): enforce observation and ID invariants
8dc90d4e0c7ef5629b71255c950bb3b1445f546f docs(delivery): update task 003 review evidence
5a6c81c71dc6a995baaa056b46c35feca7a25ddc fix(core): restrict observation sources to valid HTTPS URLs
f7cb58d75d48af2f6841d4d46da34ac52c096666 docs(delivery): update task 003 URI review
bd425a7d843a06c20301318a39d92a1c2e3c650d fix(core): reject ambiguous numeric observation hosts
404de03f6e1238061929de81644907be6cc69277 docs(delivery): update task 003 host review
```

The approval evidence commit containing this section follows `404de03` and changes only `docs/delivery/task-003.md`.

## Final status

**FINAL APPROVED / COMPLETE.** Both required independent reviews report zero BLOCKER and zero MAJOR findings. Task 3 is complete; Task 4 may proceed under its own reviewed change-control cycle.

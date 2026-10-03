# Task 001 Delivery Evidence

## Scope

Initialized the Python 3.12 `jev-trading-lab` package and hardened its package, dependency, CI, and paper-only policy after independent review found major gaps.

## Commit chain

```text
808bf56705f36961fa0c59024f4012dc97f6bd20 build: initialize paper-only trading lab
3cdd7d6                                docs(delivery): record task 001 evidence
c14463db0cee4b46fb27e5536ec2fd123d2fcd30 fix(build): harden paper-only package policy
```

The earlier evidence commit remains in history. Its PASS verdict was premature and is superseded by this document.

## Original RED evidence

The initial implementation recorded assertion failures before the package shell, paper-only scanner, build configuration, and CLI callback were added. The original observed failures were:

```text
pytest tests/test_package_policy.py::test_package_imports -q
E   AssertionError: assert None is not None
1 failed

pytest tests/test_package_policy.py::test_no_live_execution_surface -q
E   AssertionError: pyproject.toml must define the package policy
1 failed

pytest tests/test_package_policy.py::test_locked_build_configuration -q
E   AssertionError: assert None == 'hatchling.build'
1 failed

pytest tests/test_package_policy.py::test_cli_root_describes_paper_research -q
RuntimeError: Could not get a command for this Typer instance
1 failed
```

## Independent review findings

Independent review rejected the original PASS evidence with major findings:

1. The paper-only scanner could miss PEP 503 hyphen/underscore variants, unknown trading SDKs, direct HTTP POST calls to order endpoints, string-dispatched signing/order methods, shell and ops adapters, and executable/config surfaces outside Python AST names.
2. Required development dependencies `pytest-asyncio`, `respx`, and `hypothesis` were absent.
3. Hatchling could resolve outside `uv.lock` because the build requirement was ranged and Hatchling was not in a locked dependency group.
4. Pytest was locked to 8.4.2 rather than a fixed minimum of 9.0.3, and CI lacked a reproducible vulnerability audit.
5. GitHub Actions used mutable tags and checkout retained credentials.
6. The prior delivery document claimed PASS before independent review.

## Review-fix RED evidence

Positive controls were added before scanner/dependency/CI implementation changes. Command:

```console
uv run pytest tests/test_package_policy.py::test_policy_positive_controls_reject_bypasses tests/test_package_policy.py::test_required_dev_toolchain_is_declared_and_locked tests/test_package_policy.py::test_locked_build_configuration tests/test_package_policy.py::test_ci_uses_immutable_actions_and_locked_audit -q
```

Observed expected assertion failures:

```text
FFFFFFFFFFFFFF                                                           [100%]
14 failed in 0.08s
```

The failures covered `py-clob-client`, `eth-account`, `alpaca-py`, `ib-insync`, an unknown trading SDK, direct order-endpoint POST, string-dispatched `submit-order` and `sign-order`, an ops service adapter, a shell adapter, required dev tools, exact Hatchling lock policy, and immutable CI action pins.

Additional literal-concatenation control was added and observed failing before its minimal AST fix:

```console
uv run pytest 'tests/test_package_policy.py::test_policy_positive_controls_reject_bypasses[src/sample.py-import requests\nrequests.post("https://api.example.test" + "/orders")\n-orders]' -q
```

```text
E   AssertionError: ['src/sample.py: unauthorized import requests']
1 failed in 0.13s
```

Additional config and lock-surface controls were added and observed failing before their minimal fixes:

```console
uv run pytest tests/test_package_policy.py::test_policy_positive_controls_reject_bypasses -q
```

```text
......FF.......                                                          [100%]
2 failed, 13 passed in 0.14s
```

The two failures proved that a POST adapter embedded in `pyproject.toml` tool configuration and a prohibited package present in `uv.lock` were not yet detected.

## Fixes

Production fix commit:

```text
c14463db0cee4b46fb27e5536ec2fd123d2fcd30 fix(build): harden paper-only package policy
```

Changes:

- PEP 503-normalized explicit runtime dependency allowlist (`typer` only).
- AST import boundary limited to the standard library, `jev_trading_lab`, and the allowed runtime package.
- AST string, identifier, attribute, literal-concatenation, and call-aware checks for live/signing/private-key/order surfaces and direct POSTs to order endpoints.
- Text scanning for all non-binary executable/config surfaces under `src/`, `scripts/`, and `ops/`, plus structured checks of `pyproject.toml` and `uv.lock`.
- Positive controls for normalized trading SDK names, unknown SDKs, entry points, config commands, lock packages, Python calls/literals, shell adapters, and ops adapters. Rule literals remain assembled to avoid scanner self-matches.
- Added and locked `pytest-asyncio`, `respx`, `hypothesis`, and `pip-audit`; upgraded Pytest with `pytest>=9.0.3,<10` (resolved 9.1.1).
- Pinned Hatchling exactly at 1.27.0 in both build-system and locked dev group.
- Added frozen lock export plus `pip-audit` CI gate.
- Pinned checkout, setup-uv, and gitleaks actions to retrieved 40-character tag SHAs with version comments; set `persist-credentials: false`; retained `contents: read`.

Action tag refs were retrieved, not invented:

```text
actions/checkout v4:       11d5960a326750d5838078e36cf38b85af677262
astral-sh/setup-uv v6:     d0d8abe699bfb85fec6de9f7adb5ae17292296ff
gitleaks/gitleaks-action v2: dcedce43c6f43de0b836d1fe38946645c9c638dc
```

## Focused paper-only policy verification

Command:

```console
uv run pytest tests/test_package_policy.py -q
```

Observed output:

```text
.................................                                        [100%]
33 passed in 0.23s
```

This includes all positive controls and a scan of the repository's current runtime/config surfaces.

## Lock, sync, full suite, lint, type, audit, and build verification

Command:

```console
uv lock --check && uv sync --frozen --all-groups && uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv export --frozen --all-groups --no-emit-project --format requirements-txt --output-file /tmp/jev-audit-requirements.txt >/tmp/jev-export.log && uv run pip-audit --strict --requirement /tmp/jev-audit-requirements.txt --no-deps --disable-pip && uv build
```

Observed output and exit status 0:

```text
Resolved 51 packages in 1ms
Checked 50 packages in 0.52ms

.................................                                        [100%]
33 passed in 0.23s
All checks passed!
5 files already formatted
Success: no issues found in 2 source files
WARNING:pip_audit._cli:--no-deps is supported, but users are encouraged to fully hash their pinned dependencies
WARNING:pip_audit._cli:Consider using a tool like `pip-compile`: https://pip-tools.readthedocs.io/en/latest/#using-hashes
No known vulnerabilities found
Building source distribution...
Building wheel from source distribution...
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl
```

`uv export` generated a fully pinned, hashed requirements file from the frozen lock. `pip-audit --locked .` was also attempted, but pip-audit 2.10.1 returned `no lockfiles found in .` because it does not recognize `uv.lock`; the frozen export route above is the reproducible CI/local audit path.

Locked reviewed tool versions:

```text
hatchling 1.27.0
hypothesis 6.168.3
pip-audit 2.10.1
pytest 9.1.1
pytest-asyncio 1.4.0
respx 0.23.1
```

## Self-review

- Production diff is limited to `.github/workflows/ci.yml`, `pyproject.toml`, `tests/test_package_policy.py`, and `uv.lock`.
- No approved design, plan, or review file changed.
- `git diff --check` passed.
- Production fix commit was verified with `git show --stat --oneline c14463d`.
- Vulnerability audit completed over the frozen, fully pinned lock export with no known vulnerabilities.
- Policy positive controls and the current repository scan passed.

## Review status

**PENDING INDEPENDENT RE-REVIEW.** The prior PASS claim is withdrawn. Task 1 must not advance to Task 2 until independent spec-compliance and quality/security re-review report zero BLOCKER or MAJOR findings.

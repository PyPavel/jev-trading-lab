# Task 001 Delivery Evidence

## Scope

Initialized the Python 3.12 `jev-trading-lab` package and hardened its package, dependency, CI, and paper-only policy after independent review found major gaps.

## Commit chain

```text
808bf56705f36961fa0c59024f4012dc97f6bd20 build: initialize paper-only trading lab
3cdd7d6                                docs(delivery): record task 001 evidence
c14463db0cee4b46fb27e5536ec2fd123d2fcd30 fix(build): harden paper-only package policy
9be18de                                docs(delivery): update task 001 review evidence
1d78de00212e7a26c5c64cb539813b0e002c199c fix(build): enforce fail-closed package capabilities
fe13916                                docs(delivery): update task 001 security rereview
10433a2ec8211fb444c1775657f4b26503c2b0d5 fix(build): close paper-policy provenance gaps
e48a5578c97155675a0b2ca29d2586a959e2df53 fix(build): reject mutated bindings and bytecode
395ad157a3c6ba01ec47a7286cba58fcfca444ce fix(build): freeze task1 source capability surface
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

## Independent re-review findings

Two independent re-reviews still found major issues after `c14463d`:

1. The syntax-pattern scanner allowed every standard-library import and the top-level `jev_trading_lab` package. This permitted network/process capabilities (`urllib`, `http`, `socket`, `subprocess`, `os.system`, and `importlib`) plus project-local execution modules through import aliases and full paths.
2. Pattern checks could be bypassed through `urllib.request.Request(method="POST")`, `http.client.HTTPSConnection.request`, composed subprocess/curl arguments, dynamic imports, `getattr` name composition, and mapping or keyword URL arguments.
3. Non-UTF8 files under `src/`, `scripts/`, and `ops/` were silently skipped.
4. CI used isolated `uv build`, which could resolve a build environment separately from the frozen synced lock.

## Re-review RED evidence

Mandatory positive controls were added before the implementation change. Command:

```console
uv run pytest tests/test_package_policy.py -k 'fail_closed or ci_uses' -q
```

Observed expected output:

```text
FFFFFFFFFFFFFFFFFFFF                                                     [100%]
20 failed, 32 deselected in 0.25s
```

The failures covered urllib POST plus `urlopen`, `http.client` POST, subprocess curl with composed argv, dynamic `__import__` and `importlib`, project-local broker/wallet/signing/order/execution/live imports, composed `getattr`, mapping and keyword URL arguments, composed shell variables, non-UTF8 files in all three guarded roots, and CI's missing `--no-build-isolation` flag.

## Re-review fixes

Production fix commit:

```text
1d78de00212e7a26c5c64cb539813b0e002c199c fix(build): enforce fail-closed package capabilities
```

Changes:

- Replaced the growing dangerous-spelling scan with exact per-path import and call allowlists for the current `__init__.py` and `cli.py` package only.
- Inspected complete import paths, including names imported by `from` statements; any future production module, import, or call now fails until explicitly authorized under a failing test.
- Restricted project entry points and complete `[tool]` configuration to the current known values while retaining the PEP 503-normalized runtime dependency allowlist.
- Rejected every current `scripts/` and `ops/` file and every non-Python file under `src/`; unreadable/non-UTF8 surfaces therefore fail closed rather than being skipped.
- Changed CI to build from the frozen synced environment with `uv build --no-build-isolation` and added a policy assertion preventing regression to isolated build mode.

## Focused paper-only policy verification

Command:

```console
uv run pytest tests/test_package_policy.py -q
```

Observed output:

```text
........................................                                 [100%]
40 passed in 0.23s
```

This includes all positive controls and a scan of the repository's current runtime/config surfaces.

## Lock, sync, full suite, lint, type, audit, and build verification

Commands were run sequentially and each exited 0:

```console
uv lock --check
uv sync --frozen --all-groups
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv export --frozen --all-groups --no-emit-project --format requirements-txt --output-file /tmp/jev-audit-requirements.txt
uv run pip-audit --strict --requirement /tmp/jev-audit-requirements.txt --no-deps --disable-pip
uv build --no-build-isolation
git diff --check
```

Observed output:

```text
Resolved 51 packages in 1ms
Checked 50 packages in 0.90ms

........................................                                 [100%]
40 passed in 0.23s
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

The successful build used `--no-build-isolation`; a normal isolated build is no longer treated as reproducibility evidence.

`uv export` generated a fully pinned, hashed requirements file from the frozen lock for the reproducible CI/local audit path.

Locked reviewed tool versions:

```text
hatchling 1.27.0
hypothesis 6.168.3
pip-audit 2.10.1
pytest 9.1.1
pytest-asyncio 1.4.0
respx 0.23.1
```

## Provenance re-review findings

A further independent review found four remaining fail-open paths after `1d78de0`:

1. Lexically allowed call names `main` and `app` could be rebound or aliased to `exec`, `__import__`, `os.system`, urllib/http/process functions, or `getattr`, then invoked through the allowed spelling.
2. Arbitrary direct packages could be added to development dependency groups, and their direct lock representations were not checked.
3. `[project.optional-dependencies]` and the corresponding lock section could introduce trading SDKs without violating runtime dependency checks.
4. Production paths beneath `src/**/__pycache__` were skipped, including executable, unreadable, or non-UTF8 source fixtures.

## Provenance RED evidence

Positive controls were added before implementation. Command:

```console
uv run pytest tests/test_package_policy.py -k 'allowed_call_names or dependency_group_rejects or optional_dependencies_are_rejected or production_pycache' -q
```

Observed expected output:

```text
FFFFFFFFFFFFFFF                                                          [100%]
15 failed, 40 deselected in 0.19s
```

The failures covered `main` rebound to `exec`, `__import__`, `os.system`, urllib, HTTP, subprocess, and `getattr`; `app` rebound to `exec`; assignment/alias chains; an unapproved `py-clob-client` dev dependency in `pyproject.toml` and `uv.lock`; `alpaca-py` optional dependencies in both files; and executable non-UTF8 `src/__pycache__/live.py`.

Additional frozen-contract and lock-metadata controls were observed failing before their minimal fixes:

```console
uv run pytest tests/test_package_policy.py -k frozen_source_contract -q
uv run pytest tests/test_package_policy.py::test_lock_metadata_dev_group_rejects_unapproved_direct_dependency -q
```

```text
3 failed, 59 deselected in 0.15s
1 failed in 0.12s
```

These controls cover mutation of allowed imported attributes, mutation of the `app` instance, arbitrary alias assignments, and uv's `[package.metadata.requires-dev]` direct dependency representation.

Regenerated-lock controls were verified against real uv output in temporary projects. Both an unapproved dev group and an optional `alpaca-py` extra produced valid locks (`uv lock --check` exit 0) with project-level `dev-dependencies` and `optional-dependencies` sections, respectively.

## Provenance fixes

Production fix commit:

```text
10433a2ec8211fb444c1775657f4b26503c2b0d5 fix(build): close paper-policy provenance gaps
```

Changes:

- Calls now require allowed provenance: the imported `typer` module, the one allowlisted `typer.Typer()` app binding, or the declared local `main` function. Dynamic execution and introspection calls are explicitly blocked.
- Protected `app`/`main` bindings reject assignment, import aliasing, nested declarations, arguments, deletion, and alias chains. The frozen two-file contract also rejects every assignment except the current app initialization and package version constant.
- Runtime and every dependency group now use exact direct dependency allowlists. uv project-lock `dev-dependencies`, `dependency-groups`, and `metadata.requires-dev` representations are all checked.
- Optional dependency tables are rejected in both `pyproject.toml` and the project package's uv lock section.
- Production source enumeration uses Git tracked plus non-ignored untracked paths for the real repository, so ordinary ignored interpreter caches do not make tests flaky. Explicit fixture roots inspect their complete source tree and reject any `__pycache__` path, including unreadable/non-UTF8/executable content.
- Invalid or unreadable lock TOML now records a policy violation rather than escaping the scanner.

## Final verification

Commands were run sequentially from the repository root and exited 0:

```console
uv lock
uv lock --check
uv sync --frozen --all-groups
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv export --frozen --all-groups --no-emit-project --format requirements-txt --output-file /tmp/jev-audit-requirements.txt
uv run pip-audit --strict --requirement /tmp/jev-audit-requirements.txt --no-deps --disable-pip
uv build --no-build-isolation
git diff --check
```

Observed output:

```text
Resolved 51 packages in 1ms
Resolved 51 packages in 1ms
Checked 50 packages in 0.48ms
..............................................................           [100%]
62 passed in 0.33s
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

`uv lock` regenerated no tracked change. `git diff --check` produced no output.

## Self-review

- Latest production fix is limited to `tests/test_package_policy.py`; delivery evidence is isolated in this document.
- No approved design, plan, or review file changed.
- Production fix commit was verified with `git show --stat --oneline 10433a2`.
- Vulnerability audit completed over the frozen, fully pinned lock export with no known vulnerabilities.
- All 62 tests, including mandatory positive controls and current-repository policy scan, passed.
- Delivery evidence remains pending independent review; no Task 2 work started.

## Remaining spec-review MAJOR findings

The adversarial spec re-review found two concrete bypasses after `10433a2`:

1. Binding provenance could still be changed by binding constructs outside direct assignments. In particular, a top-level `for typer in [Evil]` target could replace the approved import before a lexically allowed `typer.Typer()` call, and nested `typer.Typer = exec` could mutate the approved module object.
2. Git-based source enumeration excluded ignored `src/**/__pycache__`, `.pyc`, and `.pyo` paths. A physically present unchecked-hash bytecode file could therefore execute while the scanner passed.

## Bytecode review RED evidence

Mandatory positive controls were added before the implementation change. Commands:

```console
PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_package_policy.py -k 'binding_mutation' -q
PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_package_policy.py -k 'git_ignored_pycache' -q
uv run pytest tests/test_package_policy.py::test_test_process_disables_bytecode_writes -q
uv run pytest tests/test_package_policy.py::test_ci_uses_immutable_actions_and_locked_audit -q
```

Observed expected output:

```text
FFFFF                                                                    [100%]
5 failed, 62 deselected in 0.19s

FF                                                                       [100%]
2 failed, 65 deselected in 0.16s

F                                                                        [100%]
1 failed in 0.14s

F                                                                        [100%]
1 failed in 0.14s
```

The binding failures covered for-target rebinding of `typer`, nested imported-attribute mutation, `with ... as`, named-expression, and exception targets. The physical-tree failures used Git-initialized temporary repositories with the production-equivalent `__pycache__/` and `*.py[cod]` ignores, and proved both non-UTF8 `live.py` and valid malicious unchecked-hash `.pyc` were invisible to Git enumeration. The remaining RED controls proved ordinary pytest and CI had no bytecode-write suppression.

## Bytecode review fixes

Production fix commit:

```text
e48a5578c97155675a0b2ca29d2586a959e2df53 fix(build): reject mutated bindings and bytecode
```

Changes:

- Added a fail-closed AST binding contract for the current two production files. Only the exact package-version assignment, Typer app assignment, and current top-level functions are accepted; other name/delete targets, attribute mutation, aliases, arguments, exception targets, nested/class declarations, `global`, and `nonlocal` are violations. Future source constructs require RED controls before allowlist expansion.
- Added physical `src/` traversal before Git filtering. It independently rejects every `__pycache__`, `.pyc`, `.pyo`, unreadable path, and non-UTF8 physical file, including ignored files.
- Added Git-initialized ignored-cache fixtures that create non-UTF8 source and real unchecked-hash malicious bytecode with `py_compile`.
- Added repository pytest bootstrap and CI `PYTHONDONTWRITEBYTECODE=1` protection. Existing generated `src/jev_trading_lab/__pycache__` was removed explicitly before final verification; the policy never deletes suspicious caches.

## Bytecode review final verification

Commands were run sequentially from the repository root and exited 0:

```console
uv lock --check
uv sync --frozen --all-groups
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv export --frozen --all-groups --no-emit-project --format requirements-txt --output-file /tmp/jev-audit-requirements.txt
uv run pip-audit --strict --requirement /tmp/jev-audit-requirements.txt --no-deps --disable-pip
uv build --no-build-isolation
git diff --check
```

Observed output:

```text
Resolved 51 packages in 1ms
Checked 50 packages in 0.49ms
....................................................................     [100%]
68 passed in 0.37s
All checks passed!
6 files already formatted
Success: no issues found in 2 source files
WARNING:pip_audit._cli:--no-deps is supported, but users are encouraged to fully hash their pinned dependencies
WARNING:pip_audit._cli:Consider using a tool like `pip-compile`: https://pip-tools.readthedocs.io/en/latest/#using-hashes
No known vulnerabilities found
Building source distribution...
Building wheel from source distribution...
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl
```

`git diff --check` produced no output. Physical `src/` checks after the gates found no `__pycache__` directory or `.pyc` file.

## Frozen-source review findings

A later adversarial review found that the increasingly complex AST and Git-enumeration policy still had MAJOR bypasses:

1. `match` capture could rebind the approved `app` name without an AST store target recognized by the scanner.
2. Subscript mutation such as `typer.__dict__["Typer"] = exec` could alter an approved imported capability without an attribute-store node.
3. Git-ignored readable Python under `src/` was physically present and executable but omitted from policy enumeration.
4. A Git-ignored symlink under `src/` could point outside the approved tree to a package or bytecode surface.

The old scanner bypasses were reproduced directly from commit `42247b2` before replacement:

```text
match-capture BYPASS []
nested-subscript-mutation BYPASS []
```

## Frozen-source RED evidence

New policy controls were run before implementation. Commands:

```console
PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_package_policy.py::test_exact_source_hash_rejects_current_ast_bypasses -q
PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_package_policy.py::test_git_ignored_readable_python_is_rejected -q
PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_package_policy.py::test_git_ignored_symlink_to_external_package_is_rejected -q
PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_package_policy.py::test_git_ignored_pycache_rejects_unchecked_hash_bytecode -q
```

Observed output:

```text
2 failed in 0.17s
1 failed in 0.17s
1 failed in 0.18s
1 passed in 0.12s
```

The hash assertions failed because the old policy had no exact-source hashes. The ignored readable Python and ignored symlink assertions failed because Git enumeration omitted both physical paths. The pre-existing ignored unchecked-hash bytecode control already passed after `e48a557`; it was retained and strengthened to require both unapproved-file and bytecode violations.

## Frozen-source fix

Production fix commit:

```text
395ad157a3c6ba01ec47a7286cba58fcfca444ce fix(build): freeze task1 source capability surface
```

Changes:

- Deleted the ad-hoc AST import, call, provenance, and binding analyzer.
- Added one physical `lstat`/`scandir` walk of `src/` independent of Git ignore, without following symlinks.
- Approved only directories `src` and `src/jev_trading_lab` and files `src/jev_trading_lab/__init__.py` and `src/jev_trading_lab/cli.py`.
- Froze exact SHA-256 values for both approved files. Any source mutation now fails without semantic interpretation.
- Rejected symlinks, extra files/directories, `__pycache__`, `.pyc`/`.pyo`, unreadable/non-UTF8 files, special files, and missing approved paths.
- Retained exact dependency-group, lock, optional-dependency, entry-point, tool-config, workflow, operational-path, audit, and build checks.
- Retained positive controls for arbitrary source mutation, both current AST bypasses, ignored readable Python, ignored external symlinks, ignored unchecked-hash bytecode, nonUTF8/unreadable/special paths, and physical extras.

This is a current-source change-control gate, not a runtime sandbox. Future implementation tasks must deliberately update the approved source path set and hashes under their own RED policy tests and review.

## Frozen-source final verification

Commands were run sequentially from the repository root after the final code change and exited 0:

```console
uv lock --check
uv sync --frozen --all-groups
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv export --frozen --all-groups --no-emit-project --format requirements-txt --output-file /tmp/jev-audit-requirements.txt
uv run pip-audit --strict --requirement /tmp/jev-audit-requirements.txt --no-deps --disable-pip
uv build --no-build-isolation
git diff --check
```

Observed output:

```text
Resolved 51 packages in 1ms
Checked 50 packages in 0.49ms
...............................                                          [100%]
31 passed in 0.25s
All checks passed!
6 files already formatted
Success: no issues found in 2 source files
No known vulnerabilities found
Building source distribution...
Building wheel from source distribution...
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl
```

`git diff --check` produced no output. A separate `lstat`/`scandir` verification found exactly `src`, `src/jev_trading_lab`, `src/jev_trading_lab/__init__.py`, and `src/jev_trading_lab/cli.py`, with no symlink or special file.

## Review status

**PENDING FINAL INDEPENDENT RE-REVIEW.** The frozen-source change addresses the latest MAJOR bypasses, but Task 1 must not advance to Task 2 until independent spec-compliance and quality/security review reports zero BLOCKER or MAJOR findings.

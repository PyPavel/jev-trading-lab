# Task 001 Delivery Evidence

## Scope

Initialized the Python 3.12 `jev-trading-lab` package with a locked, paper-only runtime surface, CI policy checks, and no real-money execution dependencies or commands.

## RED evidence

Each required node was executed independently and failed with an assertion for the intended missing behavior. Collection and import errors were not accepted.

### Package import

Command:

```console
pytest tests/test_package_policy.py::test_package_imports -q
```

Expected and observed assertion failure:

```text
Pytest: 0 passed, 1 failed
Failures:
1. [FAIL] test_package_imports
     tests/test_package_policy.py:15: in test_package_imports
     assert importlib.util.find_spec("jev_trading_lab") is not None
     E   AssertionError: assert None is not None
```

The package shell was then created and the node passed:

```text
Pytest: 1 passed
```

### Prohibited execution surface

Command:

```console
pytest tests/test_package_policy.py::test_no_live_execution_surface -q
```

Expected and observed assertion failure:

```text
Pytest: 0 passed, 1 failed
Failures:
1. [FAIL] test_no_live_execution_surface
     tests/test_package_policy.py:66: in test_no_live_execution_surface
     tests/test_package_policy.py:29: in _project_data
     assert project_file.is_file(), "pyproject.toml must define the package policy"
```

A minimal project declaration was then created and the node passed:

```text
Pytest: 1 passed
```

The final policy test scans Python AST names under `src`, `scripts`, and `ops`, plus dependency and entry-point declarations in `pyproject.toml` and `uv.lock`. Scanner terms are assembled rather than stored as contiguous literals, preventing rule self-matches. Parametrized positive controls prove every prohibited term is detected.

### Locked build configuration

Command:

```console
pytest tests/test_package_policy.py::test_locked_build_configuration -q
```

Expected and observed assertion failure:

```text
Pytest: 0 passed, 1 failed
Failures:
1. [FAIL] test_locked_build_configuration
     tests/test_package_policy.py:101: in test_locked_build_configuration
     assert project_data.get("build-system", {}).get("build-backend") == "hatchling.build"
     E   AssertionError: assert None == 'hatchling.build'
```

After adding the Hatchling source layout, future SQL/template package-data globs, dev dependency group, and generated lockfile, the node passed:

```text
Using CPython 3.12.13
Creating virtual environment at: .venv
Resolved 19 packages in 341ms
Installed 18 packages in 19ms
1 passed in 0.02s
```

## GREEN and focused verification

A CLI smoke check found the initial empty Typer root could not render help. A regression test was added and observed failing with:

```text
RuntimeError: Could not get a command for this Typer instance
1 failed in 0.04s
```

The minimal callback fix made the node pass:

```text
1 passed in 0.05s
```

Required focused command:

```console
uv sync --all-groups && uv run pytest tests/test_package_policy.py -q && uv build
```

Observed output:

```text
Resolved 19 packages in 1ms
Checked 18 packages in 0.37ms
................                                                         [100%]
16 passed in 0.08s
Building source distribution...
Building wheel from source distribution...
Successfully built dist/jev_trading_lab-0.1.0.tar.gz
Successfully built dist/jev_trading_lab-0.1.0-py3-none-any.whl
```

## Full quality gate

Command:

```console
uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src
```

Observed output:

```text
................                                                         [100%]
16 passed in 0.08s
All checks passed!
5 files already formatted
Success: no issues found in 2 source files
```

## Production commit

```text
808bf56705f36961fa0c59024f4012dc97f6bd20 build: initialize paper-only trading lab
```

Committed paths:

```text
.github/workflows/ci.yml
README.md
THIRD_PARTY_NOTICES.md
pyproject.toml
src/jev_trading_lab/__init__.py
src/jev_trading_lab/cli.py
tests/test_package_policy.py
uv.lock
```

## Self-review

Verdict: PASS with zero blocking or major findings after the CLI regression fix.

Checks performed:

- production diff contains exactly the eight task paths;
- resolved runtime tree contains only the project, Typer, and Typer's display dependencies;
- wheel contains only package modules and distribution metadata;
- static secret/dangerous-call scan returned no matches;
- `jev-lab --help` succeeds and exposes only a paper-research root with no operational commands;
- no design, plan, or review file changed.

Clean-wheel migration and render smoke remain intentionally deferred until those resources exist, as required by the task.

## Final status

PASS. Production commit and delivery evidence are separate commits. Post-delivery verification requires the full quality gate to remain green and `git status --short` to return no output.

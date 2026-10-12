#!/usr/bin/env python3
"""Fail closed on reviewed Task 1 surfaces before dependency tooling executes."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).absolute().parent.parent
SCRIPT = ROOT / "scripts/policy_preflight.py"
EXPECTED_LOCK_HASH = "4535dbd779b3c903f57235bb5f3a5c5ce12e3bf13f1b8e01da0576f97df212ac"
PYPI_SOURCE = {"registry": "https://pypi.org/simple"}
EXPECTED_SOURCE_DIRS = frozenset(
    {
        "src",
        "src/jev_trading_lab",
        "src/jev_trading_lab/artifacts",
        "src/jev_trading_lab/core",
        "src/jev_trading_lab/persistence",
        "src/jev_trading_lab/persistence/schema",
        "src/jev_trading_lab/polymarket",
        "src/jev_trading_lab/stocks",
    }
)
EXPECTED_CONFIG_DIRS = frozenset({"config"})
EXPECTED_CONFIG_HASHES = {
    "config/phase1.example.json": (
        "091c49428f15eb99fcea3a54b444b9fde929681e5ddfeaaadc4d4ebfa94438db"
    )
}
EXPECTED_OPERATIONAL_HASHES = {
    "scripts/wheel_migration_smoke.py": (
        "cc9789a277c503e229973cd2eca8662031a451cf3787a2e02143837366bfe68b"
    )
}
EXPECTED_SOURCE_HASHES = {
    "src/jev_trading_lab/artifacts/xnys-schedule-2020-2036.json": (
        "ade5009ba938836a5d17e298b0e704fc3635b8b1eef329ba4b43cba5c40fc978"
    ),
    "src/jev_trading_lab/__init__.py": (
        "583ff05fc923ac30ef5cd0e9c44271e53746a65e312f53f112ec1b49e827647b"
    ),
    "src/jev_trading_lab/cli.py": (
        "5cace8245387ec229b0bfa56fd062c366c57c838b42b681aa6228afaac9dda88"
    ),
    "src/jev_trading_lab/core/ids.py": (
        "0e73742a406e112f269e7b8d5fd205c448eb4fbf49541cab3c669bf7b4dc0dd4"
    ),
    "src/jev_trading_lab/core/manifest.py": (
        "43c98f3eb07039c7ad6581dd23a533e60e078e65c3e24d6963f686c8f6ff70ba"
    ),
    "src/jev_trading_lab/core/run_window.py": (
        "f90c328655cec51fafcb9850f8a9a8ccecf24f78398ff746814118614e277e51"
    ),
    "src/jev_trading_lab/core/observations.py": (
        "5c6f9d3c76c7f766b3170513dae374873b1df5970b4590e1ffa9c66367be2252"
    ),
    "src/jev_trading_lab/core/time.py": (
        "edbc66ad7acf0bfb2da0a2d4038cbdc914474f33482a723161155ea81744312e"
    ),
    "src/jev_trading_lab/core/values.py": (
        "806e4d4681da763cd78efa8b636ad51be74f61b425c9ad833b44781d43b10087"
    ),
    "src/jev_trading_lab/persistence/__init__.py": (
        "be5cdea878ba9575ad6a83bc43819e3c1cbaecdf8ad351b2d8374f94b6c496e9"
    ),
    "src/jev_trading_lab/persistence/core_repo.py": (
        "b4c2a856b006ee6bde860e8abff04c209915eb420e1c55425805e3e25042d133"
    ),
    "src/jev_trading_lab/persistence/blobs.py": (
        "708978037457d6656af09f6c635f34f4519f55d2e4afdec438ff28382e9a7bce"
    ),
    "src/jev_trading_lab/persistence/journal.py": (
        "74c92b09eeb9c10e713c99354df7d40e43c2d90e7de0088968e95197f1a0dce2"
    ),
    "src/jev_trading_lab/persistence/db.py": (
        "6eddc63cafb5ca7fdb965009c2c8b31bf7e8baa8cc4a90d158a44257aa6d7560"
    ),
    "src/jev_trading_lab/persistence/migrations.py": (
        "6848e5a2c83d91867649079acc32fbe0df6f6059e3d0e03c274f3d86c7d82255"
    ),
    "src/jev_trading_lab/persistence/migration_helper.py": (
        "d3187180b1319fd6100c8c96d002acf0e5134a87eea87f5ae49b3aec37977263"
    ),
    "src/jev_trading_lab/persistence/schema/__init__.py": (
        "5f19444569734cd60895d22219e7e2481fd00efb6054fb1d4cf9f7b38e26e4a5"
    ),
    "src/jev_trading_lab/persistence/schema/001_migration_metadata.sql": (
        "1be6dbbcb20a6ab8efe016172d786280470fc7a450f403825e2a61e199a1cb48"
    ),
    "src/jev_trading_lab/persistence/schema/002_core_entities.sql": (
        "9026b984313d972dec6c3db8b442e4324f2a7f02ec71bd555c0c17f8ed362c54"
    ),
    "src/jev_trading_lab/persistence/schema/003_append_only_guards.sql": (
        "d18492f31e6c0948fa87f11927f2bc4fb2a5a41ffb45c27c9e966339d5e35995"
    ),
    "src/jev_trading_lab/polymarket/__init__.py": (
        "2c8708e587ec6804d9c15736661a265c5b441cc3ce83b0b152e15f5a2fcf6739"
    ),
    "src/jev_trading_lab/polymarket/ai.py": (
        "eae325e131fac2ebfe0f6ff3f10d4b2d576e746f98b6c4cbcda143133abc358a"
    ),
    "src/jev_trading_lab/polymarket/call_journal.py": (
        "530a5c905ea3cb3beb3e42e80670622403a1729fa4d4151ec9c781e14139f9a6"
    ),
    "src/jev_trading_lab/polymarket/client.py": (
        "3aa790d4772b8ffed8d5f229a498173b656ef20d8059638de5cf10fd4354ff1d"
    ),
    "src/jev_trading_lab/polymarket/control.py": (
        "cadd87042c9293c2fd97d0017f532b06500d2afd23b07a9bd16c5e7bceef2b8d"
    ),
    "src/jev_trading_lab/polymarket/models.py": (
        "4aa32a806efa5f6e8a02c1db7d62f93fa9a57c34ec89728384613367affd8ed7"
    ),
    "src/jev_trading_lab/polymarket/operations.py": (
        "59ecc2a70b1c3277abd0c16d785f4500ad96427fa581418a4955b57e67c1c179"
    ),
    "src/jev_trading_lab/polymarket/metrics_render.py": (
        "e5bd5bd1491e2185a4decd7059d29ca4c1c2beae7aed0c84f5e85300eeb23fd4"
    ),
    "src/jev_trading_lab/polymarket/metrics_schema.py": (
        "ff1451f62060669bffa9018796a56c15a54bd92f81b689cf9fd392653a6ed9d3"
    ),
    "src/jev_trading_lab/polymarket/paper.py": (
        "f17e8c6cef480cf1926b04999fe3097079bb473cbb32a188a335cd89e287003e"
    ),
    "src/jev_trading_lab/polymarket/pipeline.py": (
        "2f362f017c05c5bebe816f310f7e4bfdae4e4566ea07d8536dc245950bc18a62"
    ),
    "src/jev_trading_lab/polymarket/store.py": (
        "5ab501c3f32b1b3267df376bf103fcfc57747bf1f96532333d11d461badefc43"
    ),
    "src/jev_trading_lab/stocks/__init__.py": (
        "47549384d0ac5349e974d9c72796b1a48c439b574630a4b1001d6b48b17d0156"
    ),
    "src/jev_trading_lab/stocks/calendar.py": (
        "9536ea64aba216d7afcea434ba1e73634eca501095e72229797b45b8e2fed453"
    ),
}
EXPECTED_PROJECT = {
    "name": "jev-trading-lab",
    "version": "0.1.0",
    "description": "Paper-only evidence-first market research lab",
    "readme": "README.md",
    "requires-python": ">=3.12",
    "dependencies": [
        "exchange-calendars==4.11.2",
        "pydantic>=2.12,<3",
        "typer>=0.19,<1",
    ],
    "classifiers": [
        "Development Status :: 2 - Pre-Alpha",
        "Intended Audience :: Science/Research",
        "Programming Language :: Python :: 3.12",
    ],
    "scripts": {"jev-lab": "jev_trading_lab.cli:app"},
}
EXPECTED_DEPENDENCY_GROUPS = {
    "dev": [
        "hatchling==1.27.0",
        "hypothesis>=6.140,<7",
        "mypy>=1.18,<2",
        "pip-audit>=2.9,<3",
        "pytest>=9.0.3,<10",
        "pytest-asyncio>=1.2,<2",
        "respx>=0.22,<1",
        "ruff>=0.13,<1",
    ]
}
EXPECTED_TOOL = {
    "hatch": {
        "build": {
            "exclude": [
                "/.git",
                "/.github",
                "/.mypy_cache",
                "/.pytest_cache",
                "/.ruff_cache",
                "/.venv",
                "/build",
                "/dist",
                "/**/__pycache__",
                "/**/*.py[cod]",
            ],
            "targets": {
                "sdist": {
                    "include": [
                        "/README.md",
                        "/THIRD_PARTY_NOTICES.md",
                        "/pyproject.toml",
                        "/src/jev_trading_lab",
                    ]
                },
                "wheel": {
                    "packages": ["src/jev_trading_lab"],
                    "include": [
                        "src/jev_trading_lab/artifacts/*.json",
                        "src/jev_trading_lab/**/*.sql",
                        "src/jev_trading_lab/templates/**/*",
                    ],
                },
            },
        }
    },
    "pytest": {"ini_options": {"testpaths": ["tests"]}},
    "ruff": {
        "target-version": "py312",
        "line-length": 88,
        "extend-exclude": ["docs"],
        "lint": {"select": ["E", "F", "I", "UP"]},
    },
    "mypy": {"python_version": "3.12", "strict": True, "files": ["src"]},
}
EXPECTED_PROJECT_PACKAGE = {
    "name": "jev-trading-lab",
    "version": "0.1.0",
    "source": {"editable": "."},
    "dependencies": [
        {"name": "exchange-calendars"},
        {"name": "pydantic"},
        {"name": "typer"},
    ],
    "dev-dependencies": {
        "dev": [
            {"name": "hatchling"},
            {"name": "hypothesis"},
            {"name": "mypy"},
            {"name": "pip-audit"},
            {"name": "pytest"},
            {"name": "pytest-asyncio"},
            {"name": "respx"},
            {"name": "ruff"},
        ]
    },
    "metadata": {
        "requires-dist": [
            {"name": "exchange-calendars", "specifier": "==4.11.2"},
            {"name": "pydantic", "specifier": ">=2.12,<3"},
            {"name": "typer", "specifier": ">=0.19,<1"},
        ],
        "requires-dev": {
            "dev": [
                {"name": "hatchling", "specifier": "==1.27.0"},
                {"name": "hypothesis", "specifier": ">=6.140,<7"},
                {"name": "mypy", "specifier": ">=1.18,<2"},
                {"name": "pip-audit", "specifier": ">=2.9,<3"},
                {"name": "pytest", "specifier": ">=9.0.3,<10"},
                {"name": "pytest-asyncio", "specifier": ">=1.2,<2"},
                {"name": "respx", "specifier": ">=0.22,<1"},
                {"name": "ruff", "specifier": ">=0.13,<1"},
            ]
        },
    },
}


def _workflow_bytes(script_hash: str) -> bytes:
    return f'''name: CI

on:
  push:
  pull_request:

permissions:
  contents: read

env:
  PYTHONDONTWRITEBYTECODE: "1"

jobs:
  quality:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4
        with:
          fetch-depth: 0
          persist-credentials: false
      - name: Verify policy preflight
        run: echo "{script_hash}  scripts/policy_preflight.py" | sha256sum -c -
      - name: Run policy preflight before dependency tooling
        run: python3 -I -B scripts/policy_preflight.py
      - uses: astral-sh/setup-uv@d0d8abe699bfb85fec6de9f7adb5ae17292296ff # v6
        with:
          version: "0.11.7"
      - uses: gitleaks/gitleaks-action@dcedce43c6f43de0b836d1fe38946645c9c638dc # v2
        env:
          GITHUB_TOKEN: ${{{{ secrets.GITHUB_TOKEN }}}}
      - run: uv python install 3.12
      - run: uv lock --check
      - run: uv sync --frozen --all-groups
      - run: uv run pytest -q
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run mypy src
      - run: >
          uv export --frozen --all-groups --no-emit-project
          --format requirements-txt --output-file audit-requirements.txt
      - run: >
          uv run pip-audit --strict --requirement audit-requirements.txt
          --no-deps --disable-pip
      - run: uv build --no-build-isolation
      - run: JEV_REQUIRE_BUILT_ARTIFACTS=1 uv run pytest tests/test_package_policy.py -q
      - run: uv run python -B scripts/wheel_migration_smoke.py
      # Clean-wheel render smoke becomes mandatory when those resources exist.
'''.encode()


def _read_toml(relative: str, violations: list[str]) -> dict[str, Any]:
    path = ROOT / relative
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        violations.append(f"{relative}: unreadable or invalid UTF-8 TOML: {error}")
        return {}


def _check_pyproject(violations: list[str]) -> None:
    data = _read_toml("pyproject.toml", violations)
    expected = {
        "build-system": {
            "requires": ["hatchling==1.27.0"],
            "build-backend": "hatchling.build",
        },
        "project": EXPECTED_PROJECT,
        "dependency-groups": EXPECTED_DEPENDENCY_GROUPS,
        "tool": EXPECTED_TOOL,
    }
    if data != expected:
        violations.append(
            "pyproject.toml: content differs from exact approved build, dependency, "
            "entry-point, and tool contract"
        )


def _check_lock(violations: list[str]) -> None:
    lock_path = ROOT / "uv.lock"
    try:
        lock_content = lock_path.read_bytes()
    except OSError as error:
        violations.append(f"uv.lock: unreadable: {error}")
        return
    if hashlib.sha256(lock_content).hexdigest() != EXPECTED_LOCK_HASH:
        violations.append(
            "uv.lock: SHA-256 does not match exact reviewed dependency lock"
        )
        return
    try:
        data = tomllib.loads(lock_content.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        violations.append(f"uv.lock: unreadable or invalid UTF-8 TOML: {error}")
        return
    packages = data.get("package")
    if not isinstance(packages, list):
        violations.append("uv.lock: package list is missing")
        return
    project_packages = [
        package
        for package in packages
        if isinstance(package, dict) and package.get("name") == "jev-trading-lab"
    ]
    if project_packages != [EXPECTED_PROJECT_PACKAGE]:
        violations.append(
            "uv.lock: direct project requirements differ from exact approved "
            "representation"
        )
    for package in packages:
        if not isinstance(package, dict):
            violations.append("uv.lock: package entry is not a table")
            continue
        name = package.get("name", "<unknown>")
        if name == "jev-trading-lab":
            continue
        if package.get("source") != PYPI_SOURCE:
            violations.append(
                f"uv.lock: third-party package {name!r} has unapproved "
                "dependency source"
            )


def _walk_exact_tree(
    root_name: str,
    allowed_dirs: set[str] | frozenset[str],
    allowed_hashes: dict[str, str],
    violations: list[str],
) -> None:
    expected = set(allowed_dirs) | set(allowed_hashes)
    seen: set[str] = set()
    pending = [ROOT / root_name]
    while pending:
        path = pending.pop()
        try:
            relative = path.relative_to(ROOT).as_posix()
            metadata = path.lstat()
        except (OSError, ValueError) as error:
            violations.append(f"{path}: approved tree path is unreadable: {error}")
            continue
        seen.add(relative)
        if stat.S_ISLNK(metadata.st_mode):
            violations.append(f"{relative}: symlink is not approved")
        elif stat.S_ISDIR(metadata.st_mode):
            if relative not in allowed_dirs:
                violations.append(f"{relative}: directory is not approved")
            if not metadata.st_mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH):
                violations.append(f"{relative}: directory is unreadable")
                continue
            try:
                with os.scandir(path) as entries:
                    pending.extend(Path(entry.path) for entry in entries)
            except OSError as error:
                violations.append(f"{relative}: directory is unreadable: {error}")
        elif stat.S_ISREG(metadata.st_mode):
            approved_hash = allowed_hashes.get(relative)
            if approved_hash is None:
                violations.append(f"{relative}: file is not approved")
                continue
            if not metadata.st_mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH):
                violations.append(f"{relative}: file is unreadable")
                continue
            try:
                content = path.read_bytes()
                content.decode("utf-8")
            except (OSError, UnicodeError) as error:
                violations.append(f"{relative}: file is not readable UTF-8: {error}")
                continue
            if hashlib.sha256(content).hexdigest() != approved_hash:
                violations.append(
                    f"{relative}: SHA-256 does not match approved content"
                )
        else:
            violations.append(f"{relative}: special file is not approved")
    for relative in sorted(expected - seen):
        violations.append(f"{relative}: approved path is missing")


def _check_workflow_and_trees(violations: list[str]) -> None:
    try:
        script_hash = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    except OSError as error:
        violations.append(f"scripts/policy_preflight.py: unreadable: {error}")
        return
    workflow = ROOT / ".github/workflows/ci.yml"
    try:
        workflow_content = workflow.read_bytes()
    except OSError as error:
        violations.append(f".github/workflows/ci.yml: unreadable: {error}")
    else:
        expected_workflow = _workflow_bytes(script_hash)
        if workflow_content != expected_workflow:
            expected_hash = hashlib.sha256(expected_workflow).hexdigest()
            violations.append(
                ".github/workflows/ci.yml: content/SHA-256 differs from exact "
                f"approved workflow ({expected_hash})"
            )
    _walk_exact_tree("src", EXPECTED_SOURCE_DIRS, EXPECTED_SOURCE_HASHES, violations)
    _walk_exact_tree("config", EXPECTED_CONFIG_DIRS, EXPECTED_CONFIG_HASHES, violations)
    _walk_exact_tree(
        "scripts",
        {"scripts"},
        {
            "scripts/policy_preflight.py": script_hash,
            **EXPECTED_OPERATIONAL_HASHES,
        },
        violations,
    )
    ops = ROOT / "ops"
    try:
        ops.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        violations.append(f"ops: operational path is unreadable: {error}")
    else:
        violations.append("ops: operational path is not approved")


def main() -> int:
    violations: list[str] = []
    _check_lock(violations)
    _check_pyproject(violations)
    _check_workflow_and_trees(violations)
    if violations:
        for violation in violations:
            print(f"policy preflight: {violation}", file=sys.stderr)
        return 1
    print("Policy preflight passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
EXPECTED_LOCK_HASH = "463b2ad06b3fcc3af5f7507dd8185b6c7397145a38f2bf9e4f9e65cb5bdad538"
PYPI_SOURCE = {"registry": "https://pypi.org/simple"}
EXPECTED_SOURCE_DIRS = frozenset(
    {"src", "src/jev_trading_lab", "src/jev_trading_lab/core"}
)
EXPECTED_CONFIG_DIRS = frozenset({"config"})
EXPECTED_CONFIG_HASHES = {
    "config/phase1.example.json": (
        "e690c1b7c8df2bd6c9d7be4b3ff9edc5266bb639eacf4ab23e9dd969006307a8"
    )
}
EXPECTED_SOURCE_HASHES = {
    "src/jev_trading_lab/__init__.py": (
        "583ff05fc923ac30ef5cd0e9c44271e53746a65e312f53f112ec1b49e827647b"
    ),
    "src/jev_trading_lab/cli.py": (
        "e1018e68f8902660a347c842866c321a42bad08d3f332cdf7f32bf0029cc84d9"
    ),
    "src/jev_trading_lab/core/ids.py": (
        "0e73742a406e112f269e7b8d5fd205c448eb4fbf49541cab3c669bf7b4dc0dd4"
    ),
    "src/jev_trading_lab/core/manifest.py": (
        "c60c589ded4a97e4a7350cd527d68d922f5ae3357ce23059c6953b0926433844"
    ),
    "src/jev_trading_lab/core/observations.py": (
        "dbad9fa1099b527a640e68fc105641757ef2ef2fd68a504e208dda06958391ec"
    ),
    "src/jev_trading_lab/core/time.py": (
        "edbc66ad7acf0bfb2da0a2d4038cbdc914474f33482a723161155ea81744312e"
    ),
    "src/jev_trading_lab/core/values.py": (
        "806e4d4681da763cd78efa8b636ad51be74f61b425c9ad833b44781d43b10087"
    ),
}
EXPECTED_PROJECT = {
    "name": "jev-trading-lab",
    "version": "0.1.0",
    "description": "Paper-only evidence-first market research lab",
    "readme": "README.md",
    "requires-python": ">=3.12",
    "dependencies": ["pydantic>=2.12,<3", "typer>=0.19,<1"],
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
    "dependencies": [{"name": "pydantic"}, {"name": "typer"}],
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
        run: python3 -I scripts/policy_preflight.py
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
      # Clean-wheel migration/render smoke becomes mandatory when those resources exist.
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
        {"scripts/policy_preflight.py": script_hash},
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

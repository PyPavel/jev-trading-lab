from __future__ import annotations

import ast
import importlib
import importlib.util
import re
import stat
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def test_package_imports() -> None:
    sys.path.insert(0, str(SRC))
    try:
        assert importlib.util.find_spec("jev_trading_lab") is not None
        assert importlib.import_module("jev_trading_lab") is not None
    finally:
        sys.path.remove(str(SRC))


def test_cli_root_describes_paper_research() -> None:
    from jev_trading_lab.cli import app

    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "paper research" in result.output.casefold()


def _project_data(root: Path = ROOT) -> dict[str, Any]:
    project_file = root / "pyproject.toml"
    assert project_file.is_file(), "pyproject.toml must define the package policy"
    return tomllib.loads(project_file.read_text(encoding="utf-8"))


def _policy_terms() -> tuple[str, ...]:
    return (
        "bro" + "ker",
        "wal" + "let",
        "private" + "_key",
        "sign" + "ing",
        "sign" + "_order",
        "submit" + "_order",
        "create" + "_order",
        "live" + "_order",
        "web" + "3",
        "cc" + "xt",
        "eth" + "_account",
        "py_" + "clob_client",
    )


def _python_surface(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.casefold() for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.casefold())
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name.casefold())
        elif isinstance(node, ast.Attribute):
            names.add(node.attr.casefold())
    return names


def _normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _requirement_name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    assert match, f"invalid requirement: {requirement}"
    return _normalize_name(match.group())


def _dangerous_spelling(value: str) -> str | None:
    normalized = re.sub(r"[-_.\s]+", "-", value).casefold()
    compact = normalized.replace("-", "")
    blocked = {
        "bro" + "ker",
        "wal" + "let",
        "sign" + "ing",
        "private" + "key",
        "sign" + "order",
        "submit" + "order",
        "create" + "order",
        "live" + "order",
        "py" + "clobclient",
        "eth" + "account",
        "alpaca" + "py",
        "ib" + "insync",
        "web" + "3",
        "cc" + "xt",
    }
    return value if any(term in compact for term in blocked) else None


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_string(node.left)
        right = _literal_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _python_policy_violations(path: Path, root: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    allowed_imports = set(sys.stdlib_module_names) | {"jev_trading_lab", "typer"}
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports = [node.module]
        else:
            imports = []
        for imported in imports:
            top_level = imported.split(".", maxsplit=1)[0]
            if top_level not in allowed_imports:
                violations.append(
                    f"{path.relative_to(root)}: unauthorized import {imported}"
                )

        candidates: list[str] = []
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            candidates.append(node.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            candidates.append(node.name)
        elif isinstance(node, ast.Attribute):
            candidates.append(node.attr)
        for candidate in candidates:
            if dangerous := _dangerous_spelling(candidate):
                violations.append(
                    f"{path.relative_to(root)}: prohibited surface {dangerous}"
                )

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr.casefold() == "post":
                for argument in node.args:
                    literal = _literal_string(argument)
                    if literal and re.search(
                        r"/(?:v\d+/)?orders?(?:[/?#]|$)", literal, re.I
                    ):
                        violations.append(
                            f"{path.relative_to(root)}: direct POST to orders endpoint "
                            f"{literal}"
                        )
    return violations


def _nested_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _nested_strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _nested_strings(item)]
    return []


def _runtime_policy_violations(root: Path) -> list[str]:
    project_data = _project_data(root)
    allowed_runtime = {"typer"}
    runtime_requirements = project_data.get("project", {}).get("dependencies", [])
    runtime_names = {
        _requirement_name(requirement) for requirement in runtime_requirements
    }
    violations = [
        f"pyproject.toml: runtime dependency not allowlisted: {name}"
        for name in sorted(runtime_names - allowed_runtime)
    ]
    for entry_point in project_data.get("project", {}).get("scripts", {}):
        if dangerous := _dangerous_spelling(entry_point):
            violations.append(f"pyproject.toml: prohibited entry point {dangerous}")
    for config_value in _nested_strings(project_data):
        if dangerous := _dangerous_spelling(config_value):
            violations.append(f"pyproject.toml: prohibited config surface {dangerous}")
        if re.search(r"\bpost\b.*?/(?:v\d+/)?orders?(?:[/?#]|$)", config_value, re.I):
            violations.append("pyproject.toml: direct POST to orders endpoint")

    lock_file = root / "uv.lock"
    if lock_file.is_file():
        lock_data = tomllib.loads(lock_file.read_text(encoding="utf-8"))
        for package in lock_data.get("package", []):
            name = _normalize_name(package["name"])
            if dangerous := _dangerous_spelling(name):
                violations.append(f"uv.lock: prohibited package {dangerous}")
        project_name = project_data.get("project", {}).get("name")
        project_package = next(
            (
                package
                for package in lock_data.get("package", [])
                if package.get("name") == project_name
            ),
            None,
        )
        if project_package is None:
            violations.append("uv.lock: project package missing")
        else:
            locked_runtime = {
                _normalize_name(dependency["name"])
                for dependency in project_package.get("dependencies", [])
            }
            for name in sorted(locked_runtime - allowed_runtime):
                violations.append(
                    f"uv.lock: runtime dependency not allowlisted: {name}"
                )

    for directory_name in ("src", "scripts", "ops"):
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix == ".py":
                violations.extend(_python_policy_violations(path, root))
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if dangerous := _dangerous_spelling(line):
                    violations.append(
                        f"{path.relative_to(root)}:{line_number}: prohibited surface "
                        f"{dangerous.strip()}"
                    )
                if re.search(r"\bpost\b.*?/(?:v\d+/)?orders?(?:[/?#]|$)", line, re.I):
                    relative = path.relative_to(root)
                    violations.append(
                        f"{relative}:{line_number}: direct POST to orders endpoint"
                    )
    return violations


def test_no_live_execution_surface() -> None:
    assert not _runtime_policy_violations(ROOT)


@pytest.mark.parametrize("term", _policy_terms())
def test_surface_scanner_rejects_prohibited_terms(term: str, tmp_path: Path) -> None:
    sample = tmp_path / "unsafe.py"
    sample.write_text(f"def {term}():\n    pass\n", encoding="utf-8")
    assert term in _python_surface(sample)


@pytest.mark.parametrize(
    ("relative_path", "content", "expected"),
    (
        (
            "pyproject.toml",
            '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["py-clob-client"]\n',
            "py-clob-client",
        ),
        (
            "pyproject.toml",
            '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["eth-account"]\n',
            "eth-account",
        ),
        (
            "pyproject.toml",
            '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["alpaca-py"]\n',
            "alpaca-py",
        ),
        (
            "pyproject.toml",
            '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["ib-insync"]\n',
            "ib-insync",
        ),
        (
            "pyproject.toml",
            '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["unknown-trading-sdk"]\n',
            "unknown-trading-sdk",
        ),
        (
            "pyproject.toml",
            '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["typer"]\n'
            '[project.scripts]\nsubmit-order="sample:main"\n',
            "submit-order",
        ),
        (
            "pyproject.toml",
            '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["typer"]\n'
            '[tool.runner]\ncommand="curl -X POST https://api.example.test/orders"\n',
            "orders",
        ),
        (
            "uv.lock",
            'version=1\n[[package]]\nname="sample"\nversion="0.1.0"\n'
            'dependencies=[{name="typer"}]\n[[package]]\nname="py-clob-client"\n'
            'version="0.1.0"\n',
            "py-clob-client",
        ),
        (
            "src/sample.py",
            'import requests\nrequests.post("https://api.example.test/v2/orders")\n',
            "orders",
        ),
        (
            "src/sample.py",
            'import requests\nrequests.post("https://api.example.test" + "/orders")\n',
            "orders",
        ),
        (
            "src/sample.py",
            'method = "submit-order"\ngetattr(client, method)()\n',
            "submit-order",
        ),
        (
            "src/sample.py",
            'handlers["sign-order"]()\n',
            "sign-order",
        ),
        (
            "ops/live.service",
            "ExecStart=/usr/bin/curl -X POST https://api.example.test/orders\n",
            "orders",
        ),
        (
            "scripts/adapter.sh",
            "#!/bin/sh\npython -m alpaca-py submit-order\n",
            "alpaca-py",
        ),
        (
            "src/sample.py",
            "import mystery_trading_sdk\n",
            "mystery_trading_sdk",
        ),
    ),
)
def test_policy_positive_controls_reject_bypasses(
    relative_path: str, content: str, expected: str, tmp_path: Path
) -> None:
    baseline = tmp_path / "pyproject.toml"
    baseline.write_text(
        '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["typer"]\n',
        encoding="utf-8",
    )
    sample = tmp_path / relative_path
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text(content, encoding="utf-8")
    if sample.suffix == ".sh":
        sample.chmod(sample.stat().st_mode | stat.S_IXUSR)

    violations = _runtime_policy_violations(tmp_path)

    assert any(expected in violation for violation in violations), violations


def test_required_dev_toolchain_is_declared_and_locked() -> None:
    project_data = _project_data()
    dev_requirements = project_data["dependency-groups"]["dev"]
    normalized = {
        re.split(r"[<>=!~\s\[]", requirement, maxsplit=1)[0]
        .replace("_", "-")
        .casefold(): requirement
        for requirement in dev_requirements
    }
    for required in ("pytest-asyncio", "respx", "hypothesis", "pip-audit"):
        assert required in normalized
    assert normalized["pytest"].startswith("pytest>=9.0.3")

    lock_data = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {package["name"]: package["version"] for package in lock_data["package"]}
    assert locked["pytest"] >= "9.0.3"
    for required in ("pytest-asyncio", "respx", "hypothesis", "pip-audit"):
        assert required in locked


def test_locked_build_configuration() -> None:
    project_data = _project_data()
    build_system = project_data.get("build-system", {})
    assert build_system.get("build-backend") == "hatchling.build"
    assert build_system.get("requires") == ["hatchling==1.27.0"]
    wheel = (
        project_data.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
    )
    assert wheel.get("packages") == ["src/jev_trading_lab"]
    included = set(wheel.get("include", []))
    assert "src/jev_trading_lab/**/*.sql" in included
    assert "src/jev_trading_lab/templates/**/*" in included
    assert set(project_data.get("dependency-groups", {})) == {"dev"}
    assert "hatchling==1.27.0" in project_data["dependency-groups"]["dev"]

    lock_file = ROOT / "uv.lock"
    assert lock_file.is_file(), "uv.lock must pin the resolved environment"
    lock_data = tomllib.loads(lock_file.read_text(encoding="utf-8"))
    locked = {package["name"]: package["version"] for package in lock_data["package"]}
    assert project_data["project"]["name"] in locked
    assert locked["hatchling"] == "1.27.0"


def test_ci_uses_immutable_actions_and_locked_audit() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    action_lines = [line.strip() for line in workflow.splitlines() if "uses:" in line]
    assert action_lines
    for line in action_lines:
        assert re.search(r"uses: [^@]+@[0-9a-f]{40}\s+# v\d", line), line
    checkout_block = workflow.split("astral-sh/setup-uv", maxsplit=1)[0]
    assert "persist-credentials: false" in checkout_block
    assert "permissions:\n  contents: read" in workflow
    assert (
        "uv export --frozen --all-groups --no-emit-project "
        "--format requirements-txt --output-file audit-requirements.txt"
    ) in workflow
    assert (
        "uv run pip-audit --strict --requirement audit-requirements.txt "
        "--no-deps --disable-pip"
    ) in workflow

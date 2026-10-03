from __future__ import annotations

import ast
import importlib
import importlib.util
import re
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


def _project_data() -> dict[str, Any]:
    project_file = ROOT / "pyproject.toml"
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


def test_no_live_execution_surface() -> None:
    project_data = _project_data()
    inspected: dict[Path, set[str]] = {}
    for directory_name in ("src", "scripts", "ops"):
        directory = ROOT / directory_name
        if directory.is_dir():
            inspected.update(
                (path, _python_surface(path)) for path in directory.rglob("*.py")
            )

    config_paths = (ROOT / "pyproject.toml", ROOT / "uv.lock")
    config_text = "\n".join(
        path.read_text(encoding="utf-8").casefold()
        for path in config_paths
        if path.is_file()
    )
    entry_points = project_data.get("project", {}).get("scripts", {})
    inspected_text = "\n".join(
        (" ".join(sorted(names)) for names in inspected.values()),
    )
    searchable = "\n".join((inspected_text, config_text, repr(entry_points).casefold()))
    violations = [
        term
        for term in _policy_terms()
        if re.search(rf"(?<![a-z]){term}(?![a-z])", searchable)
    ]
    assert not violations, f"prohibited runtime surfaces: {violations}"


@pytest.mark.parametrize("term", _policy_terms())
def test_surface_scanner_rejects_prohibited_terms(term: str, tmp_path: Path) -> None:
    sample = tmp_path / "unsafe.py"
    sample.write_text(f"def {term}():\n    pass\n", encoding="utf-8")
    assert term in _python_surface(sample)


def test_locked_build_configuration() -> None:
    project_data = _project_data()
    assert (
        project_data.get("build-system", {}).get("build-backend") == "hatchling.build"
    )
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

    lock_file = ROOT / "uv.lock"
    assert lock_file.is_file(), "uv.lock must pin the resolved environment"
    lock_data = tomllib.loads(lock_file.read_text(encoding="utf-8"))
    locked_names = {package["name"] for package in lock_data.get("package", [])}
    assert project_data["project"]["name"] in locked_names

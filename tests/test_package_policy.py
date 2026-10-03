from __future__ import annotations

import ast
import importlib
import importlib.util
import os
import py_compile
import re
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def test_test_process_disables_bytecode_writes() -> None:
    assert sys.dont_write_bytecode


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
    try:
        return tomllib.loads(project_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        pytest.fail(f"pyproject.toml must be readable valid UTF-8 TOML: {error}")


def _normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _requirement_name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    assert match, f"invalid requirement: {requirement}"
    return _normalize_name(match.group())


_ALLOWED_SOURCE_IMPORTS = {
    "src/jev_trading_lab/__init__.py": frozenset(),
    "src/jev_trading_lab/cli.py": frozenset({"typer"}),
}
_ALLOWED_SOURCE_CALLS = {
    "src/jev_trading_lab/__init__.py": frozenset(),
    "src/jev_trading_lab/cli.py": frozenset(
        {"typer.Typer", "app.callback", "app", "main"}
    ),
}
_ALLOWED_TOOL_CONFIG = {
    "hatch": {
        "build": {
            "targets": {
                "wheel": {
                    "packages": ["src/jev_trading_lab"],
                    "include": [
                        "src/jev_trading_lab/**/*.sql",
                        "src/jev_trading_lab/templates/**/*",
                    ],
                }
            }
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
_ALLOWED_ENTRY_POINTS = {"jev-lab": "jev_trading_lab.cli:app"}
_ALLOWED_DEPENDENCY_GROUPS = {
    "dev": frozenset(
        {
            "hatchling",
            "hypothesis",
            "mypy",
            "pip-audit",
            "pytest",
            "pytest-asyncio",
            "respx",
            "ruff",
        }
    )
}
_PROTECTED_CALLABLE_BINDINGS = frozenset({"app", "main"})
_BLOCKED_DYNAMIC_CALLS = frozenset(
    {
        "compile",
        "delattr",
        "eval",
        "exec",
        "getattr",
        "globals",
        "locals",
        "setattr",
        "vars",
        "__import__",
    }
)


def _import_paths(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    prefix = "." * node.level + (node.module or "")
    return [f"{prefix}.{alias.name}".strip(".") for alias in node.names]


def _call_path(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _call_path(node.value)
        return f"{owner}.{node.attr}" if owner else node.attr
    return None


def _assigned_names(node: ast.AST) -> set[str]:
    return {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
    }


def _python_bindings(
    tree: ast.Module, relative: str
) -> tuple[dict[str, str], list[str]]:
    bindings: dict[str, str] = {}
    violations: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                binding = alias.asname or alias.name.split(".", maxsplit=1)[0]
                bindings[binding] = f"import:{alias.name}"
                if binding in _PROTECTED_CALLABLE_BINDINGS:
                    violations.append(
                        f"{relative}: protected binding {binding} imported"
                    )
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                binding = alias.asname or alias.name
                imported = f"{node.module}.{alias.name}" if node.module else alias.name
                bindings[binding] = f"import:{imported}"
                if binding in _PROTECTED_CALLABLE_BINDINGS:
                    violations.append(
                        f"{relative}: protected binding {binding} imported"
                    )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bindings[node.name] = "local"
            if node.name in _PROTECTED_CALLABLE_BINDINGS and not (
                relative == "src/jev_trading_lab/cli.py"
                and isinstance(node, ast.FunctionDef)
                and node.name == "main"
            ):
                violations.append(
                    f"{relative}: protected binding {node.name} redeclared"
                )
        elif isinstance(
            node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)
        ):
            assigned = _assigned_names(node)
            allowed_app = (
                relative == "src/jev_trading_lab/cli.py"
                and assigned == {"app"}
                and isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and _call_path(node.value.func) == "typer.Typer"
            )
            allowed_version = (
                relative == "src/jev_trading_lab/__init__.py"
                and assigned == {"__version__"}
                and isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            )
            if not (allowed_app or allowed_version):
                violations.append(f"{relative}: assignment is not allowlisted")
            for binding in assigned:
                if binding in _PROTECTED_CALLABLE_BINDINGS and not allowed_app:
                    violations.append(
                        f"{relative}: protected binding {binding} reassigned"
                    )
                bindings[binding] = "typer-instance" if allowed_app else "value"
    return bindings, violations


def _allowed_binding_targets(tree: ast.Module, relative: str) -> set[ast.AST]:
    allowed: set[ast.AST] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        allowed_app = (
            relative == "src/jev_trading_lab/cli.py"
            and isinstance(target, ast.Name)
            and target.id == "app"
            and isinstance(node.value, ast.Call)
            and _call_path(node.value.func) == "typer.Typer"
        )
        allowed_version = (
            relative == "src/jev_trading_lab/__init__.py"
            and isinstance(target, ast.Name)
            and target.id == "__version__"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
        if allowed_app or allowed_version:
            allowed.add(target)
    return allowed


def _binding_policy_violations(tree: ast.Module, relative: str) -> list[str]:
    allowed_targets = _allowed_binding_targets(tree, relative)
    allowed_functions = (
        {"research", "main"} if relative == "src/jev_trading_lab/cli.py" else set()
    )
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.alias) and node.asname is not None:
            violations.append(f"{relative}: import alias is not allowlisted")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if node not in allowed_targets:
                violations.append(
                    f"{relative}: binding target {node.id} is not allowlisted"
                )
        elif isinstance(node, ast.Attribute) and isinstance(
            node.ctx, (ast.Store, ast.Del)
        ):
            violations.append(
                f"{relative}: attribute mutation {_call_path(node) or node.attr} "
                "is not allowlisted"
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (
                node not in tree.body
                or not isinstance(node, ast.FunctionDef)
                or node.name not in allowed_functions
            ):
                violations.append(
                    f"{relative}: declaration {node.name} is not allowlisted"
                )
        elif isinstance(node, ast.arg):
            violations.append(
                f"{relative}: binding target {node.arg} is not allowlisted"
            )
        elif isinstance(node, ast.ExceptHandler) and node.name is not None:
            violations.append(
                f"{relative}: binding target {node.name} is not allowlisted"
            )
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                violations.append(
                    f"{relative}: binding target {name} is not allowlisted"
                )
    return violations


def _python_policy_violations(path: Path, root: Path) -> list[str]:
    relative = path.relative_to(root).as_posix()
    if relative not in _ALLOWED_SOURCE_IMPORTS:
        return [f"{relative}: source path is not allowlisted"]
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return [f"{relative}: source is unreadable UTF-8: {error}"]
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as error:
        return [f"{relative}: source is not valid Python: {error}"]

    bindings, violations = _python_bindings(tree, relative)
    violations.extend(_binding_policy_violations(tree, relative))
    allowed_imports = _ALLOWED_SOURCE_IMPORTS[relative]
    allowed_calls = _ALLOWED_SOURCE_CALLS[relative]
    allowed_targets = _allowed_binding_targets(tree, relative)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias, imported in zip(node.names, _import_paths(node), strict=True):
                binding = alias.asname or (
                    alias.name
                    if isinstance(node, ast.ImportFrom)
                    else alias.name.split(".", maxsplit=1)[0]
                )
                if binding in _PROTECTED_CALLABLE_BINDINGS:
                    violations.append(
                        f"{relative}: protected binding {binding} imported"
                    )
                if imported not in allowed_imports:
                    violations.append(f"{relative}: unauthorized import {imported}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in _PROTECTED_CALLABLE_BINDINGS and not (
                node in tree.body
                and relative == "src/jev_trading_lab/cli.py"
                and isinstance(node, ast.FunctionDef)
                and node.name == "main"
            ):
                violations.append(f"{relative}: protected binding {node.name} declared")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if node.id in _PROTECTED_CALLABLE_BINDINGS and node not in allowed_targets:
                violations.append(f"{relative}: protected binding {node.id} modified")
        elif isinstance(node, ast.arg) and node.arg in _PROTECTED_CALLABLE_BINDINGS:
            violations.append(f"{relative}: protected binding {node.arg} declared")
        elif isinstance(node, ast.Call):
            called = _call_path(node.func)
            provenance_ok = False
            if called == "typer.Typer":
                provenance_ok = bindings.get("typer") == "import:typer"
            elif called in {"app", "app.callback"}:
                provenance_ok = bindings.get("app") == "typer-instance"
            elif called == "main":
                provenance_ok = bindings.get("main") == "local"
            if called in _BLOCKED_DYNAMIC_CALLS or not provenance_ok:
                violations.append(
                    f"{relative}: unauthorized call {called or '<dynamic>'}"
                )
            elif called not in allowed_calls:
                violations.append(f"{relative}: call not allowlisted: {called}")
    return violations


def _dependency_names(requirements: object) -> set[str]:
    if not isinstance(requirements, list):
        return set()
    return {
        _requirement_name(requirement)
        if isinstance(requirement, str)
        else _normalize_name(requirement["name"])
        for requirement in requirements
        if isinstance(requirement, str)
        or (isinstance(requirement, dict) and isinstance(requirement.get("name"), str))
    }


def _dependency_group_violations(
    groups: object, *, location: str, locked: bool = False
) -> list[str]:
    if not isinstance(groups, dict):
        return [f"{location}: dependency groups must be a table"] if groups else []
    violations: list[str] = []
    for group, requirements in groups.items():
        allowed = _ALLOWED_DEPENDENCY_GROUPS.get(group)
        names = _dependency_names(requirements)
        label = "locked dependency group" if locked else "dependency group"
        if allowed is None:
            violations.append(f"{location}: {label} not allowlisted: {group}")
            continue
        violations.extend(
            f"{location}: {label} {group} dependency not allowlisted: {name}"
            for name in sorted(names - allowed)
        )
    return violations


def _optional_dependency_violations(
    groups: object, *, location: str, locked: bool = False
) -> list[str]:
    if not groups:
        return []
    names = sorted(
        {
            name
            for requirements in groups.values()
            for name in _dependency_names(requirements)
        }
        if isinstance(groups, dict)
        else set()
    )
    label = "locked optional dependencies" if locked else "optional dependencies"
    return [f"{location}: {label} are not allowed: {', '.join(names) or groups}"]


def _physical_source_violations(root: Path) -> list[str]:
    source = root / "src"
    violations: list[str] = []

    def record_walk_error(error: OSError) -> None:
        try:
            relative = Path(error.filename or source).relative_to(root).as_posix()
        except ValueError:
            relative = str(error.filename or source)
        violations.append(f"{relative}: production source is unreadable: {error}")

    for directory, directory_names, file_names in os.walk(
        source, topdown=True, onerror=record_walk_error, followlinks=False
    ):
        directory_path = Path(directory)
        for name in directory_names:
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            if name == "__pycache__":
                violations.append(f"{relative}: production __pycache__ is not allowed")
        for name in file_names:
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            if path.suffix.casefold() in {".pyc", ".pyo"}:
                violations.append(f"{relative}: production bytecode is not allowed")
                continue
            try:
                path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                violations.append(
                    f"{relative}: production source is unreadable UTF-8: {error}"
                )
    return violations


def _production_source_paths(root: Path) -> list[Path]:
    try:
        repository_root = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if Path(repository_root).resolve() == root.resolve():
            listed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "ls-files",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                    "src",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            return [root / relative for relative in listed]
    except (OSError, subprocess.CalledProcessError):
        pass
    return [
        path
        for path in (root / "src").rglob("*")
        if path.is_file() or path.name == "__pycache__"
    ]


def _runtime_policy_violations(root: Path) -> list[str]:
    project_data = _project_data(root)
    project = project_data.get("project", {})
    allowed_runtime = {"typer"}
    runtime_names = _dependency_names(project.get("dependencies", []))
    violations = [
        f"pyproject.toml: runtime dependency not allowlisted: {name}"
        for name in sorted(runtime_names - allowed_runtime)
    ]
    violations.extend(
        _dependency_group_violations(
            project_data.get("dependency-groups", {}), location="pyproject.toml"
        )
    )
    violations.extend(
        _optional_dependency_violations(
            project.get("optional-dependencies", {}), location="pyproject.toml"
        )
    )

    entry_points = project.get("scripts", {})
    if entry_points != _ALLOWED_ENTRY_POINTS:
        violations.append(
            f"pyproject.toml: entry points not allowlisted: {entry_points}"
        )
    tool_config = project_data.get("tool", {})
    if tool_config != _ALLOWED_TOOL_CONFIG:
        violations.append(f"pyproject.toml: tool config not allowlisted: {tool_config}")

    lock_file = root / "uv.lock"
    if lock_file.is_file():
        try:
            lock_data = tomllib.loads(lock_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
            violations.append(f"uv.lock: unreadable or invalid UTF-8 TOML: {error}")
            lock_data = {}
        project_name = project.get("name")
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
            locked_runtime = _dependency_names(project_package.get("dependencies", []))
            for name in sorted(locked_runtime - allowed_runtime):
                violations.append(
                    f"uv.lock: runtime dependency not allowlisted: {name}"
                )
            locked_group_sections = [
                project_package.get("dependency-groups", {}),
                project_package.get("dev-dependencies", {}),
                project_package.get("metadata", {}).get("requires-dev", {}),
            ]
            for locked_groups in locked_group_sections:
                violations.extend(
                    _dependency_group_violations(
                        locked_groups, location="uv.lock", locked=True
                    )
                )
            violations.extend(
                _optional_dependency_violations(
                    project_package.get("optional-dependencies", {}),
                    location="uv.lock",
                    locked=True,
                )
            )

    source = root / "src"
    if source.is_dir():
        violations.extend(_physical_source_violations(root))
        for path in _production_source_paths(root):
            relative = path.relative_to(root).as_posix()
            if "__pycache__" in path.parts:
                violations.append(f"{relative}: production __pycache__ is not allowed")
            elif not path.is_file():
                violations.append(f"{relative}: unknown production source surface")
            elif path.suffix == ".py":
                violations.extend(_python_policy_violations(path, root))
            else:
                violations.append(f"{relative}: source path is not allowlisted")

    for directory_name in ("scripts", "ops"):
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.is_file():
                relative = path.relative_to(root).as_posix()
                violations.append(f"{relative}: operational path is not allowlisted")
    return violations


def test_no_live_execution_surface() -> None:
    assert not _runtime_policy_violations(ROOT)


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
            "not allowlisted",
        ),
        (
            "uv.lock",
            'version=1\n[[package]]\nname="sample"\nversion="0.1.0"\n'
            'dependencies=[{name="py-clob-client"}]\n'
            '[[package]]\nname="py-clob-client"\nversion="0.1.0"\n',
            "py-clob-client",
        ),
        (
            "src/sample.py",
            'import requests\nrequests.post("https://api.example.test/v2/orders")\n',
            "src/sample.py",
        ),
        (
            "src/sample.py",
            'import requests\nrequests.post("https://api.example.test" + "/orders")\n',
            "src/sample.py",
        ),
        (
            "src/sample.py",
            'method = "submit-order"\ngetattr(client, method)()\n',
            "src/sample.py",
        ),
        (
            "src/sample.py",
            'handlers["sign-order"]()\n',
            "src/sample.py",
        ),
        (
            "ops/live.service",
            "ExecStart=/usr/bin/curl -X POST https://api.example.test/orders\n",
            "ops/live.service",
        ),
        (
            "scripts/adapter.sh",
            "#!/bin/sh\npython -m alpaca-py submit-order\n",
            "scripts/adapter.sh",
        ),
        (
            "src/sample.py",
            "import mystery_trading_sdk\n",
            "src/sample.py",
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


@pytest.mark.parametrize(
    ("content", "expected"),
    (
        (
            "from urllib.request import Request, urlopen\n"
            'request = Request("https://api.example.test/orders", method="POST")\n'
            "urlopen(request)\n",
            "urllib.request",
        ),
        (
            "from http.client import HTTPSConnection\n"
            'connection = HTTPSConnection("api.example.test")\n'
            'connection.request("POST", "/orders")\n',
            "http.client",
        ),
        (
            "import subprocess\n"
            'verb = "PO" + "ST"\n'
            'endpoint = "https://api.example.test" + "/orders"\n'
            'argv = ["curl", "-X", verb, endpoint]\n'
            "subprocess.run(argv, check=True)\n",
            "subprocess",
        ),
        (
            'module_name = "urllib" + ".request"\n'
            "transport = __import__(module_name)\n",
            "__import__",
        ),
        (
            "import importlib\n"
            'module_name = "http" + ".client"\n'
            "transport = importlib.import_module(module_name)\n",
            "importlib",
        ),
        (
            "import jev_trading_lab.broker as transport\n",
            "jev_trading_lab.broker",
        ),
        (
            "from jev_trading_lab import broker\n",
            "jev_trading_lab.broker",
        ),
        (
            "from jev_trading_lab import wallet\n",
            "jev_trading_lab.wallet",
        ),
        (
            "from jev_trading_lab import signing\n",
            "jev_trading_lab.signing",
        ),
        (
            "from jev_trading_lab import order\n",
            "jev_trading_lab.order",
        ),
        (
            "from jev_trading_lab import execution\n",
            "jev_trading_lab.execution",
        ),
        (
            "from jev_trading_lab import live\n",
            "jev_trading_lab.live",
        ),
        (
            'method = "submit" + "_order"\ngetattr(client, method)()\n',
            "getattr",
        ),
        (
            'request = {"method": "POST", "url": "https://api.example.test/orders"}\n'
            "client.request(**request)\n",
            "client.request",
        ),
        (
            'client.request(method="POST", url="https://api.example.test/orders")\n',
            "client.request",
        ),
    ),
)
def test_fail_closed_python_capability_allowlist(
    content: str, expected: str, tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["typer"]\n',
        encoding="utf-8",
    )
    sample = tmp_path / "src/jev_trading_lab/cli.py"
    sample.parent.mkdir(parents=True)
    sample.write_text(content, encoding="utf-8")

    violations = _runtime_policy_violations(tmp_path)

    assert any(expected in violation for violation in violations), violations


@pytest.mark.parametrize(
    ("content", "binding"),
    (
        ("main = exec\nmain('pass')\n", "main"),
        ("main = __import__\nmain('os')\n", "main"),
        ("main = os.system\nmain('true')\n", "main"),
        (
            "main = urllib.request.urlopen\nmain('https://api.example.test/orders')\n",
            "main",
        ),
        ("main = http.client.HTTPSConnection\nmain('api.example.test')\n", "main"),
        ("main = subprocess.run\nmain(['true'])\n", "main"),
        ("main = getattr\nmain(client, 'submit_order')\n", "main"),
        ("app = exec\napp('pass')\n", "app"),
        ("alias = exec\nmain = alias\nmain('pass')\n", "main"),
        ("alias = getattr\napp = alias\napp(client, 'submit_order')\n", "app"),
        ("import typer as main\nmain()\n", "main"),
        ("from typer import Typer as app\napp()\n", "app"),
        ("def outer():\n    def main():\n        pass\n    main()\n", "main"),
    ),
)
def test_allowed_call_names_cannot_be_rebound(
    content: str, binding: str, tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["typer"]\n',
        encoding="utf-8",
    )
    sample = tmp_path / "src/jev_trading_lab/cli.py"
    sample.parent.mkdir(parents=True)
    sample.write_text(content, encoding="utf-8")

    violations = _runtime_policy_violations(tmp_path)

    assert any(
        binding in violation and "binding" in violation for violation in violations
    ), violations


@pytest.mark.parametrize(
    ("content", "expected"),
    (
        (
            "import typer\n"
            "class Evil:\n"
            "    Typer = exec\n"
            "for typer in [Evil]:\n"
            "    pass\n"
            "app = typer.Typer()\n"
            "app()\n",
            "binding target",
        ),
        (
            "import typer\n"
            "def main():\n"
            "    typer.Typer = exec\n"
            "    typer.Typer('pass')\n",
            "attribute mutation",
        ),
        (
            "import typer\n"
            "class Evil:\n"
            "    Typer = exec\n"
            "with Evil as typer:\n"
            "    pass\n"
            "app = typer.Typer()\n"
            "app()\n",
            "binding target",
        ),
        (
            "import typer\n"
            "class Evil:\n"
            "    Typer = exec\n"
            "if (typer := Evil):\n"
            "    pass\n"
            "app = typer.Typer()\n"
            "app()\n",
            "binding target",
        ),
        (
            "import typer\n"
            "try:\n"
            "    raise RuntimeError\n"
            "except RuntimeError as typer:\n"
            "    pass\n"
            "app = typer.Typer()\n"
            "app()\n",
            "binding target",
        ),
    ),
)
def test_frozen_source_contract_rejects_binding_mutation(
    content: str, expected: str, tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["typer"]\n',
        encoding="utf-8",
    )
    sample = tmp_path / "src/jev_trading_lab/cli.py"
    sample.parent.mkdir(parents=True)
    sample.write_text(content, encoding="utf-8")

    violations = _runtime_policy_violations(tmp_path)

    assert any(expected in violation for violation in violations), violations


@pytest.mark.parametrize(
    "content",
    (
        "app.__call__ = exec\napp()\n",
        "except_alias = exec\n",
    ),
)
def test_frozen_source_contract_rejects_extra_assignments(
    content: str, tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["typer"]\n',
        encoding="utf-8",
    )
    sample = tmp_path / "src/jev_trading_lab/cli.py"
    sample.parent.mkdir(parents=True)
    sample.write_text(content, encoding="utf-8")

    violations = _runtime_policy_violations(tmp_path)

    assert any("assignment" in violation for violation in violations), violations


def test_dev_dependency_group_rejects_unapproved_direct_dependency(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["typer"]\n'
        '[dependency-groups]\ndev=["py-clob-client"]\n',
        encoding="utf-8",
    )

    violations = _runtime_policy_violations(tmp_path)

    assert any(
        "dependency group dev" in violation and "py-clob-client" in violation
        for violation in violations
    ), violations


def test_lock_dev_dependency_group_rejects_unapproved_direct_dependency(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["typer"]\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        'version=1\n[[package]]\nname="sample"\nversion="0.1.0"\n'
        '[package.dev-dependencies]\ndev=[{name="py-clob-client"}]\n'
        '[[package]]\nname="py-clob-client"\nversion="0.1.0"\n',
        encoding="utf-8",
    )

    violations = _runtime_policy_violations(tmp_path)

    assert any(
        "locked dependency group dev" in violation and "py-clob-client" in violation
        for violation in violations
    ), violations


def test_lock_metadata_dev_group_rejects_unapproved_direct_dependency(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["typer"]\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        'version=1\n[[package]]\nname="sample"\nversion="0.1.0"\n'
        '[package.metadata.requires-dev]\ndev=[{name="py-clob-client"}]\n'
        '[[package]]\nname="py-clob-client"\nversion="0.1.0"\n',
        encoding="utf-8",
    )

    violations = _runtime_policy_violations(tmp_path)

    assert any(
        "locked dependency group dev" in violation and "py-clob-client" in violation
        for violation in violations
    ), violations


def test_optional_dependencies_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["typer"]\n'
        '[project.optional-dependencies]\ntrade=["alpaca-py"]\n',
        encoding="utf-8",
    )

    violations = _runtime_policy_violations(tmp_path)

    assert any(
        "optional dependencies" in violation and "alpaca-py" in violation
        for violation in violations
    ), violations


def test_lock_optional_dependencies_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["typer"]\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        'version=1\n[[package]]\nname="sample"\nversion="0.1.0"\n'
        '[package.optional-dependencies]\ntrade=[{name="alpaca-py"}]\n'
        '[[package]]\nname="alpaca-py"\nversion="0.1.0"\n',
        encoding="utf-8",
    )

    violations = _runtime_policy_violations(tmp_path)

    assert any(
        "locked optional dependencies" in violation and "alpaca-py" in violation
        for violation in violations
    ), violations


def _initialize_ignored_bytecode_fixture(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["typer"]\n',
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text("__pycache__/\n*.py[cod]\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "--quiet", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_git_ignored_pycache_rejects_non_utf8_source(tmp_path: Path) -> None:
    _initialize_ignored_bytecode_fixture(tmp_path)
    sample = tmp_path / "src/jev_trading_lab/__pycache__/live.py"
    sample.parent.mkdir(parents=True)
    sample.write_bytes(b"#!/usr/bin/env python\n\xff\xfe\x00")

    violations = _runtime_policy_violations(tmp_path)

    assert any(
        "src/jev_trading_lab/__pycache__/live.py" in item for item in violations
    ), violations


def test_git_ignored_pycache_rejects_unchecked_hash_bytecode(tmp_path: Path) -> None:
    _initialize_ignored_bytecode_fixture(tmp_path)
    source = tmp_path / "malicious.py"
    bytecode = tmp_path / "src/jev_trading_lab/__pycache__/malicious.pyc"
    bytecode.parent.mkdir(parents=True)
    source.write_text("exec(compile('pass', '<payload>', 'exec'))\n", encoding="utf-8")
    py_compile.compile(
        str(source),
        cfile=str(bytecode),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    source.unlink()

    violations = _runtime_policy_violations(tmp_path)

    assert any(
        "src/jev_trading_lab/__pycache__/malicious.pyc" in item for item in violations
    ), violations


def test_fail_closed_shell_surface_rejects_composed_curl_post(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["typer"]\n',
        encoding="utf-8",
    )
    sample = tmp_path / "scripts/adapter.sh"
    sample.parent.mkdir()
    sample.write_text(
        "#!/bin/sh\n"
        'verb="P"\n'
        'verb="${verb}OST"\n'
        'base="https://api.example.test"\n'
        'path="/orders"\n'
        'curl -X "$verb" "${base}${path}"\n',
        encoding="utf-8",
    )
    sample.chmod(sample.stat().st_mode | stat.S_IXUSR)

    violations = _runtime_policy_violations(tmp_path)

    assert any("scripts/adapter.sh" in violation for violation in violations), (
        violations
    )


@pytest.mark.parametrize(
    "relative_path",
    ("src/opaque.cfg", "scripts/opaque.sh", "ops/opaque.service"),
)
def test_fail_closed_surfaces_reject_non_utf8(
    relative_path: str, tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["typer"]\n',
        encoding="utf-8",
    )
    sample = tmp_path / relative_path
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_bytes(b"#!/bin/sh\n\xff\xfe\x00")
    sample.chmod(sample.stat().st_mode | stat.S_IXUSR)

    violations = _runtime_policy_violations(tmp_path)

    assert any(relative_path in violation for violation in violations), violations


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
    assert 'PYTHONDONTWRITEBYTECODE: "1"' in workflow
    assert (
        "uv export --frozen --all-groups --no-emit-project "
        "--format requirements-txt --output-file audit-requirements.txt"
    ) in workflow
    assert (
        "uv run pip-audit --strict --requirement audit-requirements.txt "
        "--no-deps --disable-pip"
    ) in workflow
    assert "run: uv build --no-build-isolation" in workflow
    assert "run: uv build\n" not in workflow

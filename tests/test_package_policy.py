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

    violations: list[str] = []
    allowed_imports = _ALLOWED_SOURCE_IMPORTS[relative]
    allowed_calls = _ALLOWED_SOURCE_CALLS[relative]
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for imported in _import_paths(node):
                if imported not in allowed_imports:
                    violations.append(f"{relative}: unauthorized import {imported}")
        elif isinstance(node, ast.Call):
            called = _call_path(node.func)
            if called not in allowed_calls:
                violations.append(
                    f"{relative}: unauthorized call {called or '<dynamic>'}"
                )
    return violations


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

    entry_points = project_data.get("project", {}).get("scripts", {})
    if entry_points != _ALLOWED_ENTRY_POINTS:
        violations.append(
            f"pyproject.toml: entry points not allowlisted: {entry_points}"
        )
    tool_config = project_data.get("tool", {})
    if tool_config != _ALLOWED_TOOL_CONFIG:
        violations.append(f"pyproject.toml: tool config not allowlisted: {tool_config}")

    lock_file = root / "uv.lock"
    if lock_file.is_file():
        lock_data = tomllib.loads(lock_file.read_text(encoding="utf-8"))
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

    source = root / "src"
    if source.is_dir():
        for path in source.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix == ".py":
                violations.extend(_python_policy_violations(path, root))
            else:
                relative = path.relative_to(root).as_posix()
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

from __future__ import annotations

import hashlib
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


_APPROVED_SOURCE_DIRS = frozenset({"src", "src/jev_trading_lab"})
_APPROVED_SOURCE_HASHES = {
    "src/jev_trading_lab/__init__.py": (
        "583ff05fc923ac30ef5cd0e9c44271e53746a65e312f53f112ec1b49e827647b"
    ),
    "src/jev_trading_lab/cli.py": (
        "e1018e68f8902660a347c842866c321a42bad08d3f332cdf7f32bf0029cc84d9"
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


def _source_policy_violations(root: Path) -> list[str]:
    expected = _APPROVED_SOURCE_DIRS | _APPROVED_SOURCE_HASHES.keys()
    seen: set[str] = set()
    violations: list[str] = []
    pending = [root / "src"]

    while pending:
        path = pending.pop()
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            violations.append(f"{path}: production path escaped repository root")
            continue
        seen.add(relative)
        try:
            metadata = path.lstat()
        except OSError as error:
            violations.append(f"{relative}: production source is unreadable: {error}")
            continue

        if stat.S_ISLNK(metadata.st_mode):
            violations.append(f"{relative}: production symlink is not allowed")
        elif stat.S_ISDIR(metadata.st_mode):
            if relative not in _APPROVED_SOURCE_DIRS:
                violations.append(f"{relative}: production directory is not approved")
            if path.name == "__pycache__":
                violations.append(f"{relative}: production __pycache__ is not allowed")
            if not metadata.st_mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH):
                violations.append(f"{relative}: production directory is unreadable")
                continue
            try:
                with os.scandir(path) as entries:
                    pending.extend(Path(entry.path) for entry in entries)
            except OSError as error:
                violations.append(
                    f"{relative}: production directory is unreadable: {error}"
                )
        elif stat.S_ISREG(metadata.st_mode):
            if relative not in _APPROVED_SOURCE_HASHES:
                violations.append(f"{relative}: production file is not approved")
            if path.suffix.casefold() in {".pyc", ".pyo"}:
                violations.append(f"{relative}: production bytecode is not allowed")
            if not metadata.st_mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH):
                violations.append(f"{relative}: production source is unreadable")
                continue
            try:
                content = path.read_bytes()
                content.decode("utf-8")
            except (OSError, UnicodeError) as error:
                violations.append(
                    f"{relative}: production source is unreadable UTF-8: {error}"
                )
                continue
            approved_hash = _APPROVED_SOURCE_HASHES.get(relative)
            if approved_hash and hashlib.sha256(content).hexdigest() != approved_hash:
                violations.append(f"{relative}: SHA-256 does not match approved source")
        else:
            violations.append(f"{relative}: special production file is not allowed")

    violations.extend(
        f"{relative}: approved production path is missing"
        for relative in sorted(expected - seen)
    )
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
        else:
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
            violations.extend(
                f"uv.lock: runtime dependency not allowlisted: {name}"
                for name in sorted(locked_runtime - allowed_runtime)
            )
            for locked_groups in (
                project_package.get("dependency-groups", {}),
                project_package.get("dev-dependencies", {}),
                project_package.get("metadata", {}).get("requires-dev", {}),
            ):
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

    violations.extend(_source_policy_violations(root))
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


def _initialize_approved_source_fixture(tmp_path: Path) -> Path:
    package = tmp_path / "src/jev_trading_lab"
    package.mkdir(parents=True)
    for name in ("__init__.py", "cli.py"):
        (package / name).write_bytes((SRC / "jev_trading_lab" / name).read_bytes())
    return package


@pytest.mark.parametrize(
    "mutation",
    (
        "\n# arbitrary source mutation\n",
        "\nmatch exec:\n    case app:\n        pass\n",
        '    typer.__dict__["Typer"] = exec\n',
    ),
)
def test_exact_source_hash_rejects_arbitrary_mutation(
    mutation: str, tmp_path: Path
) -> None:
    package = _initialize_approved_source_fixture(tmp_path)
    cli = package / "cli.py"
    source = cli.read_text(encoding="utf-8")
    if mutation.startswith("    typer"):
        source = source.replace(
            '    """Access paper research workflows."""\n',
            f'    """Access paper research workflows."""\n{mutation}',
        )
    else:
        source += mutation
    cli.write_text(source, encoding="utf-8")

    violations = _source_policy_violations(tmp_path)

    assert any("SHA-256" in violation for violation in violations), violations


def _initialize_ignored_source_fixture(tmp_path: Path) -> None:
    _initialize_approved_source_fixture(tmp_path)
    (tmp_path / ".gitignore").write_text(
        "src/ignored.py\nsrc/external\n__pycache__/\n*.py[cod]\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "init", "--quiet", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_git_ignored_readable_python_is_rejected(tmp_path: Path) -> None:
    _initialize_ignored_source_fixture(tmp_path)
    (tmp_path / "src/ignored.py").write_text("exec('pass')\n", encoding="utf-8")

    violations = _source_policy_violations(tmp_path)

    assert any("src/ignored.py" in item for item in violations), violations


def test_git_ignored_symlink_to_external_package_is_rejected(tmp_path: Path) -> None:
    _initialize_ignored_source_fixture(tmp_path)
    external = tmp_path / "external-package"
    external.mkdir()
    (external / "__init__.py").write_text("exec('pass')\n", encoding="utf-8")
    (tmp_path / "src/external").symlink_to(external, target_is_directory=True)

    violations = _source_policy_violations(tmp_path)

    assert any("src/external" in item and "symlink" in item for item in violations), (
        violations
    )


def test_git_ignored_pycache_rejects_unchecked_hash_bytecode(tmp_path: Path) -> None:
    _initialize_ignored_source_fixture(tmp_path)
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

    violations = _source_policy_violations(tmp_path)

    relative = str(bytecode.relative_to(tmp_path))
    assert any(
        relative in item and "production file is not approved" in item
        for item in violations
    ), violations
    assert any(
        relative in item and "production bytecode" in item for item in violations
    ), violations


@pytest.mark.parametrize("suffix", (".pyc", ".pyo"))
def test_production_bytecode_is_rejected(suffix: str, tmp_path: Path) -> None:
    package = _initialize_approved_source_fixture(tmp_path)
    (package / f"payload{suffix}").write_bytes(b"payload")

    violations = _source_policy_violations(tmp_path)

    assert any("production bytecode" in item for item in violations), violations


def test_non_utf8_production_source_is_rejected(tmp_path: Path) -> None:
    package = _initialize_approved_source_fixture(tmp_path)
    (package / "cli.py").write_bytes(b"\xff\xfe\x00")

    violations = _source_policy_violations(tmp_path)

    assert any("unreadable UTF-8" in item for item in violations), violations


def test_unreadable_production_source_is_rejected(tmp_path: Path) -> None:
    package = _initialize_approved_source_fixture(tmp_path)
    cli = package / "cli.py"
    cli.chmod(0)

    violations = _source_policy_violations(tmp_path)

    assert any("production source is unreadable" in item for item in violations), (
        violations
    )


def test_special_production_file_is_rejected(tmp_path: Path) -> None:
    package = _initialize_approved_source_fixture(tmp_path)
    fifo = package / "payload"
    os.mkfifo(fifo)

    violations = _source_policy_violations(tmp_path)

    assert any("special production file" in item for item in violations), violations


@pytest.mark.parametrize("kind", ("file", "directory"))
def test_extra_physical_source_path_is_rejected(kind: str, tmp_path: Path) -> None:
    package = _initialize_approved_source_fixture(tmp_path)
    extra = package / "extra"
    extra.write_text("pass\n", encoding="utf-8") if kind == "file" else extra.mkdir()

    violations = _source_policy_violations(tmp_path)

    assert any("not approved" in item for item in violations), violations


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
            '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["typer"]\n'
            '[project.scripts]\nsubmit-order="sample:main"\n',
            "submit-order",
        ),
        (
            "pyproject.toml",
            '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["typer"]\n'
            '[tool.runner]\ncommand="curl -X POST https://api.example.test/orders"\n',
            "tool config not allowlisted",
        ),
        (
            "uv.lock",
            'version=1\n[[package]]\nname="sample"\nversion="0.1.0"\n'
            'dependencies=[{name="py-clob-client"}]\n'
            '[[package]]\nname="py-clob-client"\nversion="0.1.0"\n',
            "py-clob-client",
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
    ),
)
def test_policy_positive_controls_reject_non_source_surfaces(
    relative_path: str, content: str, expected: str, tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["typer"]\n',
        encoding="utf-8",
    )
    _initialize_approved_source_fixture(tmp_path)
    sample = tmp_path / relative_path
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text(content, encoding="utf-8")

    violations = _runtime_policy_violations(tmp_path)

    assert any(expected in violation for violation in violations), violations


def test_dev_dependency_group_rejects_unapproved_direct_dependency(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["typer"]\n'
        '[dependency-groups]\ndev=["py-clob-client"]\n',
        encoding="utf-8",
    )
    _initialize_approved_source_fixture(tmp_path)

    violations = _runtime_policy_violations(tmp_path)

    assert any(
        "dependency group dev" in violation and "py-clob-client" in violation
        for violation in violations
    ), violations


@pytest.mark.parametrize("section", ("dev-dependencies", "metadata.requires-dev"))
def test_lock_dev_dependency_group_rejects_unapproved_direct_dependency(
    section: str, tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="sample"\nversion="0.1.0"\ndependencies=["typer"]\n',
        encoding="utf-8",
    )
    _initialize_approved_source_fixture(tmp_path)
    (tmp_path / "uv.lock").write_text(
        'version=1\n[[package]]\nname="sample"\nversion="0.1.0"\n'
        f'[package.{section}]\ndev=[{{name="py-clob-client"}}]\n'
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
    _initialize_approved_source_fixture(tmp_path)

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
    _initialize_approved_source_fixture(tmp_path)
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

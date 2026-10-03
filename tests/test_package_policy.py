from __future__ import annotations

import configparser
import hashlib
import importlib
import importlib.util
import os
import py_compile
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def test_test_process_disables_bytecode_writes() -> None:
    assert sys.dont_write_bytecode


def test_calendar_schedule_artifact_is_packaged_in_wheel_and_sdist() -> None:
    artifact = "jev_trading_lab/artifacts/xnys-schedule-2020-2036.json"
    wheels = tuple((ROOT / "dist").glob("*.whl"))
    sdists = tuple((ROOT / "dist").glob("*.tar.gz"))
    if not wheels or not sdists:
        pytest.skip("build artifacts are inspected after the build gate")
    newest_source = max(
        (ROOT / relative).stat().st_mtime_ns
        for relative in (
            "pyproject.toml",
            "src/jev_trading_lab/artifacts/xnys-schedule-2020-2036.json",
        )
    )
    if (
        min(wheels[-1].stat().st_mtime_ns, sdists[-1].stat().st_mtime_ns)
        < newest_source
    ):
        pytest.skip("stale build artifacts are replaced by the build gate")
    with zipfile.ZipFile(wheels[-1]) as archive:
        assert artifact in archive.namelist()
    with tarfile.open(sdists[-1]) as archive:
        assert any(name.endswith("/src/" + artifact) for name in archive.getnames())


def test_migration_sql_is_packaged_in_wheel_and_sdist() -> None:
    migration = "jev_trading_lab/persistence/schema/001_migration_metadata.sql"
    wheels = tuple((ROOT / "dist").glob("*.whl"))
    sdists = tuple((ROOT / "dist").glob("*.tar.gz"))
    if not wheels or not sdists:
        pytest.skip("build artifacts are inspected after the build gate")
    newest_source = max(
        (ROOT / relative).stat().st_mtime_ns
        for relative in (
            "pyproject.toml",
            "src/jev_trading_lab/persistence/schema/001_migration_metadata.sql",
        )
    )
    if (
        min(wheels[-1].stat().st_mtime_ns, sdists[-1].stat().st_mtime_ns)
        < newest_source
    ):
        pytest.skip("stale build artifacts are replaced by the build gate")
    with zipfile.ZipFile(wheels[-1]) as archive:
        assert migration in archive.namelist()
    with tarfile.open(sdists[-1]) as archive:
        assert any(name.endswith("/src/" + migration) for name in archive.getnames())


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


_APPROVED_SOURCE_DIRS = frozenset(
    {
        "src",
        "src/jev_trading_lab",
        "src/jev_trading_lab/artifacts",
        "src/jev_trading_lab/core",
        "src/jev_trading_lab/persistence",
        "src/jev_trading_lab/persistence/schema",
        "src/jev_trading_lab/stocks",
    }
)
_APPROVED_CONFIG_DIRS = frozenset({"config"})
_APPROVED_CONFIG_HASHES = {
    "config/phase1.example.json": (
        "091c49428f15eb99fcea3a54b444b9fde929681e5ddfeaaadc4d4ebfa94438db"
    )
}
_APPROVED_OPERATIONAL_HASHES = {
    "scripts/wheel_migration_smoke.py": (
        "b51775c3e0f2067208b83042c37191222ebed66e9a6356e846b05a5784a272ee"
    )
}
_APPROVED_SOURCE_HASHES = {
    "src/jev_trading_lab/artifacts/xnys-schedule-2020-2036.json": (
        "ade5009ba938836a5d17e298b0e704fc3635b8b1eef329ba4b43cba5c40fc978"
    ),
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
        "43c98f3eb07039c7ad6581dd23a533e60e078e65c3e24d6963f686c8f6ff70ba"
    ),
    "src/jev_trading_lab/core/run_window.py": (
        "f90c328655cec51fafcb9850f8a9a8ccecf24f78398ff746814118614e277e51"
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
    "src/jev_trading_lab/persistence/__init__.py": (
        "be5cdea878ba9575ad6a83bc43819e3c1cbaecdf8ad351b2d8374f94b6c496e9"
    ),
    "src/jev_trading_lab/persistence/db.py": (
        "37e490b3664924aeb4bb9df1dede80f99c388222cdb05002b3b23b62ed3b3eea"
    ),
    "src/jev_trading_lab/persistence/migrations.py": (
        "7e082fde7a60ae7575104ef5394630fbbce2fc01089c668ec6eb58efa8477114"
    ),
    "src/jev_trading_lab/persistence/schema/__init__.py": (
        "5f19444569734cd60895d22219e7e2481fd00efb6054fb1d4cf9f7b38e26e4a5"
    ),
    "src/jev_trading_lab/persistence/schema/001_migration_metadata.sql": (
        "1be6dbbcb20a6ab8efe016172d786280470fc7a450f403825e2a61e199a1cb48"
    ),
    "src/jev_trading_lab/stocks/__init__.py": (
        "47549384d0ac5349e974d9c72796b1a48c439b574630a4b1001d6b48b17d0156"
    ),
    "src/jev_trading_lab/stocks/calendar.py": (
        "9536ea64aba216d7afcea434ba1e73634eca501095e72229797b45b8e2fed453"
    ),
}
_ALLOWED_TOOL_CONFIG = {
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
_ALLOWED_ENTRY_POINTS = {"jev-lab": "jev_trading_lab.cli:app"}
_ALLOWED_BUILD_SYSTEM = {
    "requires": ["hatchling==1.27.0"],
    "build-backend": "hatchling.build",
}
_APPROVED_WORKFLOW_HASH = (
    "503ec51653d9bd768f824832b547bb3394adb9935d719912830a5b6ff25dbdfb"
)
_APPROVED_PREFLIGHT_HASH = (
    "a320d4f3d393bb165f766c0ca075c4ab6ab0f93826e70f89bb1f3d6ab1afab2c"
)
_ALLOWED_RUNTIME_REQUIREMENTS = [
    "exchange-calendars==4.11.2",
    "pydantic>=2.12,<3",
    "typer>=0.19,<1",
]
_ALLOWED_DEPENDENCY_GROUPS = {
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
_APPROVED_REGISTRY_SOURCE = {"registry": "https://pypi.org/simple"}


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


def _config_policy_violations(root: Path) -> list[str]:
    expected = _APPROVED_CONFIG_DIRS | _APPROVED_CONFIG_HASHES.keys()
    seen: set[str] = set()
    violations: list[str] = []
    pending = [root / "config"]
    while pending:
        path = pending.pop()
        relative = path.relative_to(root).as_posix()
        try:
            metadata = path.lstat()
        except OSError as error:
            violations.append(f"{relative}: config path is unreadable: {error}")
            continue
        seen.add(relative)
        if stat.S_ISLNK(metadata.st_mode):
            violations.append(f"{relative}: config symlink is not allowed")
        elif stat.S_ISDIR(metadata.st_mode):
            if relative not in _APPROVED_CONFIG_DIRS:
                violations.append(f"{relative}: config directory is not approved")
            try:
                with os.scandir(path) as entries:
                    pending.extend(Path(entry.path) for entry in entries)
            except OSError as error:
                violations.append(
                    f"{relative}: config directory is unreadable: {error}"
                )
        elif stat.S_ISREG(metadata.st_mode):
            approved_hash = _APPROVED_CONFIG_HASHES.get(relative)
            if approved_hash is None:
                violations.append(f"{relative}: config file is not approved")
                continue
            try:
                content = path.read_bytes()
                content.decode("utf-8")
            except (OSError, UnicodeError) as error:
                violations.append(
                    f"{relative}: config file is not readable UTF-8: {error}"
                )
                continue
            if hashlib.sha256(content).hexdigest() != approved_hash:
                violations.append(f"{relative}: SHA-256 does not match approved config")
        else:
            violations.append(f"{relative}: special config file is not allowed")
    violations.extend(
        f"{relative}: approved config path is missing"
        for relative in sorted(expected - seen)
    )
    return violations


def test_exact_config_hash_rejects_mutation(tmp_path: Path) -> None:
    config = tmp_path / "config/phase1.example.json"
    config.parent.mkdir()
    config.write_bytes((ROOT / "config/phase1.example.json").read_bytes())
    with config.open("ab") as manifest:
        manifest.write(b"\n")

    violations = _config_policy_violations(tmp_path)

    assert violations == [
        "config/phase1.example.json: SHA-256 does not match approved config"
    ]


def test_extra_physical_config_path_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "config/phase1.example.json"
    config.parent.mkdir()
    config.write_bytes((ROOT / "config/phase1.example.json").read_bytes())
    (tmp_path / "config/unreviewed.json").write_text("{}\n", encoding="utf-8")

    violations = _config_policy_violations(tmp_path)

    assert violations == ["config/unreviewed.json: config file is not approved"]


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
    label = "locked dependency group" if locked else "dependency group"
    for group, requirements in groups.items():
        allowed = _ALLOWED_DEPENDENCY_GROUPS.get(group)
        names = _dependency_names(requirements)
        if allowed is None:
            violations.append(f"{location}: {label} not allowlisted: {group}")
            continue
        allowed_names = _dependency_names(allowed)
        violations.extend(
            f"{location}: {label} {group} dependency not allowlisted: {name}"
            for name in sorted(names - allowed_names)
        )
        if not locked and requirements != allowed:
            violations.append(
                f"{location}: {label} {group} requirements differ from exact "
                "approved strings"
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


def _operational_policy_violations(root: Path) -> list[str]:
    violations: list[str] = []
    allowed_dirs = {"scripts"}
    allowed_hashes = {
        "scripts/policy_preflight.py": _APPROVED_PREFLIGHT_HASH,
        **_APPROVED_OPERATIONAL_HASHES,
    }
    seen: set[str] = set()
    for directory_name in ("scripts", "ops"):
        pending = [root / directory_name]
        while pending:
            path = pending.pop()
            relative = path.relative_to(root).as_posix()
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                violations.append(
                    f"{relative}: operational path is unreadable: {error}"
                )
                continue
            seen.add(relative)

            if stat.S_ISLNK(metadata.st_mode):
                violations.append(f"{relative}: operational symlink is not allowlisted")
            elif stat.S_ISDIR(metadata.st_mode):
                if relative not in allowed_dirs:
                    violations.append(
                        f"{relative}: operational directory is not allowlisted"
                    )
                if not metadata.st_mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH):
                    violations.append(
                        f"{relative}: operational directory is unreadable"
                    )
                    continue
                try:
                    with os.scandir(path) as entries:
                        pending.extend(Path(entry.path) for entry in entries)
                except OSError as error:
                    violations.append(
                        f"{relative}: operational directory is unreadable: {error}"
                    )
            elif stat.S_ISREG(metadata.st_mode):
                approved_hash = allowed_hashes.get(relative)
                if approved_hash is None:
                    violations.append(
                        f"{relative}: operational file is not allowlisted"
                    )
                if not metadata.st_mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH):
                    violations.append(f"{relative}: operational file is unreadable")
                    continue
                try:
                    content = path.read_bytes()
                    content.decode("utf-8")
                except (OSError, UnicodeError) as error:
                    violations.append(
                        f"{relative}: operational file is unreadable UTF-8: {error}"
                    )
                    continue
                if (
                    approved_hash
                    and hashlib.sha256(content).hexdigest() != approved_hash
                ):
                    violations.append(
                        f"{relative}: SHA-256 does not match approved operational file"
                    )
            else:
                violations.append(
                    f"{relative}: special operational file is not allowed"
                )
    violations.extend(
        f"{relative}: approved operational path is missing"
        for relative in sorted((allowed_dirs | allowed_hashes.keys()) - seen)
    )
    return violations


def _runtime_policy_violations(root: Path) -> list[str]:
    project_data = _project_data(root)
    project = project_data.get("project", {})
    allowed_runtime = {"exchange-calendars", "pydantic", "typer"}
    runtime_requirements = project.get("dependencies", [])
    runtime_names = _dependency_names(runtime_requirements)
    violations = [
        f"pyproject.toml: runtime dependency not allowlisted: {name}"
        for name in sorted(runtime_names - allowed_runtime)
    ]
    if runtime_requirements != _ALLOWED_RUNTIME_REQUIREMENTS:
        violations.append(
            "pyproject.toml: runtime dependency source or requirement differs from "
            "exact approved strings"
        )
    build_system = project_data.get("build-system", {})
    if build_system != _ALLOWED_BUILD_SYSTEM:
        violations.append(
            f"pyproject.toml: build-system not allowlisted: {build_system}"
        )
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
    for table in ("gui-scripts", "entry-points"):
        if table in project:
            violations.append(f"pyproject.toml: entry-point table not allowed: {table}")
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
        packages = lock_data.get("package", [])
        for package in packages:
            if (
                isinstance(package, dict)
                and package.get("name") != project_name
                and package.get("source") != _APPROVED_REGISTRY_SOURCE
            ):
                violations.append(
                    "uv.lock: third-party package "
                    f"{package.get('name')!r} has unapproved dependency source"
                )
        project_package = next(
            (
                package
                for package in packages
                if isinstance(package, dict) and package.get("name") == project_name
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

    workflow = root / ".github/workflows/ci.yml"
    try:
        workflow_hash = hashlib.sha256(workflow.read_bytes()).hexdigest()
    except OSError as error:
        violations.append(f".github/workflows/ci.yml: missing or unreadable: {error}")
    else:
        if workflow_hash != _APPROVED_WORKFLOW_HASH:
            violations.append(
                ".github/workflows/ci.yml: workflow SHA-256 does not match "
                "approved workflow"
            )

    violations.extend(_source_policy_violations(root))
    violations.extend(_config_policy_violations(root))
    violations.extend(_operational_policy_violations(root))
    return violations


def test_no_live_execution_surface() -> None:
    assert not _runtime_policy_violations(ROOT)


def _initialize_approved_source_fixture(tmp_path: Path) -> Path:
    package = tmp_path / "src/jev_trading_lab"
    package.mkdir(parents=True)
    for relative in (
        "__init__.py",
        "cli.py",
        "core/ids.py",
        "core/manifest.py",
        "core/observations.py",
        "core/run_window.py",
        "core/time.py",
        "core/values.py",
        "persistence/__init__.py",
        "persistence/db.py",
        "persistence/migrations.py",
        "persistence/schema/__init__.py",
        "persistence/schema/001_migration_metadata.sql",
        "stocks/__init__.py",
        "stocks/calendar.py",
    ):
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((SRC / "jev_trading_lab" / relative).read_bytes())
    workflow = tmp_path / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_bytes((ROOT / ".github/workflows/ci.yml").read_bytes())
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("policy_preflight.py", "wheel_migration_smoke.py"):
        (scripts / name).write_bytes((ROOT / "scripts" / name).read_bytes())
    config = tmp_path / "config/phase1.example.json"
    config.parent.mkdir()
    config.write_bytes((ROOT / "config/phase1.example.json").read_bytes())
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


@pytest.mark.parametrize("directory_name", ("scripts", "ops"))
def test_git_ignored_operational_directory_symlink_is_rejected(
    directory_name: str, tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_bytes((ROOT / "pyproject.toml").read_bytes())
    _initialize_ignored_source_fixture(tmp_path)
    relative = f"{directory_name}/external"
    with (tmp_path / ".gitignore").open("a", encoding="utf-8") as ignore_file:
        ignore_file.write(f"{relative}\n")
    external = tmp_path / "external-operations"
    external.mkdir()
    (external / "live.sh").write_text("#!/bin/sh\nexec live-order\n", encoding="utf-8")
    guarded = tmp_path / directory_name
    guarded.mkdir(exist_ok=True)
    (guarded / "external").symlink_to(external, target_is_directory=True)
    subprocess.run(
        ["git", "check-ignore", relative],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    violations = _runtime_policy_violations(tmp_path)

    assert any(relative in item and "symlink" in item for item in violations), (
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


def _copy_preflight_fixture(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for relative in ("pyproject.toml", "uv.lock"):
        (repo / relative).write_bytes((ROOT / relative).read_bytes())
    for relative in (".github/workflows/ci.yml",):
        target = repo / relative
        target.parent.mkdir(parents=True)
        target.write_bytes((ROOT / relative).read_bytes())
    shutil.copytree(ROOT / "src", repo / "src")
    shutil.copytree(ROOT / "config", repo / "config")
    scripts = ROOT / "scripts"
    if scripts.exists():
        shutil.copytree(scripts, repo / "scripts", symlinks=True)
    return repo


def _run_preflight(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", "-I", "scripts/policy_preflight.py"],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": ""},
        check=False,
    )


def test_same_name_local_typer_valid_lock_is_rejected(tmp_path: Path) -> None:
    repo = _copy_preflight_fixture(tmp_path)
    vendor = repo / "vendor/typer"
    vendor.mkdir(parents=True)
    (vendor / "pyproject.toml").write_text(
        '[project]\nname="typer"\nversion="0.19.2"\n'
        '[build-system]\nrequires=[]\nbuild-backend="vendor_backend"\n'
        'backend-path=["."]\n',
        encoding="utf-8",
    )
    (vendor / "vendor_backend.py").write_text(
        "def build_wheel(*args):\n    raise RuntimeError('not built during lock')\n",
        encoding="utf-8",
    )
    project = (repo / "pyproject.toml").read_text(encoding="utf-8")
    project = project.replace('"typer>=0.19,<1"', f'"typer @ {vendor.as_uri()}"', 1)
    (repo / "pyproject.toml").write_text(project, encoding="utf-8")
    subprocess.run(["uv", "lock"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(
        ["uv", "lock", "--check"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )

    violations = _runtime_policy_violations(repo)
    preflight = _run_preflight(repo)

    assert any("dependency source" in item for item in violations), violations
    assert preflight.returncode != 0
    assert "pyproject.toml" in preflight.stderr
    assert "uv.lock" in preflight.stderr


@pytest.mark.parametrize(
    "replacement",
    (
        "typer @ file:///tmp/vendor/typer",
        "typer @ https://example.test/typer-0.19.2.tar.gz",
        "typer @ git+https://example.test/typer.git@reviewed",
        "typer @ ./vendor/typer",
    ),
)
def test_preflight_rejects_non_registry_requirement_sources(
    replacement: str, tmp_path: Path
) -> None:
    repo = _copy_preflight_fixture(tmp_path)
    project = (repo / "pyproject.toml").read_text(encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        project.replace("typer>=0.19,<1", replacement, 1), encoding="utf-8"
    )

    result = _run_preflight(repo)

    assert result.returncode != 0
    assert "pyproject.toml" in result.stderr


@pytest.mark.parametrize(
    "source_config",
    (
        '[tool.uv.sources]\ntyper={path="vendor/typer", editable=true}\n',
        '[[tool.uv.index]]\nname="alternate"\nurl="https://example.test/simple"\n'
        '[tool.uv.sources]\ntyper={index="alternate"}\n',
    ),
)
def test_preflight_rejects_editable_or_alternate_index_source(
    source_config: str, tmp_path: Path
) -> None:
    repo = _copy_preflight_fixture(tmp_path)
    with (repo / "pyproject.toml").open("a", encoding="utf-8") as project:
        project.write(source_config)

    result = _run_preflight(repo)

    assert result.returncode != 0
    assert "pyproject.toml" in result.stderr


def test_preflight_rejects_non_registry_lock_source(tmp_path: Path) -> None:
    repo = _copy_preflight_fixture(tmp_path)
    lock = (repo / "uv.lock").read_text(encoding="utf-8")
    typer_registry = (
        'name = "typer"\nversion = "0.27.2"\n'
        'source = { registry = "https://pypi.org/simple" }'
    )
    lock = lock.replace(
        typer_registry,
        'name = "typer"\nversion = "0.27.2"\nsource = { directory = "vendor/typer" }',
        1,
    )
    (repo / "uv.lock").write_text(lock, encoding="utf-8")

    result = _run_preflight(repo)

    assert result.returncode != 0
    assert "uv.lock" in result.stderr


def test_preflight_rejects_registry_artifact_substitution(tmp_path: Path) -> None:
    repo = _copy_preflight_fixture(tmp_path)
    for relative in ("README.md", "THIRD_PARTY_NOTICES.md"):
        (repo / relative).write_bytes((ROOT / relative).read_bytes())
    marker = tmp_path / "malicious-wheel-executed"
    wheel = tmp_path / "annotated_doc-0.0.5-py3-none-any.whl"
    dist_info = "annotated_doc-0.0.5.dist-info"
    pth = "annotated_doc.pth"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            pth,
            f"import pathlib; pathlib.Path({str(marker)!r}).write_text('executed')\n",
        )
        archive.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.1\nName: annotated-doc\nVersion: 0.0.5\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            f"{dist_info}/RECORD",
            f"{pth},,\n{dist_info}/METADATA,,\n{dist_info}/WHEEL,,\n"
            f"{dist_info}/RECORD,,\n",
        )

    lock_path = repo / "uv.lock"
    lock = lock_path.read_text(encoding="utf-8")
    lock_data = tomllib.loads(lock)
    package = next(
        item for item in lock_data["package"] if item["name"] == "annotated-doc"
    )
    approved_artifact = package["wheels"][0]
    malicious_hash = f"sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}"
    lock = lock.replace(approved_artifact["url"], wheel.as_uri(), 1)
    lock = lock.replace(approved_artifact["hash"], malicious_hash, 1)
    lock_path.write_text(lock, encoding="utf-8")

    result = _run_preflight(repo)
    if result.returncode == 0:
        subprocess.run(
            ["uv", "lock", "--check"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        environment = tmp_path / "venv"
        sync = subprocess.run(
            ["uv", "sync", "--frozen", "--all-groups"],
            cwd=repo,
            env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(environment)},
            check=False,
            capture_output=True,
            text=True,
        )
        assert sync.returncode == 0, sync.stderr
        subprocess.run(
            [str(environment / "bin/python"), "-c", "pass"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )

    assert result.returncode != 0 and not marker.exists(), (
        f"preflight_returncode={result.returncode} marker_exists={marker.exists()}"
    )
    assert "uv.lock" in result.stderr


def test_preflight_checks_lock_hash_before_semantic_toml(tmp_path: Path) -> None:
    repo = _copy_preflight_fixture(tmp_path)
    with (repo / "uv.lock").open("ab") as lock:
        lock.write(b"\n# unreviewed lock mutation\n")
    (repo / "pyproject.toml").write_text("not valid TOML = [", encoding="utf-8")

    result = _run_preflight(repo)

    assert result.returncode != 0
    assert result.stderr.splitlines()[0].startswith(
        "policy preflight: uv.lock: SHA-256"
    )


def test_ci_order_preflight_blocks_local_backend_before_uv(tmp_path: Path) -> None:
    repo = _copy_preflight_fixture(tmp_path)
    marker = repo / "backend-executed"
    project = (repo / "pyproject.toml").read_text(encoding="utf-8")
    project = project.replace(
        'build-backend = "hatchling.build"',
        'build-backend = "malicious"\nbackend-path = ["backend"]',
        1,
    )
    (repo / "pyproject.toml").write_text(project, encoding="utf-8")
    backend = repo / "backend/malicious.py"
    backend.parent.mkdir()
    backend.write_text(
        "from pathlib import Path\n"
        "Path('backend-executed').write_text('executed')\n"
        "from hatchling.build import *  # noqa: F403\n",
        encoding="utf-8",
    )

    preflight = repo / "scripts/policy_preflight.py"
    if preflight.is_file():
        result = _run_preflight(repo)
    else:
        result = subprocess.CompletedProcess(["preflight-missing"], 0, "", "")
    if result.returncode == 0:
        subprocess.run(
            ["uv", "lock", "--check"],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["uv", "sync", "--frozen", "--all-groups"],
            cwd=repo,
            env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(tmp_path / "venv")},
            check=False,
            capture_output=True,
            text=True,
        )

    assert result.returncode != 0
    assert not marker.exists(), "local backend executed before policy rejection"


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


def test_backend_path_build_system_is_rejected(tmp_path: Path) -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project = project.replace(
        'build-backend = "hatchling.build"\n',
        'build-backend = "hatchling.build"\nbackend-path = ["."]\n',
        1,
    )
    (tmp_path / "pyproject.toml").write_text(project, encoding="utf-8")
    _initialize_approved_source_fixture(tmp_path)
    backend = tmp_path / "hatchling/build.py"
    backend.parent.mkdir()
    backend.write_text(
        "raise RuntimeError('local backend executed')\n", encoding="utf-8"
    )

    violations = _runtime_policy_violations(tmp_path)

    assert any("build-system" in item for item in violations), violations


def test_locked_build_configuration() -> None:
    project_data = _project_data()
    build_system = project_data.get("build-system", {})
    assert build_system == {
        "requires": ["hatchling==1.27.0"],
        "build-backend": "hatchling.build",
    }
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
    assert locked["exchange-calendars"] == "4.11.2"


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
    hash_check = (
        f'echo "{_APPROVED_PREFLIGHT_HASH}  scripts/policy_preflight.py" '
        "| sha256sum -c -"
    )
    preflight = "run: python3 -I scripts/policy_preflight.py"
    assert hash_check in workflow
    assert preflight in workflow
    assert workflow.index(hash_check) < workflow.index(preflight)
    assert workflow.index(preflight) < workflow.index("astral-sh/setup-uv")
    assert workflow.index(preflight) < workflow.index("uv lock --check")
    assert workflow.index(preflight) < workflow.index("uv sync --frozen --all-groups")
    assert "uv export --frozen --all-groups --no-emit-project" in workflow
    assert "--format requirements-txt --output-file audit-requirements.txt" in workflow
    assert "uv run pip-audit --strict --requirement audit-requirements.txt" in workflow
    assert "--no-deps --disable-pip" in workflow
    assert "run: uv build --no-build-isolation" in workflow
    assert "run: uv build\n" not in workflow


def test_ci_builds_before_actual_wheel_migration_smoke() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    build = "run: uv build --no-build-isolation"
    smoke = "run: uv run python scripts/wheel_migration_smoke.py"
    assert smoke in workflow
    assert workflow.index(build) < workflow.index(smoke)

    script = ROOT / "scripts/wheel_migration_smoke.py"
    assert script.is_file()
    approved_hash = _APPROVED_OPERATIONAL_HASHES[script.relative_to(ROOT).as_posix()]
    assert hashlib.sha256(script.read_bytes()).hexdigest() == approved_hash


def test_workflow_hash_accepts_current_reviewed_workflow() -> None:
    assert (
        hashlib.sha256((ROOT / ".github/workflows/ci.yml").read_bytes()).hexdigest()
        == _APPROVED_WORKFLOW_HASH
    )


def test_workflow_hash_rejects_appended_run_step(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_bytes((ROOT / "pyproject.toml").read_bytes())
    _initialize_approved_source_fixture(tmp_path)
    workflow = tmp_path / ".github/workflows/ci.yml"
    reviewed = (ROOT / ".github/workflows/ci.yml").read_bytes()
    workflow.write_bytes(
        reviewed + b"      - run: curl -X POST https://example.test/orders\n"
    )

    violations = _runtime_policy_violations(tmp_path)

    assert any("workflow SHA-256" in violation for violation in violations), violations


@pytest.mark.parametrize(
    "extra_table",
    (
        '[project.gui-scripts]\ntrade-gui="jev_trading_lab.cli:app"\n',
        '[project.entry-points.live]\nsubmit-order="jev_trading_lab.cli:app"\n',
    ),
)
def test_policy_rejects_unapproved_entry_point_tables(
    extra_table: str, tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8") + extra_table,
        encoding="utf-8",
    )
    _initialize_approved_source_fixture(tmp_path)

    violations = _runtime_policy_violations(tmp_path)

    assert any("entry-point table not allowed" in item for item in violations), (
        violations
    )


def _wheel_entry_point_violations(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        metadata_files = [
            name for name in archive.namelist() if name.endswith("entry_points.txt")
        ]
        if len(metadata_files) != 1:
            return [f"wheel must contain one entry_points.txt: {metadata_files}"]
        parser = configparser.ConfigParser()
        parser.read_string(archive.read(metadata_files[0]).decode("utf-8"))
    entry_points = {section: dict(parser[section]) for section in parser.sections()}
    allowed = {"console_scripts": _ALLOWED_ENTRY_POINTS}
    return (
        []
        if entry_points == allowed
        else [f"wheel entry points not allowed: {entry_points}"]
    )


def test_wheel_metadata_rejects_unapproved_entry_points(tmp_path: Path) -> None:
    wheel = tmp_path / "sample.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "sample-0.1.0.dist-info/entry_points.txt",
            "[console_scripts]\njev-lab = jev_trading_lab.cli:app\n"
            "[gui_scripts]\ntrade-gui = jev_trading_lab.cli:app\n",
        )

    violations = _wheel_entry_point_violations(wheel)

    assert any("gui_scripts" in item for item in violations), violations


def test_built_artifacts_exclude_repository_and_host_junk(tmp_path: Path) -> None:
    output = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--no-build-isolation", "--out-dir", str(output)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    sdist = next(output.glob("*.tar.gz"))
    wheel = next(output.glob("*.whl"))

    with tarfile.open(sdist, "r:gz") as archive:
        sdist_members = {}
        for member in archive.getmembers():
            extracted = archive.extractfile(member)
            if extracted is not None:
                sdist_members[member.name] = extracted.read()
    with zipfile.ZipFile(wheel) as archive:
        wheel_members = {name: archive.read(name) for name in archive.namelist()}

    for members in (sdist_members, wheel_members):
        names = tuple(members)
        assert not any(".git" in Path(name).parts for name in names), names
        assert not any("__pycache__" in Path(name).parts for name in names), names
        assert not any("dist" in Path(name).parts for name in names), names
        assert not any(name.endswith((".pyc", ".pyo")) for name in names), names
        content = b"\n".join(members.values())
        assert b"/home/" not in content
        assert b"persist-credentials" not in content
        assert b"GITHUB_TOKEN" not in content
        assert b"PRIVATE KEY-----" not in content

    package_files = {
        name
        for name in wheel_members
        if not any(part.endswith(".dist-info") for part in Path(name).parts)
    }
    assert package_files == {
        "jev_trading_lab/__init__.py",
        "jev_trading_lab/artifacts/xnys-schedule-2020-2036.json",
        "jev_trading_lab/cli.py",
        "jev_trading_lab/core/ids.py",
        "jev_trading_lab/core/manifest.py",
        "jev_trading_lab/core/observations.py",
        "jev_trading_lab/core/run_window.py",
        "jev_trading_lab/core/time.py",
        "jev_trading_lab/core/values.py",
        "jev_trading_lab/persistence/__init__.py",
        "jev_trading_lab/persistence/db.py",
        "jev_trading_lab/persistence/migrations.py",
        "jev_trading_lab/persistence/schema/__init__.py",
        "jev_trading_lab/persistence/schema/001_migration_metadata.sql",
        "jev_trading_lab/stocks/__init__.py",
        "jev_trading_lab/stocks/calendar.py",
    }
    assert not _wheel_entry_point_violations(wheel)

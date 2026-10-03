#!/usr/bin/env python3
"""Install the built wheel and apply packaged migrations outside the source tree."""

from __future__ import annotations

import os
import subprocess
import tempfile
import venv
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    wheels = sorted((root / "dist").glob("jev_trading_lab-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected exactly one built wheel, found {len(wheels)}")
    with tempfile.TemporaryDirectory(prefix="jev-wheel-smoke-") as temporary:
        outside = Path(temporary)
        environment = outside / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = environment / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        subprocess.run(
            [
                str(python),
                "-c",
                "import sys; assert sys.version_info[:2] == (3, 12), sys.version",
            ],
            cwd=outside,
            check=True,
        )
        subprocess.run(
            [str(python), "-I", "-m", "pip", "install", "--no-deps", str(wheels[0])],
            cwd=outside,
            check=True,
        )
        code = """
import hashlib
import sqlite3
import tempfile
from pathlib import Path
from jev_trading_lab.persistence.db import connect_database
from jev_trading_lab.persistence.migrations import apply_migrations, load_migrations

migrations = load_migrations()
assert [item.version for item in migrations] == [1, 2, 3]
assert [item.name for item in migrations] == [
    "001_migration_metadata.sql",
    "002_core_entities.sql",
    "003_append_only_guards.sql",
]
assert all(
    item.sha256 == hashlib.sha256(item.sql.encode()).hexdigest()
    for item in migrations
)
with tempfile.TemporaryDirectory(prefix="jev-wheel-db-") as temporary:
    database = Path(temporary).resolve() / "private" / "lab.sqlite3"
    connection = connect_database(
        database,
        busy_timeout_ms=1000,
        mount_resolver=lambda _path: "ext4",
    )
    try:
        apply_migrations(connection, clock=lambda: 1_700_000_000_000_000)
        rows = connection.execute(
            "SELECT version, name, sha256 FROM main.schema_migrations ORDER BY version"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (item.version, item.name, item.sha256) for item in migrations
        ]
    finally:
        connection.close()
print("Installed-wheel migration smoke passed.")
"""
        subprocess.run([str(python), "-I", "-c", code], cwd=outside, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

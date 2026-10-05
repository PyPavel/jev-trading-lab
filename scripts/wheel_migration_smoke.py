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
                "-B",
                "-c",
                "import sys; assert sys.version_info[:2] == (3, 12), sys.version",
            ],
            cwd=outside,
            check=True,
        )
        subprocess.run(
            [
                str(python),
                "-I",
                "-B",
                "-m",
                "pip",
                "install",
                "--no-deps",
                str(wheels[0]),
            ],
            cwd=outside,
            check=True,
        )
        code = """
import hashlib
import inspect
import sqlite3
import tempfile
from pathlib import Path
from jev_trading_lab.persistence.db import connect_database
from jev_trading_lab.persistence.journal import AppendOnlyJournal, make_journal_entity
from jev_trading_lab.persistence import migrations as migration_module
from jev_trading_lab.persistence.migrations import apply_migrations, load_migrations

assert tuple(inspect.signature(apply_migrations).parameters) == ("connection",)
assert not hasattr(migration_module, "_apply_migrations")
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
        apply_migrations(connection)
        rows = connection.execute(
            "SELECT version, name, sha256 FROM main.schema_migrations ORDER BY version"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (item.version, item.name, item.sha256) for item in migrations
        ]
        run_id = "a" * 64
        manifest_id = "b" * 64
        window_id = "c" * 64
        connection.execute(
            "INSERT INTO manifests VALUES (?,?,?,?)", (manifest_id, b"{}", 1, 1)
        )
        connection.execute(
            "INSERT INTO run_windows VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (window_id, b"{}", 0, 7257600000000, 7257600000001, "XNYS",
             "America/New_York", "test", "d" * 64, "e" * 64, "test", "f" * 64),
        )
        connection.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
            (run_id, manifest_id, window_id, "phase1", "paper", "1" * 64, 1),
        )
        entity = make_journal_entity(
            run_id=run_id,
            entity_type="entity",
            logical_identity="2" * 64,
            created_at_us=1,
            payload={"wheel": True},
        )
        assert AppendOnlyJournal(connection).append_entity(entity) == entity.entity_id
        for statement, parameters in (
            (
                "INSERT INTO journal_entities VALUES (?,?,?,?,?,?,?)",
                ("3" * 64, run_id, "entity", "4" * 64, 1, "5" * 64, b"not-json"),
            ),
            ("UPDATE journal_entities SET entity_id = entity_id", ()),
            ("DELETE FROM journal_entities", ()),
            ("DROP TABLE journal_entities", ()),
        ):
            try:
                connection.execute(statement, parameters)
            except sqlite3.DatabaseError as error:
                assert "not authorized" in str(error)
            else:
                raise AssertionError(
                    f"installed wheel permitted controlled write: {statement}"
                )
        assert (
            connection.execute("SELECT count(*) FROM journal_entities").fetchone()[0]
            == 1
        )
    finally:
        connection.close()
print("Installed-wheel migration and controlled journal write smoke passed.")
"""
        subprocess.run([str(python), "-I", "-B", "-c", code], cwd=outside, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

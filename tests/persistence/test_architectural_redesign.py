from __future__ import annotations

import ast
import gc
import hashlib
import inspect
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

from jev_trading_lab.persistence import db as db_module
from jev_trading_lab.persistence import journal as journal_module
from jev_trading_lab.persistence import migrations as migration_module
from jev_trading_lab.persistence.db import SecureConnection, connect_database
from jev_trading_lab.persistence.journal import AppendOnlyJournal, make_journal_entity
from jev_trading_lab.persistence.migrations import apply_migrations


def _connect(path: Path, timeout: int = 5_000) -> SecureConnection:
    connection = connect_database(
        path.absolute(),
        busy_timeout_ms=timeout,
        mount_resolver=lambda _path: "ext4",
    )
    assert type(connection) is SecureConnection
    return connection


def _hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _insert_run(connection: sqlite3.Connection) -> str:
    manifest_id = _hex("manifest")
    window_id = _hex("window")
    run_id = _hex("run")
    connection.execute(
        "INSERT INTO manifests VALUES (?,?,?,?)", (manifest_id, b"{}", 1, 1)
    )
    connection.execute(
        "INSERT INTO run_windows VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            window_id,
            b"{}",
            0,
            7_257_600_000_000,
            7_257_600_000_001,
            "XNYS",
            "America/New_York",
            "test",
            _hex("lock"),
            _hex("artifact"),
            "test",
            _hex("rule"),
        ),
    )
    connection.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
        (run_id, manifest_id, window_id, "phase1", "paper", _hex("config"), 1),
    )
    return run_id


def _assert_public_denied(connection: sqlite3.Connection, statement: str) -> None:
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        connection.execute(statement)


@pytest.mark.parametrize("hook", ("trace", "profile"))
def test_active_interpreter_hooks_cannot_borrow_journal_writer(
    tmp_path: Path, hook: str
) -> None:
    connection = _connect(tmp_path / hook / "db.sqlite3")
    try:
        apply_migrations(connection)
        run_id = _insert_run(connection)
        journal = AppendOnlyJournal(connection)
        record = make_journal_entity(
            run_id=run_id,
            entity_type="order_intent",
            logical_identity=_hex("logical"),
            created_at_us=100,
            payload={"value": 1},
        )
        attacks: list[str] = []
        armed = True

        def attack() -> None:
            nonlocal armed
            if not armed:
                return
            armed = False
            for sql in (
                "INSERT INTO main.journal_entities SELECT * FROM main.journal_entities",
                "DELETE FROM main.schema_migrations",
            ):
                try:
                    connection.execute(sql)
                except sqlite3.DatabaseError as error:
                    attacks.append(str(error))

        def trace_callback(_frame: object, _event: str, _arg: object) -> object:
            attack()
            return trace_callback

        def profile_callback(_frame: object, _event: str, _arg: object) -> None:
            attack()

        setter = sys.settrace if hook == "trace" else sys.setprofile
        setter(trace_callback if hook == "trace" else profile_callback)
        try:
            assert journal.append_entity(record) == record.entity_id
        finally:
            setter(None)
        assert len(attacks) == 2
        assert all("authorized" in error for error in attacks)
        _assert_public_denied(
            connection,
            "INSERT INTO main.journal_entities SELECT * FROM main.journal_entities",
        )
    finally:
        connection.close()


def test_signal_handler_cannot_borrow_journal_writer(tmp_path: Path) -> None:
    if not hasattr(signal, "SIGUSR1"):
        pytest.skip("SIGUSR1 is unavailable")
    connection = _connect(tmp_path / "signal" / "db.sqlite3")
    try:
        apply_migrations(connection)
        run_id = _insert_run(connection)
        journal = AppendOnlyJournal(connection)
        record = make_journal_entity(
            run_id=run_id,
            entity_type="order_intent",
            logical_identity=_hex("signal-logical"),
            created_at_us=100,
            payload={"value": 1},
        )
        attacks: list[str] = []

        def handler(_signum: int, _frame: object) -> None:
            try:
                connection.execute(
                    "INSERT INTO main.journal_entities "
                    "SELECT * FROM main.journal_entities"
                )
            except sqlite3.DatabaseError as error:
                attacks.append(str(error))

        previous = signal.signal(signal.SIGUSR1, handler)
        fired = False

        def trigger(_frame: object, _event: str, _arg: object) -> object:
            nonlocal fired
            if not fired:
                fired = True
                signal.raise_signal(signal.SIGUSR1)
            return trigger

        sys.settrace(trigger)
        try:
            assert journal.append_entity(record) == record.entity_id
        finally:
            sys.settrace(None)
            signal.signal(signal.SIGUSR1, previous)
        assert attacks and "not authorized" in attacks[0]
    finally:
        connection.close()


@pytest.mark.parametrize("hook", ("trace", "profile"))
def test_active_parent_hooks_cannot_borrow_migration_role(
    tmp_path: Path, hook: str
) -> None:
    connection = _connect(tmp_path / hook / "db.sqlite3")
    attacks: list[str] = []
    armed = True

    def attack() -> None:
        nonlocal armed
        if not armed:
            return
        armed = False
        for sql in (
            "CREATE TABLE main.forged(value INTEGER)",
            "INSERT INTO main.schema_migrations VALUES (99,'x','x',1)",
        ):
            try:
                connection.execute(sql)
            except sqlite3.DatabaseError as error:
                attacks.append(str(error))

    def trace_callback(_frame: object, _event: str, _arg: object) -> object:
        attack()
        return trace_callback

    def profile_callback(_frame: object, _event: str, _arg: object) -> None:
        attack()

    setter = sys.settrace if hook == "trace" else sys.setprofile
    setter(trace_callback if hook == "trace" else profile_callback)
    try:
        apply_migrations(connection)
    finally:
        setter(None)
    try:
        assert len(attacks) == 2
        assert all(
            "authorized" in error or "no such table" in error for error in attacks
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM main.schema_migrations"
            ).fetchone()[0]
            == 3
        )
        assert (
            connection.execute(
                "SELECT 1 FROM main.sqlite_schema WHERE name='forged'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


def test_all_thread_trace_does_not_change_public_policy_during_migration(
    tmp_path: Path,
) -> None:
    if not hasattr(threading, "settrace_all_threads"):
        pytest.skip("threading.settrace_all_threads is unavailable")
    connection = _connect(tmp_path / "all-thread" / "db.sqlite3")
    attacked = False

    def callback(_frame: object, _event: str, _arg: object) -> object:
        nonlocal attacked
        if not attacked:
            attacked = True
            _assert_public_denied(connection, "CREATE TABLE main.forged(value INTEGER)")
        return callback

    threading.settrace_all_threads(callback)
    try:
        apply_migrations(connection)
    finally:
        threading.settrace_all_threads(None)
    try:
        assert attacked
        assert (
            connection.execute(
                "SELECT count(*) FROM main.schema_migrations"
            ).fetchone()[0]
            == 3
        )
    finally:
        connection.close()


def test_production_has_no_authorizer_transitions_or_hook_preflights() -> None:
    db_source = inspect.getsource(db_module)
    journal_source = inspect.getsource(journal_module)
    migration_source = inspect.getsource(migration_module)
    forbidden = (
        "_privileged_authorizer_scope",
        "_journal_write_scope",
        "_restore_runtime_authorizer",
        "sys.gettrace",
        "sys.getprofile",
    )
    for token in forbidden:
        assert token not in db_source + journal_source + migration_source
    assert db_source.count("_set_sqlite_authorizer(") == 2


def test_fixed_journal_role_authorizer_truth_table() -> None:
    authorize = db_module._journal_authorizer("journal_entities")
    assert (
        authorize(sqlite3.SQLITE_INSERT, "journal_entities", None, "main", None)
        == sqlite3.SQLITE_OK
    )
    denied = (
        (sqlite3.SQLITE_INSERT, "journal_events", "main", None),
        (sqlite3.SQLITE_INSERT, "manifests", "main", None),
        (sqlite3.SQLITE_INSERT, "journal_entities", "temp", None),
        (sqlite3.SQLITE_INSERT, "journal_entities", "main", "trigger"),
        (sqlite3.SQLITE_UPDATE, "journal_entities", "main", None),
        (sqlite3.SQLITE_DELETE, "journal_entities", "main", None),
        (sqlite3.SQLITE_CREATE_TABLE, "anything", "main", None),
        (sqlite3.SQLITE_ATTACH, None, None, None),
    )
    for action, table, database, source in denied:
        assert authorize(action, table, None, database, source) == sqlite3.SQLITE_DENY


def test_append_rejects_schema_prefix_checksum_drift(tmp_path: Path) -> None:
    database = tmp_path / "schema-prefix" / "db.sqlite3"
    connection = _connect(database)
    apply_migrations(connection)
    run_id = _insert_run(connection)
    journal = AppendOnlyJournal(connection)
    sqlite3.Connection.set_authorizer(connection, None)
    connection.execute(
        "UPDATE schema_migrations SET sha256 = ? WHERE version = 3", ("0" * 64,)
    )
    sqlite3.Connection.set_authorizer(connection, db_module._runtime_authorizer)
    record = make_journal_entity(
        run_id=run_id,
        entity_type="order_intent",
        logical_identity=_hex("drift"),
        created_at_us=100,
        payload={"value": 1},
    )
    try:
        with pytest.raises(Exception, match="checksum|migration"):
            journal.append_entity(record)
    finally:
        connection.close()


def test_connection_close_uses_one_success_gated_cleanup_helper() -> None:
    helper = inspect.getsource(db_module._close_sqlite_with_handle)
    secure = inspect.getsource(db_module.SecureConnection.close)
    internal = inspect.getsource(db_module._InternalConnection.close)
    assert "finally:" in helper
    assert "if sqlite_closed:" in helper
    assert "_close_directory_handle(handle)" in helper
    assert "_close_sqlite_with_handle" in secure
    assert "_close_sqlite_with_handle" in internal


def test_secure_close_cooperative_cancellation_after_ownership_relinquishes_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _connect(tmp_path / "cooperative-close" / "db.sqlite3")
    handle = db_module._secure_slot(connection, "_directory_handle")
    assert isinstance(handle, db_module._DirectoryHandle)
    descriptor = handle.descriptor
    real_close = os.close

    def cancel_at_owned_close(fd: int) -> None:
        if fd == descriptor:
            raise KeyboardInterrupt("cooperative close cancellation")
        real_close(fd)

    monkeypatch.setattr(db_module.os, "close", cancel_at_owned_close)
    try:
        with pytest.raises(KeyboardInterrupt, match="cooperative close cancellation"):
            connection.close()
    finally:
        monkeypatch.undo()

    assert handle.closed
    real_close(descriptor)


def test_foreign_thread_close_preserves_live_connection_and_provenance(
    tmp_path: Path,
) -> None:
    database = tmp_path / "private" / "db.sqlite3"
    connection = _connect(database)
    handle = db_module._secure_slot(connection, "_directory_handle")
    assert isinstance(handle, db_module._DirectoryHandle)
    parent_descriptor = handle.descriptor
    failures: list[BaseException] = []

    def close_in_foreign_thread() -> None:
        try:
            connection.close()
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=close_in_foreign_thread)
    worker.start()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], sqlite3.ProgrammingError)
    assert connection.execute("SELECT 1").fetchone()[0] == 1
    assert db_module._is_factory_connection(connection)
    os.fstat(parent_descriptor)

    connection.close()
    assert handle.closed
    with pytest.raises(OSError):
        os.fstat(parent_descriptor)


def test_directory_handle_ambiguous_close_never_closes_reused_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    handle = db_module._DirectoryHandle(owned)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("safe", encoding="utf-8")
    real_close = os.close
    reused: int | None = None

    def close_then_interrupt(fd: int) -> None:
        nonlocal reused
        real_close(fd)
        replacement = os.open(sentinel, os.O_RDONLY)
        if replacement != fd:
            os.dup2(replacement, fd)
            real_close(replacement)
        reused = fd
        raise KeyboardInterrupt

    monkeypatch.setattr(db_module.os, "close", close_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        handle.close()
    assert handle.closed
    assert reused == owned

    handle.close()
    del handle
    gc.collect()
    assert os.fstat(owned).st_ino == sentinel.stat().st_ino
    real_close(owned)


def test_owned_production_fds_never_use_raw_os_close() -> None:
    for module in (db_module, migration_module):
        source = inspect.getsource(module)
        tree = ast.parse(source)
        raw_closes = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
            and node.func.attr == "close"
        ]
        assert len(raw_closes) == (0 if module is migration_module else 1)
        if module is db_module:
            assert "os.close(descriptor)" in inspect.getsource(
                db_module._DirectoryHandle.close
            )


def test_private_parent_success_transfers_live_handle(tmp_path: Path) -> None:
    parent = tmp_path / "private" / "nested"
    parent.mkdir(parents=True, mode=0o700)

    handle = db_module._open_private_parent(parent)
    assert isinstance(handle, db_module._DirectoryHandle)
    descriptor = handle.descriptor
    try:
        assert os.path.samefile(f"/proc/self/fd/{descriptor}", parent)
    finally:
        handle.close()
    assert handle.closed


def test_private_parent_keeps_ownership_across_function_boundary() -> None:
    source = inspect.getsource(db_module._open_private_parent)
    assert "-> _DirectoryHandle" in source
    assert ".release()" not in source
    assert "child_descriptor" not in source
    assert source.count("_DirectoryHandle(\n                    os.open(") == 2
    internal = inspect.getsource(db_module._internal_connection)
    public = inspect.getsource(db_module.connect_database)
    assert "_DirectoryHandle(_open_private_parent" not in internal + public


def test_private_parent_transition_error_closes_child_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "private" / "nested"
    parent.mkdir(parents=True, mode=0o700)
    before = {entry.name for entry in os.scandir("/proc/self/fd")}
    real_close = db_module._DirectoryHandle.close
    calls = 0

    def fail_first_transition(handle: db_module._DirectoryHandle) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transition failed")
        real_close(handle)

    monkeypatch.setattr(db_module._DirectoryHandle, "close", fail_first_transition)
    with pytest.raises(RuntimeError, match="transition failed"):
        db_module._open_private_parent(parent)
    gc.collect()

    after = {entry.name for entry in os.scandir("/proc/self/fd")}
    assert after == before


def test_private_parent_failure_cleanup_closes_final_descriptor(tmp_path: Path) -> None:
    parent = tmp_path / "public"
    parent.mkdir(mode=0o755)
    before = {entry.name for entry in os.scandir("/proc/self/fd")}

    with pytest.raises(Exception, match="mode 0700"):
        db_module._open_private_parent(parent)

    after = {entry.name for entry in os.scandir("/proc/self/fd")}
    assert after == before


def test_private_parent_failure_cleanup_cannot_close_reused_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "public"
    parent.mkdir(mode=0o755)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("safe", encoding="utf-8")
    real_close = os.close
    reused: int | None = None

    def close_then_interrupt(descriptor: int) -> None:
        nonlocal reused
        real_close(descriptor)
        replacement = os.open(sentinel, os.O_RDONLY)
        if replacement != descriptor:
            os.dup2(replacement, descriptor)
            real_close(replacement)
        reused = descriptor
        raise KeyboardInterrupt("cleanup interrupted after close")

    with monkeypatch.context() as context:
        context.setattr(db_module.os, "close", close_then_interrupt)
        with pytest.raises(KeyboardInterrupt, match="cleanup interrupted after close"):
            db_module._open_private_parent(parent)

    assert reused is not None
    assert os.fstat(reused).st_ino == sentinel.stat().st_ino
    real_close(reused)


def test_database_file_cleanup_cannot_close_reused_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("safe", encoding="utf-8")
    parent_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    real_close = os.close
    reused: int | None = None

    def close_then_interrupt(descriptor: int) -> None:
        nonlocal reused
        real_close(descriptor)
        replacement = os.open(sentinel, os.O_RDONLY)
        if replacement != descriptor:
            os.dup2(replacement, descriptor)
            real_close(replacement)
        reused = descriptor
        raise KeyboardInterrupt("file cleanup interrupted after close")

    try:
        with monkeypatch.context() as context:
            context.setattr(db_module.os, "close", close_then_interrupt)
            handle, _identity = db_module._prepare_database_file(
                parent / "database.sqlite3", parent_descriptor
            )
            with pytest.raises(KeyboardInterrupt, match="file cleanup interrupted"):
                db_module._close_directory_handle(handle)
        assert reused is not None
        assert os.fstat(reused).st_ino == sentinel.stat().st_ino
        real_close(reused)
    finally:
        real_close(parent_descriptor)


def test_migration_directory_cleanup_cannot_close_reused_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = tmp_path / "test_package"
    schema = package_root / "schema"
    schema.mkdir(parents=True)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("safe", encoding="utf-8")
    real_close = os.close
    reused: int | None = None
    interrupted = False

    def close_then_interrupt(descriptor: int) -> None:
        nonlocal interrupted, reused
        if interrupted:
            real_close(descriptor)
            return
        interrupted = True
        real_close(descriptor)
        replacement = os.open(sentinel, os.O_RDONLY)
        if replacement != descriptor:
            os.dup2(replacement, descriptor)
            real_close(replacement)
        reused = descriptor
        raise KeyboardInterrupt("directory cleanup interrupted after close")

    with monkeypatch.context() as context:
        context.setattr(migration_module.os, "close", close_then_interrupt)
        with pytest.raises(KeyboardInterrupt, match="directory cleanup interrupted"):
            migration_module._open_resource_directories(schema, "test_package.schema")

    assert reused is not None
    assert os.fstat(reused).st_ino == sentinel.stat().st_ino
    real_close(reused)


def test_migration_resource_cleanup_cannot_close_reused_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resource = tmp_path / "001_test.sql"
    resource.write_text("SELECT 1;\n", encoding="utf-8")
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("safe", encoding="utf-8")
    real_close = os.close
    reused: int | None = None

    def close_then_interrupt(descriptor: int) -> None:
        nonlocal reused
        real_close(descriptor)
        replacement = os.open(sentinel, os.O_RDONLY)
        if replacement != descriptor:
            os.dup2(replacement, descriptor)
            real_close(replacement)
        reused = descriptor
        raise KeyboardInterrupt("resource cleanup interrupted after close")

    with monkeypatch.context() as context:
        context.setattr(migration_module.os, "close", close_then_interrupt)
        with pytest.raises(KeyboardInterrupt, match="resource cleanup interrupted"):
            migration_module._read_filesystem_resource(resource)

    assert reused is not None
    assert os.fstat(reused).st_ino == sentinel.stat().st_ino
    real_close(reused)


def test_directory_handle_ordinary_close_has_no_leak(tmp_path: Path) -> None:
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    handle = db_module._DirectoryHandle(descriptor)

    handle.close()
    handle.close()

    assert handle.closed
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_directory_handle_finalizer_closes_owned_fd(tmp_path: Path) -> None:
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    handle = db_module._DirectoryHandle(descriptor)

    del handle
    gc.collect()

    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_internal_writer_cleanup_preserves_original_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        db_module,
        "_open_private_parent",
        lambda _path: db_module._DirectoryHandle(123),
    )

    def fail_stat(*_args: object, **_kwargs: object) -> os.stat_result:
        raise ValueError("original open failure")

    monkeypatch.setattr(db_module.os, "stat", fail_stat)
    monkeypatch.setattr(
        db_module.os,
        "close",
        lambda _fd: (_ for _ in ()).throw(RuntimeError("cleanup interrupted")),
    )
    with pytest.raises(ValueError, match="original open failure"):
        db_module._internal_connection(
            Path("/tmp/db.sqlite3"),
            timeout_ms=1000,
            authorizer=lambda *_args: sqlite3.SQLITE_DENY,
            expected_identity=(1, 2),
        )


def test_migration_helper_protocol_is_fixed() -> None:
    helper = __import__(
        "jev_trading_lab.persistence.migration_helper", fromlist=["PROTOCOL_VERSION"]
    )
    assert helper.PROTOCOL_VERSION == 1
    assert tuple(inspect.signature(helper.main).parameters) == ("argv",)


def test_migration_helper_protocol_rejects_invalid_inputs() -> None:
    helper = __import__(
        "jev_trading_lab.persistence.migration_helper", fromlist=["main"]
    )
    assert helper.main(("helper",)) == 1
    assert helper.main(("helper", "2", "/tmp/db.sqlite3", "1000")) == 1
    assert helper.main(("helper", "1", "relative.sqlite3", "1000")) == 1
    assert helper.main(("helper", "1", "/tmp/db.sqlite3", "0")) == 1
    assert helper.main(("helper", "1", "/tmp/a/../db.sqlite3", "1000")) == 1
    with pytest.raises(Exception, match="normalized"):
        helper._arguments(("helper", "1", "/tmp/a/../db.sqlite3", "1000", "1", "2"))


def test_migration_helper_output_is_capped_while_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = (
        sys.executable,
        "-B",
        "-c",
        "import os,time; os.write(1, b'x' * 20000); time.sleep(10)",
    )
    monkeypatch.setattr(migration_module, "_helper_command", lambda *_args: command)
    started = time.monotonic()
    with pytest.raises(Exception, match="output exceeded"):
        migration_module._run_migration_helper(Path("/tmp/db"), 1000, (1, 2))
    assert time.monotonic() - started < 2


def test_migration_helper_timeout_kills_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import pathlib,subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)"
    )
    command = (sys.executable, "-B", "-c", code, str(pid_file))
    monkeypatch.setattr(migration_module, "_helper_command", lambda *_args: command)
    monkeypatch.setattr(migration_module, "_HELPER_TIMEOUT_SECONDS", 0.2)
    with pytest.raises(Exception, match="timed out"):
        migration_module._run_migration_helper(Path("/tmp/db"), 1000, (1, 2))
    child_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 2
    while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{child_pid}").exists()


def test_migration_helper_eof_still_enforces_lifetime_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "descendant.pid"
    code = (
        "import os,pathlib,subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-B','-c',"
        "'import os,time; os.close(1); os.close(2); time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); "
        "time.sleep(0.02); os.close(1); os.close(2); time.sleep(1)"
    )
    command = (sys.executable, "-B", "-c", code, str(pid_file))
    monkeypatch.setattr(migration_module, "_helper_command", lambda *_args: command)
    monkeypatch.setattr(migration_module, "_HELPER_TIMEOUT_SECONDS", 0.1)

    started = time.monotonic()
    with pytest.raises(Exception, match="timed out"):
        migration_module._run_migration_helper(Path("/tmp/db"), 1000, (1, 2))
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    descendant_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 2
    while Path(f"/proc/{descendant_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{descendant_pid}").exists()


def test_successful_helper_cleans_same_group_descendant_after_parent_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "descendant.pid"
    reply = '{"protocol":1,"status":"ok"}'
    code = (
        "import os,pathlib,subprocess,sys; "
        "p=subprocess.Popen([sys.executable,'-B','-c',"
        "'import os,time; os.close(1); os.close(2); time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); "
        f"os.write(1,{reply.encode()!r})"
    )
    command = (sys.executable, "-B", "-c", code, str(pid_file))
    monkeypatch.setattr(migration_module, "_helper_command", lambda *_args: command)

    completed = migration_module._run_migration_helper(Path("/tmp/db"), 1000, (1, 2))

    assert completed.returncode == 0
    descendant_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 2
    while Path(f"/proc/{descendant_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{descendant_pid}").exists()


def test_migration_helper_wait_keyboardinterrupt_kills_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "wait.pid"
    command = (
        sys.executable,
        "-B",
        "-c",
        "import os,pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        "os.close(1); os.close(2); time.sleep(30)",
        str(pid_file),
    )
    real_popen = subprocess.Popen

    class InterruptingWait:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._process = real_popen(*args, **kwargs)  # type: ignore[arg-type]
            self.stdout = self._process.stdout
            self.stderr = self._process.stderr
            self.args = self._process.args
            self.pid = self._process.pid
            self._interrupted = False

        def wait(self, timeout: float | None = None) -> int:
            if not self._interrupted:
                self._interrupted = True
                raise KeyboardInterrupt("wait interrupted")
            return self._process.wait(timeout=timeout)

    monkeypatch.setattr(migration_module, "_helper_command", lambda *_args: command)
    monkeypatch.setattr(migration_module.subprocess, "Popen", InterruptingWait)
    with pytest.raises(KeyboardInterrupt, match="wait interrupted"):
        migration_module._run_migration_helper(Path("/tmp/db"), 1000, (1, 2))

    helper_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 2
    while Path(f"/proc/{helper_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{helper_pid}").exists()


def test_migration_helper_popen_enters_cleanup_region_immediately() -> None:
    source = inspect.getsource(migration_module._run_migration_helper)
    launch = source.index("process = subprocess.Popen(")
    outer_try = source.rfind("    try:", 0, launch)
    assert outer_try != -1
    assert "finally:" in source[launch:]


def test_migration_helper_baseexception_kills_and_reaps_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "helper.pid"
    command = (
        sys.executable,
        "-B",
        "-c",
        "import os,pathlib,subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()} {p.pid}'); "
        "time.sleep(30)",
        str(pid_file),
    )
    real_selector = migration_module.selectors.DefaultSelector

    class InterruptingSelector:
        def __init__(self) -> None:
            self._selector = real_selector()

        def register(self, fileobj: object, events: int) -> object:
            return self._selector.register(fileobj, events)  # type: ignore[arg-type]

        def get_map(self) -> object:
            return self._selector.get_map()

        def select(self, _timeout: float | None = None) -> object:
            deadline = time.monotonic() + 2
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert pid_file.exists()
            raise KeyboardInterrupt("selector interrupted")

        def close(self) -> None:
            self._selector.close()

    monkeypatch.setattr(migration_module, "_helper_command", lambda *_args: command)
    monkeypatch.setattr(
        migration_module.selectors, "DefaultSelector", InterruptingSelector
    )
    with pytest.raises(KeyboardInterrupt, match="selector interrupted"):
        migration_module._run_migration_helper(Path("/tmp/db"), 1000, (1, 2))

    helper_pid, child_pid = (int(value) for value in pid_file.read_text().split())
    deadline = time.monotonic() + 2
    while (
        any(Path(f"/proc/{pid}").exists() for pid in (helper_pid, child_pid))
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    remained = [pid for pid in (helper_pid, child_pid) if Path(f"/proc/{pid}").exists()]
    if remained:
        try:
            os.killpg(helper_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(helper_pid, 0)
        except ChildProcessError:
            pass
    assert not remained


@pytest.mark.parametrize(
    "code",
    (
        "import os; os.write(1, b'x' * 16385)",
        "import os; os.write(2, b'x' * 16385)",
        "import os; os.write(1, b'x' * 12000); os.write(2, b'y' * 12000)",
        "import os; "
        "[(os.write(1, b'x' * 1024), os.write(2, b'y' * 1024)) for _ in range(9)]",
        "import os; os.write(1, b'x' * 16384); os.write(2, b'!')",
    ),
    ids=("stdout", "stderr", "combined", "interleaved", "trailing"),
)
def test_migration_helper_enforces_one_combined_output_limit(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    command = (sys.executable, "-B", "-c", code)
    monkeypatch.setattr(migration_module, "_helper_command", lambda *_args: command)

    with pytest.raises(Exception, match="output exceeded"):
        migration_module._run_migration_helper(Path("/tmp/db"), 1000, (1, 2))


@pytest.mark.parametrize(
    "completed,match",
    (
        (subprocess.CompletedProcess([], 1, b"{}", b"failed"), "failed"),
        (subprocess.CompletedProcess([], 0, b"not json", b""), "malformed"),
        (
            subprocess.CompletedProcess([], 0, b"x" * 16_385, b""),
            "exceeded",
        ),
    ),
)
def test_migration_helper_reply_is_bounded_and_strict(
    completed: subprocess.CompletedProcess[bytes], match: str
) -> None:
    with pytest.raises(Exception, match=match):
        migration_module._validate_helper_reply(completed)


def test_post_commit_reply_loss_uses_database_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _connect(tmp_path / "reply-loss" / "db.sqlite3")
    real_run = migration_module._run_migration_helper

    def lose_reply(
        path: Path, timeout_ms: int, identity: tuple[int, int]
    ) -> subprocess.CompletedProcess[bytes]:
        completed = real_run(path, timeout_ms, identity)
        assert completed.returncode == 0
        return subprocess.CompletedProcess(completed.args, 17, b"", b"lost reply")

    monkeypatch.setattr(migration_module, "_run_migration_helper", lose_reply)
    try:
        apply_migrations(connection)
        assert (
            connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
            == 3
        )
    finally:
        connection.close()


@pytest.mark.parametrize("reply", (b"", b"{", b'{"protocol":1'))
def test_complete_prefix_wins_over_malformed_success_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: bytes
) -> None:
    connection = _connect(tmp_path / f"malformed-{len(reply)}" / "db.sqlite3")
    real_run = migration_module._run_migration_helper

    def corrupt_reply(
        path: Path, timeout_ms: int, identity: tuple[int, int]
    ) -> subprocess.CompletedProcess[bytes]:
        completed = real_run(path, timeout_ms, identity)
        assert completed.returncode == 0
        return subprocess.CompletedProcess(completed.args, 0, reply, b"")

    monkeypatch.setattr(migration_module, "_run_migration_helper", corrupt_reply)
    try:
        apply_migrations(connection)
        assert (
            connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
            == 3
        )
    finally:
        connection.close()


def test_post_commit_helper_exception_uses_database_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _connect(tmp_path / "helper-exception" / "db.sqlite3")
    real_run = migration_module._run_migration_helper

    def lose_outcome(
        path: Path, timeout_ms: int, identity: tuple[int, int]
    ) -> subprocess.CompletedProcess[bytes]:
        completed = real_run(path, timeout_ms, identity)
        assert completed.returncode == 0
        raise migration_module.MigrationError("helper outcome lost")

    monkeypatch.setattr(migration_module, "_run_migration_helper", lose_outcome)
    try:
        apply_migrations(connection)
        assert (
            connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
            == 3
        )
    finally:
        connection.close()


def test_private_writer_rejects_database_inode_replacement(tmp_path: Path) -> None:
    path = tmp_path / "inode" / "db.sqlite3"
    connection = _connect(path)
    try:
        apply_migrations(connection)
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, path)
        with pytest.raises(Exception, match="inode|identity|changed"):
            apply_migrations(connection)
    finally:
        connection.close()


def test_private_writer_binds_sqlite_fd_across_swap_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "real" / "db.sqlite3"
    connection = _connect(database)
    apply_migrations(connection)
    run_id = _insert_run(connection)
    journal = AppendOnlyJournal(connection)
    record = make_journal_entity(
        run_id=run_id,
        entity_type="order_intent",
        logical_identity=_hex("swap-restore"),
        created_at_us=100,
        payload={"value": 1},
    )

    decoy = tmp_path / "decoy" / "db.sqlite3"
    decoy_connection = _connect(decoy)
    apply_migrations(decoy_connection)
    assert _insert_run(decoy_connection) == run_id
    decoy_connection.close()

    parked_real = tmp_path / "parked-real.sqlite3"
    parked_decoy = tmp_path / "parked-decoy.sqlite3"
    real_connect = sqlite3.connect

    def swap_restore(*args: object, **kwargs: object) -> sqlite3.Connection:
        database.rename(parked_real)
        decoy.rename(database)
        writer = real_connect(*args, **kwargs)  # type: ignore[arg-type]
        database.rename(parked_decoy)
        parked_real.rename(database)
        return writer

    monkeypatch.setattr(db_module.sqlite3, "connect", swap_restore)
    try:
        with pytest.raises(Exception, match="inode|identity|descriptor"):
            journal.append_entity(record)
    finally:
        connection.close()

    for path in (database, parked_decoy):
        check = real_connect(path)
        try:
            assert (
                check.execute(
                    "SELECT count(*) FROM journal_entities WHERE entity_id = ?",
                    (record.entity_id,),
                ).fetchone()[0]
                == 0
            )
        finally:
            check.close()


def test_audit_hook_capability_theft_policy_is_explicit() -> None:
    sources = (
        inspect.getsource(db_module.SecureConnection),
        inspect.getsource(journal_module.AppendOnlyJournal),
        inspect.getsource(migration_module.apply_migrations),
    )
    policy = "\n".join(sources).casefold()
    assert "sys.addaudithook" in policy
    assert "handle" in policy and "capture" in policy
    assert "out of scope" in policy
    assert "passive audit hooks" in policy and "supported" in policy


def test_source_policy_states_precise_lifetime_boundary() -> None:
    policy = " ".join(
        "\n".join(
            (
                inspect.getsource(db_module),
                inspect.getsource(journal_module.AppendOnlyJournal),
                inspect.getsource(migration_module),
            )
        ).split()
    ).casefold()
    acquisition_exclusion = (
        "asynchronous python signal/keyboardinterrupt/systemexit delivery during "
        "native resource acquisition or before python ownership/cleanup guard is "
        "established"
    )
    required = (
        "passive trace/profile/audit hooks",
        (
            "cooperative cancellation at documented safe points after ownership "
            "is established"
        ),
        (
            "exceptions during blocking operations where cleanup guards already "
            "own resources"
        ),
        "public api callbacks using the public handle",
        "trusted fixed packaged helper",
        "ordinary hangs, crashes, closed pipes, same-group descendants, "
        "and output overflow",
        acquisition_exclusion,
        "exact bytecode between c resource acquisition and python ownership assignment",
        "frame inspection",
        "private helper import",
        "base descriptors",
        "process memory",
        "setsid escape",
        "does not promise acquisition or bytecode atomicity",
    )
    for claim in required:
        assert claim in policy
    broad_signal_claims = (
        "ordinary signal cancellation",
        "asynchronous signal cancellation",
    )
    if any(claim in policy for claim in broad_signal_claims):
        assert acquisition_exclusion in policy
    assert "ordinary signal cancellation" not in policy
    forbidden_overclaims = (
        "every baseexception after popen",
        "trace interruption after real close cannot",
        "all interpreter callbacks are contained",
    )
    for claim in forbidden_overclaims:
        assert claim not in policy


def test_passive_audit_hook_observes_migration_without_changing_result(
    tmp_path: Path,
) -> None:
    code = """
import sys
from pathlib import Path
from jev_trading_lab.persistence.db import connect_database
from jev_trading_lab.persistence.migrations import apply_migrations
events = []
sys.addaudithook(lambda event, args: events.append(event))
path = Path(sys.argv[1])
connection = connect_database(
    path, busy_timeout_ms=1000, mount_resolver=lambda _: 'ext4'
)
apply_migrations(connection)
assert connection.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] == 3
connection.close()
assert events
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", code, str(tmp_path / "audit" / "db.sqlite3")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_parent_database_descriptors_are_not_inheritable(tmp_path: Path) -> None:
    connection = _connect(tmp_path / "fds" / "db.sqlite3")
    path = tmp_path / "fds" / "db.sqlite3"
    targets: list[tuple[int, str]] = []
    for entry in Path("/proc/self/fd").iterdir():
        try:
            target = entry.readlink().as_posix()
            fd = int(entry.name)
        except (OSError, ValueError):
            continue
        if target.startswith(str(path)):
            targets.append((fd, target))
    try:
        assert targets
        assert all(not os.get_inheritable(fd) for fd, _target in targets)
    finally:
        connection.close()


def test_built_wheel_contains_helper_and_migrations() -> None:
    wheels = tuple((Path(__file__).resolve().parents[2] / "dist").glob("*.whl"))
    if not wheels:
        pytest.skip("built wheel is checked in required-artifact mode")
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
    assert "jev_trading_lab/persistence/migration_helper.py" in names
    for version in ("001", "002", "003"):
        assert any(
            name.startswith(f"jev_trading_lab/persistence/schema/{version}_")
            and name.endswith(".sql")
            for name in names
        )

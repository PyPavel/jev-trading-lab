from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from jev_trading_lab.core.ids import canonical_hash
from jev_trading_lab.persistence import journal as journal_module
from jev_trading_lab.persistence.db import (
    DatabaseConfigurationError,
    SecureConnection,
    _journal_authorizer,
    _runtime_authorizer,
    _seal_runtime_authorizer,
    connect_database,
)
from jev_trading_lab.persistence.journal import (
    AppendOnlyJournal,
    JournalConflictError,
    JournalEntity,
    JournalEvent,
    JournalRepositoryError,
    make_journal_entity,
    make_journal_event,
)
from jev_trading_lab.persistence.migrations import apply_migrations, load_migrations

HEX_A = "a" * 64
HEX_B = "b" * 64
HEX_C = "c" * 64
TABLES = ("journal_entities", "journal_events")


def _hex(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _connection(path: str | None = None) -> sqlite3.Connection:
    if path is None:
        descriptor, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(descriptor)
    connection = sqlite3.connect(path, isolation_level=None, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA trusted_schema = OFF")
    apply_migrations(connection)
    return connection


def _secure_connection(path: Path | None = None) -> SecureConnection:
    if path is None:
        path = Path(tempfile.mkdtemp()) / "journal.sqlite3"
    connection = connect_database(
        path.absolute(),
        busy_timeout_ms=5000,
        mount_resolver=lambda _path: "ext4",
    )
    assert isinstance(connection, SecureConnection)
    apply_migrations(connection)
    return connection


def _insert_run(connection: sqlite3.Connection, label: str = "one") -> str:
    manifest_id = _hex(f"manifest-{label}")
    window_id = _hex(f"window-{label}")
    run_id = _hex(f"run-{label}")
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
            _hex(f"lock-{label}"),
            _hex(f"artifact-{label}"),
            "test",
            _hex(f"rule-{label}"),
        ),
    )
    connection.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
        (run_id, manifest_id, window_id, "phase1", "paper", _hex(label), 1),
    )
    return run_id


def _entity(
    run_id: str,
    *,
    entity_type: str = "order_intent",
    logical_identity: str = HEX_A,
    created_at_us: int = 100,
    payload: object | None = None,
) -> JournalEntity:
    return make_journal_entity(
        run_id=run_id,
        entity_type=entity_type,
        logical_identity=logical_identity,
        created_at_us=created_at_us,
        payload={"value": 1} if payload is None else payload,
    )


def _event(
    entity_id: str,
    sequence: int,
    *,
    occurred_at_us: int | None = None,
    event_type: str = "recorded",
    payload: object | None = None,
    supersedes_event_id: str | None = None,
) -> JournalEvent:
    return make_journal_event(
        entity_id=entity_id,
        event_type=event_type,
        sequence=sequence,
        occurred_at_us=100 + sequence if occurred_at_us is None else occurred_at_us,
        payload={"sequence": sequence} if payload is None else payload,
        supersedes_event_id=supersedes_event_id,
    )


def _raw_entity(
    connection: sqlite3.Connection,
    run_id: str,
    label: str,
    *,
    entity_type: str = "order_intent",
    logical_identity: str | None = None,
) -> str:
    record = make_journal_entity(
        run_id=run_id,
        entity_type=entity_type,
        logical_identity=logical_identity or _hex(f"logical-{label}"),
        created_at_us=100,
        payload={"entity": label},
    )
    connection.execute(
        "INSERT INTO journal_entities VALUES (?,?,?,?,?,?,?)",
        tuple(getattr(record, field) for field in JournalEntity.__dataclass_fields__),
    )
    return record.entity_id


def _raw_event(
    connection: sqlite3.Connection,
    entity_id: str,
    sequence: int,
    *,
    label: str | None = None,
    supersedes_event_id: str | None = None,
) -> str:
    label = label or f"{entity_id}-{sequence}"
    record = make_journal_event(
        entity_id=entity_id,
        event_type="recorded",
        sequence=sequence,
        occurred_at_us=1_000 + sequence,
        payload={"event": label},
        supersedes_event_id=supersedes_event_id,
    )
    connection.execute(
        "INSERT INTO journal_events VALUES (?,?,?,?,?,?,?,?)",
        tuple(getattr(record, field) for field in JournalEvent.__dataclass_fields__),
    )
    return record.event_id


def _manual_entity_values(
    run_id: str, label: str, *, payload: bytes = b'{"value":1}'
) -> tuple[object, ...]:
    logical_identity = _hex(f"logical-{label}")
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    entity_id = canonical_hash(
        "journal-entity-v1",
        {
            "run_id": run_id,
            "entity_type": "order_intent",
            "logical_identity": logical_identity,
            "created_at_us": 100,
            "payload_sha256": payload_sha256,
        },
    )
    return (
        entity_id,
        run_id,
        "order_intent",
        logical_identity,
        100,
        payload_sha256,
        payload,
    )


def _manual_event_values(
    entity_id: str,
    sequence: int,
    *,
    payload: bytes,
    supersedes_event_id: str | None = None,
) -> tuple[object, ...]:
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    event_id = canonical_hash(
        "journal-event-v1",
        {
            "entity_id": entity_id,
            "event_type": "recorded",
            "sequence": sequence,
            "occurred_at_us": 1_000 + sequence,
            "payload_sha256": payload_sha256,
            "supersedes_event_id": supersedes_event_id,
        },
    )
    return (
        event_id,
        entity_id,
        "recorded",
        sequence,
        1_000 + sequence,
        payload_sha256,
        payload,
        supersedes_event_id,
    )


def test_secure_connection_denies_direct_journal_inserts_and_allows_reads(
    tmp_path: Path,
) -> None:
    connection = _secure_connection(tmp_path / "private" / "journal.sqlite3")
    try:
        run_id = _insert_run(connection)
        valid = _manual_entity_values(run_id, "valid")
        malformed = list(_manual_entity_values(run_id, "malformed"))
        malformed[-1] = b"not-json"
        for values in (valid, malformed):
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                connection.execute(
                    "INSERT INTO journal_entities VALUES (?,?,?,?,?,?,?)", values
                )
        entity = _entity(run_id)
        AppendOnlyJournal(connection).append_entity(entity)
        columns = tuple(JournalEntity.__dataclass_fields__)
        repeated_insert = (
            f"INSERT INTO journal_entities ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})"
        )
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            connection.execute(
                repeated_insert,
                tuple(getattr(entity, column) for column in columns),
            )
        for statement in (
            "UPDATE journal_entities SET entity_id = entity_id",
            "DELETE FROM journal_entities",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                connection.execute(statement)
        assert (
            connection.execute("SELECT count(*) FROM journal_entities").fetchone()[0]
            == 1
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "statement",
    (
        "DROP TRIGGER journal_events_immutable_delete",
        "ALTER TABLE journal_events RENAME TO replaced_events",
        "DROP TABLE journal_entities",
        "PRAGMA writable_schema = ON",
        "ATTACH DATABASE ':memory:' AS auxiliary",
    ),
)
def test_secure_connection_seals_authorizer_and_schema(
    tmp_path: Path, statement: str
) -> None:
    connection = _secure_connection(tmp_path / "private" / "journal.sqlite3")
    try:
        assert connection.authorizer_sealed
        for callback in (None, lambda *_args: sqlite3.SQLITE_OK):
            with pytest.raises(RuntimeError, match="authorizer.*sealed"):
                connection.set_authorizer(callback)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            connection.execute(statement)
    finally:
        connection.close()


def test_secure_journal_write_scope_restores_denial_after_error(tmp_path: Path) -> None:
    connection = _secure_connection(tmp_path / "private" / "journal.sqlite3")
    try:
        run_id = _insert_run(connection)
        journal = AppendOnlyJournal(connection)
        entity = _entity(run_id)
        assert journal.append_entity(entity) == entity.entity_id
        missing = _event(
            entity.entity_id,
            1,
            payload={"changed": True},
            supersedes_event_id=HEX_B,
        )
        with pytest.raises(JournalConflictError, match="predecessor"):
            journal.append_event(missing)
        direct = _manual_event_values(entity.entity_id, 0, payload=b'{"direct":true}')
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            connection.execute(
                "INSERT INTO journal_events VALUES (?,?,?,?,?,?,?,?)", direct
            )
        assert not connection.in_transaction
    finally:
        connection.close()


@pytest.mark.parametrize("adapted_type", (str, int, bytes, type(None)))
def test_global_adapters_cannot_run_during_privileged_journal_insert(
    tmp_path: Path, adapted_type: type[object]
) -> None:
    connection = _secure_connection(
        tmp_path / adapted_type.__name__ / "journal.sqlite3"
    )
    original_adapter = sqlite3.adapters.get((adapted_type, sqlite3.PrepareProtocol))
    try:
        run_id = _insert_run(connection)
        journal = AppendOnlyJournal(connection)
        legitimate_entity = _entity(run_id)
        if adapted_type is type(None):
            journal.append_entity(legitimate_entity)
            legitimate: JournalEntity | JournalEvent = _event(
                legitimate_entity.entity_id, 0
            )
        else:
            legitimate = legitimate_entity
        forged = _entity(
            run_id,
            logical_identity=_hex(f"forged-adapter-{adapted_type.__name__}"),
            payload={"forged": adapted_type.__name__},
        )
        forged_sql = journal_module._trusted_insert_sql(
            "journal_entities",
            tuple(
                getattr(forged, field) for field in JournalEntity.__dataclass_fields__
            ),
        )
        callback_ran = False

        def hostile_adapter(value: object) -> object:
            nonlocal callback_ran
            callback_ran = True
            connection.execute(forged_sql)
            return value

        sqlite3.register_adapter(adapted_type, hostile_adapter)
        if isinstance(legitimate, JournalEvent):
            assert journal.append_event(legitimate) == legitimate.event_id
        else:
            assert journal.append_entity(legitimate) == legitimate.entity_id
        assert not callback_ran
        assert (
            connection.execute(
                "SELECT count(*) FROM main.journal_entities WHERE entity_id = "
                f"CAST(X'{forged.entity_id.encode().hex()}' AS TEXT)"
            ).fetchone()[0]
            == 0
        )
    finally:
        key = (adapted_type, sqlite3.PrepareProtocol)
        if original_adapter is None:
            sqlite3.adapters.pop(key, None)
        else:
            sqlite3.adapters[key] = original_adapter
        connection.close()


@pytest.mark.parametrize(
    "value,expected_type,expected_value",
    (
        ("quote'\x00snowman ☃", "text", "quote'\x00snowman ☃"),
        (b"\x00'\xff", "blob", b"\x00'\xff"),
        (-(2**63) + 1, "integer", -(2**63) + 1),
        (None, "null", None),
    ),
)
def test_privileged_journal_literals_preserve_sqlite_boundaries(
    value: object, expected_type: str, expected_value: object
) -> None:
    statement = journal_module._trusted_insert_sql("journal_entities", (value,))
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE journal_entities(value)")
        connection.execute(statement)
        actual = connection.execute(
            "SELECT typeof(value), value FROM journal_entities"
        ).fetchone()
        assert actual == (expected_type, expected_value)
    finally:
        connection.close()


def test_trace_callback_cannot_forge_row_during_journal_begin(tmp_path: Path) -> None:
    connection = _secure_connection(tmp_path / "private" / "journal.sqlite3")
    try:
        run_id = _insert_run(connection)
        forged = _entity(run_id, logical_identity=HEX_B, payload={"forged": True})
        values = tuple(
            getattr(forged, field) for field in JournalEntity.__dataclass_fields__
        )
        registration_rejected = False

        def inject_on_begin(statement: str) -> None:
            if statement.startswith("BEGIN"):
                connection.execute(
                    "INSERT INTO main.journal_entities VALUES (?,?,?,?,?,?,?)", values
                )

        try:
            connection.set_trace_callback(inject_on_begin)
        except RuntimeError:
            registration_rejected = True

        AppendOnlyJournal(connection).append_entity(_entity(run_id))

        assert (
            connection.execute(
                "SELECT count(*) FROM main.journal_entities WHERE entity_id = ?",
                (forged.entity_id,),
            ).fetchone()[0]
            == 0
        )
        assert registration_rejected
    finally:
        connection.close()


@pytest.mark.parametrize("construct_before_assignment", (False, True))
@pytest.mark.parametrize(
    "operation",
    ("entity_idempotent", "entity_conflict", "event_idempotent", "event_conflict"),
)
def test_row_factory_cannot_reenter_journal_read_paths(
    tmp_path: Path, construct_before_assignment: bool, operation: str
) -> None:
    database = tmp_path / operation / "journal.sqlite3"
    setup = _secure_connection(database)
    run_id = _insert_run(setup)
    setup_journal = AppendOnlyJournal(setup)
    entity = _entity(run_id)
    setup_journal.append_entity(entity)
    existing_event = _event(entity.entity_id, 0)
    setup_journal.append_event(existing_event)
    setup.close()

    connection = _secure_connection(database)
    try:
        journal = AppendOnlyJournal(connection) if construct_before_assignment else None
        forged_entity = _entity(
            run_id,
            logical_identity=_hex(f"forged-{operation}-{construct_before_assignment}"),
            payload={"forged": operation},
        )
        forged_event = _event(
            entity.entity_id,
            99,
            payload={"forged": operation},
        )
        forged = forged_entity if operation.startswith("entity") else forged_event
        columns = tuple(type(forged).__dataclass_fields__)
        values = tuple(getattr(forged, column) for column in columns)
        table = (
            "journal_entities" if operation.startswith("entity") else "journal_events"
        )
        injected = False

        def inject(cursor: sqlite3.Cursor, row: tuple[object, ...]) -> sqlite3.Row:
            nonlocal injected
            if not injected:
                try:
                    connection.execute(
                        f"INSERT INTO main.{table} ({', '.join(columns)}) "
                        f"VALUES ({', '.join('?' for _ in columns)})",
                        values,
                    )
                except sqlite3.DatabaseError as error:
                    if "not authorized" not in str(error):
                        raise
                else:
                    injected = True
            return sqlite3.Row(cursor, row)

        assignment_rejected = False
        try:
            connection.row_factory = inject
        except AttributeError:
            assignment_rejected = True
        if journal is None:
            journal = AppendOnlyJournal(connection)

        if operation == "entity_idempotent":
            assert journal.append_entity(entity) == entity.entity_id
        elif operation == "entity_conflict":
            with pytest.raises(JournalConflictError, match="conflict"):
                journal.append_entity(_entity(run_id, payload={"different": True}))
        elif operation == "event_idempotent":
            assert journal.append_event(existing_event) == existing_event.event_id
        else:
            with pytest.raises(JournalConflictError, match="conflict"):
                journal.append_event(
                    _event(entity.entity_id, 0, payload={"different": True})
                )

        assert assignment_rejected
        assert connection.row_factory is sqlite3.Row
        assert (
            connection.execute(
                f"SELECT count(*) FROM main.{table} WHERE {columns[0]} = ?",
                (values[0],),
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


@pytest.mark.parametrize("construct_before_assignment", (False, True))
def test_text_factory_is_sealed_and_rows_remain_sqlite_rows(
    tmp_path: Path, construct_before_assignment: bool
) -> None:
    connection = _secure_connection(tmp_path / "private" / "journal.sqlite3")
    try:
        journal = AppendOnlyJournal(connection) if construct_before_assignment else None
        with pytest.raises(AttributeError, match="text_factory"):
            connection.text_factory = lambda value: value.decode("utf-8")
        if journal is None:
            AppendOnlyJournal(connection)
        assert connection.text_factory is str
        assert connection.row_factory is sqlite3.Row
        assert isinstance(connection.execute("SELECT 'value'").fetchone(), sqlite3.Row)
    finally:
        connection.close()


def test_public_like_override_cannot_hide_temp_objects(tmp_path: Path) -> None:
    connection = _secure_connection(tmp_path / "private" / "journal.sqlite3")
    try:
        registration_rejected = False
        try:
            connection.create_function("like", 2, lambda _pattern, _value: 1)
        except RuntimeError:
            registration_rejected = True

        sqlite3.Connection.set_authorizer(connection, None)
        connection.execute("CREATE TEMP TABLE hidden_by_like(value INTEGER)")
        sqlite3.Connection.set_authorizer(connection, _runtime_authorizer)

        with pytest.raises(DatabaseConfigurationError, match="TEMP schema"):
            AppendOnlyJournal(connection)
        assert registration_rejected
    finally:
        connection.close()


def test_base_like_override_cannot_hide_temp_objects(tmp_path: Path) -> None:
    connection = _secure_connection(tmp_path / "private" / "journal.sqlite3")
    try:
        sqlite3.Connection.create_function(
            connection, "like", 2, lambda _pattern, _value: 1
        )
        sqlite3.Connection.set_authorizer(connection, None)
        connection.execute("CREATE TEMP TABLE hidden_by_base_like(value INTEGER)")
        sqlite3.Connection.set_authorizer(connection, _runtime_authorizer)

        with pytest.raises(DatabaseConfigurationError, match="TEMP schema"):
            AppendOnlyJournal(connection)
    finally:
        connection.close()


class _Aggregate:
    def step(self) -> None:
        pass

    def finalize(self) -> int:
        return 0


class _WindowAggregate(_Aggregate):
    def value(self) -> int:
        return 0

    def inverse(self) -> None:
        pass


@pytest.mark.parametrize(
    "method, arguments, keyword_arguments",
    (
        ("set_trace_callback", (lambda _statement: None,), {}),
        ("create_function", ("blocked_function", 0, lambda: 1), {}),
        ("create_aggregate", ("blocked_aggregate", 0, _Aggregate), {}),
        ("create_window_function", ("blocked_window", 0, _WindowAggregate), {}),
        ("create_collation", ("blocked_collation", lambda _left, _right: 0), {}),
        ("set_progress_handler", (lambda: 0, 1), {}),
        ("set_authorizer", (None,), {}),
        ("enable_load_extension", (True,), {}),
        ("load_extension", ("missing_extension",), {}),
        ("deserialize", (b"not a database",), {}),
        ("blobopen", ("journal_entities", "payload", 1), {"readonly": False}),
    ),
)
def test_sealed_connection_rejects_mutable_callback_and_database_surfaces(
    tmp_path: Path,
    method: str,
    arguments: tuple[object, ...],
    keyword_arguments: dict[str, object],
) -> None:
    if not hasattr(sqlite3.Connection, method):
        pytest.skip(f"sqlite3.Connection.{method} is unavailable")
    connection = _secure_connection(tmp_path / method / "private" / "journal.sqlite3")
    try:
        before = connection.total_changes
        with pytest.raises(RuntimeError, match="sealed"):
            getattr(connection, method)(*arguments, **keyword_arguments)
        assert connection.total_changes == before
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    finally:
        connection.close()


def test_append_ignores_matching_temp_journal_shadows(tmp_path: Path) -> None:
    connection = _secure_connection(tmp_path / "private" / "journal.sqlite3")
    try:
        run_id = _insert_run(connection)
        journal = AppendOnlyJournal(connection)
        sqlite3.Connection.set_authorizer(connection, None)
        connection.execute(
            "CREATE TEMP TABLE journal_entities AS "
            "SELECT * FROM main.journal_entities WHERE 0"
        )
        connection.execute(
            "CREATE TEMP TABLE journal_events AS "
            "SELECT * FROM main.journal_events WHERE 0"
        )
        sqlite3.Connection.set_authorizer(connection, _runtime_authorizer)
        entity = _entity(run_id)
        event = _event(entity.entity_id, 0)
        assert journal.append_entity(entity) == entity.entity_id
        assert journal.append_event(event) == event.event_id
        assert (
            connection.execute("SELECT count(*) FROM main.journal_entities").fetchone()[
                0
            ]
            == 1
        )
        assert (
            connection.execute("SELECT count(*) FROM main.journal_events").fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT count(*) FROM temp.journal_entities").fetchone()[
                0
            ]
            == 0
        )
        assert (
            connection.execute("SELECT count(*) FROM temp.journal_events").fetchone()[0]
            == 0
        )
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            connection.execute(
                "INSERT INTO temp.journal_entities SELECT * FROM main.journal_entities"
            )
    finally:
        connection.close()


def test_runtime_authorizer_denies_journal_dml_in_every_database() -> None:
    for database in (None, "main", "temp", "auxiliary"):
        for action in (
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
        ):
            for table in TABLES:
                assert (
                    _runtime_authorizer(action, table, None, database, None)
                    == sqlite3.SQLITE_DENY
                )


def test_journal_authorizer_only_allows_direct_selected_main_insert() -> None:
    authorize = _journal_authorizer("journal_entities")
    assert (
        authorize(sqlite3.SQLITE_INSERT, "journal_entities", None, "main", None)
        == sqlite3.SQLITE_OK
    )
    for table, database, source in (
        ("journal_events", "main", None),
        ("journal_entities", "temp", None),
        ("journal_entities", "main", "forged_temp_trigger"),
    ):
        assert (
            authorize(sqlite3.SQLITE_INSERT, table, None, database, source)
            == sqlite3.SQLITE_DENY
        )


def test_runtime_authorizer_denies_all_available_temp_schema_actions() -> None:
    names = (
        "SQLITE_CREATE_TEMP_TABLE",
        "SQLITE_CREATE_TEMP_INDEX",
        "SQLITE_CREATE_TEMP_TRIGGER",
        "SQLITE_CREATE_TEMP_VIEW",
        "SQLITE_DROP_TEMP_TABLE",
        "SQLITE_DROP_TEMP_INDEX",
        "SQLITE_DROP_TEMP_TRIGGER",
        "SQLITE_DROP_TEMP_VIEW",
    )
    supported = [getattr(sqlite3, name) for name in names if hasattr(sqlite3, name)]
    assert supported
    for action in supported:
        assert (
            _runtime_authorizer(action, "unrelated", None, "temp", None)
            == sqlite3.SQLITE_DENY
        )


@pytest.mark.parametrize(
    "statement",
    (
        "CREATE TEMP TABLE unrelated(value INTEGER)",
        "CREATE TEMP VIEW unrelated AS SELECT 1 AS value",
        "CREATE TEMP TRIGGER inject_forged_entity "
        "AFTER INSERT ON main.journal_entities BEGIN "
        "INSERT INTO journal_entities VALUES "
        f"('{HEX_A}', '{HEX_B}', 'entity', '{HEX_C}', 0, '{HEX_A}', X'7b7d'); END",
    ),
)
def test_sealed_connection_denies_temp_schema_creation(
    tmp_path: Path, statement: str
) -> None:
    connection = _secure_connection(tmp_path / "private" / "journal.sqlite3")
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            connection.execute(statement)
    finally:
        connection.close()


def test_fresh_connection_denies_temp_schema_creation(tmp_path: Path) -> None:
    connection = connect_database(
        (tmp_path / "private" / "journal.sqlite3").absolute(),
        busy_timeout_ms=5000,
        mount_resolver=lambda _path: "ext4",
    )
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            connection.execute(
                "CREATE TEMP TABLE blocked_before_migration(value INTEGER)"
            )
    finally:
        connection.close()


def test_sealed_connection_denies_temp_index_creation(tmp_path: Path) -> None:
    connection = _secure_connection(tmp_path / "private" / "journal.sqlite3")
    try:
        sqlite3.Connection.set_authorizer(connection, None)
        connection.execute("CREATE TEMP TABLE index_target(value INTEGER)")
        sqlite3.Connection.set_authorizer(connection, _runtime_authorizer)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            connection.execute("CREATE INDEX temp.blocked_index ON index_target(value)")
    finally:
        connection.close()


@pytest.mark.parametrize(
    "setup_sql",
    (
        "CREATE TEMP TABLE unrelated(value INTEGER)",
        "CREATE TEMP TABLE base(value INTEGER); "
        "CREATE INDEX temp.unrelated_index ON base(value)",
        "CREATE TABLE main.trigger_target(value INTEGER); "
        "CREATE TEMP TRIGGER unrelated_trigger AFTER INSERT ON main.trigger_target "
        "BEGIN SELECT 1; END",
        "CREATE TEMP VIEW unrelated_view AS SELECT 1 AS value",
        "CREATE TEMP TABLE journal_entities AS SELECT 1 AS marker; "
        "CREATE TEMP TABLE journal_events AS SELECT 1 AS marker",
    ),
)
def test_sealing_rejects_preexisting_user_temp_objects(
    tmp_path: Path, setup_sql: str
) -> None:
    connection = connect_database(
        (tmp_path / _hex(setup_sql) / "journal.sqlite3").absolute(),
        busy_timeout_ms=5000,
        mount_resolver=lambda _path: "ext4",
    )
    try:
        sqlite3.Connection.set_authorizer(connection, None)
        connection.executescript(setup_sql)
        sqlite3.Connection.set_authorizer(connection, _runtime_authorizer)
        with pytest.raises(DatabaseConfigurationError, match="TEMP schema"):
            apply_migrations(connection)
    finally:
        connection.close()


def test_journal_boundary_rejects_preexisting_forging_temp_trigger(
    tmp_path: Path,
) -> None:
    connection = _secure_connection(tmp_path / "private" / "journal.sqlite3")
    try:
        run_id = _insert_run(connection)
        forged = _entity(run_id, logical_identity=HEX_B, payload={"forged": True})
        values = tuple(
            getattr(forged, field) for field in JournalEntity.__dataclass_fields__
        )
        quoted = ", ".join(
            "X'" + value.hex() + "'"
            if isinstance(value, bytes)
            else str(value)
            if isinstance(value, int)
            else "'" + value.replace("'", "''") + "'"
            for value in values
        )
        sqlite3.Connection.set_authorizer(connection, None)
        connection.execute(
            "CREATE TEMP TRIGGER inject_forged_entity "
            "AFTER INSERT ON main.journal_entities BEGIN "
            f"INSERT INTO journal_entities VALUES ({quoted}); END"
        )
        sqlite3.Connection.set_authorizer(connection, _runtime_authorizer)
        with pytest.raises(DatabaseConfigurationError, match="TEMP schema"):
            AppendOnlyJournal(connection)
        with pytest.raises(DatabaseConfigurationError, match="TEMP schema"):
            _seal_runtime_authorizer(connection)
        assert (
            connection.execute("SELECT count(*) FROM main.journal_entities").fetchone()[
                0
            ]
            == 0
        )
    finally:
        connection.close()


def test_append_only_journal_documents_supported_python_boundary() -> None:
    boundary = AppendOnlyJournal.__doc__ or ""
    assert "public instance methods" in boundary
    assert "base-class descriptors" in boundary
    assert "object internals" in boundary
    assert "process memory" in boundary
    assert "out of scope" in boundary


def test_append_only_journal_rejects_plain_sqlite_connection() -> None:
    connection = _connection()
    try:
        with pytest.raises(TypeError, match="SecureConnection"):
            AppendOnlyJournal(connection, expected_busy_timeout_ms=5000)
    finally:
        connection.close()


def test_migration_003_has_no_predicate_collation_or_json_dependency() -> None:
    sql = load_migrations()[2].sql.casefold()
    assert "collate jev_" not in sql
    assert "json_array" not in sql
    assert "authenticate" not in sql


def test_update_delete_abort() -> None:
    connection = _connection()
    try:
        run_id = _insert_run(connection)
        entity_id = _raw_entity(connection, run_id, "immutable")
        _raw_event(connection, entity_id, 0)
        for table in TABLES:
            key = connection.execute(f"PRAGMA table_info({table})").fetchone()[1]
            with pytest.raises(sqlite3.IntegrityError, match=f"immutable:{table}"):
                connection.execute(f"UPDATE {table} SET {key} = {key}")
            with pytest.raises(sqlite3.IntegrityError, match=f"immutable:{table}"):
                connection.execute(f"DELETE FROM {table}")
    finally:
        connection.close()


def test_two_node_back_edge_rejected_by_immutable_conflict() -> None:
    connection = _connection()
    try:
        run_id = _insert_run(connection)
        entity_id = _raw_entity(connection, run_id, "cycle")
        first = _raw_event(connection, entity_id, 0, label="first")
        second = _raw_event(
            connection, entity_id, 1, label="second", supersedes_event_id=first
        )
        first_row = list(
            connection.execute(
                "SELECT * FROM journal_events WHERE event_id = ?", (first,)
            ).fetchone()
        )
        first_row[-1] = second
        connection.execute("PRAGMA recursive_triggers = OFF")
        with pytest.raises(
            sqlite3.IntegrityError, match="immutable-conflict:journal_events"
        ):
            connection.execute(
                "INSERT OR REPLACE INTO journal_events VALUES (?,?,?,?,?,?,?,?)",
                first_row,
            )
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' "
            "AND name='journal_events_validate_supersession'"
        ).fetchone()[0]
        assert "WITH RECURSIVE" not in trigger_sql

    finally:
        connection.close()


def test_thousand_link_back_edge_rejected_by_immutable_conflict() -> None:
    connection = _connection()
    try:
        run_id = _insert_run(connection)
        entity_id = _raw_entity(connection, run_id, "long-cycle")
        first = _raw_event(connection, entity_id, 0, label="chain-0")
        predecessor = first
        for sequence in range(1, 1_001):
            predecessor = _raw_event(
                connection,
                entity_id,
                sequence,
                label=f"chain-{sequence}",
                supersedes_event_id=predecessor,
            )
        root = list(
            connection.execute(
                "SELECT * FROM journal_events WHERE event_id = ?", (first,)
            ).fetchone()
        )
        root[-1] = predecessor
        connection.execute("PRAGMA recursive_triggers = OFF")
        with pytest.raises(
            sqlite3.IntegrityError, match="immutable-conflict:journal_events"
        ):
            connection.execute(
                "INSERT OR REPLACE INTO journal_events VALUES (?,?,?,?,?,?,?,?)", root
            )
        assert (
            connection.execute("SELECT count(*) FROM journal_events").fetchone()[0]
            == 1001
        )
    finally:
        connection.close()


def test_one_successor_same_identity() -> None:
    connection = _connection()
    try:
        run_one = _insert_run(connection, "one")
        run_two = _insert_run(connection, "two")
        original_entity = _raw_entity(connection, run_one, "original")
        original = _raw_event(connection, original_entity, 0, label="original")
        _raw_event(
            connection,
            original_entity,
            1,
            label="successor",
            supersedes_event_id=original,
        )
        with pytest.raises(sqlite3.IntegrityError):
            _raw_event(
                connection,
                original_entity,
                2,
                label="branch",
                supersedes_event_id=original,
            )

        variants = (
            (run_one, "other_entity", _hex("logical-original"), "entity"),
            (run_two, "order_intent", _hex("logical-original"), "run"),
            (run_one, "other_type", _hex("logical-original"), "type"),
            (run_one, "order_intent", _hex("other-logical"), "logical"),
        )
        for index, (run_id, entity_type, logical, label) in enumerate(variants):
            source = _raw_entity(connection, run_one, f"source-{label}")
            predecessor = _raw_event(
                connection, source, 0, label=f"predecessor-{index}"
            )
            different = _raw_entity(
                connection,
                run_id,
                label,
                entity_type=entity_type,
                logical_identity=logical,
            )
            with pytest.raises(sqlite3.IntegrityError, match="supersession:entity"):
                _raw_event(
                    connection,
                    different,
                    1,
                    label=f"cross-{label}",
                    supersedes_event_id=predecessor,
                )
    finally:
        connection.close()


def test_replace_primary_and_secondary_unique_rejected_with_recursion_off() -> None:
    connection = _connection()
    try:
        run_id = _insert_run(connection)
        entity_id = _raw_entity(connection, run_id, "replace")
        first = _raw_event(connection, entity_id, 0, label="replace-0")
        second = _raw_event(
            connection,
            entity_id,
            1,
            label="replace-1",
            supersedes_event_id=first,
        )
        connection.execute("PRAGMA recursive_triggers = OFF")

        entity_row = list(
            connection.execute("SELECT * FROM journal_entities").fetchone()
        )
        entity_row[-1] = b'{"changed":true}'
        with pytest.raises(
            sqlite3.IntegrityError, match="immutable-conflict:journal_entities"
        ):
            connection.execute(
                "INSERT OR REPLACE INTO journal_entities VALUES (?,?,?,?,?,?,?)",
                entity_row,
            )
        entity_secondary = entity_row.copy()
        entity_secondary[0] = _hex("replacement-entity")
        entity_secondary[-2] = hashlib.sha256(entity_secondary[-1]).hexdigest()
        with pytest.raises(
            sqlite3.IntegrityError, match="immutable-conflict:journal_entities"
        ):
            connection.execute(
                "INSERT OR REPLACE INTO journal_entities VALUES (?,?,?,?,?,?,?)",
                entity_secondary,
            )

        event_row = list(
            connection.execute(
                "SELECT * FROM journal_events WHERE event_id = ?", (second,)
            ).fetchone()
        )
        event_row[-2] = b'{"changed":true}'
        with pytest.raises(
            sqlite3.IntegrityError, match="immutable-conflict:journal_events"
        ):
            connection.execute(
                "INSERT OR REPLACE INTO journal_events VALUES (?,?,?,?,?,?,?,?)",
                event_row,
            )
        for label, change in (
            ("sequence-unique", {}),
            ("successor-unique", {3: 2}),
        ):
            alternate = event_row.copy()
            alternate[0] = _hex(label)
            for index, value in change.items():
                alternate[index] = value
            alternate[-2] = json.dumps({"label": label}).encode()
            alternate[-3] = hashlib.sha256(alternate[-2]).hexdigest()
            with pytest.raises(
                sqlite3.IntegrityError, match="immutable-conflict:journal_events"
            ):
                connection.execute(
                    "INSERT OR REPLACE INTO journal_events VALUES (?,?,?,?,?,?,?,?)",
                    alternate,
                )
        assert (
            connection.execute("SELECT count(*) FROM journal_entities").fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT count(*) FROM journal_events").fetchone()[0] == 2
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("table", "row"),
    (
        (
            "journal_entities",
            (HEX_A + "\x00", HEX_B, "entity", HEX_C, 0, HEX_A, b"{}"),
        ),
        (
            "journal_entities",
            (HEX_A, HEX_B, "Bad-Slug", HEX_C, 0, HEX_A, b"{}"),
        ),
        (
            "journal_entities",
            (HEX_A, HEX_B, "entity", HEX_C, -1, HEX_A, b"{}"),
        ),
        (
            "journal_entities",
            (HEX_A, HEX_B, "entity", HEX_C, 0, HEX_A, b"x" * 262_145),
        ),
        (
            "journal_events",
            (HEX_A, HEX_B, "bad status", 0, 0, HEX_C, b"{}", None),
        ),
        (
            "journal_events",
            (HEX_A, HEX_B, "event", b"0", 0, HEX_C, b"{}", None),
        ),
        (
            "journal_events",
            (HEX_A, HEX_B, "event", 0, -62_135_596_800_000_001, HEX_C, b"{}", None),
        ),
    ),
)
def test_direct_sql_rejects_invalid_ids_hash_payload_type_slug_and_timestamp(
    table: str, row: tuple[object, ...]
) -> None:
    connection = _connection()
    try:
        run_id = _insert_run(connection)
        entity_id = _raw_entity(connection, run_id, "valid")
        values = list(row)
        if table == "journal_entities":
            values[1] = run_id
        else:
            values[1] = entity_id
        placeholders = ",".join("?" for _ in values)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f"INSERT INTO {table} VALUES ({placeholders})", values)
    finally:
        connection.close()


def test_factories_require_exact_builtin_types_and_canonical_payload_limit() -> None:
    with pytest.raises(TypeError):
        make_journal_entity(
            run_id=HEX_A,
            entity_type="entity",
            logical_identity=HEX_B,
            created_at_us=True,  # type: ignore[arg-type]
            payload={},
        )
    with pytest.raises(ValueError, match="payload"):
        make_journal_entity(
            run_id=HEX_A,
            entity_type="entity",
            logical_identity=HEX_B,
            created_at_us=0,
            payload={"large": "x" * 262_145},
        )
    with pytest.raises((TypeError, ValueError)):
        make_journal_event(
            entity_id=HEX_A,
            event_type="Bad-Type",
            sequence=0,
            occurred_at_us=0,
            payload={},
        )


def test_tampered_and_forged_records_are_semantically_reconstructed() -> None:
    connection = _secure_connection()
    try:
        run_id = _insert_run(connection)
        journal = AppendOnlyJournal(connection, expected_busy_timeout_ms=5000)
        valid = _entity(run_id)
        forged = object.__new__(JournalEntity)
        for field in JournalEntity.__dataclass_fields__:
            object.__setattr__(forged, field, getattr(valid, field))
        assert journal.append_entity(forged) == valid.entity_id

        tampered = object.__new__(JournalEntity)
        for field in JournalEntity.__dataclass_fields__:
            object.__setattr__(tampered, field, getattr(valid, field))
        object.__setattr__(tampered, "payload_sha256", HEX_C)
        with pytest.raises(ValueError, match="record"):
            journal.append_entity(tampered)
        with pytest.raises(AttributeError):
            object.__setattr__(valid, "_proof", HEX_A)
    finally:
        connection.close()


def test_append_idempotency_and_conflicts() -> None:
    connection = _secure_connection()
    try:
        run_id = _insert_run(connection)
        journal = AppendOnlyJournal(connection, expected_busy_timeout_ms=5000)
        entity = _entity(run_id)
        assert journal.append_entity(entity) == entity.entity_id
        assert journal.append_entity(entity) == entity.entity_id
        first = _event(entity.entity_id, 0)
        assert journal.append_event(first) == first.event_id
        assert journal.append_event(first) == first.event_id

        conflicting_entity = _entity(run_id, payload={"value": 2})
        with pytest.raises(JournalConflictError, match="conflict"):
            journal.append_entity(conflicting_entity)
        conflicting_event = _event(entity.entity_id, 0, payload={"other": True})
        with pytest.raises(JournalConflictError, match="conflict"):
            journal.append_event(conflicting_event)
    finally:
        connection.close()


def test_repository_uses_authoritative_database_predecessor() -> None:
    connection = _secure_connection()
    try:
        run_id = _insert_run(connection)
        journal = AppendOnlyJournal(connection, expected_busy_timeout_ms=5000)
        entity = _entity(run_id)
        journal.append_entity(entity)
        first = _event(entity.entity_id, 0)
        journal.append_event(first)
        second = _event(
            entity.entity_id,
            1,
            supersedes_event_id=first.event_id,
            payload={"changed": True},
        )
        object.__setattr__(first, "payload_sha256", HEX_C)
        assert journal.append_event(second) == second.event_id
    finally:
        connection.close()


def test_repository_transaction_ownership_and_rollback() -> None:
    connection = _secure_connection()
    try:
        run_id = _insert_run(connection)
        journal = AppendOnlyJournal(connection, expected_busy_timeout_ms=5000)
        entity = _entity(run_id)
        connection.execute("BEGIN")
        with pytest.raises(JournalRepositoryError, match="active caller transaction"):
            journal.append_entity(entity)
        assert connection.in_transaction
        connection.execute("ROLLBACK")

        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            journal.append_entity(_entity(HEX_B))
        assert not connection.in_transaction
        assert (
            connection.execute("SELECT count(*) FROM journal_entities").fetchone()[0]
            == 0
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("pragma", "drift"),
    (
        ("foreign_keys", "OFF"),
        ("recursive_triggers", "OFF"),
        ("trusted_schema", "ON"),
        ("journal_mode", "DELETE"),
        ("synchronous", "NORMAL"),
        ("busy_timeout", "1234"),
    ),
)
def test_repository_rejects_pragma_drift(pragma: str, drift: str) -> None:
    connection = _secure_connection()
    try:
        run_id = _insert_run(connection)
        journal = AppendOnlyJournal(connection, expected_busy_timeout_ms=5000)
        connection.execute(f"PRAGMA {pragma} = {drift}")
        with pytest.raises(JournalRepositoryError, match=pragma):
            journal.append_entity(_entity(run_id))
        assert (
            connection.execute("SELECT count(*) FROM journal_entities").fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_secure_connection_timeout_is_authoritative(tmp_path: Path) -> None:
    connection = connect_database(
        (tmp_path / "private" / "journal.sqlite3").absolute(),
        busy_timeout_ms=5000,
        mount_resolver=lambda _path: "ext4",
    )
    try:
        apply_migrations(connection)
        run_id = _insert_run(connection)
        with pytest.raises(ValueError, match="expected_busy_timeout_ms"):
            AppendOnlyJournal(connection, expected_busy_timeout_ms=1234)
        journal = AppendOnlyJournal(connection)
        connection.execute("PRAGMA busy_timeout = 1234")
        with pytest.raises(JournalRepositoryError, match="busy_timeout"):
            journal.append_entity(_entity(run_id))
    finally:
        connection.close()


def test_concurrent_identical_appends_converge(tmp_path: Path) -> None:
    database = tmp_path / "identical.sqlite3"
    setup = _secure_connection(database)
    run_id = _insert_run(setup)
    entity = _entity(run_id)
    setup.close()
    ready = threading.Semaphore(0)
    release = threading.Event()

    def append() -> str:
        connection = _secure_connection(database)
        try:
            ready.release()
            assert release.wait(timeout=30)
            return AppendOnlyJournal(
                connection, expected_busy_timeout_ms=5000
            ).append_entity(entity)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(append), pool.submit(append))
        assert ready.acquire(timeout=30)
        assert ready.acquire(timeout=30)
        release.set()
        results = [future.result(timeout=30) for future in futures]
    assert results == [entity.entity_id, entity.entity_id]
    check = sqlite3.connect(database)
    try:
        assert check.execute("SELECT count(*) FROM journal_entities").fetchone()[0] == 1
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()


def test_concurrent_conflicting_sequence_has_one_winner(tmp_path: Path) -> None:
    database = tmp_path / "conflict.sqlite3"
    setup = _secure_connection(database)
    run_id = _insert_run(setup)
    journal = AppendOnlyJournal(setup, expected_busy_timeout_ms=5000)
    entity = _entity(run_id)
    journal.append_entity(entity)
    setup.close()
    events = (
        _event(entity.entity_id, 0, payload={"candidate": "a"}),
        _event(entity.entity_id, 0, payload={"candidate": "b"}),
    )
    ready = threading.Semaphore(0)
    release = threading.Event()

    def append(event: JournalEvent) -> tuple[str, str]:
        connection = _secure_connection(database)
        try:
            ready.release()
            assert release.wait(timeout=30)
            try:
                result = AppendOnlyJournal(
                    connection, expected_busy_timeout_ms=5000
                ).append_event(event)
            except JournalConflictError as error:
                return "conflict", str(error)
            return "ok", result
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(append, events[0]),
            pool.submit(append, events[1]),
        )
        assert ready.acquire(timeout=30)
        assert ready.acquire(timeout=30)
        release.set()
        outcomes = [future.result(timeout=30) for future in futures]
    assert sorted(item[0] for item in outcomes) == ["conflict", "ok"]


def test_migration_003_chain_guards_and_002_unchanged() -> None:
    migrations = load_migrations()
    assert [(item.version, item.name) for item in migrations] == [
        (1, "001_migration_metadata.sql"),
        (2, "002_core_entities.sql"),
        (3, "003_append_only_guards.sql"),
    ]
    migration_003 = migrations[2].sql
    assert "CREATE TRIGGER main.manifests" not in migration_003
    assert "CREATE TRIGGER main.observations" not in migration_003
    connection = _connection()
    try:
        trigger_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger'"
            )
        }
        assert {
            f"{table}_immutable_{operation}"
            for table in TABLES
            for operation in ("update", "delete")
        } <= trigger_names
        run_id = _insert_run(connection)
        with pytest.raises(sqlite3.IntegrityError, match="immutable:runs"):
            connection.execute(
                "UPDATE runs SET run_id=run_id WHERE run_id=?", (run_id,)
            )
    finally:
        connection.close()

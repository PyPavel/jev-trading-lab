from __future__ import annotations

import hashlib
import os
import stat
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.polymarket.operations import (
    BlobRefModel,
    OperationConflictError,
    OperationCorruptError,
    OperationEvent,
    OperationKind,
    OperationSecurityError,
    append_operation,
    load_operation_cut,
    operation_event_id,
)

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
SHA = "a" * 64


@pytest.fixture(autouse=True)
def _local_test_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    from jev_trading_lab.polymarket import operations

    defaults = dict(operations.BlobStore.__init__.__kwdefaults__ or {})
    defaults["filesystem_resolver"] = lambda _path: "ext4"
    monkeypatch.setattr(operations.BlobStore.__init__, "__kwdefaults__", defaults)


STAGES = (
    "startup",
    "recovery",
    "gamma_poll",
    "eligibility",
    "precheck_book",
    "evidence_freeze",
    "openai",
    "jev",
    "decision_barrier",
    "execution_book",
    "paper_execution",
    "terminal_commit",
    "completed_slot",
    "metrics_render",
    "replay",
)


def event(**changes: Any) -> OperationEvent:
    kind = OperationKind(changes.get("kind", OperationKind.STARTUP))
    defaults: dict[str, Any] = {
        "schema_version": 1,
        "occurred_at": NOW,
        "kind": kind,
        "run_id": SHA
        if kind
        in {
            OperationKind.RUN_STARTED,
            OperationKind.RUN_TERMINAL,
            OperationKind.STAGE_TERMINAL,
            OperationKind.PROCESS_SAMPLE,
        }
        else None,
        "semantic_id": SHA if kind is OperationKind.DEDUPLICATION else None,
        "stage": {
            OperationKind.STARTUP: "startup",
            OperationKind.STAGE_TERMINAL: "gamma_poll",
            OperationKind.RECOVERY: "recovery",
            OperationKind.DEDUPLICATION: "completed_slot",
            OperationKind.STORAGE_FAILURE: "terminal_commit",
            OperationKind.METRICS_GENERATION: "metrics_render",
        }.get(kind),
        "provider": "gamma" if kind is OperationKind.STAGE_TERMINAL else None,
        "status": {
            OperationKind.STARTUP: "clean",
            OperationKind.RUN_STARTED: "started",
            OperationKind.RUN_TERMINAL: "success",
            OperationKind.STAGE_TERMINAL: "success",
            OperationKind.RECOVERY: "success",
            OperationKind.DEDUPLICATION: "deduplicated",
            OperationKind.STORAGE_FAILURE: "failure",
            OperationKind.PROCESS_SAMPLE: "success",
            OperationKind.METRICS_GENERATION: "success",
        }[kind],
        "reason": {
            OperationKind.RECOVERY: "partial_tail",
            OperationKind.STORAGE_FAILURE: "fsync",
        }.get(kind),
        "elapsed_nanoseconds": 10
        if kind in {OperationKind.RUN_TERMINAL, OperationKind.STAGE_TERMINAL}
        else None,
        "exact_values": {"discarded_bytes": 1}
        if kind is OperationKind.RECOVERY
        else {},
        "evidence": None,
    }
    defaults.update(changes)
    defaults["kind"] = kind
    if isinstance(defaults["occurred_at"], str):
        defaults["occurred_at"] = datetime.fromisoformat(
            defaults["occurred_at"].replace("Z", "+00:00")
        )
    supplied_id = defaults.pop("event_id", None)
    defaults["event_id"] = supplied_id or operation_event_id(defaults)
    return OperationEvent.model_validate(defaults)


def blob_ref(payload: bytes) -> BlobRefModel:
    digest = hashlib.sha256(payload).hexdigest()
    return BlobRefModel(
        schema_version=1,
        sha256=digest,
        byte_size=len(payload),
        storage_relpath=f"operation-blobs/{digest[:2]}/{digest[2:4]}/{digest}",
    )


def append_raw_lines(root: Path, *values: OperationEvent, tail: bytes = b"") -> Path:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    journal = root / "operations.jsonl"
    journal.write_bytes(b"".join(journal_line(value) for value in values) + tail)
    journal.chmod(0o600)
    return journal


def persist_raw_evidence(root: Path, payload: bytes) -> BlobRefModel:
    reference = blob_ref(payload)
    path = root / reference.storage_relpath
    path.parent.mkdir(mode=0o700, parents=True)
    root.chmod(0o700)
    (root / "operation-blobs").chmod(0o700)
    path.parent.parent.chmod(0o700)
    path.parent.chmod(0o700)
    path.write_bytes(payload)
    path.chmod(0o600)
    return reference


def evidence_payload(kind: str = "process_sample") -> bytes:
    return canonical_json_bytes(
        {
            "schema_version": 1,
            "kind": kind,
            "status": "unavailable",
            "detail": "probe_unavailable",
        }
    )


def journal_line(value: OperationEvent) -> bytes:
    return canonical_json_bytes(value.model_dump(mode="json")) + b"\n"


def assert_sanitized_domain_error(error: Exception, marker: str) -> None:
    rendered = "".join(traceback.format_exception(error))
    assert marker not in str(error)
    assert marker not in repr(error)
    assert error.__cause__ is None
    assert error.__suppress_context__
    assert marker not in rendered


def test_operation_kind_is_closed_to_all_nine_approved_values() -> None:
    assert tuple(kind.value for kind in OperationKind) == (
        "startup",
        "run_started",
        "run_terminal",
        "stage_terminal",
        "recovery",
        "deduplication",
        "storage_failure",
        "process_sample",
        "metrics_generation",
    )


@pytest.mark.parametrize(
    "changes",
    (
        {"kind": "startup", "stage": "startup", "status": "clean"},
        {"kind": "run_started", "run_id": SHA, "status": "started"},
        {
            "kind": "run_terminal",
            "run_id": SHA,
            "status": "success",
            "elapsed_nanoseconds": 10,
        },
        {
            "kind": "stage_terminal",
            "run_id": SHA,
            "stage": "gamma_poll",
            "provider": "gamma",
            "status": "success",
            "elapsed_nanoseconds": 10,
        },
        {
            "kind": "recovery",
            "stage": "recovery",
            "status": "success",
            "reason": "partial_tail",
            "exact_values": {"discarded_bytes": 3},
        },
        {
            "kind": "deduplication",
            "semantic_id": SHA,
            "stage": "completed_slot",
            "status": "deduplicated",
        },
        {
            "kind": "storage_failure",
            "stage": "terminal_commit",
            "status": "failure",
            "reason": "fsync",
        },
        {
            "kind": "process_sample",
            "run_id": SHA,
            "status": "success",
            "exact_values": {},
        },
        {
            "kind": "metrics_generation",
            "stage": "metrics_render",
            "status": "skipped",
        },
    ),
)
def test_all_kind_contracts_round_trip(tmp_path: Path, changes: dict[str, Any]) -> None:
    value = event(**changes)
    root = tmp_path / "audit"
    prerequisites: tuple[OperationEvent, ...] = ()
    if value.kind in {
        OperationKind.RUN_TERMINAL,
        OperationKind.STAGE_TERMINAL,
        OperationKind.PROCESS_SAMPLE,
    }:
        prerequisites = (
            event(
                kind="run_started",
                run_id=value.run_id,
                status="started",
                occurred_at="2026-10-12T11:59:59Z",
            ),
        )
        append_operation(root, prerequisites[0])

    append_operation(root, value)
    cut = load_operation_cut(root)

    assert cut.events == (*prerequisites, value)
    assert cut.journal_bytes == sum(len(journal_line(item)) for item in cut.events)
    assert cut.valid_blobs == 0
    assert cut.discarded_tail_bytes == 0
    journal = tmp_path / "audit/operations.jsonl"
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert journal.read_bytes() == b"".join(journal_line(item) for item in cut.events)


def test_evidence_is_content_addressed_and_verified(tmp_path: Path) -> None:
    payload = evidence_payload()
    value = event(
        kind="process_sample",
        run_id=SHA,
        status="failure",
        reason="process",
        evidence=blob_ref(payload).model_dump(),
    )

    root = tmp_path / "audit"
    append_operation(root, event(kind="run_started", run_id=SHA, status="started"))
    append_operation(root, value, payload)
    cut = load_operation_cut(root)

    assert cut.events[-1] == value
    assert cut.valid_blobs == 1
    assert (tmp_path / "audit" / value.evidence.storage_relpath).read_bytes() == payload  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"schema_version": 2}, "schema_version"),
        ({"occurred_at": NOW.replace(tzinfo=None)}, "UTC"),
        ({"occurred_at": NOW.astimezone(timezone(timedelta(hours=1)))}, "UTC"),
        ({"kind": "run_started", "run_id": None, "status": "started"}, "run_id"),
        ({"kind": "run_terminal", "run_id": SHA, "status": "started"}, "status"),
        (
            {
                "kind": "stage_terminal",
                "run_id": SHA,
                "stage": "gamma_poll",
                "status": "success",
                "elapsed_nanoseconds": None,
            },
            "elapsed",
        ),
        (
            {
                "kind": "recovery",
                "stage": "recovery",
                "status": "success",
                "reason": None,
            },
            "reason",
        ),
        (
            {
                "kind": "deduplication",
                "semantic_id": None,
                "stage": "completed_slot",
                "status": "deduplicated",
            },
            "semantic_id",
        ),
        (
            {
                "kind": "storage_failure",
                "stage": "terminal_commit",
                "status": "failure",
                "reason": None,
            },
            "reason",
        ),
        ({"stage": "arbitrary"}, "stage"),
        ({"status": "arbitrary"}, "status"),
        ({"reason": "arbitrary"}, "reason"),
        ({"elapsed_nanoseconds": -1}, "elapsed"),
        ({"elapsed_nanoseconds": 2**63}, "signed 64-bit"),
        ({"exact_values": {"open_fds": -1}}, "exact_values"),
        ({"exact_values": {"open_fds": 2**63}}, "signed 64-bit"),
        ({"exact_values": {"runs_total": 1}}, "exact_values"),
        ({"exact_values": {"request_id": 1}}, "exact_values"),
        ({"reason": "/tmp/private"}, "reason"),
    ),
)
def test_invalid_fields_and_kind_transitions_fail(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises((ValidationError, ValueError), match=message):
        event(**changes)


@pytest.mark.parametrize(
    "changes",
    (
        {"elapsed_nanoseconds": True},
        {"exact_values": {"open_fds": True}},
        {"exact_values": {1: 1}},
        {"exact_values": {"open_fds": 1.0}},
        {"status": 1},
    ),
)
def test_only_exact_builtin_scalar_and_container_types_are_accepted(
    changes: dict[str, Any],
) -> None:
    with pytest.raises((ValidationError, TypeError, ValueError)):
        event(**changes)


def test_event_id_is_stable_and_bound_to_every_field() -> None:
    payload = event().model_dump(mode="json", exclude={"event_id"})
    left = operation_event_id(payload)
    right = operation_event_id(dict(reversed(tuple(payload.items()))))
    assert left == right
    payload["status"] = "recovery_required"
    assert operation_event_id(payload) != left
    with pytest.raises((TypeError, ValueError)):
        operation_event_id({"bad": object()})


def test_wrong_event_id_fails() -> None:
    with pytest.raises(ValidationError, match="event_id"):
        event(event_id="b" * 64)


def test_run_transition_requires_one_started_event_before_terminal(
    tmp_path: Path,
) -> None:
    root = tmp_path / "audit"
    terminal = event(kind="run_terminal", run_id=SHA, status="success")
    with pytest.raises(OperationCorruptError, match="transition"):
        append_operation(root, terminal)

    append_operation(root, event(kind="run_started", run_id=SHA, status="started"))
    append_operation(root, terminal)
    with pytest.raises(OperationCorruptError, match="transition"):
        append_operation(
            root,
            event(
                kind="run_terminal",
                run_id=SHA,
                status="failure",
                reason="other",
                occurred_at="2026-10-12T12:00:01Z",
            ),
        )


def test_identical_duplicate_is_idempotent_and_conflict_fails(tmp_path: Path) -> None:
    root = tmp_path / "audit"
    value = event()
    append_operation(root, value)
    append_operation(root, value)
    assert load_operation_cut(root).events == (value,)

    document = value.model_dump(mode="python")
    document["status"] = "recovery_required"
    conflicting = OperationEvent.model_construct(**document)
    with pytest.raises(OperationConflictError, match="conflicting"):
        append_operation(root, conflicting)


def test_evidence_argument_must_match_reference(tmp_path: Path) -> None:
    payload = evidence_payload()
    value = event(
        kind="process_sample",
        run_id=SHA,
        status="failure",
        reason="process",
        evidence=blob_ref(payload).model_dump(),
    )
    with pytest.raises(ValueError, match="evidence"):
        append_operation(tmp_path / "missing", value)
    with pytest.raises(ValueError, match="evidence"):
        append_operation(tmp_path / "wrong", value, b"different")
    with pytest.raises(ValueError, match="evidence"):
        append_operation(tmp_path / "unexpected", event(), payload)


@pytest.mark.parametrize("damage", ("missing", "size", "hash"))
def test_blob_corruption_fails_closed(tmp_path: Path, damage: str) -> None:
    root = tmp_path / "audit"
    payload = evidence_payload()
    value = event(
        kind="process_sample",
        run_id=SHA,
        status="failure",
        reason="process",
        evidence=blob_ref(payload).model_dump(),
    )
    append_operation(root, event(kind="run_started", run_id=SHA, status="started"))
    append_operation(root, value, payload)
    path = root / value.evidence.storage_relpath  # type: ignore[union-attr]
    if damage == "missing":
        path.unlink()
    elif damage == "size":
        path.write_bytes(payload + b"x")
    else:
        path.write_bytes(b"x" * len(payload))
    with pytest.raises(OperationCorruptError, match="evidence"):
        load_operation_cut(root)


@pytest.mark.parametrize(
    "raw",
    (
        journal_line(event()).rstrip(b"\n")
        + b"\r"
        + journal_line(event(status="recovery_required")),
        journal_line(event()).replace(b"\n", b"\r\n"),
        journal_line(event())
        + journal_line(event(status="recovery_required")).replace(b"\n", b"\r\n"),
    ),
    ids=("bare-cr", "crlf", "mixed-lf-crlf"),
)
def test_only_lf_delimits_complete_journal_records(tmp_path: Path, raw: bytes) -> None:
    root = tmp_path / "audit"
    journal = append_raw_lines(root, tail=raw)

    with pytest.raises(OperationCorruptError, match="invalid line"):
        load_operation_cut(root, recover_tail=True)

    assert journal.read_bytes() == raw


@pytest.mark.parametrize("control", (b"\x00", b"\t", b"\x1f", b"\n\n"))
def test_raw_control_bytes_and_empty_records_are_invalid_framing(
    tmp_path: Path, control: bytes
) -> None:
    line = journal_line(event()).rstrip(b"\n")
    raw = line[:1] + control + line[1:] + b"\n"
    journal = append_raw_lines(tmp_path / "audit", tail=raw)

    with pytest.raises(OperationCorruptError):
        load_operation_cut(tmp_path / "audit", recover_tail=True)

    assert journal.read_bytes() == raw


def test_json_escaped_carriage_return_is_not_raw_framing(tmp_path: Path) -> None:
    document = event().model_dump(mode="json")
    document["status"] = "clean\rSUPERSECRET"
    document["event_id"] = operation_event_id(
        {key: value for key, value in document.items() if key != "event_id"}
    )
    raw = canonical_json_bytes(document) + b"\n"
    assert b"\\rSUPERSECRET" in raw
    assert b"\r" not in raw
    journal = append_raw_lines(tmp_path / "audit", tail=raw)

    with pytest.raises(
        OperationCorruptError, match="invalid canonical event JSON"
    ) as captured:
        load_operation_cut(tmp_path / "audit")

    assert journal.read_bytes() == raw
    assert_sanitized_domain_error(captured.value, "SUPERSECRET")


@pytest.mark.parametrize("failure", ("json", "utf8", "pydantic", "blob-ref"))
def test_persisted_event_parse_errors_hide_hostile_data_from_tracebacks(
    tmp_path: Path, failure: str
) -> None:
    marker = "SUPERSECRET"
    if failure == "json":
        raw = b'{"value":"SUPERSECRET"\n'
    elif failure == "utf8":
        raw = b'{"value":"SUPERSECRET\xff"}\n'
    else:
        document = event().model_dump(mode="json")
        if failure == "pydantic":
            document["status"] = marker
        else:
            document["evidence"] = {
                "schema_version": 1,
                "sha256": "b" * 64,
                "byte_size": 1,
                "storage_relpath": marker,
            }
        document["event_id"] = operation_event_id(
            {key: value for key, value in document.items() if key != "event_id"}
        )
        raw = canonical_json_bytes(document) + b"\n"
    append_raw_lines(tmp_path / "audit", tail=raw)

    with pytest.raises(OperationCorruptError) as captured:
        load_operation_cut(tmp_path / "audit")

    assert_sanitized_domain_error(captured.value, marker)


def test_persisted_hostile_evidence_is_hidden_from_tracebacks(tmp_path: Path) -> None:
    marker = "SUPERSECRET"
    root = tmp_path / "audit"
    payload = canonical_json_bytes(
        {
            "schema_version": 1,
            "kind": "process_sample",
            "status": "unavailable",
            "detail": marker,
        }
    )
    reference = persist_raw_evidence(root, payload)
    append_raw_lines(
        root,
        event(kind="run_started", run_id=SHA, status="started"),
        event(
            kind="process_sample",
            run_id=SHA,
            status="success",
            evidence=reference.model_dump(),
        ),
    )

    with pytest.raises(OperationCorruptError) as captured:
        load_operation_cut(root)

    assert_sanitized_domain_error(captured.value, marker)


def test_persisted_evidence_errors_hide_backend_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jev_trading_lab.polymarket import operations

    marker = "SUPERSECRET"
    root = tmp_path / "audit"
    payload = evidence_payload()
    reference = persist_raw_evidence(root, payload)
    started = event(kind="run_started", run_id=SHA, status="started")
    sampled = event(
        kind="process_sample",
        run_id=SHA,
        status="success",
        evidence=reference.model_dump(),
    )
    append_raw_lines(root, started, sampled)

    def fail_read(_store: Any, _reference: Any) -> bytes:
        raise operations.BlobError(marker)

    monkeypatch.setattr(operations.BlobStore, "read_verified", fail_read)
    with pytest.raises(OperationCorruptError) as captured:
        load_operation_cut(root)

    assert_sanitized_domain_error(captured.value, marker)


def test_tail_recovery_uses_last_lf_and_never_parses_discarded_cr_bytes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "audit"
    value = event()
    tail = b'SUPERSECRET\r{"torn":'
    journal = append_raw_lines(root, value, tail=tail)

    cut = load_operation_cut(root, recover_tail=True)

    assert cut.events == (value,)
    assert cut.discarded_tail_bytes == len(tail)
    assert journal.read_bytes() == journal_line(value)


def test_bare_cr_without_any_lf_is_one_discarded_tail_candidate(tmp_path: Path) -> None:
    root = tmp_path / "audit"
    raw = journal_line(event()).rstrip(b"\n") + b"\rSUPERSECRET"
    journal = append_raw_lines(root, tail=raw)

    with pytest.raises(OperationCorruptError, match="truncated tail"):
        load_operation_cut(root)
    cut = load_operation_cut(root, recover_tail=True)

    assert cut.events == ()
    assert cut.discarded_tail_bytes == len(raw)
    assert journal.read_bytes() == b""


def test_complete_corruption_is_fatal_but_torn_tail_is_optionally_recovered(
    tmp_path: Path,
) -> None:
    root = tmp_path / "audit"
    value = event()
    append_operation(root, value)
    journal = root / "operations.jsonl"
    journal.write_bytes(journal.read_bytes() + b'{"torn":')

    with pytest.raises(OperationCorruptError, match="truncated tail"):
        load_operation_cut(root)
    cut = load_operation_cut(root, recover_tail=True)
    assert cut.events == (value,)
    assert cut.discarded_tail_bytes == len(b'{"torn":')
    assert journal.read_bytes() == journal_line(value)

    journal.write_bytes(journal_line(value) + b"{}\n")
    with pytest.raises(OperationCorruptError):
        load_operation_cut(root, recover_tail=True)


@pytest.mark.parametrize("object_kind", ("symlink", "fifo", "weak_mode"))
def test_unsafe_journal_objects_are_rejected(tmp_path: Path, object_kind: str) -> None:
    root = tmp_path / "audit"
    root.mkdir(mode=0o700)
    journal = root / "operations.jsonl"
    if object_kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(b"")
        journal.symlink_to(target)
    elif object_kind == "fifo":
        os.mkfifo(journal, 0o600)
    else:
        journal.write_bytes(b"")
        journal.chmod(0o644)
    with pytest.raises(OperationSecurityError):
        append_operation(root, event())


@pytest.mark.parametrize("probe", ("stat", "fstat"))
def test_journal_security_probe_hides_backend_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe: str
) -> None:
    from jev_trading_lab.polymarket import operations

    marker = "SUPERSECRET"
    root = tmp_path / "audit"
    root.mkdir(mode=0o700)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)

    def fail_probe(*_args: Any, **_kwargs: Any) -> os.stat_result:
        raise OSError(marker)

    monkeypatch.setattr(os, probe, fail_probe)
    try:
        with pytest.raises(OperationSecurityError) as captured:
            operations._open_journal(root_fd)
    finally:
        os.close(root_fd)

    assert_sanitized_domain_error(captured.value, marker)


def test_foreign_owned_journal_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "audit"
    root.mkdir(mode=0o700)
    journal = root / "operations.jsonl"
    journal.write_bytes(b"")
    journal.chmod(0o600)
    monkeypatch.setattr(os, "geteuid", lambda: journal.stat().st_uid + 1)
    with pytest.raises(OperationSecurityError):
        append_operation(root, event())


def test_unsafe_ancestor_and_blob_symlink_are_rejected(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o777)
    with pytest.raises(OperationSecurityError):
        append_operation(unsafe / "audit", event())

    unsafe.chmod(0o700)
    target_root = tmp_path / "target-root"
    target_root.mkdir(mode=0o700)
    (unsafe / "audit").symlink_to(target_root, target_is_directory=True)
    with pytest.raises(OperationSecurityError):
        append_operation(unsafe / "audit", event())

    root = tmp_path / "safe"
    root.mkdir(mode=0o700)
    append_operation(root, event(kind="run_started", run_id=SHA, status="started"))
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    (root / "operation-blobs").symlink_to(target, target_is_directory=True)
    payload = evidence_payload()
    value = event(
        kind="process_sample",
        run_id=SHA,
        status="failure",
        reason="process",
        evidence=blob_ref(payload).model_dump(),
    )
    with pytest.raises(OperationSecurityError):
        append_operation(root, value, payload)


def test_write_and_fsync_faults_propagate_without_false_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "audit"
    append_operation(root, event(status="clean"))
    second = event(status="recovery_required")
    original_write = os.write

    def fail_write(descriptor: int, content: bytes | memoryview) -> int:
        if Path(f"/proc/self/fd/{descriptor}").resolve() == root / "operations.jsonl":
            raise OSError("write failed")
        return original_write(descriptor, content)

    monkeypatch.setattr(os, "write", fail_write)
    with pytest.raises(OSError, match="write failed"):
        append_operation(root, second)
    monkeypatch.setattr(os, "write", original_write)

    original_fsync = os.fsync

    def fail_fsync(descriptor: int) -> None:
        if Path(f"/proc/self/fd/{descriptor}").resolve() == root / "operations.jsonl":
            raise OSError("fsync failed")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="fsync failed"):
        append_operation(root, second)


def test_concurrent_appends_serialize_without_lost_events(tmp_path: Path) -> None:
    root = tmp_path / "audit"
    values = [
        event(occurred_at=f"2026-10-12T12:00:{index:02d}Z") for index in range(20)
    ]
    with ThreadPoolExecutor(max_workers=8) as executor:
        tuple(executor.map(lambda value: append_operation(root, value), values))
    assert {item.event_id for item in load_operation_cut(root).events} == {
        item.event_id for item in values
    }


def test_input_resources_and_error_messages_are_bounded(tmp_path: Path) -> None:
    with pytest.raises((ValidationError, ValueError)) as captured:
        event(reason="x" * 100_000)
    assert len(str(captured.value)) < 10_000

    root = tmp_path / "audit"
    root.mkdir(mode=0o700)
    journal = root / "operations.jsonl"
    journal.write_bytes(b"x" * (64 * 1024 * 1024 + 1))
    journal.chmod(0o600)
    with pytest.raises(OperationCorruptError) as captured:
        load_operation_cut(root)
    assert len(str(captured.value)) < 500


def test_event_and_evidence_reject_secrets_paths_and_exception_text(
    tmp_path: Path,
) -> None:
    for value in (
        "«redacted:sk-…»",
        "Authorization: Bearer ***",
        "/home/alice/private",
        "Traceback (most recent call last): ValueError: boom",
    ):
        with pytest.raises((ValidationError, ValueError)):
            event(reason=value)

    unsafe_payloads = (
        b"opaque bytes",
        b'{"schema_version":1,"kind":"process_sample","detail":"password=hunter2"}',
        b'{"schema_version":1,"kind":"process_sample","detail":"Bearer token"}',
        b'{"schema_version":1,"kind":"process_sample","detail":"../private"}',
        b'{"schema_version":1,"kind":"process_sample","detail":"C:\\\\Users\\\\me"}',
        b'{"schema_version":1,"kind":"process_sample","detail":"private/secrets.txt"}',
        b'{"schema_version":1,"kind":"process_sample","detail":"ValueError: boom"}',
        b'{"schema_version":1,"kind":"process_sample","error":"boom"}',
        b'{"schema_version":1,"kind":"process_sample","detail":{"nested":"x"}}',
        b'{"schema_version":1,"kind":"process_sample","detail":"https://example.com"}',
        b'{"schema_version":1,"kind":"process_sample","detail":"api.example.com"}',
    )
    for index, payload in enumerate(unsafe_payloads):
        value = event(
            kind="process_sample",
            run_id=SHA,
            status="failure",
            reason="process",
            evidence=blob_ref(payload).model_dump(),
            occurred_at=f"2026-10-12T12:01:{index:02d}Z",
        )
        with pytest.raises(OperationSecurityError) as captured:
            append_operation(tmp_path / f"unsafe-{index}", value, payload)
        assert len(str(captured.value)) < 200
        assert payload.decode(errors="replace") not in str(captured.value)


def test_canonical_bounded_evidence_is_published(tmp_path: Path) -> None:
    payload = (
        b'{"detail":"probe_unavailable","kind":"process_sample",'
        b'"schema_version":1,"status":"unavailable"}'
    )
    value = event(
        kind="process_sample",
        run_id=SHA,
        status="failure",
        reason="process",
        evidence=blob_ref(payload).model_dump(),
    )
    root = tmp_path / "audit"
    append_operation(root, event(kind="run_started", run_id=SHA, status="started"))
    append_operation(root, value, payload)
    assert load_operation_cut(root).events[-1] == value


@pytest.mark.parametrize(
    "payload",
    (
        canonical_json_bytes(
            {
                "schema_version": 1,
                "kind": "process_sample",
                "status": "unavailable",
                "detail": "password=hunter2",
            }
        ),
        canonical_json_bytes(
            {
                "schema_version": 1,
                "kind": "process_sample",
                "status": "unavailable",
                "detail": "Authorization: Bearer secret",
            }
        ),
        canonical_json_bytes(
            {
                "schema_version": 1,
                "kind": "process_sample",
                "status": "unavailable",
                "detail": "/home/alice/private",
            }
        ),
        canonical_json_bytes(
            {
                "schema_version": 1,
                "kind": "process_sample",
                "status": "unavailable",
                "detail": "https://example.com/private",
            }
        ),
        canonical_json_bytes(
            {
                "schema_version": 1,
                "kind": "process_sample",
                "status": "unavailable",
                "error": "ValueError: boom",
            }
        ),
        b"opaque bytes",
        canonical_json_bytes(
            {
                "schema_version": 1,
                "kind": "process_sample",
                "status": "unknown",
                "detail": "probe_unavailable",
            }
        ),
        canonical_json_bytes(
            {
                "schema_version": 1,
                "kind": "process_sample",
                "status": "unavailable",
                "detail": {"nested": "probe_unavailable"},
            }
        ),
    ),
    ids=("password", "bearer", "path", "url", "error", "opaque", "unknown", "nested"),
)
def test_replayed_hostile_evidence_fails_closed_without_tail_mutation(
    tmp_path: Path, payload: bytes
) -> None:
    root = tmp_path / "audit"
    reference = persist_raw_evidence(root, payload)
    started = event(kind="run_started", run_id=SHA, status="started")
    sampled = event(
        kind="process_sample",
        run_id=SHA,
        status="failure",
        reason="process",
        evidence=reference.model_dump(),
    )
    original = journal_line(started) + journal_line(sampled) + b'{"torn":'
    journal = append_raw_lines(root, tail=original)

    with pytest.raises(OperationCorruptError) as captured:
        load_operation_cut(root, recover_tail=True)

    assert str(captured.value) == "operation evidence is missing or corrupt"
    assert payload.decode(errors="replace") not in str(captured.value)
    assert journal.read_bytes() == original
    assert (root / reference.storage_relpath).read_bytes() == payload


def test_replayed_evidence_must_match_every_referencing_event_kind(
    tmp_path: Path,
) -> None:
    root = tmp_path / "audit"
    payload = evidence_payload()
    reference = persist_raw_evidence(root, payload)
    started = event(kind="run_started", run_id=SHA, status="started")
    sampled = event(
        kind="process_sample",
        run_id=SHA,
        status="success",
        evidence=reference.model_dump(),
    )
    terminal = event(
        kind="run_terminal",
        run_id=SHA,
        status="success",
        evidence=reference.model_dump(),
        occurred_at="2026-10-12T12:00:01Z",
    )
    append_raw_lines(root, started, sampled, terminal)

    with pytest.raises(OperationCorruptError, match="evidence"):
        load_operation_cut(root)


@pytest.mark.parametrize(
    "tail_prefix",
    (
        b"{}\n",
        journal_line(event(kind="run_terminal", run_id=SHA, status="success")),
    ),
)
def test_tail_recovery_validates_complete_prefix_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tail_prefix: bytes,
) -> None:
    root = tmp_path / "audit"
    original = tail_prefix + b'{"torn":'
    journal = append_raw_lines(root, tail=original)
    truncate_calls = 0
    original_ftruncate = os.ftruncate

    def count_ftruncate(descriptor: int, length: int) -> None:
        nonlocal truncate_calls
        truncate_calls += 1
        original_ftruncate(descriptor, length)

    monkeypatch.setattr(os, "ftruncate", count_ftruncate)
    with pytest.raises(OperationCorruptError):
        load_operation_cut(root, recover_tail=True)
    assert truncate_calls == 0
    assert journal.read_bytes() == original


def test_tail_recovery_validates_evidence_before_mutation(tmp_path: Path) -> None:
    root = tmp_path / "audit"
    payload = (
        b'{"detail":"probe_unavailable","kind":"process_sample",'
        b'"schema_version":1,"status":"unavailable"}'
    )
    started = event(kind="run_started", run_id=SHA, status="started")
    sampled = event(
        kind="process_sample",
        run_id=SHA,
        status="failure",
        reason="process",
        evidence=blob_ref(payload).model_dump(),
    )
    original = journal_line(started) + journal_line(sampled) + b'{"torn":'
    journal = append_raw_lines(root, tail=original)

    with pytest.raises(OperationCorruptError, match="evidence"):
        load_operation_cut(root, recover_tail=True)
    assert journal.read_bytes() == original


def test_tail_recovery_faults_propagate_only_after_prefix_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "audit"
    value = event()
    original = journal_line(value) + b'{"torn":'
    journal = append_raw_lines(root, tail=original)

    def fail_truncate(_descriptor: int, _length: int) -> None:
        raise OSError("truncate failed")

    monkeypatch.setattr(os, "ftruncate", fail_truncate)
    with pytest.raises(OSError, match="truncate failed"):
        load_operation_cut(root, recover_tail=True)
    assert journal.read_bytes() == original


def test_conflicting_blob_reference_metadata_is_corruption(tmp_path: Path) -> None:
    root = tmp_path / "audit"
    payload = (
        b'{"detail":"probe_unavailable","kind":"process_sample",'
        b'"schema_version":1,"status":"unavailable"}'
    )
    reference = blob_ref(payload)
    started = event(kind="run_started", run_id=SHA, status="started")
    wrong = event(
        kind="process_sample",
        run_id=SHA,
        status="failure",
        reason="process",
        evidence={**reference.model_dump(), "byte_size": len(payload) + 1},
    )
    correct = event(
        kind="process_sample",
        run_id=SHA,
        status="failure",
        reason="process",
        evidence=reference.model_dump(),
        occurred_at="2026-10-12T12:00:01Z",
    )
    append_operation(root, started)
    append_operation(root, correct, payload)
    journal = root / "operations.jsonl"
    journal.write_bytes(
        journal_line(started) + journal_line(wrong) + journal_line(correct)
    )

    with pytest.raises(OperationCorruptError, match="conflicting.*reference"):
        load_operation_cut(root)


def test_identical_blob_references_are_verified_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jev_trading_lab.polymarket import operations

    root = tmp_path / "audit"
    payload = (
        b'{"detail":"probe_unavailable","kind":"process_sample",'
        b'"schema_version":1,"status":"unavailable"}'
    )
    reference = blob_ref(payload)
    started = event(kind="run_started", run_id=SHA, status="started")
    first = event(
        kind="process_sample",
        run_id=SHA,
        status="failure",
        reason="process",
        evidence=reference.model_dump(),
    )
    second = event(
        kind="process_sample",
        run_id=SHA,
        status="failure",
        reason="process",
        evidence=reference.model_dump(),
        occurred_at="2026-10-12T12:00:01Z",
    )
    append_operation(root, started)
    append_operation(root, first, payload)
    append_raw_lines(root, started, first, second)
    calls = 0
    original = operations.BlobStore.read_verified

    def count_read(store: Any, reference: Any) -> bytes:
        nonlocal calls
        calls += 1
        return original(store, reference)

    monkeypatch.setattr(operations.BlobStore, "read_verified", count_read)
    cut = load_operation_cut(root)
    assert cut.valid_blobs == 1
    assert calls == 1


def test_duplicate_retry_reestablishes_journal_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "audit"
    value = event()
    original_fsync = os.fsync
    failed = False

    def fail_once(descriptor: int) -> None:
        nonlocal failed
        if (
            not failed
            and Path(f"/proc/self/fd/{descriptor}").resolve()
            == root / "operations.jsonl"
        ):
            failed = True
            raise OSError("fsync failed")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_once)
    with pytest.raises(OSError, match="fsync failed"):
        append_operation(root, value)
    assert (root / "operations.jsonl").read_bytes() == journal_line(value)

    journal_fsyncs = 0

    def count_fsync(descriptor: int) -> None:
        nonlocal journal_fsyncs
        if Path(f"/proc/self/fd/{descriptor}").resolve() == root / "operations.jsonl":
            journal_fsyncs += 1
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", count_fsync)
    append_operation(root, value)
    assert journal_fsyncs == 1
    assert (root / "operations.jsonl").read_bytes() == journal_line(value)


def test_retry_reestablishes_file_and_parent_after_creation_dir_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "audit"
    journal = root / "operations.jsonl"
    value = event()
    original_fsync = os.fsync
    journal_was_fsynced = False
    failed = False

    def fail_after_journal_fsync(descriptor: int) -> None:
        nonlocal failed, journal_was_fsynced
        target = Path(f"/proc/self/fd/{descriptor}").resolve()
        if target == journal:
            journal_was_fsynced = True
        if not failed and journal_was_fsynced and target == root:
            failed = True
            raise OSError("directory fsync failed")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_after_journal_fsync)
    with pytest.raises(OSError, match="directory fsync failed"):
        append_operation(root, value)
    assert journal.read_bytes() == journal_line(value)

    journal_fsyncs = 0
    root_fsyncs = 0

    def count_fsync(descriptor: int) -> None:
        nonlocal journal_fsyncs, root_fsyncs
        target = Path(f"/proc/self/fd/{descriptor}").resolve()
        if target == journal:
            journal_fsyncs += 1
        elif target == root:
            root_fsyncs += 1
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", count_fsync)
    append_operation(root, value)
    assert journal_fsyncs == 1
    assert root_fsyncs >= 1
    assert journal.read_bytes() == journal_line(value)


def test_tail_recovery_retry_reestablishes_file_and_parent_durability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "audit"
    value = event()
    journal = append_raw_lines(root, value, tail=b'{"torn":')
    original_fsync = os.fsync
    failed = False

    def fail_journal_fsync_once(descriptor: int) -> None:
        nonlocal failed
        if not failed and Path(f"/proc/self/fd/{descriptor}").resolve() == journal:
            failed = True
            raise OSError("journal fsync failed")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_journal_fsync_once)
    with pytest.raises(OSError, match="journal fsync failed"):
        load_operation_cut(root, recover_tail=True)
    assert journal.read_bytes() == journal_line(value)

    journal_fsyncs = 0
    root_fsyncs = 0

    def count_fsync(descriptor: int) -> None:
        nonlocal journal_fsyncs, root_fsyncs
        target = Path(f"/proc/self/fd/{descriptor}").resolve()
        if target == journal:
            journal_fsyncs += 1
        elif target == root:
            root_fsyncs += 1
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", count_fsync)
    cut = load_operation_cut(root, recover_tail=True)
    assert cut.events == (value,)
    assert cut.discarded_tail_bytes == 0
    assert journal_fsyncs == 1
    assert root_fsyncs >= 1


def test_load_retry_reestablishes_parent_durability_after_creation_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "audit"
    original_fsync = os.fsync
    failed = False

    def fail_once(descriptor: int) -> None:
        nonlocal failed
        if not failed and Path(f"/proc/self/fd/{descriptor}").resolve() == root:
            failed = True
            raise OSError("directory fsync failed")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_once)
    with pytest.raises(OSError, match="directory fsync failed"):
        load_operation_cut(root)
    assert (root / "operations.jsonl").read_bytes() == b""

    root_fsyncs = 0

    def count_fsync(descriptor: int) -> None:
        nonlocal root_fsyncs
        if Path(f"/proc/self/fd/{descriptor}").resolve() == root:
            root_fsyncs += 1
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", count_fsync)
    assert load_operation_cut(root).events == ()
    assert root_fsyncs >= 1


@pytest.mark.parametrize(
    "changes",
    (
        {"kind": "startup", "stage": "startup", "status": "clean"},
        {"kind": "run_started", "run_id": SHA, "status": "started"},
        {"kind": "run_terminal", "run_id": SHA, "status": "recovered"},
        {
            "kind": "stage_terminal",
            "run_id": SHA,
            "stage": "gamma_poll",
            "provider": "gamma",
            "status": "timeout",
            "reason": "timeout",
            "elapsed_nanoseconds": 1,
        },
        {
            "kind": "stage_terminal",
            "run_id": SHA,
            "stage": "openai",
            "provider": "openai",
            "status": "failure",
            "reason": "provider_error",
            "elapsed_nanoseconds": 1,
        },
        {
            "kind": "stage_terminal",
            "run_id": SHA,
            "stage": "eligibility",
            "provider": None,
            "status": "skipped",
            "elapsed_nanoseconds": 1,
        },
        {
            "kind": "recovery",
            "stage": "recovery",
            "status": "success",
            "reason": "partial_tail",
            "exact_values": {"discarded_bytes": 1},
        },
        {
            "kind": "storage_failure",
            "stage": "terminal_commit",
            "status": "failure",
            "reason": "directory_fsync",
        },
        {
            "kind": "metrics_generation",
            "stage": "metrics_render",
            "status": "failure",
            "reason": "write",
        },
    ),
)
def test_approved_event_contract_matrix(changes: dict[str, Any]) -> None:
    event(**changes)


@pytest.mark.parametrize(
    "changes",
    (
        {
            "kind": "stage_terminal",
            "run_id": SHA,
            "stage": "startup",
            "provider": "openai",
            "status": "success",
            "elapsed_nanoseconds": 1,
        },
        {
            "kind": "stage_terminal",
            "run_id": SHA,
            "stage": "gamma_poll",
            "provider": "openai",
            "status": "success",
            "elapsed_nanoseconds": 1,
        },
        {
            "kind": "deduplication",
            "semantic_id": SHA,
            "stage": "startup",
            "status": "deduplicated",
        },
        {
            "kind": "storage_failure",
            "stage": "terminal_commit",
            "status": "failure",
            "reason": "fees",
        },
        {
            "kind": "recovery",
            "stage": "recovery",
            "status": "failure",
            "reason": "fees",
            "exact_values": {},
        },
        {
            "kind": "stage_terminal",
            "run_id": SHA,
            "stage": "openai",
            "provider": "openai",
            "status": "failure",
            "reason": "wide_spread",
            "elapsed_nanoseconds": 1,
        },
        {
            "kind": "metrics_generation",
            "stage": "metrics_render",
            "status": "failure",
            "reason": "provider_error",
        },
    ),
)
def test_event_contract_matrix_rejects_denied_mutations(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError, match="contract|provider|reason|stage"):
        event(**changes)


@pytest.mark.parametrize(
    "key",
    (
        "runs_count",
        "success_count",
        "successful_runs",
        "latency_sum",
        "aggregate_runs",
        "unknown_value",
        "resident_memory_bytes",
    ),
)
def test_exact_values_use_closed_task3_raw_fact_schemas(key: str) -> None:
    with pytest.raises(ValidationError, match="exact_values"):
        event(
            kind="recovery",
            stage="recovery",
            status="success",
            reason="partial_tail",
            exact_values={key: 1},
        )


def test_exact_values_reject_bool_and_subclasses() -> None:
    class TaggedInt(int):
        pass

    class TaggedStr(str):
        pass

    for values in (
        {"discarded_bytes": True},
        {"discarded_bytes": TaggedInt(1)},
        {TaggedStr("discarded_bytes"): 1},
    ):
        with pytest.raises((ValidationError, TypeError)):
            event(
                kind="recovery",
                stage="recovery",
                status="success",
                reason="partial_tail",
                exact_values=values,
            )


def test_blob_reference_schema_metadata_is_strict() -> None:
    payload = b"{}"
    with pytest.raises(ValidationError, match="schema_version"):
        BlobRefModel(
            schema_version=2,
            sha256=hashlib.sha256(payload).hexdigest(),
            byte_size=len(payload),
            storage_relpath=(
                "operation-blobs/44/13/"
                "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
            ),
        )

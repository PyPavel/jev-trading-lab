from __future__ import annotations

import hashlib
import os
import stat
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
        "provider": None,
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
        "exact_values": {"open_fds": 4} if kind is OperationKind.PROCESS_SAMPLE else {},
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
        sha256=digest,
        byte_size=len(payload),
        storage_relpath=f"operation-blobs/{digest[:2]}/{digest[2:4]}/{digest}",
    )


def journal_line(value: OperationEvent) -> bytes:
    return canonical_json_bytes(value.model_dump(mode="json")) + b"\n"


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
            "exact_values": {"resident_memory_bytes": 4096, "open_fds": 4},
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
    payload = b'{"probe":"unavailable"}'
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
        (
            {
                "kind": "process_sample",
                "run_id": SHA,
                "status": "success",
                "exact_values": {},
            },
            "exact_values",
        ),
        ({"stage": "arbitrary"}, "stage"),
        ({"status": "arbitrary"}, "status"),
        ({"reason": "arbitrary"}, "reason"),
        ({"elapsed_nanoseconds": -1}, "elapsed"),
        ({"elapsed_nanoseconds": 2**63}, "signed 64-bit"),
        ({"exact_values": {"open_fds": -1}}, "exact_values"),
        ({"exact_values": {"open_fds": 2**63}}, "signed 64-bit"),
        ({"exact_values": {"runs_total": 1}}, "preaggregated"),
        ({"exact_values": {"request_id": 1}}, "label"),
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
    payload = b"safe evidence"
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
    payload = b"safe evidence"
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
    payload = b"safe"
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
        "sk-abcdefghijklmnopqrstuvwxyz",
        "Authorization: Bearer secret",
        "/home/alice/private",
        "Traceback (most recent call last): ValueError: boom",
    ):
        with pytest.raises((ValidationError, ValueError)):
            event(reason=value)

    payload = b'{"authorization":"Bearer secret"}'
    value = event(
        kind="process_sample",
        run_id=SHA,
        status="failure",
        reason="process",
        evidence=blob_ref(payload).model_dump(),
    )
    with pytest.raises(OperationSecurityError):
        append_operation(tmp_path / "audit", value, payload)

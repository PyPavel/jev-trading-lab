from __future__ import annotations

import hashlib
import os
import signal
import stat
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, fields
from datetime import UTC, datetime, timedelta, timezone
from decimal import MAX_PREC, localcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.polymarket.operations import (
    BlobRefModel,
    BuildIdentity,
    JournalRecovery,
    OperationConflictError,
    OperationCorruptError,
    OperationEvent,
    OperationKind,
    OperationSecurityError,
    ProcessSample,
    append_operation,
    build_identity_exact_values,
    build_identity_from_exact_values,
    capture_build_identity,
    load_operation_cut,
    operation_event_id,
    package_version_code,
    process_sample_exact_values,
    process_sample_from_exact_values,
    recover_operation_tail,
    recovery_event,
    sample_process,
)

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
SHA = "a" * 64
GIT_COMMIT = "a" * 40


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
        "exact_values": {
            OperationKind.STARTUP: {
                "build_version_code": 1000,
                "build_identity_status_code": 3,
            },
            OperationKind.RECOVERY: {"discarded_bytes": 1},
            OperationKind.PROCESS_SAMPLE: {
                "user_cpu_nanoseconds": 0,
                "system_cpu_nanoseconds": 0,
                "probe_available": 0,
            },
        }.get(kind, {}),
        "evidence": None,
    }
    defaults.update(changes)
    if kind is OperationKind.RECOVERY:
        defaults.setdefault("journal_kind", "operations")
    if (
        kind is OperationKind.PROCESS_SAMPLE
        and defaults["status"] == "failure"
        and "exact_values" not in changes
    ):
        defaults["exact_values"] = {}
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


def test_recovery_event_binds_kind_count_and_content_addressed_identity() -> None:
    value = recovery_event(JournalRecovery("artifacts", 17), occurred_at=NOW)

    assert value.journal_kind == "artifacts"
    assert value.reason == "partial_tail"
    assert value.exact_values == {"discarded_bytes": 17}
    assert value.event_id == operation_event_id(
        value.model_dump(mode="json", exclude={"event_id"})
    )


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
            "exact_values": {
                "user_cpu_nanoseconds": 0,
                "system_cpu_nanoseconds": 0,
                "probe_available": 0,
            },
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
                occurred_at="2026-10-12T11:59:58Z",
            ),
        )
        if value.kind is OperationKind.RUN_TERMINAL:
            prerequisites += (
                event(
                    kind="process_sample",
                    run_id=value.run_id,
                    occurred_at="2026-10-12T11:59:59Z",
                ),
            )
        for prerequisite in prerequisites:
            append_operation(root, prerequisite)

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
    append_operation(root, event(kind="process_sample", run_id=SHA))
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
        recover_operation_tail(root)

    assert journal.read_bytes() == raw


@pytest.mark.parametrize("control", (b"\x00", b"\t", b"\x1f", b"\n\n"))
def test_raw_control_bytes_and_empty_records_are_invalid_framing(
    tmp_path: Path, control: bytes
) -> None:
    line = journal_line(event()).rstrip(b"\n")
    raw = line[:1] + control + line[1:] + b"\n"
    journal = append_raw_lines(tmp_path / "audit", tail=raw)

    with pytest.raises(OperationCorruptError):
        recover_operation_tail(tmp_path / "audit")

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

    recovery = recover_operation_tail(root)

    assert recovery == JournalRecovery("operations", len(tail))
    assert load_operation_cut(root).events == (value,)
    assert journal.read_bytes() == journal_line(value)


def test_bare_cr_without_any_lf_is_one_discarded_tail_candidate(tmp_path: Path) -> None:
    root = tmp_path / "audit"
    raw = journal_line(event()).rstrip(b"\n") + b"\rSUPERSECRET"
    journal = append_raw_lines(root, tail=raw)

    with pytest.raises(OperationCorruptError, match="truncated tail"):
        load_operation_cut(root)
    recovery = recover_operation_tail(root)

    assert recovery == JournalRecovery("operations", len(raw))
    assert load_operation_cut(root).events == ()
    assert journal.read_bytes() == b""


def test_complete_corruption_is_fatal_but_torn_tail_is_explicitly_recovered(
    tmp_path: Path,
) -> None:
    root = tmp_path / "audit"
    value = event()
    append_operation(root, value)
    journal = root / "operations.jsonl"
    tail = b'{"torn":'
    original = journal.read_bytes() + tail
    journal.write_bytes(original)

    with pytest.raises(OperationCorruptError, match="truncated tail"):
        load_operation_cut(root)
    assert journal.read_bytes() == original
    assert recover_operation_tail(root) == JournalRecovery("operations", len(tail))
    assert load_operation_cut(root).events == (value,)
    assert journal.read_bytes() == journal_line(value)
    assert recover_operation_tail(root) is None

    journal.write_bytes(journal_line(value) + b"{}\n")
    complete_corruption = journal.read_bytes()
    with pytest.raises(OperationCorruptError):
        recover_operation_tail(root)
    assert journal.read_bytes() == complete_corruption


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
        recover_operation_tail(root)

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
        recover_operation_tail(root)
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
        recover_operation_tail(root)
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
        recover_operation_tail(root)
    assert journal.read_bytes() == original


def test_conflicting_blob_reference_metadata_is_corruption(tmp_path: Path) -> None:
    root = tmp_path / "audit"
    payload = (
        b'{"detail":"probe_unavailable","kind":"process_sample",'
        b'"schema_version":1,"status":"unavailable"}'
    )
    reference = blob_ref(payload)
    started = event(kind="run_started", run_id=SHA, status="started")
    other_started = event(
        kind="run_started",
        run_id="b" * 64,
        status="started",
        occurred_at="2026-10-12T12:00:01Z",
    )
    wrong = event(
        kind="process_sample",
        run_id=SHA,
        status="failure",
        reason="process",
        evidence={**reference.model_dump(), "byte_size": len(payload) + 1},
    )
    correct = event(
        kind="process_sample",
        run_id="b" * 64,
        status="failure",
        reason="process",
        evidence=reference.model_dump(),
        occurred_at="2026-10-12T12:00:02Z",
    )
    first_terminal = event(
        kind="run_terminal", run_id=SHA, occurred_at="2026-10-12T12:00:01Z"
    )
    append_operation(root, other_started)
    append_operation(root, correct, payload)
    journal = root / "operations.jsonl"
    journal.write_bytes(
        journal_line(started)
        + journal_line(wrong)
        + journal_line(first_terminal)
        + journal_line(other_started)
        + journal_line(correct)
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
    other_started = event(
        kind="run_started", run_id="b" * 64, occurred_at="2026-10-12T12:00:01Z"
    )
    first = event(
        kind="process_sample",
        run_id=SHA,
        status="failure",
        reason="process",
        evidence=reference.model_dump(),
    )
    second = event(
        kind="process_sample",
        run_id="b" * 64,
        status="failure",
        reason="process",
        evidence=reference.model_dump(),
        occurred_at="2026-10-12T12:00:02Z",
    )
    first_terminal = event(
        kind="run_terminal", run_id=SHA, occurred_at="2026-10-12T12:00:01Z"
    )
    append_operation(root, started)
    append_operation(root, first, payload)
    append_raw_lines(root, started, first, first_terminal, other_started, second)
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
        recover_operation_tail(root)
    assert journal.read_bytes() == journal_line(value)

    journal.write_bytes(journal_line(value) + b'{"torn":')
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
    assert recover_operation_tail(root) == JournalRecovery(
        "operations", len(b'{"torn":')
    )
    assert load_operation_cut(root).events == (value,)
    assert journal_fsyncs == 1
    assert root_fsyncs >= 1


def test_load_is_read_only_after_journal_creation(tmp_path: Path) -> None:
    root = tmp_path / "audit"
    assert load_operation_cut(root).events == ()
    journal = root / "operations.jsonl"
    before = (journal.stat().st_mtime_ns, journal.read_bytes())

    assert load_operation_cut(root).events == ()

    assert (journal.stat().st_mtime_ns, journal.read_bytes()) == before


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


def test_process_sample_normalizes_linux_units_and_exact_cpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jev_trading_lab.polymarket import operations

    monkeypatch.setattr(operations, "_read_proc_statm", lambda: "99 3 0 0 0 0 0\n")
    monkeypatch.setattr(operations.os, "sysconf", lambda name: 4096)
    monkeypatch.setattr(
        operations.resource,
        "getrusage",
        lambda who: SimpleNamespace(
            ru_maxrss=7,
            ru_utime=0.1234567899,
            ru_stime=0.9876543219,
        ),
    )
    monkeypatch.setattr(operations, "_count_open_fds", lambda: 11)

    with localcontext() as context:
        context.prec = MAX_PREC
        value = sample_process()

    assert value == ProcessSample(
        resident_memory_bytes=12_288,
        peak_resident_memory_bytes=7_168,
        user_cpu_nanoseconds=123_456_789,
        system_cpu_nanoseconds=987_654_321,
        open_fds=11,
        probe_available=True,
    )
    assert value.__dataclass_params__.frozen
    with pytest.raises((AttributeError, TypeError)):
        value.open_fds = 12  # type: ignore[misc]


def test_process_sample_does_not_use_wall_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jev_trading_lab.polymarket import operations

    monkeypatch.setattr(operations, "_read_proc_statm", lambda: "1 1\n")
    monkeypatch.setattr(operations.os, "sysconf", lambda name: 1024)
    monkeypatch.setattr(
        operations.resource,
        "getrusage",
        lambda who: SimpleNamespace(ru_maxrss=1, ru_utime=1.25, ru_stime=2.5),
    )
    monkeypatch.setattr(operations, "_count_open_fds", lambda: 1)
    monkeypatch.setattr(
        operations,
        "datetime",
        SimpleNamespace(now=lambda *_args: pytest.fail("wall clock used")),
    )

    first = sample_process()
    second = sample_process()

    assert first.user_cpu_nanoseconds == second.user_cpu_nanoseconds == 1_250_000_000
    system_values = (first.system_cpu_nanoseconds, second.system_cpu_nanoseconds)
    assert system_values == (2_500_000_000, 2_500_000_000)


@pytest.mark.parametrize(
    ("statm", "page_size"),
    (("", 4096), ("1", 4096), ("1 x", 4096), ("1 -1", 4096), ("1 1", 0)),
)
def test_malformed_statm_or_sysconf_is_an_unsupported_probe(
    monkeypatch: pytest.MonkeyPatch, statm: str, page_size: int
) -> None:
    from jev_trading_lab.polymarket import operations

    monkeypatch.setattr(operations, "_read_proc_statm", lambda: statm)
    monkeypatch.setattr(operations.os, "sysconf", lambda name: page_size)
    monkeypatch.setattr(
        operations.resource,
        "getrusage",
        lambda who: SimpleNamespace(ru_maxrss=1, ru_utime=0.0, ru_stime=0.0),
    )
    monkeypatch.setattr(operations, "_count_open_fds", lambda: 1)

    value = sample_process()

    assert value.resident_memory_bytes is None
    assert not value.probe_available


def test_unsupported_proc_and_open_fd_race_return_none_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jev_trading_lab.polymarket import operations

    monkeypatch.setattr(
        operations,
        "_read_proc_statm",
        lambda: (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setattr(
        operations,
        "_count_open_fds",
        lambda: (_ for _ in ()).throw(OSError("fd raced")),
    )
    monkeypatch.setattr(
        operations.resource,
        "getrusage",
        lambda who: SimpleNamespace(ru_maxrss=2, ru_utime=0.0, ru_stime=0.0),
    )

    value = sample_process()

    assert value.resident_memory_bytes is None
    assert value.open_fds is None
    assert value.peak_resident_memory_bytes == 2048
    assert not value.probe_available


def test_open_fd_probe_counts_entries_without_stat_or_follow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jev_trading_lab.polymarket import operations

    class Entry:
        def stat(self, *, follow_symlinks: bool = True) -> None:
            pytest.fail(f"fd entry followed: {follow_symlinks}")

    class Entries:
        def __enter__(self) -> Entries:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self) -> Any:
            return iter((Entry(), Entry(), Entry()))

    monkeypatch.setattr(operations.os, "scandir", lambda path: Entries())
    assert operations._count_open_fds() == 3


@pytest.mark.parametrize(
    ("version", "expected"),
    (("0.1.0", 1000), ("1.2.3", 1_002_003), ("999.999.999", 999_999_999)),
)
def test_package_version_code_encodes_supported_semver(
    version: str, expected: int
) -> None:
    assert package_version_code(version) == expected


@pytest.mark.parametrize(
    "version",
    (
        "1.2",
        "1.2.3.4",
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "1.2.3-alpha",
        "1.2.3+build",
        "1.1000.0",
        "1.0.1000",
        "9223372036855.0.0",
        "",
        True,
    ),
)
def test_package_version_code_rejects_unsupported_or_overflow_formats(
    version: Any,
) -> None:
    with pytest.raises((TypeError, ValueError), match="version"):
        package_version_code(version)


def _git_runner(
    *, commit: str = GIT_COMMIT, timestamp: str = "1700000000", dirty: str = ""
) -> Any:
    values = {
        ("rev-parse", "HEAD"): commit,
        ("show", "-s", "--format=%at", "HEAD"): timestamp,
        ("ls-files", "--others", "--exclude-standard"): dirty,
    }

    def run(arguments: tuple[str, ...]) -> str:
        return values[arguments]

    return run


@pytest.mark.parametrize(
    ("expected_commit", "commit", "dirty", "status", "dirty_value"),
    (
        (GIT_COMMIT, GIT_COMMIT, "", "valid", False),
        (GIT_COMMIT, GIT_COMMIT, " M source.py", "dirty", True),
        ("b" * 40, GIT_COMMIT, "", "mismatch", False),
        ("b" * 40, GIT_COMMIT, " M source.py", "dirty", True),
        (None, GIT_COMMIT, "", "unknown", None),
    ),
)
def test_build_identity_statuses_are_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch,
    expected_commit: str | None,
    commit: str,
    dirty: str,
    status: str,
    dirty_value: bool,
) -> None:
    from jev_trading_lab.polymarket import operations

    monkeypatch.setattr(operations, "_run_git", _git_runner(commit=commit, dirty=dirty))
    monkeypatch.setattr(operations, "_git_dirty", lambda: bool(dirty))

    value = capture_build_identity(expected_commit)

    assert value == BuildIdentity(1000, 1_700_000_000, dirty_value, status)
    statuses = ("valid", "dirty", "mismatch", "unknown")
    assert sum(value.status == item for item in statuses) == 1


def test_build_identity_unknown_on_unavailable_or_malformed_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jev_trading_lab.polymarket import operations

    for runner in (
        lambda arguments: (_ for _ in ()).throw(OSError("/private/path SECRET")),
        _git_runner(timestamp="not-a-timestamp"),
    ):
        monkeypatch.setattr(operations, "_run_git", runner)
        value = capture_build_identity(GIT_COMMIT)
        assert value == BuildIdentity(1000, None, None, "unknown")
        assert "/private/path" not in repr(value)
        assert "SECRET" not in repr(value)


def test_build_identity_rejects_noncanonical_expected_commit() -> None:
    for value in ("abc", "A" * 64, True, 1):
        with pytest.raises((TypeError, ValueError), match="expected_commit"):
            capture_build_identity(value)  # type: ignore[arg-type]


def test_metric_facing_build_identity_has_no_commit_path_or_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jev_trading_lab.polymarket import operations

    marker = "/home/private/repository"
    monkeypatch.setattr(operations, "_run_git", _git_runner())
    value = capture_build_identity(GIT_COMMIT)
    rendered = repr(value)

    assert tuple(field.name for field in fields(value)) == (
        "version_code",
        "commit_timestamp_seconds",
        "dirty",
        "status",
    )
    assert GIT_COMMIT not in rendered
    assert marker not in rendered
    assert str(os.getpid()) not in asdict(value).values()
    assert len(rendered) < 200


def test_process_sample_exact_values_round_trip(tmp_path: Path) -> None:
    exact_values = {
        "resident_memory_bytes": 4096,
        "peak_resident_memory_bytes": 8192,
        "user_cpu_nanoseconds": 10,
        "system_cpu_nanoseconds": 20,
        "open_fds": 3,
        "probe_available": 1,
    }
    root = tmp_path / "audit"
    started = event(kind="run_started", run_id=SHA, status="started")
    sampled = event(
        kind="process_sample",
        run_id=SHA,
        status="success",
        exact_values=exact_values,
    )

    append_operation(root, started)
    append_operation(root, sampled)

    assert load_operation_cut(root).events == (started, sampled)


@pytest.mark.parametrize("key", ("runs_total", "cpu_sum", "pid", "commit_sha"))
def test_process_sample_exact_values_remain_raw_and_closed(key: str) -> None:
    with pytest.raises(ValidationError, match="exact_values"):
        event(
            kind="process_sample",
            run_id=SHA,
            status="success",
            exact_values={key: 1},
        )


def test_process_and_build_dataclasses_reject_invalid_builtin_values() -> None:
    class TaggedInt(int):
        pass

    invalid_process = (
        {"resident_memory_bytes": -1},
        {"peak_resident_memory_bytes": 2**63},
        {"user_cpu_nanoseconds": True},
        {"system_cpu_nanoseconds": TaggedInt(1)},
        {"open_fds": -1},
        {"probe_available": 1},
    )
    process_defaults: dict[str, Any] = {
        "resident_memory_bytes": None,
        "peak_resident_memory_bytes": None,
        "user_cpu_nanoseconds": 0,
        "system_cpu_nanoseconds": 0,
        "open_fds": None,
        "probe_available": False,
    }
    for changes in invalid_process:
        with pytest.raises((TypeError, ValueError)):
            ProcessSample(**(process_defaults | changes))

    with pytest.raises((TypeError, ValueError)):
        BuildIdentity(True, None, None, "unknown")  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        BuildIdentity(1000, -1, None, "unknown")
    with pytest.raises((TypeError, ValueError)):
        BuildIdentity(1000, None, 0, "unknown")  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        BuildIdentity(1000, None, None, "other")  # type: ignore[arg-type]


def test_build_identity_uses_author_timestamp_from_real_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=repository, check=True)
    subprocess.run(("git", "config", "user.name", "Test"), cwd=repository, check=True)
    subprocess.run(
        ("git", "config", "user.email", "test@example.invalid"),
        cwd=repository,
        check=True,
    )
    (repository / "tracked").write_text("fact\n", encoding="utf-8")
    subprocess.run(("git", "add", "tracked"), cwd=repository, check=True)
    environment = os.environ | {
        "GIT_AUTHOR_DATE": "2001-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2020-01-01T00:00:00+00:00",
    }
    subprocess.run(
        ("git", "commit", "-q", "-m", "timestamp fixture"),
        cwd=repository,
        env=environment,
        check=True,
    )
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    author_timestamp = int(
        subprocess.run(
            ("git", "show", "-s", "--format=%at", "HEAD"),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    committer_timestamp = int(
        subprocess.run(
            ("git", "show", "-s", "--format=%ct", "HEAD"),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    assert author_timestamp != committer_timestamp
    monkeypatch.chdir(repository)

    assert capture_build_identity(commit).commit_timestamp_seconds == author_timestamp


@pytest.mark.parametrize(
    "value",
    (
        BuildIdentity(1000, 1, False, "valid"),
        BuildIdentity(1000, 1, True, "dirty"),
        BuildIdentity(1000, 1, False, "mismatch"),
        BuildIdentity(1000, None, None, "unknown"),
        BuildIdentity(1000, 1, None, "unknown"),
    ),
)
def test_build_identity_accepts_only_consistent_states(value: BuildIdentity) -> None:
    assert value.status in {"valid", "dirty", "mismatch", "unknown"}


@pytest.mark.parametrize(
    "arguments",
    (
        (1000, None, False, "valid"),
        (1000, 1, True, "valid"),
        (1000, None, True, "dirty"),
        (1000, 1, False, "dirty"),
        (1000, None, False, "mismatch"),
        (1000, 1, True, "mismatch"),
        (1000, 1, False, "unknown"),
        (1000, None, True, "unknown"),
    ),
)
def test_build_identity_rejects_contradictory_states(
    arguments: tuple[Any, ...],
) -> None:
    with pytest.raises((TypeError, ValueError), match="status|state"):
        BuildIdentity(*arguments)  # type: ignore[arg-type]


def test_capture_git_failure_returns_sanitized_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jev_trading_lab.polymarket import operations

    monkeypatch.setattr(
        operations,
        "_run_git",
        lambda _arguments: (_ for _ in ()).throw(ValueError("/private SECRET")),
    )
    value = capture_build_identity(GIT_COMMIT)
    assert value == BuildIdentity(1000, None, None, "unknown")
    assert "/private" not in repr(value)
    assert "SECRET" not in repr(value)


def test_capture_without_expected_commit_has_unknown_dirty_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jev_trading_lab.polymarket import operations

    monkeypatch.setattr(operations, "_run_git", _git_runner(dirty=" M tracked"))
    monkeypatch.setattr(operations, "_git_dirty", lambda: True)
    value = capture_build_identity(None)
    assert value == BuildIdentity(1000, 1_700_000_000, None, "unknown")


@pytest.mark.parametrize(
    "changes",
    (
        {"probe_available": True},
        {"resident_memory_bytes": 1, "probe_available": True},
        {
            "resident_memory_bytes": 1,
            "peak_resident_memory_bytes": 2,
            "probe_available": True,
        },
    ),
)
def test_process_sample_rejects_available_with_missing_optional_probe(
    changes: dict[str, Any],
) -> None:
    defaults: dict[str, Any] = {
        "resident_memory_bytes": None,
        "peak_resident_memory_bytes": None,
        "user_cpu_nanoseconds": 0,
        "system_cpu_nanoseconds": 0,
        "open_fds": None,
        "probe_available": False,
    }
    with pytest.raises((TypeError, ValueError), match="probe_available"):
        ProcessSample(**(defaults | changes))


def test_process_sample_available_requires_all_optional_probes() -> None:
    value = ProcessSample(1, 2, 3, 4, 5, True)
    assert value.probe_available
    with pytest.raises((TypeError, ValueError), match="probe_available"):
        ProcessSample(1, 2, 3, 4, 5, False)


def test_process_and_build_exact_values_round_trip_without_free_text() -> None:
    samples = (
        ProcessSample(1, 2, 3, 4, 5, True),
        ProcessSample(None, 2, 3, 4, None, False),
    )
    identities = (
        BuildIdentity(1000, 10, False, "valid"),
        BuildIdentity(1000, 10, True, "dirty"),
        BuildIdentity(1000, 10, False, "mismatch"),
        BuildIdentity(1000, None, None, "unknown"),
        BuildIdentity(1000, 10, None, "unknown"),
    )
    for sample in samples:
        exact_values = process_sample_exact_values(sample)
        assert process_sample_from_exact_values(exact_values) == sample
        assert set(exact_values) <= {
            "resident_memory_bytes",
            "peak_resident_memory_bytes",
            "user_cpu_nanoseconds",
            "system_cpu_nanoseconds",
            "open_fds",
            "probe_available",
        }
    for identity in identities:
        exact_values = build_identity_exact_values(identity)
        assert build_identity_from_exact_values(exact_values) == identity
        assert set(exact_values) <= {
            "build_version_code",
            "build_commit_timestamp_seconds",
            "build_dirty",
            "build_identity_status_code",
        }


def test_exact_value_inverse_helpers_reject_non_builtin_values() -> None:
    class TaggedInt(int):
        pass

    for values in (
        {
            "user_cpu_nanoseconds": True,
            "system_cpu_nanoseconds": 0,
            "probe_available": 0,
        },
        {
            "user_cpu_nanoseconds": 0,
            "system_cpu_nanoseconds": TaggedInt(0),
            "probe_available": 0,
        },
    ):
        with pytest.raises((TypeError, ValueError)):
            process_sample_from_exact_values(values)
    for values in (
        {"build_version_code": True, "build_identity_status_code": 3},
        {"build_version_code": 1000, "build_identity_status_code": TaggedInt(3)},
    ):
        with pytest.raises((TypeError, ValueError)):
            build_identity_from_exact_values(values)


def test_process_sample_event_contract_requires_reconstructable_success() -> None:
    for values in (
        {},
        {"user_cpu_nanoseconds": 1},
        {"user_cpu_nanoseconds": 1, "system_cpu_nanoseconds": 2},
        {
            "user_cpu_nanoseconds": 1,
            "system_cpu_nanoseconds": 2,
            "probe_available": 1,
        },
    ):
        with pytest.raises(ValidationError, match="process sample"):
            event(kind="process_sample", status="success", exact_values=values)


def test_process_sample_failure_has_no_fabricated_values() -> None:
    failed = event(
        kind="process_sample",
        status="failure",
        reason="process",
        exact_values={},
        evidence=blob_ref(evidence_payload()).model_dump(),
    )
    assert failed.exact_values == {}
    defaults = {
        "kind": "process_sample",
        "status": "failure",
        "reason": "process",
        "exact_values": {},
        "evidence": blob_ref(evidence_payload()).model_dump(),
    }
    for changes in ({"exact_values": {"user_cpu_nanoseconds": 0}}, {"evidence": None}):
        with pytest.raises(ValidationError, match="process sample"):
            event(**(defaults | changes))


def test_startup_event_requires_reconstructable_build_identity() -> None:
    for values in (
        {},
        {"build_version_code": 1000},
        {"build_version_code": 1000, "build_identity_status_code": 0},
        {
            "build_version_code": 1000,
            "build_identity_status_code": 0,
            "build_dirty": 1,
            "build_commit_timestamp_seconds": 1,
        },
    ):
        with pytest.raises(ValidationError, match="build identity"):
            event(kind="startup", exact_values=values)


def test_process_sample_is_exactly_once_immediately_before_terminal(
    tmp_path: Path,
) -> None:
    root = tmp_path / "valid"
    started = event(kind="run_started")
    stage = event(kind="stage_terminal")
    sampled = event(kind="process_sample")
    terminal = event(kind="run_terminal")
    for value in (started, stage, sampled, terminal):
        append_operation(root, value)
    assert load_operation_cut(root).events == (started, stage, sampled, terminal)

    later_sample = event(kind="process_sample", occurred_at="2026-10-12T12:00:01Z")
    invalid_sequences = (
        (started, terminal),
        (started, sampled, later_sample),
        (started, sampled, stage),
        (started, sampled, event(), terminal),
        (started, sampled, terminal, later_sample),
        (started, event(kind="process_sample", run_id="b" * 64)),
    )
    for index, sequence in enumerate(invalid_sequences):
        invalid_root = tmp_path / f"invalid-{index}"
        with pytest.raises(OperationCorruptError, match="transition"):
            for value in sequence:
                append_operation(invalid_root, value)


def _pid_is_running(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()[2]
    except FileNotFoundError:
        return False
    return state != "Z"


def _wait_until_process_stops(pid: int) -> None:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return
        time.sleep(0.01)
    pytest.fail(f"process {pid} survived bounded command cleanup")


def test_bounded_runner_returns_structured_status_and_bounded_bytes() -> None:
    from jev_trading_lab.polymarket import operations

    result = operations._run_bounded(
        (sys.executable, "-c", "import sys; sys.stdout.buffer.write(b' ok ')"),
        timeout=1.0,
    )
    changed = operations._run_bounded(
        (sys.executable, "-c", "import sys; sys.exit(1)"), timeout=1.0
    )

    assert (result.status, result.output) == (0, b" ok ")
    assert (changed.status, changed.output) == (1, b"")


def test_bounded_runner_rejects_oversize_output_without_buffering_unbounded() -> None:
    from jev_trading_lab.polymarket import operations

    with pytest.raises(ValueError, match="bound"):
        operations._run_bounded(
            (sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 5000)"),
            timeout=1.0,
        )


def test_bounded_runner_closes_descriptors_without_leaking() -> None:
    from jev_trading_lab.polymarket import operations

    before = len(tuple(Path("/proc/self/fd").iterdir()))
    for _ in range(20):
        operations._run_bounded((sys.executable, "-c", "pass"), timeout=1.0)
    after = len(tuple(Path("/proc/self/fd").iterdir()))

    assert after <= before


@pytest.mark.parametrize("failure_point", ("setup", "selector", "read", "wait"))
def test_bounded_runner_preserves_base_exception_while_cleaning_up(
    monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    from jev_trading_lab.polymarket import operations

    class Pipe:
        closed = False

        def fileno(self) -> int:
            return 9

        def close(self) -> None:
            self.closed = True

    class Process:
        pid = 12345
        stdout = Pipe()
        wait_calls = 0

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls += 1
            if failure_point == "wait" and self.wait_calls == 1:
                raise KeyboardInterrupt("original")
            return 0

    class Selector:
        closed = False
        registered = True

        def register(self, fileobj: object, events: int) -> None:
            pass

        def get_map(self) -> dict[int, int]:
            return {9: 1} if self.registered else {}

        def select(self, timeout: float | None = None) -> list[tuple[object, object]]:
            return [(object(), object())]

        def unregister(self, fileobj: object) -> None:
            self.registered = False

        def close(self) -> None:
            self.closed = True

    process = Process()
    selector = Selector()
    killed: list[tuple[int, int]] = []

    monkeypatch.setattr(operations.subprocess, "Popen", lambda *args, **kwargs: process)
    if failure_point == "setup":
        process.stdout = None  # type: ignore[assignment]
    if failure_point == "selector":
        monkeypatch.setattr(
            operations.selectors,
            "DefaultSelector",
            lambda: (_ for _ in ()).throw(KeyboardInterrupt("original")),
        )
    else:
        monkeypatch.setattr(operations.selectors, "DefaultSelector", lambda: selector)
    monkeypatch.setattr(
        operations.os,
        "read",
        lambda fd, size: (
            (_ for _ in ()).throw(KeyboardInterrupt("original"))
            if failure_point == "read"
            else b""
        ),
    )
    monkeypatch.setattr(
        operations.os, "killpg", lambda pid, sig: killed.append((pid, sig))
    )

    expected_exception = (
        AssertionError if failure_point == "setup" else KeyboardInterrupt
    )
    with pytest.raises(
        expected_exception, match=None if failure_point == "setup" else "original"
    ):
        operations._run_bounded(("trusted",), timeout=1.0)

    assert killed == [(process.pid, signal.SIGKILL)]
    expected_wait_calls = 1 if failure_point != "wait" else 2
    assert process.wait_calls == expected_wait_calls
    assert failure_point == "setup" or process.stdout.closed
    assert failure_point in {"setup", "selector"} or selector.closed


def test_git_helpers_accept_expected_statuses_and_reject_malformed_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jev_trading_lab.polymarket import operations

    results = iter(
        (
            operations._CommandResult(0, b""),
            operations._CommandResult(1, b""),
            operations._CommandResult(2, b""),
            operations._CommandResult(0, b"unexpected"),
        )
    )
    monkeypatch.setattr(
        operations, "_run_bounded", lambda command, timeout: next(results)
    )

    assert not operations._git_changed(("diff", "--quiet", "--"))
    assert operations._git_changed(("diff", "--quiet", "--"))
    with pytest.raises(ValueError, match="invalid status"):
        operations._git_changed(("diff", "--quiet", "--"))
    with pytest.raises(ValueError, match="unexpected output"):
        operations._git_changed(("diff", "--quiet", "--"))


def test_git_output_helper_rejects_nonzero_and_malformed_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jev_trading_lab.polymarket import operations

    results = iter(
        (
            operations._CommandResult(7, b""),
            operations._CommandResult(0, b"\xff"),
        )
    )
    monkeypatch.setattr(
        operations, "_run_bounded", lambda command, timeout: next(results)
    )

    with pytest.raises(ValueError, match="nonzero"):
        operations._run_git(("rev-parse", "HEAD"))
    with pytest.raises(UnicodeError, match="UTF-8"):
        operations._run_git(("rev-parse", "HEAD"))


def test_bounded_runner_times_out_kills_process_group_and_reaps(
    tmp_path: Path,
) -> None:
    from jev_trading_lab.polymarket import operations

    pid_path = tmp_path / "pid"
    source = (
        "import os,time,pathlib; "
        f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        operations._run_bounded((sys.executable, "-c", source), timeout=0.2)
    pid = int(pid_path.read_text(encoding="ascii"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_bounded_runner_handles_child_already_exited_during_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jev_trading_lab.polymarket import operations

    real_killpg = operations.os.killpg

    def kill_after_probe(pid: int, sig: int) -> None:
        real_killpg(pid, sig)
        raise ProcessLookupError

    monkeypatch.setattr(operations.os, "killpg", kill_after_probe)

    with pytest.raises(subprocess.TimeoutExpired):
        operations._run_bounded(
            (sys.executable, "-c", "import time; time.sleep(30)"), timeout=0.1
        )


def test_bounded_runner_success_kills_same_group_descendant_without_pipe(
    tmp_path: Path,
) -> None:
    from jev_trading_lab.polymarket import operations

    pid_path = tmp_path / "detached-pipe-descendant-pid"
    descendant = (
        "import os,time,pathlib; "
        f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
        "os.close(1); "
        "time.sleep(30)"
    )
    parent = (
        "import pathlib,subprocess,sys,time; "
        f"path=subprocess.Popen((sys.executable, '-c', {descendant!r})); "
        f"marker=pathlib.Path({str(pid_path)!r}); "
        "[(time.sleep(0.01)) for _ in range(100) if not marker.exists()]; "
        "sys.exit(0)"
    )

    try:
        result = operations._run_bounded((sys.executable, "-c", parent), timeout=2.0)
        assert (result.status, result.output) == (0, b"")
        pid = int(pid_path.read_text(encoding="ascii"))
        _wait_until_process_stops(pid)
    finally:
        if pid_path.exists():
            pid = int(pid_path.read_text(encoding="ascii"))
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_bounded_runner_boundary_excludes_descendants_that_call_setsid(
    tmp_path: Path,
) -> None:
    from jev_trading_lab.polymarket import operations

    pid_path = tmp_path / "escaped-descendant-pid"
    descendant = (
        "import os,time,pathlib; "
        f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
        "os.close(1); "
        "time.sleep(30)"
    )
    parent = (
        "import pathlib,subprocess,sys,time; "
        f"subprocess.Popen((sys.executable, '-c', {descendant!r}), "
        "start_new_session=True); "
        f"marker=pathlib.Path({str(pid_path)!r}); "
        "[(time.sleep(0.01)) for _ in range(100) if not marker.exists()]; "
        "sys.exit(0)"
    )

    try:
        result = operations._run_bounded((sys.executable, "-c", parent), timeout=2.0)
        assert (result.status, result.output) == (0, b"")
        pid = int(pid_path.read_text(encoding="ascii"))
        assert _pid_is_running(pid)
    finally:
        if pid_path.exists():
            pid = int(pid_path.read_text(encoding="ascii"))
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_bounded_runner_kills_same_group_descendant_after_parent_exits(
    tmp_path: Path,
) -> None:
    from jev_trading_lab.polymarket import operations

    pid_path = tmp_path / "descendant-pid"
    descendant = (
        "import os,time,pathlib; "
        f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    parent = (
        "import subprocess,sys; "
        f"subprocess.Popen((sys.executable, '-c', {descendant!r})); "
        "sys.exit(0)"
    )

    try:
        with pytest.raises(subprocess.TimeoutExpired):
            operations._run_bounded((sys.executable, "-c", parent), timeout=0.2)
        pid = int(pid_path.read_text(encoding="ascii"))
        _wait_until_process_stops(pid)
    finally:
        if pid_path.exists():
            pid = int(pid_path.read_text(encoding="ascii"))
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


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

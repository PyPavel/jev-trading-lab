from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from jev_trading_lab.polymarket import store
from jev_trading_lab.polymarket.call_journal import ProviderCallJournal
from jev_trading_lab.polymarket.operations import (
    JournalRecovery,
    OperationCorruptError,
    append_operation,
    load_operation_cut,
    recover_operation_tail,
    recovery_event,
)
from jev_trading_lab.polymarket.store import StoreCorruptError, recover_store_tails


def _tail(output: Path, name: str = "results.jsonl", tail: bytes = b"torn") -> Path:
    output.mkdir(mode=0o700)
    path = output / name
    path.write_bytes(tail)
    path.chmod(0o600)
    return path


def _recovery_events(output: Path) -> list[Any]:
    return [
        event
        for event in load_operation_cut(output).events
        if event.kind.value == "recovery"
    ]


def _marker(output: Path, kind: str) -> Path:
    return output / f".recovery-{kind}.json"


def test_same_length_replacement_is_not_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    path = _tail(output, tail=b"torn")
    original_recover = store._recover_store_journal

    def replace_then_recover(
        root_fd: int, opened: Any, expected: Any, selected_output: Path
    ) -> JournalRecovery:
        replacement = output / "replacement"
        replacement.write_bytes(b"evil")
        replacement.chmod(0o600)
        replacement.replace(path)
        return original_recover(root_fd, opened, expected, selected_output)

    monkeypatch.setattr(store, "_recover_store_journal", replace_then_recover)
    with pytest.raises((StoreCorruptError, ValueError), match="changed|identity"):
        recover_store_tails(output)

    assert path.read_bytes() == b"evil"
    assert not _marker(output, "results").exists()


def test_same_inode_same_length_different_bytes_is_not_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    path = _tail(output, tail=b"torn")
    original_recover = store._recover_store_journal

    def rewrite_then_recover(
        root_fd: int, opened: Any, expected: Any, selected_output: Path
    ) -> JournalRecovery:
        descriptor = opened["results"][0]
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, b"evil")
        return original_recover(root_fd, opened, expected, selected_output)

    monkeypatch.setattr(store, "_recover_store_journal", rewrite_then_recover)
    with pytest.raises((StoreCorruptError, ValueError), match="changed|identity"):
        recover_store_tails(output)

    assert path.read_bytes() == b"evil"
    assert not _marker(output, "results").exists()


def test_audit_failure_before_write_is_reconciled_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    path = _tail(output)
    real_append = store.append_operation

    def fail_before_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("audit append failed")

    monkeypatch.setattr(store, "append_operation", fail_before_write)
    with pytest.raises(OSError, match="audit append failed"):
        recover_store_tails(output)

    assert path.read_bytes() == b""
    assert _marker(output, "results").is_file()
    monkeypatch.setattr(store, "append_operation", real_append)
    assert recover_store_tails(output) == (JournalRecovery("results", 4),)
    events = _recovery_events(output)
    assert len(events) == 1
    assert events[0].journal_kind == "results"
    assert not _marker(output, "results").exists()


def test_audit_failure_after_visible_write_is_reconciled_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    _tail(output)
    real_append = store.append_operation

    def fail_after_write(*args: object, **kwargs: object) -> None:
        real_append(*args, **kwargs)
        raise OSError("audit fsync outcome ambiguous")

    monkeypatch.setattr(store, "append_operation", fail_after_write)
    with pytest.raises(OSError, match="ambiguous"):
        recover_store_tails(output)

    assert _marker(output, "results").is_file()
    monkeypatch.setattr(store, "append_operation", real_append)
    assert recover_store_tails(output) == (JournalRecovery("results", 4),)
    assert len(_recovery_events(output)) == 1
    assert not _marker(output, "results").exists()


@pytest.mark.parametrize(
    "boundary",
    (
        "marker_create",
        "truncate",
        "journal_fsync",
        "parent_fsync",
        "audit_append",
        "audit_fsync",
        "marker_unlink",
    ),
)
def test_crash_boundaries_reconcile_to_one_durable_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    output = tmp_path / "output"
    _tail(output)
    crashed = False

    def crash(name: str) -> None:
        nonlocal crashed
        if not crashed and name == boundary:
            crashed = True
            raise OSError(f"crash after {boundary}")

    monkeypatch.setattr(store, "_recovery_failpoint", crash, raising=False)
    with pytest.raises(OSError, match="crash after"):
        recover_store_tails(output)

    monkeypatch.setattr(store, "_recovery_failpoint", lambda _name: None, raising=False)
    recover_store_tails(output)
    assert len(_recovery_events(output)) == 1
    assert not _marker(output, "results").exists()
    assert (output / "results.jsonl").read_bytes() == b""


@pytest.mark.parametrize("journal", ("operations.jsonl", "provider_calls.jsonl"))
def test_operations_and_provider_tails_self_audit_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journal: str,
) -> None:
    output = tmp_path / "output"
    path = _tail(output, journal)
    failed = False
    real_fsync = os.fsync

    def fail_target_once(descriptor: int) -> None:
        nonlocal failed
        target = Path(f"/proc/self/fd/{descriptor}").resolve()
        if not failed and target == path:
            failed = True
            raise OSError("journal fsync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(store.os, "fsync", fail_target_once)
    with pytest.raises(OSError, match="journal fsync failed"):
        recover_store_tails(output)
    monkeypatch.setattr(store.os, "fsync", real_fsync)

    recovered = recover_store_tails(output)
    expected_kind = (
        "operations" if journal.startswith("operations") else "provider_calls"
    )
    assert JournalRecovery(expected_kind, 4) in recovered
    events = _recovery_events(output)
    assert len([event for event in events if event.journal_kind == expected_kind]) == 1
    assert (
        path.read_bytes().endswith(b"\n")
        if expected_kind == "operations"
        else path.read_bytes() == b""
    )
    assert not _marker(output, expected_kind).exists()


def _leave_marker(
    output: Path, monkeypatch: pytest.MonkeyPatch, *, kind: str = "results"
) -> tuple[Path, Path]:
    target = _tail(
        output, f"{kind}.jsonl" if kind != "operations" else "operations.jsonl"
    )

    def crash(name: str) -> None:
        if name == "marker_create":
            raise OSError("stop after marker")

    monkeypatch.setattr(store, "_recovery_failpoint", crash, raising=False)
    with pytest.raises(OSError, match="stop after marker"):
        recover_store_tails(output)
    marker = _marker(output, kind)
    assert marker.is_file()
    monkeypatch.setattr(store, "_recovery_failpoint", lambda _name: None, raising=False)
    return target, marker


@pytest.mark.parametrize("mutation", ("corrupt", "truncated", "mode", "hardlink"))
def test_unsafe_or_corrupt_marker_fails_closed_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    output = tmp_path / "output"
    target, marker = _leave_marker(output, monkeypatch)
    original = target.read_bytes()
    if mutation == "corrupt":
        marker.write_bytes(b"{}")
    elif mutation == "truncated":
        marker.write_bytes(marker.read_bytes()[:-1])
    elif mutation == "mode":
        marker.chmod(0o644)
    else:
        os.link(marker, output / "extra-link")

    with pytest.raises((StoreCorruptError, ValueError), match="recovery|marker|intent"):
        recover_store_tails(output)
    assert target.read_bytes() == original


@pytest.mark.parametrize("special", ("symlink", "fifo"))
def test_special_marker_fails_closed_without_mutation(
    tmp_path: Path, special: str
) -> None:
    output = tmp_path / "output"
    target = _tail(output)
    marker = _marker(output, "results")
    if special == "symlink":
        marker.symlink_to(target.name)
    else:
        os.mkfifo(marker, 0o600)

    with pytest.raises((StoreCorruptError, ValueError), match="recovery|marker|intent"):
        recover_store_tails(output)
    assert target.read_bytes() == b"torn"


def test_marker_identity_conflict_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    target, _marker_path = _leave_marker(output, monkeypatch)
    target.write_bytes(b"evil")
    target.chmod(0o600)

    with pytest.raises(
        (StoreCorruptError, ValueError), match="changed|identity|intent"
    ):
        recover_store_tails(output)
    assert target.read_bytes() == b"evil"


@pytest.mark.parametrize("raw", (b"{}\n", b"{}\ntorn"))
def test_complete_or_middle_corruption_never_mutates_or_creates_marker(
    tmp_path: Path, raw: bytes
) -> None:
    output = tmp_path / "output"
    path = _tail(output, tail=raw)

    with pytest.raises((StoreCorruptError, ValueError)):
        recover_store_tails(output)
    assert path.read_bytes() == raw
    assert not tuple(output.glob(".recovery-*.json"))
    assert not (output / "operations.jsonl").exists()


def test_two_recoverers_converge_without_duplicate_audit(tmp_path: Path) -> None:
    output = tmp_path / "output"
    _tail(output)
    barrier = threading.Barrier(2)
    results: list[tuple[JournalRecovery, ...]] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            barrier.wait()
            results.append(recover_store_tails(output))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert len(_recovery_events(output)) == 1
    assert not _marker(output, "results").exists()


def test_direct_operations_recovery_is_transactional_and_self_auditing(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    _tail(output, "operations.jsonl")

    assert recover_operation_tail(output) == JournalRecovery("operations", 4)
    events = _recovery_events(output)
    assert len(events) == 1
    assert events[0].journal_kind == "operations"
    assert recover_operation_tail(output) is None


def test_direct_provider_recovery_is_transactional_and_self_auditing(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    _tail(output, "provider_calls.jsonl")

    assert ProviderCallJournal(output).recover_tail() == JournalRecovery(
        "provider_calls", 4
    )
    events = _recovery_events(output)
    assert len(events) == 1
    assert events[0].journal_kind == "provider_calls"
    assert ProviderCallJournal(output).recover_tail() is None


def test_recovery_refuses_corrupt_operations_journal_before_store_mutation(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    target = _tail(output)
    operations = output / "operations.jsonl"
    operations.write_bytes(b"{}\n")
    operations.chmod(0o600)

    with pytest.raises((StoreCorruptError, OperationCorruptError, ValueError)):
        recover_store_tails(output)
    assert target.read_bytes() == b"torn"
    assert not _marker(output, "results").exists()


def test_writer_after_completed_recovery_converges_without_duplicate_audit(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    _tail(output)
    assert recover_store_tails(output) == (JournalRecovery("results", 4),)

    append_operation(output, recovery_event(JournalRecovery("artifacts", 5)))

    events = _recovery_events(output)
    assert len(events) == 2
    assert len({event.event_id for event in events}) == 2
    assert recover_store_tails(output) == ()


@pytest.mark.parametrize(
    ("journal", "kind"),
    (
        ("operations.jsonl", "operations"),
        ("results.jsonl", "results"),
        ("artifacts.jsonl", "artifacts"),
        ("provider_calls.jsonl", "provider_calls"),
        ("completed_slots.jsonl", "completed_slots"),
    ),
)
def test_active_writer_cannot_interleave_with_exclusive_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journal: str,
    kind: str,
) -> None:
    output = tmp_path / "output"
    _tail(output, journal)
    truncated = threading.Event()
    release = threading.Event()
    writer_done = threading.Event()
    errors: list[BaseException] = []

    def pause(name: str) -> None:
        if name == "truncate":
            truncated.set()
            assert release.wait(5)

    monkeypatch.setattr(store, "_recovery_failpoint", pause)
    monkeypatch.setattr(
        ProviderCallJournal,
        "_publish",
        lambda _self, payload: (
            "a" * 64,
            len(payload),
            "provider-call-blobs/aa/aa/" + "a" * 64,
        ),
    )

    def recover() -> None:
        try:
            recover_store_tails(output)
        except BaseException as error:
            errors.append(error)

    def write() -> None:
        try:
            if kind == "operations":
                append_operation(
                    output, recovery_event(JournalRecovery("artifacts", 1))
                )
            elif kind == "provider_calls":
                ProviderCallJournal(output).begin(
                    provider="openai",
                    opportunity_id="1" * 64,
                    request_bytes=b"request",
                    created_at=datetime(2026, 10, 12, tzinfo=UTC),
                )
            else:
                root_fd = store._open_output(output)
                try:
                    descriptor = store._open_journal(root_fd, journal)
                    try:
                        store._lock(descriptor, store.fcntl.LOCK_EX)
                        store._append_raw(descriptor, b"{}\n")
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                finally:
                    os.close(root_fd)
            writer_done.set()
        except BaseException as error:
            errors.append(error)

    recovery_thread = threading.Thread(target=recover)
    recovery_thread.start()
    assert truncated.wait(5)
    writer_thread = threading.Thread(target=write)
    writer_thread.start()
    assert not writer_done.wait(0.1)
    release.set()
    recovery_thread.join(5)
    writer_thread.join(5)

    assert not errors
    assert writer_done.is_set()
    assert not recovery_thread.is_alive() and not writer_thread.is_alive()


@pytest.mark.parametrize("replacement", ("regular", "symlink"))
def test_marker_path_replacement_before_unlink_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    output = tmp_path / "output"
    target = _tail(output)
    marker = _marker(output, "results")
    foreign = output / "foreign"
    foreign.write_bytes(b"foreign")
    foreign.chmod(0o600)

    def replace(name: str) -> None:
        if name != "audit_fsync":
            return
        marker.unlink()
        if replacement == "regular":
            marker.write_bytes(b"replacement")
            marker.chmod(0o600)
        else:
            marker.symlink_to(foreign.name)

    monkeypatch.setattr(store, "_recovery_failpoint", replace)
    with pytest.raises((StoreCorruptError, OSError), match="recovery intent"):
        recover_store_tails(output)

    assert target.read_bytes() == b""
    assert os.path.lexists(marker)
    assert foreign.read_bytes() == b"foreign"


def test_marker_replacement_in_final_quarantine_window_is_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    target = _tail(output)
    marker = _marker(output, "results")

    def replace(name: str) -> None:
        if name == "marker_quarantine":
            marker.write_bytes(b"replacement")
            marker.chmod(0o600)

    monkeypatch.setattr(store, "_recovery_failpoint", replace)
    with pytest.raises(StoreCorruptError, match="pathname was replaced"):
        recover_store_tails(output)

    assert target.read_bytes() == b""
    assert marker.read_bytes() == b"replacement"

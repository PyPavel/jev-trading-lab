from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from jev_trading_lab.polymarket.ai import AdapterFailure
from jev_trading_lab.polymarket.call_journal import (
    ProviderCallJournal,
    adapter_from_outcome,
    adapter_outcome,
)
from jev_trading_lab.polymarket.client import (
    HttpRequest,
    HttpResponse,
    PolymarketClient,
    PublicDataError,
    gamma_eligibility_reason,
    read_bounded_body,
    resolve_public_addresses,
)
from jev_trading_lab.polymarket.operations import JournalRecovery
from jev_trading_lab.polymarket.pipeline import observation_slot
from jev_trading_lab.polymarket.store import recover_store_tails

NOW = datetime(2026, 10, 10, 12, 0, 45, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _local_journal_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    from jev_trading_lab.polymarket import call_journal

    defaults = dict(call_journal.BlobStore.__init__.__kwdefaults__ or {})
    defaults["filesystem_resolver"] = lambda _path: "ext4"
    monkeypatch.setattr(call_journal.BlobStore.__init__, "__kwdefaults__", defaults)


def _market(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "m1",
        "conditionId": "c1",
        "question": "Will the measure pass?",
        "description": "A public-policy measure.",
        "rules": "Resolves Yes if enacted.",
        "resolutionSource": "Official legislature record",
        "category": "Politics",
        "tags": "[]",
        "active": True,
        "closed": False,
        "endDate": "2026-10-11T12:00:00Z",
        "volume24hr": "10000",
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.49","0.51"]',
        "clobTokenIds": '["yes","no"]',
    }
    value.update(changes)
    return value


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"active": False}, "INACTIVE"),
        ({"closed": True}, "CLOSED"),
        ({"category": "Sports"}, "SPORTS"),
        ({"outcomes": '["A","B"]'}, "NON_BINARY"),
        ({"rules": ""}, "MISSING_EVIDENCE"),
        ({"endDate": "2026-10-10T12:30:00Z"}, "CLOSE_WINDOW"),
        ({"volume24hr": "9999.999"}, "LOW_VOLUME"),
        ({}, None),
    ],
)
def test_gamma_eligibility_has_one_normalized_reason_function(
    changes: dict[str, object], reason: str | None
) -> None:
    assert gamma_eligibility_reason(_market(**changes), cutoff=NOW) == reason


def test_gamma_eligibility_rejects_tampered_types() -> None:
    with pytest.raises(ValueError, match="Gamma eligibility input"):
        gamma_eligibility_reason(_market(active=1), cutoff=NOW)


def test_observation_slot_is_minute_stable_and_adjacent_minutes_are_distinct() -> None:
    first = observation_slot(NOW)
    same = observation_slot(NOW + timedelta(seconds=14))
    adjacent = observation_slot(NOW + timedelta(seconds=15))
    assert first == same == datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    assert adjacent == datetime(2026, 10, 10, 12, 1, tzinfo=UTC)


def test_precheck_freshness_uses_http_retrieved_at_inclusive() -> None:
    market = _market()
    gamma = json.dumps([market], separators=(",", ":")).encode()
    book_time = NOW - timedelta(seconds=30)
    book = json.dumps(
        {
            "market": "c1",
            "asset_id": "yes",
            "timestamp": str(int(book_time.timestamp() * 1000)),
            "bids": [{"price": "0.49", "size": "5000"}],
            "asks": [{"price": "0.51", "size": "5000"}],
        },
        separators=(",", ":"),
    ).encode()

    class Transport:
        def __call__(self, request: HttpRequest) -> HttpResponse:
            if "gamma-api" in request.url:
                return HttpResponse(
                    200, {"content-type": "application/json"}, gamma, NOW
                )
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                book,
                NOW,
            )

    result = PolymarketClient(Transport(), page_size=20, max_pages=1).poll(
        now=NOW - timedelta(minutes=10)
    )
    assert len(result.eligible) == 1
    assert result.eligible[0].precheck_attempt.retrieved_at == NOW


def _book(at: datetime) -> bytes:
    return json.dumps(
        {
            "market": "c1",
            "asset_id": "yes",
            "timestamp": str(int(at.timestamp() * 1000)),
            "bids": [{"price": "0.49", "size": "5000"}],
            "asks": [{"price": "0.51", "size": "5000"}],
        },
        separators=(",", ":"),
    ).encode()


def _candidate_with_execution_response(response: HttpResponse):
    gamma = json.dumps([_market()], separators=(",", ":")).encode()
    replies = iter(
        [
            HttpResponse(200, {"content-type": "application/json"}, gamma, NOW),
            HttpResponse(
                200,
                {"content-type": "application/json"},
                _book(NOW),
                NOW,
            ),
            response,
        ]
    )

    class Transport:
        def __call__(self, _request: HttpRequest) -> HttpResponse:
            return next(replies)

    client = PolymarketClient(Transport(), page_size=20, max_pages=1)
    candidate = client.poll(now=NOW).eligible[0]
    return client, candidate


def test_execution_freshness_counts_request_delay_and_existing_age() -> None:
    client, candidate = _candidate_with_execution_response(
        HttpResponse(
            200,
            {"content-type": "application/json"},
            _book(NOW - timedelta(seconds=20)),
            NOW + timedelta(seconds=20),
        )
    )

    with pytest.raises(PublicDataError, match="STALE_BOOK"):
        client.execution_book(candidate, now=NOW)


def test_execution_freshness_accepts_exactly_thirty_seconds() -> None:
    retrieved_at = NOW + timedelta(seconds=20)
    client, candidate = _candidate_with_execution_response(
        HttpResponse(
            200,
            {"content-type": "application/json"},
            _book(NOW - timedelta(seconds=10)),
            retrieved_at,
        )
    )

    _, snapshot = client.execution_book(candidate, now=NOW)

    assert snapshot.response_retrieved_at == retrieved_at


def test_execution_freshness_rejects_future_book_timestamp() -> None:
    client, candidate = _candidate_with_execution_response(
        HttpResponse(
            200,
            {"content-type": "application/json"},
            _book(NOW + timedelta(seconds=1)),
            NOW,
        )
    )

    with pytest.raises(PublicDataError, match="STALE_BOOK"):
        client.execution_book(candidate, now=NOW)


def test_provider_call_intent_is_durable_and_unknown_is_never_retryable(
    tmp_path: Path,
) -> None:
    root = tmp_path
    journal = ProviderCallJournal(root)
    call_id = journal.begin(
        provider="openai",
        opportunity_id="1" * 64,
        request_bytes=b'{"model":"gpt-5"}',
        created_at=NOW,
    )
    restarted = ProviderCallJournal(root)
    state = restarted.state(call_id)
    assert state is not None
    assert state["kind"] == "provider_call_intent"
    assert state["retryable"] is False
    assert (root / "provider_calls.jsonl").read_bytes().endswith(b"\n")


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "fifo", "weak_mode"])
def test_provider_journal_rejects_unsafe_file_before_read_or_write(
    tmp_path: Path, attack: str
) -> None:
    root = tmp_path / "out"
    root.mkdir(mode=0o700)
    journal_path = root / "provider_calls.jsonl"
    target = tmp_path / "target"
    if attack == "symlink":
        target.write_bytes(b"sentinel")
        journal_path.symlink_to(target)
    elif attack == "hardlink":
        target.write_bytes(b"sentinel")
        os.chmod(target, 0o600)
        os.link(target, journal_path)
    elif attack == "fifo":
        os.mkfifo(journal_path, 0o600)
    else:
        journal_path.write_bytes(b"")
        os.chmod(journal_path, 0o644)

    with pytest.raises(ValueError, match="provider call journal"):
        ProviderCallJournal(root).begin(
            provider="openai",
            opportunity_id="1" * 64,
            request_bytes=b"request",
            created_at=NOW,
        )

    if target.exists():
        assert target.read_bytes() == b"sentinel"


def test_provider_journal_rejects_unsafe_blob_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "out"
    root.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    (root / "provider-call-blobs").symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="provider call"):
        ProviderCallJournal(root).begin(
            provider="openai",
            opportunity_id="1" * 64,
            request_bytes=b"request",
            created_at=NOW,
        )

    assert list(target.iterdir()) == []


def test_provider_journal_read_uses_shared_journal_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jev_trading_lab.polymarket import call_journal

    journal = ProviderCallJournal(tmp_path)
    operations: list[int] = []
    real_flock = call_journal.fcntl.flock

    def record_flock(descriptor: int, operation: int) -> None:
        operations.append(operation)
        real_flock(descriptor, operation)

    monkeypatch.setattr(call_journal.fcntl, "flock", record_flock)

    assert journal.state("0" * 64) is None
    assert operations == [call_journal.fcntl.LOCK_SH, call_journal.fcntl.LOCK_SH]


def test_provider_journal_completes_short_interrupted_writes(tmp_path: Path) -> None:
    journal = ProviderCallJournal(tmp_path)
    real_write = os.write
    interrupted = False

    def short_write(descriptor: int, payload: bytes | memoryview) -> int:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise InterruptedError
        return real_write(descriptor, payload[: max(1, len(payload) // 2)])

    with patch("jev_trading_lab.polymarket.call_journal.os.write", short_write):
        call_id = journal.begin(
            provider="openai",
            opportunity_id="1" * 64,
            request_bytes=b"request",
            created_at=NOW,
        )

    assert journal.state(call_id) is not None


def test_provider_journal_tail_recovery_is_explicit_and_reports_exact_bytes(
    tmp_path: Path,
) -> None:
    journal = ProviderCallJournal(tmp_path)
    call_id = journal.begin(
        provider="openai",
        opportunity_id="1" * 64,
        request_bytes=b"request",
        created_at=NOW,
    )
    path = tmp_path / "provider_calls.jsonl"
    durable_prefix = path.read_bytes()
    tail = b'{"artifact_sha256":"torn'
    original = durable_prefix + tail
    path.write_bytes(original)

    with pytest.raises(ValueError, match="truncated tail"):
        ProviderCallJournal(tmp_path).state(call_id)
    assert path.read_bytes() == original

    recovered = ProviderCallJournal(tmp_path).recover_tail()

    assert recovered == JournalRecovery("provider_calls", len(tail))
    state = ProviderCallJournal(tmp_path).state(call_id)
    assert state is not None and state["kind"] == "provider_call_intent"
    assert path.read_bytes() == durable_prefix
    assert ProviderCallJournal(tmp_path).recover_tail() is None


def test_store_recovery_audits_provider_tail(tmp_path: Path) -> None:
    journal = ProviderCallJournal(tmp_path)
    journal.begin(
        provider="openai",
        opportunity_id="1" * 64,
        request_bytes=b"request",
        created_at=NOW,
    )
    path = tmp_path / "provider_calls.jsonl"
    durable_prefix = path.read_bytes()
    tail = b"torn"
    path.write_bytes(durable_prefix + tail)

    assert recover_store_tails(tmp_path) == (
        JournalRecovery("provider_calls", len(tail)),
    )

    assert path.read_bytes() == durable_prefix
    assert recover_store_tails(tmp_path) == ()
    from jev_trading_lab.polymarket.operations import load_operation_cut

    events = load_operation_cut(tmp_path).events
    assert len(events) == 1
    assert events[0].kind.value == "recovery"
    assert events[0].journal_kind == "provider_calls"
    assert events[0].reason == "partial_tail"
    assert events[0].exact_values == {"discarded_bytes": len(tail)}


def test_provider_recovery_rejects_invalid_transition_without_mutation(
    tmp_path: Path,
) -> None:
    journal = ProviderCallJournal(tmp_path)
    journal.begin(
        provider="openai",
        opportunity_id="1" * 64,
        request_bytes=b"request",
        created_at=NOW,
    )
    path = tmp_path / "provider_calls.jsonl"
    first = path.read_bytes().splitlines()[0]
    original = first + b"\n" + first + b"\npartial"
    path.write_bytes(original)

    with pytest.raises(ValueError, match="transition"):
        journal.recover_tail()

    assert path.read_bytes() == original


def test_provider_recovery_truncate_failure_preserves_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = ProviderCallJournal(tmp_path)
    journal.begin(
        provider="openai",
        opportunity_id="1" * 64,
        request_bytes=b"request",
        created_at=NOW,
    )
    path = tmp_path / "provider_calls.jsonl"
    original = path.read_bytes() + b"partial"
    path.write_bytes(original)

    def fail(_descriptor: int, _length: int) -> None:
        raise OSError("truncate failed")

    monkeypatch.setattr(os, "ftruncate", fail)
    with pytest.raises(OSError, match="truncate failed"):
        journal.recover_tail()
    assert path.read_bytes() == original


def test_provider_empty_unnterminated_journal_is_recovered(tmp_path: Path) -> None:
    path = tmp_path / "provider_calls.jsonl"
    path.write_bytes(b"partial")
    path.chmod(0o600)

    assert ProviderCallJournal(tmp_path).recover_tail() == JournalRecovery(
        "provider_calls", 7
    )
    assert path.read_bytes() == b""


def test_provider_complete_final_line_needs_no_recovery(tmp_path: Path) -> None:
    journal = ProviderCallJournal(tmp_path)
    journal.begin(
        provider="openai",
        opportunity_id="1" * 64,
        request_bytes=b"request",
        created_at=NOW,
    )
    before = (tmp_path / "provider_calls.jsonl").read_bytes()

    assert journal.recover_tail() is None
    assert (tmp_path / "provider_calls.jsonl").read_bytes() == before


def test_provider_journal_rejects_newline_terminated_or_earlier_corruption(
    tmp_path: Path,
) -> None:
    journal = ProviderCallJournal(tmp_path)
    journal.begin(
        provider="openai",
        opportunity_id="1" * 64,
        request_bytes=b"request",
        created_at=NOW,
    )
    path = tmp_path / "provider_calls.jsonl"
    durable_prefix = path.read_bytes()
    for corrupt in (durable_prefix + b"{}\n", b"{}\n" + durable_prefix + b"torn"):
        path.write_bytes(corrupt)
        with pytest.raises(ValueError, match="provider call journal"):
            ProviderCallJournal(tmp_path).validate("1" * 64)


def test_provider_journal_rejects_duplicate_keys_and_oversized_lines(
    tmp_path: Path,
) -> None:
    journal = ProviderCallJournal(tmp_path)
    path = tmp_path / "provider_calls.jsonl"
    path.write_bytes(b'{"call_id":"a","call_id":"b"}\n')
    os.chmod(path, 0o600)
    with pytest.raises(ValueError, match="provider call journal"):
        journal.validate("1" * 64)

    path.write_bytes(b"{" + b"x" * (1024 * 1024) + b"}\n")
    with pytest.raises(ValueError, match="provider call journal"):
        journal.validate("1" * 64)


def test_provider_journal_rejects_second_request_for_same_opportunity_provider(
    tmp_path: Path,
) -> None:
    journal = ProviderCallJournal(tmp_path)
    first = journal.begin(
        provider="openai",
        opportunity_id="1" * 64,
        request_bytes=b"first",
        created_at=NOW,
    )

    same = journal.begin(
        provider="openai",
        opportunity_id="1" * 64,
        request_bytes=b"second",
        created_at=NOW,
    )

    assert same == first
    state = journal.state(first)
    assert state is not None
    assert state["request_sha256"] == __import__("hashlib").sha256(b"first").hexdigest()


@pytest.mark.parametrize(
    "changes",
    [
        {"message": "not canonical"},
        {"billed_cost_microdollars": -1},
        {
            "status_code": 504,
            "response_sha256": "3" * 64,
            "response_byte_length": 1,
            "retrieved_at": "2026-10-10T12:00:45Z",
        },
    ],
)
def test_adapter_outcome_rejects_invalid_failure_semantics(
    changes: dict[str, object],
) -> None:
    outcome = adapter_outcome(
        AdapterFailure(
            provider="openai",
            code="TIMEOUT",
            message="openai transport timed out",
            request_bytes=b"request",
            response_sha256=None,
            response_byte_length=0,
            retrieved_at=None,
            billed_cost_microdollars=0,
            status_code=None,
        )
    )
    outcome.update(changes)

    with pytest.raises(ValueError, match="provider call result"):
        adapter_from_outcome("openai", outcome)


def test_adapter_outcome_rejects_negative_success_cost() -> None:
    from jev_trading_lab.polymarket.ai import OpenAISuccess
    from jev_trading_lab.polymarket.models import ResearchBrief

    fixture = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/polymarket/feasibility_cycle.json"
        ).read_text()
    )
    response = json.loads(fixture["model_artifacts"]["openai_response_bytes"])
    message = next(item for item in response["output"] if item["type"] == "message")
    brief = ResearchBrief.model_validate_json(message["content"][0]["text"])
    outcome = adapter_outcome(OpenAISuccess(brief, b"request", b"response", NOW, -1))

    with pytest.raises(ValueError, match="provider call result"):
        adapter_from_outcome("openai", outcome)


def test_provider_journal_rejects_append_past_entry_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from jev_trading_lab.polymarket import call_journal

    journal = ProviderCallJournal(tmp_path)
    call_id = journal.begin(
        provider="jev",
        opportunity_id="2" * 64,
        request_bytes=b"request",
        created_at=NOW,
    )
    outcome = adapter_outcome(
        AdapterFailure(
            provider="jev",
            code="TIMEOUT",
            message="jev transport timed out",
            request_bytes=b"request",
            response_sha256=None,
            response_byte_length=0,
            retrieved_at=None,
            billed_cost_microdollars=0,
            status_code=None,
        )
    )
    monkeypatch.setattr(call_journal, "_MAX_LINES", 1)

    with pytest.raises(ValueError, match="maximum entries"):
        journal.finish(call_id, outcome=outcome, finished_at=NOW)

    assert journal.state(call_id)["kind"] == "provider_call_intent"  # type: ignore[index]


def test_provider_call_result_is_content_addressed_and_replayable(
    tmp_path: Path,
) -> None:
    root = tmp_path
    journal = ProviderCallJournal(root)
    call_id = journal.begin(
        provider="jev",
        opportunity_id="2" * 64,
        request_bytes=b"request",
        created_at=NOW,
    )
    outcome = adapter_outcome(
        AdapterFailure(
            provider="jev",
            code="TIMEOUT",
            message="jev transport timed out",
            request_bytes=b"request",
            response_sha256=None,
            response_byte_length=0,
            retrieved_at=None,
            billed_cost_microdollars=0,
            status_code=None,
        )
    )
    journal.finish(
        call_id,
        outcome=outcome,
        finished_at=NOW + timedelta(seconds=1),
    )
    state = ProviderCallJournal(root).state(call_id)
    assert state is not None
    assert state["kind"] == "provider_call_result"
    assert state["outcome"] == outcome
    artifact = root / state["storage_relpath"]
    assert artifact.read_bytes() == state["artifact_bytes"]


def test_full_body_deadline_rejects_slow_drip() -> None:
    ticks = iter([0.0, 0.4, 0.8, 1.2])

    class Response:
        def read(self, _size: int) -> bytes:
            return b"x"

    with pytest.raises(TimeoutError, match="deadline"):
        read_bounded_body(
            Response(), max_bytes=100, deadline=1.0, clock=lambda: next(ticks)
        )


def test_dns_saturation_stays_bounded_and_each_wait_obeys_its_deadline() -> None:
    release = threading.Event()

    def blocked_resolver(*_args: object) -> list[tuple[object, ...]]:
        release.wait(timeout=5)
        return []

    started = time.monotonic()
    try:
        for _ in range(8):
            with pytest.raises(TimeoutError, match="DNS"):
                resolve_public_addresses(
                    blocked_resolver,
                    "example.test",
                    deadline=time.monotonic() + 0.01,
                )
        dns_threads = [
            thread
            for thread in threading.enumerate()
            if thread.name.startswith("bounded-dns")
        ]
        assert len(dns_threads) <= 4
        assert time.monotonic() - started < 1
    finally:
        release.set()

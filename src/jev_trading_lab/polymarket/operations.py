"""Strict append-only operational facts and content-addressed evidence."""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from pydantic import Field, StringConstraints, field_validator, model_validator

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.persistence.blobs import (
    BlobError,
    BlobRef,
    BlobSecurityError,
    BlobStore,
    _open_or_create_root,
    _write_all,
    is_credential_name,
)
from jev_trading_lab.polymarket.models import (
    INT64_MAX,
    Sha256,
    StrictModel,
    load_strict_json,
)

_MAX_LINE_BYTES = 1024 * 1024
_MAX_JOURNAL_BYTES = 64 * 1024 * 1024
_MAX_LINES = 10_000
_MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
_STAGE_VALUES = frozenset(
    {
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
    }
)
_RELIABILITY_REASONS = frozenset(
    {
        "partial_tail",
        "reused_result",
        "unknown_in_flight",
        "stale_temporary",
        "invalid_line",
        "invalid_transition",
        "conflict",
        "missing_blob",
        "digest_mismatch",
        "unsafe_path",
        "lock_timeout",
        "write",
        "flush",
        "fsync",
        "rename",
        "directory_fsync",
        "other",
    }
)
_STORAGE_REASONS = frozenset(
    {"lock_timeout", "write", "flush", "fsync", "rename", "directory_fsync", "other"}
)
_MARKET_REASONS = frozenset(
    {
        "timeout",
        "transport",
        "http_status",
        "redirect",
        "content_type",
        "oversized",
        "not_json",
        "malformed",
        "stale",
        "crossed",
        "token_drift",
        "market_drift",
        "duplicate",
        "other",
    }
)
_MODEL_REASONS = frozenset(
    {
        "input_error",
        "input_changed",
        "config_failure",
        "unknown_in_flight",
        "provider_error",
        "timeout",
        "http_error",
        "parse_error",
        "model_mismatch",
        "citation_error",
        "credential_reflection",
        "cost_overflow",
        "other",
    }
)
_PIPELINE_REASONS = frozenset(
    {
        "none",
        "inactive",
        "closed",
        "sports",
        "non_binary",
        "missing_evidence",
        "close_window",
        "low_volume",
        "wide_spread",
        "low_depth",
        "capacity",
        "risk",
        "no_edge",
        "abstain",
        "insufficient",
        "ambiguous",
        "control_unavailable",
        "book_failure",
        "model_failure",
        "public_data_failure",
        "timeout",
        "other",
    }
)
_DEDUPLICATION_STAGES = frozenset(
    {"openai", "jev", "terminal_commit", "completed_slot", "replay"}
)
_STAGE_PROVIDERS: dict[str, frozenset[str | None]] = {
    "gamma_poll": frozenset({"gamma"}),
    "precheck_book": frozenset({"clob"}),
    "execution_book": frozenset({"clob"}),
    "openai": frozenset({"openai"}),
    "jev": frozenset({"jev"}),
    "eligibility": frozenset({None}),
    "evidence_freeze": frozenset({None}),
    "decision_barrier": frozenset({None}),
    "paper_execution": frozenset({None}),
    "terminal_commit": frozenset({None}),
    "completed_slot": frozenset({None}),
    "replay": frozenset({None}),
}
_STAGE_REASONS: dict[str, frozenset[str]] = {
    "gamma_poll": _MARKET_REASONS,
    "precheck_book": _MARKET_REASONS,
    "execution_book": _MARKET_REASONS,
    "openai": _MODEL_REASONS,
    "jev": _MODEL_REASONS,
    "eligibility": _PIPELINE_REASONS,
    "evidence_freeze": _PIPELINE_REASONS,
    "decision_barrier": _PIPELINE_REASONS | _MODEL_REASONS,
    "paper_execution": _PIPELINE_REASONS | _MARKET_REASONS,
    "terminal_commit": _STORAGE_REASONS,
    "completed_slot": _STORAGE_REASONS,
    "replay": _RELIABILITY_REASONS,
}
_EVIDENCE_KEYS = frozenset({"schema_version", "kind", "status", "detail"})
_EVIDENCE_STATUSES = frozenset({"available", "unavailable", "partial", "complete"})
_EVIDENCE_DETAILS = frozenset(
    {
        "probe_available",
        "probe_unavailable",
        "metadata_available",
        "metadata_unavailable",
    }
)
_EVIDENCE_KINDS = frozenset(
    {
        "startup",
        "run_started",
        "run_terminal",
        "stage_terminal",
        "recovery",
        "deduplication",
        "storage_failure",
        "process_sample",
        "metrics_generation",
    }
)


class OperationError(Exception):
    """Base class for operational journal failures."""


class OperationSecurityError(OperationError):
    """An operational path or payload violates the private boundary."""


class OperationCorruptError(OperationError):
    """Persisted operational state cannot be accepted."""


class OperationConflictError(OperationCorruptError):
    """An immutable event identity names different canonical bytes."""


class OperationKind(StrEnum):
    STARTUP = "startup"
    RUN_STARTED = "run_started"
    RUN_TERMINAL = "run_terminal"
    STAGE_TERMINAL = "stage_terminal"
    RECOVERY = "recovery"
    DEDUPLICATION = "deduplication"
    STORAGE_FAILURE = "storage_failure"
    PROCESS_SAMPLE = "process_sample"
    METRICS_GENERATION = "metrics_generation"


_EVENT_STATUSES = {
    OperationKind.STARTUP: frozenset({"clean", "recovery_required", "recovery_failed"}),
    OperationKind.RUN_STARTED: frozenset({"started"}),
    OperationKind.RUN_TERMINAL: frozenset(
        {"success", "failure", "replayed", "recovered"}
    ),
    OperationKind.STAGE_TERMINAL: frozenset(
        {"success", "failure", "skipped", "recovered", "timeout"}
    ),
    OperationKind.RECOVERY: frozenset({"success", "failure"}),
    OperationKind.DEDUPLICATION: frozenset({"deduplicated"}),
    OperationKind.STORAGE_FAILURE: frozenset({"failure"}),
    OperationKind.PROCESS_SAMPLE: frozenset({"success", "failure"}),
    OperationKind.METRICS_GENERATION: frozenset({"success", "failure", "skipped"}),
}
_EVENT_EXACT_KEYS = {
    OperationKind.STARTUP: frozenset(),
    OperationKind.RUN_STARTED: frozenset(),
    OperationKind.RUN_TERMINAL: frozenset(),
    OperationKind.STAGE_TERMINAL: frozenset(),
    OperationKind.RECOVERY: frozenset({"discarded_bytes"}),
    OperationKind.DEDUPLICATION: frozenset(),
    OperationKind.STORAGE_FAILURE: frozenset(),
    # Task 4 deliberately extends process facts after its own reviewed TDD cycle.
    OperationKind.PROCESS_SAMPLE: frozenset(),
    OperationKind.METRICS_GENERATION: frozenset(),
}


Stage = Literal[
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
]
Provider = Literal["openai", "jev", "gamma", "clob"]
BoundedLabel = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
]
ExactInt = Annotated[int, Field(strict=True, ge=0, le=INT64_MAX)]


class BlobRefModel(StrictModel):
    """Canonical reference to one operation evidence blob."""

    schema_version: Literal[1]
    sha256: Sha256
    byte_size: Annotated[int, Field(strict=True, ge=0, le=_MAX_EVIDENCE_BYTES)]
    storage_relpath: str

    @model_validator(mode="after")
    def bind_path(self) -> Self:
        digest = self.sha256
        expected = f"operation-blobs/{digest[:2]}/{digest[2:4]}/{digest}"
        if self.storage_relpath != expected:
            raise ValueError("operation evidence path is noncanonical")
        return self

    def blob_ref(self) -> BlobRef:
        return BlobRef(
            self.sha256,
            self.byte_size,
            self.storage_relpath.removeprefix("operation-blobs/"),
        )


def _identity_value(value: object, depth: int = 0) -> object:
    if depth > 16:
        raise ValueError("operation identity exceeds maximum depth")
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("operation timestamp must use UTC")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, StrEnum):
        return value.value
    if type(value) is list:
        return [_identity_value(item, depth + 1) for item in cast(list[object], value)]
    if type(value) is dict:
        result: dict[str, object] = {}
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise TypeError("operation identity keys must be built-in strings")
            result[key] = _identity_value(item, depth + 1)
        return result
    raise TypeError("operation identity uses an unsupported value type")


def operation_event_id(payload_without_id: Mapping[str, object]) -> str:
    """Hash one exact canonical event payload without its identity field."""

    if type(payload_without_id) is not dict:
        raise TypeError("operation identity payload must be a built-in dict")
    if "event_id" in payload_without_id:
        raise ValueError("operation identity payload must omit event_id")
    normalized = _identity_value(payload_without_id)
    return hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()


class OperationEvent(StrictModel):
    """One bounded raw operational fact; never mutable aggregate metric state."""

    schema_version: Literal[1]
    event_id: Sha256
    occurred_at: datetime
    kind: OperationKind
    run_id: Sha256 | None = None
    semantic_id: Sha256 | None = None
    stage: Stage | None = None
    provider: Provider | None = None
    status: BoundedLabel
    reason: BoundedLabel | None = None
    elapsed_nanoseconds: ExactInt | None = None
    exact_values: dict[str, ExactInt] = Field(default_factory=dict)
    evidence: BlobRefModel | None = None

    @field_validator("occurred_at", mode="after")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("operation timestamp must use UTC")
        return value

    @field_validator("exact_values", mode="before")
    @classmethod
    def exact_builtin_values(cls, value: object) -> object:
        if type(value) is not dict:
            raise TypeError("exact_values must be a built-in dict")
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str or type(item) is not int:
                raise TypeError(
                    "exact_values must contain built-in string and int values"
                )
        return value

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if self.status not in _EVENT_STATUSES[self.kind]:
            raise ValueError("operation status is invalid for event kind")
        if self.stage is not None and self.stage not in _STAGE_VALUES:
            raise ValueError("operation stage is not approved")
        if set(self.exact_values) - _EVENT_EXACT_KEYS[self.kind]:
            raise ValueError("exact_values keys are invalid for event kind")
        if (
            self.kind is OperationKind.RECOVERY
            and self.reason == "partial_tail"
            and ("discarded_bytes" not in self.exact_values)
        ):
            raise ValueError("partial-tail recovery reason requires discarded_bytes")
        if (
            self.kind is OperationKind.RECOVERY
            and self.reason != "partial_tail"
            and ("discarded_bytes" in self.exact_values)
        ):
            raise ValueError("discarded_bytes requires partial-tail recovery reason")
        requires_run = self.kind in {
            OperationKind.RUN_STARTED,
            OperationKind.RUN_TERMINAL,
            OperationKind.STAGE_TERMINAL,
            OperationKind.PROCESS_SAMPLE,
        }
        if requires_run != (self.run_id is not None):
            raise ValueError("run_id presence is invalid for event kind")
        if (self.kind is OperationKind.DEDUPLICATION) != (self.semantic_id is not None):
            raise ValueError("semantic_id presence is invalid for event kind")
        requires_stage = self.kind in {
            OperationKind.STARTUP,
            OperationKind.STAGE_TERMINAL,
            OperationKind.RECOVERY,
            OperationKind.DEDUPLICATION,
            OperationKind.STORAGE_FAILURE,
            OperationKind.METRICS_GENERATION,
        }
        if requires_stage != (self.stage is not None):
            raise ValueError("stage presence is invalid for event kind")
        expected_stage = {
            OperationKind.STARTUP: "startup",
            OperationKind.RECOVERY: "recovery",
            OperationKind.METRICS_GENERATION: "metrics_render",
        }.get(self.kind)
        if expected_stage is not None and self.stage != expected_stage:
            raise ValueError("operation stage is invalid for event kind")
        if self.kind is OperationKind.DEDUPLICATION and (
            self.stage not in _DEDUPLICATION_STAGES
        ):
            raise ValueError("operation stage is invalid for deduplication")
        if self.provider is not None and self.kind is not OperationKind.STAGE_TERMINAL:
            raise ValueError("provider is valid only for a terminal stage")
        if self.kind is OperationKind.STAGE_TERMINAL:
            if self.elapsed_nanoseconds is None:
                raise ValueError("stage terminal event requires elapsed nanoseconds")
            if self.stage not in _STAGE_PROVIDERS:
                raise ValueError("operation stage is invalid for terminal stage")
            if self.provider not in _STAGE_PROVIDERS[self.stage]:
                raise ValueError("operation provider is invalid for stage")
        elif (
            self.kind is not OperationKind.RUN_TERMINAL
            and self.elapsed_nanoseconds is not None
        ):
            raise ValueError("elapsed nanoseconds are invalid for event kind")
        reason_required = self.kind in {
            OperationKind.RECOVERY,
            OperationKind.STORAGE_FAILURE,
        } or self.status in {"failure", "timeout", "recovery_failed"}
        if reason_required != (self.reason is not None):
            raise ValueError("operation reason presence is invalid for status")
        if self.reason is not None:
            if self.kind is OperationKind.STAGE_TERMINAL:
                assert self.stage is not None
                allowed_reasons = _STAGE_REASONS[self.stage]
            elif self.kind is OperationKind.RECOVERY:
                allowed_reasons = _RELIABILITY_REASONS
            elif self.kind is OperationKind.STORAGE_FAILURE:
                allowed_reasons = _STORAGE_REASONS
            elif self.kind is OperationKind.METRICS_GENERATION:
                allowed_reasons = _STORAGE_REASONS
            elif self.kind is OperationKind.RUN_TERMINAL:
                allowed_reasons = _PIPELINE_REASONS | _RELIABILITY_REASONS
            elif self.kind is OperationKind.PROCESS_SAMPLE:
                allowed_reasons = frozenset({"process"})
            elif self.kind is OperationKind.STARTUP:
                allowed_reasons = _RELIABILITY_REASONS
            else:
                allowed_reasons = frozenset()
            if self.reason not in allowed_reasons:
                raise ValueError("operation reason is invalid for event contract")
        expected_id = operation_event_id(
            self.model_dump(mode="json", exclude={"event_id"})
        )
        if self.event_id != expected_id:
            raise ValueError("operation event_id does not match canonical payload")
        return self


@dataclass(frozen=True, slots=True)
class OperationCut:
    events: tuple[OperationEvent, ...]
    journal_bytes: int
    valid_blobs: int
    discarded_tail_bytes: int = 0


def _open_root(root: Path) -> int:
    path = root if root.is_absolute() else root.absolute()
    try:
        return _open_or_create_root(path)
    except BlobSecurityError as error:
        raise OperationSecurityError("operation root is unsafe") from error


def _open_journal(root_fd: int) -> tuple[int, bool]:
    created = False
    try:
        os.stat("operations.jsonl", dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        created = True
    flags = (
        os.O_RDWR
        | os.O_APPEND
        | os.O_CREAT
        | os.O_CLOEXEC
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
    )
    try:
        descriptor = os.open("operations.jsonl", flags, 0o600, dir_fd=root_fd)
    except OSError as error:
        raise OperationSecurityError(
            "operation journal cannot be opened safely"
        ) from error
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        os.close(descriptor)
        raise OperationSecurityError(
            "operation journal must be a private owner-only regular file"
        )
    return descriptor, created


def _fsync_root(root_fd: int) -> None:
    """Re-establish directory durability; retries cover ambiguous creation failures."""

    os.fsync(root_fd)


def _lock(descriptor: int, operation: int) -> None:
    while True:
        try:
            fcntl.flock(descriptor, operation)
            return
        except InterruptedError:
            continue


def _read_bytes(descriptor: int) -> bytes:
    size = os.fstat(descriptor).st_size
    if size > _MAX_JOURNAL_BYTES:
        raise OperationCorruptError("operation journal exceeds maximum size")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        try:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
        except InterruptedError:
            continue
        if not chunk:
            raise OperationCorruptError("operation journal changed while locked")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _parse_events(raw: bytes) -> tuple[OperationEvent, ...]:
    lines = raw.splitlines()
    if len(lines) > _MAX_LINES or any(
        not line or len(line) + 1 > _MAX_LINE_BYTES for line in lines
    ):
        raise OperationCorruptError("operation journal contains an invalid line")
    events: list[OperationEvent] = []
    identities: dict[str, bytes] = {}
    for line in lines:
        try:
            document = load_strict_json(line)
            if type(document) is not dict or canonical_json_bytes(document) != line:
                raise ValueError
            value = OperationEvent.model_validate_json(line)
        except (TypeError, ValueError) as error:
            raise OperationCorruptError(
                "operation journal contains invalid canonical event JSON"
            ) from error
        prior = identities.get(value.event_id)
        if prior is not None:
            if prior != line:
                raise OperationConflictError(
                    "operation journal contains conflicting duplicate event"
                )
            continue
        identities[value.event_id] = line
        events.append(value)
    _validate_transitions(tuple(events))
    return tuple(events)


def _validate_transitions(events: tuple[OperationEvent, ...]) -> None:
    runs: dict[str, bool] = {}
    for event in events:
        if event.kind is OperationKind.RUN_STARTED:
            assert event.run_id is not None
            if event.run_id in runs:
                raise OperationCorruptError("operation run transition is invalid")
            runs[event.run_id] = False
        elif event.kind in {
            OperationKind.RUN_TERMINAL,
            OperationKind.STAGE_TERMINAL,
        }:
            assert event.run_id is not None
            terminal = runs.get(event.run_id)
            if terminal is None or terminal:
                raise OperationCorruptError("operation run transition is invalid")
            if event.kind is OperationKind.RUN_TERMINAL:
                runs[event.run_id] = True
        elif event.kind is OperationKind.PROCESS_SAMPLE:
            assert event.run_id is not None
            if event.run_id not in runs:
                raise OperationCorruptError("operation run transition is invalid")


def _verify_blobs(root: Path, events: tuple[OperationEvent, ...]) -> int:
    references: dict[str, tuple[BlobRefModel, set[OperationKind]]] = {}
    for event in events:
        reference = event.evidence
        if reference is None:
            continue
        prior = references.get(reference.sha256)
        if prior is not None and prior[0] != reference:
            raise OperationCorruptError(
                "operation evidence has conflicting blob reference metadata"
            )
        if prior is None:
            references[reference.sha256] = (reference, {event.kind})
        else:
            prior[1].add(event.kind)
    if not references:
        return 0
    try:
        with BlobStore(
            root / "operation-blobs", max_bytes=_MAX_EVIDENCE_BYTES
        ) as store:
            for reference, kinds in references.values():
                payload = store.read_verified(reference.blob_ref())
                for kind in kinds:
                    _parse_evidence(payload, kind)
    except (BlobError, OSError, OperationSecurityError) as error:
        raise OperationCorruptError(
            "operation evidence is missing or corrupt"
        ) from error
    return len(references)


def _parse_evidence(payload: bytes, kind: OperationKind) -> dict[str, object]:
    if type(payload) is not bytes:
        raise TypeError("operation evidence must be exact bytes")
    if len(payload) > _MAX_EVIDENCE_BYTES:
        raise ValueError("operation evidence exceeds maximum size")
    try:
        document = load_strict_json(payload)
        if (
            type(document) is not dict
            or canonical_json_bytes(document) != payload
            or set(document) != _EVIDENCE_KEYS
            or type(document.get("schema_version")) is not int
            or document["schema_version"] != 1
            or type(document.get("kind")) is not str
            or document["kind"] != kind.value
            or document["kind"] not in _EVIDENCE_KINDS
            or type(document.get("status")) is not str
            or document["status"] not in _EVIDENCE_STATUSES
            or type(document.get("detail")) is not str
            or document["detail"] not in _EVIDENCE_DETAILS
            or any(is_credential_name(key) for key in document)
        ):
            raise ValueError
    except (TypeError, UnicodeError, ValueError):
        raise OperationSecurityError(
            "operation evidence violates the closed canonical schema"
        ) from None
    return cast(dict[str, object], document)


def append_operation(
    root: Path, event: OperationEvent, evidence: bytes | None = None
) -> None:
    """Durably append one unique canonical event after any evidence blob is durable."""

    if not isinstance(root, Path):
        raise TypeError("operation root must be a Path")
    if type(event) is not OperationEvent:
        raise TypeError("event must be an OperationEvent")
    supplied = canonical_json_bytes(event.model_dump(mode="json"))
    if len(supplied) + 1 > _MAX_LINE_BYTES:
        raise ValueError("operation journal line exceeds maximum size")
    if event.evidence is None:
        if evidence is not None:
            raise ValueError("unexpected operation evidence bytes")
    else:
        if evidence is None:
            raise ValueError("operation evidence bytes are required")
        if (
            len(evidence) != event.evidence.byte_size
            or hashlib.sha256(evidence).hexdigest() != event.evidence.sha256
        ):
            raise ValueError("operation evidence does not match its reference")
        _parse_evidence(evidence, event.kind)
    root_path = root if root.is_absolute() else root.absolute()
    root_fd = _open_root(root_path)
    try:
        descriptor, _created = _open_journal(root_fd)
        try:
            _lock(descriptor, fcntl.LOCK_EX)
            raw = _read_bytes(descriptor)
            if raw and not raw.endswith(b"\n"):
                raise OperationCorruptError("operation journal has a truncated tail")
            known = _parse_events(raw)
            existing = next(
                (item for item in known if item.event_id == event.event_id), None
            )
            if existing is not None:
                if canonical_json_bytes(existing.model_dump(mode="json")) != supplied:
                    raise OperationConflictError(
                        "operation event identity has conflicting canonical bytes"
                    )
                os.fsync(descriptor)
                _fsync_root(root_fd)
                return
            try:
                verified = OperationEvent.model_validate(
                    event.model_dump(mode="python")
                )
                _validate_transitions((*known, verified))
            except (TypeError, ValueError) as error:
                raise ValueError("operation event is invalid") from error
            framed = canonical_json_bytes(verified.model_dump(mode="json")) + b"\n"
            if len(raw) + len(framed) > _MAX_JOURNAL_BYTES:
                raise ValueError("operation journal exceeds maximum size")
            if len(known) >= _MAX_LINES:
                raise ValueError("operation journal exceeds maximum entries")
            if evidence is not None:
                evidence_ref = event.evidence
                assert evidence_ref is not None
                try:
                    with BlobStore(
                        root_path / "operation-blobs", max_bytes=_MAX_EVIDENCE_BYTES
                    ) as store:
                        reference = store.publish(evidence)
                        if reference != evidence_ref.blob_ref():
                            raise OperationCorruptError(
                                "operation evidence publication changed its identity"
                            )
                        store.read_verified(reference)
                except BlobSecurityError as error:
                    raise OperationSecurityError(
                        "operation evidence path is unsafe"
                    ) from error
                except BlobError as error:
                    raise OperationCorruptError(
                        "operation evidence cannot be stored safely"
                    ) from error
            os.lseek(descriptor, 0, os.SEEK_END)
            _write_all(descriptor, framed)
            os.fsync(descriptor)
            _fsync_root(root_fd)
        finally:
            os.close(descriptor)
    finally:
        os.close(root_fd)


def load_operation_cut(root: Path, *, recover_tail: bool = False) -> OperationCut:
    """Load a verified cut; optionally discard, but never parse, a torn tail."""

    if not isinstance(root, Path):
        raise TypeError("operation root must be a Path")
    if type(recover_tail) is not bool:
        raise TypeError("recover_tail must be a bool")
    root_path = root if root.is_absolute() else root.absolute()
    root_fd = _open_root(root_path)
    discarded = 0
    try:
        descriptor, _created = _open_journal(root_fd)
        try:
            _lock(descriptor, fcntl.LOCK_EX if recover_tail else fcntl.LOCK_SH)
            raw = _read_bytes(descriptor)
            candidate = raw
            if raw and not raw.endswith(b"\n"):
                boundary = raw.rfind(b"\n") + 1
                discarded = len(raw) - boundary
                if not recover_tail:
                    raise OperationCorruptError(
                        "operation journal has a truncated tail"
                    )
                candidate = raw[:boundary]
            events = _parse_events(candidate)
            valid_blobs = _verify_blobs(root_path, events)
            if discarded:
                os.ftruncate(descriptor, len(candidate))
            os.fsync(descriptor)
            _fsync_root(root_fd)
            journal_bytes = len(candidate)
        finally:
            os.close(descriptor)
    finally:
        os.close(root_fd)
    return OperationCut(events, journal_bytes, valid_blobs, discarded)

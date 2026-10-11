"""Durable provider-call journal inside a trusted private local owner boundary.

The journal detects corruption, unsafe links, malformed state, and crashes. It is not
authenticated against deliberate same-OS-user or same-process rewriting of every
journal/blob and recomputation of unkeyed hashes. Keys, signatures, and a daemon are
explicitly outside this paper-feasibility slice.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

from pydantic import Field, StringConstraints, model_validator

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.persistence.blobs import (
    BlobCorruptError,
    BlobError,
    BlobRef,
    BlobSecurityError,
    BlobStore,
    _open_or_create_root,
    _write_all,
)
from jev_trading_lab.polymarket.ai import (
    JEV_MODEL_ID,
    OPENAI_MODEL_ID,
    AdapterCostOverflow,
    AdapterFailure,
    JevSuccess,
    OpenAISuccess,
    is_canonical_failure_message,
)
from jev_trading_lab.polymarket.models import StrictModel, load_strict_json

_SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_MAX_JOURNAL_BYTES = 64 * 1024 * 1024
_MAX_LINE_BYTES = 1024 * 1024
_MAX_LINES = 10_000
_MAX_JSON_DEPTH = 32
_PROVIDER_MODEL = {"openai": OPENAI_MODEL_ID, "jev": JEV_MODEL_ID}


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("provider call timestamp must be UTC-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_depth(value: object, depth: int = 0) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("provider call journal JSON exceeds depth limit")
    if isinstance(value, dict):
        for nested in value.values():
            _json_depth(nested, depth + 1)
    elif isinstance(value, list):
        for nested in value:
            _json_depth(nested, depth + 1)


def _strict_document(raw: bytes, *, label: str) -> dict[str, Any]:
    if not raw or len(raw) > _MAX_LINE_BYTES:
        raise ValueError(f"{label} has invalid size")
    try:
        value = load_strict_json(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} contains invalid strict JSON") from error
    _json_depth(value)
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        raise ValueError(f"{label} is noncanonical")
    return cast(dict[str, Any], value)


def _call_id(
    *,
    opportunity_id: str,
    provider: Literal["openai", "jev"],
    model: str,
    request_sha256: str,
    attempt: int,
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "attempt": attempt,
                "model": model,
                "opportunity_id": opportunity_id,
                "provider": provider,
                "request_sha256": request_sha256,
            }
        )
    ).hexdigest()


class _JournalEnvelope(StrictModel):
    artifact_sha256: _SHA256
    byte_size: Annotated[int, Field(strict=True, ge=1, le=_MAX_LINE_BYTES)]
    call_id: _SHA256
    kind: Literal["provider_call_intent", "provider_call_result"]
    storage_relpath: str

    @model_validator(mode="after")
    def canonical_path(self) -> Self:
        digest = self.artifact_sha256
        expected = f"provider-call-blobs/{digest[:2]}/{digest[2:4]}/{digest}"
        if self.storage_relpath != expected:
            raise ValueError("provider call artifact path is noncanonical")
        return self


class _Intent(StrictModel):
    attempt: Literal[1]
    call_id: _SHA256
    created_at: datetime
    kind: Literal["provider_call_intent"]
    model: str
    opportunity_id: _SHA256
    provider: Literal["openai", "jev"]
    request_b64: str
    request_sha256: _SHA256
    retryable: Literal[False]

    @model_validator(mode="after")
    def bind_identity(self) -> Self:
        try:
            request = base64.b64decode(self.request_b64, validate=True)
        except ValueError as error:
            raise ValueError("provider intent request encoding is invalid") from error
        if (
            self.created_at.utcoffset() != timedelta(0)
            or self.model != _PROVIDER_MODEL[self.provider]
            or hashlib.sha256(request).hexdigest() != self.request_sha256
            or self.call_id
            != _call_id(
                opportunity_id=self.opportunity_id,
                provider=self.provider,
                model=self.model,
                request_sha256=self.request_sha256,
                attempt=self.attempt,
            )
        ):
            raise ValueError("provider intent identity is invalid")
        return self


class _Result(StrictModel):
    call_id: _SHA256
    finished_at: datetime
    intent_sha256: _SHA256
    kind: Literal["provider_call_result"]
    outcome: dict[str, Any]
    retryable: Literal[False]

    @model_validator(mode="after")
    def utc_timestamp(self) -> Self:
        if self.finished_at.utcoffset() != timedelta(0):
            raise ValueError("provider result timestamp must use UTC")
        return self


def adapter_outcome(value: object) -> dict[str, Any]:
    """Encode a sanitized adapter result for exact crash replay."""

    common: dict[str, Any] = {
        "request_b64": base64.b64encode(value.request_bytes).decode(),  # type: ignore[attr-defined]
        "type": type(value).__name__,
    }
    if isinstance(value, (OpenAISuccess, JevSuccess)):
        common.update(
            response_b64=base64.b64encode(value.response_bytes).decode(),
            retrieved_at=_utc_text(value.retrieved_at),
            billed_cost_microdollars=value.billed_cost_microdollars,
            parsed=(
                value.brief if isinstance(value, OpenAISuccess) else value.decision
            ).model_dump(mode="json"),
        )
    elif isinstance(value, AdapterFailure):
        common.update(
            code=value.code,
            message=value.message,
            response_sha256=value.response_sha256,
            response_byte_length=value.response_byte_length,
            retrieved_at=_utc_text(value.retrieved_at) if value.retrieved_at else None,
            billed_cost_microdollars=value.billed_cost_microdollars,
            cost_status=value.cost_status,
            status_code=value.status_code,
        )
    elif isinstance(value, AdapterCostOverflow):
        common.update(
            response_sha256=value.response_sha256,
            response_byte_length=value.response_byte_length,
            retrieved_at=_utc_text(value.retrieved_at),
            status_code=value.status_code,
            evidence={
                "provider": value.evidence.provider,
                "input_tokens": str(value.evidence.input_tokens),
                "cached_input_tokens": (
                    str(value.evidence.cached_input_tokens)
                    if value.evidence.cached_input_tokens is not None
                    else None
                ),
                "output_tokens": str(value.evidence.output_tokens),
                "input_rate": value.evidence.input_rate,
                "cached_input_rate": value.evidence.cached_input_rate,
                "output_rate": value.evidence.output_rate,
            },
        )
    else:
        raise TypeError("unsupported provider result")
    return common


def _valid_adapter_metadata(
    *,
    response_sha256: str | None,
    response_byte_length: int,
    retrieved_at: datetime | None,
    billed_cost_microdollars: int | None,
    status_code: int | None,
) -> bool:
    return (
        (
            response_sha256 is None
            or re.fullmatch(r"[0-9a-f]{64}", response_sha256) is not None
        )
        and type(response_byte_length) is int
        and 0 <= response_byte_length <= 2**63 - 1
        and (
            retrieved_at is None
            or (
                retrieved_at.tzinfo is not None
                and retrieved_at.utcoffset() == timedelta(0)
            )
        )
        and (
            billed_cost_microdollars is None
            or type(billed_cost_microdollars) is int
            and 0 <= billed_cost_microdollars <= 2**63 - 1
        )
        and (
            status_code is None
            or type(status_code) is int
            and 100 <= status_code <= 599
        )
    )


def adapter_from_outcome(
    provider: Literal["openai", "jev"], value: dict[str, Any]
) -> OpenAISuccess | JevSuccess | AdapterFailure | AdapterCostOverflow:
    """Strictly reconstruct one previously journaled adapter result."""

    from jev_trading_lab.polymarket.ai import CostOverflowEvidence
    from jev_trading_lab.polymarket.models import JevDecision, ResearchBrief

    try:
        request = base64.b64decode(value["request_b64"], validate=True)
        retrieved = (
            datetime.fromisoformat(value["retrieved_at"].replace("Z", "+00:00"))
            if value.get("retrieved_at")
            else None
        )
        kind = value["type"]
        if kind == "OpenAISuccess" and provider == "openai" and retrieved is not None:
            result: OpenAISuccess | JevSuccess | AdapterFailure | AdapterCostOverflow
            result = OpenAISuccess(
                ResearchBrief.model_validate(value["parsed"]),
                request,
                base64.b64decode(value["response_b64"], validate=True),
                retrieved,
                value["billed_cost_microdollars"],
            )
            if not _valid_adapter_metadata(
                response_sha256=hashlib.sha256(result.response_bytes).hexdigest(),
                response_byte_length=len(result.response_bytes),
                retrieved_at=result.retrieved_at,
                billed_cost_microdollars=result.billed_cost_microdollars,
                status_code=200,
            ):
                raise ValueError
        elif kind == "JevSuccess" and provider == "jev" and retrieved is not None:
            result = JevSuccess(
                JevDecision.model_validate(value["parsed"]),
                request,
                base64.b64decode(value["response_b64"], validate=True),
                retrieved,
                value["billed_cost_microdollars"],
            )
            if not _valid_adapter_metadata(
                response_sha256=hashlib.sha256(result.response_bytes).hexdigest(),
                response_byte_length=len(result.response_bytes),
                retrieved_at=result.retrieved_at,
                billed_cost_microdollars=result.billed_cost_microdollars,
                status_code=200,
            ):
                raise ValueError
        elif kind == "AdapterFailure":
            result = AdapterFailure(
                provider,
                value["code"],
                value["message"],
                request,
                value["response_sha256"],
                value["response_byte_length"],
                retrieved,
                value["billed_cost_microdollars"],
                value["status_code"],
            )
            if (
                not _valid_adapter_metadata(
                    response_sha256=result.response_sha256,
                    response_byte_length=result.response_byte_length,
                    retrieved_at=result.retrieved_at,
                    billed_cost_microdollars=result.billed_cost_microdollars,
                    status_code=result.status_code,
                )
                or not is_canonical_failure_message(
                    result.provider,
                    result.code,
                    result.message,
                    result.status_code,
                )
                or result.code == "HTTP_ERROR"
                and (
                    result.status_code is None
                    or 200 <= result.status_code < 300
                    or result.response_sha256 is None
                    or result.retrieved_at is None
                )
                or result.code in {"PARSE_ERROR", "MODEL_MISMATCH", "CITATION_ERROR"}
                and (
                    result.status_code is None
                    or not 200 <= result.status_code < 300
                    or result.response_sha256 is None
                    or result.retrieved_at is None
                )
                or result.code == "CREDENTIAL_REFLECTION"
                and (
                    result.status_code is None
                    or result.response_sha256 is None
                    or result.retrieved_at is None
                )
                or result.code
                in {
                    "INPUT_ERROR",
                    "INPUT_CHANGED",
                    "CONFIG_FAILURE",
                    "UNKNOWN_IN_FLIGHT",
                    "PROVIDER_ERROR",
                    "TIMEOUT",
                }
                and (
                    result.status_code is not None
                    or result.response_sha256 is not None
                    or result.retrieved_at is not None
                )
            ):
                raise ValueError
        elif kind == "AdapterCostOverflow" and retrieved is not None:
            evidence = value["evidence"]
            result = AdapterCostOverflow(
                provider,
                request,
                value["response_sha256"],
                value["response_byte_length"],
                retrieved,
                value["status_code"],
                CostOverflowEvidence(
                    provider=evidence["provider"],
                    input_tokens=int(evidence["input_tokens"]),
                    cached_input_tokens=(
                        int(evidence["cached_input_tokens"])
                        if evidence["cached_input_tokens"] is not None
                        else None
                    ),
                    output_tokens=int(evidence["output_tokens"]),
                    input_rate=evidence["input_rate"],
                    cached_input_rate=evidence["cached_input_rate"],
                    output_rate=evidence["output_rate"],
                ),
            )
            if (
                not _valid_adapter_metadata(
                    response_sha256=result.response_sha256,
                    response_byte_length=result.response_byte_length,
                    retrieved_at=result.retrieved_at,
                    billed_cost_microdollars=None,
                    status_code=result.status_code,
                )
                or not 200 <= result.status_code < 300
            ):
                raise ValueError
        else:
            raise ValueError
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("provider call result is invalid") from error
    if adapter_outcome(result) != value:
        raise ValueError("provider call result is noncanonical")
    return result


class ProviderCallJournal:
    """Persist one immutable provider attempt per opportunity/provider pair."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        path = Path(root)
        self.root = path.absolute() if not path.is_absolute() else path
        descriptor = _open_or_create_root(self.root)
        os.close(descriptor)

    def _root_fd(self) -> int:
        return _open_or_create_root(self.root)

    def _open(self, root_fd: int) -> int:
        flags = (
            os.O_RDWR
            | os.O_APPEND
            | os.O_CREAT
            | os.O_CLOEXEC
            | os.O_NOFOLLOW
            | os.O_NONBLOCK
        )
        existed = True
        try:
            os.stat("provider_calls.jsonl", dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            existed = False
        try:
            descriptor = os.open("provider_calls.jsonl", flags, 0o600, dir_fd=root_fd)
        except OSError as error:
            raise ValueError("provider call journal cannot be opened safely") from error
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            os.close(descriptor)
            raise ValueError("provider call journal must be a private owner-only file")
        if not existed:
            os.fsync(root_fd)
        return descriptor

    def _entries(self, descriptor: int) -> list[_JournalEnvelope]:
        metadata = os.fstat(descriptor)
        if metadata.st_size > _MAX_JOURNAL_BYTES:
            raise ValueError("provider call journal exceeds maximum size")
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            try:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
            except InterruptedError:
                continue
            if not chunk:
                raise ValueError("provider call journal changed while locked")
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        complete = raw
        torn_tail: bytes | None = None
        if raw and not raw.endswith(b"\n"):
            boundary = raw.rfind(b"\n") + 1
            complete, torn_tail = raw[:boundary], raw[boundary:]
        lines = complete.splitlines()
        if len(lines) > _MAX_LINES or any(
            not line or len(line) > _MAX_LINE_BYTES for line in lines
        ):
            raise ValueError("provider call journal contains an invalid line")
        entries: list[_JournalEnvelope] = []
        for line in lines:
            value = _strict_document(line, label="provider call journal")
            try:
                envelope = _JournalEnvelope.model_validate(value)
            except (TypeError, ValueError) as error:
                raise ValueError("provider call journal envelope is invalid") from error
            entries.append(envelope)
        if torn_tail is not None:
            os.ftruncate(descriptor, len(complete))
            os.fsync(descriptor)
        return entries

    def _append(self, descriptor: int, value: _JournalEnvelope) -> None:
        framed = canonical_json_bytes(value.model_dump(mode="json")) + b"\n"
        metadata = os.fstat(descriptor)
        if len(framed) > _MAX_LINE_BYTES:
            raise ValueError("provider call journal line exceeds maximum size")
        if metadata.st_size + len(framed) > _MAX_JOURNAL_BYTES:
            raise ValueError("provider call journal exceeds maximum size")
        if len(self._entries(descriptor)) >= _MAX_LINES:
            raise ValueError("provider call journal exceeds maximum entries")
        os.lseek(descriptor, 0, os.SEEK_END)
        _write_all(descriptor, framed)
        os.fsync(descriptor)

    def _publish(self, payload: bytes) -> tuple[str, int, str]:
        try:
            with BlobStore(
                self.root / "provider-call-blobs", max_bytes=_MAX_LINE_BYTES
            ) as store:
                ref = store.publish(payload)
                if store.read_verified(ref) != payload:
                    raise ValueError("provider call artifact verification failed")
        except (BlobError, OSError) as error:
            raise ValueError(
                "provider call artifact cannot be stored safely"
            ) from error
        return ref.sha256, ref.byte_length, f"provider-call-blobs/{ref.relative_path}"

    def _read_artifact(self, envelope: _JournalEnvelope) -> bytes:
        digest = envelope.artifact_sha256
        ref = BlobRef(
            digest,
            envelope.byte_size,
            f"{digest[:2]}/{digest[2:4]}/{digest}",
        )
        try:
            with BlobStore(
                self.root / "provider-call-blobs", max_bytes=_MAX_LINE_BYTES
            ) as store:
                return store.read_verified(ref)
        except (BlobCorruptError, BlobSecurityError, BlobError, OSError) as error:
            raise ValueError("provider call artifact is corrupt") from error

    def _states(
        self, entries: list[_JournalEnvelope]
    ) -> dict[str, tuple[_Intent, _Result | None, _JournalEnvelope]]:
        states: dict[str, tuple[_Intent, _Result | None, _JournalEnvelope]] = {}
        opportunities: set[tuple[str, str]] = set()
        for envelope in entries:
            payload = self._read_artifact(envelope)
            if hashlib.sha256(payload).hexdigest() != envelope.artifact_sha256:
                raise ValueError("provider call artifact is corrupt")
            _strict_document(payload, label="provider call artifact")
            try:
                if envelope.kind == "provider_call_intent":
                    intent = _Intent.model_validate_json(payload)
                    if intent.call_id != envelope.call_id or intent.call_id in states:
                        raise ValueError
                    key = (intent.opportunity_id, intent.provider)
                    if key in opportunities:
                        raise ValueError
                    opportunities.add(key)
                    states[intent.call_id] = (intent, None, envelope)
                    continue
                result = _Result.model_validate_json(payload)
                prior = states.get(result.call_id)
                if prior is None or prior[1] is not None:
                    raise ValueError
                intent, _none, intent_envelope = prior
                if (
                    result.call_id != envelope.call_id
                    or result.intent_sha256 != intent_envelope.artifact_sha256
                ):
                    raise ValueError
                outcome = adapter_from_outcome(intent.provider, result.outcome)
                if (
                    hashlib.sha256(outcome.request_bytes).hexdigest()
                    != intent.request_sha256
                ):
                    raise ValueError
                states[result.call_id] = (intent, result, envelope)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "provider call journal transition is invalid"
                ) from error
        return states

    def _locked_states(
        self, _operation: int
    ) -> tuple[int, int, dict[str, tuple[_Intent, _Result | None, _JournalEnvelope]]]:
        root_fd = self._root_fd()
        try:
            descriptor = self._open(root_fd)
        except BaseException:
            os.close(root_fd)
            raise
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                    break
                except InterruptedError:
                    continue
            states = self._states(self._entries(descriptor))
            return root_fd, descriptor, states
        except BaseException:
            os.close(descriptor)
            os.close(root_fd)
            raise

    def lookup(
        self, opportunity_id: str, provider: Literal["openai", "jev"]
    ) -> dict[str, Any] | None:
        root_fd, descriptor, states = self._locked_states(fcntl.LOCK_SH)
        try:
            selected = next(
                (
                    (call_id, state)
                    for call_id, state in states.items()
                    if state[0].opportunity_id == opportunity_id
                    and state[0].provider == provider
                ),
                None,
            )
            return self._state_value(*selected) if selected is not None else None
        finally:
            os.close(descriptor)
            os.close(root_fd)

    def opportunity_ids_created_at(self, created_at: datetime) -> tuple[str, ...]:
        """Return deterministic durable opportunities begun at one exact slot."""

        root_fd, descriptor, states = self._locked_states(fcntl.LOCK_SH)
        try:
            return tuple(
                sorted(
                    {
                        intent.opportunity_id
                        for intent, _result, _envelope in states.values()
                        if intent.created_at == created_at
                    }
                )
            )
        finally:
            os.close(descriptor)
            os.close(root_fd)

    def pending_opportunity_ids_created_at(
        self, created_at: datetime
    ) -> tuple[str, ...]:
        """Return same-slot opportunities with a durable unterminated provider call."""

        root_fd, descriptor, states = self._locked_states(fcntl.LOCK_SH)
        try:
            return tuple(
                sorted(
                    {
                        intent.opportunity_id
                        for intent, result, _envelope in states.values()
                        if intent.created_at == created_at and result is None
                    }
                )
            )
        finally:
            os.close(descriptor)
            os.close(root_fd)

    def begin(
        self,
        *,
        provider: Literal["openai", "jev"],
        opportunity_id: str,
        request_bytes: bytes,
        created_at: datetime,
    ) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", opportunity_id) is None:
            raise ValueError("provider opportunity ID is invalid")
        model = _PROVIDER_MODEL[provider]
        request_sha256 = hashlib.sha256(request_bytes).hexdigest()
        call_id = _call_id(
            opportunity_id=opportunity_id,
            provider=provider,
            model=model,
            request_sha256=request_sha256,
            attempt=1,
        )
        root_fd, descriptor, states = self._locked_states(fcntl.LOCK_EX)
        try:
            existing = next(
                (
                    known
                    for known, state in states.items()
                    if state[0].opportunity_id == opportunity_id
                    and state[0].provider == provider
                ),
                None,
            )
            if existing is not None:
                return existing
            intent = _Intent(
                attempt=1,
                call_id=call_id,
                created_at=created_at,
                kind="provider_call_intent",
                model=model,
                opportunity_id=opportunity_id,
                provider=provider,
                request_b64=base64.b64encode(request_bytes).decode(),
                request_sha256=request_sha256,
                retryable=False,
            )
            payload = canonical_json_bytes(intent.model_dump(mode="json"))
            sha256, byte_size, storage_relpath = self._publish(payload)
            self._append(
                descriptor,
                _JournalEnvelope(
                    artifact_sha256=sha256,
                    byte_size=byte_size,
                    call_id=call_id,
                    kind="provider_call_intent",
                    storage_relpath=storage_relpath,
                ),
            )
            return call_id
        finally:
            os.close(descriptor)
            os.close(root_fd)

    def finish(
        self,
        call_id: str,
        *,
        outcome: dict[str, Any],
        finished_at: datetime,
    ) -> None:
        root_fd, descriptor, states = self._locked_states(fcntl.LOCK_EX)
        try:
            prior = states.get(call_id)
            if prior is None:
                raise ValueError("provider result has no durable intent")
            intent, result, intent_envelope = prior
            reconstructed = adapter_from_outcome(intent.provider, outcome)
            canonical_outcome = adapter_outcome(reconstructed)
            if (
                hashlib.sha256(reconstructed.request_bytes).hexdigest()
                != intent.request_sha256
            ):
                raise ValueError("provider result request differs from intent")
            if result is not None:
                if result.outcome != canonical_outcome:
                    raise ValueError("provider call already has a different result")
                return
            value = _Result(
                call_id=call_id,
                finished_at=finished_at,
                intent_sha256=intent_envelope.artifact_sha256,
                kind="provider_call_result",
                outcome=canonical_outcome,
                retryable=False,
            )
            payload = canonical_json_bytes(value.model_dump(mode="json"))
            sha256, byte_size, storage_relpath = self._publish(payload)
            self._append(
                descriptor,
                _JournalEnvelope(
                    artifact_sha256=sha256,
                    byte_size=byte_size,
                    call_id=call_id,
                    kind="provider_call_result",
                    storage_relpath=storage_relpath,
                ),
            )
        finally:
            os.close(descriptor)
            os.close(root_fd)

    def _state_value(
        self,
        call_id: str,
        state: tuple[_Intent, _Result | None, _JournalEnvelope],
    ) -> dict[str, Any]:
        intent, result, envelope = state
        value: StrictModel = result or intent
        payload = canonical_json_bytes(value.model_dump(mode="json"))
        document = value.model_dump(mode="json")
        document.update(
            artifact_sha256=envelope.artifact_sha256,
            byte_size=envelope.byte_size,
            storage_relpath=envelope.storage_relpath,
            artifact_bytes=payload,
            call_id=call_id,
        )
        return document

    def state(self, call_id: str) -> dict[str, Any] | None:
        root_fd, descriptor, states = self._locked_states(fcntl.LOCK_SH)
        try:
            state = states.get(call_id)
            return self._state_value(call_id, state) if state is not None else None
        finally:
            os.close(descriptor)
            os.close(root_fd)

    def validate(self, opportunity_id: str) -> bool:
        """Verify all transitions and bind at least one intent to an opportunity."""

        root_fd, descriptor, states = self._locked_states(fcntl.LOCK_SH)
        try:
            return any(
                intent.opportunity_id == opportunity_id
                for intent, _result, _envelope in states.values()
            )
        finally:
            os.close(descriptor)
            os.close(root_fd)

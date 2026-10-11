"""Durable, content-addressed provider-call write-ahead journal."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from jev_trading_lab.core.ids import canonical_json_bytes


def adapter_outcome(value: object) -> dict[str, Any]:
    """Encode a sanitized adapter result for exact crash replay."""

    from jev_trading_lab.polymarket.ai import (
        AdapterCostOverflow,
        AdapterFailure,
        JevSuccess,
        OpenAISuccess,
    )

    common: dict[str, Any] = {
        "request_b64": base64.b64encode(value.request_bytes).decode(),  # type: ignore[attr-defined]
        "type": type(value).__name__,
    }
    if isinstance(value, (OpenAISuccess, JevSuccess)):
        common.update(
            response_b64=base64.b64encode(value.response_bytes).decode(),
            retrieved_at=value.retrieved_at.isoformat().replace("+00:00", "Z"),
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
            retrieved_at=(
                value.retrieved_at.isoformat().replace("+00:00", "Z")
                if value.retrieved_at
                else None
            ),
            billed_cost_microdollars=value.billed_cost_microdollars,
            status_code=value.status_code,
        )
    elif isinstance(value, AdapterCostOverflow):
        common.update(
            response_sha256=value.response_sha256,
            response_byte_length=value.response_byte_length,
            retrieved_at=value.retrieved_at.isoformat().replace("+00:00", "Z"),
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


def adapter_from_outcome(
    provider: Literal["openai", "jev"], value: dict[str, Any]
) -> object:
    """Strictly reconstruct one previously journaled adapter result."""

    from datetime import UTC

    from jev_trading_lab.polymarket.ai import (
        AdapterCostOverflow,
        AdapterFailure,
        CostOverflowEvidence,
        JevSuccess,
        OpenAISuccess,
    )
    from jev_trading_lab.polymarket.models import JevDecision, ResearchBrief

    request = base64.b64decode(value["request_b64"], validate=True)
    retrieved = (
        datetime.fromisoformat(value["retrieved_at"].replace("Z", "+00:00")).astimezone(
            UTC
        )
        if value.get("retrieved_at")
        else None
    )
    kind = value["type"]
    if kind == "OpenAISuccess" and provider == "openai" and retrieved is not None:
        return OpenAISuccess(
            ResearchBrief.model_validate(value["parsed"]),
            request,
            base64.b64decode(value["response_b64"], validate=True),
            retrieved,
            value["billed_cost_microdollars"],
        )
    if kind == "JevSuccess" and provider == "jev" and retrieved is not None:
        return JevSuccess(
            JevDecision.model_validate(value["parsed"]),
            request,
            base64.b64decode(value["response_b64"], validate=True),
            retrieved,
            value["billed_cost_microdollars"],
        )
    if kind == "AdapterFailure":
        return AdapterFailure(
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
    if kind == "AdapterCostOverflow" and retrieved is not None:
        return AdapterCostOverflow(
            provider,
            request,
            value["response_sha256"],
            value["response_byte_length"],
            retrieved,
            value["status_code"],
            CostOverflowEvidence(
                provider=value["evidence"]["provider"],
                input_tokens=int(value["evidence"]["input_tokens"]),
                cached_input_tokens=(
                    int(value["evidence"]["cached_input_tokens"])
                    if value["evidence"]["cached_input_tokens"] is not None
                    else None
                ),
                output_tokens=int(value["evidence"]["output_tokens"]),
                input_rate=value["evidence"]["input_rate"],
                cached_input_rate=value["evidence"]["cached_input_rate"],
                output_rate=value["evidence"]["output_rate"],
            ),
        )
    raise ValueError("provider call result is invalid")


class ProviderCallJournal:
    """Persist intent before a provider call and outcome immediately after it."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stat.S_IMODE(self.root.stat().st_mode) != 0o700:
            raise ValueError("provider journal root must be private")

    def _entries(self, descriptor: int) -> list[dict[str, Any]]:
        os.lseek(descriptor, 0, os.SEEK_SET)
        raw = b""
        while chunk := os.read(descriptor, 65536):
            raw += chunk
        if raw and not raw.endswith(b"\n"):
            raise ValueError("provider call journal is truncated")
        entries: list[dict[str, Any]] = []
        for line in raw.splitlines():
            value = json.loads(line)
            if canonical_json_bytes(value) != line:
                raise ValueError("provider call journal is noncanonical")
            entries.append(value)
        return entries

    def _open(self) -> int:
        descriptor = os.open(
            self.root / "provider_calls.jsonl",
            os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            os.close(descriptor)
            raise ValueError("provider call journal must be a private regular file")
        return descriptor

    def _append(self, value: dict[str, Any]) -> None:
        descriptor = self._open()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            os.write(descriptor, canonical_json_bytes(value) + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _publish(self, payload: bytes) -> tuple[str, int, str]:
        digest = hashlib.sha256(payload).hexdigest()
        relative = f"provider-call-blobs/{digest[:2]}/{digest[2:4]}/{digest}"
        blobs = self.root / "provider-call-blobs"
        blobs.mkdir(mode=0o700, exist_ok=True)
        parent = blobs / digest[:2] / digest[2:4]
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = parent / digest
        try:
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            if target.read_bytes() != payload:
                raise ValueError("provider call artifact hash collision") from None
        else:
            try:
                os.write(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        return digest, len(payload), relative

    def begin(
        self,
        *,
        provider: Literal["openai", "jev"],
        opportunity_id: str,
        request_bytes: bytes,
        created_at: datetime,
    ) -> str:
        request_sha256 = hashlib.sha256(request_bytes).hexdigest()
        call_id = hashlib.sha256(
            canonical_json_bytes(
                {
                    "opportunity_id": opportunity_id,
                    "provider": provider,
                    "request_sha256": request_sha256,
                }
            )
        ).hexdigest()
        if self.state(call_id) is not None:
            return call_id
        payload = canonical_json_bytes(
            {
                "call_id": call_id,
                "created_at": created_at.isoformat().replace("+00:00", "Z"),
                "kind": "provider_call_intent",
                "opportunity_id": opportunity_id,
                "provider": provider,
                "request_sha256": request_sha256,
                "retryable": False,
            }
        )
        sha256, byte_size, storage_relpath = self._publish(payload)
        self._append(
            {
                "artifact_sha256": sha256,
                "byte_size": byte_size,
                "call_id": call_id,
                "kind": "provider_call_intent",
                "storage_relpath": storage_relpath,
            }
        )
        return call_id

    def finish(
        self,
        call_id: str,
        *,
        outcome: dict[str, Any],
        finished_at: datetime,
    ) -> None:
        prior = self.state(call_id)
        if prior is None:
            raise ValueError("provider result has no durable intent")
        if prior["kind"] == "provider_call_result":
            if prior["outcome"] != outcome:
                raise ValueError("provider call already has a different result")
            return
        payload = canonical_json_bytes(
            {
                "call_id": call_id,
                "finished_at": finished_at.isoformat().replace("+00:00", "Z"),
                "kind": "provider_call_result",
                "outcome": outcome,
                "retryable": False,
            }
        )
        sha256, byte_size, storage_relpath = self._publish(payload)
        self._append(
            {
                "artifact_sha256": sha256,
                "byte_size": byte_size,
                "call_id": call_id,
                "kind": "provider_call_result",
                "storage_relpath": storage_relpath,
            }
        )

    def state(self, call_id: str) -> dict[str, Any] | None:
        path = self.root / "provider_calls.jsonl"
        if not path.exists():
            return None
        descriptor = self._open()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            selected = [
                entry
                for entry in self._entries(descriptor)
                if entry["call_id"] == call_id
            ]
        finally:
            os.close(descriptor)
        if not selected:
            return None
        entry = selected[-1]
        artifact = self.root / entry["storage_relpath"]
        payload = artifact.read_bytes()
        if hashlib.sha256(payload).hexdigest() != entry["artifact_sha256"]:
            raise ValueError("provider call artifact is corrupt")
        value = json.loads(payload)
        value.update(entry)
        value["artifact_bytes"] = payload
        return cast(dict[str, Any], value)

    def validate(self, opportunity_id: str) -> bool:
        """Verify every call artifact and bind selected calls to an opportunity."""

        path = self.root / "provider_calls.jsonl"
        if not path.exists():
            return False
        descriptor = self._open()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            entries = self._entries(descriptor)
        finally:
            os.close(descriptor)
        intents: dict[str, dict[str, Any]] = {}
        matched = False
        for entry in entries:
            artifact = self.root / entry["storage_relpath"]
            payload = artifact.read_bytes()
            if (
                hashlib.sha256(payload).hexdigest() != entry["artifact_sha256"]
                or len(payload) != entry["byte_size"]
            ):
                raise ValueError("provider call artifact is corrupt")
            value = json.loads(payload)
            if value["call_id"] != entry["call_id"] or value["kind"] != entry["kind"]:
                raise ValueError("provider call envelope is inconsistent")
            if entry["kind"] == "provider_call_intent":
                if entry["call_id"] in intents:
                    raise ValueError("provider call intent is duplicated")
                intents[entry["call_id"]] = value
                matched = matched or value["opportunity_id"] == opportunity_id
            elif entry["call_id"] not in intents:
                raise ValueError("provider call result has no intent")
        return matched

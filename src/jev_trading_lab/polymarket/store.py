"""Exact blob freeze and append-only journals for Polymarket observations."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.persistence.blobs import BlobError, BlobRef, BlobStore
from jev_trading_lab.polymarket.models import (
    INT64_MAX,
    ObservationResult,
    Sha256,
    UtcModel,
)

_LABEL_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MAX_LINE_BYTES = 1024 * 1024
_MAX_JOURNAL_BYTES = 64 * 1024 * 1024
_REQUIRED_HASH_KINDS = {
    "evidence": "evidence_sha256",
    "execution_book": "execution_book_sha256",
    "research_brief": "research_brief_sha256",
    "jev_decision": "jev_decision_sha256",
}
Label = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
ByteSize = Annotated[int, Field(strict=True, ge=0, le=INT64_MAX)]


class StoreError(Exception):
    """Base class for observation-store failures."""


class StoreSecurityError(StoreError):
    """The output root or a journal violates the private-file boundary."""


class StoreCorruptError(StoreError):
    """Persisted journal or artifact state cannot be replayed safely."""


class StoreConflictError(StoreError):
    """An immutable identity already names different canonical bytes."""


class ArtifactEnvelope(UtcModel):
    """Canonical reference to one exact, verified artifact blob."""

    kind: Label
    sha256: Sha256
    byte_size: ByteSize
    storage_relpath: str
    source_uri_label: Label
    retrieved_at: datetime
    opportunity_id: Sha256
    predecessor_stage_sha256: Sha256 | None

    @field_validator("retrieved_at", mode="after")
    @classmethod
    def require_utc_exactly(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("retrieval timestamp must use UTC")
        return value

    @model_validator(mode="after")
    def bind_storage_path(self) -> Self:
        expected = f"{self.sha256[:2]}/{self.sha256[2:4]}/{self.sha256}"
        if self.storage_relpath != expected:
            raise ValueError("storage path must be the canonical blob path")
        return self

    def blob_ref(self) -> BlobRef:
        return BlobRef(self.sha256, self.byte_size, self.storage_relpath)


@dataclass(frozen=True, slots=True)
class ArtifactToFreeze:
    """Exact bytes and non-secret provenance needed to freeze one stage."""

    kind: str
    payload: bytes
    source_uri_label: str
    retrieved_at: datetime
    predecessor_stage_sha256: str | None

    def __post_init__(self) -> None:
        if type(self.kind) is not str or _LABEL_RE.fullmatch(self.kind) is None:
            raise ValueError("artifact kind must be a safe label")
        if type(self.payload) is not bytes:
            raise TypeError("artifact payload must be exact bytes")
        if (
            type(self.source_uri_label) is not str
            or _LABEL_RE.fullmatch(self.source_uri_label) is None
        ):
            raise ValueError("source URI label must be a safe non-URL label")
        if type(self.retrieved_at) is not datetime or (
            self.retrieved_at.tzinfo is None
            or self.retrieved_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("artifact retrieval timestamp must be UTC")
        predecessor = self.predecessor_stage_sha256
        if predecessor is not None and (
            type(predecessor) is not str
            or re.fullmatch(r"[0-9a-f]{64}", predecessor) is None
        ):
            raise ValueError("predecessor stage SHA-256 is invalid")


@dataclass(frozen=True, slots=True)
class ObservationReplay:
    result: ObservationResult
    envelopes: dict[str, ArtifactEnvelope]
    artifacts: dict[str, bytes]


def _open_output(output_dir: Path) -> int:
    path = output_dir.absolute()
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise StoreSecurityError("output directory cannot be created safely") from error
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise StoreSecurityError("output directory cannot be opened safely") from error
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        os.close(descriptor)
        raise StoreSecurityError(
            "output directory must be private and owner-controlled"
        )
    return descriptor


def _open_journal(root_fd: int, name: str) -> int:
    flags = (
        os.O_RDWR
        | os.O_APPEND
        | os.O_CREAT
        | os.O_CLOEXEC
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
    )
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=root_fd)
    except OSError as error:
        raise StoreSecurityError("journal cannot be opened safely") from error
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        os.close(descriptor)
        raise StoreSecurityError("journal must be a private owner-only regular file")
    return descriptor


def _lock(descriptor: int, operation: int) -> None:
    while True:
        try:
            fcntl.flock(descriptor, operation)
            return
        except InterruptedError:
            continue


def _read_journal(descriptor: int, *, label: str) -> list[bytes]:
    metadata = os.fstat(descriptor)
    if metadata.st_size > _MAX_JOURNAL_BYTES:
        raise StoreCorruptError(f"{label} exceeds maximum size")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = metadata.st_size
    while remaining:
        try:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
        except InterruptedError:
            continue
        if not chunk:
            raise StoreCorruptError(f"{label} changed while locked")
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if raw and not raw.endswith(b"\n"):
        raise StoreCorruptError(f"{label} has a truncated tail")
    lines = raw.splitlines()
    if any(not line or len(line) > _MAX_LINE_BYTES for line in lines):
        raise StoreCorruptError(f"{label} contains an invalid line")
    return lines


def _append_lines(descriptor: int, lines: list[bytes], *, label: str) -> None:
    for line in lines:
        framed = line + b"\n"
        if len(framed) > _MAX_LINE_BYTES:
            raise ValueError(f"{label} line exceeds maximum size")
        view = memoryview(framed)
        while view:
            try:
                written = os.write(descriptor, view)
            except InterruptedError:
                continue
            if written <= 0:
                raise OSError(errno.EIO, "journal write made no progress")
            view = view[written:]
    os.fsync(descriptor)


def _parse_results(
    lines: Iterable[bytes],
) -> dict[str, tuple[ObservationResult, bytes]]:
    parsed: dict[str, tuple[ObservationResult, bytes]] = {}
    for line in lines:
        try:
            result = ObservationResult.model_validate_json(line)
        except (ValueError, TypeError) as error:
            raise StoreCorruptError(
                "result journal contains invalid strict JSON"
            ) from error
        if result.observation_id in parsed:
            raise StoreCorruptError("result journal contains duplicate observation IDs")
        canonical = canonical_json_bytes(result.model_dump(mode="json"))
        if line != canonical:
            raise StoreCorruptError("result journal contains noncanonical JSON")
        _validate_identity(result, corruption=True)
        parsed[result.observation_id] = (result, line)
    return parsed


def _parse_envelopes(lines: Iterable[bytes]) -> list[ArtifactEnvelope]:
    parsed: list[ArtifactEnvelope] = []
    identities: set[tuple[str, str]] = set()
    for line in lines:
        try:
            envelope = ArtifactEnvelope.model_validate_json(line)
        except (ValueError, TypeError) as error:
            raise StoreCorruptError(
                "artifact journal contains invalid strict JSON"
            ) from error
        if line != canonical_json_bytes(envelope.model_dump(mode="json")):
            raise StoreCorruptError("artifact journal contains noncanonical JSON")
        identity = (envelope.opportunity_id, envelope.kind)
        if identity in identities:
            raise StoreCorruptError(
                "artifact journal contains duplicate stage identities"
            )
        identities.add(identity)
        parsed.append(envelope)
    return parsed


def _validate_identity(result: ObservationResult, *, corruption: bool) -> None:
    cutoff = result.model_dump(mode="json")["cutoff_at"]
    opportunity = hashlib.sha256(
        canonical_json_bytes(
            {
                "policy_version": result.policy_version,
                "market_id": result.market_id,
                "cutoff_at": cutoff,
            }
        )
    ).hexdigest()
    observation = hashlib.sha256(
        canonical_json_bytes(
            {
                "opportunity_id": result.opportunity_id,
                "evidence_sha256": result.evidence_sha256,
                "execution_book_sha256": result.execution_book_sha256,
                "policy_version": result.policy_version,
            }
        )
    ).hexdigest()
    if opportunity != result.opportunity_id or observation != result.observation_id:
        error = "persisted observation identity is invalid"
        if corruption:
            raise StoreCorruptError(error)
        raise ValueError(error)


def _verified_result(result: ObservationResult) -> tuple[ObservationResult, bytes]:
    canonical = canonical_json_bytes(result.model_dump(mode="json"))
    verified = ObservationResult.model_validate_json(canonical)
    _validate_identity(verified, corruption=False)
    return verified, canonical


def _freeze(
    store: BlobStore,
    result: ObservationResult,
    artifact_refs: Iterable[ArtifactToFreeze],
) -> list[tuple[ArtifactEnvelope, bytes]]:
    frozen: list[tuple[ArtifactEnvelope, bytes]] = []
    kinds: set[str] = set()
    for artifact in artifact_refs:
        if type(artifact) is not ArtifactToFreeze:
            raise TypeError("artifact references must be ArtifactToFreeze values")
        if artifact.kind in kinds:
            raise ValueError("artifact kinds must be unique per observation")
        kinds.add(artifact.kind)
        ref = store.publish(artifact.payload)
        verified = store.read_verified(ref)
        expected_hash = hashlib.sha256(artifact.payload).hexdigest()
        if (
            verified != artifact.payload
            or ref.sha256 != expected_hash
            or ref.byte_length != len(artifact.payload)
        ):
            raise StoreCorruptError("published artifact failed exact verification")
        envelope = ArtifactEnvelope(
            kind=artifact.kind,
            sha256=ref.sha256,
            byte_size=ref.byte_length,
            storage_relpath=ref.relative_path,
            source_uri_label=artifact.source_uri_label,
            retrieved_at=artifact.retrieved_at,
            opportunity_id=result.opportunity_id,
            predecessor_stage_sha256=artifact.predecessor_stage_sha256,
        )
        frozen.append((envelope, verified))
    missing = set(_REQUIRED_HASH_KINDS) - kinds
    if missing:
        raise ValueError(f"required artifact kinds are missing: {sorted(missing)}")
    for envelope, _payload in frozen:
        field = _REQUIRED_HASH_KINDS.get(envelope.kind)
        if field is not None and envelope.sha256 != getattr(result, field):
            raise ValueError(
                f"{envelope.kind} artifact does not match observation hash"
            )
    return frozen


def _verify_replay_locked(
    store: BlobStore,
    result: ObservationResult,
    envelopes: list[ArtifactEnvelope],
) -> ObservationReplay:
    selected = [
        item for item in envelopes if item.opportunity_id == result.opportunity_id
    ]
    by_kind = {item.kind: item for item in selected}
    missing = set(_REQUIRED_HASH_KINDS) - set(by_kind)
    if missing:
        raise StoreCorruptError(
            f"required artifact envelopes are missing: {sorted(missing)}"
        )
    artifacts: dict[str, bytes] = {}
    predecessor: str | None = None
    for envelope in selected:
        if envelope.predecessor_stage_sha256 != predecessor:
            raise StoreCorruptError("artifact predecessor chain is invalid")
        field = _REQUIRED_HASH_KINDS.get(envelope.kind)
        if field is not None and envelope.sha256 != getattr(result, field):
            raise StoreCorruptError("artifact envelope does not match observation hash")
        try:
            artifacts[envelope.kind] = store.read_verified(envelope.blob_ref())
        except (BlobError, ValueError) as error:
            raise StoreCorruptError("artifact blob is missing or corrupt") from error
        predecessor = envelope.sha256
    return ObservationReplay(result=result, envelopes=by_kind, artifacts=artifacts)


def record_observation(
    output_dir: str | os.PathLike[str],
    result: ObservationResult,
    artifact_refs: Iterable[ArtifactToFreeze],
) -> ObservationResult:
    """Publish and verify every blob, then durably append stages and terminal result."""

    if type(result) is not ObservationResult:
        raise TypeError("result must be an ObservationResult")
    verified_result, result_line = _verified_result(result)
    output = Path(output_dir)
    root_fd = _open_output(output)
    try:
        with BlobStore(output.absolute() / "blobs") as store:
            frozen = _freeze(store, verified_result, artifact_refs)
            results_fd = _open_journal(root_fd, "results.jsonl")
            try:
                _lock(results_fd, fcntl.LOCK_EX)
                existing_results = _parse_results(
                    _read_journal(results_fd, label="result journal")
                )
                existing = existing_results.get(verified_result.observation_id)
                artifacts_fd = _open_journal(root_fd, "artifacts.jsonl")
                try:
                    _lock(artifacts_fd, fcntl.LOCK_EX)
                    existing_envelopes = _parse_envelopes(
                        _read_journal(artifacts_fd, label="artifact journal")
                    )
                    if existing is not None:
                        if existing[1] != result_line:
                            raise StoreConflictError(
                                "observation identity already has different "
                                "canonical bytes"
                            )
                        prior_artifacts = [
                            item
                            for item in existing_envelopes
                            if item.opportunity_id == verified_result.opportunity_id
                        ]
                        if prior_artifacts != [item for item, _payload in frozen]:
                            raise StoreConflictError(
                                "observation identity already has a different "
                                "artifact set"
                            )
                        _verify_replay_locked(store, existing[0], existing_envelopes)
                        return existing[0]
                    current = {
                        (item.opportunity_id, item.kind): item
                        for item in existing_envelopes
                    }
                    append: list[bytes] = []
                    for envelope, _payload in frozen:
                        identity = (envelope.opportunity_id, envelope.kind)
                        prior = current.get(identity)
                        if prior is not None:
                            if prior != envelope:
                                raise StoreConflictError(
                                    "artifact stage identity already has different "
                                    "bytes"
                                )
                            continue
                        append.append(
                            canonical_json_bytes(envelope.model_dump(mode="json"))
                        )
                    _append_lines(artifacts_fd, append, label="artifact journal")
                    _append_lines(results_fd, [result_line], label="result journal")
                    return verified_result
                finally:
                    os.close(artifacts_fd)
            finally:
                os.close(results_fd)
    finally:
        os.close(root_fd)


def load_observation(
    output_dir: str | os.PathLike[str], observation_id: str
) -> ObservationReplay:
    """Replay one observation only from strict journals and verified exact blobs."""

    if (
        type(observation_id) is not str
        or re.fullmatch(r"[0-9a-f]{64}", observation_id) is None
    ):
        raise ValueError("observation ID must be a lowercase SHA-256")
    output = Path(output_dir)
    root_fd = _open_output(output)
    try:
        with BlobStore(output.absolute() / "blobs") as store:
            results_fd = _open_journal(root_fd, "results.jsonl")
            try:
                _lock(results_fd, fcntl.LOCK_SH)
                results = _parse_results(
                    _read_journal(results_fd, label="result journal")
                )
                found = results.get(observation_id)
                if found is None:
                    raise StoreCorruptError("observation result is missing")
                artifacts_fd = _open_journal(root_fd, "artifacts.jsonl")
                try:
                    _lock(artifacts_fd, fcntl.LOCK_SH)
                    envelopes = _parse_envelopes(
                        _read_journal(artifacts_fd, label="artifact journal")
                    )
                    return _verify_replay_locked(store, found[0], envelopes)
                finally:
                    os.close(artifacts_fd)
            finally:
                os.close(results_fd)
    finally:
        os.close(root_fd)

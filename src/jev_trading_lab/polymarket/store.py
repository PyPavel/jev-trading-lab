"""Exact blob freeze and append-only journals for Polymarket observations."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import stat
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.persistence.blobs import BlobError, BlobRef, BlobStore
from jev_trading_lab.polymarket.ai import (
    JEV_MODEL_ID,
    OPENAI_MODEL_ID,
    build_jev_request,
    build_openai_request,
)
from jev_trading_lab.polymarket.models import (
    INT64_MAX,
    BookSnapshot,
    EvidencePacket,
    JevDecision,
    JevResponse,
    ObservationResult,
    OpenAIResponse,
    ResearchBrief,
    Sha256,
    StrictModel,
    UtcModel,
    _raw_book_snapshot,
    jev_response_decision,
    load_strict_json,
    openai_response_brief,
)

_LABEL_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MAX_LINE_BYTES = 1024 * 1024
_MAX_JOURNAL_BYTES = 64 * 1024 * 1024
_SECRET_PATTERN = re.compile(
    r"(?i)(?:openai_api_key|typesafe_api_key|bearer\s+\S{4,}|"
    r"(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|"
    r"api[-_]?key)\s*[:=]\s*\S{4,})"
)


class ArtifactKind(StrEnum):
    GAMMA_RAW = "gamma_raw"
    SANITIZED_EVIDENCE = "sanitized_evidence"
    PRECHECK_BOOK = "precheck_book"
    OPENAI_REQUEST = "openai_request"
    OPENAI_RESPONSE = "openai_response"
    RESEARCH_BRIEF = "research_brief"
    JEV_REQUEST = "jev_request"
    JEV_RESPONSE = "jev_response"
    JEV_DECISION = "jev_decision"
    EXECUTION_BOOK = "execution_book"
    OBSERVATION_RESULT = "observation_result"


_STAGE_ORDER = tuple(ArtifactKind)
_RESULT_HASH_KINDS = {
    ArtifactKind.GAMMA_RAW: "evidence_sha256",
    ArtifactKind.EXECUTION_BOOK: "execution_book_sha256",
    ArtifactKind.RESEARCH_BRIEF: "research_brief_sha256",
    ArtifactKind.JEV_DECISION: "jev_decision_sha256",
}
Label = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
ByteSize = Annotated[int, Field(strict=True, ge=0, le=INT64_MAX)]
TypedArtifact = StrictModel | Mapping[str, object] | None


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
    observation_id: Sha256
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
    typed_artifacts: dict[str, TypedArtifact]


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
        try:
            ArtifactKind(envelope.kind)
        except ValueError as error:
            raise StoreCorruptError(
                "artifact journal contains an unknown stage"
            ) from error
        identity = (envelope.observation_id, envelope.kind)
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


def _screen_payload(payload: bytes, forbidden_secret_values: tuple[bytes, ...]) -> None:
    try:
        text = unicodedata.normalize("NFKC", payload.decode("utf-8")).casefold()
    except UnicodeDecodeError:
        text = ""
    if _SECRET_PATTERN.search(text):
        raise StoreSecurityError("artifact contains credential-like data")
    for value in forbidden_secret_values:
        if type(value) is not bytes or not value:
            raise ValueError("forbidden secret values must be nonempty bytes")
        try:
            sentinel = unicodedata.normalize("NFKC", value.decode("utf-8")).casefold()
        except UnicodeDecodeError:
            if value in payload:
                raise StoreSecurityError("artifact contains a forbidden secret value")
        else:
            if sentinel in text:
                raise StoreSecurityError("artifact contains a forbidden secret value")


def _canonical_stage_hash(envelope: ArtifactEnvelope) -> str:
    return hashlib.sha256(
        canonical_json_bytes(envelope.model_dump(mode="json"))
    ).hexdigest()


def _book_from_raw(payload: bytes) -> BookSnapshot:
    value = load_strict_json(payload)
    if not isinstance(value, Mapping):
        raise ValueError("book artifact must be a JSON object")
    raw_hash = hashlib.sha256(payload).hexdigest()
    return _raw_book_snapshot(payload.decode("utf-8"), raw_hash)


def _reconstruct(
    kind: ArtifactKind, payload: bytes, result: ObservationResult
) -> TypedArtifact:
    try:
        if kind is ArtifactKind.GAMMA_RAW:
            value = load_strict_json(payload)
            if not isinstance(value, Mapping):
                raise ValueError("Gamma artifact must be a JSON object")
            return value
        if kind is ArtifactKind.SANITIZED_EVIDENCE:
            return EvidencePacket.model_validate_json(payload)
        if kind in {ArtifactKind.PRECHECK_BOOK, ArtifactKind.EXECUTION_BOOK}:
            return _book_from_raw(payload)
        if kind is ArtifactKind.RESEARCH_BRIEF:
            return ResearchBrief.model_validate_json(payload)
        if kind is ArtifactKind.JEV_DECISION:
            return JevDecision.model_validate_json(payload)
        if kind is ArtifactKind.OPENAI_RESPONSE:
            response = OpenAIResponse.model_validate_json(payload)
            if (
                response.status != "completed"
                or response.usage is None
                or response.error is not None
                or response.incomplete_details is not None
            ):
                raise ValueError("OpenAI response is not a completed success")
            return response
        if kind is ArtifactKind.JEV_RESPONSE:
            return JevResponse.model_validate_json(payload)
        if kind is ArtifactKind.OBSERVATION_RESULT:
            return ObservationResult.model_validate_json(payload)
        value = load_strict_json(payload)
        if not isinstance(value, Mapping):
            raise ValueError("request artifact must be a JSON object")
        return value
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{kind.value} artifact violates its strict contract"
        ) from error


def _validate_reconstructed(
    result: ObservationResult, typed: dict[str, TypedArtifact], *, corruption: bool
) -> None:
    def fail(message: str) -> None:
        if corruption:
            raise StoreCorruptError(message)
        raise ValueError(message)

    evidence = typed[ArtifactKind.SANITIZED_EVIDENCE]
    precheck = typed[ArtifactKind.PRECHECK_BOOK]
    execution = typed[ArtifactKind.EXECUTION_BOOK]
    brief = typed[ArtifactKind.RESEARCH_BRIEF]
    decision = typed[ArtifactKind.JEV_DECISION]
    openai_response = typed[ArtifactKind.OPENAI_RESPONSE]
    jev_response = typed[ArtifactKind.JEV_RESPONSE]
    terminal = typed[ArtifactKind.OBSERVATION_RESULT]
    if not isinstance(evidence, EvidencePacket):
        fail("sanitized evidence artifact type is invalid")
    if not isinstance(precheck, BookSnapshot) or not isinstance(
        execution, BookSnapshot
    ):
        fail("book artifact type is invalid")
    if not isinstance(brief, ResearchBrief) or not isinstance(decision, JevDecision):
        fail("validated model artifact type is invalid")
    if not isinstance(openai_response, OpenAIResponse) or not isinstance(
        jev_response, JevResponse
    ):
        fail("provider response artifact type is invalid")
    assert isinstance(evidence, EvidencePacket)
    assert isinstance(precheck, BookSnapshot)
    assert isinstance(execution, BookSnapshot)
    assert isinstance(brief, ResearchBrief)
    assert isinstance(decision, JevDecision)
    assert isinstance(openai_response, OpenAIResponse)
    assert isinstance(jev_response, JevResponse)
    if terminal != result:
        fail("terminal result artifact does not match journal result")
    if (
        evidence.market_id != result.market_id
        or evidence.source_sha256 != result.evidence_sha256
    ):
        fail("sanitized evidence does not match observation")
    if any(book.market_id != result.market_id for book in (precheck, execution)):
        fail("book market does not match observation")
    if execution.source_sha256 != result.execution_book_sha256:
        fail("execution book does not match observation hash")
    if brief.evidence_sha256 != result.evidence_sha256:
        fail("research brief does not match observation evidence")
    if jev_response_decision(jev_response) != decision:
        fail("Jev response does not match decision")
    if openai_response_brief(openai_response) != brief:
        fail("OpenAI response does not match research brief")
    openai_request = typed[ArtifactKind.OPENAI_REQUEST]
    jev_request = typed[ArtifactKind.JEV_REQUEST]
    if openai_request != load_strict_json(
        build_openai_request(evidence, OPENAI_MODEL_ID)
    ):
        fail("OpenAI request does not match evidence")
    if jev_request != load_strict_json(
        build_jev_request(brief, evidence, JEV_MODEL_ID)
    ):
        fail("Jev request does not match evidence and brief")


def _prepare(
    result: ObservationResult,
    artifact_refs: Iterable[ArtifactToFreeze],
    forbidden_secret_values: tuple[bytes, ...],
) -> tuple[list[ArtifactToFreeze], list[ArtifactEnvelope], dict[str, TypedArtifact]]:
    artifacts = list(artifact_refs)
    if any(type(item) is not ArtifactToFreeze for item in artifacts):
        raise TypeError("artifact references must be ArtifactToFreeze values")
    kinds = tuple(item.kind for item in artifacts)
    expected = tuple(kind.value for kind in _STAGE_ORDER)
    if kinds != expected:
        raise ValueError(
            "artifact stage order must match the complete reviewed registry"
        )
    envelopes: list[ArtifactEnvelope] = []
    typed: dict[str, TypedArtifact] = {}
    predecessor: str | None = None
    for artifact, kind in zip(artifacts, _STAGE_ORDER, strict=True):
        _screen_payload(artifact.payload, forbidden_secret_values)
        payload_hash = hashlib.sha256(artifact.payload).hexdigest()
        envelope = ArtifactEnvelope(
            kind=kind.value,
            sha256=payload_hash,
            byte_size=len(artifact.payload),
            storage_relpath=f"{payload_hash[:2]}/{payload_hash[2:4]}/{payload_hash}",
            source_uri_label=artifact.source_uri_label,
            retrieved_at=artifact.retrieved_at,
            opportunity_id=result.opportunity_id,
            observation_id=result.observation_id,
            predecessor_stage_sha256=artifact.predecessor_stage_sha256,
        )
        if envelope.predecessor_stage_sha256 != predecessor:
            raise ValueError("artifact predecessor chain is invalid")
        typed[kind.value] = _reconstruct(kind, artifact.payload, result)
        envelopes.append(envelope)
        predecessor = _canonical_stage_hash(envelope)
    for kind, field in _RESULT_HASH_KINDS.items():
        envelope = envelopes[_STAGE_ORDER.index(kind)]
        if envelope.sha256 != getattr(result, field):
            raise ValueError(f"{kind.value} artifact does not match observation hash")
    _validate_reconstructed(result, typed, corruption=False)
    return artifacts, envelopes, typed


def _publish(
    store: BlobStore,
    artifacts: list[ArtifactToFreeze],
    envelopes: list[ArtifactEnvelope],
) -> list[tuple[ArtifactEnvelope, bytes]]:
    frozen: list[tuple[ArtifactEnvelope, bytes]] = []
    for artifact, envelope in zip(artifacts, envelopes, strict=True):
        ref = store.publish(artifact.payload)
        verified = store.read_verified(ref)
        if (
            verified != artifact.payload
            or ref.sha256 != envelope.sha256
            or ref.byte_length != envelope.byte_size
            or ref.relative_path != envelope.storage_relpath
        ):
            raise StoreCorruptError("published artifact failed exact verification")
        frozen.append((envelope, verified))
    return frozen


def _verify_replay_locked(
    store: BlobStore,
    result: ObservationResult,
    envelopes: list[ArtifactEnvelope],
    forbidden_secret_values: tuple[bytes, ...] = (),
) -> ObservationReplay:
    selected = [
        item for item in envelopes if item.observation_id == result.observation_id
    ]
    if tuple(item.kind for item in selected) != tuple(
        kind.value for kind in _STAGE_ORDER
    ):
        raise StoreCorruptError("artifact stage order is incomplete or invalid")
    by_kind = {item.kind: item for item in selected}
    artifacts: dict[str, bytes] = {}
    typed: dict[str, TypedArtifact] = {}
    predecessor: str | None = None
    for envelope, kind in zip(selected, _STAGE_ORDER, strict=True):
        if (
            envelope.opportunity_id != result.opportunity_id
            or envelope.observation_id != result.observation_id
        ):
            raise StoreCorruptError("artifact envelope observation references mismatch")
        if envelope.predecessor_stage_sha256 != predecessor:
            raise StoreCorruptError("artifact predecessor chain is invalid")
        field = _RESULT_HASH_KINDS.get(kind)
        if field is not None and envelope.sha256 != getattr(result, field):
            raise StoreCorruptError("artifact envelope does not match observation hash")
        try:
            payload = store.read_verified(envelope.blob_ref())
            _screen_payload(payload, forbidden_secret_values)
            typed[kind.value] = _reconstruct(kind, payload, result)
        except StoreSecurityError:
            raise
        except (BlobError, TypeError, ValueError) as error:
            raise StoreCorruptError("artifact blob is missing or corrupt") from error
        artifacts[kind.value] = payload
        predecessor = _canonical_stage_hash(envelope)
    _validate_reconstructed(result, typed, corruption=True)
    return ObservationReplay(
        result=result,
        envelopes=by_kind,
        artifacts=artifacts,
        typed_artifacts=typed,
    )


def record_observation(
    output_dir: str | os.PathLike[str],
    result: ObservationResult,
    artifact_refs: Iterable[ArtifactToFreeze],
    *,
    forbidden_secret_values: tuple[bytes, ...] = (),
) -> ObservationResult:
    """Validate all stages, publish blobs, then append envelopes and terminal result."""

    if type(result) is not ObservationResult:
        raise TypeError("result must be an ObservationResult")
    verified_result, result_line = _verified_result(result)
    _screen_payload(result_line, forbidden_secret_values)
    artifacts, envelopes, _typed = _prepare(
        verified_result, artifact_refs, forbidden_secret_values
    )
    envelope_lines = [
        canonical_json_bytes(envelope.model_dump(mode="json")) for envelope in envelopes
    ]
    for line in envelope_lines:
        _screen_payload(line, forbidden_secret_values)
    output = Path(output_dir)
    root_fd = _open_output(output)
    try:
        with BlobStore(output.absolute() / "blobs") as store:
            _publish(store, artifacts, envelopes)
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
                    prior_artifacts = [
                        item
                        for item in existing_envelopes
                        if item.observation_id == verified_result.observation_id
                    ]
                    if existing is not None:
                        if existing[1] != result_line:
                            raise StoreConflictError(
                                "observation identity already has different "
                                "canonical bytes"
                            )
                        if prior_artifacts != envelopes:
                            raise StoreConflictError(
                                "observation identity already has a different "
                                "artifact set"
                            )
                        _verify_replay_locked(
                            store,
                            existing[0],
                            existing_envelopes,
                            forbidden_secret_values,
                        )
                        os.fsync(artifacts_fd)
                        os.fsync(results_fd)
                        return existing[0]
                    if prior_artifacts and prior_artifacts != envelopes:
                        raise StoreConflictError(
                            "artifact stage identity already has different bytes"
                        )
                    if prior_artifacts:
                        _verify_replay_locked(
                            store,
                            verified_result,
                            existing_envelopes,
                            forbidden_secret_values,
                        )
                    else:
                        _append_lines(
                            artifacts_fd, envelope_lines, label="artifact journal"
                        )
                    _append_lines(results_fd, [result_line], label="result journal")
                    return verified_result
                finally:
                    os.close(artifacts_fd)
            finally:
                os.close(results_fd)
    finally:
        os.close(root_fd)


def load_observation(
    output_dir: str | os.PathLike[str],
    observation_id: str,
    *,
    forbidden_secret_values: tuple[bytes, ...] = (),
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
                    return _verify_replay_locked(
                        store, found[0], envelopes, forbidden_secret_values
                    )
                finally:
                    os.close(artifacts_fd)
            finally:
                os.close(results_fd)
    finally:
        os.close(root_fd)

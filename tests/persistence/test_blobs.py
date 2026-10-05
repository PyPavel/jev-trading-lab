from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest


def _api() -> Any:
    from jev_trading_lab.persistence import blobs

    return blobs


def _store(tmp_path: Path, **kwargs: Any) -> Any:
    return _api().BlobStore(tmp_path / "blobs", **kwargs)


def _final_path(store: Any, ref: Any) -> Path:
    return store.root.joinpath(*ref.relative_path.split("/"))


def test_publication_order(tmp_path: Path) -> None:
    events: list[str] = []
    store = _store(tmp_path, failpoint=events.append)

    def reference_writer(ref: Any) -> None:
        assert store.read_verified(ref) == b"provider evidence"
        events.append("reference")

    ref = store.publish_and_reference(b"provider evidence", reference_writer)

    assert events == [
        "after_temp_create",
        "after_temp_write",
        "after_file_fsync",
        "before_rename",
        "after_rename",
        "before_dir_fsync",
        "after_dir_fsync",
        "before_reference",
        "reference",
    ]
    assert ref.sha256 == hashlib.sha256(b"provider evidence").hexdigest()
    assert ref.byte_length == len(b"provider evidence")
    assert ref.relative_path == f"{ref.sha256[:2]}/{ref.sha256[2:4]}/{ref.sha256}"


@pytest.mark.parametrize(
    "boundary",
    (
        "after_temp_create",
        "after_temp_write",
        "after_file_fsync",
        "before_rename",
        "after_rename",
        "before_dir_fsync",
        "after_dir_fsync",
        "before_reference",
    ),
)
def test_failpoints_leave_no_bad_reference(tmp_path: Path, boundary: str) -> None:
    referenced: list[Any] = []

    class SimulatedKill(BaseException):
        pass

    def failpoint(name: str) -> None:
        if name == boundary:
            raise SimulatedKill

    root = tmp_path / "blobs"
    store = _api().BlobStore(root, failpoint=failpoint)
    with pytest.raises(SimulatedKill):
        store.publish_and_reference(b"durable", referenced.append)

    assert referenced == []
    assert not list(root.rglob(".tmp-*"))
    reopened = _api().BlobStore(root)
    finals = [path for path in root.rglob("*") if path.is_file()]
    if finals:
        digest = hashlib.sha256(b"durable").hexdigest()
        ref = _api().BlobRef(
            digest, len(b"durable"), f"{digest[:2]}/{digest[2:4]}/{digest}"
        )
        assert reopened.read_verified(ref) == b"durable"


def test_corrupt_missing_detected(tmp_path: Path) -> None:
    api = _api()
    store = _store(tmp_path)
    ref = store.publish(b"original")
    final = _final_path(store, ref)
    final.write_bytes(b"tampered")
    final.chmod(0o600)

    with pytest.raises(api.BlobCorruptError, match="blob verification failed"):
        store.read_verified(ref)

    final.unlink()
    with pytest.raises(api.BlobMissingError, match="blob is missing"):
        store.read_verified(ref)


def test_structured_envelope_redacts_secrets_before_hash_and_publish(
    tmp_path: Path,
) -> None:
    api = _api()
    store = _store(tmp_path)
    envelope = api.ArtifactEnvelope(
        body=json.dumps(
            {
                "result": "preserve Bearer body-text and api_key=body-text",
                "access_token": "json-secret",
                "nested": {"client_secret": "nested-secret", "value": 7},
            }
        ).encode(),
        url=(
            "https://alice:password@example.test/data?symbol=JEV&X-Amz-Signature=sig"
            "&X-Amz-Credential=credential&api_key=query-secret#fragment-secret"
        ),
        headers={
            "Authorization": "Bearer header-secret",
            "Cookie": "session=cookie-secret",
            "X-Api-Key": "header-api-secret",
            "Content-Type": "application/json",
            "X-Request-Id": "safe-id",
        },
    )

    ref = store.publish(envelope)
    stored = store.read_verified(ref)
    document = json.loads(stored)
    encoded = stored.decode()

    assert ref.sha256 == hashlib.sha256(stored).hexdigest()
    assert (
        document["body"]["result"] == "preserve Bearer body-text and api_key=body-text"
    )
    assert document["body"]["access_token"] == "[REDACTED]"
    assert document["body"]["nested"]["client_secret"] == "[REDACTED]"
    assert document["body"]["nested"]["value"] == 7
    assert document["headers"] == {
        "Authorization": "[REDACTED]",
        "Content-Type": "application/json",
        "Cookie": "[REDACTED]",
        "X-Api-Key": "[REDACTED]",
        "X-Request-Id": "safe-id",
    }
    assert document["url"] == (
        "https://example.test/data?symbol=JEV&X-Amz-Signature=%5BREDACTED%5D"
        "&X-Amz-Credential=%5BREDACTED%5D&api_key=%5BREDACTED%5D"
    )
    for secret in (
        "password",
        "sig",
        "credential",
        "query-secret",
        "fragment-secret",
        "header-secret",
        "cookie-secret",
        "header-api-secret",
        "json-secret",
        "nested-secret",
    ):
        assert secret not in encoded


def test_opaque_bytes_are_preserved_exactly(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = b"Bearer literal evidence; api_key=literal; not a structured secret"

    ref = store.publish(payload)

    assert store.read_verified(ref) == payload


@pytest.mark.parametrize("payload", (bytearray(b"x"), memoryview(b"x"), "x", None))
def test_rejects_non_exact_bytes_payloads(tmp_path: Path, payload: object) -> None:
    store = _store(tmp_path)

    with pytest.raises(TypeError, match="payload must be bytes or ArtifactEnvelope"):
        store.publish(payload)


def test_rejects_oversize_before_creating_files(tmp_path: Path) -> None:
    store = _store(tmp_path, max_bytes=3)

    with pytest.raises(ValueError, match="payload exceeds maximum size"):
        store.publish(b"four")

    assert not [path for path in store.root.rglob("*") if path.is_file()]


def test_short_writes_and_eintr_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api = _api()
    real_write = api.os.write
    calls = 0

    def interrupted_short_write(fd: int, data: bytes | memoryview[Any]) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InterruptedError
        return real_write(fd, data[:2])

    monkeypatch.setattr(api.os, "write", interrupted_short_write)
    store = _store(tmp_path)

    ref = store.publish(b"abcdefgh")

    assert calls >= 5
    assert store.read_verified(ref) == b"abcdefgh"


@pytest.mark.parametrize("error_number", (errno.ENOSPC, errno.EIO))
def test_write_failure_cleans_temp_and_never_references(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error_number: int
) -> None:
    api = _api()
    real_write = api.os.write
    calls = 0

    def failing_write(fd: int, data: bytes | memoryview[Any]) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, data[:1])
        raise OSError(error_number, "secret must not be reflected")

    monkeypatch.setattr(api.os, "write", failing_write)
    referenced: list[Any] = []
    store = _store(tmp_path)

    with pytest.raises(OSError) as raised:
        store.publish_and_reference(b"private-payload", referenced.append)

    assert "private-payload" not in str(raised.value)
    assert referenced == []
    assert not list(store.root.rglob(".tmp-*"))
    assert not [path for path in store.root.rglob("*") if path.is_file()]


def test_dedup_race_accepts_only_verified_existing_blob(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api = _api()
    store = _store(tmp_path)
    payload = b"same bytes"
    first = store.publish(payload)
    real_publish = api._rename_noreplace

    def race(*args: Any, **kwargs: Any) -> None:
        raise FileExistsError(errno.EEXIST, "exists")

    monkeypatch.setattr(api, "_rename_noreplace", race)
    second = store.publish(payload)
    assert second == first
    assert store.read_verified(second) == payload
    assert not list(store.root.rglob(".tmp-*"))

    monkeypatch.setattr(api, "_rename_noreplace", real_publish)
    _final_path(store, first).write_bytes(b"corrupt")
    _final_path(store, first).chmod(0o600)
    monkeypatch.setattr(api, "_rename_noreplace", race)
    with pytest.raises(api.BlobCorruptError):
        store.publish(payload)
    assert _final_path(store, first).read_bytes() == b"corrupt"


def test_reference_failure_leaves_verified_orphan(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def fail_reference(ref: Any) -> None:
        assert store.read_verified(ref) == b"orphan"
        raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        store.publish_and_reference(b"orphan", fail_reference)

    files = [path for path in store.root.rglob("*") if path.is_file()]
    assert len(files) == 1
    assert files[0].read_bytes() == b"orphan"


def test_reference_callback_receives_only_blob_ref(tmp_path: Path) -> None:
    api = _api()
    received: list[Any] = []
    store = _store(tmp_path)

    store.publish_and_reference(b"secret raw bytes", received.append)

    assert len(received) == 1
    assert isinstance(received[0], api.BlobRef)
    assert not hasattr(received[0], "payload")


def test_read_rejects_malformed_ref_symlink_hardlink_and_weak_mode(
    tmp_path: Path,
) -> None:
    api = _api()
    store = _store(tmp_path)
    ref = store.publish(b"secure")
    path = _final_path(store, ref)

    with pytest.raises(ValueError, match="invalid blob reference"):
        api.BlobRef(ref.sha256.upper(), ref.byte_length, ref.relative_path)

    path.chmod(0o640)
    with pytest.raises(api.BlobCorruptError):
        store.read_verified(ref)
    path.chmod(0o600)

    first_shard = store.root / ref.sha256[:2]
    first_shard.chmod(0o755)
    with pytest.raises(api.BlobCorruptError):
        store.read_verified(ref)
    first_shard.chmod(0o700)

    hardlink = tmp_path / "hardlink"
    os.link(path, hardlink)
    with pytest.raises(api.BlobCorruptError):
        store.read_verified(ref)
    hardlink.unlink()

    path.unlink()
    path.symlink_to(tmp_path / "outside")
    with pytest.raises(api.BlobCorruptError):
        store.read_verified(ref)


def test_rejects_nonlocal_filesystem_before_creating_root(tmp_path: Path) -> None:
    api = _api()
    root = tmp_path / "blobs"

    with pytest.raises(api.BlobSecurityError, match="unsupported filesystem"):
        api.BlobStore(root, filesystem_resolver=lambda _path: "nfs")

    assert not root.exists()


def test_rejects_symlinked_root_ancestor(tmp_path: Path) -> None:
    api = _api()
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(api.BlobSecurityError, match="ancestor"):
        api.BlobStore(linked_parent / "blobs", filesystem_resolver=lambda _path: "ext4")

    assert not (real_parent / "blobs").exists()


def test_root_and_shards_are_private_and_symlink_root_rejected(tmp_path: Path) -> None:
    api = _api()
    root = tmp_path / "blobs"
    store = api.BlobStore(root, filesystem_resolver=lambda _path: "ext4")
    ref = store.publish(b"private")

    for directory in (
        root,
        root / ref.sha256[:2],
        root / ref.sha256[:2] / ref.sha256[2:4],
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(_final_path(store, ref).stat().st_mode) == 0o600

    outside = tmp_path / "outside"
    outside.mkdir()
    symlink = tmp_path / "linked-root"
    symlink.symlink_to(outside, target_is_directory=True)
    with pytest.raises(api.BlobSecurityError):
        api.BlobStore(symlink)


def test_scavenge_removes_only_owned_stale_regular_temps(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = store.publish(b"valid")
    shard = _final_path(store, ref).parent
    stale = shard / (".tmp-" + "a" * 32)
    recent = shard / (".tmp-" + "b" * 32)
    symlink = shard / (".tmp-" + "c" * 32)
    unrelated = shard / ".tmp-not-owned"
    stale.write_bytes(b"stale")
    stale.chmod(0o600)
    recent.write_bytes(b"recent")
    recent.chmod(0o600)
    unrelated.write_bytes(b"unrelated")
    symlink.symlink_to(_final_path(store, ref))
    os.utime(stale, (1, 1))

    removed = store.scavenge_stale_temps(older_than_seconds=60, now=100)

    assert removed == 1
    assert not stale.exists()
    assert recent.exists()
    assert symlink.is_symlink()
    assert unrelated.exists()
    assert store.read_verified(ref) == b"valid"


def test_concurrent_same_bytes_converge_and_different_bytes_do_not_overwrite(
    tmp_path: Path,
) -> None:
    root = tmp_path / "blobs"

    def publish(payload: bytes) -> Any:
        return _api().BlobStore(root).publish(payload)

    payloads = [b"same"] * 16 + [f"different-{index}".encode() for index in range(8)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        refs = list(executor.map(publish, payloads))

    same_refs = refs[:16]
    assert len(set(same_refs)) == 1
    assert len(set(refs)) == 9
    store = _api().BlobStore(root)
    assert [store.read_verified(ref) for ref in refs] == payloads
    assert len([path for path in root.rglob("*") if path.is_file()]) == 9


def test_blob_ref_is_frozen_and_validated(tmp_path: Path) -> None:
    api = _api()
    store = _store(tmp_path)
    ref = store.publish(b"immutable")

    with pytest.raises((AttributeError, TypeError)):
        ref.sha256 = "0" * 64
    with pytest.raises(ValueError):
        api.BlobRef("../bad", 1, "../bad")

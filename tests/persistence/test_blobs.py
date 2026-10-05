from __future__ import annotations

import errno
import gc
import hashlib
import json
import os
import stat
import subprocess
import sys
import textwrap
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest


def _api() -> Any:
    from jev_trading_lab.persistence import blobs

    return blobs


@pytest.fixture(autouse=True)
def _local_test_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api()
    defaults = dict(api.BlobStore.__init__.__kwdefaults__ or {})
    defaults["filesystem_resolver"] = lambda _path: "ext4"
    monkeypatch.setattr(api.BlobStore.__init__, "__kwdefaults__", defaults)


def _store(tmp_path: Path, **kwargs: Any) -> Any:
    return _api().BlobStore(tmp_path / "blobs", **kwargs)


def _final_path(store: Any, ref: Any) -> Path:
    return store.root.joinpath(*ref.relative_path.split("/"))


def test_filesystem_boundary_source_policy_is_explicit_and_non_atomic() -> None:
    source = " ".join(Path(_api().__file__).read_text(encoding="utf-8").split())
    delivery = " ".join(
        (Path(__file__).parents[2] / "docs/delivery/task-009.md")
        .read_text(encoding="utf-8")
        .split()
    )
    required = (
        "Concurrent same-user raw filesystem mutation is unsupported "
        "for the entire operation",
        "crash and power-loss durability",
        "documented filesystem semantics",
        "descriptor-relative no-follow validation",
        "fails closed on preexisting or checkpoint-observed state drift",
        "callback runs only after fsync and final canonical-state verification "
        "as observed at that checkpoint",
        "malicious process-wide or private hooks",
    )
    forbidden = (
        "canonical path cannot change after final verification",
        "cannot leave a callback-created dangling reference",
        "cannot leave callback-created dangling reference",
        "prevents bad references",
        "atomic against concurrent same-user filesystem mutation",
    )

    for contract in (source, delivery):
        assert all(claim in contract for claim in required)
        assert all(overclaim not in contract for overclaim in forbidden)


def test_root_owned_sticky_world_writable_ancestor_is_accepted() -> None:
    api = _api()
    system_tmp = Path("/tmp")
    descriptor = os.open(system_tmp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        metadata = api._validate_ancestor_directory(descriptor)
    finally:
        os.close(descriptor)

    assert metadata.st_uid == 0
    assert stat.S_IMODE(metadata.st_mode) == 0o1777


def test_current_owned_sticky_world_writable_ancestor_is_traversal_only(
    tmp_path: Path,
) -> None:
    api = _api()
    sticky = tmp_path / "sticky"
    sticky.mkdir(mode=0o700)
    sticky.chmod(0o1777)

    store = api.BlobStore(
        sticky / "private" / "root", filesystem_resolver=lambda _path: "ext4"
    )
    ref = store.publish(b"private hierarchy")

    assert sticky.stat().st_uid == os.geteuid()
    assert stat.S_IMODE(sticky.stat().st_mode) == 0o1777
    for directory in (
        sticky / "private",
        store.root,
        store.root / ref.sha256[:2],
        _final_path(store, ref).parent,
    ):
        assert directory.stat().st_uid == os.geteuid()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_nonsticky_world_writable_ancestor_and_sticky_blob_root_are_rejected(
    tmp_path: Path,
) -> None:
    api = _api()
    public = tmp_path / "public"
    public.mkdir(mode=0o700)
    public.chmod(0o777)

    with pytest.raises(api.BlobSecurityError, match="ancestor"):
        api.BlobStore(public / "root", filesystem_resolver=lambda _path: "ext4")

    sticky_root = tmp_path / "sticky-root"
    sticky_root.mkdir(mode=0o700)
    sticky_root.chmod(0o1777)
    with pytest.raises(api.BlobSecurityError, match="blob root is not private"):
        api.BlobStore(sticky_root, filesystem_resolver=lambda _path: "ext4")


def test_sticky_ancestor_symlink_is_rejected(tmp_path: Path) -> None:
    api = _api()
    sticky = tmp_path / "sticky"
    sticky.mkdir(mode=0o700)
    sticky.chmod(0o1777)
    real = sticky / "real"
    real.mkdir(mode=0o700)
    linked = sticky / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(api.BlobSecurityError, match="ancestor"):
        api.BlobStore(linked / "root", filesystem_resolver=lambda _path: "ext4")

    assert not (real / "root").exists()


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
    redacted_body = json.loads(__import__("base64").b64decode(document["body_base64"]))
    assert redacted_body["result"] == "preserve Bearer body-text and api_key=body-text"
    assert redacted_body["access_token"] == "[REDACTED]"
    assert redacted_body["nested"]["client_secret"] == "[REDACTED]"
    assert redacted_body["nested"]["value"] == 7
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


def test_filesystem_root_cannot_be_used_as_blob_root() -> None:
    api = _api()

    with pytest.raises(api.BlobSecurityError, match="root"):
        api.BlobStore("/", filesystem_resolver=lambda _path: "ext4")


def test_parent_traversal_component_is_rejected(tmp_path: Path) -> None:
    api = _api()
    lexical = tmp_path / "child" / ".." / "root"

    with pytest.raises(api.BlobSecurityError, match="root"):
        api.BlobStore(lexical, filesystem_resolver=lambda _path: "ext4")

    assert not (tmp_path / "root").exists()


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


def test_multiprocess_same_bytes_deduplicate_to_one_verified_blob(
    tmp_path: Path,
) -> None:
    root = tmp_path / "multiprocess"
    code = textwrap.dedent(
        f"""
        import multiprocessing
        from pathlib import Path
        from jev_trading_lab.persistence.blobs import BlobStore

        root = Path({str(root)!r})
        payload = b'multiprocess-dedup'
        queue = multiprocessing.Queue()

        def publish():
            try:
                store = BlobStore(root, filesystem_resolver=lambda _path: 'ext4')
                queue.put(store.publish(payload).relative_path)
            except BaseException as error:
                queue.put(type(error).__name__ + ':' + str(error))

        processes = [multiprocessing.Process(target=publish) for _ in range(12)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
        assert all(process.exitcode == 0 for process in processes)
        references = [queue.get(timeout=1) for _ in processes]
        assert len(set(references)) == 1, references
        store = BlobStore(root, filesystem_resolver=lambda _path: 'ext4')
        assert store.read_verified(store.publish(payload)) == payload
        assert len([path for path in root.rglob('*') if path.is_file()]) == 1
        """
    )

    result = subprocess.run([sys.executable, "-B", "-c", code], timeout=30, check=False)

    assert result.returncode == 0


def test_blob_ref_is_frozen_and_validated(tmp_path: Path) -> None:
    api = _api()
    store = _store(tmp_path)
    ref = store.publish(b"immutable")

    with pytest.raises((AttributeError, TypeError)):
        ref.sha256 = "0" * 64
    with pytest.raises(ValueError):
        api.BlobRef("../bad", 1, "../bad")


def test_fresh_directory_entries_are_fsynced_in_creation_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api()
    real_fsync = os.fsync
    events: list[str] = []

    def traced_fsync(descriptor: int) -> None:
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        events.append("file" if ".tmp-" in target else Path(target).name)
        real_fsync(descriptor)

    monkeypatch.setattr(api.os, "fsync", traced_fsync)
    store = api.BlobStore(tmp_path / "root")
    ref = store.publish(b"ordered")

    expected_ancestors = [Path((tmp_path / "root").anchor).name]
    expected_ancestors.extend((tmp_path / "root").parts[1:-1])
    assert events == expected_ancestors + [
        "root",
        ref.sha256[:2],
        "file",
        ref.sha256[2:4],
    ]
    reopened = api.BlobStore(tmp_path / "root")
    assert reopened.read_verified(ref) == b"ordered"


def test_preexisting_directories_fsync_each_parent_before_descent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api()
    first = api.BlobStore(tmp_path / "root").publish(b"same-shard")
    real_fsync = os.fsync
    events: list[str] = []

    def traced_fsync(descriptor: int) -> None:
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        if ".tmp-" not in target:
            events.append(Path(target).name)
        real_fsync(descriptor)

    monkeypatch.setattr(api.os, "fsync", traced_fsync)
    second = api.BlobStore(tmp_path / "root").publish(b"same-shard")

    assert second == first
    root_index = events.index("root")
    first_index = events.index(first.sha256[:2], root_index + 1)
    second_index = events.index(first.sha256[2:4], first_index + 1)
    assert root_index < first_index < second_index


def test_existing_child_is_fsynced_before_caller_can_descend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api()
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True, mode=0o700)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    events: list[str] = []
    real_fsync = os.fsync

    def traced_fsync(descriptor: int) -> None:
        events.append("parent-fsync")
        real_fsync(descriptor)

    monkeypatch.setattr(api.os, "fsync", traced_fsync)
    try:
        child_fd, created = api._ensure_child_directory(parent_fd, "child")
        events.append("descend")
    finally:
        if "child_fd" in locals():
            os.close(child_fd)
        os.close(parent_fd)

    assert created is False
    assert events == ["parent-fsync", "descend"]


def test_raced_second_shard_replacement_fails_before_reference(tmp_path: Path) -> None:
    api = _api()
    root = tmp_path / "root"
    called: list[Any] = []
    moved: Path | None = None

    def failpoint(name: str) -> None:
        nonlocal moved
        if name != "before_reference":
            return
        digest = hashlib.sha256(b"replacement-race").hexdigest()
        visible = root / digest[:2] / digest[2:4]
        moved = visible.with_name(visible.name + "-moved")
        visible.rename(moved)
        visible.mkdir(mode=0o700)

    store = api.BlobStore(root, failpoint=failpoint)
    with pytest.raises(api.BlobSecurityError):
        store.publish_and_reference(b"replacement-race", called.append)

    assert moved is not None
    assert called == []


def test_raced_second_shard_mode_drift_fails_before_reference(tmp_path: Path) -> None:
    api = _api()
    root = tmp_path / "root"
    called: list[Any] = []

    def failpoint(name: str) -> None:
        if name == "before_reference":
            digest = hashlib.sha256(b"mode-race").hexdigest()
            (root / digest[:2] / digest[2:4]).chmod(0o755)

    store = api.BlobStore(root, failpoint=failpoint)
    with pytest.raises(api.BlobSecurityError):
        store.publish_and_reference(b"mode-race", called.append)

    assert called == []


def test_nested_existing_symlink_ancestor_is_rejected_lexically(tmp_path: Path) -> None:
    api = _api()
    real = tmp_path / "real"
    existing = real / "existing"
    existing.mkdir(parents=True, mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(api.BlobSecurityError, match="ancestor"):
        api.BlobStore(
            link / "existing" / "new" / "root",
            filesystem_resolver=lambda _path: "ext4",
        )

    assert not (existing / "new").exists()


def test_higher_existing_symlink_ancestor_is_rejected_lexically(tmp_path: Path) -> None:
    api = _api()
    real = tmp_path / "real"
    (real / "one" / "two").mkdir(parents=True, mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(api.BlobSecurityError, match="ancestor"):
        api.BlobStore(
            link / "one" / "two" / "three" / "root",
            filesystem_resolver=lambda _path: "ext4",
        )

    assert not (real / "one" / "two" / "three").exists()


def test_scavenger_directory_replacement_cannot_delete_outside_victim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    ref = store.publish(b"inside")
    first = store.root / ref.sha256[:2]
    outside = tmp_path / "outside"
    outside_second = outside / ref.sha256[2:4]
    outside_second.mkdir(parents=True)
    victim = outside_second / (".tmp-" + "d" * 32)
    victim.write_bytes(b"outside")
    victim.chmod(0o600)
    os.utime(victim, (1, 1))
    replaced = False

    real_open = os.open

    def racing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal replaced
        if path == first.name and kwargs.get("dir_fd") is not None and not replaced:
            replaced = True
            first.rename(first.with_name(first.name + "-old"))
            first.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(_api().BlobSecurityError):
        store.scavenge_stale_temps(older_than_seconds=1, now=100)
    assert replaced
    assert victim.read_bytes() == b"outside"


def test_normalized_redaction_and_envelope_repr_hide_originals(tmp_path: Path) -> None:
    api = _api()
    secrets = ["auth", "proxy", "cookie", "setcookie", "api", "json-a", "json-b"]
    body = b'{ "passWord" : "json-a", "session-token":"json-b", "safe" : 1.00 }'
    envelope = api.ArtifactEnvelope(
        body=body,
        url=(
            "https://example.test/?X-Goog-Signature=goog-secret"
            "&sig=azure-secret&GoogleAccessId=google-id-secret"
        ),
        headers={
            "Auth\uff4frization": secrets[0],
            "Proxy_Auth": secrets[1],
            "COOKIE": secrets[2],
            "Set Cookie": secrets[3],
            "xApiKey": secrets[4],
            "Content-Type": "application/json",
        },
    )
    representation = repr(envelope)
    stored = _store(tmp_path).read_verified(_store(tmp_path).publish(envelope))
    encoded = stored.decode()
    for secret in secrets + ["goog-secret", "azure-secret", "google-id-secret"]:
        assert secret not in representation
        assert secret not in encoded


@pytest.mark.parametrize(
    "name",
    [
        "X-Amz-Algorithm",
        "X-Amz-Credential",
        "X-Amz-Date",
        "X-Amz-Expires",
        "X-Amz-Security-Token",
        "X-Amz-Signature",
        "X-Amz-SignedHeaders",
        "X-Goog-Algorithm",
        "X-Goog-Credential",
        "X-Goog-Date",
        "X-Goog-Expires",
        "X-Goog-Security-Token",
        "X-Goog-Signature",
        "X-Goog-SignedHeaders",
        "GoogleAccessId",
        "sig",
        "se",
        "sv",
        "sp",
        "sr",
        "st",
        "spr",
        "sip",
        "si",
        "Signature",
        "Policy",
        "Key-Pair-Id",
        "token",
        "credential",
        "api-key",
    ],
)
def test_signed_query_fields_are_redacted(tmp_path: Path, name: str) -> None:
    api = _api()
    envelope = api.ArtifactEnvelope(body=b"ok", url=f"https://e.test/?{name}=TOPSECRET")
    stored = _store(tmp_path).read_verified(_store(tmp_path).publish(envelope))
    assert b"TOPSECRET" not in stored


def test_json_redaction_preserves_every_nonsecret_lexical_byte(tmp_path: Path) -> None:
    api = _api()
    body = b'{\n "safe" : "\\u0061", "nested":[{"clientSecret" : "old"}], "n":1.00\n}'
    expected = (
        b'{\n "safe" : "\\u0061", "nested":'
        b'[{"clientSecret" : "[REDACTED]"}], "n":1.00\n}'
    )
    envelope = api.ArtifactEnvelope(
        body=body, headers={"Content-Type": "application/problem+json"}
    )
    stored = _store(tmp_path).read_verified(_store(tmp_path).publish(envelope))
    document = json.loads(stored)
    assert __import__("base64").b64decode(document["body_base64"]) == expected


def test_invalid_declared_json_with_secret_marker_fails_closed(tmp_path: Path) -> None:
    api = _api()
    secret = "DO-NOT-LEAK"
    envelope = api.ArtifactEnvelope(
        body=f'{{"password":"{secret}"'.encode(),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(ValueError) as caught:
        _store(tmp_path).publish(envelope)
    assert secret not in str(caught.value)


def test_invalid_declared_json_with_escaped_secret_marker_fails_closed(
    tmp_path: Path,
) -> None:
    api = _api()
    secret = "ESCAPED-DO-NOT-LEAK"
    envelope = api.ArtifactEnvelope(
        body=f'{{"pass\\u0077ord":"{secret}"'.encode(),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(ValueError, match="declared JSON body is unsafe") as caught:
        _store(tmp_path).publish(envelope)
    assert secret not in str(caught.value)


def test_invalid_declared_json_without_secret_marker_fails_closed(
    tmp_path: Path,
) -> None:
    api = _api()
    body = b'{"ordinary": incomplete'
    envelope = api.ArtifactEnvelope(
        body=body, headers={"Content-Type": "application/json"}
    )
    with pytest.raises(ValueError, match="declared JSON body is unsafe"):
        _store(tmp_path).publish(envelope)


@pytest.mark.parametrize("error_number", [errno.ENOSYS, errno.EOPNOTSUPP])
def test_renameat2_unavailable_is_configuration_error_without_reference_or_poison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_number: int
) -> None:
    api = _api()
    store = _store(tmp_path)
    monkeypatch.setattr(
        api,
        "_rename_noreplace",
        lambda *_args: (_ for _ in ()).throw(OSError(error_number, "x")),
    )
    called: list[Any] = []
    with pytest.raises(api.BlobConfigurationError):
        store.publish_and_reference(b"payload", called.append)
    assert called == []
    assert not list(store.root.rglob(".tmp-*"))
    assert not [path for path in store.root.rglob("*") if path.is_file()]


def test_tmpfs_root_is_rejected_and_resolver_receives_opened_root(
    tmp_path: Path,
) -> None:
    api = _api()
    seen: list[Path] = []
    root = tmp_path / "root"
    with pytest.raises(api.BlobSecurityError, match="unsupported filesystem"):
        api.BlobStore(
            root, filesystem_resolver=lambda path: seen.append(path) or "tmpfs"
        )
    assert len(seen) == 1
    assert str(seen[0]).startswith("/proc/self/fd/")


def test_root_mode_and_identity_drift_fail_closed(tmp_path: Path) -> None:
    api = _api()
    root = tmp_path / "root"
    store = api.BlobStore(root)
    root.chmod(0o755)
    with pytest.raises(api.BlobSecurityError):
        store.publish(b"mode drift")
    root.chmod(0o700)
    moved = tmp_path / "moved"
    root.rename(moved)
    root.mkdir(mode=0o700)
    with pytest.raises(api.BlobSecurityError):
        store.publish(b"identity drift")


def test_root_symlink_swap_fails_closed_and_store_closes(tmp_path: Path) -> None:
    api = _api()
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    store = api.BlobStore(root)
    root.rename(tmp_path / "old")
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(api.BlobSecurityError):
        store.read_verified(api.BlobRef("0" * 64, 0, "00/00/" + "0" * 64))
    store.close()
    store.close()
    with api.BlobStore(tmp_path / "other") as managed:
        managed.publish(b"ok")


def test_fifo_target_never_blocks(tmp_path: Path) -> None:
    code = textwrap.dedent(
        f"""
        from pathlib import Path
        from jev_trading_lab.persistence.blobs import BlobStore, BlobCorruptError
        root = Path({str(tmp_path / "root")!r})
        store = BlobStore(root, filesystem_resolver=lambda _path: 'ext4')
        ref = store.publish(b'fifo')
        target = root / ref.relative_path
        target.unlink()
        target.parent.joinpath(ref.sha256).unlink(missing_ok=True)
        import os
        os.mkfifo(target, 0o600)
        try:
            store.read_verified(ref)
        except BlobCorruptError:
            raise SystemExit(0)
        raise SystemExit(2)
        """
    )
    result = subprocess.run([sys.executable, "-B", "-c", code], timeout=3, check=False)
    assert result.returncode == 0


def test_aggregate_bounds_reject_before_url_or_json_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api()
    store = api.BlobStore(tmp_path / "root", max_bytes=256)
    monkeypatch.setattr(api, "urlsplit", lambda _value: pytest.fail("urlsplit called"))
    monkeypatch.setattr(api.json, "loads", lambda _value: pytest.fail("json called"))
    with pytest.raises(ValueError):
        store.publish(
            api.ArtifactEnvelope(body=b"{}", url="https://e.test/" + "x" * 300)
        )
    with pytest.raises(ValueError):
        store.publish(api.ArtifactEnvelope(body=b"{}", headers={"X" * 300: "v"}))


def test_query_count_and_envelope_memory_are_bounded(tmp_path: Path) -> None:
    api = _api()
    store = api.BlobStore(tmp_path / "root", max_bytes=4096)
    url = "https://e.test/?" + "&".join(f"k{i}=v" for i in range(2000))
    tracemalloc.start()
    try:
        with pytest.raises(ValueError):
            store.publish(api.ArtifactEnvelope(body=b"x", url=url))
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2_000_000


@pytest.mark.parametrize(
    "boundary", ["after_file_fsync", "after_rename", "after_dir_fsync"]
)
def test_real_process_crash_boundaries_reopen_safely(
    tmp_path: Path, boundary: str
) -> None:
    root = tmp_path / boundary
    code = textwrap.dedent(
        f"""
        import os
        from jev_trading_lab.persistence.blobs import BlobStore
        def stop(name):
            if name == {boundary!r}: os._exit(91)
        BlobStore(
            {str(root)!r},
            failpoint=stop,
            filesystem_resolver=lambda _path: 'ext4',
        ).publish(b'crash-data')
        """
    )
    result = subprocess.run([sys.executable, "-B", "-c", code], check=False)
    assert result.returncode == 91
    store = _api().BlobStore(root)
    digest = hashlib.sha256(b"crash-data").hexdigest()
    ref = _api().BlobRef(digest, 10, f"{digest[:2]}/{digest[2:4]}/{digest}")
    if boundary == "after_file_fsync":
        with pytest.raises(_api().BlobMissingError):
            store.read_verified(ref)
    else:
        assert store.read_verified(ref) == b"crash-data"


@pytest.mark.parametrize("operation", ["fsync", "rename", "unlink"])
@pytest.mark.parametrize("error_number", [errno.ENOSPC, errno.EIO])
def test_io_failures_leave_no_reference_or_fd_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, error_number: int
) -> None:
    api = _api()
    store = _store(tmp_path)
    before = len(os.listdir("/proc/self/fd"))
    called: list[Any] = []
    if operation == "fsync":
        monkeypatch.setattr(
            api.os,
            "fsync",
            lambda _fd: (_ for _ in ()).throw(OSError(error_number, "safe")),
        )
    elif operation == "rename":
        monkeypatch.setattr(
            api,
            "_rename_noreplace",
            lambda *_a: (_ for _ in ()).throw(OSError(error_number, "safe")),
        )
    else:
        monkeypatch.setattr(
            api,
            "_rename_noreplace",
            lambda *_a: (_ for _ in ()).throw(FileExistsError()),
        )
        monkeypatch.setattr(
            api.os,
            "unlink",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError(error_number, "safe")),
        )
    with pytest.raises((OSError, api.BlobConfigurationError)) as caught:
        store.publish_and_reference(b"failure", called.append)
    assert "failure" not in str(caught.value)
    assert called == []
    assert len(os.listdir("/proc/self/fd")) == before


def test_root_open_and_filesystem_validation_are_descriptor_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api()
    root = tmp_path / "root"
    real_open = api.os.open
    opens: list[tuple[Any, Any]] = []
    resolved: list[Path] = []

    def traced_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        opens.append((path, kwargs.get("dir_fd")))
        return real_open(path, flags, *args, **kwargs)

    def resolver(path: Path) -> str:
        resolved.append(path)
        assert str(path).startswith("/proc/self/fd/")
        assert os.path.samestat(os.stat(path), root.stat())
        return "ext4"

    monkeypatch.setattr(api.os, "open", traced_open)
    api.BlobStore(root, filesystem_resolver=resolver)
    assert any(path == root.name and dir_fd is not None for path, dir_fd in opens)
    assert len(resolved) == 1


def test_final_envelope_bound_rejects_before_base64_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api()
    store = api.BlobStore(tmp_path / "root", max_bytes=64)
    monkeypatch.setattr(
        api.base64,
        "b64encode",
        lambda _body: pytest.fail("base64 encoding must not run"),
    )
    with pytest.raises(ValueError, match="payload exceeds maximum size"):
        store.publish(api.ArtifactEnvelope(body=b"x" * 48))


def test_query_count_rejects_before_parse_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api()
    store = api.BlobStore(tmp_path / "root")
    monkeypatch.setattr(
        api,
        "parse_qsl",
        lambda *_args, **_kwargs: pytest.fail("parse_qsl must not run"),
    )
    query = "&".join(f"k{i}=v" for i in range(api._MAX_QUERY_FIELDS + 1))
    with pytest.raises(ValueError, match="artifact URL is invalid"):
        store.publish(api.ArtifactEnvelope(body=b"x", url="https://e.test/?" + query))


@pytest.mark.parametrize(
    "name",
    ["X-Amz-Custom-Future-Field", "secret", "password", "cookie"],
)
def test_wildcard_aws_and_common_secret_query_fields_are_redacted(
    tmp_path: Path, name: str
) -> None:
    api = _api()
    store = api.BlobStore(tmp_path / "root")
    envelope = api.ArtifactEnvelope(
        body=b"x", url=f"https://e.test/?{name}=UNIQUE-QUERY-SECRET"
    )
    assert b"UNIQUE-QUERY-SECRET" not in store.read_verified(store.publish(envelope))


def test_scavenger_rejects_weak_shard_without_fd_leak(tmp_path: Path) -> None:
    api = _api()
    store = api.BlobStore(tmp_path / "root")
    ref = store.publish(b"x")
    first = store.root / ref.sha256[:2]
    first.chmod(0o755)
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(3):
        with pytest.raises(api.BlobSecurityError):
            store.scavenge_stale_temps(older_than_seconds=0)
    assert len(os.listdir("/proc/self/fd")) == before


def test_renameat2_symbol_absence_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api()
    store = api.BlobStore(tmp_path / "root")
    monkeypatch.setattr(api.ctypes, "CDLL", lambda *_args, **_kwargs: object())
    called: list[Any] = []
    with pytest.raises(api.BlobConfigurationError, match="renameat2"):
        store.publish_and_reference(b"x", called.append)
    assert called == []
    assert not list(store.root.rglob(".tmp-*"))


@pytest.mark.parametrize(
    "body",
    (
        b'{"bad\\q":0,"pass\\u0077ord":"LEAK-ME"',
        b'{"x":"\\q","pass\\u0077ord":"TOPLEAK"}',
        b'{"safe":"\xff"}',
        b'{"safe":"\\q"}',
        b'{"safe":[{"nested":0}',
    ),
)
def test_any_malformed_declared_json_fails_closed_without_original_bytes(
    tmp_path: Path, body: bytes
) -> None:
    api = _api()
    envelope = api.ArtifactEnvelope(
        body=body, headers={"Content-Type": "application/json"}
    )

    with pytest.raises(ValueError, match="declared JSON body is unsafe") as caught:
        _store(tmp_path).publish(envelope)

    for secret in ("LEAK-ME", "TOPLEAK"):
        assert secret not in str(caught.value)
        assert secret not in repr(caught.value)


@pytest.mark.parametrize(
    "name",
    (
        "client_secret",
        "client-secret",
        "clientSecret",
        "access_token",
        "refresh_token",
        "id_token",
        "code",
    ),
)
def test_oauth_query_fields_are_redacted(tmp_path: Path, name: str) -> None:
    api = _api()
    envelope = api.ArtifactEnvelope(
        body=b"opaque", url=f"https://e.test/callback?{name}=OAUTH-QUERY-SECRET&safe=ok"
    )
    store = _store(tmp_path)
    stored = store.read_verified(store.publish(envelope))

    assert b"OAUTH-QUERY-SECRET" not in stored
    assert b"safe=ok" in stored


@pytest.mark.parametrize(
    "name",
    (
        "Client-Secret",
        "client_secret",
        "clientSecret",
        "Access-Token",
        "access_token",
        "accessToken",
        "Refresh-Token",
        "refresh_token",
        "refreshToken",
    ),
)
def test_oauth_header_fields_are_redacted(tmp_path: Path, name: str) -> None:
    api = _api()
    envelope = api.ArtifactEnvelope(
        body=b"opaque", headers={name: "OAUTH-HEADER-SECRET", "Safe": "keep"}
    )
    store = _store(tmp_path)
    stored = store.read_verified(store.publish(envelope))

    assert b"OAUTH-HEADER-SECRET" not in stored
    assert json.loads(stored)["headers"]["Safe"] == "keep"


@pytest.mark.parametrize(
    "name",
    (
        "oauth_token",
        "oauth-token-secret",
        "oauthToken",
        "providerOAuthTokenSecret",
        "x_oidc_id_token",
        "ＯＡｕｔｈ＿Ｔｏｋｅｎ",
    ),
)
def test_oauth_oidc_query_credential_variants_never_reach_stored_bytes(
    tmp_path: Path, name: str
) -> None:
    api = _api()
    envelope = api.ArtifactEnvelope(
        body=b"opaque",
        url=f"https://e.test/callback?{name}=QUERY-CREDENTIAL-LEAK&safe_name=keep",
    )
    store = _store(tmp_path)
    stored = store.read_verified(store.publish(envelope))

    assert b"QUERY-CREDENTIAL-LEAK" not in stored
    assert b"safe_name=keep" in stored
    assert "QUERY-CREDENTIAL-LEAK" not in repr(envelope)


@pytest.mark.parametrize(
    "name",
    (
        "Id-Token",
        "OAuth-Token",
        "X-Client-Secret",
        "xOAuthToken",
        "X-OIDC-Id-Token",
        "ＯＡｕｔｈ－Ｔｏｋｅｎ",
    ),
)
def test_oauth_oidc_header_credential_variants_never_reach_stored_bytes(
    tmp_path: Path, name: str
) -> None:
    api = _api()
    envelope = api.ArtifactEnvelope(
        body=b"opaque", headers={name: "HEADER-CREDENTIAL-LEAK", "Safe-Name": "keep"}
    )
    store = _store(tmp_path)
    stored = store.read_verified(store.publish(envelope))

    assert b"HEADER-CREDENTIAL-LEAK" not in stored
    assert json.loads(stored)["headers"]["Safe-Name"] == "keep"
    assert "HEADER-CREDENTIAL-LEAK" not in repr(envelope)


@pytest.mark.parametrize(
    "name",
    (
        "token_count",
        "secretary",
        "password_policy",
        "session_duration",
        "cookie_size",
        "code_version",
        "keynote",
    ),
)
def test_harmless_structured_names_remain_exact(tmp_path: Path, name: str) -> None:
    api = _api()
    marker_value = f"safe-{name}"
    body = json.dumps({name: marker_value}, separators=(",", ":")).encode()
    envelope = api.ArtifactEnvelope(
        body=body,
        url=f"https://e.test/?{name}={marker_value}",
        headers={name: marker_value, "Content-Type": "application/json"},
    )
    store = _store(tmp_path)
    document = json.loads(store.read_verified(store.publish(envelope)))
    stored_body = json.loads(__import__("base64").b64decode(document["body_base64"]))

    assert marker_value in document["url"]
    assert document["headers"][name] == marker_value
    assert stored_body[name] == marker_value


@pytest.mark.parametrize(
    "name",
    (
        "possession",
        "session",
        "tokenization",
        "secretariat",
        "passwordPolicy",
        "cookiePreferences",
        "monkey",
        "keyframe",
        "donkey",
        "apikeynote",
        "accessibilityToken",
    ),
)
def test_semantically_harmless_names_remain_exact_on_every_surface(
    tmp_path: Path, name: str
) -> None:
    api = _api()
    marker_value = "SAFE-STRUCTURED-VALUE"
    body = json.dumps({name: marker_value}, separators=(",", ":")).encode()
    envelope = api.ArtifactEnvelope(
        body=body,
        url=f"https://e.test/?{name}={marker_value}",
        headers={name: marker_value, "Content-Type": "application/json"},
    )
    stored = _store(tmp_path).read_verified(_store(tmp_path).publish(envelope))
    document = json.loads(stored)
    stored_body = json.loads(__import__("base64").b64decode(document["body_base64"]))

    assert marker_value in document["url"]
    assert document["headers"][name] == marker_value
    assert stored_body[name] == marker_value


@pytest.mark.parametrize(
    "name",
    (
        "provider_api_key",
        "provider-api-key",
        "providerApiKey",
        "ｐｒｏｖｉｄｅｒ＿ａｐｉ＿ｋｅｙ",
        "OPENAI_API_KEY",
        "openaiApiKey",
        "TYPESAFE_API_KEY",
        "typesafe-api-key",
    ),
)
@pytest.mark.parametrize("surface", ("query", "header", "json"))
def test_provider_prefixed_api_key_is_redacted_on_every_structured_surface(
    tmp_path: Path, name: str, surface: str
) -> None:
    api = _api()
    secret = f"{surface}-PROVIDER-API-KEY-LEAK"
    kwargs: dict[str, Any] = {"body": b"opaque"}
    if surface == "query":
        kwargs["url"] = f"https://e.test/?{name}={secret}&safe=keep"
    elif surface == "header":
        kwargs["headers"] = {name: secret, "Safe": "keep"}
    else:
        kwargs["body"] = json.dumps({name: secret, "safe": "keep"}).encode()
        kwargs["headers"] = {"Content-Type": "application/json"}
    envelope = api.ArtifactEnvelope(**kwargs)

    stored = _store(tmp_path).read_verified(_store(tmp_path).publish(envelope))

    assert secret.encode() not in stored
    assert secret not in repr(envelope)


@pytest.mark.parametrize(
    "surface,name",
    (
        ("query", "session_token"),
        ("header", "X-Session-Token"),
        ("header", "Session-Token"),
        ("json", "sessionToken"),
        ("json", "session_token"),
    ),
)
def test_session_token_is_redacted_on_declared_structured_surfaces(
    tmp_path: Path, surface: str, name: str
) -> None:
    api = _api()
    secret = f"{surface}-SESSION-TOKEN-LEAK"
    kwargs: dict[str, Any] = {"body": b"opaque"}
    if surface == "query":
        kwargs["url"] = f"https://e.test/?{name}={secret}"
    elif surface == "header":
        kwargs["headers"] = {name: secret}
    else:
        kwargs["body"] = json.dumps({name: secret}).encode()
        kwargs["headers"] = {"Content-Type": "application/json"}
    envelope = api.ArtifactEnvelope(**kwargs)

    stored = _store(tmp_path).read_verified(_store(tmp_path).publish(envelope))

    assert secret.encode() not in stored
    assert secret not in repr(envelope)


@pytest.mark.parametrize(
    "name",
    (
        "sessionToken",
        "session_token",
        "provider_api_key",
        "provider-api-key",
        "providerApiKey",
        "ｐｒｏｖｉｄｅｒ＿ａｐｉ＿ｋｅｙ",
        "OPENAI_API_KEY",
        "openaiApiKey",
        "TYPESAFE_API_KEY",
        "typesafe-api-key",
    ),
)
def test_declared_json_credential_errors_never_include_values(
    tmp_path: Path, name: str
) -> None:
    api = _api()
    secret = "DECLARED-JSON-CREDENTIAL-LEAK"
    envelope = api.ArtifactEnvelope(
        body=("{" + json.dumps(name) + ":" + json.dumps(secret)).encode(),
        headers={"Content-Type": "application/json"},
    )

    with pytest.raises(ValueError, match="declared JSON body is unsafe") as caught:
        _store(tmp_path).publish(envelope)

    rendered = str(caught.value) + repr(caught.value)
    assert secret not in rendered
    assert secret not in repr(envelope)


@pytest.mark.parametrize(
    "name",
    (
        "providerAPIKey",
        "ProviderAPIKey",
        "openaiAPIKey",
        "vendorAPIKey",
        "vendorSESSIONToken",
        "vendorACCESSToken",
        "vendorREFRESHToken",
        "vendorIDToken",
        "vendorOAUTHToken",
        "vendorCLIENTSecret",
        "vendorOAUTHTokenSecret",
        "vendorPassword",
        "vendorPassphrase",
        "vendorPrivateKey",
        "vendorSecretKey",
        "vendorSecurityToken",
        "vendorBearerToken",
        "vendorCookie",
        "vendorAuthToken",
        "vendor_api_key_2",
        "vendor.api-key",
        "ｖｅｎｄｏｒＡＰＩＫｅｙ",
    ),
)
@pytest.mark.parametrize("surface", ("query", "header", "json"))
def test_acronym_credentials_are_redacted_on_every_structured_surface(
    tmp_path: Path, name: str, surface: str
) -> None:
    api = _api()
    secret = f"{surface}-ACRONYM-CREDENTIAL-LEAK"
    kwargs: dict[str, Any] = {"body": b"opaque"}
    if surface == "query":
        kwargs["url"] = f"https://e.test/?{name}={secret}&safe=keep"
    elif surface == "header":
        kwargs["headers"] = {name: secret, "Safe": "keep"}
    else:
        kwargs["body"] = json.dumps({name: secret, "safe": "keep"}).encode()
        kwargs["headers"] = {"Content-Type": "application/json"}
    envelope = api.ArtifactEnvelope(**kwargs)

    stored = _store(tmp_path).read_verified(_store(tmp_path).publish(envelope))

    assert secret.encode() not in stored
    assert secret not in repr(envelope)


def test_escaped_json_acronym_credential_key_is_redacted(tmp_path: Path) -> None:
    api = _api()
    secret = "ESCAPED-ACRONYM-CREDENTIAL-LEAK"
    envelope = api.ArtifactEnvelope(
        body=b'{"vendor\\u0041PIKey":"' + secret.encode() + b'","safe":"keep"}',
        headers={"Content-Type": "application/json"},
    )

    stored = _store(tmp_path).read_verified(_store(tmp_path).publish(envelope))

    assert secret.encode() not in stored
    assert secret not in repr(envelope)


@pytest.mark.parametrize(
    "name",
    (
        "providerAPIKey",
        "vendorSESSIONToken",
        "vendorACCESSToken",
        "vendorREFRESHToken",
        "vendorIDToken",
        "vendorOAUTHToken",
        "vendorCLIENTSecret",
        "vendor_api_key_2",
        "vendor.api-key",
        "ｖｅｎｄｏｒＡＰＩＫｅｙ",
        "vendor\\u0041PIKey",
    ),
)
def test_acronym_credential_json_errors_never_include_values(
    tmp_path: Path, name: str
) -> None:
    api = _api()
    secret = "ERROR-ACRONYM-CREDENTIAL-LEAK"
    envelope = api.ArtifactEnvelope(
        body=f'{{"{name}":"{secret}"'.encode(),
        headers={"Content-Type": "application/json"},
    )

    with pytest.raises(ValueError, match="declared JSON body is unsafe") as caught:
        _store(tmp_path).publish(envelope)

    rendered = str(caught.value) + repr(caught.value) + repr(envelope)
    assert secret not in rendered


def test_oauth_body_redaction_and_stored_bytes(tmp_path: Path) -> None:
    api = _api()
    body = (
        b'{"client_secret":"BODY-A","access_token":"BODY-B",'
        b'"refresh_token":"BODY-C","id_token":"BODY-D","code":"keep"}'
    )
    envelope = api.ArtifactEnvelope(
        body=body, headers={"Content-Type": "application/json"}
    )
    store = _store(tmp_path)
    stored = store.read_verified(store.publish(envelope))
    redacted = json.loads(
        __import__("base64").b64decode(json.loads(stored)["body_base64"])
    )

    assert redacted == {
        "client_secret": "[REDACTED]",
        "access_token": "[REDACTED]",
        "refresh_token": "[REDACTED]",
        "id_token": "[REDACTED]",
        "code": "keep",
    }
    assert all(
        secret not in stored for secret in (b"BODY-A", b"BODY-B", b"BODY-C", b"BODY-D")
    )


@pytest.mark.parametrize(
    "envelope",
    (
        lambda api: api.ArtifactEnvelope(
            body=b"x", url="https://e.test/\ud800URL-LEAK"
        ),
        lambda api: api.ArtifactEnvelope(
            body=b"x", headers={"X-\ud800HEADER-LEAK": "v"}
        ),
        lambda api: api.ArtifactEnvelope(body=b"x", headers={"X": "\ud800VALUE-LEAK"}),
        lambda api: api.ArtifactEnvelope(
            body=b'{"password":"\xed\xa0\x80BODY-LEAK"}',
            headers={"Content-Type": "application/json"},
        ),
    ),
)
def test_lone_surrogate_inputs_raise_generic_errors_without_originals(
    tmp_path: Path, envelope: Any
) -> None:
    api = _api()
    value = envelope(api)

    with pytest.raises(ValueError) as caught:
        _store(tmp_path).publish(value)

    rendered = str(caught.value) + repr(caught.value)
    for secret in ("URL-LEAK", "HEADER-LEAK", "VALUE-LEAK", "BODY-LEAK"):
        assert secret not in rendered


def test_nested_root_ancestors_are_fsynced_before_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api()
    existing = tmp_path / "existing"
    existing.mkdir(mode=0o700)
    real_fsync = os.fsync
    events: list[str] = []

    def traced_fsync(descriptor: int) -> None:
        target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if ".tmp-" not in target.name:
            events.append(target.name)
        real_fsync(descriptor)

    monkeypatch.setattr(api.os, "fsync", traced_fsync)
    store = api.BlobStore(existing / "a" / "b" / "root")
    store.publish_and_reference(b"nested", lambda _ref: events.append("callback"))

    assert events.index("existing") < events.index("a") < events.index("b")
    assert events.index("b") < events.index("root") < events.index("callback")


def test_root_replacement_is_never_accepted_during_rapid_recreate_attempts(
    tmp_path: Path,
) -> None:
    api = _api()
    for attempt in range(200):
        parent = tmp_path / f"attempt-{attempt}"
        root = parent / "root"
        store = api.BlobStore(root, filesystem_resolver=lambda _path: "ext4")
        root.rename(parent / "pinned")
        root.mkdir(mode=0o700)
        try:
            with pytest.raises(api.BlobSecurityError):
                store.publish(b"must-not-publish")
            assert not any(root.iterdir())
        finally:
            store.close()


def test_blob_store_close_context_and_finalizer_do_not_leak_descriptors(
    tmp_path: Path,
) -> None:
    api = _api()
    gc.collect()
    before = len(os.listdir("/proc/self/fd"))
    for attempt in range(100):
        store = api.BlobStore(
            tmp_path / f"closed-{attempt}", filesystem_resolver=lambda _path: "ext4"
        )
        store.close()
        store.close()
    for attempt in range(100):
        with api.BlobStore(
            tmp_path / f"context-{attempt}", filesystem_resolver=lambda _path: "ext4"
        ) as store:
            store.publish(b"x")
    for attempt in range(100):
        store = api.BlobStore(
            tmp_path / f"finalized-{attempt}", filesystem_resolver=lambda _path: "ext4"
        )
        del store
    gc.collect()

    assert len(os.listdir("/proc/self/fd")) == before


def test_json_redactor_rejects_oversize_keys_before_decoding() -> None:
    api = _api()
    body = b'{"' + b"a" * (api._MAX_JSON_KEY_BYTES + 1) + b'":0}'
    with pytest.raises(ValueError, match="declared JSON body is unsafe"):
        api._redact_json_bytes(body)


def test_json_redactor_handles_default_maximum_with_linear_memory() -> None:
    api = _api()
    body = b'{"safe":"' + b"a" * (64 * 1024 * 1024 - 32) + b'"}'
    tracemalloc.start()
    try:
        redacted = api._redact_json_bytes(body)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert redacted == body
    assert peak < len(body) * 4


def test_json_redactor_memory_is_bounded_for_one_megabyte_safe_document() -> None:
    api = _api()
    body = b'{"safe":"' + b"a" * (1024 * 1024) + b'","n":1.00}'
    tracemalloc.start()
    try:
        redacted = api._redact_json_bytes(body)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert redacted == body
    assert peak < len(body) * 10

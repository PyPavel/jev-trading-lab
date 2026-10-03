from __future__ import annotations

import dataclasses
import importlib.util
from types import ModuleType

import pytest

HASH_A = "a" * 64
HASH_B = "b" * 64


def _module(name: str) -> ModuleType:
    assert importlib.util.find_spec(name) is not None, f"{name} module must exist"
    return __import__(name, fromlist=["*"])


def _metadata(**changes: object) -> object:
    observations = _module("jev_trading_lab.core.observations")
    fields = {
        "source_uri": "https://example.test/data?id=1",
        "published_at_us": 100,
        "retrieved_at_us": 200,
        "status": observations.ObservationStatus.OK,
        "content_sha256": HASH_A,
        "supersedes_id": None,
    }
    fields.update(changes)
    return observations.ObservationMetadata(**fields)


def test_first_retrieval_is_immutable() -> None:
    original = _metadata()

    with pytest.raises(dataclasses.FrozenInstanceError):
        original.retrieved_at_us = 50  # type: ignore[misc]


def test_corrections_append() -> None:
    observations = _module("jev_trading_lab.core.observations")
    original = _metadata()

    correction = observations.correct_observation(
        original,
        retrieved_at_us=300,
        content_sha256=HASH_B,
        supersedes_id=original.observation_id,
    )

    assert correction.supersedes_id == original.observation_id
    assert correction.retrieved_at_us == 300
    assert correction.content_sha256 == HASH_B
    assert original.retrieved_at_us == 200
    assert original.content_sha256 == HASH_A


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("missing-supersedes", "supersedes_id"),
        ("same-content", "content hash"),
        ("backdated", "earlier"),
    ),
)
def test_invalid_corrections_are_rejected(case: str, message: str) -> None:
    observations = _module("jev_trading_lab.core.observations")
    original = _metadata()
    fields = {
        "retrieved_at_us": 300,
        "content_sha256": HASH_B,
        "supersedes_id": original.observation_id,
    }
    if case == "missing-supersedes":
        fields["supersedes_id"] = None
    elif case == "same-content":
        fields["content_sha256"] = HASH_A
    else:
        fields["retrieved_at_us"] = 199

    with pytest.raises(ValueError, match=message):
        observations.correct_observation(original, **fields)


def test_direct_correction_constructor_requires_predecessor() -> None:
    original = _metadata()

    with pytest.raises(ValueError, match="predecessor"):
        _metadata(
            retrieved_at_us=300,
            content_sha256=HASH_B,
            supersedes_id=original.observation_id,
        )


def test_direct_correction_constructor_rejects_backdating() -> None:
    original = _metadata()

    with pytest.raises(ValueError, match="earlier than or equal"):
        _metadata(
            retrieved_at_us=original.retrieved_at_us,
            content_sha256=HASH_B,
            supersedes_id=original.observation_id,
            predecessor=original,
        )


def test_direct_correction_constructor_rejects_same_content_hash() -> None:
    original = _metadata()

    with pytest.raises(ValueError, match="different content hash"):
        _metadata(
            retrieved_at_us=300,
            content_sha256=original.content_sha256,
            supersedes_id=original.observation_id,
            predecessor=original,
        )


def test_direct_correction_constructor_rejects_unrelated_predecessor() -> None:
    original = _metadata()
    unrelated = _metadata(source_uri="https://example.test/other")

    with pytest.raises(ValueError, match="identify the predecessor"):
        _metadata(
            retrieved_at_us=300,
            content_sha256=HASH_B,
            supersedes_id=original.observation_id,
            predecessor=unrelated,
        )


def test_direct_non_correction_constructor_rejects_predecessor() -> None:
    original = _metadata()

    with pytest.raises(ValueError, match="without supersedes_id"):
        _metadata(predecessor=original)


@pytest.mark.parametrize(
    "source_uri",
    (
        "https://example.test/%zz",
        "https://example.test:invalid/data",
        "https://:443/data",
        "https://user@:443/data",
        "https://example.test/data\x00tail",
        "https://example.test/data\x7ftail",
        "https://[2001:db8::1/data",
        "custom:?query-only",
    ),
)
def test_hostile_source_uris_are_rejected(source_uri: str) -> None:
    with pytest.raises(ValueError, match="source URI"):
        _metadata(source_uri=source_uri)


@pytest.mark.parametrize(
    "source_uri",
    (
        "https://[2001:db8::1]:443/a%20b?value=%2F#ok",
        "urn:isbn:9780140328721",
        "file:///tmp/provider%20payload",
    ),
)
def test_valid_absolute_source_uris_are_accepted(source_uri: str) -> None:
    assert _metadata(source_uri=source_uri) is not None


def test_metadata_validates_required_fields() -> None:
    with pytest.raises(ValueError, match="source URI"):
        _metadata(source_uri="not-a-uri")
    with pytest.raises(ValueError, match="source URI"):
        _metadata(source_uri="https:///missing-host")
    with pytest.raises(ValueError, match="content_sha256"):
        _metadata(content_sha256="A" * 64)
    with pytest.raises(ValueError, match="content_sha256"):
        _metadata(content_sha256="a" * 63)
    with pytest.raises(TypeError, match="status"):
        _metadata(status="ok")
    with pytest.raises(TypeError, match="built-in integer"):
        _metadata(retrieved_at_us=True)


def test_published_time_may_precede_but_not_follow_retrieval() -> None:
    assert _metadata(published_at_us=None) is not None
    assert _metadata(published_at_us=199) is not None

    with pytest.raises(ValueError, match="after retrieved_at_us"):
        _metadata(published_at_us=201)


def test_observation_id_is_stable_and_covers_immutable_fields() -> None:
    first = _metadata()
    second = _metadata()
    changed = _metadata(
        status=_module("jev_trading_lab.core.observations").ObservationStatus.STALE
    )

    assert first.observation_id == second.observation_id
    assert len(first.observation_id) == 64
    assert first.observation_id != changed.observation_id


def test_status_enum_has_exact_contract() -> None:
    observations = _module("jev_trading_lab.core.observations")

    assert {status.value for status in observations.ObservationStatus} == {
        "ok",
        "missing",
        "invalid",
        "stale",
    }

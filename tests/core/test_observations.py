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


def test_revalidation_returns_fresh_semantically_equivalent_observation() -> None:
    observations = _module("jev_trading_lab.core.observations")
    original = _metadata()

    trusted = observations.revalidate_observation(original)

    assert trusted == original
    assert trusted.observation_id == original.observation_id
    assert trusted is not original
    assert not hasattr(trusted, "_constructor_proof")


def test_revalidation_accepts_semantically_valid_forged_exact_wrapper() -> None:
    observations = _module("jev_trading_lab.core.observations")
    forged = object.__new__(observations.ObservationMetadata)
    fields = {
        "source_uri": "https://example.test/forged",
        "published_at_us": None,
        "retrieved_at_us": 200,
        "status": observations.ObservationStatus.OK,
        "content_sha256": HASH_A,
        "supersedes_id": None,
    }
    for name, value in fields.items():
        object.__setattr__(forged, name, value)

    trusted = observations.revalidate_observation(forged)

    assert trusted is not forged
    assert trusted.observation_id == forged.observation_id
    assert tuple(getattr(trusted, name) for name in fields) == tuple(fields.values())


@pytest.mark.parametrize(
    ("attribute", "value"),
    (
        ("source_uri", "http://example.test/x"),
        ("status", "ok"),
        ("content_sha256", "A" * 64),
        ("retrieved_at_us", True),
        ("supersedes_id", HASH_B),
    ),
)
def test_revalidation_rejects_malicious_field_and_proof_mutation(
    attribute: str, value: object
) -> None:
    observations = _module("jev_trading_lab.core.observations")
    observation = _metadata()
    object.__setattr__(observation, attribute, value)

    with pytest.raises((TypeError, ValueError)):
        observations.revalidate_observation(observation)


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
        "ftp://example.test/data",
        "ftp://example.test:invalid/data",
        "ftp:///missing-host",
        "custom:provider-record",
    ),
)
def test_source_uri_requires_exact_lowercase_https_scheme(source_uri: str) -> None:
    with pytest.raises(ValueError, match="source URI"):
        _metadata(source_uri=source_uri)


@pytest.mark.parametrize(
    "source_uri",
    ("HTTPS://example.test/data", "Https://example.test/data"),
)
def test_source_uri_rejects_non_lowercase_https_scheme(source_uri: str) -> None:
    with pytest.raises(ValueError, match="source URI"):
        _metadata(source_uri=source_uri)


@pytest.mark.parametrize(
    "source_uri",
    (
        "https://exa|mple.test/data",
        "https://-example.test/data",
        "https://example-.test/data",
        "https://example..test/data",
        "https://exa_mple.test/data",
        "https://999.1.1.1/data",
        "https://:443/data",
    ),
)
def test_source_uri_rejects_invalid_dns_hosts(source_uri: str) -> None:
    with pytest.raises(ValueError, match="source URI"):
        _metadata(source_uri=source_uri)


@pytest.mark.parametrize(
    "host",
    (
        "127.1",
        "127.0.1",
        "2130706433",
        "0x7f000001",
        "999",
        "0177.0.0.1",
        "127.000.000.001",
        "1.2.3",
        "1.2.3.4.5",
        "0",
        "00",
        "0xffffffff",
        "127.0x0.0.1",
        "example.999",
    ),
)
def test_source_uri_rejects_ambiguous_numeric_hosts(host: str) -> None:
    with pytest.raises(ValueError, match="source URI"):
        _metadata(source_uri=f"https://{host}/data")


@pytest.mark.parametrize(
    "source_uri",
    (
        "https://[fe80::1%eth0]/data",
        "https://[fe80::1%25eth0]/data",
    ),
)
def test_source_uri_rejects_scoped_ipv6_hosts(source_uri: str) -> None:
    with pytest.raises(ValueError, match="source URI"):
        _metadata(source_uri=source_uri)


@pytest.mark.parametrize(
    "source_uri",
    (
        "https://[2001:db8::1/data",
        "https://2001:db8::1/data",
        "https://[2001:db8::gg]/data",
        "https://example.test/path[0]",
    ),
)
def test_source_uri_rejects_malformed_or_misplaced_ipv6_brackets(
    source_uri: str,
) -> None:
    with pytest.raises(ValueError, match="source URI"):
        _metadata(source_uri=source_uri)


@pytest.mark.parametrize(
    "source_uri",
    (
        "https://example.test:invalid/data",
        "https://example.test:/data",
        "https://example.test:0/data",
        "https://example.test:65536/data",
    ),
)
def test_source_uri_rejects_invalid_ports(source_uri: str) -> None:
    with pytest.raises(ValueError, match="source URI"):
        _metadata(source_uri=source_uri)


@pytest.mark.parametrize(
    "source_uri",
    (
        "https://example.test/%",
        "https://example.test/%2",
        "https://example.test/%zz",
        "https://example.test/data?value=%GG",
    ),
)
def test_source_uri_rejects_invalid_percent_escapes(source_uri: str) -> None:
    with pytest.raises(ValueError, match="source URI"):
        _metadata(source_uri=source_uri)


@pytest.mark.parametrize(
    "source_uri",
    (
        "https://user@example.test/data",
        "https://user:secret@example.test/data",
        "https://user@:443/data",
    ),
)
def test_source_uri_rejects_userinfo(source_uri: str) -> None:
    with pytest.raises(ValueError, match="source URI"):
        _metadata(source_uri=source_uri)


@pytest.mark.parametrize(
    "source_uri",
    ("https://example.test/data#section", "https://example.test/data#"),
)
def test_source_uri_rejects_fragments(source_uri: str) -> None:
    with pytest.raises(ValueError, match="source URI"):
        _metadata(source_uri=source_uri)


@pytest.mark.parametrize(
    "character",
    ("<", ">", '"', "{", "}", "|", "\\", "^", "`", "[", "]"),
)
def test_source_uri_rejects_raw_disallowed_path_characters(character: str) -> None:
    with pytest.raises(ValueError, match="source URI"):
        _metadata(source_uri=f"https://example.test/a{character}b")


@pytest.mark.parametrize("character", ("\x00", "\t", " ", "\x7f", "é"))
def test_source_uri_rejects_controls_whitespace_and_non_ascii(character: str) -> None:
    with pytest.raises(ValueError, match="source URI"):
        _metadata(source_uri=f"https://example.test/a{character}b")


@pytest.mark.parametrize(
    "source_uri",
    (
        "https://example.test",
        "https://example.test/a-z_A-Z~!$&'()*+,;=:@/segment?value=ok/?;:@&=+$,",
        "https://example.test/a%20b?value=%2F",
        "https://192.0.2.1/data",
        "https://[2001:db8::1]/data",
        "https://example123.test/data",
        "https://example.test:443/data",
        "https://[2001:db8::1]:8443/data",
    ),
)
def test_valid_https_source_uris_are_accepted(source_uri: str) -> None:
    metadata = _metadata(source_uri=source_uri)

    assert getattr(metadata, "source_uri") == source_uri


def test_source_uri_is_not_silently_normalized() -> None:
    source_uri = "https://EXAMPLE.test:443/%7eitem?Value=%2f"
    metadata = _metadata(source_uri=source_uri)
    normalized = _metadata(source_uri="https://example.test:443/~item?Value=%2F")

    assert getattr(metadata, "source_uri") == source_uri
    assert getattr(metadata, "observation_id") != getattr(normalized, "observation_id")


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

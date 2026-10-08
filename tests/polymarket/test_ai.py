from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.polymarket.ai import (
    AdapterFailure,
    JevSuccess,
    OpenAISuccess,
    TransportResponse,
    build_jev_request,
    build_openai_request,
    call_jev,
    call_openai,
)
from jev_trading_lab.polymarket.models import (
    BookSnapshot,
    EvidencePacket,
    ResearchBrief,
    load_strict_json,
)

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures/polymarket/feasibility_cycle.json"
OPENAI_MODEL = "gpt-5-2026-09-30"
JEV_MODEL = "jev-1-2026-09-15"
CREDENTIAL_SENTINEL = "unit3-secret-never-persist"
FORBIDDEN_NAMES = {
    "ask",
    "asks",
    "bestask",
    "bestbid",
    "bid",
    "bids",
    "book",
    "booksnapshot",
    "midpoint",
    "outcomeprices",
    "price",
    "prices",
    "token",
    "tokenid",
}


@pytest.fixture(scope="module")
def cycle() -> dict[str, Any]:
    loaded = load_strict_json(FIXTURE_PATH.read_bytes())
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture
def evidence(cycle: dict[str, Any]) -> EvidencePacket:
    return EvidencePacket.model_validate_json(
        canonical_json_bytes(cycle["sanitized_evidence"])
    )


@pytest.fixture
def brief(cycle: dict[str, Any]) -> ResearchBrief:
    return ResearchBrief.model_validate_json(
        canonical_json_bytes(cycle["research_brief"])
    )


class FixtureTransport:
    def __init__(
        self,
        body: bytes,
        *,
        status_code: int = 200,
        billed_cost_microdollars: int = 0,
        error: BaseException | None = None,
    ) -> None:
        self.body = body
        self.status_code = status_code
        self.billed_cost_microdollars = billed_cost_microdollars
        self.error = error
        self.calls: list[bytes] = []

    def __call__(self, request_bytes: bytes) -> TransportResponse:
        self.calls.append(request_bytes)
        if self.error is not None:
            raise self.error
        return TransportResponse(
            status_code=self.status_code,
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {CREDENTIAL_SENTINEL}",
            },
            raw_bytes=self.body,
            retrieved_at=datetime(2026, 10, 3, 12, 0, 30, tzinfo=UTC),
            billed_cost_microdollars=self.billed_cost_microdollars,
        )


def _normalized_name(name: str) -> str:
    return "".join(character for character in name.lower() if character.isalnum())


def _assert_no_forbidden_names(value: object) -> None:
    if isinstance(value, dict):
        assert not ({_normalized_name(key) for key in value} & FORBIDDEN_NAMES)
        for nested in value.values():
            _assert_no_forbidden_names(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_forbidden_names(nested)


def _replace_json(raw: str, mutate: Any) -> bytes:
    value = json.loads(raw)
    mutate(value)
    return canonical_json_bytes(value)


def test_openai_request_is_canonical_strict_schema_without_tools_or_prices(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    request_bytes = build_openai_request(evidence, OPENAI_MODEL)
    request = load_strict_json(request_bytes)

    assert request_bytes == canonical_json_bytes(request)
    assert set(request) == {"input", "model", "response_format"}
    assert "tools" not in request
    assert request["model"] == OPENAI_MODEL
    assert request["input"] == (
        "<evidence>"
        + canonical_json_bytes(evidence.model_dump(mode="json")).decode()
        + "</evidence>"
    )
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["strict"] is True
    assert (
        request["response_format"]["json_schema"]["schema"]["additionalProperties"]
        is False
    )
    _assert_no_forbidden_names(request)
    assert request_bytes == cycle["model_artifacts"]["openai_request_bytes"].encode()
    assert (
        hashlib.sha256(request_bytes).hexdigest()
        == cycle["expected"]["artifact_sha256"]["openai_request_bytes"]
    )


def test_openai_canned_response_maps_to_expected_brief_in_one_call(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief
) -> None:
    response_bytes = cycle["model_artifacts"]["openai_response_bytes"].encode()
    transport = FixtureTransport(
        response_bytes,
        billed_cost_microdollars=cycle["model_costs_microdollars"]["openai"],
    )

    result = call_openai(evidence, OPENAI_MODEL, transport)

    assert isinstance(result, OpenAISuccess)
    assert result.brief == brief
    assert (
        result.request_bytes
        == cycle["model_artifacts"]["openai_request_bytes"].encode()
    )
    assert result.response_bytes == response_bytes
    assert (
        hashlib.sha256(result.response_bytes).hexdigest()
        == cycle["expected"]["artifact_sha256"]["openai_response_bytes"]
    )
    assert result.billed_cost_microdollars == 375_000
    assert transport.calls == [result.request_bytes]
    assert CREDENTIAL_SENTINEL not in repr(result)


def test_jev_request_is_exact_price_free_state_and_matches_fixture(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief
) -> None:
    request_bytes = build_jev_request(brief, evidence, JEV_MODEL)
    request = load_strict_json(request_bytes)

    assert request_bytes == canonical_json_bytes(request)
    assert set(request) == {"model", "state", "questions"}
    assert set(request["state"]) == {
        "brief",
        "evidence_sha256",
        "research_brief_sha256",
    }
    assert request["questions"] == [
        "evidence_sufficiency",
        "ambiguity",
        "p_yes",
        "action (BUY_YES|BUY_NO|ABSTAIN)",
        "confidence",
    ]
    _assert_no_forbidden_names(request)
    assert request_bytes == cycle["model_artifacts"]["jev_request_bytes"].encode()
    assert (
        hashlib.sha256(request_bytes).hexdigest()
        == cycle["expected"]["artifact_sha256"]["jev_request_bytes"]
    )


def test_jev_request_rejects_book_snapshot_at_runtime(
    cycle: dict[str, Any], brief: ResearchBrief
) -> None:
    book = BookSnapshot.model_validate_json(
        canonical_json_bytes(cycle["execution_book"])
    )

    with pytest.raises(TypeError, match="EvidencePacket"):
        build_jev_request(brief, book, JEV_MODEL)  # type: ignore[arg-type]


def test_jev_canned_response_maps_to_expected_decision_in_one_call(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief
) -> None:
    response_bytes = cycle["model_artifacts"]["jev_response_bytes"].encode()
    transport = FixtureTransport(
        response_bytes,
        billed_cost_microdollars=cycle["model_costs_microdollars"]["jev"],
    )

    result = call_jev(brief, evidence, JEV_MODEL, transport)

    assert isinstance(result, JevSuccess)
    assert result.decision.model_dump(mode="json") == cycle["jev_decision"]
    assert (
        result.request_bytes == cycle["model_artifacts"]["jev_request_bytes"].encode()
    )
    assert result.response_bytes == response_bytes
    assert (
        hashlib.sha256(result.response_bytes).hexdigest()
        == cycle["expected"]["artifact_sha256"]["jev_response_bytes"]
    )
    assert result.billed_cost_microdollars == 125_000
    assert transport.calls == [result.request_bytes]
    assert CREDENTIAL_SENTINEL not in repr(result)


@pytest.mark.parametrize("provider", ("openai", "jev"))
@pytest.mark.parametrize("raw", (b"not-json", b'{"model":"truncated"'))
def test_malformed_or_truncated_json_is_explicit_parse_failure(
    cycle: dict[str, Any],
    evidence: EvidencePacket,
    brief: ResearchBrief,
    provider: str,
    raw: bytes,
) -> None:
    transport = FixtureTransport(raw)

    result = (
        call_openai(evidence, OPENAI_MODEL, transport)
        if provider == "openai"
        else call_jev(brief, evidence, JEV_MODEL, transport)
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"
    assert result.response_bytes == raw
    assert len(transport.calls) == 1


@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_duplicate_json_is_explicit_parse_failure(
    cycle: dict[str, Any],
    evidence: EvidencePacket,
    brief: ResearchBrief,
    provider: str,
) -> None:
    raw = cycle["model_artifacts"][f"{provider}_response_bytes"]
    raw = raw.replace('"model":', '"model":"duplicate","model":', 1).encode()
    transport = FixtureTransport(raw)

    result = (
        call_openai(evidence, OPENAI_MODEL, transport)
        if provider == "openai"
        else call_jev(brief, evidence, JEV_MODEL, transport)
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"
    assert len(transport.calls) == 1


@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_response_extra_keys_are_explicit_parse_failures(
    cycle: dict[str, Any],
    evidence: EvidencePacket,
    brief: ResearchBrief,
    provider: str,
) -> None:
    raw = _replace_json(
        cycle["model_artifacts"][f"{provider}_response_bytes"],
        lambda value: value.update(unexpected=True),
    )
    transport = FixtureTransport(raw)

    result = (
        call_openai(evidence, OPENAI_MODEL, transport)
        if provider == "openai"
        else call_jev(brief, evidence, JEV_MODEL, transport)
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"


@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_wrong_model_is_explicit_model_failure(
    cycle: dict[str, Any],
    evidence: EvidencePacket,
    brief: ResearchBrief,
    provider: str,
) -> None:
    raw = _replace_json(
        cycle["model_artifacts"][f"{provider}_response_bytes"],
        lambda value: value.update(model="wrong-model"),
    )
    transport = FixtureTransport(raw)

    result = (
        call_openai(evidence, OPENAI_MODEL, transport)
        if provider == "openai"
        else call_jev(brief, evidence, JEV_MODEL, transport)
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "MODEL_MISMATCH"


def test_openai_invalid_citation_is_explicit_failure(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    raw = _replace_json(
        cycle["model_artifacts"]["openai_response_bytes"],
        lambda value: value["output"].update(evidence_sha256="0" * 64),
    )
    value = json.loads(raw)
    for citation in value["output"]["cited_facts"]:
        citation["evidence_sha256"] = "0" * 64
    transport = FixtureTransport(
        canonical_json_bytes(value), billed_cost_microdollars=375_000
    )

    result = call_openai(evidence, OPENAI_MODEL, transport)

    assert isinstance(result, AdapterFailure)
    assert result.code == "CITATION_ERROR"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("p_yes_micros", 1_000_001),
        ("confidence_micros", -1),
        ("sufficiency_micros", 1_000_001),
        ("ambiguity_micros", -1),
        ("action", "HOLD"),
    ),
)
def test_jev_range_and_action_failures_are_explicit(
    cycle: dict[str, Any],
    evidence: EvidencePacket,
    brief: ResearchBrief,
    field: str,
    value: object,
) -> None:
    raw = _replace_json(
        cycle["model_artifacts"]["jev_response_bytes"],
        lambda response: response["result"].update({field: value}),
    )
    transport = FixtureTransport(raw, billed_cost_microdollars=125_000)

    result = call_jev(brief, evidence, JEV_MODEL, transport)

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"


def test_jev_action_question_cannot_omit_abstain(
    evidence: EvidencePacket, brief: ResearchBrief
) -> None:
    request = load_strict_json(build_jev_request(brief, evidence, JEV_MODEL))

    assert "ABSTAIN" in request["questions"][3]


@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_http_failure_is_explicit_and_called_once(
    cycle: dict[str, Any],
    evidence: EvidencePacket,
    brief: ResearchBrief,
    provider: str,
) -> None:
    transport = FixtureTransport(b'{"error":"unavailable"}', status_code=503)

    result = (
        call_openai(evidence, OPENAI_MODEL, transport)
        if provider == "openai"
        else call_jev(brief, evidence, JEV_MODEL, transport)
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "HTTP_ERROR"
    assert result.status_code == 503
    assert len(transport.calls) == 1


@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_timeout_is_explicit_called_once_and_sanitized(
    evidence: EvidencePacket, brief: ResearchBrief, provider: str
) -> None:
    transport = FixtureTransport(
        b"", error=TimeoutError(f"timeout with {CREDENTIAL_SENTINEL}")
    )

    result = (
        call_openai(evidence, OPENAI_MODEL, transport)
        if provider == "openai"
        else call_jev(brief, evidence, JEV_MODEL, transport)
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "TIMEOUT"
    assert result.response_bytes == b""
    assert len(transport.calls) == 1
    assert CREDENTIAL_SENTINEL not in repr(result)
    assert CREDENTIAL_SENTINEL not in result.message


@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_billed_cost_must_match_strict_provider_envelope(
    cycle: dict[str, Any],
    evidence: EvidencePacket,
    brief: ResearchBrief,
    provider: str,
) -> None:
    raw = cycle["model_artifacts"][f"{provider}_response_bytes"].encode()
    transport = FixtureTransport(raw, billed_cost_microdollars=1)

    result = (
        call_openai(evidence, OPENAI_MODEL, transport)
        if provider == "openai"
        else call_jev(brief, evidence, JEV_MODEL, transport)
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "COST_MISMATCH"
    assert result.billed_cost_microdollars == 1
    assert len(transport.calls) == 1


def test_openai_rejects_forbidden_schema_name_used_as_nested_value(
    evidence: EvidencePacket,
) -> None:
    unsafe = evidence.model_copy(update={"tags": ["token_id"]})

    with pytest.raises(ValueError, match="forbidden"):
        build_openai_request(unsafe, OPENAI_MODEL)


def test_openai_mismatched_individual_citation_is_citation_failure(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    value = json.loads(cycle["model_artifacts"]["openai_response_bytes"])
    value["output"]["cited_facts"][0]["evidence_sha256"] = "0" * 64
    transport = FixtureTransport(
        canonical_json_bytes(value), billed_cost_microdollars=375_000
    )

    result = call_openai(evidence, OPENAI_MODEL, transport)

    assert isinstance(result, AdapterFailure)
    assert result.code == "CITATION_ERROR"

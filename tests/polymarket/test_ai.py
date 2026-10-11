from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import pytest

from jev_trading_lab.core.ids import canonical_json_bytes
from jev_trading_lab.polymarket.ai import (
    JEV_MODEL_ID,
    OPENAI_MODEL_ID,
    AdapterFailure,
    JevSuccess,
    OpenAISuccess,
    TransportResponse,
    _contains_credentials,
    build_jev_request,
    build_openai_request,
    call_jev,
    call_openai,
)
from jev_trading_lab.polymarket.models import (
    BookSnapshot,
    EvidencePacket,
    OpenAIResponse,
    ResearchBrief,
    load_strict_json,
)

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures/polymarket/feasibility_cycle.json"
CREDENTIAL_SENTINEL = "unit3-secret-never-persist"
INT64_MAX = 2**63 - 1
FORBIDDEN_NAMES = {
    "ask",
    "asks",
    "bestask",
    "bestbid",
    "bid",
    "bids",
    "book",
    "booksnapshot",
    "impliedprobability",
    "marketprice",
    "midpoint",
    "outcomeprices",
    "price",
    "prices",
    "token",
    "tokenid",
    "yesprice",
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


_DEFAULT_RESPONSE = object()


class FixtureTransport:
    def __init__(
        self,
        body: bytes,
        *,
        status_code: int = 200,
        billed_cost_microdollars: int = 0,
        error: BaseException | None = None,
        headers: object = None,
        response: object = _DEFAULT_RESPONSE,
        sensitive_values: tuple[bytes, ...] = (),
    ) -> None:
        self.body = body
        self.status_code = status_code
        self.billed_cost_microdollars = billed_cost_microdollars
        self.error = error
        self.headers = (
            {
                "content-type": "application/json",
                "authorization": f"Bearer {CREDENTIAL_SENTINEL}",
                "set-cookie": f"session={CREDENTIAL_SENTINEL}",
            }
            if headers is None
            else headers
        )
        self.response = response
        self.sensitive_values = sensitive_values
        self.calls: list[bytes] = []

    def __call__(self, request_bytes: bytes) -> TransportResponse:
        self.calls.append(request_bytes)
        if self.error is not None:
            raise self.error
        if self.response is not _DEFAULT_RESPONSE:
            return self.response  # type: ignore[return-value]
        return TransportResponse(
            status_code=self.status_code,
            headers=self.headers,  # type: ignore[arg-type]
            raw_bytes=self.body,
            retrieved_at=datetime(2026, 10, 3, 12, 0, 30, tzinfo=UTC),
            billed_cost_microdollars=self.billed_cost_microdollars,
            sensitive_values=self.sensitive_values,
        )


def _normalized_name(name: str) -> str:
    import unicodedata

    return "".join(
        character
        for character in unicodedata.normalize("NFKC", name).casefold()
        if character.isalnum()
    )


def _assert_no_forbidden_names(value: object) -> None:
    if isinstance(value, dict):
        assert not ({_normalized_name(str(key)) for key in value} & FORBIDDEN_NAMES)
        for nested in value.values():
            _assert_no_forbidden_names(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_forbidden_names(nested)
    elif isinstance(value, str) and value.strip().startswith(("{", "[")):
        try:
            decoded = load_strict_json(value)
        except ValueError:
            return
        _assert_no_forbidden_names(decoded)


def _wire_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def _replace_json(raw: str | bytes, mutate: Any) -> bytes:
    value = json.loads(raw)
    mutate(value)
    return _wire_json(value)


def _openai_body(cycle: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(cycle["model_artifacts"]["openai_response_bytes"])
    assert isinstance(value, dict)
    return value


def _jev_body(cycle: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(cycle["model_artifacts"]["jev_response_bytes"])
    assert isinstance(value, dict)
    return value


def _openai_message(response: dict[str, Any]) -> dict[str, Any]:
    return next(item for item in response["output"] if item["type"] == "message")


def _openai_text(response: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(_openai_message(response)["content"][0]["text"])
    assert isinstance(value, dict)
    return value


def _set_openai_text(response: dict[str, Any], value: object) -> None:
    _openai_message(response)["content"][0]["text"] = canonical_json_bytes(
        value
    ).decode()


def _choice_confidence(probabilities: dict[str, float]) -> float:
    count = len(probabilities)
    return max(0.0, min(1.0, (count * max(probabilities.values()) - 1) / (count - 1)))


def _score_confidence(probabilities: dict[str, float]) -> float:
    ordered = [probabilities[str(index)] for index in range(len(probabilities))]
    maximum = max(ordered)
    winners = [
        index for index, probability in enumerate(ordered) if probability == maximum
    ]
    assert len(winners) == 1
    mode = winners[0]
    spread = sum(
        probability * abs(index - mode) for index, probability in enumerate(ordered)
    )
    even_spread = sum(
        abs(index - (len(ordered) - 1) / 2) for index in range(len(ordered))
    ) / len(ordered)
    return max(0.0, min(1.0, 1 - spread / even_spread))


def _call(
    provider: str,
    cycle: dict[str, Any],
    evidence: EvidencePacket,
    brief: ResearchBrief,
    transport: Any,
) -> OpenAISuccess | JevSuccess | AdapterFailure:
    if provider == "openai":
        return call_openai(evidence, OPENAI_MODEL_ID, transport)
    return call_jev(brief, evidence, JEV_MODEL_ID, transport)


def test_openai_request_uses_responses_text_format_and_frozen_input(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    request_bytes = build_openai_request(evidence, OPENAI_MODEL_ID)
    request = load_strict_json(request_bytes)

    assert request_bytes == canonical_json_bytes(request)
    assert set(request) == {"input", "model", "text"}
    assert "tools" not in request
    assert "response_format" not in request
    assert request["model"] == OPENAI_MODEL_ID
    assert request["input"] == (
        "<evidence>"
        + canonical_json_bytes(evidence.model_dump(mode="json")).decode()
        + "</evidence>"
    )
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
    assert request["text"]["format"]["schema"]["additionalProperties"] is False
    _assert_no_forbidden_names(request)
    assert request_bytes == cycle["model_artifacts"]["openai_request_bytes"].encode()
    assert (
        hashlib.sha256(request_bytes).hexdigest()
        == cycle["expected"]["artifact_sha256"]["openai_request_bytes"]
    )


def test_openai_raw_responses_fixture_maps_one_output_text_to_brief(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief
) -> None:
    response_bytes = cycle["model_artifacts"]["openai_response_bytes"].encode()
    transport = FixtureTransport(response_bytes, billed_cost_microdollars=375_000)

    result = call_openai(evidence, OPENAI_MODEL_ID, transport)

    assert isinstance(result, OpenAISuccess)
    assert result.brief == brief
    assert result.response_bytes == response_bytes
    assert result.billed_cost_microdollars == 375_000
    assert transport.calls == [result.request_bytes]
    assert CREDENTIAL_SENTINEL not in repr(result)


def test_jev_request_uses_typed_question_map_and_exact_pinned_model(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief
) -> None:
    request_bytes = build_jev_request(brief, evidence, JEV_MODEL_ID)
    request = load_strict_json(request_bytes)

    assert request_bytes == canonical_json_bytes(request)
    assert set(request) == {"model", "questions", "state"}
    assert request["model"] == "jev-1.13.0"
    assert set(request["questions"]) == {
        "evidence_sufficient",
        "materially_ambiguous",
        "p_yes",
        "action",
        "confidence",
    }
    assert request["questions"]["evidence_sufficient"]["type"] == "noul"
    assert request["questions"]["materially_ambiguous"]["type"] == "noul"
    assert request["questions"]["p_yes"]["type"] == "score"
    assert request["questions"]["confidence"]["type"] == "score"
    assert request["questions"]["action"]["type"] == "choice"
    assert set(request["questions"]["action"]["criteria"]) == {
        "BUY_YES",
        "BUY_NO",
        "ABSTAIN",
    }
    assert all(question["instructions"] for question in request["questions"].values())
    _assert_no_forbidden_names(request)
    assert request_bytes == cycle["model_artifacts"]["jev_request_bytes"].encode()


def test_jev_raw_systemone_fixture_maps_answers_to_expected_decision(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief
) -> None:
    response_bytes = cycle["model_artifacts"]["jev_response_bytes"].encode()
    response = _jev_body(cycle)
    transport = FixtureTransport(response_bytes, billed_cost_microdollars=125_000)

    result = call_jev(brief, evidence, JEV_MODEL_ID, transport)

    assert isinstance(result, JevSuccess)
    assert result.decision.model_dump(mode="json") == cycle["jev_decision"]
    assert result.decision.confidence_micros == round(
        _choice_confidence(response["answers"]["action"]["probabilities"]) * 1_000_000
    )
    assert result.response_bytes == response_bytes
    assert result.billed_cost_microdollars == 125_000
    assert transport.calls == [result.request_bytes]
    assert CREDENTIAL_SENTINEL not in repr(result)


def test_jev_fixture_confidences_use_official_provider_formulas(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief
) -> None:
    response = _jev_body(cycle)
    answers = response["answers"]

    assert answers["action"]["confidence"] == pytest.approx(
        _choice_confidence(answers["action"]["probabilities"])
    )
    for answer_id in ("p_yes", "confidence"):
        answer = answers[answer_id]
        assert answer["confidence"] == pytest.approx(
            _score_confidence(answer["probabilities"])
        )

    for answer_id in ("action", "p_yes", "confidence"):
        for mutated_confidence in (0.0, 0.123):
            mutated = _jev_body(cycle)
            mutated["answers"][answer_id]["confidence"] = mutated_confidence
            result = call_jev(
                brief,
                evidence,
                JEV_MODEL_ID,
                FixtureTransport(_wire_json(mutated)),
            )
            assert isinstance(result, AdapterFailure)
            assert result.code == "PARSE_ERROR"


@pytest.mark.parametrize(
    ("provider", "model"),
    (
        ("openai", "gpt-5-latest"),
        ("openai", "gpt-5-preview"),
        ("openai", "gpt-5-2026-09-29"),
        ("jev", "jev-latest"),
        ("jev", "jev-preview"),
        ("jev", "jev-1.12.0"),
    ),
)
def test_unapproved_or_moving_model_is_input_failure_before_transport(
    provider: str,
    model: str,
    evidence: EvidencePacket,
    brief: ResearchBrief,
) -> None:
    transport = FixtureTransport(b"never sent")

    result = (
        call_openai(evidence, model, transport)
        if provider == "openai"
        else call_jev(brief, evidence, model, transport)
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "INPUT_ERROR"
    assert result.request_bytes == b""
    assert transport.calls == []


def test_direct_builders_reject_unapproved_models(
    evidence: EvidencePacket, brief: ResearchBrief
) -> None:
    with pytest.raises(ValueError, match="approved pinned"):
        build_openai_request(evidence, "gpt-5-latest")
    with pytest.raises(ValueError, match="approved pinned"):
        build_jev_request(brief, evidence, "jev-preview")


@pytest.mark.parametrize(
    "residual",
    (
        "The API key is 'AlphabeticSecret' in the report.",
        "The API key is 'AlphabeticSecret",
        "Authorization: Bearer AlphabeticSecret",
        "client_secret=abc:def/ghi",
        "Basic QWxwaGEvKz0=",
    ),
)
def test_openai_input_fails_closed_on_residual_credential_semantics(
    evidence: EvidencePacket, residual: str
) -> None:
    unsafe = evidence.model_copy(update={"description": residual})
    transport = FixtureTransport(b"never sent")

    with pytest.raises(ValueError, match="credential"):
        build_openai_request(unsafe, OPENAI_MODEL_ID)
    result = call_openai(unsafe, OPENAI_MODEL_ID, transport)

    assert isinstance(result, AdapterFailure)
    assert result.code == "INPUT_ERROR"
    assert result.request_bytes == b""
    assert transport.calls == []


def test_jev_request_rejects_book_snapshot_at_runtime(
    cycle: dict[str, Any], brief: ResearchBrief
) -> None:
    book = BookSnapshot.model_validate_json(
        canonical_json_bytes(cycle["execution_book"])
    )

    with pytest.raises(TypeError, match="EvidencePacket"):
        build_jev_request(brief, book, JEV_MODEL_ID)  # type: ignore[arg-type]

    transport = FixtureTransport(b"never sent")
    result = call_jev(brief, book, JEV_MODEL_ID, transport)  # type: ignore[arg-type]
    assert isinstance(result, AdapterFailure)
    assert result.code == "INPUT_ERROR"
    assert transport.calls == []


@pytest.mark.parametrize(
    "text",
    (
        "Ordinary price discussion is allowed in prose.",
        "A token can be an ordinary word, as can bid and ask.",
    ),
)
def test_prose_words_are_not_treated_as_structured_leakage(
    evidence: EvidencePacket, text: str
) -> None:
    safe = evidence.model_copy(update={"description": text})
    build_openai_request(safe, OPENAI_MODEL_ID)


@pytest.mark.parametrize(
    "embedded",
    (
        '{"market_price":0.5}',
        '{"yes_price":0.5}',
        '{"implied_probability":0.5}',
        '{"bestBid":0.5}',
        '{"token_id":"secret"}',
        '{"ｍａｒｋｅｔ＿ｐｒｉｃｅ":0.5}',
        '[{"ＴＯＫＥＮ＿ＩＤ":"secret"}]',
    ),
)
def test_embedded_json_strings_are_scanned_for_normalized_field_names(
    evidence: EvidencePacket, embedded: str
) -> None:
    unsafe = evidence.model_copy(update={"tags": [embedded]})

    with pytest.raises(ValueError, match="forbidden"):
        build_openai_request(unsafe, OPENAI_MODEL_ID)


@pytest.mark.parametrize(
    "delimiter",
    ("<evidence>", "</evidence>", "＜ｅｖｉｄｅｎｃｅ＞", "＜／ｅｖｉｄｅｎｃｅ＞"),
)
def test_evidence_prose_cannot_inject_frozen_delimiters(
    evidence: EvidencePacket, delimiter: str
) -> None:
    unsafe = evidence.model_copy(update={"description": f"safe {delimiter} injected"})

    with pytest.raises(ValueError, match="delimiter"):
        build_openai_request(unsafe, OPENAI_MODEL_ID)

    result = call_openai(unsafe, OPENAI_MODEL_ID, FixtureTransport(b"never sent"))
    assert isinstance(result, AdapterFailure)
    assert result.code == "INPUT_ERROR"


@pytest.mark.parametrize(
    "embedded",
    (
        'prefix ｛"price":0.5｝ suffix',
        'prefix ｛"ＰＲＩＣＥ":0.5｝ suffix',
        'prefix ［｛"safe":｛"MarketPrice":0.5｝｝］ suffix',
        'prefix ｛"safe":"x","Ｔｏｋｅｎ＿Ｉｄ":"secret"｝ suffix',
    ),
)
def test_nfkc_json_delimiters_and_field_aliases_cannot_bypass_leakage_scan(
    evidence: EvidencePacket, embedded: str
) -> None:
    unsafe = evidence.model_copy(update={"description": embedded})

    with pytest.raises(ValueError, match="forbidden"):
        build_openai_request(unsafe, OPENAI_MODEL_ID)


def test_ordinary_unicode_prose_remains_accepted(evidence: EvidencePacket) -> None:
    safe = evidence.model_copy(
        update={"description": "政策讨论： café, Δ概率，以及日本語の説明。"}
    )

    build_openai_request(safe, OPENAI_MODEL_ID)


@pytest.mark.parametrize("provider", ("openai", "jev"))
@pytest.mark.parametrize("status_code", (200, 500))
def test_empty_response_retains_canonical_empty_body_hash(
    cycle: dict[str, Any],
    evidence: EvidencePacket,
    brief: ResearchBrief,
    provider: str,
    status_code: int,
) -> None:
    result = _call(
        provider,
        cycle,
        evidence,
        brief,
        FixtureTransport(b"", status_code=status_code),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == ("PARSE_ERROR" if status_code == 200 else "HTTP_ERROR")
    assert result.response_sha256 == hashlib.sha256(b"").hexdigest()
    assert result.response_byte_length == 0


@pytest.mark.parametrize("provider", ("openai", "jev"))
@pytest.mark.parametrize("raw", (b"not-json", b'{"model":"truncated"'))
def test_malformed_json_is_sanitized_parse_failure(
    cycle: dict[str, Any],
    evidence: EvidencePacket,
    brief: ResearchBrief,
    provider: str,
    raw: bytes,
) -> None:
    transport = FixtureTransport(raw)
    result = _call(provider, cycle, evidence, brief, transport)

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"
    assert result.response_sha256 == hashlib.sha256(raw).hexdigest()
    assert result.response_byte_length == len(raw)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_duplicate_json_is_sanitized_parse_failure(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief, provider: str
) -> None:
    raw = cycle["model_artifacts"][f"{provider}_response_bytes"]
    raw = raw.replace('"model":', '"model":"duplicate","model":', 1).encode()
    result = _call(provider, cycle, evidence, brief, FixtureTransport(raw))

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"
    assert result.response_sha256 == hashlib.sha256(raw).hexdigest()
    assert result.response_byte_length == len(raw)


@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_unknown_response_fields_are_parse_failures(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief, provider: str
) -> None:
    raw = _replace_json(
        cycle["model_artifacts"][f"{provider}_response_bytes"],
        lambda value: value.update(unexpected=True),
    )
    result = _call(provider, cycle, evidence, brief, FixtureTransport(raw))

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"


@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_wrong_response_model_is_explicit_model_failure(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief, provider: str
) -> None:
    raw = _replace_json(
        cycle["model_artifacts"][f"{provider}_response_bytes"],
        lambda value: value.update(model="wrong-model"),
    )
    result = _call(provider, cycle, evidence, brief, FixtureTransport(raw))

    assert isinstance(result, AdapterFailure)
    assert result.code == "MODEL_MISMATCH"


@pytest.mark.parametrize(
    "mutate",
    (
        lambda r: r.update(status="incomplete"),
        lambda r: r["output"].append(_openai_message(r).copy()),
        lambda r: r["output"].append({"type": "function_call", "name": "tool"}),
        lambda r: _openai_message(r).update(status="in_progress"),
        lambda r: _openai_message(r)["content"].clear(),
        lambda r: _openai_message(r)["content"].append(
            {"type": "refusal", "refusal": "no"}
        ),
        lambda r: _openai_message(r)["content"].append(
            _openai_message(r)["content"][0].copy()
        ),
        lambda r: _openai_message(r)["content"][0]["annotations"].append(
            {"type": "url_citation"}
        ),
        lambda r: _openai_message(r)["content"][0]["logprobs"].append({"token": "x"}),
    ),
)
def test_openai_rejects_noncompleted_refusal_tool_or_nonunique_output_text(
    cycle: dict[str, Any], evidence: EvidencePacket, mutate: Any
) -> None:
    response = _openai_body(cycle)
    mutate(response)
    result = call_openai(
        evidence,
        OPENAI_MODEL_ID,
        FixtureTransport(_wire_json(response), billed_cost_microdollars=375_000),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"


def test_openai_mismatched_individual_citation_is_citation_failure(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    response = _openai_body(cycle)
    output = _openai_text(response)
    output["cited_facts"][0]["evidence_sha256"] = "0" * 64
    _set_openai_text(response, output)

    result = call_openai(
        evidence,
        OPENAI_MODEL_ID,
        FixtureTransport(_wire_json(response), billed_cost_microdollars=375_000),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "CITATION_ERROR"


def test_openai_invalid_citation_is_explicit_failure(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    response = _openai_body(cycle)
    output = _openai_text(response)
    output["evidence_sha256"] = "0" * 64
    for citation in output["cited_facts"]:
        citation["evidence_sha256"] = "0" * 64
    _set_openai_text(response, output)

    result = call_openai(
        evidence,
        OPENAI_MODEL_ID,
        FixtureTransport(_wire_json(response), billed_cost_microdollars=375_000),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "CITATION_ERROR"


@pytest.mark.parametrize(
    ("answer_id", "field", "value"),
    (
        ("evidence_sufficient", "noul", 1.1),
        ("materially_ambiguous", "noul", -0.1),
        ("p_yes", "score", float("nan")),
        ("p_yes", "score", 1.1),
        ("confidence", "score", -0.1),
        ("action", "choice", "HOLD"),
        ("action", "confidence", 1.1),
    ),
)
def test_jev_answer_bounds_and_action_are_strict(
    cycle: dict[str, Any],
    evidence: EvidencePacket,
    brief: ResearchBrief,
    answer_id: str,
    field: str,
    value: object,
) -> None:
    response = _jev_body(cycle)
    response["answers"][answer_id][field] = value
    raw = json.dumps(response, allow_nan=True, separators=(",", ":")).encode()
    result = call_jev(
        brief,
        evidence,
        JEV_MODEL_ID,
        FixtureTransport(raw, billed_cost_microdollars=125_000),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"


@pytest.mark.parametrize(
    "mutate",
    (
        lambda r: r["answers"].pop("confidence"),
        lambda r: r["answers"].update(extra={"type": "noul", "noul": 0.5}),
        lambda r: r["answers"]["action"]["probabilities"].pop("ABSTAIN"),
        lambda r: r["answers"]["action"]["probabilities"].update(HOLD=0.0),
        lambda r: r["answers"]["action"]["probabilities"].update(
            BUY_YES=0.5, BUY_NO=0.5, ABSTAIN=0.5
        ),
        lambda r: r["answers"]["p_yes"]["legend"].update({"1": "detached rubric"}),
        lambda r: r["answers"]["p_yes"].update(type="noul"),
        lambda r: r["usage"].update(input_tokens=True),
        lambda r: r["usage"].update(output_tokens=-1),
        lambda r: r["usage"].update(extra=1),
    ),
)
def test_jev_requires_exact_answer_ids_types_distributions_rubrics_and_usage(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief, mutate: Any
) -> None:
    response = _jev_body(cycle)
    mutate(response)
    result = call_jev(
        brief,
        evidence,
        JEV_MODEL_ID,
        FixtureTransport(
            json.dumps(response, separators=(",", ":"), sort_keys=True).encode(),
            billed_cost_microdollars=125_000,
        ),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"


@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_non_2xx_body_and_headers_never_enter_failure(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief, provider: str
) -> None:
    secret_body = (
        f'{{"error":"Bearer {CREDENTIAL_SENTINEL}; '
        f'Cookie: session={CREDENTIAL_SENTINEL}; api_key={CREDENTIAL_SENTINEL}"}}'
    ).encode()
    result = _call(
        provider,
        cycle,
        evidence,
        brief,
        FixtureTransport(secret_body, status_code=503, billed_cost_microdollars=7),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "CREDENTIAL_REFLECTION"
    assert result.status_code == 503
    assert result.response_sha256 == hashlib.sha256(secret_body).hexdigest()
    assert result.response_byte_length == len(secret_body)
    assert result.billed_cost_microdollars == 7
    assert CREDENTIAL_SENTINEL not in repr(result)
    assert CREDENTIAL_SENTINEL not in result.message


@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_timeout_is_one_call_and_sanitized(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief, provider: str
) -> None:
    transport = FixtureTransport(
        b"", error=TimeoutError(f"timeout with {CREDENTIAL_SENTINEL}")
    )
    result = _call(provider, cycle, evidence, brief, transport)

    assert isinstance(result, AdapterFailure)
    assert result.code == "TIMEOUT"
    assert result.response_sha256 is None
    assert result.response_byte_length == 0
    assert len(transport.calls) == 1
    assert CREDENTIAL_SENTINEL not in repr(result)


@pytest.mark.parametrize(
    "malformed",
    (
        None,
        object(),
        {"status_code": 200},
        TransportResponse(
            status_code=True,
            headers={},
            raw_bytes=b"{}",
            retrieved_at=datetime(2026, 10, 3, tzinfo=UTC),
            billed_cost_microdollars=0,
        ),
        TransportResponse(
            status_code=200,
            headers=[],  # type: ignore[arg-type]
            raw_bytes=b"{}",
            retrieved_at=datetime(2026, 10, 3, tzinfo=UTC),
            billed_cost_microdollars=0,
        ),
        TransportResponse(
            status_code=200,
            headers={"x": 1},  # type: ignore[dict-item]
            raw_bytes=b"{}",
            retrieved_at=datetime(2026, 10, 3, tzinfo=UTC),
            billed_cost_microdollars=0,
        ),
        TransportResponse(
            status_code=200,
            headers={},
            raw_bytes=bytearray(b"{}"),  # type: ignore[arg-type]
            retrieved_at=datetime(2026, 10, 3, tzinfo=UTC),
            billed_cost_microdollars=0,
        ),
        TransportResponse(
            status_code=200,
            headers={},
            raw_bytes=b"{}",
            retrieved_at=datetime(2026, 10, 3),
            billed_cost_microdollars=0,
        ),
        TransportResponse(
            status_code=200,
            headers={},
            raw_bytes=b"{}",
            retrieved_at=datetime(2026, 10, 3, tzinfo=UTC),
            billed_cost_microdollars=INT64_MAX + 1,
        ),
    ),
)
@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_malformed_transport_objects_become_provider_failure(
    cycle: dict[str, Any],
    evidence: EvidencePacket,
    brief: ResearchBrief,
    provider: str,
    malformed: object,
) -> None:
    result = _call(
        provider,
        cycle,
        evidence,
        brief,
        FixtureTransport(b"", response=malformed),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PROVIDER_ERROR"
    assert result.response_sha256 is None
    assert result.response_byte_length == 0
    assert result.billed_cost_microdollars is None
    assert result.status_code is None


@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_transport_cost_is_authoritative_exact_int64(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief, provider: str
) -> None:
    raw = cycle["model_artifacts"][f"{provider}_response_bytes"].encode()
    result = _call(
        provider,
        cycle,
        evidence,
        brief,
        FixtureTransport(raw, billed_cost_microdollars=INT64_MAX),
    )

    assert isinstance(result, OpenAISuccess | JevSuccess)
    assert result.billed_cost_microdollars == INT64_MAX


def _overflow_evidence(provider: str) -> Any:
    from jev_trading_lab.polymarket.ai import CostOverflowEvidence

    if provider == "openai":
        return CostOverflowEvidence(
            provider="openai",
            input_tokens=10**13,
            cached_input_tokens=4,
            output_tokens=3,
            input_rate=10**12,
            cached_input_rate=10**11,
            output_rate=10**12,
        )
    return CostOverflowEvidence(
        provider="jev",
        input_tokens=10**13,
        cached_input_tokens=None,
        output_tokens=3,
        input_rate=10**12,
        cached_input_rate=None,
        output_rate=10**12,
    )


def _overflow_transport(
    provider: str,
    raw: bytes,
    *,
    cost: int | None = None,
    evidence: Any = None,
    status_code: int = 200,
) -> FixtureTransport:
    return FixtureTransport(
        raw,
        response=TransportResponse(
            status_code=status_code,
            headers={"content-type": "application/json"},
            raw_bytes=raw,
            retrieved_at=datetime(2026, 10, 3, 12, 0, 30, tzinfo=UTC),
            billed_cost_microdollars=cost,  # type: ignore[arg-type]
            sensitive_values=(CREDENTIAL_SENTINEL.encode(),),
            cost_overflow=(
                _overflow_evidence(provider) if evidence is None else evidence
            ),
        ),
    )


@pytest.mark.parametrize("provider", ("openai", "jev"))
@pytest.mark.parametrize("status_code", (200, 503))
def test_finding_6_cost_overflow_is_a_typed_adapter_result_not_provider_error(
    cycle: dict[str, Any],
    evidence: EvidencePacket,
    brief: ResearchBrief,
    provider: str,
    status_code: int,
) -> None:
    from jev_trading_lab.polymarket.ai import AdapterCostOverflow

    raw = cycle["model_artifacts"][f"{provider}_response_bytes"].encode()
    transport = _overflow_transport(provider, raw, status_code=status_code)

    result = _call(provider, cycle, evidence, brief, transport)

    assert isinstance(result, AdapterCostOverflow)
    assert result.provider == provider
    assert result.status_code == status_code
    assert result.response_sha256 == hashlib.sha256(raw).hexdigest()
    assert result.response_byte_length == len(raw)
    assert result.retrieved_at == datetime(2026, 10, 3, 12, 0, 30, tzinfo=UTC)
    assert result.evidence == _overflow_evidence(provider)
    assert result.evidence.exact_cost_microdollars() > INT64_MAX
    assert result.request_bytes == transport.calls[0]
    assert len(transport.calls) == 1
    assert raw.decode() not in repr(result)
    assert CREDENTIAL_SENTINEL not in repr(result)


@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_finding_6_cost_overflow_precedes_credential_reflection_and_never_keeps_body(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief, provider: str
) -> None:
    from jev_trading_lab.polymarket.ai import AdapterCostOverflow

    reflected = f'{{"error":"Bearer {CREDENTIAL_SENTINEL}"}}'.encode()
    transport = _overflow_transport(provider, reflected)

    result = _call(provider, cycle, evidence, brief, transport)

    assert isinstance(result, AdapterCostOverflow)
    assert result.response_sha256 == hashlib.sha256(reflected).hexdigest()
    assert CREDENTIAL_SENTINEL not in repr(result)
    assert not hasattr(result, "response_bytes") and not hasattr(result, "raw_bytes")


_COMMON_OVERFLOW_MUTATIONS = (
    "missing_evidence",
    "cost_and_evidence",
    "non_overflowing_evidence",
    "wrong_provider",
    "negative_tokens",
    "rate_out_of_range",
)
_OVERFLOW_MUTATIONS = [
    (provider, mutation)
    for provider in ("openai", "jev")
    for mutation in _COMMON_OVERFLOW_MUTATIONS
] + [("openai", "cached_exceeds_input"), ("jev", "jev_with_cached_fields")]


@pytest.mark.parametrize(("provider", "mutation"), _OVERFLOW_MUTATIONS)
def test_finding_6_inconsistent_overflow_metadata_is_provider_error(
    cycle: dict[str, Any],
    evidence: EvidencePacket,
    brief: ResearchBrief,
    provider: str,
    mutation: str,
) -> None:
    from dataclasses import replace

    raw = cycle["model_artifacts"][f"{provider}_response_bytes"].encode()
    good = _overflow_evidence(provider)
    if mutation == "missing_evidence":
        transport = FixtureTransport(
            raw,
            response=TransportResponse(
                status_code=200,
                headers={},
                raw_bytes=raw,
                retrieved_at=datetime(2026, 10, 3, tzinfo=UTC),
                billed_cost_microdollars=None,  # type: ignore[arg-type]
            ),
        )
    elif mutation == "cost_and_evidence":
        transport = _overflow_transport(provider, raw, cost=7)
    elif mutation == "non_overflowing_evidence":
        transport = _overflow_transport(
            provider, raw, evidence=replace(good, input_tokens=1)
        )
    elif mutation == "wrong_provider":
        other = "jev" if provider == "openai" else "openai"
        transport = _overflow_transport(
            provider, raw, evidence=_overflow_evidence(other)
        )
    elif mutation == "negative_tokens":
        transport = _overflow_transport(
            provider, raw, evidence=replace(good, output_tokens=-1)
        )
    elif mutation == "cached_exceeds_input":
        transport = _overflow_transport(
            provider, raw, evidence=replace(good, cached_input_tokens=10**14)
        )
    elif mutation == "rate_out_of_range":
        transport = _overflow_transport(
            provider, raw, evidence=replace(good, input_rate=10**12 + 1)
        )
    else:
        transport = _overflow_transport(
            provider, raw, evidence=replace(good, cached_input_tokens=1)
        )

    result = _call(provider, cycle, evidence, brief, transport)

    assert isinstance(result, AdapterFailure)
    assert result.code == "PROVIDER_ERROR"
    assert result.billed_cost_microdollars is None


@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_hardening_deeply_nested_provider_body_is_a_typed_failure_after_billing(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief, provider: str
) -> None:
    body = b"[" * 100_000 + b"]" * 100_000

    result = _call(
        provider,
        cycle,
        evidence,
        brief,
        FixtureTransport(body, billed_cost_microdollars=9),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"
    assert result.billed_cost_microdollars == 9
    assert result.response_sha256 == hashlib.sha256(body).hexdigest()


def test_hardening_deeply_nested_brief_text_is_a_typed_failure_after_billing(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    response = _openai_body(cycle)
    _openai_message(response)["content"][0]["text"] = "[" * 5_000 + "]" * 5_000

    result = call_openai(
        evidence,
        OPENAI_MODEL_ID,
        FixtureTransport(_wire_json(response), billed_cost_microdollars=11),
    )

    assert isinstance(result, AdapterFailure)
    assert result.billed_cost_microdollars == 11


def test_score_to_micros_uses_deterministic_half_up_rounding(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief
) -> None:
    response = _jev_body(cycle)
    response["answers"]["p_yes"]["score"] = 0.7000005
    response["answers"]["p_yes"]["probabilities"] = {
        "0": 0.2999995,
        "1": 0.7000005,
    }
    response["answers"]["p_yes"]["confidence"] = _score_confidence(
        response["answers"]["p_yes"]["probabilities"]
    )
    result = call_jev(
        brief,
        evidence,
        JEV_MODEL_ID,
        FixtureTransport(
            json.dumps(response, separators=(",", ":"), sort_keys=True).encode()
        ),
    )

    assert isinstance(result, JevSuccess)
    assert result.decision.p_yes_micros == 700_001


def _official_openai_response(cycle: dict[str, Any]) -> dict[str, Any]:
    fixture = _openai_body(cycle)
    return {
        "access_programs": None,
        "created_at": fixture["created_at"],
        "id": fixture["id"],
        "model": fixture["model"],
        "object": fixture["object"],
        "output": fixture["output"],
        "parallel_tool_calls": fixture["parallel_tool_calls"],
        "status": fixture["status"],
        "tool_choice": fixture["tool_choice"],
        "tools": fixture["tools"],
        "usage": fixture["usage"],
    }


def _openai_optional_fields(cycle: dict[str, Any]) -> dict[str, object]:
    return {
        "background": False,
        "completed_at": 1_780_488_031,
        "conversation": None,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "max_tool_calls": None,
        "metadata": {},
        "moderation": None,
        "previous_response_id": None,
        "prompt": None,
        "prompt_cache_diagnostics": None,
        "prompt_cache_key": None,
        "prompt_cache_options": None,
        "prompt_cache_retention": "in_memory",
        "reasoning": None,
        "safety_identifier": None,
        "service_tier": "fast",
        "temperature": None,
        "text": None,
        "top_logprobs": None,
        "top_p": None,
        "truncation": None,
        "user": None,
    }


def _official_message(response: dict[str, Any]) -> dict[str, Any]:
    return next(item for item in response["output"] if item["type"] == "message")


def test_openai_accepts_exact_official_completed_envelope_with_reasoning_content(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief
) -> None:
    response = _official_openai_response(cycle)
    _official_message(response)["phase"] = "final_answer"
    response["output"][0].update(
        content=[
            {"type": "reasoning_text", "text": "Never derive the brief from this."}
        ],
        encrypted_content="opaque-reasoning-payload",
        status="incomplete",
    )

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
    )

    assert isinstance(result, OpenAISuccess)
    assert result.brief == brief


@pytest.mark.parametrize("field", ("status", "usage"))
def test_openai_optional_wire_status_and_usage_validate_but_call_requires_them(
    cycle: dict[str, Any], evidence: EvidencePacket, field: str
) -> None:
    response = _official_openai_response(cycle)
    response.pop(field)

    envelope = OpenAIResponse.model_validate(response)
    assert getattr(envelope, field) is None

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"


def test_openai_rejects_missing_required_output_text_annotations(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    response = _official_openai_response(cycle)
    _official_message(response)["content"][0].pop("annotations")

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"


def test_openai_rejects_missing_required_cache_write_tokens(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    response = _official_openai_response(cycle)
    response["usage"]["input_tokens_details"].pop("cache_write_tokens")

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"


@pytest.mark.parametrize("synthetic", ("store", "output_text"))
def test_openai_rejects_synthetic_sdk_only_response_fields(
    cycle: dict[str, Any], evidence: EvidencePacket, synthetic: str
) -> None:
    response = _official_openai_response(cycle)
    response[synthetic] = None

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"


@pytest.mark.parametrize(
    "mutate",
    (
        lambda r: r.update(created_at=True),
        lambda r: r.update(object="chat.completion"),
        lambda r: r.update(error={"message": "failed"}),
        lambda r: r.update(incomplete_details={"reason": "max_output_tokens"}),
        lambda r: r["output"].append(
            {"type": "function_call", "id": "call_1", "name": "x"}
        ),
        lambda r: _official_message(r)["content"].append(
            {"type": "refusal", "refusal": "no"}
        ),
    ),
)
def test_openai_official_envelope_rejects_invalid_or_unapproved_outputs(
    cycle: dict[str, Any], evidence: EvidencePacket, mutate: Any
) -> None:
    response = _official_openai_response(cycle)
    mutate(response)

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"


@pytest.mark.parametrize(
    "embedded",
    (
        'prefix {"market_price":0.5} suffix',
        'prefix [{"nested":{"ＴＯＫＥＮ＿ＩＤ":"secret"}}] suffix',
        '{"safe":"prefix {\\"bestBid\\":0.5} suffix"}',
        'safe [1,{"outer":{"yes_price":0.5}}] trailing',
    ),
)
def test_embedded_json_is_found_at_any_offset_and_recursively_scanned(
    evidence: EvidencePacket, embedded: str
) -> None:
    unsafe = evidence.model_copy(update={"description": embedded})

    with pytest.raises(ValueError, match="forbidden"):
        build_openai_request(unsafe, OPENAI_MODEL_ID)


def test_embedded_json_scanner_is_explicitly_bounded(evidence: EvidencePacket) -> None:
    unsafe = evidence.model_copy(update={"description": "x" * 20_001})

    with pytest.raises(ValueError, match="scan limit"):
        build_openai_request(unsafe, OPENAI_MODEL_ID)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda r: r["answers"]["p_yes"].update(score=0.2),
        lambda r: r["answers"]["confidence"].update(score=0.2),
        lambda r: r["answers"]["action"].update(choice="BUY_NO"),
        lambda r: r["answers"]["action"].update(
            choice="BUY_YES",
            probabilities={"BUY_YES": 0.4, "BUY_NO": 0.4, "ABSTAIN": 0.2},
        ),
        lambda r: r["answers"]["p_yes"].update(
            score=0.5, probabilities={"0": 0.5, "1": 0.5}, confidence=0.0
        ),
        lambda r: r["answers"]["confidence"].update(
            score=0.5, probabilities={"0": 0.5, "1": 0.5}, confidence=0.0
        ),
    ),
)
def test_jev_rejects_contradictory_scores_choices_and_ties(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief, mutate: Any
) -> None:
    response = _jev_body(cycle)
    mutate(response)

    result = call_jev(
        brief,
        evidence,
        JEV_MODEL_ID,
        FixtureTransport(_wire_json(response)),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"


@pytest.mark.parametrize("provider", ("openai", "jev"))
@pytest.mark.parametrize(
    ("body", "sensitive_values"),
    (
        (b'{"error":"Bearer reflected-secret"}', ()),
        (b'{"error":"opaque-reflected-secret"}', (b"opaque-reflected-secret",)),
    ),
)
def test_credential_reflection_discards_2xx_body_and_preserves_digest_metadata(
    cycle: dict[str, Any],
    evidence: EvidencePacket,
    brief: ResearchBrief,
    provider: str,
    body: bytes,
    sensitive_values: tuple[bytes, ...],
) -> None:
    result = _call(
        provider,
        cycle,
        evidence,
        brief,
        FixtureTransport(
            body,
            billed_cost_microdollars=11,
            sensitive_values=sensitive_values,
        ),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "CREDENTIAL_REFLECTION"
    assert result.response_sha256 == hashlib.sha256(body).hexdigest()
    assert result.response_byte_length == len(body)
    assert result.status_code == 200
    assert result.billed_cost_microdollars == 11
    assert body.decode() not in repr(result)


@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_all_response_failures_preserve_only_hash_and_length(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief, provider: str
) -> None:
    body = b"not-json-secret-body"
    result = _call(provider, cycle, evidence, brief, FixtureTransport(body))

    assert isinstance(result, AdapterFailure)
    assert not hasattr(result, "response_bytes")
    assert result.response_sha256 == hashlib.sha256(body).hexdigest()
    assert result.response_byte_length == len(body)
    assert body.decode() not in repr(result)
    assert body.decode() not in result.message


def test_success_repr_hides_request_and_response_bytes(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    response = _wire_json(_official_openai_response(cycle))
    result = call_openai(evidence, OPENAI_MODEL_ID, FixtureTransport(response))

    assert isinstance(result, OpenAISuccess)
    assert result.request_bytes not in repr(result).encode()
    assert response not in repr(result).encode()
    assert result.response_bytes == response


@pytest.mark.parametrize(
    "sensitive_values",
    ((b"",), (b"x" * 257,), tuple(b"x" for _ in range(17))),
)
def test_invalid_sensitive_value_metadata_is_provider_error(
    cycle: dict[str, Any],
    evidence: EvidencePacket,
    sensitive_values: tuple[bytes, ...],
) -> None:
    response = _wire_json(_official_openai_response(cycle))
    result = call_openai(
        evidence,
        OPENAI_MODEL_ID,
        FixtureTransport(response, sensitive_values=sensitive_values),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PROVIDER_ERROR"
    assert result.response_sha256 is None
    assert result.response_byte_length == 0


def test_transport_property_failure_is_provider_error(evidence: EvidencePacket) -> None:
    class BrokenResponse:
        @property
        def status_code(self) -> int:
            raise RuntimeError("credential-bearing property failure")

    result = call_openai(
        evidence,
        OPENAI_MODEL_ID,
        FixtureTransport(b"", response=BrokenResponse()),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PROVIDER_ERROR"
    assert result.request_bytes
    assert result.response_sha256 is None


def test_openai_uses_official_pinned_gpt5_snapshot_and_rejects_old_id(
    evidence: EvidencePacket,
) -> None:
    assert OPENAI_MODEL_ID == "gpt-5-2025-08-07"
    with pytest.raises(ValueError, match="approved pinned"):
        build_openai_request(evidence, "gpt-5-2026-09-30")


@pytest.mark.parametrize(
    "field",
    (
        "background",
        "completed_at",
        "conversation",
        "error",
        "incomplete_details",
        "instructions",
        "max_output_tokens",
        "max_tool_calls",
        "metadata",
        "moderation",
        "previous_response_id",
        "prompt",
        "prompt_cache_diagnostics",
        "prompt_cache_key",
        "prompt_cache_options",
        "prompt_cache_retention",
        "reasoning",
        "safety_identifier",
        "service_tier",
        "temperature",
        "text",
        "top_logprobs",
        "top_p",
        "truncation",
        "user",
    ),
)
def test_openai_accepts_each_current_optional_response_field(
    cycle: dict[str, Any],
    evidence: EvidencePacket,
    field: str,
) -> None:
    value = _openai_optional_fields(cycle)[field]
    response = _official_openai_response(cycle)
    response[field] = value

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
    )

    assert isinstance(result, OpenAISuccess)


def test_openai_accepts_current_official_response_fields_together(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief
) -> None:
    response = _official_openai_response(cycle)
    response.update(_openai_optional_fields(cycle))

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
    )

    assert isinstance(result, OpenAISuccess)
    assert result.brief == brief


@pytest.mark.parametrize(
    "access_programs",
    (
        None,
        {"cyber": "standard"},
        {"cyber": "daybreak_blue"},
        {"cyber": "daybreak_red"},
    ),
)
def test_openai_accepts_current_access_programs_variants(
    cycle: dict[str, Any],
    evidence: EvidencePacket,
    brief: ResearchBrief,
    access_programs: object,
) -> None:
    response = _official_openai_response(cycle)
    response["access_programs"] = access_programs

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
    )

    assert isinstance(result, OpenAISuccess)
    assert result.brief == brief


def test_openai_access_programs_is_optional_in_current_official_schema(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief
) -> None:
    response = _official_openai_response(cycle)
    response.pop("access_programs")

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
    )

    assert isinstance(result, OpenAISuccess)
    assert result.brief == brief


@pytest.mark.parametrize(
    "access_programs",
    (
        {},
        {"cyber": "unofficial"},
        {"cyber": "standard", "extra": True},
        "standard",
    ),
)
def test_openai_rejects_invalid_access_programs(
    cycle: dict[str, Any], evidence: EvidencePacket, access_programs: object
) -> None:
    response = _official_openai_response(cycle)
    response["access_programs"] = access_programs

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"


_PROMPT_CACHE_MISS_REASONS = (
    "model_changed",
    "prompt_cache_key_changed",
    "tools_changed",
    "text_format_changed",
    "reasoning_effort_changed",
    "verbosity_changed",
    "context_compacted",
    "input_changed",
    "service_tier_changed",
)


@pytest.mark.parametrize(
    "diagnostics",
    (
        None,
        {"type": "cache_hit"},
        {"type": "comparison_response_not_found"},
        {"type": "unavailable"},
        *(
            {"type": "cache_miss", "cache_missed_tokens": 0, "reason": reason}
            for reason in _PROMPT_CACHE_MISS_REASONS
        ),
        {
            "type": "cache_miss",
            "cache_missed_tokens": INT64_MAX,
            "reason": "input_changed",
            "comparison_reusable_tokens": None,
        },
        {
            "type": "cache_miss",
            "cache_missed_tokens": 1,
            "reason": "input_changed",
            "comparison_reusable_tokens": INT64_MAX,
        },
    ),
)
def test_openai_accepts_current_prompt_cache_diagnostics_as_audit_metadata(
    cycle: dict[str, Any],
    evidence: EvidencePacket,
    brief: ResearchBrief,
    diagnostics: object,
) -> None:
    response = _official_openai_response(cycle)
    response["prompt_cache_diagnostics"] = diagnostics

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
    )

    assert isinstance(result, OpenAISuccess)
    assert result.brief == brief


def test_openai_prompt_cache_diagnostics_is_optional(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief
) -> None:
    response = _official_openai_response(cycle)

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
    )

    assert isinstance(result, OpenAISuccess)
    assert result.brief == brief


@pytest.mark.parametrize(
    "diagnostics",
    (
        {"type": "cache_hit", "extra": True},
        {"type": "unknown"},
        {"type": "cache_miss"},
        {"type": "cache_miss", "cache_missed_tokens": True, "reason": "input_changed"},
        {"type": "cache_miss", "cache_missed_tokens": -1, "reason": "input_changed"},
        {
            "type": "cache_miss",
            "cache_missed_tokens": INT64_MAX + 1,
            "reason": "input_changed",
        },
        {
            "type": "cache_miss",
            "cache_missed_tokens": 0,
            "reason": "unknown",
        },
        {
            "type": "cache_miss",
            "cache_missed_tokens": 0,
            "reason": "input_changed",
            "comparison_reusable_tokens": -1,
        },
        {
            "type": "cache_miss",
            "cache_missed_tokens": 0,
            "reason": "input_changed",
            "comparison_reusable_tokens": INT64_MAX + 1,
        },
    ),
)
def test_openai_rejects_invalid_prompt_cache_diagnostics(
    cycle: dict[str, Any], evidence: EvidencePacket, diagnostics: object
) -> None:
    response = _official_openai_response(cycle)
    response["prompt_cache_diagnostics"] = diagnostics

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"


def test_openai_response_covers_all_current_official_generated_fields() -> None:
    official_fields = {
        "id",
        "access_programs",
        "created_at",
        "error",
        "incomplete_details",
        "instructions",
        "metadata",
        "model",
        "object",
        "output",
        "parallel_tool_calls",
        "temperature",
        "tool_choice",
        "tools",
        "top_p",
        "background",
        "completed_at",
        "conversation",
        "max_output_tokens",
        "max_tool_calls",
        "moderation",
        "previous_response_id",
        "prompt",
        "prompt_cache_diagnostics",
        "prompt_cache_key",
        "prompt_cache_options",
        "prompt_cache_retention",
        "reasoning",
        "safety_identifier",
        "service_tier",
        "status",
        "text",
        "top_logprobs",
        "truncation",
        "usage",
        "user",
    }

    assert official_fields == OpenAIResponse.model_fields.keys()


def test_openai_current_optional_envelope_fields_default_to_none() -> None:
    required = {
        "id",
        "object",
        "created_at",
        "model",
        "output",
        "parallel_tool_calls",
        "tool_choice",
        "tools",
    }

    for name, field in OpenAIResponse.model_fields.items():
        if name not in required:
            assert not field.is_required(), name
            assert field.default is None, name


def test_openai_semantically_scans_access_programs_for_credential_reflection(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    response = _official_openai_response(cycle)
    response["access_programs"] = {"cyber": "opaque-reflected-secret"}
    body = _wire_json(response).replace(b"secret", b"\\u0073ecret")

    result = call_openai(
        evidence,
        OPENAI_MODEL_ID,
        FixtureTransport(body, sensitive_values=(b"opaque-reflected-secret",)),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "CREDENTIAL_REFLECTION"


def test_openai_accepts_current_ultrafast_service_tier(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief
) -> None:
    response = _official_openai_response(cycle)
    response["service_tier"] = "ultrafast"

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
    )

    assert isinstance(result, OpenAISuccess)
    assert result.brief == brief


@pytest.mark.parametrize("phase", (None, "commentary", "final_answer"))
def test_openai_accepts_official_message_phase_variants(
    cycle: dict[str, Any], evidence: EvidencePacket, phase: str | None
) -> None:
    response = _official_openai_response(cycle)
    if phase is not None:
        _official_message(response)["phase"] = phase

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
    )

    assert isinstance(result, OpenAISuccess)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("instructions", []),
        ("moderation", {}),
        ("prompt", {}),
        ("prompt_cache_options", {}),
        ("prompt_cache_retention", "forever"),
        ("conversation", {}),
        ("tools", [{"type": "web_search"}]),
        ("tool_choice", {"type": "function"}),
    ),
)
def test_openai_rejects_unsupported_optional_capabilities(
    cycle: dict[str, Any],
    evidence: EvidencePacket,
    field: str,
    value: object,
) -> None:
    response = _official_openai_response(cycle)
    response[field] = value

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"


@pytest.mark.parametrize("value", (0, 1_780_488_030, 0.0, 1_780_488_030.0))
@pytest.mark.parametrize("field", ("created_at", "completed_at"))
def test_openai_timestamps_accept_and_preserve_nonnegative_unix_numbers(
    cycle: dict[str, Any], evidence: EvidencePacket, field: str, value: int | float
) -> None:
    response = _official_openai_response(cycle)
    response[field] = value

    envelope = OpenAIResponse.model_validate(response)
    assert getattr(envelope, field) == value
    assert type(getattr(envelope, field)) is type(value)
    result = call_openai(
        evidence,
        OPENAI_MODEL_ID,
        FixtureTransport(_wire_json(response)),
    )
    assert isinstance(result, OpenAISuccess)


@pytest.mark.parametrize(
    "value",
    (True, "1780488030", -1, float("nan"), float("inf"), float("-inf"), 1e309),
)
@pytest.mark.parametrize("field", ("created_at", "completed_at"))
def test_openai_timestamps_reject_nonfinite_negative_or_nonnumeric_values(
    cycle: dict[str, Any], evidence: EvidencePacket, field: str, value: object
) -> None:
    response = _official_openai_response(cycle)
    response[field] = value
    body = json.dumps(response, allow_nan=True, separators=(",", ":")).encode()

    result = call_openai(evidence, OPENAI_MODEL_ID, FixtureTransport(body))

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("background", 1),
        ("completed_at", True),
        ("conversation", {"id": "conv", "extra": True}),
        ("max_tool_calls", -1),
        ("prompt_cache_key", 1),
        ("safety_identifier", "x" * 65),
        ("service_tier", "unofficial"),
        ("top_logprobs", 21),
        ("user", 1),
    ),
)
def test_openai_rejects_invalid_documented_optional_response_field_types(
    cycle: dict[str, Any],
    evidence: EvidencePacket,
    field: str,
    value: object,
) -> None:
    response = _official_openai_response(cycle)
    response[field] = value

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"


def test_openai_accepts_cache_write_tokens_as_strict_nonnegative_int64(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    for value in (0, INT64_MAX):
        response = _official_openai_response(cycle)
        response["usage"]["input_tokens_details"]["cache_write_tokens"] = value
        result = call_openai(
            evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
        )
        assert isinstance(result, OpenAISuccess)


@pytest.mark.parametrize("value", (True, -1, INT64_MAX + 1, "1"))
def test_openai_rejects_invalid_cache_write_tokens(
    cycle: dict[str, Any], evidence: EvidencePacket, value: object
) -> None:
    response = _official_openai_response(cycle)
    response["usage"]["input_tokens_details"]["cache_write_tokens"] = value

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PARSE_ERROR"


@pytest.mark.parametrize("provider", ("openai", "jev"))
def test_unicode_escaped_sensitive_value_is_semantically_rejected(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief, provider: str
) -> None:
    response = (
        _official_openai_response(cycle) if provider == "openai" else _jev_body(cycle)
    )
    if provider == "openai":
        response["prompt_cache_key"] = "opaque-reflected-secret"
    else:
        response["answers"]["action"]["choice"] = "opaque-reflected-secret"
    body = _wire_json(response).replace(b"secret", b"\\u0073ecret")

    result = _call(
        provider,
        cycle,
        evidence,
        brief,
        FixtureTransport(body, sensitive_values=(b"opaque-reflected-secret",)),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "CREDENTIAL_REFLECTION"
    assert result.response_sha256 == hashlib.sha256(body).hexdigest()
    assert result.response_byte_length == len(body)
    assert "opaque-reflected-secret" not in repr(result)


def test_unicode_escaped_sensitive_value_in_reasoning_summary_is_rejected(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    response = _official_openai_response(cycle)
    response["output"][0]["summary"] = [
        {"type": "summary_text", "text": "opaque-reflected-secret"}
    ]
    body = _wire_json(response).replace(b"secret", b"\\u0073ecret")

    result = call_openai(
        evidence,
        OPENAI_MODEL_ID,
        FixtureTransport(body, sensitive_values=(b"opaque-reflected-secret",)),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "CREDENTIAL_REFLECTION"


def test_unicode_escaped_sensitive_value_in_reasoning_content_is_rejected(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    response = _official_openai_response(cycle)
    response["output"][0]["content"] = [
        {"type": "reasoning_text", "text": "opaque-reflected-secret"}
    ]
    body = _wire_json(response).replace(b"secret", b"\\u0073ecret")

    result = call_openai(
        evidence,
        OPENAI_MODEL_ID,
        FixtureTransport(body, sensitive_values=(b"opaque-reflected-secret",)),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "CREDENTIAL_REFLECTION"


def test_unicode_escaped_sensitive_value_in_decoded_output_text_is_rejected(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    response = _official_openai_response(cycle)
    output = _openai_text(response)
    output["arguments_for"][0] = "opaque-reflected-secret"
    _set_openai_text(response, output)
    body = _wire_json(response).replace(b"secret", b"\\\\u0073ecret")

    result = call_openai(
        evidence,
        OPENAI_MODEL_ID,
        FixtureTransport(body, sensitive_values=(b"opaque-reflected-secret",)),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "CREDENTIAL_REFLECTION"


def test_semantically_decoded_generic_credential_pattern_is_rejected(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    response = _official_openai_response(cycle)
    response["prompt_cache_key"] = "Bearer reflected-secret"
    body = _wire_json(response).replace(b"Bearer", b"B\\u0065arer")

    result = call_openai(evidence, OPENAI_MODEL_ID, FixtureTransport(body))

    assert isinstance(result, AdapterFailure)
    assert result.code == "CREDENTIAL_REFLECTION"


def test_semantic_credential_scanner_checks_string_mapping_keys_only() -> None:
    sentinel = b"opaque-reflected-secret"

    assert _contains_credentials({"opaque-reflected-secret": "safe"}, (sentinel,))
    assert not _contains_credentials({1: "safe", "ordinary-metadata": "safe"}, ())
    assert _contains_credentials({1: "Bearer reflected-secret"}, ())


def test_openai_rejects_unicode_escaped_sensitive_metadata_key(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    response = _official_openai_response(cycle)
    response["metadata"] = {"opaque-reflected-secret": "safe"}
    body = _wire_json(response).replace(b"secret", b"\\u0073ecret")

    result = call_openai(
        evidence,
        OPENAI_MODEL_ID,
        FixtureTransport(body, sensitive_values=(b"opaque-reflected-secret",)),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "CREDENTIAL_REFLECTION"
    assert not hasattr(result, "response_bytes")
    assert result.response_sha256 == hashlib.sha256(body).hexdigest()
    assert result.response_byte_length == len(body)
    assert "opaque-reflected-secret" not in repr(result)


def test_openai_rejects_sensitive_mapping_key_in_reasoning_output_json(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    response = _official_openai_response(cycle)
    response["output"][0]["summary"] = [
        {
            "type": "summary_text",
            "text": '{"opaque-reflected-secret":"safe"}',
        }
    ]
    body = _wire_json(response).replace(b"secret", b"\\\\u0073ecret")

    result = call_openai(
        evidence,
        OPENAI_MODEL_ID,
        FixtureTransport(body, sensitive_values=(b"opaque-reflected-secret",)),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "CREDENTIAL_REFLECTION"
    assert result.response_sha256 == hashlib.sha256(body).hexdigest()
    assert result.response_byte_length == len(body)
    assert "opaque-reflected-secret" not in repr(result)


def test_openai_rejects_sensitive_mapping_key_in_embedded_output_json(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    response = _official_openai_response(cycle)
    output = _openai_text(response)
    output["opaque-reflected-secret"] = "safe"
    _set_openai_text(response, output)
    body = _wire_json(response).replace(b"secret", b"\\\\u0073ecret")

    result = call_openai(
        evidence,
        OPENAI_MODEL_ID,
        FixtureTransport(body, sensitive_values=(b"opaque-reflected-secret",)),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "CREDENTIAL_REFLECTION"
    assert result.response_sha256 == hashlib.sha256(body).hexdigest()
    assert result.response_byte_length == len(body)
    assert "opaque-reflected-secret" not in repr(result)


def test_jev_rejects_sensitive_mapping_key_in_nested_answer_map(
    cycle: dict[str, Any], evidence: EvidencePacket, brief: ResearchBrief
) -> None:
    response = _jev_body(cycle)
    response["answers"]["p_yes"]["legend"]["opaque-reflected-secret"] = "safe"
    body = _wire_json(response).replace(b"secret", b"\\u0073ecret")

    result = call_jev(
        brief,
        evidence,
        JEV_MODEL_ID,
        FixtureTransport(body, sensitive_values=(b"opaque-reflected-secret",)),
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "CREDENTIAL_REFLECTION"
    assert result.response_sha256 == hashlib.sha256(body).hexdigest()
    assert result.response_byte_length == len(body)
    assert "opaque-reflected-secret" not in repr(result)


@pytest.mark.parametrize("key", ("authorization", "api_key", "cookie", "set-cookie"))
def test_openai_rejects_generic_credential_pattern_metadata_keys(
    cycle: dict[str, Any], evidence: EvidencePacket, key: str
) -> None:
    response = _official_openai_response(cycle)
    response["metadata"] = {key: "safe"}

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "CREDENTIAL_REFLECTION"


def test_openai_accepts_harmless_normal_metadata_key(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    response = _official_openai_response(cycle)
    response["metadata"] = {"ordinary-metadata": "safe"}

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(_wire_json(response))
    )

    assert isinstance(result, OpenAISuccess)


def test_custom_zero_offset_tzinfo_cannot_reach_result_metadata_or_repr(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    sentinel = "custom-tz-secret-never-retain"

    class SecretZeroOffset(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta:
            return timedelta(0)

        def dst(self, dt: datetime | None) -> timedelta:
            return timedelta(0)

        def tzname(self, dt: datetime | None) -> str:
            return sentinel

        def __repr__(self) -> str:
            return sentinel

    response = TransportResponse(
        status_code=200,
        headers={},
        raw_bytes=_wire_json(_official_openai_response(cycle)),
        retrieved_at=datetime(2026, 10, 3, tzinfo=SecretZeroOffset()),
        billed_cost_microdollars=0,
    )

    result = call_openai(
        evidence, OPENAI_MODEL_ID, FixtureTransport(b"", response=response)
    )

    assert isinstance(result, AdapterFailure)
    assert result.code == "PROVIDER_ERROR"
    assert result.retrieved_at is None
    assert sentinel not in repr(result)


def test_transport_timestamp_is_exact_builtin_utc_datetime(
    cycle: dict[str, Any], evidence: EvidencePacket
) -> None:
    result = call_openai(
        evidence,
        OPENAI_MODEL_ID,
        FixtureTransport(_wire_json(_official_openai_response(cycle))),
    )

    assert isinstance(result, OpenAISuccess)
    assert type(result.retrieved_at) is datetime
    assert result.retrieved_at.tzinfo is UTC


def test_call_catches_unexpected_builder_exception_as_input_error(
    evidence: EvidencePacket, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_builder(evidence: EvidencePacket, model: str) -> bytes:
        raise RuntimeError("do not expose builder internals")

    monkeypatch.setattr(
        "jev_trading_lab.polymarket.ai.build_openai_request", broken_builder
    )
    transport = FixtureTransport(b"never sent")

    result = call_openai(evidence, OPENAI_MODEL_ID, transport)

    assert isinstance(result, AdapterFailure)
    assert result.code == "INPUT_ERROR"
    assert result.request_bytes == b""
    assert transport.calls == []

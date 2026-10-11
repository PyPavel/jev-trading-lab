from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from jev_trading_lab.polymarket.ai import (
    JEV_MODEL_ID,
    OPENAI_MODEL_ID,
    LiveHttpRequest,
    LiveHttpResponse,
    _provider_network,
    live_jev_transport,
    live_openai_transport,
)

NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


class Network:
    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.requests: list[LiveHttpRequest] = []

    def __call__(self, request: LiveHttpRequest) -> LiveHttpResponse:
        self.requests.append(request)
        return LiveHttpResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=json.dumps(self.body).encode(),
            retrieved_at=NOW,
        )


def test_openai_live_transport_posts_exact_route_and_computes_usage_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-secret")
    monkeypatch.setenv("OPENAI_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS", "1250000")
    monkeypatch.setenv(
        "OPENAI_CACHED_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS", "125000"
    )
    monkeypatch.setenv("OPENAI_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS", "10000000")
    network = Network(
        {
            "model": OPENAI_MODEL_ID,
            "usage": {
                "input_tokens": 101,
                "input_tokens_details": {"cached_tokens": 1, "cache_write_tokens": 0},
                "output_tokens": 3,
                "output_tokens_details": {"reasoning_tokens": 2},
                "total_tokens": 104,
            },
        }
    )
    response = live_openai_transport(network=network)(b'{"model":"gpt-5-2025-08-07"}')
    request = network.requests[0]
    assert (request.method, request.url) == (
        "POST",
        "https://api.openai.com/v1/responses",
    )
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Authorization"] == "Bearer openai-test-secret"
    assert response.sensitive_values == (b"openai-test-secret",)
    assert response.billed_cost_microdollars == 156
    assert "openai-test-secret" not in repr(response)


def test_typesafe_live_transport_posts_exact_route_and_computes_usage_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "typesafe-test-secret")
    monkeypatch.setenv("TYPESAFE_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS", "42000")
    monkeypatch.setenv("TYPESAFE_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS", "42000")
    network = Network(
        {"model": JEV_MODEL_ID, "usage": {"input_tokens": 300, "output_tokens": 40}}
    )
    response = live_jev_transport(network=network)(b'{"model":"jev-1.13.0"}')
    request = network.requests[0]
    assert (request.method, request.url) == (
        "POST",
        "https://api.typesafe.ai/v1/systemone",
    )
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Authorization"] == "Bearer typesafe-test-secret"
    assert response.sensitive_values == (b"typesafe-test-secret",)
    assert response.billed_cost_microdollars == 15


@pytest.mark.parametrize(
    ("factory", "missing"),
    [
        (live_openai_transport, "OPENAI_API_KEY"),
        (live_openai_transport, "OPENAI_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS"),
        (live_jev_transport, "TYPESAFE_API_KEY"),
        (live_jev_transport, "TYPESAFE_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS"),
    ],
)
def test_missing_key_or_rate_is_typed_pre_response_failure(
    factory: Any, missing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = {
        "OPENAI_API_KEY": "x",
        "OPENAI_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS": "1",
        "OPENAI_CACHED_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS": "1",
        "OPENAI_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS": "1",
        "TYPESAFE_API_KEY": "y",
        "TYPESAFE_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS": "1",
        "TYPESAFE_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS": "1",
    }
    for name, value in names.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing)
    calls = 0

    def network(_: LiveHttpRequest) -> LiveHttpResponse:
        nonlocal calls
        calls += 1
        raise AssertionError("network must not run")

    with pytest.raises(ValueError, match="configuration"):
        factory(network=network)(b"{}")
    assert calls == 0


def test_live_network_rejects_redirect_content_type_and_oversize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("OPENAI_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS", "1")
    monkeypatch.setenv("OPENAI_CACHED_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS", "1")
    monkeypatch.setenv("OPENAI_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS", "1")
    for response in (
        LiveHttpResponse(302, {"location": "https://evil"}, b"", NOW),
        LiveHttpResponse(200, {"content-type": "text/html"}, b"{}", NOW),
        LiveHttpResponse(
            200, {"content-type": "application/json"}, b"{}", NOW, oversized=True
        ),
    ):
        transport = live_openai_transport(network=lambda _: response)
        with pytest.raises(ValueError):
            transport(b"{}")


INT64_MAX = 2**63 - 1
_MILLION = 1_000_000


def _openai_usage(input_tokens: int, cached: int, output_tokens: int) -> dict[str, Any]:
    return {
        "model": OPENAI_MODEL_ID,
        "usage": {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": 0},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": input_tokens + output_tokens,
        },
    }


def _set_rates(monkeypatch: pytest.MonkeyPatch, provider: str, *rates: int) -> None:
    if provider == "openai":
        monkeypatch.setenv("OPENAI_API_KEY", "openai-test-secret")
        names = (
            "OPENAI_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
            "OPENAI_CACHED_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
            "OPENAI_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
        )
    else:
        monkeypatch.setenv("TYPESAFE_API_KEY", "typesafe-test-secret")
        names = (
            "TYPESAFE_INPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
            "TYPESAFE_OUTPUT_COST_MICRODOLLARS_PER_MILLION_TOKENS",
        )
    for name, rate in zip(names, rates, strict=True):
        monkeypatch.setenv(name, str(rate))


def test_finding_6_openai_live_transport_reports_typed_cost_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_rates(monkeypatch, "openai", 10**12, 5 * 10**11, 10**12)
    network = Network(_openai_usage(10**13 + 7, 3, 5))

    response = live_openai_transport(network=network)(b'{"model":"gpt-5-2025-08-07"}')

    assert response.billed_cost_microdollars is None
    evidence = response.cost_overflow
    assert evidence is not None
    assert evidence.provider == "openai"
    assert (
        evidence.input_tokens,
        evidence.cached_input_tokens,
        evidence.output_tokens,
    ) == (10**13 + 7, 3, 5)
    assert (evidence.input_rate, evidence.cached_input_rate, evidence.output_rate) == (
        10**12,
        5 * 10**11,
        10**12,
    )
    numerator = (10**13 + 4) * 10**12 + 3 * 5 * 10**11 + 5 * 10**12
    assert evidence.exact_cost_microdollars() == -(-numerator // _MILLION)
    assert evidence.exact_cost_microdollars() > INT64_MAX
    assert "openai-test-secret" not in repr(response) + repr(evidence)


def test_finding_6_jev_live_transport_reports_typed_cost_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_rates(monkeypatch, "jev", 10**12, 10**12)
    network = Network(
        {"model": JEV_MODEL_ID, "usage": {"input_tokens": 10**13, "output_tokens": 9}}
    )

    response = live_jev_transport(network=network)(b'{"model":"jev-1.13.0"}')

    assert response.billed_cost_microdollars is None
    evidence = response.cost_overflow
    assert evidence is not None
    assert evidence.provider == "jev"
    assert (
        evidence.input_tokens,
        evidence.cached_input_tokens,
        evidence.output_tokens,
    ) == (10**13, None, 9)
    assert (evidence.input_rate, evidence.cached_input_rate, evidence.output_rate) == (
        10**12,
        None,
        10**12,
    )
    assert evidence.exact_cost_microdollars() == 10**19 + 9 * 10**6
    assert "typesafe-test-secret" not in repr(response) + repr(evidence)


@pytest.mark.parametrize("provider", ["openai", "jev"])
def test_finding_6_live_transport_keeps_exact_int64_maximum_representable(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    if provider == "openai":
        _set_rates(monkeypatch, provider, _MILLION, _MILLION, _MILLION)
        exact = live_openai_transport(network=Network(_openai_usage(INT64_MAX, 0, 0)))
        over = live_openai_transport(network=Network(_openai_usage(INT64_MAX, 0, 1)))
    else:
        _set_rates(monkeypatch, provider, _MILLION, _MILLION)
        usage = {"input_tokens": INT64_MAX, "output_tokens": 0}
        exact = live_jev_transport(
            network=Network({"model": JEV_MODEL_ID, "usage": usage})
        )
        usage = {"input_tokens": INT64_MAX, "output_tokens": 1}
        over = live_jev_transport(
            network=Network({"model": JEV_MODEL_ID, "usage": usage})
        )

    representable = exact(b"{}")
    assert representable.billed_cost_microdollars == INT64_MAX
    assert representable.cost_overflow is None
    overflowing = over(b"{}")
    assert overflowing.billed_cost_microdollars is None
    assert overflowing.cost_overflow is not None
    assert overflowing.cost_overflow.exact_cost_microdollars() == INT64_MAX + 1


@pytest.mark.parametrize("provider", ["openai", "jev"])
@pytest.mark.parametrize(
    ("tokens", "rate"), [(2**63, _MILLION), (2**70, 10**12), (10**400, 1)]
)
def test_finding_6_token_counts_beyond_int64_overflow_with_exact_evidence(
    monkeypatch: pytest.MonkeyPatch, provider: str, tokens: int, rate: int
) -> None:
    if provider == "openai":
        _set_rates(monkeypatch, provider, rate, rate, rate)
        transport = live_openai_transport(network=Network(_openai_usage(tokens, 0, 0)))
    else:
        _set_rates(monkeypatch, provider, rate, rate)
        usage = {"input_tokens": tokens, "output_tokens": 0}
        transport = live_jev_transport(
            network=Network({"model": JEV_MODEL_ID, "usage": usage})
        )

    response = transport(b"{}")

    assert response.billed_cost_microdollars is None
    evidence = response.cost_overflow
    assert evidence is not None
    assert evidence.input_tokens == tokens
    assert evidence.exact_cost_microdollars() == -(-(tokens * rate) // _MILLION)
    assert evidence.exact_cost_microdollars() > INT64_MAX


@pytest.mark.parametrize("provider", ["openai", "jev"])
def test_finding_6_token_counts_beyond_int64_with_a_representable_cost_price_exactly(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    tokens = 2**70
    if provider == "openai":
        _set_rates(monkeypatch, provider, 1, 1, 1)
        transport = live_openai_transport(network=Network(_openai_usage(tokens, 0, 0)))
    else:
        _set_rates(monkeypatch, provider, 1, 1)
        usage = {"input_tokens": tokens, "output_tokens": 0}
        transport = live_jev_transport(
            network=Network({"model": JEV_MODEL_ID, "usage": usage})
        )

    response = transport(b"{}")

    assert response.billed_cost_microdollars == -(-tokens // _MILLION)
    assert response.cost_overflow is None


def test_finding_6_openai_usage_without_cached_details_prices_every_input_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_rates(monkeypatch, "openai", _MILLION, 1, _MILLION)
    plain = Network(
        {"model": OPENAI_MODEL_ID, "usage": {"input_tokens": 1000, "output_tokens": 10}}
    )
    bad_details = Network(
        {
            "model": OPENAI_MODEL_ID,
            "usage": {
                "input_tokens": 1000,
                "input_tokens_details": {"cached_tokens": 2000},
                "output_tokens": 10,
            },
        }
    )

    assert live_openai_transport(network=plain)(b"{}").billed_cost_microdollars == 1010
    assert (
        live_openai_transport(network=bad_details)(b"{}").billed_cost_microdollars
        == 1010
    )

    _set_rates(monkeypatch, "openai", 10**12, 1, 10**12)
    huge = Network(
        {
            "model": OPENAI_MODEL_ID,
            "usage": {"input_tokens": 10**13, "output_tokens": 5},
        }
    )
    overflow = live_openai_transport(network=huge)(b"{}")
    assert overflow.billed_cost_microdollars is None
    assert overflow.cost_overflow is not None
    assert overflow.cost_overflow.cached_input_tokens == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://user@api.openai.com/v1/responses",
        "https://@api.openai.com/v1/responses",
        "https://:pass@api.typesafe.ai/v1/systemone",
        "https://api.openai.com/v1/responses#fragment",
        "https://api.openai.com/v1/responses?extra=1",
        "https://api.openai.com:444/v1/responses",
    ],
)
def test_provider_network_rejects_non_exact_uris(url: str) -> None:
    with pytest.raises(ValueError):
        _provider_network(LiveHttpRequest("POST", url, {}, b"{}"))

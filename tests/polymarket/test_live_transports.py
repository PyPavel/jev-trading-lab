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

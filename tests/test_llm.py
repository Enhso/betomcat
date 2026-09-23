"""Tests for the OpenRouter client (cost/usage parsing, error handling)."""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from betomcat.llm import OPENROUTER_URL, LLMError, OpenRouterClient
from betomcat.pool import ModelSpec

MODEL = ModelSpec(
    id="anthropic/claude-sonnet-5",
    tier="frontier",
    enabled=True,
    max_tokens=4000,
    reasoning={"effort": "medium"},
)


@pytest.fixture
async def client() -> OpenRouterClient:
    async with httpx.AsyncClient() as http_client:
        yield OpenRouterClient("test-key", client=http_client)


async def test_complete_parses_cost_and_tokens(
    httpx_mock: HTTPXMock, client: OpenRouterClient
) -> None:
    httpx_mock.add_response(
        url=OPENROUTER_URL,
        json={
            "choices": [{"message": {"content": "Probability: 42%"}}],
            "usage": {"cost": 0.0123, "prompt_tokens": 100, "completion_tokens": 50},
        },
    )

    result = await client.complete(MODEL, "some prompt", timeout=10.0)

    assert result.text == "Probability: 42%"
    assert result.cost_usd == pytest.approx(0.0123)
    assert result.tokens_in == 100
    assert result.tokens_out == 50

    request = httpx_mock.get_requests()[0]
    assert request.headers["authorization"] == "Bearer test-key"
    import orjson

    body = orjson.loads(request.content)
    assert body["model"] == "anthropic/claude-sonnet-5"
    assert body["usage"] == {"include": True}
    assert body["max_tokens"] == 4000
    assert body["reasoning"] == {"effort": "medium"}


async def test_complete_raises_on_http_error(
    httpx_mock: HTTPXMock, client: OpenRouterClient
) -> None:
    httpx_mock.add_response(url=OPENROUTER_URL, status_code=500, text="server error")

    with pytest.raises(LLMError, match="HTTP 500"):
        await client.complete(MODEL, "prompt", timeout=10.0)


async def test_complete_raises_on_empty_content(
    httpx_mock: HTTPXMock, client: OpenRouterClient
) -> None:
    httpx_mock.add_response(
        url=OPENROUTER_URL,
        json={
            "choices": [{"message": {"content": ""}}],
            "usage": {"cost": 0.0},
        },
    )

    with pytest.raises(LLMError, match="empty completion"):
        await client.complete(MODEL, "prompt", timeout=10.0)


async def test_complete_raises_on_no_choices(
    httpx_mock: HTTPXMock, client: OpenRouterClient
) -> None:
    httpx_mock.add_response(url=OPENROUTER_URL, json={"choices": [], "usage": {}})

    with pytest.raises(LLMError, match="no choices"):
        await client.complete(MODEL, "prompt", timeout=10.0)


async def test_complete_defaults_cost_to_zero_when_missing(
    httpx_mock: HTTPXMock, client: OpenRouterClient
) -> None:
    httpx_mock.add_response(
        url=OPENROUTER_URL,
        json={"choices": [{"message": {"content": "hi"}}]},
    )

    result = await client.complete(MODEL, "prompt", timeout=10.0)

    assert result.cost_usd == 0.0
    assert result.tokens_in == 0
    assert result.tokens_out == 0


# -- BYOK cost parsing (C3c) ---------------------------------------------------


async def test_complete_adds_upstream_inference_cost_for_byok(
    httpx_mock: HTTPXMock, client: OpenRouterClient
) -> None:
    # Verified live 2026-09-23: the funded key is BYOK, so `usage.cost` is 0
    # and the real charge sits in `cost_details.upstream_inference_cost`.
    httpx_mock.add_response(
        url=OPENROUTER_URL,
        json={
            "choices": [{"message": {"content": "hi"}}],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 17,
                "cost": 0,
                "is_byok": True,
                "cost_details": {
                    "upstream_inference_cost": 9.6e-06,
                    "upstream_inference_prompt_cost": 1.1e-06,
                    "upstream_inference_completions_cost": 8.5e-06,
                },
            },
        },
    )

    result = await client.complete(MODEL, "prompt", timeout=10.0)

    assert result.cost_usd == pytest.approx(9.6e-06)


async def test_complete_byok_sums_cost_and_upstream_cost(
    httpx_mock: HTTPXMock, client: OpenRouterClient
) -> None:
    httpx_mock.add_response(
        url=OPENROUTER_URL,
        json={
            "choices": [{"message": {"content": "hi"}}],
            "usage": {
                "cost": 0.001,
                "is_byok": True,
                "cost_details": {"upstream_inference_cost": 0.002},
            },
        },
    )

    result = await client.complete(MODEL, "prompt", timeout=10.0)

    assert result.cost_usd == pytest.approx(0.003)


async def test_complete_non_byok_does_not_add_upstream_cost(
    httpx_mock: HTTPXMock, client: OpenRouterClient
) -> None:
    httpx_mock.add_response(
        url=OPENROUTER_URL,
        json={
            "choices": [{"message": {"content": "hi"}}],
            "usage": {
                "cost": 0.0123,
                "is_byok": False,
                "cost_details": {"upstream_inference_cost": 9.6e-06},
            },
        },
    )

    result = await client.complete(MODEL, "prompt", timeout=10.0)

    assert result.cost_usd == pytest.approx(0.0123)


async def test_complete_byok_tolerates_missing_cost_details(
    httpx_mock: HTTPXMock, client: OpenRouterClient
) -> None:
    httpx_mock.add_response(
        url=OPENROUTER_URL,
        json={
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"cost": 0.0, "is_byok": True},
        },
    )

    result = await client.complete(MODEL, "prompt", timeout=10.0)

    assert result.cost_usd == 0.0


# -- key routing (C3b) --------------------------------------------------------

FREE_MODEL = ModelSpec(id="nex-agi/nex-n2.5-pro:free", tier="free", enabled=True)
GOOGLE_FREE_MODEL = ModelSpec(
    id="google/gemma-4-31b-it:free", tier="free", enabled=True
)


async def _ok_response(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=OPENROUTER_URL,
        json={"choices": [{"message": {"content": "ok"}}], "usage": {}},
    )


async def test_complete_uses_funded_key_when_no_free_key_configured(
    httpx_mock: HTTPXMock,
) -> None:
    await _ok_response(httpx_mock)
    async with httpx.AsyncClient() as http_client:
        client = OpenRouterClient("funded-key", client=http_client)
        await client.complete(FREE_MODEL, "prompt", timeout=10.0)

    request = httpx_mock.get_requests()[0]
    assert request.headers["authorization"] == "Bearer funded-key"


async def test_complete_routes_free_model_to_free_key(httpx_mock: HTTPXMock) -> None:
    await _ok_response(httpx_mock)
    async with httpx.AsyncClient() as http_client:
        client = OpenRouterClient("funded-key", "free-key", client=http_client)
        await client.complete(FREE_MODEL, "prompt", timeout=10.0)

    request = httpx_mock.get_requests()[0]
    assert request.headers["authorization"] == "Bearer free-key"


async def test_complete_routes_google_free_model_to_funded_key(
    httpx_mock: HTTPXMock,
) -> None:
    await _ok_response(httpx_mock)
    async with httpx.AsyncClient() as http_client:
        client = OpenRouterClient("funded-key", "free-key", client=http_client)
        await client.complete(GOOGLE_FREE_MODEL, "prompt", timeout=10.0)

    request = httpx_mock.get_requests()[0]
    assert request.headers["authorization"] == "Bearer funded-key"


async def test_complete_routes_paid_model_to_funded_key(httpx_mock: HTTPXMock) -> None:
    await _ok_response(httpx_mock)
    async with httpx.AsyncClient() as http_client:
        client = OpenRouterClient("funded-key", "free-key", client=http_client)
        await client.complete(MODEL, "prompt", timeout=10.0)

    request = httpx_mock.get_requests()[0]
    assert request.headers["authorization"] == "Bearer funded-key"


async def test_complete_respects_explicit_key_override(httpx_mock: HTTPXMock) -> None:
    forced_free = ModelSpec(
        id="anthropic/claude-sonnet-5", tier="frontier", enabled=True, key="free"
    )
    await _ok_response(httpx_mock)
    async with httpx.AsyncClient() as http_client:
        client = OpenRouterClient("funded-key", "free-key", client=http_client)
        await client.complete(forced_free, "prompt", timeout=10.0)

    request = httpx_mock.get_requests()[0]
    assert request.headers["authorization"] == "Bearer free-key"

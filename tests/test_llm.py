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

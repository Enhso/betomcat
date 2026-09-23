"""Async OpenRouter chat-completions client.

Sends `usage: {"include": true}` so every response carries `usage.cost` in
USD, which is how the bot tracks spend against the $100 OpenRouter budget
(brief s2). Timeouts are supplied by the caller (the pipeline's deadline
ladder owns pacing, not this client).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from betomcat.pool import ModelSpec, effective_key

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class LLMError(Exception):
    """Raised on an OpenRouter HTTP error or an empty/malformed response."""


@dataclass(frozen=True)
class LLMResult:
    text: str
    cost_usd: float
    tokens_in: int
    tokens_out: int


class OpenRouterClient:
    """Thin async wrapper around OpenRouter's chat-completions endpoint."""

    def __init__(
        self,
        api_key: str,
        free_api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Args:
        api_key: Funded OpenRouter API key. Never logged.
        free_api_key: Personal free-tier OpenRouter key, used for pool
            entries that route to it (`pool.effective_key`). Never logged.
            When absent, every call falls back to `api_key`.
        client: Optional pre-built `httpx.AsyncClient` (tests inject one).
        """
        self._api_key = api_key
        self._free_api_key = free_api_key
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _select_api_key(self, model: ModelSpec) -> str:
        if effective_key(model) == "free" and self._free_api_key:
            return self._free_api_key
        return self._api_key

    async def complete(
        self, model: ModelSpec, prompt: str, timeout: float
    ) -> LLMResult:
        """Run one chat-completion call.

        Args:
            model: Pool entry supplying the OpenRouter model id, `max_tokens`,
                and `reasoning` params.
            prompt: The full user-turn prompt text.
            timeout: Request timeout in seconds.

        Returns:
            The completion text plus cost and token usage.

        Raises:
            LLMError: On an HTTP error, a request failure, or an empty
                completion.
        """
        payload: dict[str, object] = {
            "model": model.id,
            "messages": [{"role": "user", "content": prompt}],
            "usage": {"include": True},
        }
        if model.max_tokens is not None:
            payload["max_tokens"] = model.max_tokens
        if model.reasoning:
            payload["reasoning"] = model.reasoning

        try:
            response = await self._client.post(
                OPENROUTER_URL,
                json=payload,
                headers={"Authorization": f"Bearer {self._select_api_key(model)}"},
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"{model.id}: request failed: {exc}") from exc

        if response.status_code >= 400:
            raise LLMError(
                f"{model.id}: HTTP {response.status_code}: {response.text[:500]}"
            )

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"{model.id}: no choices in response")
        text = (choices[0].get("message") or {}).get("content")
        if not text or not text.strip():
            raise LLMError(f"{model.id}: empty completion content")

        usage = data.get("usage") or {}
        return LLMResult(
            text=text,
            cost_usd=float(usage.get("cost", 0.0) or 0.0),
            tokens_in=int(usage.get("prompt_tokens", 0) or 0),
            tokens_out=int(usage.get("completion_tokens", 0) or 0),
        )

"""DeepSeek API provider — OpenAI-compatible Chat Completions with thinking mode.

Inherits from OpenAIProvider and reuses its full streaming/non-streaming execution,
SSE error detection, and configurable `reasoning_field` round-trip (`default_reasoning_field = "reasoning_content"`).

Thinking mode is controlled via `reasoning_effort`:
- When `reasoning_effort` is `"none"` (case-insensitive): thinking is disabled (`{"thinking": {"type": "disabled"}}`),
  `reasoning_effort` is omitted from payload, and `reasoning_content` is not sent back in history.
- Otherwise (default `"high"`): thinking is enabled (`{"thinking": {"type": "enabled"}}`) alongside `reasoning_effort`.

Token limits:
- DeepSeek only supports `max_tokens`. If `max_completion_tokens` is configured without `max_tokens`,
  it is automatically mapped to `max_tokens`.
"""

from __future__ import annotations

from typing import Any

import httpx

from .openai import OpenAIProvider
from .base import ProviderResponse, ToolCall
from . import dsml as _dsml

_DEFAULT_BASE_URL = "https://api.deepseek.com"


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek provider with thinking mode controlled by `reasoning_effort`."""

    provider_name = "deepseek"
    default_reasoning_field = "reasoning_content"
    default_reasoning_effort = "high"
    default_max_tokens: int | None = None
    default_max_completion_tokens: int | None = None

    def __init__(
        self,
        *,
        http: httpx.AsyncClient | None = None,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        timeout: float = 600.0,
        reasoning_effort: str | None = None,
        reasoning_field: str | None = None,
        max_tokens: int | None = None,
        max_completion_tokens: int | None = None,
        provider_options: dict[str, Any] | None = None,
    ) -> None:
        if http is None:
            http = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))
            self._owns_http = True
        else:
            self._owns_http = False

        if not base_url:
            base_url = _DEFAULT_BASE_URL

        po = provider_options if isinstance(provider_options, dict) else {}

        OpenAIProvider.__init__(
            self,
            http=http,
            api_key=api_key,
            base_url=base_url,
            model=model,
            reasoning_field=reasoning_field,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            max_completion_tokens=max_completion_tokens,
            provider_options=po,
        )

        # DeepSeek only supports max_tokens; map max_completion_tokens -> max_tokens if max_tokens is not set
        if self._max_tokens is None and self._max_completion_tokens is not None:
            self._max_tokens = self._max_completion_tokens
        self._max_completion_tokens = None

        # reasoning_effort == "none" disables thinking
        if (self._reasoning_effort or "").lower() == "none":
            self._thinking = False
            self._always_backfill_reasoning = False
            self._reasoning_effort = None
            if reasoning_field is None and "reasoning_field" not in po:
                self._reasoning_field = ""
        else:
            self._thinking = True
            self._always_backfill_reasoning = True

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        stream: bool = False,
    ) -> dict[str, Any]:
        payload = OpenAIProvider._build_payload(self, messages, tools, stream=stream)
        if self._thinking:
            payload["thinking"] = {"type": "enabled"}
        else:
            payload["thinking"] = {"type": "disabled"}
            payload.pop("reasoning_effort", None)
        return payload

    def _build_provider_meta(self) -> dict[str, Any]:
        return {"provider": "deepseek", "thinking": self._thinking}

    @staticmethod
    def _recover_dsml(resp: ProviderResponse) -> ProviderResponse:
        if resp.ok and not resp.tool_calls and resp.text and "DSML" in resp.text:
            text, calls = _dsml.parse_tool_calls(resp.text)
            if calls:
                resp.text = text or None
                resp.tool_calls = [ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"]) for c in calls]
        return resp

    async def chat(self, **kwargs: Any) -> ProviderResponse:
        return self._recover_dsml(await super().chat(**kwargs))

    async def chat_stream(self, **kwargs: Any) -> ProviderResponse:
        return self._recover_dsml(await super().chat_stream(**kwargs))

    async def aclose(self) -> None:
        """Close the HTTP client if we own it."""
        if self._owns_http:
            await self._http.aclose()

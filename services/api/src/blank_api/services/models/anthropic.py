"""Anthropic Messages API adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from blank_api.services.models.base import (
    AdapterConfig,
    ModelAdapter,
    ModelError,
    ModelResponse,
    Usage,
    data_payload,
)


class AnthropicAdapter(ModelAdapter):
    def __init__(self, config: AdapterConfig) -> None:
        super().__init__(config)
        self.base = config.base_url.rstrip("/")

    def _headers(self) -> dict:
        return {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def _payload(self, messages, tools, stream: bool) -> dict:
        system = "\n".join(m.content for m in messages if m.role == "system")
        payload = {
            "model": self.config.model,
            "system": system or None,
            "messages": [
                {"role": "assistant" if m.role == "assistant" else "user", "content": m.content}
                for m in messages
                if m.role != "system"
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": stream,
        }
        if tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools
            ]
        return {k: v for k, v in payload.items() if v is not None}

    async def invoke(self, messages, tools=None, **kwargs) -> ModelResponse:
        self.require_tools(tools or [])
        data = await self._request_json(
            "POST", f"{self.base}/v1/messages", headers=self._headers(), json=self._payload(messages, tools, False)
        )
        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        tool_calls = [
            {"id": b.get("id"), "type": "function", "function": {"name": b.get("name"), "arguments": json.dumps(b.get("input", {}))}}
            for b in data.get("content", [])
            if b.get("type") == "tool_use"
        ]
        usage = data.get("usage") or {}
        return ModelResponse(
            text=text,
            usage=Usage(
                prompt_tokens=int(usage.get("input_tokens") or 0),
                completion_tokens=int(usage.get("output_tokens") or 0),
            ),
            model=data.get("model") or self.config.model,
            tool_calls=tool_calls,
            finish_reason=data.get("stop_reason"),
        )

    async def stream(self, messages, tools=None, **kwargs) -> AsyncIterator[str]:
        self.require_tools(tools or [])
        async with self._client() as client:
            async with client.stream(
                "POST",
                f"{self.base}/v1/messages",
                headers=self._headers(),
                json=self._payload(messages, tools, True),
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="ignore")
                    raise ModelError("model_http_error", f"模型返回 {resp.status_code}：{body[:200]}")
                async for raw in resp.aiter_lines():
                    payload = data_payload(raw)
                    if not payload:
                        continue
                    event = json.loads(payload)
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta") or {}
                        if text := delta.get("text"):
                            yield text

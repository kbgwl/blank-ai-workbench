"""Local Ollama adapter (/api/chat, newline-delimited JSON)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from blank_api.services.models.base import (
    AdapterConfig,
    ModelAdapter,
    ModelError,
    ModelResponse,
    Usage,
)


class OllamaAdapter(ModelAdapter):
    def __init__(self, config: AdapterConfig) -> None:
        super().__init__(config)
        self.base = config.base_url.rstrip("/")

    def _payload(self, messages, tools, stream: bool) -> dict:
        payload = {
            "model": self.config.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": stream,
            "options": {"temperature": self.config.temperature, "num_predict": self.config.max_tokens},
        }
        if tools:
            payload["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
                for t in tools
            ]
        return payload

    async def invoke(self, messages, tools=None, **kwargs) -> ModelResponse:
        self.require_tools(tools or [])
        data = await self._request_json("POST", f"{self.base}/api/chat", json=self._payload(messages, tools, False))
        usage = Usage(
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            completion_tokens=int(data.get("eval_count") or 0),
        )
        message = data.get("message") or {}
        return ModelResponse(
            text=message.get("content") or "",
            usage=usage,
            model=data.get("model") or self.config.model,
            tool_calls=message.get("tool_calls") or [],
            finish_reason="stop" if data.get("done") else None,
        )

    async def stream(self, messages, tools=None, **kwargs) -> AsyncIterator[str]:
        self.require_tools(tools or [])
        async with self._client() as client:
            async with client.stream(
                "POST", f"{self.base}/api/chat", json=self._payload(messages, tools, True)
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="ignore")
                    raise ModelError("model_http_error", f"模型返回 {resp.status_code}：{body[:200]}")
                async for raw in resp.aiter_lines():
                    if not raw.strip():
                        continue
                    chunk = json.loads(raw)
                    if content := (chunk.get("message") or {}).get("content"):
                        yield content
                    if chunk.get("done"):
                        return

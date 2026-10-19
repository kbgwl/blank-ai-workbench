"""OpenAI-compatible adapter: OpenAI, DeepSeek, Qwen compatible-mode, any /chat/completions gateway."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence

from blank_api.services.models.base import (
    AdapterConfig,
    ChatMessage,
    ModelAdapter,
    ModelResponse,
    ToolSpecLite,
    Usage,
    data_payload,
)


class OpenAICompatibleAdapter(ModelAdapter):
    def __init__(self, config: AdapterConfig) -> None:
        super().__init__(config)
        self.base = config.base_url.rstrip("/")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}

    def _message_payload(self, message: ChatMessage) -> dict:
        payload = {"role": message.role, "content": message.content}
        if message.tool_call_id:
            payload["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            payload["tool_calls"] = message.tool_calls
        return payload

    def _payload(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpecLite] | None, stream: bool) -> dict:
        payload = {
            "model": self.config.model,
            "messages": [self._message_payload(m) for m in messages],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": stream,
        }
        if tools:
            payload["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
                for t in tools
            ]
        return payload

    async def invoke(self, messages, tools=None, **kwargs) -> ModelResponse:
        self.require_tools(tools or [])
        data = await self._request_json(
            "POST", f"{self.base}/chat/completions", headers=self._headers(), json=self._payload(messages, tools, False)
        )
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}
        # OpenAI 的 prompt_tokens 已包含缓存命中部分，这里只把细分项拆出来记账。
        prompt_details = usage.get("prompt_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        return ModelResponse(
            text=message.get("content") or "",
            usage=Usage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                cached_tokens=int(prompt_details.get("cached_tokens") or 0),
                reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
            ),
            model=data.get("model") or self.config.model,
            tool_calls=message.get("tool_calls") or [],
            finish_reason=choice.get("finish_reason"),
        )

    async def stream(self, messages, tools=None, **kwargs) -> AsyncIterator[str]:
        self.require_tools(tools or [])
        async with self._client() as client:
            async with client.stream(
                "POST",
                f"{self.base}/chat/completions",
                headers=self._headers(),
                json=self._payload(messages, tools, True),
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="ignore")
                    from blank_api.services.models.base import ModelError

                    raise ModelError("model_http_error", f"模型返回 {resp.status_code}：{body[:200]}")
                async for raw in resp.aiter_lines():
                    payload = data_payload(raw)
                    if not payload:
                        continue
                    delta = json.loads(payload).get("choices", [{}])[0].get("delta", {})
                    if content := delta.get("content"):
                        yield content

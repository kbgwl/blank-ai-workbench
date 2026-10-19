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

    def _system_blocks(self, messages) -> list[dict] | None:
        """系统提示打成块，并在末尾打一个缓存断点。

        Anthropic 的缓存是"前缀缓存"：断点之前的全部内容（system → tools → messages）
        在 5 分钟内重复出现时按 cache_read 计价，比重新计算便宜一个数量级。
        """
        text = "\n".join(m.content for m in messages if m.role == "system")
        if not text:
            return None
        block: dict = {"type": "text", "text": text}
        if self.config.prompt_cache:
            block["cache_control"] = {"type": "ephemeral"}
        return [block]

    def _message_payload(self, messages, cache_last: bool) -> list[dict]:
        converted: list[dict] = []
        for m in messages:
            if m.role == "system":
                continue
            converted.append(
                {
                    "role": "assistant" if m.role == "assistant" else "user",
                    "content": [{"type": "text", "text": m.content}],
                }
            )
        if cache_last and converted and converted[-1]["content"]:
            # 断点跟着对话尾部走：下一次调用能命中"上一次的全部前缀"，命中率随轮次上升。
            converted[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
        return converted

    def _payload(self, messages, tools, stream: bool) -> dict:
        payload = {
            "model": self.config.model,
            "system": self._system_blocks(messages),
            "messages": self._message_payload(messages, cache_last=bool(self.config.prompt_cache)),
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": stream,
        }
        if tools:
            specs = [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools]
            if self.config.prompt_cache and specs:
                # 工具表是每轮都一样的稳定前缀，值得单独打一个断点。
                specs[-1] = {**specs[-1], "cache_control": {"type": "ephemeral"}}
            payload["tools"] = specs
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
        # input_tokens 不含缓存部分，三块加起来才是这次真实的输入总量。
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_write = int(usage.get("cache_creation_input_tokens") or 0)
        return ModelResponse(
            text=text,
            usage=Usage(
                prompt_tokens=int(usage.get("input_tokens") or 0) + cache_read + cache_write,
                completion_tokens=int(usage.get("output_tokens") or 0),
                cached_tokens=cache_read,
                cache_write_tokens=cache_write,
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

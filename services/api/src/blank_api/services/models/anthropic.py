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
    retry_after_seconds,
)
from blank_api.services.models.streaming import ToolCallAccumulator


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
            # 图片放在文本之前：Anthropic 官方建议图在前、问题在后，模型看图的
            # 位置与问题离得近时引用更准。
            blocks: list[dict] = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image.get("media_type", "image/png"),
                        "data": image.get("data", ""),
                    },
                }
                for image in (m.images or ())
            ]
            blocks.append({"type": "text", "text": m.content})
            converted.append(
                {
                    "role": "assistant" if m.role == "assistant" else "user",
                    "content": blocks,
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
            response_id=str(data.get("id") or ""),
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
                    raise ModelError("model_http_error", f"模型返回 {resp.status_code}：{body[:200]}",
                                     status=resp.status_code,
                                     retry_after=retry_after_seconds(resp.headers.get("Retry-After")))
                async for raw in resp.aiter_lines():
                    payload = data_payload(raw)
                    if not payload:
                        continue
                    event = json.loads(payload)
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta") or {}
                        if text := delta.get("text"):
                            yield text

    async def bind_tools_stream(self, tools, messages, *, on_delta=None, **kwargs) -> ModelResponse:
        """带工具调用的流式调用。

        Anthropic 的工具参数是 ``input_json_delta.partial_json`` 逐段下发的，而且
        **没有** OpenAI 那种 index 里的 name —— 名字在 ``content_block_start`` 里，
        拼块的键是内容块的 index。两边形状不同，但聚合规则一样：流结束前不放手。
        """
        self.require_tools(tools or [])
        text: list[str] = []
        calls = ToolCallAccumulator()
        usage = Usage()
        model = self.config.model
        stop_reason: str | None = None
        message_id = ""  # 供应商那一笔响应的 id，Anthropic 在 message_start 帧里给

        async with self._client() as client:
            async with client.stream(
                "POST",
                f"{self.base}/v1/messages",
                headers=self._headers(),
                json=self._payload(messages, tools, True),
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="ignore")
                    raise ModelError("model_http_error", f"模型返回 {resp.status_code}：{body[:200]}",
                                     status=resp.status_code,
                                     retry_after=retry_after_seconds(resp.headers.get("Retry-After")))
                async for raw in resp.aiter_lines():
                    piece = data_payload(raw)
                    if not piece:
                        continue
                    try:
                        event = json.loads(piece)
                    except json.JSONDecodeError:
                        continue
                    kind = event.get("type") or ""
                    if kind == "message_start":
                        message = event.get("message") or {}
                        model = message.get("model") or model
                        message_id = str(message.get("id") or "") or message_id
                        usage = self._usage(message.get("usage") or {}, usage)
                    elif kind == "content_block_start":
                        block = event.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            calls.add(
                                int(event.get("index") or 0),
                                call_id=str(block.get("id") or ""),
                                name=str(block.get("name") or ""),
                            )
                    elif kind == "content_block_delta":
                        delta = event.get("delta") or {}
                        if text_piece := delta.get("text"):
                            text.append(text_piece)
                            if on_delta is not None:
                                await on_delta(text_piece)
                        if partial := delta.get("partial_json"):
                            calls.add(int(event.get("index") or 0), arguments=partial)
                    elif kind == "message_delta":
                        # 输出 token 与 stop_reason 只在这个事件里是终值
                        usage = self._usage(event.get("usage") or {}, usage)
                        stop_reason = (event.get("delta") or {}).get("stop_reason") or stop_reason
                    elif kind == "error":
                        error = event.get("error") or {}
                        raise ModelError("model_stream_error", str(error.get("message") or error))

        if not text and not len(calls):
            raise ModelError("model_stream_empty", f"模型 {self.config.model} 的流式响应为空")
        return ModelResponse(
            text="".join(text),
            usage=usage,
            model=model,
            tool_calls=calls.as_list(),
            finish_reason=stop_reason,
            response_id=message_id,
        )

    @staticmethod
    def _usage(raw: dict, base: Usage) -> Usage:
        """流式里 usage 分两次到：message_start 给输入，message_delta 给输出。

        没给到的字段沿用上一次的值——把缺失当成 0 会让成本记账凭空减半。
        """
        cache_read = raw.get("cache_read_input_tokens")
        cache_write = raw.get("cache_creation_input_tokens")
        inputs = raw.get("input_tokens")
        outputs = raw.get("output_tokens")
        cached = base.cached_tokens if cache_read is None else int(cache_read)
        written = base.cache_write_tokens if cache_write is None else int(cache_write)
        if inputs is None:
            prompt = base.prompt_tokens
        else:
            # input_tokens 不含缓存部分，三块加起来才是真实输入总量
            prompt = int(inputs) + cached + written
        return Usage(
            prompt_tokens=prompt,
            completion_tokens=base.completion_tokens if outputs is None else int(outputs),
            cached_tokens=cached,
            cache_write_tokens=written,
            reasoning_tokens=base.reasoning_tokens,
        )

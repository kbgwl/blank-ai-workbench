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
    retry_after_seconds,
)
from blank_api.services.models.streaming import ToolCallAccumulator


def _ollama_message(message) -> dict:
    """Ollama 的图片不是一个 part 数组，而是消息上一个 ``images`` 字段（裸 base64，无前缀）。

    和 OpenAI 系最大的不同也在这里：它没有"图片 part"这个概念，所以顺序无法表达，
    图与文本的相对位置由它自己拼。
    """
    payload: dict = {"role": message.role, "content": message.content}
    if message.images:
        payload["images"] = [image.get("data", "") for image in message.images]
    return payload


class OllamaAdapter(ModelAdapter):
    def __init__(self, config: AdapterConfig) -> None:
        super().__init__(config)
        self.base = config.base_url.rstrip("/")
    def _payload(self, messages, tools, stream: bool) -> dict:
        payload = {
            "model": self.config.model,
            "messages": [_ollama_message(m) for m in messages],
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
        # Ollama 的 /api/chat 回复里没有请求 id（本地进程，没有供应商侧可查的那一笔），
        # 所以 response_id 留空串——空是"这家没给"，不许拿我们自己的 uuid 顶上。
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
                    raise ModelError("model_http_error", f"模型返回 {resp.status_code}：{body[:200]}",
                                     status=resp.status_code,
                                     retry_after=retry_after_seconds(resp.headers.get("Retry-After")))
                async for raw in resp.aiter_lines():
                    if not raw.strip():
                        continue
                    chunk = json.loads(raw)
                    if content := (chunk.get("message") or {}).get("content"):
                        yield content
                    if chunk.get("done"):
                        return

    async def bind_tools_stream(self, tools, messages, *, on_delta=None, **kwargs) -> ModelResponse:
        """Ollama 是 NDJSON（每行一个完整 JSON），不是 SSE——所以这里不认 ``data:`` 前缀。

        工具调用一次性出现在某一行里（不是碎片），但仍然走同一个聚合器：
        供应商之间的差异不该渗进调用链，调用链只认"流结束时参数必须是完整的"。
        """
        self.require_tools(tools or [])
        text: list[str] = []
        calls = ToolCallAccumulator()
        usage = Usage()
        model = self.config.model
        done = False

        async with self._client() as client:
            async with client.stream(
                "POST", f"{self.base}/api/chat", json=self._payload(messages, tools, True)
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="ignore")
                    raise ModelError("model_http_error", f"模型返回 {resp.status_code}：{body[:200]}",
                                     status=resp.status_code,
                                     retry_after=retry_after_seconds(resp.headers.get("Retry-After")))
                async for raw in resp.aiter_lines():
                    if not raw.strip():
                        continue
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    model = chunk.get("model") or model
                    message = chunk.get("message") or {}
                    if content := message.get("content"):
                        text.append(content)
                        if on_delta is not None:
                            await on_delta(content)
                    for index, item in enumerate(message.get("tool_calls") or []):
                        function = item.get("function") or {}
                        calls.add(
                            index,
                            call_id=str(item.get("id") or ""),
                            name=str(function.get("name") or ""),
                            # Ollama 给的是对象而不是字符串，这里统一成 JSON 文本
                            arguments=json.dumps(function.get("arguments") or {}, ensure_ascii=False),
                        )
                    if chunk.get("done"):
                        usage = Usage(
                            prompt_tokens=int(chunk.get("prompt_eval_count") or 0),
                            completion_tokens=int(chunk.get("eval_count") or 0),
                        )
                        done = True

        if not done and not text and not len(calls):
            raise ModelError("model_stream_empty", f"模型 {self.config.model} 的流式响应为空")
        return ModelResponse(
            text="".join(text),
            usage=usage,
            model=model,
            tool_calls=calls.as_list(),
            finish_reason="stop" if done else None,
        )

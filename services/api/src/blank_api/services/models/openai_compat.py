"""OpenAI-compatible adapter: OpenAI, DeepSeek, Qwen compatible-mode, any /chat/completions gateway."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence

from blank_api.services.models.base import (
    AdapterConfig,
    ChatMessage,
    ModelAdapter,
    ModelError,
    ModelResponse,
    ToolSpecLite,
    Usage,
    data_payload,
    openai_content,
)
from blank_api.services.models.streaming import ToolCallAccumulator


class OpenAICompatibleAdapter(ModelAdapter):
    def __init__(self, config: AdapterConfig) -> None:
        super().__init__(config)
        self.base = config.base_url.rstrip("/")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}

    def _message_payload(self, message: ChatMessage) -> dict:
        payload = {"role": message.role, "content": openai_content(message)}
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
        return self._parse_completion(data)

    def _parse_completion(self, data: dict) -> ModelResponse:
        """非流式响应体 → ModelResponse。

        ``invoke`` 和"网关其实给了整包"的退路共用它：同一份数据不该有两套解析。
        """
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        return ModelResponse(
            text=message.get("content") or "",
            usage=_usage_from(data.get("usage") or {}),
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
                    raise ModelError("model_http_error", f"模型返回 {resp.status_code}：{body[:200]}")
                async for raw in resp.aiter_lines():
                    payload = data_payload(raw)
                    if not payload:
                        continue
                    delta = json.loads(payload).get("choices", [{}])[0].get("delta", {})
                    if content := delta.get("content"):
                        yield content

    async def bind_tools_stream(self, tools, messages, *, on_delta=None, **kwargs) -> ModelResponse:
        """带工具调用的流式调用。

        三件必须做对的事：
        * ``tool_calls`` 按 index 聚合。分片到达时 name 只在第一片、arguments 逐段，
          拼不完整就不能执行——所以本函数只在流结束后才返回，调用方拿到的必然是
          一条可以直接执行的完整响应。
        * ``usage`` 默认不在流里。要显式要 ``stream_options.include_usage``，
          否则用量记账会全是 0，而 0 会被当成"这次调用免费"。
        * 空流是**失败**，不是空回复。有的网关不认 stream，会把整个 JSON 一次返回，
          这时 ``data:`` 前缀不存在，一行也解析不出来；空响应必须抛错交给上层
          退回非流式，否则会静默变成"模型什么都没说"。不过在那之前先看这坨
          "不是 SSE 的行"是不是恰好就是完整响应当—是的话就地认掉，不必
          再花一次请求的钱去拿同一份数据。
        """
        self.require_tools(tools or [])
        payload = self._payload(messages, tools, True)
        payload["stream_options"] = {"include_usage": True}

        text: list[str] = []
        calls = ToolCallAccumulator()
        usage = Usage()
        model = self.config.model
        finish_reason: str | None = None
        # 不是 SSE 帧的行先收着：不认 stream 的网关会把整包 JSON 塞在这里
        plain: list[str] = []

        async with self._client() as client:
            async with client.stream(
                "POST",
                f"{self.base}/chat/completions",
                headers=self._headers(),
                json=payload,
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="ignore")
                    raise ModelError("model_http_error", f"模型返回 {resp.status_code}：{body[:200]}")
                async for raw in resp.aiter_lines():
                    piece = data_payload(raw)
                    if not piece:
                        if raw.strip():
                            plain.append(raw)
                        continue
                    try:
                        chunk = json.loads(piece)
                    except json.JSONDecodeError:
                        # 心跳/注释行不该中断整次调用，跳过即可
                        continue
                    model = chunk.get("model") or model
                    if chunk.get("usage"):
                        usage = _usage_from(chunk["usage"])
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if content := delta.get("content"):
                            text.append(content)
                            if on_delta is not None:
                                await on_delta(content)
                        for item in delta.get("tool_calls") or []:
                            calls.add(
                                int(item.get("index") or 0),
                                call_id=str(item.get("id") or ""),
                                name=str((item.get("function") or {}).get("name") or ""),
                                arguments=str((item.get("function") or {}).get("arguments") or ""),
                            )
                        finish_reason = choice.get("finish_reason") or finish_reason

        if not text and not len(calls):
            whole = _whole_completion(plain)
            if whole is not None:
                response = self._parse_completion(whole)
                # 整包也要推一次，否则界面等到最后才蹦出全文，流式就白开了
                if on_delta is not None and response.text:
                    await on_delta(response.text)
                return response
            raise ModelError("model_stream_empty", f"模型 {self.config.model} 的流式响应为空")
        return ModelResponse(
            text="".join(text),
            usage=usage,
            model=model,
            tool_calls=calls.as_list(),
            finish_reason=finish_reason,
        )


def _whole_completion(lines: list[str]) -> dict | None:
    """认出"整包返回的完整响应"，认不出返回 None。

    只认 choices 里带 ``message`` 的形状。放宽到"能解析就算"会把一次流式回包
    误当成完整响应，结果是静默空回复——比直接报错更难查。
    """
    raw = "\n".join(line for line in lines if line.strip())
    if not raw.lstrip().startswith("{"):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    choices = data.get("choices")
    if not isinstance(choices, list):
        return None
    if not any(isinstance(choice, dict) and "message" in choice for choice in choices):
        return None
    return data


def _usage_from(usage: dict) -> Usage:
    """OpenAI 的 prompt_tokens 已包含缓存命中部分，这里只把细分项拆出来记账。"""
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    return Usage(
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        cached_tokens=int(prompt_details.get("cached_tokens") or 0),
        reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
    )

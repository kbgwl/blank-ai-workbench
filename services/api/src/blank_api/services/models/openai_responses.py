"""OpenAI Responses API 适配器（/v1/responses）。

和 /chat/completions 的差别不只是换路径，是换了一套消息模型：

- 系统提示 → 顶层 `instructions`，不再占 messages 的位置；
- 对话 → `input[]` 条目流（message / function_call / function_call_output），
  工具调用与工具结果都是一等条目，靠 `call_id` 配对，不再靠 role=tool 反查；
- 工具 schema → 扁平定义（`{"type":"function","name",...}`），不再套一层 `function`；
- 隐私 → `store: false` 表示服务端不落库；
- 成本 → `prompt_cache_key` 让相同前缀在服务端命中缓存，`input_tokens_details.cached_tokens`
  会告诉我们命中了多少；
- 推理 → `reasoning.effort` 控制想多久，`include=["reasoning.encrypted_content"]` 让推理内容
  可以跨轮次回传（不传就只能拿到摘要）。

这几项是拆 Codex 时最值得抄的部分：同一份提示词，缓存命中与不命中的输入单价差一个数量级。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator

from blank_api.services.models.base import (
    AdapterConfig,
    ModelAdapter,
    ModelError,
    classify_http_failure,
    ModelResponse,
    Usage,
    data_payload,
    retry_after_seconds,
)
from blank_api.services.models.streaming import ToolCallAccumulator, invalid_tool_arguments
from blank_api.services.effort import rank

# 只有这两档的模型接受 reasoning 参数；其余模型带上去会 400。
_REASONING_EFFORTS = {"minimal", "low", "medium", "high"}


class OpenAIResponsesAdapter(ModelAdapter):
    def __init__(self, config: AdapterConfig) -> None:
        super().__init__(config)
        self.base = config.base_url.rstrip("/")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}

    # ---- 消息模型转换 -------------------------------------------------------
    def _split_system(self, messages) -> tuple[str, list[dict]]:
        """第一条 system 进 instructions，其余 system 变成 developer 条目。

        代理会在中途插入交接笔记/压缩摘要（也是 system 角色），它们不能丢，
        但 Responses 只有一个 instructions 槽位，所以后来者降级为 developer。
        """
        instructions = ""
        extras: list[dict] = []
        for message in messages:
            if message.role != "system":
                continue
            if not instructions:
                instructions = message.content
            else:
                extras.append(
                    {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": message.content}],
                    }
                )
        return instructions, extras

    def _input_items(self, messages) -> list[dict]:
        items: list[dict] = []
        for message in messages:
            if message.role == "system":
                continue
            if message.role == "tool":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id or "",
                        "output": message.content,
                    }
                )
                continue
            if message.role == "assistant":
                if message.content:
                    items.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": message.content}],
                        }
                    )
                for call in message.tool_calls or []:
                    function = call.get("function") or {}
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": call.get("id") or "",
                            "name": function.get("name") or "",
                            # arguments 在 Responses 里始终是 JSON 字符串，原样透传
                            "arguments": function.get("arguments") or "{}",
                        }
                    )
                continue
            content: list[dict] = []
            # 图片在 Responses 里是 input_image part，且只接受 data URL 或 URL 字符串。
            for image in message.images or ():
                content.append(
                    {
                        "type": "input_image",
                        "image_url": (
                            f"data:{image.get('media_type', 'image/png')};base64,{image.get('data', '')}"
                        ),
                    }
                )
            content.append({"type": "input_text", "text": message.content})
            items.append(
                {
                    "type": "message",
                    "role": message.role,
                    "content": content,
                }
            )
        return items

    def _cache_key(self, instructions: str) -> str:
        """按稳定前缀算缓存键：系统提示相同即命中，与任务/会话无关。"""
        return hashlib.sha256(instructions.encode("utf-8")).hexdigest()[:32]

    def _payload(self, messages, tools, stream: bool, *, effort: str = "", model: str = "") -> dict:
        instructions, extras = self._split_system(messages)
        payload: dict = {
            "model": model or self.config.model,
            "input": [*extras, *self._input_items(messages)],
            "max_output_tokens": self.payload_max_tokens(model),
            "stream": stream,
            # 服务端不落库：客户的任务内容不该停在供应商的存储里
            "store": False,
        }
        if instructions:
            payload["instructions"] = instructions
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                }
                for tool in tools
            ]
        # 本轮档位优先于配置里的静态值；xhigh/max 在 Responses 词汇表里没有
        # 对应档，收口到 high。
        reasoning = effort if effort in _REASONING_EFFORTS else (
            "high" if effort and rank(effort) > rank("high") else self.config.reasoning_effort
        )
        if reasoning in _REASONING_EFFORTS:
            payload["reasoning"] = {"effort": reasoning}
            payload["include"] = ["reasoning.encrypted_content"]
            # 推理模型不接受 temperature，带上会直接 400
        else:
            payload["temperature"] = self.config.temperature
        if self.config.verbosity:
            payload["text"] = {"verbosity": self.config.verbosity}
        if self.config.prompt_cache and instructions:
            payload["prompt_cache_key"] = self._cache_key(instructions)
        return payload

    # ---- 响应解析 -----------------------------------------------------------
    @staticmethod
    def _parse_output(data: dict) -> tuple[str, list[dict], str | None]:
        texts: list[str] = []
        tool_calls: list[dict] = []
        for item in data.get("output") or []:
            kind = item.get("type")
            if kind == "message":
                for block in item.get("content") or []:
                    if block.get("type") in {"output_text", "text"}:
                        texts.append(block.get("text") or "")
            elif kind == "function_call":
                tool_calls.append(
                    {
                        "id": item.get("call_id") or item.get("id"),
                        "type": "function",
                        "function": {
                            "name": item.get("name") or "",
                            "arguments": item.get("arguments") or "{}",
                        },
                    }
                )
        return "".join(texts), tool_calls, data.get("status")

    @staticmethod
    def _parse_usage(data: dict) -> Usage:
        usage = data.get("usage") or {}
        prompt_details = usage.get("input_tokens_details") or {}
        completion_details = usage.get("output_tokens_details") or {}
        return Usage(
            prompt_tokens=int(usage.get("input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
            cached_tokens=int(prompt_details.get("cached_tokens") or 0),
            reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
            source="provider"
            if (usage.get("input_tokens") or usage.get("output_tokens"))
            else "unknown",
        )

    async def invoke(self, messages, tools=None, *, effort: str = "", model: str = "", **kwargs) -> ModelResponse:
        self.require_tools(tools or [])
        data = await self._request_json(
            "POST", f"{self.base}/responses", headers=self._headers(), json=self._payload(messages, tools, False, effort=effort, model=model)
        )
        if data.get("error"):
            error = data["error"] or {}
            raise ModelError("model_http_error", f"模型返回错误：{error.get('message') or error}")
        text, tool_calls, status = self._parse_output(data)
        return ModelResponse(
            text=text,
            usage=self._parse_usage(data),
            model=data.get("model") or (model or self.config.model),
            tool_calls=tool_calls,
            finish_reason=status,
            response_id=str(data.get("id") or ""),
        )

    async def stream(self, messages, tools=None, *, effort: str = "", model: str = "", **kwargs) -> AsyncIterator[str]:
        self.require_tools(tools or [])
        async with self._client() as client:
            async with client.stream(
                "POST",
                f"{self.base}/responses",
                headers=self._headers(),
                json=self._payload(messages, tools, True, effort=effort, model=model),
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="ignore")
                    raise classify_http_failure(
                        resp.status_code,
                        body,
                        retry_after=retry_after_seconds(resp.headers.get("Retry-After")),
                    )
                async for raw in resp.aiter_lines():
                    payload = data_payload(raw)
                    if not payload:
                        continue
                    event = json.loads(payload)
                    kind = event.get("type") or ""
                    if kind == "response.output_text.delta":
                        if delta := event.get("delta"):
                            yield delta
                    elif kind == "error":
                        error = event.get("error") or {}
                        raise ModelError("model_stream_error", str(error.get("message") or error))

    async def bind_tools_stream(self, tools, messages, *, on_delta=None, effort: str = "", model: str = "", **kwargs) -> ModelResponse:
        """Responses 的流式：文本 delta 与函数调用 delta 是两套事件。

        函数调用的参数按 ``item_id`` 聚合（不是 OpenAI Chat 那种 index），
        名字只在 ``output_item.added`` 里出现一次。收尾优先用 ``response.completed``
        里的完整响应——那是唯一能同时拿到 usage 和全部 output 的地方。
        """
        self.require_tools(tools or [])
        text: list[str] = []
        calls = ToolCallAccumulator()
        usage = Usage()
        model = model or self.config.model
        finished: ModelResponse | None = None
        # 思考增量单独计数：本家会在正文之前推几十条 reasoning 事件（实测同一段对话
        # 先思考 52s、第 66s 才出正文）。一行正文都没有时，要能分清"网关没认流"
        # 与"模型只想不说"——后者退回整包只会让同一段思考再跑一遍。
        reasoning_events = 0
        reasoning_chars = 0
        # item_id → 累积器里的 index，两套编号需要一个映射
        item_index: dict[str, int] = {}

        async with self._client() as client:
            async with client.stream(
                "POST",
                f"{self.base}/responses",
                headers=self._headers(),
                json=self._payload(messages, tools, True, effort=effort, model=model),
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="ignore")
                    raise classify_http_failure(
                        resp.status_code,
                        body,
                        retry_after=retry_after_seconds(resp.headers.get("Retry-After")),
                    )
                async for raw in resp.aiter_lines():
                    piece = data_payload(raw)
                    if not piece:
                        continue
                    try:
                        event = json.loads(piece)
                    except json.JSONDecodeError:
                        continue
                    kind = event.get("type") or ""
                    if kind == "response.output_text.delta":
                        if delta := event.get("delta"):
                            text.append(delta)
                            if on_delta is not None:
                                await on_delta(delta)
                    elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                        reasoning_events += 1
                        reasoning_chars += len(str(event.get("delta") or ""))
                    elif kind == "response.output_item.added":
                        item = event.get("item") or {}
                        if item.get("type") == "function_call":
                            key = str(item.get("id") or item.get("call_id") or len(item_index))
                            item_index[key] = len(item_index)
                            calls.add(
                                item_index[key],
                                call_id=str(item.get("call_id") or item.get("id") or ""),
                                name=str(item.get("name") or ""),
                                arguments=str(item.get("arguments") or ""),
                            )
                    elif kind == "response.function_call_arguments.delta":
                        key = str(event.get("item_id") or "")
                        if key in item_index and (delta := event.get("delta")):
                            calls.add(item_index[key], arguments=delta)
                    elif kind == "response.completed":
                        data = event.get("response") or {}
                        done_text, done_calls, status = self._parse_output(data)
                        finished = ModelResponse(
                            text=done_text or "".join(text),
                            usage=self._parse_usage(data),
                            model=data.get("model") or model,
                            tool_calls=done_calls,
                            finish_reason=status,
                            response_id=str(data.get("id") or ""),
                        )
                    elif kind == "error":
                        error = event.get("error") or {}
                        raise ModelError("model_stream_error", str(error.get("message") or error))

        if finished is not None:
            bad = invalid_tool_arguments(finished.tool_calls)
            if bad:
                name, size = bad[0]
                raise ModelError(
                    "model_stream_truncated",
                    f"流在工具调用 {name} 的参数中途结束：{size} 字不是完整 JSON",
                    retryable=True,
                )
            return finished
        if not text and not len(calls):
            detail = (
                f"（期间收到 {reasoning_events} 个思考增量、{reasoning_chars} 字，没有正文与工具调用）"
                if reasoning_events
                else ""
            )
            raise ModelError(
                "model_stream_empty",
                f"模型 {self.config.model} 的流式响应为空{detail}",
                reasoning_events=reasoning_events,
                reasoning_chars=reasoning_chars,
            )
        final_calls = calls.as_list()
        bad = invalid_tool_arguments(final_calls)
        if bad:
            name, size = bad[0]
            # 截断的调用不许派发，也不许写回上下文；重试一次流才是对的路
            raise ModelError(
                "model_stream_truncated",
                f"流在工具调用 {name} 的参数中途结束：{size} 字不是完整 JSON",
                retryable=True,
            )
        # 没等到 response.completed（连接提前结束）：用累积到的碎片拼一条出来，
        # 但 usage 必然是空的——记账会显示 0，这是真实情况，不要伪造。
        return ModelResponse(
            text="".join(text),
            usage=usage,
            model=model,
            tool_calls=final_calls,
            finish_reason="incomplete",
        )

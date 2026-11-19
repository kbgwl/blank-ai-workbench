"""OpenAI-compatible adapter: OpenAI, DeepSeek, Qwen compatible-mode, any /chat/completions gateway."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from blank_api.services.models.base import (
    AdapterConfig,
    ChatMessage,
    ModelAdapter,
    ModelError,
    classify_http_failure,
    ModelResponse,
    ToolSpecLite,
    Usage,
    data_payload,
    openai_content,
    retry_after_seconds,
)
from blank_api.services.models.streaming import ToolCallAccumulator, invalid_tool_arguments
from blank_api.services.effort import rank

# OpenAI 兼容层的 reasoning_effort 词汇表只有 low/medium/high；xhigh/max 没有
# 对应档，收口到 high——塞一个网关不认识的值会被拒，比悄悄降级更糟。
_OPENAI_EFFORTS = ("low", "medium", "high")


def _image_fingerprint(data: str) -> dict:
    """把一张附图的 base64 压成可核对的元数据。

    400 说「unsupported image」时，光看报错什么都定不了：可能是格式、可能是长度、
    可能是我们自己在路上把它截断了。这里只留能定案的东西——长度、首尾、能否解码、
    解出来的前 8 个字节。**不落全文**：一张截图 80KB，转储文件会变成垃圾场。
    """
    import base64 as _b64

    text = str(data or "")
    info: dict = {"chars": len(text), "head": text[:32], "tail": text[-32:]}
    try:
        raw = _b64.b64decode(text, validate=True)
    except Exception as exc:  # 解不开说明 base64 本身就坏了（截断/混入别的字符）
        info["decode"] = f"failed: {type(exc).__name__}"
        return info
    info["bytes"] = len(raw)
    info["magic"] = raw[:8].hex()
    info["decode"] = "ok"
    return info


def dump_failed_request(payload: dict, error_body: str) -> None:
    """把一次 4xx 的请求体写下来（图只留指纹），用 ``BLANK_DUMP_REQUEST=1`` 打开。

    为什么常驻这个开关：模型层报 400 的时候，报错里只有一句网关的原话，
    我们这边却看不到自己到底发了什么。没有它就只能靠猜——而"猜"在这条链路上
    已经害过我们一次（图片被裁到 2 万字符，模型收到半截 data URI）。
    """
    if os.environ.get("BLANK_DUMP_REQUEST") != "1":
        return
    try:
        slim = json.loads(json.dumps(payload))
        for message in slim.get("messages") or []:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "image_url":
                    continue
                url = str((part.get("image_url") or {}).get("url") or "")
                prefix, _, b64 = url.partition(";base64,")
                part["image_url"] = {"url": f"{prefix};base64,<{json.dumps(_image_fingerprint(b64), ensure_ascii=False)}>"}
        target = Path(os.environ.get("BLANK_DUMP_PATH") or "data/last-request-failure.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"error": str(error_body)[:2000], "payload": slim}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    except Exception:  # 取证失败绝不能影响原本的错误路径
        pass


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

    def _reasoning_effort(self, effort: str) -> str:
        """本轮档位 → OpenAI 的 ``reasoning_effort``。

        传了本轮档位就以它为准（xhigh/max 收口到 high）；没传就回落到配置里
        静态写的那一份。空串两种情况都不下发。
        """
        if effort:
            if effort in _OPENAI_EFFORTS:
                return effort
            return "high" if rank(effort) > rank("high") else effort
        return self.config.reasoning_effort

    def _payload(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpecLite] | None, stream: bool, *, effort: str = "", model: str = "") -> dict:
        payload = {
            "model": model or self.config.model,
            "messages": [self._message_payload(m) for m in messages],
            "temperature": self.config.temperature,
            "max_tokens": self.payload_max_tokens(model),
            "stream": stream,
        }
        reasoning = self._reasoning_effort(effort)
        if reasoning:
            payload["reasoning_effort"] = reasoning
        if tools:
            payload["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
                for t in tools
            ]
        return payload

    async def invoke(self, messages, tools=None, *, effort: str = "", model: str = "", **kwargs) -> ModelResponse:
        self.require_tools(tools or [])
        data = await self._request_json(
            "POST", f"{self.base}/chat/completions", headers=self._headers(), json=self._payload(messages, tools, False, effort=effort, model=model)
        )
        return self._parse_completion(data, model=model)

    def _parse_completion(self, data: dict, *, model: str = "") -> ModelResponse:
        """非流式响应体 → ModelResponse。

        ``invoke`` 和"网关其实给了整包"的退路共用它：同一份数据不该有两套解析。
        """
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        return ModelResponse(
            text=message.get("content") or "",
            usage=_usage_from(data.get("usage") or {}),
            model=data.get("model") or (model or self.config.model),
            tool_calls=message.get("tool_calls") or [],
            finish_reason=choice.get("finish_reason"),
            response_id=str(data.get("id") or ""),
        )

    async def stream(self, messages, tools=None, *, effort: str = "", model: str = "", **kwargs) -> AsyncIterator[str]:
        self.require_tools(tools or [])
        async with self._client() as client:
            async with client.stream(
                "POST",
                f"{self.base}/chat/completions",
                headers=self._headers(),
                json=self._payload(messages, tools, True, effort=effort, model=model),
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="ignore")
                    dump_failed_request(payload, body)
                    raise classify_http_failure(
                        resp.status_code,
                        body,
                        retry_after=retry_after_seconds(resp.headers.get("Retry-After")),
                    )
                async for raw in resp.aiter_lines():
                    payload = data_payload(raw)
                    if not payload:
                        continue
                    delta = json.loads(payload).get("choices", [{}])[0].get("delta", {})
                    if content := delta.get("content"):
                        yield content

    async def bind_tools_stream(self, tools, messages, *, on_delta=None, effort: str = "", model: str = "", **kwargs) -> ModelResponse:
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
        payload = self._payload(messages, tools, True, effort=effort, model=model)
        payload["stream_options"] = {"include_usage": True}

        text: list[str] = []
        calls = ToolCallAccumulator()
        usage = Usage()
        model = model or self.config.model
        finish_reason: str | None = None
        # 流式帧每片都带同一个 `id`（供应商那一笔响应的 id）；帧里拿不到就用响应头。
        # 两个都要不上才写空串——空串是"这家没给 id"这个事实，不是"还没实现"。
        response_id = ""
        # 不是 SSE 帧的行先收着：不认 stream 的网关会把整包 JSON 塞在这里
        plain: list[str] = []
        # 思考增量单独计数。这条通道上它与正文是同一个 `delta` 里的两个键，
        # 不数就分不清「网关没认流」与「模型只想不说」——后者退整包只会把
        # 同一段思考再跑一遍（对方在整个思考期一个字节都不发）。
        reasoning_events = 0
        reasoning_chars = 0

        async with self._client() as client:
            async with client.stream(
                "POST",
                f"{self.base}/chat/completions",
                headers=self._headers(),
                json=payload,
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="ignore")
                    dump_failed_request(payload, body)
                    raise classify_http_failure(
                        resp.status_code,
                        body,
                        retry_after=retry_after_seconds(resp.headers.get("Retry-After")),
                    )
                header_id = str(resp.headers.get("x-request-id") or resp.headers.get("x-dashscope-request-id") or "")
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
                    response_id = response_id or str(chunk.get("id") or "")
                    if chunk.get("usage"):
                        usage = _usage_from(chunk["usage"])
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        # 思考增量：DeepSeek/vLLM/讯飞推 `reasoning_content`，
                        # 少数网关推 `reasoning`。两个都认，但只计数不展示——
                        # 它不是回答，混进正文会让用户看到一大段自问自答。
                        thought = delta.get("reasoning_content") or delta.get("reasoning")
                        if isinstance(thought, str) and thought:
                            reasoning_events += 1
                            reasoning_chars += len(thought)
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
                response = self._parse_completion(whole, model=model)
                bad = invalid_tool_arguments(response.tool_calls)
                if bad:
                    name, size = bad[0]
                    raise ModelError(
                        "model_stream_truncated",
                        f"流在工具调用 {name} 的参数中途结束：{size} 字不是完整 JSON",
                        retryable=True,
                    )
                # 整包也要推一次，否则界面等到最后才蹦出全文，流式就白开了
                if on_delta is not None and response.text:
                    await on_delta(response.text)
                # 整包形态下思考在 `message.reasoning_content` 里，不在 delta 里；
                # 不补这一段，上层会把"只想不说"误判成"网关不认流"。
                if not response.reasoning_chars:
                    message = ((whole.get("choices") or [{}])[0] or {}).get("message") or {}
                    thinking = message.get("reasoning_content") or message.get("reasoning") or ""
                    response.reasoning_chars = len(thinking) if isinstance(thinking, str) else 0
                return response
            raise ModelError(
                "model_stream_empty",
                f"模型 {self.config.model} 的流式响应为空{_empty_detail(reasoning_events, reasoning_chars, finish_reason)}",
                reasoning_events=reasoning_events,
                reasoning_chars=reasoning_chars,
                finish_reason=str(finish_reason or ""),
            )
        final_calls = calls.as_list()
        bad = invalid_tool_arguments(final_calls)
        if bad:
            name, size = bad[0]
            raise ModelError(
                "model_stream_truncated",
                f"流在工具调用 {name} 的参数中途结束：{size} 字不是完整 JSON",
                retryable=True,
            )
        return ModelResponse(
            text="".join(text),
            usage=usage,
            model=model,
            tool_calls=final_calls,
            finish_reason=finish_reason,
            reasoning_chars=reasoning_chars,
            response_id=response_id or header_id,
        )


def _empty_detail(events: int, chars: int, finish_reason: str | None) -> str:
    """空正文时补一句"为什么空"。

    没收到思考增量就不补——那时空流的原因在网关，写一堆 0 只是噪音。
    """
    if not events:
        return ""
    return (
        f"（期间收到 {events} 个思考增量、{chars} 字，没有正文与工具调用；"
        f"finish_reason={finish_reason or '未给'}）"
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
    """OpenAI 的 prompt_tokens 已包含缓存命中部分，这里只把细分项拆出来记账。

    供应商真的回了 usage（有任一非零计数字段）→ source="provider"；
    空字典 / 全零 → source 保持 "unknown"：0 个 token 与"没说用了多少"是两回事。
    """
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    has_usage = bool(
        usage.get("prompt_tokens") or usage.get("completion_tokens")
        or usage.get("total_tokens")
    )
    return Usage(
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        cached_tokens=int(prompt_details.get("cached_tokens") or 0),
        reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
        source="provider" if has_usage else "unknown",
    )

"""Unified model layer: business code never talks to a vendor SDK directly."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field

import httpx


class ModelError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


# 增量文本回调：await 它是为了背压——模型在流式输出时，消费方（记日志、广播给界面）
# 也应当是同一节奏，而不是把模型读得快、把回调堆在后台队列里。
DeltaHandler = Callable[[str], Awaitable[None]]


@dataclass
class ChatMessage:
    role: str  # system | user | assistant | tool
    content: str
    tool_call_id: str | None = None
    tool_calls: list[dict] = field(default_factory=list)


@dataclass
class ToolSpecLite:
    name: str
    description: str
    input_schema: dict


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 命中提示词缓存的输入 token：这部分按折扣价计费，单独记账才能算出真实省了多少。
    cached_tokens: int = 0
    # 写入提示词缓存的输入 token：Anthropic 收 1.25 倍，OpenAI 侧自动缓存不计写入费。
    cache_write_tokens: int = 0
    # 推理 token：已包含在 completion_tokens 里，单独给出用于解释成本与耗时。
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cache_hit_rate(self) -> float:
        """缓存命中率。分母用输入总量，没命中也没写入时返回 0。"""
        if self.prompt_tokens <= 0:
            return 0.0
        return round(self.cached_tokens / self.prompt_tokens, 4)


@dataclass
class ModelResponse:
    text: str
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str | None = None


@dataclass
class AdapterConfig:
    name: str
    kind: str
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.2
    max_tokens: int = 2048
    # 模型上下文窗口（tokens）。0 表示未知：此时上下文压缩退回字符预算。
    context_window: int = 0
    supports_tools: bool = False
    supports_vision: bool = False
    timeout_seconds: float = 60.0
    # 输出档位：Responses / o 系列用 minimal|low|medium|high 控制"想多久"，留空表示不下发。
    reasoning_effort: str = ""
    # 文本冗长档位：low|medium|high，留空表示不下发。
    verbosity: str = ""
    # 是否打提示词缓存断点。默认开：同一段系统提示与工具表在连续调用间可以复用。
    prompt_cache: bool = True
    client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient


class ModelAdapter(ABC):
    def __init__(self, config: AdapterConfig) -> None:
        self.config = config

    # ---- capability surface -------------------------------------------------
    @property
    def capabilities(self) -> dict:
        return {
            "provider": self.config.name,
            "kind": self.config.kind,
            "model": self.config.model,
            "streaming": True,
            "tools": self.config.supports_tools,
            "vision": self.config.supports_vision,
            "structured_output": True,
        }

    def require_tools(self, tools: Sequence[ToolSpecLite]) -> None:
        if tools and not self.config.supports_tools:
            raise ModelError(
                code="model_tools_unsupported",
                message=(
                    f"模型 {self.config.model} 未声明支持工具调用，无法在 Agent 流程中使用。"
                    "请在设置中勾选工具能力或改用支持工具的模型。"
                ),
            )

    # ---- transport helpers --------------------------------------------------
    def _client(self) -> httpx.AsyncClient:
        factory = self.config.client_factory
        if isinstance(factory, httpx.AsyncClient):
            return factory
        return factory(timeout=httpx.Timeout(self.config.timeout_seconds))

    async def _request_json(self, method: str, url: str, **kwargs) -> dict:
        try:
            async with self._client() as client:
                resp = await client.request(method, url, **kwargs)
        except httpx.TimeoutException:
            raise ModelError("model_timeout", f"模型请求超时：{self.config.name}", retryable=True) from None
        except httpx.HTTPError as exc:
            raise ModelError("model_unreachable", f"模型服务不可达：{self.config.name}（{exc.__class__.__name__}）", retryable=True) from None
        if resp.status_code >= 400:
            raise ModelError(
                "model_http_error",
                f"模型返回 {resp.status_code}：{resp.text[:200]}",
                retryable=resp.status_code >= 500,
            )
        return resp.json()

    @abstractmethod
    async def invoke(self, messages: Sequence[ChatMessage], **kwargs) -> ModelResponse: ...

    @abstractmethod
    def stream(self, messages: Sequence[ChatMessage], **kwargs) -> AsyncIterator[str]: ...

    async def structured_output(self, schema: dict, messages: Sequence[ChatMessage], **kwargs) -> dict:
        instruction = (
            "只输出符合以下 JSON Schema 的 JSON，不要输出解释或代码块标记：\n"
            + json.dumps(schema, ensure_ascii=False)
        )
        prompted = [ChatMessage(role="system", content=instruction), *messages]
        result = await self.invoke(prompted, **kwargs)
        return _parse_json_object(result.text)

    async def bind_tools(self, tools: Sequence[ToolSpecLite], messages: Sequence[ChatMessage], **kwargs) -> ModelResponse:
        self.require_tools(tools)
        return await self.invoke(messages, tools=tools, **kwargs)

    async def bind_tools_stream(
        self,
        tools: Sequence[ToolSpecLite],
        messages: Sequence[ChatMessage],
        *,
        on_delta: DeltaHandler | None = None,
        **kwargs,
    ) -> ModelResponse:
        """流式版 bind_tools：文字边到边回调，返回聚合后的完整响应。

        这里是**退路**，不是空实现：没有专门实现流式的适配器一次性拿完，然后把全文
        回调一次。语义与 ``bind_tools`` 完全一致，只是没有增量体验。

        一条退路比四处各写一份安全——把"要不要流式"变成每个适配器都要自己答的问题，
        迟早会有一家答错，而错法是静默的（文字一直在，只是界面不动）。
        """
        self.require_tools(tools)
        response = await self.invoke(messages, tools=tools, **kwargs)
        if on_delta is not None and response.text:
            await on_delta(response.text)
        return response


def _parse_json_object(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped[4:] if stripped.lower().startswith("json") else stripped
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end == -1:
        raise ModelError("model_output_not_json", "模型未按要求返回 JSON")
    try:
        parsed = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ModelError("model_output_not_json", f"模型返回的 JSON 无法解析：{exc}") from None
    if not isinstance(parsed, dict):
        raise ModelError("model_output_not_json", "模型返回的 JSON 不是对象")
    return parsed


def sse_lines(body: AsyncIterator[bytes]) -> AsyncIterator[str]:
    return body


def data_payload(line: str) -> str | None:
    """Extract the payload of an SSE `data:` line."""
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    return None if payload in {"", "[DONE]"} else payload

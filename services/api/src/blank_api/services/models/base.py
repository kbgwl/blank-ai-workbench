"""Unified model layer: business code never talks to a vendor SDK directly."""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime

import httpx


class ModelError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        *,
        status: int = 0,
        retry_after: float = 0.0,
        reasoning_events: int = 0,
        reasoning_chars: int = 0,
        finish_reason: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        # 供应商给的结束原因（`length` = 预算用光被切、空串 = 没给）。
        # 「正文为空」有两种完全不同的原因：网关不认流，和模型把额度花在思考上。
        # 只有这个字段能区分，光看正文长度分不出来。
        self.finish_reason = finish_reason
        # 供应商给回的状态码（0 = 这一笔根本没走到有状态码的那一步）。
        # 上层要它才能分清"换个说法再说一遍可能有用"与"再说一遍只是添乱"：
        # 400 常是在拒 `stream` 这个参数，429/5xx 拒的是"现在不许问"，与问法无关。
        self.status = status
        # 供应商在 429 上明说的等待秒数（`Retry-After`，0 = 没说）。退避取它与
        # 指数退避的较大者：服务器已经说了多久后再来，比客户端自己猜的更有依据。
        self.retry_after = retry_after
        # 流式响应里收到的思考增量。`model_stream_empty` 不能一律当成"网关不认流"：
        # 收到过思考增量说明流是通的，只是模型把预算全花在思考上、没给正文——
        # 这种请求退回整包只会把同一段思考再跑一遍（真机 01f1fa83 卡了 90 分钟）。
        self.reasoning_events = reasoning_events
        self.reasoning_chars = reasoning_chars


# 供应商 4xx 正文里的特征词。分不清具体原因时，用户只能看到"模型返回 400"，
# 既不知道是 Key 错了、模型名写错了，还是上下文太长——这三种修法完全不同。
_CONTEXT_MARKERS = (
    "context length",
    "context_length",
    "maximum context",
    "too many tokens",
    "prompt is too long",
    "reduce the length",
    "exceeds the maximum number of tokens",
    "input is too long",
)
_MODEL_MARKERS = (
    "model_not_found",
    "model not found",
    "does not exist",
    "invalid model",
    "unknown model",
    "no such model",
    "not a valid model",
)


def classify_http_failure(status: int, body: str, *, retry_after: float = 0.0) -> ModelError:
    """把供应商的 HTTP 失败翻成"用户能据此动手"的错误。

    对齐 Claude / Codex 的做法：认证失败、模型名不存在、上下文超限这三类
    **不能重试**（同样的请求再发一遍只会得到同样的 401/400），而且必须说清是什么问题；
    429 与 5xx 才是"现在别问"，交给上层退避重试。
    """
    text = (body or "")[:2000]
    lowered = text.lower()
    snippet = text[:200]

    if status in (401, 403):
        return ModelError(
            "authentication_error",
            f"API Key 无效或没有该模型的权限（HTTP {status}）：{snippet}",
            retryable=False,
            status=status,
        )
    if any(marker in lowered for marker in _CONTEXT_MARKERS):
        return ModelError(
            "context_overflow",
            f"上下文超出模型窗口（HTTP {status}）：{snippet}",
            retryable=False,
            status=status,
        )
    if any(marker in lowered for marker in _MODEL_MARKERS):
        return ModelError(
            "model_not_found",
            f"模型名不存在或未开通（HTTP {status}）：{snippet}。检查设置里的 Model ID。",
            retryable=False,
            status=status,
        )
    if status == 404:
        return ModelError(
            "endpoint_not_found",
            f"接口地址不存在（HTTP 404）：{snippet}。检查设置里的接口地址是否需要 /v1。",
            retryable=False,
            status=status,
        )
    if status == 429:
        return ModelError(
            "rate_limit",
            f"供应商限流（HTTP 429）：{snippet}",
            retryable=True,
            status=status,
            retry_after=retry_after,
        )
    if status >= 500:
        return ModelError(
            "model_overloaded",
            f"供应商暂时不可用（HTTP {status}）：{snippet}",
            retryable=True,
            status=status,
        )
    return ModelError("model_http_error", f"模型返回 {status}：{snippet}", retryable=False, status=status)


def retry_after_seconds(value: str | None, *, now: float | None = None) -> float:
    """把 `Retry-After` 读成秒数：支持整秒与 HTTP 日期，读不懂就当没给。

    RFC 9110 允许两种形状：`Retry-After: 120` 与 `Retry-After: Fri, 23 Oct 2026 08:00:04 GMT`。
    主流网关两样都用，只认其中一种会把另一种静默读成 0（退回指数退避），
    看起来"有退避"、实际没听供应商的。过去的时间按 0 算，不返回负数。
    """
    if not value:
        return 0.0
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return 0.0
    if when is None:
        return 0.0
    if when.tzinfo is None:  # 日期没带时区时按 UTC 读（HTTP 日期的规范时区）
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, when.timestamp() - (time.time() if now is None else now))


# —— 连接池 ——
# 默认出口（没有任何注入 factory）时，整个进程共用按超时时间分组的客户端。
# 测试注入 mock transport 的那条路**不进池**：那些客户端各自带着不同的 transport，
# 混用会让一个用例的桩去服务另一个用例的请求，排查起来像"模型偶尔答错"。
_POOL: dict[float, httpx.AsyncClient] = {}
_POOL_LIMITS = httpx.Limits(max_connections=32, max_keepalive_connections=16)


def pooled_client(timeout_seconds: float) -> httpx.AsyncClient:
    """取（或建）一个共享客户端。按超时时间分组：超时是客户端级属性，不能中途改。"""
    key = float(timeout_seconds or 60.0)
    client = _POOL.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=httpx.Timeout(key), limits=_POOL_LIMITS)
        _POOL[key] = client
    return client


async def aclose_pooled_clients() -> int:
    """进程收尾时关掉共享客户端，返回关了几个。

    不关也不会立刻出问题（进程退出时内核回收），但测试里会留下
    "Task was destroyed but it is pending" 的噪音，而噪音会掩盖真问题。
    """
    closed = 0
    for key, client in list(_POOL.items()):
        _POOL.pop(key, None)
        if client.is_closed:
            continue
        try:
            await client.aclose()
            closed += 1
        except Exception:  # pragma: no cover - 收尾失败不该让进程退出变慢
            continue
    return closed


def pooled_client_count() -> int:
    """当前池里活着几个客户端（指标与测试用）。"""
    return len([client for client in _POOL.values() if not client.is_closed])


# 增量文本回调：await 它是为了背压——模型在流式输出时，消费方（记日志、广播给界面）
# 也应当是同一节奏，而不是把模型读得快、把回调堆在后台队列里。
DeltaHandler = Callable[[str], Awaitable[None]]

# 单条工具调用参数拼完整时的回调（推测性执行用）。形状与 OpenAI 的 tool_call 一致：
# {"id", "name", "arguments"}。实现方**不得**在回调里阻塞等待工具执行完，否则
# 流式读会被拖慢，推测反而变成串行；正确做法是立刻调度后台任务。
ToolCallHandler = Callable[[dict], Awaitable[None]]


@dataclass
class ChatMessage:
    role: str  # system | user | assistant | tool
    content: str
    tool_call_id: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    # 随这条消息一起给模型看的图片，形如 [{"media_type": "image/png", "data": "<base64>"}]。
    # 只挂在 user 消息上：工具输出里的图片走 artifact，不进上下文。
    # content 仍然保持字符串，四家供应商的差异在各自的 payload 里消化——业务代码
    # 不该为了"某家要 list 某家要字段"分成两套写法。
    images: list[dict] = field(default_factory=list)

    @property
    def text(self) -> str:
        """纯文本部分。压缩、指纹、日志都只该看这个，别把 base64 带进去。"""
        return self.content


def openai_content(message: ChatMessage) -> object:
    """OpenAI chat/completions 的 content：纯文本给字符串，带图给 part 数组。

    没图时保持字符串而不是给单元素数组：部分网关（尤其国内兼容层）对数组形式
    的纯文本支持不全，而纯文本是绝大多数请求——不能为了少数情况让多数情况走险路。
    """
    if not message.images:
        return message.content
    parts: list[dict] = []
    if message.content:
        parts.append({"type": "text", "text": message.content})
    for image in message.images:
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{image.get('media_type', 'image/png')};base64,{image.get('data', '')}"},
            }
        )
    return parts


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
    # 这一份用量的来源：provider=供应商在响应里真的回了 usage；unknown=没回
    # （比如网关吞了 stream_options）。0 个 token 与"没说用了多少"是两回事，
    # 混在一起就会把断供当成免费。对接 task书 §4.2 的 usage_source 统一字段。
    source: str = "unknown"

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
    # 这一笔收到的思考增量字数（0 = 没收到，或这家不推思考）。
    # 判「预算被思考吃光」只看这个数与正文是否为空：`finish_reason=length`
    # 配上非零思考、零正文，就是模型想太久把额度用完了，不是网关不认流。
    reasoning_chars: int = 0
    # 供应商侧这一笔请求的 id（Codex 的 `token_usage_record.response_id` 就是它）。
    # 空串是事实："这家没给 id"，不是"还没实现"——用户报"这轮答得不对"时，
    # 只有这个 id 能在供应商那边定位到具体那一笔；我们的 trace_id 只到自己家门口。
    response_id: str = ""



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
    # 恢复动作给的临时输出预算（0 = 用配置值）。由上层在失败之后设置，
    # 并且**必须用完复位**：它挂在共享的适配器实例上，留着就会影响下一个任务。
    # 适配器自己从不修改它，只读。
    max_tokens_override: int = 0

    def __init__(self, config: AdapterConfig) -> None:
        self.config = config

    def payload_max_tokens(self, model: str = "") -> int:
        """这次请求实际下发多少输出预算（已按模型上限夹紧）。

        三个适配器都从这里取，而不是直接用 ``config.max_tokens``：
        配置是"用户想要多少"，模型上限是"这家给不了更多"，两者取小才是
        一次能成功的请求。
        """
        from blank_api.services.models.budget import clamp_max_tokens

        return clamp_max_tokens(
            self.config.max_tokens,
            model or self.config.model,
            override=self.max_tokens_override,
        )

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
    @asynccontextmanager
    async def _client(self) -> AsyncIterator[httpx.AsyncClient]:
        """客户端出口统一把传输失败翻译成 ModelError。

        流式调用在 `aiter_lines()` 迭代到一半时才抛超时，那一刻没有任何 `try` 兜着：
        异常会一路穿出任务端点，客户收到 500「服务崩了」，而真实的答案是「模型太慢，
        可以重试」。收在这一个出口上，各家适配器的 8 个流式调用点不必各写一遍翻译。

        连接复用（见 :func:`pooled_client`）：**共享的客户端不在这里关闭**。
        一次任务几十步、每步一次模型调用，每次新建客户端等于每次重新握手（https
        还要完整 TLS），而 `httpx.AsyncClient` 本来就是为复用连接设计的。
        """
        factory = self.config.client_factory
        if factory is httpx.AsyncClient:
            # 默认出口：整进程共用一个客户端，连接池跟着复用。
            # 翻译仍然照做——池里那条路同样会超时，不translate 就等于把 500 还回去。
            try:
                yield pooled_client(self.config.timeout_seconds)
            except httpx.TimeoutException:
                raise self._timeout_error() from None
            except httpx.HTTPError as exc:
                raise self._unreachable_error(exc) from None
            return
        client = self._new_client()
        try:
            async with client as opened:
                yield opened
        except httpx.TimeoutException:
            raise self._timeout_error() from None
        except httpx.HTTPError as exc:
            raise self._unreachable_error(exc) from None

    def _timeout_error(self) -> ModelError:
        return ModelError("model_timeout", f"模型请求超时：{self.config.name}", retryable=True)

    def _unreachable_error(self, exc: BaseException) -> ModelError:
        return ModelError(
            "model_unreachable",
            f"模型服务不可达：{self.config.name}（{exc.__class__.__name__}）",
            retryable=True,
        )

    def _new_client(self) -> httpx.AsyncClient:
        factory = self.config.client_factory
        if isinstance(factory, httpx.AsyncClient):
            return factory
        return factory(timeout=httpx.Timeout(self.config.timeout_seconds))

    async def _request_json(self, method: str, url: str, **kwargs) -> dict:
        async with self._client() as client:
            resp = await client.request(method, url, **kwargs)
        if resp.status_code >= 400:
            raise ModelError(
                "model_http_error",
                f"模型返回 {resp.status_code}：{resp.text[:200]}",
                retryable=resp.status_code >= 500,
                status=resp.status_code,
                retry_after=retry_after_seconds(resp.headers.get("Retry-After")),
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
        on_tool_call: ToolCallHandler | None = None,
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
        if on_tool_call is not None:
            # 退路拿到的响应已经完整：逐条回调只是保持一致，省不了等待。
            for call in response.tool_calls or ():
                ready = _ready_tool_call(call)
                if ready is not None:
                    await on_tool_call(ready)
        return response


def _ready_tool_call(call: dict) -> dict | None:
    """把一条完整的工具调用换算成推测执行认识的形状；参数不完整返回 None。"""
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(function, dict):
        return None
    name = str(function.get("name") or "").strip()
    if not name:
        return None
    raw = function.get("arguments")
    if isinstance(raw, dict):
        arguments = raw
    else:
        try:
            arguments = json.loads(str(raw or "{}"))
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(arguments, dict):
        return None
    return {
        "id": str(call.get("id") or ""),
        "name": name,
        "arguments": arguments,
    }


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

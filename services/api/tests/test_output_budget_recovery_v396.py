"""输出预算：请求前夹紧、思考吃光后放大、被切断后接着写。

真机背景（2026-11-17 三方同题对比）：Spark-X2.6 走 OpenAI 兼容通道，
`max_tokens=24576`，模型吐了 6.8 万字思考，正文与工具调用一个字都没有，
任务被判 `model_stream_empty` 失败。同一个提示词把预算调到 65536 就是满分。

也就是说缺的不是模型能力，是三件事：
1. 请求前没按模型上限夹紧 `max_tokens`；
2. 空正文时只看"空"，不看"空之前收过多少思考"，于是把预算问题
   当成网关问题，还差点退回整包再跑一遍同样的思考；
3. 正文被 `finish_reason=length` 切开之后没有"接着写"，只能整轮重来。

这三条与 Claude Code 的请求构造、`thinking_only_retry`、
`truncated_response_recovery` 一一对应，这里按同样的判定次序落地：
先放大预算（结构没变，只是空间不够），再续写（结构已经对了，只是没写完）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services import metrics
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import (
    MAX_BUDGET_RECOVERIES,
    MAX_CONTINUATIONS,
    MAX_THINKING_NUDGES,
    RESUME_PROMPT,
    THINKING_ONLY_PROMPT,
    AgentSession,
    _continuation_wanted,
    _join_continuation,
    _output_budget_exhausted,
    _with_continuation,
)
from blank_api.services.models.base import (
    AdapterConfig,
    ChatMessage,
    ModelAdapter,
    ModelError,
    ModelResponse,
    ToolSpecLite,
    Usage,
)
from blank_api.services.models.budget import (
    DEFAULT_CEILING,
    clamp_max_tokens,
    grown_max_tokens,
    output_ceiling,
)
from blank_api.services.models.openai_compat import OpenAICompatibleAdapter
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway

TOOLS = [ToolSpecLite("read_file", "读文件", {"type": "object", "properties": {}})]
MESSAGES = [ChatMessage(role="user", content="做一个记账页")]
TASK_ID = "task-budget"


# ── 预算表与夹紧 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "model,expected",
    [
        ("spark-x2.6", 65536),
        ("deepseek-reasoner", 65536),
        ("claude-sonnet-4-5", 64000),
        ("gpt-5-mini", 128000),
        ("qwen3-max", 32768),
        ("某个从没见过的小模型", DEFAULT_CEILING),
        ("", DEFAULT_CEILING),
    ],
)
def test_ceiling_is_known_per_family(model, expected):
    assert output_ceiling(model) == expected


def test_clamp_only_lowers_and_never_returns_zero():
    # 用户从别家抄来的大值必须按模型上限收下来
    assert clamp_max_tokens(65536, "claude-3-5-sonnet") == 8192
    # 配置比上限小时不许抬高：配多少是用户的决定，这里只负责"别超"
    assert clamp_max_tokens(4096, "spark-x2.6") == 4096
    # override 同样受上限约束：恢复不能把请求推到会被拒的那一侧
    assert clamp_max_tokens(4096, "claude-3-5-sonnet", override=99999) == 8192
    assert clamp_max_tokens(0, "spark-x2.6") == 1


def test_growth_doubles_until_the_ceiling_then_stops():
    assert grown_max_tokens(24576, "spark-x2.6") == 49152
    assert grown_max_tokens(49152, "spark-x2.6") == 65536
    # 顶满之后原样返回：调用方据此判断"再放大没有落点"
    assert grown_max_tokens(65536, "spark-x2.6") == 65536
    # 小配置也能一次跨过思考的消耗（取翻倍与 +8192 的较大者）
    assert grown_max_tokens(2048, "deepseek-reasoner") == 10240


# ── 判据 ────────────────────────────────────────────────────────────────────


def test_thinking_that_ate_the_budget_counts_as_budget_problem():
    error = ModelError("model_stream_empty", "空", reasoning_events=12, reasoning_chars=68000)
    assert _output_budget_exhausted(error)
    # 供应商说明 length 但没有思考增量，同样算预算问题
    assert _output_budget_exhausted(ModelError("model_stream_empty", "空", finish_reason="length"))


def test_truly_empty_stream_is_not_a_budget_problem():
    """一行都没收到、也没有思考：这是网关不认流，放大预算救不了它。"""
    error = ModelError("model_stream_empty", "空")
    assert not _output_budget_exhausted(error)
    assert not _output_budget_exhausted(ModelError("model_http_error", "限流", status=429))


@pytest.mark.parametrize(
    "text,tool_calls,finish,expected",
    [
        ("写到一半", [], "length", True),
        ("写到一半", [], "max_tokens", True),
        # 写完了：不接
        ("写完了", [], "stop", False),
        # 参数被切是另一条路（model_stream_truncated），接一句只会重写整段调用
        ("", [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}], "length", False),
        # 一个字都没有：没有东西可接，该放大预算
        ("   ", [], "length", False),
        ("有正文但带工具调用", [{"id": "c1", "function": {"name": "x", "arguments": "{}"}}], "length", False),
    ],
)
def test_continuation_only_for_truncated_plain_text(text, tool_calls, finish, expected):
    response = ModelResponse(text=text, tool_calls=tool_calls, finish_reason=finish)
    assert _continuation_wanted(response) is expected


def test_continuation_keeps_the_last_assistant_turn_in_history():
    chat = [ChatMessage(role="user", content="做一个记账页")]
    extended = _with_continuation(chat, "前半段")
    assert [m.role for m in extended] == ["user", "assistant", "user"]
    assert extended[1].content == "前半段"
    # 提示词要能抑制"重写一遍"：那是把已经推给用户的正文再发一次
    assert "no apology" in extended[2].content.lower()
    assert extended[2].content == RESUME_PROMPT
    # 原列表不被就地改写：它还要用于下一轮
    assert len(chat) == 1


def test_joined_response_only_replaces_text():
    last = ModelResponse(text="后半段", model="m", finish_reason="stop", response_id="r2",
                         usage=Usage(prompt_tokens=10, completion_tokens=5))
    merged = _join_continuation(["前半段", "中段"], last)
    assert merged.text == "前半段中段后半段"
    assert merged.response_id == "r2"
    assert merged.usage.prompt_tokens == 10


# ── OpenAI 兼容通道：思考增量必须被看见 ────────────────────────────────────


def _sse(*chunks: dict) -> bytes:
    return "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks).encode()


def _compat(body: bytes) -> tuple[OpenAICompatibleAdapter, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    config = AdapterConfig(
        name="spark",
        kind="openai_compatible",
        base_url="https://example.test/v1",
        api_key="k",
        model="spark-x2.6",
        max_tokens=24576,
        supports_tools=True,
        client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw),
    )
    return OpenAICompatibleAdapter(config), seen


async def _drain(adapter: OpenAICompatibleAdapter) -> ModelResponse:
    return await adapter.bind_tools_stream(TOOLS, MESSAGES)


async def test_reasoning_content_is_counted_on_the_compat_channel():
    adapter, _ = _compat(_sse(
        {"choices": [{"delta": {"reasoning_content": "先想"}}]},
        {"choices": [{"delta": {"reasoning": "再想"}}]},
        {"choices": [{"delta": {"content": "结论"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ))
    response = await _drain(adapter)
    assert response.text == "结论"
    # 两个键都认：DeepSeek/vLLM 推 reasoning_content，少数网关推 reasoning
    assert response.reasoning_chars == len("先想再想")


async def test_reasoning_only_stream_reports_why_it_was_empty():
    adapter, _ = _compat(_sse(
        {"choices": [{"delta": {"reasoning_content": "想了六千八百字"}}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
    ))
    with pytest.raises(ModelError) as caught:
        await _drain(adapter)
    error = caught.value
    assert error.code == "model_stream_empty"
    # 计数要传出去，否则上层只能看到"空"，会把预算问题当成网关问题
    assert error.reasoning_events == 1
    assert error.reasoning_chars == len("想了六千八百字")
    assert error.finish_reason == "length"
    assert "思考增量" in error.message


async def test_plain_empty_stream_does_not_claim_to_be_thinking():
    adapter, _ = _compat(b"")
    with pytest.raises(ModelError) as caught:
        await _drain(adapter)
    assert caught.value.reasoning_events == 0
    assert "思考增量" not in caught.value.message


async def test_request_budget_is_clamped_to_the_model_ceiling():
    adapter, seen = _compat(_sse({"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]}))
    adapter.config.max_tokens = 999999
    await _drain(adapter)
    sent = json.loads(seen[0].content.decode())
    assert sent["max_tokens"] == 65536


# ── 会话层：真的放大、真的续写、真的复位 ────────────────────────────────────


class BudgetHungryAdapter(ModelAdapter):
    """前 `failures` 次"只想不说"，之后正常。记录每次下发的预算。"""

    def __init__(self, failures: int, *, model: str = "spark-x2.6", max_tokens: int = 24576) -> None:
        super().__init__(
            AdapterConfig(
                name="hungry",
                kind="openai_compatible",
                base_url="https://example.test/v1",
                api_key="k",
                model=model,
                max_tokens=max_tokens,
                supports_tools=True,
            )
        )
        self.failures = failures
        self.calls = 0
        self.sent_budgets: list[int] = []

    async def _attempt(self) -> ModelResponse:
        self.calls += 1
        self.sent_budgets.append(self.payload_max_tokens())
        if self.calls <= self.failures:
            raise ModelError(
                "model_stream_empty",
                "流式响应为空（期间收到 9 个思考增量、68000 字）",
                reasoning_events=9,
                reasoning_chars=68000,
                finish_reason="length",
            )
        return ModelResponse(text="好了", model=self.config.model, finish_reason="stop")

    async def invoke(self, messages, **kwargs):
        return await self._attempt()

    async def bind_tools(self, tools, messages, **kwargs):
        return await self._attempt()

    async def bind_tools_stream(self, tools, messages, *, on_delta=None, **kwargs):
        return await self._attempt()

    async def stream(self, messages, **kwargs):
        yield "好了"


class TruncatingAdapter(ModelAdapter):
    """按剧本返回若干段：前几段 `length`，最后一段 `stop`。"""

    def __init__(self, parts: list[tuple[str, str]], *, model: str = "spark-x2.6") -> None:
        super().__init__(
            AdapterConfig(
                name="trunc", kind="openai_compatible", base_url="https://example.test/v1",
                api_key="k", model=model, max_tokens=16384, supports_tools=True,
            )
        )
        self.parts = parts
        self.calls = 0
        self.seen_messages: list[list[ChatMessage]] = []

    async def _attempt(self, messages) -> ModelResponse:
        self.calls += 1
        self.seen_messages.append(list(messages))
        text, finish = self.parts[min(self.calls - 1, len(self.parts) - 1)]
        return ModelResponse(text=text, model=self.config.model, finish_reason=finish)

    async def invoke(self, messages, **kwargs):
        return await self._attempt(messages)

    async def bind_tools(self, tools, messages, **kwargs):
        return await self._attempt(messages)

    async def bind_tools_stream(self, tools, messages, *, on_delta=None, **kwargs):
        return await self._attempt(messages)

    async def stream(self, messages, **kwargs):
        yield "x"


@pytest.fixture(autouse=True)
async def schema():
    metrics.reset()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="p-budget", name="预算"))
        session.add(Task(id=TASK_ID, project_id="p-budget", title="预算", goal="预算"))
        await session.commit()
    yield
    metrics.reset()


def _session(tmp_path, adapter: ModelAdapter) -> AgentSession:
    return AgentSession(
        adapter=adapter,
        gateway=ToolGateway([]),
        context=ToolContext(root=tmp_path, task_id=TASK_ID, session_factory=get_session_factory()),
        recorder=EventRecorder(get_session_factory(), TASK_ID),
        session_factory=get_session_factory(),
        provider_id="p",
        provider_name=adapter.config.name,
        goal="预算",
    )


async def _events(kind: str) -> list[dict]:
    from sqlalchemy import select

    async with get_session_factory()() as session:
        rows = (
            await session.execute(select(AgentEvent).where(AgentEvent.type == kind))
        ).scalars().all()
    return [row.payload for row in rows]


async def test_thinking_that_ate_the_budget_is_retried_with_more_room(tmp_path):
    adapter = BudgetHungryAdapter(failures=1)
    session = _session(tmp_path, adapter)

    response, _ = await session.call_model([{"role": "user", "content": "做一个记账页"}], "execute_action")

    assert response.text == "好了"
    assert adapter.calls == 2
    # 第一次 24576，第二次放大到 49152
    assert adapter.sent_budgets == [24576, 49152]
    raised = await _events("model.budget_raised")
    assert len(raised) == 1
    assert raised[0]["from"] == 24576 and raised[0]["to"] == 49152
    assert raised[0]["reasoning_events"] == 9
    # 用完必须复位：它挂在共享实例上，漏出去会让下一个任务用着这一轮的值
    assert adapter.max_tokens_override == 0


async def test_budget_recovery_is_bounded_then_fails_with_the_real_reason(tmp_path):
    adapter = BudgetHungryAdapter(failures=99)
    session = _session(tmp_path, adapter)

    with pytest.raises(ModelError) as caught:
        await session.call_model([{"role": "user", "content": "做一个记账页"}], "execute_action")

    # 放大到头仍然空：根因要换成可行动的那句话，而不是「流式响应为空」
    assert caught.value.code == "model_reasoning_exhausted"
    assert "思考" in caught.value.message and "最大输出 token" in caught.value.message
    # 首启一次 + 恢复预算次，一次都不许多
    assert adapter.calls == MAX_BUDGET_RECOVERIES + 1
    assert adapter.max_tokens_override == 0


async def test_no_retry_when_the_ceiling_is_already_reached(tmp_path):
    """已经顶到模型上限：再重发一次注定一样，直接失败。"""
    adapter = BudgetHungryAdapter(failures=1, model="claude-3-5-sonnet", max_tokens=8192)
    session = _session(tmp_path, adapter)

    with pytest.raises(ModelError) as caught:
        await session.call_model([{"role": "user", "content": "做一个记账页"}], "execute_action")

    # 一次都没重发，但用户看到的仍然是「预算不够」而不是「网络空流」
    assert caught.value.code == "model_reasoning_exhausted"
    assert adapter.calls == 1
    assert await _events("model.budget_raised") == []


async def test_truncated_text_is_continued_instead_of_restarted(tmp_path):
    adapter = TruncatingAdapter([("前半段，", "length"), ("后半段。", "stop")])
    session = _session(tmp_path, adapter)

    response, _ = await session.call_model([{"role": "user", "content": "写一段说明"}], "execute_action")

    assert response.text == "前半段，后半段。"
    assert adapter.calls == 2
    # 第二次请求必须带上"已经说出去的那一段"，否则模型会从头重写
    second = adapter.seen_messages[1]
    assert second[-2].role == "assistant" and second[-2].content == "前半段，"
    assert second[-1].content == RESUME_PROMPT
    assert len(await _events("model.continuation")) == 1


async def test_continuation_stops_at_the_limit_and_says_so(tmp_path):
    adapter = TruncatingAdapter([("永远写不完", "length")])
    session = _session(tmp_path, adapter)

    response, _ = await session.call_model([{"role": "user", "content": "写一段说明"}], "execute_action")

    assert adapter.calls == MAX_CONTINUATIONS + 1
    assert response.text.count("永远写不完") == MAX_CONTINUATIONS + 1
    # 接满了还是被切：正文照给，但必须留下"它是残缺的"这个事实
    exhausted = await _events("model.continuation_exhausted")
    assert len(exhausted) == 1
    assert exhausted[0]["attempts"] == MAX_CONTINUATIONS


async def test_completed_answer_is_not_continued(tmp_path):
    adapter = TruncatingAdapter([("一句就写完了。", "stop")])
    session = _session(tmp_path, adapter)

    response, _ = await session.call_model([{"role": "user", "content": "写一句"}], "execute_action")

    assert response.text == "一句就写完了。"
    assert adapter.calls == 1
    assert await _events("model.continuation") == []


# ── 只想不说：催一句，而不是重发同一句 ──────────────────────────────────────
# Claude Code 2.1.271 的 `query_thinking_only_response` 就是这个动作：
# 结束原因是正常收尾、但没有任何可见输出时，注入一条 meta 提示并重试一次。
# 重发同一句等于让模型把同一段思考再走一遍，结果还是空。


class SilentAdapter(ModelAdapter):
    """前 `silent` 次「正常收尾但一个字都没说」，之后正常。"""

    def __init__(self, silent: int, *, blank_success: bool = False, model: str = "gpt-5-mini") -> None:
        super().__init__(
            AdapterConfig(
                name="silent", kind="openai_compatible", base_url="https://example.test/v1",
                api_key="k", model=model, max_tokens=16384, supports_tools=True,
            )
        )
        self.silent = silent
        self.blank_success = blank_success
        self.calls = 0
        self.seen_messages: list[list[ChatMessage]] = []

    async def _attempt(self, messages) -> ModelResponse:
        self.calls += 1
        self.seen_messages.append(list(messages))
        if self.calls <= self.silent:
            if self.blank_success:
                # 有些通道「只想了没说」时给的是正常收尾的空响应，不抛错
                return ModelResponse(text="   ", model=self.config.model, finish_reason="stop")
            raise ModelError(
                "model_stream_empty",
                "流式响应为空",
                reasoning_events=0,
                reasoning_chars=0,
                finish_reason="stop",
            )
        return ModelResponse(text="这次说了", model=self.config.model, finish_reason="stop")

    async def invoke(self, messages, **kwargs):
        return await self._attempt(messages)

    async def bind_tools(self, tools, messages, **kwargs):
        return await self._attempt(messages)

    async def bind_tools_stream(self, tools, messages, *, on_delta=None, **kwargs):
        return await self._attempt(messages)

    async def stream(self, messages, **kwargs):
        yield "x"


class _ReasoningBlankAdapter(ModelAdapter):
    """第一次「思考了 800 字、正文空白」，第二次正常。"""

    def __init__(self, model: str = "gpt-5-mini") -> None:
        super().__init__(
            AdapterConfig(
                name="reasoning-blank", kind="openai_compatible", base_url="https://example.test/v1",
                api_key="k", model=model, max_tokens=16384, supports_tools=True,
            )
        )
        self.calls = 0

    async def _attempt(self, messages) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text="   ", model=self.config.model, finish_reason="stop", reasoning_chars=800
            )
        return ModelResponse(text="这次说了", model=self.config.model, finish_reason="stop")

    async def invoke(self, messages, **kwargs):
        return await self._attempt(messages)

    async def bind_tools(self, tools, messages, **kwargs):
        return await self._attempt(messages)

    async def bind_tools_stream(self, tools, messages, *, on_delta=None, **kwargs):
        return await self._attempt(messages)

    async def stream(self, messages, **kwargs):
        yield "x"

async def test_silent_turn_is_nudged_instead_of_resent(tmp_path):
    adapter = SilentAdapter(silent=1)
    session = _session(tmp_path, adapter)

    response, _ = await session.call_model([{"role": "user", "content": "做一个记账页"}], "execute_action")

    assert response.text == "这次说了"
    assert adapter.calls == 2
    # 第二次发出去的最后一句必须是催促，而不是原问题
    second = adapter.seen_messages[1]
    assert second[-1].content == THINKING_ONLY_PROMPT
    assert second[-1].content != "做一个记账页"
    nudged = await _events("model.thinking_only")
    assert len(nudged) == 1
    assert nudged[0]["attempt"] == 1
    # 催促不是预算恢复：预算一动都没动
    assert await _events("model.budget_raised") == []


async def test_blank_success_without_reasoning_is_left_to_the_empty_guard(tmp_path):
    """连思考都没有的纯空白不在这里催：那是 `ask` 的空响应闸门管的。

    两条路径叠在一起会连发三次一模一样的请求——催两次、`ask` 再重试一次——
    真机上就是"卡两分钟再失败，失败原因还写着空响应"。
    """
    adapter = SilentAdapter(silent=1, blank_success=True)
    session = _session(tmp_path, adapter)

    response, _ = await session.call_model([{"role": "user", "content": "做一个记账页"}], "execute_action")

    assert response.text.strip() == ""
    assert adapter.calls == 1
    assert await _events("model.thinking_only") == []


async def test_blank_success_with_reasoning_is_nudged(tmp_path):
    """想过但没说是这一条要治的：确有思考增量的空白响应，催一句。"""

    adapter = _ReasoningBlankAdapter()
    session = _session(tmp_path, adapter)

    response, _ = await session.call_model([{"role": "user", "content": "做一个记账页"}], "execute_action")

    assert response.text == "这次说了"
    assert adapter.calls == 2
    assert len(await _events("model.thinking_only")) == 1


async def test_nudging_is_bounded_and_then_reports_the_empty_stream(tmp_path):
    adapter = SilentAdapter(silent=99)
    session = _session(tmp_path, adapter)

    with pytest.raises(ModelError) as caught:
        await session.call_model([{"role": "user", "content": "做一个记账页"}], "execute_action")

    # 催不回来就是真的没话说：原样报空流，不伪装成预算问题
    assert caught.value.code == "model_stream_empty"
    assert adapter.calls == MAX_THINKING_NUDGES + 1
    assert len(await _events("model.thinking_only")) == MAX_THINKING_NUDGES

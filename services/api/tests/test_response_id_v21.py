"""供应商那一笔请求的 id 要一路带到账本与事件里（对标 Codex `token_usage_record.response_id`）。

之所以要记这个 id，是因为它决定了**出问题时能不能去供应商那边对账**：DeepSeek 的
后台、网关的日志都按 request id 索引，而我们的 `trace_id` 只在本地有意义——
「这笔在 DeepSeek 那边是哪一笔」原来回答不了（docs/REA-WAVE4-CODEX-RUNTIME.md §7.3 缺口 3）。

这一组断言管四个方向，每个都能失败：

1. 三家供应商的 id 都能从各自的形状里取到（chat/completions 的 `id`、
   Responses 的 `id`、Anthropic 的 `message_start.id`）；流式还要能退回响应头；
2. **取不到就写空串**——不许拿我们的 uuid 或 trace_id 顶上。一个像是 id 的字符串
   比空串坏得多：它会让人去供应商后台搜一个根本不存在的东西；
3. 这个 id 同时落进 `model_usage` 行和 `model.usage` 事件的 `call` 口径，
   两处必须相等（否则事件说得漂亮、表里没有，取证时以哪边为准？）；
4. 子代理与失败调用走同一套：子代理的 id 是子线程那一笔的；失败的调用没有响应，
   所以 id 是空串——但那一行必须在。
"""

import json

import httpx
import pytest

from blank_api.db import get_session_factory
from blank_api.models.model_provider import ModelUsage
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import AgentSession
from blank_api.services.models.anthropic import AnthropicAdapter
from blank_api.services.models.base import (
    AdapterConfig,
    ChatMessage,
    ModelAdapter,
    ModelError,
    ModelResponse,
    ToolSpecLite,
    Usage,
)
from blank_api.services.models.ollama import OllamaAdapter
from blank_api.services.models.openai_compat import OpenAICompatibleAdapter
from blank_api.services.models.openai_responses import OpenAIResponsesAdapter
from blank_api.services.subagent import SubAgentRequest, run_subagent
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway

TASK_ID = "task-response-id-1"
PROVIDER_ID = "provider-response-id"
TOOLS = [ToolSpecLite("read_file", "读文件", {"type": "object", "properties": {}})]


def _config(kind: str, **overrides) -> AdapterConfig:
    base = dict(
        name="t",
        kind=kind,
        base_url="https://example.test/v1",
        api_key="k",
        model="m",
        supports_tools=True,
    )
    base.update(overrides)
    return AdapterConfig(**base)


def _adapter(kind: str, body: str, *, headers: dict[str, str] | None = None, status: int = 200):
    """把响应体固定成 body 的适配器；`headers` 用来模拟只在响应头里给 request id 的网关。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body, headers={"content-type": "text/event-stream", **(headers or {})})

    def client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    config = _config(kind, client_factory=client)
    return {
        "openai_compatible": OpenAICompatibleAdapter,
        "anthropic": AnthropicAdapter,
        "responses": OpenAIResponsesAdapter,
        "ollama": OllamaAdapter,
    }[kind](config)


def _sse(chunks: list[dict]) -> str:
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


# ------------------------------------------------------- 1. 各家形状里的 id


async def test_openai_non_stream_takes_the_completion_id():
    body = (
        '{"id":"chatcmpl-0802a","model":"m1","choices":[{"message":{"content":"好"},'
        '"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":2}}'
    )
    adapter = _adapter("openai_compatible", body)

    response = await adapter.bind_tools(TOOLS, [ChatMessage("user", "hi")])

    assert response.response_id == "chatcmpl-0802a"


async def test_openai_stream_takes_the_id_from_the_frames():
    body = _sse(
        [
            {"id": "chatcmpl-stream-1", "choices": [{"delta": {"content": "好"}}]},
            {"id": "chatcmpl-stream-1", "choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
    )
    adapter = _adapter("openai_compatible", body)

    response = await adapter.bind_tools_stream(TOOLS, [ChatMessage("user", "hi")])

    assert response.response_id == "chatcmpl-stream-1"


async def test_openai_stream_falls_back_to_the_request_id_header():
    """自建网关常把 id 只放在响应头（帧里不带），这时头里的值就是唯一可对账的 id。"""
    body = _sse([{"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]}])
    adapter = _adapter("openai_compatible", body, headers={"x-request-id": "req-header-77"})

    response = await adapter.bind_tools_stream(TOOLS, [ChatMessage("user", "hi")])

    assert response.response_id == "req-header-77"


async def test_openai_stream_without_any_id_is_an_empty_string():
    # 这一条是整套设计的关键：没有就是没有，不许造一个
    body = _sse([{"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]}])
    adapter = _adapter("openai_compatible", body)

    response = await adapter.bind_tools_stream(TOOLS, [ChatMessage("user", "hi")])

    assert response.response_id == ""


async def test_gateway_that_ignores_stream_still_reports_its_id():
    """不认 stream 的网关会把整包 JSON 一次返回；这条路也要带 id，否则退回非流式就丢了可对账的那一笔。"""
    whole = (
        '{"id":"chatcmpl-whole-3","choices":[{"message":{"content":"好"},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":8,"completion_tokens":2}}'
    )
    adapter = _adapter("openai_compatible", whole)

    response = await adapter.bind_tools_stream(TOOLS, [ChatMessage("user", "hi")])

    assert response.response_id == "chatcmpl-whole-3"
    assert response.text == "好"


async def test_openai_responses_api_takes_the_response_id():
    body = '{"id":"resp_0802b","output_text":"好","usage":{"input_tokens":5,"output_tokens":2}}'
    adapter = _adapter("responses", body)

    response = await adapter.bind_tools(TOOLS, [ChatMessage("user", "hi")])

    assert response.response_id == "resp_0802b"


async def test_anthropic_takes_the_message_id_in_both_modes():
    whole = (
        '{"id":"msg_0802c","type":"message","role":"assistant","content":[{"type":"text","text":"好"}],'
        '"usage":{"input_tokens":5,"output_tokens":2},"stop_reason":"end_turn"}'
    )
    non_stream = _adapter("anthropic", whole)
    assert (await non_stream.bind_tools(TOOLS, [ChatMessage("user", "hi")])).response_id == "msg_0802c"

    stream = _sse(
        [
            {"type": "message_start", "message": {"id": "msg_0802d", "usage": {"input_tokens": 5}}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "好"}},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 2},
            },
        ]
    )
    assert (await _adapter("anthropic", stream).bind_tools_stream(TOOLS, [ChatMessage("user", "hi")])).response_id == "msg_0802d"


async def test_ollama_has_no_request_id_and_says_so():
    """本地进程没有供应商侧的那一笔：留空串，不许用我们的 id 冒充。"""
    body = '{"model":"m","done":true,"message":{"role":"assistant","content":"好"},"prompt_eval_count":5,"eval_count":2}'
    adapter = _adapter("ollama", body)

    response = await adapter.bind_tools(TOOLS, [ChatMessage("user", "hi")])

    assert response.response_id == ""


# ----------------------------------------------------- 账本：行与事件同源


class _IdAdapter(ModelAdapter):
    """按脚本返回带 id 的响应；None 表示这一次调用直接失败。"""

    def __init__(self, script: list[tuple[str, Usage] | None]) -> None:
        super().__init__(
            AdapterConfig(
                name="mock",
                kind="openai_compatible",
                base_url="",
                api_key="",
                model="mock",
                supports_tools=True,
            )
        )
        self.script = script
        self.calls = 0

    async def invoke(self, messages, **kwargs):  # pragma: no cover - 不走这条路
        raise NotImplementedError

    async def stream(self, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def bind_tools(self, tools, messages, **kwargs):
        item = self.script[self.calls]
        self.calls += 1
        if item is None:
            raise ModelError("rate_limited", "限流", retryable=True)
        response_id, usage = item
        return ModelResponse(text="好", usage=usage, model="mock", response_id=response_id)


async def _seed() -> None:
    from blank_api.db import Base, get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="project-response-id", name="响应 id"))
        session.add(
            Task(id=TASK_ID, project_id="project-response-id", title="响应 id", goal="响应 id", max_steps=10)
        )
        await session.commit()


@pytest.fixture
async def seeded():
    await _seed()


def _session(tmp_path, adapter: ModelAdapter) -> AgentSession:
    factory = get_session_factory()
    return AgentSession(
        adapter=adapter,
        gateway=ToolGateway([]),
        context=ToolContext(root=tmp_path, task_id=TASK_ID, session_factory=factory),
        recorder=EventRecorder(factory, TASK_ID),
        session_factory=factory,
        provider_id=PROVIDER_ID,
        provider_name="mock",
        goal="响应 id",
        max_steps=10,
    )


async def _usage_rows() -> list:
    async with get_session_factory()() as session:
        rows = (await session.execute(ModelUsage.__table__.select())).fetchall()
    return list(rows)


async def _records() -> list[tuple[str, dict]]:
    """(事件的 node, payload) 按 seq 有序返回。

    用量行不能按顺序断言：主键是 uuid，`order_by(id)` 排出的是随机顺序，
    同一份代码跑两次可能一绿一红。事件有 seq，才是可依赖的时间顺序。
    """
    async with get_session_factory()() as session:
        rows = (await session.execute(AgentEvent.__table__.select().order_by(AgentEvent.seq))).fetchall()
    return [(row._mapping["node"], row.payload) for row in rows if row.type == "model.usage"]


def _row_ids(rows: list) -> dict[str, str]:
    """用量行按 node 归位（同一任务里每个节点各一笔时，node 就是稳定的对照键）。"""
    return {row._mapping["node"]: row._mapping["response_id"] for row in rows}


def _event_ids(records: list[tuple[str, dict]]) -> dict[str, str]:
    return {node: payload["call"]["response_id"] for node, payload in records}


async def test_response_id_lands_in_both_the_row_and_the_event(seeded, tmp_path):
    adapter = _IdAdapter(
        [
            ("chatcmpl-main-1", Usage(prompt_tokens=100, completion_tokens=10)),
            ("chatcmpl-main-2", Usage(prompt_tokens=200, completion_tokens=20)),
        ]
    )
    session = _session(tmp_path, adapter)
    await session.call_model([{"role": "user", "content": "第一步"}], "execute_action", step=1)
    await session.call_model([{"role": "user", "content": "第二步"}], "review_result", step=2)

    rows = await _usage_rows()
    records = await _records()
    assert _row_ids(rows) == {"execute_action": "chatcmpl-main-1", "review_result": "chatcmpl-main-2"}
    # 行与事件必须指向同一个 id：取证时以哪边为准，取决于它们是否可能分叉
    assert _event_ids(records) == _row_ids(rows)
    # 这个 id 是供应商给的，不是我们本地生成的任何东西——否则整条记录就失去意义
    for row in rows:
        assert row._mapping["response_id"] != row._mapping["trace_id"]
        assert row._mapping["response_id"] != row._mapping["id"]
    # 三级口径说的是"花了多少"，这一条说的是"去哪家查这一笔"；两个口径都要在同一个 payload 里
    assert {"call", "turn", "task"} <= set(records[0][1])
    assert len(records) == len(rows) == 2, "一次调用一行一条事件，谁双发或漏发都在这条上露出来"


async def test_failed_call_keeps_the_row_with_an_empty_id(seeded, tmp_path):
    """失败的调用没有响应体，所以 id 只能是空串——但那一行必须在。"""
    adapter = _IdAdapter([None])
    session = _session(tmp_path, adapter)

    with pytest.raises(ModelError):
        await session.call_model([{"role": "user", "content": "第一步"}], "execute_action", step=1)

    rows = await _usage_rows()
    assert len(rows) == 1
    assert rows[0]._mapping["response_id"] == ""
    assert rows[0]._mapping["status"] == "error"
    assert (await _records())[0][1]["call"]["response_id"] == ""


async def test_summarizer_call_is_traceable_too(seeded, tmp_path):
    """压缩历史那一笔是最容易被漏掉的：它不走 call_model，直接调 adapter.invoke。"""

    class _InvokeAdapter(_IdAdapter):
        async def invoke(self, messages, **kwargs):
            response_id, usage = self.script[self.calls]
            self.calls += 1
            return ModelResponse(text='{"goal":"x"}', usage=usage, model="mock", response_id=response_id)

    adapter = _InvokeAdapter([("chatcmpl-sum-9", Usage(prompt_tokens=50, completion_tokens=5))])
    session = _session(tmp_path, adapter)
    await session.summarize_history([{"role": "user", "content": "很久以前的历史"}], "")

    rows = await _usage_rows()
    assert _row_ids(rows) == {"context_compact": "chatcmpl-sum-9"}
    assert rows[0]._mapping["node"] == "context_compact"


async def test_subagent_row_carries_its_own_response_id(seeded, tmp_path):
    """子线程那一笔的钱要能单独去供应商那边查。"""
    adapter = _IdAdapter([("chatcmpl-sub-3", Usage(prompt_tokens=30, completion_tokens=3))])
    factory = get_session_factory()
    await run_subagent(
        adapter=adapter,
        tools=[],
        root=tmp_path,
        task_id=TASK_ID,
        session_factory=factory,
        recorder=EventRecorder(factory, TASK_ID),
        request=SubAgentRequest(goal="读 README 并复述", max_steps=2),
        agent_path="root/sub-1",
        depth=1,
        parent_thread_id="root-thread",
        provider_id=PROVIDER_ID,
        provider_name="mock",
    )

    rows = await _usage_rows()
    sub_rows = [row for row in rows if row._mapping["node"] == "subagent"]
    assert [row._mapping["response_id"] for row in sub_rows] == ["chatcmpl-sub-3"]
    sub_records = [payload for _, payload in await _records() if payload.get("role") == "subagent"]
    assert sub_records[0]["call"]["response_id"] == "chatcmpl-sub-3"

"""流式输出：碎片聚合、增量推送、以及"流不了就退回去"。

这一组测试的存在理由只有一条：**流式绝不能改变执行结果**。
文字可以边到边到，但工具参数必须在流结束、拼完整之后才交给执行层——
少一个引号就是改错文件，而错会出现在几十行之后，届时看不出根因在解析层。

所以断言分三层：
1. 聚合器按 index 拼、缺 id 补稳定 id、没有名字的调用直接丢掉；
2. 三家供应商的 SSE 方言各自能拼出正确响应（含 usage）；
3. 接进图谱之后：开关关着什么都不变，开着会推增量，流不出来会自动退回非流式，
   但"已经收到文字之后再出错"不许退（重跑会让用户看到两遍）。
"""

import json
from pathlib import Path

import httpx
import pytest

from blank_api.db import get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import AgentSession
from blank_api.services.agent.live import TAIL_CHARS, LiveFeed, OutputRelay
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
from blank_api.services.models.openai_compat import OpenAICompatibleAdapter
from blank_api.services.models.streaming import ToolCallAccumulator
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway

TOOLS = [ToolSpecLite("read_file", "读文件", {"type": "object", "properties": {}})]
TASK_ID = "task-stream"


# --------------------------------------------------------------- 聚合器


def test_fragments_are_joined_by_index():
    calls = ToolCallAccumulator()
    calls.add(0, call_id="call_a", name="read_file", arguments='{"pa')
    calls.add(0, arguments='th": "a.py"}')

    result = calls.as_list()

    assert len(result) == 1
    assert result[0]["id"] == "call_a"
    assert result[0]["function"]["name"] == "read_file"
    # 参数必须是一个能解析的完整 JSON：这是"拼接正确"的唯一判据
    assert json.loads(result[0]["function"]["arguments"]) == {"path": "a.py"}


def test_two_calls_keep_their_own_arguments():
    calls = ToolCallAccumulator()
    calls.add(0, call_id="a", name="read_file", arguments='{"path": "x"}')
    calls.add(1, call_id="b", name="run_shell", arguments='{"cmd": "ls"}')

    result = calls.as_list()

    assert [item["function"]["name"] for item in result] == ["read_file", "run_shell"]
    assert [item["id"] for item in result] == ["a", "b"]


def test_missing_id_gets_a_stable_one():
    # 自建网关常常不回 id，而工具结果回填必须能对上号，对不上就是一次调用丢失
    calls = ToolCallAccumulator()
    calls.add(3, name="read_file", arguments="{}")

    assert calls.as_list()[0]["id"] == "call_3"


def test_call_without_a_name_is_dropped():
    calls = ToolCallAccumulator()
    calls.add(0, call_id="x", arguments='{"a": 1}')

    # 没有名字的调用不可能执行，留着只会在执行层变成"未注册的工具："
    assert calls.as_list() == []
    assert len(calls) == 1  # 但"收到过碎片"这件事要能看见


# --------------------------------------------------- 供应商各自的 SSE 方言


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


def _adapter(kind: str, body: str, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body, headers={"content-type": "text/event-stream"})

    def client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    config = _config(kind, client_factory=client)
    if kind == "anthropic":
        return AnthropicAdapter(config)
    return OpenAICompatibleAdapter(config)


async def test_openai_stream_aggregates_text_tools_and_usage():
    chunks = [
        {"model": "m1", "choices": [{"delta": {"content": "我先"}}]},
        {"model": "m1", "choices": [{"delta": {"content": "看看"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "call_1", "function": {"name": "read_file", "arguments": "{\"pa"}}
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'th": "a.py"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 30}},
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    adapter = _adapter("openai_compatible", body)

    seen: list[str] = []

    async def on_delta(piece: str) -> None:
        seen.append(piece)

    response = await adapter.bind_tools_stream(TOOLS, [ChatMessage("user", "读 a.py")], on_delta=on_delta)

    assert response.text == "我先看看"
    # 回调是按片给的，聚合后才是全文
    assert seen == ["我先", "看看"]
    assert response.model == "m1"
    assert response.finish_reason == "tool_calls"
    assert json.loads(response.tool_calls[0]["function"]["arguments"]) == {"path": "a.py"}
    # usage 靠 stream_options.include_usage 才有，缺了会记成"这次免费"
    assert response.usage.prompt_tokens == 120
    assert response.usage.completion_tokens == 30


async def test_openai_stream_requests_usage_explicitly():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')

    def client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    adapter = OpenAICompatibleAdapter(_config("openai_compatible", client_factory=client))

    await adapter.bind_tools_stream(TOOLS, [ChatMessage("user", "hi")])

    assert captured["stream"] is True
    assert captured["stream_options"] == {"include_usage": True}


async def test_empty_stream_is_an_error_not_an_empty_reply():
    """一行 data: 都没有、也不是完整响应：这才是真的空。

    静默当成"模型什么都没说"是最坏的结果——任务会以"完成"结束，但什么也没做。
    """
    adapter = _adapter("openai_compatible", "")

    with pytest.raises(ModelError) as error:
        await adapter.bind_tools_stream(TOOLS, [ChatMessage("user", "hi")])

    assert error.value.code == "model_stream_empty"


async def test_whole_json_body_is_used_in_place():
    """网关不认 stream、把整包 JSON 一次返回：就地认掉，不再发第二次请求。

    重发一次的代价不只是钱：配额、延迟、以及"同一步被算成两步"的记账全都跟着错。
    """
    adapter = _adapter("openai_compatible", '{"choices":[{"message":{"content":"hi"}}]}')

    seen: list[str] = []
    response = await adapter.bind_tools_stream(
        TOOLS, [ChatMessage("user", "hi")], on_delta=lambda piece: _collect(seen, piece)
    )

    assert response.text == "hi"
    # 整包也要推一次，否则界面等到最后才蹦出全文
    assert seen == ["hi"]


async def test_unrecognisable_body_still_raises():
    """能解析不等于能用：没有 choices.message 的形状不能被当成完整响应。

    放宽成"能解析就算"，会把一次流式回包误认成整包，结果是静默空回复。
    """
    adapter = _adapter("openai_compatible", '{"error":{"message":"quota exceeded"}}')

    with pytest.raises(ModelError) as error:
        await adapter.bind_tools_stream(TOOLS, [ChatMessage("user", "hi")])

    assert error.value.code == "model_stream_empty"


async def test_anthropic_stream_splits_usage_across_two_events():
    events = [
        {"type": "message_start", "message": {"model": "claude-x", "usage": {"input_tokens": 100, "cache_read_input_tokens": 40}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "答案"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tu_1", "name": "read_file"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"path"'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": ': "a.py"}'}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 12}},
    ]
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    adapter = _adapter("anthropic", body)

    seen: list[str] = []
    response = await adapter.bind_tools_stream(
        TOOLS, [ChatMessage("user", "读")], on_delta=lambda piece: _collect(seen, piece)
    )

    assert response.text == "答案"
    assert json.loads(response.tool_calls[0]["function"]["arguments"]) == {"path": "a.py"}
    assert response.finish_reason == "tool_use"
    # input_tokens 不含缓存部分：100 + 40 才是这次真实输入
    assert response.usage.prompt_tokens == 140
    assert response.usage.cached_tokens == 40
    assert response.usage.completion_tokens == 12


async def _collect(sink: list[str], piece: str) -> None:
    sink.append(piece)


# ------------------------------------------------------------- 继电器


async def test_relay_sends_tails_not_full_text():
    LiveFeed.reset()
    queue = LiveFeed.subscribe(TASK_ID)
    relay = OutputRelay(TASK_ID, "execute_action")

    await relay("a" * 200)
    first = queue.get_nowait()
    assert first["chars"] == 200
    assert first["text"] == "a" * 200
    assert first["done"] is False

    relay.close()
    last = queue.get_nowait()
    # done 帧让界面知道这一屏说完了，可以清掉
    assert last["done"] is True
    assert last["chars"] >= 200
    LiveFeed.reset()


async def test_relay_keeps_only_the_tail():
    LiveFeed.reset()
    queue = LiveFeed.subscribe(TASK_ID)
    relay = OutputRelay(TASK_ID, "review_result")

    for _ in range(10):
        await relay("x" * 200)  # 累计 2000 字，超过尾窗
    relay.close()

    frames = []
    while not queue.empty():
        frames.append(queue.get_nowait())

    assert frames, "攒了一整段却一帧都没推"
    # 每一帧都只是尾巴：字数累计是对的，但带出去的字符永远不超过尾窗
    assert all(len(frame["text"]) <= TAIL_CHARS for frame in frames)
    assert all(frame["chars"] <= 2000 for frame in frames)

    frame = frames[-1]
    assert frame["done"] is True
    assert frame["chars"] == 2000
    assert len(frame["text"]) == TAIL_CHARS
    assert frame["truncated"] is True
    LiveFeed.reset()


def test_live_feed_drops_frame_instead_of_blocking():
    LiveFeed.reset()
    queue = LiveFeed.subscribe(TASK_ID)

    for index in range(200):  # 远超队列容量
        LiveFeed.publish(TASK_ID, {"text": str(index)})

    # 没有订阅者被阻塞、也没有抛异常：丢的是旧帧，留下的是最新的
    assert queue.full()
    assert queue.get_nowait()["text"] == str(200 - 64)
    LiveFeed.reset()


def test_unsubscribe_stops_delivery():
    LiveFeed.reset()
    queue = LiveFeed.subscribe(TASK_ID)
    LiveFeed.unsubscribe(TASK_ID, queue)

    LiveFeed.publish(TASK_ID, {"text": "x"})

    assert queue.empty()
    assert LiveFeed.subscriber_count(TASK_ID) == 0


# ------------------------------------------------------- 接进 call_model


class StreamingAdapter(ModelAdapter):
    """按脚本吐字：正常流、空流、吐一半就炸，三种都能演。"""

    def __init__(self, name: str, *, pieces=("好",), fail_at: int | None = None, empty: bool = False) -> None:
        super().__init__(
            AdapterConfig(name=name, kind="openai_compatible", base_url="", api_key="", model=name, supports_tools=True)
        )
        self.pieces = pieces
        self.fail_at = fail_at
        self.empty = empty
        self.calls = 0

    async def invoke(self, messages, **kwargs):  # pragma: no cover - 只走 bind_tools
        raise AssertionError("流式路径不该调用 invoke")

    async def stream(self, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def bind_tools(self, tools, messages, **kwargs):
        # empty 只影响流式：退路的全部意义就是这条路必须能拿到结果。
        # 这里跟着一起抛，就等于把"网关不支持流式"写成了"网关不支持调用"。
        self.calls += 1
        return ModelResponse(text="非流式结果", usage=Usage(prompt_tokens=7), model=self.config.name)

    async def bind_tools_stream(self, tools, messages, *, on_delta=None, **kwargs):
        if self.empty:
            raise ModelError("model_stream_empty", "流式响应为空")
        for index, piece in enumerate(self.pieces):
            if self.fail_at is not None and index == self.fail_at:
                raise ModelError("model_stream_error", "内容被拦截")
            if on_delta is not None:
                await on_delta(piece)
        return ModelResponse(text="".join(self.pieces), usage=Usage(prompt_tokens=5), model=self.config.name)


def _session(tmp_path: Path, adapter: ModelAdapter, *, stream: bool) -> AgentSession:
    return AgentSession(
        adapter=adapter,
        gateway=ToolGateway([]),
        context=ToolContext(root=tmp_path, task_id=TASK_ID, session_factory=get_session_factory()),
        recorder=EventRecorder(get_session_factory(), TASK_ID),
        session_factory=get_session_factory(),
        provider_id="p",
        provider_name=adapter.config.name,
        goal="流式",
        stream_output=stream,
    )


async def _events(kind: str) -> list[dict]:
    async with get_session_factory()() as session:
        rows = (await session.execute(AgentEvent.__table__.select())).fetchall()
    return [row.payload for row in rows if row.type == kind]


@pytest.fixture
async def seeded():
    from blank_api.db import Base, get_engine

    LiveFeed.reset()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="project-stream", name="流式"))
        session.add(Task(id=TASK_ID, project_id="project-stream", title="流式", goal="流式", max_steps=5))
        await session.commit()


async def test_switch_off_never_streams(seeded, tmp_path):
    adapter = StreamingAdapter("m", pieces=("一", "二"))
    session = _session(tmp_path, adapter, stream=False)
    queue = LiveFeed.subscribe(TASK_ID)

    response = await session._call_adapter(adapter, TOOLS, [ChatMessage("user", "hi")], "execute_action", "coder")

    assert response.text == "非流式结果"
    assert adapter.calls == 1
    assert queue.empty()
    assert await _events("model.streamed") == []


async def test_switch_on_publishes_deltas_and_records_metrics(seeded, tmp_path):
    adapter = StreamingAdapter("m", pieces=("一", "二", "三"))
    session = _session(tmp_path, adapter, stream=True)
    queue = LiveFeed.subscribe(TASK_ID)

    response = await session._call_adapter(adapter, TOOLS, [ChatMessage("user", "hi")], "execute_action", "coder")

    assert response.text == "一二三"
    frames = []
    while not queue.empty():
        frames.append(queue.get_nowait())
    assert frames, "开着流式却一帧都没推，界面还是卡的"
    assert frames[-1]["done"] is True
    assert frames[-1]["chars"] == 3

    payloads = await _events("model.streamed")
    assert payloads and payloads[0]["chunks"] == 3
    assert payloads[0]["chars"] == 3
    assert payloads[0]["first_delta_ms"] >= 1


async def test_unstreamable_gateway_falls_back_to_one_shot(seeded, tmp_path):
    adapter = StreamingAdapter("m", empty=True)
    session = _session(tmp_path, adapter, stream=True)

    response = await session._call_adapter(adapter, TOOLS, [ChatMessage("user", "hi")], "execute_action", "coder")

    assert response.text == "非流式结果"
    assert adapter.calls == 1
    payloads = await _events("model.stream_fallback")
    assert payloads and payloads[0]["code"] == "model_stream_empty"


async def test_failure_after_first_chunk_is_not_retried(seeded, tmp_path):
    """已经收到文字之后再出错，不能退回去重跑。

    重跑会让用户看到两遍输出；而且流里的错（内容审核、上下文超长）多半重跑还是错。
    """
    adapter = StreamingAdapter("m", pieces=("一", "二", "三"), fail_at=1)
    session = _session(tmp_path, adapter, stream=True)

    with pytest.raises(ModelError):
        await session._call_adapter(adapter, TOOLS, [ChatMessage("user", "hi")], "execute_action", "coder")

    assert adapter.calls == 0
    assert await _events("model.stream_fallback") == []


async def test_stream_failure_before_any_text_still_falls_back(seeded, tmp_path):
    adapter = StreamingAdapter("m", pieces=("一",), fail_at=0)
    session = _session(tmp_path, adapter, stream=True)

    response = await session._call_adapter(adapter, TOOLS, [ChatMessage("user", "hi")], "execute_action", "coder")

    # 一个字都没到：退回一次性调用是安全的
    assert response.text == "非流式结果"
    assert adapter.calls == 1

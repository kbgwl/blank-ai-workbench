"""#306 批：MessageDisplay 钩子（对齐上游 MessageDisplay 事件）。

上游语义（取证自 `claude.uniq.txt`）：

* 事件名 ``MessageDisplay``，每批流式输出的**新完成行**展示给用户时触发。
* 载荷：``turn_id`` / ``message_id`` / ``index`` / ``final`` / ``delta``。
* 输出：``displayContent``（可选字符串）——替换展示文本。不给就照原样展示。
* **仅展示层**：落库的消息与模型看到的原文一字不改。非阻断事件。

本批覆盖三件事，每件都钉在"能失败"的判据上：

* **事件面与解析**：``message_display`` 真的注册了、``MessageDisplay`` 别名映射
  正确、不在阻断集合；``parse_hook_output`` 对 ``displayContent`` 做类型校验
  （只认字符串，非字符串即错型不解析）；``HookResult`` 装配与 ``as_dict`` 序列化。
* **OutputRelay 触发**：每帧展示前跑钩子，``displayContent`` 替换展示文本；
  没挂钩子走纯发布路径零开销；钩子抛异常时照原样展示（上游同口径）。
* **端到端**：通过 ``_call_adapter`` 流式路径，真子进程钩子拿到载荷、输出
  ``displayContent`` 后界面看到的确实是替换后的文本。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.live import LiveFeed, OutputRelay
from blank_api.services.hooks import (
    BLOCKING_EVENTS,
    EVENT_ALIASES,
    HOOK_EVENTS,
    HookResult,
    parse_hook_output,
)

TASK_ID = "task-msgdisplay"


# --------------------------------------------------------------------------- #
# 1. 事件面
# --------------------------------------------------------------------------- #
def test_message_display_registered_and_aliased():
    assert "message_display" in HOOK_EVENTS
    assert EVENT_ALIASES["MessageDisplay"] == "message_display"


def test_message_display_is_not_blocking():
    """展示层钩子不改变执行——它只改界面看到什么，不能拦流程。"""
    assert "message_display" not in BLOCKING_EVENTS


# --------------------------------------------------------------------------- #
# 2. parse_hook_output：displayContent 类型校验
# --------------------------------------------------------------------------- #
def test_display_content_parsed_from_hook_specific_output():
    text = json.dumps(
        {"hookSpecificOutput": {"displayContent": "替换后的文本"}},
        ensure_ascii=False,
    )
    parsed = parse_hook_output(text)
    assert parsed["display_content"] == "替换后的文本"


def test_display_content_parsed_from_top_level():
    text = json.dumps({"displayContent": "顶层替换"}, ensure_ascii=False)
    parsed = parse_hook_output(text)
    assert parsed["display_content"] == "顶层替换"


def test_display_content_non_string_is_rejected():
    """非字符串是写错的钩子——把它读成"要替换"等于让一次错型变成一次静默的界面篡改。"""
    for bad in (123, True, None, ["text"], {"k": "v"}):
        text = json.dumps({"hookSpecificOutput": {"displayContent": bad}})
        parsed = parse_hook_output(text)
        assert "display_content" not in parsed, f"displayContent={bad!r} 不该被解析"


def test_display_content_empty_string_is_omitted():
    """空串等于没替换，不进解析结果。"""
    text = json.dumps({"hookSpecificOutput": {"displayContent": "   "}})
    parsed = parse_hook_output(text)
    assert "display_content" not in parsed


def test_display_content_absent_means_no_replacement():
    text = json.dumps({"hookSpecificOutput": {}})
    parsed = parse_hook_output(text)
    assert "display_content" not in parsed


# --------------------------------------------------------------------------- #
# 3. HookResult 装配与序列化
# --------------------------------------------------------------------------- #
def test_hook_result_assembles_display_content():
    result = HookResult(
        event="message_display",
        command="echo",
        status="ok",
        display_content="替换文本",
    )
    assert result.display_content == "替换文本"
    body = result.as_dict()
    assert body["display_content_chars"] == 4


def test_hook_result_omits_display_content_when_empty():
    result = HookResult(event="message_display", command="echo", status="ok")
    body = result.as_dict()
    assert "display_content_chars" not in body


# --------------------------------------------------------------------------- #
# 4. OutputRelay：display_hook 触发与文本替换
# --------------------------------------------------------------------------- #
async def test_relay_without_hook_publishes_original_text():
    LiveFeed.reset()
    queue = LiveFeed.subscribe(TASK_ID)
    relay = OutputRelay(TASK_ID, "execute_action")

    await relay("原始文本" * 50)
    await relay.close()

    frames = []
    while not queue.empty():
        frames.append(queue.get_nowait())
    assert frames, "没挂钩子也要正常推帧"
    assert all(frame["text"] != "" for frame in frames)
    # 没有 display_hook，展示文本就是原文尾巴
    assert frames[0]["text"] == "原始文本" * 50
    LiveFeed.reset()


async def test_relay_display_hook_replaces_text():
    LiveFeed.reset()
    queue = LiveFeed.subscribe(TASK_ID)
    seen_payloads: list[dict] = []

    async def display_hook(payload: dict) -> str:
        seen_payloads.append(payload)
        return "钩子替换的文本"

    relay = OutputRelay(TASK_ID, "execute_action", display_hook=display_hook)

    await relay("模型输出" * 50)
    await relay.close()

    frames = []
    while not queue.empty():
        frames.append(queue.get_nowait())
    assert frames, "挂了钩子也要正常推帧"
    # 每一帧的展示文本都是钩子给的替换文本，不是原文
    assert all(frame["text"] == "钩子替换的文本" for frame in frames)
    # 载荷五键齐全
    assert seen_payloads, "钩子没被调用"
    payload = seen_payloads[0]
    assert set(payload) == {"turn_id", "message_id", "index", "final", "delta"}
    assert payload["turn_id"] == TASK_ID
    assert payload["index"] == 0
    assert payload["final"] is False
    # delta 是当前帧的尾巴
    assert payload["delta"] == "模型输出" * 50
    # close 那一帧 final=True
    assert seen_payloads[-1]["final"] is True
    LiveFeed.reset()


async def test_relay_display_hook_failure_falls_back_to_original():
    """钩子抛异常时照原样展示——上游同口径："emitting original text"。"""
    LiveFeed.reset()
    queue = LiveFeed.subscribe(TASK_ID)

    async def display_hook(payload: dict) -> str:
        raise RuntimeError("钩子炸了")

    relay = OutputRelay(TASK_ID, "execute_action", display_hook=display_hook)

    await relay("原始文本" * 50)
    await relay.close()

    frames = []
    while not queue.empty():
        frames.append(queue.get_nowait())
    assert frames, "钩子炸了也要正常推帧"
    # 钩子抛异常 → 展示原文
    assert frames[0]["text"] == "原始文本" * 50
    LiveFeed.reset()


async def test_relay_display_hook_empty_return_keeps_original():
    """钩子返回空串等于没替换，照原样展示。"""
    LiveFeed.reset()
    queue = LiveFeed.subscribe(TASK_ID)

    async def display_hook(payload: dict) -> str:
        return ""

    relay = OutputRelay(TASK_ID, "execute_action", display_hook=display_hook)

    await relay("原文" * 50)
    await relay.close()

    frames = []
    while not queue.empty():
        frames.append(queue.get_nowait())
    assert frames
    assert frames[0]["text"] == "原文" * 50
    LiveFeed.reset()


async def test_relay_display_hook_index_increments_per_flush():
    LiveFeed.reset()
    queue = LiveFeed.subscribe(TASK_ID)
    indices: list[int] = []

    async def display_hook(payload: dict) -> str:
        indices.append(payload["index"])
        return ""

    relay = OutputRelay(TASK_ID, "execute_action", display_hook=display_hook)

    # 每次超过 FLUSH_CHARS 就触发一帧
    await relay("x" * 200)
    await relay("y" * 200)
    await relay.close()

    # index 从 0 递增
    assert indices == sorted(indices)
    assert indices[0] == 0
    assert indices[-1] >= 1  # close 也触发一次
    LiveFeed.reset()


# --------------------------------------------------------------------------- #
# 5. 端到端：通过 _call_adapter 流式路径
# --------------------------------------------------------------------------- #
from blank_api.db import get_session_factory
from blank_api.services.agent.graph import AgentSession
from blank_api.services.models.base import (
    AdapterConfig,
    ChatMessage,
    ModelAdapter,
    ModelResponse,
    ToolSpecLite,
    Usage,
)
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway

TOOLS = [ToolSpecLite("read_file", "读文件", {"type": "object", "properties": {}})]


class _StreamAdapter(ModelAdapter):
    """按脚本吐字的流式适配器。"""

    def __init__(self, name: str, pieces: tuple[str, ...]) -> None:
        super().__init__(
            AdapterConfig(name=name, kind="openai_compatible", base_url="", api_key="", model=name, supports_tools=True)
        )
        self.pieces = pieces

    async def invoke(self, messages, **kwargs):  # pragma: no cover
        raise AssertionError("流式路径不该调用 invoke")

    async def stream(self, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def bind_tools(self, tools, messages, **kwargs):  # pragma: no cover
        raise AssertionError("流式路径不该调用 bind_tools")

    async def bind_tools_stream(self, tools, messages, *, on_delta=None, **kwargs):
        for piece in self.pieces:
            if on_delta is not None:
                await on_delta(piece)
        return ModelResponse(text="".join(self.pieces), usage=Usage(prompt_tokens=5), model=self.config.name)


def _make_session(tmp_path: Path, *, hooks=None) -> AgentSession:
    adapter = _StreamAdapter("m", ("一", "二", "三"))
    hook_runner = None
    if hooks is not None:
        from blank_api.services.hooks import HookRunner

        hook_runner = HookRunner(hooks)
    return AgentSession(
        adapter=adapter,
        gateway=ToolGateway([]),
        context=ToolContext(root=tmp_path, task_id=TASK_ID, session_factory=get_session_factory()),
        recorder=EventRecorder(get_session_factory(), TASK_ID),
        session_factory=get_session_factory(),
        provider_id="p",
        provider_name="m",
        goal="测试",
        stream_output=True,
        hooks=hook_runner,
    )


@pytest.fixture
async def seeded_md():
    from blank_api.db import Base, get_engine

    LiveFeed.reset()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        from blank_api.models.project import Project
        from blank_api.models.task import Task

        session.add(Project(id="project-md", name="MD"))
        session.add(Task(id=TASK_ID, project_id="project-md", title="MD", goal="测试", max_steps=5))
        await session.commit()


async def test_call_adapter_with_message_display_hook_replaces_frames(seeded_md, tmp_path):
    """端到端：配了 message_display 钩子，流式帧的展示文本被替换。"""
    from blank_api.services.hooks import HookSpec

    spec = HookSpec(
        kind="command",
        command=f'"{sys.executable}" -c "import json,sys; sys.stdin.reconfigure(encoding=\'utf-8\'); json.load(sys.stdin); print(json.dumps({{\\"hookSpecificOutput\\": {{\\"displayContent\\": \\"钩子替换\\"}}}}, ensure_ascii=False))"',
        match="",
        timeout=10,
    )
    session = _make_session(tmp_path, hooks={"message_display": [spec]})
    queue = LiveFeed.subscribe(TASK_ID)

    response = await session._call_adapter(
        session.adapter, TOOLS, [ChatMessage("user", "hi")], "execute_action", "coder"
    )

    assert response.text == "一二三"
    frames = []
    while not queue.empty():
        item = queue.get_nowait()
        if item.get("wake"):
            continue
        frames.append(item)
    assert frames, "开着流式却一帧都没推"
    # 展示文本被钩子替换
    assert all(frame["text"] == "钩子替换" for frame in frames)
    LiveFeed.reset()


async def test_call_adapter_without_message_display_hook_keeps_original(seeded_md, tmp_path):
    """端到端：没配 message_display 钩子，展示文本照原样。"""
    session = _make_session(tmp_path, hooks=None)
    queue = LiveFeed.subscribe(TASK_ID)

    response = await session._call_adapter(
        session.adapter, TOOLS, [ChatMessage("user", "hi")], "execute_action", "coder"
    )

    assert response.text == "一二三"
    frames = []
    while not queue.empty():
        item = queue.get_nowait()
        if item.get("wake"):
            continue
        frames.append(item)
    assert frames
    # 没挂钩子 → 展示原文
    last = frames[-1]
    assert "一二三" in last["text"]
    LiveFeed.reset()

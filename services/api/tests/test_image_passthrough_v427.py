"""附图不能被文本的剪刀剪：两处各剪一刀，模型就收到半张图。

这条链上有两个"对文本正确、对图片致命"的默认值，都踩过：

1. **网关的 4 万字符上限**。`_redact_value` 会把工具结果里每个字符串都削到
   `MAX_RESULT_CHARS`。一张 59KB 的截图 base64 有 79,620 个字符，削完就只剩
   半截 data URI——DeepSeek 直接回「unsupported image」，整轮 400，而报错里
   看不出是被我们自己剪的（真机：任务 b9230111 之前连续三次失败）。
2. **payload 的 2 万字符上限**。图片留在 `data.image` 里进 `render()`，会让
   工具消息变成 20,025 字符的 JSON；这一步不打 400，但每走一步都要重发这一坨
   残废的 base64（约 7k token × 步数），而且模型读到的是断掉的 JSON。

所以判据盯两件事：**base64 一个字符都不许少**，**工具消息里一个字符都不许有**。
"""

from __future__ import annotations

import base64
import types

import pytest

from blank_api.db import get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import Task
from blank_api.services.agent.graph import _append_tool_response
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome
from blank_api.services.tools.gateway import MAX_RESULT_CHARS, ToolGateway


def _jpeg_b64(kilobytes: int = 60) -> str:
    """造一段比 4 万字符上限还长的假图（头部是真的 JPEG SOI）。"""
    return base64.b64encode(b"\xff\xd8\xff\xe0" + b"x" * (kilobytes * 1024 - 4)).decode()


def _image_outcome(b64: str) -> ToolOutcome:
    return ToolOutcome(
        ok=True,
        output="已打开：file:///demo/index.html\n控制台：没有任何错误",
        data={
            "url": "file:///demo/index.html",
            "screenshot": ".blank/shots/a.png",
            "image": {"media_type": "image/jpeg", "data": b64, "path": ".blank/shots/a.png", "bytes": 61_440},
        },
    )


TASK_ID = "task-image-pass"


@pytest.fixture
async def seeded():
    """网关要落产物行：给它一个真的库和一条真任务，别用 None 假装。"""
    from blank_api.db import Base, get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="project-img", name="图通道"))
        session.add(Task(id=TASK_ID, project_id="project-img", title="图", goal="图", max_steps=5))
        await session.commit()


def _returns(outcome: ToolOutcome):
    async def handler(context, arguments):  # noqa: ANN001, ANN202 - 测试替身
        return outcome

    return handler


async def _run_through_gateway(outcome: ToolOutcome, root) -> dict:
    tool = ToolDefinition(
        name="browser_check",
        description="打开页面",
        input_schema={"type": "object", "properties": {}},
        risk_level="low",
        handler=_returns(outcome),
    )
    gateway = ToolGateway([tool])
    context = ToolContext(
        root=root, task_id=TASK_ID, session_factory=get_session_factory(), approval_policy=lambda *_: True
    )
    return await gateway.execute("browser_check", {}, context, "execute_action")


async def test_the_gateway_does_not_cut_base64_into_half_an_image(workspace, seeded):
    """59KB 截图 = 79,620 个字符，必须原样到模型手里。"""
    b64 = _jpeg_b64(60)
    assert len(b64) > MAX_RESULT_CHARS, "样本要比网关上限长，否则这条测试证明不了什么"

    payload = await _run_through_gateway(_image_outcome(b64), workspace)

    assert "data" in payload, f"网关返回：{payload}"
    got = payload["data"]["image"]["data"]
    assert got == b64, f"base64 被剪了：{len(got)} / {len(b64)} 个字符"
    base64.b64decode(got, validate=True), "剪出来的东西连 base64 都不是"


async def test_text_in_the_same_result_is_still_trimmed_and_redacted(workspace, seeded):
    """图放行不等于整条结果放行：同一条结果里的长文本照旧要削。"""
    long_text = "日志行 " * 20_000
    outcome = _image_outcome(_jpeg_b64(40))
    outcome.data["log"] = long_text
    outcome.data["secret"] = "sk-live-1234567890"

    payload = await _run_through_gateway(outcome, workspace)

    assert len(payload["data"]["log"]) <= MAX_RESULT_CHARS
    assert "sk-live-1234567890" not in str(payload["data"].get("secret"))


def test_the_image_never_enters_the_tool_message():
    """工具消息里带一张图 = 每步重发 7k token 的残废 JSON；图只走它自己那条消息。"""
    b64 = _jpeg_b64(20)
    payload = {
        "ok": True,
        "status": "ok",
        "output": "控制台：0 条错误",
        "data": {"url": "file:///x", "image": {"media_type": "image/jpeg", "data": b64, "path": "a.png"}},
    }
    session = types.SimpleNamespace(
        supports_vision=True, output_seen={}, trimmed_chars=0, trim_events=[], run_id="r1"
    )
    call = {"id": "c1", "function": {"name": "browser_check", "arguments": "{}"}}

    messages = _append_tool_response([], call, payload, session=session, step=1)

    tool_message = messages[0]
    assert "base64" not in tool_message["content"] and b64[:40] not in tool_message["content"]
    assert len(tool_message["content"]) < 2_000, "工具消息里又混进了图"
    image_message = messages[1]
    assert image_message["role"] == "user"
    assert image_message["images"][0]["data"] == b64, "图本身要完整过去"

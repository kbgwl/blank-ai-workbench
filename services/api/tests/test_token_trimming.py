"""工具输出瘦身与补丁可修复性。

这两件事解决的是同一个问题：**一步没成，钱已经花了**。

- 工具输出进上下文之前先瘦身。一次 100 步的任务，上下文里绝大部分是工具输出，
  而它们会被每一步重发一遍；不瘦身的话，上下文按步数复利，账单也按步数复利。
- 补丁失败必须带真实上下文。以前只回一句「第 N 行附近不匹配」，模型唯一的
  办法是重读整个文件再盲猜重打——实测 64 次补丁里 19 次失败，其中好几次是
  同一个错误重复三次。

测试盯的是这两条真的落地了，不是"加了个函数"。
"""

from __future__ import annotations

import uuid

import pytest

from blank_api.db import get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import AgentSession, execute_action_node
from blank_api.services.agent.payload import MAX_PAYLOAD_CHARS, render
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome
from blank_api.services.tools.gateway import ToolGateway
from blank_api.services.tools.patch import PatchError, apply_patch_text, infer_path

TASK_ID = "task-trim-1"


# ── 1. 渲染层：预算、头尾、折叠 ──────────────────────────────────────


def _shell_payload(size: int) -> dict:
    body = "\n".join(f"line {index}: 测试输出内容" for index in range(size // 20))
    return {"ok": True, "status": "ok", "output": body, "data": {"exit_code": 1}}


def test_shell_output_keeps_head_and_tail_but_drops_the_middle():
    payload = _shell_payload(40_000)
    text, stats = render("run_shell", payload)

    assert len(text) < 7_000, "shell 输出没有按预算裁剪"
    assert "line 0" in text, "头部丢了：开头通常是命令与失败摘要"
    assert "line 1999" in text or "line 1998" in text, "尾部丢了：结论与堆栈在最后"
    assert "省略" in text, "裁掉了却不说，模型会以为输出就这么点"
    assert stats["saved"] > 25_000


def test_typical_read_is_not_touched():
    """预算要在均值之上：正常大小的文件读取必须原样进上下文，不能"为了省"把信息切掉。"""
    content = "x" * 2_500
    text, stats = render("read_file", {"ok": True, "status": "ok", "output": content})
    assert stats["saved"] == 0
    assert content in text


def test_patch_result_does_not_echo_the_diff_back():
    """补丁是模型自己写的，把 diff 再回吐一遍等于让它读自己的作业。"""
    payload = {
        "ok": True,
        "status": "ok",
        "output": "已修改文件：a.py（2 行变更）",
        "data": {"path": "a.py", "diff": "- old\n+ new\n" * 500},
    }
    text, stats = render("apply_patch", payload)
    assert "diff_omitted" in text
    assert stats["dropped_diff"] is True
    assert len(text) < 1_000


def test_identical_output_is_folded_to_a_pointer():
    payload = {"ok": True, "status": "ok", "output": "同样的内容 " * 200}
    seen: dict[str, int] = {}
    first, _ = render("read_file", payload, seen, step=3)
    second, stats = render("read_file", payload, seen, step=7)

    assert "与第 3 步的输出完全相同" in second
    assert "同样的内容" not in second
    assert stats["duplicate"] is True
    # ok/status 必须留着：否则模型会把"已折叠"当成"没结果"再跑一遍，更贵。
    assert '"ok": true' in second
    assert len(first) > len(second)


def test_payload_hard_limit_applies_to_the_whole_json():
    payload = {"ok": True, "status": "ok", "output": "y" * 30_000, "data": {"blob": "z" * 30_000}}
    text, _ = render("search_code", payload)
    assert len(text) <= MAX_PAYLOAD_CHARS + 200


# ── 2. 端到端：瘦身真的发生在进上下文之前，并且有账 ──────────────────


def _schema() -> dict:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def _long_tool(name: str, output: str) -> ToolDefinition:
    async def handler(context: ToolContext, arguments: dict) -> ToolOutcome:
        return ToolOutcome(ok=True, output=output)

    return ToolDefinition(
        name=name,
        description=f"测试用 {name}",
        input_schema=_schema(),
        risk_level="low",
        handler=handler,
        timeout_seconds=5.0,
    )


def _call(name: str) -> dict:
    return {
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


async def _seed_task() -> None:
    async with get_session_factory()() as session:
        session.add(Project(id="project-trim", name="瘦身测试"))
        session.add(Task(id=TASK_ID, project_id="project-trim", title="瘦身", goal="瘦身", max_steps=10))
        await session.commit()


@pytest.fixture
async def seeded():
    from blank_api.db import Base, get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await _seed_task()


def _session(tmp_path, tools: list[ToolDefinition], tool_calls: list[dict]) -> AgentSession:
    session = AgentSession(
        adapter=None,  # type: ignore[arg-type]
        gateway=ToolGateway(tools),
        context=ToolContext(
            root=tmp_path,
            task_id=TASK_ID,
            session_factory=get_session_factory(),
            approval_policy=lambda *_: True,
        ),
        recorder=EventRecorder(get_session_factory(), TASK_ID),
        session_factory=get_session_factory(),
        provider_id="provider-1",
        provider_name="mock",
        goal="跑一个很吵的命令",
        max_steps=10,
    )

    async def fake_ask(state, prompt, node, *, messages=None):
        return {
            "text": "好了",
            "tool_calls": [],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "calls": 1},
            "messages": list(messages or []) + [{"role": "assistant", "content": "好了"}],
        }

    session.ask = fake_ask  # type: ignore[method-assign]
    return session


async def _events() -> list[tuple[str, dict]]:
    async with get_session_factory()() as session:
        rows = (await session.execute(AgentEvent.__table__.select())).fetchall()
    return [(row.type, row.payload or {}) for row in rows]


async def test_noisy_command_never_reaches_the_context_at_full_size(seeded, tmp_path):
    noisy = "\n".join(f"pytest: {index} passed" for index in range(2_000))
    session = _session(tmp_path, [_long_tool("run_shell", noisy)], [_call("run_shell")])

    result = await execute_action_node(
        {"messages": [{"role": "user", "content": "跑测试"}], "steps": 0, "pending": [_call("run_shell")]},
        session,
    )

    tool_messages = [item for item in result["messages"] if item.get("role") == "tool"]
    assert tool_messages, "工具结果没进上下文"
    assert len(tool_messages[-1]["content"]) < 7_000, "30KB 的命令输出被整段塞进了上下文"

    events = dict(await _events())
    assert "context.trimmed" in events, "省了多少必须记账，否则只是说法"
    assert events["context.trimmed"]["total_saved_chars"] > 3_000
    assert events["context.trimmed"]["total_saved_tokens_est"] > 900


# ── 3. 补丁：路径能推断，失败能自修 ──────────────────────────────────


def test_path_is_inferred_from_the_patch_header():
    patch = "--- a/src/cli_tools/time.py\n+++ b/src/cli_tools/time.py\n@@ -1 +1 @@\n-x\n+y\n"
    assert infer_path(patch) == "src/cli_tools/time.py"
    # 只有 --- 头也要能取到，别把这种情况判成"没路径"
    assert infer_path("--- pkg/mod.py\n@@ -1 +1 @@\n-x\n+y\n") == "pkg/mod.py"
    assert infer_path("没有头部") == ""


def test_mismatch_error_carries_the_real_lines_with_numbers():
    original = "def add(a, b):\n    return a - b\n"
    patch = "@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a + b\n+    return a - b\n"
    with pytest.raises(PatchError) as caught:
        apply_patch_text(original, patch)

    error = caught.value
    assert "上下文不匹配" in str(error)
    # 关键：把真实内容回给模型，它才能一次改对
    assert "return a - b" in error.near
    assert "2|" in error.near, "附近内容必须带行号"


def test_line_mismatch_also_reports_what_the_patch_expected():
    original = "one\ntwo\nthree\n"
    patch = "@@ -2,1 +2,1 @@\n-two changed\n+two\n"
    with pytest.raises(PatchError) as caught:
        apply_patch_text(original, patch)
    assert "two changed" in str(caught.value), "要告诉模型它写的是哪一行，否则只能盲试"
    assert "two" in caught.value.near


# ── 4. 审查回路不能空转 ────────────────────────────────────────────────


async def test_unparseable_review_does_not_loop_forever(seeded, tmp_path):
    """审查模型没返回 JSON 时，不该被当成"审查不通过"去打回重做。

    实测：一个只有 6 次工具调用的任务，被 审查→执行→审查 的空转回路放大成
    31 次执行 + 27 次审查、35 万 token，产出和 6 步时一模一样。
    """
    from blank_api.services.agent.graph import review_result_node, route_after_review

    session = _session(tmp_path, [], [])
    # fake_ask 默认回一句自然语言，解析不出 JSON —— 正是线上常见的失败形态
    result = await review_result_node({"messages": [{"role": "user", "content": "干活"}]}, session)

    assert result["review"]["approved"] is True, "没有审查结论不能等价于审查不通过"
    assert result["review"].get("unavailable") is True
    assert "审查未完成" in result["review"]["findings"][0]
    assert route_after_review({**result, "repair_rounds": 0}, session) == "summarize_result"


async def test_review_rounds_are_capped(seeded, tmp_path):
    from blank_api.services.agent.graph import MAX_REVIEW_ROUNDS, route_after_review

    session = _session(tmp_path, [], [])
    # 即使审查明确说不通过，轮次到顶也必须收尾：再回执行也不会有新东西。
    state = {"review": {"approved": False}, "repair_rounds": 0, "review_rounds": MAX_REVIEW_ROUNDS}
    assert route_after_review(state, session) == "summarize_result"

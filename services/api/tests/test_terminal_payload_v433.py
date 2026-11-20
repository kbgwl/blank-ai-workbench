"""取消 / 暂停事件的载荷判据（v433）。

上一轮把终态码接到三条取消路径上，但只有一条被端到端断言；另两处靠"共用同一个
``terminal_detail``"兜着——现在把载荷本身抽成一个函数，判据分两层：

1. **函数形状**：``cancelled_payload`` / ``paused_payload`` 产出的 ``terminal.code``
   必须是封闭枚举里的那一档。这两个函数是四处写入点的唯一答案，改了它就全都改。
2. **真实分支**：执行节点在"进入时发现已取消"与"执行中工具被中断"两条路上，
   落下来的事件必须带码。只测函数会漏掉"函数对了但没人调用"——那正是上一轮
   留下的缺口。
"""

from __future__ import annotations

import json
import uuid

import pytest

from blank_api.db import get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent import graph
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import AgentSession, execute_action_node
from blank_api.services.agent.terminal import cancelled_payload, paused_payload
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome
from blank_api.services.tools.gateway import ToolGateway

TASK_ID = "task-terminal-payload"


def _schema() -> dict:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def _noop_tool() -> ToolDefinition:
    async def handler(context: ToolContext, arguments: dict) -> ToolOutcome:  # noqa: ARG001
        return ToolOutcome(ok=True, output="ok", data={})

    return ToolDefinition(
        name="write_file",
        description="写文件",
        input_schema=_schema(),
        risk_level="low",
        handler=handler,
        timeout_seconds=5.0,
    )


def _call() -> dict:
    return {
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {
            "name": "write_file",
            "arguments": json.dumps({"path": "index.html", "content": "<p>hi</p>"}),
        },
    }


@pytest.fixture
async def seeded():
    from blank_api.db import Base, get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as db:
        db.add(Project(id="project-terminal", name="终态载荷"))
        db.add(Task(id=TASK_ID, project_id="project-terminal", title="t", goal="t", max_steps=10))
        await db.commit()


def _session(tmp_path, *, status: str | None = None) -> AgentSession:
    session = AgentSession(
        adapter=None,  # type: ignore[arg-type]  # 本用例不走到模型调用
        gateway=ToolGateway([_noop_tool()]),
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
        goal="帮我做个公司官网",
        max_steps=10,
    )
    if status is not None:

        async def current_status() -> str:
            return status

        session.current_status = current_status  # type: ignore[method-assign]
    return session


def _state() -> dict:
    return {
        "messages": [{"role": "user", "content": "帮我做个公司官网"}],
        "steps": 0,
        "pending": [_call()],
    }


async def _events(kind: str) -> list[AgentEvent]:
    from sqlalchemy import select

    async with get_session_factory()() as db:
        rows = await db.execute(select(AgentEvent).where(AgentEvent.type == kind).order_by(AgentEvent.seq))
        return list(rows.scalars())


# ── 1. 函数形状 ─────────────────────────────────────────────────────────


def test_cancel_and_pause_payloads_both_carry_a_code():
    cancel = cancelled_payload("用户已取消任务")
    assert cancel["terminal"]["code"] == "cancelled"
    assert cancel["reason"] == "用户已取消任务"
    # 人话与码要一致：界面读 code、用户读 reason，两处指向同一件事。
    assert cancel["terminal"]["reason"] == cancel["reason"]

    pause = paused_payload("用户暂停了任务")
    assert pause["terminal"]["code"] == "paused"
    assert pause["terminal"]["reason"] == pause["reason"]


def test_an_empty_reason_still_gets_a_sentence():
    """reason 为空时不能给出一个空壳：界面上"结束了但没原因"和崩了没区别。"""
    assert cancelled_payload("")["reason"]
    assert paused_payload("")["terminal"]["reason"]


# ── 2. 真实分支 ─────────────────────────────────────────────────────────


async def test_entering_the_executor_with_a_cancelled_task_still_carries_the_code(
    seeded, tmp_path
) -> None:  # noqa: F811
    """执行节点进入时发现已取消：这条事件是用户最终看到的那一条。

    ``_finish`` 见到已有终态就跳过自己那条，所以这里漏码等于整轮没有码。
    """
    session = _session(tmp_path, status="cancelled")

    result = await execute_action_node(_state(), session)

    assert result["status"] == "cancelled"
    events = await _events("task.cancelled")
    assert len(events) == 1, [item.type for item in events]
    terminal = events[0].payload.get("terminal") or {}
    assert terminal.get("code") == "cancelled", events[0].payload


async def test_a_tool_interrupted_mid_flight_still_carries_the_code(
    seeded, tmp_path, monkeypatch
) -> None:  # noqa: F811
    """执行中工具被中断：这条分支以前完全没有判据。

    用 ``_execute_all`` 这个缝直接给一个 cancelled 回执——要测的是"拿到这个回执之后
    节点做了什么"，不是工具怎么被杀掉的（那属于工具层自己的判据）。
    """
    session = _session(tmp_path)

    async def fake_execute_all(*_args, **_kwargs):
        return [{"status": "cancelled", "tool": "write_file"}]

    monkeypatch.setattr(graph, "_execute_all", fake_execute_all)

    result = await execute_action_node(_state(), session)

    assert result["status"] == "cancelled"
    events = await _events("task.cancelled")
    assert len(events) == 1, [item.type for item in events]
    payload = events[0].payload
    assert (payload.get("terminal") or {}).get("code") == "cancelled", payload
    # 理由要说得比"取消了"更具体：用户要能分清是"我在开头就叫停"还是"跑到一半被杀"。
    assert "中断" in str(payload.get("reason") or ""), payload


async def test_pausing_at_the_entry_carries_the_code_too(seeded, tmp_path) -> None:  # noqa: F811
    """暂停以前连码都没有。它同样是"这一轮为什么停"，界面要能机读出来。"""
    session = _session(tmp_path, status="paused")

    result = await execute_action_node(_state(), session)

    assert result["status"] == "paused"
    events = await _events("task.paused")
    assert len(events) == 1, [item.type for item in events]
    assert (events[0].payload.get("terminal") or {}).get("code") == "paused", events[0].payload

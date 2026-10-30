"""节点级证据：「不懂就问」这条约束是拦得住的，不是说说而已。

上一组测试（``test_unknown_facts_v147``）测的是判定函数；这一组测的是它在
``execute_action_node`` 里的实际效果——**写操作到底有没有发生**。

三件事一眼要能看出来：
1. 编一个电话写页面 → 工具 handler 一次都没被调用（不是"写了以后提醒"，是没写）；
2. 用户答"就用这个" → 同一次调用照原样放行；
3. 用户给了真号码 → 原来那条仍然不执行，理由里带着用户给的值，改写后才放行。
"""

from __future__ import annotations

import uuid

import pytest

from blank_api.db import get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent import graph
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import AgentSession, execute_action_node
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome
from blank_api.services.tools.gateway import ToolGateway

TASK_ID = "task-facts-1"
PHONE = "138-1234-5678"


def _schema() -> dict:
    return {"type": "object", "properties": {}, "additionalProperties": False}


class Counter:
    def __init__(self) -> None:
        self.calls: list[dict] = []


def _write_tool(counter: Counter) -> ToolDefinition:
    async def handler(context: ToolContext, arguments: dict) -> ToolOutcome:
        counter.calls.append(dict(arguments))
        return ToolOutcome(ok=True, output="ok", data={"path": arguments.get("path", "")})

    return ToolDefinition(
        name="write_file",
        description="写文件",
        input_schema=_schema(),
        # risk 低 = 不过审批门。本组要看的只有事实闸门这一件事。
        risk_level="low",
        handler=handler,
        timeout_seconds=5.0,
    )


def _call(content: str, path: str = "index.html") -> dict:
    import json

    return {
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {
            "name": "write_file",
            "arguments": json.dumps({"path": path, "content": content}, ensure_ascii=False),
        },
    }


async def _seed_task() -> None:
    async with get_session_factory()() as session:
        session.add(Project(id="project-facts", name="事实闸门"))
        session.add(
            Task(id=TASK_ID, project_id="project-facts", title="做个官网", goal="做个官网", max_steps=10)
        )
        await session.commit()


@pytest.fixture
async def seeded():
    from blank_api.db import Base, get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await _seed_task()


def _session(tmp_path, tools: list[ToolDefinition]) -> AgentSession:
    session = AgentSession(
        adapter=None,  # type: ignore[arg-type]  # 本用例不走到模型调用
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
        goal="帮我做个公司官网",
        max_steps=10,
    )

    async def fake_ask(state, prompt, node, *, messages=None):
        return {
            "text": "继续",
            "tool_calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "calls": 1},
            "messages": list(messages or []),
        }

    session.ask = fake_ask  # type: ignore[method-assign]
    return session


def _state(pending: list[dict]) -> dict:
    return {
        "messages": [{"role": "user", "content": "帮我做个公司官网"}],
        "steps": 0,
        "pending": pending,
    }


def _answer(monkeypatch, answers: dict | None) -> None:
    """把 interrupt 换成一个"用户马上回答"的桩。位置与返回值形状都和真实一致。"""
    monkeypatch.setattr(graph, "interrupt", lambda payload: answers or {})


async def _events() -> list[AgentEvent]:
    from sqlalchemy import select

    async with get_session_factory()() as session:
        rows = await session.execute(select(AgentEvent).order_by(AgentEvent.seq))
        return list(rows.scalars())


async def test_a_fabricated_phone_never_reaches_the_disk(seeded, tmp_path, monkeypatch) -> None:
    counter = Counter()
    session = _session(tmp_path, [_write_tool(counter)])
    _answer(monkeypatch, {})

    result = await execute_action_node(_state([_call(f"<p>电话：{PHONE}</p>")]), session)

    # 这一次工具压根没被执行。
    assert counter.calls == []
    outcome = result["tool_results"][0]
    assert outcome["ok"] is False
    assert outcome["status"] == "denied"
    assert "已拦下这次写入" in str(outcome)

    # 问的是"这个号码从哪来"，并且问题进了时间线（用户看得见）。
    asked = [event for event in await _events() if event.type == "question.asked"]
    assert asked, [event.type for event in await _events()]
    questions = asked[0].payload["questions"]
    assert [item["header"] for item in questions] == ["电话出处"]
    assert PHONE in questions[0]["question"]


async def test_confirming_the_number_writes_it_as_it_is(seeded, tmp_path, monkeypatch) -> None:
    counter = Counter()
    session = _session(tmp_path, [_write_tool(counter)])
    _answer(monkeypatch, {"fact_1": "就用这个"})

    result = await execute_action_node(_state([_call(PHONE)]), session)

    assert len(counter.calls) == 1
    assert counter.calls[0]["content"] == PHONE
    assert result["tool_results"][0]["ok"] is True


async def test_a_real_number_from_the_user_makes_the_rewrite_land(
    seeded, tmp_path, monkeypatch
) -> None:
    counter = Counter()
    session = _session(tmp_path, [_write_tool(counter)])
    _answer(monkeypatch, {"fact_1": "13900001111"})

    first = await execute_action_node(_state([_call(f"电话 {PHONE}")]), session)
    assert counter.calls == []
    # 理由里必须带着用户给的值：不然它下一步只会再编一个。
    assert "13900001111" in str(first["tool_results"][0])

    # 下一轮按用户给的值重写：这一次应当直接放行，不再弹卡。
    monkeypatch.setattr(graph, "interrupt", lambda payload: pytest.fail("不该再问了"))
    second = await execute_action_node(_state([_call("电话 139-0000-1111")]), session)
    assert len(counter.calls) == 1
    assert "139-0000-1111" in counter.calls[0]["content"]
    assert second["tool_results"][0]["ok"] is True


async def test_a_phone_the_user_already_gave_is_not_questioned(
    seeded, tmp_path, monkeypatch
) -> None:
    counter = Counter()
    session = _session(tmp_path, [_write_tool(counter)])
    _answer(monkeypatch, {})
    state = _state([_call(f"电话 {PHONE}")])
    state["messages"] = [{"role": "user", "content": f"我的电话 {PHONE}，写进联系页"}]

    await execute_action_node(state, session)

    assert len(counter.calls) == 1

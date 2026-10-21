"""只读调研与 token 预算。

两条约束都是为了「别再白烧下去」，所以测试要证明的是真拦住了：

1. 只读模式下写类工具被拒，且拒得干净 —— handler 一次都没被调用过；
2. token 用超之后不再接受新的工具调用，并把这次收尾说清楚。

事故背景：一次「你觉得界面有什么可以优化的」被判成 act，116 次工具调用、
近 600 条事件，最后还被判失败。步数上限一次都没拦住，因为它管的是"跑多久"，
不是"烧多少"——每一步都重发一遍上下文，30k 和 120k 是四倍差距。
"""

from __future__ import annotations

import uuid

import pytest

from blank_api.db import get_session_factory
from blank_api.limits import DEFAULT_TOKEN_BUDGET, TOKEN_WRAP_UP_RATIO
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import (
    AgentSession,
    execute_action_node,
    route_after_validation,
    run_validation_node,
    summarize_result_node,
)
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome
from blank_api.services.tools.gateway import ToolGateway

TASK_ID = "task-readonly-1"


def _schema() -> dict:
    return {"type": "object", "properties": {}, "additionalProperties": False}


class Counter:
    def __init__(self) -> None:
        self.calls: list[str] = []


def _tool(name: str, counter: Counter, risk: str = "low") -> ToolDefinition:
    async def handler(context: ToolContext, arguments: dict) -> ToolOutcome:
        counter.calls.append(name)
        return ToolOutcome(ok=True, output=f"{name} ok", data={"path": "a.txt", "change_type": "modify"})

    return ToolDefinition(
        name=name,
        description=f"测试用 {name}",
        input_schema=_schema(),
        risk_level=risk,
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
        session.add(Project(id="project-readonly", name="只读测试"))
        session.add(Task(id=TASK_ID, project_id="project-readonly", title="只读", goal="只读", max_steps=10))
        await session.commit()


@pytest.fixture
async def seeded():
    from blank_api.db import Base, get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await _seed_task()


def _session(tmp_path, tools: list[ToolDefinition], *, reply: str = "看完了", tool_calls=None) -> AgentSession:
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
        goal="你觉得哪里能优化",
        max_steps=10,
    )

    async def fake_ask(state, prompt, node, *, messages=None):
        return {
            "text": reply,
            "tool_calls": list(tool_calls or []),
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "calls": 1},
            "messages": list(messages or []) + [{"role": "assistant", "content": reply}],
        }

    session.ask = fake_ask  # type: ignore[method-assign]
    return session


async def _events() -> list[tuple[str, dict]]:
    async with get_session_factory()() as session:
        rows = (await session.execute(AgentEvent.__table__.select())).fetchall()
    return [(row.type, row.payload or {}) for row in rows]


async def test_write_tool_is_refused_during_readonly_research(seeded, tmp_path):
    counter = Counter()
    session = _session(tmp_path, [_tool("read_file", counter), _tool("write_file", counter, risk="low")])
    session.readonly = True

    result = await execute_action_node(
        {
            "messages": [{"role": "user", "content": "你觉得哪里能优化"}],
            "steps": 0,
            "readonly": True,
            "pending": [_call("read_file"), _call("write_file")],
        },
        session,
    )

    # 读的跑了，写的没跑 —— 这是这条约束的全部意义
    assert counter.calls == ["read_file"]
    by_tool = {item["tool"]: item for item in result["tool_results"]}
    assert by_tool["read_file"]["ok"] is True
    assert by_tool["write_file"]["ok"] is False
    assert by_tool["write_file"]["status"] == "denied"
    # 拒绝理由要能读：用户看到的是"为什么没做"，不是一句"失败"
    assert "只做调研" in by_tool["write_file"]["output"] or "只做调研" in str(by_tool["write_file"])


async def test_readonly_validation_is_skipped_even_after_a_refused_test_run(seeded, tmp_path):
    """被拒掉的 run_tests 不能当成"测试没过"，否则整个任务会被判失败。"""
    counter = Counter()
    session = _session(tmp_path, [_tool("read_file", counter)])
    session.readonly = True

    validation = await run_validation_node(
        {
            "messages": [],
            "readonly": True,
            "tool_results": [{"tool": "run_tests", "status": "denied", "ok": False, "output": "只读"}],
        },
        session,
    )

    assert validation["validation"]["status"] == "skipped"
    assert validation["validation"]["passed"] is True


async def test_token_budget_stops_the_loop(seeded, tmp_path):
    counter = Counter()
    # 本轮模型还想继续调工具，但预算已经用完：调用必须被丢弃，不能再来一轮
    session = _session(
        tmp_path,
        [_tool("read_file", counter)],
        tool_calls=[_call("read_file")],
    )
    session.token_budget = DEFAULT_TOKEN_BUDGET

    result = await execute_action_node(
        {
            "messages": [{"role": "user", "content": "读代码"}],
            "steps": 0,
            "pending": [],
            "usage": {"prompt_tokens": DEFAULT_TOKEN_BUDGET, "completion_tokens": 0},
        },
        session,
    )

    assert result["budget_exhausted"] is True
    assert result["pending"] == []
    exceeded = [payload for kind, payload in await _events() if kind == "budget.exceeded"]
    assert exceeded and exceeded[0]["reason"] == "token 用完"
    assert exceeded[0]["tokens"] >= DEFAULT_TOKEN_BUDGET


async def test_token_budget_warns_before_it_runs_out(seeded, tmp_path):
    counter = Counter()
    session = _session(tmp_path, [_tool("read_file", counter)])
    session.token_budget = DEFAULT_TOKEN_BUDGET

    result = await execute_action_node(
        {
            "messages": [{"role": "user", "content": "读代码"}],
            "steps": 0,
            "pending": [],
            "usage": {"prompt_tokens": int(DEFAULT_TOKEN_BUDGET * TOKEN_WRAP_UP_RATIO), "completion_tokens": 0},
        },
        session,
    )

    # 收尾区仍然允许完成最后一个动作，只是不再开新工作面
    assert result["budget_exhausted"] is False
    warnings = [payload for kind, payload in await _events() if kind == "budget.warning"]
    assert warnings
    assert warnings[0]["token_budget"] == DEFAULT_TOKEN_BUDGET


async def test_readonly_never_goes_through_review(seeded, tmp_path):
    """预算耗尽时本该去做审查，但只读调研没有交付物可审——直接总结。"""

    class _S:
        readonly = True

    class _NotReadonly:
        readonly = False

    assert (
        route_after_validation(
            {"readonly": True, "budget_exhausted": True, "validation": {"status": "failed"}}, _S()
        )
        == "summarize_result"
    )
    # 反向守卫：动手类任务预算耗尽后仍要审查，不然刚刚的改动没人复核。
    assert (
        route_after_validation(
            {"budget_exhausted": True, "validation": {"status": "failed"}}, _NotReadonly()
        )
        == "review_result"
    )


async def test_readonly_delivery_is_a_judgement_not_a_work_report(seeded, tmp_path):
    counter = Counter()
    session = _session(
        tmp_path,
        [_tool("read_file", counter)],
        reply="输入框是 3 行固定高度，占位文案容易只显示半行。",
    )
    session.readonly = True

    result = await summarize_result_node(
        {
            "messages": [{"role": "user", "content": "你觉得界面哪里能优化"}],
            "readonly": True,
            "tool_results": [{"tool": "read_file", "ok": True, "output": "..."}],
        },
        session,
    )

    assert "输入框是 3 行固定高度" in result["summary"]
    # 不许套「结论 / 下一步」标题：那是工作汇报的壳，用户问的是判断。
    assert "结论" not in result["summary"]
    assert result["acceptance_results"] == []

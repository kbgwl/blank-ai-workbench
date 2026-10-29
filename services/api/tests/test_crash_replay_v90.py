"""崩溃续跑时，同一次工具调用不会被第二次执行。

``execute_action`` 把「执行工具」和「问模型下一步」放在同一个节点里，检查点只落在
节点边界。进程死在两者之间，LangGraph 从节点开头重放；节点若不看执行日志，写入类
工具就会执行两遍（跨进程探针旧读数：progress.txt 两行、ToolCall 两行，
E:/tmp/m90/worker/probe-1.log）。

这里把「同一节点、同一状态、同一 trace 再跑一遍」钉成仓库用例：它证明的是节点自己
的幂等；跨进程接管那一半由 E:/tmp/m90/worker/probe_worker.py 负责。
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.artifacts import DEFAULT_ARTIFACT_ROOT
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import AgentSession, execute_action_node
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome
from blank_api.services.tools.gateway import ToolGateway

pytestmark = pytest.mark.asyncio

TRACE = "task:replay:attempt:1"


class Counter:
    def __init__(self) -> None:
        self.runs: list[str] = []

    def tool(self, name: str) -> ToolDefinition:
        async def handler(context: ToolContext, arguments: dict) -> ToolOutcome:
            self.runs.append(name)
            return ToolOutcome(ok=True, output=f"{name} run #{len(self.runs)}", data={"tool": name})

        return ToolDefinition(
            name=name,
            description=f"测试用 {name}",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            risk_level="low",
            handler=handler,
            timeout_seconds=5.0,
        )


def _call(name: str, call_id: str | None) -> dict:
    item = {"type": "function", "function": {"name": name, "arguments": "{}"}}
    if call_id is not None:
        item["id"] = call_id
    return item


class NoHooks:
    async def run(self, event: str, tool: str = "", payload: dict | None = None):
        return []


async def _seed() -> str:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    task_id = f"t-{uuid.uuid4().hex[:8]}"
    async with get_session_factory()() as session:
        session.add(Project(id=f"p-{task_id}", name="崩溃续跑"))
        session.add(Task(id=task_id, project_id=f"p-{task_id}", title="续跑", goal="续跑", max_steps=6))
        await session.commit()
    return task_id


def _session(tmp_path, task_id: str, counter: Counter) -> AgentSession:
    session = AgentSession(
        adapter=None,  # type: ignore[arg-type]  # 本用例不走到真实模型
        gateway=ToolGateway([counter.tool("run_shell")]),
        context=ToolContext(
            root=tmp_path,
            task_id=task_id,
            session_factory=get_session_factory(),
            approval_policy=lambda *_: False,
        ),
        recorder=EventRecorder(get_session_factory(), task_id, trace_id=TRACE),
        session_factory=get_session_factory(),
        provider_id="provider-1",
        provider_name="mock",
        goal="续跑",
        trace_id=TRACE,
        max_steps=6,
    )
    session.hooks = NoHooks()  # type: ignore[assignment]

    async def fake_ask(state, prompt, node, *, messages=None):
        return {
            "text": "完成",
            "tool_calls": [],
            "usage": {},
            "messages": list(messages or []) + [{"role": "assistant", "content": "完成"}],
        }

    session.ask = fake_ask  # type: ignore[method-assign]
    return session


def _state(call: dict) -> dict:
    return {"messages": [{"role": "user", "content": "往 progress.txt 追加一行"}], "steps": 0, "pending": [call]}


async def _events(task_id: str, event_type: str) -> list[dict]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(AgentEvent.payload).where(
                    AgentEvent.task_id == task_id, AgentEvent.type == event_type
                )
            )
        ).scalars().all()
    return [dict(item or {}) for item in rows]


def _first_tool_message(state: dict) -> str:
    contents = [m["content"] for m in state["messages"] if m.get("role") == "tool"]
    assert contents, state["messages"]
    return contents[0]


def _json(text: str) -> dict:
    return json.loads(text)


@pytest.fixture(autouse=True)
async def seeded():
    task_id = await _seed()
    yield task_id
    shutil.rmtree(Path(DEFAULT_ARTIFACT_ROOT) / task_id, ignore_errors=True)


async def test_the_same_call_runs_once_and_the_model_sees_the_same_result(seeded, tmp_path):
    """同一 call id 重跑节点：工具只执行一次，上下文里还是那一份结果。"""
    counter = Counter()
    session = _session(tmp_path, seeded, counter)
    call = _call("run_shell", "call-fixed-1")

    first = await execute_action_node(_state(call), session)
    second = await execute_action_node(_state(call), session)

    assert counter.runs == ["run_shell"], counter.runs
    assert len(await _events(seeded, "tool.requested")) == 1
    assert len(await _events(seeded, "tool.completed")) == 1
    replayed = await _events(seeded, "tool.replayed")
    assert [item.get("call_id") for item in replayed] == ["call-fixed-1"], replayed

    first_tool = _json(_first_tool_message(first))
    second_tool = _json(_first_tool_message(second))
    # 结果本身必须是同一份：模型拿到的 output/data/status 一致，不因为重放而变样
    for key in ("ok", "status", "output", "data", "tool"):
        assert second_tool.get(key) == first_tool.get(key), (key, first_tool, second_tool)


async def test_a_different_call_id_is_not_replayed(seeded, tmp_path):
    """对照格：换一次调用就必须真的执行——否则上面的绿只是「什么都不跑」。"""
    counter = Counter()
    session = _session(tmp_path, seeded, counter)

    await execute_action_node(_state(_call("run_shell", "call-a")), session)
    await execute_action_node(_state(_call("run_shell", "call-b")), session)

    assert counter.runs == ["run_shell", "run_shell"], counter.runs
    assert len(await _events(seeded, "tool.completed")) == 2
    assert await _events(seeded, "tool.replayed") == []


async def test_a_call_without_an_id_is_never_replayed(seeded, tmp_path):
    """没有模型给的 id 就分不清「同一次」还是「又来一次」，宁可再执行也不顶掉。

    拿工具名当键会把第二次 grep 对成第一次的结果——那比多跑一次更糟。
    """
    counter = Counter()
    session = _session(tmp_path, seeded, counter)

    await execute_action_node(_state(_call("run_shell", None)), session)
    await execute_action_node(_state(_call("run_shell", None)), session)

    assert counter.runs == ["run_shell", "run_shell"], counter.runs
    assert await _events(seeded, "tool.replayed") == []


async def test_a_new_call_reusing_an_answered_id_is_not_swallowed(seeded, tmp_path):
    """供应商复用 tool_call id：第二条是新调用，不许拿第一条的结果把它顶掉。

    形状取自方言端到端第 80/83/84/85 步（桩那时每轮都发 ``call_1``）：第一条命令执行完、
    结果已经进了上下文，模型在这一步的基础上又点了**第二条**命令，id 仍是 ``call_1``。
    旧口径只按 id 认执行日志，于是第二条被当成第一条的重放——工具不执行、审批卡不挂、
    任务直接 succeeded。对用户就是"点了批准、命令一条没跑、它说干完了"。

    判据看两头：工具真的执行了两次，且上下文里两条命令各有一份自己的结果。
    """
    counter = Counter()
    session = _session(tmp_path, seeded, counter)
    call = _call("run_shell", "call-same")

    first = await execute_action_node(_state(call), session)
    # 第二次进来时，对话里已经带着第一条的结果——这正是"模型据此点出下一条"的那一刻。
    state = {
        "messages": list(first["messages"]) + [{"role": "assistant", "content": "", "tool_calls": [call]}],
        "steps": 1,
        "pending": [call],
    }
    second = await execute_action_node(state, session)

    assert counter.runs == ["run_shell", "run_shell"], counter.runs
    assert len(await _events(seeded, "tool.completed")) == 2
    assert await _events(seeded, "tool.replayed") == []
    contents = [_json(m["content"]) for m in second["messages"] if m.get("role") == "tool"]
    assert len(contents) == 2, contents
    # 两次执行的结果必须各是各的（Counter 的 output 里带着第几次），不能是同一条抄两遍
    assert {item.get("output") for item in contents} == {"run_shell run #1", "run_shell run #2"}, contents

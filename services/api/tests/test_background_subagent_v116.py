"""#116：后台子代理句柄（spawn_agent(background) / task_output / task_stop）。

对标 Claude Code 的 Task 工具面：派出去的子代理不必把父代理按在那里等，
父代理先拿到一个句柄继续做事，需要结论时再取，不要了能停。这里钉五件事：

1. **非阻塞**：`spawn_agent(background=true)` 在子代理跑完之前就返回 `agent_id`；
2. **可取结论**：`task_output(block=true)` 等到结束并带回结论，超时只返回
   "仍在运行"，**不能把子代理取消掉**；
3. **可停**：`task_stop` 取消子代理、删掉隔离副本，并留下 `subagent.cancelled`；
4. **未知句柄如实说**：不猜、不返回别人的结论；
5. **任务收尾不留孤儿**：任务结束（含失败）时仍在跑的后台子代理必须被取消，
   并在事件里点名，否则它们会在任务显示"已完成"之后继续烧钱。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.task import AgentEvent
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.runtime import AgentRuntime
from blank_api.services.models.base import AdapterConfig, ModelAdapter, ModelResponse, Usage
from blank_api.services.subagent import SubAgentRequest, SubAgentRunner
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.delegate import delegate_tools
from blank_api.services.tools.gateway import ToolGateway

from tests.test_agent_v04 import (  # noqa: F401  (env 是夹具，故意导入)
    create_project,
    create_task,
    env,
)


class SlowAdapter(ModelAdapter):
    """每次模型调用都睡 `delay` 秒；足够让"后台"与"等待"两条路径区分开。"""

    def __init__(self, delay: float, text: str = "子代理结论") -> None:
        super().__init__(
            AdapterConfig(
                name="slow-v116",
                kind="openai",
                base_url="http://localhost",
                api_key="",
                model="slow-v116",
                supports_tools=False,
            )
        )
        self.delay = delay
        self.text = text

    async def invoke(self, messages, **kwargs) -> ModelResponse:  # pragma: no cover
        return ModelResponse(text=self.text, usage=Usage(1, 1), model="slow-v116")

    async def stream(self, messages, **kwargs):  # pragma: no cover
        yield ""

    async def bind_tools(self, specs, messages, **kwargs) -> ModelResponse:
        await asyncio.sleep(self.delay)
        return ModelResponse(text=self.text, usage=Usage(12, 4), model="slow-v116")


class FakeRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.run_id = ""
        self.origin_run_id = ""

    async def add(self, event: str, node: str, payload: dict) -> None:
        self.events.append((event, payload))

    def types(self) -> list[str]:
        return [event for event, _ in self.events]


class MirrorRecorder(FakeRecorder):
    """既留在内存里供等待/断言，又写进真实事件表——收尾清理的判据要读库。"""

    def __init__(self, inner: EventRecorder) -> None:
        super().__init__()
        self.inner = inner
        self.run_id = inner.run_id
        self.origin_run_id = inner.origin_run_id

    async def add(self, event: str, node: str, payload: dict) -> None:
        self.events.append((event, payload))
        await self.inner.add(event, node, payload)


def _session(root: Path, adapter: SlowAdapter, *, task_id: str = "task-v116"):
    return SimpleNamespace(
        adapter=adapter,
        recorder=FakeRecorder(),
        trace_id="thread-v116",
        root_thread_id="thread-v116",
        depth=0,
        agent_path="root",
        sandbox_tier="workspace",
        session_factory=None,
        provider_id="",
        provider_name="",
        compactor=None,
        context=SimpleNamespace(root=Path(root), task_id=task_id),
    )


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等待的条件在超时前没有成立")


# --------------------------------------------------------------------------- #
# 1. 非阻塞句柄
# --------------------------------------------------------------------------- #
async def test_background_spawn_returns_before_the_child_finishes(workspace: Path) -> None:
    session = _session(workspace, SlowAdapter(1.0))
    runner = SubAgentRunner().bind(session)
    try:
        started = time.perf_counter()
        agent_id, error = await runner.spawn_background(SubAgentRequest(goal="慢慢查清楚", max_steps=1))
        elapsed = time.perf_counter() - started
        assert error is None and agent_id
        # 子代理要跑 1 秒；句柄必须在它结束前就回来，否则「后台」只是换个说法。
        assert elapsed < 0.5, elapsed

        running = await runner.output(agent_id, block=False)
        assert running["status"] == "running"

        done = await runner.output(agent_id, block=True, timeout=10)
        assert done["status"] == "completed"
        assert done["summary"] == "子代理结论"

        started_events = [
            payload for event, payload in session.recorder.events if event == "subagent.started"
        ]
        completed_events = [
            payload for event, payload in session.recorder.events if event == "subagent.completed"
        ]
        assert started_events and started_events[0]["agent_id"] == agent_id
        assert completed_events and completed_events[0]["agent_id"] == agent_id
    finally:
        await runner.aclose(session.context.task_id)


async def test_task_output_timeout_does_not_cancel_the_child(workspace: Path) -> None:
    session = _session(workspace, SlowAdapter(0.6))
    runner = SubAgentRunner().bind(session)
    try:
        agent_id, _ = await runner.spawn_background(SubAgentRequest(goal="慢活", max_steps=1))
        early = await runner.output(agent_id, block=True, timeout=0.05)
        assert early["status"] == "running"
        # 等超时不能把子代理等死：它还要能给出结论。
        late = await runner.output(agent_id, block=True, timeout=10)
        assert late["status"] == "completed"
    finally:
        await runner.aclose(session.context.task_id)


# --------------------------------------------------------------------------- #
# 2. 停止与清理
# --------------------------------------------------------------------------- #
async def test_stop_cancels_the_child_and_removes_its_copy(workspace: Path) -> None:
    session = _session(workspace, SlowAdapter(30.0))
    runner = SubAgentRunner().bind(session)
    agent_id, _ = await runner.spawn_background(
        SubAgentRequest(goal="卡住不返回", isolate=True, timeout_seconds=60)
    )
    await _wait_for(lambda: "worktree.created" in session.recorder.types())
    created = next(
        payload for event, payload in session.recorder.events if event == "worktree.created"
    )
    tree = Path(created["path"])
    assert tree.is_dir()

    started = time.perf_counter()
    stopped = await runner.stop(agent_id)
    elapsed = time.perf_counter() - started
    assert stopped["status"] == "cancelled"
    # 不能等 30 秒的模型调用自然结束。
    assert elapsed < 5.0, elapsed
    assert not tree.exists(), "取消后隔离副本必须删掉"

    cancelled = [
        payload for event, payload in session.recorder.events if event == "subagent.cancelled"
    ]
    assert cancelled and cancelled[0]["agent_id"] == agent_id
    assert cancelled[0]["worktree_removed"] is True
    # 已经结束的句柄仍然可取状态，但不会被当成"还在跑"。
    assert (await runner.output(agent_id, block=False))["status"] == "cancelled"


async def test_unknown_agent_id_is_reported_not_guessed(workspace: Path) -> None:
    session = _session(workspace, SlowAdapter(0.01))
    runner = SubAgentRunner().bind(session)
    # 先留一个真实句柄：这样"找不到时拿最后一个凑数"的实现才会被这一步抓住，
    # 只查空注册表的话，任何猜测型实现都会碰巧通过。
    real, _ = await runner.spawn_background(SubAgentRequest(goal="真实句柄", max_steps=1))
    try:
        missing = await runner.output("agent-missing", block=False)
        assert missing["status"] == "not_found" and missing["ok"] is False
        assert "agent-missing" in missing["message"]
        stopped = await runner.stop("agent-missing")
        assert stopped["status"] == "not_found" and stopped["ok"] is False
        assert "agent-missing" in stopped["message"]
    finally:
        await runner.aclose(session.context.task_id)


# --------------------------------------------------------------------------- #
# 3. 任务收尾不留孤儿
# --------------------------------------------------------------------------- #
async def test_finishing_the_task_cancels_and_reports_leftovers(env, workspace: Path):  # noqa: F811
    project_id = await create_project(env, workspace)
    task_id = await create_task(env, project_id, ["后台子代理"])
    factory = get_session_factory()
    recorder = EventRecorder(factory, task_id, trace_id="thread-v116")
    session = _session(workspace, SlowAdapter(30.0), task_id=task_id)
    session.recorder = MirrorRecorder(recorder)
    runner = SubAgentRunner().bind(session)
    agent_id, _ = await runner.spawn_background(
        SubAgentRequest(goal="任务结束时还没跑完", isolate=True, timeout_seconds=60)
    )
    await _wait_for(lambda: "worktree.created" in session.recorder.types())
    tree = Path(
        next(payload for event, payload in session.recorder.events if event == "worktree.created")[
            "path"
        ]
    )

    scope = SimpleNamespace(
        store=factory,
        task_id=task_id,
        recorder=recorder,
        session=SimpleNamespace(router=None),
        subagents=runner,
    )
    await AgentRuntime(factory)._finish(scope, "succeeded", summary="完成", reason="")

    assert (await runner.output(agent_id, block=False))["status"] == "not_found"
    assert not tree.exists()
    async with factory() as db:
        rows = (
            await db.execute(select(AgentEvent).where(AgentEvent.task_id == task_id))
        ).scalars().all()
    types = [row.type for row in rows]
    assert "subagents.released" in types and "subagent.cancelled" in types
    released = next(row for row in rows if row.type == "subagents.released")
    assert released.payload["cancelled"] == [agent_id]


# --------------------------------------------------------------------------- #
# 4. 工具面：spawn_agent(background) → task_output → task_stop
# --------------------------------------------------------------------------- #
async def test_gateway_exposes_background_spawn_output_and_stop(env, workspace: Path):  # noqa: F811
    project_id = await create_project(env, workspace)
    task_id = await create_task(env, project_id, ["后台子代理工具面"])
    session = _session(workspace, SlowAdapter(1.0), task_id=task_id)
    session.session_factory = get_session_factory()
    runner = SubAgentRunner().bind(session)
    context = ToolContext(
        root=workspace,
        task_id=task_id,
        session_factory=get_session_factory(),
        subagent_runner=runner,
    )
    gateway = ToolGateway(delegate_tools())

    started = time.perf_counter()
    spawned = await gateway.execute(
        "spawn_agent", {"goal": "后台查一下 main.py", "background": True}, context, "execute_action"
    )
    elapsed = time.perf_counter() - started
    assert spawned["ok"] is True and elapsed < 0.5, spawned
    agent_id = spawned["data"]["agent_id"]
    assert spawned["data"]["status"] == "running"

    output = await gateway.execute(
        "task_output", {"agent_id": agent_id, "block": True, "timeout": 10}, context, "execute_action"
    )
    assert output["ok"] is True, output
    assert output["data"]["status"] == "completed"
    assert output["data"]["summary"] == "子代理结论"

    second, _ = await runner.spawn_background(SubAgentRequest(goal="再派一个卡住的", timeout_seconds=60))
    stopped = await gateway.execute(
        "task_stop", {"agent_id": second}, context, "execute_action"
    )
    assert stopped["ok"] is True and stopped["data"]["status"] == "cancelled"
    await runner.aclose(task_id)

    names = {tool.name for tool in delegate_tools()}
    assert {"spawn_agent", "task_output", "task_stop"} <= names

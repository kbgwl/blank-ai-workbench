# -*- coding: utf-8 -*-
"""Worker 崩溃恢复、租约接管、以及"接管之后工具不会被跑第二遍"。

三条判据对应的都是真实事故形状：

* **崩溃恢复**：进程在写终态之前被杀，下次启动必须从检查点续跑，而不是从头再开一遍。
  `attempts > 0` 就是这个事实的凭据——所以它必须真的把 `crashed=True` 传下去。
* **租约接管**：租约还新鲜的 run 不许被别人抢；过期的必须能被接走，否则一台机器挂掉
  之后那条任务就永远停在"运行中"。
* **工具不重跑**：接管与续跑都会重放节点，重放必须复用执行日志里的结果。这一条在
  `test_crash_replay_v90.py` 里按节点钉过，这里补一条"从接管入口进去也一样"的。
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from blank_api import worker as worker_module
from blank_api.api.routes import tasks as task_routes
from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.project import Project
from blank_api.models.run import AgentRun
from blank_api.models.task import AgentEvent, Task
from blank_api.services import metrics
from blank_api.services.agent.artifacts import DEFAULT_ARTIFACT_ROOT
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import AgentSession, execute_action_node
from blank_api.services.agent.queue import utc_now
from blank_api.services.agent.runtime import RunRequest
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome
from blank_api.services.tools.gateway import ToolGateway

TRACE = "task:takeover:attempt:1"


@pytest.fixture(autouse=True)
async def schema() -> AsyncIterator[None]:
    metrics.reset()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    metrics.reset()


async def _new_run(*, attempts: int = 0, status: str = "queued", lease: timedelta | None = None) -> tuple[str, str]:
    """建一条任务 + run，返回 (task_id, run_id)。

    ``lease`` 给了就写上租约（正数=还新鲜，负数=已过期），模拟另一个 worker 的持有状态。
    """
    stamp = uuid.uuid4().hex[:8]
    async with get_session_factory()() as session:
        session.add(Project(id=f"p-{stamp}", name="p"))
        session.add(
            Task(
                id=f"t-{stamp}",
                project_id=f"p-{stamp}",
                title="t",
                goal="t",
                status=status,
                provider_id="",
            )
        )
        await session.flush()
        run = AgentRun(
            id=f"r-{stamp}",
            task_id=f"t-{stamp}",
            status=status,
            attempts=attempts,
            request_payload={"provider_id": "p-missing", "approve_high_risk": False},
        )
        if lease is not None:
            run.worker_id = "someone-else"
            run.lease_expires_at = utc_now() + lease
        session.add(run)
        await session.commit()
    return f"t-{stamp}", f"r-{stamp}"


@pytest.fixture
def captured(monkeypatch):
    """把"真正去执行"这一步换成记录器，只量 worker 的决策。

    入参重建也一起换掉：那条路径要一个绑定好的工作区与供应商，与本节要量的
    "崩溃/接管决策"无关，拉进来只会让用例依赖一堆无关夹具。
    """
    calls: list[dict] = []

    async def fake_execute(run_id, worker_id, request, *, crashed=False, lease_seconds=90):
        calls.append({"run_id": run_id, "worker_id": worker_id, "crashed": crashed})

    async def fake_request(run_id: str) -> RunRequest:
        async with get_session_factory()() as session:
            run = await session.get(AgentRun, run_id)
            assert run is not None
            return RunRequest(
                task_id=run.task_id, provider_id="pv", provider_name="n", adapter=None, root="."
            )

    monkeypatch.setattr(task_routes, "_execute_claimed_run", fake_execute)
    monkeypatch.setattr(worker_module, "_request_for", fake_request)
    return calls


async def test_crashed_run_is_recovered_as_a_resume(captured):
    _, run_id = await _new_run(attempts=1)

    handled = await worker_module.run_once("worker-a")

    assert handled == 1
    assert captured and captured[0]["run_id"] == run_id
    # 关键的一格：必须告诉运行时"上次没正常收尾"，否则它会从头再开一遍。
    assert captured[0]["crashed"] is True
    assert metrics.counter(metrics.WORKER_RECOVERED) == 1


async def test_fresh_run_is_not_treated_as_a_crash(captured):
    await _new_run(attempts=0)

    assert await worker_module.run_once("worker-a") == 1
    assert captured[0]["crashed"] is False
    assert metrics.counter(metrics.WORKER_RECOVERED) == 0


async def test_live_lease_is_not_preempted(captured):
    """另一个进程正持有租约：这一轮一条都不许领走。"""
    await _new_run(status="running", attempts=1, lease=timedelta(seconds=90))

    assert await worker_module.run_once("worker-a") == 0
    assert captured == []


async def test_expired_lease_is_taken_over(captured):
    _, run_id = await _new_run(status="running", attempts=1, lease=timedelta(seconds=-5))

    assert await worker_module.run_once("worker-b") == 1
    assert captured[0]["run_id"] == run_id
    # 接管要换主人：租约跟着 worker 身份走，旧进程之后写不进来。
    async with get_session_factory()() as session:
        run = await session.get(AgentRun, run_id)
        assert run.worker_id == "worker-b"


async def test_run_that_cannot_start_is_abandoned_not_looped_forever(captured):
    task_id, run_id = await _new_run(attempts=task_routes.MAX_RECOVERY_ATTEMPTS)

    handled = await worker_module.run_once("worker-a")

    assert handled == 0
    assert captured == [], "反复启动失败的 run 不许再一次次重试"
    async with get_session_factory()() as session:
        run = await session.get(AgentRun, run_id)
        task = await session.get(Task, task_id)
        failures = (
            await session.execute(
                select(AgentEvent.payload).where(
                    AgentEvent.task_id == task_id, AgentEvent.type == "task.failed"
                )
            )
        ).scalars().all()
    assert run.status == "failed"
    assert task.status == "failed"
    # 原因要写给用户看，不能只说"失败了"。
    assert failures and "重新发起" in str(failures[-1].get("message") or failures[-1].get("reason") or "")
    assert metrics.counter(metrics.WORKER_ABANDONED) == 1


async def test_second_worker_cannot_claim_a_run_that_is_already_leased(captured):
    """认领是原子的：第一个 worker 拿到之后，第二个同拍必须一事无成。

    它的租约刚写下去还新鲜，所以第二个 worker 走的是"看到 live lease → 让开"，
    而不是"接管"。这一格与 `test_live_lease_is_not_preempted` 是一对：那边造的是
    外部持有者，这边造的是"刚被真实认领过"的形状。
    """
    _, run_id = await _new_run(attempts=0)

    first = await worker_module.run_once("worker-a")
    second = await worker_module.run_once("worker-b")

    assert (first, second) == (1, 0)
    assert len(captured) == 1, captured
    async with get_session_factory()() as session:
        run = await session.get(AgentRun, run_id)
    assert run.worker_id == "worker-a"


# ── 接管之后：工具不许跑第二遍 ──────────────────────────────────────────────


class Counter:
    def __init__(self) -> None:
        self.runs: list[str] = []

    def tool(self) -> ToolDefinition:
        async def handler(context: ToolContext, arguments: dict) -> ToolOutcome:
            self.runs.append("write")
            return ToolOutcome(ok=True, output=f"写入第 {len(self.runs)} 次", data={"count": len(self.runs)})

        return ToolDefinition(
            name="run_shell",
            description="测试用写操作",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            risk_level="low",
            handler=handler,
            timeout_seconds=5.0,
        )


class NoHooks:
    async def run(self, event: str, tool: str = "", payload: dict | None = None):
        return []


def _session(tmp_path, task_id: str, counter: Counter) -> AgentSession:
    session = AgentSession(
        adapter=None,  # type: ignore[arg-type]  # 本用例不走到真实模型
        gateway=ToolGateway([counter.tool()]),
        context=ToolContext(
            root=tmp_path,
            task_id=task_id,
            session_factory=get_session_factory(),
            approval_policy=lambda *_: False,
        ),
        recorder=EventRecorder(get_session_factory(), task_id, trace_id=TRACE),
        session_factory=get_session_factory(),
        provider_id="p",
        provider_name="mock",
        goal="接管",
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


async def test_takeover_replays_an_executed_call_instead_of_running_it_again(tmp_path):
    """接管重放节点时，已经执行完的那条调用必须复用结果。

    ``session.effects`` 是"真的执行过"的计数——它比"事件里有几条"更严格：
    重放、拦截、驳回都不加。
    """
    task_id, _ = await _new_run(attempts=1)
    counter = Counter()
    session = _session(tmp_path, task_id, counter)
    call = {"id": "call-takeover-1", "type": "function", "function": {"name": "run_shell", "arguments": "{}"}}
    state = {"messages": [{"role": "user", "content": "写一次"}], "steps": 0, "pending": [call]}

    try:
        await execute_action_node(state, session)
        assert session.effects == 1
        assert counter.runs == ["write"]

        # 模拟新 worker 接管后从同一节点重放：同一个 call id 又回来了。
        await execute_action_node(state, session)
        await execute_action_node(state, session)

        assert counter.runs == ["write"], counter.runs
        assert session.effects == 1, "重放不算一次执行"
        async with get_session_factory()() as session_db:
            completed = (
                await session_db.execute(
                    select(AgentEvent.payload).where(
                        AgentEvent.task_id == task_id, AgentEvent.type == "tool.completed"
                    )
                )
            ).scalars().all()
        assert len(completed) == 1
    finally:
        shutil.rmtree(Path(DEFAULT_ARTIFACT_ROOT) / task_id, ignore_errors=True)

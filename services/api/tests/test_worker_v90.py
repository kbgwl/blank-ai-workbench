# -*- coding: utf-8 -*-
"""#90 独立 worker：认领、崩溃续跑、停止重试、以及 API 不再抢队列。

这里钉的是 worker 的决策，不钉运行时本身：
- 第一次被认领 → 正常开跑；
- attempts 已经大于 0 → 上次没写终态，必须走 ``resume_crash``（从检查点续跑），
  而不是 ``run`` 新开 attempt 把工具再执行一遍；
- 反复启动失败到上限 → 判死并把原因交给用户，不再空转；
- API 进程内执行开关关掉后，启动恢复不再认领这条队列。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime

import pytest
from sqlalchemy import select

from blank_api import worker as worker_module
from blank_api.api.routes import tasks
from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.project import Project
from blank_api.models.run import AgentRun
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.runtime import RunRequest

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def schema() -> AsyncIterator[None]:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


async def _new_run(*, attempts: int = 0, status: str = "queued", leased: bool = False) -> tuple[str, str]:
    async with get_session_factory()() as session:
        stamp = uuid.uuid4().hex[:8]
        project = Project(id=f"p-{stamp}", name="p", description="")
        session.add(project)
        await session.flush()
        task = Task(id=f"t-{stamp}", project_id=project.id, title="t", goal="g", status=status)
        session.add(task)
        await session.flush()
        run = AgentRun(
            id=f"r-{stamp}", task_id=task.id, status=status, attempts=attempts,
            request_payload={},
        )
        if leased:
            from datetime import timedelta

            from blank_api.services.agent.queue import utc_now

            run.worker_id = "someone-else"
            run.lease_expires_at = utc_now() + timedelta(seconds=90)
        session.add(run)
        await session.commit()
        return task.id, run.id


async def _row(run_id: str) -> dict:
    async with get_session_factory()() as session:
        run = await session.get(AgentRun, run_id)
        assert run is not None
        return {"status": run.status, "worker_id": run.worker_id, "attempts": run.attempts}


@pytest.fixture
def executed(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    async def fake_execute(run_id, worker_id, request, *, crashed=False, lease_seconds=90):
        calls.append(
            {
                "run_id": run_id,
                "worker": worker_id,
                "task_id": request.task_id,
                "crashed": crashed,
                "lease_seconds": lease_seconds,
            }
        )

    async def fake_request(run_id: str) -> RunRequest:
        async with get_session_factory()() as session:
            run = await session.get(AgentRun, run_id)
            assert run is not None
            return RunRequest(
                task_id=run.task_id, provider_id="pv", provider_name="n", adapter=None, root="."
            )

    monkeypatch.setattr(tasks, "_execute_claimed_run", fake_execute)
    monkeypatch.setattr(worker_module, "_request_for", fake_request)
    return calls


async def test_a_queued_run_is_claimed_once_and_runs_normally(executed) -> None:
    task_id, run_id = await _new_run()

    handled = await worker_module.run_once("w-1")

    assert handled == 1
    assert len(executed) == 1, executed
    assert executed[0]["task_id"] == task_id
    assert executed[0]["crashed"] is False, executed
    assert executed[0]["lease_seconds"] == worker_module.LEASE_SECONDS
    row = await _row(run_id)
    assert (row["status"], row["worker_id"], row["attempts"]) == ("running", "w-1", 1), row


async def test_a_second_claim_of_the_same_run_resumes_from_the_checkpoint(executed) -> None:
    """attempts=1 说明上次认领后没写终态：必须续跑，不能从头再执行工具。"""
    _, run_id = await _new_run(attempts=1, status="running")

    assert await worker_module.run_once("w-2") == 1
    assert len(executed) == 1
    assert executed[0]["crashed"] is True, executed
    assert (await _row(run_id))["attempts"] == 2


async def test_a_fresh_lease_is_left_to_its_owner(executed) -> None:
    _, run_id = await _new_run(attempts=1, status="running", leased=True)

    assert await worker_module.run_once("w-3") == 0
    assert executed == []
    row = await _row(run_id)
    assert (row["status"], row["worker_id"]) == ("running", "someone-else"), row


async def test_a_lease_expiring_exactly_now_is_scanned_for_takeover(
    executed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """租约到期这一拍：扫描必须认它已过期，否则 worker 整整一拍扫不到、白等一个轮询。

    冻结 worker 的时钟让这一格能失败：不冻结时两次 utc_now() 之间可能跨过一个刻度，
    `<` 与 `<=` 都能过。本机同拍相等 200/200（E:/tmp/kbg144/clock_probe.py）。
    """
    frozen = datetime(2026, 10, 29, 0, 0, 0, 500000)
    _, run_id = await _new_run(attempts=1, status="running")
    async with get_session_factory()() as session:
        run = await session.get(AgentRun, run_id)
        assert run is not None
        run.lease_expires_at = frozen
        await session.commit()
    monkeypatch.setattr(worker_module, "utc_now", lambda: frozen)

    assert await worker_module.run_once("w-5") == 1
    assert len(executed) == 1 and executed[0]["crashed"] is True, executed
    row = await _row(run_id)
    assert (row["status"], row["worker_id"], row["attempts"]) == ("running", "w-5", 2), row


async def test_a_run_that_kept_crashing_is_stopped_with_a_reason(executed) -> None:
    task_id, run_id = await _new_run(attempts=tasks.MAX_RECOVERY_ATTEMPTS, status="running")

    assert await worker_module.run_once("w-4") == 0
    assert executed == [], "到上限的执行不允许再被领走"
    assert (await _row(run_id))["status"] == "failed"
    async with get_session_factory()() as session:
        task = await session.get(Task, task_id)
        assert task is not None and task.status == "failed"
        failed = (
            await session.execute(
                select(AgentEvent).where(
                    AgentEvent.task_id == task_id, AgentEvent.type == "task.failed"
                )
            )
        ).scalars().all()
    assert failed, "判死必须留下一条 task.failed，用户要知道为什么停了"


async def test_the_api_leaves_the_queue_to_the_worker_when_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, run_id = await _new_run()

    monkeypatch.setenv("BLANK_INPROCESS_RUNNER", "0")
    assert tasks.inprocess_runner_enabled() is False
    assert await tasks.recover_pending_runs() == 0
    row = await _row(run_id)
    assert (row["status"], row["worker_id"], row["attempts"]) == ("queued", "", 0), row

    # 对照格：默认（开关打开）必须认这条队列，否则上面那格只是"谁都不认领"
    monkeypatch.delenv("BLANK_INPROCESS_RUNNER")
    assert tasks.inprocess_runner_enabled() is True

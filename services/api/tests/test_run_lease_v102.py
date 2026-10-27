# -*- coding: utf-8 -*-
"""#90/#102 运行租约：谁认领的、谁续的、谁能收尾。

这一组判据量的是"同一条 run 会不会被两个人同时算账"。租约的语义只有一句话：
**写这条 run 的资格跟着租约走**。所以下面每一格都问同一个问题的两面——
持约人能做成，失去租约的人必须做不成；两边都做成或都做不成，就是缺陷。

同库六路并发认领的形状已在跨进程探针实测（E:/tmp/m90/race-3.log：赢家恰好 1、
0 次 database is locked）；这里把它钉成仓库用例，另加租约到期后的三条。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select

from blank_api.api.routes import tasks
from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.project import Project
from blank_api.models.run import AgentRun
from blank_api.models.task import Task
from blank_api.services.agent.queue import abandon_run, claim_run, finish_run, renew_run
from blank_api.services.agent.runtime import RunRequest

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def schema() -> AsyncIterator[None]:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


async def _new_run() -> tuple[str, str]:
    """建一条 queued 的 run，返回 (task_id, run_id)。"""
    async with get_session_factory()() as session:
        stamp = uuid.uuid4().hex[:8]
        project = Project(id=f"p-{stamp}", name="p", description="")
        session.add(project)
        await session.flush()
        task = Task(id=f"t-{stamp}", project_id=project.id, title="t", goal="g", status="queued")
        session.add(task)
        await session.flush()
        run = AgentRun(id=f"r-{stamp}", task_id=task.id, status="queued", request_payload={})
        session.add(run)
        await session.commit()
        return task.id, run.id


async def _row(run_id: str) -> dict:
    async with get_session_factory()() as session:
        run = await session.get(AgentRun, run_id)
        assert run is not None
        return {"status": run.status, "worker_id": run.worker_id, "attempts": run.attempts,
                "lease": run.lease_expires_at}


async def test_six_workers_one_claim_and_one_attempt() -> None:
    """六路并发认领：赢家必须恰好一个，attempts 必须只加一次。

    这一格防的是"两个 worker 都以为拿到了任务，各跑一遍各写一次账"。
    """
    _, run_id = await _new_run()

    async def grab(worker: str) -> bool:
        async with get_session_factory()() as session:
            return await claim_run(session, run_id, worker)

    won = await asyncio.gather(*(grab(f"w{i}") for i in range(6)))
    row = await _row(run_id)
    assert sum(1 for got in won if got) == 1, won
    assert row["attempts"] == 1, row
    assert row["status"] == "running", row
    assert row["worker_id"].startswith("w"), row
    assert row["lease"] is not None, row


async def test_a_fresh_lease_keeps_everyone_out() -> None:
    """租约没到期：别人不能抢，也不能替它续。"""
    _, run_id = await _new_run()
    async with get_session_factory()() as session:
        assert await claim_run(session, run_id, "owner-A", lease_seconds=90)
    async with get_session_factory()() as session:
        assert not await claim_run(session, run_id, "owner-B", lease_seconds=90)
    async with get_session_factory()() as session:
        assert not await renew_run(session, run_id, "owner-B", lease_seconds=90)
    assert (await _row(run_id))["worker_id"] == "owner-A"


async def test_an_expired_lease_is_not_the_old_owners_anymore() -> None:
    """租约过期后，旧 owner 的续租必须失败。

    queue.renew_run 的注释写着 "stale workers cannot revive a run"——过期就是 stale。
    如果过期还能自己续上，接管窗口永远打开着，租约等于没有：老 worker 一边跑，
    接管者一边跑，两边都往同一条 run 写。
    """
    _, run_id = await _new_run()
    async with get_session_factory()() as session:
        assert await claim_run(session, run_id, "owner-A", lease_seconds=0)  # 落到库里就已过期
    row = await _row(run_id)
    assert row["lease"] is not None and row["lease"] <= _now(), row
    async with get_session_factory()() as session:
        assert not await renew_run(session, run_id, "owner-A", lease_seconds=90), await _row(run_id)


async def test_a_non_owner_cannot_write_the_terminal_state() -> None:
    """被接管之后，原 worker 不能把这条 run 收尾。

    实测过：改之前 finish_run 只按 run_id 更新，A 在 B 接管之后照样写成 succeeded，
    账本上就成了"两个人都收尾过"（E:/tmp/m90/lease-readings.txt）。
    """
    _, run_id = await _new_run()
    async with get_session_factory()() as session:
        assert await claim_run(session, run_id, "owner-A", lease_seconds=0)
    async with get_session_factory()() as session:
        assert await claim_run(session, run_id, "owner-B", lease_seconds=90)
    async with get_session_factory()() as session:
        assert not await finish_run(session, run_id, "succeeded", worker_id="owner-A"), await _row(run_id)
    row = await _row(run_id)
    assert (row["status"], row["worker_id"]) == ("running", "owner-B"), row

    # 对照格：现役 owner 必须写得成，且租约随收尾清空——否则上面的红只是"谁都写不成"
    async with get_session_factory()() as session:
        assert await finish_run(session, run_id, "succeeded", worker_id="owner-B")
    row = await _row(run_id)
    assert (row["status"], row["lease"]) == ("succeeded", None), row


async def test_giving_up_before_execution_needs_no_owner_but_a_stale_lease() -> None:
    """启动恢复里"不再重试"那两格：没有 worker，但也不能踩住一条正被别人跑的 run。"""
    # queued、从没被认领：可以直接判死
    _, run_id = await _new_run()
    async with get_session_factory()() as session:
        assert await abandon_run(session, run_id)
    assert (await _row(run_id))["status"] == "failed"

    # running 且租约还新鲜（另一个进程正在跑）：判死必须失败
    _, running = await _new_run()
    async with get_session_factory()() as session:
        assert await claim_run(session, running, "owner-A", lease_seconds=90)
    async with get_session_factory()() as session:
        assert not await abandon_run(session, running)
    row = await _row(running)
    assert (row["status"], row["worker_id"]) == ("running", "owner-A"), row

    # running 但租约已过期（上次执行没收尾就断了）：判死成立，并把 worker 与租约清干净
    _, stale = await _new_run()
    async with get_session_factory()() as session:
        assert await claim_run(session, stale, "owner-A", lease_seconds=0)
    async with get_session_factory()() as session:
        assert await abandon_run(session, stale)
    row = await _row(stale)
    assert (row["status"], row["lease"], row["worker_id"]) == ("failed", None, ""), row


async def test_losing_the_lease_stops_the_execution_not_only_the_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """续租失败必须把执行 itself 停下来，并且一行账都不许再写。

    改之前心跳只是 return：循环不续租了，AgentRuntime 照跑到底，最后照样 finish。
    那时租约早归别人，写下去的终态就是抢账。所以这一格两头都钉：
    执行必须收到取消，run 行必须保持"running / owner-A / attempts=1"原样。
    """
    task_id, run_id = await _new_run()
    async with get_session_factory()() as session:
        assert await claim_run(session, run_id, "owner-A", lease_seconds=0)  # 一开始就过期

    cancelled = asyncio.Event()

    class HangingRuntime:
        def __init__(self, session_factory) -> None:
            pass

        async def run(self, request: RunRequest) -> dict:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return {"task": {"status": "succeeded"}}  # pragma: no cover - 真跑不到

    monkeypatch.setattr(tasks, "AgentRuntime", HangingRuntime)
    monkeypatch.setattr(tasks, "LEASE_HEARTBEAT_SECONDS", 0.05)
    request = RunRequest(task_id=task_id, provider_id="pv", provider_name="n", adapter=None, root=".")

    worker = asyncio.create_task(tasks._execute_claimed_run(run_id, "owner-A", request))
    try:
        await asyncio.wait_for(cancelled.wait(), 5)
        outcome = (await asyncio.gather(worker, return_exceptions=True))[0]
        assert isinstance(outcome, asyncio.CancelledError), outcome
    finally:
        if not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    row = await _row(run_id)
    assert (row["status"], row["worker_id"], row["attempts"]) == ("running", "owner-A", 1), row


def _now():  # 与队列同一只表：naive UTC
    from blank_api.services.agent.queue import utc_now

    return utc_now()

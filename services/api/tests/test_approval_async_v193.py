# -*- coding: utf-8 -*-
"""E13：审批恢复异步化。

审批接口不再同步 `await AgentRuntime.resume`，而是「校验 → 落 approval.decided →
把决定写进 run.request_payload → 入队 → 立即 202」。恢复由进程内 runner 或独立
worker 认领；决定是否已投递只看 `task.resumed` 事件，不拿 attempts 猜。

这里钉死验收表 1–6：

1. 慢恢复下审批接口立即返回；
2. 同一审批只恢复一次（重复点击幂等、后台恰好消费一次）；
3. 恢复 run 可被 worker 认领；
4. 已投递的决定崩溃后走 resume_crash，不重复投递；
5. 驳回 / 打回语义原样进入恢复；
6. 身份不符 / 过期不入队；
外加：入队失败保持可重试，启动补偿能把「已批准但没入队」补回来。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from blank_api import worker as worker_module
from blank_api.api.routes import tasks as task_routes
from blank_api.db import get_session_factory
from blank_api.models.run import AgentRun
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.runtime import AgentRuntime

from tests.conftest import _drain_active_runs
from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具)
    API,
    ENV_PATCH,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)


async def _waiting_task(env, workspace: Path) -> tuple[str, str]:
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["写入 .env 需人工审批"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 写入 .env"},
            {"content": "写入 .env", "tool_calls": [call("apply_patch", {"path": ".env", "patch": ENV_PATCH})]},
            {"content": ".env 已写入"},
            {"content": "已完成 .env 写入"},
        ]
    )
    # 用 /start 走真实的生产路径：它会写 agent_runs 行，审批恢复才有原始入参可复用。
    resp = await env.client.post(
        f"{API}/tasks/{task_id}/start", json={"provider_id": provider_id}
    )
    assert resp.status_code == 202, resp.text
    await _drain_active_runs()
    assert await _task_status(task_id) == "waiting_approval"
    return task_id, provider_id


async def _pending_card(env, task_id: str) -> dict:
    resp = await env.client.get(f"{API}/tasks/{task_id}/evidence/summary")
    assert resp.status_code == 200, resp.text
    cards = resp.json().get("pending_approvals") or []
    assert cards, "证据包里没有待审批卡"
    return cards[0]


async def _events(task_id: str, kind: str) -> list[AgentEvent]:
    async with get_session_factory()() as session:
        return list(
            (
                await session.execute(
                    select(AgentEvent)
                    .where(AgentEvent.task_id == task_id, AgentEvent.type == kind)
                    .order_by(AgentEvent.seq)
                )
            ).scalars()
        )


async def _task_status(task_id: str) -> str:
    async with get_session_factory()() as session:
        task = await session.get(Task, task_id)
        return str(task.status) if task is not None else ""


async def _run_row(task_id: str) -> AgentRun | None:
    async with get_session_factory()() as session:
        return (
            await session.execute(select(AgentRun).where(AgentRun.task_id == task_id))
        ).scalar_one_or_none()


def _stub_resume(monkeypatch, calls: list, *, status: str = "waiting_approval"):
    async def fake_resume(self, request, decision=True):
        calls.append(dict(decision) if isinstance(decision, dict) else decision)
        # 与 runtime._resume 同序：先写 task.resumed 再消费决定。补偿扫描靠它
        # 判断"这条决定已经投递过"。
        await EventRecorder(get_session_factory(), request.task_id).add(
            "task.resumed", "receive_task", {"decision": decision}
        )
        return {"task": {"status": status}, "pending_approvals": []}

    monkeypatch.setattr(AgentRuntime, "resume", fake_resume)


# ------------------------------------------------------------------ #
# 1：慢恢复不影响接口返回
# ------------------------------------------------------------------ #


async def test_slow_resume_still_returns_202_immediately(env, workspace, monkeypatch):  # noqa: F811
    task_id, _ = await _waiting_task(env, workspace)
    card = await _pending_card(env, task_id)
    calls: list = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_resume(self, request, decision=True):
        calls.append(dict(decision) if isinstance(decision, dict) else decision)
        started.set()
        await release.wait()
        return {"task": {"status": "waiting_approval"}, "pending_approvals": []}

    monkeypatch.setattr(AgentRuntime, "resume", slow_resume)

    began = time.monotonic()
    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={
            "approve": True,
            "approval_id": card["approval_id"],
            "tool_call_id": card["tool_call_id"],
            "arguments_hash": card["arguments_hash"],
        },
    )
    elapsed = time.monotonic() - began
    assert resp.status_code == 202, resp.text
    assert elapsed < 2.0, f"审批接口等了 {elapsed:.2f}s，恢复仍然是同步的"
    body = resp.json()
    assert body["status"] == "queued", body
    assert body["run_id"], body
    assert body["approval_id"] == card["approval_id"], body
    assert await _task_status(task_id) == "queued"

    # 后台真的开始恢复了；放行后收尾，避免把这条 run 留给下一个用例。
    await asyncio.wait_for(started.wait(), timeout=5)
    assert len(calls) == 1
    # 交给图的只有决定本身，内部记账键不能混进去。
    assert calls[0] == {"approve": True, "scope": "once"}, calls[0]
    release.set()
    await _drain_active_runs()


# ------------------------------------------------------------------ #
# 2：同一审批只恢复一次
# ------------------------------------------------------------------ #


async def test_repeat_submission_returns_already_decided_and_resumes_once(
    env, workspace, monkeypatch
):  # noqa: F811
    task_id, _ = await _waiting_task(env, workspace)
    card = await _pending_card(env, task_id)
    calls: list = []
    _stub_resume(monkeypatch, calls)
    body = {
        "approve": True,
        "approval_id": card["approval_id"],
        "tool_call_id": card["tool_call_id"],
        "arguments_hash": card["arguments_hash"],
    }

    first = await env.client.post(f"{API}/tasks/{task_id}/approval", json=body)
    assert first.status_code == 202, first.text
    second = await env.client.post(f"{API}/tasks/{task_id}/approval", json=body)
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "already_decided", second.text

    await _drain_active_runs()
    assert len(calls) == 1, f"同一张卡恢复了 {len(calls)} 次"
    decided = [
        event
        for event in await _events(task_id, "approval.decided")
        if (event.payload or {}).get("approval_id") == card["approval_id"]
    ]
    assert len(decided) == 1, decided


# ------------------------------------------------------------------ #
# 3：恢复 run 能被独立 worker 认领
# ------------------------------------------------------------------ #


async def test_worker_claims_the_queued_resume_run(env, workspace, monkeypatch):  # noqa: F811
    task_id, _ = await _waiting_task(env, workspace)
    card = await _pending_card(env, task_id)
    # 从这里开始把队列交给独立 worker：审批只入队，不启动进程内 runner。
    monkeypatch.setenv("BLANK_INPROCESS_RUNNER", "0")
    calls: list = []
    _stub_resume(monkeypatch, calls)

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={"approve": True, "approval_id": card["approval_id"]},
    )
    assert resp.status_code == 202, resp.text
    run = await _run_row(task_id)
    assert run is not None and run.status == "queued", run

    handled = await worker_module.run_once("test-worker")
    assert handled == 1
    assert len(calls) == 1, calls
    assert calls[0]["approve"] is True
    assert calls[0]["scope"] == "once"
    resumed = await _events(task_id, "task.resumed")
    assert resumed, "worker 认领后没有写 task.resumed"
    assert resumed[-1].seq > (await _events(task_id, "approval.decided"))[-1].seq
    run = await _run_row(task_id)
    assert run is not None and run.status == "waiting_approval", run

    # 再扫一轮：这条 run 已是终态，不会重复恢复。
    await worker_module.run_once("test-worker")
    assert len(calls) == 1


# ------------------------------------------------------------------ #
# 4：决定已投递后再崩溃 → resume_crash，不重投决定
# ------------------------------------------------------------------ #


async def test_delivered_decision_resumes_from_checkpoint_after_crash(
    env, workspace, monkeypatch
):  # noqa: F811
    task_id, _ = await _waiting_task(env, workspace)
    card = await _pending_card(env, task_id)
    monkeypatch.setenv("BLANK_INPROCESS_RUNNER", "0")
    calls: list = []
    resumed_calls: list = []

    async def forbidden_resume(self, request, decision=True):
        raise AssertionError("决定已经投递过，不应该再 resume 一次")

    async def fake_resume_crash(self, request):
        resumed_calls.append(request.resume_decision)
        return {"task": {"status": "succeeded"}, "pending_approvals": []}

    monkeypatch.setattr(AgentRuntime, "resume", forbidden_resume)
    monkeypatch.setattr(AgentRuntime, "resume_crash", fake_resume_crash)

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={"approve": True, "approval_id": card["approval_id"]},
    )
    assert resp.status_code == 202, resp.text
    # 模拟「运行时的 task.resumed 已写、图还没跑完进程就没了」。
    await EventRecorder(get_session_factory(), task_id).add(
        "task.resumed", "receive_task", {"crash_recovery": False, "test": True}
    )

    handled = await worker_module.run_once("test-worker")
    assert handled == 1
    assert len(resumed_calls) == 1, resumed_calls
    assert calls == []
    run = await _run_row(task_id)
    assert run is not None and run.status == "succeeded", run


# ------------------------------------------------------------------ #
# 5：驳回 / 打回语义原样进入恢复
# ------------------------------------------------------------------ #


async def test_deny_carries_feedback_and_send_back_into_the_resume(
    env, workspace, monkeypatch
):  # noqa: F811
    task_id, _ = await _waiting_task(env, workspace)
    card = await _pending_card(env, task_id)
    calls: list = []
    _stub_resume(monkeypatch, calls, status="succeeded")

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={
            "approve": False,
            "scope": "once",
            "approval_id": card["approval_id"],
            "feedback": "先把测试方案补上",
            "send_back_step_id": "step-2",
        },
    )
    assert resp.status_code == 202, resp.text
    await _drain_active_runs()
    assert calls == [
        {
            "approve": False,
            "scope": "once",
            "feedback": "先把测试方案补上",
            "step_id": "step-2",
        }
    ], calls
    decided = (await _events(task_id, "approval.decided"))[-1].payload or {}
    # 补偿扫描要从事件重建决定：这两个字段不能只活在这次请求里。
    assert decided["feedback"] == "先把测试方案补上", decided
    assert decided["step_id"] == "step-2", decided


# ------------------------------------------------------------------ #
# 6：身份不符 / 过期 → 409，且不产生新 run
# ------------------------------------------------------------------ #


async def test_mismatched_identity_is_rejected_without_enqueueing(env, workspace):  # noqa: F811
    task_id, _ = await _waiting_task(env, workspace)
    card = await _pending_card(env, task_id)
    before = await _run_row(task_id)

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={"approve": True, "approval_id": "apr-doesnotexist"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "approval_mismatch", resp.text
    assert await _events(task_id, "approval.decided") == []
    assert await _task_status(task_id) == "waiting_approval"
    after = await _run_row(task_id)
    assert before is not None and after is not None
    assert after.status == before.status == "waiting_approval"


async def test_expired_card_is_rejected_without_enqueueing(env, workspace):  # noqa: F811
    task_id, _ = await _waiting_task(env, workspace)
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(AgentEvent).where(
                    AgentEvent.task_id == task_id,
                    AgentEvent.type == "approval.required",
                )
            )
        ).scalars().all()
        assert rows, "没有 approval.required 事件"
        rows[-1].payload = {**rows[-1].payload, "expires_at": "2020-01-01T00:00:00+00:00"}
        await session.commit()

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval", json={"approve": True}
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "approval_expired", resp.text
    assert await _events(task_id, "approval.decided") == []
    assert await _task_status(task_id) == "waiting_approval"


# ------------------------------------------------------------------ #
# T3：入队失败保持可重试 + 启动补偿
# ------------------------------------------------------------------ #


async def test_enqueue_failure_stays_waiting_and_startup_scan_recovers(
    env, workspace, monkeypatch
):  # noqa: F811
    task_id, _ = await _waiting_task(env, workspace)
    card = await _pending_card(env, task_id)
    calls: list = []
    _stub_resume(monkeypatch, calls)

    from blank_api.services.orchestration import runner as orchestration_runner

    real_enqueue = orchestration_runner.enqueue_run

    async def broken_enqueue(*args, **kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr(orchestration_runner, "enqueue_run", broken_enqueue)
    try:
        with pytest.raises(RuntimeError):
            await env.client.post(
                f"{API}/tasks/{task_id}/approval",
                json={"approve": True, "approval_id": card["approval_id"]},
            )
    finally:
        monkeypatch.setattr(orchestration_runner, "enqueue_run", real_enqueue)

    # 先落事件、后入队：决定留了痕，任务仍停在等待审批，用户可重试。
    decided = await _events(task_id, "approval.decided")
    assert len(decided) == 1
    assert await _task_status(task_id) == "waiting_approval"
    assert await _events(task_id, "task.resumed") == []

    # 进程重启后的补偿扫描按事件账把它补回来。
    recovered = await task_routes.recover_orphan_approvals()
    assert recovered == 1
    await _drain_active_runs()
    assert len(calls) == 1, calls
    assert calls[0]["approve"] is True
    assert (await _events(task_id, "task.resumed")), "补偿后没有恢复"

    # 已经投递过：再扫一次不重复恢复。
    await task_routes.recover_orphan_approvals()
    assert len(calls) == 1


async def test_compensation_skips_a_newer_card(env, workspace, monkeypatch):  # noqa: F811
    """决定之后又出现了新的审批卡：旧决定不能拿来顶新卡。"""
    task_id, _ = await _waiting_task(env, workspace)
    card = await _pending_card(env, task_id)
    calls: list = []
    _stub_resume(monkeypatch, calls)

    await EventRecorder(get_session_factory(), task_id).add(
        "approval.decided",
        "execute_action",
        {
            "approve": True,
            "scope": "once",
            "approval_id": card["approval_id"],
            "arguments_hash": card["arguments_hash"],
            "tool_name": card["tool"],
        },
    )
    # 决定之后又出现一张新卡（例如恢复跑到了下一个断点）：这条旧决定已经作废。
    await EventRecorder(get_session_factory(), task_id).add(
        "approval.required",
        "execute_action",
        {
            "approve": False,
            "tool": "run_shell",
            "arguments": {"command": "echo next"},
            "arguments_hash": "next-card",
            "approval_id": "apr-next-card",
            "tool_call_id": "call-next-card",
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
    )

    assert await task_routes.recover_orphan_approvals() == 0
    await _drain_active_runs()
    assert calls == []

# -*- coding: utf-8 -*-
"""最终阻塞任务单 §3.4 / §4.3：把「实现里有、但没被测试钉住」的硬要求补齐。

先复现再改：这几条不是补文档，是拿真实的审批端点与队列层跑一遍，确认当前
HEAD 到底守不守得住。任何一条红了就是产品缺陷，不能靠改断言糊过去。

§4.3 缺的两项：
  - 错误 tool_call_id 必须 409（原来只测了 approval_id / arguments_hash / tool_name）
  - 多个 pending 审批不能串台（原来所有用例都只有一张卡）
§3.4 缺的两项：
  - 一个 task 不能重复创建多个 run
  - API/新 worker 起来后 queued run 能被认领（进程重启恢复的队列层那一半）
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from blank_api.api.errors import ApiError
from blank_api.db import get_session_factory
from blank_api.models.run import AgentRun
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.queue import claim_run, enqueue_run, finish_run

from tests.test_agent_v04 import API, env  # noqa: F401 (pytest fixture)
from tests.test_approval_identity_v101 import _pending_card, _waiting_task


# --------------------------------------------------------------------------- #
# §4.3 审批唯一身份
# --------------------------------------------------------------------------- #


async def test_wrong_tool_call_id_is_rejected_with_409(env, workspace):  # noqa: F811
    """四元匹配的第三元：拿别的调用的 tool_call_id 来批这张卡，必须 409。"""
    task_id = await _waiting_task(env, workspace)
    card = await _pending_card(env, task_id)

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={
            "approve": True,
            "approval_id": card["approval_id"],
            "tool_call_id": "call-belongs-to-another-card",
            "arguments_hash": card["arguments_hash"],
            "tool_name": card["tool"],
        },
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()["error"]
    assert body["code"] == "approval_mismatch", body
    assert "审批对象已变化" in body["message"], body
    assert not (workspace / ".env").exists(), "错卡被放行，文件不该落地"


async def test_two_pending_cards_never_cross_wire(env, workspace):  # noqa: F811
    """两张卡并存时按「最近一次 approval.required」定址，旧卡身份不得串到新卡。

    老实现是「取任务里第一张 pending 卡」：第二张卡来了之后点第一张，会静默把
    第二张批掉。这里直接补一条更新的中断事件，再用旧卡全身份提交，必须 409。
    """
    task_id = await _waiting_task(env, workspace)
    card = await _pending_card(env, task_id)

    await EventRecorder(get_session_factory(), task_id).add(
        "approval.required",
        "execute_action",
        {
            "approval_id": "apr-second-card",
            "tool_call_id": "call-second",
            "tool": "run_shell",
            "tool_name": "run_shell",
            "arguments_hash": "hash-second",
            "resource_path": "package.json",
        },
    )

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={
            "approve": True,
            "approval_id": card["approval_id"],
            "tool_call_id": card["tool_call_id"],
            "arguments_hash": card["arguments_hash"],
            "tool_name": card["tool"],
        },
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "approval_mismatch", resp.text
    assert not (workspace / ".env").exists(), "旧卡串到了新卡上"


# --------------------------------------------------------------------------- #
# §3.4 后端接管：一个 task 一条 run + queued 可被新 worker 认领
# --------------------------------------------------------------------------- #


async def test_a_task_never_owns_two_runs(env, workspace):  # noqa: F811
    """同一 task 二次入队必须复用/拒绝，绝不能多插一行 AgentRun。"""
    task_id = f"task-final-{uuid4().hex[:12]}"
    async with get_session_factory()() as session:
        run = await enqueue_run(session, task_id, {"from": "first"})
        run_id = run.id
        with pytest.raises(ApiError) as caught:
            await enqueue_run(session, task_id, {"from": "second"})
        assert caught.value.code == "task_running", caught.value
        rows = (
            await session.execute(select(AgentRun).where(AgentRun.task_id == task_id))
        ).scalars().all()
        assert len(rows) == 1, rows
        assert rows[0].id == run_id, "第二次入队换了新 run"
        await session.rollback()


async def test_queued_run_is_claimable_by_a_fresh_worker(env, workspace):  # noqa: F811
    """进程重启后 queued 恢复的队列层事实：新 worker 能认领、能写终态。

    真机「重启 API 后任务自己接着跑」另见验收报告；这里只钉住队列行不会
    因为旧进程消失而永远卡在 queued，且旧 worker 不能给新 owner 写终态。
    """
    task_id = f"task-final-{uuid4().hex[:12]}"
    async with get_session_factory()() as session:
        run = await enqueue_run(session, task_id, {"from": "before-restart"})
        run_id = run.id
        await session.commit()

    async with get_session_factory()() as session:
        assert await claim_run(session, run_id, "worker-after-restart") is True
        row = await session.get(AgentRun, run_id)
        assert row.status == "running"
        assert row.worker_id == "worker-after-restart"
        assert await finish_run(session, run_id, "failed", "worker-gone") is False
        assert await finish_run(session, run_id, "succeeded", "worker-after-restart") is True

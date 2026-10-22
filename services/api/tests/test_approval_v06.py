import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.checkpoint import AgentCheckpoint
from blank_api.models.task import AgentEvent, ToolCall
from blank_api.services.agent.evidence import approval_is_expired

from tests.test_agent_v04 import (
    API,
    ENV_PATCH,
    MockModel,
    call,
    create_project,
    create_provider,
    create_task,
    env,  # noqa: F401  (复用阶段3 的夹具)
)


async def _waiting_task(env, workspace: Path):  # noqa: F811
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
    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run",
        json={"provider_id": provider_id},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "waiting_approval"
    return task_id, provider_id


async def test_approval_endpoint_continues_from_the_same_node(env, workspace: Path):  # noqa: F811
    task_id, provider_id = await _waiting_task(env, workspace)
    assert not (workspace / ".env").exists()

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval", json={"approve": True, "provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (workspace / ".env").read_text(encoding="utf-8") == "TOKEN=plain"
    assert body["task"]["status"] != "waiting_approval"
    assert body["evidence"]["pending_approvals"] == []

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    types = [event["type"] for event in events]
    assert "approval.decided" in types and "approval.granted" in types, types
    granted = [event for event in events if event["type"] == "approval.granted"]
    assert [event["payload"]["tool"] for event in granted] == ["apply_patch"], granted
    # 审批接口不接受 approved_tools：预先放行会让图中断被跳过，前端就看不到 granted 轨迹。
    assert not any("approved_tools" in event["payload"] for event in events if event["type"] == "approval.decided")

    async with get_session_factory()() as session:
        threads = set(
            (
                await session.execute(select(AgentCheckpoint.thread_id).where(AgentCheckpoint.task_id == task_id))
            ).scalars()
        )
    assert len(threads) == 1, f"审批恢复必须复用同一 thread：{threads}"


async def test_second_approval_call_is_rejected_as_idempotent(env, workspace: Path):  # noqa: F811
    task_id, provider_id = await _waiting_task(env, workspace)
    first = await env.client.post(
        f"{API}/tasks/{task_id}/approval", json={"approve": True, "provider_id": provider_id}
    )
    assert first.status_code == 200, first.text

    second = await env.client.post(
        f"{API}/tasks/{task_id}/approval", json={"approve": True, "provider_id": provider_id}
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "task_not_waiting_approval"
    assert (workspace / ".env").read_text(encoding="utf-8") == "TOKEN=plain"


async def test_rejection_leaves_the_step_unexecuted_and_pauses(env, workspace: Path):  # noqa: F811
    task_id, provider_id = await _waiting_task(env, workspace)

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval", json={"approve": False, "provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert not (workspace / ".env").exists(), "驳回后不得写入文件"
    assert body["task"]["status"] == "paused"
    assert "驳回" in body["task"]["failure_reason"]

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    types = [event["type"] for event in events]
    assert "approval.denied" in types


async def test_restarted_runtime_resumes_a_waiting_task(env, workspace: Path):  # noqa: F811
    task_id, provider_id = await _waiting_task(env, workspace)
    # 每个 HTTP 请求都新建 AgentRuntime，等价于进程重启后从检查点恢复
    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval", json={"approve": True, "provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text
    assert (workspace / ".env").exists()


async def test_auto_fix_stops_after_three_repair_rounds(env, workspace: Path):  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["测试必须通过"], max_steps=20)
    steps = [{"content": "执行计划：\n1. 反复修补"}]
    for _ in range(12):
        steps.append(
            {
                "content": "再次修补",
                "tool_calls": [call("run_tests", {"command": "python -c 'raise SystemExit(1)'"})],
            }
        )
    env.mock.reset(steps)

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id, "approve_high_risk": True}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task"]["status"] == "failed"
    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    validations = [item for item in events if item["type"] == "validation.completed"]
    assert 0 < len(validations) <= 4, f"自动修复最多 3 轮，实际验证 {len(validations)} 次"


async def _retimed_approval(task_id: str, expires_at: str) -> None:
    """把审批中断事件里的 TTL 改到过去。读侧（`/evidence`）和写侧（提交审批时拒绝）
    都从同一条事件取值，所以一次改写就能同时问两边，看它们还给不给同一个答案。
    """
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(AgentEvent).where(AgentEvent.task_id == task_id).order_by(AgentEvent.seq.desc())
            )
        ).scalars().all()
        for row in rows:
            if row.type in ("approval.required", "approval.requested") and isinstance(row.payload, dict):
                row.payload = {**row.payload, "expires_at": expires_at}
                await session.commit()
                return
    raise AssertionError("没找到审批中断事件：测试装置本身失效，判定不可能成立")


async def test_expired_approval_reads_and_writes_the_same_answer(env, workspace: Path):  # noqa: F811
    task_id, provider_id = await _waiting_task(env, workspace)

    fresh = (await env.client.get(f"{API}/tasks/{task_id}/evidence")).json()["pending_approvals"]
    assert fresh and fresh[0]["approval_state"] == "待审批" and fresh[0]["expired"] is False, fresh

    await _retimed_approval(task_id, "2020-01-01T00:00:00+00:00")

    # 修好之前这里是「待审批」：界面照它渲染一个批准按钮，点下去 409。
    stale = (await env.client.get(f"{API}/tasks/{task_id}/evidence")).json()["pending_approvals"]
    assert stale[0]["approval_state"] == "已过期" and stale[0]["expired"] is True, stale[0]

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval", json={"approve": True, "provider_id": provider_id}
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "approval_expired", resp.text
    assert not (workspace / ".env").exists(), "过期审批被点下去也不该写文件"


async def test_toolcall_shaped_approval_ages_out_too(env, workspace: Path):  # noqa: F811
    """分支 1（证据里带 ToolCall 行的待审批）同样要跟着 TTL 变。

    今天的真实流程都在网关之前 interrupt，库里不留 `awaiting_approval` 的 ToolCall 行，
    所以这条支路只有事件兜底那一支被端到端测到；给这一支打变异测试照样绿。
    这里直接种一行把分支 1 引出来，别再让它是没人的看着的路。
    """
    task_id, provider_id = await _waiting_task(env, workspace)
    async with get_session_factory()() as session:
        session.add(
            ToolCall(
                id="call-branch-1",
                task_id=task_id,
                name="apply_patch",
                arguments={"path": ".env"},
                result_summary="写入 .env 需人工审批",
                status="awaiting_approval",
                risk_level="high",
            )
        )
        await session.commit()

    fresh = (await env.client.get(f"{API}/tasks/{task_id}/evidence")).json()["pending_approvals"]
    assert [item["tool_call_id"] for item in fresh] == ["call-branch-1"], fresh
    assert fresh[0]["approval_state"] == "待审批" and fresh[0]["expired"] is False, fresh[0]

    await _retimed_approval(task_id, "2020-01-01T00:00:00+00:00")

    stale = (await env.client.get(f"{API}/tasks/{task_id}/evidence")).json()["pending_approvals"]
    assert stale[0]["approval_state"] == "已过期" and stale[0]["expired"] is True, stale[0]
    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval", json={"approve": True, "provider_id": provider_id}
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "approval_expired", resp.text


def test_expiry_predicate_covers_the_shapes_it_meets():
    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    # 没时间戳＝不按 TTL 拦；读不懂＝点不成，宁可说已过期，不给一个必然失败的按钮。
    assert approval_is_expired(None, now=now) is False
    assert approval_is_expired("", now=now) is False
    assert approval_is_expired("2026-10-22T12:04:00+00:00", now=now) is False
    assert approval_is_expired("2026-10-22T11:59:00Z", now=now) is True
    assert approval_is_expired("2026-10-22T11:59:00", now=now) is True
    assert approval_is_expired("not-a-time", now=now) is True

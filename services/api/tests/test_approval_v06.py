import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.checkpoint import AgentCheckpoint

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

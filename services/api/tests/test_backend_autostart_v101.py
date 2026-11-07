# -*- coding: utf-8 -*-
"""v0.10.1 §2/§4：后端真正接管自动执行 + 权限快照链。

要证明五件事：

1. **auto_start 由后端消费**。前端只发一次 POST turns，任务就地入队并跑完；
   没有任何一次 /start 调用。窗口关了任务也照跑——这条链路的后端半边在这里。
2. **run 有生命周期事件**。run.created（入队）与 run.started（开跑）分开记，
   「排队没人跑」和「跑起来后失败」在事件流里必须可区分。
3. **用户的显式意愿压过监督者判定**。manual 模式下 auto_start=True 也启动；
   auto_start=False 永远不自动启动；闲聊（answer 意图）不会被当任务跑。
4. **派发失败有名字**。启动失败记 task.dispatch_failed（含原因与任务现状），
   响应仍是 201，任务留在可重试的状态，绝不静默也不停在 planned。
5. **权限快照链**。发送栏选的档在创建时刻写进下发快照并随任务固定；
   之后改全局默认档，不影响已经在跑的这一轮。
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.task import AgentEvent, Task
from blank_api.services.settings import SettingsStore

from tests.test_agent_v04 import (  # noqa: F401  (复用脚本化假模型夹具)
    API,
    create_project,
    env,
)

API_ROOT = API


async def _make_provider(env) -> str:
    resp = await env.client.post(
        f"{API}/providers",
        json={
            "name": f"vendor-{uuid.uuid4().hex[:6]}",
            "kind": "openai_compatible",
            "base_url": "https://mock.example/v1",
            "model": "mock-coder",
            "api_key": "sk-mocksecret123456",
            "supports_tools": True,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _thread(env, project_id: str) -> str:
    resp = await env.client.post(f"{API}/projects/{project_id}/threads", json={"title": "自主执行"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _turn(env, thread_id: str, **extra) -> dict:
    text = extra.pop("text", "把登录页的按钮改成蓝色")
    resp = await env.client.post(
        f"{API}/threads/{thread_id}/turns",
        json={"title": text[:40], "goal": text, **extra},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _task_status(task_id: str) -> str:
    async with get_session_factory()() as session:
        task = await session.get(Task, task_id)
        return str(task.status or "") if task is not None else ""


async def _wait_terminal(task_id: str, seconds: float = 15.0) -> str:
    deadline = asyncio.get_event_loop().time() + seconds
    status = ""
    while asyncio.get_event_loop().time() < deadline:
        status = await _task_status(task_id)
        if status in ("succeeded", "failed", "cancelled"):
            return status
        await asyncio.sleep(0.1)
    return status


async def _events(task_id: str) -> list[AgentEvent]:
    async with get_session_factory()() as session:
        return list(
            (
                await session.execute(
                    select(AgentEvent)
                    .where(AgentEvent.task_id == task_id)
                    .order_by(AgentEvent.seq)
                )
            ).scalars()
        )


async def _event_types(task_id: str) -> list[str]:
    return [event.type for event in await _events(task_id)]


# ------------------------------------------------------------------ #
# 1+2：后端就地启动；run 生命周期事件齐全
# ------------------------------------------------------------------ #


async def test_backend_starts_and_finishes_without_any_frontend_call(env, workspace):  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await _make_provider(env)
    thread_id = await _thread(env, project_id)

    task = await _turn(env, thread_id, provider_id=provider_id)
    # 后端必须就地启动：响应里的状态不再是 draft，前端没有第二次调用。
    assert task["status"] in ("queued", "running", "succeeded"), task
    assert task["goal_spec"]["dispatch"]["auto_start"] is True

    final = await _wait_terminal(task["id"])
    assert final == "succeeded", f"任务没有跑完：{final}"

    types = await _event_types(task["id"])
    assert "run.created" in types, types
    assert "run.started" in types, types
    assert "task.auto_started" in types, types
    assert "task.dispatch_failed" not in types, types
    # 事件里标明启动来源是后端，不是前端替用户点的「运行」。
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(AgentEvent).where(
                    AgentEvent.task_id == task["id"], AgentEvent.type == "task.auto_started"
                )
            )
        ).scalar_one()
    assert row.payload["source"] == "backend"


async def test_queued_run_records_created_and_started_separately(env, workspace):  # noqa: F811
    """run.created 必须先于 run.started：入队和开跑是两件事。"""
    project_id = await create_project(env, workspace)
    provider_id = await _make_provider(env)
    thread_id = await _thread(env, project_id)

    task = await _turn(env, thread_id, provider_id=provider_id)
    await _wait_terminal(task["id"])

    events = await _events(task["id"])
    created_seq = next(e.seq for e in events if e.type == "run.created")
    started_seq = next(e.seq for e in events if e.type == "run.started")
    assert created_seq < started_seq


# ------------------------------------------------------------------ #
# 3：显式意愿与监督者判定的边界
# ------------------------------------------------------------------ #


async def test_explicit_auto_start_true_overrides_manual_mode(env, workspace, tmp_path, monkeypatch):  # noqa: F811
    # 改全局设置必须用本用例独立的 settings 文件：conftest 预置的共享文件只有
    # safety 段，guardian 靠 BLANK_GUARDIAN 环境变量生效；一旦 PUT /settings/agent
    # 把 normalize 后的全部段（含 guardian 默认 off）物化进去，后面的 guardian
    # 用例就会被文件里的 off 遮蔽环境变量（guardian.from_settings 的回落规则）。
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    project_id = await create_project(env, workspace)
    provider_id = await _make_provider(env)
    thread_id = await _thread(env, project_id)
    resp = await env.client.put(
        f"{API}/settings/agent", json={"background": {"automation_mode": "manual"}}
    )
    assert resp.status_code == 200, resp.text
    try:
        task = await _turn(env, thread_id, provider_id=provider_id, auto_start=True)
        # manual 模式的监督者判定是 queue；用户显式 True 要能越过它。
        assert task["goal_spec"]["dispatch"]["auto_start"] is True
        final = await _wait_terminal(task["id"])
        assert final == "succeeded", final
    finally:
        await env.client.put(
            f"{API}/settings/agent", json={"background": {"automation_mode": "assist"}}
        )


async def test_explicit_auto_start_false_never_starts(env, workspace):  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await _make_provider(env)
    thread_id = await _thread(env, project_id)

    task = await _turn(
        env, thread_id, provider_id=provider_id, auto_start=False
    )
    assert task["status"] == "draft", task
    assert task["goal_spec"]["dispatch"]["auto_start"] is False
    types = await _event_types(task["id"])
    assert "run.created" not in types and "task.auto_started" not in types


async def test_small_talk_never_becomes_a_running_task(env, workspace):  # noqa: F811
    """answer 意图（寒暄/概念）不进执行：监督者判定 blocked，后端也不启动。"""
    project_id = await create_project(env, workspace)
    provider_id = await _make_provider(env)
    thread_id = await _thread(env, project_id)

    task = await _turn(env, thread_id, provider_id=provider_id, text="你好")
    assert task["status"] == "draft", task
    types = await _event_types(task["id"])
    assert "run.created" not in types


# ------------------------------------------------------------------ #
# 4：派发失败有名字
# ------------------------------------------------------------------ #


async def test_dispatch_failure_records_event_and_still_returns_201(env, workspace):  # noqa: F811
    """没配模型供应商：判定能过（immediate），启动必失败。必须有事件、有现状、不 500。"""
    project_id = await create_project(env, workspace)
    thread_id = await _thread(env, project_id)

    task = await _turn(env, thread_id)  # 故意不带 provider_id，库里也没有任何供应商
    assert task["status"] == "draft", task

    events = await _events(task["id"])
    failures = [e for e in events if e.type == "task.dispatch_failed"]
    assert failures, [e.type for e in events]
    payload = failures[-1].payload
    assert "供应商" in payload["reason"] or payload["code"], payload
    assert payload["task_status"] == "draft", payload
    # 任务没有被人偷偷判死：用户还能点「开始」重试。
    assert await _task_status(task["id"]) == "draft"


# ------------------------------------------------------------------ #
# 5：权限快照链
# ------------------------------------------------------------------ #


async def test_permission_mode_snapshot_beats_later_global_change(env, workspace, tmp_path, monkeypatch):  # noqa: F811
    """发送栏选了 plan：任务创建后把全局默认改成 bypass，这一轮仍按 plan 执行。"""
    # 同上：改全局设置用独立 settings 文件，不物化共享 fixture 的 guardian 段。
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    store = SettingsStore()
    original = dict(store.read().get("permissions") or {})
    project_id = await create_project(env, workspace)
    provider_id = await _make_provider(env)
    thread_id = await _thread(env, project_id)
    try:
        resp = await env.client.put(
            f"{API}/settings/agent", json={"permissions": {"default_mode": "bypass"}}
        )
        assert resp.status_code == 200, resp.text

        task = await _turn(
            env, thread_id, provider_id=provider_id, permission_mode="plan"
        )
        assert task["goal_spec"]["dispatch"]["permission_mode"] == "plan"

        final = await _wait_terminal(task["id"])
        assert final == "succeeded", final

        started = [
            e for e in await _events(task["id"]) if e.type == "task.started"
        ]
        assert started, "任务没有 task.started 事件"
        # 真实生效的档位是任务快照里的 plan，不是后来改的全局 bypass。
        assert started[-1].payload["permission_mode"] == "plan", started[-1].payload.get("permission_mode")
    finally:
        await env.client.put(f"{API}/settings/agent", json={"permissions": dict(original)})


# ------------------------------------------------------------------ #
# 6：独立任务路径（POST /projects/{id}/tasks）同样由后端就地启动
# ------------------------------------------------------------------ #


async def _create_task(env, project_id: str, **extra) -> dict:
    resp = await env.client.post(
        f"{API}/projects/{project_id}/tasks",
        json={"title": "独立任务", "goal": "把登录页的按钮改成蓝色", **extra},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_create_task_auto_start_runs_to_completion(env, workspace):  # noqa: F811
    """auto_start=True：create_task 就地启动跑完，前端不需要补一次 /run。"""
    project_id = await create_project(env, workspace)
    provider_id = await _make_provider(env)

    task = await _create_task(env, project_id, provider_id=provider_id, auto_start=True)
    assert task["status"] in ("queued", "running", "succeeded"), task

    final = await _wait_terminal(task["id"])
    assert final == "succeeded", final

    types = await _event_types(task["id"])
    assert "task.auto_started" in types, types
    assert "run.created" in types and "run.started" in types, types
    assert "task.dispatch_failed" not in types, types
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(AgentEvent).where(
                    AgentEvent.task_id == task["id"], AgentEvent.type == "task.auto_started"
                )
            )
        ).scalar_one()
    assert row.payload["source"] == "backend"


async def test_create_task_auto_start_false_stays_draft(env, workspace):  # noqa: F811
    """默认 auto_start=False：行为与从前一致，留在 draft 等手动运行。"""
    project_id = await create_project(env, workspace)
    provider_id = await _make_provider(env)

    task = await _create_task(env, project_id, provider_id=provider_id)
    assert task["status"] == "draft", task
    types = await _event_types(task["id"])
    assert "run.created" not in types and "task.auto_started" not in types


async def test_create_task_permission_mode_pinned_in_snapshot(env, workspace):  # noqa: F811
    """创建时选的 plan 钉进快照：全局默认之后改成 bypass 也不影响这一单。"""
    store = SettingsStore()
    original = dict(store.read().get("permissions") or {})
    project_id = await create_project(env, workspace)
    provider_id = await _make_provider(env)
    try:
        resp = await env.client.put(
            f"{API}/settings/agent", json={"permissions": {"default_mode": "bypass"}}
        )
        assert resp.status_code == 200, resp.text

        task = await _create_task(
            env, project_id, provider_id=provider_id, auto_start=True, permission_mode="plan"
        )
        assert task["goal_spec"]["dispatch"]["permission_mode"] == "plan"

        final = await _wait_terminal(task["id"])
        assert final == "succeeded", final

        started = [e for e in await _events(task["id"]) if e.type == "task.started"]
        assert started, "任务没有 task.started 事件"
        assert started[-1].payload["permission_mode"] == "plan", started[-1].payload.get(
            "permission_mode"
        )
    finally:
        await env.client.put(f"{API}/settings/agent", json={"permissions": dict(original)})

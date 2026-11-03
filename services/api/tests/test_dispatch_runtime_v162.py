# -*- coding: utf-8 -*-
"""v162 派发快照的执行侧效力：写库、只读闸门、步数预算，以及开关关闭时的原样。

这里钉的是「快照不是一份记录，而是真的在执行时生效」：
- 开关关着 → 不写快照、不发事件，行为与从前一致；
- 开关开着 → 创建任务即落快照，run 按快照收紧步数与只读；
- 库里的快照被改成「日常场景可写」也放不开权限（数据不能提权）。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.task import AgentEvent, Task, ToolCall
from blank_api.services.agent.dispatch import route

from tests.test_agent_v04 import (  # noqa: F401  (复用端到端夹具)
    API,
    PATCH,
    call,
    create_project,
    create_provider,
    env,
)

FLAG = "BLANK_FEATURE_SUPER_AGENT_DISPATCH"


async def _task_row(task_id: str) -> Task:
    async with get_session_factory()() as session:
        row = await session.get(Task, task_id)
        assert row is not None
        return row


async def _events(task_id: str, kind: str) -> list[dict]:
    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(AgentEvent)
                    .where(AgentEvent.task_id == task_id, AgentEvent.type == kind)
                    .order_by(AgentEvent.seq)
                )
            ).scalars()
        )
    return [dict(row.payload or {}) for row in rows]


async def _create(env, project_id: str, **kwargs) -> dict:
    resp = await env.client.post(
        f"{API}/projects/{project_id}/tasks",
        json={
            "title": kwargs.get("title", "给 main.py 增加输出"),
            "goal": kwargs.get("goal", "修改 main.py 并运行测试"),
            "scenario": kwargs.get("scenario", "dev"),
            "acceptance_criteria": kwargs.get("criteria", []),
            "max_steps": kwargs.get("max_steps", 6),
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── 落库 ────────────────────────────────────────────────────────────────────


async def test_flag_off_writes_no_snapshot_and_no_event(env, workspace):
    project_id = await create_project(env, workspace)
    task = await _create(env, project_id)
    assert task["dispatch_snapshot"] == {}
    assert await _events(task["id"], "dispatch.decided") == []


async def test_flag_on_writes_snapshot_and_decision_event(env, workspace, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    project_id = await create_project(env, workspace)
    task = await _create(env, project_id, max_steps=6)
    snapshot = task["dispatch_snapshot"]
    assert snapshot["snapshot_id"].startswith("dsp-")
    assert snapshot["intent"] == "execute"
    assert snapshot["tool_policy"] == "workspace_edit"
    # 快照步数不超过任务上限：两者取小，快照不能反过来放宽。
    assert snapshot["budget"]["max_steps"] == 6
    events = await _events(task["id"], "dispatch.decided")
    assert len(events) == 1
    assert events[0]["snapshot_id"] == snapshot["snapshot_id"]
    assert events[0]["why"]


async def test_chitchat_snapshot_is_single_step(env, workspace, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    project_id = await create_project(env, workspace)
    task = await _create(env, project_id, title="你好", goal="你好", max_steps=50)
    snapshot = task["dispatch_snapshot"]
    assert snapshot["intent"] == "answer"
    assert snapshot["budget"]["max_steps"] == 1
    assert snapshot["notify_policy"] == "silent"


async def test_daily_task_snapshot_is_read_only(env, workspace, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    project_id = await create_project(env, workspace)
    task = await _create(env, project_id, scenario="daily", goal="帮我改一下这句话")
    assert task["dispatch_snapshot"]["tool_policy"] == "read_only"


# ── 执行侧效力 ──────────────────────────────────────────────────────────────


async def _force_snapshot(task_id: str, *, readonly: bool, max_steps: int) -> None:
    """把库里的快照改成指定形态，模拟「当时路由判成只读」。"""
    snapshot = route("修改 main.py 并运行测试", scenario="dev", max_steps=max_steps)
    stored = snapshot.tightened(readonly=readonly)
    async with get_session_factory()() as session:
        row = await session.get(Task, task_id)
        assert row is not None
        row.dispatch_snapshot = stored.as_dict()
        await session.commit()


async def test_snapshot_readonly_blocks_writes_at_execution(env, workspace, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task = await _create(env, project_id)
    await _force_snapshot(task["id"], readonly=True, max_steps=6)
    env.mock.reset(
        [
            {"content": "", "tool_calls": [call("apply_patch", {"patch": PATCH})]},
            {"content": "这次只做调研，没改文件。"},
        ]
    )
    resp = await env.client.post(
        f"{API}/tasks/{task['id']}/run", json={"provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text
    started = (await _events(task["id"], "task.started"))[-1]
    assert started["readonly"] is True
    assert started["dispatch"]["snapshot_id"].startswith("dsp-")
    # 真的没写：补丁被拒，文件内容还是原样。
    assert (workspace / "main.py").read_text(encoding="utf-8") == "print('hi')\n"
    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(ToolCall).where(ToolCall.task_id == task["id"])
                )
            ).scalars()
        )
    patch_calls = [row for row in rows if row.name == "apply_patch"]
    assert patch_calls, "模型请求过 apply_patch，应该留下一条被拒记录"
    assert patch_calls[-1].status in ("denied", "blocked")


async def test_snapshot_readonly_is_not_the_default_path(env, workspace, monkeypatch):
    """对照组：同一句话，快照没判只读时补丁真的落地。

    没有这一组，上面那条「文件没变」可能只是因为补丁格式本身有问题。
    """
    monkeypatch.setenv(FLAG, "1")
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task = await _create(env, project_id)
    await _force_snapshot(task["id"], readonly=False, max_steps=6)
    env.mock.reset(
        [
            {"content": "", "tool_calls": [call("apply_patch", {"patch": PATCH})]},
            {"content": "改好了"},
            {"content": "改好了"},
        ]
    )
    resp = await env.client.post(
        f"{API}/tasks/{task['id']}/run", json={"provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text
    started = (await _events(task["id"], "task.started"))[-1]
    assert started["readonly"] is False
    assert "patched" in (workspace / "main.py").read_text(encoding="utf-8")


async def test_run_max_steps_comes_from_snapshot_budget(env, workspace, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task = await _create(env, project_id, title="你好", goal="你好", max_steps=50)
    env.mock.reset([{"content": "你好，有什么要做的？"}])
    resp = await env.client.post(
        f"{API}/tasks/{task['id']}/run", json={"provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text
    started = (await _events(task["id"], "task.started"))[-1]
    # 问答类快照只给 1 步：任务上限 50 不能把它放大回来。
    assert started["max_steps"] == 1


async def test_stored_snapshot_cannot_widen_scenario_permission(env, workspace, monkeypatch):
    """库里被改成「日常场景 + 可写工作区」的快照，执行时必须还是只读。"""
    monkeypatch.setenv(FLAG, "1")
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task = await _create(env, project_id, scenario="daily", goal="帮我改一下这句话")
    dirty = dict(task["dispatch_snapshot"])
    dirty["tool_policy"] = "workspace_edit"
    async with get_session_factory()() as session:
        row = await session.get(Task, task["id"])
        row.dispatch_snapshot = dirty
        await session.commit()
    env.mock.reset([{"content": "这句话可以更短一些。"}])
    resp = await env.client.post(
        f"{API}/tasks/{task['id']}/run", json={"provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text
    started = (await _events(task["id"], "task.started"))[-1]
    assert started["readonly"] is True


async def test_snapshot_survives_refresh_and_keeps_its_id(env, workspace, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    project_id = await create_project(env, workspace)
    task = await _create(env, project_id)
    again = (await env.client.get(f"{API}/tasks/{task['id']}")).json()
    assert again["dispatch_snapshot"]["snapshot_id"] == task["dispatch_snapshot"]["snapshot_id"]
    # 快照的 id 是字段哈希：再读一次不会漂移。
    assert again["dispatch_snapshot"]["dispatch"] == "queue"

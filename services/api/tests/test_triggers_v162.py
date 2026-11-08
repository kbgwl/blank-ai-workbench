# -*- coding: utf-8 -*-
"""v162 事件触发：去重、过期、点火、worker 到点放行。

核心断言只有一句：**同一件事只生成一个任务**。所以每条用例都回库数任务行，
而不是只看接口返回的 created 字段——接口说自己去重了、库里多一条，是最坏的情况。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from blank_api import worker as worker_module
from blank_api.db import get_session_factory
from blank_api.models.run import AgentRun
from blank_api.models.task import Task
from blank_api.models.trigger import AgentTrigger
from blank_api.services.agent import triggers as trg

from tests.test_agent_v04 import (  # noqa: F401  (复用端到端夹具)
    API,
    create_project,
    create_provider,
    env,
)

FEATURE = "BLANK_FEATURE_BACKGROUND_EXECUTION"
DISPATCH_FEATURE = "BLANK_FEATURE_SUPER_AGENT_DISPATCH"


async def _task_count() -> int:
    async with get_session_factory()() as session:
        return int((await session.execute(select(func.count()).select_from(Task))).scalar_one())


async def _post(env, project_id: str, **body):
    return await env.client.post(f"{API}/projects/{project_id}/triggers", json=body)


# ── 纯函数 ──────────────────────────────────────────────────────────────────


def test_unknown_event_type_is_rejected():
    with pytest.raises(ValueError):
        trg.normalize(event_type="email_received")


def test_dedupe_key_ignores_irrelevant_payload_fields():
    base = {"task_id": "t1", "status": "failed"}
    first = trg.dedupe_key_of("task_failed", "watchdog", base)
    second = trg.dedupe_key_of("task_failed", "watchdog", {**base, "at": "2026-01-01T00:00:00"})
    assert first == second
    # 关键事实变了就是另一件事
    assert trg.dedupe_key_of("task_failed", "watchdog", {**base, "status": "ok"}) != first


def test_normalize_defaults_window_and_times():
    event = trg.normalize(event_type="task_failed", source="runner", payload={"task_id": "t1"})
    assert event.dedupe_key
    assert event.expires_at is not None
    assert event.expires_at - event.created_at == trg.DEFAULT_WINDOW
    assert event.expired is False


def test_event_in_the_past_is_expired():
    event = trg.normalize(
        event_type="task_failed",
        expires_at=trg.utc_now() - timedelta(minutes=5),
    )
    assert event.expired is True


# ── 接口：开关与去重 ────────────────────────────────────────────────────────


async def test_endpoint_refuses_when_feature_off(env, workspace):
    project_id = await create_project(env, workspace)
    resp = await _post(env, project_id, event_type="task_failed", goal="修一下失败的测试")
    assert resp.status_code == 409
    assert "后台执行未开启" in resp.json()["error"]["message"]


async def test_trigger_creates_task_once_and_dedupes_repeats(env, workspace, monkeypatch):
    monkeypatch.setenv(FEATURE, "1")
    monkeypatch.setenv(DISPATCH_FEATURE, "1")
    project_id = await create_project(env, workspace)
    body = {
        "event_type": "task_failed",
        "source": "runner",
        "payload": {"task_id": "t-1", "status": "failed", "reason": "tests failed"},
        "goal": "修复失败的测试",
        "dedupe_key": "build-42",
    }
    first = await _post(env, project_id, **body)
    assert first.status_code == 201, first.text
    payload = first.json()
    assert payload["created"] is True
    assert payload["trigger"]["status"] == "pending"
    assert payload["task_id"]
    after_first = await _task_count()

    second = await _post(env, project_id, **body)
    assert second.status_code == 200, second.text
    assert second.json()["created"] is False
    assert second.json()["task_id"] == payload["task_id"]
    # 关键一条：库里没有多出第二条任务。
    assert await _task_count() == after_first

    # 触发确实落了一条事件，任务上能看到它从哪来
    detail = (await env.client.get(f"{API}/tasks/{payload['task_id']}")).json()
    assert detail["dispatch_snapshot"]["intent"] in ("execute", "answer", "ask_user", "schedule", "watch", "brainstorm")


async def test_different_facts_create_a_second_trigger(env, workspace, monkeypatch):
    monkeypatch.setenv(FEATURE, "1")
    project_id = await create_project(env, workspace)
    base = {"event_type": "task_failed", "source": "runner", "goal": "修复失败的测试"}
    first = await _post(env, project_id, **base, payload={"task_id": "t-1", "status": "failed"})
    second = await _post(env, project_id, **base, payload={"task_id": "t-2", "status": "failed"})
    assert first.json()["created"] is True
    assert second.json()["created"] is True
    assert first.json()["trigger"]["id"] != second.json()["trigger"]["id"]


async def test_expired_event_is_refused(env, workspace, monkeypatch):
    monkeypatch.setenv(FEATURE, "1")
    project_id = await create_project(env, workspace)
    past = (trg.utc_now() - timedelta(hours=1)).isoformat()
    resp = await _post(
        env, project_id, event_type="task_failed", goal="修一下", expires_at=past
    )
    assert resp.status_code == 400
    assert await _task_count() == 0


async def test_expired_trigger_row_is_reused_with_new_task(env, workspace, monkeypatch):
    """过期之后再发生同一件事：可以再开一次，但复用同一行（唯一键）。"""
    monkeypatch.setenv(FEATURE, "1")
    project_id = await create_project(env, workspace)
    body = {"event_type": "task_failed", "goal": "修复失败的测试", "dedupe_key": "k-1"}
    first = (await _post(env, project_id, **body)).json()
    async with get_session_factory()() as session:
        row = await session.get(AgentTrigger, first["trigger"]["id"])
        assert row is not None
        row.expires_at = trg.utc_now() - timedelta(seconds=1)
        await session.commit()
    second = (await _post(env, project_id, **body)).json()
    assert second["created"] is True
    assert second["trigger"]["id"] == first["trigger"]["id"]
    assert second["task_id"] != first["task_id"]
    async with get_session_factory()() as session:
        rows = (await session.execute(select(AgentTrigger))).scalars().all()
    assert len(rows) == 1


async def test_event_without_goal_registers_but_skips_task(env, workspace, monkeypatch):
    monkeypatch.setenv(FEATURE, "1")
    project_id = await create_project(env, workspace)
    resp = await _post(env, project_id, event_type="workspace_changed", payload={"path": "src/app.py"})
    payload = resp.json()
    assert payload["created"] is True
    assert payload["trigger"]["status"] == "skipped"
    assert payload["task_id"] is None
    # 不建空任务：没有目标就没有"任务"可言。
    assert await _task_count() == 0


# ── 点火与定时 ──────────────────────────────────────────────────────────────


async def test_fire_endpoint_queues_the_task_once(env, workspace, monkeypatch):
    monkeypatch.setenv(FEATURE, "1")
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    created = (
        await _post(
            env,
            project_id,
            event_type="user_task",
            goal="看一下 README",
            dedupe_key="fire-1",
            provider_id=provider_id,
        )
    ).json()

    fired = await env.client.post(f"{API}/triggers/{created['trigger']['id']}/fire")
    assert fired.status_code == 200, fired.text
    assert fired.json()["run_id"]
    async with get_session_factory()() as session:
        runs = (await session.execute(select(AgentRun))).scalars().all()
    assert len(runs) == 1

    again = await env.client.post(f"{API}/triggers/{created['trigger']['id']}/fire")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "trigger_not_pending"


async def test_worker_releases_due_trigger_once(env, workspace, monkeypatch):
    """独立 worker 形态：到点只入队，执行由认领循环负责。

    ``BLANK_INPROCESS_RUNNER=0`` 是这条的前提（多进程部署）。桌面形态下
    没有第二个进程来认领，放行时就必须自己起跑——那一条另有用例覆盖。
    """
    monkeypatch.setenv(FEATURE, "1")
    monkeypatch.setenv("BLANK_INPROCESS_RUNNER", "0")
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    created = (
        await _post(
            env,
            project_id,
            event_type="schedule",
            goal="跑一下测试",
            provider_id=provider_id,
            dedupe_key="due-1",
            fire_at=(trg.utc_now() - timedelta(seconds=5)).isoformat(),
        )
    ).json()

    released = await worker_module.release_due_triggers()
    assert released == 1
    async with get_session_factory()() as session:
        runs = (await session.execute(select(AgentRun))).scalars().all()
        row = await session.get(AgentTrigger, created["trigger"]["id"])
        assert row is not None
        assert row.status == trg.STATUS_FIRED
    assert len(runs) == 1 and runs[0].status == "queued"

    # 再扫一轮不会重复入队（状态已经是 fired，due() 不再返回它）。
    assert await worker_module.release_due_triggers() == 0
    async with get_session_factory()() as session:
        runs = (await session.execute(select(AgentRun))).scalars().all()
    assert len(runs) == 1


async def test_due_trigger_runs_itself_when_no_worker_will_claim_it(env, workspace, monkeypatch):
    """桌面形态：到点必须自己起跑，否则那条 run 永远停在 queued。

    桌面进程里没有独立 worker，``recover_pending_runs`` 也只在启动时扫一次。
    放行时若不入队即起跑，界面上会一直显示"排队中"，而没有任何东西会来认领。
    """
    import asyncio

    from blank_api.api.routes import tasks as task_routes

    monkeypatch.setenv(FEATURE, "1")
    monkeypatch.delenv("BLANK_INPROCESS_RUNNER", raising=False)  # 默认 = 桌面形态
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    await _post(
        env,
        project_id,
        event_type="schedule",
        goal="跑一下测试",
        provider_id=provider_id,
        dedupe_key="due-desktop-1",
        fire_at=(trg.utc_now() - timedelta(seconds=5)).isoformat(),
    )
    executed: list[str] = []

    async def fake_execute(run_id, worker_id, request, **kwargs):
        executed.append(run_id)

    monkeypatch.setattr(task_routes, "_execute_claimed_run", fake_execute)

    assert await worker_module.release_due_triggers() == 1
    for _ in range(40):
        if executed:
            break
        await asyncio.sleep(0.05)
    assert executed, "到点只入队没人认领：桌面形态下这条任务会永远 queued"


async def test_pending_without_fire_at_is_not_released(env, workspace, monkeypatch):
    """fire_at 为空 = 等外部事件，不是"现在就到期"。"""
    monkeypatch.setenv(FEATURE, "1")
    project_id = await create_project(env, workspace)
    await _post(env, project_id, event_type="user_task", goal="等我确认", dedupe_key="wait-1")
    assert await worker_module.release_due_triggers() == 0


async def test_expired_pending_rows_are_marked_not_deleted(env, workspace, monkeypatch):
    monkeypatch.setenv(FEATURE, "1")
    project_id = await create_project(env, workspace)
    created = (
        await _post(env, project_id, event_type="user_task", goal="过期看一下", dedupe_key="exp-1")
    ).json()
    async with get_session_factory()() as session:
        row = await session.get(AgentTrigger, created["trigger"]["id"])
        row.expires_at = trg.utc_now() - timedelta(seconds=1)
        await session.commit()
    await worker_module.release_due_triggers()
    async with get_session_factory()() as session:
        row = await session.get(AgentTrigger, created["trigger"]["id"])
        assert row is not None, "过期不能删行——'这件事没人在窗口内处理'本身就是证据"
        assert row.status == trg.STATUS_EXPIRED


async def test_list_triggers_filters_by_status(env, workspace, monkeypatch):
    monkeypatch.setenv(FEATURE, "1")
    project_id = await create_project(env, workspace)
    await _post(env, project_id, event_type="task_failed", goal="修测试", dedupe_key="l-1")
    await _post(env, project_id, event_type="workspace_changed", payload={"path": "a.py"}, dedupe_key="l-2")
    pending = (await env.client.get(f"{API}/projects/{project_id}/triggers?status=pending")).json()
    skipped = (await env.client.get(f"{API}/projects/{project_id}/triggers?status=skipped")).json()
    assert len(pending) == 1 and pending[0]["dedupe_key"] == "l-1"
    assert len(skipped) == 1 and skipped[0]["dedupe_key"] == "l-2"

# -*- coding: utf-8 -*-
"""v0.11.2 §8：触发器取消 / 暂停 / 恢复，以及 watch 扫描的路径边界。

§8 之前只有「等着」和「跑过了」两种活法：交代错了「每天 9 点」就没有任何办法
让它停下来，而它每天都会真的开一个任务。这里钉住三件事：

1. 停掉的触发器不再到点、不再被 watch 唤醒、也不能手动放行；
2. 状态变化在事件流里留痕，取消是终态但**不挡**同名事件重新登记；
3. watch 只能扫项目登记过的工作区，payload 里被改坏的 root 不能把整块盘扫一遍。

判据全部落在「它到底动没动」上，不看接口返回的 status 自述。
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from blank_api import worker as worker_module
from blank_api.db import get_session_factory
from blank_api.models.task import AgentEvent, Task
from blank_api.models.trigger import AgentTrigger
from blank_api.services.agent import triggers as trg
from blank_api.services.agent import watch_scan

from tests.test_agent_v04 import API, create_project, env  # noqa: F401


async def _seed(env, workspace, *, root=None, since=None, fire_at=None):
    """落一条真实的 pending 触发器 + 它关联的 draft 任务。"""
    project_id = await create_project(env, workspace)
    task_id = str(uuid4())
    trigger_id = str(uuid4())
    dedupe_key = f"task:{task_id}"
    payload = {"task_id": task_id, "intent": "watch", "goal": "盯着工作区"}
    if root:
        payload["watch_root"] = root
    if since:
        payload["watch_since"] = since
    async with get_session_factory()() as session:
        session.add(
            Task(
                id=task_id,
                project_id=project_id,
                title="触发器",
                goal="盯着工作区",
                status="draft",
                goal_spec={},
            )
        )
        session.add(
            AgentTrigger(
                id=trigger_id,
                project_id=project_id,
                event_type="schedule",
                source="task.start",
                payload=payload,
                dedupe_key=dedupe_key,
                task_id=task_id,
                provider_id="",
                run_payload={},
                fire_at=fire_at,
                status=trg.STATUS_PENDING,
            )
        )
        await session.commit()
    return project_id, task_id, trigger_id, dedupe_key


async def _due_ids() -> set[str]:
    async with get_session_factory()() as session:
        return {row.id for row in await trg.due(session, limit=200)}


async def test_pause_freezes_resume_restores_cancel_is_terminal(env, workspace):
    _, _, tid, _ = await _seed(env, workspace, fire_at=trg.utc_now() - timedelta(seconds=5))
    assert tid in await _due_ids(), "已经到点的 pending 触发器本该被扫到"

    paused = await env.client.post(f"{API}/triggers/{tid}/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == trg.STATUS_PAUSED
    assert tid not in await _due_ids(), "暂停后还在到点列表里，等于没停"

    again = await env.client.post(f"{API}/triggers/{tid}/pause")
    assert again.status_code == 409, again.text

    resumed = await env.client.post(f"{API}/triggers/{tid}/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == trg.STATUS_PENDING
    assert tid in await _due_ids(), "恢复后必须回到扫描面，原来的 fire_at 不能丢"

    cancelled = await env.client.post(f"{API}/triggers/{tid}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == trg.STATUS_CANCELLED
    assert tid not in await _due_ids()
    assert (await env.client.post(f"{API}/triggers/{tid}/resume")).status_code == 409


async def test_status_changes_leave_an_event_trail(env, workspace):
    """界面上看不见的后台状态变化，必须能在事件流里查到（§8 可审计）。"""
    _, task_id, tid, _ = await _seed(env, workspace)
    for action in ("pause", "resume", "cancel"):
        resp = await env.client.post(f"{API}/triggers/{tid}/{action}")
        assert resp.status_code == 200, f"{action}: {resp.text}"
    async with get_session_factory()() as session:
        kinds = set(
            (
                await session.execute(
                    select(AgentEvent.type).where(AgentEvent.task_id == task_id)
                )
            ).scalars()
        )
    assert {"trigger.paused", "trigger.resumed", "trigger.cancelled"} <= kinds, kinds


async def test_cancelled_row_does_not_block_re_registration(env, workspace):
    """取消是终态，但用户把同一句话再说一次，必须能重新登记。"""
    project_id, _, tid, key = await _seed(env, workspace)
    resp = await env.client.post(f"{API}/triggers/{tid}/cancel")
    assert resp.status_code == 200, resp.text

    async with get_session_factory()() as session:
        assert await trg.find_active(session, project_id, key) is None
        event = trg.normalize(
            event_type="schedule",
            source="test",
            payload={"goal": "再设一次"},
            dedupe_key=key,
        )
        row, created = await trg.ingest(
            session, project_id, event, status=trg.STATUS_PENDING
        )
        await session.commit()
    assert created is True, "被取消的行还挡着去重，用户会以为「我设了它不理我」"
    assert row.status == trg.STATUS_PENDING


def test_allowed_watch_root_only_accepts_a_workspace_or_its_child(tmp_path):
    workspace_dir = tmp_path / "repo"
    workspace_dir.mkdir()
    child = workspace_dir / "src"
    child.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    assert watch_scan.allowed_watch_root(workspace_dir, [str(workspace_dir)])
    assert watch_scan.allowed_watch_root(child, [str(workspace_dir)])
    assert not watch_scan.allowed_watch_root(elsewhere, [str(workspace_dir)])
    assert not watch_scan.allowed_watch_root(tmp_path, [str(workspace_dir)]), "父目录不算"
    assert not watch_scan.allowed_watch_root(workspace_dir, []), "没登记工作区就不该扫"


async def test_watch_outside_workspace_is_never_released(env, workspace, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "changed.py").write_text("x", encoding="utf-8")
    _, _, tid, _ = await _seed(
        env,
        workspace,
        root=str(outside),
        since=(trg.utc_now() - timedelta(seconds=2)).isoformat(),
    )

    called: list[str] = []

    async def fake_release(name: str, *, start_runner: bool = False) -> bool:
        called.append(name)
        return True

    monkeypatch.setattr(worker_module, "release_one", fake_release)
    await watch_scan.watch_once()

    assert tid not in called, "路径越界还是放行了"
    async with get_session_factory()() as session:
        row = await session.get(AgentTrigger, tid)
    assert row is not None
    assert row.payload.get("watch_blocked_reason") == "root_outside_workspace"

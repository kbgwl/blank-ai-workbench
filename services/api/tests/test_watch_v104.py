# -*- coding: utf-8 -*-
"""v0.10.4：「盯着」与「到点」两条路真的会醒。

这两条以前都是断的，而且断法一样——**登记成功、永远不醒**：

* 定时：defer 写死 ``fire_at=None``，``due()`` 只按时间扫，扫不到；
* 盯着：``workspace_changed`` 在整个仓库里没有生产者，没人唤醒。

所以这里的断言都落在"它到底动没动"上，不看接口返回的 status。
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from blank_api import worker as worker_module
from blank_api.db import get_session_factory
from blank_api.models.task import Task
from blank_api.models.trigger import AgentTrigger
from blank_api.services.agent import triggers as trg
from blank_api.services.agent import watch_scan

from tests.test_agent_v04 import (  # noqa: F401  (复用端到端夹具)
    API,
    create_provider,
    create_project,
    env,
)

FEATURE = "BLANK_FEATURE_BACKGROUND_EXECUTION"


def test_changed_files_finds_edits_and_skips_dependencies(tmp_path):
    """node_modules 里的 mtime 不算"工作区变化"：一次 install 就能造上万条噪音。"""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x", encoding="utf-8")
    (root / "node_modules" / "pkg" / "index.js").write_text("y", encoding="utf-8")

    since = trg.utc_now() - timedelta(minutes=1)
    assert watch_scan.changed_files(root, since) == ["src/a.py"]


def test_changed_files_reports_nothing_for_the_future(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.txt").write_text("x", encoding="utf-8")
    assert watch_scan.changed_files(root, trg.utc_now() + timedelta(minutes=5)) == []


def test_changed_files_survives_missing_directory(tmp_path):
    """路径不可达就当没变化：一条盯不上的监控不该把整轮巡检带走。"""
    assert watch_scan.changed_files(tmp_path / "nope", trg.utc_now()) == []


async def _draft_task(project_id: str, goal: str) -> str:
    task_id = str(uuid4())
    async with get_session_factory()() as session:
        session.add(
            Task(
                id=task_id,
                project_id=project_id,
                title=goal[:20],
                goal=goal,
                status="draft",
                goal_spec={"supervisor": {"intent": "watch", "needs_execution": False}},
            )
        )
        await session.commit()
    return task_id


async def _watch_trigger(project_id: str, task_id: str, *, since: str) -> str:
    trigger_id = str(uuid4())
    async with get_session_factory()() as session:
        session.add(
            AgentTrigger(
                id=trigger_id,
                project_id=project_id,
                event_type="schedule",
                source="task.start",
                payload={"task_id": task_id, "intent": "watch", "watch_since": since, "goal": "盯着"},
                dedupe_key=f"task:{task_id}",
                task_id=task_id,
                provider_id="",
                run_payload={},
                fire_at=None,
                status="pending",
            )
        )
        await session.commit()
    return trigger_id


async def test_watch_is_released_when_workspace_changes(env, workspace, monkeypatch):
    """工作区真的改了文件，那条「盯着」就必须被放行。"""
    project_id = await create_project(env, workspace)
    task_id = await _draft_task(project_id, "盯着工作区，有变化就告诉我")
    trigger_id = await _watch_trigger(
        project_id, task_id, since=(trg.utc_now() - timedelta(seconds=2)).isoformat()
    )
    (workspace / "edited.txt").write_text("hello", encoding="utf-8")

    calls: list[tuple[str, bool]] = []

    async def fake_release(name: str, *, start_runner: bool = False) -> bool:
        calls.append((name, start_runner))
        return True

    monkeypatch.setattr(worker_module, "release_one", fake_release)

    assert await watch_scan.watch_once() == 1
    assert calls == [(trigger_id, True)], "放行时必须自己起跑，桌面形态没人认领队列"

    async with get_session_factory()() as session:
        row = await session.get(AgentTrigger, trigger_id)
    assert row is not None
    # 夹具建工作区时也会写文件，它们同样在 since 之后——这里只要求改的那个在里面。
    assert "edited.txt" in row.payload["changed"]


async def test_watch_stays_put_when_nothing_changed(env, workspace, monkeypatch):
    """没变化就不能放行：否则「盯着」会变成每 30 秒跑一次。"""
    project_id = await create_project(env, workspace)
    task_id = await _draft_task(project_id, "盯着工作区，有变化就告诉我")
    await _watch_trigger(
        project_id, task_id, since=(trg.utc_now() + timedelta(minutes=10)).isoformat()
    )

    async def boom(*args, **kwargs):  # pragma: no cover - 走到这里就是错
        raise AssertionError("没有变化却放行了")

    monkeypatch.setattr(worker_module, "release_one", boom)
    assert await watch_scan.watch_once() == 0


async def test_watch_uses_the_path_recorded_at_registration(env, workspace, monkeypatch, tmp_path):
    """登记时固化的路径优先：之后切换活动工作区，不该让这条盯着换地方。

    否则用户说的是「盯着 README」，系统去盯另一个仓库——而且界面上完全看不出来。
    """
    from blank_api.models.project import Workspace

    watched = tmp_path / "watched-repo"
    watched.mkdir()
    other = tmp_path / "other-repo"
    other.mkdir()

    project_id = await create_project(env, workspace)
    async with get_session_factory()() as session:
        # v0.11.2 §8：登记时固化的 watch_root 现在必须是项目登记过的工作区，
        # 所以这里补上 watched 的 Workspace 行（真实登记流程本来就会写这一行）。
        # 断言一字未改：active 仍是 other，测的还是「登记路径优先」。
        session.add_all(
            [
                Workspace(
                    id=str(uuid4()),
                    project_id=project_id,
                    label="另一个",
                    path=str(other),
                    is_active=True,
                ),
                Workspace(
                    id=str(uuid4()),
                    project_id=project_id,
                    label="登记时盯的目录",
                    path=str(watched),
                    is_active=False,
                ),
            ]
        )
        await session.commit()

    task_id = await _draft_task(project_id, "盯着 watched-repo，有变化就告诉我")
    async with get_session_factory()() as session:
        session.add(
            AgentTrigger(
                id=str(uuid4()),
                project_id=project_id,
                event_type="schedule",
                source="task.start",
                payload={
                    "task_id": task_id,
                    "intent": "watch",
                    "watch_since": (trg.utc_now() - timedelta(seconds=2)).isoformat(),
                    "watch_root": str(watched),
                    "goal": "盯着 watched-repo",
                },
                dedupe_key=f"task:{task_id}",
                task_id=task_id,
                provider_id="",
                run_payload={},
                fire_at=None,
                status="pending",
            )
        )
        await session.commit()

    (watched / "changed.py").write_text("x", encoding="utf-8")
    calls: list[str] = []

    async def fake_release(name: str, *, start_runner: bool = False) -> bool:
        calls.append(name)
        return True

    monkeypatch.setattr(worker_module, "release_one", fake_release)

    assert await watch_scan.watch_once() == 1, "改的是登记时那个目录，必须放行"
    assert len(calls) == 1


async def test_schedule_turn_gets_a_real_future_fire_at(env, workspace, monkeypatch):
    """「30 分钟后提醒我」必须落成一个未来的 fire_at，而不是空。

    这条同时钉住两处：supervisor 要把这句话判成定时（只认「每天/到点」
    这些词的话它会被当成普通任务当场跑掉），defer 要把时间解析出来写进库。
    """
    monkeypatch.setenv(FEATURE, "1")
    project_id = await create_project(env, workspace)
    thread = (
        await env.client.post(f"{API}/projects/{project_id}/threads", json={"title": "定时"})
    ).json()
    resp = await env.client.post(
        f"{API}/threads/{thread['id']}/turns",
        json={"title": "提醒", "goal": "30分钟后提醒我检查一下部署", "auto_start": True},
    )
    assert resp.status_code == 201, resp.text
    task = resp.json()
    assert task["goal_spec"]["supervisor"]["intent"] == "schedule"
    assert task["status"] == "draft"

    async with get_session_factory()() as session:
        rows = (
            await session.execute(select(AgentTrigger).where(AgentTrigger.task_id == task["id"]))
        ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.fire_at is not None, "定时却写了个空的到点时间，等于永远不响"
    assert row.fire_at > trg.utc_now(), "到点时间必须在将来"
    assert row.fire_at <= trg.utc_now() + timedelta(minutes=40)
    assert row.payload.get("intent") == "schedule"


async def test_schedule_without_a_time_falls_back_to_running_now(env, workspace, monkeypatch):
    """说了「定时」却读不出时间：当场做一次，而不是登记一条永远不响的。"""
    monkeypatch.setenv(FEATURE, "1")
    project_id = await create_project(env, workspace)
    await create_provider(env)
    thread = (
        await env.client.post(f"{API}/projects/{project_id}/threads", json={"title": "定时"})
    ).json()
    resp = await env.client.post(
        f"{API}/threads/{thread['id']}/turns",
        json={"title": "定时任务", "goal": "定时把依赖升级一下", "auto_start": True},
    )
    assert resp.status_code == 201, resp.text
    task = resp.json()
    assert task["status"] != "draft", "读不出时间就不该停在 draft 里等一个不会来的时刻"

    async with get_session_factory()() as session:
        rows = (
            await session.execute(select(AgentTrigger).where(AgentTrigger.task_id == task["id"]))
        ).scalars().all()
    assert rows == [], "没有到点时间就不该登记一条 pending"


async def test_daily_trigger_reschedules_itself_after_firing(env, workspace, monkeypatch):
    """每天 9 点：跑完必须自动排下一次，否则「每天」只发生一次。"""
    monkeypatch.setenv(FEATURE, "1")
    project_id = await create_project(env, workspace)
    task_id = await _draft_task(project_id, "每天早上 8 点汇总一次")
    fire_at = trg.utc_now() - timedelta(seconds=5)
    async with get_session_factory()() as session:
        session.add(
            AgentTrigger(
                id=str(uuid4()),
                project_id=project_id,
                event_type="schedule",
                source="task.start",
                payload={"task_id": task_id, "intent": "schedule", "repeat": {"kind": "daily", "at": "08:00"}},
                dedupe_key=f"task:{task_id}",
                task_id=task_id,
                provider_id="",
                run_payload={},
                fire_at=fire_at,
                status="pending",
            )
        )
        await session.commit()

    async def fake_launch(session, task, payload, request, **kwargs):
        return {"task_id": task.id, "run_id": "r", "status": "queued"}

    from blank_api.api.routes import tasks as task_routes

    monkeypatch.setattr(task_routes, "launch_task", fake_launch)

    assert await worker_module.release_due_triggers() == 1
    async with get_session_factory()() as session:
        pending = (
            await session.execute(
                select(AgentTrigger).where(
                    AgentTrigger.task_id == task_id,
                    AgentTrigger.status == trg.STATUS_PENDING,
                )
            )
        ).scalars().all()
    assert len(pending) == 1, "「每天」跑完没排下一次，就只会发生一次"
    assert pending[0].fire_at == fire_at + timedelta(days=1)
    assert pending[0].payload.get("repeat") == {"kind": "daily", "at": "08:00"}

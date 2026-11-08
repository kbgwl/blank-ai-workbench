# -*- coding: utf-8 -*-
"""v0.10.2 监控页：把「它正在盯什么」变成可读的读数。

这组用例盯三件事：

1. 判定流只收**带 supervisor 段**的任务——把 v0.9.1 的旧轮次混进来，会让
   「判成什么」这一列大面积空着，看起来像判定失败了；
2. 「登记了却永远等不到」的待触发项必须被标出来，不能装作它在盯着；
3. 触发源清单里 IM 三条必须是**未接通**——写错这一格，等于对用户声称
   它能读你的聊天记录。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from blank_api.db import get_session_factory
from blank_api.models.task import Task
from blank_api.models.trigger import AgentTrigger

from tests.test_agent_v04 import (  # noqa: F401  (复用端到端夹具)
    API,
    create_project,
    create_provider,
    env,
)

FEATURE = "BLANK_FEATURE_BACKGROUND_EXECUTION"


async def _make_task(project_id: str, *, goal: str = "改一下按钮颜色", status: str = "succeeded", supervisor: dict | None = None) -> str:
    async with get_session_factory()() as session:
        task = Task(
            id=str(uuid4()),
            project_id=project_id,
            title=goal[:20],
            goal=goal,
            status=status,
            goal_spec={"supervisor": supervisor} if supervisor else {},
        )
        session.add(task)
        await session.commit()
        return task.id


async def _make_trigger(project_id: str, *, event_type: str = "schedule", fire_at: datetime | None = None, goal: str = "盯着部署脚本") -> str:
    async with get_session_factory()() as session:
        row = AgentTrigger(
            id=str(uuid4()),
            project_id=project_id,
            event_type=event_type,
            source="task.start",
            payload={"goal": goal},
            dedupe_key=f"k-{uuid4().hex[:8]}",
            fire_at=fire_at,
            status="pending",
        )
        session.add(row)
        await session.commit()
        return row.id


async def _get(env, query: str = ""):
    return (await env.client.get(f"{API}/monitor{query}")).json()


async def _judgement() -> dict:
    return {
        "intent": "execute",
        "reason": "要动工作区",
        "needs_execution": True,
        "scenario": "dev",
        "automation_mode": "assist",
    }


async def test_monitor_skips_tasks_without_a_supervisor_block(env, workspace):
    """旧轮次没有 supervisor 段，不该出现在「它把这句话判成了什么」里。"""
    project_id = await create_project(env, workspace)
    await _make_task(project_id, goal="老任务", supervisor=None)
    fresh_id = await _make_task(project_id, goal="新一轮", supervisor=await _judgement())

    data = await _get(env)

    ids = [item["task_id"] for item in data["watching"]]
    assert ids == [fresh_id], "只该有带判定段的那一条"
    assert data["watching"][0]["intent"] == "execute"
    assert data["watching"][0]["needs_execution"] is True
    assert data["counts"]["watching"] == 1


async def test_monitor_lists_open_tasks_but_not_finished_ones(env, workspace):
    project_id = await create_project(env, workspace)
    running_id = await _make_task(project_id, goal="正在跑", status="running", supervisor=await _judgement())
    waiting_id = await _make_task(project_id, goal="等审批", status="waiting_approval", supervisor=await _judgement())
    await _make_task(project_id, goal="已经做完", status="succeeded", supervisor=await _judgement())

    data = await _get(env)

    open_ids = {item["task_id"] for item in data["running"]}
    assert open_ids == {running_id, waiting_id}
    assert data["counts"]["running"] == 1
    assert data["counts"]["waiting_approval"] == 1


async def test_trigger_without_fire_time_is_flagged_as_never_firing(env, workspace, monkeypatch):
    """这正是「监控聊天」被登记后的真实处境：没有到点时间，也没有事件源。

    ``triggers.due()`` 只捞 ``fire_at <= now`` 的行，而 watch/schedule 登记时
    fire_at 是空的——它既不会被时间扫到，也不会被事件唤醒。界面必须说破这件事，
    否则用户会以为自己在被盯着。
    """
    monkeypatch.setenv(FEATURE, "1")
    project_id = await create_project(env, workspace)
    trigger_id = await _make_trigger(project_id, event_type="schedule", fire_at=None)

    data = await _get(env)

    item = next(p for p in data["pending"] if p["trigger_id"] == trigger_id)
    assert item["never_fires"] is True
    assert "不会到来" in item["waiting_for"]
    assert item["goal"] == "盯着部署脚本"
    assert data["counts"]["never_fires"] == 1


async def test_trigger_with_fire_time_is_a_real_schedule(env, workspace, monkeypatch):
    monkeypatch.setenv(FEATURE, "1")
    project_id = await create_project(env, workspace)
    trigger_id = await _make_trigger(
        project_id, event_type="schedule", fire_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=30)
    )

    data = await _get(env)

    item = next(p for p in data["pending"] if p["trigger_id"] == trigger_id)
    assert item["never_fires"] is False
    assert "到点" in item["waiting_for"]
    assert item["fire_at"], "带时间的定时项要能看见它定在什么时候"
    assert data["counts"]["never_fires"] == 0


async def test_pending_is_empty_and_explained_when_background_is_off(env, workspace, monkeypatch):
    """后台执行关着时，待触发项不会有人消费——接口要把这件事说清楚。"""
    monkeypatch.delenv(FEATURE, raising=False)
    project_id = await create_project(env, workspace)
    await _make_trigger(project_id)

    data = await _get(env)

    assert data["background_execution"] is False
    assert data["pending"] == []
    assert data["counts"]["pending"] == 0


async def test_im_channels_are_reported_as_not_connected(env, workspace):
    """三个 IM 渠道都必须明说未接通：这是「监控聊天软件」这件事的诚实读数。"""
    project_id = await create_project(env, workspace)  # noqa: F841  只要库在
    data = await _get(env)

    im = {item["id"]: item for item in data["sources"] if item["kind"] == "im"}
    assert set(im) == {"wecom", "dingtalk", "feishu"}
    assert all(item["connected"] is False for item in im.values())
    assert all("未接通" in item["note"] for item in im.values())

    builtin = [item for item in data["sources"] if item["kind"] == "builtin"]
    assert {"user_task", "task_failed", "approval_expired", "workspace_changed", "schedule"} <= {
        item["id"] for item in builtin
    }
    assert all(item["connected"] is True for item in builtin)


async def test_limit_is_clamped_and_response_shape_is_stable(env, workspace):
    project_id = await create_project(env, workspace)
    await _make_task(project_id, goal="a", supervisor=await _judgement())
    await _make_task(project_id, goal="b", supervisor=await _judgement())

    data = await _get(env, "?limit=1")

    assert len(data["watching"]) == 1
    for key in ("now", "watching", "running", "pending", "sources", "counts", "background_execution"):
        assert key in data


# ── 「盯着 X」必须真的登记成待触发（v0.10.2 修的两个断点）────────────────────
async def test_watch_turn_is_registered_instead_of_running(env, workspace, monkeypatch):
    """说「盯着 .env，一旦变化就告诉我」，不能变成一句什么都不发生的话。

    真机上这条链路断过两处：`needs_execution=False` 让 defer 分支根本不进；
    进了之后 `_defer_if_needed` 又只读 `task.dispatch_snapshot`（对话轮次从不写它）。
    两处都通之后，任务停成 draft，并留下一条 pending 触发器。
    """
    from sqlalchemy import select

    monkeypatch.setenv(FEATURE, "1")
    project_id = await create_project(env, workspace)
    thread = (
        await env.client.post(f"{API}/projects/{project_id}/threads", json={"title": "监控"})
    ).json()
    resp = await env.client.post(
        f"{API}/threads/{thread['id']}/turns",
        json={
            "title": "盯着 .env",
            "goal": "盯着工作区的 .env，一旦有变化就告诉我改了什么",
            "auto_start": True,
        },
    )
    assert resp.status_code == 201, resp.text
    task = resp.json()
    assert task["goal_spec"]["supervisor"]["intent"] == "watch"
    assert task["status"] == "draft", "监控类请求不该当场跑起来"

    async with get_session_factory()() as session:
        rows = (await session.execute(select(AgentTrigger).where(AgentTrigger.task_id == task["id"]))).scalars().all()
    assert len(rows) == 1, "没有登记待触发，就等于这句话没被记住"
    assert rows[0].fire_at is None
    assert rows[0].status == "pending"

    data = await _get(env)
    item = next(p for p in data["pending"] if p["task_id"] == task["id"])
    assert item["never_fires"] is True, "没有事件源就必须说破，不能装作在盯"


async def test_defer_intent_reads_both_snapshot_shapes(env, workspace):
    """两种任务形态都要认：有 dispatch_snapshot 的走快照，对话轮次的走 goal_spec。"""
    from blank_api.api.routes.tasks import _defer_intent

    project_id = await create_project(env, workspace)
    plain = Task(id=str(uuid4()), project_id=project_id, title="空", goal="空", status="draft", goal_spec={})
    assert _defer_intent(plain) == ""

    watch_task = Task(
        id=str(uuid4()),
        project_id=project_id,
        title="盯着",
        goal="盯着 a 文件",
        status="draft",
        goal_spec={"dispatch": {"intent": "watch"}},
    )
    assert _defer_intent(watch_task) == "watch"

    execute_task = Task(
        id=str(uuid4()),
        project_id=project_id,
        title="改",
        goal="改按钮颜色",
        status="draft",
        goal_spec={"dispatch": {"intent": "execute"}},
    )
    assert _defer_intent(execute_task) == "", "只有 watch/schedule 能进 defer，别放宽"

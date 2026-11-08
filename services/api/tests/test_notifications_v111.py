# -*- coding: utf-8 -*-
"""v0.11.1 桌面通知：后端判完，壳只负责弹。

这组用例钉住"取数口不重判"这件事：

1. 只吐 ``disturb=true`` 的行。后端说不打扰，客户端就不该收到——否则会出现
   「设置里说了静默、系统却在响」这种无法解释的状态；
2. 带着任务标题与 ``origin``：定时提醒弹出来必须像提醒，不能写"任务完成"；
3. 游标语义要经得起反复调：空批次不能把游标推回去（推回去就会重弹旧通知）。

时间口径在这里格外容易写错：``agent_events.created_at`` 是 naive **UTC**
（SQLite 的 CURRENT_TIMESTAMP）。拿本地时间比，东八区会整整偏 8 小时。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from blank_api.db import get_session_factory
from blank_api.models.task import AgentEvent, Task

from tests.test_agent_v04 import (  # noqa: F401  (复用端到端夹具)
    API,
    create_project,
    create_provider,
    env,
)


def _utc(**kwargs) -> datetime:
    """库里的口径：naive UTC。"""
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).replace(tzinfo=None)


async def _make_task(
    project_id: str,
    *,
    goal: str = "检查一下部署",
    status: str = "succeeded",
    intent: str = "",
) -> str:
    async with get_session_factory()() as session:
        task = Task(
            id=str(uuid4()),
            project_id=project_id,
            title=goal[:20],
            goal=goal,
            status=status,
            dispatch_snapshot={"intent": intent} if intent else {},
        )
        session.add(task)
        await session.commit()
        return task.id


async def _add_decision(
    task_id: str,
    *,
    disturb: bool = True,
    kind: str = "deliverable",
    status: str = "succeeded",
    origin: str = "",
    created_at: datetime | None = None,
    seq: int = 1,
) -> str:
    async with get_session_factory()() as session:
        event = AgentEvent(
            id=str(uuid4()),
            task_id=task_id,
            seq=seq,
            node="finish",
            type="notify.decided",
            payload={
                "policy": "milestone",
                "event": kind,
                "quiet_hours": False,
                "disturb": disturb,
                "reason": "命中 milestone 策略" if disturb else "milestone 策略下不打扰",
                "status": status,
                "origin": origin,
            },
            created_at=created_at or _utc(seconds=1),
        )
        session.add(event)
        await session.commit()
        return event.id


async def _get(env, query: str = ""):
    response = await env.client.get(f"{API}/notifications{query}")
    assert response.status_code == 200, response.text
    return response.json()


async def test_only_disturbing_decisions_are_returned(env, workspace):
    """判过是"别打扰"的那条不该出现在取数口里。"""
    project_id = await create_project(env, workspace)
    audible = await _make_task(project_id, goal="部署检查")
    muted = await _make_task(project_id, goal="安静的任务")
    await _add_decision(audible, disturb=True)
    await _add_decision(muted, disturb=False, seq=1)

    data = await _get(env)

    task_ids = [item["task_id"] for item in data["items"]]
    assert task_ids == [audible], "只有 disturb=true 的那条该被吐出来"
    assert muted not in task_ids


async def test_item_carries_what_the_shell_needs_to_render(env, workspace):
    """标题、正文、跳转目标、来源——壳手上不该缺这些。"""
    project_id = await create_project(env, workspace)
    task_id = await _make_task(project_id, goal="检查一下部署")
    await _add_decision(task_id, kind="deliverable", origin="schedule")

    data = await _get(env)
    item = data["items"][0]

    assert item["task_id"] == task_id
    assert item["goal"] == "检查一下部署"
    assert item["title"]
    assert item["kind"] == "deliverable"
    assert item["status"] == "succeeded"
    assert item["origin"] == "schedule", "定时起的任务要能跟手动任务区分开"
    assert item["reason"], "理由要带出来，证据页靠它解释为什么响"


async def test_origin_is_taken_from_the_event_not_recomputed(env, workspace):
    """取数口不许有自己的口径：事件说 schedule 就是 schedule。

    这条来自真机验收踩的坑：``_record_notify_decision`` 已经从触发器把 origin
    补成 ``schedule`` 了，而这个接口又按（空的）任务快照算回 ``user``——
    结果是定时提醒弹出来写「做完了」。同一件事两个人算，就一定有一个是错的。
    """
    project_id = await create_project(env, workspace)
    # 任务快照里写着 watch，但事件里定的是 schedule——取数口必须听事件的。
    task_id = await _make_task(project_id, intent="watch")
    await _add_decision(task_id, origin="schedule")

    data = await _get(env)

    assert data["items"][0]["origin"] == "schedule", (
        "取数口按快照重算了 origin——判定与取数各算一次，必然漂移"
    )


async def test_origin_falls_back_to_user_when_the_event_has_none(env, workspace):
    """事件里没写来源的是人自己发起的，不能被认成定时。"""
    project_id = await create_project(env, workspace)
    task_id = await _make_task(project_id, goal="随手问一句")
    await _add_decision(task_id)

    data = await _get(env)

    assert data["items"][0]["origin"] == "user"


async def test_since_cursor_returns_only_newer_rows(env, workspace):
    """游标之后的行才算新的——这是"不重弹"唯一的凭据。"""
    project_id = await create_project(env, workspace)
    old_task = await _make_task(project_id, goal="很早以前")
    new_task = await _make_task(project_id, goal="刚刚跑完")
    old_at = _utc(minutes=30)
    await _add_decision(old_task, created_at=old_at)

    first = await _get(env)
    assert [item["task_id"] for item in first["items"]] == [old_task]
    cursor = first["cursor"]
    assert cursor, "有结果就必须给游标，否则客户端只能靠时间瞎猜"

    await _add_decision(new_task, created_at=_utc(seconds=1))
    second = await _get(env, f"?since={cursor}")

    assert [item["task_id"] for item in second["items"]] == [new_task]


async def test_empty_batch_does_not_move_the_cursor_backwards(env, workspace):
    """空批次返回空游标，客户端据此保留原游标——推回去会把旧通知重弹一遍。"""
    project_id = await create_project(env, workspace)
    task_id = await _make_task(project_id)
    await _add_decision(task_id, created_at=_utc(minutes=5))

    first = await _get(env)
    cursor = first["cursor"]

    second = await _get(env, f"?since={cursor}")

    assert second["items"] == []
    assert second["cursor"] == "", "没读到新东西就不该给游标"


async def test_items_are_ordered_oldest_first(env, workspace):
    """正序：壳按顺序弹，最后出现的那条才是最新的。倒序会让最新的被盖住。"""
    project_id = await create_project(env, workspace)
    older = await _make_task(project_id, goal="先跑完的")
    newer = await _make_task(project_id, goal="后跑完的")
    await _add_decision(older, created_at=_utc(minutes=10))
    await _add_decision(newer, created_at=_utc(seconds=5))

    data = await _get(env)

    assert [item["task_id"] for item in data["items"]] == [older, newer]


async def test_broken_cursor_falls_back_instead_of_erroring(env, workspace):
    """壳传了个坏游标，退回默认回看窗口就好——回 400 只会让通知彻底不响。"""
    project_id = await create_project(env, workspace)
    task_id = await _make_task(project_id)
    await _add_decision(task_id, created_at=_utc(minutes=1))

    data = await _get(env, "?since=not-a-timestamp")

    assert [item["task_id"] for item in data["items"]] == [task_id]


async def test_待审批的通知即使碰上免打扰也要送出去(env, workspace):
    """审批压着任务动不了，免打扰时段也必须响——这一条是 notify.py 的口径，
    这里只确认它穿过取数口时没被吞掉。"""
    project_id = await create_project(env, workspace)
    task_id = await _make_task(project_id, goal="要你批准的命令", status="waiting_approval", intent="schedule")
    async with get_session_factory()() as session:
        session.add(
            AgentEvent(
                id=str(uuid4()),
                task_id=task_id,
                seq=1,
                node="finish",
                type="notify.decided",
                payload={
                    "policy": "silent",
                    "event": "approval",
                    "quiet_hours": True,
                    "disturb": True,
                    "reason": "审批等待",
                    "status": "waiting_approval",
                    "origin": "schedule",
                },
                created_at=_utc(seconds=1),
            )
        )
        await session.commit()

    data = await _get(env)

    assert len(data["items"]) == 1
    assert data["items"][0]["kind"] == "approval"
    assert data["items"][0]["status"] == "waiting_approval"

"""v165 Phase 1：Chat Supervisor 与 Goal Tracker。

验收口径（docs/workbuddy-v165-autonomous-super-agent.md §17 Phase 1）：
用户一句自然语言自动形成目标；后续「继续」恢复原目标，不是又开一轮空任务。
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.goal import Goal
from blank_api.models.project import Project, Workspace
from blank_api.models.task import AgentEvent, Task
from blank_api.models.thread import Thread
from blank_api.services.agent.supervisor import (
    INTENT_ANSWER,
    INTENT_APPROVE,
    INTENT_CANCEL,
    INTENT_CONTINUE,
    INTENT_EXECUTE,
    INTENT_INSPECT,
    INTENT_PAUSE,
    execution_override,
    observe,
    wants_new_goal,
)

API = "/api/v1"


async def _project(*, with_workspace: bool = True) -> Project:
    """默认建一个已绑定工作区的项目：真机上用户都是这么用的。

    ``with_workspace=False`` 专门留给「Dispatcher 不该自动下发」那条反例——
    运行时 _run_request 遇到裸项目会直接 409，下发器必须在点运行之前就拦住。
    """
    async with get_session_factory()() as session:
        project = Project(id=str(uuid.uuid4()), name="监督者测试")
        session.add(project)
        if with_workspace:
            session.add(
                Workspace(
                    id=str(uuid.uuid4()),
                    project_id=project.id,
                    label="测试工作区",
                    path="E:/tmp/blank-v165-test",
                    is_active=True,
                )
            )
        await session.commit()
        return project


async def _thread(project_id: str) -> Thread:
    async with get_session_factory()() as session:
        thread = Thread(
            id=str(uuid.uuid4()),
            project_id=project_id,
            title="监督者会话",
            turn_count=0,
        )
        session.add(thread)
        await session.commit()
        return thread


async def _turn(client, thread_id: str, text: str):
    return await client.post(
        f"{API}/threads/{thread_id}/turns",
        json={"title": text[:40], "goal": text},
    )


# --------------------------------------------------------------------------- #
# 纯判断：一条消息只判一次，不调模型、不改原话
# --------------------------------------------------------------------------- #

def test_control_words_are_classified_without_a_model_call():
    assert observe("继续", has_active_goal=True).intent == INTENT_CONTINUE
    assert observe("暂停", has_active_goal=True).intent == INTENT_PAUSE
    assert observe("取消", has_active_goal=True).intent == INTENT_CANCEL
    assert observe("重试", has_active_goal=True).control_action == "retry"
    assert observe("同意", active_task_status="waiting_approval").intent == INTENT_APPROVE
    # 没有审批卡时「同意」只是一句附和，不能去点一个不存在的按钮。
    assert observe("同意", active_task_status="running").intent == INTENT_ANSWER
    # 没有旧目标时「继续」不能假装恢复了什么。
    assert observe("继续", has_active_goal=False).intent == INTENT_ANSWER


def test_discussion_is_not_execution_and_natural_language_is_a_goal():
    assert observe("你觉得这个界面有什么可以优化的").intent == INTENT_INSPECT
    assert observe("能不能做个官网").intent == INTENT_ANSWER
    assert observe("帮我看一下 README").intent == INTENT_INSPECT
    assert observe("把登录页的按钮改成蓝色").intent == INTENT_EXECUTE
    # 一句自然语言默认就是目标，不要求用户再点一次「开始执行」。
    assert observe("做一个能收简历的官网").intent == INTENT_EXECUTE


def test_followup_stays_on_the_same_goal_and_new_goal_is_explicit():
    assert observe("这个问题还没解决", has_active_goal=True).intent == INTENT_CONTINUE
    assert observe("再测试一下", has_active_goal=True).intent == INTENT_CONTINUE
    assert observe("按刚才的要求继续", has_active_goal=True).intent == INTENT_CONTINUE
    assert wants_new_goal("换个任务，做一个官网")
    assert not wants_new_goal("再改一下按钮颜色")


def test_supervisor_overrides_chat_misclassification_without_touching_the_goal_text():
    followup = observe("这个问题还没解决", has_active_goal=True).as_dict()
    assert execution_override(followup, None) == "act"
    assert execution_override(followup, INTENT_ANSWER) == "act"
    # 规则已经判成动手时不覆盖，避免两套判定互相打架。
    assert execution_override(followup, "act") is None
    assert execution_override(observe("你好").as_dict(), None) is None
    assert execution_override(observe("帮我看一下 README").as_dict(), None) == "advise"


def test_judgement_shape_matches_the_spec():
    j = observe("修复登录报错", has_active_goal=True).as_dict()
    for key in (
        "intent", "needs_execution", "needs_approval", "needs_clarification",
        "priority", "confidence", "reason", "scenario", "automation_mode",
        "goal_id", "related_task_id",
    ):
        assert key in j
    assert j["needs_approval"] is False  # 审批由权限层决定，监督者不越权放行


# --------------------------------------------------------------------------- #
# 落库与分流
# --------------------------------------------------------------------------- #

async def test_first_message_creates_a_goal_and_links_the_turn(client):
    project = await _project()
    thread = await _thread(project.id)
    response = await _turn(client, thread.id, "把登录页的按钮改成蓝色")
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]

    goal_response = await client.get(f"{API}/threads/{thread.id}/goal")
    assert goal_response.status_code == 200
    goal = goal_response.json()
    assert goal["title"] == "把登录页的按钮改成蓝色"
    assert goal["current_task_id"] == task_id
    assert goal["task_ids"] == [task_id]
    # Phase 2 Dispatcher：七条件下发齐备 → 立即执行，目标同步进 executing。
    assert goal["status"] == "executing"
    dispatch = response.json()["goal_spec"]["dispatch"]
    assert dispatch["auto_start"] is True and dispatch["decision"] == "immediate"
    assert dispatch["goal_id"] == goal["id"] and dispatch["task_id"] == task_id

    async with get_session_factory()() as session:
        events = (
            await session.execute(select(AgentEvent).where(AgentEvent.task_id == task_id))
        ).scalars().all()
        types = {item.type for item in events}
        stored = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()
        spec = dict(stored.goal_spec or {})
        goal_text = stored.goal
    assert {"message.observed", "goal.created", "goal.linked", "thread.turn"} <= types
    # 判定进结构化目标，不进用户原话：goal 字段必须还是用户那句话。
    assert spec["supervisor"]["intent"] == INTENT_EXECUTE
    assert spec["goal_id"] == goal["id"]
    assert goal_text == "把登录页的按钮改成蓝色"


async def test_second_message_reuses_the_same_goal(client):
    project = await _project()
    thread = await _thread(project.id)
    first = await _turn(client, thread.id, "做一个官网首页")
    first_id = first.json()["id"]
    goal_before = (await client.get(f"{API}/threads/{thread.id}/goal")).json()

    second = await _turn(client, thread.id, "再改一下配色")
    assert second.status_code == 201
    second_id = second.json()["id"]
    assert second_id != first_id

    goal_after = (await client.get(f"{API}/threads/{thread.id}/goal")).json()
    assert goal_after["id"] == goal_before["id"]
    assert goal_after["task_ids"] == [first_id, second_id]
    assert goal_after["current_task_id"] == second_id

    async with get_session_factory()() as session:
        count = (
            await session.execute(select(Goal).where(Goal.conversation_id == thread.id))
        ).scalars().all()
    assert len(count) == 1


async def test_continue_does_not_open_a_new_turn(client, monkeypatch):
    """上一轮失败时「继续」应回到那一轮，而不是再建一个空任务。"""
    from blank_api.api.routes import tasks as task_routes

    async def _no_launch(db, task, payload, request, **kwargs):
        # 只验证路由分流，不在单测里真的拉起后台 runner。
        return {}

    monkeypatch.setattr(task_routes, "launch_task", _no_launch)

    project = await _project()
    thread = await _thread(project.id)
    first = await _turn(client, thread.id, "修复登录报错")
    task_id = first.json()["id"]
    async with get_session_factory()() as session:
        task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()
        task.status = "failed"
        task.failure_reason = "测试未通过"
        await session.commit()

    continued = await _turn(client, thread.id, "继续")
    assert continued.status_code == 201, continued.text
    assert continued.json()["id"] == task_id  # 复用，不是新一轮

    async with get_session_factory()() as session:
        await session.refresh(
            (await session.execute(select(Thread).where(Thread.id == thread.id))).scalar_one()
        )
        thread_row = (
            await session.execute(select(Thread).where(Thread.id == thread.id))
        ).scalar_one()
        assert int(thread_row.turn_count or 0) == 1
        events = (
            await session.execute(select(AgentEvent).where(AgentEvent.task_id == task_id))
        ).scalars().all()
        assert "supervisor.action" in {item.type for item in events}


async def test_explicit_new_goal_archives_the_old_one(client):
    project = await _project()
    thread = await _thread(project.id)
    await _turn(client, thread.id, "做一个官网")
    await _turn(client, thread.id, "换个任务，写一个命令行工具")

    async with get_session_factory()() as session:
        goals = (
            await session.execute(select(Goal).where(Goal.conversation_id == thread.id))
        ).scalars().all()
    assert len(goals) == 2
    assert {item.status for item in goals} == {"archived", "executing"}


async def test_unbound_workspace_stops_the_dispatcher_before_any_run(client):
    """§5.1 反例：没绑工作区时 /start 必然 409，不能先自动跑再让用户看见失败。"""
    project = await _project(with_workspace=False)
    thread = await _thread(project.id)
    response = await _turn(client, thread.id, "做一个官网")
    assert response.status_code == 201, response.text
    dispatch = response.json()["goal_spec"]["dispatch"]
    assert dispatch["decision"] == "wait_user" and dispatch["auto_start"] is False
    assert dispatch["conditions"]["workspace_bound"] is False
    assert "工作区" in dispatch["reasons"][0]
    goal = (await client.get(f"{API}/threads/{thread.id}/goal")).json()
    assert goal["status"] == "waiting_user"


async def test_completed_goal_is_still_readable_by_the_status_bar(client):
    """任务成功后状态条不能把目标格清空（真机撞到过：33 秒跑完，目标同时消失）。

    展示口径用 ``latest()``（含终态），新建/复用口径仍用 ``active()``（不含终态）。
    """
    from blank_api.services.agent.goals import GoalTracker

    project = await _project()
    thread = await _thread(project.id)
    created = (await _turn(client, thread.id, "做一个官网")).json()
    goal_id = created["goal_spec"]["goal_id"]
    assert goal_id

    async with get_session_factory()() as session:
        goal = await session.get(Goal, goal_id)
        goal.status = "succeeded"
        await session.commit()

    shown = (await client.get(f"{API}/threads/{thread.id}/goal")).json()
    assert shown is not None and shown["id"] == goal_id
    assert shown["status"] == "succeeded"

    async with get_session_factory()() as session:
        # 决策侧不受影响：已结束的目标不再黏住下一句话。
        assert await GoalTracker(session).active(thread.id) is None

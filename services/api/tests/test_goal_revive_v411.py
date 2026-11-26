# -*- coding: utf-8 -*-
"""恢复执行时目标状态回写。

真机：模型问完问题、用户答完（或超时用默认值），任务已经继续跑，状态条却整轮
显示「等你批准」——目标停在中断那一刻的 waiting_approval，恢复路径没人改回来。
"""
from __future__ import annotations

import uuid

from blank_api.db import get_session_factory
from blank_api.models.goal import Goal
from blank_api.models.task import Task
from blank_api.services.agent.runtime import AgentRuntime
from tests.test_v165_supervisor import _project, _thread


async def _goal_with_task(status: str) -> tuple[str, str]:
    project = await _project()
    thread = await _thread(project.id)
    goal_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    async with get_session_factory()() as session:
        session.add(
            Goal(
                id=goal_id,
                project_id=project.id,
                conversation_id=thread.id,
                title="给我写一个项目计划书",
                status=status,
            )
        )
        session.add(
            Task(
                id=task_id,
                project_id=project.id,
                thread_id=thread.id,
                title="给我写一个项目计划书",
                goal="给我写一个项目计划书",
                status="running",
                goal_spec={"goal_id": goal_id},
            )
        )
        await session.commit()
    return goal_id, task_id


async def test_revive_moves_waiting_approval_goal_back_to_executing(client):
    goal_id, task_id = await _goal_with_task("waiting_approval")
    await AgentRuntime(get_session_factory())._revive_goal(task_id)
    async with get_session_factory()() as session:
        goal = await session.get(Goal, goal_id)
        assert goal is not None and goal.status == "executing"


async def test_revive_leaves_other_statuses_alone(client):
    goal_id, task_id = await _goal_with_task("succeeded")
    await AgentRuntime(get_session_factory())._revive_goal(task_id)
    async with get_session_factory()() as session:
        goal = await session.get(Goal, goal_id)
        assert goal is not None and goal.status == "succeeded"

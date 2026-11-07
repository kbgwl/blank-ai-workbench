"""Goal Tracker：目标的新建、关联与状态迁移（v165 §3）。

刻意不在这里写事件、不 commit：事件要挂在**具体的那一轮任务**上（事件表按 task
组织），事务边界也应该由路由层统一收口。这样跟踪器只是纯粹的持久化读写，单测里
可以直接拿一个 session 用，不需要起半个应用。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from blank_api.models.goal import Goal, TERMINAL_GOAL_STATUSES
from blank_api.models.task import Task


class GoalTracker:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def active(self, conversation_id: str) -> Goal | None:
        """这条会话当前活跃的目标；没有就返回 None。

        只认非终态。用户说「算了」之后旧目标被取消，下一句话就该是新目标，
        不能被一条已经结束的目标继续黏住。
        """
        rows = await self.db.execute(
            select(Goal)
            .where(
                Goal.conversation_id == conversation_id,
                Goal.status.notin_(sorted(TERMINAL_GOAL_STATUSES)),
            )
            .order_by(Goal.created_at.desc())
            .limit(1)
        )
        return rows.scalars().first()

    async def latest(self, conversation_id: str) -> Goal | None:
        """这条会话最近的一个目标，**含已结束的**。

        只给「展示」用，不参与决策：状态条要在任务成功/失败之后继续显示
        「目标 · 已完成」，否则最该被看见的那一刻目标恰好消失（真机撞到过：
        任务 33 秒跑完，状态条上的目标格同时不见了）。新建/复用目标仍然必须走
        ``active()``——那条要的正是「终止态不黏人」。
        """
        rows = await self.db.execute(
            select(Goal)
            .where(Goal.conversation_id == conversation_id)
            .order_by(Goal.created_at.desc())
            .limit(1)
        )
        return rows.scalars().first()

    async def create(
        self,
        *,
        project_id: str,
        conversation_id: str,
        title: str,
        scenario: str = "dev",
        automation_mode: str = "assist",
        priority: str = "normal",
        summary: str = "",
    ) -> Goal:
        goal = Goal(
            id=str(uuid.uuid4()),
            project_id=project_id,
            conversation_id=conversation_id,
            title=(title or "未命名目标")[:200],
            summary=summary,
            scenario=scenario or "dev",
            automation_mode=automation_mode or "assist",
            priority=priority or "normal",
            status="captured",
            last_active_at=datetime.now(),
        )
        self.db.add(goal)
        return goal

    def attach(self, goal: Goal, task: Task, *, status: str = "executing") -> None:
        """把一轮任务挂到目标上。append 顺序就是轮次顺序，不用再按秒级时间戳排。"""
        ids = [str(item) for item in (goal.task_ids or []) if item]
        if task.id not in ids:
            ids.append(task.id)
        goal.task_ids = ids
        goal.current_task_id = task.id
        goal.status = status
        goal.last_active_at = datetime.now()
        if not goal.summary:
            goal.summary = (task.goal or task.title or "")[:2000]

    async def latest_task(self, goal: Goal | None) -> Task | None:
        if goal is None:
            return None
        task_id = str(goal.current_task_id or "")
        if not task_id:
            ids = [str(item) for item in (goal.task_ids or []) if item]
            task_id = ids[-1] if ids else ""
        if not task_id:
            return None
        rows = await self.db.execute(select(Task).where(Task.id == task_id))
        return rows.scalars().first()

    @staticmethod
    def transition(goal: Goal, status: str, **fields: object) -> None:
        """改状态并按需带几个字段；未知字段直接拒绝，避免悄悄拼错列名。"""
        allowed = {
            "summary", "blocked_reason", "next_action", "priority", "scenario",
            "automation_mode", "steps_done", "steps_pending", "steps_failed",
            "tokens_used", "budget_tokens",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"未知目标字段：{sorted(unknown)}")
        goal.status = status
        for key, value in fields.items():
            setattr(goal, key, value)
        goal.last_active_at = datetime.now()

    @staticmethod
    def as_dict(goal: Goal | None) -> dict:
        if goal is None:
            return {}
        return {
            "goal_id": goal.id,
            "conversation_id": goal.conversation_id,
            "title": goal.title,
            "summary": goal.summary,
            "status": goal.status,
            "scenario": goal.scenario,
            "automation_mode": goal.automation_mode,
            "priority": goal.priority,
            "steps_done": list(goal.steps_done or []),
            "steps_pending": list(goal.steps_pending or []),
            "steps_failed": list(goal.steps_failed or []),
            "blocked_reason": goal.blocked_reason,
            "next_action": goal.next_action,
            "current_task_id": goal.current_task_id,
            "task_ids": list(goal.task_ids or []),
            "budget_tokens": int(goal.budget_tokens or 0),
            "tokens_used": int(goal.tokens_used or 0),
        }

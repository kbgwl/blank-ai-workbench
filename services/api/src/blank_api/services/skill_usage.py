"""技能级用量归因：回答"这条技能最近有没有被用、花了多少 token"。

为什么需要它
------------
技能清单是每轮都在付的固定成本（一行一条），所以"装了不用"和"装得太多"都是
真实的浪费。Claude Code 的 `/skills` 列表里每条都带 `7d tokens`——用量是判断
"这条该不该留"的唯一依据；我们此前只有总量账本，看不出是哪条技能花掉的。

口径（刻意保守，宁可少算也不要虚高）
----------------------------------
- **调用**：`skill.invoked` 事件（技能正文真正被加载的那一刻，由 skill 工具写入）。
  只进清单不算调用——那只是名字在系统提示里占了一行。
- **任务**：去重后的 task 数。一次任务里同一技能重复调用只算一次任务。
- **token**：这些任务在 `model_usage` 里的总量（prompt + completion）。
  这是"**带着这条技能干完一次活**"的整体成本，不是技能正文自身的长度——
  后者早就由 `skill.invoked` 的 `chars` 记着了。任务里还有别的工作，所以这个数
  只能横向比"哪条技能的任务更贵"，不能当成技能的净开销。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from blank_api.models.model_provider import ModelUsage
from blank_api.models.task import AgentEvent

# 单次查询按这个大小切块：SQLite 的参数上限是 999，任务多时会撞上。
_TASK_CHUNK = 400


@dataclass(frozen=True)
class SkillUsage:
    calls: int
    tasks: int
    tokens: int
    last_used: str

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "tasks": self.tasks,
            "tokens": self.tokens,
            "last_used": self.last_used,
        }


async def skill_usage(db: AsyncSession, *, days: int = 7, now: datetime | None = None) -> dict[str, SkillUsage]:
    """按技能归集最近 ``days`` 天的调用与 token。没有调用的技能不出现在结果里。"""
    cutoff = (now or datetime.now()) - timedelta(days=max(1, days))
    rows = (
        await db.execute(
            select(AgentEvent.task_id, AgentEvent.payload, AgentEvent.created_at)
            .where(AgentEvent.type == "skill.invoked")
            .where(AgentEvent.created_at >= cutoff)
        )
    ).all()

    calls: dict[str, int] = {}
    last: dict[str, datetime] = {}
    tasks: dict[str, set[str]] = {}
    for task_id, payload, created_at in rows:
        name = ""
        if isinstance(payload, dict):
            name = str(payload.get("skill") or "").strip()
        if not name:
            continue
        calls[name] = calls.get(name, 0) + 1
        if created_at is not None and (name not in last or created_at > last[name]):
            last[name] = created_at
        if task_id:
            tasks.setdefault(name, set()).add(task_id)

    if not tasks:
        return {}

    # 任务 → 技能是多对多（一次任务可能加载两条技能），所以按任务求总量后，
    # 给每个用到它的技能都记上一份。**不要按任务切分**：技能正文本身没有独立
    # 的调用账，切出来的数字是编的。
    all_tasks = sorted({task for group in tasks.values() for task in group})
    tokens_by_task: dict[str, int] = {}
    for start in range(0, len(all_tasks), _TASK_CHUNK):
        chunk = all_tasks[start : start + _TASK_CHUNK]
        usage_rows = (
            await db.execute(
                select(
                    ModelUsage.task_id,
                    ModelUsage.prompt_tokens,
                    ModelUsage.completion_tokens,
                ).where(ModelUsage.task_id.in_(chunk))
            )
        ).all()
        for task_id, prompt, completion in usage_rows:
            if not task_id:
                continue
            tokens_by_task[task_id] = tokens_by_task.get(task_id, 0) + int(prompt or 0) + int(completion or 0)

    result: dict[str, SkillUsage] = {}
    for name, group in tasks.items():
        result[name] = SkillUsage(
            calls=calls.get(name, 0),
            tasks=len(group),
            tokens=sum(tokens_by_task.get(task_id, 0) for task_id in group),
            last_used=last[name].isoformat(sep=" ", timespec="seconds") if name in last else "",
        )
    return result

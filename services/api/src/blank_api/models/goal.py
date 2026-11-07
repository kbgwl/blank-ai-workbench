"""长期目标（v165 §3）。

一条会话同一时间只保留一个活跃目标：用户的「继续 / 再改一下 / 这个问题还没解决」
都挂回它，而不是每来一句就新起一个目标。任务仍是「一轮一行」，目标负责把这些轮
串成一件持续的事——所以外键方向是 goal 存 task_ids，不去动 tasks 表结构。
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from blank_api.db import Base

GOAL_STATUSES = (
    "captured", "clarifying", "planned", "executing", "waiting_approval",
    "waiting_user", "verifying", "repairing", "succeeded", "failed",
    "paused", "cancelled", "archived",
)
TERMINAL_GOAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "archived"})


class Goal(Base):
    __tablename__ = "goals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 会话 id（threads.id）。v165 里叫 conversation_id，实际指向的就是现有的会话。
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("('')"))
    scenario: Mapped[str] = mapped_column(String(20), nullable=False, default="dev", server_default="dev")
    automation_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="assist", server_default="assist"
    )
    priority: Mapped[str] = mapped_column(
        String(10), nullable=False, default="normal", server_default="normal"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="captured", server_default="captured", index=True
    )
    steps_done: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default=text("('[]')"))
    steps_pending: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default=text("('[]')"))
    steps_failed: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default=text("('[]')"))
    blocked_reason: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("('')"))
    next_action: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("('')"))
    current_task_id: Mapped[str] = mapped_column(String(36), nullable=False, default="", server_default="")
    task_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default=text("('[]')"))
    run_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default=text("('[]')"))
    budget_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_active_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

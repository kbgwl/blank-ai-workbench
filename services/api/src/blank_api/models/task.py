from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from blank_api.db import Base
from blank_api.limits import DEFAULT_MAX_STEPS

TASK_STATUSES = (
    "draft",
    "queued",
    "running",
    "waiting_approval",
    "paused",
    "succeeded",
    "failed",
    "cancelled",
)
RISK_LEVELS = ("low", "medium", "high", "critical")
TOOL_CALL_STATUSES = ("ok", "error", "denied", "timeout", "awaiting_approval")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # 所属会话（多轮对话）。为空表示这是一次独立任务，行为与没有会话时完全一致。
    # ON DELETE SET NULL：删会话不该连任务一起删——那些任务的执行记录是审计材料。
    # 名字与迁移 0014 的 `create_foreign_key("fk_tasks_thread_id", ...)` 对齐：不写名字时
    # MySQL 会自动起 `tasks_ibfk_2`、PG 会起 `tasks_thread_id_fkey`，按名字 DROP 的脚本会扑空。
    thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("threads.id", name="fk_tasks_thread_id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 会话内第几轮，从 1 开始；独立任务恒为 0。
    # 不靠 created_at 排序：SQLite 的 CURRENT_TIMESTAMP 只到秒，同一秒里连开两轮时
    # 顺序是随机的，而"第几轮"决定了历史里谁在前——顺序错了，模型读到的对话是乱的。
    turn_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    goal: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("('')")
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="draft", server_default="draft"
    )
    acceptance_criteria: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    acceptance_results: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 用户这一轮贴进来的图片（{id, name, mime, size}）。文件本体在 data/uploads 下，
    # 这里只存引用：图片进库会让备份和迁移一起变重，而它们从来不需要被 SQL 查询。
    # `text("('[]')")` 不是口味：MySQL 8.4 拒绝给 JSON/TEXT 列写字面量默认值（1101），
    # 只有 8.0.13+ 的表达式默认值这一种写法两种方言都吃。
    attachments: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("('[]')")
    )
    # 迁移里的服务端默认值仍是 8（供裸 SQL 用）；应用插入走 Python default=200。
    max_steps: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_MAX_STEPS, server_default="8"
    )
    steps_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    result_summary: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("('')")
    )
    failure_reason: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("('')")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class AgentEvent(Base):
    __tablename__ = "agent_events"
    __table_args__ = (UniqueConstraint("task_id", "seq", name="uq_agent_events_task_seq"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[str] = mapped_column(
        String(120), nullable=False, default="", server_default="", index=True
    )
    # 发起轮：引起这一整棵任务树的那一轮。和账本 `model_usage.origin_run_id` 同源同宽；
    # 空串只读作"这一行上线前就存在，当时没记"，不许拿 task_id/run_id 顶上。
    origin_run_id: Mapped[str] = mapped_column(
        String(120), nullable=False, default="", server_default="", index=True
    )
    step_id: Mapped[str] = mapped_column(
        String(36), nullable=False, default="", server_default=""
    )
    parent_event_id: Mapped[str] = mapped_column(
        String(36), nullable=False, default="", server_default=""
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    node: Mapped[str] = mapped_column(
        String(60), nullable=False, default="", server_default=""
    )
    type: Mapped[str] = mapped_column(String(60), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    trace_id: Mapped[str] = mapped_column(
        String(120), nullable=False, default="", server_default="", index=True
    )
    node: Mapped[str] = mapped_column(
        String(60), nullable=False, default="", server_default=""
    )
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result_summary: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("('')")
    )
    artifact_path: Mapped[str] = mapped_column(
        String(1000), nullable=False, default="", server_default=""
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="ok", server_default="ok"
    )
    risk_level: Mapped[str] = mapped_column(
        String(20), nullable=False, default="low", server_default="low"
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class FileChange(Base):
    __tablename__ = "file_changes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_call_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    change_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="modified", server_default="modified"
    )
    diff: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("('')")
    )
    backup_path: Mapped[str] = mapped_column(
        String(1000), nullable=False, default="", server_default=""
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

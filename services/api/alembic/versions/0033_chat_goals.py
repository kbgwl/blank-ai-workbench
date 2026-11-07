"""长期目标表（v165 §3 Goal Tracker）

Revision ID: 0033_chat_goals
Revises: 0032_agent_triggers

## 为什么改

v0.9.1 是「一句话 = 一个任务」，用户说「继续」「再改一下」时后端照样新建一轮，
旧目标、已完成步骤和失败原因全都留在上一轮里，于是只能靠用户不停重说。
目标表把同一会话的多轮任务串成一件持续的事，是 Chat Supervisor 的持久化前提。

## 改了什么

新增表 `goals`：目标原文与摘要、会话/项目归属、场景与自动化模式、状态、
已完成/待办/失败步骤、阻塞原因与下一步、关联 task/run、预算与用量、活动时间。
不动 `tasks` 表——关联方向是 goal 存 task_ids，存量行零影响。

## 兼容

带存在性检查（与 0028–0032 同口径）：表已在时跳过。
"""
from alembic import op
import sqlalchemy as sa

revision = "0033_chat_goals"
down_revision = "0032_agent_triggers"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "goals" in _tables():
        return
    op.create_table(
        "goals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=sa.text("('')")),
        sa.Column("scenario", sa.String(20), nullable=False, server_default="dev"),
        sa.Column("automation_mode", sa.String(20), nullable=False, server_default="assist"),
        sa.Column("priority", sa.String(10), nullable=False, server_default="normal"),
        sa.Column("status", sa.String(20), nullable=False, server_default="captured"),
        sa.Column("steps_done", sa.JSON(), nullable=False, server_default=sa.text("('[]')")),
        sa.Column("steps_pending", sa.JSON(), nullable=False, server_default=sa.text("('[]')")),
        sa.Column("steps_failed", sa.JSON(), nullable=False, server_default=sa.text("('[]')")),
        sa.Column("blocked_reason", sa.Text(), nullable=False, server_default=sa.text("('')")),
        sa.Column("next_action", sa.Text(), nullable=False, server_default=sa.text("('')")),
        sa.Column("current_task_id", sa.String(36), nullable=False, server_default=""),
        sa.Column("task_ids", sa.JSON(), nullable=False, server_default=sa.text("('[]')")),
        sa.Column("run_ids", sa.JSON(), nullable=False, server_default=sa.text("('[]')")),
        sa.Column("budget_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_active_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_goals_project_id", "goals", ["project_id"])
    op.create_index("ix_goals_conversation_id", "goals", ["conversation_id"])
    op.create_index("ix_goals_status", "goals", ["status"])


def downgrade() -> None:
    if "goals" in _tables():
        op.drop_table("goals")

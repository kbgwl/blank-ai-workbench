"""事件触发器表（v162 Super Agent §5）

Revision ID: 0032_agent_triggers
Revises: 0031_dispatch_snapshot

## 为什么改

去重必须由数据库保证。只靠"先查有没有、再插一条"，两个触发源同时上报时会双双
查到空、双双插入——重复事件生成两条任务，而这正是后台执行最容易失控的地方。
唯一键 `(project_id, dedupe_key)` 让"同一件事只开一条任务"成为可验证的事实。

## 改了什么

新增表 `agent_triggers`：事件类型/来源/载荷/去重键/关联任务/触发时间/有效期/状态。
行只改状态不删除：过期与跳过本身是"这件事没被处理"的证据。

## 兼容

带存在性检查（与 0028–0031 同口径）：表已在时跳过。
"""
from alembic import op
import sqlalchemy as sa

revision = "0032_agent_triggers"
down_revision = "0031_dispatch_snapshot"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "agent_triggers" in _tables():
        return
    op.create_table(
        "agent_triggers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("source", sa.String(120), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("('{}')")),
        sa.Column("dedupe_key", sa.String(120), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=False, server_default=""),
        sa.Column("provider_id", sa.String(36), nullable=False, server_default=""),
        sa.Column("run_payload", sa.JSON(), nullable=False, server_default=sa.text("('{}')")),
        sa.Column("fire_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("fired_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "dedupe_key", name="uq_agent_triggers_dedupe"),
    )
    op.create_index("ix_agent_triggers_project_id", "agent_triggers", ["project_id"])
    op.create_index("ix_agent_triggers_fire_at", "agent_triggers", ["fire_at"])
    op.create_index("ix_agent_triggers_expires_at", "agent_triggers", ["expires_at"])
    op.create_index("ix_agent_triggers_status", "agent_triggers", ["status"])


def downgrade() -> None:
    if "agent_triggers" in _tables():
        op.drop_table("agent_triggers")

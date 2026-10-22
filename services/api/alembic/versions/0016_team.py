"""team bots, groups, rounds and messages

Revision ID: 0016_team
Revises: 0015_task_attachments

团队模式没有另起一套执行引擎：每个 Bot 在群里有一条隐藏会话（threads.kind=team），
每次发言仍然写一条普通 Task。因此这一版只加数据形状，不改任务运行路径。

threads.kind 默认 "chat"：老数据自动成为用户对话，不会突然出现在团队列表里，
团队会话也不会污染原来的对话侧栏。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_team"
down_revision: Union[str, None] = "0015_task_attachments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("threads") as batch:
        batch.add_column(
            sa.Column("kind", sa.String(length=16), nullable=False, server_default="chat")
        )

    op.create_table(
        "team_bots",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=24), nullable=False, unique=True),
        sa.Column("role", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("instructions", sa.Text(), nullable=False, server_default=""),
        sa.Column("provider_id", sa.String(length=36), nullable=True),
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        sa.Column("permission_mode", sa.String(length=20), nullable=False, server_default="default"),
        sa.Column("max_steps", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["provider_id"], ["model_providers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_team_bots_provider_id", "team_bots", ["provider_id"])

    op.create_table(
        "team_groups",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False, server_default=""),
        sa.Column("admin_bot_id", sa.String(length=36), nullable=True),
        sa.Column("max_speakers", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("token_budget", sa.Integer(), nullable=False, server_default="60000"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["admin_bot_id"], ["team_bots.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_team_groups_project_id", "team_groups", ["project_id"])

    op.create_table(
        "team_members",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("group_id", sa.String(length=36), nullable=False),
        sa.Column("bot_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("thread_id", sa.String(length=36), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["group_id"], ["team_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["bot_id"], ["team_bots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("group_id", "bot_id", name="uq_team_members_group_bot"),
    )
    op.create_index("ix_team_members_group_id", "team_members", ["group_id"])
    op.create_index("ix_team_members_bot_id", "team_members", ["bot_id"])

    op.create_table(
        "team_rounds",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("group_id", sa.String(length=36), nullable=False),
        sa.Column("user_message_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="running"),
        sa.Column("queue", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("spoken", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wrap_up_done", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["group_id"], ["team_groups.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_team_rounds_group_id", "team_rounds", ["group_id"])
    op.create_index("ix_team_rounds_status", "team_rounds", ["status"])

    op.create_table(
        "team_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("group_id", sa.String(length=36), nullable=False),
        sa.Column("round_id", sa.String(length=36), nullable=True),
        sa.Column("role", sa.String(length=12), nullable=False),
        sa.Column("bot_id", sa.String(length=36), nullable=True),
        sa.Column("bot_name", sa.String(length=24), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["group_id"], ["team_groups.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_team_messages_group_id", "team_messages", ["group_id"])
    op.create_index("ix_team_messages_round_id", "team_messages", ["round_id"])
    op.create_index("ix_team_messages_bot_id", "team_messages", ["bot_id"])
    op.create_index("ix_team_messages_task_id", "team_messages", ["task_id"])


def downgrade() -> None:
    op.drop_table("team_messages")
    op.drop_table("team_rounds")
    op.drop_table("team_members")
    op.drop_table("team_groups")
    op.drop_table("team_bots")
    with op.batch_alter_table("threads") as batch:
        batch.drop_column("kind")

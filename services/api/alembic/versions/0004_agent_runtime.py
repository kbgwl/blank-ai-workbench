"""tasks, agent_events, tool_calls, file_changes and LangGraph checkpoint tables

Revision ID: 0004_agent_runtime
Revises: 0003_models
Create Date: 2026-10-15

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0004_agent_runtime"
down_revision: Union[str, None] = "0003_models"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_id", sa.String(36), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False, server_default=sa.text("('')")),
        sa.Column("status", sa.String(30), nullable=False, server_default="draft"),
        sa.Column("acceptance_criteria", sa.JSON(), nullable=False),
        sa.Column("acceptance_results", sa.JSON(), nullable=False),
        sa.Column("max_steps", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("steps_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result_summary", sa.Text(), nullable=False, server_default=sa.text("('')")),
        sa.Column("failure_reason", sa.Text(), nullable=False, server_default=sa.text("('')")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_tasks_project_id", "tasks", ["project_id"])
    op.create_index("ix_tasks_provider_id", "tasks", ["provider_id"])

    op.create_table(
        "agent_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("node", sa.String(60), nullable=False, server_default=""),
        sa.Column("type", sa.String(60), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("task_id", "seq", name="uq_agent_events_task_seq"),
    )
    op.create_index("ix_agent_events_task_id", "agent_events", ["task_id"])

    op.create_table(
        "tool_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("node", sa.String(60), nullable=False, server_default=""),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("result_summary", sa.Text(), nullable=False, server_default=sa.text("('')")),
        sa.Column("status", sa.String(30), nullable=False, server_default="ok"),
        sa.Column("risk_level", sa.String(20), nullable=False, server_default="low"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_tool_calls_task_id", "tool_calls", ["task_id"])

    op.create_table(
        "file_changes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool_call_id", sa.String(36), nullable=True),
        sa.Column("path", sa.String(1000), nullable=False),
        sa.Column("change_type", sa.String(20), nullable=False, server_default="modified"),
        sa.Column("diff", sa.Text(), nullable=False, server_default=sa.text("('')")),
        sa.Column("backup_path", sa.String(1000), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_file_changes_task_id", "file_changes", ["task_id"])

    op.create_table(
        "agent_checkpoints",
        sa.Column("thread_id", sa.String(120), primary_key=True),
        sa.Column("checkpoint_ns", sa.String(120), primary_key=True, server_default=""),
        sa.Column("checkpoint_id", sa.String(80), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("parent_checkpoint_id", sa.String(80), nullable=True),
        sa.Column("serde_type", sa.String(40), nullable=False, server_default="msgpack"),
        sa.Column("checkpoint", sa.LargeBinary(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_agent_checkpoints_task_id", "agent_checkpoints", ["task_id"])

    op.create_table(
        "agent_checkpoint_writes",
        sa.Column("thread_id", sa.String(120), primary_key=True),
        sa.Column("checkpoint_ns", sa.String(120), primary_key=True, server_default=""),
        sa.Column("checkpoint_id", sa.String(80), primary_key=True),
        sa.Column("write_task_id", sa.String(80), primary_key=True),
        sa.Column("idx", sa.Integer(), primary_key=True),
        sa.Column("task_path", sa.String(200), nullable=False, server_default=""),
        sa.Column("channel", sa.String(120), nullable=False),
        sa.Column("serde_type", sa.String(40), nullable=False, server_default="msgpack"),
        sa.Column("value", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("agent_checkpoint_writes")
    op.drop_index("ix_agent_checkpoints_task_id", table_name="agent_checkpoints")
    op.drop_table("agent_checkpoints")
    op.drop_index("ix_file_changes_task_id", table_name="file_changes")
    op.drop_table("file_changes")
    op.drop_index("ix_tool_calls_task_id", table_name="tool_calls")
    op.drop_table("tool_calls")
    op.drop_index("ix_agent_events_task_id", table_name="agent_events")
    op.drop_table("agent_events")
    op.drop_index("ix_tasks_provider_id", table_name="tasks")
    op.drop_index("ix_tasks_project_id", table_name="tasks")
    op.drop_table("tasks")

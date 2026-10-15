"""model_providers and model_usage tables

Revision ID: 0003_models
Revises: 0002_workspaces
Create Date: 2026-10-15

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0003_models"
down_revision: Union[str, None] = "0002_workspaces"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "model_providers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("base_url", sa.String(500), nullable=False),
        sa.Column("api_key_encrypted", sa.String(1000), nullable=False, server_default=""),
        sa.Column("default_model", sa.String(200), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=False, server_default="0.2"),
        sa.Column("max_tokens", sa.Integer(), nullable=False, server_default="2048"),
        sa.Column("context_window", sa.Integer(), nullable=False, server_default="128000"),
        sa.Column("supports_tools", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("supports_vision", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        # SQLite 不支持 ALTER TABLE ADD CONSTRAINT，唯一约束必须随建表一起声明。
        sa.UniqueConstraint("name", name="uq_model_providers_name"),
    )
    op.create_table(
        "model_usage",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider_id", sa.String(36), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=True),
        sa.Column("node", sa.String(60), nullable=False, server_default="chat"),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="ok"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["provider_id"], ["model_providers.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_model_usage_provider_id", "model_usage", ["provider_id"])
    op.create_index("ix_model_usage_task_id", "model_usage", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_model_usage_task_id", table_name="model_usage")
    op.drop_index("ix_model_usage_provider_id", table_name="model_usage")
    op.drop_table("model_usage")
    op.drop_table("model_providers")

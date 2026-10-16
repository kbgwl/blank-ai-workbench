"""trace IDs for model and tool observability

Revision ID: 0005_trace_ids
Revises: 0004_agent_runtime
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_trace_ids"
down_revision: Union[str, None] = "0004_agent_runtime"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tool_calls", sa.Column("trace_id", sa.String(120), nullable=False, server_default=""))
    op.create_index("ix_tool_calls_trace_id", "tool_calls", ["trace_id"])
    op.add_column("model_usage", sa.Column("trace_id", sa.String(120), nullable=False, server_default=""))
    op.create_index("ix_model_usage_trace_id", "model_usage", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_model_usage_trace_id", table_name="model_usage")
    op.drop_column("model_usage", "trace_id")
    op.drop_index("ix_tool_calls_trace_id", table_name="tool_calls")
    op.drop_column("tool_calls", "trace_id")

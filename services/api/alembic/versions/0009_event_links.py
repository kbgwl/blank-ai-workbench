"""link events to runs and parent steps

Revision ID: 0009_event_links
Revises: 0008_run_request_payload
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_event_links"
down_revision: Union[str, None] = "0008_run_request_payload"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_events", sa.Column("run_id", sa.String(36), nullable=False, server_default=""))
    op.add_column("agent_events", sa.Column("step_id", sa.String(36), nullable=False, server_default=""))
    op.add_column("agent_events", sa.Column("parent_event_id", sa.String(36), nullable=False, server_default=""))
    op.create_index("ix_agent_events_run_id", "agent_events", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_events_run_id", table_name="agent_events")
    op.drop_column("agent_events", "parent_event_id")
    op.drop_column("agent_events", "step_id")
    op.drop_column("agent_events", "run_id")

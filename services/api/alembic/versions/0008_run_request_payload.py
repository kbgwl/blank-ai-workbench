"""persist the exact run request for restart recovery

Revision ID: 0008_run_request_payload
Revises: 0007_agent_runs
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_run_request_payload"
down_revision: Union[str, None] = "0007_agent_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("request_payload", sa.JSON(), nullable=False, server_default=sa.text("('{}')")))


def downgrade() -> None:
    op.drop_column("agent_runs", "request_payload")

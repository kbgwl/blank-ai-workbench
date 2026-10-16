"""store paths for complete tool output artifacts

Revision ID: 0010_tool_artifacts
Revises: 0009_event_links
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_tool_artifacts"
down_revision: Union[str, None] = "0009_event_links"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tool_calls", sa.Column("artifact_path", sa.String(1000), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("tool_calls", "artifact_path")

"""memory candidates and reviewed skills

Revision ID: 0006_skills
Revises: 0005_trace_ids
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_skills"
down_revision: Union[str, None] = "0005_trace_ids"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "memory_candidates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source", sa.String(120), nullable=False, server_default="daily"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "skills",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_candidate_id", sa.String(36), sa.ForeignKey("memory_candidates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("skills")
    op.drop_table("memory_candidates")

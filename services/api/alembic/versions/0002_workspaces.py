"""workspaces table and project metadata columns

Revision ID: 0002_workspaces
Revises: 0001_projects
Create Date: 2026-10-15

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0002_workspaces"
down_revision: Union[str, None] = "0001_projects"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("('')")))
    op.add_column(
        "projects",
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.add_column(
        "projects",
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.String(200), nullable=False, server_default=""),
        sa.Column("path", sa.String(1000), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_workspaces_project_id", "workspaces", ["project_id"])
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("workspace_path")


def downgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("workspace_path", sa.String(1000), nullable=False, server_default=""),
    )
    op.drop_index("ix_workspaces_project_id", table_name="workspaces")
    op.drop_table("workspaces")
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("updated_at")
        batch.drop_column("created_at")
        batch.drop_column("description")

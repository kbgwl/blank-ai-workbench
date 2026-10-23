"""cross-group chat search index

Revision ID: 0017_search_docs
Revises: 0016_team

聊天检索不依赖 FTS5：索引表就是普通表，token 串由应用层生成，
SQLite / Postgres 用同一条 LIKE + 打分路径，搬库时迁移能照常跑。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_search_docs"
down_revision: Union[str, None] = "0016_team"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "search_docs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="team_message"),
        sa.Column("source_id", sa.String(length=80), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("group_id", sa.String(length=36), nullable=True),
        sa.Column("thread_id", sa.String(length=36), nullable=True),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("speaker", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("role", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False, server_default=sa.text("('')")),
        sa.Column("tokens", sa.Text(), nullable=False, server_default=sa.text("('')")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["team_groups.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("source", "source_id", name="uq_search_docs_source"),
    )
    op.create_index("ix_search_docs_project_id", "search_docs", ["project_id"])
    op.create_index("ix_search_docs_group_id", "search_docs", ["group_id"])
    op.create_index("ix_search_docs_thread_id", "search_docs", ["thread_id"])
    op.create_index("ix_search_docs_task_id", "search_docs", ["task_id"])
    op.create_index("ix_search_docs_message_id", "search_docs", ["message_id"])
    op.create_index(
        "ix_search_docs_project_group", "search_docs", ["project_id", "group_id"]
    )


def downgrade() -> None:
    op.drop_table("search_docs")

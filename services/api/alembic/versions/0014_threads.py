"""threads and tasks.thread_id

Revision ID: 0014_threads
Revises: 0013_provider_params

多轮会话要解决的不是"再存一遍消息"，而是"上一轮说过的话，这一轮还算数"。
所以只加两样东西：一张 threads 表（轮次计数与分叉来源），以及 tasks.thread_id
（这一轮属于哪条会话）。消息本身不复制——一轮的产物已经是它自己的 task。

tasks.thread_id 用 SET NULL 而不是 CASCADE：删会话是用户的整理动作，
不该顺带删掉那些轮次的执行记录，那是审计材料。

SQLite 不支持 ALTER TABLE 加外键，所以用 batch 模式重建表；库里数据量小，
重建的代价可以忽略，而"外键在 SQLite 上其实没建上"这种差异会一直潜伏到换库那天。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_threads"
down_revision: Union[str, None] = "0013_provider_params"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "threads",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("forked_from", sa.String(length=36), nullable=True),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_threads_project_id", "threads", ["project_id"])
    op.create_index("ix_threads_forked_from", "threads", ["forked_from"])

    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("thread_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("turn_index", sa.Integer(), nullable=False, server_default="0"))
        batch.create_foreign_key(
            "fk_tasks_thread_id", "threads", ["thread_id"], ["id"], ondelete="SET NULL"
        )
    op.create_index("ix_tasks_thread_id", "tasks", ["thread_id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_thread_id", table_name="tasks")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_constraint("fk_tasks_thread_id", type_="foreignkey")
        batch.drop_column("turn_index")
        batch.drop_column("thread_id")
    op.drop_index("ix_threads_forked_from", table_name="threads")
    op.drop_index("ix_threads_project_id", table_name="threads")
    op.drop_table("threads")

"""tasks.attachments

Revision ID: 0015_task_attachments
Revises: 0014_threads

用户贴进来的图片存在任务上，而不是单独一张消息表里。

理由：一轮任务就是一条消息。它"看到了什么"是这一轮的输入的一部分，回放、
复跑、审计都要能原样还原。存成 tasks 上的一个 JSON 数组，最少的结构改动就能
做到这一点；再开一张表要加外键、加联表查询，换来的只是"以后也许能按图片查"，
而目前没有任何地方需要那样查。

数组元素形状：{id, name, mime, size}。文件本体在 data/uploads/<project>/ 下，
库里只存引用——图片不该进数据库，备份和迁移都会跟着变重。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_task_attachments"
down_revision: Union[str, None] = "0014_threads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("attachments", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("tasks", "attachments")

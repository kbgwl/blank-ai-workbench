"""发起轮进账本：model_usage 补 origin_run_id，并把两处 run_id 放宽到 120

Revision ID: 0021_usage_origin_run_id
Revises: 0020_usage_parallel_group

团队链上实测出来的两件事，一次迁移收掉：

① 「这一轮群聊，几个成员合起来花了多少」问不出。一轮里每个发言成员都有自己的 task 和
   自己的 run_id（`team-{轮}-{发言}`），`run_id` 是"每人一个"的键，而轮只有一个值——
   按 run 筛只能一人一行地看，合起来的数只能靠编排器自己累加的 `TeamRound.tokens`，
   而那个计数器只在发言正常收尾时累加：中途失败的那次发言钱花了、数不计。
   origin_run_id 把整棵任务树的钱收在一个可按的键上（放行续跑的 `-resume` 也归同一轮）。

② `model_usage.run_id` 与 `agent_events.run_id` 声明 String(36)，实物最长 51 字符
   （`task:{uuid}:attempt:{n}`）。本地看不出问题：SQLite 根本不按 varchar 长度检查；
   换成 MySQL 或 PostgreSQL 就直接拒。列宽要跟着实物走，不然"换数据库才炸"。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021_usage_origin_run_id"
down_revision: Union[str, None] = "0020_usage_parallel_group"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 两处要放宽的列：建库时都写成 String(36) + server_default=""，模型现在都是 String(120)。
WIDENED = (("model_usage", "run_id"), ("agent_events", "run_id"))


def _widen(table: str, column: str) -> None:
    """按方言放宽一列。SQLite 走 batch（它是本项目的主开发库，必须真的能跑）。"""
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                column,
                type_=sa.String(length=120),
                existing_type=sa.String(length=36),
                existing_nullable=False,
                existing_server_default="",
            )
    else:
        op.alter_column(
            table,
            column,
            type_=sa.String(length=120),
            existing_type=sa.String(length=36),
            existing_nullable=False,
            existing_server_default="",
        )


def _narrow(table: str, column: str) -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                column,
                type_=sa.String(length=36),
                existing_type=sa.String(length=120),
                existing_nullable=False,
                existing_server_default="",
            )
    else:
        op.alter_column(
            table,
            column,
            type_=sa.String(length=36),
            existing_type=sa.String(length=120),
            existing_nullable=False,
            existing_server_default="",
        )


def upgrade() -> None:
    op.add_column(
        "model_usage",
        sa.Column("origin_run_id", sa.String(length=120), nullable=False, server_default=""),
    )
    op.create_index("ix_model_usage_origin_run_id", "model_usage", ["origin_run_id"])
    for table, column in WIDENED:
        _widen(table, column)


def downgrade() -> None:
    for table, column in reversed(WIDENED):
        _narrow(table, column)
    op.drop_index("ix_model_usage_origin_run_id", table_name="model_usage")
    op.drop_column("model_usage", "origin_run_id")

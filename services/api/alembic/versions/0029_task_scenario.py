"""场景系统：任务级 scenario 列

Revision ID: 0029_task_scenario
Revises: 0028_plan_persistence

## 为什么改

场景系统（升级任务书 §7）需要任务级归属：一轮任务在哪个场景下跑，
决定了系统提示附加段、可用工具白名单与记忆权限。场景是任务属性，
不是运行参数——恢复审批、重跑、查证据时都要还原"当时在哪个场景"。

## 改了什么

``tasks`` 加一列 ``scenario``（String(20)，服务端默认 ``'dev'``）。
存量行不需要回填：开发场景就是历史行为的原样。

## 兼容

带存在性检查（与 0028 同口径）：列已在时跳过，版本号与结构不一致的
老库不会整条链断在半路。五种取值之外的名字在应用层 ``resolve()``
一律回退到开发场景，脏数据不会改变默认行为。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029_task_scenario"
down_revision: Union[str, None] = "0028_plan_persistence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    if "scenario" in _columns("tasks"):
        return
    op.add_column(
        "tasks",
        sa.Column("scenario", sa.String(20), nullable=False, server_default="dev"),
    )


def downgrade() -> None:
    if "scenario" in _columns("tasks"):
        op.drop_column("tasks", "scenario")

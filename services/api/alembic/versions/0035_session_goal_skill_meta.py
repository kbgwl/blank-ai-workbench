"""会话目标与技能提案元数据（R6：ProposeGoal / ProposeSkills）

Revision ID: 0035_session_goal_skill_meta
Revises: 0034_channels

## 为什么改

R6 对齐 Claude Code 的两个提案工具：

- ``ProposeGoal`` 需要一处跨轮保留的「这次会话要达成什么」。产品级 ``goals``
  表按会话/项目追踪状态与步骤，用它装会话目标会抢占用户的 active goal；
  目标就一句话，放在 ``threads`` 上最贴合它的生命周期。
- ``ProposeSkills`` 的提案要带 kind/new-improvement/target/description/evidence。
  塞进 ``skills.history`` 会让 rollback 把提案当版本记录读而在
  ``KeyError: instructions`` 上炸掉，所以单开一列 ``metadata``。

## 改了什么

- ``threads`` 加 ``session_goal``（TEXT，默认空串）；
- ``skills`` 加 ``metadata``（JSON，默认 ``{}``）。

## 兼容

带存在性检查（与 0028–0034 同口径）：列已在时跳过。LOB 列一律用表达式默认值，
MySQL 不接受 TEXT/JSON 上的字面量 DEFAULT（1101）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0035_session_goal_skill_meta"
down_revision: Union[str, None] = "0034_channels"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    if "session_goal" not in _columns("threads"):
        op.add_column(
            "threads",
            sa.Column("session_goal", sa.Text(), nullable=False, server_default=sa.text("('')")),
        )
    if "metadata" not in _columns("skills"):
        op.add_column(
            "skills",
            sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("('{}')")),
        )


def downgrade() -> None:
    if "metadata" in _columns("skills"):
        op.drop_column("skills", "metadata")
    if "session_goal" in _columns("threads"):
        op.drop_column("threads", "session_goal")

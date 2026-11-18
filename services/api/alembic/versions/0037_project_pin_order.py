"""项目置顶与手工排序（对齐 Codex 的 Project.position / pinned）

Revision ID: 0037_project_pin_order
Revises: 0036_task_expansion

## 为什么改

逆向 Codex 桌面端拿到的设置字段原文是：

    "Unpinned ... project ids from the default Projects sidebar section, in their
     desired display order. Projects not included keep their ..."

也就是项目既能**置顶**，也能按**人工顺序**排。我们之前只有 created_at，
于是测试残留项目永远占在最前面，真实项目被挤到下面。

## 改了什么

``projects`` 加两列：

- ``pinned``  布尔，默认 false：置顶。
- ``position`` 整数，默认 0：同组内的人工顺序，小的在前。

## 兼容

带存在性检查（与 0028/0029/0030/0031/0036 同口径）：列已在时跳过。
存量数据 position 全为 0，排序退化成"置顶优先 + 创建时间倒序"，与从前几乎一致。
"""
from alembic import op
import sqlalchemy as sa

revision = "0037_project_pin_order"
down_revision = "0036_task_expansion"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    cols = _columns("projects")
    if "pinned" not in cols:
        op.add_column(
            "projects",
            sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        )
    if "position" not in cols:
        op.add_column(
            "projects",
            sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        )


def downgrade() -> None:
    cols = _columns("projects")
    if "pinned" in cols:
        op.drop_column("projects", "pinned")
    if "position" in cols:
        op.drop_column("projects", "position")

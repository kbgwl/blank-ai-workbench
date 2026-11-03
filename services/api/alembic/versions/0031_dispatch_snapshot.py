"""任务记下派发快照（v162 Super Agent）

Revision ID: 0031_dispatch_snapshot
Revises: 0030_usage_source

## 为什么改

Router 的决定必须可追溯：同一条任务，「当时按什么工具边界跑的」不能靠事后
读场景配置反推——配置会改，而审计要的是当时那一份。落成一列 JSON 快照，
执行侧只读它、不改它（``dispatch.py`` 里 snapshot_id 是字段哈希）。

## 改了什么

``tasks`` 加一列 ``dispatch_snapshot``（JSON，服务端默认 ``'{}'``）。
存量行为空对象 = 没有快照，执行路径与从前逐字节一致。

## 兼容

带存在性检查（与 0028/0029/0030 同口径）：列已在时跳过。
"""
from alembic import op
import sqlalchemy as sa

revision = "0031_dispatch_snapshot"
down_revision = "0030_usage_source"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    if "dispatch_snapshot" in _columns("tasks"):
        return
    op.add_column(
        "tasks",
        # MySQL 8.4 不接受给 JSON 列写字面量默认值，必须用表达式默认值；
        # 与 0028/0029 的 text("('[]')") 同一写法。
        sa.Column("dispatch_snapshot", sa.JSON(), nullable=False, server_default=sa.text("('{}')")),
    )


def downgrade() -> None:
    if "dispatch_snapshot" in _columns("tasks"):
        op.drop_column("tasks", "dispatch_snapshot")

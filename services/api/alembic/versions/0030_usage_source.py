"""用量账本记 usage 来源（provider / unknown）

Revision ID: 0030_usage_source
Revises: 0029_task_scenario

## 为什么改

升级任务书 §4.2：0 个 token 与「供应商没说用了多少」是两回事。没有来源标记，
断供/网关吞掉 usage 的调用会被读成免费，成本对比与 fallback 计费都失去依据。

## 改了什么

``model_usage`` 加一列 ``usage_source``（String(20)，服务端默认 ``'unknown'``）。
存量行在列加上之前没记过来源，一律按 unknown 读——不冒充 provider。

## 兼容

带存在性检查（与 0028/0029 同口径）：列已在时跳过。

"""
from alembic import op
import sqlalchemy as sa

revision = "0030_usage_source"
down_revision = "0029_task_scenario"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    if "usage_source" in _columns("model_usage"):
        return
    op.add_column(
        "model_usage",
        sa.Column("usage_source", sa.String(20), nullable=False, server_default="unknown"),
    )


def downgrade() -> None:
    if "usage_source" in _columns("model_usage"):
        op.drop_column("model_usage", "usage_source")

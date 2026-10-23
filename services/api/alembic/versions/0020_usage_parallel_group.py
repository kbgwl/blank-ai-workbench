"""并行批次进账本：model_usage 补 parallel_group

Revision ID: 0020_usage_parallel_group
Revises: 0019_usage_turn_attribution

REA 拆 Codex 的用量记录时发现它把"钱"按线程与回合两级都能筛出来；我们这边 #43 有了
`run_id`/`agent_path`，但**一次 `fan_out` 到底花了多少**仍然答不出：组 id 只埋在
`agent_path` 的 `par-{group}-{i}` 字符串里，要算合计就得先把这列字符串重新解析一遍——
把一个可指认的事实降级成正则表达式。这一列让"哪一批并行"成为可按的键。
`server_default=""`：单发子代理、主线程、摘要调用本来就不属于任何并行批次，
老行则是这列上线前就存在的——空串是"没说"，不是"第 0 批"。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020_usage_parallel_group"
down_revision: Union[str, None] = "0019_usage_turn_attribution"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "model_usage",
        sa.Column("parallel_group", sa.String(length=24), nullable=False, server_default=""),
    )
    op.create_index("ix_model_usage_parallel_group", "model_usage", ["parallel_group"])


def downgrade() -> None:
    op.drop_index("ix_model_usage_parallel_group", table_name="model_usage")
    op.drop_column("model_usage", "parallel_group")

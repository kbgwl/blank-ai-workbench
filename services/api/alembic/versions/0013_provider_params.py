"""total / active parameters on model providers

Revision ID: 0013_provider_params
Revises: 0012_provider_output_knobs

MoE 模型有两个参数量：总参数决定显存，激活参数决定算力与价格。
只存一个"参数量"字段的话，671B / 37B 这种模型会被误判成两种极端：
要么当成 671B 的大模型（结论是塞不进单机），要么当成 37B 的小模型（结论是哪都能跑）。
两种结论都错，而它们决定的是完全不同的部署方案。

默认 0 表示未填写。路由与成本比较遇到 0 会跳过该维度，而不是当成"零参数"。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_provider_params"
down_revision: Union[str, None] = "0012_provider_output_knobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("model_providers", sa.Column("total_params_b", sa.Float(), nullable=False, server_default="0"))
    op.add_column("model_providers", sa.Column("active_params_b", sa.Float(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("model_providers", "active_params_b")
    op.drop_column("model_providers", "total_params_b")

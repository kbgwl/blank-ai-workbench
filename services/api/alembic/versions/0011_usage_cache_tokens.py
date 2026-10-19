"""cache and reasoning token accounting on model_usage

Revision ID: 0011_usage_cache_tokens
Revises: 0010_tool_artifacts

缓存命中与推理 token 是两件花钱的事，但原来只记 prompt/completion 两个总数，
算不出"这次便宜是因为命中缓存"还是"少调了一次模型"。补三列细分计量。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_usage_cache_tokens"
down_revision: Union[str, None] = "0010_tool_artifacts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("model_usage", sa.Column("cached_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("model_usage", sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("model_usage", sa.Column("reasoning_tokens", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("model_usage", "reasoning_tokens")
    op.drop_column("model_usage", "cache_write_tokens")
    op.drop_column("model_usage", "cached_tokens")

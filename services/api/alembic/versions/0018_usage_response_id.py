"""provider response id on model_usage

Revision ID: 0018_usage_response_id
Revises: 0017_search_docs

RE-A 拆 Codex 的 rollout 时，`token_usage_record` 每行都带 `response_id`——供应商那一笔请求的 id。
我们原来只有自己的 `trace_id`，它能定位到"本任务的第几步"，定位不到"DeepSeek 那边是哪一笔"。
用户报"这轮答得不对"时，这条链在自家门口就断了。补一列，空串表示"这家没给 id"（是事实，不是缺失）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_usage_response_id"
down_revision: Union[str, None] = "0017_search_docs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("model_usage", sa.Column("response_id", sa.String(length=120), nullable=False, server_default=""))
    # 反向查询是这条记录唯一的用途：拿供应商给的 id 找回来。所以索引建在它身上，不是 task_id 上。
    op.create_index("ix_model_usage_response_id", "model_usage", ["response_id"])


def downgrade() -> None:
    op.drop_index("ix_model_usage_response_id", table_name="model_usage")
    op.drop_column("model_usage", "response_id")

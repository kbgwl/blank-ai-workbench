"""发起轮进事件时间线：agent_events 补 origin_run_id

Revision ID: 0024_events_origin_run_id
Revises: 0023_timestamps_not_null

#47 把发起轮写进了账本（`model_usage.origin_run_id`），事件时间线只记了 `run_id`——
那是"每人一个"的键。团队一轮里每个发言成员各有自己的 run，于是拿事件回答
"这条事件属于哪一轮群聊"必须再去 join 任务或账本，而证据包给的就是事件本身。

这一列与账本同源同宽、同样带索引。存量行保持空串：它们上线前没记过发起轮，
不许拿 run_id 或 task_id 顶上（那会把"没记"伪装成一个答案）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024_events_origin_run_id"
down_revision: Union[str, None] = "0023_timestamps_not_null"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_events",
        sa.Column("origin_run_id", sa.String(length=120), nullable=False, server_default=""),
    )
    op.create_index("ix_agent_events_origin_run_id", "agent_events", ["origin_run_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_events_origin_run_id", table_name="agent_events")
    op.drop_column("agent_events", "origin_run_id")

"""执行边界进账本：model_usage.sandbox_mode 与 team_groups.sandbox_tier

Revision ID: 0025_sandbox_mode_boundary
Revises: 0024_events_origin_run_id

M3 要求「每个 agent_path 都能查出它当时所在的档位」：模型调用与团队群各自
落一列，事件侧另有 `tool.completed.sandbox_mode` / `boundary_kind`。

存量行保持空串而不是补默认档位：它们上线前没记过档位，拿 `workspace` 顶上
就是把"没记"伪装成"当时按 workspace 跑"。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025_sandbox_mode_boundary"
down_revision: Union[str, None] = "0024_events_origin_run_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "model_usage",
        sa.Column("sandbox_mode", sa.String(length=20), nullable=False, server_default=""),
    )
    op.add_column(
        "team_groups",
        sa.Column("sandbox_tier", sa.String(length=20), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("team_groups", "sandbox_tier")
    op.drop_column("model_usage", "sandbox_mode")

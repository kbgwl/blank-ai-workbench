"""每轮回退点：给任务行加 rewound_at

Revision ID: 0026_task_rewound_at
Revises: 0025_sandbox_mode_boundary

回退**不删**轮次：那一轮的事件、工具调用、用量账都挂在任务行上，删了就是
毁审计材料。这一列只表达"它不再属于对话的一部分"，历史拼装与详情列表据此过滤。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026_task_rewound_at"
down_revision: Union[str, None] = "0025_sandbox_mode_boundary"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("rewound_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "rewound_at")

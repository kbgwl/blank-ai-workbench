"""输出上限抬到 8192：2048 会把整文件写入截断

Revision ID: 0027_max_tokens_default
Revises: 0026_task_rewound_at

2048 这个默认值坏得很安静：不报错，只表现为「工具参数偶尔是空的」。输出被切在
中途，流式 JSON 不完整，参数解析成空对象，于是回一句「缺少必填参数：patch」。
用户看到的是它反复重来，猜不到是输出上限。

0.8.17 真机对照（同一句建站任务，deepseek-flash）：

| 上限 | 步数 | apply_patch 失败 | 产物 |
|---|---|---|---|
| 2048 | 28 | 10 次（6 次是空参数） | 14.7 KB |
| 8192 | 5 | 0 次 | 20.3 KB |

机制证据：8192 那次有一次调用实际输出 7,186 token——在 2048 的档位上必然被切断。

存量行一并抬起来：只动 ``<= 2048`` 的档位，用户明确设过 4096 / 8192 的不碰。
启动时另有一层兜底（``registry.upgrade_truncating_providers``），防止有人把它改回去。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027_max_tokens_default"
down_revision: Union[str, None] = "0026_task_rewound_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("model_providers") as batch:
        batch.alter_column(
            "max_tokens",
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default="8192",
        )
    op.execute("UPDATE model_providers SET max_tokens = 8192 WHERE max_tokens <= 2048")


def downgrade() -> None:
    with op.batch_alter_table("model_providers") as batch:
        batch.alter_column(
            "max_tokens",
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default="2048",
        )

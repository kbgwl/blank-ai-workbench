"""回合归属进账本：model_usage 补 run_id / root_trace_id / parent_trace_id / agent_path

Revision ID: 0019_usage_turn_attribution
Revises: 0018_usage_response_id

REA 拆 Codex 的 `token_usage_record` 与 `resume_metadata.turn_attribution`（docs §4.1）时，
它的每一行都同时带着 `turn_id` / `root_turn_id` / `session_id` / `initiating_agent_path`。
我们这边钱表只有 `task_id` + `trace_id`，于是有两个问题是答不出的：
① 「这一轮花了多少」——同一个任务重跑三次，行里没有任何字段能把三轮分开；
② 「这条子分支是谁引起的」——父子关系只写在事件 payload 里，账本上只有一串看不出源头的 trace_id。
四列全部 `server_default=""`：空串表示"这行上线前就存在，当时没记"，是事实而不是缺失，
读出来必须是 null，不许当成第 0 轮或根为空。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_usage_turn_attribution"
down_revision: Union[str, None] = "0018_usage_response_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = (
    ("run_id", 36, True),
    ("root_trace_id", 120, False),
    ("parent_trace_id", 120, False),
    ("agent_path", 200, False),
)


def upgrade() -> None:
    for name, length, indexed in COLUMNS:
        op.add_column("model_usage", sa.Column(name, sa.String(length=length), nullable=False, server_default=""))
        # 只有 run_id 需要被单独查（"这一轮的账"是按轮筛出来的），其余三列跟着行读，不建索引。
        if indexed:
            op.create_index(f"ix_model_usage_{name}", "model_usage", [name])


def downgrade() -> None:
    for name, _length, indexed in reversed(COLUMNS):
        if indexed:
            op.drop_index(f"ix_model_usage_{name}", table_name="model_usage")
        op.drop_column("model_usage", name)

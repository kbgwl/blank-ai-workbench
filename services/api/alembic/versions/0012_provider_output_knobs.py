"""reasoning effort and verbosity on model providers

Revision ID: 0012_provider_output_knobs
Revises: 0011_usage_cache_tokens

推理档位与冗长档位是模型侧的输出开关，写死在代码里就等于所有客户共用一个档位；
放到供应商行上，同一个模型可以在"快而短"和"慢而深"之间按业务切。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_provider_output_knobs"
down_revision: Union[str, None] = "0011_usage_cache_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("model_providers", sa.Column("reasoning_effort", sa.String(20), nullable=False, server_default=""))
    op.add_column("model_providers", sa.Column("verbosity", sa.String(20), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("model_providers", "verbosity")
    op.drop_column("model_providers", "reasoning_effort")

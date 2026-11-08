"""消息通道表（v0.10.3）

Revision ID: 0034_channels
Revises: 0033_chat_goals

## 为什么改

企业微信/钉钉/飞书的接入以前只存在于任务书的描述里：没有表、没有适配器，
界面上那三行「未接通」是写死的常量。客户想用只能改代码。这一版把它变成
**填表即用**：一行 = 一套客户自己的凭据 + 落点，密钥加密入库。

## 改了什么

新增表 `channels`。`(kind, name)` 唯一：同类型可配多条（两个群机器人、两个应用），
但同名不允许，否则界面上会出现两条长得一样、连的却不是同一个东西的通道。

`callback_token` 单独一列而不是塞进加密配置：回调校验要读它，而读加密配置意味着
每收一条消息都要解一次密。

## 兼容

带存在性检查（与 0028–0033 同口径）：表已在时跳过。
"""
from alembic import op
import sqlalchemy as sa

revision = "0034_channels"
down_revision = "0033_chat_goals"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "channels" in _tables():
        return
    op.create_table(
        "channels",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False, server_default="app"),
        # LOB 列不许写字面量默认值：MySQL 会在 TEXT 上报 1101（BLOB/TEXT can't have
        # a default value）。统一用表达式默认值，两条建库路径才对得上。
        sa.Column("config_encrypted", sa.Text(), nullable=False, server_default=sa.text("('')")),
        sa.Column("push", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("receive", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("receive_mode", sa.String(20), nullable=False, server_default="off"),
        sa.Column("callback_token", sa.String(64), nullable=False, server_default=""),
        sa.Column("project_id", sa.String(36), nullable=False, server_default=""),
        sa.Column("thread_id", sa.String(36), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="unverified"),
        sa.Column("status_note", sa.Text(), nullable=False, server_default=sa.text("('')")),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.Column("last_inbound_at", sa.DateTime(), nullable=True),
        sa.Column("last_inbound_error", sa.Text(), nullable=False, server_default=sa.text("('')")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("kind", "name", name="uq_channels_kind_name"),
    )
    op.create_index("ix_channels_kind", "channels", ["kind"])


def downgrade() -> None:
    if "channels" in _tables():
        op.drop_table("channels")

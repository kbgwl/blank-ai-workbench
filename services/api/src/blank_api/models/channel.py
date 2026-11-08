"""消息通道：把「客户自己的企业微信/钉钉/飞书」变成一条可配置的连接。

为什么是表而不是配置文件：客户配一次就要在界面上看见「通没通、为什么不通」，
而且凭据必须加密落库（与模型供应商 Key 同一把主密钥）。写进 settings.json 会让
密钥以明文躺在用户可编辑的文件里，出错时也无法回答「这条通道什么时候验过」。

三件事从行上分开，因为它们是三个不同的问题：

* ``config_encrypted`` —— 客户填的东西。整份 JSON 加密，读出来只回显掩码。
* ``status`` —— 上一次真实验证的结果（ok/failed/unverified）。空配置不能算 ok。
* ``project_id`` / ``thread_id`` —— 从这条通道进来的消息落到哪个项目、哪条会话。
  会话单独记：聊天要接着上文，每句话新开一条会话就变成"每次都失忆"。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, UniqueConstraint, func, text, true
from sqlalchemy.orm import Mapped, mapped_column

from blank_api.db import Base

# 支持的通道类型。加一种要同时有：适配器（收发实现）+ 字段声明 + 真实联调。
CHANNEL_WECOM = "wecom"
CHANNEL_DINGTALK = "dingtalk"
CHANNEL_FEISHU = "feishu"
CHANNEL_WEBHOOK = "webhook"
CHANNEL_KINDS = (CHANNEL_WECOM, CHANNEL_DINGTALK, CHANNEL_FEISHU, CHANNEL_WEBHOOK)

# 两种接法。群机器人/自定义 webhook 只能发；企业应用能收能发。
MODE_APP = "app"
MODE_ROBOT = "robot"
CHANNEL_MODES = (MODE_APP, MODE_ROBOT)

# 入站方式。webhook 要公网可达；stream 是主动连出去的长连接，内网机器也能用。
RECEIVE_WEBHOOK = "webhook"
RECEIVE_STREAM = "stream"
RECEIVE_OFF = "off"
RECEIVE_MODES = (RECEIVE_WEBHOOK, RECEIVE_STREAM, RECEIVE_OFF)

STATUS_UNKNOWN = "unverified"
STATUS_OK = "ok"
STATUS_FAILED = "failed"


class Channel(Base):
    __tablename__ = "channels"
    __table_args__ = (UniqueConstraint("kind", "name", name="uq_channels_kind_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # 客户自己起的名字。同类型可以配多条（两个群、两个机器人）。
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default=MODE_APP, server_default=MODE_APP)
    # 整份配置（含密钥）加密存放，与 model_providers.api_key_encrypted 用同一把主密钥。
    config_encrypted: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("('')"))
    # 出站：任务提醒（等审批/失败/交付）推到这条通道。
    push: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    # 入站：允许这条通道上的消息开任务。
    receive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    receive_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=RECEIVE_OFF, server_default=RECEIVE_OFF
    )
    # 回调地址里的不可猜段。厂商侧填的 URL 就是 /api/v1/channels/{id}/callback/{token}，
    # 没这个段等于把「任何知道地址的人都能让 Agent 开任务」暴露在公网上。
    callback_token: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    # 消息落到哪。project_id 为空 → 回落到第一个项目；thread_id 为空 → 首次收到时建一条。
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, default="", server_default="")
    thread_id: Mapped[str] = mapped_column(String(36), nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=STATUS_UNKNOWN, server_default=STATUS_UNKNOWN
    )
    # 验证结果原文。失败时这里就是用户照着改的那句话，所以不截断成"验证失败"。
    status_note: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("('')"))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 入站最后一次收到消息的时间。它和 checked_at 是两件事：凭据有效 ≠ 真的有消息进来。
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_inbound_error: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("('')"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

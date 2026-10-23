"""团队：Bot、群聊、轮次与消息。

团队不是第二套 Agent。每个 Bot 在群里有一条**隐藏会话**（threads.kind = "team"），
每次发言仍然是一条普通 Task——权限、审批、事件、用量、快照全部复用现有链路。
这里只多存三样现有模型回答不了的东西：

* Bot 是谁：名字、角色、指令、绑定哪个供应商/目录/权限档；
* 群里说过什么：消息正文与顺序（Task 只记录"这一轮要什么、交付什么"）；
* 这一轮排到谁、花了多少：队列、已发言成员、token 累计，重启后还能接着跑。
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from blank_api.db import Base

TEAM_ROUND_STATUSES = ("running", "waiting_approval", "cancelling", "completed", "failed", "cancelled")
TEAM_MESSAGE_ROLES = ("user", "bot", "system")
TEAM_MESSAGE_STATUSES = (
    "sent",
    "running",
    "succeeded",
    "pass",
    "failed",
    "waiting_approval",
    "cancelled",
)


class TeamBot(Base):
    __tablename__ = "team_bots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(24), nullable=False, unique=True)
    role: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 没绑供应商就跟随"第一个启用的供应商"；绑了就必须在运行时仍然启用。
    provider_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_providers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 没绑目录就跟随所在项目当前的活动目录。绑了就必须属于该群所在项目。
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )
    permission_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="default")
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    # 主理人（小白管家）只有一位：不能改名、不能删除、不能停用。
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class TeamGroup(Base):
    __tablename__ = "team_groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False, default="")
    admin_bot_id: Mapped[str | None] = mapped_column(
        ForeignKey("team_bots.id", ondelete="SET NULL"), nullable=True
    )
    # 一轮里最多几次发言（含主理人开场与收尾）。这是防止 Bot 互相 @ 停不下来的硬闸。
    max_speakers: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    # 一轮的 token 预算，按本轮各 Task 的 ModelUsage 累计。
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=60_000)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class TeamMember(Base):
    __tablename__ = "team_members"
    __table_args__ = (UniqueConstraint("group_id", "bot_id", name="uq_team_members_group_bot"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("team_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    bot_id: Mapped[str] = mapped_column(
        ForeignKey("team_bots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 每个 Bot 在群里一条自己的会话：上一轮它说过什么，这一轮还记得。
    thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("threads.id", ondelete="SET NULL"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TeamRound(Base):
    """一次用户发言触发的协作轮次。

    队列与已发言名单必须落库：Bot 需要审批时这一轮会停在半路，用户批准后
    服务可能已经换了一个进程——只在内存里记"下一个该谁"，恢复时就接不上。
    """

    __tablename__ = "team_rounds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("team_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running", index=True)
    # 待发言的 bot_id 顺序队列，以及已经完成过一轮的 bot_id（含 [PASS]）。
    # `text("('[]')")` 不是口味：MySQL 8.4 拒绝给 JSON/TEXT 列写字面量默认值（1101），
    # 只有 8.0.13+ 的表达式默认值这一种写法两种方言都吃。
    queue: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("('[]')")
    )
    spoken: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("('[]')")
    )
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 主理人是否已经做过收尾。没有成员的轮次不收尾，避免自问自答。
    wrap_up_done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class TeamMessage(Base):
    """群聊消息。整数主键就是群内的自然序号，轮询只用 after_id。"""

    __tablename__ = "team_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("team_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    round_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(12), nullable=False)
    bot_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # 名字冗余一份：Bot 改名或被删除后，历史消息仍然要能显示"当时是谁说的"。
    bot_name: Mapped[str] = mapped_column(String(24), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    meta: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("('{}')")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

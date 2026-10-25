from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from blank_api.db import Base

THREAD_STATUSES = ("active", "archived")


class Thread(Base):
    """一条会话：一串以「轮」为单位的任务，共享对话上下文。

    刻意**不**保存消息：一轮的产物是它自己的 task（目标、事件、工具调用、总结），
    会话只需要知道"哪几轮、按什么顺序"。存一份消息副本就会有两份真相，
    而它们迟早会不一致——那时用户看到的上下文和模型看到的上下文不是一回事。

    ``forked_from`` 表示这条会话是从另一条分叉出来的。分叉**不复制任务行**：
    复制一份任务等于复制它的全部事件与审计，而用户要的只是"带上刚才聊过的内容
    重新开始"。所以分叉只记来源，上下文在读取时沿链回溯。
    """

    __tablename__ = "threads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", server_default=""
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    # chat = 用户对话；team = 团队群里某个 Bot 自己的隐藏会话。
    # 隐藏会话不进对话列表，否则用户每建一个 Bot 就会多出一条点开没内容的"对话"。
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="chat", server_default="chat"
    )
    forked_from: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # 轮次计数单独存一份：列表页要显示"共几轮"，每次去 count 一遍 tasks
    # 在一个有几百轮的会话上会变成列表接口的主要开销。
    turn_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

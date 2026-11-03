from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from blank_api.db import Base


class AgentTrigger(Base):
    """一条事件触发器（v162 §5）。

    `(project_id, dedupe_key)` 唯一：去重不能只靠"先查再插"——两个触发源同时
    上报同一件事时，两次查询都会看到"还没有"，然后各插一条。这是库里的一条硬约束，
    也是"重复事件只生成一个任务"能被证明的唯一方式。

    行不删，只改状态（pending/fired/skipped/expired）：过期和跳过都是事实，
    删了之后"我那件事后来怎样了"就查不到。
    """

    __tablename__ = "agent_triggers"
    __table_args__ = (
        UniqueConstraint("project_id", "dedupe_key", name="uq_agent_triggers_dedupe"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, default="user_task")
    source: Mapped[str] = mapped_column(String(120), nullable=False, default="", server_default="")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default=text("('{}')"))
    dedupe_key: Mapped[str] = mapped_column(String(120), nullable=False)
    # 触发后要跑的任务。空串 = 还没有任务（事件只登记、未建任务）。
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, default="", server_default="")
    provider_id: Mapped[str] = mapped_column(String(36), nullable=False, default="", server_default="")
    # 触发时要用的运行参数（TaskRunIn 的形状）。存下来是为了断点/重启后能照着
    # 当时的授权跑，而不是用今天设置页里的默认值。
    run_payload: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("('{}')")
    )
    fire_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    fired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending", index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

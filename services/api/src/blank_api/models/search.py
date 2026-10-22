"""跨群、跨任务的聊天索引。

消息正文与任务结论在写入时切词并落一行索引；查询按 token 命中数打分。
不用 SQLite FTS5：它的建表语法、分词器和 Postgres 的 tsvector 不是一套东西，
而这个应用要能在 SQLite（默认）与 Postgres 之间搬库。索引表本身是普通表，
迁移和备份都按普通表走。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from blank_api.db import Base


class SearchDoc(Base):
    __tablename__ = "search_docs"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_search_docs_source"),
        Index("ix_search_docs_project_group", "project_id", "group_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # team_message = 群聊里的一句话；task = 一轮对话的结论。
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="team_message")
    source_id: Mapped[str] = mapped_column(String(80), nullable=False)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    group_id: Mapped[str | None] = mapped_column(
        ForeignKey("team_groups.id", ondelete="CASCADE"), nullable=True, index=True
    )
    thread_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 群名或任务标题：搜索结果列表里要能一眼看出"这是哪里的对话"。
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    speaker: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 空格包围的 token 串（" 登录 接口 jie "），查询用 LIKE 做候选集，再精确打分。
    tokens: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

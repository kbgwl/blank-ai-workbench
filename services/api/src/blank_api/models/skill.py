from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from blank_api.db import Base


class MemoryCandidate(Base):
    __tablename__ = "memory_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(
        String(120), nullable=False, default="daily", server_default="daily"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft", server_default="draft"
    )
    version: Mapped[int] = mapped_column(nullable=False, default=1, server_default="1")
    source_candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("memory_candidates.id", ondelete="SET NULL"), nullable=True
    )
    history: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 提案元数据（对齐 Claude 的 ProposeSkills）：kind(new/improvement)、target、
    # description、evidence、proposed_at。不能塞进 history——rollback 把它当
    # [{version, instructions}] 读，缺 instructions 键会直接 KeyError。
    metadata_json: Mapped[dict] = mapped_column(
        "metadata", JSON, nullable=False, default=dict, server_default=text("('{}')")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

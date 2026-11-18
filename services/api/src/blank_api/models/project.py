from datetime import datetime

from sqlalchemy import Boolean, Integer, DateTime, ForeignKey, String, Text, func, text, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from blank_api.db import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("('')")
    )
    # 置顶与人工顺序（对齐 Codex 的 Project.pinned / position）。
    # 之前只有 created_at，测试残留项目会永远占住前排。
    pinned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    position: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    workspaces: Mapped[list["Workspace"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def active_workspace(self) -> "Workspace | None":
        for ws in self.workspaces:
            if ws.is_active:
                return ws
        return self.workspaces[0] if self.workspaces else None


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", server_default=""
    )
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        default=True, nullable=False, server_default=true()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    project: Mapped[Project] = relationship(back_populates="workspaces")

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from blank_api.db import Base


class AgentCheckpoint(Base):
    """LangGraph checkpoint rows; thread_id is derived from the task and run attempt."""

    __tablename__ = "agent_checkpoints"

    thread_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    checkpoint_ns: Mapped[str] = mapped_column(String(120), primary_key=True, default="")
    checkpoint_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_checkpoint_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    serde_type: Mapped[str] = mapped_column(String(40), nullable=False, default="msgpack")
    checkpoint: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AgentCheckpointWrite(Base):
    __tablename__ = "agent_checkpoint_writes"

    thread_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    checkpoint_ns: Mapped[str] = mapped_column(String(120), primary_key=True, default="")
    checkpoint_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    write_task_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_path: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    channel: Mapped[str] = mapped_column(String(120), nullable=False)
    serde_type: Mapped[str] = mapped_column(String(40), nullable=False, default="msgpack")
    value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

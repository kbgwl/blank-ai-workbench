from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from blank_api.limits import DEFAULT_MAX_STEPS, HARD_MAX_STEPS


class ThreadCreate(BaseModel):
    title: str = Field(default="", max_length=200)
    # 从哪条会话分叉过来。为空表示全新会话。
    forked_from: str | None = None


class ThreadTurnIn(BaseModel):
    """在已有会话里开新的一轮。字段与 TaskCreate 一致，只是项目由会话决定。"""

    title: str = Field(min_length=1, max_length=200)
    goal: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=20)
    provider_id: str | None = None
    max_steps: int = Field(default=DEFAULT_MAX_STEPS, ge=1, le=HARD_MAX_STEPS)


class ThreadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    title: str
    status: str
    forked_from: str | None
    turn_count: int
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def _serialize_utc(self, value: datetime) -> str:
        aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        return aware.isoformat()


class ThreadTurnOut(BaseModel):
    """一轮的概要。刻意比 TaskOut 窄：会话列表不需要把每个任务的目标全文带上。"""

    id: str
    title: str
    goal: str
    status: str
    result_summary: str
    created_at: datetime

    @field_serializer("created_at")
    def _serialize_utc(self, value: datetime) -> str:
        aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        return aware.isoformat()


class ThreadDetailOut(ThreadOut):
    turns: list[ThreadTurnOut] = Field(default_factory=list)

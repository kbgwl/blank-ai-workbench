from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    goal: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=20)
    provider_id: str | None = None
    max_steps: int = Field(default=8, ge=1, le=30)


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    provider_id: str | None
    title: str
    goal: str
    status: str
    acceptance_criteria: list[str]
    acceptance_results: list[dict]
    max_steps: int
    steps_used: int
    result_summary: str
    failure_reason: str
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def _serialize_utc(self, value: datetime) -> str:
        # 库里存的是无时区的 UTC 值，补上偏移后前端才能算对相对时间。
        aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        return aware.isoformat()


class TaskRunIn(BaseModel):
    provider_id: str | None = None
    approve_high_risk: bool = False
    approved_tools: list[str] = Field(default_factory=list)
    approved_actions: list[str] = Field(default_factory=list, max_length=50)
    fallback_provider_ids: list[str] = Field(default_factory=list, max_length=5)


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    node: str
    type: str
    payload: dict


class TaskApprovalIn(BaseModel):
    approve: bool
    provider_id: str | None = None

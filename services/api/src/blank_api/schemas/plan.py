"""计划模板的请求 / 响应形状。"""

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class PlanTemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    goal_spec: dict = Field(default_factory=dict)
    steps: list[dict] = Field(default_factory=list, max_length=40)
    milestones: list[dict] = Field(default_factory=list, max_length=10)


class PlanTemplateUpdate(BaseModel):
    """局部更新。全部可选——从任务存模板时只带 name/description 也是合法的。"""

    name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    goal_spec: dict | None = None
    steps: list[dict] | None = None
    milestones: list[dict] | None = None


class PlanTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    name: str
    description: str
    goal_spec: dict = Field(default_factory=dict)
    steps: list[dict] = Field(default_factory=list)
    milestones: list[dict] = Field(default_factory=list)
    usage_count: int = 0
    source_task_id: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def _serialize_utc(self, value: datetime) -> str:
        aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        return aware.isoformat()

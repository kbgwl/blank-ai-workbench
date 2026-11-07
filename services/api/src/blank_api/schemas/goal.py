"""目标对象的出参（v165 §3.1 / §9.1 顶部状态）。"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class GoalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    conversation_id: str
    title: str
    summary: str = ""
    scenario: str = "dev"
    automation_mode: str = "assist"
    priority: str = "normal"
    status: str = "captured"
    steps_done: list = Field(default_factory=list)
    steps_pending: list = Field(default_factory=list)
    steps_failed: list = Field(default_factory=list)
    blocked_reason: str = ""
    next_action: str = ""
    current_task_id: str = ""
    task_ids: list[str] = Field(default_factory=list)
    run_ids: list[str] = Field(default_factory=list)
    budget_tokens: int = 0
    tokens_used: int = 0
    last_active_at: datetime
    created_at: datetime
    updated_at: datetime

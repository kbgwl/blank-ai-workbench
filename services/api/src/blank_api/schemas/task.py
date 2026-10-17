from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from blank_api.limits import DEFAULT_MAX_STEPS, HARD_MAX_STEPS


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    goal: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=20)
    provider_id: str | None = None
    max_steps: int = Field(default=DEFAULT_MAX_STEPS, ge=1, le=HARD_MAX_STEPS)


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
    # 命令前缀放行（对标 Codex 的 approved_command_prefixes）："git status"、"npm run" 这类签名。
    approved_command_prefixes: list[str] = Field(default_factory=list, max_length=50)
    # 四档权限模式（对标 Claude Code 的 defaultMode）；空串表示沿用该任务已有的模式。
    permission_mode: Literal["", "default", "accept_edits", "plan", "bypass"] = ""
    fallback_provider_ids: list[str] = Field(default_factory=list, max_length=5)
    role_provider_ids: dict[str, str] = Field(default_factory=dict, max_length=8)


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    run_id: str = ""
    step_id: str = ""
    parent_event_id: str = ""
    node: str
    type: str
    payload: dict


class TaskApprovalIn(BaseModel):
    approve: bool
    provider_id: str | None = None
    # 批准作用域：仅本次 / 本会话该命令前缀 / 本会话该工具。
    scope: Literal["once", "prefix", "tool"] = "once"


class TaskAnswerIn(BaseModel):
    """回答 Agent 的提问：按问题 id 回填，或直接给一个自由文本答案。"""

    answers: dict[str, str] = Field(default_factory=dict, max_length=8)
    answer: str = Field(default="", max_length=4000)
    provider_id: str | None = None


class TaskForkIn(BaseModel):
    """从某个检查点分叉出新线程：trunk_reuse 带上状态继续，trunk_new 只留血缘。"""

    from_thread_id: str = Field(default="", max_length=120)
    strategy: Literal["trunk_reuse", "trunk_new"] = "trunk_reuse"


class TaskSteerIn(BaseModel):
    """运行中插话：不打断当前步骤，从下一次模型调用开始生效。"""

    message: str = Field(min_length=1, max_length=4000)

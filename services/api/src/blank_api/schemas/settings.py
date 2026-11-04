"""Schemas for the agent runtime settings page."""

from __future__ import annotations

from pydantic import BaseModel, Field


class GuardianSettings(BaseModel):
    mode: str = "veto"
    timeout_seconds: float = 8.0


class CompactionSettings(BaseModel):
    trigger_ratio: float = 0.82
    reserve_tokens: int = 16_384


class PermissionSettings(BaseModel):
    default_mode: str = "default"


class ToolSettings(BaseModel):
    search: bool = True


class SafetySettings(BaseModel):
    """能力边界。前端拿这一块画「安全与权限」面板。"""

    shell_enabled: bool = False
    network_enabled: bool = False
    confirm_all_commands: bool = True
    restrict_to_workspace: bool = True
    timeout_seconds: float = 120.0
    allowed_roots: list[str] = Field(default_factory=list)
    denied_commands: list[str] = Field(default_factory=list)


class BackgroundSettings(BaseModel):
    """后台执行与低打扰（任务书 §5）。所有数值都有硬上限，模型不能自行提升。"""

    enabled: bool = True
    notify_policy: str = "approval_only"
    quiet_hours: str = ""
    max_seconds: int = 1800
    max_steps: int = 200
    token_budget: int = 1_500_000
    auto_recoveries: int = 2
    default_model_policy: str = "default"


class AgentSettings(BaseModel):
    guardian: GuardianSettings = Field(default_factory=GuardianSettings)
    compaction: CompactionSettings = Field(default_factory=CompactionSettings)
    permissions: PermissionSettings = Field(default_factory=PermissionSettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    safety: SafetySettings = Field(default_factory=SafetySettings)
    background: BackgroundSettings = Field(default_factory=BackgroundSettings)
    # 设置文件位置与合法取值：前端用来画滑杆边界，顺便让"改到哪去了"可查
    path: str = ""
    limits: dict = Field(default_factory=dict)


class GuardianPatch(BaseModel):
    mode: str | None = None
    timeout_seconds: float | None = None


class CompactionPatch(BaseModel):
    trigger_ratio: float | None = None
    reserve_tokens: int | None = None


class PermissionPatch(BaseModel):
    default_mode: str | None = None


class ToolPatch(BaseModel):
    search: bool | None = None


class SafetyPatch(BaseModel):
    shell_enabled: bool | None = None
    network_enabled: bool | None = None
    confirm_all_commands: bool | None = None
    restrict_to_workspace: bool | None = None
    timeout_seconds: float | None = None
    allowed_roots: list[str] | None = None
    denied_commands: list[str] | None = None


class BackgroundPatch(BaseModel):
    enabled: bool | None = None
    notify_policy: str | None = None
    quiet_hours: str | None = None
    max_seconds: int | None = None
    max_steps: int | None = None
    token_budget: int | None = None
    auto_recoveries: int | None = None
    default_model_policy: str | None = None


class AgentSettingsPatch(BaseModel):
    guardian: GuardianPatch | None = None
    compaction: CompactionPatch | None = None
    permissions: PermissionPatch | None = None
    tools: ToolPatch | None = None
    safety: SafetyPatch | None = None
    background: BackgroundPatch | None = None

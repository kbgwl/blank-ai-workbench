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


class AgentSettings(BaseModel):
    guardian: GuardianSettings = Field(default_factory=GuardianSettings)
    compaction: CompactionSettings = Field(default_factory=CompactionSettings)
    permissions: PermissionSettings = Field(default_factory=PermissionSettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
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


class AgentSettingsPatch(BaseModel):
    guardian: GuardianPatch | None = None
    compaction: CompactionPatch | None = None
    permissions: PermissionPatch | None = None
    tools: ToolPatch | None = None

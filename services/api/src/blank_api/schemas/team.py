from typing import Literal

from pydantic import BaseModel, Field, field_validator

from blank_api.limits import HARD_MAX_STEPS

PermissionMode = Literal["default", "accept_edits", "plan", "bypass"]


class TeamBotIn(BaseModel):
    name: str = Field(min_length=1, max_length=24)
    role: str = Field(default="", max_length=100)
    instructions: str = Field(default="", max_length=4000)
    provider_id: str | None = None
    workspace_id: str | None = None
    permission_mode: PermissionMode = "default"
    max_steps: int = Field(default=8, ge=1, le=HARD_MAX_STEPS)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _name_is_mentionable(cls, value: str) -> str:
        # 名字要能被 @ 精确点名：空白会让 "@后端 同学" 这种写法天然有歧义，
        # @ / ＠ 本身则会让点名解析陷入"名字里还有名字"的循环。
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("名字不能为空")
        if any(ch.isspace() or ch in "@＠" for ch in cleaned):
            raise ValueError("名字不能包含空格或 @")
        return cleaned


class TeamBotUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=24)
    role: str | None = Field(default=None, max_length=100)
    instructions: str | None = Field(default=None, max_length=4000)
    provider_id: str | None = None
    workspace_id: str | None = None
    permission_mode: PermissionMode | None = None
    max_steps: int | None = Field(default=None, ge=1, le=HARD_MAX_STEPS)
    enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def _name_is_mentionable(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("名字不能为空")
        if any(ch.isspace() or ch in "@＠" for ch in cleaned):
            raise ValueError("名字不能包含空格或 @")
        return cleaned


class TeamGroupIn(BaseModel):
    project_id: str = Field(min_length=1, max_length=36)
    name: str = Field(min_length=1, max_length=100)
    topic: str = Field(default="", max_length=2000)
    # 只列成员；主理人（小白管家）由后端自动加进去，避免前端漏传导致群没有主持人。
    member_ids: list[str] = Field(default_factory=list, max_length=4)
    max_speakers: int = Field(default=5, ge=1, le=8)
    token_budget: int = Field(default=60_000, ge=5_000, le=2_000_000)
    # 整群的执行档位；成员任务逐字继承。空串=按设置默认档。
    sandbox_tier: Literal["", "off", "workspace", "workspace-net"] = ""


class TeamGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    topic: str | None = Field(default=None, max_length=2000)
    member_ids: list[str] | None = Field(default=None, max_length=4)
    max_speakers: int | None = Field(default=None, ge=1, le=8)
    token_budget: int | None = Field(default=None, ge=5_000, le=2_000_000)


class TeamMessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class TeamApprovalIn(BaseModel):
    approve: bool
    scope: Literal["once", "prefix", "tool"] = "once"


class TeamAnswerIn(BaseModel):
    answers: dict[str, str] = Field(default_factory=dict, max_length=8)
    answer: str = Field(default="", max_length=4000)

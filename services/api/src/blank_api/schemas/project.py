from pydantic import BaseModel, ConfigDict, Field


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    label: str
    path: str
    is_active: bool
    # 当前 git 分支。Codex 的 Thread.gitInfo / 桌面端那条 `项目 · Local · master`
    # 就是它。不是仓库时为空串（空串 = 不知道，不是"没有分支"）。
    branch: str = ""


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    pinned: bool | None = None
    position: int | None = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    pinned: bool = False
    position: int = 0
    workspaces: list[WorkspaceOut] = []


class WorkspaceCreate(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    label: str = ""

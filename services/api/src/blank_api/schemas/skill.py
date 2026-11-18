from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MemoryCandidateCreate(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    source: str = Field(default="daily", max_length=120)


class MemoryCandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    text: str
    source: str
    status: str
    created_at: datetime
    reviewed_at: datetime | None


class SkillOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    instructions: str
    status: str
    version: int
    source_candidate_id: str | None
    history: list
    # ORM 属性叫 metadata_json（`metadata` 是 SQLAlchemy 声明式的保留名），
    # 对外的字段名仍然是 metadata，所以读的时候走 validation_alias。
    metadata: dict = Field(default_factory=dict, validation_alias="metadata_json")
    created_at: datetime
    updated_at: datetime


class SkillUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    instructions: str | None = Field(default=None, min_length=1, max_length=12000)


class SkillLibraryItem(BaseModel):
    """磁盘上真正能被模型加载的一条技能（内置 / 用户 / 项目 / 插件）。

    与 ``SkillOut`` 的区别：那是库里"用户审过的记忆技能"，这是**运行时可用的
    技能目录**。技能页要回答的是"我现在能用什么"，只列前者会让人以为只有几条。
    """

    name: str
    display_name: str = ""
    description: str = ""
    when_to_use: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    model_invocable: bool = True
    source: str = "builtin"
    path: str = ""
    # 同名时被更高层覆盖掉的那一份（比如项目里自己写的 code-review）。
    shadowed: bool = False
    # 安装来源（来自技能目录里的 .skill-source.json）。内置技能没有这两项。
    installed_from: str = ""
    installed_at: str = ""


class SkillUsageOut(BaseModel):
    """一条技能最近的用量。口径见 services/skill_usage.py 的模块文档。"""

    calls: int = 0
    tasks: int = 0
    tokens: int = 0
    last_used: str = ""


class SkillLibraryOut(BaseModel):
    items: list[SkillLibraryItem] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    # 技能名 → 最近 7 天用量；没被调用过的技能不出现在这里。
    usage: dict[str, SkillUsageOut] = Field(default_factory=dict)
    usage_days: int = 7
    root: str = ""
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SkillInstallIn(BaseModel):
    """安装来源：一个 http(s) 地址（.md / .zip）、或本机目录 / 文件。"""

    source: str = Field(min_length=1, max_length=2000)
    scope: str = Field(default="user", pattern="^(user|project)$")
    project_id: str | None = None
    name: str = Field(default="", max_length=64)
    overwrite: bool = False


class SkillInstallOut(BaseModel):
    name: str
    path: str
    scope: str
    files: int
    source: str
    description: str = ""


class SkillUninstallIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    scope: str = Field(default="user", pattern="^(user|project)$")
    project_id: str | None = None
    # 技能库里那条记录的目录。多项目时按名字猜会删错项目，所以优先用它。
    path: str = Field(default="", max_length=2000)


class MarketSourceOut(BaseModel):
    id: str
    label: str
    kind: str
    repo: str = ""


class MarketItemOut(BaseModel):
    id: str
    name: str
    description: str
    source_id: str
    source_label: str


class MarketListOut(BaseModel):
    source: str
    items: list[MarketItemOut] = Field(default_factory=list)


class MarketHitOut(BaseModel):
    kind: str = "skill"
    name: str
    description: str = ""
    repo: str
    path: str = ""
    stars: int = 0
    url: str = ""
    updated_at: str = ""


class MarketSyncIn(BaseModel):
    """同步来源：https 目录地址，或直接给一份 payload（离线导入用）。"""

    url: str = Field(default="", max_length=2000)
    payload: dict | None = None
    source: str = Field(default="", max_length=200)


class MarketCatalogOut(BaseModel):
    synced_at: str = ""
    source: str = ""
    items: list["MarketHitOut"] = Field(default_factory=list)


class MarketSearchOut(BaseModel):
    query: str
    items: list[MarketHitOut] = Field(default_factory=list)
    # github = 实时搜索；local-cache = 网络不可用，回落到本机目录（带同步日期）。
    origin: str = "github"
    synced_at: str = ""
    notice: str = ""


class MarketInstallRepoIn(BaseModel):
    """按搜索结果安装：repo + path 直接来自 find_skills / search 的返回。"""

    repo: str = Field(min_length=3, max_length=200, pattern=r"^[\w.-]+/[\w.-]+$")
    path: str = Field(min_length=1, max_length=400)
    ref: str = Field(default="main", max_length=120)
    scope: str = Field(default="user", pattern="^(user|project)$")
    project_id: str | None = None
    name: str = Field(default="", max_length=64)
    overwrite: bool = False
    stars: int = 0


class MarketInstallIn(BaseModel):
    """从市场装一条：必须回传 list 给出的 install_ref，避免前端拼 URL。"""

    source: str = Field(min_length=1, max_length=500)
    item_id: str = Field(min_length=1, max_length=300)
    scope: str = Field(default="user", pattern="^(user|project)$")
    project_id: str | None = None
    name: str = Field(default="", max_length=64)
    overwrite: bool = False

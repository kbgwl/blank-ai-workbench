from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from blank_api.limits import DEFAULT_MAX_STEPS, HARD_MAX_STEPS
from blank_api.schemas.task import AttachmentIn


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
    # 场景（scenarios.py 五选一），与 TaskCreate.scenario 同名同义。
    scenario: Literal["", "dev", "docs", "daily", "brainstorm", "reverse"] = ""
    max_steps: int = Field(default=DEFAULT_MAX_STEPS, ge=1, le=HARD_MAX_STEPS)
    # 这一轮贴的图片。和 TaskCreate 同名同义：一轮任务就是一条消息。
    attachments: list[AttachmentIn] = Field(default_factory=list, max_length=8)
    # v0.10.1 §2：auto_start 由后端消费——True 时创建即入队/启动，不再等前端
    # 第二次调用 /start。None 表示交给监督者判定（decide_dispatch 的七条件）；
    # 显式 True 是用户的直接意愿（可越过 manual 模式的排队，越过不了 blocked /
    # wait_user 这类「环境本身不成立」的判定）；显式 False 永远不自动启动。
    auto_start: bool | None = None
    # v0.10.1 §4：发送栏的权限选择。非空时覆盖设置页默认档，并随任务快照固定——
    # 任务创建后再改全局默认，不影响已经在跑的这一轮。
    permission_mode: Literal["", "default", "accept_edits", "plan", "bypass"] = ""
    # 仅日常场景生效：True=记住习惯，False=不记住。与 TaskRunIn.memory_opt_in 同义，
    # 创建时带上就不必再靠 start 请求补发。
    memory_opt_in: bool = False


class ThreadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    title: str
    status: str
    forked_from: str | None
    turn_count: int
    # 最后一轮的状态（draft/running/succeeded/failed/cancelled），会话列表靠它画状态点。
    # 空串表示"这条会话还没有任何一轮"，与"跑完了"必须能区分：前者不该画点。
    last_turn_status: str = ""
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
    # 回放时能看到"当时它看到了什么图"。形状 {id, name, mime, size}。
    attachments: list[dict] = Field(default_factory=list)
    created_at: datetime

    @field_serializer("created_at")
    def _serialize_utc(self, value: datetime) -> str:
        aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        return aware.isoformat()


class ThreadDetailOut(ThreadOut):
    turns: list[ThreadTurnOut] = Field(default_factory=list)


class ThreadUpdate(BaseModel):
    """PATCH /threads/:id — 只更新传了的字段。"""

    title: str | None = Field(default=None, max_length=200)
    status: str | None = None  # "active" | "archived"


class ThreadRewindIn(BaseModel):
    """回退到第几轮（1 开始）。"""

    turn: int = Field(ge=1)


class ThreadRewindPointOut(BaseModel):
    """一个可选的回退点。

    ``restorable`` 说的是**代码**能不能回到这一轮结束时的样子：这需要这一轮
    之后有某个轮次留下了可恢复的快照（快照拍在写入之前，所以第 M+1 轮的快照
    正好是第 M 轮写完后的状态）。为 false 时 ``skipped_reason`` 必须说明原因——
    只报一个 false 而不说为什么，用户不知道该等什么。
    """

    turn: int
    task_id: str
    title: str
    status: str
    created_at: datetime
    restorable: bool
    snapshot_id: str = ""
    skipped_reason: str = ""

    @field_serializer("created_at")
    def _serialize_utc(self, value: datetime) -> str:
        aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        return aware.isoformat()


class ThreadRewoundTurnOut(BaseModel):
    id: str
    turn: int


class ThreadRewindOut(BaseModel):
    """回退的结果：对话做了什么、代码做了什么，分开说。

    ``atomic`` 恒为 false：对话截断先落库，代码恢复是随后的一次文件操作。
    快照缺失或复制失败时对话框已经截断，不会回滚——把它说成一次原子操作，
    用户会以为"要么都成、要么都没变"。
    """

    thread_id: str
    turn: int
    rewound: list[ThreadRewoundTurnOut] = Field(default_factory=list)
    snapshot_id: str = ""
    restored: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    skipped_reason: str = ""
    atomic: bool = False

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from blank_api.limits import DEFAULT_MAX_STEPS, HARD_MAX_STEPS


class TriggerIn(BaseModel):
    """上报一条事件。字段与 ``triggers.TriggerEvent`` 对应，多出的三项是"顺带建任务"。"""

    event_type: str = "user_task"
    source: str = Field(default="", max_length=120)
    payload: dict = Field(default_factory=dict)
    # 空 = 由服务端按 (类型, 来源, 关键事实) 算一个稳定摘要。外部系统能自带就更准。
    dedupe_key: str = Field(default="", max_length=120)
    # 要执行什么。不给 goal 就只登记事件、不建任务（空任务只会污染最近对话）。
    title: str = Field(default="", max_length=200)
    goal: str = ""
    provider_id: str | None = None
    scenario: Literal["", "dev", "docs", "daily", "brainstorm", "reverse"] = ""
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=20)
    max_steps: int = Field(default=DEFAULT_MAX_STEPS, ge=1, le=HARD_MAX_STEPS)
    memory_opt_in: bool = False
    # 到点时间。为空 = 等外部事件（不参与定时扫描）。
    fire_at: datetime | None = None
    expires_at: datetime | None = None
    # 去重窗口，默认 24 小时（见 triggers.DEFAULT_WINDOW）。
    window_seconds: int | None = Field(default=None, ge=60, le=30 * 24 * 3600)
    # 立刻开跑（仍需通过 Router 快照与 Worker 队列）。
    autostart: bool = False


class TriggerOut(BaseModel):
    id: str
    project_id: str
    event_type: str
    source: str = ""
    dedupe_key: str
    payload: dict = Field(default_factory=dict)
    task_id: str | None = None
    provider_id: str | None = None
    fire_at: str = ""
    expires_at: str = ""
    fired_at: str = ""
    status: str
    created_at: str = ""


class TriggerAck(BaseModel):
    """上报结果。``created=False`` 说明这次被去重了，``task_id`` 指向已有任务。"""

    created: bool
    trigger: TriggerOut
    task_id: str | None = None
    # 这次有没有真的把任务推进队列（autostart 且到点）。
    launched: bool = False
    run_id: str | None = None

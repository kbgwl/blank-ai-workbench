"""Persistent Agent event timeline (one increasing seq per task)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from blank_api.models.task import AgentEvent
from blank_api.services.log_safety import mask_deep

# 事件序号是"读最大值再插一条"，并行子代理共用一个记录器时两个协程会读到同一个 seq。
# 撞一次就重读再来；四次还不够说明系统真的在过载，那时把错误如实抛出去。
MAX_SEQ_ATTEMPTS = 5


class EventRecorder:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_id: str,
        trace_id: str = "",
        run_id: str = "",
        origin_run_id: str = "",
    ) -> None:
        self.session_factory = session_factory
        self.task_id = task_id
        self.trace_id = trace_id or task_id
        self.run_id = run_id
        # 发起轮：引起这一整棵任务树的那一轮。顶层运行的发起轮就是它自己，
        # 所以没单独给值时退回 run_id 不是兜底，是定义。空串读作"当时没记"。
        # 放在记录器上（而不是会话字段上）的理由：子代理共用父记录器，
        # 孙辈因此天然知道自己是被哪一轮引起的，不必再传一次、也不必自己猜。
        self.origin_run_id = origin_run_id or run_id

    async def add(self, event_type: str, node: str = "", payload: dict[str, Any] | None = None) -> int:
        # 时间线是这条链路上唯一"写进库又端给用户"的边界：审批卡的参数、工具摘要、
        # 停轮原因都从这里过。补丁里带的明文密钥若在这里原样落库，`/tasks/{id}/evidence`
        # 就把密钥回显给任何能打开这个任务的人——FileChange.diff 单独打码挡不住这一层
        # （方言端到端第 58 步实测到的：diff 干净，approval.required/tool.requested 还带着原件）。
        # 打码只动库里这份证据，不动检查点里模型真正拿去执行的那份参数，
        # 也不动用户批准之后落到工作区的文件。
        event_payload = mask_deep(dict(payload or {}))
        event_payload.setdefault("trace_id", self.trace_id)
        last_error: IntegrityError | None = None
        for _ in range(MAX_SEQ_ATTEMPTS):
            try:
                async with self.session_factory() as session:
                    previous = (
                        await session.execute(
                            select(AgentEvent)
                            .where(AgentEvent.task_id == self.task_id)
                            .order_by(AgentEvent.seq.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    last = previous.seq if previous is not None else 0
                    event_id = str(uuid.uuid4())
                    seq = int(last or 0) + 1
                    session.add(
                        AgentEvent(
                            id=event_id,
                            task_id=self.task_id,
                            run_id=self.run_id,
                            origin_run_id=self.origin_run_id,
                            step_id=event_id,
                            parent_event_id=previous.id if previous is not None else "",
                            seq=seq,
                            node=node,
                            type=event_type,
                            payload=event_payload,
                        )
                    )
                    await session.commit()
                return seq
            except IntegrityError as exc:
                # 丢事件比丢一次写入更糟：时间线是唯一的证据来源。重读最大值再来一次。
                last_error = exc
                await asyncio.sleep(0)
        assert last_error is not None
        raise last_error

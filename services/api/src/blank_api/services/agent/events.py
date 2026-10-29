"""Persistent Agent event timeline (one increasing seq per task)."""

from __future__ import annotations

import asyncio
import logging
import random
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

log = logging.getLogger(__name__)

# 终态事件：时间线上"这一轮已经结束"的唯一凭据。前端靠它停止轮询、解锁输入框，
# 台账靠它算一次任务的终点。写第二条不会让前端更早解锁，只会让"一轮结束了几次"
# 变成需要解释的问题，所以收尾前要先查有没有。
TERMINAL_EVENT_TYPES = (
    "task.completed",
    "task.failed",
    "task.cancelled",
    "task.canceled",
    "task.paused",
)


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
        # seq 是「读最大值再插一条」，同一记录器上的并发写会读到同一个号。捕获
        # IntegrityError 重试能兜住，但十二个协程同时重试时是**齐步走**的：大家在同一拍
        # 重读、同一拍再撞，几轮下来仍然有事件写不进去，而写不进去的事件是静默消失的
        # （时间线是唯一能拿给用户看的证据）。所以进程内先串行，跨进程/跨记录器再靠重试。
        # 2026-10-29 Linux runner 全量里 `test_parallel_children_do_not_lose_events_to_the_seq_race`
        # 报的就是 `UNIQUE constraint failed: agent_events.task_id, agent_events.seq`。
        self._seq_lock = asyncio.Lock()

    async def has_terminal(self) -> bool:
        """这一轮是否已经有终态事件。

        取消走的是「先改任务状态、再补一条事件」——改状态这一步运行时看不到，
        只有事件是双方都能读到的凭据。取消端点因此必须先落终态事件，否则界面
        会一直以为它还在跑；而运行时收尾时也要先问一句，避免同一轮结束两次。
        """
        # 查不到就按"还没有"处理：多写一条终态事件顶多让时间线多一行，
        # 而收尾路径上抛异常会让任务收不干净——两害相权，取前者。
        try:
            async with self.session_factory() as session:
                row = (
                    await session.execute(
                        select(AgentEvent.id)
                        .where(
                            AgentEvent.task_id == self.task_id,
                            AgentEvent.type.in_(TERMINAL_EVENT_TYPES),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
            return row is not None
        except Exception:  # noqa: BLE001
            log.exception("读取终态事件失败 task=%s", self.task_id)
            return False

    async def add(self, event_type: str, node: str = "", payload: dict[str, Any] | None = None) -> int:
        # 时间线是这条链路上唯一"写进库又端给用户"的边界：审批卡的参数、工具摘要、
        # 停轮原因都从这里过。补丁里带的明文密钥若在这里原样落库，`/tasks/{id}/evidence`
        # 就把密钥回显给任何能打开这个任务的人——FileChange.diff 单独打码挡不住这一层
        # （方言端到端第 58 步实测到的：diff 干净，approval.required/tool.requested 还带着原件）。
        # 打码只动库里这份证据，不动检查点里模型真正拿去执行的那份参数，
        # 也不动用户批准之后落到工作区的文件。
        event_payload = mask_deep(dict(payload or {}))
        event_payload.setdefault("trace_id", self.trace_id)
        # 串行段只包住「读最大值 + 插一条 + commit」：打码与拼载荷在锁外做，
        # 免得一次耗时的 mask 把整条时间线堵住。
        async with self._seq_lock:
            last_error: IntegrityError | None = None
            for attempt in range(MAX_SEQ_ATTEMPTS):
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
                    # 重试之间加一点随机退避：跨进程撞号时（另一个进程也在写同一个 task），
                    # 齐步走的 sleep(0) 会让两边的重读永远落在同一拍上。
                    last_error = exc
                    await asyncio.sleep(0.005 * (attempt + 1) * (0.5 + random.random()))
            assert last_error is not None
            raise last_error

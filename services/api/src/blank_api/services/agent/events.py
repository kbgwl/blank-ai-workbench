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

# 同一任务的所有记录器共用一把锁，但要按**事件循环**再隔一层。
#
# 为什么不能只挂在记录器实例上（v0.10.1）：后端在创建轮次时就地启动 runner，
# `create_turn` 手里那个记录器与后台 runner 自己的记录器会同时写同一条任务——
# 两把实例锁互不相干，重试 5 次仍可能齐步撞号（全量实测：`test_second_turn_
# knows_the_first_turn_had_images` 报 `UNIQUE constraint failed: agent_events.task_id,
# agent_events.seq`）。
#
# 为什么又要按循环隔离：测试装置里不同用例可能复用同一个 task_id，而每个用例
# 有自己的事件循环。上一个循环关闭时锁可能仍处于持锁态，下一个用例拿到同一把
# 死锁就永久等待（`test_parallel_budget_v26` 整文件跑会卡死在 acquire）。
# 锁挂在循环对象上：同循环内共享、跨循环天然隔离，循环销毁即随之回收。
_SEQ_LOCKS_ATTR = "_blank_seq_locks"


def _seq_lock_for(task_id: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = getattr(loop, _SEQ_LOCKS_ATTR, None)
    if locks is None:
        locks = {}
        setattr(loop, _SEQ_LOCKS_ATTR, locks)
    lock = locks.get(task_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[task_id] = lock
    return lock

log = logging.getLogger(__name__)


def _wake_subscribers(task_id: str) -> None:
    """本进程的 SSE 订阅者叫醒。导入放在函数里：``live`` 是纯内存模块，
    顶层互相 import 会把 events 和 live 绑成一个环（这条链已经在 agent/__init__ 里踩过）。"""
    try:
        from blank_api.services.agent.live import LiveFeed

        LiveFeed.wake(task_id)
    except Exception:  # pragma: no cover - 叫醒失败绝不能影响事件落库
        log.debug("唤醒事件流失败：task=%s", task_id, exc_info=True)

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
        # seq 是「读最大值再插一条」，两个记录器同时写同一条任务就会读到同一个号。
        # 锁按「事件循环 + task_id」共享（见 _seq_lock_for）：创建轮次的记录器与
        # 后台 runner 的记录器是不同实例，实例锁挡不住它们；跨进程仍靠 IntegrityError 重试。
        # 这里不预先取锁：构造记录器时未必处在事件循环里（同步测试会直接 new）。

    async def has_terminal(self, *, paused_counts: bool = False) -> bool:
        """这一轮是否已经有终态事件。

        取消走的是「先改任务状态、再补一条事件」——改状态这一步运行时看不到，
        只有事件是双方都能读到的凭据。取消端点因此必须先落终态事件，否则界面
        会一直以为它还在跑；而运行时收尾时也要先问一句，避免同一轮结束两次。

        ``paused_counts`` 默认 False：暂停不是"这一轮结束"，恢复后还要写
        task.completed。把 task.paused 当终态会吞掉恢复后的完成事件——任务做完了，
        界面上却停在"已暂停"。只有收尾本身就是 paused 时才传 True，防重复写。
        """
        # 查不到就按"还没有"处理：多写一条终态事件顶多让时间线多一行，
        # 而收尾路径上抛异常会让任务收不干净——两害相权，取前者。
        try:
            kinds = (
                TERMINAL_EVENT_TYPES
                if paused_counts
                else tuple(kind for kind in TERMINAL_EVENT_TYPES if kind != "task.paused")
            )
            async with self.session_factory() as session:
                row = (
                    await session.execute(
                        select(AgentEvent.id)
                        .where(
                            AgentEvent.task_id == self.task_id,
                            AgentEvent.type.in_(kinds),
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
        async with _seq_lock_for(self.task_id):
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
                    # 事件已经落库：叫醒本进程里等这条任务的事件流，省掉一整轮空轮询。
                    # 放在 commit 之后（不是之前）：先叫醒会让订阅者读到"还没有这条"，
                    # 于是它退避得更久，反而更慢。跨进程收不到这声唤醒，那边继续靠轮询。
                    _wake_subscribers(self.task_id)
                    return seq
                except IntegrityError as exc:
                    # 丢事件比丢一次写入更糟：时间线是唯一的证据来源。重读最大值再来一次。
                    # 重试之间加一点随机退避：跨进程撞号时（另一个进程也在写同一个 task），
                    # 齐步走的 sleep(0) 会让两边的重读永远落在同一拍上。
                    last_error = exc
                    await asyncio.sleep(0.005 * (attempt + 1) * (0.5 + random.random()))
            assert last_error is not None
            raise last_error

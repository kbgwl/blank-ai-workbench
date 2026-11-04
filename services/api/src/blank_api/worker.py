"""Standalone run worker: ``python -m blank_api.worker``.

HTTP 入口只负责把 run 写进 ``agent_runs``（``BLANK_INPROCESS_RUNNER=0`` 时连进程内
执行也关掉），这个进程负责认领与执行。数据库是唯一的真源：进程随时可以被杀掉，
下次启动继续从 queued / 租约过期的行往下领；已经写进 ``tool.completed`` 执行日志的
工具结果不会重跑（检查点只到节点边界，粒度不够——见 docs/REA-WAVE4-CODEX-RUNTIME §7.56）。

环境变量：
    BLANK_WORKER_ONCE=1            扫一轮就退出（探针/一次性排空）
    BLANK_WORKER_POLL_SECONDS      空闲轮询间隔，默认 2 秒
    BLANK_WORKER_LEASE_SECONDS     认领租约，默认 90 秒；心跳取 min(30 秒, 租约/3)
    BLANK_WORKER_ID                固定 worker 身份（默认 主机名-随机串）
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from datetime import timedelta

from sqlalchemy import or_, select

from blank_api.api.routes import tasks as task_routes
from blank_api.db import get_session_factory
from blank_api.models.run import AgentRun
from blank_api.models.task import Task
from blank_api.models.trigger import AgentTrigger
from blank_api.schemas.task import TaskRunIn
from blank_api.services import metrics
from blank_api.services.agent import triggers as triggers_module
from blank_api.services.agent import watchdog as watchdog_module
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.queue import abandon_run, claim_run, finish_run, utc_now
from blank_api.services.features import is_enabled

log = logging.getLogger("blank_api.worker")

POLL_SECONDS = float(os.environ.get("BLANK_WORKER_POLL_SECONDS") or 2)
LEASE_SECONDS = int(os.environ.get("BLANK_WORKER_LEASE_SECONDS") or 90)
ONCE = (os.environ.get("BLANK_WORKER_ONCE") or "0").strip().lower() in ("1", "true", "yes", "on")


def worker_id() -> str:
    configured = (os.environ.get("BLANK_WORKER_ID") or "").strip()
    if configured:
        return configured
    return f"{socket.gethostname() or 'worker'}-{uuid.uuid4().hex[:8]}"


async def _candidates() -> list[tuple[str, str, int]]:
    """(run_id, task_id, attempts)：此刻可以被认领的行。"""
    now = utc_now()
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(AgentRun.id, AgentRun.task_id, AgentRun.attempts)
                .where(
                    or_(
                        AgentRun.status == "queued",
                        (AgentRun.status == "running")
                        & (
                            (AgentRun.lease_expires_at.is_(None))
                            # 与 claim_run 同一条边界：等于 now 就算过期，否则这一拍扫不到它
                            | (AgentRun.lease_expires_at <= now)
                        ),
                    )
                )
                .order_by(AgentRun.created_at, AgentRun.id)
            )
        ).all()
    return [(row[0], row[1], int(row[2] or 0)) for row in rows]


async def _give_up(run_id: str, task_id: str, message: str) -> bool:
    """判死一条不再重试的 run，并把原因写进任务状态。"""
    async with get_session_factory()() as session:
        if not await abandon_run(session, run_id):
            # 租约还新鲜：另一个进程正在跑，不能替它判死
            log.warning("跳过判死：run=%s 仍被现役 worker 持有", run_id)
            return False
    await task_routes._record_run_failure(task_id, run_id, RuntimeError(message))
    return True


async def _fail_claimed(run_id: str, task_id: str, worker: str, message: str) -> None:
    """认领之后才发现跑不了：以 owner 身份写 failed，否则租约还新鲜、abandon 判不了死。"""
    async with get_session_factory()() as session:
        await finish_run(session, run_id, "failed", worker)
    await task_routes._record_run_failure(task_id, run_id, RuntimeError(message))


async def _request_for(run_id: str):
    """从 run 的入参快照重建 RunRequest；和启动恢复走同一个构造函数。"""
    async with get_session_factory()() as session:
        run = await session.get(AgentRun, run_id)
        if run is None:
            return None
        task = await session.get(Task, run.task_id)
        if task is None:
            return None
        try:
            payload = TaskRunIn.model_validate(run.request_payload or {})
        except Exception:
            return None
        request = await task_routes._run_request(session, task, payload, None)
        request.run_id = run_id
        return request


async def expire_stale_approvals() -> int:
    """把「审批已过期、也没人再来点」的任务收敛成失败。

    审批卡有有效期（默认 30 分钟，``BLANK_APPROVAL_TTL_SECONDS`` 可调）。过期之后
    那张卡再也批不动——提交会拿到 409。但任务状态原先一直停在 ``waiting_approval``：
    界面写着「进行中」，用户既批不了、也没有重试入口，只能关掉重开一轮。真实 GUI
    验收里这个死状态连撞三次（两次是我离开工位超过 5 分钟，一次是点了才被告知过期）。

    收敛成 failed 而不是悄悄删除：界面因此给出「重试」，用户知道发生了什么。
    留一条 ``approval.expired`` 事件说明原因，事后能追。
    """
    if not is_enabled("background_execution"):
        return 0
    from blank_api.services.agent.graph import approval_ttl_seconds

    cutoff = utc_now() - timedelta(seconds=approval_ttl_seconds() + 60)
    expired: list[str] = []
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(Task.id).where(
                    Task.status == "waiting_approval",
                    Task.updated_at <= cutoff,
                )
            )
        ).scalars().all()
        for task_id in rows:
            task = await session.get(Task, task_id)
            if task is None or task.status != "waiting_approval":
                continue
            task.status = "failed"
            task.failure_reason = (
                "审批已过期：这张审批卡超过有效期没人处理，本轮已结束。需要继续请点「重试」。"
            )
            task.updated_at = utc_now()
            expired.append(task_id)
        if expired:
            await session.commit()
    for task_id in expired:
        try:
            recorder = EventRecorder(get_session_factory(), task_id)
            await recorder.add(
                "approval.expired",
                "worker",
                {"reason": "审批卡超过有效期没人处理，任务已收敛为失败，可重试"},
            )
            await recorder.add("task.failed", "worker", {"status": "failed", "reason": "approval_expired"})
        except Exception:
            log.exception("记录审批过期事件失败 task=%s", task_id)
    if expired:
        log.warning("审批过期收敛 %d 条任务：%s", len(expired), ",".join(t[:8] for t in expired))
    return len(expired)


async def run_once(worker: str | None = None) -> int:
    """扫一轮队列，返回这一轮真正被本进程领走的 run 数量。"""
    worker = worker or worker_id()
    handled = 0
    # 审批过期收敛（v163 验收补）：先清掉再也走不下去的等待，别让它一直挂着"进行中"。
    try:
        await expire_stale_approvals()
    except Exception:
        log.exception("审批过期收敛失败（不影响本轮认领）")
    # 先放到点的触发器（v162）：它们会产生新的 queued run，紧接着的那一轮扫描
    # 就能认领，不必等下一次轮询。开关关着时这一步什么都不做。
    await release_due_triggers()
    # 巡检（v162）：只看已存在的 run，不改状态。放在认领之前，这样"刚刚到期"的
    # 租约能先被记一笔诊断，而不是被下一轮认领之后再也看不到。
    await inspect_runs()
    for run_id, task_id, attempts in await _candidates():
        if attempts >= task_routes.MAX_RECOVERY_ATTEMPTS:
            metrics.inc(metrics.WORKER_ABANDONED)
            await _give_up(
                run_id,
                task_id,
                f"这条任务连续 {attempts} 次没能正常启动（上一次执行没有正常收尾），"
                "已停止自动重试。请点「重试」重新发起。",
            )
            continue
        if not await _claim(run_id, worker):
            continue
        handled += 1
        crashed = attempts > 0
        if crashed:
            # 上一次认领后没写终态（进程被杀/断电）。这个数就是"崩溃恢复真的发生过几次"，
            # 没有它，"我们从崩溃里恢复得怎么样"只能靠日志翻。
            metrics.inc(metrics.WORKER_RECOVERED)
        try:
            request = await _request_for(run_id)
        except Exception as error:
            log.exception("重建执行入参失败 run=%s", run_id)
            await _fail_claimed(run_id, task_id, worker, f"无法恢复执行入参：{error}")
            continue
        if request is None:
            await _fail_claimed(run_id, task_id, worker, "任务或运行记录已不存在，无法继续执行")
            continue
        try:
            await task_routes._execute_claimed_run(
                run_id,
                worker,
                request,
                crashed=crashed,
                lease_seconds=LEASE_SECONDS,
            )
        except asyncio.CancelledError:
            # 进程被要求停下：租约留给下一个进程接管
            raise
        except Exception:
            # _execute_claimed_run 自己会写 task.failed；这里只保证不把整轮循环带走
            log.exception("执行失败 run=%s", run_id)
        metrics.inc(metrics.WORKER_RUN_FINISHED)
    return handled


async def release_due_triggers() -> int:
    """把到点的触发器推进 run 队列（v162 §5 的本地定时器）。

    只入队、不在这里执行：执行路径必须只有一条（worker 的认领循环），否则
    "谁在跑这条任务"会出现两个答案，而租约只能挡住其中一个。

    失败一条不影响其他条：触发器是个别事件，一条跑不起来不该让整轮扫描停下。
    """
    if not is_enabled("background_execution"):
        return 0
    released = 0
    async with get_session_factory()() as session:
        await triggers_module.expire_stale(session)
        await session.commit()
        rows = await triggers_module.due(session)
        trigger_ids = [row.id for row in rows]
    for trigger_id in trigger_ids:
        try:
            async with get_session_factory()() as session:
                row = await session.get(AgentTrigger, trigger_id)
                if row is None or row.status != triggers_module.STATUS_PENDING:
                    continue
                task = await session.get(Task, row.task_id) if row.task_id else None
                if task is None:
                    row.status = triggers_module.STATUS_SKIPPED
                    row.payload = {**dict(row.payload or {}), "error": "关联任务不存在"}
                    await session.commit()
                    continue
                if task.status in ("running", "queued"):
                    # 已经有人在跑：标记成已放行即可，不重复入队。
                    await triggers_module.mark_fired(session, row)
                    await session.commit()
                    continue
                payload = TaskRunIn.model_validate(row.run_payload or {})
                await task_routes.launch_task(session, task, payload, None, start_runner=False)
                await triggers_module.mark_fired(session, row)
                await session.commit()
                released += 1
        except Exception as error:  # noqa: BLE001 - 单条失败不该带走整轮
            log.warning("触发器放行失败 trigger=%s: %s", trigger_id, error)
    return released


async def inspect_runs() -> int:
    """Watchdog 一轮巡检（v162，开关 watchdog）。返回这一轮新记下的告警条数。

    刻意不在巡检里改 run 状态：判死与放行都由正常的认领路径负责。观察者一旦
    动手改状态，出问题时就没有第三方能回答"当时它看到的是什么"。
    """
    if not is_enabled("watchdog"):
        return 0
    try:
        async with get_session_factory()() as session:
            alerts = await watchdog_module.sweep(session)
        return len(alerts)
    except Exception:  # noqa: BLE001 - 巡检失败绝不能带走 worker 主循环
        log.warning("Watchdog 巡检失败", exc_info=True)
        return 0


async def _claim(run_id: str, worker: str) -> bool:
    async with get_session_factory()() as session:
        return await claim_run(session, run_id, worker, lease_seconds=LEASE_SECONDS)


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    worker = worker_id()
    log.info(
        "worker %s 启动：poll=%.1fs lease=%ss once=%s",
        worker,
        POLL_SECONDS,
        LEASE_SECONDS,
        ONCE,
    )
    while True:
        try:
            handled = await run_once(worker)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 数据库短暂不可用不该让 worker 进程退出，下一轮再试
            log.exception("扫描队列失败")
            handled = 0
        if ONCE:
            return 0
        if handled:
            continue
        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(0)

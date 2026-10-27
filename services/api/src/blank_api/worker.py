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

from sqlalchemy import or_, select

from blank_api.api.routes import tasks as task_routes
from blank_api.db import get_session_factory
from blank_api.models.run import AgentRun
from blank_api.models.task import Task
from blank_api.schemas.task import TaskRunIn
from blank_api.services.agent.queue import abandon_run, claim_run, finish_run, utc_now

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
                            | (AgentRun.lease_expires_at < now)
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


async def run_once(worker: str | None = None) -> int:
    """扫一轮队列，返回这一轮真正被本进程领走的 run 数量。"""
    worker = worker or worker_id()
    handled = 0
    for run_id, task_id, attempts in await _candidates():
        if attempts >= task_routes.MAX_RECOVERY_ATTEMPTS:
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
    return handled


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

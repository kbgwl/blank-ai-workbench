"""存储与生命周期：算清占用、按引用关系安全清理。

这一组端点刻意做成两个：``GET /maintenance/storage`` 只读，``POST /maintenance/purge``
才会动文件，而且**默认 dry-run**——请求体里不写 ``dry_run: false`` 就只报"会删什么"。

为什么不放在后台自动跑：这些文件是用户排障时的证据（工具完整输出、工作区快照），
自动删掉之后"为什么这条任务打不开"就再也查不出来了。清理必须是有人按下去的按钮。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select

from blank_api.api.auth import require_token
from blank_api.db import get_session_factory
from blank_api.models.task import Task
from blank_api.services import maintenance

router = APIRouter(
    prefix="/maintenance", tags=["maintenance"], dependencies=[Depends(require_token)]
)


@router.get("/storage")
async def storage() -> dict:
    """当前占用与可回收量。数字全部现算，不做缓存——缓存过的占用报表会骗人。"""
    return await maintenance.storage_report(get_session_factory())


@router.get("/metrics")
async def process_metrics() -> dict:
    """进程内指标读数：模型超时/重试/切备用、worker 接管、SSE 连接与轮询、产物写入。

    口径写在 ``services/metrics.py`` 里，读之前先看一眼：**进程重启归零**，
    跨重启的历史在事件表与用量表里。多进程部署时每个进程各一份，这里只报本进程。
    """
    from blank_api.services import metrics

    return metrics.snapshot()


@router.post("/purge")
async def purge(payload: dict | None = None) -> dict:
    """按 body 清理（或瘦身）指定的几类数据，默认 dry-run。

    支持的键：
    * ``artifacts``：``{ttl_days, dry_run}`` —— 过期且无人引用的工具完整输出
    * ``snapshots``：``{keep_per_task, ttl_days, dry_run}`` —— 超出保留份数的旧快照
    * ``orphans``：``{dry_run}`` —— 没有任何任务指向的事件/工具/改动行
    * ``events``：``{retention_days, dry_run}`` —— 终态任务的超期事件**裁 payload**（不删行）

    没写的段一律不动。这个端点永远不会"猜测你想要的默认值"。
    用量账本没有对应段：它不参与自动清理（行即历史事实），读数在 ``GET /maintenance/storage``。
    """
    body = dict(payload or {})
    factory = get_session_factory()
    result: dict = {"applied": sorted(body.keys())}
    if "artifacts" in body:
        section = dict(body["artifacts"] or {})
        result["artifacts"] = await maintenance.purge_artifacts(
            factory,
            ttl_days=float(section.get("ttl_days", maintenance.DEFAULT_ARTIFACT_TTL_DAYS)),
            dry_run=bool(section.get("dry_run", True)),
        )
    if "snapshots" in body:
        section = dict(body["snapshots"] or {})
        async with factory() as session:
            protected_task_ids = {
                str(task_id)
                for task_id in (
                    await session.execute(
                        select(Task.id).where(
                            Task.status.in_(maintenance.ACTIVE_TASK_STATUSES)
                        )
                    )
                ).scalars().all()
            }
        result["snapshots"] = maintenance.purge_snapshots(
            keep_per_task=int(
                section.get("keep_per_task", maintenance.DEFAULT_KEEP_SNAPSHOTS_PER_TASK)
            ),
            ttl_days=float(section.get("ttl_days", 30)),
            dry_run=bool(section.get("dry_run", True)),
            protected_task_ids=protected_task_ids,
        )
    if "events" in body:
        section = dict(body["events"] or {})
        result["events"] = await maintenance.compact_events(
            factory,
            retention_days=float(
                section.get("retention_days", maintenance.DEFAULT_EVENT_RETENTION_DAYS)
            ),
            dry_run=bool(section.get("dry_run", True)),
        )
    if "orphans" in body:
        section = dict(body["orphans"] or {})
        result["orphans"] = await maintenance.purge_orphans(
            factory, dry_run=bool(section.get("dry_run", True))
        )
        if not section.get("dry_run", True):
            # 任务已经被删、快照还挂在盘上的那部分，跟着孤儿一起收。
            async with factory() as session:
                alive = {str(row) for row in (await session.execute(select(Task.id))).scalars().all()}
            result["snapshot_orphans"] = maintenance.purge_orphan_snapshots(
                alive_task_ids=alive, dry_run=False
            )
    return result

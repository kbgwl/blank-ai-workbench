"""近期负载：把 ``model_usage`` 里最近 N 分钟的实际表现压成每个供应商一行。

为什么要读库，而不是只数本次任务：一次任务内部的偏心是暂时的，真正的信号在
**上一次任务**身上——如果过去十几分钟所有调用都压在 A 上（B 一直 429、或者用户
刚把 A 设成主用），这一次一开头就该往 B 走。只靠内存里的计数器永远看不到这件事。

反过来也有个坑：库里的历史**不能**当成精确配额。一个「100 次 vs 0 次」的窗口如果
照单兑现，下一个任务会连续几十步都补在同一个模型上，比不均衡更难解释。所以
``router`` 那边把它截到 ``LOAD_SEED_CAP``——这里只负责「如实统计」，不做取舍。

失败一律当「没有数据」：负载是锦上添花，不该成为任务起不来的理由。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from blank_api.models.model_provider import ModelUsage
from blank_api.services.agent.router import LOAD_WINDOW_MINUTES, LoadStat


def _utcnow() -> datetime:
    """按 UTC 取当前时间，且不带时区。

    ``created_at`` 是库端的 ``CURRENT_TIMESTAMP``，写进去的就是 UTC；这里用本地时间
    去比会让窗口整体偏掉时区差（东八区就是 8 小时），表现为「最近什么都没发生」。
    也不用 ``func.datetime('now', '-15 minutes')``：那是 SQLite 方言。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def recent_load(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    minutes: int = LOAD_WINDOW_MINUTES,
    providers: Any = None,
) -> dict[str, LoadStat]:
    """近 ``minutes`` 分钟每个供应商的调用数、失败数、token 与平均延迟。

    ``providers`` 只用来缩小扫描范围（传本次任务的候选 id 集合）；不传就统计全量，
    结果一样，只是多扫几行——所以它不是正确性的一部分。
    """
    window = max(1, int(minutes))
    cutoff = _utcnow() - timedelta(minutes=window)
    failures = func.sum(case((ModelUsage.status != "ok", 1), else_=0))
    tokens = func.sum(ModelUsage.prompt_tokens + ModelUsage.completion_tokens)
    latency = func.avg(ModelUsage.latency_ms)
    query = (
        select(ModelUsage.provider_id, func.count(ModelUsage.id), failures, tokens, latency)
        .where(ModelUsage.created_at >= cutoff)
        .group_by(ModelUsage.provider_id)
    )
    ids = [str(item) for item in (providers or ()) if str(item).strip()]
    if ids:
        query = query.where(ModelUsage.provider_id.in_(ids))

    try:
        async with session_factory() as session:
            rows = (await session.execute(query)).all()
    except Exception:  # noqa: BLE001 - 老库缺列、表还没建好，都不值得让任务起不来
        return {}

    stats: dict[str, LoadStat] = {}
    for provider_id, calls, bad, total_tokens, average_latency in rows:
        if not provider_id:
            continue
        stats[str(provider_id)] = LoadStat(
            calls=int(calls or 0),
            failures=int(bad or 0),
            tokens=int(total_tokens or 0),
            latency_ms=int(average_latency or 0),
            window=window,
        )
    return stats

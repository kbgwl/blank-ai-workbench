"""用量台账：把「花了多少 token」拆成「钱换成了什么」。

只看总额没有意义——900 万 token 里可能 700 万出自一个跑废的任务。这里按任务把
花销归到三个去处：

- ``delivered`` 有文件改动且成功：钱换成了产物
- ``advised``   成功但没有改动：钱换成了判断（调研/问答），本该很便宜
- ``wasted``    失败、取消、跑飞：钱换成了废纸

口径与 ``scripts/token_ledger.py`` 必须一致：那个脚本是运维侧只读视图，这里是
应用侧同一个答案。两处数字对不上，用户就没有理由相信任何一个。

四道判据：

- ``per_call_context`` 单次调用的平均上下文。成本的地基：步数不变、上下文翻倍，
  账单就翻倍。
- ``cache_hit``        命中缓存的比例。命中部分单价低，但绝对量仍在。
- ``failed_tool_rate`` 工具失败率。失败一次要重试，是「多跑几步」的源头。
- ``tokens_per_file``  每个有效改动文件的 token。跨模型、跨任务可比。
- ``blocked_calls``    被空转断路器拦掉的调用数。它回答的是另一个问题：
  失败率说明"错了多少"，拦截数说明"错了之后没有继续白跑多少"。只有应用侧有
  （事件流里的事件），``scripts/token_ledger.py`` 那份只读视图没有这一项。
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from blank_api.models.model_provider import ModelUsage
from blank_api.models.task import AgentEvent, FileChange, Task, ToolCall

DELIVERED_STATUSES = {"succeeded", "completed"}
DEAD_STATUSES = {"failed", "cancelled", "error"}
BUCKETS = ("delivered", "advised", "wasted", "running")
MAX_TASKS = 500


def bucket_of(record: dict) -> str:
    if record["status"] in DEAD_STATUSES:
        return "wasted"
    if record["status"] not in DELIVERED_STATUSES:
        # running / waiting_approval：钱还没结算，算进任何一类都会失真。
        return "running"
    return "delivered" if record["files"] > 0 else "advised"


def summarize(records: list[dict]) -> dict:
    total = sum(item["tokens"] for item in records)
    by_bucket: dict[str, dict] = {}
    for key in BUCKETS:
        # 缺 bucket 就现算：调用方可能是只拿到原始记录的外部脚本，不该要求它先跑一遍归类。
        rows = [item for item in records if (item.get("bucket") or bucket_of(item)) == key]
        tokens = sum(item["tokens"] for item in rows)
        by_bucket[key] = {
            "tasks": len(rows),
            "tokens": tokens,
            "share": round(tokens / total, 4) if total else 0.0,
        }
    calls = sum(item["calls"] for item in records)
    prompt = sum(item["prompt_tokens"] for item in records)
    cached = sum(item["cached_tokens"] for item in records)
    tools = sum(item["tools"] for item in records)
    failed_tools = sum(item["failed_tools"] for item in records)
    files = sum(item["files"] for item in records)
    delivered = [
        item["tokens"] for item in records if (item.get("bucket") or bucket_of(item)) == "delivered"
    ]
    return {
        "tasks": len(records),
        "calls": calls,
        "prompt_tokens": prompt,
        "completion_tokens": sum(item["completion_tokens"] for item in records),
        "total_tokens": total,
        "cache_hit": round(cached / prompt, 4) if prompt else 0.0,
        "per_call_context": round(prompt / calls) if calls else 0,
        "peak_context": max((item["peak_context"] for item in records), default=0),
        "failed_tool_rate": round(failed_tools / tools, 4) if tools else 0.0,
        # 每个工具动作对应几次模型调用。一次模型调用带多个工具时会小于 1，
        # 所以只看上限：>2 说明在空转（反复审查、反复重试），每步都在重发整段上下文。
        "calls_per_tool": round(calls / tools, 2) if tools else 0.0,
        "tools": tools,
        # 被空转断路器拦掉的调用：没有真实执行，也没有那一轮上下文。
        # 用 .get 是因为这个函数也吃外部脚本传进来的原始记录，那些没有这一项。
        "blocked_calls": sum(int(item.get("blocked_calls") or 0) for item in records),
        "files": files,
        "buckets": by_bucket,
        "median_task_tokens": int(statistics.median([item["tokens"] for item in records])) if records else 0,
        "median_delivered_tokens": int(statistics.median(delivered)) if delivered else 0,
        "tokens_per_file": round(by_bucket["delivered"]["tokens"] / files) if files else 0,
        # 唯一一个「越低越该改」的总分：钱真正换成产物的比例。
        "effective_share": by_bucket["delivered"]["share"],
    }


def top_offenders(records: list[dict], limit: int = 5) -> list[dict]:
    rows = sorted(records, key=lambda item: item["tokens"], reverse=True)[:limit]
    return [
        {
            "id": item["id"],
            "title": item["title"],
            "status": item["status"],
            "calls": item["calls"],
            "tokens": item["tokens"],
            "files": item["files"],
            "failed_tools": item["failed_tools"],
            # 逐个任务给「几次调用才换一次工具动作」：空转是单任务现象，混在总账里会被平均掉。
            "per_tool": round(item["calls"] / item["tools"], 1) if item["tools"] else 0.0,
            "blocked_calls": int(item.get("blocked_calls") or 0),
        }
        for item in rows
    ]


async def build_ledger(session: AsyncSession, days: int = 7) -> dict:
    """最近 ``days`` 天的台账。一次聚合查询，不做 N+1。"""
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max(days, 1))
    task_rows = (
        await session.execute(
            select(
                Task.id,
                Task.title,
                Task.status,
                Task.steps_used,
                Task.created_at,
                Task.failure_reason,
            )
            .where(Task.created_at >= since)
            .order_by(Task.created_at.desc())
            .limit(MAX_TASKS)
        )
    ).all()
    ids = [row.id for row in task_rows]
    if not ids:
        return {
            "window_days": days,
            "summary": summarize([]),
            "buckets": summarize([])["buckets"],
            "offenders": [],
            "tasks": [],
        }

    usage = {
        row[0]: row
        for row in (
            await session.execute(
                select(
                    ModelUsage.task_id,
                    func.count().label("calls"),
                    func.coalesce(func.sum(ModelUsage.prompt_tokens), 0).label("prompt"),
                    func.coalesce(func.sum(ModelUsage.completion_tokens), 0).label("completion"),
                    func.coalesce(func.sum(ModelUsage.cached_tokens), 0).label("cached"),
                    func.coalesce(func.max(ModelUsage.prompt_tokens), 0).label("peak"),
                )
                .where(ModelUsage.task_id.in_(ids))
                .group_by(ModelUsage.task_id)
            )
        ).all()
    }
    tools = {
        row[0]: row
        for row in (
            await session.execute(
                select(
                    ToolCall.task_id,
                    # 被空转拦下的那次没有执行，不算一个"工具动作"：算进去会把
                    # calls_per_tool 的分母撑大，让真正的空转看起来更健康。
                    func.coalesce(
                        func.sum(case((ToolCall.status != "blocked", 1), else_=0)), 0
                    ).label("total"),
                    # 失败率只数"跑了但错了"。把拦截算成失败，就等于说"这个工具不可靠"，
                    # 而事实是"我们没让它重复"——两种结论指向完全不同的下一步。
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    (ToolCall.status != "ok") & (ToolCall.status != "blocked"),
                                    1,
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("failed"),
                )
                .where(ToolCall.task_id.in_(ids))
                .group_by(ToolCall.task_id)
            )
        ).all()
    }
    file_rows = (
        await session.execute(
            select(FileChange.task_id, FileChange.path)
            .where(FileChange.task_id.in_(ids))
            .distinct()
        )
    ).all()
    file_counts: dict[str, int] = {}
    for task_id, _path in file_rows:
        file_counts[task_id] = file_counts.get(task_id, 0) + 1

    # 空转拦截数：只数事件，不重算判定——判定发生在执行那一刻，事后重算会得出
    # 一个"当时并没有发生"的数（参数、上下文都可能已经变了）。
    blocked_counts = {
        row[0]: int(row[1])
        for row in (
            await session.execute(
                select(AgentEvent.task_id, func.count().label("blocked"))
                .where(AgentEvent.task_id.in_(ids), AgentEvent.type == "tool.blocked")
                .group_by(AgentEvent.task_id)
            )
        ).all()
    }

    records: list[dict] = []
    for row in task_rows:
        usage_row = usage.get(row.id)
        prompt = int(usage_row.prompt) if usage_row else 0
        completion = int(usage_row.completion) if usage_row else 0
        tool_row = tools.get(row.id)
        record = {
            "id": row.id,
            "title": (row.title or "").strip()[:40],
            "status": row.status,
            "created_at": row.created_at.isoformat() if row.created_at else "",
            "steps": int(row.steps_used or 0),
            "calls": int(usage_row.calls) if usage_row else 0,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cached_tokens": int(usage_row.cached) if usage_row else 0,
            "tokens": prompt + completion,
            "peak_context": int(usage_row.peak) if usage_row else 0,
            "tools": int(tool_row.total) if tool_row else 0,
            "failed_tools": int(tool_row.failed) if tool_row else 0,
            "files": file_counts.get(row.id, 0),
            "blocked_calls": blocked_counts.get(row.id, 0),
            "failure_reason": (row.failure_reason or "")[:120],
        }
        record["bucket"] = bucket_of(record)
        records.append(record)

    summary = summarize(records)
    return {
        "window_days": days,
        "summary": summary,
        "buckets": summary["buckets"],
        "offenders": top_offenders(records),
        # 逐条明细最多给 50 条：台账页要的是"哪几笔最贵"，不是把所有任务列一遍。
        "tasks": sorted(records, key=lambda item: item["tokens"], reverse=True)[:50],
    }

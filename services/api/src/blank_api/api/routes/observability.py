from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from blank_api.api.auth import require_token
from blank_api.deps import DbSession
from blank_api.models.model_provider import PROVIDER_GONE, ModelProvider, ModelUsage
from blank_api.models.run import AgentRun
from blank_api.models.task import AgentEvent, ToolCall
from blank_api.services.models.pricing import estimate_cost

router = APIRouter(prefix="/observability", tags=["observability"], dependencies=[Depends(require_token)])


def _role(node: str) -> str:
    return {
        "create_plan": "planner",
        "execute_action": "coder",
        "review_result": "reviewer",
        "summarize_result": "reviewer",
    }.get(node, "tester" if node == "run_validation" else "runtime")


# 供应商这一行已经不在了（被删掉，或者这笔账落下时就没有对应的配置行）。钱是真花出去的，
# 所以这里必须用左连接，并且给这些行一个念得出人话的桶名（PROVIDER_GONE）——直接 outerjoin
# 会得到 "None/模型名"，既读不出含义，又会把孤儿当成一家供应商。


@router.get("/ledger")
async def observability_ledger(db: DbSession, days: int = 7) -> dict:
    """用量台账：钱换成了产物、判断，还是废纸。

    与 ``/summary`` 分开是有意的：那个回答「谁在花」，这个回答「花得值不值」。
    放进同一张表会让两个问题互相稀释。
    """
    from blank_api.services.usage_ledger import build_ledger

    return await build_ledger(db, days=max(1, min(int(days), 90)))


@router.get("/goals")
async def observability_goals(db: DbSession, limit: int = 50) -> dict:
    """v165 §15：活跃目标、后台任务、队列长度、Worker 健康的同一份读数。

    以前这些数散在会话列表、任务列表、run 表里，回答「现在到底有几个目标在跑、
    有没有卡住的 worker」要开三个页面对照。这里一次给全，而且全部按库里的状态
    算，不看 Agent 自述：``orphans`` 是「已结束状态却还留在后台列表」的任务数，
    正常应为 0。
    """
    from datetime import datetime, timezone

    from blank_api.models.goal import TERMINAL_GOAL_STATUSES, Goal
    from blank_api.models.task import Task

    cap = max(1, min(int(limit), 200))
    goals = (
        await db.execute(
            select(Goal).where(Goal.status.notin_(sorted(TERMINAL_GOAL_STATUSES)))
            .order_by(Goal.last_active_at.desc()).limit(cap)
        )
    ).scalars().all()
    # draft 也要算：v165 的自动下发是「先建 draft，再由前端按 dispatch.auto_start
    # 点运行」，把这一段漏掉就会出现「目标说执行中、后台列表是空的」的老毛病。
    active_statuses = ("draft", "queued", "running", "waiting_approval", "paused", "failed")
    tasks = (
        await db.execute(
            select(Task).where(Task.status.in_(active_statuses))
            .order_by(Task.updated_at.desc()).limit(cap)
        )
    ).scalars().all()
    run_rows = (
        await db.execute(select(AgentRun.status, func.count()).group_by(AgentRun.status))
    ).all()
    run_counts = {status: count for status, count in run_rows}
    leased = (
        await db.execute(
            select(func.count()).select_from(AgentRun).where(AgentRun.status == "running")
        )
    ).scalar_one()
    stale = (
        await db.execute(
            select(func.count()).select_from(AgentRun).where(
                AgentRun.status == "running",
                AgentRun.lease_expires_at.is_not(None),
                AgentRun.lease_expires_at < datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
    ).scalar_one()
    by_status: dict[str, int] = defaultdict(int)
    for task in tasks:
        by_status[str(task.status)] += 1
    return {
        "goals": [
            {
                "goal_id": g.id,
                "conversation_id": g.conversation_id,
                "project_id": g.project_id,
                "title": g.title,
                "status": g.status,
                "scenario": g.scenario,
                "automation_mode": g.automation_mode,
                "current_task_id": g.current_task_id,
                "blocked_reason": g.blocked_reason,
                "next_action": g.next_action,
                "updated_at": g.updated_at.isoformat() if g.updated_at else "",
            }
            for g in goals
        ],
        "background_tasks": [
            {
                "task_id": t.id,
                "conversation_id": str(t.thread_id or ""),
                "goal_id": str((t.goal_spec or {}).get("goal_id") or ""),
                "title": t.title,
                "status": t.status,
            }
            for t in tasks
        ],
        "by_status": dict(by_status),
        "queue_length": run_counts.get("queued", 0),
        "runs": run_counts,
        "worker": {"running": leased, "stale_leases": stale, "healthy": stale == 0},
        # 只数 v0.10.0 的任务：v0.9.1 走 /projects/{id}/tasks 的旧轮次本来就没有
        # supervisor 段，把它们算成孤儿会让这个读数永远是脏的（同 §16 指标口径）。
        "orphans": sum(
            1
            for t in tasks
            if (t.goal_spec or {}).get("supervisor")
            and not str((t.goal_spec or {}).get("goal_id") or "")
        ),
    }


@router.get("/summary")
async def observability_summary(db: DbSession) -> dict:
    model_rows = (
        await db.execute(
            select(
                ModelProvider.name,
                ModelUsage.model,
                ModelUsage.node,
                ModelUsage.status,
                ModelUsage.prompt_tokens,
                ModelUsage.completion_tokens,
                ModelUsage.latency_ms,
                ModelUsage.trace_id,
                ModelUsage.cached_tokens,
            ).outerjoin(ModelProvider, ModelProvider.id == ModelUsage.provider_id)
        )
    ).all()
    role_totals: dict[str, dict] = defaultdict(lambda: {"calls": 0, "failed": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "latency_ms": 0})
    model_totals: dict[str, dict] = defaultdict(lambda: {"calls": 0, "failed": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "latency_ms": 0})
    traces: set[str] = set()
    for provider, model, node, status, prompt, completion, latency, trace_id, cached in model_rows:
        role = _role(node)
        bucket = role_totals[role]
        bucket["calls"] += 1
        bucket["failed"] += status != "ok"
        bucket["prompt_tokens"] += prompt or 0
        bucket["completion_tokens"] += completion or 0
        bucket["cached_tokens"] += cached or 0
        bucket["latency_ms"] += latency or 0
        model_bucket = model_totals[f"{provider or PROVIDER_GONE}/{model}"]
        model_bucket["calls"] += 1
        model_bucket["failed"] += status != "ok"
        model_bucket["prompt_tokens"] += prompt or 0
        model_bucket["completion_tokens"] += completion or 0
        model_bucket["cached_tokens"] += cached or 0
        model_bucket["latency_ms"] += latency or 0
        if trace_id:
            traces.add(trace_id)

    run_rows = (await db.execute(select(AgentRun.status, func.count()).group_by(AgentRun.status))).all()
    tool_rows = (await db.execute(select(ToolCall.status, func.count()).group_by(ToolCall.status))).all()
    for bucket in role_totals.values():
        bucket["success_rate"] = round((bucket["calls"] - bucket["failed"]) / bucket["calls"], 3) if bucket["calls"] else 0
        bucket["avg_latency_ms"] = round(bucket["latency_ms"] / bucket["calls"]) if bucket["calls"] else 0
    for model_key, bucket in model_totals.items():
        bucket["success_rate"] = round((bucket["calls"] - bucket["failed"]) / bucket["calls"], 3) if bucket["calls"] else 0
        bucket["avg_latency_ms"] = round(bucket["latency_ms"] / bucket["calls"]) if bucket["calls"] else 0
        bucket["cost"] = estimate_cost(
            model_key.split("/", 1)[1] if "/" in model_key else model_key,
            bucket["prompt_tokens"],
            bucket["completion_tokens"],
            bucket["cached_tokens"],
        ).as_dict()
        bucket["cache_hit_rate"] = (
            round(bucket["cached_tokens"] / bucket["prompt_tokens"], 3) if bucket["prompt_tokens"] else 0
        )
    return {
        "traces": len(traces),
        "model_calls": len(model_rows),
        "roles": dict(role_totals),
        "models": dict(model_totals),
        "runs": {status: count for status, count in run_rows},
        "tools": {status: count for status, count in tool_rows},
    }


# 收尾事件里带着路由记账（见 runtime._finish）。转不转正只看这一段汇总，
# 不看"开起来感觉挺好"。
_FINISH_TYPES = ("task.completed", "task.failed", "task.cancelled")

# 扫最近这么多条收尾事件。不设上限会让这个接口随着历史越来越慢，而评估 beta
# 用的样本根本不需要全表。
_ROUTING_SCAN = 2000


@router.get("/routing")
async def routing_evidence(db: DbSession) -> dict:
    """按需选模型（model_routing）的转正依据。

    ``saved_ratio`` 的分母是"每一步都交给池子里最强模型"的成本当量，所以这个比值
    不依赖任何一家的真实单价：省 3% 还要多一个可能判错的环节，那就不该转正；
    省 40% 且失败率没抬头，才值得开成默认。
    """
    rows = (
        await db.execute(
            select(AgentEvent.task_id, AgentEvent.type, AgentEvent.payload, AgentEvent.created_at)
            .where(AgentEvent.type.in_(_FINISH_TYPES))
            .order_by(AgentEvent.created_at.desc())
            .limit(_ROUTING_SCAN)
        )
    ).all()

    calls = switched = escalations = 0
    cost_units = baseline_units = 0.0
    with_routing = 0
    samples: list[dict] = []
    for task_id, kind, payload, created_at in rows:
        routing = (payload or {}).get("routing") or {}
        if not isinstance(routing, dict) or not routing:
            continue
        with_routing += 1
        calls += int(routing.get("calls") or 0)
        switched += int(routing.get("switched") or 0)
        escalations += int(routing.get("escalations") or 0)
        cost_units += float(routing.get("cost_units") or 0)
        baseline_units += float(routing.get("baseline_units") or 0)
        if len(samples) < 20:
            samples.append(
                {
                    "task_id": task_id,
                    "status": kind.removeprefix("task."),
                    "at": created_at.isoformat() if created_at else "",
                    "calls": int(routing.get("calls") or 0),
                    "switched": int(routing.get("switched") or 0),
                    "saved_ratio": float(routing.get("saved_ratio") or 0),
                }
            )

    saved_ratio = round((baseline_units - cost_units) / baseline_units, 3) if baseline_units else 0.0
    # 样本太少时不给结论：一两个任务的比值波动极大，照着它转正等于拍脑袋。
    verdict = "样本不足" if with_routing < 5 or calls < 20 else ("值得转正" if saved_ratio >= 0.2 else "暂不转正")
    return {
        "scanned": len(rows),
        "sampled_tasks": with_routing,
        "calls": calls,
        "switched": switched,
        "escalations": escalations,
        "cost_units": round(cost_units, 2),
        "baseline_units": round(baseline_units, 2),
        "saved_ratio": saved_ratio,
        "switch_ratio": round(switched / calls, 3) if calls else 0.0,
        "verdict": verdict,
        "recent": samples,
    }

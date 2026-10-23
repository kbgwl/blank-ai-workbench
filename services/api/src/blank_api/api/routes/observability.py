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

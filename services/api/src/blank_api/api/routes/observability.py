from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from blank_api.api.auth import require_token
from blank_api.deps import DbSession
from blank_api.models.model_provider import ModelProvider, ModelUsage
from blank_api.models.run import AgentRun
from blank_api.models.task import ToolCall
from blank_api.services.models.pricing import estimate_cost

router = APIRouter(prefix="/observability", tags=["observability"], dependencies=[Depends(require_token)])


def _role(node: str) -> str:
    return {
        "create_plan": "planner",
        "execute_action": "coder",
        "review_result": "reviewer",
        "summarize_result": "reviewer",
    }.get(node, "tester" if node == "run_validation" else "runtime")


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
            ).join(ModelProvider, ModelProvider.id == ModelUsage.provider_id)
        )
    ).all()
    role_totals: dict[str, dict] = defaultdict(lambda: {"calls": 0, "failed": 0, "prompt_tokens": 0, "completion_tokens": 0, "latency_ms": 0})
    model_totals: dict[str, dict] = defaultdict(lambda: {"calls": 0, "failed": 0, "prompt_tokens": 0, "completion_tokens": 0, "latency_ms": 0})
    traces: set[str] = set()
    for provider, model, node, status, prompt, completion, latency, trace_id in model_rows:
        role = _role(node)
        bucket = role_totals[role]
        bucket["calls"] += 1
        bucket["failed"] += status != "ok"
        bucket["prompt_tokens"] += prompt or 0
        bucket["completion_tokens"] += completion or 0
        bucket["latency_ms"] += latency or 0
        model_bucket = model_totals[f"{provider}/{model}"]
        model_bucket["calls"] += 1
        model_bucket["failed"] += status != "ok"
        model_bucket["prompt_tokens"] += prompt or 0
        model_bucket["completion_tokens"] += completion or 0
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
        ).as_dict()
    return {
        "traces": len(traces),
        "model_calls": len(model_rows),
        "roles": dict(role_totals),
        "models": dict(model_totals),
        "runs": {status: count for status, count in run_rows},
        "tools": {status: count for status, count in tool_rows},
    }

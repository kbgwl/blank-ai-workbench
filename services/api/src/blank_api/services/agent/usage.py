"""一笔模型调用变成一条账，只在这个地方发生。

对标 Codex rollout 里的 `token_usage_record`：一条记录同时写下三个口径
（单次 / 本回合 / 整个任务）。之所以要有这个模块，是因为落账点原来只有一处
（`AgentSession`），而子代理走的是另一条循环——结果子代理真花了钱，
任务级的求和里却没有它。主代理与子代理共用这一个函数，两条链路就不可能再各记各的。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select

from blank_api.models.model_provider import ModelUsage
from blank_api.services.models.base import Usage


def accumulate_usage(usage: Usage) -> dict:
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "cached_tokens": usage.cached_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "total_tokens": usage.prompt_tokens + usage.completion_tokens,
        "calls": 1,
    }


def merge_usage(base: dict, delta: dict) -> dict:
    merged = dict(base or {})
    merged["prompt_tokens"] = int(merged.get("prompt_tokens") or 0) + int(delta.get("prompt_tokens") or 0)
    merged["completion_tokens"] = int(merged.get("completion_tokens") or 0) + int(
        delta.get("completion_tokens") or 0
    )
    for key in ("cached_tokens", "cache_write_tokens", "reasoning_tokens"):
        merged[key] = int(merged.get(key) or 0) + int(delta.get(key) or 0)
    merged["total_tokens"] = merged["prompt_tokens"] + merged["completion_tokens"]
    merged["calls"] = int(merged.get("calls") or 0) + int(delta.get("calls") or 0)
    for key in ("provider", "model"):
        # 只在真的带值时落下这个键：三级口径里 turn 是"本回合合计"，跨模型时
        # 写一个 None 占位比不写更容易被误读成"这回合用的模型是空"。
        if delta.get(key) is not None:
            merged.setdefault(key, delta[key])
    return merged


async def task_usage_totals(session_factory, task_id: str, fallback: dict) -> dict:
    """任务级累计：按 task_id 现查一次聚合。

    三级里只有"单次"属于这一次调用，回合级在内存里滚，任务级必须跨会话合起来算
    （子代理、摘要、审查员都往同一张表写，同一个 task_id）。读出时求和推出来的数
    只能证明"曾经有这些行"，事件里同时带三个口径才是一条可核对的事实。
    """
    if not task_id or session_factory is None:
        return dict(fallback)

    async with session_factory() as session:
        totals = (
            await session.execute(
                select(
                    func.coalesce(func.sum(ModelUsage.prompt_tokens), 0),
                    func.coalesce(func.sum(ModelUsage.completion_tokens), 0),
                    func.coalesce(func.sum(ModelUsage.cached_tokens), 0),
                    func.coalesce(func.sum(ModelUsage.cache_write_tokens), 0),
                    func.coalesce(func.sum(ModelUsage.reasoning_tokens), 0),
                    func.coalesce(func.sum(ModelUsage.latency_ms), 0),
                    func.count(ModelUsage.id),
                ).where(ModelUsage.task_id == task_id)
            )
        ).one()
    prompt, completion, cached, cache_write, reasoning, latency, calls = (int(value or 0) for value in totals)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": reasoning,
        "total_tokens": prompt + completion,
        "latency_ms": latency,
        "calls": calls,
    }


async def account_call(
    *,
    session_factory,
    recorder,
    task_id: str,
    trace_id: str,
    provider_id: str,
    provider_name: str,
    node: str,
    role: str,
    depth: int,
    agent_path: str,
    model: str,
    provider: str,
    usage: Usage,
    latency_ms: int,
    status: str,
    response_id: str = "",
    turn: dict,
    role_provider_ids: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
    run_id: str = "",
    origin_run_id: str = "",
    parent_thread_id: str = "",
    root_thread_id: str = "",
    parallel_group: str = "",
) -> dict:
    """落一行用量 + 发一条三级事件，返回新的回合累计。

    落库与记账是同一个动作的两个视角：行进表、事件同时写出单次/本回合/整个任务
    三个口径。失败的调用同样要进来（`status="error"`、usage 全零）——一次请求根本没
    花 token，只有这条记录说得出「发生过、失败了」。

    `run_id` / `parent_thread_id` / `root_thread_id` 是这一笔的**回合归属**（对标 Codex
    的 `token_usage_record.turn_id` 与 `turn_attribution.root_turn_id/parent_turn_id`）：
    "这一轮花了多少"要按 run 筛，"这条分支是谁引起的"要能一跳回到父线程与根线程。
    这三个值**由调用方给出，这里不推导也不兜正**——主代理的根就是它自己，
    子代理的根是从父会话一路传下来的那个；在这里现取等于把归属写成本次的复制品。

    `origin_run_id` 是**发起轮**：引起这一整棵任务树的那一轮。它和 `run_id` 只在跨任务的
    链上分家——一轮群聊里每个发言成员各有自己的 task 与 run，按 run 筛只能一人一行地看，
    "这几个人合起来花了多少"就得按发起轮筛。值和上面三个一样由调用方给出，这里不推导。

    `session_factory` 为空是评测 harness 的形状：没有库可写，但三级事件照发，
    任务级退回本回合——离线跑图核对的是事件序列，不是表。
    """
    row_provider_id = (role_provider_ids or {}).get(role) or provider_id
    if session_factory is not None:
        async with session_factory() as session:
            session.add(
                ModelUsage(
                    id=str(uuid.uuid4()),
                    provider_id=row_provider_id,
                    task_id=task_id,
                    trace_id=trace_id,
                    node=node,
                    model=model or provider_name,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    cached_tokens=usage.cached_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    reasoning_tokens=usage.reasoning_tokens,
                    latency_ms=latency_ms,
                    status=status,
                    response_id=response_id or "",
                    run_id=run_id or "",
                    origin_run_id=origin_run_id or "",
                    root_trace_id=root_thread_id or "",
                    parent_trace_id=parent_thread_id or "",
                    agent_path=agent_path or "",
                    parallel_group=parallel_group or "",
                )
            )
            await session.commit()

    call = accumulate_usage(usage)
    call.update(
        {
            "model": model or provider_name,
            "provider": provider or provider_name,
            "latency_ms": latency_ms,
            "status": status,
            # 供应商那一笔请求的 id：三级口径说的是"花了多少"，这一条说的是"去哪家查这一笔"。
            "response_id": response_id or "",
        }
    )
    merged = merge_usage(turn, accumulate_usage(usage))
    payload: dict[str, Any] = {
        "role": role,
        "trace_id": trace_id,
        "depth": depth,
        "agent_path": agent_path,
        # 回合归属与上面三个"花了多少"是两件事：三级口径说的是量，这一块说的是这笔钱算在谁头上。
        "attribution": {
            "run_id": run_id or "",
            # 发起轮：整棵任务树共用一个值（顶层运行里它等于 run_id，团队轮里它等于那一轮，
            # 放行续跑的那次也算同一轮）。空串同样是"当时没记"。
            "origin_run_id": origin_run_id or "",
            "thread_id": trace_id,
            "parent_thread_id": parent_thread_id or "",
            "root_thread_id": root_thread_id or "",
            "agent_path": agent_path or "",
            "depth": depth,
            # 哪一次并行编排引起的。空串=不属于任何一批（单发子代理、主线程、摘要），
            # 与"第 0 批"是两回事；老行同样是空串，读出来必须是 null。
            "parallel_group": parallel_group or "",
        },
        "call": call,
        "turn": dict(merged),
        "task": await task_usage_totals(session_factory, task_id, merged),
    }
    if extra:
        payload.update(extra)
    await recorder.add("model.usage", node, payload)
    return merged

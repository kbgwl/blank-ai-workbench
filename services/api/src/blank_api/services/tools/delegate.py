"""spawn_agent / spawn_agents: hand narrow goals to child agents.

``spawn_agent`` is one child, read-only by default; with ``isolate`` it works in a
throwaway copy of the workspace and reports back a diff, so trying something risky
never touches the real files.

``spawn_agents`` fans several children out **at the same time**, each in its own
copy, and aggregates their conclusions. This is the shape both Codex and Claude
Code converged on for "go read these five subsystems in parallel": the parent
keeps one context window, the children spend their own.

Merging is opt-in and deliberately conservative (see ``worktree.apply_changes``):
two children that touched the same file produce a conflict, and a file with
uncommitted changes in the real workspace is never overwritten. Exploration is
the point; the parent decides what lands.
"""

from __future__ import annotations

from blank_api.services.subagent import (
    DEFAULT_CONCURRENCY,
    MAX_PARALLEL_AGENTS,
    SubAgentRequest,
    render_merge,
)
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok

MAX_GOAL_CHARS = 2_000


def _spawn_agents_risk(arguments: dict) -> str:
    """只探索不定稿：写回主工作区是要人点头的那一步。"""
    return "high" if bool(arguments.get("merge")) else "medium"


def _parse_tasks(raw) -> tuple[list[SubAgentRequest], str]:
    if not isinstance(raw, list) or not raw:
        return [], "spawn_agents 需要至少一个子任务"
    requests: list[SubAgentRequest] = []
    for item in raw[:MAX_PARALLEL_AGENTS]:
        if isinstance(item, str):
            goal = item.strip()
            isolate, steps, label = True, 4, ""
        elif isinstance(item, dict):
            goal = str(item.get("goal") or "").strip()
            isolate = bool(item.get("isolate", True))
            try:
                steps = int(item.get("max_steps") or 4)
            except (TypeError, ValueError):
                steps = 4
            label = str(item.get("label") or "")
        else:
            return [], "子任务必须是字符串或 {goal, isolate, max_steps} 对象"
        if not goal:
            return [], "每个子任务都需要具体的 goal"
        requests.append(
            SubAgentRequest(
                goal=goal[:MAX_GOAL_CHARS],
                isolate=isolate,
                max_steps=max(1, min(8, steps)),
                label=label or "par",
            )
        )
    return requests, ""


async def _spawn_agent(context: ToolContext, arguments: dict) -> ToolOutcome:
    runner = getattr(context, "subagent_runner", None)
    if runner is None:
        return fail("error", "当前运行时没有启用子代理")
    try:
        request = SubAgentRequest(
            goal=str(arguments.get("goal") or ""),
            isolate=bool(arguments.get("isolate")),
            max_steps=int(arguments.get("max_steps") or 4),
            label=str(arguments.get("label") or ""),
        )
    except (TypeError, ValueError):
        return fail("error", "子代理参数无效")
    if not request.goal.strip():
        return fail("error", "spawn_agent 需要具体的子任务目标")

    result = await runner(request)
    if not result.ok and not result.summary:
        return fail("error", result.error or "子代理执行失败")
    return ok(
        result.render()[:8_000],
        ok=result.ok,
        summary=result.summary,
        steps=result.steps,
        depth=result.depth,
        agent_path=result.agent_path,
        worktree=result.worktree,
        diff=result.diff[:20_000],
        error=result.error,
        usage=result.usage,
    )


async def _spawn_agents(context: ToolContext, arguments: dict) -> ToolOutcome:
    runner = getattr(context, "subagent_runner", None)
    if runner is None:
        return fail("error", "当前运行时没有启用子代理")
    if not hasattr(runner, "fan_out"):
        return fail("error", "当前运行时没有启用并行子代理")
    requests, error = _parse_tasks(arguments.get("tasks"))
    if error:
        return fail("error", error)
    try:
        concurrency = int(arguments.get("concurrency") or DEFAULT_CONCURRENCY)
    except (TypeError, ValueError):
        concurrency = DEFAULT_CONCURRENCY
    merge = bool(arguments.get("merge"))

    results = await runner.fan_out(requests, concurrency=concurrency, merge=merge)
    if not results:
        return fail("error", "并行子代理没有返回结果")

    lines: list[str] = [f"并行执行了 {len(results)} 个子代理："]
    payload: list[dict] = []
    for index, result in enumerate(results, start=1):
        status = "完成" if result.ok else "失败"
        lines.append(f"\n[{index}] {status}（{result.steps} 步，{result.agent_path}）")
        lines.append(result.summary.strip() or "（未给出结论）")
        if result.error:
            lines.append(f"异常：{result.error}")
        if result.merge:
            lines.append("合并：" + render_merge(result.merge))
        if result.diff:
            # 每个 diff 单独截断，避免一个巨大的 diff 把其余子代理的结论挤掉
            lines.append("改动摘要：\n" + result.diff[:4_000])
        payload.append(
            {
                "agent_path": result.agent_path,
                "ok": result.ok,
                "steps": result.steps,
                "summary": result.summary[:2_000],
                "error": result.error,
                "merge": result.merge,
                "diff": result.diff[:20_000],
            }
        )

    succeeded = sum(1 for item in results if item.ok)
    return ok(
        "\n".join(lines)[:16_000],
        count=len(results),
        succeeded=succeeded,
        merge=merge,
        agents=payload,
    )


def delegate_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="spawn_agent",
            description=(
                "把一个可以独立完成、边界清楚的子任务交给子代理，它会返回结论（可带隔离副本中的 diff）。"
                "适合大范围侦察、反复试错、需要独立上下文的工作；简单一步就能做完的事不要用它。"
                "isolate=true 时子代理在临时副本里工作，改动不会影响真实工作区。"
                "需要同时分头做几件事时改用 spawn_agents。"
            ),
            input_schema={
                "type": "object",
                "required": ["goal"],
                "properties": {
                    "goal": {"type": "string", "description": "子任务的完整描述，子代理看不到当前对话"},
                    "isolate": {
                        "type": "boolean",
                        "description": "是否在临时隔离副本中执行（允许写文件，但只影响副本）",
                    },
                    "max_steps": {"type": "integer", "description": "子代理最多几步，默认 4，上限 8"},
                    "label": {"type": "string", "description": "隔离副本的名字前缀，便于辨认"},
                },
            },
            risk_level="medium",
            handler=_spawn_agent,
            timeout_seconds=330.0,
        ),
        ToolDefinition(
            name="spawn_agents",
            description=(
                "并行分派多个子代理：每个子任务拿到自己的隔离副本与独立上下文，同时开工，"
                "最后把各自的结论和 diff 汇总回来。适合「同时摸清几个模块 / 各写一份方案再对比」这类工作。"
                "merge=true 时会把无冲突、且主工作区对应文件没有未提交改动的改动写回；"
                "同一文件被多个子代理改过算冲突，不写入，只交回 diff。"
            ),
            input_schema={
                "type": "object",
                "required": ["tasks"],
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": f"子任务列表，最多 {MAX_PARALLEL_AGENTS} 个；元素可以是字符串或 {{goal, isolate, max_steps}}",
                        "items": {"type": "string"},
                    },
                    "concurrency": {
                        "type": "integer",
                        "description": f"同时最多跑几个，默认 {DEFAULT_CONCURRENCY}",
                    },
                    "merge": {
                        "type": "boolean",
                        "description": "是否把无冲突改动写回主工作区，默认 false（只交回 diff）",
                    },
                },
            },
            risk_level="medium",
            risk_for=_spawn_agents_risk,
            handler=_spawn_agents,
            timeout_seconds=600.0,
        ),
    ]

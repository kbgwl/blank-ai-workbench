"""spawn_agent: hand a narrow goal to a child agent.

Read-only by default. With ``isolate`` the child works in a throwaway copy of the
workspace and reports back a diff, so trying something risky never touches the
real files.
"""

from __future__ import annotations

from blank_api.services.subagent import SubAgentRequest
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok


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


def delegate_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="spawn_agent",
            description=(
                "把一个可以独立完成、边界清楚的子任务交给子代理，它会返回结论（可带隔离副本中的 diff）。"
                "适合大范围侦察、反复试错、需要独立上下文的工作；简单一步就能做完的事不要用它。"
                "isolate=true 时子代理在临时副本里工作，改动不会影响真实工作区。"
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
        )
    ]

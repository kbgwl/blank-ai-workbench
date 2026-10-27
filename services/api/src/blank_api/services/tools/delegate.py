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
    batch_usage,
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

    if bool(arguments.get("background")):
        if not hasattr(runner, "spawn_background"):
            return fail("error", "当前运行时没有启用后台子代理")
        agent_id, error_result = await runner.spawn_background(request)
        if error_result is not None:
            return fail("error", error_result.error or "后台子代理启动失败")
        # 只给句柄，不给结论：结论要等 task_output。返回里写清"去哪儿取"，
        # 否则模型会把"已派出"读成"已完成"。
        return ok(
            f"已在后台派出子代理 {agent_id}（{request.goal[:120]}）。"
            f"它还在运行；用 task_output(agent_id=\"{agent_id}\") 取结论，"
            f"不需要了用 task_stop(agent_id=\"{agent_id}\") 取消。",
            agent_id=agent_id,
            status="running",
            goal=request.goal[:200],
        )

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
        agent_id=result.agent_id,
        worktree=result.worktree,
        diff=result.diff[:20_000],
        error=result.error,
        usage=result.usage,
    )


async def _task_output(context: ToolContext, arguments: dict) -> ToolOutcome:
    runner = getattr(context, "subagent_runner", None)
    if runner is None or not hasattr(runner, "output"):
        return fail("error", "当前运行时没有启用后台子代理")
    agent_id = str(arguments.get("agent_id") or "").strip()
    if not agent_id:
        return fail("error", "task_output 需要 agent_id")
    try:
        timeout = float(arguments.get("timeout") or 60)
    except (TypeError, ValueError):
        timeout = 60.0
    snapshot = await runner.output(agent_id, block=bool(arguments.get("block", True)), timeout=timeout)
    status = str(snapshot.get("status") or "")
    if status == "not_found":
        return fail("error", str(snapshot.get("message") or "没有找到该子代理"))
    data = {
        key: snapshot[key]
        for key in (
            "agent_id",
            "agent_path",
            "goal",
            "status",
            "summary",
            "steps",
            "error",
            "worktree",
            "diff",
            "usage",
            "thread_id",
            "elapsed_seconds",
        )
        if key in snapshot
    }
    if status == "running":
        return ToolOutcome(ok=True, status="ok", output=str(snapshot.get("message") or ""), data=data)
    output = str(snapshot.get("text") or snapshot.get("message") or "")
    if status == "cancelled":
        return ToolOutcome(
            ok=False, status="error", output=output, data=data, message="子代理已取消"
        )
    if not snapshot.get("ok"):
        return ToolOutcome(
            ok=False,
            status="error",
            output=output,
            data=data,
            message=str(snapshot.get("error") or "子代理未能完成"),
        )
    return ToolOutcome(ok=True, status="ok", output=output, data=data)


async def _task_stop(context: ToolContext, arguments: dict) -> ToolOutcome:
    runner = getattr(context, "subagent_runner", None)
    if runner is None or not hasattr(runner, "stop"):
        return fail("error", "当前运行时没有启用后台子代理")
    agent_id = str(arguments.get("agent_id") or "").strip()
    if not agent_id:
        return fail("error", "task_stop 需要 agent_id")
    snapshot = await runner.stop(agent_id)
    status = str(snapshot.get("status") or "")
    if status == "not_found":
        return fail("error", str(snapshot.get("message") or "没有找到该子代理"))
    data = {
        key: snapshot[key]
        for key in ("agent_id", "agent_path", "goal", "status", "elapsed_seconds")
        if key in snapshot
    }
    if status == "cancelled":
        return ok(f"已取消子代理 {agent_id}，隔离副本已清理。", **data)
    # 已经结束的句柄：停止是空操作，但要把"它其实已经跑完了"说清楚，
    # 否则模型会以为取消成功、结论被丢弃了。
    return ok(
        f"子代理 {agent_id} 已经结束（{status}），没有需要取消的运行。",
        **data,
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
                "thread_id": result.thread_id,
                "ok": result.ok,
                "steps": result.steps,
                "summary": result.summary[:2_000],
                "error": result.error,
                "merge": result.merge,
                "diff": result.diff[:20_000],
                "usage": dict(result.usage or {}),
            }
        )

    succeeded = sum(1 for item in results if item.ok)
    return ok(
        "\n".join(lines)[:16_000],
        count=len(results),
        succeeded=succeeded,
        merge=merge,
        agents=payload,
        # 这一批的合计与批次号：主代理的预算要把派出去的那部分算在自己这一轮头上，
        # 「证明」面板要能指着一次 fan_out 说合计是多少。没有它们，这笔钱在工具结果里就消失了。
        parallel_group=results[0].parallel_group,
        usage=batch_usage(results),
    )


def delegate_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="spawn_agent",
            description=(
                "把一个可以独立完成、边界清楚的子任务交给子代理，它会返回结论（可带隔离副本中的 diff）。"
                "适合大范围侦察、反复试错、需要独立上下文的工作；简单一步就能做完的事不要用它。"
                "isolate=true 时子代理在临时副本里工作，改动不会影响真实工作区。"
                "background=true 时立即返回 agent_id，之后用 task_output 取结论、task_stop 取消——"
                "需要同时推进别的事时用它；需要立刻拿到结论时不要用。"
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
                    "background": {
                        "type": "boolean",
                        "description": "true=立即返回 agent_id 在后台跑，之后用 task_output/task_stop；默认 false（等到结束）",
                    },
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
        ToolDefinition(
            name="task_output",
            description=(
                "取后台子代理（spawn_agent(background=true)）的结论。"
                "block=true 时最多等 timeout 秒；等不到只表示它还在跑，不会因此取消它。"
                "返回状态 completed / failed / cancelled / running，未知 agent_id 会如实报找不到。"
            ),
            input_schema={
                "type": "object",
                "required": ["agent_id"],
                "properties": {
                    "agent_id": {"type": "string", "description": "spawn_agent(background=true) 返回的句柄"},
                    "block": {"type": "boolean", "description": "是否等待结束，默认 true"},
                    "timeout": {"type": "number", "description": "最多等几秒，默认 60，上限 300"},
                },
            },
            risk_level="low",
            handler=_task_output,
            timeout_seconds=330.0,
        ),
        ToolDefinition(
            name="task_stop",
            description=(
                "取消一个仍在运行的后台子代理；它的隔离副本会一并删除，并留下 subagent.cancelled 记录。"
                "已经结束的句柄照实返回它当初的状态，不会谎报取消。"
            ),
            input_schema={
                "type": "object",
                "required": ["agent_id"],
                "properties": {
                    "agent_id": {"type": "string", "description": "要停止的后台子代理句柄"},
                },
            },
            risk_level="low",
            handler=_task_stop,
        ),
    ]

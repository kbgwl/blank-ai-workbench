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

import os

from blank_api.services.agents_defs import (
    BUILTIN_AGENT,
    BUILTIN_FORK,
    BUILTIN_FORK_AGENT,
    BUILTIN_GENERAL_PURPOSE,
)
from blank_api.services.subagent import (
    DEFAULT_CONCURRENCY,
    DEFAULT_STEPS,
    MAX_PARALLEL_AGENTS,
    MAX_STEPS,
    SubAgentRequest,
    batch_usage,
    render_merge,
)
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok

MAX_GOAL_CHARS = 2_000

MODEL_FORCE_ENV = "CLAUDE_CODE_SUBAGENT_MODEL_FORCE"


def _model_forced() -> bool:
    """上游按字符串真值判定：``a.CLAUDE_CODE_SUBAGENT_MODEL_FORCE`` 非空即开。"""
    return bool(os.environ.get(MODEL_FORCE_ENV))


def _ignored_model(arguments: dict) -> str:
    """置位时调用方仍塞进来的 ``model``：不生效，但要留痕，别让模型反复重试。"""
    if not _model_forced():
        return ""
    raw = arguments.get("model", _MODEL_MISSING)
    if raw is _MODEL_MISSING or raw is None:
        return ""
    return str(raw)


def _ignored_note(model: str) -> str:
    return (
        f"{MODEL_FORCE_ENV} 已设置：忽略传入的 model {model!r}，"
        "子代理沿用父会话模型。"
    )


def _resolve_agent(context: ToolContext, raw) -> tuple:
    """把 subagent_type 解析成定义；未知类型 fail-closed 并列出可用名字。

    没有运行时会话时（单测 / 非 runtime 入口）只认内置 general-purpose 与 fork：
    宁可明确说"没有这个类型"，也不要凭一个不存在的目录去猜。
    """
    name = str(raw or "").strip() or BUILTIN_AGENT
    catalog = getattr(context, "agents", None)
    if catalog is not None:
        definition = catalog.get(name)
        if definition is not None:
            return definition, ""
        return None, f"未知子代理类型 '{name}'。可用类型：{', '.join(catalog.names())}"
    if name == BUILTIN_AGENT:
        return BUILTIN_GENERAL_PURPOSE, ""
    if name == BUILTIN_FORK:
        return BUILTIN_FORK_AGENT, ""
    return None, f"未知子代理类型 '{name}'。可用类型：{BUILTIN_AGENT}, {BUILTIN_FORK}"


def _apply_definition(request: SubAgentRequest, definition) -> SubAgentRequest:
    """把定义挂到请求上；max_steps 由调用方决定是否让 maxTurns 参与。"""
    request.agent_name = definition.name
    request.instructions = definition.instructions
    request.allowed_tools = definition.tools
    request.disallowed_tools = definition.disallowed_tools
    request.skills = definition.skills
    request.agent_ignored = definition.ignored
    # permissionMode 只是"声明"，真正生效与否在 run_subagent 里按基线收紧判定。
    request.permission_mode = definition.permission_mode
    # 思考强度：定义里声明的档位一路带到子代理，由 run_subagent 按模型封顶裁决。
    # 空串表示定义没声明，子代理按兜底档跑。
    request.effort = definition.effort
    # 模型覆盖：``inherit``/空串由 run_subagent 回落到父会话模型；fork 恒继承。
    # FORCE 置位时定义里的覆盖同样失效（上游在解析入口把 n 置空，36500302）。
    request.model = "" if _model_forced() else definition.model
    # 定义钩子跟着请求走，由 run_subagent 注册进子代理自己的作用域。
    request.agent_hooks = dict(getattr(definition, "hooks", None) or {})
    # 记忆作用域：派发时解析目录、注入记忆段并挂上作用域内的写入工具。
    request.memory_scope = str(getattr(definition, "memory", "") or "")
    # omitClaudeMd：定义写 true 时子代理不继承工作区指令文件（AGENTS.md 那套）。
    request.omit_claude_md = bool(getattr(definition, "omit_claude_md", False))
    # 上游 Szn：isolation 的生效值 = 派发参数 ?? 定义值；定义写 worktree 时
    # 调用方显式传 isolate:false 也压不住它——用户要的是"每次都在副本里跑"。
    if definition.isolation == "worktree":
        request.isolate = True
    # 上游 Pts：定义里的 background:true 会压过调用方的 run_in_background=false。
    request.definition_background = definition.background
    request.fork = definition.name == BUILTIN_FORK
    return request


_MODEL_MISSING = object()


def _tool_model(arguments: dict) -> tuple:
    """工具入参的 ``model``：显式给出时压过定义里的 ``model``。

    空串与 ``inherit`` 是"回落父会话"的语义值，会压掉定义值；``None`` 等同没传。
    类型不对就 fail-closed——把 ``123`` 当模型名发给供应商只会换来一个 400。
    """
    raw = arguments.get("model", _MODEL_MISSING)
    if _model_forced():
        # 上游先 ``n=void 0`` 再解析：值不会被使用，类型也就不该再把派发拒掉。
        return _MODEL_MISSING, ""
    if raw is _MODEL_MISSING or raw is None:
        return _MODEL_MISSING, ""
    if not isinstance(raw, str):
        return _MODEL_MISSING, "model 必须是字符串（不传则用定义或父会话模型）"
    return raw.strip(), ""


def _request_steps(raw, definition) -> int:
    """没显式传步数时用定义的 maxTurns；两者都受子代理全局上限约束。"""
    if raw is None or raw == "":
        return max(1, min(MAX_STEPS, definition.max_turns or DEFAULT_STEPS))
    return max(1, min(MAX_STEPS, int(raw)))


def _spawn_agents_risk(arguments: dict) -> str:
    """只探索不定稿：写回主工作区是要人点头的那一步。"""
    return "high" if bool(arguments.get("merge")) else "medium"


def _parse_tasks(raw, context: ToolContext) -> tuple[list[SubAgentRequest], str]:
    if not isinstance(raw, list) or not raw:
        return [], "spawn_agents 需要至少一个子任务"
    requests: list[SubAgentRequest] = []
    for item in raw[:MAX_PARALLEL_AGENTS]:
        if isinstance(item, str):
            goal = item.strip()
            isolate, steps, label, agent_type = True, 4, "", BUILTIN_AGENT
            model = _MODEL_MISSING
        elif isinstance(item, dict):
            goal = str(item.get("goal") or "").strip()
            isolate = bool(item.get("isolate", True))
            steps = item.get("max_steps")
            label = str(item.get("label") or "")
            agent_type = item.get("subagent_type")
            model, model_error = _tool_model(item)
            if model_error:
                return [], f"子任务的 {model_error}"
        else:
            return [], (
                "子任务必须是字符串或 "
                "{goal, isolate, max_steps, subagent_type, model} 对象"
            )
        if not goal:
            return [], "每个子任务都需要具体的 goal"
        definition, error = _resolve_agent(context, agent_type)
        if error:
            return [], error
        if definition.name == BUILTIN_FORK:
            # fork 继承的是父会话到此刻为止的对话；并行批里的分身各自拿到同一份
            # 快照却分头改动，回来时的结论没法拼成一个"我"。Claude 也只按单个
            # Agent 调用来 fork。
            return [], "fork 子代理只能通过 spawn_agent 前台派发，不能放进 spawn_agents 并行批次"
        try:
            resolved_steps = _request_steps(steps, definition)
        except (TypeError, ValueError):
            resolved_steps = DEFAULT_STEPS
        request = SubAgentRequest(
            goal=goal[:MAX_GOAL_CHARS],
            isolate=isolate,
            max_steps=resolved_steps,
            label=label or "par",
        )
        _apply_definition(request, definition)
        if model is not _MODEL_MISSING:
            request.model = model
        requests.append(request)
    return requests, ""


async def _spawn_agent(context: ToolContext, arguments: dict) -> ToolOutcome:
    runner = getattr(context, "subagent_runner", None)
    if runner is None:
        return fail("error", "当前运行时没有启用子代理")
    definition, error = _resolve_agent(context, arguments.get("subagent_type"))
    if error:
        return fail("error", error)
    model, model_error = _tool_model(arguments)
    if model_error:
        return fail("error", model_error)
    ignored_model = _ignored_model(arguments)
    try:
        request = SubAgentRequest(
            goal=str(arguments.get("goal") or ""),
            isolate=bool(arguments.get("isolate")),
            max_steps=_request_steps(arguments.get("max_steps"), definition),
            label=str(arguments.get("label") or ""),
        )
    except (TypeError, ValueError):
        return fail("error", "子代理参数无效")
    if not request.goal.strip():
        return fail("error", "spawn_agent 需要具体的子任务目标")
    _apply_definition(request, definition)
    # 上游 ``model:Jt?void 0:we``：工具入参压过定义，fork 形态直接丢掉它。
    if model is not _MODEL_MISSING and not request.fork:
        request.model = model

    # Claude Code 的 Agent 工具默认后台跑：不传就是后台，只有「下一步马上就要结论、
    # 期间没有别的事可做」时才显式传 false。旧参数名 background 继续接受。
    # fork 是唯一例外：Claude 的 fork 形态没有 run_in_background，直接传 true 是
    # 参数错，不是"换个方式跑"——它继承的是派发这一刻的对话快照。
    raw_background = arguments.get("run_in_background")
    if raw_background is None:
        raw_background = arguments.get("background")
    if request.fork:
        if raw_background is not None and bool(raw_background):
            return fail(
                "error",
                "fork 子代理只能前台运行：它继承的是派发这一刻的父会话对话，"
                "后台跑会让结论落在父会话已经走远之后。去掉 run_in_background 或传 false。",
            )
        background = False
    else:
        background = True if raw_background is None else bool(raw_background)
        # 上游 Pts：定义里写了 background:true 就按后台派出，即使调用方传了 false。
        if request.definition_background:
            background = True
    if background:
        if not hasattr(runner, "spawn_background"):
            return fail("error", "当前运行时没有启用后台子代理")
        agent_id, error_result = await runner.spawn_background(request)
        if error_result is not None:
            return fail("error", error_result.error or "后台子代理启动失败")
        # 只给句柄，不给结论：结论要等 task_output。返回里写清"去哪儿取"，
        # 否则模型会把"已派出"读成"已完成"。
        body = (
            f"已在后台派出子代理 {agent_id}（{request.goal[:120]}）。"
            f"它还在运行；用 task_output(agent_id=\"{agent_id}\") 取结论，"
            f"不需要了用 task_stop(agent_id=\"{agent_id}\") 取消。"
        )
        if ignored_model:
            body = f"{body}\n{_ignored_note(ignored_model)}"
        return ok(
            body,
            agent_id=agent_id,
            status="running",
            goal=request.goal[:200],
            agent_type=definition.name,
            **({"model_ignored": ignored_model} if ignored_model else {}),
        )

    result = await runner(request)
    if not result.ok and not result.summary:
        return fail("error", result.error or "子代理执行失败")
    body = result.render()[:8_000]
    if ignored_model:
        body = f"{body}\n\n{_ignored_note(ignored_model)}"
    return ok(
        body,
        ok=result.ok,
        summary=result.summary,
        steps=result.steps,
        depth=result.depth,
        agent_path=result.agent_path,
        agent_id=result.agent_id,
        agent_type=definition.name,
        worktree=result.worktree,
        diff=result.diff[:20_000],
        error=result.error,
        usage=result.usage,
        **({"model_ignored": ignored_model} if ignored_model else {}),
    )


def _handle_id(arguments: dict) -> str:
    """task_output / task_stop 的句柄：task_id 是通用名，旧名 agent_id / shell_id 继续接受。"""
    for key in ("task_id", "shell_id", "agent_id", "monitor_id"):
        value = str(arguments.get(key) or "").strip()
        if value:
            return value
    return ""


def _is_shell_handle(arguments: dict, handle_id: str) -> bool:
    # 后台命令的句柄带固定前缀；显式传 shell_id 也按命令处理，不靠前缀猜。
    if handle_id.startswith("shell-"):
        return True
    return str(arguments.get("shell_id") or "").strip() == handle_id


def _is_monitor_handle(arguments: dict, handle_id: str) -> bool:
    # monitor 的句柄固定 mon- 开头；显式传 monitor_id 也按监控处理。
    if handle_id.startswith("mon-"):
        return True
    return str(arguments.get("monitor_id") or "").strip() == handle_id


def _is_workflow_handle(handle_id: str) -> bool:
    # 工作流句柄固定 wf_ 开头（与 Claude 的 resumeFromRunId 同一形态）。
    return handle_id.startswith("wf_")


async def _workflow_output(context: ToolContext, workflow_id: str, arguments: dict) -> ToolOutcome:
    from blank_api.services.agent import workflow as workflow_module

    try:
        timeout = float(arguments.get("timeout") or 60)
    except (TypeError, ValueError):
        timeout = 60.0
    snapshot = await workflow_module.output(
        str(getattr(context, "task_id", "") or ""),
        workflow_id,
        block=bool(arguments.get("block", True)),
        timeout=timeout,
    )
    status = str(snapshot.get("status") or "")
    if status == "not_found":
        return fail("error", str(snapshot.get("message") or "没有找到该工作流"))
    data = {
        key: snapshot[key]
        for key in (
            "workflow_id",
            "name",
            "description",
            "status",
            "agents",
            "cached",
            "elapsed_seconds",
            "phases",
            "script_path",
        )
        if key in snapshot
    }
    logs = [str(item) for item in snapshot.get("logs") or []]
    log_tail = ("\n日志末尾：\n" + "\n".join(logs[-5:])) if logs else ""
    if status == "running":
        return ToolOutcome(
            ok=True,
            status="ok",
            output=(
                f"工作流 {workflow_id}（{snapshot.get('name') or ''}）还在运行："
                f"已派 {snapshot.get('agents') or 0} 个子代理，已完成阶段 "
                f"{'、'.join(snapshot.get('phases') or []) or '无'}。"
                f"结束时你会收到系统通知。{log_tail}"
            ).strip(),
            data=data,
        )
    body = str(snapshot.get("result") or "") or log_tail
    if status == "cancelled":
        return ToolOutcome(ok=False, status="error", output=body, data=data, message="工作流已取消")
    if not snapshot.get("ok"):
        return ToolOutcome(
            ok=False,
            status="error",
            output=body,
            data=data,
            message=str(snapshot.get("error") or "工作流未能完成"),
        )
    return ToolOutcome(ok=True, status="ok", output=body or "工作流完成，没有返回值", data=data)


async def _workflow_stop(context: ToolContext, workflow_id: str) -> ToolOutcome:
    from blank_api.services.agent import workflow as workflow_module

    snapshot = await workflow_module.stop(
        str(getattr(context, "task_id", "") or ""), workflow_id
    )
    status = str(snapshot.get("status") or "")
    if status == "not_found":
        return fail("error", str(snapshot.get("message") or "没有找到该工作流"))
    data = {
        key: snapshot[key]
        for key in ("workflow_id", "name", "status", "agents", "elapsed_seconds")
        if key in snapshot
    }
    if status == "cancelled":
        return ok(f"已取消工作流 {workflow_id}，仍在跑的子代理一并收走。", **data)
    return ok(
        f"工作流 {workflow_id} 已经结束（{status}），没有需要取消的运行。"
        f"结论用 task_output(task_id=\"{workflow_id}\") 取。",
        **data,
    )


async def _shell_output(context: ToolContext, shell_id: str, arguments: dict) -> ToolOutcome:
    from blank_api.services import shell_tasks
    from blank_api.services.tools.shell import MAX_OUTPUT_CHARS, mask_output

    try:
        timeout = float(arguments.get("timeout") or 60)
    except (TypeError, ValueError):
        timeout = 60.0
    snapshot = await shell_tasks.output(
        str(getattr(context, "task_id", "") or ""),
        shell_id,
        block=bool(arguments.get("block", True)),
        timeout=timeout,
    )
    status = str(snapshot.get("status") or "")
    if status == "not_found":
        return fail("error", str(snapshot.get("message") or "没有找到该后台命令"))
    body = mask_output(str(snapshot.get("text") or ""), context.root)
    if len(body) > MAX_OUTPUT_CHARS:
        body = body[:MAX_OUTPUT_CHARS] + "\n…（输出已截断）"
    data = {
        key: snapshot[key]
        for key in ("shell_id", "command", "description", "status", "exit_code", "timed_out", "elapsed_seconds", "truncated")
        if key in snapshot
    }
    message = str(snapshot.get("message") or "")
    if status == "running":
        return ToolOutcome(ok=True, status="ok", output=f"{message}\n{body}".strip(), data=data)
    if status == "cancelled":
        return ToolOutcome(ok=False, status="error", output=body, data=data, message=message)
    if status != "completed":
        return ToolOutcome(ok=False, status="error", output=body, data=data, message=message)
    return ToolOutcome(ok=True, status="ok", output=body or "命令执行完成，无输出", data=data)


async def _shell_stop(context: ToolContext, shell_id: str) -> ToolOutcome:
    from blank_api.services import shell_tasks

    snapshot = await shell_tasks.stop(str(getattr(context, "task_id", "") or ""), shell_id)
    status = str(snapshot.get("status") or "")
    if status == "not_found":
        return fail("error", str(snapshot.get("message") or "没有找到该后台命令"))
    data = {
        key: snapshot[key]
        for key in ("shell_id", "command", "description", "status", "exit_code", "elapsed_seconds")
        if key in snapshot
    }
    if status == "cancelled":
        return ok(f"已取消后台命令 {shell_id}，进程已终止。", **data)
    return ok(f"后台命令 {shell_id} 已经结束（{status}），没有需要终止的运行。", **data)


async def _monitor_output(context: ToolContext, monitor_id: str, arguments: dict) -> ToolOutcome:
    from blank_api.services import shell_tasks
    from blank_api.services.tools.shell import MAX_OUTPUT_CHARS, mask_output

    try:
        timeout = float(arguments.get("timeout") or 60)
    except (TypeError, ValueError):
        timeout = 60.0
    snapshot = await shell_tasks.output(
        str(getattr(context, "task_id", "") or ""),
        monitor_id,
        block=bool(arguments.get("block", True)),
        timeout=timeout,
    )
    status = str(snapshot.get("status") or "")
    if status == "not_found":
        return fail("error", str(snapshot.get("message") or "没有找到该监控"))
    body = mask_output(str(snapshot.get("text") or ""), context.root)
    if len(body) > MAX_OUTPUT_CHARS:
        body = body[:MAX_OUTPUT_CHARS] + "\n…（输出已截断）"
    data = {
        key: snapshot[key]
        for key in (
            "monitor_id",
            "kind",
            "command",
            "description",
            "status",
            "exit_code",
            "timed_out",
            "elapsed_seconds",
            "truncated",
        )
        if key in snapshot
    }
    message = str(snapshot.get("message") or "")
    if status == "running":
        return ToolOutcome(ok=True, status="ok", output=f"{message}\n{body}".strip(), data=data)
    if status == "cancelled":
        return ToolOutcome(ok=False, status="error", output=body, data=data, message=message)
    if status != "completed":
        return ToolOutcome(ok=False, status="error", output=body, data=data, message=message)
    return ToolOutcome(ok=True, status="ok", output=body or "监控的命令已结束，没有输出", data=data)


async def _monitor_stop(context: ToolContext, monitor_id: str) -> ToolOutcome:
    from blank_api.services import shell_tasks

    snapshot = await shell_tasks.stop(str(getattr(context, "task_id", "") or ""), monitor_id)
    status = str(snapshot.get("status") or "")
    if status == "not_found":
        return fail("error", str(snapshot.get("message") or "没有找到该监控"))
    data = {
        key: snapshot[key]
        for key in ("monitor_id", "kind", "command", "description", "status", "exit_code", "elapsed_seconds")
        if key in snapshot
    }
    if status == "cancelled":
        return ok(f"已终止监控 {monitor_id}，监控的进程已结束。", **data)
    return ok(f"监控 {monitor_id} 已经结束（{status}），没有需要终止的运行。", **data)


async def _task_output(context: ToolContext, arguments: dict) -> ToolOutcome:
    handle_id = _handle_id(arguments)
    if not handle_id:
        return fail("error", "task_output 需要 task_id（后台子代理、后台命令或 monitor 的句柄）")
    if _is_monitor_handle(arguments, handle_id):
        return await _monitor_output(context, handle_id, arguments)
    if _is_shell_handle(arguments, handle_id):
        return await _shell_output(context, handle_id, arguments)
    if _is_workflow_handle(handle_id):
        return await _workflow_output(context, handle_id, arguments)
    runner = getattr(context, "subagent_runner", None)
    if runner is None or not hasattr(runner, "output"):
        return fail("error", "当前运行时没有启用后台子代理")
    agent_id = handle_id
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


async def _list_agents(context: ToolContext, arguments: dict) -> ToolOutcome:
    runner = getattr(context, "subagent_runner", None)
    if runner is None or not hasattr(runner, "list_agents"):
        return fail("error", "当前运行时没有启用后台子代理")
    items = runner.list_agents()
    if not items:
        return ok("当前没有后台子代理", agents=[], count=0)
    lines = [
        f"{item['agent_id']}  [{item['status']}]  {item['goal']}"
        for item in items
    ]
    return ok("\n".join(lines), agents=items, count=len(items))


async def _task_stop(context: ToolContext, arguments: dict) -> ToolOutcome:
    handle_id = _handle_id(arguments)
    if not handle_id:
        return fail("error", "task_stop 需要 task_id（后台子代理、后台命令或 monitor 的句柄）")
    if _is_monitor_handle(arguments, handle_id):
        return await _monitor_stop(context, handle_id)
    if _is_shell_handle(arguments, handle_id):
        return await _shell_stop(context, handle_id)
    if _is_workflow_handle(handle_id):
        return await _workflow_stop(context, handle_id)
    runner = getattr(context, "subagent_runner", None)
    if runner is None or not hasattr(runner, "stop"):
        return fail("error", "当前运行时没有启用后台子代理")
    agent_id = handle_id
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


async def _send_message(context: ToolContext, arguments: dict) -> ToolOutcome:
    runner = getattr(context, "subagent_runner", None)
    if runner is None or not hasattr(runner, "send_message"):
        return fail("error", "当前运行时没有启用子代理")
    agent_id = str(arguments.get("agent_id") or "").strip()
    if not agent_id:
        return fail("error", "send_message 需要 agent_id")
    message = str(arguments.get("message") or "").strip()
    if not message:
        return fail("error", "send_message 需要具体的后续问题")
    result = await runner.send_message(agent_id, message[:MAX_GOAL_CHARS])
    if not result.ok and not result.summary:
        return fail("error", result.error or "子代理续聊失败")
    return ok(
        result.render()[:8_000],
        ok=result.ok,
        summary=result.summary,
        steps=result.steps,
        agent_path=result.agent_path,
        agent_id=result.agent_id or agent_id,
        error=result.error,
        usage=result.usage,
    )


async def _spawn_agents(context: ToolContext, arguments: dict) -> ToolOutcome:
    runner = getattr(context, "subagent_runner", None)
    if runner is None:
        return fail("error", "当前运行时没有启用子代理")
    if not hasattr(runner, "fan_out"):
        return fail("error", "当前运行时没有启用并行子代理")
    requests, error = _parse_tasks(arguments.get("tasks"), context)
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
                "子代理默认在后台跑：立即返回 agent_id，结束时你会收到一条系统通知，"
                "不必靠轮询；需要更细的进度或完整文本时用 task_output，不要了用 task_stop。"
                "只有你的下一步马上就要它的结论、期间没有别的事可做时，才传 run_in_background=false 等它。"
                "子代理的结论要在你的最终答复里转述给用户；不要重复执行它已经做完的活，"
                "也不要把它还没给出的结论说成已完成。"
                "需要同时分头做几件事时改用 spawn_agents。"
                "子代理类型由工作区 .blank/agents/*.md（以及用户级 agents 目录）定义；"
                "不传 subagent_type 就是内置 general-purpose，未知类型会被拒绝并列出可用名字。"
                "subagent_type=\"fork\" 是内置分身类型：它继承你到此为止的完整对话、系统提示与工具权限，"
                "适合「接着我刚才查到的线索继续，但别占我的上下文」这类工作；fork 只能前台运行，"
                "不能放进 spawn_agents 的多任务批次。"
            ),
            input_schema={
                "type": "object",
                "required": ["goal"],
                "properties": {
                    "goal": {"type": "string", "description": "子任务的完整描述，子代理看不到当前对话"},
                    "subagent_type": {
                        "type": "string",
                        "description": (
                            "子代理类型，默认 general-purpose；"
                            "工作区 .blank/agents 里定义的名称也可以直接用；"
                            "fork 是内置分身（继承当前对话，只能前台）"
                        ),
                    },
                    # 上游 4302142：FORCE 置位时 schema 直接把 model 剃掉。
                    **(
                        {}
                        if _model_forced()
                        else {
                            "model": {
                                "type": "string",
                                "description": (
                                    "可选模型覆盖，压过子代理定义里的 model；不传就用定义，"
                                    "定义也没写就继承父会话模型。传空串或 inherit 表示回落父会话。"
                                    "fork 形态忽略此参数（分身恒继承父会话模型）。"
                                ),
                            }
                        }
                    ),
                    "isolate": {
                        "type": "boolean",
                        "description": "是否在临时隔离副本中执行（允许写文件，但只影响副本）",
                    },
                    "max_steps": {"type": "integer", "description": "子代理最多几步，默认 4，上限 8"},
                    "label": {"type": "string", "description": "隔离副本的名字前缀，便于辨认"},
                    "run_in_background": {
                        "type": "boolean",
                        "description": (
                            "默认 true：立即返回 agent_id 在后台跑，结束时收到系统通知，"
                            "结论用 task_output 取、task_stop 取消。只有下一步马上要它的结果、"
                            "期间没有别的事可做时才传 false（等到结束）。"
                        ),
                    },
                    "background": {
                        "type": "boolean",
                        "description": "run_in_background 的旧名，等价；新调用请用 run_in_background",
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
                "各自的结论由你在最终答复里转述；不要把某个子代理还没给出的结论说成已完成。"
            ),
            input_schema={
                "type": "object",
                "required": ["tasks"],
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": (
                            f"子任务列表，最多 {MAX_PARALLEL_AGENTS} 个；元素可以是字符串或 "
                            + (
                                "{goal, isolate, max_steps, subagent_type}；"
                                f"model 在本会话被 {MODEL_FORCE_ENV} 忽略，不要传。"
                                if _model_forced()
                                else "{goal, isolate, max_steps, subagent_type, model}；"
                                "model 压过子代理定义里的 model，不传就用定义。"
                            )
                            + "subagent_type 不传就是内置 general-purpose，"
                            "也可以用工作区 .blank/agents 里定义的类型；未知类型整批拒绝；"
                            "fork 只能走 spawn_agent，放进这里会整批拒绝"
                        ),
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
                "取后台任务的现状与输出：后台子代理（spawn_agent）的结论、后台命令"
                "（run_shell(run_in_background=true)）的输出、monitor 监控的日志，"
                "或工作流（workflow）的结果。"
                "block=true 时最多等 timeout 秒；等不到只表示它还在跑，不会因此取消它。"
                "返回状态 running / completed / failed / timeout / cancelled，未知句柄会如实报找不到。"
                "后台任务自己结束时你会收到系统通知，这里只在需要更细的进度或拿完整文本时用。"
                "取到的结论由你转述给用户，不要替未结束的任务编造结果。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "后台任务句柄（agent_id、shell_id 或 wf_ 工作流 id）",
                    },
                    "agent_id": {"type": "string", "description": "task_id 的旧名，只适用于后台子代理"},
                    "shell_id": {"type": "string", "description": "task_id 的旧名，只适用于后台命令"},
                    "block": {"type": "boolean", "description": "是否等待结束，默认 true"},
                    "timeout": {"type": "number", "description": "最多等几秒，默认 60，上限 600"},
                },
            },
            risk_level="low",
            handler=_task_output,
            timeout_seconds=330.0,
        ),
        ToolDefinition(
            name="send_message",
            description=(
                "给一个已经跑过的子代理发后续消息：它保留原来的上下文（读过的文件、已经给出的结论），"
                "只接着回答你新问的这一句。要换方向或开一个互不相干的任务请重新 spawn_agent——"
                "那是一个全新上下文。隔离子代理的副本结束即清理，这类子代理不能续聊，会直接拒绝。"
                "仍在后台跑的句柄需要先 task_output 等它结束，续聊本身是同步的。"
            ),
            input_schema={
                "type": "object",
                "required": ["agent_id", "message"],
                "properties": {
                    "agent_id": {"type": "string", "description": "spawn_agent 返回的句柄"},
                    "message": {
                        "type": "string",
                        "description": "后续问题或补充要求，子代理会带着上一轮的上下文回答",
                    },
                },
            },
            risk_level="medium",
            handler=_send_message,
            timeout_seconds=330.0,
        ),
        ToolDefinition(
            name="list_agents",
            description=(
                "列出本任务派出的后台子代理及各自状态（running / completed / failed / cancelled）。"
                "用来确认「还有谁在跑、谁已经结束」，不必逐个 task_output 去猜。"
                "已结束的结论仍用 task_output 取；这里只给状态与摘要，不代替转述。"
            ),
            input_schema={"type": "object", "properties": {}},
            risk_level="low",
            handler=_list_agents,
        ),
        ToolDefinition(
            name="task_stop",
            description=(
                "终止一个仍在运行的后台任务：后台子代理（隔离副本一并删除并留下 subagent.cancelled）、"
                "后台命令（进程终止并留下 shell.cancelled）、monitor 监控（进程终止并留下 monitor.cancelled），"
                "或工作流（脚本进程终止、仍在跑的子代理收走）。"
                "已经结束的句柄照实返回它当初的状态，不会谎报取消。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "要终止的后台任务句柄"},
                    "agent_id": {"type": "string", "description": "task_id 的旧名，只适用于后台子代理"},
                    "shell_id": {"type": "string", "description": "task_id 的旧名，只适用于后台命令"},
                },
            },
            risk_level="low",
            handler=_task_stop,
        ),
    ]

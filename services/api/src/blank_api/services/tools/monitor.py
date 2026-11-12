"""monitor：把一条命令的输出逐行当事件推送（对齐 Claude 的 Monitor 工具）。

Claude 的语义是「命令或 WebSocket 说一句，你就收到一条通知」：每条 stdout 行是一个
事件，结束时 watch 结束；期间不要轮询、不要 sleep，事件不是用户的发言。本实现只支持
command（WebSocket 尚未移植）：

* 进程、日志、超时、取消、任务收尾全部复用 ``shell_tasks`` 的后台句柄表；
* 输出按行切进事件队列，由 ``drain_monitor_events`` 在每次模型调用前投递；
* 命令仍过 ``policy_gate("run_shell")``、安全超时与沙箱档位——监控不是绕过执行边界
  的旁路，只是把同一条命令的输出换了个投递方式。
"""

from __future__ import annotations

from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok
from blank_api.services.tools.shell import (
    _event_recorder,
    assess_risk,
    execute_command,
    mask_output,
    normalize_python_command,
    policy_gate,
)

# 与 Claude 契约同一组边界：默认 5 分钟，上限 30 分钟；0 表示不设监控期限。
MONITOR_DEFAULT_TIMEOUT_MS = 300_000
MONITOR_MAX_TIMEOUT_MS = 1_800_000


def _timeout_ms(arguments: dict) -> int:
    raw = arguments.get("timeout_ms")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("timeout_ms 必须是整数毫秒（0 表示不设监控期限）")
    if int(raw) != raw:
        raise ValueError("timeout_ms 必须是整数毫秒")
    value = int(raw)
    if value < 0:
        raise ValueError("timeout_ms 不能为负数（0 表示不设监控期限）")
    if value > MONITOR_MAX_TIMEOUT_MS:
        raise ValueError(f"timeout_ms 必须 ≤ {MONITOR_MAX_TIMEOUT_MS}（30 分钟）")
    return value


def _budget(timeout_ms: int, context: ToolContext) -> tuple[float, list[str]]:
    """算出命令实际能跑多久，以及「请求的期限和安全边界不一致」时该怎么如实说。"""
    from blank_api.services import shell_tasks

    safety = getattr(context, "safety", None)
    limit = float(getattr(safety, "timeout_seconds", 0) or 0) if safety is not None else 0.0
    requested = timeout_ms / 1000 if timeout_ms else 0.0
    budget = requested or (limit or shell_tasks.DEFAULT_BACKGROUND_TIMEOUT)
    if limit:
        budget = min(budget, limit)
    budget = max(0.1, budget)
    notes: list[str] = []
    if timeout_ms == 0:
        if limit and budget < shell_tasks.DEFAULT_BACKGROUND_TIMEOUT:
            notes.append(
                f"timeout_ms=0：监控本身不设期限（直到 task_stop 或任务结束）；"
                f"但命令执行受安全超时 {limit:g}s 限制，到点会被终止、监控随之结束。"
            )
        else:
            notes.append("timeout_ms=0：监控不设期限，直到命令自己结束、task_stop 或任务收尾。")
    elif requested > budget:
        notes.append(
            f"请求监控 {requested:g}s，但命令执行受安全超时 {limit:g}s 限制，"
            f"实际 {budget:g}s 后会终止。"
        )
    return budget, notes


async def _record_started(context: ToolContext, record, command: str) -> None:
    recorder = _event_recorder(context)
    if recorder is None:
        return
    try:
        from blank_api.services.log_safety import redact

        await recorder.add(
            "monitor.started",
            "monitor",
            {
                "monitor_id": record.shell_id,
                "command": redact(command)[:300],
                "description": record.description[:200],
                "timeout_seconds": record.timeout_seconds,
            },
        )
    except Exception:  # noqa: BLE001 - 记账失败不该让监控起不来
        pass


async def _monitor(context: ToolContext, arguments: dict) -> ToolOutcome:
    command = str(arguments.get("command") or "").strip()
    if not command:
        return fail("error", "monitor 需要 command：要监控的那条命令")
    description = str(arguments.get("description") or "").strip()
    if not description:
        return fail("error", "monitor 需要 description：这条监控在盯什么（每条通知里都会带上）")
    try:
        timeout_ms = _timeout_ms(arguments)
    except ValueError as error:
        return fail("error", str(error))

    blocked = policy_gate(context, "run_shell", arguments)
    if blocked is not None:
        return blocked
    if not getattr(context, "allow_background_shell", True):
        return ToolOutcome(
            ok=False,
            status="denied",
            message=(
                "子代理里不能启动 monitor：它的工作副本在本轮结束时会被清理，"
                "监控活不到有人取事件的时候。"
            ),
        )

    command = normalize_python_command(command, context.root)
    budget, notes = _budget(timeout_ms, context)
    snapshot = getattr(context, "shell", None)
    env = snapshot.env_for() if hasattr(snapshot, "env_for") else None
    safety = getattr(context, "safety", None)

    def runner(sink):
        return execute_command(
            command,
            context.root,
            budget,
            env=env,
            safety=safety,
            tier=getattr(context, "sandbox_tier", "") or "",
            isolation_profile=str(getattr(context, "isolation_profile", "") or ""),
            on_chunk=sink,
        )

    from blank_api.services import shell_tasks

    record = shell_tasks.start(
        task_id=str(getattr(context, "task_id", "") or ""),
        command=command,
        description=description,
        timeout_seconds=budget,
        runner=runner,
        recorder=_event_recorder(context),
        mask=lambda text: mask_output(text, context.root),
        kind="monitor",
    )
    await _record_started(context, record, command)
    body = (
        f"已开始监控 {record.shell_id}：{command[:160]}\n"
        "每条输出行都会在后续的系统通知里投给你；这期间照常做别的事。"
        "不要轮询、不要为等输出而 sleep——事件不是用户的发言。"
        "命令退出、被 task_stop 或超时都会结束监控，结束时你会收到通知。"
        f'最近日志用 task_output(task_id="{record.shell_id}") 取。'
    )
    if notes:
        body = f"{body}\n" + "\n".join(notes)
    return ok(
        body,
        monitor_id=record.shell_id,
        kind="monitor",
        status="running",
        command=command,
        description=record.description,
        timeout_ms=timeout_ms,
        timeout_seconds=budget,
        persistent=timeout_ms == 0,
        notes=notes,
    )


def monitor_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="monitor",
            description=(
                "监控一条命令的输出（watch / tail / 盯进程或日志）：每条输出行都会作为事件"
                "在系统通知里投给你，命令退出即结束监控。适合「有个东西会慢慢输出，我想随时知道」"
                "这类场景；要的是最终结果而不是过程，用 run_shell(run_in_background=true)。"
                "监控期间照常做别的事：不要轮询、不要为等事件而 sleep；事件不是用户的发言。"
                "timeout_ms 默认 300000、上限 1800000（30 分钟），0 表示不设监控期限；"
                "实际时长仍受安全设置的单条命令超时约束。"
                "命令结束、被 task_stop 或超时会结束监控；最近日志用 task_output 取。"
                "不支持 WebSocket 监控（Claude 的 ws 变体尚未移植）。"
            ),
            input_schema={
                "type": "object",
                "required": ["description", "command", "timeout_ms"],
                "additionalProperties": False,
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "这条监控在盯什么（每条通知里都会带上，便于辨认）",
                    },
                    "command": {"type": "string", "description": "要监控的命令"},
                    "timeout_ms": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": MONITOR_MAX_TIMEOUT_MS,
                        "description": (
                            f"监控期限（毫秒），默认 {MONITOR_DEFAULT_TIMEOUT_MS}；"
                            f"上限 {MONITOR_MAX_TIMEOUT_MS}；0 表示不设期限，直到命令退出或 task_stop"
                        ),
                    },
                },
            },
            risk_level="medium",
            risk_for=lambda arguments: assess_risk(str(arguments.get("command", ""))),
            handler=_monitor,
            timeout_seconds=30.0,
        )
    ]

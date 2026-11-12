"""repl：用一段持久 JavaScript 组合多次工具调用（对齐 Claude 的 REPL 工具）。

Claude 的契约：``code``（必填，支持顶层 await，变量跨调用保留）、
``description``（可选，一句话说明这段脚本在做什么）、``timeout``（默认 30000，
上限 600000）。返回值里有 code / result / stdout / stderr / error。

边界与顶层调用完全一致：脚本本身跑在密封的 Node ``vm`` 上下文里（没有
``require`` / ``process`` / ``import``），文件、命令、检索只能通过注入的工具函数
发生；每次内层调用都回宿主走同一套网关（场景禁用、安全策略、权限、审计、沙箱）。
需要审批的内层调用无法弹卡，因此直接拒绝并回 ``{error}``——模型应把这一步改成
顶层工具调用。
"""

from __future__ import annotations

import json
import time

from blank_api.services.agent import repl as repl_module
from blank_api.services.agent import workflow as workflow_module
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok

# 脚本里不放这些工具：会话/编排类工具要么自己开进程，要么依赖图里的中断/审批
# 状态，塞进一次 REPL 调用只会把语义搅乱。其余工具按当前任务真实可用的清单注入。
INNER_TOOL_DENY = frozenset(
    {
        "repl",
        "ask_user",
        "enter_plan_mode",
        "exit_plan_mode",
        "update_plan",
        "spawn_agent",
        "spawn_agents",
        "send_message",
        "task_output",
        "task_stop",
        "workflow",
        "cron_create",
        "cron_delete",
        "cron_list",
        "schedule_wakeup",
        "skill",
        "tool_search",
    }
)

NODE_GAP = (
    "本机没有可用的 Node.js，REPL 无法执行（不是脚本写错了）。"
    "先补齐再重跑：安装 Node 20+（winget install OpenJS.NodeJS.LTS / "
    "choco install nodejs-lts / 或从 nodejs.org 下载），"
    "或用环境变量 BLANK_NODE_BIN 指向已有的 node.exe。"
)


def _timeout_ms(arguments: dict) -> int:
    raw = arguments.get("timeout")
    if raw is None:
        return repl_module.REPL_DEFAULT_TIMEOUT_MS
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("timeout 必须是整数毫秒")
    if int(raw) != raw:
        raise ValueError("timeout 必须是整数毫秒")
    value = int(raw)
    if value < 1:
        raise ValueError("timeout 必须是正整数毫秒")
    if value > repl_module.REPL_MAX_TIMEOUT_MS:
        raise ValueError(f"timeout 必须 ≤ {repl_module.REPL_MAX_TIMEOUT_MS}（10 分钟）")
    return value


def _render(message: dict) -> tuple[str, dict]:
    stdout = str(message.get("stdout") or "")
    stderr = str(message.get("stderr") or "")
    error = str(message.get("error") or "")
    result = message.get("result")
    has_result = "result" in message
    data: dict = {
        "code": str(message.get("code") or ""),
        "stdout": stdout[: repl_module.MAX_STREAM_CHARS],
        "stderr": stderr[: repl_module.MAX_STREAM_CHARS],
        "tool_calls": int(message.get("tool_calls") or 0),
    }
    if has_result:
        try:
            data["result"] = result
        except Exception:  # noqa: BLE001 - 结果已是 JSON 值，这里只是防御
            data["result"] = str(result)
    if error:
        data["error"] = error[: repl_module.MAX_ERROR_CHARS]
    if message.get("truncated"):
        data["truncated"] = True
    if message.get("timed_out"):
        data["timed_out"] = True
    if message.get("session_restarted"):
        data["session_restarted"] = True

    parts: list[str] = []
    if has_result:
        try:
            rendered = json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            rendered = str(result)
        parts.append(f"[result]\n{rendered}")
    if stdout:
        parts.append(f"[stdout]\n{stdout}")
    if stderr:
        parts.append(f"[stderr]\n{stderr}")
    if error:
        parts.append(f"[error]\n{error}")
    if message.get("truncated"):
        parts.append("[输出超过上限，已截断]")
    if not parts:
        parts.append("执行完成（没有输出，也没有返回值）。")
    return "\n".join(parts), data


async def _repl(context: ToolContext, arguments: dict) -> ToolOutcome:
    code = arguments.get("code")
    if not isinstance(code, str) or not code.strip():
        return fail("error", "repl 需要 code：要执行的 JavaScript")
    if len(code) > repl_module.MAX_CODE_CHARS:
        return fail(
            "error",
            f"code 太长（{len(code)} 字符），上限 {repl_module.MAX_CODE_CHARS}；"
            "请只保留这次需要检查的部分",
        )
    try:
        timeout_ms = _timeout_ms(arguments)
    except ValueError as error:
        return fail("error", str(error))

    gateway = getattr(context, "tool_gateway", None)
    if gateway is None or not hasattr(gateway, "execute"):
        return fail(
            "error",
            "REPL 只在主代理的完整运行环境里可用：子代理没有独立网关，"
            "请直接用普通工具完成这一步。",
        )
    node = workflow_module.find_node()
    if not node:
        return ToolOutcome(
            ok=False,
            status="skipped",
            message=NODE_GAP,
            data={"missing_command": "node", "skipped_reason": "本机没有 Node.js，REPL 没有执行"},
        )

    description = str(arguments.get("description") or "").strip()
    excluded = set(getattr(gateway, "excluded", ()) or ())
    names = sorted(
        name for name in gateway.tools if name not in INNER_TOOL_DENY and name not in excluded
    )

    async def runner(name: str, call_arguments: dict) -> dict:
        if name in INNER_TOOL_DENY:
            return {
                "ok": False,
                "status": "denied",
                "message": f"{name} 不能在 REPL 里调用：它是会话/编排类工具，请用顶层工具调用。",
            }
        return await gateway.execute(name, call_arguments, context, inner=True)

    session = await repl_module.get_session(context.task_id, node)
    started = time.perf_counter()
    try:
        message = await session.evaluate(
            code,
            timeout_ms,
            runner,
            root=context.root,
            tools=names,
        )
    except repl_module.ReplTimeout as error:
        message = {
            "stdout": "",
            "stderr": "",
            "error": (
                f"{error}：超过 timeout={timeout_ms}ms。卡住的脚本已被终止，"
                "REPL 状态已清空（下一次调用从新会话开始）；"
                "需要长任务的请拆小，或改用 run_shell(run_in_background=true)。"
            ),
            "timed_out": True,
            "session_restarted": True,
        }
    except repl_module.ReplError as error:
        message = {
            "stdout": "",
            "stderr": "",
            "error": f"{error}；REPL 状态已清空，下一次调用会重建会话。",
            "session_restarted": True,
        }
    message["code"] = code
    message["tool_calls"] = session.tool_calls
    duration_ms = int((time.perf_counter() - started) * 1000)
    text, data = _render(message)
    await _record(context, description, code, data, duration_ms)
    if message.get("error"):
        return ToolOutcome(
            ok=False,
            status="timeout" if message.get("timed_out") else "error",
            output=text,
            message=str(message.get("error") or "")[: repl_module.MAX_ERROR_CHARS],
            data=data,
        )
    return ok(text, **data)


async def _record(
    context: ToolContext, description: str, code: str, data: dict, duration_ms: int
) -> None:
    # 与 workflow 同一口径：直接写任务级 recorder，拿不到就不记（事件是旁路）。
    recorder = getattr(context, "recorder", None)
    if recorder is None:
        return
    try:
        await recorder.add(
            "repl.executed",
            "repl",
            {
                "description": description[:300],
                "code_chars": len(code),
                "duration_ms": duration_ms,
                "tool_calls": data.get("tool_calls", 0),
                "ok": "error" not in data,
                "timed_out": bool(data.get("timed_out")),
                "session_restarted": bool(data.get("session_restarted")),
                "result_chars": len(json.dumps(data.get("result"), ensure_ascii=False))
                if "result" in data
                else 0,
            },
        )
    except Exception:  # noqa: BLE001 - 事件是旁路，不能把执行本身带崩
        pass


def repl_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="repl",
            description=(
                "在持久 JavaScript 环境里组合工具调用：支持顶层 await，变量跨调用保留，"
                "最后一条表达式就是返回值；console.log / console.error 分别回 stdout / stderr。"
                "工具按本地名字当异步函数用：await read_file({path}), await edit_file({...}), "
                "await search_code({...}), await run_shell({command}), await glob({...}) 等；"
                "参数结构与顶层调用完全一致。import/require/process 不可用（上下文是密封的）。"
                "一次调用里可以循环、分支、用 Promise.all 并行；把一次调查的多步合并成一段脚本，"
                "不要为每一步单独调用一次。内层调用走同一套权限与沙箱；需要审批的动作在 REPL 里"
                "会被拒绝（弹不出审批卡），请把这类动作改成顶层工具调用。"
                "timeout 默认 30000ms、上限 600000ms；超时会终止会话并清空状态。"
            ),
            input_schema={
                "type": "object",
                "required": ["code"],
                "additionalProperties": False,
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "要执行的 JavaScript（支持顶层 await，变量跨调用保留）。"
                            "最后一条表达式作为返回值；需要多步调查时写成一段脚本"
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "一句话说明这段脚本在做什么（主动语态，5-10 个词）",
                    },
                    "timeout": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": repl_module.REPL_MAX_TIMEOUT_MS,
                        "description": (
                            f"执行期限（毫秒），默认 {repl_module.REPL_DEFAULT_TIMEOUT_MS}；"
                            f"上限 {repl_module.REPL_MAX_TIMEOUT_MS}"
                        ),
                    },
                },
            },
            risk_level="low",
            handler=_repl,
            timeout_seconds=repl_module.REPL_MAX_TIMEOUT_MS / 1000 + 10,
        )
    ]


__all__ = ["INNER_TOOL_DENY", "NODE_GAP", "repl_tools"]

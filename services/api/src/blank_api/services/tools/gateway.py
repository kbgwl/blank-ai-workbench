"""Tool Gateway: validates, executes, records and redacts every tool call."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Sequence

from blank_api.api.errors import ApiError
from blank_api.models.task import ToolCall
from blank_api.services.log_safety import redact
from blank_api.services.models.base import ToolSpecLite
from blank_api.services.agent.artifacts import ArtifactStore
from blank_api.services.permissions import (
    ASK,
    DENY,
    Decision,
    approval_options,
    ask,
    approve,
    deny,
    tool_grant_too_wide,
)
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, approval_key, validate_arguments
from blank_api.services.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

MAX_LOGGED_CHARS = 4000
MAX_RESULT_CHARS = 8000
APPROVAL_STATUS = "awaiting_approval"
# 命令类工具（run_shell / run_tests）在结果里写这个键；只读工具不写。
# 于是「键在不在」本身就是「这次调用起没起进程」的判据，见 _sandbox_backend。
SANDBOX_DATA_KEY = "sandbox"


def _redact(text: str, secrets: Sequence[str], limit: int = MAX_LOGGED_CHARS) -> str:
    return redact(text, secrets)[:limit]


def _redact_value(value, secrets: Sequence[str], limit: int = MAX_RESULT_CHARS):
    if isinstance(value, str):
        return _redact(value, secrets, limit)
    if isinstance(value, dict):
        return {key: _redact_value(item, secrets, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, secrets, limit) for item in value]
    return value


def _sandbox_backend(outcome: ToolOutcome) -> tuple[str | None, bool]:
    """(跑在哪, 这次调用起没起进程)。

    命令类工具在沙箱里跑时写后端名（如 docker），沙箱开关关着时写 None ——
    意思是「进程直接落在宿主上」，而不是「没跑命令」。所以判据取键的存在性：
    把宿主直连和只读工具都读成"缺字段"，最该露出来的那一档就跟没发生过一样。
    """
    data = outcome.data if isinstance(outcome.data, dict) else {}
    if data.get("tier_blocked"):
        # 档位不可用时命令根本没起。读成"跑在宿主上"是最危险的一种误读：
        # 它会把一条被拒绝的命令记成一次宿主执行，审计里正好反过来。
        return None, False
    if SANDBOX_DATA_KEY not in data:
        return None, False
    value = data[SANDBOX_DATA_KEY]
    return (value if isinstance(value, str) and value else None), True


# 边界种类：一次工具调用实际落在哪一层。"审批问没问"与"在哪执行"是两回事，
# 证据里必须分得开——同一个工具在不同档位下可能落在完全不同的边界上。
BOUNDARY_BLOCKED = "blocked"  # 档位不可用，没执行
BOUNDARY_MCP_HOST = "mcp_host_process"  # 调用走外部 MCP 服务端；stdio 服务端自 #94 起挂 Job，边界见 boundaries.mcp
BOUNDARY_PROCESS = "process"  # 我们起的子进程（受沙箱档位约束）
BOUNDARY_IN_PROCESS = "in_process"  # 在 API 进程内完成（文件读写、规划等）
BOUNDARY_FS_BROKER = "fs_broker"  # 文件工具在 fs-broker 子进程里完成（#105，挂 Job Object）
# broker 失败的副作用口径（本体在 fs_broker.SIDE_EFFECTS_*，这里镜像一次以免网关热路径依赖导入）：
# 值必须与那边相等，由 tests/test_fs_broker_v93.py 的一条判据当场钉住。
FS_SIDE_EFFECTS_UNKNOWN = "unknown"


def _boundary_kind(name: str, outcome: ToolOutcome, ran_command: bool) -> str:
    data = outcome.data if isinstance(outcome.data, dict) else {}
    if data.get("tier_blocked"):
        return BOUNDARY_BLOCKED
    fs_error = data.get("fs_broker_error")
    if isinstance(fs_error, dict) and fs_error.get("layer"):
        # broker 坏了。`blocked` 在这里的口径是"这次调用没有成功交付结果"，不是"磁盘上什么都没发生"：
        # 请求已经进过 broker 的那些失败（超时/取消）side_effects=unknown——动作可能已经落盘，
        # 把边界记成 blocked（"根本没执行"）就是替这次动作开脱，所以这类失败仍记在 fs_broker 边界上。
        # 是哪一层坏的、动作发生没有，由 fs_broker_error 单独说，不混进 blocked 的文案里。
        # 这里不 import fs_broker：每条工具调用都要过这个函数，网关不该在热路径上依赖 broker 可导入；
        # 两个常量的相等由 test_fs_broker_v93 钉住（口径漂移当场就红）。
        if str(fs_error.get("side_effects") or "") == FS_SIDE_EFFECTS_UNKNOWN:
            return BOUNDARY_FS_BROKER
        return BOUNDARY_BLOCKED
    if data.get("fs_broker"):
        return BOUNDARY_FS_BROKER
    if str(name or "").startswith("mcp__"):
        return BOUNDARY_MCP_HOST
    if ran_command:
        return BOUNDARY_PROCESS
    return BOUNDARY_IN_PROCESS


def _command_enforcement(payload: dict) -> dict:
    """从工具结果里取"这一次执行实际被什么强制"。缺键就是没起进程，不补默认值。"""
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    enforcement = data.get("enforcement")
    if not isinstance(enforcement, dict) or not enforcement:
        return {}
    keep = (
        "limits_applied", "boundary_strength", "boundary_strength_label",
        "memory_enforced", "memory_bytes", "pids_enforced", "pids_limit",
        "tree_kill_enforced", "filesystem_enforced", "network_enforced",
        "cpu_enforced", "disk_enforced", "job_created", "job_assigned", "job_error",
        # 档位：事件里要能回答"这条命令当时按哪一档跑、降级了没有、为什么"。
        "sandbox_tier", "sandbox_tier_requested", "sandbox_tier_degraded",
        "tier_available", "tier_reason", "tier_notice",
    )
    return {key: enforcement[key] for key in keep if key in enforcement}


def sandbox_backend_event_field(payload: dict) -> dict:
    """把执行边界原样带进 `tool.completed` 事件，口径只在这里定义一次。

    键缺席 = 这次调用没起进程（只读工具、被拒绝的调用）；值为 null = 进程起在宿主上。
    这两个状态在证据里必须长得不一样，否则「宿主直连执行」就藏进了一片缺字段里。
    `sandbox_enforcement` 回答的是另一半问题：起了进程之后，policy 里那些数字**哪几个真的被执行了**。
    只记后端名，就还会重演"面板写 512m、内核什么都没拦"那一类谎。
    """
    out: dict = {}
    # 每次调用都要能回答"按哪一档跑、实际落在哪一层边界"。它们与起没起进程无关，
    # 所以必须放在下面那个早退之前：只读工具也有边界，只是边界在进程内。
    mode = payload.get("sandbox_mode")
    if isinstance(mode, str) and mode:
        out["sandbox_mode"] = mode
    kind = payload.get("boundary_kind")
    if isinstance(kind, str) and kind:
        out["boundary_kind"] = kind
    # fs-broker 的两笔账与档位无关、与"这次调用落在哪一层边界"有关，跟着一起走。
    for key in ("fs_broker_latency", "fs_broker_error"):
        if isinstance(payload.get(key), dict):
            out[key] = payload[key]
    if "sandbox_blocked" in payload:
        # 档位不可用：命令没起，但"为什么没起"必须进事件，否则界面只剩一条普通的 denied。
        out["sandbox_blocked"] = payload["sandbox_blocked"]
    if "sandbox_backend" not in payload:
        # 没起进程（只读工具、被拒的调用）：两条事实一起缺席，读的一侧不用分辨"缺一个还是缺两个"。
        return out
    out["sandbox_backend"] = payload["sandbox_backend"]
    if payload.get("sandbox_bypass") is True:
        # 越界与"跑在宿主上"是两个事实：关档（off）同样 sandbox_backend=null，
        # 但那是本来的形态，不是绕过。缺这一键就不许在证据里说成越界。
        out["sandbox_bypass"] = True
        out["sandbox_bypass_reason"] = str(payload.get("sandbox_bypass_reason") or "")
    enforcement = _command_enforcement(payload)
    if enforcement:
        out["sandbox_enforcement"] = enforcement
    return out


def _audit_action(name: str, arguments: dict) -> str:
    """审计行的一行摘要：优先取能看懂这次动作干了什么的参数。"""
    for key in ("command", "path", "pattern", "query", "goal"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return f"{name} {value.strip()[:200]}"
    return name


def _audit(
    name: str,
    arguments: dict,
    outcome: ToolOutcome,
    context: ToolContext,
    node: str,
    duration_ms: int,
    backend: str | None,
    ran_command: bool,
) -> None:
    """每次经过网关的调用（含被拒绝的）落一条审计账，按任务可还原调用链。

    命令已经跑完了才写账，写失败不能反过来把调用判成失败，所以这里只留日志。
    但也不能无声吞掉：审计通道断了却没人知道，比没有审计更危险。
    """
    from blank_api.services.sandbox import AuditRecord, audit_log  # 延迟导入，与 shell.py 同一口径

    data = outcome.data if isinstance(outcome.data, dict) else {}
    exit_code = data.get("exit_code")
    try:
        audit_log().record(
            AuditRecord(
                action=_audit_action(name, arguments),
                tool=name,
                task_id=context.task_id,
                node=node,
                project_id=(context.team_scope or {}).get("project_id") or None,
                workspace=str(context.root),
                detail={
                    "arguments": _redact_value(arguments, context.extra_secrets, MAX_LOGGED_CHARS),
                    "ran_command": ran_command,
                    "host_execution": ran_command and backend is None,
                    # 审计账也要能和事件对得上：同一次调用在两处读出的档位/边界必须一致。
                    "sandbox_mode": str(context.sandbox_tier or ""),
                    "boundary_kind": _boundary_kind(name, outcome, ran_command),
                    # fs-broker 那一层的事实同步进审计账：broker 失败时事件之外还要有一条能对质的。
                    "fs_broker_layer": (
                        "ok" if data.get("fs_broker")
                        else str((data.get("fs_broker_error") or {}).get("layer") or "")
                    ),
                    "message": outcome.message[:500],
                    # 单次越界必须和“本来就没开沙箱”分开：审计账是第三处能对质的地方。
                    "sandbox_bypass": bool(data.get("sandbox_bypass")),
                    "sandbox_bypass_reason": str(data.get("bypass_reason") or ""),
                },
                exit_code=exit_code if isinstance(exit_code, int) else None,
                duration_ms=duration_ms,
                backend=backend,
                status=outcome.status,
            ),
            context.extra_secrets,
        )
    except Exception as exc:  # noqa: BLE001 - 审计写入失败不该改判本次调用
        log.warning("审计日志写入失败（本次调用结果不受影响）：%s %s", exc.__class__.__name__, exc)


class ToolGateway:
    def __init__(self, tools: Sequence[ToolDefinition], *, defer: bool = True) -> None:
        self.tools: dict[str, ToolDefinition] = {tool.name: tool for tool in tools}
        # 并发执行只读工具时，多条 _record 会同时落库。SQLite 的连接池本身有 30s
        # 锁等待兜底，但内存库用 StaticPool 只有一个连接，并发拿同一个连接会直接
        # 抛错——这类失败偶发且难复现，用一把锁把写库这一段串起来最省心。
        # 写库是毫秒级，串行化它不影响并行的收益（省下的是 handler 的等待）。
        self._record_lock = asyncio.Lock()
        # 注册表决定"模型这一轮看得见哪些工具"。defer=False 给子代理用：
        # 它拿到的本来就是一小组白名单工具，再藏一层只会让它无从下手。
        self.registry = ToolRegistry(tools, activate_all=not defer)

    def specs(self) -> list[ToolSpecLite]:
        return [tool.spec() for tool in self.registry.visible()]

    def get(self, name: str) -> ToolDefinition | None:
        return self.tools.get(name)

    async def refuse(
        self, name: str, arguments: dict, context: ToolContext, node: str = "", reason: str = ""
    ) -> dict:
        """审批驳回或钩子拦截的调用：不执行，但必须留下可审计的拒绝记录。"""
        tool = self.tools.get(name)
        risk = tool.resolve_risk(arguments) if tool else "high"
        outcome = ToolOutcome(
            ok=False,
            status="denied",
            message=reason.strip() or f"高风险操作未获批准，已拒绝执行：{name}",
        )
        return await self._finish(name, arguments, outcome, context, node, risk, time.perf_counter())

    async def settle(
        self,
        name: str,
        arguments: dict,
        outcome: ToolOutcome,
        context: ToolContext,
        node: str = "",
        rule: str = "external",
    ) -> dict:
        """结果由外部决定（例如用户回答了问题）：不调用 handler，但照样记账留证据。"""
        tool = self.tools.get(name)
        risk = tool.resolve_risk(arguments) if tool else "low"
        return await self._finish(
            name, arguments, outcome, context, node, risk, time.perf_counter(), approve(risk, rule)
        )

    async def execute(self, name: str, arguments: dict, context: ToolContext, node: str = "") -> dict:
        started = time.perf_counter()
        tool = self.tools.get(name)
        if tool is None:
            risk = "high"
            outcome = ToolOutcome(ok=False, status="denied", message=f"未注册的工具：{name}")
            decision = deny(risk, "unregistered", outcome.message)
        else:
            risk = tool.resolve_risk(arguments)
            decision = self.decide(tool, arguments, context, risk)
            invalid = validate_arguments(tool.input_schema, arguments)
            if invalid is not None:
                outcome = ToolOutcome(ok=False, status="error", message=invalid)
            elif decision.outcome == DENY:
                outcome = self._denied(tool, arguments, decision)
            elif decision.outcome == ASK:
                outcome = self._awaiting_approval(tool, arguments, risk, decision)
            else:
                outcome = await self._run(tool, arguments, context)
        # decision.risk 可能被安全策略抬高（"每条命令都要确认"），台账要按最终口径记
        return await self._finish(
            name, arguments, outcome, context, node, decision.risk or risk, started, decision
        )

    def decide(
        self,
        tool: ToolDefinition,
        arguments: dict,
        context: ToolContext,
        risk: str | None = None,
    ) -> Decision:
        """统一的权限判定入口：图里的审批门与网关必须拿到同一个答案。"""
        risk = tool.resolve_risk(arguments) if risk is None else risk
        # 先过能力边界。这里拒绝的动作，就算用户在审批卡上点了「允许」也不执行，
        # 所以必须在生成问句之前拦掉 —— 否则会变成"点了允许还是失败"。
        mode = context.permissions.mode if context.permissions is not None else "default"
        if context.safety is not None:
            verdict = context.safety.gate(tool.name, arguments, context.root, mode=mode)
            if verdict.denied:
                return deny(risk, verdict.rule, verdict.reason, verdict.prefix)
            # 「每条命令都要确认」不单独造一套状态机：抬高风险等级即可，
            # 审批、会话记忆、前缀白名单全都复用同一套既有逻辑。
            if verdict.asks and risk not in ("high", "critical"):
                risk = "high"
        if context.permissions is not None:
            return context.permissions.decide(tool, arguments, risk)
        if context.approval_policy is not None and self._approved(tool, arguments, context):
            return approve(risk, "legacy:approved")
        if risk in ("high", "critical"):
            return ask(risk, "legacy:ask")
        return approve(risk, "legacy:auto")

    async def _finish(
        self,
        name: str,
        arguments: dict,
        outcome: ToolOutcome,
        context: ToolContext,
        node: str,
        risk: str,
        started: float,
        decision: Decision | None = None,
    ) -> dict:
        duration_ms = int((time.perf_counter() - started) * 1000)
        safe = ToolOutcome(
            ok=outcome.ok,
            status=outcome.status,
            output=_redact(outcome.output, context.extra_secrets, MAX_RESULT_CHARS),
            message=_redact(outcome.message, context.extra_secrets, MAX_RESULT_CHARS),
            data=_redact_value(outcome.data, context.extra_secrets),
        )
        tool_call_id, artifact_path = await self._record(
            name, arguments, safe, context, node, risk, duration_ms
        )
        payload = safe.as_dict()
        payload.update(
            {
                "tool": name,
                "risk_level": risk,
                "duration_ms": duration_ms,
                "tool_call_id": tool_call_id,
                "artifact_path": artifact_path,
                "permission": decision.as_dict() if decision is not None else {},
            }
        )
        backend, ran_command = _sandbox_backend(safe)
        if ran_command:
            payload["sandbox_backend"] = backend
        elif safe.data.get("tier_blocked"):
            facts = safe.data.get("enforcement") or {}
            payload["sandbox_blocked"] = facts.get("tier_notice") or "沙箱档位不可用，命令未执行"
        # fs-broker 的两笔账提到顶层（与 sandbox_blocked 同一风格）：
        # latency 回答"IPC 往返多慢"，error 回答"是哪一层坏了、为什么没执行"。
        if isinstance(safe.data.get("fs_broker_latency"), dict):
            payload["fs_broker_latency"] = safe.data["fs_broker_latency"]
        if isinstance(safe.data.get("fs_broker_error"), dict):
            payload["fs_broker_error"] = safe.data["fs_broker_error"]
        # 档位与边界种类对每次调用都写：面板/证据包按 tool.completed 就能还原
        # "这个动作当时被哪一层拦住、还是在宿主进程里跑的"。
        payload["sandbox_mode"] = str(context.sandbox_tier or "")
        payload["boundary_kind"] = _boundary_kind(name, safe, ran_command)
        if ran_command and safe.data.get("sandbox_bypass") is True:
            # 顶层写一次：事件读的是这里，证据包再把同一份贴回工具清单。
            payload["sandbox_bypass"] = True
            payload["sandbox_bypass_reason"] = str(safe.data.get("bypass_reason") or "")
        _audit(name, arguments, safe, context, node, duration_ms, backend, ran_command)
        return payload

    def _approved(self, tool: ToolDefinition, arguments: dict, context: ToolContext) -> bool:
        try:
            return bool(context.approval_policy(tool, arguments))
        except Exception:
            return False

    def _denied(
        self, tool: ToolDefinition, arguments: dict, decision: Decision
    ) -> ToolOutcome:
        return ToolOutcome(
            ok=False,
            status="denied",
            message=decision.reason or f"权限策略拒绝执行：{tool.name}",
            data={
                "requires_approval": False,
                "tool": tool.name,
                "arguments": arguments,
                "risk_level": decision.risk,
                "permission_rule": decision.rule,
            },
        )

    def _awaiting_approval(
        self, tool: ToolDefinition, arguments: dict, risk: str, decision: Decision | None = None
    ) -> ToolOutcome:
        raw_prefix = decision.prefix if decision is not None else ""
        # 建议的「总是允许 X」取自决策里那条可记忆规则（解释器类带正文），
        # 不是命令首段：只拿 raw_prefix 会把 `python -c …` 缩成 `python`。
        memo = decision.memo if decision is not None else ""
        return ToolOutcome(
            ok=False,
            status=APPROVAL_STATUS,
            message=(
                f"工具 {tool.name} 的风险等级为 {risk}，未获得审批，已拒绝执行并生成待审批请求。"
                "请在审批通过后重新运行该步骤。"
            ),
            data={
                "requires_approval": True,
                "tool": tool.name,
                "arguments": arguments,
                "risk_level": risk,
                "approval_state": "待审批",
                "approval_key": approval_key(tool.name, arguments),
                "permission_rule": decision.rule if decision is not None else "",
                "command_prefix": raw_prefix,
                "suggested_prefix": memo,
                "approval_options": approval_options(
                    tool.name, memo, tool_scope=not tool_grant_too_wide(tool.name, arguments)
                ),
            },
        )

    async def _run(
        self, tool: ToolDefinition, arguments: dict, context: ToolContext
    ) -> ToolOutcome:
        try:
            return await asyncio.wait_for(
                tool.handler(context, dict(arguments)), timeout=tool.timeout_seconds + 5
            )
        except asyncio.TimeoutError:
            return ToolOutcome(
                ok=False,
                status="timeout",
                message=f"工具执行超时：{tool.name}（{tool.timeout_seconds:.0f}s）",
            )
        except ApiError as exc:
            # 只有 403 才是平台主动拦截（越权、绝对路径）；其余 ApiError 属于普通失败，不能标成"已拒绝"。
            status = "denied" if exc.status_code == 403 else "error"
            return ToolOutcome(ok=False, status=status, message=exc.message, data={"error_code": exc.code})
        except (ValueError, TypeError) as exc:
            return ToolOutcome(ok=False, status="error", message=f"工具参数错误：{exc}")
        except Exception as exc:
            return ToolOutcome(
                ok=False, status="error", message=f"工具执行失败：{exc.__class__.__name__}"
            )

    async def _record(
        self,
        name: str,
        arguments: dict,
        outcome: ToolOutcome,
        context: ToolContext,
        node: str,
        risk: str,
        duration_ms: int,
    ) -> tuple[str, str]:
        tool_call_id = str(uuid.uuid4())
        artifact_path = ""
        summary = _redact(
            "\n".join(part for part in (outcome.output, outcome.message) if part),
            context.extra_secrets,
        )
        full_payload = {
            "tool": name,
            "status": outcome.status,
            "output": _redact(outcome.output, context.extra_secrets, 10_000_000),
            "message": _redact(outcome.message, context.extra_secrets, 10_000_000),
            "data": _redact_value(outcome.data, context.extra_secrets, 10_000_000),
        }
        artifact_path = ArtifactStore().save(context.task_id, tool_call_id, full_payload)
        async with self._record_lock:
            async with context.session_factory() as session:
                session.add(
                    ToolCall(
                        id=tool_call_id,
                        task_id=context.task_id,
                        trace_id=context.trace_id,
                        node=node,
                        name=name,
                        arguments=_redact_value(arguments, context.extra_secrets, MAX_LOGGED_CHARS),
                        result_summary=summary or outcome.status,
                        artifact_path=artifact_path,
                        status=outcome.status,
                        risk_level=risk,
                        duration_ms=duration_ms,
                    )
                )
                await session.commit()
        return tool_call_id, artifact_path

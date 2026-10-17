"""Tool Gateway: validates, executes, records and redacts every tool call."""

from __future__ import annotations

import asyncio
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
    memoizable_prefix,
)
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, approval_key, validate_arguments

MAX_LOGGED_CHARS = 4000
MAX_RESULT_CHARS = 8000
APPROVAL_STATUS = "awaiting_approval"


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


class ToolGateway:
    def __init__(self, tools: Sequence[ToolDefinition]) -> None:
        self.tools: dict[str, ToolDefinition] = {tool.name: tool for tool in tools}

    def specs(self) -> list[ToolSpecLite]:
        return [tool.spec() for tool in self.tools.values()]

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
        return await self._finish(name, arguments, outcome, context, node, risk, started, decision)

    def decide(
        self,
        tool: ToolDefinition,
        arguments: dict,
        context: ToolContext,
        risk: str | None = None,
    ) -> Decision:
        """统一的权限判定入口：图里的审批门与网关必须拿到同一个答案。"""
        risk = tool.resolve_risk(arguments) if risk is None else risk
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
        # 只有值得记住的前缀才作为「总是允许 X」的建议下发；rm 这类裸可执行名不会出现在这里。
        suggested = memoizable_prefix(tool.name, raw_prefix)
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
                "suggested_prefix": suggested,
                "approval_options": approval_options(tool.name, raw_prefix),
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

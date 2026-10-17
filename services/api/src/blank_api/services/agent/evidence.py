"""交付证明包：验收条件、文件变更与 Diff、测试结论、用量与事件时间线。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from blank_api.api.errors import ApiError
from blank_api.models.model_provider import ModelProvider, ModelUsage
from blank_api.models.task import AgentEvent, FileChange, Task, ToolCall


async def load_task(session: AsyncSession, task_id: str) -> Task:
    task = await session.get(Task, task_id)
    if task is None:
        raise ApiError(code="task_not_found", message="任务不存在", status_code=404)
    return task


async def build_evidence(
    session_factory: async_sessionmaker[AsyncSession], task_id: str
) -> dict[str, Any]:
    async with session_factory() as session:
        task = await load_task(session, task_id)
        events = (
            (
                await session.execute(
                    select(AgentEvent).where(AgentEvent.task_id == task_id).order_by(AgentEvent.seq)
                )
            )
            .scalars()
            .all()
        )
        calls = (
            (
                await session.execute(
                    select(ToolCall).where(ToolCall.task_id == task_id).order_by(ToolCall.created_at)
                )
            )
            .scalars()
            .all()
        )
        changes = (
            (
                await session.execute(
                    select(FileChange).where(FileChange.task_id == task_id).order_by(FileChange.created_at)
                )
            )
            .scalars()
            .all()
        )
        usage_rows = (
            (
                await session.execute(
                    select(ModelUsage, ModelProvider.name)
                    .join(ModelProvider, ModelProvider.id == ModelUsage.provider_id)
                    .where(ModelUsage.task_id == task_id)
                )
            )
            .all()
        )

    acceptance = list(task.acceptance_results or [])
    criteria = list(task.acceptance_criteria or [])
    if criteria and not acceptance:
        acceptance = [
            {"criterion": item, "covered": False, "evidence": "任务未产出验收结论"} for item in criteria
        ]
    validation = _latest_validation(events)
    # 审批请求里的权限上下文（模式 / 命中规则 / 建议前缀 / 作用域选项）在中断事件里；
    # 前端要拿它渲染「仅本次 / 总是允许 git status / 总是允许 run_shell」。
    approval_context: dict[str, Any] = {}
    for event in reversed(events):
        if event.type in ("approval.required", "approval.requested") and isinstance(event.payload, dict):
            approval_context = dict(event.payload)
            break
    pending_approvals = [
        {
            "tool_call_id": call.id,
            "tool": call.name,
            "arguments": call.arguments,
            "risk_level": call.risk_level,
            "message": call.result_summary,
            "approval_state": "待审批",
            **_approval_hints(approval_context, call.name, call.arguments),
        }
        for call in calls
        if call.status == "awaiting_approval"
    ]
    if not pending_approvals and task.status == "waiting_approval":
        # 图在工具网关之前就被 interrupt() 打断时不会留下 ToolCall 行，退回最近一次审批事件。
        for event in reversed(events):
            if event.type not in ("approval.required", "approval.requested"):
                continue
            payload = event.payload or {}
            pending_approvals.append(
                {
                    "tool_call_id": str(payload.get("tool_call_id") or ""),
                    "tool": str(payload.get("tool") or ""),
                    "arguments": payload.get("arguments") or {},
                    "risk_level": str(payload.get("risk_level") or "high"),
                    "message": "高风险操作等待人工审批",
                    "approval_state": "待审批",
                    "expires_at": (payload.get("expires_at") if isinstance(payload, dict) else None),
                    **_approval_hints(payload, str(payload.get("tool") or ""), payload.get("arguments") or {}),
                }
            )
            break
    prompt_tokens = sum(row.prompt_tokens for row, _ in usage_rows)
    completion_tokens = sum(row.completion_tokens for row, _ in usage_rows)
    models = sorted({row.model for row, _ in usage_rows if row.model})
    providers = sorted({name for _, name in usage_rows})
    trace_ids = sorted({row.trace_id for row, _ in usage_rows if row.trace_id})
    unverified = [
        f"验收条件未验证：{item['criterion']}"
        for item in acceptance
        if not item.get("covered")
    ]
    if not validation:
        unverified.append("未执行测试验证")
    if pending_approvals:
        unverified.append(
            f"存在待审批的高风险操作：{', '.join(sorted({a['tool'] for a in pending_approvals}))}"
        )
    return {
        "task": {
            "id": task.id,
            "project_id": task.project_id,
            "title": task.title,
            "goal": task.goal,
            "status": task.status,
            "steps_used": task.steps_used,
            "max_steps": task.max_steps,
            "result_summary": task.result_summary,
            "failure_reason": task.failure_reason,
            "created_at": _iso(task.created_at),
            "updated_at": _iso(task.updated_at),
        },
        "acceptance": acceptance,
        "changes": {
            "count": len(changes),
            "files": [change.path for change in changes],
            "reversible": all(bool(change.backup_path) or change.change_type == "created" for change in changes),
            "diff": "\n".join(change.diff for change in changes if change.diff),
            "items": [
                {
                    "path": change.path,
                    "change_type": change.change_type,
                    "backup_path": change.backup_path,
                    "created_at": _iso(change.created_at),
                }
                for change in changes
            ],
        },
        "validation": validation,
        "usage": {
            "providers": providers,
            "models": models,
            "trace_ids": trace_ids,
            "calls": len(usage_rows),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            # 模型累计耗时（毫秒），前端「耗时」行读这个字段；不填就只会显示「暂无」。
            "latency_ms": sum(row.latency_ms for row, _ in usage_rows),
        },
        "tools": [
            {
                "id": call.id,
                "trace_id": call.trace_id,
                "node": call.node,
                "name": call.name,
                "arguments": call.arguments,
                "status": call.status,
                "risk_level": call.risk_level,
                "duration_ms": call.duration_ms,
                "summary": call.result_summary,
                "artifact_path": call.artifact_path,
                "created_at": _iso(call.created_at),
            }
            for call in calls
        ],
        "pending_approvals": pending_approvals,
        "unverified_risks": unverified,
        "timeline": [
            {
                "seq": event.seq,
                "node": event.node,
                "type": event.type,
                "payload": event.payload,
                "created_at": _iso(event.created_at),
            }
            for event in events
        ],
    }


def _approval_hints(context: dict, tool: str, arguments) -> dict[str, Any]:
    """把审批事件里的权限上下文摘到待审批项上；上下文不属于该工具时留空，不编造。"""
    if not context or str(context.get("tool") or "") != tool:
        return {}
    hints: dict[str, Any] = {
        "permission_mode": context.get("permission_mode") or "",
        "permission_rule": context.get("permission_rule") or "",
        "command_prefix": context.get("command_prefix") or "",
        "suggested_prefix": context.get("suggested_prefix") or "",
        "approval_options": context.get("approval_options") or [],
    }
    if not context.get("arguments") and arguments:
        # 事件里没带参数时，用调用记录补一份，前端才能渲染「总是允许 X」的实际命令。
        hints["arguments_preview"] = arguments
    return hints


def _latest_validation(events: list[AgentEvent]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.type == "validation.completed":
            return dict(event.payload or {})
    return None


def _iso(value) -> str:
    return value.isoformat(timespec="seconds") if value else ""

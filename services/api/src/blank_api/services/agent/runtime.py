"""Task runner: builds the tool gateway, the LangGraph workflow and the evidence pack."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.errors import GraphInterrupt
from langgraph.types import Command
from sqlalchemy import select

from blank_api.api.errors import ApiError
from blank_api.models.checkpoint import AgentCheckpoint
from blank_api.models.task import Task
from blank_api.services.agent.checkpoints import (
    SqlAlchemyCheckpointSaver,
    task_id_from_thread,
    thread_id_for,
)
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.evidence import build_evidence
from blank_api.services.agent.graph import AgentSession, build_agent_graph, initial_messages
from blank_api.services.agent.store import SerializedSessionFactory
from blank_api.services.models.base import ModelAdapter, ModelError
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolContext, approval_key
from blank_api.services.tools.gateway import ToolGateway
from blank_api.services.sandbox import SnapshotStore
from blank_api.services.workspace import mask_path


class _CancelledLookup:
    def __init__(self, session_factory, task_id: str) -> None:
        self.session_factory = session_factory
        self.task_id = task_id

    async def __call__(self) -> bool:
        async with self.session_factory() as session:
            status = (
                await session.execute(select(Task.status).where(Task.id == self.task_id))
            ).scalar_one_or_none()
        return status == "cancelled"


def _attempt(thread_id: str) -> int:
    try:
        return int(str(thread_id).rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return 0


async def threads_for_task(session_factory, task_id: str) -> list[str]:
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(AgentCheckpoint.thread_id).where(AgentCheckpoint.task_id == task_id)
                )
            )
            .scalars()
            .all()
        )
    return sorted({thread for thread in rows if task_id_from_thread(thread) == task_id})


async def next_thread_id(session_factory, task_id: str) -> str:
    attempts = [_attempt(thread) for thread in await threads_for_task(session_factory, task_id)]
    return thread_id_for(task_id, max(attempts or {0}) + 1)


async def latest_thread_id(session_factory, task_id: str) -> str:
    threads = await threads_for_task(session_factory, task_id)
    if not threads:
        raise ApiError(code="task_not_runnable", message="任务尚未运行过，没有可恢复的检查点", status_code=409)
    return max(threads, key=_attempt)


@dataclass
class RunRequest:
    task_id: str
    provider_id: str
    provider_name: str
    adapter: ModelAdapter
    root: str | Path
    title: str = ""
    goal: str = ""
    acceptance_criteria: Sequence[str] = ()
    max_steps: int = 8
    approve_high_risk: bool = False
    approved_tools: set[str] | None = None
    approved_actions: set[str] | None = None
    skill_instructions: Sequence[str] = ()
    role_adapters: dict[str, ModelAdapter] | None = None
    role_provider_ids: dict[str, str] | None = None


@dataclass
class _RunScope:
    task_id: str
    store: SerializedSessionFactory
    recorder: EventRecorder
    session: AgentSession
    graph: Any
    config: dict
    thread_id: str


class AgentRuntime:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def _prepare(self, request: RunRequest, thread_id: str) -> _RunScope:
        store = SerializedSessionFactory(self.session_factory)
        recorder = EventRecorder(store, request.task_id, trace_id=thread_id)
        gateway = ToolGateway(build_default_tools())
        allowed = set(request.approved_tools or set())
        approved_actions = set(request.approved_actions or set())
        if request.approve_high_risk:
            allowed.update(tool.name for tool in gateway.tools.values() if tool.requires_approval)
        adapters = [request.adapter, *(request.role_adapters or {}).values()]
        context = ToolContext(
            root=Path(request.root),
            task_id=request.task_id,
            session_factory=store,
            approval_policy=lambda tool, arguments: (
                approval_key(tool.name, arguments) in approved_actions
                or (tool.resolve_risk(arguments) not in ("high", "critical") and tool.name in allowed)
                or (request.approve_high_risk and tool.resolve_risk(arguments) in ("high", "critical"))
            ),
            extra_secrets=tuple({s for adapter in adapters for s in (adapter.config.api_key,) if s}),
            approved_actions=frozenset(approved_actions),
            trace_id=thread_id,
        )
        session = AgentSession(
            adapter=request.adapter,
            gateway=gateway,
            context=context,
            recorder=recorder,
            session_factory=store,
            provider_id=request.provider_id,
            provider_name=request.provider_name,
            trace_id=thread_id,
            goal=request.goal or request.title,
            skill_instructions=[str(item) for item in request.skill_instructions],
            acceptance_criteria=[str(item) for item in request.acceptance_criteria or ()],
            max_steps=max(1, int(request.max_steps)),
            approved_tools=allowed,
            approved_actions=approved_actions,
            role_adapters=request.role_adapters or {},
            role_provider_ids=request.role_provider_ids or {},
            is_cancelled=_CancelledLookup(store, request.task_id),
        )
        graph = build_agent_graph(session, SqlAlchemyCheckpointSaver(store))
        config = {
            "configurable": {"thread_id": thread_id, "checkpoint_ns": ""},
            "recursion_limit": session.max_steps * 4 + 12,
        }
        return _RunScope(
            task_id=request.task_id,
            store=store,
            recorder=recorder,
            session=session,
            graph=graph,
            config=config,
            thread_id=thread_id,
        )

    async def run(self, request: RunRequest) -> dict[str, Any]:
        thread_id = await next_thread_id(self.session_factory, request.task_id)
        scope = self._prepare(request, thread_id)
        recorder, session = scope.recorder, scope.session
        snapshot = SnapshotStore().capture(
            str(session.context.root), reason="任务执行前快照", task_id=request.task_id
        )
        await recorder.add(
            "snapshot.created",
            "receive_task",
            {"snapshot_id": snapshot["id"], "files": snapshot["files"]},
        )
        if await session.is_cancelled():
            await recorder.add("task.cancelled", "receive_task", {"reason": "任务已取消，未执行任何工具"})
            await self._finish(scope, "cancelled", summary="", reason="任务已取消", steps=0)
            return await build_evidence(scope.store, request.task_id)
        await self._set_status(scope, "running")
        await recorder.add(
            "task.started",
            "receive_task",
            {
                "title": request.title,
                "goal": session.goal,
                "acceptance_criteria": session.acceptance_criteria,
                "workspace": mask_path(session.context.root),
                "provider": request.provider_name,
                "model": request.adapter.config.model,
                "max_steps": session.max_steps,
                "tools": [tool.name for tool in session.gateway.specs()],
                "thread_id": thread_id,
                "snapshot_id": snapshot["id"],
            },
        )
        state: dict[str, Any] = {
            "task_id": request.task_id,
            "messages": initial_messages(
                session.goal, session.acceptance_criteria, session.skill_instructions
            ),
            "plan": [],
            "steps": 0,
            "repair_rounds": 0,
            "tool_results": [],
            "pending": [],
            "acceptance_results": [],
            "usage": {},
            "validation": {},
            "summary": "",
            "status": "running",
        }
        try:
            result = await scope.graph.ainvoke(state, scope.config)
        except ModelError as exc:
            await self._finish(
                scope,
                "failed",
                summary="",
                reason=exc.message,
                extra={"code": exc.code, "retryable": exc.retryable, "thread_id": thread_id},
            )
            raise ApiError(code=exc.code, message=exc.message, status_code=502) from None
        except GraphInterrupt as exc:
            result, interrupts = {}, list(exc.args or [])
        else:
            interrupts = list(result.get("__interrupt__") or [])
        return await self._finalize(request, scope, result, interrupts)

    async def resume(self, request: RunRequest, decision: bool = True) -> dict[str, Any]:
        thread_id = await latest_thread_id(self.session_factory, request.task_id)
        scope = self._prepare(request, thread_id)
        await self._set_status(scope, "running")
        await scope.recorder.add(
            "task.resumed",
            "receive_task",
            {"thread_id": thread_id, "decision": decision, "approved_tools": sorted(request.approved_tools or set())},
        )
        try:
            result = await scope.graph.ainvoke(Command(resume=decision), scope.config)
        except ModelError as exc:
            await self._finish(
                scope,
                "failed",
                summary="",
                reason=exc.message,
                extra={"code": exc.code, "thread_id": thread_id},
            )
            raise ApiError(code=exc.code, message=exc.message, status_code=502) from None
        except GraphInterrupt as exc:
            result, interrupts = {}, list(exc.args or [])
        else:
            interrupts = list(result.get("__interrupt__") or [])
        return await self._finalize(request, scope, result, interrupts)

    async def _finalize(
        self,
        request: RunRequest,
        scope: _RunScope,
        result: dict,
        interrupts: list,
    ) -> dict[str, Any]:
        task_id = request.task_id
        if interrupts:
            payload = getattr(interrupts[0], "value", interrupts[0]) or {}
            await scope.recorder.add("approval.required", "execute_action", dict(payload))
            await self._finish(
                scope,
                "waiting_approval",
                summary=str(result.get("summary") or ""),
                reason="存在待审批的高风险操作",
                acceptance=result.get("acceptance_results"),
                steps=int(result.get("steps") or 0),
                extra={"thread_id": scope.thread_id, "interrupt": dict(payload)},
            )
            return await build_evidence(scope.store, task_id)

        status = resolve_status(result)
        validation = result.get("validation") or {}
        reason = ""
        if status == "failed":
            detail = "测试未能执行" if validation.get("status") == "error" else "测试未通过"
            reason = f"{detail}：{validation.get('command') or 'run_tests'}"
        elif status == "cancelled":
            reason = "任务已取消"
        elif status == "paused":
            reason = "已驳回高风险操作，该步骤未执行，任务已暂停"
        await self._finish(
            scope,
            status,
            summary=str(result.get("summary") or ""),
            reason=reason,
            acceptance=result.get("acceptance_results"),
            steps=int(result.get("steps") or 0),
            extra={
                "usage": result.get("usage") or {},
                "validation": validation,
                "thread_id": scope.thread_id,
            },
        )
        return await build_evidence(scope.store, task_id)

    async def _set_status(self, scope: _RunScope, status: str) -> None:
        async with scope.store() as session:
            task = await session.get(Task, scope.task_id)
            if task is not None:
                task.status = status
                await session.commit()

    async def _finish(
        self,
        scope: _RunScope,
        status: str,
        *,
        summary: str,
        reason: str,
        acceptance: list[dict] | None = None,
        steps: int | None = None,
        extra: dict | None = None,
    ) -> None:
        async with scope.store() as session:
            task = await session.get(Task, scope.task_id)
            if task is None:
                return
            task.status = status
            if summary:
                task.result_summary = summary
            task.failure_reason = reason if status != "succeeded" else ""
            if acceptance is not None:
                task.acceptance_results = acceptance
            if steps is not None:
                task.steps_used = steps
            await session.commit()
        event_type = {
            "succeeded": "task.completed",
            "failed": "task.failed",
            "cancelled": "task.cancelled",
            "paused": "task.paused",
            "waiting_approval": "task.waiting_approval",
        }.get(status, "task.failed")
        await scope.recorder.add(
            event_type,
            "complete",
            {"status": status, "reason": reason, "summary": summary, **(extra or {})},
        )


def resolve_status(result: dict) -> str:
    if result.get("status") in ("cancelled", "paused", "failed"):
        return str(result["status"])
    validation = result.get("validation") or {}
    if validation and validation.get("status") != "ok":
        return "failed"
    if validation.get("exit_code") not in (None, 0):
        return "failed"
    return "succeeded"

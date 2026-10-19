"""Task runner: builds the tool gateway, the LangGraph workflow and the evidence pack."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.errors import GraphInterrupt
from langgraph.types import Command
from sqlalchemy import select

from blank_api.api.errors import ApiError
from blank_api.models.checkpoint import AgentCheckpoint
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.checkpoints import (
    SqlAlchemyCheckpointSaver,
    task_id_from_thread,
)
from blank_api.services.agent.context import ContextCompactor
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.evidence import build_evidence
from blank_api.services.agent.graph import AgentSession, build_agent_graph, initial_messages
from blank_api.services.agent.router import Candidate, Router
from blank_api.services.agent.store import SerializedSessionFactory
from blank_api.services.features import is_enabled
from blank_api.services.fork import allocate_thread
from blank_api.services.guardian import Guardian
from blank_api.services.hooks import HookRunner
from blank_api.services.instructions import load_layers, provenance
from blank_api.services.mcp import manager as mcp_manager
from blank_api.services.memory import MemoryStore
from blank_api.services.notes import NoteStore
from blank_api.services.safety import SafetyPolicy
from blank_api.services.settings import SettingsStore
from blank_api.services.shell_env import capture as capture_shell
from blank_api.services.subagent import SubAgentRunner
from blank_api.services.models.base import ModelAdapter, ModelError
from blank_api.services.permissions import MODE_DEFAULT, PermissionPolicy
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolContext
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
    # 分叉出来的空树干没有检查点，只靠检查点算 attempt 会把同一个号发两次
    async with session_factory() as session:
        return await allocate_thread(session, task_id)


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
    run_id: str = ""
    title: str = ""
    goal: str = ""
    acceptance_criteria: Sequence[str] = ()
    max_steps: int = 8
    approve_high_risk: bool = False
    approved_tools: set[str] | None = None
    approved_actions: set[str] | None = None
    approved_command_prefixes: Sequence[str] = ()
    # 空串表示"沿用该任务已有的模式"：审批恢复时前端不重发模式，不能被默认值悄悄改口径。
    permission_mode: str = ""
    skill_instructions: Sequence[str] = ()
    role_adapters: dict[str, ModelAdapter] | None = None
    role_provider_ids: dict[str, str] | None = None
    # 按需选模型的候选池：只能从「本次任务已经选定的供应商」里取，不含用户没选过的。
    routing_pool: Sequence[Candidate] | None = None
    routing_adapters: dict[str, ModelAdapter] | None = None


@dataclass
class _RunScope:
    task_id: str
    store: SerializedSessionFactory
    recorder: EventRecorder
    session: AgentSession
    graph: Any
    config: dict
    thread_id: str
    # 外部工具（MCP）这轮的连接结果：写进 task.started，让"我的服务端怎么没生效"
    # 有一个可以直接看的地方，而不是只剩"工具列表里没有"。
    mcp: dict = field(default_factory=dict)


class AgentRuntime:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    async def _rehydrate(self, task_id: str, permissions: PermissionPolicy) -> None:
        """从事件日志回放本任务的批准与驳回。

        会话记忆本来只活在进程内存里，而审批恢复、后端重启都会重建 AgentSession；
        不回放的话，用户"本会话总是允许 git status"的授权每次恢复都会丢。
        """
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(AgentEvent)
                        .where(
                            AgentEvent.task_id == task_id,
                            AgentEvent.type.in_(("approval.granted", "approval.denied")),
                        )
                        .order_by(AgentEvent.seq)
                    )
                )
                .scalars()
                .all()
            )
        for row in rows:
            payload = row.payload or {}
            tool = str(payload.get("tool") or "")
            if not tool:
                continue
            arguments = payload.get("arguments") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            scope = str(payload.get("scope") or "once")
            if row.type == "approval.granted":
                permissions.remember(tool, arguments, scope)
            else:
                permissions.forget(tool, arguments, scope)

    async def _stored_mode(self, task_id: str) -> str:
        """任务首次运行时记下的权限模式，供审批恢复沿用。"""
        async with self.session_factory() as session:
            payload = (
                await session.execute(
                    select(AgentEvent.payload)
                    .where(AgentEvent.task_id == task_id, AgentEvent.type == "task.started")
                    .order_by(AgentEvent.seq.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        return str((payload or {}).get("permission_mode") or "")

    async def _prepare(self, request: RunRequest, thread_id: str) -> _RunScope:
        store = SerializedSessionFactory(self.session_factory)
        recorder = EventRecorder(store, request.task_id, trace_id=thread_id, run_id=request.run_id)
        settings = SettingsStore().read()
        # 设置页可以关掉按需加载：关掉后所有工具每轮都下发，tool_search 也就没有意义了。
        # 走特性注册表，老字段 tools.search 仍然作为回落值生效。
        tool_search_enabled = is_enabled("tool_search", settings)
        tools = build_default_tools()
        if not tool_search_enabled:
            tools = [tool for tool in tools if tool.name != "tool_search"]
        # 外部工具（MCP）：走同一个网关、同一套审批、同一份审计。连接失败只影响
        # 那个服务端自己——半份工具表也远好过起不来任务。
        mcp_bundle = await mcp_manager().bundle()
        tools = [*tools, *mcp_bundle.tools]
        gateway = ToolGateway(tools, defer=tool_search_enabled)
        allowed = set(request.approved_tools or set())
        approved_actions = set(request.approved_actions or set())
        if request.approve_high_risk:
            allowed.update(tool.name for tool in gateway.tools.values() if tool.requires_approval)
        # 请求没带模式时用设置页的默认模式：桌面端用户改的就是那一处，不该有两套默认值
        mode = request.permission_mode or (settings.get("permissions") or {}).get("default_mode") or MODE_DEFAULT
        permissions = PermissionPolicy.build(
            mode,
            approve_high_risk=request.approve_high_risk,
            approved_tools=allowed,
            approved_actions=approved_actions,
            approved_prefixes=request.approved_command_prefixes or (),
        )
        adapters = [request.adapter, *(request.role_adapters or {}).values()]
        # 按需选模型：候选不足两个就没有"选"可言，直接不建路由，
        # 关掉开关时行为与单模型完全一致（router=None 时 call_model 走原路径）。
        routing_pool = tuple(request.routing_pool or ())
        router = Router(pool=routing_pool) if routing_pool and is_enabled("model_routing", settings) else None
        if router is not None and not router.active:
            router = None
        guardian = Guardian.from_settings(
            (request.role_adapters or {}).get("guardian") or request.adapter, settings=settings
        )
        compaction = settings.get("compaction") or {}
        compactor = ContextCompactor(
            context_window=int(getattr(request.adapter.config, "context_window", 0) or 0),
            trigger_ratio=float(compaction.get("trigger_ratio") or 0.82),
            reserve_tokens=int(compaction.get("reserve_tokens") or 16_384),
        )
        subagents = SubAgentRunner()
        notes = NoteStore()
        memory = MemoryStore()
        shell = capture_shell(request.root)
        # 能力边界跟着设置页走：没开就是没开，请求里带什么都不放宽。
        # 放在这里读一次，同一任务的网关、工具处理、子代理拿到的是同一份策略。
        safety = SafetyPolicy.load()
        # 用户级 → 项目 AGENTS.md（+ 可选外部指令文件）；任务级偏好走 skill_instructions
        layers = load_layers(request.root)
        context = ToolContext(
            root=Path(request.root),
            task_id=request.task_id,
            session_factory=store,
            # 审批回调保留给外部集成；运行时的唯一真源是 permissions（模式 + 前缀 + 会话记忆）。
            approval_policy=permissions.decide,
            extra_secrets=tuple({s for adapter in adapters for s in (adapter.config.api_key,) if s}),
            approved_actions=frozenset(approved_actions),
            trace_id=thread_id,
            permissions=permissions,
            safety=safety,
            subagent_runner=subagents,
            notes=notes,
            memory=memory,
            shell=shell,
            tool_registry=gateway.registry,
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
            permissions=permissions,
            role_adapters=request.role_adapters or {},
            role_provider_ids=request.role_provider_ids or {},
            router=router,
            routing_adapters=request.routing_adapters or {},
            # 工具裁剪只在真按档位路由时才有意义：没有 router 就没有 tier，
            # 硬开着它只会让每一步都拿去和 full 比一次，然后什么也没变。
            tiered_tools=bool(router is not None and is_enabled("tiered_tools", settings)),
            stream_output=is_enabled("stream_output", settings),
            is_cancelled=_CancelledLookup(store, request.task_id),
            hooks=HookRunner.from_env(),
            guardian=guardian,
            compactor=compactor,
            notes=notes,
            memory=memory,
            shell=shell,
            instruction_layers=layers,
        )
        subagents.bind(session)
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
            mcp=mcp_bundle.as_dict(),
        )

    async def _fire_hooks(self, scope: _RunScope, event: str, payload: dict) -> None:
        """钩子是可选扩展（BLANK_HOOKS 配置）：失败绝不影响任务，但每次都要留审计记录。"""
        results = await scope.session.hooks.run(event, "", payload)
        if results:
            await scope.recorder.add(
                "hook.completed",
                "receive_task",
                {"event": event, "results": [item.as_dict() for item in results]},
            )

    async def run(self, request: RunRequest) -> dict[str, Any]:
        thread_id = await next_thread_id(self.session_factory, request.task_id)
        scope = await self._prepare(request, thread_id)
        await self._rehydrate(request.task_id, scope.session.permissions)
        recorder, session = scope.recorder, scope.session
        # 快照不在这里拍。它的意义是「动手之前留一份原样」，所以时机应该在
        # 意图判定之后、第一个动作之前——纯问答任务从来不写工作区，开场就复制
        # 一遍整个仓库纯属浪费（大仓库要几十秒，界面看起来像卡死）。
        # 具体见 AgentSession.ensure_snapshot() 与 create_plan 节点里的调用点。
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
                "permission_mode": session.permission_mode,
                "permissions": session.permissions.snapshot(),
                "tools": session.gateway.registry.visible_names(),
                "tools_deferred": session.gateway.registry.deferred_names(),
                "mcp": scope.mcp,
                "compaction": {
                    "context_window": session.compactor.context_window,
                    "trigger_ratio": session.compactor.trigger_ratio,
                    "reserve_tokens": session.compactor.reserve_tokens,
                    "trigger_at": session.compactor.trigger_tokens(),
                },
                "guardian_mode": session.guardian.mode,
                "thread_id": thread_id,
                # 快照改为「动手前才拍」，这里必然还是空的；
                # 真正拍完会另发 snapshot.created，界面按那条事件定位即可。
                "snapshot_id": session.snapshot_id,
            },
        )
        # 会话生命周期钩子：对标 Codex 的 session_start/user_prompt_submit
        await self._fire_hooks(
            scope,
            "session_start",
            {"task_id": request.task_id, "thread_id": thread_id, "workspace": mask_path(session.context.root)},
        )
        await self._fire_hooks(
            scope,
            "user_prompt_submit",
            {"goal": session.goal, "acceptance_criteria": session.acceptance_criteria},
        )
        # shell 快照与指令来源：出问题时能回答"当时是什么环境、读了哪些约定文件"
        if session.shell is not None:
            await recorder.add("shell.snapshot", "receive_task", session.shell.as_dict())
        if session.instruction_layers:
            await recorder.add(
                "instructions.loaded",
                "receive_task",
                {"layers": provenance(session.instruction_layers)},
            )

        state: dict[str, Any] = {
            "task_id": request.task_id,
            "messages": initial_messages(
                session.goal,
                session.acceptance_criteria,
                session.skill_instructions,
                session.instruction_layers,
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
        # 会话收尾钩子：run 与 resume 两条路径都要发
        await self._fire_hooks(
            scope,
            "session_end",
            {"task_id": request.task_id, "thread_id": scope.thread_id, "interrupts": len(interrupts)},
        )
        return await self._finalize(request, scope, result, interrupts)

    async def resume(self, request: RunRequest, decision: Any = True) -> dict[str, Any]:
        thread_id = await latest_thread_id(self.session_factory, request.task_id)
        if not request.permission_mode:
            request.permission_mode = await self._stored_mode(request.task_id) or MODE_DEFAULT
        scope = await self._prepare(request, thread_id)
        await self._rehydrate(request.task_id, scope.session.permissions)
        await self._set_status(scope, "running")
        await scope.recorder.add(
            "task.resumed",
            "receive_task",
            {
                "thread_id": thread_id,
                "decision": decision if isinstance(decision, (bool, str)) else dict(decision),
                "permission_mode": scope.session.permission_mode,
                "permissions": scope.session.permissions.snapshot(),
            },
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
        # 会话收尾钩子：run 与 resume 两条路径都要发
        await self._fire_hooks(
            scope,
            "session_end",
            {"task_id": request.task_id, "thread_id": scope.thread_id, "interrupts": len(interrupts)},
        )
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
            payload = dict(payload) if isinstance(payload, dict) else {}
            waiting_for_input = str(payload.get("kind") or "") == "question"
            await scope.recorder.add(
                "question.required" if waiting_for_input else "approval.required",
                "execute_action",
                payload,
            )
            await self._finish(
                scope,
                "waiting_approval",
                summary=str(result.get("summary") or ""),
                reason="等待用户回答问题" if waiting_for_input else "存在待审批的高风险操作",
                acceptance=result.get("acceptance_results"),
                steps=int(result.get("steps") or 0),
                extra={"thread_id": scope.thread_id, "interrupt": payload},
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
                "context_tokens": scope.session.context_tokens,
                "steer": scope.session.steer_metrics,
            },
        )
        if status == "succeeded":
            await self._consolidate(scope, result, title=request.title)
        return await build_evidence(scope.store, task_id)

    async def _consolidate(self, scope: _RunScope, result: dict, *, title: str) -> None:
        """任务成功后把结论与笔记蒸馏进项目长期记忆（确定性规则，不调模型）。"""
        session = scope.session
        if session.memory is None:
            return
        facts: list[str] = []
        for item in result.get("acceptance_results") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("criterion") or item.get("name") or item.get("text") or "").strip()
            covered = item.get("covered")
            if covered is True:
                state = "覆盖"
            elif covered is False:
                state = "未覆盖"
            else:
                state = str(item.get("status") or "未知")
            if text:
                facts.append(f"验收「{text}」：{state}")
        validation = result.get("validation") or {}
        if validation.get("command"):
            facts.append(
                f"验证命令 {validation.get('command')} → {validation.get('status') or '未知'}"
            )
        notes = ""
        if session.notes is not None:
            try:
                notes = session.notes.read(scope.task_id).content
            except Exception:
                notes = ""
        try:
            outcome = session.memory.consolidate(
                session.context.root,
                task_id=scope.task_id,
                title=title or session.goal,
                summary=str(result.get("summary") or ""),
                notes=notes,
                facts=facts,
            )
        except Exception as exc:  # 固化失败不该让已经成功的任务变成失败
            await scope.recorder.add(
                "memory.consolidation_failed", "complete", {"error": str(exc)[:300]}
            )
            return
        await scope.recorder.add("memory.consolidated", "complete", outcome.as_dict())

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
        # 路由记账跟着收尾一起落：没有这个数，就没人知道这个 beta 到底省没省钱，
        # 也就永远不敢把它转正。
        payload = {"status": status, "reason": reason, "summary": summary, **(extra or {})}
        routing = _routing_summary(scope.session)
        if routing:
            payload["routing"] = routing
        await scope.recorder.add(event_type, "complete", payload)


def _routing_summary(session: AgentSession) -> dict:
    router = getattr(session, "router", None)
    if router is None:
        return {}
    try:
        return router.summary()
    except Exception:  # 记账失败绝不能影响任务收尾
        return {}


def resolve_status(result: dict) -> str:
    if result.get("status") in ("cancelled", "paused", "failed"):
        return str(result["status"])
    validation = result.get("validation") or {}
    # skipped = 本次没改文件，校验本就不适用（纯问答），不算失败
    if validation and validation.get("status") not in ("ok", "skipped"):
        return "failed"
    if validation.get("exit_code") not in (None, 0):
        return "failed"
    return "succeeded"

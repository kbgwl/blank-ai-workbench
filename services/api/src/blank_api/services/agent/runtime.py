"""Task runner: builds the tool gateway, the LangGraph workflow and the evidence pack."""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.errors import GraphInterrupt
from langgraph.types import Command
from sqlalchemy import select

from blank_api.api.errors import ApiError
from blank_api.limits import DEFAULT_TOKEN_BUDGET
from blank_api.models.checkpoint import AgentCheckpoint
from blank_api.models.task import AgentEvent, Task
from blank_api.models.thread import Thread
from blank_api.services.agent.checkpoints import (
    SqlAlchemyCheckpointSaver,
    task_id_from_thread,
)
from blank_api.services.agent.context import ContextCompactor
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.evidence import build_evidence
from blank_api.services.agent.graph import AgentSession, build_agent_graph, initial_messages
from blank_api.services.agent.loadstats import recent_load
from blank_api.services.agent.router import Candidate, Router
from blank_api.services.agent.store import SerializedSessionFactory
from blank_api.services.approval_rules import load_prefix_rules
from blank_api.services.approvals import approval_key
from blank_api.services.permission_rules import load_ruleset
from blank_api.services.chat_index import index_task_summary
from blank_api.services.features import is_enabled
from blank_api.services.fork import allocate_thread
from blank_api.services.guardian import Guardian
from blank_api.services.hooks import HookRunner
from blank_api.services.instructions import load_layers, provenance
from blank_api.services.context_curator import curate
from blank_api.services.mcp import manager as mcp_manager
from blank_api.services.memory import MemoryStore
from blank_api.services.notes import NoteStore
from blank_api.services.safety import SafetyPolicy
from blank_api.services.settings import SettingsStore
from blank_api.services.shell_env import capture as capture_shell
from blank_api.services.subagent import SubAgentRunner
from blank_api.services.threads import history_for, summarize_history
from blank_api.services.models.base import ModelAdapter, ModelError
from blank_api.services.permissions import MODE_DEFAULT, PermissionPolicy
from blank_api.services.tools import TEAM_ADMIN_TOOL_NAMES, build_default_tools
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway
from blank_api.services.sandbox import SnapshotStore
from blank_api.services.sandbox.tiers import (
    TierDowngrade,
    UnknownTier,
    assert_not_looser,
    normalize_tier,
    resolve_tier,
)
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


def _token_budget() -> int:
    """单任务 token 预算。用环境变量覆盖是为了不为一件事加一次数据库迁移：
    它是个安全网阈值，进程级调一下就够了，不需要按任务持久化。"""
    raw = (os.environ.get("BLANK_TOKEN_BUDGET") or "").strip()
    if not raw:
        return DEFAULT_TOKEN_BUDGET
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_TOKEN_BUDGET
    return value if value > 0 else DEFAULT_TOKEN_BUDGET


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
    # 发起轮：引起这一整棵任务树的那一轮。空=这次运行自己就是发起轮（记录器里退回 run_id）。
    # 只有跨任务的链上它才和 run_id 不同：一轮群聊里每个发言成员各有自己的 run_id。
    origin_run_id: str = ""
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
    # 空串表示"用设置页的默认档"；审批恢复时同样要读回任务首次运行记下的档位，
    # 否则一次恢复就能把 workspace 静默换成 off（或反之）。
    sandbox_tier: str = ""
    skill_instructions: Sequence[str] = ()
    # 团队群聊的强规则（Bot 身份、@ 协议、本轮记录）。空表示行为与原来完全一致。
    system_instructions: Sequence[str] = ()
    role_adapters: dict[str, ModelAdapter] | None = None
    role_provider_ids: dict[str, str] | None = None
    # 按需选模型的候选池：只能从「本次任务已经选定的供应商」里取，不含用户没选过的。
    routing_pool: Sequence[Candidate] | None = None
    routing_adapters: dict[str, ModelAdapter] | None = None
    # 所属会话（多轮对话）。空串表示这是独立任务，行为与没有会话时完全一致。
    thread_ref: str = ""
    # 用户贴进来的图片，形如 [{"name", "media_type", "data"(base64)}]。
    # 放在这里而不是读磁盘：路由那层已经把它们读成字节了，运行时不该再碰文件系统。
    images: Sequence[dict] = ()
    # 文本类附件（含 PDF 抽出的正文），形如 [{"name", "text", "truncated"}]。
    # 它们不是多模态内容，进提示词就够了，不需要任何供应商支持。
    texts: Sequence[dict] = ()
    # 当前模型看不看得懂图。不支持时图片降级成一段文字说明，而不是把 image block
    # 硬塞过去——那会直接 400，用户看到的是"上传了图片就发不出去"。
    supports_vision: bool = False
    # 团队群聊里的身份边界：group_id / bot_id / is_admin / project_id。
    # 空表示这不是团队任务，团队管理类工具一律不可见。
    team_scope: dict = field(default_factory=dict)
    # 长期记忆里这条结论的来源标签（例如「团队·研发群·后端」）。
    memory_source: str = ""
    # 团队 Bot 的每条任务都会另存一条群消息；关掉任务级索引，避免搜索里出现两份。
    index_summary: bool = True


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
    # 档位解析结果（requested/tier/strength/notice）。task.started 与 turn.context
    # 都从这里取，避免两处各自解析出不同的档位。
    sandbox: dict = field(default_factory=dict)
    # 本轮的子代理运行器：收尾时要把仍在跑的后台子代理收走（见 _finish）。
    subagents: Any = None


@dataclass
class _PromptDecision:
    """提交钩子对"这一条用户正文"的处置结果（#123）。

    `goal_for_model` 是唯一去处：钩子没说话时它就是原文；说"补一段"时是原文加那一段；
    说"原文别进上下文"时只剩那一段。`blocked` 为真时什么都不送，任务直接进终态。
    原文与账是两件事——`tasks.goal` 永远是用户写的那份，这里改的只是送进模型的那一份。
    """

    blocked: bool = False
    reason: str = ""
    goal_for_model: str = ""
    # 原文真的被抑制时才为真：管家检索的关键词通道随之关闭（见 run 里的 curate）。
    suppressed: bool = False


def _prompt_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12] if text else ""


class AgentRuntime:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    async def _rehydrate(self, task_id: str, permissions: PermissionPolicy) -> None:
        """从事件日志回放本任务的批准、驳回与已消费的一次性授权。

        会话记忆本来只活在进程内存里，而审批恢复、后端重启都会重建 AgentSession；
        不回放的话，用户"本会话总是允许 git status"的授权每次恢复都会丢。

        `approval.consumed` 必须和授权一起按 seq 回放：单次越界批准在执行前就销账，
        只回放 `approval.granted` 会把同一枚 key 重新授权一次，用户的驳回也被顶掉。
        三类事件共用一个序，后发生的事实覆盖先发生的。
        """
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(AgentEvent)
                        .where(
                            AgentEvent.task_id == task_id,
                            AgentEvent.type.in_(
                                ("approval.granted", "approval.denied", "approval.consumed")
                            ),
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
            if row.type == "approval.consumed":
                # 记录里带了 key 就直接用：arguments 落库前经过打码，含密钥的命令
                # 重新算出来的指纹与批准时那枚并不相同。
                key = str(payload.get("approval_key") or "")
                permissions.approved_actions.discard(key or approval_key(tool, arguments))
            elif row.type == "approval.granted":
                permissions.remember(tool, arguments, scope)
            else:
                permissions.forget(tool, arguments, scope)

    async def _stored_mode(self, task_id: str) -> str:
        """任务当前生效的权限模式：新开一轮、审批恢复都沿用。

        取最近一条 `permission.mode_changed` 或 `task.started`。模型主动拨过的模式
        必须活过审批中断与后续轮次——否则"进入计划模式"只在这一轮有效，用户下一条
        消息就悄悄变回默认，而界面上的治理快照还写着 plan。
        """
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(AgentEvent.type, AgentEvent.payload)
                    .where(
                        AgentEvent.task_id == task_id,
                        AgentEvent.type.in_(("task.started", "permission.mode_changed")),
                    )
                    .order_by(AgentEvent.seq.desc())
                    .limit(1)
                )
            ).first()
        if row is None:
            return ""
        kind, payload = row
        data = payload if isinstance(payload, dict) else {}
        if kind == "permission.mode_changed":
            return str(data.get("to") or "")
        return str(data.get("permission_mode") or "")

    async def _stored_tier(self, task_id: str) -> str:
        """任务首次运行请求的档位，供审批恢复沿用。

        读 `requested` 而不是 `tier`：上次可能因为部署开关关闭被落成 off，
        恢复时开关若已打开，仍然应该按用户当初要的那一档重新解析。
        """
        async with self.session_factory() as session:
            payload = (
                await session.execute(
                    select(AgentEvent.payload)
                    .where(AgentEvent.task_id == task_id, AgentEvent.type == "task.started")
                    .order_by(AgentEvent.seq.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        sandbox = (payload or {}).get("sandbox")
        if not isinstance(sandbox, dict):
            return ""
        return str(sandbox.get("requested") or "")

    async def _guard_tier_change(self, request: RunRequest) -> None:
        """同一个任务不许换到更宽的档位继续跑。

        续跑/重跑可以显式给档位，但只能收紧。把 `workspace` 的任务改用 `off` 续跑，
        等于在用户点过的那道边界之外接着花他的钱；子代理同理（见 `subagent.run_subagent`）。
        老任务没有 `task.started.sandbox` 时 `stored` 为空，不冒充知道。
        """
        stored = await self._stored_tier(request.task_id)
        try:
            assert_not_looser(stored, request.sandbox_tier)
        except TierDowngrade as exc:
            raise ApiError(
                code="sandbox_tier_downgrade",
                message=f"{exc}；请沿用任务首次运行时的档位，或新建任务",
                status_code=409,
            ) from None

    async def _prepare(self, request: RunRequest, thread_id: str) -> _RunScope:
        store = SerializedSessionFactory(self.session_factory)
        # 一次执行必须有一个轮 id：入口给了就用入口的（那是 run 台账里那一行，能和
        # AgentRun 对上），没给就退回这条线程的 attempt 键。留空等于账本上出现一批
        # 「按哪轮都筛不到」的钱——同步 /run 这个入口真的没有 run 台账。
        recorder = EventRecorder(
            store,
            request.task_id,
            trace_id=thread_id,
            run_id=request.run_id or thread_id,
            origin_run_id=request.origin_run_id,
        )
        settings = SettingsStore().read()
        # 档位在装配层解析一次：网关、工具上下文、子代理与治理快照读的是同一份结果。
        # 不可用就整任务拒绝——不让它有"先跑起来再说"的机会。
        requested_tier = request.sandbox_tier or str(
            (settings.get("sandbox") or {}).get("default_tier") or ""
        )
        try:
            # 装配前保证后端读数不是"还没探过"：拿 pending 做拒绝/放行等于在赌。
            # 缓存新鲜时这里不起进程，过期才补探一次（放线程里，不占事件循环）。
            from blank_api.services.sandbox import docker_probe

            await docker_probe.ensure_fresh()
            sandbox = resolve_tier(requested_tier, strict=True)
        except UnknownTier as exc:
            raise ApiError(code="sandbox_tier_invalid", message=str(exc), status_code=422) from None
        if not sandbox.available:
            raise ApiError(
                code="sandbox_tier_unavailable",
                message=sandbox.notice,
                status_code=409,
            )
        # 设置页可以关掉按需加载：关掉后所有工具每轮都下发，tool_search 也就没有意义了。
        # 走特性注册表，老字段 tools.search 仍然作为回落值生效。
        tool_search_enabled = is_enabled("tool_search", settings)
        tools = build_default_tools()
        if not tool_search_enabled:
            tools = [tool for tool in tools if tool.name != "tool_search"]
        # 团队管理工具只给主理人：不是团队任务、或不是主理人时，从装配层直接拿掉，
        # 模型连"有这个工具"都不该知道。工具自身还会再校验一次身份。
        if not (request.team_scope or {}).get("is_admin"):
            tools = [tool for tool in tools if tool.name not in TEAM_ADMIN_TOOL_NAMES]
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
            sandbox_tier=sandbox.tier,
        )
        # 落盘的前缀规则：重启/换任务后仍认得用户以前批准过的命令签名。
        # 读不出来（文件缺失/损坏）就是空集——fail-closed，多问一次而不是少问一次。
        permissions.persistent_prefixes = load_prefix_rules()
        # 人手写的 allow/deny 规则。文件写坏时整份停用并把错误记进事件：
        # 静默失效会让用户以为"我明明写了 deny"。
        ruleset = load_ruleset()
        permissions.ruleset = ruleset
        if ruleset.rules or ruleset.error:
            await recorder.add("permission_rules.loaded", "receive_task", ruleset.summary())
        adapters = [request.adapter, *(request.role_adapters or {}).values()]
        # 按需选模型：候选不足两个就没有"选"可言，直接不建路由，
        # 关掉开关时行为与单模型完全一致（router=None 时 call_model 走原路径）。
        routing_pool = tuple(request.routing_pool or ())
        router = Router(pool=routing_pool) if routing_pool and is_enabled("model_routing", settings) else None
        if router is not None and not router.active:
            router = None
        # 负载均衡只在真的会"选"的时候才有意义：候选不足两个时连查库都省掉。
        # 查不到数据也照开——本次任务内部的轮换不依赖它。
        if router is not None and is_enabled("load_balance", settings):
            router.seed_load(await recent_load(self.session_factory))
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
            team_scope=dict(request.team_scope or {}),
            sandbox_tier=sandbox.tier,
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
            system_instructions=[str(item) for item in request.system_instructions],
            acceptance_criteria=[str(item) for item in request.acceptance_criteria or ()],
            max_steps=max(1, int(request.max_steps)),
            # 模型看不懂图时一张都不挂：挂了就是 400，用户看到的是"传了图就发不出去"。
            # 降级的说法已经写进 initial_messages 的提示里了。
            vision_images=list(request.images) if request.supports_vision else [],
            token_budget=_token_budget(),
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
            sandbox_tier=sandbox.tier,
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
            sandbox=sandbox.as_dict(),
            subagents=subagents,
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

    async def _submit_prompt(self, scope: _RunScope) -> _PromptDecision:
        """提交那一刻的三道决定：补上下文、抑制原文、否决提交（#123）。

        对标 Claude Code 的 `UserPromptSubmit` / `UserPromptExpansion`：钩子给的
        `additionalContext` 过去只进审计、正文被静默丢掉，`suppressOriginalPrompt`
        这个键我们连读都不读。现在三道都真的作用在"送进模型的那一份"上，
        而 `tasks.goal` 里的原文一个字都不动——抑制的是上下文，不是账。
        """
        session, recorder = scope.session, scope.recorder
        goal = session.goal or ""
        results = await session.hooks.run(
            "user_prompt_submit", "", {"goal": goal, "acceptance_criteria": session.acceptance_criteria}
        )
        if not results:
            return _PromptDecision(goal_for_model=goal)
        await recorder.add(
            "hook.completed",
            "receive_task",
            {"event": "user_prompt_submit", "results": [item.as_dict() for item in results]},
        )
        denied = next((item for item in results if item.denied), None)
        if denied is not None:
            reason = (denied.reason or "钩子拒绝这次提交")[:300]
            await recorder.add(
                "hook.prompt_blocked",
                "receive_task",
                {
                    "reason": reason,
                    "original_chars": len(goal),
                    "original_sha": _prompt_sha(goal),
                    "exit_code": denied.exit_code,
                    "sources": [mask_path(item.command)[:120] for item in results if item.denied],
                },
            )
            return _PromptDecision(blocked=True, reason=reason, goal_for_model=goal)
        asking = next((item for item in results if item.asks), None)
        if asking is not None:
            # 确认界面是工具档的事（审批卡），提交这一步没有：钩子要求"先问一声"时
            # 只能按拒绝处理。静默放行等于把钩子写下的条件丢掉，而它自己以为问过了。
            note = f"：{asking.reason}" if asking.reason else ""
            reason = f"提交钩子要求先确认（ask），提交这一步没有确认界面，按拒绝处理{note}"[:300]
            await recorder.add(
                "hook.prompt_blocked",
                "receive_task",
                {
                    "reason": reason,
                    "decision": "ask",
                    "original_chars": len(goal),
                    "original_sha": _prompt_sha(goal),
                    "exit_code": asking.exit_code,
                    "sources": [mask_path(item.command)[:120] for item in results if item.asks],
                },
            )
            return _PromptDecision(blocked=True, reason=reason, goal_for_model=goal)
        # 钩子只能收紧：任何一条要求"原文不进上下文"就抑制；替代正文按配置顺序拼接。
        notes = [item.additional_context.strip() for item in results if item.additional_context.strip()]
        replacement = "\n\n".join(notes)
        sources = [mask_path(item.command)[:120] for item in results]
        if not any(item.suppress_original for item in results):
            if replacement:
                await recorder.add(
                    "hook.prompt_injected",
                    "receive_task",
                    {"count": len(notes), "chars": len(replacement), "original_kept": True, "sources": sources},
                )
                return _PromptDecision(goal_for_model=f"{goal}\n\n{replacement}" if goal else replacement)
            return _PromptDecision(goal_for_model=goal)
        if not replacement:
            # 要求抑制却没给替代正文 = 给模型一个空目标，比照送更糟。照送并说明。
            await recorder.add(
                "hook.prompt_substituted",
                "receive_task",
                {
                    "suppressed": False,
                    "scope": "goal",
                    "original_chars": len(goal),
                    "original_sha": _prompt_sha(goal),
                    "replacement_chars": 0,
                    "sources": sources,
                    "skipped_reason": "钩子要求抑制原文但没有给出替代正文，原文照送",
                },
            )
            return _PromptDecision(goal_for_model=goal)
        await recorder.add(
            "hook.prompt_substituted",
            "receive_task",
            {
                "suppressed": True,
                "scope": "goal",
                "original_chars": len(goal),
                "original_sha": _prompt_sha(goal),
                "replacement_chars": len(replacement),
                "sources": sources,
            },
        )
        return _PromptDecision(
            goal_for_model="（本节由提交钩子改写，用户原始提交未进入上下文）\n" + replacement,
            suppressed=True,
        )

    async def _close_hooks(self, scope: _RunScope) -> None:
        """任务收尾：给后台钩子一个有界窗口，并如实记下没等到 / 没送到的那些。

        后台钩子退出码 2 的正常去处是「下一次模型调用」；任务已经收尾时没有下一次，
        所以这里不假装送达——记 ``hook.wakeup_missed``，让人在时间线上看见
        「有个叫醒晚了」。等不到的子进程会被取消，事件里只留掩码后的命令与原因。
        """
        hooks = scope.session.hooks
        completed, abandoned = await hooks.close()
        for item in completed:
            await scope.recorder.add(
                "hook.completed",
                "receive_task",
                {
                    "event": item.event,
                    "async": True,
                    "phase": "session_end",
                    "results": [item.as_dict()],
                },
            )
        wakeups = hooks.drain_wakeups()
        if wakeups:
            await scope.recorder.add(
                "hook.wakeup_missed",
                "receive_task",
                {
                    "count": len(wakeups),
                    "delivered": False,
                    "reason": "任务已收尾，没有下一次模型调用",
                    "items": [
                        {
                            "event": str(item.get("event") or ""),
                            "exit_code": item.get("exit_code"),
                        }
                        for item in wakeups
                    ],
                },
            )
        if abandoned:
            await scope.recorder.add(
                "hook.abandoned",
                "receive_task",
                {
                    "count": len(abandoned),
                    "phase": "session_end",
                    "budget_seconds": hooks.settle_timeout,
                    "commands": [mask_path(command) for command in abandoned],
                },
            )

    async def run(self, request: RunRequest) -> dict[str, Any]:
        thread_id = await next_thread_id(self.session_factory, request.task_id)
        await self._guard_tier_change(request)
        # 同一任务的后继轮次沿用模型拨过的模式；没有历史事件才落到设置页默认值。
        if not request.permission_mode:
            request.permission_mode = await self._stored_mode(request.task_id)
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
        # 提交钩子在这一刻决定"这条正文进不进模型"（#123）。拦下时连 task.started 都不发：
        # 它从未开始跑，先念一句"已开始"再念一句"失败"是给时间线作假。
        prompt = await self._submit_prompt(scope)
        if prompt.blocked:
            await self._finish(
                scope, "failed", summary="", reason=f"提交被钩子拦下：{prompt.reason}", steps=0
            )
            return await build_evidence(scope.store, request.task_id)
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
                # 档位是任务级配置：审批恢复与事后审计都读这一条（requested 优先）。
                "sandbox": scope.sandbox,
                # 扁平一份进去：事件消费方（面板/证据包）不必再钻进 sandbox.tier 里找。
                "sandbox_mode": getattr(session, "sandbox_tier", ""),
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
        # 会话生命周期钩子：对标 Codex 的 session_start。
        # user_prompt_submit 不在这里——它在 task.started 之前就跑完了（#123），
        # 因为它的决定是"这条正文进不进模型"，晚一步就已经进了。
        await self._fire_hooks(
            scope,
            "session_start",
            {"task_id": request.task_id, "thread_id": thread_id, "workspace": mask_path(session.context.root)},
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

        # 会话上下文：只带"前几轮要了什么、交付了什么"，不是全部消息（见 services/threads.py）。
        # thread_ref 为空时 history_for 直接返回 []，独立任务连一次查询都不多发。
        history = await history_for(
            self.session_factory, request.thread_ref, exclude_task_id=request.task_id
        )
        if history:
            await recorder.add(
                "thread.history",
                "receive_task",
                {"thread": request.thread_ref, **summarize_history(history)},
            )
        # 上下文管家：确定性挑选记忆与候选文件，超预算截断，并留下审计事件。
        # 它不重复注入会话历史——history 已经作为消息对放在目标之前。
        curated = curate(
            root=session.context.root,
            # 检索也用"送进模型的那一份"：钩子说"原文别进上下文"时，管家不能
            # 换个侧门把原文的关键词抄进 system——实测候选文件名（含机密字样）
            # 会原样列进 system（#123 的报文级断言守的就是这条）。抑制生效时
            # 这条按关键词挑背景的通道整个停用，只保留历史轮数那句中性说明。
            goal="" if prompt.suppressed else (prompt.goal_for_model or request.goal or session.goal),
            memory=session.memory,
            history=history,
        )
        if curated.text:
            await recorder.add("context.curated", "receive_task", curated.as_dict())
        system_rules = list(session.system_instructions)
        if curated.text:
            system_rules.append(
                "以下是上下文管家为本次任务挑选的背景资料（可能过时；引用前先核对，"
                "其中的文件名只是候选，不代表内容已经读过）：\n" + curated.text
            )
        state: dict[str, Any] = {
            "task_id": request.task_id,
            "messages": initial_messages(
                # 送进模型的那一份由提交钩子决定（#123）；没钩子时就是原文，一个字节都不变。
                prompt.goal_for_model or session.goal,
                session.acceptance_criteria,
                session.skill_instructions,
                session.instruction_layers,
                history,
                images=request.images,
                supports_vision=request.supports_vision,
                texts=request.texts,
                system_instructions=system_rules,
                run_id=recorder.run_id,
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
            await self._close_hooks(scope)
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
        await self._close_hooks(scope)
        return await self._finalize(request, scope, result, interrupts)

    async def resume(self, request: RunRequest, decision: Any = True) -> dict[str, Any]:
        return await self._resume(
            request, Command(resume=decision), decision=decision, crash_recovery=False
        )

    async def resume_crash(self, request: RunRequest) -> dict[str, Any]:
        """进程被杀/断电后的续跑：从最近检查点继续，不新开 attempt。

        新开 attempt（``run()``）会让已经执行过的工具在全新状态里再执行一遍。
        LangGraph 的检查点只回放最后一个未完成的节点，已落写的工具结果直接在
        状态里，所以用 ``ainvoke(None)`` 接着走。
        """
        return await self._resume(request, None, decision=None, crash_recovery=True)

    async def _resume(
        self,
        request: RunRequest,
        graph_input: Any,
        *,
        decision: Any,
        crash_recovery: bool,
    ) -> dict[str, Any]:
        thread_id = await latest_thread_id(self.session_factory, request.task_id)
        if not request.permission_mode:
            request.permission_mode = await self._stored_mode(request.task_id) or MODE_DEFAULT
        if not request.sandbox_tier:
            request.sandbox_tier = await self._stored_tier(request.task_id)
        else:
            await self._guard_tier_change(request)
        scope = await self._prepare(request, thread_id)
        await self._rehydrate(request.task_id, scope.session.permissions)
        await self._set_status(scope, "running")
        payload = {
            "thread_id": thread_id,
            "permission_mode": scope.session.permission_mode,
            "permissions": scope.session.permissions.snapshot(),
            "sandbox": scope.sandbox,
        }
        if crash_recovery:
            payload["crash_recovery"] = True
        else:
            payload["decision"] = decision if isinstance(decision, (bool, str)) else dict(decision)
        await scope.recorder.add("task.resumed", "receive_task", payload)
        try:
            result = await scope.graph.ainvoke(graph_input, scope.config)
        except ModelError as exc:
            await self._finish(
                scope,
                "failed",
                summary="",
                reason=exc.message,
                extra={"code": exc.code, "thread_id": thread_id},
            )
            await self._close_hooks(scope)
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
        await self._close_hooks(scope)
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
            if request.index_summary:
                await self._index_summary(scope, request)
            await self._consolidate(
                scope, result, title=request.title, source=request.memory_source
            )
        return await build_evidence(scope.store, task_id)

    async def _index_summary(self, scope: _RunScope, request: RunRequest) -> None:
        """把这一轮的结论写进聊天索引。索引失败不能把成功的任务变成失败。"""
        try:
            async with scope.store() as session:
                task = await session.get(Task, scope.task_id)
                if task is None:
                    return
                thread = (
                    await session.get(Thread, task.thread_id) if task.thread_id else None
                )
                await index_task_summary(session, task, thread, title=request.title)
                await session.commit()
        except Exception as exc:
            await scope.recorder.add(
                "search.index_failed", "complete", {"error": str(exc)[:300]}
            )

    async def _consolidate(
        self, scope: _RunScope, result: dict, *, title: str, source: str = ""
    ) -> None:
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
                # 验收覆盖度是模型自报的，不是独立核对结果；记忆里如实标注，
                # 免得下一轮把它当成已验证的事实引用。
                facts.append(f"自报验收「{text}」：{state}")
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
                source=source,
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
        # 任务走到终点时仍在跑的后台子代理必须收走：否则任务已经显示"完成"，
        # 它们还在调用模型、写事件，账目与界面从此对不上。等待审批不是一个终点——
        # 恢复后还要用同一个句柄取结论，所以那一档不收。
        if status != "waiting_approval" and scope.subagents is not None:
            await scope.subagents.aclose(scope.task_id, reason=f"task_{status}")
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
        # 取消端点会当场写一条 task.cancelled：界面靠它立刻解锁，不用等运行时
        # 走到收尾（模型调用挂着时那一步可能永远不到）。这里再写一条不会让界面
        # 更早解锁，只会让"这一轮结束了几次"变成需要解释的问题，所以先问一句。
        skip_terminal = status in ("succeeded", "failed", "cancelled", "paused") and await scope.recorder.has_terminal()
        if not skip_terminal:
            await scope.recorder.add(event_type, "complete", payload)
        if status in ("succeeded", "failed", "cancelled"):
            # fs-broker（#105）：任务走到终点，它那一枚 broker 随任务收走——
            # 关 Job 句柄由内核带走整棵进程树（M0 P6.7 实测生效）。收尸失败不改判任务，
            # 但必须把异常喊出来，不给"残留 broker 还活着"留静默。
            from blank_api.services.tools import fs_broker as _fs_broker

            try:
                await _fs_broker.manager().release_task(scope.task_id)
            except Exception as exc:  # noqa: BLE001
                await scope.recorder.add(
                    "fs_broker.release_failed", "complete",
                    {"reason": f"{exc.__class__.__name__}: {exc}"},
                )


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

"""Task runner: builds the tool gateway, the LangGraph workflow and the evidence pack."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field, replace as dc_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langgraph.errors import GraphInterrupt
from langgraph.types import Command
from sqlalchemy import select

from blank_api.api.errors import ApiError
from blank_api.limits import DEFAULT_TOKEN_BUDGET
from blank_api.models.checkpoint import AgentCheckpoint
from blank_api.models.goal import Goal
from blank_api.models.task import AgentEvent, Task
from blank_api.models.thread import Thread
from blank_api.services.agent.checkpoints import (
    SqlAlchemyCheckpointSaver,
    task_id_from_thread,
)
from blank_api.services.agent.context import ContextCompactor
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.evidence import build_evidence
from blank_api.services.agent.goals import GoalTracker
from blank_api.services.agent.graph import (
    AgentSession,
    _changed_paths,
    _git_base_sha,
    _last_assistant_text,
    _with_delivery_notes,
    assumption_ledger,
    build_agent_graph,
    initial_messages,
)
from blank_api.services.agent.verifier import (
    failure_reason as verdict_failure_reason,
    verify as verify_facts,
)
from blank_api.services.agent.terminal import cancelled_payload, terminal_detail
from blank_api.services.agent.automation import resolve_mode as resolve_automation_mode
from blank_api.services.agent.loadstats import recent_load
from blank_api.services.agent import repl as repl_module
from blank_api.services.agent import workflow as workflow_module
from blank_api.services import plan_mode
from blank_api.services import shell_tasks, worktree_session
from blank_api.services.agent.router import Candidate, Router
from blank_api.services.agents_defs import discover_agents
from blank_api.services.commands_defs import discover_commands
from blank_api.services.output_styles_defs import discover_output_styles
from blank_api.services.skills_defs import discover_skills, merge_skill_entries
from blank_api.services.agent.scenarios import (
    excluded_tools,
    resolve as resolve_scenario,
    system_prompt as scenario_prompt,
)
from blank_api.services.agent.store import SerializedSessionFactory
from blank_api.services.approval_rules import load_prefix_rules
from blank_api.services.session_grants import load_categories as load_session_grants
from blank_api.services.approvals import approval_key
from blank_api.services.permission_rules import (
    load_ruleset,
    merge_rulesets,
    parse_settings_rules,
)
from blank_api.services.chat_index import index_task_summary
from blank_api.services.features import is_enabled
from blank_api.services.fork import allocate_thread
from blank_api.services.guardian import Guardian
from blank_api.services.hooks import (
    ExpansionOutcome,
    HookResult,
    HookRunner,
    non_blocking_errors,
    prompt_sha,
    run_expansion_hooks,
    stop_failure_error_category,
    usable_context,
    write_transcript,
)
from blank_api.services.instructions import load_layers, provenance
from blank_api.services.context_curator import curate
from blank_api.services.lsp import LSPServerManager
from blank_api.services.mcp import manager as mcp_manager
from blank_api.services.memory import MemoryStore
from blank_api.services.plan import plan_fingerprint
from blank_api.services.notes import NoteStore
from blank_api.services.safety import SafetyPolicy
from blank_api.services.settings import SettingsStore
from blank_api.services.shell_env import capture as capture_shell
from blank_api.services.subagent import DEFAULT_STEPS, SubAgentRequest, SubAgentRunner
from blank_api.services.threads import history_for, session_goal_for, summarize_history
from blank_api.services.models.base import ModelAdapter, ModelError
from blank_api.services.permissions import (
    MODE_ACCEPT_EDITS,
    MODE_DEFAULT,
    MODE_PLAN,
    SCOPE_SESSION,
    PermissionPolicy,
)
from blank_api.services.tools import TEAM_ADMIN_TOOL_NAMES, SkillCatalog, build_default_tools
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.file_state import FileStateCache
from blank_api.services.tools.gateway import ToolGateway
from blank_api.services.tools.delegate import _apply_definition, _resolve_agent
from blank_api.services.sandbox import SnapshotStore
from blank_api.services.sandbox.tiers import (
    TierDowngrade,
    UnknownTier,
    assert_not_looser,
    normalize_tier,
    resolve_tier,
)
from blank_api.services.workspace import mask_path

log = logging.getLogger(__name__)


# 开启强制时，交付文本必须自己说明哪几条没过——与 UNVERIFIED_NOTE 同一处手法：
# 状态可以还是成功（活确实干完了），但绝不能让读者以为"按他的标准做到了"。
STANDARD_UNMET_NOTE = (
    "\n按你的做事标准，这次有没做到的：{items}。"
    "（这几条是收尾时按事实核对的，不是模型自评。想关掉：设置 features.standard_enforce=false）"
)


def _standard_enforced() -> bool:
    """是否把标准变成硬约束。**默认关**。

    为什么默认关：条款是从他的话里展开的，理解可能偏；一上来就拦，会把
    "理解偏了"变成"任务被判不合格"。先只看不拦，等符合度有真实样本再说。
    """
    try:
        from blank_api.services.settings import SettingsStore

        features = SettingsStore().read().get("features") or {}
        return bool(features.get("standard_enforce"))
    except Exception:  # noqa: BLE001 - 读不到设置就当没开
        return False


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


# 轮询取消信号的间隔。太密没意义（人从看到界面到按下停止至少几百毫秒），
# 太疏会让"停止"看起来没反应。0.4 秒是这两个约束之间的取值。
CANCEL_POLL_SECONDS = 0.4


async def _watch_cancellation(
    store, task_id: str, event: asyncio.Event
) -> None:
    """任务被取消时置位执行侧的取消事件。

    单独起一个协程，而不是在图里查：图只在节点边界醒过来，一条跑了五分钟的命令
    期间没有任何节点在跑。只有旁边的轮询能在那一刻叫停那条命令。
    """
    lookup = _CancelledLookup(store, task_id)
    while not event.is_set():
        try:
            if await lookup():
                event.set()
                return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 读库失败不该让整轮挂掉
            pass
        await asyncio.sleep(CANCEL_POLL_SECONDS)


class _StatusLookup:
    """读当前任务状态。取消与暂停要分开处理，一个布尔不够用。"""

    def __init__(self, session_factory, task_id: str) -> None:
        self.session_factory = session_factory
        self.task_id = task_id

    async def __call__(self) -> str:
        async with self.session_factory() as session:
            status = (
                await session.execute(select(Task.status).where(Task.id == self.task_id))
            ).scalar_one_or_none()
        return str(status or "")


def _attempt(thread_id: str) -> int:
    try:
        return int(str(thread_id).rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return 0


def _plan_signature(steps: list[dict] | None) -> tuple:
    """计划的"有没有变过"指纹。真源在 services.plan.plan_fingerprint，
    这里只留个别名——版本号判定必须和手工改计划那条路径用同一套口径。"""
    return plan_fingerprint(steps)


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
    # 纯聊天：没有工作目录，不暴露任何文件/命令工具，直接作答。
    chat_only: bool = False
    # 用户明确要求本轮不要调用工具：可以有工作目录，但工具表必须为空。
    no_tools: bool = False
    # 用户只要文件/目录清单：只给 list_files / glob，不读文件内容。
    list_only: bool = False
    run_id: str = ""
    # 发起轮：引起这一整棵任务树的那一轮。空=这次运行自己就是发起轮（记录器里退回 run_id）。
    # 只有跨任务的链上它才和 run_id 不同：一轮群聊里每个发言成员各有自己的 run_id。
    origin_run_id: str = ""
    title: str = ""
    goal: str = ""
    acceptance_criteria: Sequence[str] = ()
    # 结构化目标 {objective, done_when, constraints, out_of_scope}。constraints /
    # out_of_scope 会进首轮提示（模型必须看见用户声明的边界）；done_when 只用于收尾对账。
    goal_spec: dict | None = None
    # 这条 goal 的来历（斜杠命令展开，对标上游 UserPromptExpansion 的载荷）：
    # {expansion_type, command_name, command_args, command_source, prompt, ...}。
    # None = 不是展开来的，提交钩子里的 user_prompt_expansion 一个都不触发。
    expansion: dict | None = None
    # 验收条件与计划步骤的对应关系 [{id, criterion, milestone_id, step_ids}]。
    # 有了它，验收失败能指到具体是哪一步没做完，而不是只知道"整体没过"。
    acceptance_links: Sequence[dict] = ()
    max_steps: int = 8
    approve_high_risk: bool = False
    approved_tools: set[str] | None = None
    approved_actions: set[str] | None = None
    approved_command_prefixes: Sequence[str] = ()
    # 空串表示"沿用该任务已有的模式"：审批恢复时前端不重发模式，不能被默认值悄悄改口径。
    permission_mode: str = ""
    # 场景（scenarios.py）。决定系统提示附加段与工具白名单；不向用户 prompt 拼前缀。
    scenario: str = "dev"
    # 派发快照（dispatch.py，v162）。空 dict = 这条 run 不是按快照起的。
    # 执行侧只读它：工具边界与步数上限都从这里来，不重新解释用户权限。
    dispatch: dict = field(default_factory=dict)
    # 快照判定的只读。True 时整个回合只放行只读工具（与「征求意见」同一道闸），
    # 而不是靠提示词"请勿修改"——那句话模型可以不听。
    readonly: bool = False
    # 仅日常场景有效：用户选择"记住习惯"时开放 memory_write。
    memory_opt_in: bool = False
    # 空串表示"用设置页的默认档"；审批恢复时同样要读回任务首次运行记下的档位，
    # 否则一次恢复就能把 workspace 静默换成 off（或反之）。
    sandbox_tier: str = ""
    # 旧形状：把启用 Skill 的正文直接注进系统提示。保留给外部集成；主路径改成
    # skill_catalog——清单进提示、正文由 skill 工具按需加载（对齐 Claude）。
    skill_instructions: Sequence[str] = ()
    # 可用 Skill 的只读快照 [{name, instructions}]。空表示没有可加载的 Skill，
    # `skill` 工具会从这一轮的工具表里被拿掉。
    skill_catalog: Sequence[dict] = ()
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
    # 审批决定的恢复载荷（E13）。只在「审批 → 入队 → worker 认领」这条内部链路上
    # 从 ``agent_runs.request_payload`` 重建，不进入公开 API schema。None 表示这条
    # run 不是审批恢复；``_execute_claimed_run`` 据此分流 resume / resume_crash。
    resume_decision: dict | None = None


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
    # 任务级隔离根（J3，task_isolation.TaskRoot）；None 表示这一轮跑在共享工作区。
    isolation: Any = None
    # 收尾只结算一次：_finish 可能被走到多次（提前取消、异常、正常收尾）。
    isolation_settled: bool = False
    # 场景 key（scenarios.py）。task.started 记"当时在哪个场景"用，事件与证据都以它为准。
    scenario_key: str = "dev"
    # 本轮选中的输出样式（as_prompt 形状）；None ＝ 没选/不可用，系统提示走默认。
    output_style: dict | None = None
    # 这条 goal 的来历（斜杠命令展开）；None ＝ 不是展开来的，提交钩子不触发
    # user_prompt_expansion。与 goal 分开带：goal 是展开后的正文，这里记的是
    # 展开本身的事实（钩子载荷要读的那几个键）。
    expansion: dict | None = None
    # 轮询取消信号的协程句柄。收尾时必须取消它：这个协程的生命周期比这一轮长，
    # 没人收就会一直读库，任务结束后每 0.4 秒查一次。
    cancel_watch: Any = None


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


@dataclass
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
                # 会话级授权即使落盘失败，也要靠事件回放活过本次恢复；
                # 事件里的类别是批准当时算出来的，不按打码后的参数重算。
                if scope == SCOPE_SESSION:
                    permissions.remember_session_grant(
                        str(payload.get("session_grant_category") or "")
                    )
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

    async def _stored_info(self, task_id: str) -> dict:
        """一次查询拿到 mode 与 tier_requested，避免 _stored_mode + _stored_tier
        各自开启独立 session 带来的重复 DB 往返。"""
        mode = ""
        tier = ""
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(AgentEvent.type, AgentEvent.payload)
                    .where(
                        AgentEvent.task_id == task_id,
                        AgentEvent.type.in_(
                            ("task.started", "permission.mode_changed")
                        ),
                    )
                    .order_by(AgentEvent.seq.desc())
                    .limit(2)
                )
            ).all()
        for kind, payload in rows:
            data = payload if isinstance(payload, dict) else {}
            if kind == "permission.mode_changed" and not mode:
                mode = str(data.get("to") or "")
            elif kind == "task.started":
                if not mode:
                    mode = str(data.get("permission_mode") or "")
                if not tier:
                    sandbox = data.get("sandbox")
                    if isinstance(sandbox, dict):
                        tier = str(sandbox.get("requested") or "")
            if mode and tier:
                break
        return {"mode": mode, "tier": tier}

    async def _stored_pre_plan_mode(self, task_id: str) -> str:
        """进计划模式之前是什么模式（对标 Claude 的 `prePlanMode`）。

        审批中断会换一个新会话，内存里的 `pre_plan_mode` 就没了；不把它从事件里
        捡回来，用户从「接受编辑」进的计划模式，退出来会掉到「默认」——写文件
        突然又要一张张点，而他没改过任何设置。

        只认"进入计划模式"那一条事件的 `from`：`permission.mode_changed` 的 `from`
        就是进入前的模式，这条事实在库里，不用另存一份。
        """
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(AgentEvent.payload)
                    .where(
                        AgentEvent.task_id == task_id,
                        AgentEvent.type == "permission.mode_changed",
                    )
                    .order_by(AgentEvent.seq.desc())
                    .limit(20)
                )
            ).scalars().all()
        for payload in rows:
            data = payload if isinstance(payload, dict) else {}
            if str(data.get("to") or "") != MODE_PLAN:
                continue
            previous = str(data.get("from") or "")
            if previous in (MODE_DEFAULT, MODE_ACCEPT_EDITS):
                return previous
            return ""
        return ""

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
        chat_only = bool(request.chat_only)
        no_tools = chat_only or bool(request.no_tools)
        list_only = bool(request.list_only)
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
        # 设置按 用户 < 项目 < 本机 < 管理 叠一次（见 services/settings_layers.py）。
        # 沙箱档位、审批模式、guardian 与安全段只认用户/管理层，项目文件写这些也进
        # ignored；其余偏好（压缩水位、输出样式、工具检索、后台预算）项目层可以覆盖。
        layered = SettingsStore().read_layered(request.root)
        settings = layered.settings
        if layered.changed():
            await recorder.add("settings.layers", "receive_task", layered.as_dict())
        # 档位在装配层解析一次：网关、工具上下文、子代理与治理快照读的是同一份结果。
        # 不可用就整任务拒绝——不让它有"先跑起来再说"的机会。
        #
        # 快照里的档位优先：它是派发那一刻定下的"这件事跑在哪"。执行时另传一个更宽的
        # 档位不能把它顶掉，否则"沙箱档位固定"只是句口号（v164 §5.3）。
        pinned_tier = str((request.dispatch or {}).get("sandbox_tier") or "").strip()
        requested_tier = pinned_tier or request.sandbox_tier or str(
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
        # UP-04：网络在本机后端不由系统强制时，任务一启动就把这条事实推给界面。
        # 只提示不阻断：用户仍可用，但不能以为命令已经被禁网。
        # 没有持久化通道（单测/嵌入式装配）时不写事件：这条只是给界面的提示，
        # 没有会话工厂就没有界面；硬写会变成 'NoneType' object is not callable。
        if self.session_factory is not None and not sandbox.network_enforced and not no_tools:
            await recorder.add(
                "sandbox.warning",
                "receive_task",
                {
                    "message": "本机模式下网络访问未受限，Agent 执行的命令可访问外网",
                    "level": "warning",
                    "tier": sandbox.tier,
                    "backend": sandbox.backend,
                },
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
        # 用原始工作区根而不是任务隔离副本：`.mcp.json` 跟着仓库走，
        # 副本是本次任务的写根，里面不该凭空多出一份 MCP 配置。
        if no_tools or list_only:
            # 纯聊天/明确不要工具/只要清单：不连接任何外部工具。MCP 也会按
            # 用户级配置起进程/联网，这一轮不该有这种副作用。
            from blank_api.services.mcp.bridge import MCPBundle

            mcp_bundle = MCPBundle()
        else:
            mcp_bundle = await mcp_manager().bundle(request.root)
            tools = [*tools, *mcp_bundle.tools]
        # 场景装配（scenarios.py）：只影响系统提示附加段与工具可用性，
        # 不向用户 prompt 拼前缀。被禁工具在注册表里消失、在网关执行处硬拦。
        scenario = resolve_scenario(request.scenario)
        scenario_excluded = excluded_tools(scenario, memory_opt_in=request.memory_opt_in)
        # 纯聊天没有工作目录，也不该有任何能碰电脑的入口。工具表在装配层清空，
        # 模型连工具 schema 都看不到；下面建网关仍走同一条路，只是池子为空。
        if no_tools:
            tools = []
        elif list_only:
            # 只要清单：读文件内容这一步本身就该被挡在装配层，而不是等模型
            # 读了一堆文件再靠提示词劝它“别读”。
            from blank_api.services.agent.chat import LIST_ONLY_TOOLS

            tools = [tool for tool in tools if tool.name in LIST_ONLY_TOOLS]
        # J3 任务级隔离：每个任务一个独立可写根 + 一个只属于它的 AppContainer 包。
        # 逆向场景的写根已经单独切到分析区（下一步），不再套一层副本；其余场景打开开关
        # 就整任务迁移到副本里跑，AC 建不起来直接拒绝——不偷偷退回共享根。
        # 续跑/恢复复用同一个任务的根：换一个新克隆会把审批前做的改动丢掉。
        isolation = None
        if (
            sandbox.policy.task_isolation
            and scenario.key != "reverse"
            and not no_tools
            and not list_only
        ):
            from blank_api.services.sandbox import task_isolation

            isolation = task_isolation.get(request.task_id)
            reused = isolation is not None and Path(isolation.path).is_dir()
            if not reused:
                try:
                    isolation = await task_isolation.create_async(request.root, request.task_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 边界建不起来就整任务拒绝
                    raise ApiError(
                        code="task_isolation_unavailable",
                        message=(
                            "任务级隔离根创建失败，任务未执行："
                            f"{exc.__class__.__name__}: {exc}"
                        ),
                        status_code=409,
                    ) from None
            await recorder.add(
                "isolation.reused" if reused else "isolation.created",
                "receive_task",
                isolation.as_dict(),
            )
        run_root = Path(isolation.path) if isolation is not None else Path(request.root)
        # 上一轮最后停在 worktree 里：恢复后的任务仍然在那棵树上，而不是悄悄退回主工作区。
        # 会话记录按 task_id 存活（审批恢复会重建 ToolContext），目录不在时这里返回 None。
        worktree_root = (
            None if (no_tools or list_only) else worktree_session.active_root(request.task_id)
        )
        if worktree_root is not None:
            run_root = worktree_root
        # 文件型子代理定义：在最终运行根上发现一次（用户级 agents 目录 + 项目
        # .blank/agents）。单个坏文件只进 errors 清单，不拖垮整张工具表；
        # 没有任何自定义类型时仍可用内置 general-purpose。
        agents = discover_agents(run_root)
        if agents.custom() or agents.errors:
            await recorder.add("agents.loaded", "receive_task", agents.as_dict())
        # 磁盘 Skill（.blank/skills/<name>/SKILL.md）与库里的启用项合成同一份目录：
        # 清单进系统提示，正文等模型调用 skill 再按需加载。层级从低到高是
        # 库（应用里启用的）→ 用户级 → 项目级，项目能盖住同名项（与 Claude 一致）；
        # 但"模型不可调用"的同名定义不能盖掉可调用的那一份。
        disk_skills = discover_skills(run_root)
        if disk_skills.entries or disk_skills.errors or disk_skills.warnings:
            await recorder.add("skills.loaded", "receive_task", disk_skills.as_dict())
        # 自定义斜杠命令（.blank/commands/**/*.md）：用户侧 `/name args` 由路由
        # 展开，模型侧与 Skill 共用同一份目录（同名时命令胜）。
        commands = discover_commands(run_root)
        if commands.entries or commands.errors or commands.warnings:
            await recorder.add("commands.loaded", "receive_task", commands.as_dict())
        # 输出样式（.blank/output-styles/**/*.md）：设置页选一个，正文进系统提示。
        # 选中的名字查不到时按默认样式跑（Claude 的 `?? null`），只在事件里留痕。
        output_styles = discover_output_styles(run_root)
        selected_style = str(settings.get("output_style") or "").strip() or "default"
        active_style = output_styles.resolve(selected_style)
        if output_styles.entries or output_styles.errors or output_styles.warnings:
            await recorder.add(
                "output_styles.loaded",
                "receive_task",
                {
                    **output_styles.as_dict(),
                    "selected": selected_style,
                    "active": active_style.name if active_style is not None else "default",
                },
            )
        dropped_fallbacks: list[str] = []
        skills = SkillCatalog(
            merge_skill_entries(
                request.skill_catalog,
                [item.as_entry() for item in disk_skills.entries.values()],
                [item.as_entry() for item in commands.entries.values()],
                dropped=dropped_fallbacks,
            )
        )
        if dropped_fallbacks:
            await recorder.add(
                "skills.fallback_shadowed",
                "receive_task",
                {"dropped": list(dropped_fallbacks)},
            )
        # 一个模型可加载的都没有时，工具也一并拿掉——列不出名字却摆着加载入口，
        # 只会让模型反复试错。
        if not skills.model_available():
            tools = [tool for tool in tools if tool.name != "skill"]
        # 逆向场景：读根仍是用户的工作区（样本在那儿），写根切到独立分析区。
        # 这一层不写在提示词里——提示词只是"别改原文"的建议，换写入根才是约束（v164 §5）。
        write_root = None
        if scenario.key == "reverse":
            from blank_api.services.agent.analysis import analysis_root

            write_root = analysis_root(request.task_id)
            await recorder.add(
                "analysis.workspace",
                "receive_task",
                {"root": str(write_root), "readonly_source": str(request.root)},
            )
        # LSP 语言服务器按任务持有：它的 initialize 绑定了工作区根（含隔离根/worktree），
        # 跨任务复用会把 A 任务的符号表答给 B 任务。这里只建壳；进程等第一次 lsp 调用才拉起。
        lsp_manager = LSPServerManager(root=run_root)
        gateway = ToolGateway(
            tools,
            defer=tool_search_enabled,
            excluded=scenario_excluded,
            scenario=scenario.key,
            write_root=write_root,
        )
        gateway.registry.exclude(scenario_excluded)
        # 文档-only：只改 README/文档、不动源码。把任务台账、子代理、技能发现、
        # 浏览器、notebook/REPL、计划模式与审查上报收掉；每轮少发约 10K 字符的
        # schema，且不会让模型看到一堆和这次任务无关的入口。tool_search 仍能
        # 拉回未被排除的能力（git/web 等）。
        from blank_api.services.agent.directives import DOC_ONLY_INSTRUCTION, documentation_only
        from blank_api.services.tools.registry import DOCS_ONLY_EXCLUDED

        docs_only = documentation_only(request.goal, request.acceptance_criteria)
        if docs_only:
            removed = gateway.registry.exclude(DOCS_ONLY_EXCLUDED)
            if removed:
                await recorder.add(
                    "tools.excluded",
                    "receive_task",
                    {"profile": "docs_only", "tools": removed},
                )
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
            # v165 Phase 4：设置页的自动化模式真正进入审批链。它只可能让
            # 「全放行」多问一次高风险动作，不会放宽任何既有判定。
            automation_mode=resolve_automation_mode(settings),
        )
        # 落盘的前缀规则：重启/换任务后仍认得用户以前批准过的命令签名。
        # 读不出来（文件缺失/损坏）就是空集——fail-closed，多问一次而不是少问一次。
        permissions.persistent_prefixes = load_prefix_rules()
        # 会话级授权按 thread_ref 读：同一会话的下一轮任务仍记得“这类动作
        # 不再逐条问”。独立任务（thread_ref 为空）没有这一层，行为不变。
        permissions.session_grants = load_session_grants(request.thread_ref)
        # 计划文件路径随任务固定：计划模式里它是唯一的可写目标，退出后又是"计划在
        # 哪儿"的答案。审批恢复起的新会话必须重新拿到它，否则继承下来的 plan 模式
        # 会变成"连计划都写不了"的死锁。
        permissions.plan_file = plan_mode.plan_relpath(request.task_id)
        if permissions.mode == MODE_PLAN:
            # 从审批中断/上一轮恢复进计划模式：把"从哪个模式进来的"捡回来，
            # 退出时才回得去（否则一律掉回 default，等于偷偷改了用户的档位）。
            permissions.pre_plan_mode = await self._stored_pre_plan_mode(request.task_id)
        # 人手写的 allow/deny 规则。文件写坏时整份停用并把错误记进事件：
        # 静默失效会让用户以为"我明明写了 deny"。
        ruleset = load_ruleset()
        # settings.json 的 permissions.allow/deny/ask（Claude 形状）。它是第二来源：
        # 文件里的规则排在前面（先命中先算），两边的错误与 note 一起带进事件。
        # 写不出来的 deny/ask 会让整份 settings 规则停用；allow 只记 note 跳过。
        settings_rules = parse_settings_rules(settings.get("permissions") or {}, source="settings.json")
        if settings_rules.rules or settings_rules.error or settings_rules.notes:
            await recorder.add(
                "permission_rules.settings",
                "receive_task",
                {
                    "source": layered.sources.get("permissions", "user"),
                    **settings_rules.summary(),
                },
            )
            ruleset = merge_rulesets(ruleset, settings_rules)
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
        shell = capture_shell(run_root)
        # 能力边界跟着设置页走：没开就是没开，请求里带什么都不放宽。
        # 放在这里读一次，同一任务的网关、工具处理、子代理拿到的是同一份策略。
        safety = SafetyPolicy.load()
        # 用户级 → 项目 AGENTS.md（+ 可选外部指令文件）；任务级偏好走 skill_instructions
        layers = load_layers(run_root)
        # 读后写状态跟着任务走：read_file 记录、edit_file/write_file/apply_patch 前对照。
        file_state = FileStateCache()
        # 钩子实例先建出来：任务清单的 TaskCreated / TaskCompleted 在工具层就要判定，
        # 而 ToolContext 比 AgentSession 先构造。同一个实例交给两边，审计结果不分家。
        # 两个来源合并：settings.json 的 hooks（用户/管理层，项目层已被 ignored）与
        # BLANK_HOOKS（运维/测试）；disableAllHooks 为真时一个都不加载。
        hooks = HookRunner.from_env(
            settings=settings.get("hooks"),
            disabled=bool(settings.get("disable_all_hooks")),
        )
        if hooks.enabled or hooks.warnings or hooks.disabled:
            await recorder.add("hooks.loaded", "receive_task", hooks.describe())
        # 取消信号：`/cancel` 只把任务行改成 cancelled，正在跑的那条命令收不到任何
        # 通知——用户按了停止，进程还在烧 CPU、还在往工作区写文件，界面却已经显示
        # "已取消"。这个事件是执行侧唯一能收到那个信号的通道，由下面的 watcher 置位。
        cancel_event = asyncio.Event()
        context = ToolContext(
            hooks=hooks,
            mcp=mcp_bundle,
            lsp=lsp_manager,
            root=run_root,
            task_id=request.task_id,
            recorder=recorder,
            cancel_event=cancel_event,
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
            tool_gateway=gateway,
            team_scope=dict(request.team_scope or {}),
            skills=skills if skills else None,
            agents=agents,
            sandbox_tier=sandbox.tier,
            isolation_profile=isolation.profile_name if isolation is not None else "",
            file_state=file_state,
            # 斜杠命令展开带来的工具约束：expansion 里有 allowed_tools /
            # disallowed_tools 就写进 turn_state，本轮工具池按这份收窄。
            # 对齐上游命令的 allowedTools/disallowedTools——用户敲 /name 后，
            # 后续步骤看得见的工具与能执行的工具都受这份约束。
            turn_state=_turn_state_from_expansion(request.expansion),
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
            chat_only=chat_only,
            no_tools=no_tools,
            list_only=list_only,
            goal=request.goal or request.title,
            skill_instructions=[str(item) for item in request.skill_instructions],
            skill_catalog=skills.listing(),
            agent_catalog=agents.listing(),
            system_instructions=[
                *[str(item) for item in request.system_instructions],
                *([DOC_ONLY_INSTRUCTION] if docs_only else []),
            ],
            acceptance_criteria=[str(item) for item in request.acceptance_criteria or ()],
            goal_spec=dict(request.goal_spec or {}),
            acceptance_links=[dict(item) for item in request.acceptance_links or () if isinstance(item, dict)],
            max_steps=max(1, int(request.max_steps)),
            # 模型看不懂图时一张都不挂：挂了就是 400，用户看到的是"传了图就发不出去"。
            # 降级的说法已经写进 initial_messages 的提示里了。
            vision_images=list(request.images) if request.supports_vision else [],
            # 这一轮模型能不能看图。read_file 读到图片时按它决定「附图」还是「只说一句
            # 看不到」：给不支持视觉的模型塞 image block 会直接 400。
            supports_vision=bool(request.supports_vision),
            token_budget=_token_budget(),
            permissions=permissions,
            # 快照判成只读时，这一整回合就不许写：闸门在这里，不在提示词里。
            # 只要清单的任务也按只读跑。
            readonly=bool(request.readonly) or list_only,
            role_adapters=request.role_adapters or {},
            role_provider_ids=request.role_provider_ids or {},
            router=router,
            routing_adapters=request.routing_adapters or {},
            # 工具裁剪只在真按档位路由时才有意义：没有 router 就没有 tier，
            # 硬开着它只会让每一步都拿去和 full 比一次，然后什么也没变。
            tiered_tools=bool(router is not None and is_enabled("tiered_tools", settings)),
            stream_output=is_enabled("stream_output", settings),
            is_cancelled=_CancelledLookup(store, request.task_id),
            current_status=_StatusLookup(store, request.task_id),
            hooks=hooks,
            guardian=guardian,
            compactor=compactor,
            notes=notes,
            memory=memory,
            shell=shell,
            instruction_layers=layers,
            sandbox_tier=sandbox.tier,
            # 场景 key：执行期还要用它做两件事——只读场景收到"动手"请求时给明确回答，
            # 逆向场景把写入重定向到独立分析区。
            scenario=scenario.key,
            # 会话归属：propose_goal 接受后要写回这条会话的 session_goal。
            thread_ref=str(request.thread_ref or ""),
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
            cancel_watch=asyncio.create_task(
                _watch_cancellation(store, request.task_id, cancel_event)
            ),
            mcp=mcp_bundle.as_dict(),
            sandbox=sandbox.as_dict(),
            subagents=subagents,
            scenario_key=scenario.key,
            isolation=isolation,
            output_style=active_style.as_prompt() if active_style is not None else None,
            expansion=request.expansion,
        )

    async def _fire_hooks(
        self, scope: _RunScope, event: str, payload: dict, target: str = "", *, effort: str = ""
    ) -> list[HookResult]:
        """钩子是可选扩展（BLANK_HOOKS 配置）：失败绝不影响任务，但每次都要留审计记录。

        ``target`` 是钩子配置里 ``match`` 的比对对象：SessionStart 按来源
        （startup / resume）、SessionEnd 按结束原因匹配——上游就是这么分流的。
        返回结果列表：SessionStart 的 additionalContext 要真的进模型上下文，
        "记了审计但正文被丢掉"正是 #123 修过的那种静默丢失。

        ``effort`` 只在上游也带档位的场合传：会话生命周期钩子（SessionStart /
        SessionEnd）上游的输入里没有 effort，只有 Stop 这类工具上下文事件才有。
        """
        results = await scope.session.hooks.run(event, target, payload, effort=effort)
        if results:
            await scope.recorder.add(
                "hook.completed",
                "receive_task",
                {"event": event, "target": target, "results": [item.as_dict() for item in results]},
            )
        return list(results or [])

    async def _fire_stop_failure(self, scope: _RunScope, exc: ModelError) -> None:
        """API 错误终结回合时发 StopFailure（上游在这时**替代** Stop 触发）。

        fire-and-forget：上游明说钩子的输出与退出码一律忽略，所以这里只留审计，
        不读结果、不注入上下文、也不把退出码 2 当成错误——任务已经因为模型失败而
        收尾，钩子说什么都不该再改这个结论。matcher 的比对对象是错误分类
        （见 :func:`stop_failure_error_category`），上游就是这么分流的。
        """
        category = stop_failure_error_category(exc.code, exc.status)
        payload = {"error": category, "error_details": exc.message[:500]}
        try:
            snapshot = await scope.graph.aget_state(scope.config)
            values = getattr(snapshot, "values", None) or {}
            if isinstance(values, dict):
                messages = values.get("messages") or []
                last = _last_assistant_text(messages)
                if last:
                    payload["last_assistant_message"] = last[:4000]
                payload["agent_transcript_path"] = write_transcript(
                    messages,
                    Path(scope.session.context.root)
                    / ".blank"
                    / "context"
                    / "agent-transcript.jsonl",
                )
        except Exception:  # noqa: BLE001 - 读不到就少一个可选字段，事件照发
            log.warning("StopFailure 读取最近回复失败 task=%s", scope.thread_id, exc_info=True)
        await self._fire_hooks(
            scope, "stop_failure", payload, target=category, effort=scope.session.turn_effort
        )

    @staticmethod
    def _session_end_reason(result: dict, interrupts: list) -> str:
        """收尾原因：上游 SessionEnd 的 reason 是钩子的分派键（match=reason）。

        只写我们真的分得清的三种：等人审批、被取消/暂停、正常结束。其余状态
        一律归到 completed——猜一个上游词汇（clear/logout）等于给钩子看假话。
        """
        if interrupts:
            return "waiting_approval"
        status = str((result or {}).get("status") or "")
        if status in ("cancelled", "paused"):
            return "interrupted"
        return "completed"

    async def _run_expansion_hooks(self, scope: _RunScope) -> ExpansionOutcome:
        """提交路径的展开钩子。处置与载荷见 :func:`run_expansion_hooks`。

        插话（steer）路径在 `graph.drain_steer` 里跑同一份，两处不该分叉。
        """
        return await run_expansion_hooks(
            scope.session.hooks, scope.recorder, scope.expansion, node="receive_task"
        )

    async def _submit_prompt(self, scope: _RunScope) -> _PromptDecision:
        """提交那一刻的四道决定：展开钩子、提交钩子、抑制原文、否决提交（#123）。

        对标 Claude Code 的 `UserPromptExpansion` / `UserPromptSubmit`：钩子给的
        `additionalContext` 过去只进审计、正文被静默丢掉，`suppressOriginalPrompt`
        这个键我们连读都不读。现在四道都真的作用在"送进模型的那一份"上，
        而 `tasks.goal` 里的原文一个字都不动——抑制的是上下文，不是账。

        展开钩子排在提交钩子之前，与上游同次序：它否决的是"这次展开"，提交钩子
        否决的是"这次提交"，两条否决各说各的事，先展开后提交。
        """
        session, recorder = scope.session, scope.recorder
        goal = session.goal or ""
        expansion = await self._run_expansion_hooks(scope)
        if expansion.blocked:
            return _PromptDecision(blocked=True, reason=expansion.reason, goal_for_model=goal)
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
                    "original_sha": prompt_sha(goal),
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
                    "original_sha": prompt_sha(goal),
                    "exit_code": asking.exit_code,
                    "sources": [mask_path(item.command)[:120] for item in results if item.asks],
                },
            )
            return _PromptDecision(blocked=True, reason=reason, goal_for_model=goal)
        # 钩子只能收紧：任何一条要求"原文不进上下文"就抑制；替代正文按配置顺序拼接。
        # 展开钩子的补段在前（说的是这次展开），提交钩子的在后。
        if expansion.notes:
            # 展开钩子补的上下文单独记一条：`hook.prompt_injected` 是"模型上下文里
            # 有几段钩子文本"的合计，sources 混在一起分不清哪段来自哪个事件。
            await recorder.add(
                "hook.context_injected",
                "receive_task",
                {
                    "event": "user_prompt_expansion",
                    "count": len(expansion.notes),
                    "chars": sum(len(note) for note in expansion.notes),
                    "sources": expansion.sources,
                },
            )
        notes = [*expansion.notes]
        notes.extend(item.additional_context.strip() for item in results)
        notes = [note for note in notes if note]
        replacement = "\n\n".join(notes)
        sources = [mask_path(item.command)[:120] for item in results]
        if expansion.notes:
            sources = [*sources, *expansion.sources]
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
                    "original_sha": prompt_sha(goal),
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
                "original_sha": prompt_sha(goal),
                "replacement_chars": len(replacement),
                "sources": sources,
            },
        )
        return _PromptDecision(
            goal_for_model="（本节由提交钩子改写，用户原始提交未进入上下文）\n" + replacement,
            suppressed=True,
        )

    async def _dispatch_command_as_subagent(
        self, scope: _RunScope, prompt: _PromptDecision
    ) -> dict | None:
        """斜杠命令声明了 ``context: fork`` 时，把正文派给子代理执行。

        对齐上游 ``vQt`` / ``EQt``：``context == "fork"`` 是派发闸门，``agent``
        只是"fork 时用哪个子代理类型"（缺省 general-purpose），``background``
        只是"fork 时前台等还是后台跑"。没有 ``context: fork`` 时，即使写了
        ``agent`` / ``background`` 也不派发——按常规路径继续。

        返回证据包 dict 时表示已派发（调用方应直接返回、不再走 ``graph.ainvoke``）；
        返回 None 表示没声明 fork，按常规路径继续。

        未知 agent 类型 fail-closed：宁可明确报错，也不静默降级到常规注入。
        """
        expansion = scope.expansion
        if not expansion:
            return None
        if str(expansion.get("context") or "").strip() != "fork":
            return None
        command_agent = str(expansion.get("agent") or "").strip()
        command_background = bool(expansion.get("background") or False)

        request = SubAgentRequest(
            goal=prompt.goal_for_model or "",
            max_steps=DEFAULT_STEPS,
            label=f"command:{str(expansion.get('command_name') or '')}",
        )
        definition, error = _resolve_agent(scope.session.context, command_agent)
        if error:
            await self._finish(
                scope, "failed", summary="", reason=error, steps=0
            )
            return await build_evidence(scope.store, scope.task_id)
        _apply_definition(request, definition)

        await scope.recorder.add(
            "command.dispatched",
            "receive_task",
            {
                "command": str(expansion.get("command_name") or ""),
                "agent": request.agent_name,
                "background": command_background,
                "goal_chars": len(request.goal),
            },
        )

        if command_background:
            agent_id, error_result = await scope.subagents.spawn_background(request)
            if error_result is not None:
                await self._finish(
                    scope,
                    "failed",
                    summary="",
                    reason=error_result.error or "后台子代理启动失败",
                    steps=0,
                )
                return await build_evidence(scope.store, scope.task_id)
            await self._finish(
                scope,
                "succeeded",
                summary=f"已在后台派出子代理 {agent_id} 执行命令。",
                reason="background_dispatched",
                steps=0,
            )
            return await build_evidence(scope.store, scope.task_id)

        result = await scope.subagents(request)
        if not result.ok:
            await self._finish(
                scope,
                "failed",
                summary="",
                reason=result.error or "子代理执行命令失败",
                steps=result.steps,
            )
            return await build_evidence(scope.store, scope.task_id)
        await self._finish(
            scope,
            "succeeded",
            summary=result.summary,
            reason="command_subagent_succeeded",
            steps=result.steps,
        )
        return await build_evidence(scope.store, scope.task_id)

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
            # 这条事件在 _finish 之前写，而 _finish 看到已有终态就跳过自己的那条，
            # 所以终态码必须在这里就带上——否则任务被取消后，事件流里留下的
            # 那条恰恰是没有码的那个。载荷与另外三处共用同一个函数。
            await recorder.add(
                "task.cancelled",
                "receive_task",
                cancelled_payload("任务已取消，未执行任何工具"),
            )
            await self._finish(scope, "cancelled", summary="", reason="任务已取消", steps=0)
            return await build_evidence(scope.store, request.task_id)
        await self._set_status(scope, "running")
        # 提交钩子在这一刻决定"这条正文进不进模型"（#123）。拦下时连 task.started 都不发：
        # 它从未开始跑，先念一句"已开始"再念一句"失败"是给时间线作假。
        prompt = await self._submit_prompt(scope)
        if prompt.blocked:
            await self._finish(
                scope,
                "failed",
                summary="",
                reason=f"提交被钩子拦下：{prompt.reason}",
                steps=0,
                # 钩子拦下是配置事实，由这里声明，不让终态码去中文里猜。
                extra={"terminal_code": "hook_blocked"},
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
                "scenario": scope.scenario_key,
                # 场景策略是执行事实，不只写在提示词里：预算/规划口径都按它跑。
                "scenario_policy": {
                    "planning": resolve_scenario(scope.scenario_key).policy.planning,
                    "verification": resolve_scenario(scope.scenario_key).policy.verification,
                    "budget_scale": resolve_scenario(scope.scenario_key).policy.budget_scale,
                },
                # 派发快照（v162）：执行侧按它收紧，事后审计读它——不再靠场景配置反推。
                "dispatch": dict(request.dispatch or {}),
                "readonly": bool(request.readonly) or bool(request.list_only),
                "list_only": bool(request.list_only),
                "memory_opt_in": bool(request.memory_opt_in),
                "max_steps": session.max_steps,
                "permission_mode": session.permission_mode,
                "permissions": session.permissions.snapshot(),
                # 档位是任务级配置：审批恢复与事后审计都读这一条（requested 优先）。
                "sandbox": scope.sandbox,
                # 任务级隔离（J3）：副本模式、包名、容量上限与基线错误。空表示没开。
                "isolation": scope.isolation.as_dict() if scope.isolation is not None else {},
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
        # 命令侧 ``context: fork`` 派发（#312）：声明了 fork 的斜杠命令不再
        # 注入主代理循环，而是派给子代理执行。放在 task.started 之后、会话钩子
        # 之前：任务已开始，但主代理会话没启动（session_start 不触发）。
        dispatched = await self._dispatch_command_as_subagent(scope, prompt)
        if dispatched is not None:
            return dispatched
        # 会话生命周期钩子：对标 Codex 的 session_start。
        # user_prompt_submit 不在这里——它在 task.started 之前就跑完了（#123），
        # 因为它的决定是"这条正文进不进模型"，晚一步就已经进了。
        start_hook_results = await self._fire_hooks(
            scope,
            "session_start",
            {
                "task_id": request.task_id,
                "thread_id": thread_id,
                "workspace": mask_path(session.context.root),
                # 上游按 source 匹配（startup / resume / clear / compact）：钩子作者
                # 想"只在续跑时装东西"靠的就是这一位，不能永远发同一个值。
                "source": "startup",
                "model": request.adapter.config.model,
            },
            target="startup",
        )
        # 上游把 SessionStart 的 additionalContext 作为 attachment 放进会话，
        # exit 2 则单独渲染成"钩子报错"提示。两种用途共用一个字段时必须分开：
        # 把报错正文当上下文注入，等于让一条失败变成模型的作业指令。
        session_hook_notes = usable_context(start_hook_results)
        start_hook_errors = non_blocking_errors(start_hook_results)
        if session_hook_notes:
            await recorder.add(
                "hook.context_injected",
                "receive_task",
                {
                    "event": "session_start",
                    "count": len(session_hook_notes),
                    "chars": sum(len(note) for note in session_hook_notes),
                },
            )
        if start_hook_errors:
            await recorder.add(
                "hook.non_blocking_error",
                "receive_task",
                {
                    "event": "session_start",
                    "count": len(start_hook_errors),
                    "reasons": [
                        (item.additional_context or item.reason or "钩子以退出码 2 结束")[:300]
                        for item in start_hook_errors
                    ],
                },
            )
        # SessionStart 钩子输出四字段（#304，对齐上游 Sn()）：
        # sessionTitle → 更新会话标题；initialUserMessage → 注入首条 user 消息；
        # watchPaths → 写 .blank/context/watch.json；reloadSkills → 重扫 Skill 目录。
        # 四字段各自有独立消费端，互不影响：一个钩子只给其中一两个也照常生效。
        session_title = next(
            (item.session_title for item in start_hook_results if item.session_title), ""
        )
        initial_user_message = next(
            (
                item.initial_user_message
                for item in start_hook_results
                if item.initial_user_message
            ),
            "",
        )
        watch_paths = [
            path for item in start_hook_results for path in item.watch_paths
        ]
        reload_skills = any(item.reload_skills for item in start_hook_results)

        if session_title:
            async with scope.store() as db:
                # thread_id 是检查点格式（task:<id>:attempt:<n>），Thread.id 是 UUID，
                # 两者不是同一个标识。必须经 Task.thread_id 这一 FK 才能查到 Thread 行。
                task = await db.get(Task, request.task_id)
                thread = (
                    await db.get(Thread, task.thread_id)
                    if task is not None and task.thread_id
                    else None
                )
                if thread is not None:
                    thread.title = session_title
                    await db.commit()
            await recorder.add(
                "hook.session_title",
                "receive_task",
                {"event": "session_start", "title": session_title},
            )

        if watch_paths:
            watch_dir = Path(session.context.root) / ".blank" / "context"
            watch_dir.mkdir(parents=True, exist_ok=True)
            (watch_dir / "watch.json").write_text(
                json.dumps(watch_paths, ensure_ascii=False),
                encoding="utf-8",
            )
            await recorder.add(
                "hook.watch_paths",
                "receive_task",
                {"event": "session_start", "count": len(watch_paths)},
            )

        if reload_skills:
            root = Path(session.context.root)
            disk_skills = discover_skills(root)
            disk_commands = discover_commands(root)
            dropped_fallbacks: list[str] = []
            reloaded = SkillCatalog(
                merge_skill_entries(
                    request.skill_catalog,
                    [item.as_entry() for item in disk_skills.entries.values()],
                    [item.as_entry() for item in disk_commands.entries.values()],
                    dropped=dropped_fallbacks,
                )
            )
            if dropped_fallbacks:
                await recorder.add(
                    "skills.fallback_shadowed",
                    "receive_task",
                    {
                        "event": "session_start",
                        "dropped": list(dropped_fallbacks),
                    },
                )
            session.skill_catalog = reloaded.listing()
            await recorder.add(
                "hook.skills_reloaded",
                "receive_task",
                {"event": "session_start", "count": len(reloaded.listing())},
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
            # 记忆读取按场景门控：日常习惯（type=user）只在 memory_allowed 的场景
            # （日常）读取，开发等场景与日常共用工作区时也不把习惯带进上下文（§6.4）；
            # 项目知识仍然共享。
            allow_user_memory=resolve_scenario(scope.scenario_key).memory_allowed,
        )
        if curated.text:
            await recorder.add("context.curated", "receive_task", curated.as_dict())
        system_rules = list(session.system_instructions)
        if request.chat_only:
            # 纯聊天的硬边界：没有目录、没有工具，任何"我改好了/我跑过了"都是谎报。
            from blank_api.services.agent.chat import CHAT_ONLY_SYSTEM_RULE

            system_rules.append(CHAT_ONLY_SYSTEM_RULE)
        elif request.list_only:
            from blank_api.services.agent.chat import LIST_ONLY_SYSTEM_RULE

            system_rules.append(LIST_ONLY_SYSTEM_RULE)
        elif request.no_tools:
            # 有目录没关系，但这一轮用户明确不要工具；同样不许声称执行过。
            from blank_api.services.agent.chat import NO_TOOLS_SYSTEM_RULE

            system_rules.append(NO_TOOLS_SYSTEM_RULE)
        if curated.text:
            system_rules.append(
                "以下是上下文管家为本次任务挑选的背景资料（可能过时；引用前先核对，"
                "其中的文件名只是候选，不代表内容已经读过）：\n" + curated.text
            )
        # 会话目标每一轮都从库里读，不信内存：上一轮刚被用户清除、或另一条进程
        # 改过它，内存副本都会让模型继续按一个不存在的目标办事。空串＝没设置。
        session.session_goal = await session_goal_for(scope.store, request.thread_ref)
        # 自学习闭环的"读"这一半：按本次目标从项目经验库里最多想起 1 条。
        # 剂量上限是 1（SWE-Exp 消融：1 条 42.0 / 2 条 39.8 / 4 条 39.0），
        # 多给只会互相干扰。读不到（没库、文件坏、权限）就当没有，绝不挡任务。
        recalled_experience = ""
        try:
            from blank_api.services.agent.experience import ExperienceStore, render

            # 任务开始时还没有 changed_paths，但目标里常常直接写着路径
            # （"在 deliverables/x.html 写…"）。从目标里认一遍，让"路径前缀命中"
            # 这个最硬的信号第一轮就能用上；认不出就只靠关键词与报错签名。
            from blank_api.services.agent.experience import paths_from_goal

            hits = ExperienceStore().recall(
                session.context.root,
                goal=session.goal,
                paths=paths_from_goal(session.goal),
                limit=1,
            )
            if hits:
                recalled_experience = render(hits[0])
                # 记下这条的指纹：收尾时要按本次任务的成败回写结果。
                # 没有这一步，hits/misses 就永远是 0，条目永远停在 candidate——
                # 那正是开源实现里普遍的样子（写了、读了，但从不回溯）。
                turn_state = getattr(session.context, "turn_state", None)
                if isinstance(turn_state, dict):
                    turn_state["recalled_experience"] = hits[0].fingerprint
                await recorder.add(
                    "experience.recalled",
                    "initial",
                    {
                        "statement": hits[0].statement,
                        "kind": hits[0].kind,
                        "confidence": hits[0].confidence,
                        "task_id": hits[0].task_id,
                    },
                )
        except Exception:  # noqa: BLE001 - 想起一条经验失败，不该让任务起不来
            recalled_experience = ""

        # 使用者模型：这里**只读不写**。
        #
        # 第一版在这里顺手从任务描述里学，拿 111 个真实任务跑出来是灾难：任务描述
        # 写的是"这一次要做什么"，不是"他一贯怎么做"。它把冒烟脚本里的
        # "必须用 run_shell 执行 echo smoke-ok" 学了进去，还因为同一段脚本跑了
        # 26 遍而晋级成"一贯要求"。
        #
        # 所以写入分两条路：驳回反馈（他明说的，当场记，两次即生效）、
        # 任务描述（`scripts/persona.py --harvest` 显式扫，只出候选，要人确认）。
        persona_block = ""
        try:
            from blank_api.services.agent.persona import (
                PersonaStore,
                injectable,
                standards_of,
            )

            persona = PersonaStore()
            persona_block = persona.persona_block(session.context.root)

            # 追问：同一个 thread 的第二轮及以后，用户那句话本身就是"上次哪里不对"，
            # 是"工序"最主要的来源。任务描述（第一轮）不能学，因为它写的是这一次要
            # 做什么；追问不一样，它天然是纠正。
            if history:
                persona.learn_from_text(
                    session.context.root,
                    session.goal,
                    task_id=request.task_id,
                    source="followup",
                    scope_ref=str(session.context.root),
                )

            entries = persona.load_all(session.context.root)
            used = injectable(entries, scope_ref=str(session.context.root))
            if used or standards_of(entries):
                await recorder.add(
                    "persona.recalled",
                    "initial",
                    {
                        "rules": [item.rule[:120] for item in used],
                        "standards": standards_of(entries),
                    },
                )
        except Exception:  # noqa: BLE001 - 学人失败不该让任务起不来
            persona_block = ""

        # 开局环境预检：把「这台机器有什么解释器、有没有 pip/node/git」一次问清，
        # 放进系统提示。真实台账里模型为找可装包的 Python 烧掉了十条命令，全是
        # 信息缺失造成的空转；预检每条进程 3 秒封顶，失败也不影响任务开跑。
        environment_block = ""
        if not getattr(session, "no_tools", False):
            try:
                from blank_api.services.agent.env_profile import profile_async, render_block

                shell_snapshot = getattr(session, "shell", None)
                shell_env = shell_snapshot.env_for() if shell_snapshot is not None else None
                if shell_env is None:
                    # 没有环境快照时不要探测：空 PATH 下会把「没装」写成事实。
                    environment_block = ""
                else:
                    env_info = await profile_async(str(session.context.root), shell_env)
                    environment_block = render_block(env_info)
                    await recorder.add("environment.profiled", "initial", env_info)
            except Exception:  # noqa: BLE001 - 预检失败不该让任务起不来
                log.warning("环境预检失败 task=%s", request.task_id, exc_info=True)
                environment_block = ""

        state: dict[str, Any] = {
            "task_id": request.task_id,
            "messages": initial_messages(
                # 送进模型的那一份由提交钩子决定（#123）；没钩子时就是原文，一个字节都不变。
                prompt.goal_for_model or session.goal,
                session.acceptance_criteria,
                session.skill_instructions,
                session.instruction_layers,
                history=history,
                skill_catalog=session.skill_catalog,
                agent_catalog=session.agent_catalog,
                goal_spec=session.goal_spec,
                session_goal=session.session_goal,
                images=request.images,
                supports_vision=request.supports_vision,
                texts=request.texts,
                system_instructions=system_rules,
                scenario_prompt=scenario_prompt(
                    resolve_scenario(request.scenario),
                    memory_opt_in=request.memory_opt_in,
                ),
                output_style=scope.output_style,
                # 两个场景各有自己的方法论：dev 用 Claude 编程纪律（coding），
                # daily 用 Codex 通用 agent 口径（daily）。两者能力相同（都能读写跑命令），
                # 差别在提示词风格，所以按**所选场景**分发，不再按首轮意图切提示词。
                # 这样日常场景里"帮我改个脚本"也走 Codex 口径，不会突然换成 Claude 纪律。
                scene="daily" if request.scenario == "daily" else "coding",
                run_id=recorder.run_id,
                hook_context=session_hook_notes,
                initial_user_message=initial_user_message,
                recalled_experience=recalled_experience,
                persona_block=persona_block,
                environment_block=environment_block,
            ),
            "plan": [],
            "steps": 0,
            "repair_rounds": 0,
            # 审查确定性范围的 baseSha：开局取一次，审批恢复沿用检查点。
            "base_sha": await asyncio.to_thread(_git_base_sha, str(session.context.root)),
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
            # 上游用 StopFailure 顶掉 Stop：这一轮是**因为 API 错误**结束的，
            # 钩子需要知道是"被拦下"还是"模型挂了"，两者的处置完全不同。
            await self._fire_stop_failure(scope, exc)
            await self._close_hooks(scope)
            raise ApiError(code=exc.code, message=exc.message, status_code=502) from None
        except GraphInterrupt as exc:
            result, interrupts = {}, list(exc.args or [])
        else:
            interrupts = list(result.get("__interrupt__") or [])
        # 会话收尾钩子：run 与 resume 两条路径都要发
        end_reason = self._session_end_reason(result, interrupts)
        await self._fire_hooks(
            scope,
            "session_end",
            {
                "task_id": request.task_id,
                "thread_id": scope.thread_id,
                "interrupts": len(interrupts),
                "reason": end_reason,
            },
            target=end_reason,
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
        # 空串＝这次恢复没指定模式，读回任务首次运行记下的那一档。
        # 判据只能是 request.permission_mode 本身：审批恢复时前端常常不重发模式，
        # 拿默认值顶上就等于把 workspace 静默换成 off（或反之）。
        if not request.permission_mode:
            info = await self._stored_info(request.task_id)
            request.permission_mode = info["mode"] or MODE_DEFAULT
            request.sandbox_tier = info["tier"]
        else:
            await self._guard_tier_change(request)
        scope = await self._prepare(request, thread_id)
        await self._rehydrate(request.task_id, scope.session.permissions)
        await self._set_status(scope, "running")
        await self._revive_goal(request.task_id)
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
        if crash_recovery:
            # 暂停后恢复：检查点里的 status 还停在 paused，直接 ainvoke(None) 会
            # 立刻按 paused 再收一次尾——用户点了恢复却什么都没发生。先把检查点
            # 里的状态改回 running，再接着走。读不到状态就照旧继续（老检查点）。
            try:
                snapshot = await scope.graph.aget_state(scope.config)
                values = getattr(snapshot, "values", None) or {}
                if isinstance(values, dict) and values.get("status") == "paused":
                    await scope.graph.aupdate_state(scope.config, {"status": "running"})
            except Exception:  # noqa: BLE001 - 状态读写失败不该让恢复直接崩
                log.warning("恢复前重置 paused 状态失败 task=%s", request.task_id, exc_info=True)
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
            # 续跑路径同样：恢复后模型又挂了，也要按"API 错误终结"发 StopFailure。
            await self._fire_stop_failure(scope, exc)
            await self._close_hooks(scope)
            raise ApiError(code=exc.code, message=exc.message, status_code=502) from None
        except GraphInterrupt as exc:
            result, interrupts = {}, list(exc.args or [])
        else:
            interrupts = list(result.get("__interrupt__") or [])
        # 会话收尾钩子：run 与 resume 两条路径都要发
        end_reason = self._session_end_reason(result, interrupts)
        await self._fire_hooks(
            scope,
            "session_end",
            {
                "task_id": request.task_id,
                "thread_id": scope.thread_id,
                "interrupts": len(interrupts),
                "reason": end_reason,
            },
            target=end_reason,
        )
        await self._close_hooks(scope)
        return await self._finalize(request, scope, result, interrupts)

    async def _revive_goal(self, task_id: str) -> None:
        """审批/提问恢复后把目标从 waiting_approval 拉回 executing。

        真机：用户答完问题，任务已经继续跑，状态条却整轮写着「等你批准」——
        目标的 waiting_approval 是中断那一刻写下的，恢复路径没人改回来。
        这里只改 waiting_approval，别的状态一律不动。
        """
        try:
            async with self.session_factory() as session:
                task = await session.get(Task, task_id)
                if task is None:
                    return
                goal_id = _goal_id_of(task.goal_spec)
                if not goal_id:
                    return
                goal = await session.get(Goal, goal_id)
                if goal is None or str(goal.status or "") != "waiting_approval":
                    return
                GoalTracker.transition(goal, "executing", next_action="")
                await session.commit()
        except Exception:  # noqa: BLE001 - 状态条一致性不该让恢复本身失败
            log.warning("恢复时目标状态回写失败 task=%s", task_id, exc_info=True)

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
                # 等审批时要先把计划存下来：用户回来看到的第一屏就是"做到哪了"，
                # 而这一步往往停在计划刚更新完、还没往下跑的位置。
                plan_steps=result.get("plan_steps"),
                milestones=result.get("milestones"),
                goal_spec=result.get("goal_spec"),
                extra={"thread_id": scope.thread_id, "interrupt": payload},
            )
            return await build_evidence(scope.store, task_id)

        validation = result.get("validation") or {}
        # v165 §6 独立 Verifier：只读磁盘事实/工具结果/测试结论/交付文本，不调模型、
        # 不看 Agent 自述。放在终态集中点算，才能同时拿到交付文本与验证结果。
        verdict = verify_facts(
            goal_spec=result.get("goal_spec") or {},
            plan=result.get("plan_manifest"),
            tool_results=result.get("tool_results") or [],
            validation=validation,
            changed_paths=_changed_paths(result.get("tool_results") or []),
            report=str(result.get("summary") or ""),
            readonly=bool(result.get("readonly")),
            direct_answer=bool(result.get("direct_answer")),
            budget_exhausted=bool(result.get("budget_exhausted")),
            # 判据里的 path_exists / file_contains 要读磁盘；拿不到就不判（记 unknown）。
            root=getattr(scope.session.context, "root", None),
        )
        await scope.recorder.add(
            "verification.verdict",
            "complete",
            {
                "verdict": verdict["verdict"],
                "passed": verdict["passed"],
                "blocking": verdict["blocking"],
                "checks": verdict["checks"],
                "findings": verdict["findings"],
                "evidence": verdict["evidence"],
                "goal_id": str((result.get("goal_spec") or {}).get("goal_id") or ""),
            },
        )
        status = resolve_status({**result, "verdict": verdict})
        reason = ""
        if status == "failed":
            reason = verdict_failure_reason(verdict) or _failure_reason(validation)
        elif status == "cancelled":
            reason = "任务已取消"
        elif status == "paused":
            reason = "用户暂停了任务"
        unverified = validation_unverified(validation) if status == "succeeded" else ""
        summary = str(result.get("summary") or "")
        if unverified:
            # 状态可以是成功（交付确实在、事实校验也过了），但绝不能让它看起来
            # 像"验证过了"。模型自己写的那句"自动化校验未通过：npm test"指的是
            # 同一条没跑完的命令，留着会自相矛盾，所以整句如实改写。
            summary = summary.split("（自动化校验")[0].rstrip() + "\n" + UNVERIFIED_NOTE.format(reason=unverified)
        # 交付**是否符合他一贯的标准**——必须在落状态之前算：开启强制时它要改交付文本，
        # 落完状态再算就只能事后记账了。判据只读事实，与 done_when 同一条纪律
        # （没改文件记 unverifiable，不记失败）。
        standard_results: list[dict] = []
        try:
            from blank_api.services.agent.persona import (
                PersonaStore,
                conformance,
                standards_of,
            )

            persona_store = PersonaStore()
            standard_keys = standards_of(persona_store.load_all(scope.session.context.root))
            if standard_keys:
                rows = [item for item in (result.get("tool_results") or []) if isinstance(item, dict)]
                standard_results = conformance(
                    standard_keys,
                    {
                        "changed_paths": _changed_paths(rows),
                        "validation": validation,
                        "report": summary,
                        "failures": len(
                            [
                                item
                                for item in rows
                                if item.get("ok") is False
                                or str(item.get("status") or "") in ("failed", "error", "timeout")
                            ]
                        ),
                    },
                )
                unmet = [item for item in standard_results if item["status"] == "failed"]
                if unmet and _standard_enforced():
                    # 不许"看起来完成了"。和 unverified 同一处手法：交付文本自己说出来。
                    summary = summary.rstrip() + "\n\n" + STANDARD_UNMET_NOTE.format(
                        items="；".join(item["text"] for item in unmet[:4])
                    )
                await scope.recorder.add(
                    "standard.conformance",
                    "complete",
                    {
                        "standards": standard_keys,
                        "results": standard_results,
                        "passed": sum(
                            1 for item in standard_results if item["status"] == "passed"
                        ),
                        "failed": len(unmet),
                        "unverifiable": sum(
                            1 for item in standard_results if item["status"] == "unverifiable"
                        ),
                        "enforced": _standard_enforced(),
                    },
                )
        except Exception:  # noqa: BLE001 - 核对失败不该让任务结论变掉
            standard_results = []

        await self._finish(
            scope,
            status,
            summary=summary,
            reason=reason,
            acceptance=result.get("acceptance_results"),
            steps=int(result.get("steps") or 0),
            plan_steps=result.get("plan_steps"),
            milestones=result.get("milestones"),
            goal_spec=result.get("goal_spec"),
            extra={
                "usage": result.get("usage") or {},
                "validation": validation,
                "unverified": unverified,
                "verdict": {
                    "verdict": verdict["verdict"],
                    "passed": verdict["passed"],
                    "blocking": verdict["blocking"],
                    "findings": verdict["findings"],
                    # 机器码跟着 verdict 走：终态码由它派生，界面也读它。
                    # 只带 findings 的话，下游又得回去读中文。
                    "codes": verdict.get("codes") or [],
                },
                "thread_id": scope.thread_id,
                "context_tokens": scope.session.context_tokens,
                "steer": scope.session.steer_metrics,
            },
        )
        # 回写"这次用到的经验结果如何"——成功失败都算，因为失败才是需要降权的信号。
        recalled_id = str(
            (getattr(scope.session.context, "turn_state", {}) or {}).get("recalled_experience") or ""
        )
        if recalled_id:
            try:
                from blank_api.services.agent.experience import ExperienceStore

                new_status = ExperienceStore().record_outcome(
                    scope.session.context.root, recalled_id, success=(status == "succeeded")
                )
                if new_status:
                    await scope.recorder.add(
                        "experience.outcome",
                        "complete",
                        {"status": new_status, "success": status == "succeeded"},
                    )
            except Exception:  # noqa: BLE001 - 回写失败不影响任务结论
                pass

        if status == "succeeded":
            if request.index_summary:
                await self._index_summary(scope, request)
            # 「日常 + 未选记住」是用户明说“这次别记住”：自动固化也一并跳过，
            # 否则开关只挡住了 memory_write，任务结论照样落进项目记忆。
            if not (request.scenario == "daily" and not request.memory_opt_in):
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
        """任务收尾时抽 0–2 条可复用经验，写进项目经验库。

        **不再写"整任务总结"**。原来的实现把 summary / 验收 / 验证命令拼成一段
        散文追加进 `MemoryStore`，实测后果是：57 条里 45 条是冒烟流水账，同一条事实
        重复 13 次，还有两条互相矛盾的结论并存。而 2604.27003 / 2608.20274 的结论是
        更坏的——**整任务总结会负迁移，低于"完全没有记忆"的基线**。

        现在走 `experience.extract()`：确定性规则、0–2 条、必须带证据指针。
        "这次学到 0 条"是正常结果——冒烟测试、纯问答本来就没什么可学的。
        """
        session = scope.session
        try:
            from blank_api.services.agent.experience import ExperienceStore, extract

            rows = [item for item in (result.get("tool_results") or []) if isinstance(item, dict)]
            changed = [
                str((row.get("data") or {}).get("path") or "")
                for row in rows
                if row.get("ok") and str((row.get("data") or {}).get("change_type") or "")
            ]
            failures = [
                row
                for row in rows
                if row.get("ok") is False
                or str(row.get("status") or "") in ("failed", "error", "timeout")
            ]
            learned = extract(
                task_id=scope.task_id,
                goal=session.goal,
                changed_paths=[path for path in changed if path],
                validation=result.get("validation") or {},
                failures=failures,
                at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            store = ExperienceStore()
            actions = [store.record(session.context.root, entry)[0] for entry in learned]
        except Exception as exc:  # noqa: BLE001 - 学到东西失败，不该让已经成功的任务变成失败
            await scope.recorder.add(
                "experience.failed", "complete", {"error": str(exc)[:300]}
            )
            return
        await scope.recorder.add(
            "experience.learned",
            "complete",
            {"count": len(learned), "actions": actions, "statements": [e.statement[:160] for e in learned]},
        )


        # 他的标准沉淀成 Skill 提案——一句话进提示只能影响"注意什么"，
        # 真正"按他的方式做"是程序性的，得有个带步骤的正文。
        # 走既有的 proposed 通道：人点采用才生效，因为展开是我们的理解。
        # 幂等（同名已存在就跳过），否则每跑一个任务就提一次。
        try:
            from blank_api.services.agent.persona import PersonaStore
            from blank_api.services.agent.skillsmith import drafts_for, stage as stage_skills

            drafts = drafts_for(PersonaStore(), session.context.root)
            if drafts:
                await stage_skills(scope.store, drafts, recorder=scope.recorder)
        except Exception:  # noqa: BLE001 - 沉淀失败不该让任务结论变掉
            pass


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
        plan_steps: list[dict] | None = None,
        milestones: list[dict] | None = None,
        goal_spec: dict | None = None,
        extra: dict | None = None,
    ) -> None:
        # 取消轮询先收：它比这一轮活得久，不收就会在任务结束后继续读库。
        # 审批中断也收——恢复时会按新的 scope 重新起一个，旧的那个已经没有
        # 正在跑的命令需要叫停了。
        watch = getattr(scope, "cancel_watch", None)
        if watch is not None and not watch.done():
            watch.cancel()
        # 任务走到终点时仍在跑的后台子代理必须收走：否则任务已经显示"完成"，
        # 它们还在调用模型、写事件，账目与界面从此对不上。等待审批不是一个终点——
        # 恢复后还要用同一个句柄取结论，所以那一档不收。
        # 后台命令同理：进程还活着、日志还占着盘，也得在这里收走。
        # LSP 不在这一档例外里：语言服务器不保存用户可续跑的会话状态，等审批时让它
        # 占着内存没有收益；恢复后的下一回合会按需重拉一个。
        lsp_manager = getattr(getattr(scope.session, "context", None), "lsp", None)
        if lsp_manager is not None:
            await lsp_manager.close()
        if status != "waiting_approval":
            await shell_tasks.aclose(scope.task_id, reason=f"task_{status}")
            # 工作流的脚本进程与它派出的子代理都挂在任务上：任务一收尾就没人取结论了，
            # 必须在这里取消，否则进程和模型调用会陪着一条已完成的任务继续烧。
            await workflow_module.aclose(scope.task_id, reason=f"task_{status}")
            # REPL 会话同理：Node 进程和它清空前的状态都挂在任务上，任务收尾后没人再调用。
            await repl_module.aclose(scope.task_id, reason=f"task_{status}")
            # worktree 会话只摘记录，工作树留在盘上：删不删由 exit_worktree 决定，
            # 任务收尾不该替用户丢掉还没合并的工作。
            worktree_session.release(scope.task_id)
        if status != "waiting_approval" and scope.subagents is not None:
            await scope.subagents.aclose(scope.task_id, reason=f"task_{status}")
        # 任务级隔离根的收尾：成功先合并，再释放副本/卷/包；失败与取消只释放。
        # 暂停与等待审批不释放——恢复后要在同一个根里接着干，换新克隆会丢改动。
        if (
            getattr(scope, "isolation", None) is not None
            and not getattr(scope, "isolation_settled", False)
            and status in ("succeeded", "failed", "cancelled")
        ):
            scope.isolation_settled = True
            await self._settle_isolation(scope, status)
        # harness 自己的状态（.blank_backups、.blank/scratchpad）不是交付物。收尾这条
        # 路必须独立走一遍：超时/取消时根本到不了 summarize_result，只靠那里清场会漏。
        if status in ("succeeded", "failed", "cancelled"):
            await self._externalize_workspace_state(scope, status)
        async with scope.store() as session:
            task = await session.get(Task, scope.task_id)
            if task is None:
                return
            task.status = status
            # 「替你定的」从事件流重放而不是读内存：用户答完之后是另一个回合，
            # 新建的 session 里那本账已经不在了，事件流才是跨回合的唯一事实来源。
            answered_events = (
                (
                    await session.execute(
                        select(AgentEvent).where(
                            AgentEvent.task_id == scope.task_id,
                            AgentEvent.type == "question.answered",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assumptions = assumption_ledger(list(answered_events))
            delivered = (
                _with_delivery_notes(summary, scope.session, {}, assumptions=assumptions)
                if (summary or assumptions)
                else ""
            )
            if delivered:
                task.result_summary = delivered
            task.failure_reason = reason if status != "succeeded" else ""
            if acceptance is not None:
                task.acceptance_results = acceptance
            if steps is not None:
                task.steps_used = steps
            # 计划落库。之前只活在图状态里，任务一结束就沉底：新任务从空白纸开始，
            # 同一会话下一轮问"做到哪了"也答不出来。
            if plan_steps is not None:
                # 只在内容真的变了才加版本号——每轮收尾都 +1 会让这个数变成"跑了几次"，
                # 而不是"计划改过几次"。
                if _plan_signature(plan_steps) != _plan_signature(task.plan_steps or []):
                    task.plan_revision = int(task.plan_revision or 0) + 1
                task.plan_steps = plan_steps
            if milestones is not None:
                task.milestones = milestones
            if goal_spec:
                task.goal_spec = goal_spec
            # v165 §3.2：目标状态必须跟着任务终态走。Phase 1 只在控制词分支改过它，
            # 于是正常跑完一轮后目标永远停在 planned，界面看起来「还在执行中」。
            goal_id = _goal_id_of(goal_spec, task.goal_spec)
            if goal_id:
                goal = await session.get(Goal, goal_id)
                goal_status = _GOAL_STATUS_BY_TASK.get(status)
                if goal is not None and goal_status:
                    GoalTracker.transition(
                        goal,
                        goal_status,
                        next_action="" if status == "succeeded" else reason[:200],
                    )
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
        # 终态码与人话分开装：`reason` 保持原样（已有文档、老数据、界面读它），
        # `terminal` 是本轮新增的机读结构。放在 `**extra` 之后，避免调用方
        # 传进来的同名键把它覆盖掉——终态码只能由这一个函数产出。
        payload = terminal_payload(status, summary, reason, extra)
        routing = _routing_summary(scope.session)
        if routing:
            payload["routing"] = routing
        # 取消端点会当场写一条 task.cancelled：界面靠它立刻解锁，不用等运行时
        # 走到收尾（模型调用挂着时那一步可能永远不到）。这里再写一条不会让界面
        # 更早解锁，只会让"这一轮结束了几次"变成需要解释的问题，所以先问一句。
        skip_terminal = status in ("succeeded", "failed", "cancelled", "paused") and await scope.recorder.has_terminal(
            # 只有收尾本身就是 paused 时，才把已有的 task.paused 当成"这一轮已结束"；
            # 恢复后跑成功时它不算数，否则完成事件会被暂停事件吞掉。
            paused_counts=(status == "paused")
        )
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

    async def _externalize_workspace_state(self, scope: _RunScope, status: str) -> None:
        """把备份与 scratchpad 移出工作区；失败只记账，不改判任务结论。"""
        from blank_api.services.agent.workspace_state import externalize

        root = getattr(getattr(scope.session, "context", None), "root", None)
        if not root:
            return
        try:
            outcome = await asyncio.to_thread(externalize, root, scope.task_id)
        except OSError as exc:
            await scope.recorder.add(
                "workspace.state_externalize_failed",
                "complete",
                {"status": status, "reason": f"{exc.__class__.__name__}: {exc}"},
            )
            return
        if outcome.get("moved"):
            await scope.recorder.add(
                "workspace.state_externalized", "complete", {"status": status, **outcome}
            )

    async def _settle_isolation(self, scope: _RunScope, status: str) -> None:
        """收任务根：成功的先合并改动，再释放副本/容量卷/包；失败与取消只释放。

        合并规则复用 `worktree.apply_changes`：同一文件被并发改过、或工作区该文件在任务
        开始后又被改过，都只交回 diff，不覆盖；删除类改动不自动执行。合并失败要留痕，
        但不改写任务结论——结论是模型与验证器的事，合并是我们自己的收尾。
        """
        from blank_api.services.sandbox import task_isolation

        root = scope.isolation
        if status == "succeeded":
            try:
                harvested = await task_isolation.harvest_async(root)
                await scope.recorder.add("isolation.harvested", "complete", harvested)
            except Exception as exc:  # noqa: BLE001 - 合并失败必须可见，但不能吞掉任务结论
                await scope.recorder.add(
                    "isolation.harvest_failed",
                    "complete",
                    {"reason": f"{exc.__class__.__name__}: {exc}"},
                )
        try:
            released = await task_isolation.release_async(root)
        except Exception as exc:  # noqa: BLE001
            await scope.recorder.add(
                "isolation.release_failed",
                "complete",
                {"reason": f"{exc.__class__.__name__}: {exc}"},
            )
            return
        event = "isolation.release_errors" if released.get("errors") else "isolation.released"
        await scope.recorder.add(event, "complete", released)
        # 收尾之后还要固化的记忆按工作区取 slug。副本已经删了，换回真实工作区——
        # 否则记忆会落在一个已不存在的路径名字下。
        scope.session.context = dc_replace(
            scope.session.context, root=Path(root.workspace), isolation_profile=""
        )


def _routing_summary(session: AgentSession) -> dict:
    router = getattr(session, "router", None)
    if router is None:
        return {}
    try:
        return router.summary()
    except Exception:  # 记账失败绝不能影响任务收尾
        return {}


def _failure_reason(validation: dict) -> str:
    """失败原因必须与证据一致。

    以前这里只有两句：`status == "error"` 说"测试未能执行"，其余一律说"测试未通过"。
    于是两种完全不同的情况被写成同一句话：本次**压根没跑**（被跳过、等审批、环境不可用）
    和**反复跑同一条必败命令被拦下**。用户看到"测试未通过"就会去翻代码，而真正该做的
    是看审批卡或看清楚自己卡在哪一步上——真实台账里那个烧掉 713 万 token 的任务，
    失败原因写的就是"测试未通过"，而它的 run_tests 一次都没真正执行。
    """
    command = str(validation.get("command") or "run_tests")
    state = str(validation.get("status") or "")
    if not validation:
        return "没有拿到测试结论，任务未完成"
    if state == "skipped":
        return f"任务未完成：{validation.get('skipped_reason') or '本次没有执行自动化校验'}"
    if state == "blocked":
        return f"反复执行同一条命令没产生新结果，已停止：{command}"
    if state == "timeout":
        # 超时 ≠ 没通过。真机轨迹里 `npm test` 是仓库根的全量后端单测（约 25 分钟），
        # 180 秒上限必然超时；写进"测试未通过"会让用户去翻一堆没问题的代码，
        # 也让修复回路去"修"一个不存在的问题。
        budget = validation.get("timeout_seconds")
        span = f"{int(float(budget))} 秒" if budget else "单次上限"
        return (
            f"测试超时（{span}内没跑完，进程已终止）：{command}——"
            "这不是测试失败，是这条命令不适合在一次调用里跑完"
        )
    if state == "error":
        return f"测试未能执行：{command}"
    return f"测试未通过：{command}"


# 任务终态 → v165 §3.2 目标状态。目标与任务同寿，但状态不是同一个：
# 一个目标可以跨多个 task，只有终态才把目标推到对应位置。
_GOAL_STATUS_BY_TASK = {
    "succeeded": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
    "paused": "paused",
    "waiting_approval": "waiting_approval",
}


def _goal_id_of(*specs: object) -> str:
    for spec in specs:
        if isinstance(spec, dict):
            value = str(spec.get("goal_id") or "").strip()
            if value:
                return value
    return ""


def _turn_state_from_expansion(expansion: dict | None) -> dict:
    """从斜杠命令展开载荷里提取工具约束，构造 turn_state 的初始值。

    expansion 里的 ``allowed_tools`` / ``disallowed_tools`` 是 expand_user_input
    从命令 frontmatter 解析来的。没有声明时返回空 dict——运行时不做任何收窄，
    与"没有约束"等价。
    """
    if not isinstance(expansion, dict):
        return {}
    state: dict = {}
    allowed = list(expansion.get("allowed_tools") or ())
    disallowed = list(expansion.get("disallowed_tools") or ())
    if allowed:
        state["skill_allowed_tools"] = tuple(allowed)
    if disallowed:
        state["skill_disallowed_tools"] = tuple(disallowed)
    return state


# 「验证没跑成」与「验证跑了但没通过」是两件事，混在一起会判死好交付。
#
# 真机里撞过三次：run_tests 按仓库根的 test 脚本跑了 `npm test`（全仓后端单测，
# 约 25 分钟），单次上限 120-180 秒，必然超时；交付物是好的（12KB 页面、
# browser_check 零控制台错误），却因为一句「没跑完的命令」被判失败。
#
# 所以只有**明确跑了并失败**才算失败；超时、命令找不到、判定器起不来，
# 都是「我们没拿到结论」——状态可以是成功，但必须显式标注未验证。
UNVERIFIED_STATUSES = frozenset({"timeout", "error", "unavailable", "skipped"})

UNVERIFIED_NOTE = "（验证未完成：{reason}——本次结论未经自动化校验，请自行复核）"


def validation_unverified(validation: dict) -> str:
    """验证没跑成时返回原因，跑成了（无论通过与否）返回空串。"""
    if not validation:
        return ""
    status = str(validation.get("status") or "")
    if status == "timeout":
        return "验证命令超时，没拿到任何结论"
    if status in ("error", "unavailable"):
        return str(validation.get("message") or "验证命令没能执行")
    return ""


def terminal_payload(status: str, summary: str, reason: str, extra: dict | None = None) -> dict:
    """终态事件的载荷。

    抽成纯函数是为了能被直接断言：起一个完整运行时去验证"失败事件的形状"
    要花掉十几秒和一堆夹具，而这里真正该测的只是"码有没有产出、有没有被覆盖"。
    """
    return {
        "status": status,
        "reason": reason,
        "summary": summary,
        # 调用方给的键先铺，`terminal` 最后写：它只能由终端码函数产出，
        # 不能被 `extra` 里同名的键顶掉。
        # `terminal_code` 是只用来派生码的内部输入，不进载荷——事件流是给界面
        # 和老数据读的，多一个同义字段只会让"以哪个为准"变成新的分歧。
        **{k: v for k, v in (extra or {}).items() if k != "terminal_code"},
        "terminal": terminal_detail(status, reason, extra),
    }


def resolve_status(result: dict) -> str:
    if result.get("status") in ("cancelled", "paused", "failed"):
        return str(result["status"])
    # v165 §6：独立事实校验发现的硬违规（零改动伪成功、交付假称测试通过）
    # 不能算成功。判定只读事实，不看模型自述，也不受自动化模式影响。
    verdict = result.get("verdict") or {}
    if verdict.get("blocking"):
        return "failed"
    validation = result.get("validation") or {}
    # 只有「跑了并失败」才判失败；没跑成（超时/起不来）算未验证。
    if validation:
        status = str(validation.get("status") or "")
        if status and status not in UNVERIFIED_STATUSES and status != "ok":
            return "failed"
    if validation.get("exit_code") not in (None, 0):
        return "failed"
    return "succeeded"


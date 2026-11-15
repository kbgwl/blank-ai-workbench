"""Sub-agents with an isolated workspace.

The pattern both Codex and Claude Code converged on: a child agent gets a narrow
goal, a small budget and its own context window, reports back a summary, and dies.
The parent never loses its own context to the child's exploration.

Two safety rules, both deliberate:

* **No nested interrupts.** A child cannot ask the human anything. Anything that
  would need approval is simply refused, so the child must work within the
  permissions it was given. This keeps the interrupt channel single-owner.
* **No nested spawning.** The child's gateway does not include ``spawn_agent``, so
  depth is capped at one instead of relying on a counter that can drift.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blank_api.services.agent.context import ContextCompactor
from blank_api.services.agent.usage import account_call, merge_usage
from blank_api.services.agent_memory import (
    agent_memory_dir,
    agent_memory_tools,
    render_memory_section,
)
from blank_api.services.agents_defs import BUILTIN_AGENT, MAX_INSTRUCTIONS_CHARS
from blank_api.services.instructions import compose, load_layers, provenance
from blank_api.services.hooks import (
    MAX_CONTEXT,
    HookRunner,
    HookSpec,
    non_blocking_errors,
    stop_hook_block_cap,
    usable_context,
    write_transcript,
)
from blank_api.services.models.base import ChatMessage, ModelAdapter, ModelError, Usage
from blank_api.services.effort import (
    EffortPolicy,
    ThinkingConfig,
    resolve_turn_effort,
    thinking_budget_ceiling,
)
from blank_api.services.permissions import (
    MODE_ACCEPT_EDITS,
    MODE_BYPASS,
    MODE_DEFAULT,
    MODE_PLAN,
    PermissionPolicy,
)
from blank_api.services.safety import SafetyPolicy
from blank_api.services.sandbox.tiers import TierDowngrade, assert_not_looser
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.file_state import FileStateCache
from blank_api.services.tools.gateway import ToolGateway, sandbox_backend_event_field
from blank_api.services.tools.skills import (
    SkillCatalog,
    preload_skills,
    skill_loading_header,
)
from blank_api.services.worktree import (
    Worktree,
    apply_changes,
    changed_files,
    create_clone,
    create_worktree_with_hooks,
    remove_worktree,
    remove_worktree_with_hooks,
    render_diff,
)
from blank_api.services.workspace import mask_path

READ_ONLY_TOOLS = (
    "read_file",
    "list_files",
    "glob",
    "search_code",
    "git",
    "web_search",
    "web_fetch",
    # 子代理也要能按项目现成的流程干活；Skill 只是读内存里的说明，不碰工作区。
    # 没有可加载的 Skill 时下面会把这条从工具表里拿掉。
    "skill",
)
ISOLATED_TOOLS = (
    *READ_ONLY_TOOLS,
    "apply_patch",
    "edit_file",
    "write_file",
    "notebook_edit",
    "run_shell",
    "run_tests",
)
MAX_DEPTH = 1
MAX_STEPS = 8
DEFAULT_STEPS = 4
MAX_TIMEOUT = 300.0
MIN_TIMEOUT = 20.0
# 并行编排上限：四个副本已经是"分头探索"的合理规模，再多就是烧钱而不是加速
MAX_PARALLEL_AGENTS = 4
DEFAULT_CONCURRENCY = 3
# 子代理默认后台跑（对齐 Claude Code 的 Agent 工具），任务收尾前给它一个有界等待窗口：
# 这个窗口里结束的，结论能进交付；还没结束的才会被取消并点名。窗口不能无限长——
# 主代理已经写完结论，界面不该再干等十分钟。
BACKGROUND_SETTLE_SECONDS = 90.0
# 后台句柄只在本进程内有效：重启后 task_output 会如实说"找不到"，而不是伪造一个结果。
# 按任务分桶、键是 agent_id。放模块级而不是 runner 实例上，是因为审批恢复会重建
# 一个 runner——句柄若绑在实例上，恢复之后 task_output 就再也找不到它了。
_BACKGROUND: dict[str, dict[str, "_BackgroundAgent"]] = {}
# 续聊（对齐 Claude Code 的 SendMessage）：子代理跑完把它这一轮的对话留在这里，
# 按任务分桶、键是 agent_id。新的 spawn_agent 从空上下文开始，续聊则接着这一份
# 往下说——否则「刚才你查到的那条」会让它一脸茫然，用户只能把背景再讲一遍。
# 只留非隔离子代理的对话：隔离副本跑完就删，上下文还在但文件状态没了，
# 续聊会给出与磁盘不符的答案。隔离子代理的条目也留（只留标记），用来给出准确拒绝。
_CONVERSATIONS: dict[str, dict[str, dict]] = {}
# 内存护栏：一个任务里派十几个子代理、每个几十条消息，不设上限会一路涨到重启。
MAX_CONVERSATIONS = 20
MAX_CONVERSATION_MESSAGES = 60
MAX_CONVERSATION_CHARS = 8_000

SUBAGENT_SYSTEM = (
    "你是一个受限的子代理，只负责完成交给你的单个子任务。\n"
    "规则：\n"
    "1. 你无法向用户提问，也无法请求审批；需要审批的动作会被直接拒绝，请换一种做法。\n"
    "2. 少而准地调用工具，最多 {steps} 步，完成后用一段中文说明你查到了什么或改了什么。\n"
    "3. 不要尝试越出工作区，不要执行不可逆命令。"
)

# fork 子代理不是从零开始：它继承父会话到此刻为止的全部对话，所以不能套用
# SUBAGENT_SYSTEM（那会把父会话的历史整个丢在系统提示之后讲不通）。这里只追加
# 分身自己的约束，接在原系统提示末尾——仍然是一条 system，前缀缓存与供应商兼容性
# 都按原样处理。
FORK_SYSTEM = (
    "\n\n你正在以 fork 方式运行：你是主代理在这个任务里的分身，"
    "上面就是主会话到目前为止的完整对话，你继承它的工作区、工具与权限。\n"
    "规则：\n"
    "1. 你无法向用户提问，也无法请求审批；需要审批的动作会被直接拒绝，请换一种做法。\n"
    "2. 最多 {steps} 步；完成后用一段中文说明你做了什么、结论是什么。\n"
    "3. 不要越出工作区，不要执行不可逆命令。"
)
# 续聊时识别"这条系统提示里已经写过 fork 约束"：分身的第一轮把约束并进了
# 父会话的系统提示，续聊再从 _CONVERSATIONS 里取回来时不能又追加一遍。
FORK_MARKER = "你正在以 fork 方式运行"

# SubagentStart 钩子注入的附加上下文用一条独立消息承载（上游是
# ``hook_additional_context`` attachment）。标记头让续聊能认出"这份上下文已经
# 给过"，与 Skill 预加载头同一口径。
SUBAGENT_START_MARKER = "<subagent-start-hook>"

# fork 拿父会话的工具表，但这几类必须拿掉：子代理不能再派子代理（深度上限靠
# 工具表缺失成立，不靠计数器），REPL/LSP 绑定父会话的网关与进程，workflow 内部
# 也会派子代理。拿掉它们不是收窄权限——父会话本来能做这些事，只是分身不能。
FORK_EXCLUDED_TOOLS = frozenset(
    {
        "spawn_agent",
        "spawn_agents",
        "send_message",
        "task_output",
        "task_stop",
        "list_agents",
        "repl",
        "lsp",
        "workflow",
    }
)


@dataclass
class SubAgentRequest:
    goal: str
    isolate: bool = False
    max_steps: int = DEFAULT_STEPS
    timeout_seconds: float = 120.0
    label: str = ""
    # 并行编排：保留副本直到主代理决定是否合并（默认跑完即删，与单发行为一致）
    keep_worktree: bool = False
    parallel_group: str = ""
    # 自定义子代理类型（.blank/agents/*.md）。解析在工具层完成：这里只带
    # "已经选中的定义"，运行器据此收窄工具表并追加正文。
    agent_name: str = ""
    instructions: str = ""
    # None = 不限（对应定义里的 tools: "*"）；否则是允许的工具名白名单。
    allowed_tools: tuple[str, ...] | None = None
    disallowed_tools: tuple[str, ...] = ()
    # 定义里声明的 Skill 名字：派发时把正文预加载进子代理的启动消息（对齐
    # Claude 的 agent frontmatter ``skills``）。解析失败的项只告警，不阻断派发。
    skills: tuple[str, ...] = ()
    # 定义里写了、但我们不支持因此没有生效的字段（hooks/mcpServers/...）。
    # 只用于审计：绝不能悄悄吞掉，否则用户以为那条权限模式起了作用。
    agent_ignored: tuple[str, ...] = ()
    # 对齐 Claude 的 fork 子代理：继承父会话到此刻为止的完整对话与工具权限。
    # 只能前台跑——后台拿的是派发那一刻的快照，父会话之后的变化它看不见，
    # 结论回来时父会话多半已经走远了。
    fork: bool = False
    # 定义里声明的 Claude permissionMode（已规范成 default/plan/acceptEdits/
    # bypassPermissions 之一）。只用于相对派发基线收紧；fork 直接继承父权限。
    permission_mode: str = ""
    # 定义里写了 ``background: true``：上游会压过调用方的 ``run_in_background=false``。
    definition_background: bool = False
    # 定义里声明的本轮思考强度（Claude 的 ``effort``）。空串 = 定义没声明，
    # 派发时按模型封顶取兜底档。认不出的值在定义解析阶段就已经告警丢弃，
    # 到这里只会是档位词或关键词升档后的结果。
    effort: str = ""
    # 定义里声明的模型覆盖（上游 agent schema 的 ``model``）。``inherit`` 与
    # 空串都表示沿用父会话模型；字面量永远不发给供应商。fork 恒继承，忽略它。
    model: str = ""
    # 定义里声明的钩子：注册进这个子代理自己的作用域，不污染父会话。
    # ``stop`` 在注册时改写成 ``subagent_stop``；解析阶段保留原事件名。
    agent_hooks: dict[str, tuple[HookSpec, ...]] = field(default_factory=dict)
    # 定义里声明的持久记忆作用域（user/project/local）。空串 = 不声明：
    # 派发时不建目录、不注入记忆段、也不挂写入工具。
    memory_scope: str = ""
    # 定义里声明的 ``omitClaudeMd``：为 true 时不继承用户/项目指令文件。
    omit_claude_md: bool = False

    def normalized(self) -> "SubAgentRequest":
        return SubAgentRequest(
            goal=self.goal.strip()[:2_000],
            isolate=bool(self.isolate),
            max_steps=max(1, min(MAX_STEPS, int(self.max_steps or DEFAULT_STEPS))),
            timeout_seconds=max(MIN_TIMEOUT, min(MAX_TIMEOUT, float(self.timeout_seconds or 120.0))),
            label=self.label,
            keep_worktree=bool(self.keep_worktree),
            parallel_group=self.parallel_group,
            agent_name=str(self.agent_name or "").strip()[:200],
            # 定义正文在发现阶段已经截断；这里再夹一次是因为请求也可能来自
            # 续聊或其他调用方，不能指望它们都走过那条路径。
            instructions=str(self.instructions or "").strip()[:MAX_INSTRUCTIONS_CHARS],
            allowed_tools=None if self.allowed_tools is None else tuple(self.allowed_tools),
            disallowed_tools=tuple(self.disallowed_tools or ()),
            skills=tuple(self.skills or ()),
            agent_ignored=tuple(self.agent_ignored or ()),
            fork=bool(self.fork),
            permission_mode=str(self.permission_mode or "").strip(),
            definition_background=bool(self.definition_background),
            effort=str(self.effort or "").strip(),
            model=str(self.model or "").strip(),
            agent_hooks=dict(self.agent_hooks or {}),
            memory_scope=str(self.memory_scope or "").strip().lower(),
            omit_claude_md=bool(self.omit_claude_md),
        )


@dataclass
class SubAgentResult:
    ok: bool
    summary: str = ""
    steps: int = 0
    depth: int = 1
    agent_path: str = ""
    # 后台句柄：前台子代理也有，只是没人需要拿它去 task_output。
    agent_id: str = ""
    worktree: dict = field(default_factory=dict)
    diff: str = ""
    error: str = ""
    usage: dict = field(default_factory=dict)
    # 哪一次并行编排派出的这一位；单发子代理是空串（"不属于任何一批"，不是"第 0 批"）。
    parallel_group: str = ""
    # 这一位自己的线程 id：用量行按它归属。批次汇总要能一次点出"这批包含哪几条线程"，
    # 否则读的人只能把整串事件按 trace_id 重归一遍堆。
    thread_id: str = ""
    # 实际创建的副本：只在本进程内用于合并，不进事件日志（内容是整份文件）
    tree: Worktree | None = field(default=None, repr=False)
    merge: dict = field(default_factory=dict)
    # J3：这一位自己的 AppContainer 包名（空 = 跑在共享工作区/父级身份里）。
    # 收尸时要连带删包；不写进事件正文，包名本身不是证据。
    isolation_profile: str = field(default="", repr=False)
    # 这一轮结束时的完整对话：续聊（send_message）靠它接着往下说。
    # 不进 as_dict / 事件：里面是子代理读过的文件内容，写进时间线等于把上下文泄漏给所有人。
    conversation: list[dict] = field(default_factory=list, repr=False)
    # SubagentStart 钩子的非阻断报错（退出码 2）。上游把它作为父会话里一条
    # 可见提示；这里进交付正文与事件，但不进子代理上下文（那不是作业指令）。
    hook_warnings: list[str] = field(default_factory=list)


    def as_dict(self) -> dict:
        payload = {
            "ok": self.ok,
            "summary": self.summary,
            "steps": self.steps,
            "depth": self.depth,
            "agent_path": self.agent_path,
            "agent_id": self.agent_id,
            "usage": self.usage,
            "parallel_group": self.parallel_group,
            "thread_id": self.thread_id,
        }
        if self.worktree:
            payload["worktree"] = self.worktree
        if self.diff:
            payload["diff"] = self.diff
        if self.merge:
            payload["merge"] = self.merge
        if self.error:
            payload["error"] = self.error
        if self.hook_warnings:
            payload["hook_warnings"] = list(self.hook_warnings)
        return payload

    def render(self) -> str:
        parts = [self.summary.strip() or "子代理未给出结论"]
        if self.diff:
            parts.append("【隔离副本中的改动】\n" + self.diff)
        if self.merge:
            parts.append("【合并结果】" + render_merge(self.merge))
        if self.hook_warnings:
            parts.append("【子代理钩子告警】\n" + "\n".join(self.hook_warnings))
        if self.error:
            parts.append(f"【异常】{self.error}")
        return "\n\n".join(parts)


def _drop_profile(name: str) -> None:
    """删子代理的任务包；失败只该影响下一次建包，不把它升级成子代理失败。

    包里有 SID 与已授权的 ACL，留着就是一块没有主人、仍然可写的边界。
    """
    if not name:
        return
    from blank_api.services.sandbox import windows_appcontainer as wac

    try:
        wac.delete_profile(name)
    except Exception:  # noqa: BLE001 - 收尸失败不能反过来改写子代理结论
        return


def render_merge(payload: dict) -> str:
    parts: list[str] = []
    applied = payload.get("applied") or []
    conflicts = payload.get("conflicts") or []
    skipped = payload.get("skipped") or []
    if applied:
        parts.append("已写入主工作区：" + "、".join(map(str, applied)))
    if conflicts:
        parts.append("冲突未合并：" + "、".join(map(str, conflicts)))
    for item in skipped:
        if isinstance(item, dict):
            parts.append(f"跳过 {item.get('path')}：{item.get('reason')}")
    return "；".join(parts) or "无改动"


@dataclass
class _PreparedSpawn:
    """通过校验、已经分好 agent_path/agent_id 的一次派发。

    前台 `__call__` 与后台 `spawn_background` 共用这一段：两条路如果各算各的
    agent_path，同一个任务里会出现两条 `root/sub-1`，事件里就分不清谁是谁。
    """

    session: Any
    request: SubAgentRequest
    depth: int
    agent_path: str
    agent_id: str


@dataclass
class _BackgroundAgent:
    agent_id: str
    task_id: str
    request: SubAgentRequest
    agent_path: str
    depth: int
    started_at: float
    task: asyncio.Task | None = None
    result: SubAgentResult | None = None
    error: str = ""
    notified: bool = False


class _Recorder:
    """包一层事件记录：子代理的每条事件都带 agent_path 与 depth，以及它属于哪一轮。"""

    def __init__(
        self, recorder, *, agent_path: str, depth: int, parent_thread_id: str, root_thread_id: str = ""
    ) -> None:
        self.recorder = recorder
        self.agent_path = agent_path
        self.depth = depth
        self.parent_thread_id = parent_thread_id
        self.root_thread_id = root_thread_id
        # 账本行要按轮筛，用的必须是事件行同一个值：从被包的记录器直接读，不在这里再造。
        self.run_id = str(getattr(recorder, "run_id", "") or "")
        # 发起轮也跟着读：孙代理不必知道团队的存在，它父辈是谁引起的它就记谁。
        self.origin_run_id = str(getattr(recorder, "origin_run_id", "") or "")

    async def add(self, event: str, node: str, payload: dict) -> None:
        await self.recorder.add(
            event,
            node,
            {
                **payload,
                "agent_path": self.agent_path,
                "depth": self.depth,
                "parent_thread_id": self.parent_thread_id,
                "root_thread_id": self.root_thread_id,
            },
        )


def _tool_call_name(call: dict) -> str:
    return str((call.get("function") or {}).get("name") or call.get("name") or "")


def _tool_call_args(call: dict) -> dict:
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _session_provider(session) -> tuple[str, str]:
    """这次子代理的花费挂在哪个账号上：角色单独指定过就按角色，否则跟着任务的供应商。"""
    role_ids = getattr(session, "role_provider_ids", None) or {}
    return (
        role_ids.get("subagent") or getattr(session, "provider_id", "") or "",
        getattr(session, "provider_name", "") or "",
    )


# Claude 的 permissionMode 名字 → 本机审批档。数字越小越严，与上游 ``Ho``
# （plan < default < acceptEdits < bypassPermissions）同一顺序。
PERMISSION_MODE_MAP: dict[str, str] = {
    "plan": MODE_PLAN,
    "default": MODE_DEFAULT,
    "acceptEdits": MODE_ACCEPT_EDITS,
    "bypassPermissions": MODE_BYPASS,
}
_MODE_RANK: dict[str, int] = {
    MODE_PLAN: 0,
    MODE_DEFAULT: 1,
    MODE_ACCEPT_EDITS: 2,
    MODE_BYPASS: 3,
}


def _child_permission_mode(
    baseline: str, declared: str, *, fork_inherits: bool = False
) -> tuple[str, str]:
    """把定义里声明的 permissionMode 压到派发基线上：只收紧，不放宽。

    对齐上游 ``lK(spawn, current)``：档位序号不大于当前基线才生效。fork 分身
    的权限对象就是父会话那一份，声明无权改动它。返回 ``(生效档, 判定)``，判定
    为空串表示没有声明，其余为 ``applied`` / ``looser`` / ``unsupported`` /
    ``fork_inherits_parent``，供调用方写进事件。
    """
    if not declared:
        return baseline, ""
    if fork_inherits:
        return baseline, "fork_inherits_parent"
    mapped = PERMISSION_MODE_MAP.get(declared)
    if mapped is None:
        return baseline, "unsupported"
    base_rank = _MODE_RANK.get(baseline)
    if base_rank is None:
        return baseline, "unsupported"
    if _MODE_RANK[mapped] > base_rank:
        return baseline, "looser"
    return mapped, "applied"


def _inherited_watermark(session) -> dict:
    """子代理的上下文管家跟着父会话的水位走，而不是自己开一套。

    原来是裸构造 `ContextCompactor()`：主会话按供应商窗口（比如 64k 的 80%）压水位，
    子代理却退回 48k 字符预算——同一趟任务里两套口径，派得越多越看不见窗口在哪儿。

    只搬父会话**真的定了**的键：拿不到管家（评测 harness 那条路）、或某一项就是 0
    （意味着"没设"而不是"设成 0"）时把它连同 0 一起塞进去，等于替子代理做了一个
    谁都没做过的决定——0 的 trigger_ratio 会让水位线落在 0，每一步都触发压缩。
    这种键交给 ContextCompactor 自己的默认值。
    """
    compactor = getattr(session, "compactor", None)
    if compactor is None:
        return {}
    inherited: dict = {}
    for key in ("context_window", "reserve_tokens", "max_chars"):
        value = int(getattr(compactor, key, 0) or 0)
        if value > 0:
            inherited[key] = value
    ratio = float(getattr(compactor, "trigger_ratio", 0.0) or 0.0)
    if ratio > 0.0:
        inherited["trigger_ratio"] = ratio
    return inherited


def batch_usage(results: list[SubAgentResult]) -> dict:
    """一批子代理的合计用量（token 与调用次数）。

    刻意不加 `latency_ms`：并行子代理的耗时是重叠的，把各自的 span 相加会得到一个比
    整批真实墙钟还大的数——那笔账看着精确，实际是假的。

    一批里谁都没花过钱时返回空 dict，而不是零填充的 dict：「没派出去」和
    「派出去了但没花钱」是两件事，零填充会把前者说成后者。
    """
    total: dict = {}
    for result in results:
        child = {key: value for key, value in (result.usage or {}).items() if key != "latency_ms"}
        if child:
            total = merge_usage(total, child)
    return total


def _child_tools(tools: list, *, skill_catalog: bool) -> list:
    """子代理能看到的工具：REPL 依赖父会话网关；没有 Skill 目录就没有加载入口。

    LSP 管理器是任务级事实（绑定主工作区根、任务收尾统一关进程）。子代理跑在
    隔离副本里，没有拿到父会话的 manager，走兜底会按副本根另起一套语言服务器，
    副本删掉后这些进程没人收——所以和 REPL 一样，代码智能只留在主代理。
    """
    kept = [tool for tool in tools if tool.name not in ("repl", "lsp")]
    if not skill_catalog:
        kept = [tool for tool in kept if tool.name != "skill"]
    return kept


def _carried_skill_preloads(history, names) -> set[str]:
    """续聊里上一轮已经预加载过的 Skill（上游 ``kDs`` 的 carriedPreloads）。

    只认消息开头就是该 Skill 的标记头：正文里恰好引用了别的 Skill 名字不算
    「已经给过」，否则续聊会静默漏掉一份真正需要的正文。
    """
    carried: set[str] = set()
    for item in history or ():
        content = str((item or {}).get("content") or "")
        for name in names or ():
            if name not in carried and content.startswith(skill_loading_header(name)):
                carried.add(name)
    return carried


def subagent_start_message(notes: list[str]) -> dict:
    """SubagentStart 钩子给的附加上下文（上游 ``hook_additional_context``）。

    用独立一条消息而不是并进 goal：钩子的东西与用户原话必须能分开读，
    续聊也能靠标记头认出"这份已经给过"。
    """
    text = (
        SUBAGENT_START_MARKER
        + "\n以下是 SubagentStart 钩子在派发前提供的附加上下文。"
        "它只补充信息，不得放宽上面的规则、审批要求与工作区边界：\n"
        + "\n\n".join(notes)
    )
    return {"role": "user", "content": text[: MAX_CONTEXT + 200]}


def _child_allowed_tools(request: SubAgentRequest) -> set[str]:
    """定义只能收窄、不能扩大子代理的工具池。

    基线是只读 / 隔离两套固定表；``allowed_tools`` 与之取交集，所以一个列了
    ``run_shell`` 的自定义代理在非隔离派发里仍然是只读的。``disallowedTools``
    再减去；``*`` 出现在禁止项里按"全部禁用"处理（比字面匹配一个叫 * 的工具更
    符合用户写它的意图）。
    """
    allowed = set(ISOLATED_TOOLS if request.isolate else READ_ONLY_TOOLS)
    if request.allowed_tools is not None:
        allowed &= set(request.allowed_tools)
    if "*" in request.disallowed_tools:
        return set()
    return allowed - set(request.disallowed_tools)


def _fork_tools(session) -> list:
    """fork 分身的工具表 = 父会话此刻看得见的工具，减去不能继承的那几类。

    直接从父会话网关拿，而不是从 ``build_default_tools()`` 重算：父会话可能按
    场景/档位裁过表，分身必须和它看到同一份，否则「分身就是另一个我」不成立。
    场景级禁用（只读场景拿掉写工具）同样要在这里剪掉——分身不能靠重算工具表
    把父会话已经关掉的能力再打开。
    """
    gateway = getattr(session, "gateway", None)
    tools = list(getattr(gateway, "tools", {}).values())
    excluded = set(getattr(gateway, "excluded", ()) or ())
    return [
        tool
        for tool in tools
        if tool.name not in FORK_EXCLUDED_TOOLS and tool.name not in excluded
    ]


def _scope_agent_hooks(
    parent: HookRunner | None,
    extra: dict[str, tuple[HookSpec, ...]],
    *,
    agent_name: str,
) -> tuple[HookRunner | None, dict[str, int], list[str]]:
    """把这个子代理定义的钩子并入一个**独立**运行器。

    上游在派发点把定义钩子注册进子会话的钩子注册表：``Stop`` 改写成
    ``SubagentStop``，注册发生在 SubagentStart 之后。本机父子共享一个
    ``HookRunner``，直接往里面加会泄漏到父会话的下一轮，所以这里复制一份
    映射给子代理用——父 runner 的配置一个字都不动。

    返回 ``(runner, 新注册的事件计数, 被改写的事件名列表)``。``disableAllHooks``
    为真时返回父 runner 本身、零注册：总开关下定义钩子也不能复活。
    """
    if not extra:
        return parent, {}, []
    if parent is not None and parent.disabled:
        return parent, {}, []

    converted: list[str] = []
    added: dict[str, list[HookSpec]] = {}
    for event, specs in extra.items():
        target = "subagent_stop" if event == "stop" else event
        if target != event:
            converted.append(event)
        added.setdefault(target, []).extend(specs)

    merged: dict[str, list[HookSpec]] = {
        event: list(specs) for event, specs in (parent.hooks.items() if parent else ())
    }
    registered: dict[str, int] = {}
    for event, specs in added.items():
        fingerprints = {
            (spec.command, spec.match, spec.timeout)
            for spec in merged.get(event, [])
        }
        fresh: list[HookSpec] = []
        for spec in specs:
            fingerprint = (spec.command, spec.match, spec.timeout)
            if fingerprint in fingerprints:
                continue
            fingerprints.add(fingerprint)
            fresh.append(spec)
        if fresh:
            merged.setdefault(event, []).extend(fresh)
            registered[event] = len(fresh)
    if not registered:
        return parent, {}, []

    if parent is None:
        runner = HookRunner(merged)
    else:
        runner = HookRunner(
            merged,
            settle_timeout=parent.settle_timeout,
            warnings=list(parent.warnings),
            prompt_runner=parent.prompt_runner,
            mcp_runner=parent.mcp_runner,
        )
        # 异步钩子的完成结果与唤醒是会话级事实：父子共用同一份队列，
        # 否则子代理这边 drain 不到父会话刚跑完的后台结果。
        runner._tasks = parent._tasks
        runner._completed = parent._completed
        runner._wakeups = parent._wakeups
    runner.warnings.append(
        f"Agent {agent_name or '?'} 注册了 {sum(registered.values())} 条定义钩子"
    )
    return runner, registered, converted


async def run_subagent(
    *,
    adapter: ModelAdapter,
    tools: list,
    root: Path,
    task_id: str,
    session_factory,
    recorder,
    request: SubAgentRequest,
    agent_path: str,
    depth: int,
    parent_thread_id: str,
    root_thread_id: str = "",
    provider_id: str = "",
    provider_name: str = "",
    watermark: dict | None = None,
    sandbox_tier: str = "workspace",
    parent_tier: str = "",
    agent_id: str = "",
    # 后台取消时由调用方按这个把手清副本：取消可能落在这条协程的任意一个 await 上，
    # 连函数自己的 try 都还没进——清理责任只能交给包住它的那一层。
    tracker: dict | None = None,
    # 隔离副本的创建/删除钩子。为 None 时走默认的 git worktree / 整树复制。
    hooks: HookRunner | None = None,
    # 续聊：上一轮的完整对话。非空时这一轮接在它后面，而不是从系统提示 + goal 重新开始。
    history: list[dict] | None = None,
    # 父任务的 Skill 目录。子代理拿一份**拷贝**：加载状态是会话级事实，
    # 子代理加载过的 Skill 不能让父代理误以为自己也已经见过正文。
    skill_catalog: SkillCatalog | None = None,
    # fork 分身继承父会话的权限对象本身（不是复制一份）：同一个模式、同一批
    # 已批准动作。审批卡在子代理里弹不出来，所以需要审批的动作照旧被拒绝——
    # 分身拿到的是父会话**已经拥有**的能力，不是一张新的白名单。
    parent_permissions: PermissionPolicy | None = None,
    # fork 在同一工作区里跑，父会话允许的后台命令它也允许；普通子代理的隔离
    # 副本跑完即删，后台进程活不到有人取结果，所以默认关掉。
    allow_background_shell: bool = False,
    # fork 的父网关：子网关要沿用父会话的场景闸门（文件类型白名单）、场景禁用
    # 与写根，否则分身就是绕过场景模式的一道侧门。普通子代理传 None。
    fork_gateway: ToolGateway | None = None,
    # 新派发前跑 SubagentStart 钩子；续聊（同一个子代理接着说话）不是新派发，
    # 上游也不会重跑——上下文已经在上一轮的对话里。
    start_hooks: bool = True,
) -> SubAgentResult:
    request = request.normalized()
    if not request.goal:
        return SubAgentResult(ok=False, error="子任务目标为空", agent_path=agent_path, depth=depth)
    # 目录是父目录的独立副本：能加载的 Skill 一样，加载状态从空开始。
    catalog = SkillCatalog(skill_catalog.entries()) if skill_catalog is not None else None
    if catalog is not None and not len(catalog):
        catalog = None
    # REPL 是主代理的会话级能力，子代理没有独立网关；没有模型可调用的 Skill 时
    # 加载入口也没意义。两条口径都在 _child_tools 里。
    tools = _child_tools(tools, skill_catalog=catalog is not None and catalog.model_available())
    try:
        sandbox_tier = assert_not_looser(parent_tier, sandbox_tier)
    except TierDowngrade as exc:
        # 子代理只能继承父级的边界，不许自己挑一个更宽的。这不是参数错，是越权。
        return SubAgentResult(ok=False, error=str(exc), agent_path=agent_path, depth=depth)

    # 定义声明的持久记忆：目录按**父工作区**解析——隔离子代理的临时副本跑完就删，
    # 记忆写在那里面等于没写。目录建不出来时不注入提示（否则等于告诉模型一个
    # 不存在的记忆目录），照常派发并留一条事件。
    memory_dir: Path | None = None
    memory_section = ""
    memory_error = ""
    if request.memory_scope and not request.fork:
        try:
            memory_dir = agent_memory_dir(
                request.memory_scope, request.agent_name, Path(root)
            )
            memory_dir.mkdir(parents=True, exist_ok=True)
            memory_section = render_memory_section(
                scope=request.memory_scope,
                agent_name=request.agent_name,
                directory=memory_dir,
            )
            # 记忆写入是定义声明的作用域能力；显式禁掉这个工具的定义仍然说了算。
            if (
                "*" not in request.disallowed_tools
                and "memory_write" not in request.disallowed_tools
            ):
                tools = [*tools, *agent_memory_tools(memory_dir)]
        except OSError as exc:
            memory_dir = None
            memory_section = ""
            memory_error = f"{exc.__class__.__name__}: {exc}"

    # 子代理这一轮实际用的模型：定义里的覆盖，``inherit``/空串回落到供应商配置；
    # fork 恒继承父会话模型（上游 ``Jt ? "inherit" : we``）。一轮只算一次，
    # 每一步调用都用同一个——子代理是同一次作业，中途换模型会让账与裁决各说各话。
    child_model = "" if request.fork or request.model in ("", "inherit") else request.model
    # 子代理这一轮的思考强度：定义里声明的档位 + 目标正文里的关键词，走与主代理
    # 同一套裁决（模型封顶、thinking 关断回落）。一轮只算一次，之后每一步调用
    # 都复用它——子代理的每一步都是同一次作业，档位不该逐步重算、更不该逐步变。
    # 定义没声明时回落到供应商配置的静态档位（与主代理同一份 ``reasoning_effort``），
    # 否则同一家供应商在主线程与子代理里会跑出两个档位。
    # 放在越权守卫之后：被守卫拦下时一个适配器字段都不该读，adapter=None 也能走到守卫。
    child_effort = resolve_turn_effort(
        request.effort or str(getattr(adapter.config, "reasoning_effort", "") or ""),
        model=child_model or str(getattr(adapter.config, "model", "") or ""),
        policy=EffortPolicy(),
        thinking=ThinkingConfig(),
        keyword_text=request.goal,
    )
    # 扩展思考的开关与预算：与主代理同一份口径（auto 不发、off 发 disabled、
    # extended 发 enabled + 预算，预算天花板是输出上限减一）。
    thinking_config = ThinkingConfig()
    if thinking_config.mode == "off":
        child_thinking: dict | None = {"type": "disabled"}
    elif thinking_config.mode == "extended":
        child_thinking = {
            "type": "enabled",
            "budget_tokens": thinking_config.budget_tokens
            or thinking_budget_ceiling(int(getattr(adapter.config, "max_tokens", 0) or 0)),
        }
    else:
        child_thinking = None

    tree: Worktree | None = None
    run_root = Path(root)
    isolated = request.isolate
    isolation_profile = ""
    if isolated:
        from blank_api.services.sandbox import task_isolation

        isolation_on = task_isolation.enabled()
        try:
            if isolation_on:
                # J3：子代理也拿一个只属于它的包。副本必须用独立仓库克隆——worktree 的
                # `git add` 会写父仓库共享对象库，等于给子代理改历史的钥匙（probe_git.py
                # 实测）。克隆只读借用源对象库，所以把那一棵子树以 RX 授给子代理包。
                tree = create_clone(run_root, label=request.label or "sub")
                isolation_profile, _sid = task_isolation.secure_tree(
                    tree, f"{task_id}:{uuid.uuid4().hex}", source=run_root
                )
                hook_results: list = []
            else:
                tree, hook_results = await create_worktree_with_hooks(
                    run_root, label=request.label or "sub", hooks=hooks
                )
        except (ValueError, RuntimeError) as exc:
            return SubAgentResult(
                ok=False, error=str(exc), agent_path=agent_path, depth=depth
            )
        run_root = tree.path
        if tracker is not None:
            # 先登记再发事件：事件写库也是一个 await，取消落在这里时副本必须已可被找回。
            tracker["tree"] = tree
            if isolation_profile:
                tracker["profile"] = isolation_profile
        worktree_log = _Recorder(
            recorder,
            agent_path=agent_path,
            depth=depth,
            parent_thread_id=parent_thread_id,
            root_thread_id=root_thread_id,
        )
        if hook_results:
            await worktree_log.add(
                "hook.completed",
                "subagent",
                {
                    "event": "worktree_create",
                    "mode": tree.mode,
                    "results": [item.as_dict() for item in hook_results],
                },
            )
        await worktree_log.add("worktree.created", "subagent", tree.as_dict())

    if fork_gateway is not None:
        # 分身沿用父会话的场景与写根，只换上剪过的工具表。新造一个默认 dev 场景的
        # 网关会让「文档场景只能写 .md」这类约束在分身里静默失效。
        scenario = getattr(getattr(fork_gateway, "scenario", None), "key", "dev")
        gateway = ToolGateway(
            tools,
            defer=False,
            excluded=getattr(fork_gateway, "excluded", ()) or (),
            scenario=scenario,
            write_root=getattr(fork_gateway, "write_root", None),
        )
    else:
        gateway = ToolGateway(tools, defer=False)
    baseline_mode = MODE_ACCEPT_EDITS if isolated else MODE_DEFAULT
    log = _Recorder(
        recorder,
        agent_path=agent_path,
        depth=depth,
        parent_thread_id=parent_thread_id,
        root_thread_id=root_thread_id,
    )
    if memory_error:
        await log.add(
            "subagent.memory_failed",
            "subagent",
            {
                "agent_id": agent_id,
                "agent_type": request.agent_name or BUILTIN_AGENT,
                "scope": request.memory_scope,
                "error": memory_error[:300],
            },
        )
    elif memory_dir is not None:
        # 审计只报作用域与条目规模，不复制记忆正文（里面可能有项目敏感信息）。
        await log.add(
            "subagent.memory_enabled",
            "subagent",
            {
                "agent_id": agent_id,
                "agent_type": request.agent_name or BUILTIN_AGENT,
                "scope": request.memory_scope,
                "path": mask_path(str(memory_dir)),
                "chars": len(memory_section),
            },
        )
    if child_effort.warning:
        # 档位被压下来必须留痕：定义里写了 max、模型只认 high 时，子代理照跑，
        # 但读事件的人得能看出来"这一轮没有按声明的档位走"。
        await log.add(
            "effort.warning",
            "subagent",
            {
                "warning": child_effort.warning,
                "requested": child_effort.requested,
                "effort": child_effort.level,
            },
        )
    if parent_permissions is not None:
        # fork 的权限对象就是父会话那一份：定义里再写 permissionMode 也改不动它。
        permissions = parent_permissions
        parent_mode = str(getattr(parent_permissions, "mode", baseline_mode) or baseline_mode)
        effective_mode, verdict = _child_permission_mode(
            parent_mode, request.permission_mode, fork_inherits=True
        )
    else:
        effective_mode, verdict = _child_permission_mode(
            baseline_mode, request.permission_mode
        )
        permissions = PermissionPolicy.build(
            effective_mode,
            approved_tools=set(),
            approved_actions=set(),
            # 子代理继承父任务的档位，不许自己挑一个更宽的执行边界。
            sandbox_tier=sandbox_tier,
        )
    if verdict:
        # 声明了却没生效必须留痕：用户以为写了 plan，实际仍在 acceptEdits，
        # 这条事件是唯一能看出差别的地方。
        await log.add(
            "permission.mode.applied" if verdict == "applied" else "permission.mode.ignored",
            "subagent",
            {
                "declared": request.permission_mode,
                "baseline": parent_mode if parent_permissions is not None else baseline_mode,
                "effective": effective_mode,
                "reason": verdict,
                "fork": bool(request.fork),
            },
        )
    # 定义钩子在这里装配成子代理**自己的**作用域运行器（父钩子照旧可见）。
    # 注册必须排在 SubagentStart 之后：定义里写的 SubagentStart 钩子看不到
    # 自己这次启动，与上游一致。
    child_hooks, registered_hooks, converted_hooks = _scope_agent_hooks(
        hooks, request.agent_hooks, agent_name=request.agent_name
    )
    context = ToolContext(
        root=run_root,
        task_id=task_id,
        session_factory=session_factory,
        approval_policy=permissions.decide,
        trace_id=parent_thread_id,
        permissions=permissions,
        sandbox_tier=sandbox_tier,
        isolation_profile=isolation_profile,
        # 子代理不能成为绕过客户安全设置的侧门：同一台机器、同一份策略。
        safety=SafetyPolicy.load(),
        tool_registry=gateway.registry,
        skills=catalog,
        # 读后写状态按副本独立记：子代理只在它自己的根里读和写。
        file_state=FileStateCache(),
        # 子树在本轮结束即删除/合并：后台命令活不到有人取结果的时候，入口直接关掉。
        # fork 在同一个工作区里跑，父会话能开的它也能开。
        allow_background_shell=bool(allow_background_shell),
        # 子代理这一轮的档位：它自己调 skill 工具时，正文里的 ${CLAUDE_EFFORT}
        # 用的就是这一档（与发给模型的 output_config.effort 同一份裁决结果）。
        turn_state={"effort": child_effort.level},
        # 子代理的工具调用走同一条网关，所以 PermissionRequest / PermissionDenied /
        # 审批通知这些钩子也必须在子代理里成立——否则"派下去干活"就是钩子看不见的盲区。
        hooks=child_hooks,
        # 只在配了钩子时才把事件记录器交给网关（钩子结果要落 hook.completed）；
        # 没配钩子时保持原样，免得别的工具突然开始在子代理里写事件。
        recorder=log if child_hooks is not None else None,
    )
    # SubagentStart：派发前问一次，附加上下文注入子代理的启动上下文（上游按
    # agent_type 匹配）。退出码 2 是"钩子报错"，上游把它渲染成父会话里一条可见
    # 提示、子代理照常启动——这里记事件与交付正文，但绝不把它当作业指令注入。
    start_notes: list[str] = []
    hook_warnings: list[str] = []
    agent_type = request.agent_name or BUILTIN_AGENT
    if start_hooks and hooks is not None and hooks.enabled:
        scratchpad_dir = run_root / ".blank" / "context" / "scratchpad"
        scratchpad_dir.mkdir(parents=True, exist_ok=True)
        transcript_file = run_root / ".blank" / "context" / "subagent-transcript.jsonl"
        start_results = await hooks.run(
            "subagent_start",
            agent_type,
            {
                "agent_id": agent_id,
                "agent_type": agent_type,
                "agent_path": agent_path,
                "depth": depth,
                "goal": request.goal[:500],
                "parent_thread_id": parent_thread_id,
                "root_thread_id": root_thread_id,
                "workspace": str(run_root),
                "permission_mode": effective_mode,
                "fork": bool(request.fork),
                "session_id": task_id,
                "transcript_path": str(transcript_file),
                "scratchpad_dir": str(scratchpad_dir),
                "prompt_id": uuid.uuid4().hex,
                "caller_session_id": parent_thread_id,
                "effort": {"level": child_effort.level},
            },
            effort=child_effort.level,
        )
        if start_results:
            await log.add(
                "hook.completed",
                "subagent",
                {
                    "event": "subagent_start",
                    "target": agent_type,
                    "results": [item.as_dict() for item in start_results],
                },
            )
            start_notes = usable_context(start_results)
            if start_notes:
                await log.add(
                    "hook.context_received",
                    "subagent",
                    {
                        "event": "subagent_start",
                        "target": agent_type,
                        "count": len(start_notes),
                        "chars": sum(len(note) for note in start_notes),
                    },
                )
            errors = non_blocking_errors(start_results)
            hook_warnings = [
                (item.additional_context or item.reason or "钩子以退出码 2 结束")[:300]
                for item in errors
            ]
            if hook_warnings:
                await log.add(
                    "hook.non_blocking_error",
                    "subagent",
                    {
                        "event": "subagent_start",
                        "target": agent_type,
                        "agent_id": agent_id,
                        "count": len(hook_warnings),
                        "reasons": hook_warnings,
                    },
                )
    if registered_hooks:
        # 注册发生在 SubagentStart 之后，审计事件也按这个顺序落。
        await log.add(
            "subagent.hooks_registered",
            "subagent",
            {
                "agent_id": agent_id,
                "agent_type": agent_type,
                "events": dict(registered_hooks),
                "sources": sorted(
                    {
                        spec.source
                        for specs in request.agent_hooks.values()
                        for spec in specs
                    }
                ),
                "converted": list(converted_hooks),
            },
        )
    compactor = ContextCompactor(**(watermark or {}))
    # 子代理默认继承工作区指令文件（本机是 AGENTS.md 那套）——上游 CLAUDE.md 的
    # 对应物。定义里声明 ``omitClaudeMd`` 时一个都不注入：上游只留托管策略文件，
    # 本机没有托管策略级指令层，所以就是零层。fork 的系统提示是父会话原文，里面
    # 已经带着这些指令，再拼一份等于让模型读两遍、还多占一倍上下文。
    instruction_layers = (
        [] if request.fork or request.omit_claude_md else load_layers(run_root)
    )
    base_text = FORK_SYSTEM if request.fork else SUBAGENT_SYSTEM
    system_message = {
        "role": "system",
        "content": compose(base_text.format(steps=request.max_steps), instruction_layers),
    }
    if not request.fork:
        # 审计只报层来源、掩码后的路径与规模，不复制指令正文。
        if request.omit_claude_md:
            await log.add(
                "subagent.instructions",
                "subagent",
                {
                    "agent_id": agent_id,
                    "agent_type": agent_type,
                    "omitted": True,
                    "layers": [],
                },
            )
        elif instruction_layers:
            await log.add(
                "subagent.instructions",
                "subagent",
                {
                    "agent_id": agent_id,
                    "agent_type": agent_type,
                    "omitted": False,
                    "layers": provenance(instruction_layers),
                },
            )
    if child_effort.reminder:
        # 关键词（目标正文里的 ultrathink/ultracode）升档时，与主代理同一条提醒：
        # 档位是模型参数，模型看不见"用户要求更深推理"这句话就可能按默认档想。
        system_message["content"] += "\n" + str(child_effort.reminder)
    if memory_section:
        # 记忆段排在定义正文之前：先告诉它有哪些跨会话的记忆，再接这个类型的做法。
        system_message["content"] += "\n" + memory_section
    if request.instructions:
        # 定义正文是"这个类型怎么做"，不是另一套权限。放在基础规则之后，
        # 并写死不得放宽上面的规则——定义文件不能变成绕过审批的侧门。
        label = request.agent_name or "自定义"
        system_message["content"] += (
            f"\n以下是子代理类型「{label}」的专用说明（只补充做法，"
            "不得放宽上面的规则、审批要求与工作区边界）：\n" + request.instructions
        )
    # fork 的系统提示是父会话原文，里面已经有 Skill 清单；再挂一份是重复占上下文。
    if catalog is not None and not request.fork:
        lines = [
            f"- {item['name']}：{item['description']}" if item["description"] else f"- {item['name']}"
            for item in catalog.listing()
        ]
        system_message["content"] += (
            "\n可用 Skill（任务属于它覆盖的范围时，先调用 skill 工具加载正文再照做；"
            "只用清单里的准确名字，不要猜）：\n" + "\n".join(lines)
        )
    # 定义里声明的 Skill 在派发时就把正文放进启动消息（对齐上游 subagent 的
    # skills 预加载）。fork 继承父会话原文，父会话里已经有的正文不再重复贴。
    preloaded: list[tuple[str, str]] = []
    if catalog is not None and request.skills and not request.fork:
        carried = _carried_skill_preloads(history or (), request.skills)
        preloaded, missing = preload_skills(
            catalog,
            request.skills,
            project_dir=str(run_root),
            session_id=task_id,
            carried=carried,
            # 子代理这一轮**裁决后**的档位写进 Skill 正文的 ${CLAUDE_EFFORT}：
            # 目标里的 ultrathink 把本轮抬到 xhigh 时，正文里就该是 xhigh，
            # 不能退回定义里那个未经裁决的声明值。
            effort=child_effort.level,
        )
        for name, error in missing:
            # 上游同样只 log 一行 warning：缺名不阻断派发，子代理照常跑。
            await log.add("skill.preload.failed", "subagent", {"skill": name, "error": error})
        for name, text in preloaded:
            await log.add("skill.preloaded", "subagent", {"skill": name, "chars": len(text)})
    preloaded_messages = [{"role": "user", "content": text} for _, text in preloaded]
    # 钩子的附加上下文紧挨着 goal 之前：它是"这次派发的补充说明"，不是另一条任务。
    start_messages = [subagent_start_message(start_notes)] if start_notes else []
    if history:
        # 续聊 / fork：历史里已经有系统提示与上一轮的读/写/结论，这一轮只追加新问题。
        messages: list[dict] = list(history)
        if not messages or messages[0].get("role") != "system":
            messages.insert(0, system_message)
        elif request.fork:
            # 分身沿用父会话的系统提示，只把 fork 约束接在它末尾。另起一条 system
            # 会让"同一条系统提示"这个前提在部分供应商上直接不成立。续聊取回的
            # 系统提示已经带着这段约束，按标记跳过，免得越续越长。
            first = dict(messages[0])
            current = str(first.get("content") or "")
            if FORK_MARKER not in current:
                first["content"] = current + str(system_message["content"])
                messages[0] = first
        messages.extend(preloaded_messages)
        messages.extend(start_messages)
        messages.append({"role": "user", "content": request.goal})
    else:
        messages = [
            system_message,
            *preloaded_messages,
            *start_messages,
            {
                "role": "user",
                "content": request.goal + ("\n（你在一个临时隔离副本里工作，改动不会影响真实工作区）" if isolated else ""),
            },
        ]
    usage_started = time.perf_counter()
    provider_name = provider_name or adapter.config.name
    # 子代理是一条独立的线程：用量行按 trace_id 归属到"哪一个 agent"，
    # parent_thread_id 只在事件里用来还原父子关系（与 subagent.started 一致）。
    thread_id = uuid.uuid4().hex
    steps = 0
    summary = ""
    error = ""
    turn: dict = {}

    async def account(
        model: str, call_usage: Usage, latency_ms: int, status: str, response_id: str = ""
    ) -> None:
        """每一次子代理调用都落进与主代理同一张表、同一套三级口径。

        原来子代理只在本地把数字累加，钱真花了但任务级的求和里没有它——
        一个把活派下去的任务，账上却看不见被派出去的那部分。
        """
        nonlocal turn
        turn = await account_call(
            session_factory=session_factory,
            recorder=log,
            task_id=task_id,
            trace_id=thread_id,
            provider_id=provider_id,
            provider_name=provider_name,
            node="subagent",
            role="subagent",
            depth=depth,
            agent_path=agent_path,
            model=model,
            provider=str(getattr(adapter, "last_provider", "") or ""),
            usage=call_usage,
            latency_ms=latency_ms,
            status=status,
            response_id=response_id,
            turn=turn,
            run_id=log.run_id,
            origin_run_id=log.origin_run_id,
            parent_thread_id=parent_thread_id,
            root_thread_id=root_thread_id,
            parallel_group=request.parallel_group,
            sandbox_mode=sandbox_tier,
        )

    async def invoke(specs: list, chat: list[ChatMessage]) -> Any:
        """一次调用，成功与失败都进账（失败的那笔根本没花 token，只有账本说得出）。"""
        started = time.perf_counter()
        try:
            response = await adapter.bind_tools(
                specs,
                chat,
                effort=child_effort.level,
                thinking=child_thinking,
                model=child_model,
            )
        except ModelError:
            await account("", Usage(), int((time.perf_counter() - started) * 1000), "error")
            raise
        await account(
            response.model,
            response.usage,
            int((time.perf_counter() - started) * 1000),
            "ok",
            response.response_id,
        )
        return response

    stop_blocks = 0

    async def stop_gate(text: str) -> bool:
        """交付前问一次 SubagentStop：返回 True 表示这一轮不许收尾。

        与主代理的 Stop 同一套语义（含连续阻断上限）：阻断理由作为一条 user 消息
        交回子代理继续干，`stop_hook_active` 从第二次起为 true。
        """
        nonlocal stop_blocks
        if child_hooks is None:
            return False
        results = list(
            await child_hooks.run(
                "subagent_stop",
                "",
                {
                    "stop_hook_active": stop_blocks > 0,
                    "last_assistant_message": str(text or "")[:4000],
                    "background_tasks": [],
                    "session_crons": [],
                    "agent_id": agent_id,
                    "agent_type": request.agent_name or BUILTIN_AGENT,
                    "agent_transcript_path": write_transcript(
                        messages,
                        run_root / ".blank" / "context" / "subagent-transcript.jsonl",
                    ),
                },
                # 上游的 SubagentStop 属于工具上下文事件，钩子输入里带这一轮的档位。
                effort=child_effort.level,
            )
            or []
        )
        if not results:
            return False
        await log.add(
            "hook.completed",
            "subagent",
            {"event": "subagent_stop", "results": [item.as_dict() for item in results]},
        )
        denied = [item for item in results if item.denied]
        if not denied:
            return False
        stop_blocks += 1
        cap = stop_hook_block_cap()
        reasons = [item.reason or "钩子要求先别结束这一轮" for item in denied]
        if stop_blocks > cap:
            await log.add(
                "hook.stop_capped",
                "subagent",
                {"blocks": stop_blocks - 1, "cap": cap, "reason": "；".join(reasons)[:500]},
            )
            return False
        messages.append({"role": "user", "content": "本轮暂不能结束：" + "；".join(reasons)})
        await log.add(
            "hook.turn_blocked",
            "subagent",
            {"blocks": stop_blocks, "cap": cap, "reason": "；".join(reasons)[:500]},
        )
        return True

    async def loop() -> None:
        nonlocal summary, steps
        while steps < request.max_steps:
            steps += 1
            built = await compactor.build(messages)
            chat = [
                ChatMessage(
                    role=item["role"],
                    content=item.get("content", ""),
                    tool_call_id=item.get("tool_call_id"),
                    tool_calls=item.get("tool_calls") or [],
                )
                for item in built
            ]
            response = await invoke(gateway.specs(), chat)
            assistant: dict = {"role": "assistant", "content": response.text}
            if response.tool_calls:
                assistant["tool_calls"] = response.tool_calls
            messages.append(assistant)
            if not response.tool_calls:
                if await stop_gate(response.text):
                    continue
                summary = response.text
                return
            for call in response.tool_calls:
                name = _tool_call_name(call)
                arguments = _tool_call_args(call)
                await log.add("tool.requested", "subagent", {"tool": name, "arguments": arguments})
                payload = await gateway.execute(name, arguments, context, "subagent")
                await log.add(
                    "tool.completed",
                    "subagent",
                    {
                        "tool": name,
                        "status": payload.get("status"),
                        "ok": payload.get("ok"),
                        "summary": str(payload.get("output") or payload.get("message") or "")[:300],
                        **sandbox_backend_event_field(payload),
                    },
                )
                messages.append(
                    {
                        "role": "tool",
                        "content": json.dumps(payload, ensure_ascii=False, default=str)[:8_000],
                        "tool_call_id": call.get("id") or name,
                    }
                )
        # 预算用尽：让模型在无工具的情况下收个尾
        chat = [ChatMessage(role=item["role"], content=item.get("content", "")) for item in messages]
        chat.append(ChatMessage(role="user", content="步骤已用完，请直接给出结论，不要再调用工具。"))
        response = await invoke([], chat)
        summary = response.text

    try:
        await asyncio.wait_for(loop(), request.timeout_seconds)
    except asyncio.TimeoutError:
        error = f"子代理超时（{request.timeout_seconds:.0f}s）"
        summary = summary or "子代理在超时前未给出结论"
    except ModelError as exc:
        error = exc.message
    except Exception as exc:  # 子代理失败绝不能拖垮父任务
        error = f"子代理执行失败：{exc.__class__.__name__}"

    diff = ""
    if tree is not None:
        try:
            diff = render_diff(tree)
        except Exception:
            diff = ""
        if request.keep_worktree:
            # 并行编排：副本先留着，等主代理决定要不要合并，再由 fan_out 统一清理
            await log.add(
                "worktree.kept", "subagent", {"path": str(tree.path), "mode": tree.mode}
            )
        else:
            await log.add("worktree.removed", "subagent", {"path": str(tree.path), "mode": tree.mode})
            removed_hooks = await remove_worktree_with_hooks(tree, hooks=child_hooks)
            if removed_hooks:
                await log.add(
                    "hook.completed",
                    "subagent",
                    {
                        "event": "worktree_remove",
                        "results": [item.as_dict() for item in removed_hooks],
                    },
                )
            # 副本没了，这个包对谁都不再有意义：留着就是一个能写已删目录的空边界。
            _drop_profile(isolation_profile)
        if tracker is not None:
            tracker.pop("tree", None)
            if not request.keep_worktree:
                tracker.pop("profile", None)

    result = SubAgentResult(
        ok=not error,
        summary=summary.strip() or ("子代理未给出结论" if not error else ""),
        steps=steps,
        depth=depth,
        agent_path=agent_path,
        agent_id=agent_id,
        worktree=tree.as_dict() if tree else {},
        diff=diff,
        error=error,
        usage={**turn, "latency_ms": int((time.perf_counter() - usage_started) * 1000)},
        parallel_group=request.parallel_group,
        thread_id=thread_id,
        tree=tree if request.keep_worktree else None,
        isolation_profile=isolation_profile if request.keep_worktree else "",
        conversation=messages,
        hook_warnings=hook_warnings,
    )
    await log.add(
        "subagent.completed",
        "subagent",
        {
            "ok": result.ok,
            "steps": result.steps,
            "summary": result.summary[:500],
            "error": result.error,
            "agent_id": agent_id,
            "fork": request.fork,
            "worktree_mode": tree.mode if tree else "",
            # 这一位花了多少、属于哪一批、是哪条线程——三个数原先只在 `model.usage` 里，
            # 批次要汇总就得先把整串事件按 trace_id 归堆；在这里直说一次，汇总才有可指认的来源。
            "thread_id": thread_id,
            "parallel_group": request.parallel_group,
            "usage": dict(result.usage),
            "context_window": compactor.context_window,
            "hook_warnings": len(hook_warnings),
        },
    )
    # 上游的 agent 完成通知：外部看到"这个后台代理回来了"，靠的不是轮询 task_output。
    # 通知钩子失败不改变结果——这时候活已经干完了。
    if child_hooks is not None:
        try:
            notified = list(
                await child_hooks.run(
                    "notification",
                    "agent_completed",
                    {
                        "message": result.summary[:500] or ("子代理失败：" + result.error if error else ""),
                        "title": f"子代理完成：{request.agent_name or BUILTIN_AGENT}",
                        "notification_type": "agent_completed",
                    },
                )
                or []
            )
        except Exception:  # noqa: BLE001 - 通知失败不许把已完成的子代理改成失败
            notified = []
        if notified:
            await log.add(
                "hook.completed",
                "subagent",
                {"event": "notification", "results": [item.as_dict() for item in notified]},
            )
    return result


class SubAgentRunner:
    """Late-bound runner: the session is built after the ToolContext it lives in."""

    def __init__(self) -> None:
        self.session = None
        self.spawned = 0

    def bind(self, session) -> "SubAgentRunner":
        self.session = session
        return self

    def _prepare(self, request: SubAgentRequest, *, agent_id: str = "") -> _PreparedSpawn | SubAgentResult:
        session = self.session
        if session is None:
            return SubAgentResult(ok=False, error="子代理运行器尚未绑定会话")
        depth = int(getattr(session, "depth", 0)) + 1
        if depth > MAX_DEPTH:
            return SubAgentResult(ok=False, error="子代理不允许再嵌套子代理", depth=depth)
        request = request.normalized()
        if not request.goal:
            return SubAgentResult(ok=False, error="子任务目标为空", depth=depth)
        self.spawned += 1
        agent_path = f"{getattr(session, 'agent_path', 'root')}/sub-{self.spawned}"
        return _PreparedSpawn(
            session=session,
            request=request,
            depth=depth,
            agent_path=agent_path,
            agent_id=agent_id or f"agent-{uuid.uuid4().hex[:8]}",
        )

    async def __call__(self, request: SubAgentRequest, *, agent_id: str = "") -> SubAgentResult:
        prepared = self._prepare(request, agent_id=agent_id)
        if isinstance(prepared, SubAgentResult):
            return prepared
        return await self._run_prepared(prepared)

    async def _run_prepared(
        self,
        prepared: _PreparedSpawn,
        *,
        tracker: dict | None = None,
        history: list[dict] | None = None,
        continuation: bool = False,
    ) -> SubAgentResult:
        session, request = prepared.session, prepared.request
        # 根线程从父会话一路往下传：孙代理的根仍然是最初那条线程，不是它的父亲。
        root_thread_id = str(getattr(session, "root_thread_id", "") or session.trace_id)
        tier = str(getattr(session, "sandbox_tier", "") or "")
        if request.fork:
            # 分身拿父会话此刻的工具表（少派发类），而不是只读基线。
            tools = _fork_tools(session)
            allowed = {tool.name for tool in tools}
            if history is None:
                history = list(getattr(session, "conversation_snapshot", None) or [])
            if not history:
                # 没有父对话就不是分身，是从零开始的普通子代理——那会给出
                # "我记得我们刚才说过"的错误答案，不如直接说拿不到。
                return SubAgentResult(
                    ok=False,
                    error="fork 子代理拿不到父会话对话（当前会话没有可继承的消息）",
                    depth=prepared.depth,
                    agent_path=prepared.agent_path,
                    agent_id=prepared.agent_id,
                )
        else:
            allowed = _child_allowed_tools(request)
            tools = [tool for tool in _all_tools() if tool.name in allowed]
        # 事件里报的是真正会生效的那一档（定义只收紧；fork 报父会话的档）。
        started_mode = MODE_ACCEPT_EDITS if request.isolate else MODE_DEFAULT
        fork_permissions = getattr(session, "permissions", None)
        if request.fork and fork_permissions is not None:
            started_mode = str(
                getattr(fork_permissions, "mode", started_mode) or started_mode
            )
        started_mode, _ = _child_permission_mode(
            started_mode,
            request.permission_mode,
            fork_inherits=bool(request.fork and fork_permissions is not None),
        )
        await session.recorder.add(
            "subagent.started",
            "subagent",
            {
                "goal": request.goal[:500],
                "isolate": request.isolate,
                "fork": request.fork,
                "max_steps": request.max_steps,
                "agent_type": request.agent_name or BUILTIN_AGENT,
                "agent_ignored": list(request.agent_ignored),
                "agent_tools": sorted(allowed),
                "agent_skills": list(request.skills),
                "agent_permission_mode": started_mode,
                # 定义声明的档位（未钳制）。真正生效的那一档在子代理内部按模型
                # 封顶裁决，被压下来时由 effort.warning 事件记录——这里只记声明，
                # 不在这里重算一遍，免得两处裁决各写一份、迟早写歪。
                "agent_effort_declared": request.effort,
                # 模型覆盖：空串表示沿用父会话模型（inherit / 未声明 / fork）。
                "agent_model": "" if request.fork or request.model in ("", "inherit") else request.model,
                "agent_path": prepared.agent_path,
                "agent_id": prepared.agent_id,
                "depth": prepared.depth,
                "parent_thread_id": session.trace_id,
                "root_thread_id": root_thread_id,
                "sandbox_tier": tier,
            },
        )
        provider_id, provider_name = _session_provider(session)
        result = await run_subagent(
            adapter=session.adapter,
            tools=tools,
            root=Path(session.context.root),
            task_id=session.context.task_id,
            session_factory=session.session_factory,
            recorder=session.recorder,
            request=request,
            agent_path=prepared.agent_path,
            depth=prepared.depth,
            parent_thread_id=session.trace_id,
            root_thread_id=root_thread_id,
            provider_id=provider_id,
            provider_name=provider_name,
            watermark=_inherited_watermark(session),
            sandbox_tier=tier,
            parent_tier=tier,
            agent_id=prepared.agent_id,
            tracker=tracker,
            hooks=getattr(session, "hooks", None),
            history=history,
            skill_catalog=getattr(session.context, "skills", None),
            parent_permissions=getattr(session, "permissions", None) if request.fork else None,
            allow_background_shell=bool(
                request.fork and getattr(session.context, "allow_background_shell", False)
            ),
            fork_gateway=getattr(session, "gateway", None) if request.fork else None,
            # 续聊是同一个子代理接着说话，不是新派发：SubagentStart 不再跑一遍。
            start_hooks=not continuation,
        )
        # 留给 send_message：无论这一轮成功还是失败，已经说过的话都要能接上。
        self._remember(prepared, result)
        return result

    # ------------------------------------------------------------------ #
    # 续聊：把上一轮的上下文接着用
    # ------------------------------------------------------------------ #
    def _remember(self, prepared: _PreparedSpawn, result: SubAgentResult) -> None:
        """把这一轮结束时的对话存进任务桶；隔离子代理只存标记，用于给出准确拒绝。"""
        session = self.session
        if session is None or not result.conversation:
            return
        task_id = str(getattr(getattr(session, "context", None), "task_id", "") or "")
        if not task_id:
            return
        bucket = _CONVERSATIONS.setdefault(task_id, {})
        raw = list(result.conversation)
        if len(raw) > MAX_CONVERSATION_MESSAGES:
            # 系统提示永远保留：丢了它续聊就没有规矩可依。
            raw = [raw[0], *raw[-(MAX_CONVERSATION_MESSAGES - 1):]]
        trimmed: list[dict] = []
        for item in raw:
            content = str(item.get("content") or "")
            if len(content) > MAX_CONVERSATION_CHARS:
                content = content[:MAX_CONVERSATION_CHARS] + "…（已截断）"
            trimmed.append({**item, "content": content})
        bucket[prepared.agent_id] = {
            "agent_path": prepared.agent_path,
            "isolate": bool(prepared.request.isolate),
            "messages": trimmed,
            # 续聊是同一个子代理接着说话：它的类型与工具边界不能悄悄退回默认，
            # 否则第一轮受限于只读，第二轮却拿到了写入工具。
            "agent_name": prepared.request.agent_name,
            "instructions": prepared.request.instructions,
            "allowed_tools": None
            if prepared.request.allowed_tools is None
            else list(prepared.request.allowed_tools),
            "disallowed_tools": list(prepared.request.disallowed_tools),
            "agent_ignored": list(prepared.request.agent_ignored),
            "skills": list(prepared.request.skills),
            "permission_mode": prepared.request.permission_mode,
            "max_steps": prepared.request.max_steps,
            "fork": bool(prepared.request.fork),
            # 模型覆盖也要带到下一轮：第一轮用 mock-fast、第二轮悄悄换回父模型，
            # 就是同一段对话中途换了个脑子。定义钩子同理——续聊仍是同一个子代理，
            # 第二轮不能悄悄失去它的 Stop/PreToolUse 守卫。
            "model": prepared.request.model,
            "agent_hooks": dict(prepared.request.agent_hooks),
            # 记忆作用域也要带到下一轮：同一个子代理接着说话，不能第二轮就丢失记忆。
            "memory_scope": prepared.request.memory_scope,
            # 指令继承开关同理：第一轮 omit 掉 AGENTS.md、第二轮又悄悄读回来，
            # 它就看见了第一轮没看见的项目约定。
            "omit_claude_md": bool(prepared.request.omit_claude_md),
        }
        while len(bucket) > MAX_CONVERSATIONS:
            bucket.pop(next(iter(bucket)))

    async def send_message(self, agent_id: str, message: str) -> SubAgentResult:
        """给一个已经跑过的子代理发后续消息，保留它原来的上下文。

        新的 ``spawn_agent`` 是全新上下文；续聊则接着上一轮的读、写与结论往下说。
        隔离子代理的副本跑完就删，上下文在、文件状态不在——那种情况明确拒绝，
        而不是让它在真实工作区里接着答一个它以为还在副本里的问题。
        """
        session = self.session
        if session is None:
            return SubAgentResult(ok=False, error="子代理运行器尚未绑定会话")
        task_id = str(getattr(getattr(session, "context", None), "task_id", "") or "")
        entry = _CONVERSATIONS.get(task_id, {}).get(agent_id)
        if entry is None:
            handle = self._handle(agent_id)
            if handle is not None and handle.task is not None and not handle.task.done():
                return SubAgentResult(
                    ok=False,
                    error=f"子代理 {agent_id} 还在后台运行，先用 task_output 等它结束再续聊",
                )
            return SubAgentResult(
                ok=False,
                error=f"没有该子代理的上下文：{agent_id}（只有本进程里跑过的子代理能续聊）",
            )
        if entry.get("isolate"):
            return SubAgentResult(
                ok=False,
                error=(
                    f"子代理 {agent_id} 使用隔离副本，副本跑完已清理，无法续聊；"
                    "请重新派一个子代理，或让它在这一轮里就把问题问完"
                ),
            )
        depth = int(getattr(session, "depth", 0)) + 1
        if depth > MAX_DEPTH:
            return SubAgentResult(ok=False, error="子代理不允许再嵌套子代理", depth=depth)
        allowed_tools = entry.get("allowed_tools")
        request = SubAgentRequest(
            goal=message,
            max_steps=int(entry.get("max_steps") or DEFAULT_STEPS),
            agent_name=str(entry.get("agent_name") or ""),
            instructions=str(entry.get("instructions") or ""),
            allowed_tools=None if allowed_tools is None else tuple(allowed_tools),
            disallowed_tools=tuple(entry.get("disallowed_tools") or ()),
            agent_ignored=tuple(entry.get("agent_ignored") or ()),
            skills=tuple(entry.get("skills") or ()),
            # 续聊是同一个子代理接着说话：fork 的工具面与权限也必须跟着回来，
            # 否则第二轮会悄悄退回到只读基线。定义里声明的审批档同样要记住，
            # 免得第一轮是 plan、第二轮却按 default 问一遍。
            permission_mode=str(entry.get("permission_mode") or ""),
            model=str(entry.get("model") or ""),
            agent_hooks=dict(entry.get("agent_hooks") or {}),
            memory_scope=str(entry.get("memory_scope") or ""),
            omit_claude_md=bool(entry.get("omit_claude_md")),
            fork=bool(entry.get("fork")),
        ).normalized()
        if not request.goal:
            return SubAgentResult(ok=False, error="续聊消息为空")
        prepared = _PreparedSpawn(
            session=session,
            request=request,
            depth=depth,
            agent_path=str(entry.get("agent_path") or ""),
            agent_id=agent_id,
        )
        return await self._run_prepared(
            prepared, history=list(entry["messages"]), continuation=True
        )

    # ------------------------------------------------------------------ #
    # 后台句柄：spawn 立即返回 agent_id，结论后来再取
    # ------------------------------------------------------------------ #
    async def spawn_background(self, request: SubAgentRequest) -> tuple[str, SubAgentResult | None]:
        """派一个后台子代理，返回 ``(agent_id, 立即失败结果)``。

        不在这里 await 子代理：那正是「后台」要避免的事。返回得越早，父代理
        越能继续做事；模型调用、副本创建都交给独立 Task。
        """
        prepared = self._prepare(request)
        if isinstance(prepared, SubAgentResult):
            return "", prepared
        handle = _BackgroundAgent(
            agent_id=prepared.agent_id,
            task_id=str(prepared.session.context.task_id),
            request=prepared.request,
            agent_path=prepared.agent_path,
            depth=prepared.depth,
            started_at=time.perf_counter(),
        )
        _BACKGROUND.setdefault(handle.task_id, {})[handle.agent_id] = handle
        try:
            handle.task = asyncio.create_task(self._run_in_background(prepared, handle))
        except RuntimeError as exc:  # 没有运行中的事件循环
            _BACKGROUND.get(handle.task_id, {}).pop(handle.agent_id, None)
            return "", SubAgentResult(
                ok=False,
                error=f"后台子代理启动失败：{exc}",
                agent_path=prepared.agent_path,
                depth=prepared.depth,
            )
        return handle.agent_id, None

    async def _run_in_background(self, prepared: _PreparedSpawn, handle: _BackgroundAgent) -> None:
        tracker: dict = {}
        try:
            handle.result = await self._run_prepared(prepared, tracker=tracker)
        except asyncio.CancelledError:
            # 取消点可能在 run_subagent 自己的 try 之前（比如刚建完副本），
            # 所以收尸责任在这里：按 tracker 找回副本、删掉、留一条可查的取消记录。
            await self._cleanup_cancelled(prepared, tracker)
            raise
        except Exception as exc:  # 子代理失败不能变成未处理异常
            handle.error = f"{exc.__class__.__name__}: {exc}"

    async def _cleanup_cancelled(self, prepared: _PreparedSpawn, tracker: dict) -> None:
        tree = tracker.pop("tree", None)
        profile = tracker.pop("profile", "")
        recorder = prepared.session.recorder
        if tree is not None:
            try:
                await asyncio.shield(
                    recorder.add(
                        "worktree.removed",
                        "subagent",
                        {"path": str(tree.path), "mode": tree.mode},
                    )
                )
            except Exception:
                pass
            try:
                await asyncio.shield(
                    remove_worktree_with_hooks(
                        tree, hooks=getattr(prepared.session, "hooks", None)
                    )
                )
            except Exception:
                remove_worktree(tree)
        # 包与副本同生共死：副本已经删掉，包也不能留。
        _drop_profile(profile)
        try:
            await asyncio.shield(
                recorder.add(
                    "subagent.cancelled",
                    "subagent",
                    {
                        "agent_id": prepared.agent_id,
                        "agent_path": prepared.agent_path,
                        "goal": prepared.request.goal[:300],
                        "worktree_removed": tree is not None,
                    },
                )
            )
        except Exception:
            pass

    def _handle(self, agent_id: str) -> _BackgroundAgent | None:
        session = self.session
        if session is None or not agent_id:
            return None
        return _BACKGROUND.get(str(session.context.task_id), {}).get(agent_id)

    def drain_notifications(self) -> list[dict]:
        """取走「后台子代理已结束」的通知，每个句柄只投递一次。

        主代理不会盯着 task_output 轮询：后台跑完的时刻它往往正在做别的事。
        这里把刚结束、还没通知过的句柄交回调用方，由它注入下一次模型调用——
        对齐 Claude Code 的后台 Agent 语义。已通知的句柄仍留在表里，task_output
        照样能取到完整结论；任务收尾时 aclose 再整批摘除。
        """
        session = self.session
        if session is None:
            return []
        task_id = str(getattr(getattr(session, "context", None), "task_id", "") or "")
        bucket = _BACKGROUND.get(task_id, {})
        notes: list[dict] = []
        for handle in bucket.values():
            if handle.notified:
                continue
            task = handle.task
            if task is None or not task.done():
                continue
            if task.cancelled():
                note = {
                    "agent_id": handle.agent_id,
                    "agent_path": handle.agent_path,
                    "goal": handle.request.goal[:200],
                    "status": "cancelled",
                    "ok": False,
                    "summary": "",
                    "error": "",
                }
            else:
                snapshot = self._snapshot(handle)
                note = {
                    "agent_id": handle.agent_id,
                    "agent_path": handle.agent_path,
                    "goal": handle.request.goal[:200],
                    "status": str(snapshot.get("status") or ""),
                    "ok": bool(snapshot.get("ok")),
                    "summary": str(snapshot.get("summary") or "")[:2000],
                    "error": str(snapshot.get("error") or handle.error or "")[:1000],
                }
            handle.notified = True
            notes.append(note)
        return notes

    def list_agents(self) -> list[dict]:
        """列出本任务的后台子代理（含已结束的），对齐 Claude Code 的 ListAgents。

        只读快照，不等待、不改变通知状态：running 的句柄照实说还在跑，
        已完成的不在这里重复摘要全文——结论仍由 task_output 取。
        """
        session = self.session
        if session is None:
            return []
        task_id = str(getattr(getattr(session, "context", None), "task_id", "") or "")
        bucket = _BACKGROUND.get(task_id, {})
        items: list[dict] = []
        for handle in bucket.values():
            snapshot = self._snapshot(handle)
            items.append(
                {
                    "agent_id": handle.agent_id,
                    "agent_path": handle.agent_path,
                    "goal": handle.request.goal[:200],
                    "status": str(snapshot.get("status") or ""),
                    "summary": str(snapshot.get("summary") or "")[:500],
                    "error": str(snapshot.get("error") or handle.error or "")[:500],
                    "elapsed_seconds": snapshot.get("elapsed_seconds", 0),
                }
            )
        return items

    async def output(self, agent_id: str, *, block: bool = True, timeout: float = 60.0) -> dict:
        """取后台子代理的现状；``block`` 时最多等 ``timeout`` 秒。

        等待用 ``asyncio.wait`` 而不是 ``wait_for``：后者的超时会取消被等的 Task，
        那样「等一会儿没等到」就变成了「把子代理杀了」——两种完全不同的语义。
        """
        handle = self._handle(agent_id)
        if handle is None:
            return {
                "ok": False,
                "status": "not_found",
                "agent_id": agent_id,
                "message": f"没有找到子代理：{agent_id}（后台句柄只在本进程内有效）",
            }
        if block and handle.task is not None and not handle.task.done():
            # 下限是 0，不是 1 秒：0 表示"只看一眼"，与 block=false 等价。
            # 硬抬到 1 秒会让调用方以为自己在轮询，实际每次都被按住一秒。
            wait_seconds = max(0.0, min(MAX_TIMEOUT, float(60 if timeout is None else timeout)))
            await asyncio.wait({handle.task}, timeout=wait_seconds)
        return self._snapshot(handle)

    async def settle_background(self, timeout: float | None = None) -> dict:
        """任务收尾前给后台子代理一个有界等待窗口，返回最终快照。

        子代理默认后台跑，但任务一旦收尾，仍在跑的句柄会被 ``aclose`` 取消——结论
        直接丢失，交付里却看不出来。所以 summarize 之前先等一段有限时间：窗口内结束
        的，结论能进交付；还没结束的在这里取消，并把它们点名写进交付。等待有上限，
        主代理已经写完结论时，界面不该再干等十分钟。
        """
        session = self.session
        if session is None:
            return {"waited_seconds": 0.0, "finished": [], "cancelled": []}
        # 每次调用时读模块常量：测试与运维都能在不重建 runner 的情况下收紧窗口。
        budget = BACKGROUND_SETTLE_SECONDS if timeout is None else float(timeout)
        task_id = str(getattr(getattr(session, "context", None), "task_id", "") or "")
        bucket = _BACKGROUND.get(task_id, {})
        pending = [
            handle
            for handle in bucket.values()
            if handle.task is not None and not handle.task.done()
        ]
        started = time.perf_counter()
        if pending and budget > 0:
            # 和 output 一样用 wait 而不是 wait_for：超时是"没等到"，不是"杀掉"；
            # 真正要取消的由下面显式处理，取消也要留记录。
            await asyncio.wait(
                {handle.task for handle in pending}, timeout=max(0.0, budget)
            )
        cancelled_ids: list[str] = []
        for handle in bucket.values():
            if handle.task is not None and not handle.task.done():
                handle.task.cancel()
                cancelled_ids.append(handle.agent_id)
        for handle in bucket.values():
            if handle.task is None:
                continue
            try:
                await handle.task
            except (asyncio.CancelledError, Exception):
                pass
        finished: list[dict] = []
        cancelled: list[dict] = []
        for handle in bucket.values():
            snapshot = self._snapshot(handle)
            item = {
                "agent_id": handle.agent_id,
                "agent_path": handle.agent_path,
                "goal": handle.request.goal[:200],
                "status": str(snapshot.get("status") or ""),
                "ok": bool(snapshot.get("ok")),
                "summary": str(snapshot.get("summary") or "")[:2000],
                "error": str(snapshot.get("error") or handle.error or "")[:1000],
            }
            # 已通知过的不再重复投递：这里给出的就是最终口径。
            handle.notified = True
            if handle.agent_id in cancelled_ids:
                cancelled.append(item)
            else:
                finished.append(item)
        return {
            "waited_seconds": round(time.perf_counter() - started, 3),
            "finished": finished,
            "cancelled": cancelled,
        }

    def _snapshot(self, handle: _BackgroundAgent) -> dict:
        elapsed = round(time.perf_counter() - handle.started_at, 3)
        base = {
            "agent_id": handle.agent_id,
            "agent_path": handle.agent_path,
            "goal": handle.request.goal[:200],
            "elapsed_seconds": elapsed,
        }
        task = handle.task
        if task is not None and not task.done():
            return {**base, "ok": True, "status": "running", "message": "子代理仍在运行"}
        if task is not None and task.cancelled():
            return {**base, "ok": False, "status": "cancelled", "message": "子代理已取消"}
        result = handle.result
        if result is None:
            return {
                **base,
                "ok": False,
                "status": "failed",
                "message": handle.error or "子代理异常结束",
            }
        return {
            **base,
            "ok": result.ok,
            "status": "completed" if result.ok else "failed",
            "summary": result.summary,
            "steps": result.steps,
            "error": result.error,
            "worktree": result.worktree,
            "diff": result.diff,
            "usage": result.usage,
            "thread_id": result.thread_id,
            "text": result.render(),
        }

    async def stop(self, agent_id: str) -> dict:
        """取消一个后台子代理；已经结束的句柄照实返回它当初的结果，不谎报取消。"""
        handle = self._handle(agent_id)
        if handle is None:
            return {
                "ok": False,
                "status": "not_found",
                "agent_id": agent_id,
                "message": f"没有找到子代理：{agent_id}（后台句柄只在本进程内有效）",
            }
        if handle.task is not None and not handle.task.done():
            handle.task.cancel()
            try:
                await handle.task
            except (asyncio.CancelledError, Exception):
                pass
        return self._snapshot(handle)

    async def aclose(self, task_id: str = "", *, reason: str = "task_finished") -> int:
        """任务收尾：取消仍在跑的后台子代理，并点名说明，返回取消的个数。

        不取消它们，任务显示"已完成"之后模型调用还在继续花钱，事件也会写进一个
        已经收尾的任务里——账和界面从此对不上。已完成但没人取过的句柄一并摘除：
        结论已经写进 `subagent.completed` 事件，不需要一直占内存。
        """
        session = self.session
        task_id = task_id or str(getattr(getattr(session, "context", None), "task_id", "") or "")
        # 任务收尾之后没有任何续聊的合法入口，对话缓存一并摘掉，别把上下文留在进程里。
        _CONVERSATIONS.pop(task_id, None)
        bucket = _BACKGROUND.pop(task_id, {})
        if not bucket:
            return 0
        running = [handle for handle in bucket.values() if handle.task is not None and not handle.task.done()]
        for handle in running:
            handle.task.cancel()
        for handle in bucket.values():
            if handle.task is None:
                continue
            try:
                await handle.task
            except (asyncio.CancelledError, Exception):
                pass
        recorder = getattr(session, "recorder", None)
        if recorder is not None:
            try:
                await recorder.add(
                    "subagents.released",
                    "subagent",
                    {
                        "reason": reason,
                        "count": len(bucket),
                        "cancelled": [handle.agent_id for handle in running],
                        "finished": [handle.agent_id for handle in bucket.values() if handle not in running],
                    },
                )
            except Exception:
                pass
        return len(running)

    async def fan_out(
        self,
        requests: list[SubAgentRequest],
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
        merge: bool = False,
    ) -> list[SubAgentResult]:
        """并行跑一组子代理，各自一个副本，然后按需做保守合并。

        并行编排真正的价值在"同时分头探索"，所以这里不做模型级调度：
        每个子代理拿到独立目标、独立上下文、独立副本，最后只把结论和 diff 汇总。
        合并默认关闭——探索的产物应当由主代理决定怎么落，而不是自动写进主工作区。
        """
        session = self.session
        if session is None:
            return [SubAgentResult(ok=False, error="子代理运行器尚未绑定会话")]
        depth = int(getattr(session, "depth", 0)) + 1
        if depth > MAX_DEPTH:
            return [SubAgentResult(ok=False, error="子代理不允许再嵌套子代理", depth=depth)]
        bounded = list(requests)[:MAX_PARALLEL_AGENTS]
        if not bounded:
            return []
        group = uuid.uuid4().hex[:6]
        keep = bool(merge)
        limit = max(1, min(MAX_PARALLEL_AGENTS, int(concurrency or DEFAULT_CONCURRENCY)))
        await session.recorder.add(
            "subagents.planned",
            "subagent",
            {
                "group": group,
                "count": len(bounded),
                "concurrency": limit,
                "merge": merge,
                "goals": [item.goal[:200] for item in bounded],
                "depth": depth,
            },
        )

        semaphore = asyncio.Semaphore(limit)
        all_tools = _all_tools()
        provider_id, provider_name = _session_provider(session)
        root_thread_id = str(getattr(session, "root_thread_id", "") or session.trace_id)
        tier = str(getattr(session, "sandbox_tier", "") or "")

        async def one(index: int, item: SubAgentRequest) -> SubAgentResult:
            async with semaphore:
                self.spawned += 1
                agent_path = f"{getattr(session, 'agent_path', 'root')}/par-{group}-{index + 1}"
                request = item.normalized()
                request.keep_worktree = keep
                request.parallel_group = group
                allowed = _child_allowed_tools(request)
                started_mode, _ = _child_permission_mode(
                    MODE_ACCEPT_EDITS if request.isolate else MODE_DEFAULT,
                    request.permission_mode,
                )
                await session.recorder.add(
                    "subagent.started",
                    "subagent",
                    {
                        "goal": request.goal[:500],
                        "isolate": request.isolate,
                        "max_steps": request.max_steps,
                        "agent_type": request.agent_name or BUILTIN_AGENT,
                        "agent_ignored": list(request.agent_ignored),
                        "agent_tools": sorted(allowed),
                        "agent_permission_mode": started_mode,
                        "agent_path": agent_path,
                        "depth": depth,
                        "parent_thread_id": session.trace_id,
                        "root_thread_id": root_thread_id,
                        "sandbox_tier": tier,
                        "parallel_group": group,
                        "index": index + 1,
                    },
                )
                return await run_subagent(
                    adapter=session.adapter,
                    tools=[tool for tool in all_tools if tool.name in allowed],
                    root=Path(session.context.root),
                    task_id=session.context.task_id,
                    session_factory=session.session_factory,
                    recorder=session.recorder,
                    request=request,
                    agent_path=agent_path,
                    depth=depth,
                    parent_thread_id=session.trace_id,
                    root_thread_id=root_thread_id,
                    provider_id=provider_id,
                    provider_name=provider_name,
                    watermark=_inherited_watermark(session),
                    sandbox_tier=tier,
                    parent_tier=tier,
                    skill_catalog=getattr(session.context, "skills", None),
                )

        results = await asyncio.gather(
            *(one(index, item) for index, item in enumerate(bounded)), return_exceptions=False
        )
        # 批次收口：一次说清"这一批派了谁、各自花了多少、合计多少"。
        # 逐个的 `subagent.completed` 已经各自带了用量，但那是一条条散着的行；
        # 主代理的预算与「证明」面板要指认的是**这一批**，所以合计必须有一个可指的出处，
        # 而不是让读的人自己把 N 条事件加起来。
        total = batch_usage(list(results))
        await session.recorder.add(
            "subagents.completed",
            "subagent",
            {
                "group": group,
                "count": len(results),
                "succeeded": sum(1 for item in results if item.ok),
                "merge": merge,
                "depth": depth,
                "parent_thread_id": session.trace_id,
                "root_thread_id": root_thread_id,
                "children": [
                    {
                        "agent_path": item.agent_path,
                        "thread_id": item.thread_id,
                        "ok": item.ok,
                        "steps": item.steps,
                        "usage": dict(item.usage or {}),
                    }
                    for item in results
                ],
                "usage": total,
                # 这批的共同水位：排查"子代理为什么压得比主代理早"时看这一条。
                "context_window": int(_inherited_watermark(session).get("context_window") or 0),
            },
        )
        if merge:
            await self._merge(results, group)
        for result in results:
            if result.tree is not None:
                await remove_worktree_with_hooks(
                    result.tree, hooks=getattr(session, "hooks", None)
                )
                result.tree = None
                _drop_profile(result.isolation_profile)
                result.isolation_profile = ""
        return list(results)

    async def _merge(self, results: list[SubAgentResult], group: str) -> None:
        """把各副本的改动按"无冲突才写入"的规则落回主工作区。"""
        session = self.session
        owners: dict[str, list[int]] = {}
        for index, result in enumerate(results):
            if result.tree is None:
                continue
            for entry in changed_files(result.tree):
                owners.setdefault(entry["path"], []).append(index)
        conflicts = {path for path, indexes in owners.items() if len(indexes) > 1}
        for index, result in enumerate(results):
            if result.tree is None:
                continue
            # 同一文件被多个子代理改过：谁的都不写，交回 diff
            shared = {path for path in conflicts if index in owners.get(path, [])}
            report = apply_changes(result.tree, blocked=shared)
            result.merge = report.as_dict()
            if session is not None:
                await session.recorder.add(
                    "worktree.merged",
                    "subagent",
                    {
                        "group": group,
                        "agent_path": result.agent_path,
                        **report.as_dict(),
                    },
                )


def _all_tools() -> list:
    from blank_api.services.tools import build_default_tools

    return build_default_tools()

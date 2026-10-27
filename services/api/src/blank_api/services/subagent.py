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
from blank_api.services.hooks import HookRunner
from blank_api.services.models.base import ChatMessage, ModelAdapter, ModelError, Usage
from blank_api.services.permissions import MODE_ACCEPT_EDITS, MODE_DEFAULT, PermissionPolicy
from blank_api.services.safety import SafetyPolicy
from blank_api.services.sandbox.tiers import TierDowngrade, assert_not_looser
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway, sandbox_backend_event_field
from blank_api.services.worktree import (
    Worktree,
    apply_changes,
    changed_files,
    create_worktree_with_hooks,
    remove_worktree,
    remove_worktree_with_hooks,
    render_diff,
)

READ_ONLY_TOOLS = (
    "read_file",
    "list_files",
    "search_code",
    "git",
    "web_search",
    "web_fetch",
)
ISOLATED_TOOLS = (
    *READ_ONLY_TOOLS,
    "apply_patch",
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
# 后台句柄只在本进程内有效：重启后 task_output 会如实说"找不到"，而不是伪造一个结果。
# 按任务分桶、键是 agent_id。放模块级而不是 runner 实例上，是因为审批恢复会重建
# 一个 runner——句柄若绑在实例上，恢复之后 task_output 就再也找不到它了。
_BACKGROUND: dict[str, dict[str, "_BackgroundAgent"]] = {}

SUBAGENT_SYSTEM = (
    "你是一个受限的子代理，只负责完成交给你的单个子任务。\n"
    "规则：\n"
    "1. 你无法向用户提问，也无法请求审批；需要审批的动作会被直接拒绝，请换一种做法。\n"
    "2. 少而准地调用工具，最多 {steps} 步，完成后用一段中文说明你查到了什么或改了什么。\n"
    "3. 不要尝试越出工作区，不要执行不可逆命令。"
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

    def normalized(self) -> "SubAgentRequest":
        return SubAgentRequest(
            goal=self.goal.strip()[:2_000],
            isolate=bool(self.isolate),
            max_steps=max(1, min(MAX_STEPS, int(self.max_steps or DEFAULT_STEPS))),
            timeout_seconds=max(MIN_TIMEOUT, min(MAX_TIMEOUT, float(self.timeout_seconds or 120.0))),
            label=self.label,
            keep_worktree=bool(self.keep_worktree),
            parallel_group=self.parallel_group,
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
        return payload

    def render(self) -> str:
        parts = [self.summary.strip() or "子代理未给出结论"]
        if self.diff:
            parts.append("【隔离副本中的改动】\n" + self.diff)
        if self.merge:
            parts.append("【合并结果】" + render_merge(self.merge))
        if self.error:
            parts.append(f"【异常】{self.error}")
        return "\n\n".join(parts)


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
) -> SubAgentResult:
    request = request.normalized()
    if not request.goal:
        return SubAgentResult(ok=False, error="子任务目标为空", agent_path=agent_path, depth=depth)
    try:
        sandbox_tier = assert_not_looser(parent_tier, sandbox_tier)
    except TierDowngrade as exc:
        # 子代理只能继承父级的边界，不许自己挑一个更宽的。这不是参数错，是越权。
        return SubAgentResult(ok=False, error=str(exc), agent_path=agent_path, depth=depth)

    tree: Worktree | None = None
    run_root = Path(root)
    isolated = request.isolate
    if isolated:
        try:
            tree, hook_results = await create_worktree_with_hooks(
                run_root, label=request.label or "sub", hooks=hooks
            )
        except ValueError as exc:
            return SubAgentResult(
                ok=False, error=str(exc), agent_path=agent_path, depth=depth
            )
        run_root = tree.path
        if tracker is not None:
            # 先登记再发事件：事件写库也是一个 await，取消落在这里时副本必须已可被找回。
            tracker["tree"] = tree
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

    gateway = ToolGateway(tools, defer=False)
    permissions = PermissionPolicy.build(
        MODE_ACCEPT_EDITS if isolated else MODE_DEFAULT,
        approved_tools=set(),
        approved_actions=set(),
        # 子代理继承父任务的档位，不许自己挑一个更宽的执行边界。
        sandbox_tier=sandbox_tier,
    )
    context = ToolContext(
        root=run_root,
        task_id=task_id,
        session_factory=session_factory,
        approval_policy=permissions.decide,
        trace_id=parent_thread_id,
        permissions=permissions,
        sandbox_tier=sandbox_tier,
        # 子代理不能成为绕过客户安全设置的侧门：同一台机器、同一份策略。
        safety=SafetyPolicy.load(),
        tool_registry=gateway.registry,
    )
    log = _Recorder(
        recorder,
        agent_path=agent_path,
        depth=depth,
        parent_thread_id=parent_thread_id,
        root_thread_id=root_thread_id,
    )
    compactor = ContextCompactor(**(watermark or {}))
    messages: list[dict] = [
        {"role": "system", "content": SUBAGENT_SYSTEM.format(steps=request.max_steps)},
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
            response = await adapter.bind_tools(specs, chat)
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
            removed_hooks = await remove_worktree_with_hooks(tree, hooks=hooks)
            if removed_hooks:
                await log.add(
                    "hook.completed",
                    "subagent",
                    {
                        "event": "worktree_remove",
                        "results": [item.as_dict() for item in removed_hooks],
                    },
                )
        if tracker is not None:
            tracker.pop("tree", None)

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
            "worktree_mode": tree.mode if tree else "",
            # 这一位花了多少、属于哪一批、是哪条线程——三个数原先只在 `model.usage` 里，
            # 批次要汇总就得先把整串事件按 trace_id 归堆；在这里直说一次，汇总才有可指认的来源。
            "thread_id": thread_id,
            "parallel_group": request.parallel_group,
            "usage": dict(result.usage),
            "context_window": compactor.context_window,
        },
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

    async def _run_prepared(self, prepared: _PreparedSpawn, *, tracker: dict | None = None) -> SubAgentResult:
        session, request = prepared.session, prepared.request
        # 根线程从父会话一路往下传：孙代理的根仍然是最初那条线程，不是它的父亲。
        root_thread_id = str(getattr(session, "root_thread_id", "") or session.trace_id)
        tier = str(getattr(session, "sandbox_tier", "") or "")
        allowed = ISOLATED_TOOLS if request.isolate else READ_ONLY_TOOLS
        tools = [tool for tool in _all_tools() if tool.name in allowed]
        await session.recorder.add(
            "subagent.started",
            "subagent",
            {
                "goal": request.goal[:500],
                "isolate": request.isolate,
                "max_steps": request.max_steps,
                "agent_path": prepared.agent_path,
                "agent_id": prepared.agent_id,
                "depth": prepared.depth,
                "parent_thread_id": session.trace_id,
                "root_thread_id": root_thread_id,
                "sandbox_tier": tier,
            },
        )
        provider_id, provider_name = _session_provider(session)
        return await run_subagent(
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
        allowed = ISOLATED_TOOLS
        tools = [tool for tool in _all_tools() if tool.name in allowed]
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
                await session.recorder.add(
                    "subagent.started",
                    "subagent",
                    {
                        "goal": request.goal[:500],
                        "isolate": request.isolate,
                        "max_steps": request.max_steps,
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
                    tools=tools,
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

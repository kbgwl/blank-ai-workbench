"""LangGraph plan / act / observe workflow for a Task."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from blank_api.limits import DEFAULT_MAX_STEPS, WRAP_UP_STEPS
from blank_api.models.model_provider import ModelUsage
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.intent import (
    INTENT_ACT,
    INTENT_ANSWER,
    INTENT_INSTRUCTION,
    REPORT_INSTRUCTION,
    as_report,
    guess_intent,
    normalize_answer,
    parse_intent,
)
from blank_api.services.agent.context import (
    ContextCompactor,
    build_summary_prompt,
    load_summary_json,
    render_summary,
)
from blank_api.services.agent.router import LIGHT, TIER_LABELS, Router
from blank_api.services.features import is_enabled as feature_enabled
from blank_api.services.guardian import Guardian
from blank_api.services.hooks import HookRunner, blocking_denial
from blank_api.services.models.base import ChatMessage, ModelAdapter, ModelError, Usage
from blank_api.services.permissions import (
    MODE_DEFAULT,
    SCOPE_ONCE,
    PermissionPolicy,
    approval_options,
    is_irreversible,
    memoizable_prefix,
    parse_decision,
)
from blank_api.services.tools.ask import normalize_questions, parse_answers, render_answers
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, approval_key
from blank_api.services.tools.gateway import ToolGateway
from blank_api.services.tools.registry import PARALLEL_SAFE_TOOLS, PROFILE_FULL, PROFILE_LEAN

MAX_REPAIR_ROUNDS = 3


def tool_profile(tier: str) -> str:
    """模型档位 → 工具表视图。

    只有轻量档收窄，标准和重型都保持全量。反过来的诱惑是"重型给更多"，但那些
    工具本来就在按需加载里（full 已经是最全的一份），再"多给"只能是把 deferred
    也全下发——那等于把 schema 成本和选择难度一起抬上去，正好和精简的目的相反。

    tier 为空表示这一步没有路由（开关关着，或池子里只有一个候选）。此时必须
    返回 full：beta 开关的效果只能是"关掉就完全不变"。
    """
    return PROFILE_LEAN if tier == LIGHT else PROFILE_FULL

SYSTEM_PROMPT = (
    "你是空白格 AI 操作台的执行 Agent，工作在受限工作区内。\n"
    "做事规则：\n"
    "1. 只能通过工具读写工作区，路径一律使用工作区相对路径，禁止绝对路径与 ..。\n"
    "2. 按已确认的步骤调用工具执行，每次拿到结果再决定下一步，不要重复调用同一个工具。\n"
    "3. 修改文件用 apply_patch，验证用 run_tests。\n"
    "4. 工具按需加载：git、联网、记忆检索、子代理不在默认列表里，"
    "需要时先用 tool_search 检索（例如「提交」「查资料」「并行」），命中后本轮即可直接调用。"
    "列表里看不到某个工具（例如 run_tests）时同理，先检索再调用，不要因此跳过验证。\n"
    # 语气纪律：模型默认会写「好的，让我来分析一下」，在对话流里读起来全是噪音。
    "回复纪律：\n"
    "5. 不写开场白（好的 / 当然 / 让我 / 首先），不复述用户的话，不解释你打算怎么做。\n"
    "6. 交付时只讲三件事：做完了什么、还差什么、建议用户下一步做什么。多余的话不要写。\n"
    "7. 篇幅默认不超过 5 行；用户明确要清单或要详细说明时才展开，展开也只写短句条目。\n"
    "8. 闲聊、问概念、解释已有代码这类不需要动工作区的请求，直接回答，不要调用工具。"
)


class AgentState(TypedDict, total=False):
    task_id: str
    messages: list[dict]
    plan: list[str]
    steps: int
    repair_rounds: int
    tool_results: list[dict]
    pending: list[dict]
    acceptance_results: list[dict]
    usage: dict
    validation: dict
    review: dict
    review_rounds: int
    summary: str
    status: str
    error: dict
    # 步数预算已耗尽：此后只能取证与总结，不能再进入修复回路，否则图会空转到递归上限。
    budget_exhausted: bool
    # update_plan 写回的结构化计划（plan 保留为步骤文本，向后兼容）。
    plan_steps: list[dict]
    # 模型判定这次不用动工作区，直接回答了（闲聊/概念提问），执行回合整段跳过。
    direct_answer: bool


async def _never_cancelled() -> bool:
    return False


@dataclass
class AgentSession:
    adapter: ModelAdapter
    gateway: ToolGateway
    context: ToolContext
    recorder: EventRecorder
    session_factory: async_sessionmaker[AsyncSession]
    provider_id: str
    provider_name: str
    goal: str
    trace_id: str = ""
    skill_instructions: list[str] = field(default_factory=list)
    role_adapters: dict[str, ModelAdapter] = field(default_factory=dict)
    role_provider_ids: dict[str, str] = field(default_factory=dict)
    # 按需选模型（特性开关 model_routing）。router 为 None 时 call_model 完全走老路径；
    # routing_adapters 是「本次任务已选定的供应商」的适配器，路由只在这个集合里挑，
    # 绝不会把任务发给用户没为这个任务选过的供应商。
    router: Router | None = None
    routing_adapters: dict[str, ModelAdapter] = field(default_factory=dict)
    # 工具表按档位裁剪（特性开关 tiered_tools）。默认 False —— 关着时 registry
    # 恒为 full 档，与裁剪功能上线前逐字节一致。
    tiered_tools: bool = False
    acceptance_criteria: list[str] = field(default_factory=list)
    max_steps: int = DEFAULT_MAX_STEPS
    permissions: PermissionPolicy = field(default_factory=PermissionPolicy)
    is_cancelled: Callable[[], Awaitable[bool]] = _never_cancelled
    compactor: ContextCompactor = field(default_factory=ContextCompactor)
    hooks: HookRunner = field(default_factory=HookRunner)
    # 最近一次组装的上下文 token 估算，前端"上下文 8.2k"那一行读它。
    context_tokens: int = 0
    # 已经被记录过的问题调用：节点会因中断被重放，不记账就会重复写 question.* 事件。
    recorded_questions: set[str] = field(default_factory=set)
    # 中途插话（steer）的度量：次数与生效步，用来说明人机接力是否真的省事。
    steer_metrics: dict = field(default_factory=lambda: {"count": 0, "steps": []})
    # 已经消费到的 steer 事件 seq，防止同一条插话被反复注入
    steer_cursor: int = 0
    # 独立审查员：默认关闭（无适配器），由 runtime 按 BLANK_GUARDIAN 装配。
    guardian: Guardian = field(default_factory=Guardian)
    # 跨窗口交接：notes 在压缩后与换窗口后的第一次模型调用时注入
    notes: Any = None
    memory: Any = None
    shell: Any = None
    # 分层指令的来源（只用于审计，不重复进 prompt）
    instruction_layers: list = field(default_factory=list)
    notes_injected: bool = False
    # 工作区快照只拍一次，时机是「意图判定为动作类、第一个动作之前」。
    # 纯问答任务不写工作区，也就不拍——大仓库里那一下就是几十秒白等。
    snapshot_id: str = ""
    snapshot_ready: bool = False
    # 代理树位置：子代理事件靠这两个字段还原"谁在哪个分支上干活"。
    depth: int = 0
    agent_path: str = "root"

    @property
    def permission_mode(self) -> str:
        return self.permissions.mode or MODE_DEFAULT

    def approval_policy(self, tool: ToolDefinition, arguments: dict) -> bool:
        """旧的布尔口径：批准与否。新代码请直接用 gateway.decide / permissions.decide。"""
        return self.permissions.decide(tool, arguments).allowed

    async def ask(self, state: AgentState, prompt: str, node: str, *, messages: list[dict] | None = None) -> dict:
        messages = list(messages) if messages is not None else list(state.get("messages") or [])
        messages = messages + [{"role": "user", "content": prompt}]
        # 中途插话：在每次模型调用前取一次，插话在"下一步"生效，撤回不了已经跑完的动作
        messages = messages + await drain_steer(self, node, int(state.get("steps") or 0))
        response, usage = await self.call_model(messages, node, step=int(state.get("steps") or 0))
        assistant: dict[str, Any] = {"role": "assistant", "content": response.text}
        if response.tool_calls:
            assistant["tool_calls"] = response.tool_calls
        return {
            "text": response.text,
            "tool_calls": response.tool_calls,
            "usage": usage,
            "messages": [*messages, assistant],
        }

    async def ensure_snapshot(self, node: str = "create_plan") -> str:
        """动手之前留一份原样。已经拍过就什么都不做。

        快照是安全网，不是前置条件：拍不了也要继续跑（单文件回滚另有写入前备份兜底），
        但必须在事件里说清楚为什么没拍——静默跳过会让人以为还有一份可以回滚的副本。

        审批恢复会重建 AgentSession，内存标记随之丢失，所以还要回查磁盘上已有的快照，
        否则每次批准都会再复制一遍整个工作区。
        """
        if self.snapshot_ready or self.snapshot_id:
            return self.snapshot_id
        from blank_api.services.sandbox import SnapshotStore

        store = SnapshotStore()
        try:
            existing = await asyncio.to_thread(store.list, self.context.task_id)
        except Exception:  # noqa: BLE001 - 查不到就当作没有，重新拍一份更安全
            existing = []
        if existing:
            self.snapshot_id = str((existing[-1] or {}).get("id") or "")
            self.snapshot_ready = True
            await self.recorder.add("snapshot.reused", node, {"snapshot_id": self.snapshot_id})
            return self.snapshot_id
        try:
            # capture() 是同步的文件遍历，必须放进线程：工作区指到大仓库时会把事件循环
            # 占住几十秒——表现就是任务启动之后后端连 /health 都不回，界面上像彻底卡死。
            snapshot = await asyncio.to_thread(
                store.capture,
                str(self.context.root),
                reason="任务执行前快照",
                task_id=self.context.task_id,
            )
        except Exception as error:  # noqa: BLE001 - 任何快照问题都不该中断任务
            self.snapshot_ready = True
            await self.recorder.add("snapshot.failed", node, {"reason": str(error)})
            return ""
        self.snapshot_id = str(snapshot.get("id") or "")
        self.snapshot_ready = True
        await self.recorder.add(
            "snapshot.created",
            node,
            {
                "snapshot_id": self.snapshot_id,
                # 大仓库有上万条，事件里只放个摘要，完整清单在 meta.json
                "files": snapshot["files"][:200],
                "file_count": len(snapshot["files"]),
                "copied": snapshot.get("copied", True),
                "skipped_reason": snapshot.get("skipped_reason", ""),
            },
        )
        return self.snapshot_id

    def notes_handoff(self) -> str:
        """压缩或换窗口时要带回来的交接笔记；没有笔记时返回空串。"""
        store = self.notes
        if store is None:
            return ""
        try:
            return store.read(self.context.task_id).handoff()
        except Exception:  # 笔记是可选的，读写失败绝不能中断任务
            return ""

    async def summarize_history(self, folded: list[dict], previous: str) -> str:
        """把滑出上下文窗口的历史折叠进五要素摘要。

        这里直接调用 adapter.invoke，不经过 call_model —— 否则压缩会递归触发压缩。
        """
        prompt = build_summary_prompt(folded, previous)
        chat = [ChatMessage(role=item["role"], content=item["content"]) for item in prompt]
        started = time.perf_counter()
        try:
            response = await self.adapter.invoke(chat)
        except ModelError:
            await self._record_usage("context_compact", "", Usage(), 0, "error", role="summarizer")
            raise
        await self._record_usage(
            "context_compact",
            response.model,
            response.usage,
            int((time.perf_counter() - started) * 1000),
            "ok",
            role="summarizer",
        )
        return render_summary(load_summary_json(response.text))

    async def _routed_adapter(
        self, node: str, step: int, default: ModelAdapter
    ) -> tuple[ModelAdapter, str]:
        """按需选模型：在本任务的候选里挑「够用且最便宜」的那个。

        返回 (适配器, 档位)。档位是第二步要用的信息——同一个决定既决定了"用哪个
        模型"，也决定了"给它看多少个工具"，两件事分开发两次判断就会分叉。

        三条边界：
        * 打分只看已有状态，不额外调模型——路由自己花一次模型调用，省下的钱就没了。
        * 选中的供应商必须在 routing_adapters 里。不在就退回默认：宁可放弃省钱，
          也不能把任务发给用户没为这个任务选过的供应商。
        * needs_tools 一律按 True 算。这几个节点都可能返回工具调用，把一个不支持
          工具的模型选进来，只会在下一轮变成一次没人注意的静默降级。
        """
        decision = self.router.choose(
            node=node,
            context_tokens=self.context_tokens,
            step=step,
            needs_tools=True,
            has_criteria=bool(self.acceptance_criteria),
        )
        picked = self.routing_adapters.get(decision.provider_id)
        if picked is None:
            # 退回默认供应商时也退回全量工具表：这一步没有"按需"可言，
            # 用一个没被选中的档位去裁默认模型的工具，纯属无据可依。
            return default, ""
        switched = picked.config.name != default.config.name
        # 记账在返回之前做：这条事件发出去之后，用户就能在收尾里看到省了多少，
        # 而那个比值是这个 beta 特性该不该转正的唯一依据。
        self.router.note_decision(decision, switched=switched)
        # 事件里同时给「为什么选它」和「为什么没选别人」，否则路由就是个黑盒：
        # 出了问题只能猜，而猜错的代价是换个模型再跑一次。
        await self.recorder.add(
            "model.routed",
            node,
            {
                **decision.as_dict(),
                "default": default.config.name,
                "switched": switched,
                "tier_label": TIER_LABELS.get(decision.tier, decision.tier),
            },
        )
        return picked, decision.tier

    async def _scope_tools(self, node: str, tier: str) -> None:
        """按档位收窄这一轮下发的工具表。

        只在可见集合真的变了时记一条事件：每步都记会把活动流刷成噪音，而人想知道
        的只有"什么时候菜单变了、变成什么样"。被藏起来的名字也一并放进事件里，
        否则"工具没了"在界面上永远是个无法解释的现象。
        """
        if not self.tiered_tools:
            # 开关关着时连 set_profile 都不调：它虽然只会把 full 写回 full，
            # 但"绝对不动"比"动了但结果一样"更容易证明。
            return
        profile = tool_profile(tier)
        if not self.gateway.registry.set_profile(profile):
            return
        await self.recorder.add(
            "tools.scoped",
            node,
            {
                "profile": profile,
                "tier": tier or "unrouted",
                "tier_label": TIER_LABELS.get(tier, "") if tier else "",
                "visible": self.gateway.registry.visible_names(),
                "hidden": self.gateway.registry.deferred_names(),
            },
        )

    async def call_model(self, messages: list[dict], node: str, *, step: int = 0) -> tuple[Any, dict]:
        role = role_for_node(node)
        adapter = self.role_adapters.get(role, self.adapter)
        tier = ""
        if self.router is not None and self.router.active:
            adapter, tier = await self._routed_adapter(node, step, adapter)
        if not adapter.config.supports_tools:
            # 选中的供应商其实不支持工具调用，退回默认模型。档位也要一起退：
            # 它是「给这个模型看多少工具」的依据，模型换了依据就失效了。
            adapter, tier = self.adapter, ""
        # 每个角色可能挂着不同的模型，窗口大小不同；水位必须按"这次要用的窗口"算。
        window = int(getattr(adapter.config, "context_window", 0) or 0)
        if window and window != self.compactor.context_window:
            self.compactor.context_window = window
        covered_before = self.compactor.covered
        compacted = await self.compactor.build(messages, self.summarize_history)
        report = self.compactor.last_report
        self.context_tokens = report.tokens_after
        # 交接笔记：压缩后必带（摘要会丢操作细节），换窗口后的第一次调用也带一次。
        # 开关关掉就整块跳过：笔记本身要占上下文，长对话里有人宁可让摘要独扛；
        # 注意跳过时也不置 notes_injected，否则关掉再打开就再也注入不进去了。
        if feature_enabled("handoff_notes") and (
            report.folded or self.compactor.covered > covered_before or not self.notes_injected
        ):
            self.notes_injected = True
            block = self.notes_handoff()
            if block:
                compacted = _insert_handoff(compacted, block)
        if report.micro.applied:
            # 第一层：确定性裁剪。不花模型钱，但要能解释"为什么这一步省了 token"。
            await self.recorder.add("context.microcompacted", node, report.as_dict())
        if report.folded or self.compactor.covered > covered_before:
            await self.recorder.add(
                "context.compacted",
                node,
                {**report.as_dict(), "folded": report.folded or (self.compactor.covered - covered_before)},
            )
            # post_compact 钩子：两家都有同名事件，压缩后是最需要外部记账/上报的时刻
            hook_results = await self.hooks.run("post_compact", "", report.as_dict())
            if hook_results:
                await self.recorder.add(
                    "hook.completed",
                    node,
                    {"event": "post_compact", "results": [item.as_dict() for item in hook_results]},
                )
        await self.recorder.add("context.measured", node, report.as_dict())
        chat = [
            ChatMessage(
                role=item["role"],
                content=item.get("content", ""),
                tool_call_id=item.get("tool_call_id"),
                tool_calls=item.get("tool_calls") or [],
            )
            for item in compacted
        ]
        started = time.perf_counter()
        # 工具表跟着档位走。这里放在压缩之后、真正发请求之前：可见工具数会改变
        # 上下文体积，但改的是"下发的 schema"，不是已经算好的对话 token。
        await self._scope_tools(node, tier)
        try:
            response = await adapter.bind_tools(self.gateway.specs(), chat)
        except ModelError:
            await self._record_usage(node, "", Usage(), 0, "error", role=role)
            raise
        usage = response.usage
        # 用真实 prompt_tokens 校准估算器：水位要按模型的说法算，不能只信字符启发式
        self.compactor.observe(usage.prompt_tokens, report.tokens_after)
        selected_provider = str(getattr(adapter, "last_provider", adapter.config.name))
        if selected_provider != adapter.config.name:
            await self.recorder.add(
                "model.fallback",
                node,
                {"role": role, "from": adapter.config.name, "to": selected_provider},
            )
        await self._record_usage(node, response.model, usage, int((time.perf_counter() - started) * 1000), "ok", role=role)
        return response, accumulate_usage(usage)
    async def record_usage(
        self, node: str, model: str, usage: Usage, latency_ms: int, status: str, *, role: str = "coder"
    ) -> None:
        await self._record_usage(node, model, usage, latency_ms, status, role=role)

    async def _record_usage(
        self, node: str, model: str, usage: Usage, latency_ms: int, status: str, *, role: str = "coder"
    ) -> None:
        async with self.session_factory() as session:
            session.add(
                ModelUsage(
                    id=str(uuid.uuid4()),
                    provider_id=self.role_provider_ids.get(role, self.provider_id),
                    task_id=self.context.task_id,
                    trace_id=self.trace_id,
                    node=node,
                    model=model or self.provider_name,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    cached_tokens=usage.cached_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    reasoning_tokens=usage.reasoning_tokens,
                    latency_ms=latency_ms,
                    status=status,
                )
            )
            await session.commit()


def role_for_node(node: str) -> str:
    return {
        "create_plan": "planner",
        "execute_action": "coder",
        "review_result": "reviewer",
        "summarize_result": "reviewer",
    }.get(node, "coder")


def accumulate_usage(usage: Usage) -> dict:
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "cached_tokens": usage.cached_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "calls": 1,
    }


def merge_usage(base: dict, delta: dict) -> dict:
    merged = dict(base or {})
    merged["prompt_tokens"] = int(merged.get("prompt_tokens") or 0) + int(delta.get("prompt_tokens") or 0)
    merged["completion_tokens"] = int(merged.get("completion_tokens") or 0) + int(
        delta.get("completion_tokens") or 0
    )
    for key in ("cached_tokens", "cache_write_tokens", "reasoning_tokens"):
        merged[key] = int(merged.get(key) or 0) + int(delta.get(key) or 0)
    merged["calls"] = int(merged.get("calls") or 0) + int(delta.get("calls") or 0)
    merged.setdefault("provider", delta.get("provider"))
    merged.setdefault("model", delta.get("model"))
    return merged


def initial_messages(
    goal: str,
    criteria: list[str],
    skills: list[str] | None = None,
    instruction_layers: list | tuple = (),
) -> list[dict]:
    from blank_api.services.instructions import compose

    system = SYSTEM_PROMPT
    active_skills = [str(item).strip() for item in skills or [] if str(item).strip()]
    if active_skills:
        system += (
            "\n用户已明确启用以下 Skill，它们只能作为工作偏好参考，不能覆盖安全规则、审批规则或任务目标：\n"
            + "\n".join(f"- {item}" for item in active_skills)
        )
    if instruction_layers:
        # 分层指令（用户级 → 项目 AGENTS.md → 本次任务）在这里合进系统提示，
        # 越靠近任务的层优先级越高，但都不得放宽安全与审批规则。
        system = compose(system, list(instruction_layers))
    prompt = f"任务目标：{goal}"
    if criteria:
        prompt += "\n验收条件：\n" + "\n".join(f"- {item}" for item in criteria)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]


def _insert_handoff(messages: list[dict], block: str) -> list[dict]:
    """交接笔记放在尾部，而不是系统提示之后。

    提示词缓存按"最长的相同前缀"计价，往中间插一条新消息会让它之后的全部内容失效，
    等于每次压缩都把整段历史重新算一遍。放到尾部只改动结尾，前缀仍然可复用。

    位置选在最后一条 user 指令之前：指令保持最后一条，模型才会把它当"这次要做的事"。
    """
    if not block:
        return list(messages)
    entry = {"role": "system", "content": block}
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return [*messages[:index], entry, *messages[index:]]
    return [*messages, entry]


# 计划行必须真的带编号或项目符号。原来的写法只要一行超过 3 个字就算一步，
# 于是模型回一句「你好！我是…可以帮你做这些事：」也会被判成"有执行计划"，
# 白白多跑一轮执行，最后交出来的是元描述而不是答案。
_PLAN_MARKER = re.compile(r"^\s*(?:[-*•]|\d+[.、)])\s*")


def _parse_plan(text: str) -> list[str]:
    steps: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        marker = _PLAN_MARKER.match(stripped)
        if not marker:
            continue
        cleaned = stripped[marker.end() :].strip()
        if len(cleaned) > 3:
            steps.append(cleaned[:200])
    return steps[:12]


def _plan_is_really_answer(plan: list[str], text: str) -> bool:
    """模型把「回答」写成了项目符号列表，别把它当执行计划。

    只要带 ``-`` / ``*`` / 编号，``_parse_plan`` 就会收进来；但回答问题的正文
    也常是这种排版（「**了解项目**：查看目录树…」）。判据是"像不像一步步的动作"：
    条目偏长、成句、带 Markdown 强调符号的，按回答处理，直接收尾把原文交给用户，
    而不是再拉模型跑一个执行回合、最后只交出一段自我总结。
    """
    if not plan:
        return True
    if any("**" in item or "`" in item for item in plan):
        return True
    long_items = sum(1 for item in plan if len(item) >= 50)
    if long_items * 2 >= len(plan):
        return True
    # 每条都在讲"是什么/有什么"，而不是"做什么"
    declarative = sum(
        1 for item in plan if re.search(r"(：|:)\s*\S", item) and not re.match(r"^(运行|执行|修改|新建|检查|读取|打开|安装|生成|删除)", item)
    )
    return declarative * 2 >= len(plan) and len(plan) >= 2


def _extract_json(text: str) -> dict:
    """从模型输出里捞出第一个能解析的 JSON 对象。

    不能直接 find("{")..rfind("}")：模型常把 JSON 混在解释文字或 ```json 围栏里，
    中间夹着别的花括号时整段 json.loads 会失败，于是被判成"模型未返回 JSON"。
    """
    body = (text or "").strip()
    if not body:
        raise ValueError("模型未返回 JSON")
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    depth, start = 0, -1
    for index, char in enumerate(body):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    parsed = json.loads(body[start : index + 1])
                except json.JSONDecodeError:
                    start = -1
                    continue
                if isinstance(parsed, dict):
                    return parsed
                start = -1
    raise ValueError("模型未返回 JSON")


def tool_call_name(call: dict) -> str:
    return str((call.get("function") or {}).get("name") or call.get("name") or "")


def tool_call_args(call: dict) -> dict:
    raw = (call.get("function") or {}).get("arguments")
    if raw in (None, ""):
        raw = call.get("args")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _append_tool_response(messages: list[dict], call: dict, payload: dict) -> list[dict]:
    return messages + [
        {
            "role": "tool",
            "content": json.dumps(payload, ensure_ascii=False, default=str)[:8000],
            "tool_call_id": call.get("id") or tool_call_name(call),
        }
    ]


def _last_user_text(state: AgentState) -> str:
    """取最后一条用户消息。多轮追问时按"最新这句"判意图，而不是原始任务目标。"""
    for item in reversed(state.get("messages") or []):
        if item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text = "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") in (None, "text")
            ).strip()
            if text:
                return text
    return ""


async def create_plan_node(state: AgentState, session: AgentSession) -> AgentState:
    """第一步只做两件事：判意图、拆步骤。

    判成 ``answer`` 就当场答完走人（一次模型调用，不进工具循环、不跑审查），
    判成 ``act`` 才把步骤交给执行节点。之前这里让模型"边勘察边给计划"，
    结果是简单问题也要跑一整轮工具，回复里塞满过程描述。
    """
    node = "create_plan"
    await session.recorder.add("node.started", node, {"task_id": state.get("task_id", "")})
    goal = _last_user_text(state) or session.goal
    quick = guess_intent(goal)
    prompt = INTENT_INSTRUCTION
    if quick == INTENT_ANSWER:
        prompt += "\n系统预判：这句话不需要动工作区，intent 必须是 answer。"
    elif quick == INTENT_ACT:
        prompt += "\n系统预判：这句话需要动工作区，intent 必须是 act。"
    result = await session.ask(state, prompt, node)
    parsed = parse_intent(result["text"])
    intent = parsed.get("intent") or ""
    steps = parsed.get("steps") or []
    answer = parsed.get("answer") or ""
    if result["tool_calls"]:
        # 模型跳过了格式直接开干：按 act 处理，工具调用原样带进执行节点
        intent = INTENT_ACT
        steps = steps or _parse_plan(result["text"])
    if not intent:
        # 模型没给可解析的 JSON：先看规则预判，再按"有没有调工具 / 有没有给出编号步骤"判。
        # 只给计划不调工具是合法写法，这种情况要继续执行，不能当成回答。
        parsed_steps = _parse_plan(result["text"])
        if quick:
            intent = quick
        elif result["tool_calls"]:
            intent = INTENT_ACT
        elif parsed_steps and not _plan_is_really_answer(parsed_steps, result["text"]):
            intent = INTENT_ACT
        else:
            intent = INTENT_ANSWER
        if intent == INTENT_ACT and not steps:
            steps = parsed_steps
    if intent == INTENT_ANSWER and not answer:
        answer = result["text"]
    await session.recorder.add(
        "intent.classified",
        node,
        {"intent": intent, "note": (parsed.get("note") or goal)[:120], "steps": steps[:8]},
    )
    usage = merge_usage(state.get("usage") or {}, result["usage"])

    if intent == INTENT_ANSWER:
        text = normalize_answer(answer) or normalize_answer(result["text"])
        return {
            "messages": result["messages"],
            "plan": [],
            "plan_steps": [],
            "summary": text,
            "usage": usage,
            "pending": [],
            "status": "running",
            "direct_answer": True,
        }

    # 动作类任务：动手之前拍快照。放在这里而不是任务启动时，是因为这是
    # "知道要动工作区"的最早时刻——问答类走到上面那个 return 就结束了，不会拍。
    await session.ensure_snapshot(node)

    if steps:
        await session.recorder.add("plan.created", node, {"plan": steps, "text": result["text"][:2000]})
    return {
        "messages": result["messages"],
        "plan": steps,
        "usage": usage,
        "pending": result["tool_calls"],
        "status": "running",
        "direct_answer": False,
    }


def _decision(session: AgentSession, tool: ToolDefinition, arguments: dict):
    """审批门与网关共用同一个判定入口，两边口径不可能漂移。"""
    return session.gateway.decide(tool, arguments, session.context)


def _call_key(call: dict) -> str:
    return str(call.get("id") or tool_call_name(call) or "call")


async def drain_steer(session: AgentSession, node: str, step: int = 0) -> list[dict]:
    """取出用户在运行中插入的指令，转成一条 user 消息。

    队列放在事件日志里而不是内存里：桌面壳重启、多次审批恢复之后，
    用户排的这句话都还在，不会因为进程换了就丢。
    """
    from sqlalchemy import select

    from blank_api.models.task import AgentEvent

    try:
        async with session.session_factory() as db:
            if session.steer_cursor <= 0:
                # 恢复运行时 AgentSession 是新建的，游标会回到 0；如果不先对齐到
                # 最后一条 steer.applied，审批恢复后所有历史插话会被重复注入。
                applied = (
                    await db.execute(
                        select(AgentEvent.seq)
                        .where(
                            AgentEvent.task_id == session.context.task_id,
                            AgentEvent.type == "steer.applied",
                        )
                        .order_by(AgentEvent.seq.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                session.steer_cursor = int(applied or 0)
            rows = (
                (
                    await db.execute(
                        select(AgentEvent)
                        .where(
                            AgentEvent.task_id == session.context.task_id,
                            AgentEvent.type == "steer.queued",
                            AgentEvent.seq > session.steer_cursor,
                        )
                        .order_by(AgentEvent.seq)
                    )
                )
                .scalars()
                .all()
            )
    except Exception:  # 插话是增强能力，读失败就当没有，不要让任务挂掉
        return []

    texts: list[str] = []
    for row in rows or []:
        payload = row.payload or {}
        message = str(payload.get("message") or "").strip()
        if not message:
            continue
        texts.append(message)
        session.steer_cursor = max(session.steer_cursor, int(row.seq))
        queued_at = payload.get("queued_at")
        delay_ms = 0
        if isinstance(queued_at, (int, float)) and queued_at > 0:
            delay_ms = max(0, int(time.time() * 1000 - queued_at))
        session.steer_metrics["count"] = int(session.steer_metrics.get("count") or 0) + 1
        session.steer_metrics.setdefault("steps", []).append(
            {"step": step, "chars": len(message), "delay_ms": delay_ms}
        )
        await session.recorder.add(
            "steer.applied",
            node,
            {"step": step, "chars": len(message), "delay_ms": delay_ms},
        )

    if not texts:
        return []
    joined = "\n".join(f"- {item}" for item in texts)
    return [
        {
            "role": "user",
            "content": (
                "用户在你运行期间补充了以下指示，请从下一步开始遵守，已经完成的操作不要重做：\n"
                f"{joined}"
            ),
        }
    ]


async def _question_gate(session: AgentSession, node: str, pending: list[dict]) -> dict[str, str]:
    """Agent 主动提问：一次中断带走本步的所有问题，恢复后按问题 id 落答案。

    与审批共用同一条中断通道，所以传输层与 UI 不需要第二套机制；区别只在
    ``kind`` 字段，以及恢复值是一份答案而不是批准决定。
    """
    asked: list[dict] = []
    call_key = ""
    for call in pending:
        if tool_call_name(call) != "request_user_input":
            continue
        questions = normalize_questions(tool_call_args(call))
        if not questions:
            continue
        asked.extend(questions)
        call_key = call_key or _call_key(call)
    if not asked:
        return {}

    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    # 必须每次都调用 interrupt()：LangGraph 按位置回放恢复值，少调一次会让后续中断错位。
    answer = interrupt(
        {
            "kind": "question",
            "tool": "request_user_input",
            "questions": asked,
            "expires_at": expires_at,
            "permission_mode": session.permission_mode,
        }
    )
    answers = parse_answers(answer, asked)
    if call_key not in session.recorded_questions:
        session.recorded_questions.add(call_key)
        await session.recorder.add(
            "question.asked", node, {"questions": asked, "expires_at": expires_at}
        )
        await session.recorder.add(
            "question.answered",
            node,
            {"answers": answers, "answered": len(answers), "questions": [q["id"] for q in asked]},
        )
    return answers


async def _approval_gate(
    session: AgentSession, node: str, pending: list[dict]
) -> tuple[set[str], dict[str, str]]:
    """高风险工具只有图中断一条审批路径。

    中断返回的不再只是"准/不准"，而是带作用域的批准：仅本次 / 本会话该命令前缀 / 本会话该工具。
    作用域写回 PermissionPolicy 后，后续步骤命中规则就不再重复打断——这就是 Codex 的
    ``approved_command_prefixes`` 与 Claude 的 ``user_approved_for_session``。

    放在执行循环之前，是为了恢复运行时整节重放不会重复执行已经跑过的工具。

    guardian 在此处插一道独立审查：deny 直接拒绝且不打断（模型可以换个做法），
    allow 只有在 auto 模式下才对可逆动作生效，不可逆动作永远交给人。
    """
    denied: set[str] = set()
    blocked: dict[str, str] = {}
    for call in pending:
        name = tool_call_name(call)
        tool = session.gateway.get(name)
        if tool is None or name in denied:
            continue
        arguments = tool_call_args(call)
        decision = _decision(session, tool, arguments)
        if not decision.asks:
            # allow 交给网关执行；deny 交给网关拒绝并留审计——两条都不该在这里中断。
            continue
        command = str(arguments.get("command") or "")
        reversible = not (command and is_irreversible(command))
        if session.guardian.enabled:
            verdict = await session.guardian.review(
                name, arguments, risk=decision.risk, reversible=reversible
            )
            await session.record_usage(
                "guardian_review",
                verdict.model or "",
                Usage(
                    prompt_tokens=verdict.prompt_tokens,
                    completion_tokens=verdict.completion_tokens,
                ),
                verdict.latency_ms,
                "error" if verdict.error else "ok",
                role="guardian",
            )
            await session.recorder.add(
                "guardian.reviewed", node, {"tool": name, "arguments": arguments, **verdict.as_dict()}
            )
            if verdict.denied:
                blocked[name] = "；".join(verdict.reasons) or "审查模型判定该操作不安全"
                continue
            if reversible and session.guardian.can_auto_approve(verdict):
                session.permissions.remember(name, arguments, SCOPE_ONCE)
                await session.recorder.add(
                    "guardian.auto_approved",
                    node,
                    {"tool": name, "arguments": arguments, "reasons": verdict.reasons},
                )
                continue
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        answer = interrupt(
            {
                "tool": name,
                "arguments": arguments,
                "risk_level": decision.risk,
                "approval_key": approval_key(name, arguments),
                "permission_mode": session.permission_mode,
                "permission_rule": decision.rule,
                "command_prefix": decision.prefix,
                "suggested_prefix": memoizable_prefix(name, decision.prefix),
                "approval_options": approval_options(name, decision.prefix),
                "guardian": verdict.as_dict() if session.guardian.enabled else None,
                "expires_at": expires_at,
            }
        )
        granted, scope = parse_decision(answer)
        if granted:
            session.permissions.remember(name, arguments, scope)
            await session.recorder.add(
                "approval.granted",
                node,
                {
                    "tool": name,
                    "arguments": arguments,
                    "approval_key": approval_key(name, arguments),
                    "scope": scope,
                    "prefix": decision.prefix,
                    "expires_at": expires_at,
                },
            )
        else:
            denied.add(name)
            session.permissions.forget(name, arguments, scope)
            await session.recorder.add(
                "approval.denied",
                node,
                {"tool": name, "arguments": arguments, "scope": scope, "prefix": decision.prefix},
            )
    return denied, blocked


@dataclass
class _PlannedCall:
    """一轮里一次工具调用的「决定」。

    执行被拆成三段：先全部决策（提问卡与审批可能中断、钩子可能拦），再执行
    （只有只读的可以并发），最后按原顺序收尾写事件。之所以不能在一个循环里
    做完，是因为并发的前提是「决策已经定死」——审批与钩子是串行语义，混在
    一起就没法保证「被拦下的工具一定没跑过」。
    """

    call: dict
    name: str
    arguments: dict
    # "question"（用户已答，走 settle）| "refuse"（钩子/审查/驳回）| "execute"
    mode: str
    reason: str = ""
    # 被 pre_tool_use 钩子拦下的调用不跑 post_tool_use：它压根没执行，
    # 报一次"执行完成"会让钩子作者以为自己的前置判断没生效。
    hook_blocked: bool = False
    answers: dict = field(default_factory=dict)


def _parallel_ok(enabled: bool, item: _PlannedCall) -> bool:
    return enabled and item.mode == "execute" and item.name in PARALLEL_SAFE_TOOLS


async def _dispatch(session: "AgentSession", node: str, item: _PlannedCall) -> dict:
    """把一条计划变成网关返回的 payload。三条分支都不执行写操作以外的东西。"""
    if item.mode == "question":
        questions = normalize_questions(item.arguments)
        first = questions[0]["id"] if questions else ""
        if first and item.answers.get(first):
            outcome = ToolOutcome(
                ok=True,
                output=render_answers(questions, item.answers),
                data={"questions": questions, "answers": item.answers, "answered": True},
            )
        else:
            outcome = ToolOutcome(
                ok=False,
                status="unanswered",
                message="用户未回答该问题，请自行做出合理假设并在总结里说明。",
                data={"questions": questions, "answers": item.answers, "answered": False},
            )
        return await session.gateway.settle(
            item.name, item.arguments, outcome, session.context, node, rule="question:answered"
        )
    if item.mode == "refuse":
        return await session.gateway.refuse(
            item.name, item.arguments, session.context, node, reason=item.reason
        )
    return await session.gateway.execute(item.name, item.arguments, session.context, node)


async def _execute_all(
    session: "AgentSession", node: str, planned: list[_PlannedCall]
) -> list[dict]:
    """按「连续只读段」并发执行。

    不是把整批一起并发：A 读文件 → B 改文件 → C 读文件 是有顺序依赖的，
    全并发会让 C 可能读到 B 改之前的版本。所以只把相邻的无依赖只读聚成一段
    并发，碰到写操作或需要串行的就断开。
    """
    payloads: list[dict] = []
    enabled = feature_enabled("parallel_read_tools")
    index = 0
    while index < len(planned):
        if not _parallel_ok(enabled, planned[index]):
            payloads.append(await _dispatch(session, node, planned[index]))
            index += 1
            continue
        end = index
        while end < len(planned) and _parallel_ok(enabled, planned[end]):
            end += 1
        batch = planned[index:end]
        if len(batch) == 1:
            payloads.extend([await _dispatch(session, node, batch[0])])
        else:
            payloads.extend(await asyncio.gather(*(_dispatch(session, node, item) for item in batch)))
        index = end
    return payloads


async def execute_action_node(state: AgentState, session: AgentSession) -> AgentState:
    node = "execute_action"
    if await session.is_cancelled():
        await session.recorder.add("task.cancelled", node, {"reason": "用户已取消任务"})
        return {"status": "cancelled", "pending": []}
    steps = int(state.get("steps") or 0) + 1
    await session.recorder.add("node.started", node, {"step": steps, "max_steps": session.max_steps})

    messages = list(state.get("messages") or [])
    usage = dict(state.get("usage") or {})
    results = list(state.get("tool_results") or [])
    pending = list(state.get("pending") or [])
    # 先问、再批：问题门在前，恢复时两道门各占一个中断位，恢复值按位置回放不会串味。
    answers = await _question_gate(session, node, pending)
    denied, guardian_blocked = await _approval_gate(session, node, pending)

    # 第一段：串行决策。事件按模型给出的顺序落，与执行顺序解耦。
    planned: list[_PlannedCall] = []
    for call in pending:
        name = tool_call_name(call)
        arguments = tool_call_args(call)
        await session.recorder.add("tool.requested", node, {"tool": name, "arguments": arguments})

        if name == "request_user_input" and answers:
            planned.append(
                _PlannedCall(call, name, arguments, "question", answers=answers)
            )
            continue

        # pre_tool_use 钩子：可以拦下一次工具调用（对标 Codex / Claude Code 的同名事件）
        hook_results = await session.hooks.run(
            "pre_tool_use", name, {"tool": name, "arguments": arguments}
        )
        if hook_results:
            await session.recorder.add(
                "hook.completed",
                node,
                {"event": "pre_tool_use", "results": [item.as_dict() for item in hook_results]},
            )
        blocked = blocking_denial(hook_results)

        if blocked is not None:
            planned.append(
                _PlannedCall(
                    call,
                    name,
                    arguments,
                    "refuse",
                    reason=f"钩子拒绝执行 {name}：{blocked.reason or '未给出原因'}",
                    hook_blocked=True,
                )
            )
        elif name in guardian_blocked:
            planned.append(
                _PlannedCall(
                    call,
                    name,
                    arguments,
                    "refuse",
                    reason=f"独立审查未通过，已拒绝执行 {name}：{guardian_blocked[name]}",
                )
            )
        elif name in denied:
            planned.append(
                _PlannedCall(
                    call,
                    name,
                    arguments,
                    "refuse",
                    reason=f"用户驳回了该操作，本次会话不再执行：{name}",
                )
            )
        else:
            planned.append(_PlannedCall(call, name, arguments, "execute"))

    # 第二段：执行。只读且连续的可以并发，其余串行。
    payloads = await _execute_all(session, node, planned)

    # 第三段：收尾。全部按模型给出的顺序写事件与消息，前端看到的次序与串行时一致。
    plan_update: list[dict] | None = None
    for item, payload in zip(planned, payloads):
        name = item.name
        if item.mode == "question":
            results.append(
                {
                    "tool": name,
                    "status": payload.get("status"),
                    "ok": bool(payload.get("ok")),
                    "data": payload.get("data") or {},
                    "output": str(payload.get("output") or payload.get("message") or "")[-2000:],
                }
            )
            messages = _append_tool_response(messages, item.call, payload)
            continue

        if not item.hook_blocked:
            post_results = await session.hooks.run(
                "post_tool_use",
                name,
                {
                    "tool": name,
                    "arguments": item.arguments,
                    "ok": bool(payload.get("ok")),
                    "status": payload.get("status"),
                },
            )
            if post_results:
                await session.recorder.add(
                    "hook.completed",
                    node,
                    {"event": "post_tool_use", "results": [item.as_dict() for item in post_results]},
                )

        # update_plan：计划是可更新对象，工具结果直接落回图状态
        # 注意别把外层计数器 steps（int）覆盖掉，否则后面算预算时 int - list 直接崩
        if name == "update_plan" and payload.get("ok"):
            plan_steps = (payload.get("data") or {}).get("steps")
            if isinstance(plan_steps, list) and plan_steps:
                plan_update = plan_steps
        await session.recorder.add(
            "tool.completed",
            node,
            {
                "tool": name,
                "status": payload.get("status"),
                "ok": payload.get("ok"),
                "risk_level": payload.get("risk_level"),
                "permission": payload.get("permission") or {},
                "duration_ms": payload.get("duration_ms"),
                "tool_call_id": payload.get("tool_call_id"),
                "summary": str(payload.get("output") or payload.get("message") or "")[:500],
            },
        )
        data = payload.get("data") or {}
        if payload.get("ok") and data.get("path") and data.get("change_type"):
            await session.recorder.add(
                "file.changed",
                node,
                {"path": data.get("path"), "change_type": data.get("change_type")},
            )
        results.append(
            {
                "tool": name,
                "status": payload.get("status"),
                "ok": bool(payload.get("ok")),
                "data": data,
                "output": str(payload.get("output") or payload.get("message") or "")[-2000:],
            }
        )
        messages = _append_tool_response(messages, item.call, payload)

    # 升级信号：真跑失败才提档。被驳回 / 被钩子拦下的不算——那是策略判断，
    # 不是模型不够强，为它去换更强的模型等于拿钱重试用户刚刚说「不」的事。
    failed_tools = [
        item.name
        for item, payload in zip(planned, payloads)
        if item.mode == "execute" and not payload.get("ok")
    ]
    if failed_tools and session.router is not None:
        bumped = session.router.note_failure(node)
        await session.recorder.add(
            "model.escalation",
            node,
            {
                "reason": "工具执行失败，下一步提档重试",
                "tools": failed_tools,
                "escalation": bumped,
            },
        )

    plan_patch: dict = {}
    if plan_update:
        await session.recorder.add(
            "plan.updated",
            node,
            {"steps": plan_update, "total": len(plan_update)},
        )
        plan_patch = {
            "plan": [str(step.get("step") or "") for step in plan_update],
            "plan_steps": plan_update,
        }

    if denied:
        reason = f"已驳回高风险操作：{', '.join(sorted(denied))}，该步骤未执行"
        await session.recorder.add("task.paused", node, {"reason": reason})
        return {
            "messages": messages,
            "steps": steps,
            "usage": usage,
            "tool_results": results,
            "pending": [],
            "status": "paused",
            **plan_patch,
        }

    # 预算处理：循环本身靠"模型不再返回工具调用"自终止，预算只是安全网。
    # 因此不做生硬截断，而是分三段：正常 / 收尾区 / 耗尽。
    remaining = session.max_steps - steps
    if remaining <= 0:
        await session.recorder.add(
            "budget.exceeded", node, {"steps": steps, "max_steps": session.max_steps}
        )
        prompt = (
            "步数预算已用完。不要再调用任何工具。"
            "请基于已经完成的动作和已有的工具结果，直接输出一段中文总结："
            "做了什么、结果如何、还有哪些没做完。"
        )
    elif remaining <= WRAP_UP_STEPS:
        await session.recorder.add(
            "budget.warning",
            node,
            {"steps": steps, "remaining": remaining, "max_steps": session.max_steps},
        )
        prompt = (
            f"步数预算只剩 {remaining} 步，请立即收尾：不要再启动新的探索或大范围改动，"
            "优先把当前这一步做完并落下结论。"
        )
    else:
        prompt = "请继续：如未完成请调用工具执行下一步；如已完成请输出中文总结。"
    result = await session.ask(
        state,
        prompt,
        node,
        messages=messages,
    )
    usage = merge_usage(usage, result["usage"])
    # 只有预算真正耗尽时才丢弃工具调用；收尾区仍允许模型完成最后一个动作。
    pending_calls = [] if remaining <= 0 else result["tool_calls"]
    return {
        "messages": result["messages"],
        "steps": steps,
        "usage": usage,
        "tool_results": results,
        "pending": pending_calls,
        "budget_exhausted": remaining <= 0,
        **plan_patch,
    }


# 会改动工作区的工具。校验只对「改过东西」的任务有意义，
# 问一句「你好」也去跑 pytest 的话，沙箱不可用时任务会被判成失败——
# 用户看到的就是「输入了没反应 / 执行失败」，而实际上模型已经正常回复了。
MUTATING_TOOLS = frozenset({"apply_patch", "write_file", "create_file", "delete_file", "move_file"})


def _changed_paths(results: list[dict]) -> list[str]:
    paths: list[str] = []
    for item in results:
        if not item.get("ok"):
            continue
        data = item.get("data") or {}
        path = data.get("path")
        if path and (data.get("change_type") or item.get("tool") in MUTATING_TOOLS):
            paths.append(str(path))
    return paths


async def run_validation_node(state: AgentState, session: AgentSession) -> AgentState:
    node = "run_validation"
    await session.recorder.add("node.started", node, {})
    results = state.get("tool_results") or []
    executed = [item for item in results if item.get("tool") == "run_tests"]
    changed = _changed_paths(results)
    if not executed and not changed:
        # 纯问答/纯排查：没有可校验的产物，直接放行，不要凭空造一条失败
        validation = {
            "command": "",
            "passed": True,
            "status": "skipped",
            "exit_code": None,
            "summary": {},
            "output_tail": "",
            "skipped_reason": "本次没有改动任何文件，无需执行测试",
        }
        await session.recorder.add("validation.completed", node, validation)
        return {"validation": validation}
    if executed:
        last = executed[-1]
        payload = {
            "ok": last.get("status") == "ok",
            "status": last.get("status"),
            "output": last.get("output", ""),
            "data": last.get("data") or {},
        }
    else:
        payload = await session.gateway.execute("run_tests", {}, session.context, node)
        await session.recorder.add(
            "tool.completed",
            node,
            {"tool": "run_tests", "status": payload.get("status"), "ok": payload.get("ok")},
        )
    data = payload.get("data") or {}
    status = str(payload.get("status") or "ok")
    if status == "denied":
        # 安全策略不允许执行命令，或用户驳回了这次审批。这是配置/授权问题，不是代码问题：
        # 记成 failed 会让任务被判「测试未通过」，用户会去翻代码，而真正该做的是改设置。
        validation = {
            "command": data.get("command") or "run_tests",
            "passed": True,
            "status": "skipped",
            "exit_code": None,
            "summary": {},
            "output_tail": str(payload.get("message") or payload.get("output") or "")[-2000:],
            "skipped_reason": "命令执行未获授权，本次未做自动化校验",
        }
        await session.recorder.add("validation.completed", node, validation)
        return {"validation": validation}
    validation = {
        "command": data.get("command") or "run_tests",
        "passed": status == "ok",
        "status": status,
        "exit_code": data.get("exit_code"),
        "summary": data.get("summary") or {},
        "output_tail": str(payload.get("output") or payload.get("message") or "")[-2000:],
    }
    await session.recorder.add("validation.completed", node, validation)
    update: AgentState = {"validation": validation}
    if not validation["passed"]:
        update["repair_rounds"] = int(state.get("repair_rounds") or 0) + 1
    return update


async def review_result_node(state: AgentState, session: AgentSession) -> AgentState:
    """Review the produced change and validation evidence before delivery."""
    node = "review_result"
    await session.recorder.add("node.started", node, {})
    prompt = (
        "你是交付审查 Agent。请审查当前任务的工具结果、测试结论和验收条件，"
        "只输出 JSON：{\"approved\": true, \"findings\": [\"...\"], \"summary\": \"...\"}。"
        "如果发现实现缺陷、测试不足或验收条件未覆盖，approved 必须为 false，并给出可执行修复意见。"
    )
    try:
        result = await session.ask(state, prompt, node)
        parsed = _extract_json(result["text"])
        review = {
            "approved": bool(parsed.get("approved")),
            "findings": [str(item) for item in (parsed.get("findings") or [])][:12],
            "summary": str(parsed.get("summary") or ""),
        }
        rounds = int(state.get("review_rounds") or 0) + 1
        # 修复预算必须与验证回路共用同一个计数器：两个独立计数器会把"最多 3 轮自动修复"
        # 实际放大到 6~8 轮，既费钱也违背验收口径。
        total_repairs = int(state.get("repair_rounds") or 0) + (0 if review["approved"] else 1)
        await session.recorder.add(
            "review.completed", node, {**review, "round": rounds, "repair_rounds": total_repairs}
        )
        return {
            "messages": result["messages"],
            "review": review,
            "review_rounds": rounds,
            "repair_rounds": total_repairs,
            "usage": merge_usage(state.get("usage") or {}, result["usage"]),
            "status": (
                "failed" if not review["approved"] and total_repairs > MAX_REPAIR_ROUNDS else "running"
            ),
        }
    except (ModelError, ValueError, json.JSONDecodeError) as exc:
        await session.recorder.add("review.unavailable", node, {"reason": str(exc)[:300]})
        return {"review": {"approved": False, "findings": ["审查模型未返回可解析结果"], "summary": ""}, "status": "running"}


def _last_assistant_text(messages: list[dict]) -> str:
    """取最后一条模型回复的正文。没有验收条件时这就是任务交付内容本身，
    否则界面上只会剩一句「任务执行完成」，看起来跟没回复一样。"""
    for item in reversed(messages or []):
        if item.get("role") != "assistant":
            continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text = "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") in (None, "text")
            ).strip()
            if text:
                return text
    return ""


def _first_answer_text(messages: list[dict], min_chars: int = 60) -> str:
    """取最早一段"真的有内容"的模型正文。

    没调过任何工具的任务，最后一条助手消息常是「任务已完成，本轮无文件改动」这类
    收尾套话；真正回答问题的内容在前面。宁可交付前面那段，也不要交自我总结。
    """
    for item in messages or []:
        if item.get("role") != "assistant":
            continue
        content = item.get("content")
        text = ""
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            text = "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") in (None, "text")
            ).strip()
        if len(text) >= min_chars:
            return text
    return ""


async def summarize_result_node(state: AgentState, session: AgentSession) -> AgentState:
    node = "summarize_result"
    await session.recorder.add("node.started", node, {})
    results: list[dict] = []
    raw = str(state.get("summary") or "") or _last_assistant_text(state.get("messages") or [])
    if not state.get("tool_results") and not state.get("direct_answer"):
        raw = _first_answer_text(state.get("messages") or []) or raw

    if state.get("direct_answer"):
        # 已经在 intent 阶段答完了。这里不再拉模型改写——它只会把答案换成一段
        # 「我做了哪些判断」的元描述，用户看到的就不再是答案本身。
        text = normalize_answer(raw) or "已收到，没有需要额外说明的内容。"
        await session.recorder.add("delivery.recorded", node, {"mode": "answer", "chars": len(text)})
        return {
            "summary": text,
            "acceptance_results": [
                {"criterion": item, "covered": False, "evidence": "纯问答任务，未涉及工作区操作"}
                for item in session.acceptance_criteria
            ],
        }

    # 执行型任务：交付文本固定三段（已完成 / 未完成 / 下一步）。
    # 有验收条件时把验收判断并进同一次调用，省一轮往返也省一次费用。
    hint = raw.strip()[:1200]
    if session.acceptance_criteria:
        prompt = (
            REPORT_INSTRUCTION
            + "\n同时逐项判断验收条件是否已验证。只输出 JSON："
            + '{"report": "按上面三段写的交付说明", "acceptance": [{"criterion": "原文", "covered": true, "evidence": "证据"}]}'
            + "\n验收条件：\n"
            + "\n".join(f"- {item}" for item in session.acceptance_criteria)
            + (f"\n本次执行摘要（仅供参考，不要照抄）：{hint}" if hint else "")
        )
        try:
            ask = await session.ask(state, prompt, node)
            parsed = _extract_json(ask["text"])
            report = as_report(str(parsed.get("report") or parsed.get("summary") or ""))
            returned = parsed.get("acceptance") or []
            by_text = {
                str(item.get("criterion") or "").strip(): item
                for item in returned
                if isinstance(item, dict)
            }
            for criterion in session.acceptance_criteria:
                match = by_text.get(criterion.strip()) or next(
                    (item for text, item in by_text.items() if criterion.strip() in text), None
                )
                results.append(
                    {
                        "criterion": criterion,
                        "covered": bool(match and match.get("covered")),
                        "evidence": str((match or {}).get("evidence") or ""),
                    }
                )
            await session.recorder.add("delivery.recorded", node, {"mode": "report", "chars": len(report)})
            return {
                "summary": report,
                "acceptance_results": results,
                "usage": merge_usage(state.get("usage") or {}, ask["usage"]),
            }
        except (ModelError, ValueError, json.JSONDecodeError):
            results = [
                {"criterion": item, "covered": False, "evidence": "模型未返回可解析的验收结论"}
                for item in session.acceptance_criteria
            ]
            report = as_report(raw)
            await session.recorder.add("delivery.recorded", node, {"mode": "fallback", "chars": len(report)})
            return {"summary": report, "acceptance_results": results}

    prompt = REPORT_INSTRUCTION + (f"\n本次执行摘要（仅供参考，不要照抄）：{hint}" if hint else "")
    try:
        ask = await session.ask(state, prompt, node)
        report = as_report(ask["text"])
        usage = merge_usage(state.get("usage") or {}, ask["usage"])
        mode = "report"
    except ModelError:
        report = as_report(raw)
        usage = state.get("usage") or {}
        mode = "fallback"
    await session.recorder.add("delivery.recorded", node, {"mode": mode, "chars": len(report)})
    return {"summary": report, "acceptance_results": results, "usage": usage}


def route_after_plan(state: AgentState, session: AgentSession) -> str:
    if state.get("direct_answer"):
        return "summarize_result"
    return "execute_action"


def route_after_act(state: AgentState, session: AgentSession) -> str:
    if state.get("status") in ("cancelled", "paused") or state.get("error"):
        return "finish"
    if state.get("pending"):
        return "execute_action"
    return "run_validation"


def route_after_validation(state: AgentState, session: AgentSession) -> str:
    validation = state.get("validation") or {}
    # 预算耗尽后不可能再修复，必须直接进审查，否则会在 validation ↔ execute 之间空转到递归上限。
    if state.get("budget_exhausted"):
        return "review_result"
    # 只读任务（问答、排查、看代码）没有交付物可审，直接总结；
    # 硬拉模型去"审查"只会拿到一段散文，被判成不可解析，然后白白修复三轮。
    if validation.get("status") == "skipped":
        return "summarize_result"
    if validation.get("status") in ("failed", "timeout") and int(
        state.get("repair_rounds") or 0
    ) <= MAX_REPAIR_ROUNDS:
        return "execute_action"
    return "review_result"


def route_after_review(state: AgentState, session: AgentSession) -> str:
    review = state.get("review") or {}
    if state.get("budget_exhausted"):
        return "summarize_result"
    # 与验证回路共用同一份修复预算，避免两处各算 3 轮。
    if not review.get("approved") and int(state.get("repair_rounds") or 0) <= MAX_REPAIR_ROUNDS:
        return "execute_action"
    return "summarize_result"


def build_agent_graph(session: AgentSession, checkpointer: BaseCheckpointSaver | None = None):
    builder = StateGraph(AgentState)
    builder.add_node("create_plan", partial(create_plan_node, session=session))
    builder.add_node("execute_action", partial(execute_action_node, session=session))
    builder.add_node("run_validation", partial(run_validation_node, session=session))
    builder.add_node("review_result", partial(review_result_node, session=session))
    builder.add_node("summarize_result", partial(summarize_result_node, session=session))
    builder.add_edge(START, "create_plan")
    builder.add_conditional_edges(
        "create_plan",
        lambda state: route_after_plan(state, session),
        {"execute_action": "execute_action", "summarize_result": "summarize_result"},
    )
    builder.add_conditional_edges(
        "execute_action",
        lambda state: route_after_act(state, session),
        {
            "execute_action": "execute_action",
            "run_validation": "run_validation",
            "finish": END,
        },
    )
    builder.add_conditional_edges(
        "run_validation",
        lambda state: route_after_validation(state, session),
        {
            "execute_action": "execute_action",
            "review_result": "review_result",
            "summarize_result": "summarize_result",
        },
    )
    builder.add_conditional_edges(
        "review_result",
        lambda state: route_after_review(state, session),
        {"execute_action": "execute_action", "summarize_result": "summarize_result"},
    )
    builder.add_edge("summarize_result", END)
    return builder.compile(checkpointer=checkpointer)

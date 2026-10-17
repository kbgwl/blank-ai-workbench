"""LangGraph plan / act / observe workflow for a Task."""

from __future__ import annotations

import json
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
from blank_api.services.agent.context import (
    ContextCompactor,
    build_summary_prompt,
    load_summary_json,
    render_summary,
)
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

MAX_REPAIR_ROUNDS = 3
SYSTEM_PROMPT = (
    "你是空白格 AI 操作台的开发 Agent，工作在受限工作区内。\n"
    "规则：\n"
    "1. 只能通过工具读写工作区，路径一律使用工作区相对路径，禁止绝对路径与 ..。\n"
    "2. 先给出简短执行计划，再调用工具逐步执行；每次工具返回后观察结果再决定下一步。\n"
    "3. 修改文件必须使用 apply_patch，验证必须使用 run_tests。\n"
    "4. 不要重复调用已经执行过的工具，全部完成后用一段中文总结。"
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
        response, usage = await self.call_model(messages, node)
        assistant: dict[str, Any] = {"role": "assistant", "content": response.text}
        if response.tool_calls:
            assistant["tool_calls"] = response.tool_calls
        return {
            "text": response.text,
            "tool_calls": response.tool_calls,
            "usage": usage,
            "messages": [*messages, assistant],
        }

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

    async def call_model(self, messages: list[dict], node: str) -> tuple[Any, dict]:
        role = role_for_node(node)
        adapter = self.role_adapters.get(role, self.adapter)
        if not adapter.config.supports_tools:
            adapter = self.adapter
        covered_before = self.compactor.covered
        compacted = await self.compactor.build(messages, self.summarize_history)
        report = self.compactor.last_report
        self.context_tokens = report.tokens_after
        # 交接笔记：压缩后必带（摘要会丢操作细节），换窗口后的第一次调用也带一次
        if report.folded or self.compactor.covered > covered_before or not self.notes_injected:
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
        try:
            response = await adapter.bind_tools(self.gateway.specs(), chat)
        except ModelError:
            await self._record_usage(node, "", Usage(), 0, "error", role=role)
            raise
        usage = response.usage
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
        "calls": 1,
    }


def merge_usage(base: dict, delta: dict) -> dict:
    merged = dict(base or {})
    merged["prompt_tokens"] = int(merged.get("prompt_tokens") or 0) + int(delta.get("prompt_tokens") or 0)
    merged["completion_tokens"] = int(merged.get("completion_tokens") or 0) + int(
        delta.get("completion_tokens") or 0
    )
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
    """交接笔记插在系统提示之后：靠前的系统块更容易被当成长期约束。"""
    if not block:
        return list(messages)
    entry = {"role": "system", "content": block}
    for index, item in enumerate(messages):
        if item.get("role") != "system":
            return [*messages[:index], entry, *messages[index:]]
    return [entry, *messages]


def _parse_plan(text: str) -> list[str]:
    steps: list[str] = []
    for line in (text or "").splitlines():
        cleaned = line.strip(" \t-*•0123456789.").strip()
        if len(cleaned) > 3:
            steps.append(cleaned[:200])
    return steps[:12]


def _extract_json(text: str) -> dict:
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start == -1 or end <= start:
        raise ValueError("模型未返回 JSON")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("模型返回的 JSON 不是对象")
    return parsed


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


async def create_plan_node(state: AgentState, session: AgentSession) -> AgentState:
    node = "create_plan"
    await session.recorder.add("node.started", node, {"task_id": state.get("task_id", "")})
    result = await session.ask(
        state,
        "请先了解项目与目标，输出编号执行计划（3-6 步）。需要查看文件时直接调用工具。",
        node,
    )
    plan = _parse_plan(result["text"])
    await session.recorder.add("plan.created", node, {"plan": plan, "text": result["text"][:2000]})
    return {
        "messages": result["messages"],
        "plan": plan,
        "usage": merge_usage(state.get("usage") or {}, result["usage"]),
        "pending": result["tool_calls"],
        "status": "running",
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

    plan_update: list[dict] | None = None
    for call in pending:
        name = tool_call_name(call)
        arguments = tool_call_args(call)
        await session.recorder.add("tool.requested", node, {"tool": name, "arguments": arguments})

        if name == "request_user_input" and answers:
            questions = normalize_questions(arguments)
            first = questions[0]["id"] if questions else ""
            if first and answers.get(first):
                outcome = ToolOutcome(
                    ok=True,
                    output=render_answers(questions, answers),
                    data={"questions": questions, "answers": answers, "answered": True},
                )
            else:
                outcome = ToolOutcome(
                    ok=False,
                    status="unanswered",
                    message="用户未回答该问题，请自行做出合理假设并在总结里说明。",
                    data={"questions": questions, "answers": answers, "answered": False},
                )
            payload = await session.gateway.settle(
                name, arguments, outcome, session.context, node, rule="question:answered"
            )
            results.append(
                {
                    "tool": name,
                    "status": payload.get("status"),
                    "ok": bool(payload.get("ok")),
                    "data": payload.get("data") or {},
                    "output": str(payload.get("output") or payload.get("message") or "")[-2000:],
                }
            )
            messages = _append_tool_response(messages, call, payload)
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
            payload = await session.gateway.refuse(
                name,
                arguments,
                session.context,
                node,
                reason=f"钩子拒绝执行 {name}：{blocked.reason or '未给出原因'}",
            )
        elif name in guardian_blocked:
            payload = await session.gateway.refuse(
                name,
                arguments,
                session.context,
                node,
                reason=f"独立审查未通过，已拒绝执行 {name}：{guardian_blocked[name]}",
            )
        elif name in denied:
            payload = await session.gateway.refuse(
                name,
                arguments,
                session.context,
                node,
                reason=f"用户驳回了该操作，本次会话不再执行：{name}",
            )
        else:
            payload = await session.gateway.execute(name, arguments, session.context, node)

        if blocked is None:
            post_results = await session.hooks.run(
                "post_tool_use",
                name,
                {
                    "tool": name,
                    "arguments": arguments,
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
        if name == "update_plan" and payload.get("ok"):
            steps = (payload.get("data") or {}).get("steps")
            if isinstance(steps, list) and steps:
                plan_update = steps
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
        messages = _append_tool_response(messages, call, payload)

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


async def run_validation_node(state: AgentState, session: AgentSession) -> AgentState:
    node = "run_validation"
    await session.recorder.add("node.started", node, {})
    executed = [item for item in state.get("tool_results") or [] if item.get("tool") == "run_tests"]
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


async def summarize_result_node(state: AgentState, session: AgentSession) -> AgentState:
    node = "summarize_result"
    await session.recorder.add("node.started", node, {})
    results: list[dict] = []
    summary = ""
    if session.acceptance_criteria:
        prompt = (
            "请逐项判断验收条件是否已由本次工具执行结果验证，只输出 JSON："
            '{"summary": "中文总结", "acceptance": [{"criterion": "原文", "covered": true, "evidence": "证据"}]}'
            "\n验收条件：\n" + "\n".join(f"- {item}" for item in session.acceptance_criteria)
        )
        try:
            ask = await session.ask(state, prompt, node)
            parsed = _extract_json(ask["text"])
            summary = str(parsed.get("summary") or "")
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
            return {
                "summary": summary or "任务执行完成",
                "acceptance_results": results,
                "usage": merge_usage(state.get("usage") or {}, ask["usage"]),
            }
        except (ModelError, ValueError, json.JSONDecodeError):
            results = [
                {"criterion": item, "covered": False, "evidence": "模型未返回可解析的验收结论"}
                for item in session.acceptance_criteria
            ]
    return {"summary": summary or "任务执行完成", "acceptance_results": results}


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
    builder.add_edge("create_plan", "execute_action")
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
        {"execute_action": "execute_action", "review_result": "review_result"},
    )
    builder.add_conditional_edges(
        "review_result",
        lambda state: route_after_review(state, session),
        {"execute_action": "execute_action", "summarize_result": "summarize_result"},
    )
    builder.add_edge("summarize_result", END)
    return builder.compile(checkpointer=checkpointer)

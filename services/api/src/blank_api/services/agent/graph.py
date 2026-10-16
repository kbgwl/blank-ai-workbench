"""LangGraph plan / act / observe workflow for a Task."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from blank_api.models.model_provider import ModelUsage
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.context import compact_messages
from blank_api.services.models.base import ChatMessage, ModelAdapter, ModelError, Usage
from blank_api.services.tools.base import ToolContext, ToolDefinition, approval_key
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
    summary: str
    status: str
    error: dict


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
    acceptance_criteria: list[str] = field(default_factory=list)
    max_steps: int = 8
    approved_tools: set[str] = field(default_factory=set)
    approved_actions: set[str] = field(default_factory=set)
    is_cancelled: Callable[[], Awaitable[bool]] = _never_cancelled

    def approval_policy(self, tool: ToolDefinition, arguments: dict) -> bool:
        return approval_key(tool.name, arguments) in self.approved_actions or tool.name in self.approved_tools

    async def ask(self, state: AgentState, prompt: str, node: str, *, messages: list[dict] | None = None) -> dict:
        messages = list(messages) if messages is not None else list(state.get("messages") or [])
        messages = messages + [{"role": "user", "content": prompt}]
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

    async def call_model(self, messages: list[dict], node: str) -> tuple[Any, dict]:
        chat = [
            ChatMessage(
                role=item["role"],
                content=item.get("content", ""),
                tool_call_id=item.get("tool_call_id"),
                tool_calls=item.get("tool_calls") or [],
            )
            for item in compact_messages(messages)
        ]
        started = time.perf_counter()
        try:
            response = await self.adapter.bind_tools(self.gateway.specs(), chat)
        except ModelError:
            await self._record_usage(node, "", Usage(), 0, "error")
            raise
        usage = response.usage
        await self._record_usage(node, response.model, usage, int((time.perf_counter() - started) * 1000), "ok")
        return response, accumulate_usage(usage)

    async def _record_usage(
        self, node: str, model: str, usage: Usage, latency_ms: int, status: str
    ) -> None:
        async with self.session_factory() as session:
            session.add(
                ModelUsage(
                    id=str(uuid.uuid4()),
                    provider_id=self.provider_id,
                    task_id=self.context.task_id,
                    node=node,
                    model=model or self.provider_name,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    latency_ms=latency_ms,
                    status=status,
                )
            )
            await session.commit()


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


def initial_messages(goal: str, criteria: list[str]) -> list[dict]:
    prompt = f"任务目标：{goal}"
    if criteria:
        prompt += "\n验收条件：\n" + "\n".join(f"- {item}" for item in criteria)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]


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


def _needs_approval(session: AgentSession, tool: ToolDefinition, arguments: dict) -> bool:
    """审批口径与网关一致：高风险且尚未授权的工具才需要图中断确认。"""
    if tool.resolve_risk(arguments) not in ("high", "critical"):
        return False
    try:
        return not bool(session.context.approval_policy(tool, arguments))
    except Exception:
        return True


async def _approval_gate(session: AgentSession, node: str, pending: list[dict]) -> set[str]:
    """高风险工具只有图中断一条审批路径：批准进 approved_tools，驳回返回待拒绝的工具名。

    放在执行循环之前，是为了恢复运行时整节重放不会重复执行已经跑过的工具。
    """
    denied: set[str] = set()
    for call in pending:
        name = tool_call_name(call)
        tool = session.gateway.get(name)
        if tool is None or name in denied:
            continue
        arguments = tool_call_args(call)
        if not _needs_approval(session, tool, arguments):
            continue
        risk = tool.resolve_risk(arguments)
        decision = bool(interrupt({"tool": name, "arguments": arguments, "risk_level": risk}))
        if decision:
            action = approval_key(name, arguments)
            session.approved_actions.add(action)
            await session.recorder.add("approval.granted", node, {"tool": name, "approval_key": action})
        else:
            denied.add(name)
            await session.recorder.add("approval.denied", node, {"tool": name})
    return denied


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
    denied = await _approval_gate(session, node, pending)

    for call in pending:
        name = tool_call_name(call)
        arguments = tool_call_args(call)
        await session.recorder.add("tool.requested", node, {"tool": name, "arguments": arguments})
        if name in denied:
            payload = await session.gateway.refuse(name, arguments, session.context, node)
        else:
            payload = await session.gateway.execute(name, arguments, session.context, node)
        await session.recorder.add(
            "tool.completed",
            node,
            {
                "tool": name,
                "status": payload.get("status"),
                "ok": payload.get("ok"),
                "risk_level": payload.get("risk_level"),
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
        }

    if steps >= session.max_steps and not results:
        await session.recorder.add("budget.exceeded", node, {"steps": steps})
    budget_exceeded = steps >= session.max_steps
    result = await session.ask(
        state,
        "请继续：如未完成请调用工具执行下一步；如已完成请输出中文总结。",
        node,
        messages=messages,
    )
    usage = merge_usage(usage, result["usage"])
    pending_calls = [] if budget_exceeded else result["tool_calls"]
    return {
        "messages": result["messages"],
        "steps": steps,
        "usage": usage,
        "tool_results": results,
        "pending": pending_calls,
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
    if validation.get("status") in ("failed", "timeout") and int(
        state.get("repair_rounds") or 0
    ) <= MAX_REPAIR_ROUNDS:
        return "execute_action"
    return "summarize_result"


def build_agent_graph(session: AgentSession, checkpointer: BaseCheckpointSaver | None = None):
    builder = StateGraph(AgentState)
    builder.add_node("create_plan", partial(create_plan_node, session=session))
    builder.add_node("execute_action", partial(execute_action_node, session=session))
    builder.add_node("run_validation", partial(run_validation_node, session=session))
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
        {"execute_action": "execute_action", "summarize_result": "summarize_result"},
    )
    builder.add_edge("summarize_result", END)
    return builder.compile(checkpointer=checkpointer)

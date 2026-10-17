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
from dataclasses import dataclass, field
from pathlib import Path

from blank_api.services.agent.context import ContextCompactor
from blank_api.services.models.base import ChatMessage, ModelAdapter, ModelError, Usage
from blank_api.services.permissions import MODE_ACCEPT_EDITS, MODE_DEFAULT, PermissionPolicy
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway
from blank_api.services.worktree import Worktree, create_worktree, remove_worktree, render_diff

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

    def normalized(self) -> "SubAgentRequest":
        return SubAgentRequest(
            goal=self.goal.strip()[:2_000],
            isolate=bool(self.isolate),
            max_steps=max(1, min(MAX_STEPS, int(self.max_steps or DEFAULT_STEPS))),
            timeout_seconds=max(MIN_TIMEOUT, min(MAX_TIMEOUT, float(self.timeout_seconds or 120.0))),
            label=self.label,
        )


@dataclass
class SubAgentResult:
    ok: bool
    summary: str = ""
    steps: int = 0
    depth: int = 1
    agent_path: str = ""
    worktree: dict = field(default_factory=dict)
    diff: str = ""
    error: str = ""
    usage: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        payload = {
            "ok": self.ok,
            "summary": self.summary,
            "steps": self.steps,
            "depth": self.depth,
            "agent_path": self.agent_path,
            "usage": self.usage,
        }
        if self.worktree:
            payload["worktree"] = self.worktree
        if self.diff:
            payload["diff"] = self.diff
        if self.error:
            payload["error"] = self.error
        return payload

    def render(self) -> str:
        parts = [self.summary.strip() or "子代理未给出结论"]
        if self.diff:
            parts.append("【隔离副本中的改动】\n" + self.diff)
        if self.error:
            parts.append(f"【异常】{self.error}")
        return "\n\n".join(parts)


class _Recorder:
    """包一层事件记录：子代理的每条事件都带 agent_path 与 depth。"""

    def __init__(self, recorder, *, agent_path: str, depth: int, parent_thread_id: str) -> None:
        self.recorder = recorder
        self.agent_path = agent_path
        self.depth = depth
        self.parent_thread_id = parent_thread_id

    async def add(self, event: str, node: str, payload: dict) -> None:
        await self.recorder.add(
            event,
            node,
            {
                **payload,
                "agent_path": self.agent_path,
                "depth": self.depth,
                "parent_thread_id": self.parent_thread_id,
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
) -> SubAgentResult:
    request = request.normalized()
    if not request.goal:
        return SubAgentResult(ok=False, error="子任务目标为空", agent_path=agent_path, depth=depth)

    tree: Worktree | None = None
    run_root = Path(root)
    isolated = request.isolate
    if isolated:
        try:
            tree = create_worktree(run_root, label=request.label or "sub")
        except ValueError as exc:
            return SubAgentResult(
                ok=False, error=str(exc), agent_path=agent_path, depth=depth
            )
        run_root = tree.path
        await _Recorder(recorder, agent_path=agent_path, depth=depth, parent_thread_id=parent_thread_id).add(
            "worktree.created", "subagent", tree.as_dict()
        )

    gateway = ToolGateway(tools)
    permissions = PermissionPolicy.build(
        MODE_ACCEPT_EDITS if isolated else MODE_DEFAULT,
        approved_tools=set(),
        approved_actions=set(),
    )
    context = ToolContext(
        root=run_root,
        task_id=task_id,
        session_factory=session_factory,
        approval_policy=permissions.decide,
        trace_id=parent_thread_id,
        permissions=permissions,
    )
    log = _Recorder(recorder, agent_path=agent_path, depth=depth, parent_thread_id=parent_thread_id)
    compactor = ContextCompactor()
    messages: list[dict] = [
        {"role": "system", "content": SUBAGENT_SYSTEM.format(steps=request.max_steps)},
        {
            "role": "user",
            "content": request.goal + ("\n（你在一个临时隔离副本里工作，改动不会影响真实工作区）" if isolated else ""),
        },
    ]
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
    steps = 0
    summary = ""
    error = ""

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
            response = await adapter.bind_tools(gateway.specs(), chat)
            usage["prompt_tokens"] += response.usage.prompt_tokens
            usage["completion_tokens"] += response.usage.completion_tokens
            usage["calls"] += 1
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
        response = await adapter.bind_tools([], chat)
        usage["prompt_tokens"] += response.usage.prompt_tokens
        usage["completion_tokens"] += response.usage.completion_tokens
        usage["calls"] += 1
        summary = response.text

    started = time.perf_counter()
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
        await log.add("worktree.removed", "subagent", {"path": str(tree.path), "mode": tree.mode})
        remove_worktree(tree)

    result = SubAgentResult(
        ok=not error,
        summary=summary.strip() or ("子代理未给出结论" if not error else ""),
        steps=steps,
        depth=depth,
        agent_path=agent_path,
        worktree=tree.as_dict() if tree else {},
        diff=diff,
        error=error,
        usage={**usage, "latency_ms": int((time.perf_counter() - started) * 1000)},
    )
    await log.add(
        "subagent.completed",
        "subagent",
        {
            "ok": result.ok,
            "steps": result.steps,
            "summary": result.summary[:500],
            "error": result.error,
            "worktree_mode": tree.mode if tree else "",
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

    async def __call__(self, request: SubAgentRequest) -> SubAgentResult:
        session = self.session
        if session is None:
            return SubAgentResult(ok=False, error="子代理运行器尚未绑定会话")
        depth = int(getattr(session, "depth", 0)) + 1
        if depth > MAX_DEPTH:
            return SubAgentResult(ok=False, error="子代理不允许再嵌套子代理", depth=depth)
        self.spawned += 1
        agent_path = f"{getattr(session, 'agent_path', 'root')}/sub-{self.spawned}"
        allowed = ISOLATED_TOOLS if request.isolate else READ_ONLY_TOOLS
        tools = [tool for tool in _all_tools() if tool.name in allowed]
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
        )


def _all_tools() -> list:
    from blank_api.services.tools import build_default_tools

    return build_default_tools()

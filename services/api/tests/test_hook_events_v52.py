"""AB 批：钩子事件面第二批（对齐上游 7 个事件）。

覆盖三件事，每件都钉在"能失败"的判据上：

* **事件面**：新事件真的注册了；阻断集合只多了该多的三个；stdin 里带
  `hook_event_name`（只认这个字段的钩子必须能用）。
* **接线**：PermissionRequest 的 allow/deny 真的作用在执行上，计划模式不认 allow；
  PermissionDenied 在策略拒绝与钩子拒绝两条路上都发；审批卡发 Notification；
  一批工具跑完发 PostToolBatch（载荷是这一批的 tool_calls）；Stop/SubagentStop 阻断
  后带着理由继续干且有上限；PreCompact 的指令真的进摘要提示词用完即清；
  SessionStart/SessionEnd 按 source/reason 分派。
* **端到端**：上面这些在真图、真子进程钩子里各走一遍（不是只测函数）。
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.services import permissions
from blank_api.services.agent.context import build_summary_prompt, render_summary
from blank_api.services.agent.graph import (
    AgentSession,
    _stop_hook_gate,
    route_after_summarize,
)
from blank_api.services.agent.runtime import AgentRuntime
from blank_api.services.hooks import (
    BLOCKING_EVENTS,
    HOOK_EVENTS,
    HookResult,
    HookRunner,
    HookSpec,
)
from blank_api.services.tools.base import ToolDefinition, ToolOutcome
from blank_api.services.tools.gateway import ToolGateway

from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)

NEW_EVENTS = (
    "pre_compact",
    "notification",
    "permission_request",
    "permission_denied",
    "post_tool_batch",
    "stop",
    "subagent_stop",
)


# --------------------------------------------------------------------------- #
# 1. 事件面与 stdin 契约
# --------------------------------------------------------------------------- #
def test_new_events_registered_and_blocking_set_is_narrow():
    missing = [event for event in NEW_EVENTS if event not in HOOK_EVENTS]
    assert not missing, missing
    # 只该多这四个：它们是"返回决定就立刻生效"的事件。通知/批处理/压缩前后
    # 都不许拦流程——把任意一个塞进集合，异步配置就会被迫同步等待。
    # user_prompt_expansion 与 user_prompt_submit 同族：上游 UserPromptExpansion
    # 的退出码 2 就是"这次展开作废"，不进集合的话否决会退化成一条非阻断错误。
    assert BLOCKING_EVENTS == {
        "pre_tool_use",
        "user_prompt_submit",
        "user_prompt_expansion",
        "task_created",
        "task_completed",
        "permission_request",
        "stop",
        "subagent_stop",
    }


async def test_stdin_carries_hook_event_name(tmp_path):
    """有些钩子只认 `hook_event_name`（上游 base hook input 的必备字段）。"""
    seen = tmp_path / "stdin.json"
    script = tmp_path / "echo.py"
    script.write_text(
        "import json, sys\n"
        "sys.stdin.reconfigure(encoding='utf-8')\n"
        "payload = json.load(sys.stdin)\n"
        f"open({str(seen)!r}, 'w', encoding='utf-8').write(json.dumps(payload, ensure_ascii=False))\n",
        encoding="utf-8",
    )
    runner = HookRunner({"permission_request": [HookSpec(command=f'"{sys.executable}" "{script}"')]})
    await runner.run(
        "permission_request",
        "run_shell",
        {"tool_name": "run_shell", "tool_input": {"command": "ls"}, "permission_suggestions": []},
    )
    payload = json.loads(seen.read_text(encoding="utf-8"))
    assert payload["hook_event_name"] == "permission_request"
    assert payload["event"] == "permission_request"
    assert payload["target"] == "run_shell"
    assert payload["tool_name"] == "run_shell"


# --------------------------------------------------------------------------- #
# 2. 网关：权限三件套 + 审批通知
# --------------------------------------------------------------------------- #
class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def add(self, type_: str, node: str, payload: dict | None = None) -> None:
        self.events.append((type_, node, dict(payload or {})))


class _Hooks:
    """最小钩子替身：按事件返回预设结果，并记下每次调用的载荷。"""

    # HookRunner.enabled：子代理派发点用它判断"有没有配钩子"。替身必须同形，
    # 否则 run_subagent 会在这里 AttributeError（AD 批接线后暴露）。
    enabled = True

    def __init__(self, script: dict[str, list[list[HookResult]]] | None = None) -> None:
        self.script = {key: list(value) for key, value in (script or {}).items()}
        self.calls: list[tuple[str, str, dict]] = []

    async def run(self, event: str, target: str = "", payload: dict | None = None, *, effort: str = ""):
        self.calls.append((event, target, dict(payload or {})))
        queue = self.script.get(event) or []
        return list(queue.pop(0)) if queue else []

    def events(self) -> list[str]:
        return [item[0] for item in self.calls]


class _Perms:
    def __init__(self, outcome: str = permissions.ASK, mode: str = "default") -> None:
        self.mode = mode
        self.outcome = outcome

    def decide(self, tool, arguments, risk=None):
        risk = risk or tool.resolve_risk(arguments)
        if self.outcome == permissions.DENY:
            return permissions.deny(risk, "test_rule", "策略拒绝")
        return permissions.ask(risk, "test_rule", memo=tool.name)


def _result(event: str, decision: str = "", reason: str = "", context: str = "") -> HookResult:
    return HookResult(
        event=event,
        command="fake",
        status="ok",
        decision=decision,
        reason=reason,
        additional_context=context,
    )


def _marker_tool(marker: Path) -> ToolDefinition:
    async def handler(_context, _arguments):
        marker.write_text("ran", encoding="utf-8")
        return ToolOutcome(ok=True, output="ran")

    return ToolDefinition(
        name="demo_write",
        description="demo",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        risk_level="low",
        risk_for=lambda _arguments: "high",
        handler=handler,
    )


def _ctx(env, root: Path, perms: _Perms, hooks: _Hooks):
    base = env.context("t52", root)
    return replace(base, permissions=perms, hooks=hooks, safety=None)


async def _call(env, tmp_path: Path, perms: _Perms, hooks: _Hooks, marker: Path) -> dict:
    gateway = ToolGateway([_marker_tool(marker)])
    return await gateway.execute(
        "demo_write", {"path": "out.txt"}, _ctx(env, tmp_path, perms, hooks), "execute_action"
    )


async def test_permission_request_allow_executes_the_call(env, tmp_path):  # noqa: F811
    marker = tmp_path / "ran.txt"
    hooks = _Hooks({"permission_request": [[_result("permission_request", "allow")]]})
    payload = await _call(env, tmp_path, _Perms(permissions.ASK), hooks, marker)
    assert payload["ok"] is True, payload
    assert marker.exists(), "钩子说 allow，工具却没真的跑"
    event, target, body = hooks.calls[0]
    assert (event, target) == ("permission_request", "demo_write")
    # 上游把建议规则一起交出去：钩子要能看见"人会被问什么"
    assert body["tool_name"] == "demo_write"
    assert body["tool_input"] == {"path": "out.txt"}
    assert body["permission_suggestions"][0]["scope"] == permissions.SCOPE_ONCE


async def test_permission_request_allow_is_ignored_in_plan_mode(env, tmp_path):  # noqa: F811
    marker = tmp_path / "ran.txt"
    hooks = _Hooks({"permission_request": [[_result("permission_request", "allow")]]})
    payload = await _call(env, tmp_path, _Perms(permissions.ASK, mode=permissions.MODE_PLAN), hooks, marker)
    assert payload["status"] == "awaiting_approval", payload
    assert not marker.exists(), "计划模式被钩子的 allow 绕过了"


async def test_permission_request_deny_blocks_and_fires_permission_denied(env, tmp_path):  # noqa: F811
    marker = tmp_path / "ran.txt"
    hooks = _Hooks({"permission_request": [[_result("permission_request", "deny", "别写这个文件")]]})
    payload = await _call(env, tmp_path, _Perms(permissions.ASK), hooks, marker)
    assert payload["status"] == "denied", payload
    assert "别写这个文件" in payload["message"]
    assert not marker.exists()
    denied = [item for item in hooks.calls if item[0] == "permission_denied"]
    assert denied and denied[0][2]["reason"] == "别写这个文件"
    assert denied[0][1] == "demo_write"


async def test_policy_deny_fires_permission_denied(env, tmp_path):  # noqa: F811
    marker = tmp_path / "ran.txt"
    hooks = _Hooks()
    payload = await _call(env, tmp_path, _Perms(permissions.DENY), hooks, marker)
    assert payload["status"] == "denied"
    assert "permission_denied" in hooks.events()
    body = next(item[2] for item in hooks.calls if item[0] == "permission_denied")
    assert body["reason"] == "策略拒绝"


async def test_approval_card_fires_a_permission_prompt_notification(env, tmp_path):  # noqa: F811
    marker = tmp_path / "ran.txt"
    hooks = _Hooks()
    payload = await _call(env, tmp_path, _Perms(permissions.ASK), hooks, marker)
    assert payload["status"] == "awaiting_approval"
    assert hooks.calls and hooks.calls[-1][0] == "notification"
    event, target, body = hooks.calls[-1]
    assert target == "permission_prompt"
    assert body["notification_type"] == "permission_prompt"
    assert "demo_write" in body["title"]


# --------------------------------------------------------------------------- #
# 3. 图内：审批门的权限钩子与通知（真实发卡路径）
# --------------------------------------------------------------------------- #
class _GateInterrupted(Exception):
    """把 interrupt 换成一个可捕获的哨兵：只关心"有没有发卡"，不真跑图。"""


class _GateGateway:
    def __init__(self, tool: ToolDefinition, decision) -> None:
        self._tool = tool
        self._decision = decision

    def get(self, name: str):
        return self._tool if name == self._tool.name else None

    def decide(self, tool, arguments, _context):
        return self._decision(tool, arguments)


def _gate_session(hooks: _Hooks, *, mode: str = permissions.MODE_DEFAULT):
    tool = _marker_tool(Path("unused-marker"))
    policy = permissions.PermissionPolicy(mode=mode)
    return SimpleNamespace(
        gateway=_GateGateway(tool, lambda tool, arguments: permissions.ask("high", "test_rule", memo=tool.name)),
        permissions=policy,
        permission_mode=policy.mode,
        guardian=SimpleNamespace(enabled=False),
        recorder=_Recorder(),
        hooks=hooks,
        context=SimpleNamespace(task_id="task-gate", root=Path(".")),
        hook_context=[],
        run_id="run-gate",
        turn_effort="",
    )


def _patch_interrupt(monkeypatch) -> dict:
    from blank_api.services.agent import graph as graph_module

    seen: dict = {}

    def _stop(payload):
        seen["payload"] = payload
        raise _GateInterrupted

    monkeypatch.setattr(graph_module, "interrupt", _stop)
    return seen


async def test_gate_hook_deny_skips_the_card_and_reports_the_reason(monkeypatch):
    from blank_api.services.agent.graph import _approval_gate

    _patch_interrupt(monkeypatch)
    hooks = _Hooks({"permission_request": [[_result("permission_request", "deny", "别动这个目录")]]})
    session = _gate_session(hooks)
    denied, _blocked, hook_denied = await _approval_gate(
        session, "call_model", [{"id": "call-1", "name": "demo_write", "args": {"path": "out.txt"}}]
    )
    assert denied == set()
    assert hook_denied == {"call-1": "权限钩子拒绝：别动这个目录"}
    # 拒绝不能变成一张卡：没有任何人要按这个钮。
    assert "permission_denied" in hooks.events()
    denied_event = next(item for item in hooks.calls if item[0] == "permission_denied")
    assert denied_event[2]["reason"] == "别动这个目录"
    assert any(event[0] == "permission.denied" for event in session.recorder.events)


async def test_gate_hook_allow_grants_once_without_a_card(monkeypatch):
    from blank_api.services.agent.graph import _approval_gate

    seen = _patch_interrupt(monkeypatch)
    hooks = _Hooks({"permission_request": [[_result("permission_request", "allow", "沙箱里可以写")]]})
    session = _gate_session(hooks)
    denied, _blocked, hook_denied = await _approval_gate(
        session, "call_model", [{"id": "call-1", "name": "demo_write", "args": {"path": "out.txt"}}]
    )
    assert (denied, hook_denied) == (set(), {})
    assert "payload" not in seen, "钩子答应之后仍然发了卡"
    granted = [event for event in session.recorder.events if event[0] == "approval.granted"]
    assert granted and granted[0][2]["rule"] == "hook:permission_request"
    # 批准真的写进会话账本：后续网关判定不该再问同一次调用。
    from blank_api.services.approvals import approval_key

    assert approval_key("demo_write", {"path": "out.txt"}) in session.permissions.approved_actions


async def test_gate_hook_allow_is_ignored_in_plan_mode(monkeypatch):
    """计划模式里的 ASK 是"这个动作不该发生"，钩子不能替用户按掉它。"""
    from blank_api.services.agent.graph import _approval_gate

    seen = _patch_interrupt(monkeypatch)
    hooks = _Hooks({"permission_request": [[_result("permission_request", "allow")]]})
    session = _gate_session(hooks, mode=permissions.MODE_PLAN)
    with pytest.raises(_GateInterrupted):
        await _approval_gate(
            session, "call_model", [{"id": "call-1", "name": "demo_write", "args": {"path": "out.txt"}}]
        )
    assert seen["payload"]["tool"] == "demo_write"
    assert session.permissions.approved_actions == set()


async def test_gate_card_notifies_before_it_asks(monkeypatch):
    from blank_api.services.agent.graph import _approval_gate

    seen = _patch_interrupt(monkeypatch)
    hooks = _Hooks()
    session = _gate_session(hooks)
    with pytest.raises(_GateInterrupted):
        await _approval_gate(
            session, "call_model", [{"id": "call-1", "name": "demo_write", "args": {"path": "out.txt"}}]
        )
    assert seen["payload"]["tool"] == "demo_write"
    event, target, body = hooks.calls[-1]
    assert (event, target) == ("notification", "permission_prompt")
    assert body["notification_type"] == "permission_prompt"
    assert body["title"] == "需要审批：demo_write"


# --------------------------------------------------------------------------- #
# 4. 图内：Stop 门、路由、压缩前指令、SessionEnd 原因
# --------------------------------------------------------------------------- #
def _stop_session(hooks: _Hooks, *, background: list[dict] | None = None):
    runner = SimpleNamespace(list_agents=lambda: list(background or []))
    return SimpleNamespace(
        hooks=hooks,
        recorder=_Recorder(),
        hook_context=[],
        context=SimpleNamespace(subagent_runner=runner),
        turn_effort="",
    )


async def test_stop_gate_blocks_then_releases_after_cap(monkeypatch):
    monkeypatch.setenv("BLANK_STOP_HOOK_BLOCK_CAP", "1")
    hooks = _Hooks(
        {
            "stop": [
                [_result("stop", "deny", "测试还没跑")],
                [_result("stop", "deny", "测试还没跑")],
                [_result("stop", "deny", "测试还没跑")],
            ]
        }
    )
    session = _stop_session(hooks)
    state = {
        "messages": [{"role": "assistant", "content": "我做完了"}],
        "stop_blocks": 0,
    }

    blocked = await _stop_hook_gate(state, session, "summarize_result")
    assert blocked == {"stop_blocked": True, "stop_blocks": 1}
    assert session.hook_context and "本轮暂不能结束：测试还没跑" in session.hook_context[0]["text"]

    body = hooks.calls[0][2]
    assert body["stop_hook_active"] is False
    assert body["last_assistant_message"] == "我做完了"
    assert body["background_tasks"] == []
    assert body["session_crons"] == []

    again = await _stop_hook_gate({**state, "stop_blocks": 1}, session, "summarize_result")
    assert again is None, "超过上限还继续拦 = 死循环"
    assert hooks.calls[1][2]["stop_hook_active"] is True
    assert any(item[0] == "hook.stop_capped" for item in session.recorder.events)


async def test_stop_gate_reports_running_background_tasks():
    hooks = _Hooks()
    session = _stop_session(
        hooks,
        background=[
            {"agent_id": "a1", "goal": "后台跑测试", "status": "running"},
            {"agent_id": "a2", "goal": "已结束", "status": "completed"},
        ],
    )
    assert await _stop_hook_gate({"messages": []}, session, "summarize_result") is None
    body = hooks.calls[0][2]
    # 只报还在跑的：把已结束的当成"还有活"会让钩子永远不敢放行
    assert body["background_tasks"] == [
        {"id": "a1", "description": "后台跑测试", "status": "running"}
    ]


def test_route_after_summarize_returns_to_execution_only_when_blocked():
    assert route_after_summarize({"stop_blocked": True}, None) == "execute_action"
    assert route_after_summarize({"stop_blocked": False}, None) == "finish"
    assert route_after_summarize({}, None) == "finish"


async def test_precompact_instructions_reach_the_summary_prompt_then_clear():
    hooks = _Hooks(
        {"pre_compact": [[_result("pre_compact", context="必须保住第 3 步的失败原因")]]}
    )
    session = SimpleNamespace(hooks=hooks, recorder=_Recorder(), compact_instructions="")
    await AgentSession._precompact_gate(session, "call_model")
    assert session.compact_instructions == "必须保住第 3 步的失败原因"
    assert hooks.calls[0][1] == "auto"
    assert hooks.calls[0][2]["trigger"] == "auto"

    prompt = build_summary_prompt([{"role": "user", "content": "hi"}], "", instructions=session.compact_instructions)
    assert "必须保住第 3 步的失败原因" in prompt[0]["content"]

    # 不带指令时不许凭空出现那段抬头
    plain = build_summary_prompt([{"role": "user", "content": "hi"}], "")
    assert "压缩前钩子补充的指令" not in plain[0]["content"]


async def test_summarize_history_uses_and_clears_the_instructions():
    captured: dict = {}

    async def _invoke(chat, node="", role=""):
        captured["chat"] = chat
        return SimpleNamespace(
            text=json.dumps({"goal": "目标", "done": ["做了一步"]}, ensure_ascii=False),
            model="mock",
            usage=SimpleNamespace(),
            response_id="r1",
        )

    async def _record(*args, **kwargs):
        return None

    session = SimpleNamespace(
        compact_instructions="锁住第 3 步",
        compaction_response_id="",
        _invoke_with_retry=_invoke,
        _record_usage=_record,
    )
    text = await AgentSession.summarize_history(session, [{"role": "user", "content": "hi"}], "")
    assert "锁住第 3 步" in captured["chat"][0].content
    assert session.compact_instructions == "", "指令只属于这一次压缩"
    assert "执行历史摘要" in text


def test_session_end_reason_mapping():
    assert AgentRuntime._session_end_reason({}, [object()]) == "waiting_approval"
    assert AgentRuntime._session_end_reason({"status": "paused"}, []) == "interrupted"
    assert AgentRuntime._session_end_reason({"status": "cancelled"}, []) == "interrupted"
    assert AgentRuntime._session_end_reason({"status": "completed"}, []) == "completed"


# --------------------------------------------------------------------------- #
# 4. 端到端：真图 + 真子进程钩子
# --------------------------------------------------------------------------- #
def _log_hook(tmp_path: Path, name: str, body: str = "") -> tuple[str, Path]:
    """把每次调用的 stdin 追加进 JSONL；body 可在写完之后追加输出。"""
    log = tmp_path / f"{name}.jsonl"
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import json, sys\n"
        "sys.stdin.reconfigure(encoding='utf-8')\n"
        "payload = json.load(sys.stdin)\n"
        f"open({str(log)!r}, 'a', encoding='utf-8').write(json.dumps(payload, ensure_ascii=False) + '\\n')\n"
        + body,
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"', log


def _hooks_env(monkeypatch, **events: str) -> None:
    monkeypatch.setenv(
        "BLANK_HOOKS",
        json.dumps({event: [{"command": command, "timeout": 20}] for event, command in events.items()}),
    )


def _lines(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


async def test_post_tool_batch_receives_the_whole_batch(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    command, log = _log_hook(tmp_path, "batch")
    _hooks_env(monkeypatch, post_tool_batch=command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset(
        [
            {"content": "计划：\n1. 读取 main.py"},
            {"content": "读取", "tool_calls": [call("read_file", {"path": "main.py"})]},
            {"content": json.dumps({"summary": "done", "acceptance": []}, ensure_ascii=False)},
        ]
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text

    rows = [row for row in _lines(log) if row["event"] == "post_tool_batch"]
    assert rows, "post_tool_batch 没有在工具批次后运行"
    row = rows[0]
    assert row["hook_event_name"] == "post_tool_batch"
    assert row["target"] == ""
    calls = row["tool_calls"]
    assert [item["tool_name"] for item in calls] == ["read_file"]
    assert calls[0]["ok"] is True
    assert calls[0]["tool_input"] == {"path": "main.py"}
    assert calls[0]["mode"] == "execute"


async def test_stop_hook_blocks_once_then_the_turn_finishes(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    counter = tmp_path / "stop-count.txt"
    body = (
        "count = 0\n"
        f"p = {str(counter)!r}\n"
        "import os\n"
        "if os.path.exists(p):\n"
        "    count = int(open(p, encoding='utf-8').read())\n"
        "open(p, 'w', encoding='utf-8').write(str(count + 1))\n"
        "if count == 0:\n"
        "    sys.stdout.write(json.dumps({'decision': 'deny', 'reason': '验收条件还没覆盖'}))\n"
    )
    command, log = _log_hook(tmp_path, "stop", body)
    _hooks_env(monkeypatch, stop=command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset(
        [
            {"content": "计划：\n1. 读取 main.py"},
            {"content": "读取", "tool_calls": [call("read_file", {"path": "main.py"})]},
            {"content": json.dumps({"summary": "第一版", "acceptance": []}, ensure_ascii=False)},
            {"content": "补一句：验收条件已覆盖"},
            {"content": json.dumps({"summary": "第二版", "acceptance": []}, ensure_ascii=False)},
        ]
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "succeeded", resp.text

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    blocked = [event for event in events if event["type"] == "hook.turn_blocked"]
    assert blocked and blocked[0]["payload"]["blocks"] == 1
    assert blocked[0]["payload"]["cap"] == 8
    stops = [
        event
        for event in events
        if event["type"] == "hook.completed" and event["payload"].get("event") == "stop"
    ]
    assert len(stops) == 2, stops
    assert [row["stop_hook_active"] for row in _lines(log)] == [False, True]
    # 理由必须真的回到模型面前，否则"继续干"不知道往哪干
    texts = [
        str(message.get("content") or "")
        for request in env.mock.requests
        for message in request.get("messages") or []
    ]
    assert any("本轮暂不能结束：验收条件还没覆盖" in text for text in texts)


async def test_permission_request_allow_runs_the_command_end_to_end(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    body = (
        "if payload.get('tool_name') == 'run_shell':\n"
        "    sys.stdout.write(json.dumps({'decision': 'allow', 'reason': '沙箱临时区可删'}))\n"
    )
    command, log = _log_hook(tmp_path, "perm", body)
    _hooks_env(monkeypatch, permission_request=command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["清理临时文件"])
    (workspace / "notes.txt").write_text("keep me\n", encoding="utf-8")
    env.mock.reset(
        [
            {"content": "计划：\n1. 删除 notes.txt"},
            {"content": "执行", "tool_calls": [call("run_shell", {"command": "rm -rf notes.txt"})]},
            {"content": json.dumps({"summary": "已删除", "acceptance": []}, ensure_ascii=False)},
        ]
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] != "waiting_approval", resp.text
    assert not (workspace / "notes.txt").exists(), "钩子 allow 之后命令没有真的执行"

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    completed = [
        event
        for event in events
        if event["type"] == "hook.completed" and event["payload"].get("event") == "permission_request"
    ]
    assert completed, events
    rows = _lines(log)
    assert rows and rows[0]["target"] == "run_shell"
    assert rows[0]["tool_input"] == {"command": "rm -rf notes.txt"}


async def test_permission_request_deny_blocks_the_command_end_to_end(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    body = (
        "if payload.get('tool_name') == 'run_shell':\n"
        "    sys.stdout.write(json.dumps({'decision': 'deny', 'reason': '这个文件不许删'}))\n"
    )
    command, log = _log_hook(tmp_path, "perm-deny", body)
    _hooks_env(monkeypatch, permission_request=command, permission_denied=command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["清理临时文件"])
    (workspace / "notes.txt").write_text("keep me\n", encoding="utf-8")
    env.mock.reset(
        [
            {"content": "计划：\n1. 删除 notes.txt"},
            {"content": "执行", "tool_calls": [call("run_shell", {"command": "rm -rf notes.txt"})]},
            {"content": json.dumps({"summary": "已被钩子拒绝", "acceptance": []}, ensure_ascii=False)},
        ]
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    # 钩子拒绝不该变成一张卡：没有任何人要按这个钮。
    assert resp.json()["task"]["status"] != "waiting_approval", resp.text
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "keep me\n"

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    denied = [event for event in events if event["type"] == "permission.denied"]
    assert denied and denied[0]["payload"]["rule"] == "hook:permission_request"
    rows = _lines(log)
    followup = [row for row in rows if row["event"] == "permission_denied"]
    assert followup and followup[0]["reason"] == "这个文件不许删"
    # 拒绝的文案必须是图内这条路径给出的（"钩子拒绝执行"），不能是网关事后拦下的
    # "权限钩子拒绝执行"——后者说明审批门根本没把钩子的拒绝当回事，只是运气好没跑成。
    texts = [
        str(message.get("content") or "")
        for request in env.mock.requests
        for message in request.get("messages") or []
    ]
    assert any("钩子拒绝执行 run_shell：权限钩子拒绝：这个文件不许删" in text for text in texts), texts


async def test_approval_card_sends_a_permission_prompt_notification(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    command, log = _log_hook(tmp_path, "notify")
    _hooks_env(monkeypatch, notification=command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["清理临时文件"])
    (workspace / "notes.txt").write_text("keep me\n", encoding="utf-8")
    env.mock.reset(
        [
            {"content": "计划：\n1. 删除 notes.txt"},
            {"content": "执行", "tool_calls": [call("run_shell", {"command": "rm -rf notes.txt"})]},
        ]
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "waiting_approval"

    rows = [row for row in _lines(log) if row["notification_type"] == "permission_prompt"]
    assert rows, _lines(log)
    assert rows[0]["target"] == "permission_prompt"
    assert rows[0]["title"].startswith("需要审批：")


async def test_session_start_and_end_carry_source_and_reason(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    command, log = _log_hook(tmp_path, "session")
    _hooks_env(monkeypatch, session_start=command, session_end=command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset(
        [
            {"content": "计划：\n1. 读取 main.py"},
            {"content": "读取", "tool_calls": [call("read_file", {"path": "main.py"})]},
            {"content": json.dumps({"summary": "done", "acceptance": []}, ensure_ascii=False)},
        ]
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text

    rows = _lines(log)
    started = [row for row in rows if row["event"] == "session_start"]
    ended = [row for row in rows if row["event"] == "session_end"]
    assert started and started[0]["source"] == "startup"
    assert started[0]["target"] == "startup"
    assert started[0]["model"], "session_start 必须带上这次用的模型"
    assert ended and ended[0]["reason"] == "completed"
    assert ended[0]["target"] == "completed"


async def test_subagent_stop_can_keep_a_child_working():
    """SubagentStop 与主代理 Stop 同一语义：阻断一次后再交付。"""
    from tests.test_subagent_usage_v18 import (  # noqa: F401
        PROVIDER_ID,
        TASK_ID,
        ScriptedAdapter,
        _factory,
        _response,
        _seed,
    )
    from blank_api.services.agent.events import EventRecorder
    from blank_api.services.models.base import Usage
    from blank_api.services.subagent import SubAgentRequest, run_subagent

    await _seed()
    hooks = _Hooks(
        {
            "subagent_stop": [
                [_result("subagent_stop", "deny", "还没核对 README")],
                [],
            ]
        }
    )
    adapter = ScriptedAdapter(
        "mock",
        [
            _response("第一版结论", Usage(prompt_tokens=10, completion_tokens=2)),
            _response("第二版结论", Usage(prompt_tokens=10, completion_tokens=2)),
        ],
    )
    factory = _factory()
    result = await run_subagent(
        adapter=adapter,
        tools=[],
        root=Path("."),
        task_id=TASK_ID,
        session_factory=factory,
        recorder=EventRecorder(factory, TASK_ID),
        request=SubAgentRequest(goal="读 README 并复述", max_steps=4),
        agent_path="root/sub-stop",
        depth=1,
        parent_thread_id="root-thread",
        provider_id=PROVIDER_ID,
        provider_name="deepseek",
        hooks=hooks,
    )
    assert result.ok and result.summary == "第二版结论", result
    stops = [item for item in hooks.calls if item[0] == "subagent_stop"]
    assert len(stops) == 2
    first, second = stops
    assert first[1] == ""
    assert first[2]["last_assistant_message"] == "第一版结论"
    assert first[2]["agent_id"] == result.agent_id
    assert second[2]["stop_hook_active"] is True
    notifications = [item for item in hooks.calls if item[0] == "notification"]
    assert notifications and notifications[0][2]["notification_type"] == "agent_completed"

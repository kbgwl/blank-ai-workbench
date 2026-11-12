"""AD 批：SubagentStart 钩子接线 + SessionStart 的附加上下文真的进模型。

上游（Claude Code）的语义由本批实测锚定：

* ``SubagentStart`` 不是阻断事件，matcher 比对的是 ``agent_type``；
* 退出码 2 被渲染成 ``hook_non_blocking_error``（给人看的提示），子代理照常启动；
* ``additionalContext`` 以 attachment 形式注入子代理启动上下文，续聊不重复注入。

本地还要多守一条：``_execute`` 对非阻断事件的 exit 2 会把 stderr 并进
``additional_context``，两种用途共用一个字段，注入前必须按退出码分开——
否则一条"钩子报错"会变成子代理的作业指令。

每条判据都在"拿掉对应实现就会红"的位置上：matcher 按 agent_type、上下文位置、
exit 2 不注入、续聊不重跑、无钩子零改动、SessionStart 正文进模型。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from blank_api.services.hooks import (
    BLOCKING_EVENTS,
    EVENT_ALIASES,
    HOOK_EVENTS,
    HookResult,
    HookRunner,
    non_blocking_errors,
    usable_context,
)
from blank_api.services.subagent import (
    SUBAGENT_START_MARKER,
    SubAgentRequest,
    SubAgentRunner,
    run_subagent,
)

from tests.test_agent_v04 import (  # noqa: F401  (复用 HTTP 夹具)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)
from tests.test_claude_send_message_v30 import (
    ScriptedAdapter,
    _context,
    _session,
    _tools,
)
from tests.test_hook_events_v52 import _hooks_env, _lines, _log_hook


class _Events:
    """最小事件记录器：和 EventRecorder 一样被 `_Recorder` 包一层。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []
        self.run_id = ""
        self.origin_run_id = ""

    async def add(self, event: str, node: str, payload: dict) -> None:
        self.events.append((event, node, dict(payload)))

    def named(self, event: str) -> list[dict]:
        return [payload for name, _node, payload in self.events if name == event]


class _Hooks:
    """SubAgentRunner 从 session.hooks 取钩子；这里只记调用次数与载荷。"""

    def __init__(self, notes: list[str] | None = None) -> None:
        self.enabled = True
        self.notes = list(notes or [])
        self.calls: list[tuple[str, str, dict]] = []

    async def run(self, event: str, target: str = "", payload: dict | None = None):
        self.calls.append((event, target, dict(payload or {})))
        if event != "subagent_start" or not self.notes:
            return []
        return [
            HookResult(
                event=event,
                command="fake",
                status="ok",
                additional_context=note,
                exit_code=0,
            )
            for note in self.notes
        ]


def _hook_runner(specs: dict[str, list[dict]]) -> HookRunner:
    runner = HookRunner.from_env(json.dumps(specs), settings=None)
    assert not runner.warnings, runner.warnings
    return runner


async def _child(
    adapter,
    *,
    hooks=None,
    agent_name: str = "",
    recorder: _Events | None = None,
    history: list[dict] | None = None,
    start_hooks: bool = True,
):
    return await run_subagent(
        adapter=adapter,
        tools=[],
        root=Path("."),
        task_id="task-v54",
        session_factory=None,
        recorder=recorder or _Events(),
        request=SubAgentRequest(goal="读取 README 并复述", max_steps=4, agent_name=agent_name),
        agent_path="root/ad",
        depth=1,
        parent_thread_id="root-thread",
        agent_id="agent-ad",
        hooks=hooks,
        history=history,
        start_hooks=start_hooks,
    )


def _contents(snapshot: list[dict]) -> list[str]:
    return [str(item.get("content") or "") for item in snapshot]


# --------------------------------------------------------------------------- #
# 1. 事件面
# --------------------------------------------------------------------------- #
def test_subagent_start_registered_aliased_and_not_blocking():
    assert "subagent_start" in HOOK_EVENTS
    assert EVENT_ALIASES["SubagentStart"] == "subagent_start"
    # 上游把它的 exit 2 当"钩子报错"，子代理照常启动：进阻断集合就会变成"派不出去"。
    assert "subagent_start" not in BLOCKING_EVENTS
    runner = _hook_runner({"SubagentStart": [{"command": "x", "matcher": "reviewer"}]})
    assert runner.hooks["subagent_start"][0].match == "reviewer"


def test_usable_context_drops_exit_two_output():
    """exit 2 的正文是"钩子报错"，不是作业指令；两个接线点共用这条口径。"""
    ok = HookResult(event="subagent_start", command="a", status="ok", additional_context="正常上下文")
    bad = HookResult(
        event="subagent_start",
        command="b",
        status="failed",
        additional_context="钩子炸了",
        exit_code=2,
    )
    assert usable_context([ok, bad]) == ["正常上下文"]
    assert [item.command for item in non_blocking_errors([ok, bad])] == ["b"]


# --------------------------------------------------------------------------- #
# 2. matcher 按 agent_type 分发（真子进程钩子）
# --------------------------------------------------------------------------- #
async def test_matcher_routes_on_agent_type(tmp_path: Path):
    command, log = _log_hook(tmp_path, "ad-match")
    hooks = _hook_runner({"subagent_start": [{"command": command, "match": "reviewer", "timeout": 20}]})

    await _child(ScriptedAdapter(["reviewer 结论"]), hooks=hooks, agent_name="reviewer")
    await _child(ScriptedAdapter(["通用结论"]), hooks=hooks, agent_name="general-purpose")

    rows = _lines(log)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["hook_event_name"] == "subagent_start"
    assert row["target"] == "reviewer"
    assert row["agent_type"] == "reviewer"
    assert row["depth"] == 1
    assert row["goal"] == "读取 README 并复述"
    assert row["permission_mode"] == "default"
    assert row["fork"] is False
    assert row["parent_thread_id"] == "root-thread"
    assert row["agent_id"], "钩子要能拿到它被派给哪个子代理"


# --------------------------------------------------------------------------- #
# 3. additionalContext 注入：独立一条、紧挨 goal 之前、goal 原文不动
# --------------------------------------------------------------------------- #
async def test_additional_context_is_a_separate_message_before_the_goal(tmp_path: Path):
    body = (
        "sys.stdout.write(json.dumps({'hookSpecificOutput': {"
        "'hookEventName': 'SubagentStart', 'additionalContext': '仓库约定：先读 AGENTS.md'}}))\n"
    )
    command, _log = _log_hook(tmp_path, "ad-ctx", body)
    hooks = _hook_runner({"subagent_start": [{"command": command, "timeout": 20}]})
    adapter = ScriptedAdapter(["结论"])

    result = await _child(adapter, hooks=hooks)

    assert result.ok, result
    snapshot = adapter.calls[0]
    assert snapshot[-1]["content"] == "读取 README 并复述", "goal 原文不许被钩子改写"
    injected = snapshot[-2]["content"]
    assert injected.startswith(SUBAGENT_START_MARKER)
    assert "仓库约定：先读 AGENTS.md" in injected
    assert len(snapshot) == 3, snapshot  # system + 钩子上下文 + goal
    # 钩子的东西必须可辨认地属于"补充信息"，不能冒充用户任务。
    assert "只补充信息" in injected


# --------------------------------------------------------------------------- #
# 4. exit 2：可见告警，但不进模型上下文
# --------------------------------------------------------------------------- #
async def test_exit_two_becomes_a_visible_warning_not_an_instruction(tmp_path: Path):
    body = (
        "sys.stderr.reconfigure(encoding='utf-8')\n"
        "sys.stderr.write('钩子自己炸了：这不是作业指令')\n"
        "sys.exit(2)\n"
    )
    command, _log = _log_hook(tmp_path, "ad-exit2", body)
    hooks = _hook_runner({"subagent_start": [{"command": command, "timeout": 20}]})
    adapter = ScriptedAdapter(["照常完成"])
    recorder = _Events()

    result = await _child(adapter, hooks=hooks, recorder=recorder)

    assert result.ok, "SubagentStart 的 exit 2 不许阻断派发"
    assert result.summary == "照常完成"
    assert result.hook_warnings and "钩子自己炸了" in result.hook_warnings[0]
    assert "【子代理钩子告警】" in result.render()
    assert "钩子自己炸了" in result.render()
    messages = _contents(adapter.calls[0])
    assert not any("钩子自己炸了" in text for text in messages), messages
    assert len(messages) == 2, messages  # system + goal，没有注入消息
    assert recorder.named("hook.non_blocking_error"), recorder.events
    assert not recorder.named("hook.context_received")


# --------------------------------------------------------------------------- #
# 5. 无钩子零改动
# --------------------------------------------------------------------------- #
async def test_without_hooks_the_message_list_is_untouched():
    adapter = ScriptedAdapter(["结论"])
    await _child(adapter, hooks=None)
    snapshot = adapter.calls[0]
    assert len(snapshot) == 2, snapshot
    assert not any(SUBAGENT_START_MARKER in text for text in _contents(snapshot))


# --------------------------------------------------------------------------- #
# 6. 续聊不是新派发：SubagentStart 不重跑
# --------------------------------------------------------------------------- #
async def test_send_message_does_not_rerun_subagent_start(workspace: Path):
    task_id = "task-v54-continue"
    hooks = _Hooks(notes=["续聊不该看到第二份"])
    adapter = ScriptedAdapter(["第一轮结论", "续聊结论"])
    session = _session(workspace, adapter, task_id=task_id)
    session.hooks = hooks
    runner = SubAgentRunner().bind(session)
    tools = _tools()
    spawned = await tools["spawn_agent"].handler(
        _context(workspace, runner, task_id),
        {"goal": "查一下 main.py", "run_in_background": False},
    )
    assert spawned.ok is True, spawned.output
    agent_id = spawned.data["agent_id"]
    second = await tools["send_message"].handler(
        _context(workspace, runner, task_id),
        {"agent_id": agent_id, "message": "接着说"},
    )
    assert second.ok is True, second.message

    starts = [item for item in hooks.calls if item[0] == "subagent_start"]
    assert len(starts) == 1, starts
    assert starts[0][1] == "general-purpose"
    # 第一轮的注入消息随历史保留（只一份），第二轮不再新增。
    marker_counts = [text.count(SUBAGENT_START_MARKER) for text in _contents(adapter.calls[1])]
    assert sum(marker_counts) == 1, marker_counts


# --------------------------------------------------------------------------- #
# 7. SessionStart 的 additionalContext 真的进模型（真子进程钩子 + HTTP 端到端）
# --------------------------------------------------------------------------- #
async def test_session_start_context_reaches_the_model(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    body = (
        "sys.stdout.write(json.dumps({'hookSpecificOutput': {"
        "'hookEventName': 'SessionStart', 'additionalContext': '环境提示：本仓库用 Python 3.12'}}))\n"
    )
    command, _log = _log_hook(tmp_path, "ad-sstart", body)
    _hooks_env(monkeypatch, session_start=command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset(
        [
            {"content": "计划：\n1. 读取 main.py"},
            {"content": json.dumps({"summary": "done", "acceptance": []}, ensure_ascii=False)},
        ]
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "succeeded", resp.text

    texts = [
        str(message.get("content") or "")
        for request in env.mock.requests
        for message in request.get("messages") or []
    ]
    assert any("环境提示：本仓库用 Python 3.12" in text for text in texts), texts
    # 目标原文一个字不动：上下文是独立一条，不是拼进任务描述里。
    goals = [text for text in texts if text.startswith("任务目标：修改 main.py 并运行测试")]
    assert goals and all("环境提示" not in text for text in goals), texts
    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    injected = [event for event in events if event["type"] == "hook.context_injected"]
    assert injected and injected[0]["payload"]["count"] == 1, events


async def test_session_start_exit_two_warns_without_injecting(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    body = (
        "sys.stderr.reconfigure(encoding='utf-8')\n"
        "sys.stderr.write('会话钩子失败：不要把这段当上下文')\n"
        "sys.exit(2)\n"
    )
    command, _log = _log_hook(tmp_path, "ad-sstart2", body)
    _hooks_env(monkeypatch, session_start=command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset(
        [
            {"content": "计划：\n1. 读取 main.py"},
            {"content": json.dumps({"summary": "done", "acceptance": []}, ensure_ascii=False)},
        ]
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "succeeded", resp.text

    texts = [
        str(message.get("content") or "")
        for request in env.mock.requests
        for message in request.get("messages") or []
    ]
    assert not any("会话钩子失败" in text for text in texts), texts
    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    warnings = [event for event in events if event["type"] == "hook.non_blocking_error"]
    assert warnings and warnings[0]["payload"]["event"] == "session_start", events
    assert not [event for event in events if event["type"] == "hook.context_injected"]

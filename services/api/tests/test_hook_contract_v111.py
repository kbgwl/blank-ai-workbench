"""#111 钩子契约：ask / updatedInput / additionalContext / PostToolUseFailure。

判据分三层：
* 纯解析：Claude 的 hookSpecificOutput 与旧版顶层字段都要认，ask 不能读成放行；
* 收紧校验：updatedInput 过 schema，且不许把审批判定改宽、风险改高；
* 真链路：钩子改写后的参数必须真的被执行，ask 必须真的弹审批卡，allow 不许
  绕过 permission 策略，失败事件必须真的跑。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.services import permissions
from blank_api.services.agent.graph import _tightened_input
from blank_api.services.hooks import HookRunner, HookSpec, parse_hook_output
from blank_api.services.tools.base import ToolDefinition, ToolOutcome
from tests.test_agent_v04 import API, call, create_project, create_provider, create_task, env  # noqa: F401


def test_parse_hook_output_accepts_claude_nested_shape():
    parsed = parse_hook_output(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": "needs confirmation",
                    "updatedInput": {"path": "src/core.py"},
                    "additionalContext": "external index points to core.py",
                }
            }
        )
    )
    assert parsed["decision"] == "ask"
    assert parsed["reason"] == "needs confirmation"
    assert parsed["updated_input"] == {"path": "src/core.py"}
    assert parsed["additional_context"] == "external index points to core.py"


def test_parse_hook_output_keeps_legacy_shape_and_rejects_unknown():
    parsed = parse_hook_output('{"decision": "block", "reason": "no"}')
    assert (parsed["decision"], parsed["reason"]) == ("deny", "no")
    assert parse_hook_output('{"decision": "maybe"}')["decision"] == ""
    assert parse_hook_output("not json") == {}


async def test_exit_code_two_blocks_pre_tool_use_and_feeds_context_elsewhere(tmp_path):
    script = tmp_path / "block_exit2.py"
    script.write_text(
        "import sys\nsys.stderr.write('blocked by exit code 2')\nsys.exit(2)\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{script}"'
    runner = HookRunner(
        {
            "pre_tool_use": [HookSpec(command=command)],
            "post_tool_use_failure": [HookSpec(command=command)],
        }
    )

    blocked = await runner.run("pre_tool_use", "run_shell", {})
    assert blocked[0].decision == "deny"
    assert "blocked by exit code 2" in blocked[0].reason
    assert blocked[0].exit_code == 2

    # 非阻断事件上的退出码 2 不拦任何东西，而是把 stderr 当作给模型的上下文。
    failed = await runner.run("post_tool_use_failure", "run_shell", {})
    assert failed[0].decision == ""
    assert "blocked by exit code 2" in failed[0].additional_context


def _fake_session(decide):
    gateway = SimpleNamespace(decide=decide)
    return SimpleNamespace(gateway=gateway, context=None)


async def _noop(_context, _arguments):
    return ToolOutcome(ok=True)


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="demo",
        description="demo",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "mode": {"type": "string"},
                "danger": {"type": "boolean"},
            },
            "required": ["path"],
        },
        risk_level="low",
        risk_for=lambda args: "high" if args.get("danger") else "low",
        handler=_noop,
    )


def test_updated_input_is_schema_checked_and_must_not_loosen():
    def decide(tool, arguments, context):
        if arguments.get("mode") == "ask":
            return permissions.ask(tool.resolve_risk(arguments), "test")
        return permissions.approve(tool.resolve_risk(arguments), "test")

    session = _fake_session(decide)
    tool = _tool()

    accepted, reason = _tightened_input(session, tool, "demo", {"path": "a"}, {"path": "b"})
    assert accepted == {"path": "b"} and reason == ""

    rejected, reason = _tightened_input(session, tool, "demo", {"path": "a"}, {"foo": 1})
    assert rejected is None and "schema" in reason

    rejected, reason = _tightened_input(
        session, tool, "demo", {"path": "a"}, {"path": "a", "danger": True}
    )
    assert rejected is None and "风险" in reason

    rejected, reason = _tightened_input(session, tool, "demo", {"path": "a", "mode": "ask"}, {"path": "a"})
    assert rejected is None and "放宽" in reason


def _hook_command(tmp_path: Path, name: str, body: str) -> str:
    script = tmp_path / name
    script.write_text(
        "import json, sys\n"
        "sys.stdin.reconfigure(encoding='utf-8')\n"
        "payload = json.load(sys.stdin)\n"
        "tool = payload.get('tool') or ''\n"
        "args = payload.get('arguments') or {}\n" + body,
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


def _hooks(monkeypatch, event: str, command: str) -> None:
    monkeypatch.setenv(
        "BLANK_HOOKS", json.dumps({event: [{"command": command, "timeout": 20}]})
    )


def _mock_steps(steps: list[dict]) -> list[dict]:
    # 计划 / 执行 / 审查 / 总结：执行之后至少有两次 call_model，足以观察上下文注入。
    return steps + [
        {"content": json.dumps({"approved": True, "findings": [], "summary": "ok"}, ensure_ascii=False)},
        {
            "content": json.dumps(
                {"summary": "done", "acceptance": []}, ensure_ascii=False
            )
        },
    ]


async def test_hook_updated_input_rewrites_execution_and_context_reaches_model(
    env, workspace: Path, tmp_path: Path, monkeypatch
):  # noqa: F811
    command = _hook_command(
        tmp_path,
        "rewrite.py",
        "if payload.get('event') == 'pre_tool_use' and tool == 'read_file' and args.get('path') == 'main.py':\n"
        "    sys.stdout.write(json.dumps({'hookSpecificOutput': {\n"
        "        'hookEventName': 'PreToolUse',\n"
        "        'permissionDecision': 'allow',\n"
        "        'permissionDecisionReason': 'rewrite to core',\n"
        "        'updatedInput': {'path': 'src/core.py'},\n"
        "        'additionalContext': 'external index points to core.py',\n"
        "    }}))\n",
    )
    _hooks(monkeypatch, "pre_tool_use", command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读到 core.py"])
    env.mock.reset(
        _mock_steps(
            [
                {"content": "计划：\n1. 读取 main.py"},
                {"content": "读取", "tool_calls": [call("read_file", {"path": "main.py"})]},
            ]
        )
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    updated = [e for e in events if e["type"] == "hook.input_updated"]
    assert updated and updated[0]["payload"]["arguments"] == {"path": "src/core.py"}
    completed = [
        e
        for e in events
        if e["type"] == "tool.completed" and e["payload"].get("tool") == "read_file"
    ]
    assert completed and "VALUE = 1" in completed[0]["payload"]["summary"]
    assert any(e["type"] == "hook.context_received" for e in events)
    assert any(e["type"] == "hook.context_injected" for e in events)

    # 真发出去的请求里必须出现这条外部上下文，而不是只记了一条事件。
    texts = [
        str(message.get("content") or "")
        for request in env.mock.requests
        for message in request.get("messages") or []
    ]
    assert any("external index points to core.py" in text for text in texts)


async def test_hook_ask_escalates_a_low_risk_call_to_approval(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    command = _hook_command(
        tmp_path,
        "ask.py",
        "if tool == 'read_file':\n"
        "    sys.stdout.write(json.dumps({'hookSpecificOutput': {\n"
        "        'permissionDecision': 'ask',\n"
        "        'permissionDecisionReason': 'needs confirmation',\n"
        "    }}))\n",
    )
    _hooks(monkeypatch, "pre_tool_use", command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset(
        [
            {"content": "计划：\n1. 读取 main.py"},
            {"content": "读取", "tool_calls": [call("read_file", {"path": "main.py"})]},
        ]
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "waiting_approval"
    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    required = [e for e in events if e["type"] == "approval.required"]
    assert required and required[0]["payload"]["hook_ask"] == "needs confirmation"
    assert await env.tool_calls(task_id) == []


async def test_hook_allow_does_not_bypass_permission_policy(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    command = _hook_command(
        tmp_path,
        "allow.py",
        "if tool == 'run_shell':\n    sys.stdout.write(json.dumps({'decision': 'allow'}))\n",
    )
    _hooks(monkeypatch, "pre_tool_use", command)
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
    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    ignored = [e for e in events if e["type"] == "hook.allow_ignored"]
    assert ignored and ignored[0]["payload"]["tool"] == "run_shell"
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "keep me\n"


async def test_post_tool_use_failure_runs_on_failed_call(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    marker = tmp_path / "failure-hit.txt"
    command = _hook_command(
        tmp_path,
        "failure.py",
        "if payload.get('event') == 'post_tool_use_failure':\n"
        f"    open({str(marker)!r}, 'w', encoding='utf-8').write(tool)\n",
    )
    _hooks(monkeypatch, "post_tool_use_failure", command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取文件"])
    env.mock.reset(
        _mock_steps(
            [
                {"content": "计划：\n1. 读取 missing.txt"},
                {"content": "读取", "tool_calls": [call("read_file", {"path": "missing.txt"})]},
            ]
        )
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert marker.exists(), "post_tool_use_failure 没有在失败调用上运行"
    assert marker.read_text(encoding="utf-8") == "read_file"
    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    failures = [
        e
        for e in events
        if e["type"] == "hook.completed" and e["payload"].get("event") == "post_tool_use_failure"
    ]
    assert failures, events

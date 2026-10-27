"""update_plan 工具与 hooks 生命周期。

对标 Codex / Claude Code（拆解证据见 docs/REA-CODEX-CLAUDE.md）：
- 计划必须是可更新对象，且能容错解析模型给的各种形状
- 钩子必须能拦下工具调用，但**坏钩子绝不能把任务搞挂**（fail-open）
"""

import json
import sys
from pathlib import Path

import itertools

from blank_api.services.hooks import BLOCKING_EVENTS, HookRunner, blocking_denial, parse_decision
from blank_api.services.tools.base import ToolContext, validate_arguments
from blank_api.services.tools.plan import normalize_steps, plan_tools, render_plan


# ── update_plan ────────────────────────────────────────────────────────────

def test_update_plan_registered_with_low_risk():
    tools = {tool.name: tool for tool in plan_tools()}
    assert list(tools) == ["update_plan", "enter_plan_mode", "exit_plan_mode"]
    tool = tools["update_plan"]
    assert tool.risk_level == "low"
    # 计划是纯状态更新，不该触发审批
    assert tool.requires_approval is False
    # 模式开关也不靠 requires_approval 挂卡：退出是否需要人批由 permissions.decide
    # 的 mode:switch 决定（那里能看见当前模式与会话记忆，工具定义看不见）。
    for name in ("enter_plan_mode", "exit_plan_mode"):
        assert tools[name].risk_level == "low"
        assert tools[name].requires_approval is False


def test_update_plan_schema_rejects_missing_steps():
    tool = plan_tools()[0]
    assert validate_arguments(tool.input_schema, {"steps": []}) is None
    assert validate_arguments(tool.input_schema, {}) is not None


def test_normalize_steps_accepts_strings_and_objects():
    steps = normalize_steps(["读取文件", {"step": "应用补丁", "status": "in_progress"}])
    assert steps == [
        {"step": "读取文件", "status": "pending"},
        {"step": "应用补丁", "status": "in_progress"},
    ]


def test_normalize_steps_drops_junk_and_caps_length():
    steps = normalize_steps([None, "", "   ", {"nope": 1}, "x" * 500])
    assert len(steps) == 1
    assert len(steps[0]["step"]) == 200


def test_normalize_steps_unknown_status_falls_back_to_pending():
    assert normalize_steps([{"step": "a", "status": "whatever"}])[0]["status"] == "pending"


def test_render_plan_marks_each_state():
    text = render_plan(
        [
            {"step": "一", "status": "completed"},
            {"step": "二", "status": "in_progress"},
            {"step": "三", "status": "pending"},
        ]
    )
    assert "[x] 1. 一" in text and "[~] 2. 二" in text and "[ ] 3. 三" in text


async def test_update_plan_handler_returns_steps():
    tool = plan_tools()[0]
    context = ToolContext(root=Path("."), task_id="t", session_factory=None, approval_policy=lambda *_: False)
    outcome = await tool.handler(context, {"steps": [{"step": "读代码", "status": "completed"}], "explanation": "先看"})
    assert outcome.ok is True
    assert outcome.data["total"] == 1 and outcome.data["completed"] == 1
    assert outcome.data["steps"][0]["step"] == "读代码"


async def test_update_plan_handler_rejects_empty():
    tool = plan_tools()[0]
    context = ToolContext(root=Path("."), task_id="t", session_factory=None, approval_policy=lambda *_: False)
    outcome = await tool.handler(context, {"steps": []})
    assert outcome.ok is False and outcome.status == "error"


# ── hooks ──────────────────────────────────────────────────────────────────

def test_parse_decision_tolerates_wrapping_text():
    assert parse_decision('前缀 {"decision": "deny", "reason": "不许删"}\n') == ("deny", "不许删")
    assert parse_decision('{"permissionDecision": "block"}') == ("deny", "")
    assert parse_decision('{"decision": "approve"}') == ("allow", "")
    assert parse_decision("not json at all") == ("", "")


def test_from_env_empty_or_broken_disables_hooks():
    assert HookRunner.from_env("").enabled is False
    assert HookRunner.from_env("{ not json").enabled is False
    assert HookRunner.from_env('{"unknown_event": [{"command": "x"}]}').enabled is False
    assert HookRunner.from_env('{"pre_tool_use": [{"command": ""}]}').enabled is False


def test_from_env_parses_and_clamps_timeout():
    runner = HookRunner.from_env(
        '{"pre_tool_use":[{"command":"node guard.mjs","match":"run_shell","timeout":9999}]}'
    )
    assert runner.enabled is True
    spec = runner.hooks["pre_tool_use"][0]
    assert spec.match == "run_shell"
    assert spec.timeout == 60.0  # 上限收敛，避免钩子卡死任务


def test_hook_matching_by_target():
    runner = HookRunner.from_env('{"pre_tool_use":[{"command":"x","match":"run_shell"}]}')
    spec = runner.hooks["pre_tool_use"][0]
    assert spec.applies("run_shell") is True
    assert spec.applies("apply_patch") is False


_HOOK_SEQ = itertools.count()


def _hook_script(tmp_path: Path, body: str) -> str:
    """生成一个可执行的钩子脚本，用当前解释器跑，避免依赖 shell 方言。

    文件名必须每次唯一 —— 否则同一测试里写两个钩子会互相覆盖，
    看起来像"钩子没生效"，实际是测到自己写错的脚本。
    """
    script = tmp_path / f"hook_{next(_HOOK_SEQ)}.py"
    script.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


async def test_hook_can_deny_a_tool_call(tmp_path):
    command = _hook_script(tmp_path, "import sys, json; print(json.dumps({'decision': 'deny', 'reason': '禁止'}))")
    runner = HookRunner.from_env(json.dumps({"pre_tool_use": [{"command": command}]}))
    results = await runner.run("pre_tool_use", "run_shell", {"tool": "run_shell"})
    assert len(results) == 1
    assert results[0].denied is True
    assert blocking_denial(results) is results[0]


async def test_hook_stdin_receives_event_payload(tmp_path):
    command = _hook_script(
        tmp_path,
        "import sys, json; d = json.loads(sys.stdin.read()); "
        "print(json.dumps({'decision': 'allow' if d['tool'] == 'run_shell' else 'deny'}))",
    )
    runner = HookRunner.from_env(json.dumps({"pre_tool_use": [{"command": command}]}))
    results = await runner.run("pre_tool_use", "run_shell", {"tool": "run_shell"})
    assert results[0].decision == "allow"


async def test_failing_hook_does_not_block(tmp_path):
    command = _hook_script(tmp_path, "import sys; sys.exit(3)")
    runner = HookRunner.from_env(json.dumps({"pre_tool_use": [{"command": command}]}))
    results = await runner.run("pre_tool_use", "run_shell", {})
    assert results[0].status == "failed"
    # fail-open：钩子失败不能变成拒绝
    assert blocking_denial(results) is None


async def test_timeout_hook_does_not_block(tmp_path):
    command = _hook_script(tmp_path, "import time; time.sleep(30)")
    runner = HookRunner.from_env(json.dumps({"pre_tool_use": [{"command": command, "timeout": 0.5}]}))
    results = await runner.run("pre_tool_use", "run_shell", {})
    assert results[0].status == "timeout"
    assert blocking_denial(results) is None


async def test_non_blocking_event_ignores_deny(tmp_path):
    command = _hook_script(tmp_path, "import json; print(json.dumps({'decision': 'deny'}))")
    runner = HookRunner.from_env(json.dumps({"post_tool_use": [{"command": command}]}))
    results = await runner.run("post_tool_use", "run_shell", {})
    # post_tool_use 是通知性质：即便钩子说 deny，也不构成阻断（不在 BLOCKING_EVENTS 里）
    assert results[0].decision == "deny"
    assert "post_tool_use" not in BLOCKING_EVENTS


async def test_blocking_event_stops_after_first_deny(tmp_path):
    deny = _hook_script(tmp_path, "import json; print(json.dumps({'decision': 'deny'}))")
    allow = _hook_script(tmp_path, "import json; print(json.dumps({'decision': 'allow'}))")
    runner = HookRunner.from_env(
        json.dumps({"pre_tool_use": [{"command": deny}, {"command": allow}]})
    )
    results = await runner.run("pre_tool_use", "run_shell", {})
    assert len(results) == 1  # 第一个就拒了，不再往下跑


async def test_results_are_recorded_for_audit(tmp_path):
    command = _hook_script(tmp_path, "print('noop')")
    runner = HookRunner.from_env(json.dumps({"session_start": [{"command": command}]}))
    await runner.run("session_start", "", {})
    assert len(runner.results) == 1
    assert runner.results[0].event == "session_start"

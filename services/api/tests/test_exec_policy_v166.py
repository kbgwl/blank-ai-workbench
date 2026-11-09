"""v166：命令级策略（execpolicy）。

对标 Codex 的 execpolicy：批准/驳回的粒度不能停在"外层命令首词"。
本文件钉四件事：

1. 壳包装展开：`cmd /c "…"`、`powershell -Command "…"`、`bash -c "…"` 的内层载荷
   参与规则匹配；脚本路径（`bash script.sh`）不展开。
2. deny 是"任一段命中就拒"：`echo hi && rm -rf build` 里真正的删除动作不能搭便车；
   转义引号形状（`\\"…\\"`）归一后再比对，只收紧不放宽。
3. allow 仍是"每段都被覆盖"：`cmd /c "git status"` 可放行，
   `cmd /c "git status && rm -rf src"` 不行。
4. 默认拒绝档 `BLANK_SHELL_POLICY=allowlist`：没有显式批准的 run_shell 回到人手，
   显式规则/会话批准仍然放行；关闭时产品行为不变。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from blank_api.services import permissions
from blank_api.services.permission_rules import parse_rules, rules_path
from blank_api.services.permissions import (
    ALLOW,
    ASK,
    DENY,
    SCOPE_PREFIX,
    PermissionPolicy,
    covered_by_any,
    covered_by_rules,
    expand_command,
    prefix_rule,
    tokenize,
    unwrap_payload,
)
from blank_api.services.tools.base import ToolDefinition

from tests.test_agent_v04 import (  # noqa: F401  (复用端到端夹具)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)


def _tool(name: str = "run_shell", risk: str = "high", keys: tuple[str, ...] = ("command",)) -> ToolDefinition:
    async def handler(context, arguments):  # pragma: no cover - 只用于构造定义
        raise AssertionError("handler should not be called")

    return ToolDefinition(
        name=name,
        description="d",
        input_schema={"type": "object", "required": list(keys)},
        risk_level=risk,
        handler=handler,
    )


def _decide(policy: PermissionPolicy, command: str, risk: str = "high"):
    return policy.decide(_tool(), {"command": command}, risk)


def _policy(text: str = "", mode: str = "default", **kwargs) -> PermissionPolicy:
    policy = PermissionPolicy(mode=mode, **kwargs)
    if text:
        ruleset = parse_rules(text, source="test")
        assert ruleset.error == "", ruleset.error
        policy.ruleset = ruleset
    return policy


# --------------------------------------------------------------------------- #
# 1. 展开
# --------------------------------------------------------------------------- #
def test_unwrap_payload_only_fires_for_inline_wrappers() -> None:
    assert unwrap_payload(tokenize('cmd /c "rm -rf build"')) == "rm -rf build"
    assert unwrap_payload(tokenize('powershell -Command "Write-Host x"')) == "Write-Host x"
    assert unwrap_payload(tokenize('bash -c "git status"')) == "git status"
    # 脚本路径不是内联载荷：展开它等于替用户批准脚本里的任意内容。
    assert unwrap_payload(tokenize("bash script.sh")) == ""
    assert unwrap_payload(tokenize("git status")) == ""


def test_expand_command_returns_original_and_inner_segments() -> None:
    expanded = expand_command('cmd /c "git status && rm -rf src"')
    assert expanded == ['cmd /c "git status && rm -rf src"', "git status", "rm -rf src"]


def test_expansion_is_bounded_for_deep_nesting() -> None:
    # 四层以上的嵌套不再展开：匹配输入必须封顶，不能让一条命令把规则比对拖爆。
    command = "cmd /c " * 12 + "rm -rf build"
    expanded = expand_command(command)
    assert expanded, expanded
    assert len(expanded) <= permissions._MAX_EXPANDED_SEGMENTS


# --------------------------------------------------------------------------- #
# 2. deny：任一段命中就拒
# --------------------------------------------------------------------------- #
def test_deny_rule_reaches_payload_inside_cmd_wrapper() -> None:
    policy = _policy("deny run_shell(rm -rf build)")
    decision = _decide(policy, 'cmd /c "rm -rf build"')
    assert decision.outcome == DENY, decision
    assert decision.rule == "rule:deny"


def test_deny_rule_reaches_payload_inside_powershell_wrapper() -> None:
    policy = _policy("deny run_shell(rm -rf build)")
    decision = _decide(policy, 'powershell -Command "cmd /c \\"rm -rf build\\""')
    assert decision.outcome == DENY, decision
    assert decision.rule == "rule:deny"


def test_deny_rule_denies_when_only_one_segment_matches() -> None:
    # 逐段全覆盖才算拒绝，会让真正的删除动作藏在无害段后面。
    policy = _policy("deny run_shell(rm -rf build)")
    decision = _decide(policy, "echo hi && rm -rf build")
    assert decision.outcome == DENY, decision
    assert decision.rule == "rule:deny"


def test_deny_matching_is_not_fooled_by_quoted_text() -> None:
    # 归一化只用于 deny 比对，且按段首前缀匹配：`echo "rm -rf build"` 只是打印，
    # 不能因为里面出现了被驳回的字面量就把一条无害命令拒掉。
    policy = _policy("deny run_shell(rm -rf build)")
    decision = _decide(policy, 'echo "rm -rf build"', risk="medium")
    assert decision.outcome != DENY, decision


def test_session_denial_reaches_inside_a_wrapper() -> None:
    policy = _policy()
    policy.denied_prefixes.add(prefix_rule("rm -rf build"))
    decision = _decide(policy, 'cmd /c "rm -rf build"')
    assert decision.outcome == DENY, decision
    assert decision.rule == "prefix:denied"


# --------------------------------------------------------------------------- #
# 3. allow：每一段都要被覆盖
# --------------------------------------------------------------------------- #
def test_allow_rule_covers_wrapped_command() -> None:
    policy = _policy("allow run_shell(git status)")
    assert _decide(policy, 'cmd /c "git status"').outcome == ALLOW
    assert _decide(policy, 'bash -c "git status"').outcome == ALLOW


def test_allow_rule_does_not_cover_a_smuggled_second_segment() -> None:
    policy = _policy("allow run_shell(git status)")
    decision = _decide(policy, 'cmd /c "git status && rm -rf src"')
    assert decision.outcome == ASK, decision


def test_legacy_exact_wrapper_rule_still_approves_the_same_text() -> None:
    # 用户当时批准的是整串 `cmd /c "git status"`；旧规则形状不能因为新增展开而失效。
    ruleset = parse_rules('allow run_shell(cmd /c "git status")', source="test")
    assert ruleset.error == "", ruleset.error
    policy = PermissionPolicy()
    policy.ruleset = ruleset
    assert _decide(policy, 'cmd /c "git status"').outcome == ALLOW


def test_allow_matching_requires_every_original_segment_to_match() -> None:
    rules = {prefix_rule("git status")}
    assert covered_by_rules('cmd /c "git status"', rules) == "git status"
    assert covered_by_rules('cmd /c "git status && rm -rf src"', rules) is None
    assert covered_by_any("echo hi && rm -rf build", {prefix_rule("rm -rf build")}) == "rm -rf build"


# --------------------------------------------------------------------------- #
# 4. 默认拒绝档
# --------------------------------------------------------------------------- #
def test_shell_allowlist_defaults_to_off(monkeypatch) -> None:
    monkeypatch.delenv("BLANK_SHELL_POLICY", raising=False)
    policy = _policy()
    # 产品现状：中风险命令按 risk:auto 放行。
    assert _decide(policy, "echo hi", risk="medium").outcome == ALLOW


def test_shell_allowlist_turns_unapproved_commands_into_ask(monkeypatch) -> None:
    monkeypatch.setenv("BLANK_SHELL_POLICY", "allowlist")
    policy = _policy()
    decision = _decide(policy, "echo hi", risk="medium")
    assert decision.outcome == ASK, decision
    assert decision.rule == "policy:shell_allowlist"


def test_shell_allowlist_keeps_explicit_approvals_working(monkeypatch) -> None:
    monkeypatch.setenv("BLANK_SHELL_POLICY", "allowlist")
    policy = _policy("allow run_shell(git status)")
    assert _decide(policy, "git status").outcome == ALLOW

    session = _policy()
    session.approved_prefixes.add(prefix_rule("git status"))
    assert _decide(session, "git status").outcome == ALLOW


def test_shell_allowlist_does_not_touch_other_tools(monkeypatch) -> None:
    monkeypatch.setenv("BLANK_SHELL_POLICY", "allowlist")
    policy = _policy()
    tool = _tool(name="read_file", risk="medium", keys=("path",))
    decision = policy.decide(tool, {"path": "a.txt"}, "medium")
    assert decision.outcome == ALLOW, decision


# --------------------------------------------------------------------------- #
# 5. 端到端：deny 规则在包装形状下真的挡住了执行
# --------------------------------------------------------------------------- #
def _wrapped_delete_command() -> tuple[str, str]:
    """返回 (命令, 规则载荷)：两种平台上"真跑就会删掉 core.py"的包装形状。"""
    if os.name == "nt":
        return 'cmd /c "del src\\core.py"', "del src\\core.py"
    return 'sh -c "rm -f src/core.py"', "rm -f src/core.py"


async def test_deny_rule_blocks_a_wrapped_delete_before_it_runs(
    env, workspace: Path  # noqa: F811
) -> None:
    command, payload = _wrapped_delete_command()
    rules_path().write_text(f"deny run_shell({payload})\n", encoding="utf-8")
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["包装形状拒绝"])

    env.mock.reset(
        [
            {"content": "执行计划：\n1. 删除 src/core.py"},
            {"content": "删除", "tool_calls": [call("run_shell", {"command": command})]},
            {"content": "已完成"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    # 真跑的话文件就没了：断言的是"命令没执行"，不是"任务失败"。
    assert (workspace / "src" / "core.py").exists()

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    completed = [
        event
        for event in events
        if event["type"] == "tool.completed" and event["payload"].get("tool") == "run_shell"
    ]
    assert len(completed) == 1, events
    permission = completed[0]["payload"].get("permission") or {}
    assert permission.get("outcome") == "deny", permission
    assert permission.get("rule") == "rule:deny", permission


@pytest.mark.skipif(os.name != "nt", reason="cmd 包装的删除只在 Windows 上真能执行")
async def test_allow_rule_runs_a_wrapped_delete_that_really_happens(
    env, workspace: Path  # noqa: F811
) -> None:
    # 正对照：同一条命令、只把规则从 deny 换成 allow。文件真被删掉，
    # 证明上面那条用例的红不是"命令本来就跑不起来"造成的。
    command, payload = _wrapped_delete_command()
    rules_path().write_text(f"allow run_shell({payload})\n", encoding="utf-8")
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["包装形状放行"])

    env.mock.reset(
        [
            {"content": "执行计划：\n1. 删除 src/core.py"},
            {"content": "删除", "tool_calls": [call("run_shell", {"command": command})]},
            {"content": "已完成"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert not (workspace / "src" / "core.py").exists()

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    started = [
        event
        for event in events
        if event["type"] == "tool.completed" and event["payload"].get("tool") == "run_shell"
    ]
    assert len(started) == 1, events
    permission = started[0]["payload"].get("permission") or {}
    assert permission.get("outcome") == "allow", permission
    assert permission.get("rule", "").startswith("rule:allow:"), permission

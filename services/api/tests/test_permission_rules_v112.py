"""#112：人手写的 allow / deny 规则（`permissions.rules`）。

对标 Claude Code 的 ``settings.json`` 权限规则与 Codex 的 rules 文件，但落点是
一份**纯文本、可带注释**的文件：程序只读不写，用户改完不必重启进程也能生效。

本文件钉六件事：

1. 语法：注释、空行、裸工具名、`tool(spec)` 都认；行号留痕；
2. 任何一行写坏 ⇒ 整份文件停用并带出错误（不能只丢坏行——写坏的往往正是 deny）；
3. deny 优先于一切：bypass 模式、计划模式、``approve_high_risk``、本会话刚批过的动作；
4. allow 不能越过不可逆闸门，也不能越过本会话的驳回；run_shell 必须逐段覆盖；
5. 路径规则只在工作区内生效，`..` / 绝对路径/ 盘符路径一律不匹配；
6. 端到端：坏文件进 ``permission_rules.loaded`` 事件且仍然挂卡；好规则真的免卡；
   deny 规则下命令没有执行（文件还在）。
"""

from pathlib import Path

import pytest

from blank_api.services.approvals import approval_key
from blank_api.services.permission_rules import (
    MAX_RULE_LEN,
    load_ruleset,
    parse_rules,
    rules_path,
)
from blank_api.services.permissions import (
    ALLOW,
    ASK,
    DENY,
    MODE_BYPASS,
    MODE_PLAN,
    PermissionPolicy,
    prefix_rule,
)
from blank_api.services.tools.base import ToolDefinition

from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具)
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


def _policy(text: str, mode: str = "default") -> PermissionPolicy:
    ruleset = parse_rules(text, source="test")
    assert ruleset.error == "", ruleset.error
    policy = PermissionPolicy(mode=mode)
    policy.ruleset = ruleset
    return policy


@pytest.fixture
def rules_file(_isolate_permission_rules) -> Path:
    """conftest 已把 BLANK_PERMISSION_RULES 指到本用例的临时文件。"""
    return rules_path()


# --------------------------------------------------------------------------- #
# 1. 语法
# --------------------------------------------------------------------------- #
def test_parse_accepts_comments_bare_tools_and_specs() -> None:
    text = (
        "# 永远不问\n"
        "allow run_shell(git status)\n"
        "\n"
        "  allow read_file(src/**)  \n"
        "deny run_shell(rm -rf build)\n"
        "allow list_files\n"
    )
    ruleset = parse_rules(text, source="mem")
    assert ruleset.error == ""
    assert [rule.render() for rule in ruleset.rules] == [
        "allow run_shell(git status)",
        "allow read_file(src/**)",
        "deny run_shell(rm -rf build)",
        "allow list_files",
    ]
    # 行号跟着原始文本走：报错时用户要能用编辑器一步跳过去。
    assert [rule.line for rule in ruleset.rules] == [2, 4, 5, 6]
    assert ruleset.rules[3].spec == ""
    assert ruleset.rules[3].matches("list_files", {}) is True
    assert ruleset.rules[3].matches("read_file", {"path": "a"}) is False


@pytest.mark.parametrize(
    "text",
    [
        "allow\n",
        "grant run_shell(git status)\n",
        "allow run_shell(git status\n",
        "allow run_shell()\n",
        "allow bad-name\n",
        "allow " + "x" * MAX_RULE_LEN + "\n",
    ],
    ids=["no-tool", "unknown-action", "unclosed", "empty-spec", "bad-tool", "too-long"],
)
def test_malformed_line_disables_the_whole_file(text: str) -> None:
    # 第一行本身合法：整份停用必须连它一起丢，否则"写坏的 deny 静默消失"。
    ruleset = parse_rules("allow run_shell(git status)\n" + text, source="mem")
    assert ruleset.rules == ()
    assert ruleset.error


def test_missing_file_is_an_empty_ruleset(rules_file: Path) -> None:
    assert not rules_file.exists()
    ruleset = load_ruleset()
    assert ruleset.rules == ()
    assert ruleset.error == ""
    assert ruleset.path == str(rules_file)


def test_undecodable_file_reports_error(rules_file: Path) -> None:
    rules_file.write_bytes(b"\xff\xfe\x00\x00")
    ruleset = load_ruleset()
    assert ruleset.rules == ()
    assert "读取失败" in ruleset.error


def test_written_file_roundtrips(rules_file: Path) -> None:
    rules_file.write_text(
        "# 手写\nallow run_shell(git status)\ndeny read_file(secrets/**)\n", encoding="utf-8"
    )
    ruleset = load_ruleset()
    assert ruleset.error == ""
    assert [rule.render() for rule in ruleset.rules] == [
        "allow run_shell(git status)",
        "deny read_file(secrets/**)",
    ]
    assert ruleset.path == str(rules_file)


# --------------------------------------------------------------------------- #
# 2. deny 优先于一切
# --------------------------------------------------------------------------- #
def test_deny_beats_every_mode_and_session_memory() -> None:
    command = "rm -rf build"
    ruleset_text = "deny run_shell(rm -rf build)\n"
    for mode in (MODE_BYPASS, MODE_PLAN, "default", "accept_edits"):
        policy = _policy(ruleset_text, mode=mode)
        verdict = _decide(policy, command)
        assert verdict.outcome == DENY, mode
        assert verdict.rule == "rule:deny"
        assert "rm -rf build" in verdict.reason

    policy = _policy(ruleset_text)
    policy.approve_high_risk = True
    policy.approved_actions.add(approval_key("run_shell", {"command": command}))
    policy.approved_prefixes.add(prefix_rule("rm -rf"))
    verdict = _decide(policy, command)
    assert verdict.outcome == DENY
    assert verdict.rule == "rule:deny"


def test_deny_with_bare_interpreter_keeps_its_refusal_power() -> None:
    # 裸解释器名不配做"免问"规则，但"永远别跑 powershell"必须照样生效。
    policy = _policy("deny run_shell(powershell)\n")
    verdict = _decide(policy, 'powershell -Command "Write-Host x"')
    assert verdict.outcome == DENY
    assert verdict.rule == "rule:deny"


# --------------------------------------------------------------------------- #
# 3. allow 的边界
# --------------------------------------------------------------------------- #
def test_allow_runs_high_risk_shell_only_when_every_segment_is_covered() -> None:
    policy = _policy("allow run_shell(git status)\n")
    verdict = _decide(policy, "git status -s")
    assert verdict.outcome == ALLOW
    assert verdict.rule == "rule:allow:run_shell(git status)"

    # 第二段没被任何规则覆盖 ⇒ 不能搭顺风车
    assert _decide(policy, "git status && rm -rf src").outcome == ASK
    # 另一个子命令也不算
    assert _decide(policy, "git diff").outcome == ASK


def test_allow_cannot_override_irreversible_or_session_denial() -> None:
    policy = _policy("allow run_shell(git push)\n")
    verdict = _decide(policy, "git push --force origin main")
    assert verdict.outcome == ASK
    assert verdict.rule == "irreversible"

    policy = _policy("allow run_shell(git status)\n")
    policy.forget("run_shell", {"command": "git status -s"})
    verdict = _decide(policy, "git status -s")
    assert verdict.outcome == DENY
    assert verdict.rule == "action:denied"

    policy = _policy("allow run_shell(git status)\n")
    policy.denied_prefixes.add(prefix_rule("git status"))
    verdict = _decide(policy, "git status -s")
    assert verdict.outcome == DENY
    assert verdict.rule == "prefix:denied"


def test_allow_with_bare_interpreter_is_not_admissible() -> None:
    # 免问一个裸解释器名等于把任意载荷交给模型；deny 那一路不受这条限制。
    policy = _policy("allow run_shell(python)\n")
    assert _decide(policy, 'python -c "print(1)"').outcome == ASK


def test_path_rules_stay_inside_the_workspace() -> None:
    policy = _policy("deny read_file(secrets/**)\nallow apply_patch(src/**)\n")
    reader = _tool("read_file", keys=("path",))
    patcher = _tool("apply_patch", keys=("path",))

    assert policy.decide(reader, {"path": "secrets/token.txt"}, "high").outcome == DENY
    assert policy.decide(reader, {"path": "./secrets/token.txt"}, "high").outcome == DENY
    assert policy.decide(reader, {"path": "src/core.py"}, "high").outcome == ASK
    assert policy.decide(patcher, {"path": "src/core.py"}, "high").rule == (
        "rule:allow:apply_patch(src/**)"
    )
    # 工作区外的一律不靠规则放行：相对逃逸与绝对路径都不匹配。
    assert policy.decide(patcher, {"path": "../outside.py"}, "high").outcome == ASK
    assert policy.decide(patcher, {"path": "C:/Windows/x.py"}, "high").outcome == ASK

    outside = parse_rules("allow read_file(../secrets/**)\ndeny read_file(/etc/**)\n")
    assert outside.error == ""
    assert outside.rules[0].matches("read_file", {"path": "../secrets/a"}) is False
    assert outside.rules[1].matches("read_file", {"path": "/etc/passwd"}) is False
    # 目标本身合法（没有 `..`、不是绝对路径），只有"规则指向盘符路径"这一道挡着：
    # 去掉它 fnmatch 就会命中，所以这条断言是路径闸门的唯一红点。
    drive = parse_rules("allow read_file(C:/Windows/**)\n")
    assert drive.error == ""
    assert drive.rules[0].matches("read_file", {"path": "C:/Windows/x.py"}) is False


def test_ruleset_lands_in_snapshot() -> None:
    policy = _policy("allow run_shell(git status)\ndeny read_file(secrets/**)\n")
    summary = policy.snapshot()["ruleset"]
    assert summary["allow"] == ["allow run_shell(git status)"]
    assert summary["deny"] == ["deny read_file(secrets/**)"]
    assert summary["error"] == ""


# --------------------------------------------------------------------------- #
# 4. 端到端
# --------------------------------------------------------------------------- #
async def test_malformed_rules_file_is_reported_and_task_still_asks(
    env, workspace: Path, monkeypatch  # noqa: F811
) -> None:
    path = rules_path()
    path.write_text("allow run_shell(git status\n", encoding="utf-8")
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["坏规则文件"])

    env.mock.reset(
        [
            {"content": "执行计划：\n1. 跑一段脚本"},
            {"content": "跑脚本", "tool_calls": [call("run_shell", {"command": 'python -c "print(1)" https://a.example'})]},
            {"content": "已完成"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    # fail-closed：文件坏了就一条规则都不认，高风险动作照样挂卡。
    assert resp.json()["task"]["status"] == "waiting_approval"

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    loaded = [event for event in events if event["type"] == "permission_rules.loaded"]
    assert len(loaded) == 1, events
    payload = loaded[0]["payload"]
    assert payload["allow"] == [] and payload["deny"] == []
    assert payload["error"], payload
    assert payload["path"] == str(path)


async def test_allow_rule_runs_high_risk_command_without_a_card(
    env, workspace: Path  # noqa: F811
) -> None:
    command = 'python -c "print(1)" https://a.example'
    rules_path().write_text(f"allow run_shell({command.replace(chr(34), '')})\n", encoding="utf-8")
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["规则放行"])

    env.mock.reset(
        [
            {"content": "执行计划：\n1. 跑一段脚本"},
            {"content": "跑脚本", "tool_calls": [call("run_shell", {"command": command})]},
            {"content": "已完成"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] != "waiting_approval", resp.json()["task"]

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    assert not [event for event in events if event["type"] == "approval.granted"]
    loaded = [event for event in events if event["type"] == "permission_rules.loaded"]
    assert len(loaded) == 1
    assert loaded[0]["payload"]["allow"] == [f"allow run_shell({command.replace(chr(34), '')})"]
    rules_seen = [
        (event["payload"].get("permission") or {}).get("rule")
        for event in events
        if event["type"] == "tool.completed" and event["payload"].get("tool") == "run_shell"
    ]
    assert rules_seen == ["rule:allow:run_shell(python -c print(1) https://a.example)"], rules_seen


async def test_deny_rule_blocks_the_command_before_it_runs(
    env, workspace: Path  # noqa: F811
) -> None:
    rules_path().write_text("deny run_shell(rm -rf src)\n", encoding="utf-8")
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["规则拒绝"])

    env.mock.reset(
        [
            {"content": "执行计划：\n1. 删除 src"},
            {"content": "删除", "tool_calls": [call("run_shell", {"command": "rm -rf src"})]},
            {"content": "已完成"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    # 真执行了的话这个文件就没了 —— 这里断言的是"命令没跑"，不是"任务失败"。
    assert (workspace / "src" / "core.py").exists()

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    completed = [
        event
        for event in events
        if event["type"] == "tool.completed" and event["payload"].get("tool") == "run_shell"
    ]
    assert len(completed) == 1, events
    permission = completed[0]["payload"].get("permission") or {}
    assert permission.get("rule") == "rule:deny"
    assert permission.get("outcome") == "deny"

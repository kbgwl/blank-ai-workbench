"""#254：settings.json 的 ``permissions.allow/deny/ask``（Claude Code 形状）。

对标 Claude 的三组权限规则，落点是设置文件里已有的 `permissions` 段（外加
`defaultMode`）。与 #112 的手写文本文件互补：文本文件先命中，settings 规则排在
后面；两份合起来才是运行时生效的规则集。

本文件钉七件事：

1. 工具名表与真实工具注册表逐名对齐，别名没有指到不存在的工具；
2. ``Bash(cmd:*)`` 是前缀、``Bash(cmd)`` 是整串 glob（上游口径），
   ``Bash(*)`` 是整工具；
3. 路径规则只认工作区内相对通配，指向工作区外的一律不算；
4. 写不出来的条目：**allow 记 note 跳过**（少放行＝安全），**deny/ask 整份停用**
   ——一份看起来防住了却没生效的 deny 比没有更糟；
5. settings 规则与文本文件的合并顺序、错误/note/sha 的合并口径；
6. 归一化认 Claude 的 ``defaultMode``（含 ``acceptEdits`` / ``bypassPermissions``），
   设置往返不丢规则；
7. 判定优先级：deny > 计划模式闸门 > ask > bypass / approve_high_risk / 会话批准 /
   allow；ask 卡上不给「本会话总是允许」（下一次照样命中，按了不兑现）。
"""

from pathlib import Path

import pytest

from blank_api.services.approvals import approval_key
from blank_api.services.permission_rules import (
    CLAUDE_TOOL_ALIASES,
    LOCAL_TOOL_NAMES,
    empty_ruleset,
    merge_rulesets,
    parse_rules,
    parse_settings_rules,
)
from blank_api.services.permissions import (
    ALLOW,
    ASK,
    DENY,
    MODE_BYPASS,
    MODE_PLAN,
    PermissionPolicy,
    approval_options,
    prefix_rule,
)
from blank_api.services.settings import SettingsStore, normalize
from blank_api.services.tools import build_default_tools
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


def _policy(permissions: dict, mode: str = "default") -> PermissionPolicy:
    ruleset = parse_settings_rules(permissions, source="settings.json")
    assert ruleset.error == "", ruleset.error
    policy = PermissionPolicy(mode=mode)
    policy.ruleset = ruleset
    return policy


# --------------------------------------------------------------------------- #
# 1. 工具名表
# --------------------------------------------------------------------------- #
def test_local_tool_names_match_the_real_registry_exactly() -> None:
    assert LOCAL_TOOL_NAMES == {tool.name for tool in build_default_tools()}


def test_every_alias_points_at_a_real_local_tool() -> None:
    assert set(CLAUDE_TOOL_ALIASES.values()) <= LOCAL_TOOL_NAMES
    # 语义能一一对上的才映射：Bash 的执行/审批口径就是 run_shell。
    assert CLAUDE_TOOL_ALIASES["Bash"] == "run_shell"
    assert CLAUDE_TOOL_ALIASES["Task"] == "spawn_agent"


# --------------------------------------------------------------------------- #
# 2. 前缀 vs 整串 glob
# --------------------------------------------------------------------------- #
def test_colon_star_is_a_prefix_and_bare_is_a_whole_command_glob() -> None:
    prefix = parse_settings_rules({"allow": ["Bash(git status:*)"]})
    assert prefix.error == ""
    assert prefix.rules[0].mode == "prefix"
    assert prefix.match("allow", "run_shell", {"command": "git status"}) is not None
    assert prefix.match("allow", "run_shell", {"command": "git status -s"}) is not None
    assert prefix.match("allow", "run_shell", {"command": "git diff"}) is None

    # 前缀落在 token 边界上：npm run testing 不在 npm run test 之下。
    npm = parse_settings_rules({"allow": ["Bash(npm run test:*)"]})
    assert npm.match("allow", "run_shell", {"command": "npm run test -- --watch"}) is not None
    assert npm.match("allow", "run_shell", {"command": "npm run testing"}) is None

    exact = parse_settings_rules({"allow": ["Bash(git status)"]})
    assert exact.error == ""
    assert exact.rules[0].mode == "glob"
    assert exact.match("allow", "run_shell", {"command": "git status"}) is not None
    # 上游 `aV` 口径：整条子命令做 glob，多一个参数就不命中。
    assert exact.match("allow", "run_shell", {"command": "git status -s"}) is None


def test_bash_star_covers_the_whole_tool() -> None:
    ruleset = parse_settings_rules({"allow": ["Bash(*)"]})
    assert ruleset.error == ""
    assert ruleset.rules[0].spec == ""
    assert ruleset.match("allow", "run_shell", {"command": "anything at all"}) is not None


def test_glob_allow_requires_every_segment_and_deny_hits_any_segment() -> None:
    allow = parse_settings_rules({"allow": ["Bash(git status)"]})
    assert allow.match("allow", "run_shell", {"command": "git status && rm -rf src"}) is None

    deny = parse_settings_rules({"deny": ["Bash(rm -rf *)"]})
    assert deny.match("deny", "run_shell", {"command": "echo hi && rm -rf src"}) is not None
    # 壳载荷里的内层命令也要抓到，否则换一层壳就绕过去了。
    assert deny.match("deny", "run_shell", {"command": 'cmd /c "rm -rf src"'}) is not None


# --------------------------------------------------------------------------- #
# 3. 路径规则
# --------------------------------------------------------------------------- #
def test_path_rules_only_match_workspace_relative_paths() -> None:
    ruleset = parse_settings_rules(
        {"allow": ["Read(src/**)"], "deny": ["Read(secrets/**)"]}
    )
    assert ruleset.error == ""
    assert ruleset.match("allow", "read_file", {"path": "src/core.py"}) is not None
    assert ruleset.match("allow", "read_file", {"path": "./src/core.py"}) is not None
    assert ruleset.match("deny", "read_file", {"path": "secrets/token.txt"}) is not None
    assert ruleset.match("allow", "read_file", {"path": "lib/x.py"}) is None
    # settings 里直接写本地工具名也认。
    local = parse_settings_rules({"allow": ["edit_file(src/**)"]})
    assert local.rules[0].tool == "edit_file"
    assert local.match("allow", "edit_file", {"path": "src/core.py"}) is not None


# --------------------------------------------------------------------------- #
# 4. 写不出来的条目
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "failing",
    [
        {"deny": ["mcp__gh__create_pr"]},
        {"deny": ["NoSuchTool"]},
        {"deny": ["Bash(*:*)"]},
        {"deny": ["Read(/etc/**)"]},
        {"deny": ["Read(../outside/**)"]},
        {"deny": ["Read(C:/Windows/**)"]},
        {"deny": ["WebFetch(domain:example.com)"]},
        {"deny": ["Bash(git status"]},
        {"deny": ["Bash()"]},
        {"ask": ["mcp__gh__create_pr"]},
        {"deny": "Bash(rm -rf *)"},
        {"deny": [42]},
    ],
    ids=[
        "mcp-deny",
        "unknown-tool",
        "wildcard-prefix",
        "absolute-path",
        "escaping-path",
        "drive-path",
        "domain-rule",
        "unclosed-paren",
        "empty-spec",
        "mcp-ask",
        "not-a-list",
        "non-string",
    ],
)
def test_deny_or_ask_that_cannot_be_honoured_disables_the_whole_settings_ruleset(
    failing: dict,
) -> None:
    # 第一条本身合法：整份停用必须连它一起丢，否则"写坏的 deny 静默消失"。
    ruleset = parse_settings_rules({"allow": ["Bash(git status:*)"], **failing})
    assert ruleset.rules == ()
    assert ruleset.error, ruleset


def test_allow_entry_that_cannot_be_honoured_is_a_note_not_a_failure() -> None:
    ruleset = parse_settings_rules(
        {
            "allow": [
                "Bash(git status:*)",
                "NoSuchTool",
                "mcp__gh__create_pr",
                "Read(/etc/**)",
                "Bash(*:*)",
                "  ",
            ]
        }
    )
    # 空串直接跳过（不算 note）；其余四条写不出来 ⇒ note，其余规则照常生效。
    assert ruleset.error == ""
    assert [rule.canonical() for rule in ruleset.rules] == ["allow prefix run_shell(git status)"]
    assert len(ruleset.notes) == 4
    summary = ruleset.summary()
    assert summary["error"] == ""
    assert len(summary["notes"]) == 4


def test_permissions_must_be_an_object() -> None:
    assert parse_settings_rules(["Bash(git status:*)"]).error
    assert parse_settings_rules(42).error
    # 空/缺失是「没配」，不是错误：正常运行不该因为这一段的默认值刷事件。
    for empty in (None, {}, []):
        parsed = parse_settings_rules(empty)
        assert parsed.rules == () and parsed.error == "" and parsed.notes == ()


def test_normalized_defaults_produce_no_rules_and_no_notes() -> None:
    parsed = parse_settings_rules(normalize({})["permissions"])
    assert parsed.rules == () and parsed.error == "" and parsed.notes == ()


def test_summary_reports_ask_only_when_there_is_one() -> None:
    asked = parse_settings_rules({"ask": ["Bash(git push:*)"]})
    assert asked.summary()["ask"] == ["ask run_shell(git push)"]
    assert "ask" not in parse_settings_rules({"allow": ["Bash(git status:*)"]}).summary()
    assert "notes" not in parse_settings_rules({"allow": ["Bash(git status:*)"]}).summary()


# --------------------------------------------------------------------------- #
# 5. 合并
# --------------------------------------------------------------------------- #
def test_merge_keeps_file_rules_first() -> None:
    file_rules = parse_rules("allow run_shell(git diff --stat)\n", source="permissions.rules")
    settings_rules = parse_settings_rules(
        {"allow": ["Bash(git diff:*)", "NoSuchTool"]}, source="settings.json"
    )
    merged = merge_rulesets(file_rules, settings_rules)
    assert [rule.canonical() for rule in merged.rules] == [
        "allow prefix run_shell(git diff --stat)",
        "allow prefix run_shell(git diff)",
    ]
    # 两条都命中时取第一条：文件里的规则先被读到，用户手写的就是更具体的意图。
    assert merged.match(
        "allow", "run_shell", {"command": "git diff --stat"}
    ).selector() == "run_shell(git diff --stat)"
    reversed_ = merge_rulesets(settings_rules, file_rules)
    assert reversed_.match(
        "allow", "run_shell", {"command": "git diff --stat"}
    ).selector() == "run_shell(git diff)"
    assert merged.sha != reversed_.sha, "顺序变了就是另一版规则集"
    assert merged.notes == settings_rules.notes
    assert merged.path == "permissions.rules + settings.json"


def test_merge_joins_errors_and_keeps_the_empty_distinction() -> None:
    broken = parse_settings_rules({"deny": ["NoSuchTool"]}, source="settings.json")
    merged = merge_rulesets(empty_ruleset(), broken)
    assert merged.rules == ()
    assert merged.error == broken.error
    # 「没加载过规则文件」才是不带指纹；加载过一份坏文件同样不是"生效过"。
    assert merged.sha == ""
    assert merge_rulesets(empty_ruleset(), empty_ruleset()).sha == ""
    empty_file = parse_rules("", source="permissions.rules")
    assert merge_rulesets(empty_ruleset(), empty_file).sha == empty_file.sha
    # 有规则的一侧与坏掉的一侧合并：规则保留、错误照报、指纹按规则重算。
    live = parse_settings_rules({"allow": ["Bash(git status:*)"]}, source="settings.json")
    mixed = merge_rulesets(parse_rules("allow run_shell(git diff)\n", source="permissions.rules"), live, broken)
    assert len(mixed.rules) == 2
    assert mixed.error == broken.error
    assert mixed.sha == merge_rulesets(parse_rules("allow run_shell(git diff)\n", source="a"), live).sha


# --------------------------------------------------------------------------- #
# 6. 归一化与设置往返
# --------------------------------------------------------------------------- #
def test_normalize_understands_claude_mode_names_and_cleans_the_lists() -> None:
    data = normalize(
        {
            "permissions": {
                "defaultMode": "acceptEdits",
                "allow": [" Bash(git status:*) ", "", 3, "x" * 500],
                "deny": "Bash(rm -rf *)",
                "ask": None,
            }
        }
    )
    assert data["permissions"]["default_mode"] == "accept_edits"
    assert data["permissions"]["allow"] == ["Bash(git status:*)"]
    assert data["permissions"]["deny"] == []
    assert data["permissions"]["ask"] == []
    # 直接 lower() 会把这两个名字折成认不出的形状，静默回落 default。
    assert normalize({"permissions": {"defaultMode": "bypassPermissions"}})["permissions"][
        "default_mode"
    ] == "bypass"
    assert normalize({"permissions": {"defaultMode": "plan"}})["permissions"]["default_mode"] == "plan"
    # 下划线写法优先。
    assert normalize(
        {"permissions": {"default_mode": "plan", "defaultMode": "bypassPermissions"}}
    )["permissions"]["default_mode"] == "plan"


def test_settings_store_roundtrips_the_three_lists(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    stored = store.update(
        {
            "permissions": {
                "default_mode": "accept_edits",
                "allow": ["Bash(git status:*)"],
                "deny": ["Read(secrets/**)"],
            }
        }
    )
    assert stored["permissions"]["allow"] == ["Bash(git status:*)"]
    reread = store.read()
    assert reread["permissions"] == {
        "default_mode": "accept_edits",
        "allow": ["Bash(git status:*)"],
        "deny": ["Read(secrets/**)"],
        "ask": [],
    }
    # 整段替换语义：PATCH 一个空列表就是清空，别的两条不受影响。
    store.update({"permissions": {"deny": []}})
    reread = store.read()
    assert reread["permissions"]["allow"] == ["Bash(git status:*)"]
    assert reread["permissions"]["deny"] == []
    assert reread["permissions"]["default_mode"] == "accept_edits"


# --------------------------------------------------------------------------- #
# 7. 判定优先级
# --------------------------------------------------------------------------- #
def test_deny_beats_every_mode_and_session_memory() -> None:
    command = "git push origin main"
    for mode in (MODE_BYPASS, MODE_PLAN, "default", "accept_edits"):
        verdict = _decide(_policy({"deny": ["Bash(git push:*)"]}, mode=mode), command)
        assert verdict.outcome == DENY, mode
        assert verdict.rule == "rule:deny"
        assert "git push" in verdict.reason

    policy = _policy({"deny": ["Bash(git push:*)"]})
    policy.approve_high_risk = True
    policy.approved_actions.add(approval_key("run_shell", {"command": command}))
    policy.approved_prefixes.add(prefix_rule("git push"))
    policy.approved_tools.add((policy.sandbox_tier, "run_shell", ""))
    assert _decide(policy, command).outcome == DENY


def test_ask_beats_bypass_approve_flag_and_session_memory() -> None:
    command = "git push origin main"
    for mode in (MODE_BYPASS, "default", "accept_edits"):
        verdict = _decide(_policy({"ask": ["Bash(git push:*)"]}, mode=mode), command)
        assert verdict.outcome == ASK, mode
        assert verdict.rule.startswith("rule:ask:")
        assert verdict.tool_scope_allowed is False

    policy = _policy({"ask": ["Bash(git push:*)"], "allow": ["Bash(git push:*)"]})
    policy.approve_high_risk = True
    policy.approved_actions.add(approval_key("run_shell", {"command": command}))
    policy.approved_prefixes.add(prefix_rule("git push"))
    verdict = _decide(policy, command)
    assert verdict.outcome == ASK
    assert verdict.as_dict()["tool_scope_allowed"] is False
    # 卡片上不出现「本会话总是允许」：ask 规则下一次照样命中，按了也不兑现。
    options = approval_options("run_shell", verdict.memo, tool_scope=verdict.tool_scope_allowed)
    assert [option["scope"] for option in options] == ["once"]


def test_ask_rule_covers_path_tools_and_drops_the_tool_scope() -> None:
    policy = _policy({"ask": ["Read(secrets/**)"]})
    reader = _tool("read_file", keys=("path",))
    verdict = policy.decide(reader, {"path": "secrets/token.txt"}, "high")
    assert verdict.outcome == ASK
    assert verdict.rule == "rule:ask:read_file(secrets/**)"
    assert verdict.tool_scope_allowed is False


def test_deny_beats_ask_and_ask_does_not_soften_the_plan_gate() -> None:
    both = _policy({"deny": ["Bash(git push:*)"], "ask": ["Bash(git push:*)"]})
    assert _decide(both, "git push origin main").outcome == DENY

    # 计划模式下的写操作是拒绝，不是"变成一张审批卡"。
    plan = _policy({"ask": ["Write(src/**)"]}, mode=MODE_PLAN)
    writer = _tool("write_file", keys=("path",))
    verdict = plan.decide(writer, {"path": "src/core.py"}, "high")
    assert verdict.outcome == DENY
    assert verdict.rule == "mode:plan"


def test_allow_runs_high_risk_tools_but_cannot_override_irreversible_or_session_denial() -> None:
    policy = _policy({"allow": ["Read(src/**)"]})
    reader = _tool("read_file", keys=("path",))
    verdict = policy.decide(reader, {"path": "src/core.py"}, "high")
    assert verdict.outcome == ALLOW
    assert verdict.rule == "rule:allow:read_file(src/**)"

    push = _policy({"allow": ["Bash(git push:*)"]})
    verdict = _decide(push, "git push --force origin main")
    assert verdict.outcome == ASK
    assert verdict.rule == "irreversible"

    denied = _policy({"allow": ["Bash(git status:*)"]})
    denied.forget("run_shell", {"command": "git status -s"})
    verdict = _decide(denied, "git status -s")
    assert verdict.outcome == DENY
    assert verdict.rule == "action:denied"

    prefix_denied = _policy({"allow": ["Bash(git status:*)"]})
    prefix_denied.denied_prefixes.add(prefix_rule("git status"))
    assert _decide(prefix_denied, "git status -s").rule == "prefix:denied"


# --------------------------------------------------------------------------- #
# 8. 端到端：settings 规则真的进审批链
# --------------------------------------------------------------------------- #
def _settings_events(events: list[dict]) -> list[dict]:
    return [event for event in events if event["type"] == "permission_rules.settings"]


async def test_settings_deny_is_reported_and_the_command_never_runs(
    env, workspace: Path  # noqa: F811
) -> None:
    SettingsStore().update({"permissions": {"deny": ["Bash(rm -rf src)"]}})
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["settings 规则拒绝"])

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
    # 真跑过的话这个文件就没了 —— 这里断言的是"命令没跑"。
    assert (workspace / "src" / "core.py").exists()

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    reported = _settings_events(events)
    assert len(reported) == 1, events
    payload = reported[0]["payload"]
    assert payload["source"] == "user"
    assert payload["deny"] == ["deny run_shell(rm -rf src)"]
    assert payload["error"] == ""
    loaded = [event for event in events if event["type"] == "permission_rules.loaded"]
    assert len(loaded) == 1
    assert loaded[0]["payload"]["deny"] == ["deny run_shell(rm -rf src)"]

    completed = [
        event
        for event in events
        if event["type"] == "tool.completed" and event["payload"].get("tool") == "run_shell"
    ]
    assert len(completed) == 1, events
    permission = completed[0]["payload"].get("permission") or {}
    assert permission.get("rule") == "rule:deny"
    assert permission.get("outcome") == "deny"


async def test_settings_allow_skips_the_card_and_shows_in_the_trace(
    env, workspace: Path  # noqa: F811
) -> None:
    command = 'python -c "print(1)" https://a.example'
    SettingsStore().update(
        {"permissions": {"allow": ["Bash(python -c print(1) https://a.example)"]}}
    )
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["settings 规则放行"])

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
    reported = _settings_events(events)
    assert len(reported) == 1
    assert reported[0]["payload"]["allow"] == [
        "allow run_shell(python -c print(1) https://a.example)"
    ]
    rules_seen = [
        (event["payload"].get("permission") or {}).get("rule")
        for event in events
        if event["type"] == "tool.completed" and event["payload"].get("tool") == "run_shell"
    ]
    assert rules_seen == ["rule:allow:run_shell(python -c print(1) https://a.example)"]


async def test_settings_ask_still_asks_without_a_tool_scope_button(
    env, workspace: Path  # noqa: F811
) -> None:
    SettingsStore().update({"permissions": {"ask": ["Read(secrets/**)"]}})
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["settings 每次问"])

    env.mock.reset(
        [
            {"content": "执行计划：\n1. 看密钥"},
            {
                "content": "看密钥",
                "tool_calls": [call("read_file", {"path": "secrets/token.txt"})],
            },
            {"content": "已完成"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "waiting_approval", resp.json()["task"]

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    reported = _settings_events(events)
    assert len(reported) == 1
    assert reported[0]["payload"]["ask"] == ["ask read_file(secrets/**)"]
    waiting = [
        event
        for event in events
        if event["type"] in ("approval.required", "approval.requested")
        and event["payload"].get("tool") == "read_file"
    ]
    assert waiting, events
    payload = waiting[-1]["payload"]
    assert payload["permission_rule"] == "rule:ask:read_file(secrets/**)"
    # read_file 不在 ARGUMENT_SCOPED_TOOLS 里，工具档不会被风险收窄挡掉；
    # 这里只剩「ask 本身不给工具档」这一道，选项必须只有「仅本次允许」。
    assert [option["scope"] for option in payload["approval_options"]] == ["once"]

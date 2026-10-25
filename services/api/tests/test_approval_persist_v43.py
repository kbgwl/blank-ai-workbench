"""#68：把"总是允许这个前缀"落盘，换任务/重启进程后仍然认得。

对标 Codex 的 ``~/.codex/rules/default.rules``。此前只有两层记忆：会话内
（``PermissionPolicy``）与任务内（回放 ``approval.granted``），换一个任务就要
重新问一遍相同命令。

本文件钉五件事：
1. 只落 ``scope=prefix`` 的前缀档；``once`` / ``tool`` 不写盘（宽口径写盘等于整机永久放行）；
2. 文件可读可手改：重复规则只留一条、最新在前、有上限；
3. 文件缺失/损坏/版本不认识 ⇒ 一条规则都不认（fail-closed），宁可多问一次；
4. 落盘规则仍受原有边界约束：不可逆命令、复合命令的第二段、另一段内联正文都不放行，
   本会话的驳回优先于落盘放行；
5. 端到端：批准一次前缀 → 文件里出现该规则 → 新任务的同一命令不再挂卡，
   且追踪里记的是 ``prefix:persistent:…``，不是"这次会话刚批的"。
"""

import json
from pathlib import Path

import pytest

from blank_api.services import approval_rules
from blank_api.services.permissions import (
    ALLOW,
    ASK,
    SCOPE_PREFIX,
    SCOPE_TOOL,
    PermissionPolicy,
    render_rule,
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

RULE_INLINE = ("python", "-c", "print(1)")


def _tool(name: str = "run_shell", risk: str = "high") -> ToolDefinition:
    async def handler(context, arguments):  # pragma: no cover - 只用于构造定义
        raise AssertionError("handler should not be called")

    return ToolDefinition(
        name=name,
        description="d",
        input_schema={"type": "object", "required": ["command"]},
        risk_level=risk,
        handler=handler,
    )


def _decide(policy: PermissionPolicy, command: str, risk: str = "high"):
    return policy.decide(_tool(), {"command": command}, risk)


@pytest.fixture
def approvals_file(_isolate_approvals_file) -> Path:
    """conftest 已给每个用例指了一份独立的空规则文件。"""
    return approval_rules.path()


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. 存储形状：最新在前、去重、有上限、只收 run_shell 前缀
# --------------------------------------------------------------------------- #
def test_rule_file_roundtrip(approvals_file: Path) -> None:
    assert approval_rules.add_rule("run_shell", RULE_INLINE, source_task="task-a") is True
    assert approval_rules.add_rule("run_shell", ("git", "push")) is True

    raw = json.loads(approvals_file.read_text(encoding="utf-8"))
    assert raw["version"] == approval_rules.VERSION
    assert [item["tokens"] for item in raw["rules"]] == [["git", "push"], list(RULE_INLINE)]

    rules = approval_rules.load_rules()
    assert rules[0]["source_task"] == ""
    assert rules[1]["source_task"] == "task-a"
    assert all(item["created_at"] for item in rules)
    assert approval_rules.load_prefix_rules() == {("git", "push"), RULE_INLINE}


def test_duplicate_rule_moves_to_front_without_duplicating(approvals_file: Path) -> None:
    approval_rules.add_rule("run_shell", RULE_INLINE)
    approval_rules.add_rule("run_shell", ("git", "push"))
    approval_rules.add_rule("run_shell", RULE_INLINE, source_task="again")

    # 读侧会去重，所以这里直接看文件本身：同一串只该有一行，且最新在前。
    raw = json.loads(approvals_file.read_text(encoding="utf-8"))["rules"]
    assert [item["tokens"] for item in raw] == [list(RULE_INLINE), ["git", "push"]]
    assert raw[0]["source_task"] == "again"

    rules = approval_rules.load_rules()
    assert [tuple(item["tokens"]) for item in rules] == [RULE_INLINE, ("git", "push")]
    assert rules[0]["source_task"] == "again"


def test_rule_cap_keeps_the_newest(approvals_file: Path) -> None:
    total = approval_rules.MAX_RULES + 5
    for index in range(total):
        assert approval_rules.add_rule("run_shell", ("echo", f"n{index}")) is True

    # 上限要在写盘时就生效：手改的文件也不该长到无界。
    raw = json.loads(approvals_file.read_text(encoding="utf-8"))["rules"]
    assert len(raw) == approval_rules.MAX_RULES

    rules = approval_rules.load_rules()
    assert len(rules) == approval_rules.MAX_RULES
    assert tuple(rules[0]["tokens"]) == ("echo", f"n{total - 1}")
    assert tuple(rules[-1]["tokens"]) == ("echo", f"n{total - approval_rules.MAX_RULES}")


def test_only_run_shell_prefixes_are_accepted(approvals_file: Path) -> None:
    assert approval_rules.add_rule("write_file", ("notes.md",)) is False
    assert approval_rules.add_rule("run_shell", ()) is False
    assert approval_rules.add_rule("run_shell", ("", None)) is False
    assert not approvals_file.exists()

    # 空白项被丢掉，剩下的照收
    assert approval_rules.add_rule("run_shell", ("python", "", None, "-c")) is True
    assert approval_rules.load_rules()[0]["tokens"] == ["python", "-c"]


# --------------------------------------------------------------------------- #
# 2. fail-closed：读不出来就当没批过
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        "[]",
        json.dumps({"version": 99, "rules": [{"tool": "run_shell", "tokens": ["python"]}]}),
        json.dumps({"version": 1, "rules": "nope"}),
    ],
    ids=["broken-json", "wrong-root", "unknown-version", "rules-not-list"],
)
def test_unreadable_file_means_no_rules(approvals_file: Path, payload: str) -> None:
    approvals_file.write_text(payload, encoding="utf-8")
    assert approval_rules.load_rules() == []
    assert approval_rules.load_prefix_rules() == set()


def test_missing_file_means_no_rules_until_one_is_added(approvals_file: Path) -> None:
    assert not approvals_file.exists()
    assert approval_rules.load_prefix_rules() == set()
    assert approval_rules.add_rule("run_shell", RULE_INLINE) is True
    assert approvals_file.exists()


def test_malformed_entries_are_skipped_one_by_one(approvals_file: Path) -> None:
    _write(
        approvals_file,
        {
            "version": 1,
            "rules": [
                "junk",
                {"tool": "write_file", "tokens": ["x"]},
                {"tool": "run_shell", "tokens": []},
                {"tool": "run_shell", "tokens": ["python", "-c", "print(1)"]},
                {"tool": "run_shell", "tokens": ["python", "-c", "print(1)"]},
            ],
        },
    )
    rules = approval_rules.load_rules()
    assert [tuple(item["tokens"]) for item in rules] == [RULE_INLINE]


def test_undecodable_bytes_are_fail_closed(approvals_file: Path) -> None:
    approvals_file.write_bytes(b"\xff\xfe\x00\x00")
    assert approval_rules.load_rules() == []


# --------------------------------------------------------------------------- #
# 3. 判定：落盘规则更窄、更晚、且让不开本会话的驳回
# --------------------------------------------------------------------------- #
def test_persisted_prefix_approves_with_its_own_attribution(approvals_file: Path) -> None:
    approval_rules.add_rule("run_shell", RULE_INLINE)
    policy = PermissionPolicy()
    policy.persistent_prefixes = approval_rules.load_prefix_rules()

    verdict = _decide(policy, "python -c 'print(1)'")
    assert verdict.outcome == ALLOW
    assert verdict.rule == "prefix:persistent:python -c print(1)"
    # 追踪里要分得清"以前落盘的"和"这次会话批的"
    snapshot = policy.snapshot()
    assert snapshot["persistent_prefixes"] == ["python -c print(1)"]
    assert snapshot["approved_prefixes"] == []


@pytest.mark.parametrize(
    "command",
    [
        'python -c "print(2)"',
        "python evil.py",
        "python -m pip install requests",
        "python -c 'print(1)' && python evil.py",
        'node -e "console.log(1)"',
    ],
    ids=["other-body", "other-script", "other-module", "compound-second-segment", "other-interpreter"],
)
def test_persisted_rule_does_not_widen(approvals_file: Path, command: str) -> None:
    approval_rules.add_rule("run_shell", RULE_INLINE)
    policy = PermissionPolicy()
    policy.persistent_prefixes = approval_rules.load_prefix_rules()
    assert _decide(policy, command).outcome == ASK, command


def test_hand_edited_irreversible_rule_still_asks(approvals_file: Path) -> None:
    _write(
        approvals_file,
        {
            "version": 1,
            "rules": [{"tool": "run_shell", "tokens": ["git", "push", "--force"]}],
        },
    )
    policy = PermissionPolicy()
    policy.persistent_prefixes = approval_rules.load_prefix_rules()
    verdict = _decide(policy, "git push --force origin main")
    assert verdict.outcome == ASK
    assert verdict.rule == "irreversible"


def test_session_denial_beats_the_persisted_allow(approvals_file: Path) -> None:
    approval_rules.add_rule("run_shell", RULE_INLINE)
    policy = PermissionPolicy()
    policy.persistent_prefixes = approval_rules.load_prefix_rules()
    # 直接摆前缀档的状态：remember/forget 还会写动作键，那是更早、更窄的一层，
    # 这里要验的是"落盘规则不得越过本会话的前缀驳回"。
    policy.denied_prefixes.add(RULE_INLINE)

    verdict = _decide(policy, "python -c 'print(1)'")
    assert verdict.outcome != ALLOW
    assert verdict.rule == "prefix:denied"


def test_session_approval_wins_the_trace_when_both_match(approvals_file: Path) -> None:
    approval_rules.add_rule("run_shell", RULE_INLINE)
    policy = PermissionPolicy()
    policy.persistent_prefixes = approval_rules.load_prefix_rules()
    policy.approved_prefixes.add(RULE_INLINE)

    assert _decide(policy, "python -c 'print(1)'").rule == "prefix:python -c print(1)"


# --------------------------------------------------------------------------- #
# 4. 端到端：批准 → 落盘 → 新任务不再挂卡
# --------------------------------------------------------------------------- #
def _persisted_events(events: list[dict]) -> list[dict]:
    return [event for event in events if event["type"] == "approval.rule_persisted"]


async def test_prefix_grant_persists_and_a_new_task_auto_approves(env, workspace: Path):  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    first_task = await create_task(env, project_id, ["批准一次前缀"])
    # 带 URL 才会被判高风险而挂卡；前缀规则本身只到内联正文为止。
    command = 'python -c "print(1)" https://a.example'
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 跑一段内联脚本"},
            {"content": "跑脚本", "tool_calls": [call("run_shell", {"command": command})]},
            {"content": "已完成"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(
        f"{API}/tasks/{first_task}/run", json={"provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "waiting_approval"

    resp = await env.client.post(
        f"{API}/tasks/{first_task}/approval",
        json={"approve": True, "provider_id": provider_id, "scope": SCOPE_PREFIX},
    )
    assert resp.status_code == 200, resp.text

    events = (await env.client.get(f"{API}/tasks/{first_task}/events")).json()
    persisted = _persisted_events(events)
    assert len(persisted) == 1, events
    payload = persisted[0]["payload"]
    assert payload["tool"] == "run_shell"
    assert payload["prefix"] == render_rule(RULE_INLINE)
    assert payload["persisted"] is True
    rules = approval_rules.load_rules()
    assert [tuple(item["tokens"]) for item in rules] == [RULE_INLINE]
    assert rules[0]["source_task"] == first_task

    # 新任务：同一命令不再挂卡，追踪里念的是"落盘的规则"
    second_task = await create_task(env, project_id, ["同一命令再来一次"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 同一段脚本"},
            {"content": "跑脚本", "tool_calls": [call("run_shell", {"command": command})]},
            {"content": "已完成"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(
        f"{API}/tasks/{second_task}/run", json={"provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] != "waiting_approval", resp.json()["task"]

    events = (await env.client.get(f"{API}/tasks/{second_task}/events")).json()
    assert not [event for event in events if event["type"] == "approval.granted"]
    rules_seen = [
        (event["payload"].get("permission") or {}).get("rule")
        for event in events
        if event["type"] == "tool.completed" and event["payload"].get("tool") == "run_shell"
    ]
    assert rules_seen == ["prefix:persistent:python -c print(1)"], rules_seen


async def test_tool_scope_is_never_persisted(env, workspace: Path):  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["只批本会话该工具"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 跑一段脚本"},
            {"content": "跑脚本", "tool_calls": [call("run_shell", {"command": 'python -c "print(2)" https://b.example'})]},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "waiting_approval"

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={"approve": True, "provider_id": provider_id, "scope": SCOPE_TOOL},
    )
    assert resp.status_code == 200, resp.text

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    assert _persisted_events(events) == []
    assert approval_rules.load_prefix_rules() == set()

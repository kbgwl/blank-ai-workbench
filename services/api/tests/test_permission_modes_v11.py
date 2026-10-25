"""P1-1：四档权限模式、命令前缀放行、会话级批准记忆。

覆盖三层：
1. 纯函数层——命令前缀签名、逐段覆盖判定、不可逆识别；
2. 策略层——PermissionPolicy.decide 在四种模式与各类记忆下的判定；
3. 端到端——图里中断一次、带上作用域批准之后，同类命令不再重复打断，
   并且这份记忆在审批恢复（进程内重建 AgentSession）后依然生效。
"""

import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from blank_api.services.permissions import (
    ALLOW,
    ASK,
    DENY,
    MODE_ACCEPT_EDITS,
    MODE_BYPASS,
    MODE_DEFAULT,
    MODE_PLAN,
    SCOPE_ONCE,
    SCOPE_PREFIX,
    SCOPE_TOOL,
    PermissionPolicy,
    approval_options,
    command_prefix,
    covered_by_rules,
    is_irreversible,
    memoizable_prefix,
    parse_decision,
    prefix_rule,
)
from blank_api.services.tools.base import ToolDefinition, ok
from blank_api.services.tools.gateway import ToolGateway

from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)


def _tool(name: str = "run_shell", risk: str = "medium") -> ToolDefinition:
    async def handler(context, arguments):  # pragma: no cover - 只用于构造定义
        raise AssertionError("handler should not be called")

    return ToolDefinition(
        name=name,
        description="d",
        input_schema={"type": "object"},
        risk_level=risk,
        handler=handler,
    )


# --------------------------------------------------------------------------- #
# 1. 命令前缀
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git status -s", "git status"),
        ("git diff HEAD -- .", "git diff"),
        ("npm run build", "npm run"),
        ("npm install", "npm install"),
        ("pytest -q", "pytest"),
        ("python -m pytest", "python"),
        (r"C:\Python312\python.exe -V", "python"),
        ("/usr/bin/git log -n 5", "git log"),
        ("  ", ""),
    ],
)
def test_command_prefix_signature(command: str, expected: str) -> None:
    assert command_prefix(command) == expected


def test_prefix_covers_whole_command_only() -> None:
    rules = [prefix_rule("git status")]
    # 单段命中
    assert covered_by_rules("git status -s", rules) == "git status"
    # 可执行名走 basename 归一，跨平台写法都能命中
    assert covered_by_rules("/usr/bin/git status", rules) == "git status"
    # 子命令不同就不算命中
    assert covered_by_rules("git diff HEAD", rules) is None
    # 第二段没被覆盖时不能放行——否则 rm 会搭上 npm test 的顺风车
    assert covered_by_rules("git status && echo hi", rules) is None
    assert covered_by_rules("git status && git status", rules) == "git status"


def test_irreversible_commands_are_flagged() -> None:
    assert is_irreversible("rm -rf /") is True
    assert is_irreversible("mkfs.ext4 /dev/sda1") is True
    assert is_irreversible("DROP DATABASE prod") is True
    assert is_irreversible("git push origin main --force") is True
    # 工作区内的删除是可回滚的（有快照），必须仍走普通高风险审批
    assert is_irreversible("rm -rf notes.txt") is False
    assert is_irreversible("git status") is False


def test_parse_decision_accepts_bool_and_scoped_dict() -> None:
    assert parse_decision(True) == (True, SCOPE_ONCE)
    assert parse_decision(False) == (False, SCOPE_ONCE)
    assert parse_decision({"approve": True, "scope": "prefix"}) == (True, SCOPE_PREFIX)
    assert parse_decision({"decision": "approve", "scope": "tool"}) == (True, SCOPE_TOOL)
    assert parse_decision({"decision": "deny"}) == (False, SCOPE_ONCE)
    # 未知作用域退回最保守的"仅本次"
    assert parse_decision({"approve": True, "scope": "everything"}) == (True, SCOPE_ONCE)


def test_approval_options_list_scopes_for_shell() -> None:
    scopes = [item["scope"] for item in approval_options("run_shell", "git push")]
    assert scopes == [SCOPE_ONCE, SCOPE_PREFIX, SCOPE_TOOL]
    # 非命令类工具没有"该前缀"这一档
    assert [item["scope"] for item in approval_options("apply_patch")] == [SCOPE_ONCE, SCOPE_TOOL]


def test_only_memoizable_prefixes_get_the_prefix_scope() -> None:
    """记住 ``rm`` 等于放行整个会话的删除——这类裸可执行名不给"总是允许"这一档。"""
    assert memoizable_prefix("run_shell", "git push") == "git push"
    assert memoizable_prefix("run_shell", "npm run") == "npm run"
    # 跑构建/测试类工具，单个可执行名也可以记
    assert memoizable_prefix("run_shell", "pytest") == "pytest"
    # 解释器不是"构建工具"：裸名记下来等于把任意载荷交给模型自选，
    # 必须带到真正被执行的那一段（这里靠 v37 的细则用例守住）。
    assert memoizable_prefix("run_shell", "python") == ""
    assert memoizable_prefix("run_shell", "node") == ""
    # 危险裸名一律拒绝
    for dangerous in ("rm", "bash", "sh", "curl", "git", "powershell", "pwsh", "cmd"):
        memo = memoizable_prefix("run_shell", dangerous)
        assert memo == "", dangerous
        scopes = [item["scope"] for item in approval_options("run_shell", memo)]
        assert SCOPE_PREFIX not in scopes, dangerous
    assert memoizable_prefix("apply_patch", "git push") == ""


def test_remember_refuses_to_widen_a_dangerous_prefix() -> None:
    policy = PermissionPolicy()
    # 即便调用方误传 scope=prefix，rm 也只会被记成"仅本次"
    policy.remember("run_shell", {"command": "rm -rf build"}, SCOPE_PREFIX)
    assert not policy.approved_prefixes
    assert _decide(policy, "run_shell", {"command": "rm -rf dist"}, "high").outcome == ASK
    # 裸解释器同样记不出"整个 python"
    policy.remember("run_shell", {"command": "python"}, SCOPE_PREFIX)
    assert not policy.approved_prefixes
    assert _decide(policy, "run_shell", {"command": "python evil.py"}, "high").outcome == ASK
    # 不可逆命令即便带了可记忆的前缀（git push）也不进账本
    policy.remember("run_shell", {"command": "git push origin main --force"}, SCOPE_PREFIX)
    assert not policy.approved_prefixes
    assert (
        _decide(policy, "run_shell", {"command": "git push origin main"}, "high").outcome == ASK
    )


# --------------------------------------------------------------------------- #
# 2. 策略判定
# --------------------------------------------------------------------------- #
def _decide(policy: PermissionPolicy, name: str, arguments: dict, risk: str = "medium"):
    return policy.decide(_tool(name, risk), arguments)


def test_default_mode_asks_only_for_high_risk() -> None:
    policy = PermissionPolicy()
    assert _decide(policy, "read_file", {"path": "a.py"}, "low").outcome == ALLOW
    assert _decide(policy, "run_shell", {"command": "ls"}, "medium").outcome == ALLOW
    asked = _decide(policy, "run_shell", {"command": "curl https://x"}, "high")
    assert asked.outcome == ASK
    assert asked.prefix == "curl"
    assert asked.rule == "risk:ask"


def test_plan_mode_only_allows_read_only_tools() -> None:
    policy = PermissionPolicy(mode=MODE_PLAN)
    assert _decide(policy, "read_file", {"path": "a.py"}, "low").outcome == ALLOW
    assert _decide(policy, "search_code", {"pattern": "x"}, "low").outcome == ALLOW
    assert _decide(policy, "update_plan", {"steps": ["a"]}, "low").outcome == ALLOW

    denied = _decide(policy, "apply_patch", {"path": "a.py", "patch": ""}, "medium")
    assert denied.outcome == DENY
    assert denied.rule == "mode:plan"
    assert "计划模式" in denied.reason
    assert _decide(policy, "run_shell", {"command": "ls"}, "medium").outcome == DENY


def test_accept_edits_allows_file_writes_but_not_shell() -> None:
    policy = PermissionPolicy(mode=MODE_ACCEPT_EDITS)
    assert _decide(policy, "apply_patch", {"path": "a.py", "patch": "x"}, "medium").outcome == ALLOW
    # 敏感路径的补丁仍是高风险，accept_edits 不能把它吞掉
    assert _decide(policy, "apply_patch", {"path": ".env", "patch": "x"}, "high").outcome == ASK
    assert _decide(policy, "run_shell", {"command": "curl https://x"}, "high").outcome == ASK


def test_bypass_mode_still_asks_for_irreversible_commands() -> None:
    policy = PermissionPolicy(mode=MODE_BYPASS)
    assert _decide(policy, "run_shell", {"command": "curl https://x"}, "high").outcome == ALLOW
    overridden = _decide(policy, "run_shell", {"command": "mkfs.ext4 /dev/sda1"}, "high")
    assert overridden.outcome == ASK
    assert overridden.rule == "irreversible"


def test_session_prefix_memory_skips_the_next_ask() -> None:
    policy = PermissionPolicy()
    first = _decide(policy, "run_shell", {"command": "git push origin main"}, "high")
    assert first.outcome == ASK

    policy.remember("run_shell", {"command": "git push origin main"}, SCOPE_PREFIX)
    again = _decide(policy, "run_shell", {"command": "git push origin dev"}, "high")
    assert again.outcome == ALLOW
    assert again.rule == "prefix:git push"
    # 前缀之外的命令不受影响
    assert _decide(policy, "run_shell", {"command": "git reset --hard"}, "high").outcome == ASK


def test_session_tool_memory_skips_the_next_ask() -> None:
    policy = PermissionPolicy()
    policy.remember("run_shell", {"command": "curl https://a"}, SCOPE_TOOL)
    # 工具档覆盖该工具的所有参数组合——这正是 "本会话总是允许 run_shell" 的语义
    assert _decide(policy, "run_shell", {"command": "curl https://b"}, "high").outcome == ALLOW
    assert _decide(policy, "run_shell", {"command": "wget https://b"}, "high").outcome == ALLOW
    # 但不会外溢到别的工具
    assert _decide(policy, "apply_patch", {"path": ".env", "patch": "x"}, "high").outcome == ASK
    # 不可逆命令仍在工具档之前判定，照样逐条确认（#70 的裁决）
    override = _decide(policy, "run_shell", {"command": "git push --force origin main"}, "high")
    assert override.outcome == ASK
    assert override.rule == "irreversible"


def test_once_scope_does_not_promote_to_prefix() -> None:
    policy = PermissionPolicy()
    policy.remember("run_shell", {"command": "git push origin main"}, SCOPE_ONCE)
    assert _decide(policy, "run_shell", {"command": "git push origin main"}, "high").outcome == ALLOW
    # 同前缀的另一个命令仍然要问
    assert _decide(policy, "run_shell", {"command": "git push origin dev"}, "high").outcome == ASK


def test_denial_is_remembered_for_the_session() -> None:
    policy = PermissionPolicy()
    policy.forget("run_shell", {"command": "git push origin main"}, SCOPE_ONCE)
    denied = _decide(policy, "run_shell", {"command": "git push origin main"}, "high")
    assert denied.outcome == DENY
    assert "被拒绝" in denied.reason

    policy2 = PermissionPolicy()
    policy2.forget("run_shell", {"command": "git push origin main"}, SCOPE_PREFIX)
    blocked = _decide(policy2, "run_shell", {"command": "git push origin dev"}, "high")
    assert blocked.outcome == DENY
    assert blocked.rule == "prefix:denied"


def test_prefix_rules_from_request_are_honoured() -> None:
    policy = PermissionPolicy.build(MODE_DEFAULT, approved_prefixes=["git push"])
    assert _decide(policy, "run_shell", {"command": "git push origin dev"}, "high").outcome == ALLOW
    assert _decide(policy, "run_shell", {"command": "git push"}, "high").outcome == ALLOW


# --------------------------------------------------------------------------- #
# 3. 网关口径（网关与图中的审批门必须给同一个答案）
# --------------------------------------------------------------------------- #
async def _plan_mode_context(env, root: Path, mode: str, handler):
    project_id = await create_project(env, root)
    task_id = await create_task(env, project_id, ["权限护栏"])
    gateway = ToolGateway(
        [
            ToolDefinition(
                name="run_shell",
                description="d",
                input_schema={"type": "object", "required": ["command"]},
                risk_level="medium",
                handler=handler,
            )
        ]
    )
    context = replace(env.context(task_id, root), permissions=PermissionPolicy(mode=mode))
    return gateway, context, task_id


async def _write_probe(root: Path):
    (root / "ran.txt").write_text("x", encoding="utf-8")
    return ok("ran")


async def test_gateway_denies_in_plan_mode_without_executing(env, workspace: Path):  # noqa: F811
    gateway, context, _ = await _plan_mode_context(
        env, workspace, MODE_PLAN, lambda ctx, args: _write_probe(ctx.root)
    )
    result = await gateway.execute("run_shell", {"command": "echo x"}, context, "execute_action")
    assert result["ok"] is False
    assert result["status"] == "denied"
    assert result["permission"]["rule"] == "mode:plan"
    assert not (workspace / "ran.txt").exists()


async def test_gateway_marks_irreversible_even_in_bypass(env, workspace: Path):  # noqa: F811
    gateway, context, _ = await _plan_mode_context(
        env, workspace, MODE_BYPASS, lambda ctx, args: _write_probe(ctx.root)
    )
    result = await gateway.execute(
        "run_shell", {"command": "rm -rf /tmp/x"}, context, "execute_action"
    )
    # 不可逆命令即便全放行也转为待审批；而且 rm 不是可记忆前缀，只给"本次/该工具"两档
    assert result["status"] == "awaiting_approval"
    assert result["data"]["permission_rule"] == "irreversible"
    assert result["data"]["command_prefix"] == "rm"
    assert result["data"]["suggested_prefix"] == ""
    assert [item["scope"] for item in result["data"]["approval_options"]] == [SCOPE_ONCE, SCOPE_TOOL]
    assert not (workspace / "ran.txt").exists()


# --------------------------------------------------------------------------- #
# 4. 端到端：带作用域的批准在恢复后仍然生效
# --------------------------------------------------------------------------- #
async def _waiting_push_task(env, workspace: Path, mode: str = ""):  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["推送两次分支"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 推送 main\n2. 推送 dev"},
            {
                "content": "推送 main",
                "tool_calls": [call("run_shell", {"command": "git push origin main"})],
            },
            {
                "content": "推送 dev",
                "tool_calls": [call("run_shell", {"command": "git push origin dev"})],
            },
            {"content": "两次推送均已尝试"},
            {"content": "已完成"},
        ]
    )
    payload = {"provider_id": provider_id}
    if mode:
        payload["permission_mode"] = mode
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json=payload)
    assert resp.status_code == 200, resp.text
    return task_id, provider_id, resp.json()


async def test_prefix_scope_approval_survives_the_resume(env, workspace: Path):  # noqa: F811
    task_id, provider_id, body = await _waiting_push_task(env, workspace)
    assert body["task"]["status"] == "waiting_approval"
    approvals = body["evidence"]["pending_approvals"]
    assert len(approvals) == 1
    assert approvals[0]["suggested_prefix"] == "git push"
    assert approvals[0]["permission_mode"] == MODE_DEFAULT
    assert [item["scope"] for item in approvals[0]["approval_options"]] == [
        SCOPE_ONCE,
        SCOPE_PREFIX,
        SCOPE_TOOL,
    ]

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={"approve": True, "provider_id": provider_id, "scope": SCOPE_PREFIX},
    )
    assert resp.status_code == 200, resp.text
    finished = resp.json()
    # 第二段 git push 同前缀：不该再产生新的待审批
    assert finished["task"]["status"] != "waiting_approval", finished["task"]
    assert finished["evidence"]["pending_approvals"] == []

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    granted = [event for event in events if event["type"] == "approval.granted"]
    assert len(granted) == 1, granted
    assert granted[0]["payload"]["scope"] == SCOPE_PREFIX
    assert granted[0]["payload"]["prefix"] == "git push"
    resumed = [event for event in events if event["type"] == "task.resumed"]
    assert len(resumed) == 1
    # 恢复时不重发模式：沿用任务首次运行时记下的那一档
    assert resumed[0]["payload"]["permission_mode"] == MODE_DEFAULT

    # 两条命令都真的走到了执行（git push 在临时目录会失败，这不是断言重点）
    calls = [row for row in await env.tool_calls(task_id) if row.name == "run_shell"]
    commands = [str((row.arguments or {}).get("command") or "") for row in calls]
    assert commands == ["git push origin main", "git push origin dev"], commands

    # 第一条靠"仅本次动作"放行，第二条证明前缀记忆真的生效了
    rules = [
        (event["payload"].get("permission") or {}).get("rule")
        for event in events
        if event["type"] == "tool.completed" and event["payload"].get("tool") == "run_shell"
    ]
    assert rules == ["action:approved", "prefix:git push"], rules


async def test_once_scope_asks_again_for_the_next_command(env, workspace: Path):  # noqa: F811
    task_id, provider_id, body = await _waiting_push_task(env, workspace)
    assert body["task"]["status"] == "waiting_approval"

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={"approve": True, "provider_id": provider_id, "scope": SCOPE_ONCE},
    )
    assert resp.status_code == 200, resp.text
    again = resp.json()
    # 仅本次批准只放行刚才那条命令，第二条 git push origin dev 必须再次打断
    assert again["task"]["status"] == "waiting_approval"
    assert again["evidence"]["pending_approvals"][0]["arguments"]["command"] == "git push origin dev"


async def test_plan_mode_blocks_writes_end_to_end(env, workspace: Path):  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["先只读勘察"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 读取 main.py"},
            {"content": "读取", "tool_calls": [call("read_file", {"path": "main.py"})]},
            {"content": "尝试写入", "tool_calls": [call("run_shell", {"command": "echo x > out.txt"})]},
            {"content": "写入被拒绝"},
            {"content": "已完成勘察"},
        ]
    )
    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run",
        json={"provider_id": provider_id, "permission_mode": MODE_PLAN},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task"]["status"] != "waiting_approval"
    assert not (workspace / "out.txt").exists()

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    started = [event for event in events if event["type"] == "task.started"]
    assert started[0]["payload"]["permission_mode"] == MODE_PLAN
    blocked = [
        event
        for event in events
        if event["type"] == "tool.completed"
        and event["payload"].get("status") == "denied"
        and (event["payload"].get("permission") or {}).get("rule") == "mode:plan"
    ]
    assert blocked, "计划模式必须在网关层拦下写操作"
    assert blocked[0]["payload"]["tool"] == "run_shell"


async def test_unknown_permission_mode_is_rejected(env, workspace: Path):  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["随便"])
    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run",
        json={"provider_id": provider_id, "permission_mode": "god"},
    )
    assert resp.status_code == 422, resp.text


async def test_request_prefix_rule_skips_approval_entirely(env, workspace: Path):  # noqa: F811
    """预先下发前缀规则时，第一次就无需打断——这是"配置好的可信命令集"。"""
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["推送分支"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 推送 main"},
            {"content": "推送", "tool_calls": [call("run_shell", {"command": "git push origin main"})]},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run",
        json={
            "provider_id": provider_id,
            "approved_command_prefixes": ["git push"],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task"]["status"] != "waiting_approval"
    assert body["evidence"]["pending_approvals"] == []
    calls = [row for row in await env.tool_calls(task_id) if row.name == "run_shell"]
    assert [str((row.arguments or {}).get("command") or "") for row in calls] == ["git push origin main"]
    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    done = [event for event in events if event["type"] == "tool.completed"][0]
    assert done["payload"]["permission"]["rule"] == "prefix:git push"


def test_modes_tuple_is_frozen() -> None:
    # 四档口径固定，前端下拉框直接读它
    from blank_api.services.permissions import MODE_LABELS, MODES

    assert MODES == (MODE_DEFAULT, MODE_ACCEPT_EDITS, MODE_PLAN, MODE_BYPASS)
    assert set(MODE_LABELS) == set(MODES)

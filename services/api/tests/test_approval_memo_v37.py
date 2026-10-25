"""#54：批准记忆的粒度与留痕（对标 Codex ``~/.codex/rules/default.rules``）。

实测到的逃逸（``E:/tmp/m74/probe-out.log``）：批准一条 ``python -c "print(1)"``
并选"本会话总是允许"之后，账本里记的是裸 ``python``，于是 ``python evil.py``、
``python -m pip install …``、写 ``id_rsa`` 的内联脚本全部被静默放行。

本文件钉三件事：
1. 解释器类规则必须覆盖到真正执行的那一段（内联正文 / 模块名 / 脚本路径）；
2. 卡片上的"总是允许 X"与账本里记下的规则是同一串，且含密钥的规则不给这一档；
3. 收紧只发生在解释器上，``pwsh`` / ``bash`` / ``rm`` 这些原本就拒绝的宽度没有变化。
"""

from dataclasses import replace
from pathlib import Path

import pytest

from blank_api.services.log_safety import contains_secret, mask_deep
from blank_api.services.permissions import (
    ALLOW,
    ASK,
    MODE_BYPASS,
    SCOPE_ONCE,
    SCOPE_PREFIX,
    SCOPE_TOOL,
    PermissionPolicy,
    approval_options,
    memo_rule,
    memoizable_prefix,
    prefix_rule,
    render_rule,
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

SECRET = "sk-abcdefghijklmnop1234"


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


# --------------------------------------------------------------------------- #
# 1. 规则粒度：解释器必须带到载荷那一段
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python -c 'print(1)'", ("python", "-c", "print(1)")),
        ('python -c "print(1 + 2)"', ("python", "-c", "print(1 + 2)")),
        ("node -e 'console.log(1)'", ("node", "-e", "console.log(1)")),
        ("python -m pytest -q", ("python", "-m", "pytest")),
        ("python -I -u -c 'print(1)'", ("python", "-I", "-u", "-c", "print(1)")),
        ("python -u script.py --x", ("python", "-u", "script.py")),
        (r"C:\Python312\python.exe -c 'print(1)'", ("python", "-c", "print(1)")),
        ("py script.py", ("py", "script.py")),
        # 裸解释器不记：否则等于把任意载荷交给模型自选
        ("python", ()),
        ("node", ()),
        # 原本就拒绝的裸可执行名，宽度不变
        ("pwsh -Command 'Get-ChildItem'", ()),
        ("bash -lc 'ls'", ()),
        ("rm -rf build", ()),
        # 构建/测试类工具与带子命令的普通命令照旧
        ("pytest -q", ("pytest",)),
        ("git push origin main", ("git", "push")),
    ],
    # 节点名显式钉住：命令原文里有空格和引号，默认 id 会随 pytest 版本换形，
    # 变异电池的红集合就没法逐字复核了。断言本身不看这些名字。
    ids=[
        "python-inline", "python-inline-spaces", "node-inline", "python-module",
        "python-flags-before-body", "python-script-args", "python-abs-path", "py-script",
        "bare-python", "bare-node", "pwsh-command", "bash-lc", "rm-rf", "pytest", "git-push",
    ],
)
def test_memo_rule_reaches_the_payload(command: str, expected: tuple) -> None:
    assert memo_rule(command) == expected


def test_interpreter_path_is_normalized_to_basename() -> None:
    # 规则首段与 command_prefix 一样走 basename：换种路径写法也能回读命中
    assert memo_rule(r"C:\Python312\python.exe -c 'print(1)'") == ("python", "-c", "print(1)")
    # 只有旗标、没有载荷的调用不值得记
    assert memo_rule("python -V") == ()


def test_inline_code_body_round_trips_through_render() -> None:
    rule = ("python", "-c", "print(1 + 2)")
    text = render_rule(rule)
    assert text == "python -c 'print(1 + 2)'"
    assert prefix_rule(text) == rule


def test_one_inline_body_does_not_cover_another() -> None:
    policy = PermissionPolicy()
    first = _decide(policy, 'python -c "print(1)"')
    assert first.outcome == ASK
    assert first.memo == "python -c print(1)"

    policy.remember("run_shell", {"command": 'python -c "print(1)"'}, SCOPE_PREFIX)
    again = _decide(policy, "python -c 'print(1)'")
    assert again.outcome == ALLOW
    assert again.rule == "prefix:python -c print(1)"

    escapes = [
        r"""python -c "open(r'C:\Users\me\.ssh\id_rsa','w').write('x')" """.strip(),
        "python evil.py",
        "python -m pip install requests",
        "python -m http.server 8765",
        """node -e "require('child_process')" """.strip(),
        # 整条复合命令的第二段仍要单独覆盖，不能被第一段带进去
        "python -c 'print(1)' && python evil.py",
    ]
    for escape in escapes:
        verdict = _decide(policy, escape)
        assert verdict.outcome == ASK, escape


def test_irreversible_command_never_gets_a_memo() -> None:
    policy = PermissionPolicy()
    verdict = _decide(policy, "git push origin main --force")
    assert verdict.outcome == ASK
    assert verdict.rule == "irreversible"
    assert verdict.memo == ""
    assert [item["scope"] for item in approval_options("run_shell", verdict.memo)] == [
        SCOPE_ONCE,
        SCOPE_TOOL,
    ]
    # 即便前端硬传 scope=prefix，`git push` 也不会被记进账本
    policy.remember("run_shell", {"command": "git push origin main --force"}, SCOPE_PREFIX)
    assert not policy.approved_prefixes
    assert _decide(policy, "git push origin dev").outcome == ASK


# --------------------------------------------------------------------------- #
# 2. 卡片文案与账本规则必须逐字一致
# --------------------------------------------------------------------------- #
def test_card_label_matches_the_ledger_rule() -> None:
    policy = PermissionPolicy()
    decision = _decide(policy, 'python -c "print(1 + 2)"')
    labels = {
        item["scope"]: item["label"]
        for item in approval_options("run_shell", decision.memo)
    }
    assert labels[SCOPE_PREFIX] == "本会话总是允许 python -c 'print(1 + 2)'"
    # 工具档是会话级宽口径，必须把"不可逆仍逐条确认"写在同一张卡上（#70 的裁决）
    tool_option = next(
        item for item in approval_options("run_shell", decision.memo) if item["scope"] == SCOPE_TOOL
    )
    assert "不可逆命令仍逐条确认" in tool_option["warning"]

    policy.remember("run_shell", {"command": 'python -c "print(1 + 2)"'}, SCOPE_PREFIX)
    assert [render_rule(rule) for rule in policy.approved_prefixes] == [
        "python -c 'print(1 + 2)'"
    ]
    assert policy.snapshot()["approved_prefixes"] == ["python -c 'print(1 + 2)'"]


def test_secret_bearing_rule_is_neither_offered_nor_remembered() -> None:
    command = f"""python -c "api_key = '{SECRET}'\""""
    assert contains_secret(command)
    assert memo_rule(command) == ()
    assert memoizable_prefix("run_shell", command) == ""

    policy = PermissionPolicy()
    decision = _decide(policy, command)
    assert decision.outcome == ASK
    assert decision.memo == ""
    assert SCOPE_PREFIX not in [item["scope"] for item in approval_options("run_shell", decision.memo)]

    policy.remember("run_shell", {"command": command}, SCOPE_PREFIX)
    assert not policy.approved_prefixes


def test_event_boundary_masks_a_secret_in_the_suggested_prefix() -> None:
    payload = {"suggested_prefix": f"python -c api_key={SECRET}", "tool": "run_shell"}
    masked = mask_deep(payload)
    assert SECRET not in str(masked)
    assert "****" in masked["suggested_prefix"]


# --------------------------------------------------------------------------- #
# 3. 归因与网关口径
# --------------------------------------------------------------------------- #
def test_approve_high_risk_reports_its_own_rule() -> None:
    flag = PermissionPolicy(approve_high_risk=True)
    assert _decide(flag, "echo hi").rule == "flag:approve_high_risk"
    # 全放行模式仍用自己的归因
    assert _decide(PermissionPolicy(mode=MODE_BYPASS), "echo hi").rule == "mode:bypass"
    # 两种"全放行"都拦不可逆
    assert _decide(flag, "mkfs.ext4 /dev/sda1").rule == "irreversible"


async def _ran(context, arguments):
    return ok("ran")


async def _gateway_context(env, root: Path):  # noqa: F811
    project_id = await create_project(env, root)
    task_id = await create_task(env, project_id, ["权限护栏"])
    gateway = ToolGateway(
        [
            ToolDefinition(
                name="run_shell",
                description="d",
                input_schema={"type": "object", "required": ["command"]},
                risk_level="high",
                handler=_ran,
            )
        ]
    )
    return gateway, replace(env.context(task_id, root), permissions=PermissionPolicy())


async def test_gateway_suggests_the_full_inline_body(env, workspace: Path):  # noqa: F811
    gateway, context = await _gateway_context(env, workspace)
    result = await gateway.execute(
        "run_shell", {"command": 'python -c "print(1)"'}, context, "execute_action"
    )
    assert result["status"] == "awaiting_approval"
    assert result["data"]["command_prefix"] == "python"
    assert result["data"]["suggested_prefix"] == "python -c print(1)"
    assert [item["scope"] for item in result["data"]["approval_options"]] == [
        SCOPE_ONCE,
        SCOPE_PREFIX,
        SCOPE_TOOL,
    ]
    assert result["data"]["approval_options"][1]["label"] == "本会话总是允许 python -c print(1)"


# --------------------------------------------------------------------------- #
# 4. 端到端：记住一段正文，换一段正文必须重新挂卡
# --------------------------------------------------------------------------- #
async def test_second_inline_body_asks_again_end_to_end(env, workspace: Path):  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["跑两段内联脚本"])
    first = 'python -c "print(1)" https://a.example'
    second = "python -c \"open('secrets.txt','w').write('x')\" https://b.example"
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 跑第一段"},
            {"content": "第一段", "tool_calls": [call("run_shell", {"command": first})]},
            {"content": "第二段", "tool_calls": [call("run_shell", {"command": second})]},
            {"content": "两段均已尝试"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task"]["status"] == "waiting_approval"
    pending = body["evidence"]["pending_approvals"]
    assert len(pending) == 1
    assert pending[0]["suggested_prefix"] == "python -c print(1)"

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={"approve": True, "provider_id": provider_id, "scope": SCOPE_PREFIX},
    )
    assert resp.status_code == 200, resp.text
    again = resp.json()
    # 第二段正文不同：前缀记忆不该把它带过去
    assert again["task"]["status"] == "waiting_approval", again["task"]
    assert again["evidence"]["pending_approvals"][0]["arguments"]["command"] == second

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    granted = [event for event in events if event["type"] == "approval.granted"]
    assert len(granted) == 1
    # 留痕说的是账本里真正记下的那条规则，不是命令首段
    assert granted[0]["payload"]["prefix"] == "python -c print(1)"
    assert granted[0]["payload"]["scope"] == SCOPE_PREFIX

    rules = [
        (event["payload"].get("permission") or {}).get("rule")
        for event in events
        if event["type"] == "tool.completed" and event["payload"].get("tool") == "run_shell"
    ]
    assert rules == ["action:approved"], rules

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={"approve": True, "provider_id": provider_id, "scope": SCOPE_ONCE},
    )
    assert resp.status_code == 200, resp.text
    finished = resp.json()
    assert finished["task"]["status"] != "waiting_approval", finished["task"]

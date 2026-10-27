"""#82：``scope=tool`` 免问范围按风险收窄（对标 Codex / Claude Code 的粒度实物）。

实测到的宽放行：高风险的 ``run_shell`` 审批卡上有「本会话总是允许 run_shell」这颗钮，
按下去之后 ``approved_tools`` 记的是 ``(档位, "run_shell", "")``——第三条判据里那条
**换个正文的内联脚本**在同一会话里静默跑掉，一次都不再问。也就是说用户批的是
「这一条命令」，账本上买的却是「这个会话里任意代码执行」。

对照实物（#54 量到的两份）：Codex 的 ``~/.codex/rules/default.rules`` 逐条都是
``prefix_rule``（整段 ``-Command`` 正文都在 pattern 里）；Claude Code 的免问粒度落在
``Bash(git push:*)`` 这种「命令 + 参数形状」上。两边都没有「总是允许整个 shell」这张卡。

这里钉五件事，每件都能翻红：
1. ``run_shell`` 的高风险卡只给「本次 / 这一条命令」两档（卡上没有按不下去的钮）；
2. 风险被路径框住的工具（带 ``path`` 的 ``apply_patch``）仍然给工具档，文案说清免问边界；
3. 给不起工具档的批准即使硬传 ``scope=tool`` 也不入账：同一条命令靠 ``approval_key``
   免问，换一条高风险命令必须重新挂卡；
4. 免问粒度按「登记制」对账：默认工具注册表里每个带 ``risk_for`` 的工具都必须在
   :data:`ARGUMENT_SCOPED_TOOLS` 里，漏登记＝静默放宽；
5. 端到端（真 HTTP + 图中断）：第一段用 ``scope=tool`` 批准后，任务必须再次
   ``waiting_approval``，且 ``approval.granted`` 如实记下 ``tool_grant_recorded == False``。

诚实缺口（不在这里偷偷解决，写在文档 §7.44）：不带 ``risk_for`` 的工具（含 MCP 桥接
工具，风险由远端 server 静态声明）仍保留工具档；运行请求里的 ``approved_tools`` 种子
仍按操作者声明兑现——那是与 ``permission_mode=bypass`` 同级的信任面，见第 6 条。
"""

from dataclasses import replace
from pathlib import Path

import pytest

from blank_api.services.permissions import (
    ALLOW,
    ARGUMENT_SCOPED_TOOLS,
    ASK,
    MODE_DEFAULT,
    SCOPE_ONCE,
    SCOPE_PREFIX,
    SCOPE_TOOL,
    PermissionPolicy,
    approval_options,
    path_scope,
    tool_grant_too_wide,
)
from blank_api.services.sandbox.tiers import TIER_WORKSPACE
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolDefinition
from blank_api.services.tools.gateway import ToolGateway
from blank_api.services.tools.patch import patch_tools
from blank_api.services.tools.shell import assess_risk

from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)

SECRET_PATCH = "@@ -0,0 +1,1 @@\n+sk-abcdefghijklmnop123456"
FIRST = 'python -c "print(1)" https://a.example'
SECOND = 'python -c "print(2)" https://a.example'


def _shell_tool() -> ToolDefinition:
    """带 ``risk_for`` 的 run_shell：风险由命令正文算出来，不是静态声明。"""

    async def handler(context, arguments):  # pragma: no cover - 只用于构造定义
        raise AssertionError("handler should not be called")

    return ToolDefinition(
        name="run_shell",
        description="d",
        input_schema={"type": "object", "required": ["command"]},
        risk_level="medium",
        risk_for=lambda arguments: assess_risk(str(arguments.get("command", ""))),
        handler=handler,
    )


def _policy(tier: str = TIER_WORKSPACE) -> PermissionPolicy:
    return PermissionPolicy.build(MODE_DEFAULT, sandbox_tier=tier)


def _scopes(options: list[dict]) -> list[str]:
    return [str(item.get("scope") or "") for item in options]


async def _gateway(env, root: Path, tool: ToolDefinition) -> tuple[ToolGateway, object]:
    project_id = await create_project(env, root)
    task_id = await create_task(env, project_id, ["工具档收窄"])
    context = replace(
        env.context(task_id, root),
        permissions=PermissionPolicy.build(MODE_DEFAULT, sandbox_tier=TIER_WORKSPACE),
    )
    return ToolGateway([tool]), context


# --------------------------------------------------------------------------- #
# 1. 卡上只出现按得下去的钮
# --------------------------------------------------------------------------- #
async def test_shell_card_does_not_offer_the_tool_scope(env, workspace: Path):  # noqa: F811
    tool = _shell_tool()
    gateway, context = await _gateway(env, workspace, tool)
    result = await gateway.execute("run_shell", {"command": FIRST}, context, "execute_action")
    assert result["status"] == "awaiting_approval", result
    options = result["data"]["approval_options"]
    # 「总是允许 run_shell」在这个工具上等于放行整个会话的任意代码执行——卡上不给。
    assert _scopes(options) == [SCOPE_ONCE, SCOPE_PREFIX]
    assert SCOPE_TOOL not in _scopes(options)
    assert result["data"]["suggested_prefix"] == "python -c print(1) https://a.example"


async def test_path_scoped_patch_card_keeps_the_tool_scope(env, workspace: Path):  # noqa: F811
    tool = patch_tools()[0]
    gateway, context = await _gateway(env, workspace, tool)
    arguments = {"path": "config/.env", "patch": SECRET_PATCH}
    result = await gateway.execute("apply_patch", arguments, context, "execute_action")
    assert result["status"] == "awaiting_approval", result
    # 参数被路径框住了，工具档说的是「本会话、这一片路径」，这句才说得出口。
    assert _scopes(result["data"]["approval_options"]) == [SCOPE_ONCE, SCOPE_TOOL]
    tool_option = next(
        item for item in result["data"]["approval_options"] if item["scope"] == SCOPE_TOOL
    )
    assert "换路径" in tool_option["warning"]
    assert "不可逆" in tool_option["warning"]


def test_options_follow_the_scope_flag_not_the_tool_name():
    """``approval_options`` 只认调用方算好的粒度，不自己认工具名——两处发卡才可能同源。"""
    assert _scopes(approval_options("apply_patch", "", tool_scope=False)) == [SCOPE_ONCE]
    assert _scopes(approval_options("apply_patch", "", tool_scope=True)) == [
        SCOPE_ONCE,
        SCOPE_TOOL,
    ]
    # 没有可记忆规则（非 run_shell）时，前缀档本来就不出现，少一档不是这里的收窄造成的。
    assert _scopes(
        approval_options("web_fetch", "", tool_scope=not tool_grant_too_wide("web_fetch", {}))
    ) == [SCOPE_ONCE, SCOPE_TOOL]


def test_tool_scope_has_no_default():
    """#88：`tool_scope` 是必填关键字——默认值一旦是"开"，漏传就自动长出工具档。

    那正是本批闭掉的 fail-open：发卡点漏传时界面上会出现一颗按下去不留账的钮
    （写入侧 :meth:`PermissionPolicy.remember` 照样拒记），而不是当场报错。
    现在两个发卡点（图与网关）都必须显式表态，漏一个就是 ``TypeError``。
    """
    with pytest.raises(TypeError):
        approval_options("apply_patch", "")
    # 位置参数也不行：它是 keyword-only，逼调用点写清楚"这一档给不给"
    with pytest.raises(TypeError):
        approval_options("apply_patch", "", True)


# --------------------------------------------------------------------------- #
# 2. 写入侧：给不起的工具档不入账
# --------------------------------------------------------------------------- #
def test_shell_tool_grant_is_not_recorded_but_the_command_is():
    policy = _policy(TIER_WORKSPACE)
    tool = _shell_tool()
    policy.remember("run_shell", {"command": FIRST}, SCOPE_TOOL)
    # 免问范围不许由一个客户端参数放大：账本上必须什么都没有。
    assert policy.approved_tools == set()
    assert policy.snapshot()["approved_tools"] == []
    # 同一条命令仍然免问——靠的是这次批准本来就说好了的那件事（approval_key）。
    again = policy.decide(tool, {"command": FIRST})
    assert again.outcome == ALLOW and again.rule == "action:approved"
    # 换个正文的内联脚本必须重新问，这才是"整条 shell 没有免问"的可核对形状。
    other = policy.decide(tool, {"command": SECOND})
    assert other.outcome == ASK, "一次工具档批准覆盖了两条不同的内联脚本"


def test_patch_grant_without_explicit_path_is_not_recorded():
    """``apply_patch`` 的 ``path`` 可以省略（从补丁头推断）——那一次不给宽档。

    同一张卡、同一个按钮，如果按推断出来的路径入账，免问范围就从「这个文件」
    变成「以后所有文件」；这里按参数本身判，路径没写在参数里就等于框不住。
    """
    tool = {item.name: item for item in build_default_tools()}["apply_patch"]
    inferred = {"patch": f"+++ b/src/new.py\n{SECRET_PATCH}"}
    assert path_scope(inferred) == ""
    assert tool_grant_too_wide("apply_patch", inferred) is True

    policy = _policy(TIER_WORKSPACE)
    policy.remember("apply_patch", inferred, SCOPE_TOOL)
    assert policy.approved_tools == set()
    other = policy.decide(tool, {"path": "src/other.py", "patch": SECRET_PATCH})
    assert other.outcome == ASK


def test_grant_for_a_tool_without_argument_risk_is_still_recorded():
    """登记制之外的工具（风险不由参数算出来）仍按声明兑现宽档：收窄不是全局加严。"""
    assert tool_grant_too_wide("read_file", {}) is False
    policy = _policy(TIER_WORKSPACE)
    policy.remember("read_file", {"path": "src/a.py"}, SCOPE_TOOL)
    assert ("workspace", "read_file", "src/a.py") in policy.approved_tools


async def test_a_stray_path_does_not_buy_a_shell_grant(env, workspace: Path):  # noqa: F811
    """模型多发的 `path` 不能把 shell 卡换成宽档——卡上没这颗钮，账上也不记。

    `validate_arguments` 不拒未知键，所以「command + path」的 run_shell 调用是可达的；
    而 path 对命令正文毫无影响，用它分桶等于批一次之后，模型重复那个 path 就能让
    任意命令正文免问。这条判据量的就是这个形状。
    """
    arguments = {"command": FIRST, "path": "src/a.py"}
    gateway, context = await _gateway(env, workspace, _shell_tool())
    result = await gateway.execute("run_shell", arguments, context, "execute_action")
    assert result["status"] == "awaiting_approval", result
    assert SCOPE_TOOL not in _scopes(result["data"]["approval_options"])

    policy = _policy(TIER_WORKSPACE)
    policy.remember("run_shell", arguments, SCOPE_TOOL)
    assert policy.approved_tools == set()
    followup = policy.decide(_shell_tool(), {"command": SECOND, "path": "src/a.py"})
    assert followup.outcome == ASK, "重复同一个 path 就免问了任意命令正文"


# --------------------------------------------------------------------------- #
# 3. 登记表与真实注册表逐条对账（漏登记＝静默放宽）
# --------------------------------------------------------------------------- #
def test_every_argument_risk_tool_is_registered():
    risk_for_tools = {tool.name for tool in build_default_tools() if tool.risk_for is not None}
    assert risk_for_tools <= ARGUMENT_SCOPED_TOOLS, (
        f"这些工具的风险由参数算出来却没登记，工具档会照旧放行整个工具：{sorted(risk_for_tools - ARGUMENT_SCOPED_TOOLS)}"
    )
    # 反向也要成立：登记了一个没有 risk_for 的工具，等于凭空少一档用户看得见的按钮。
    assert ARGUMENT_SCOPED_TOOLS <= risk_for_tools, sorted(ARGUMENT_SCOPED_TOOLS - risk_for_tools)


def test_operator_seed_is_still_honoured():
    """运行请求里的 ``approved_tools`` 是操作者声明，不被这次的写入侧闸门收回。

    这条不是"放宽"的借口，而是将信任面写清楚：它和 ``permission_mode=bypass`` 同级，
    审批接口本身不收 ``approved_tools``（见 test_approval_v06.py:64/65）。
    """
    policy = PermissionPolicy.build(
        MODE_DEFAULT, approved_tools={"run_shell"}, sandbox_tier=TIER_WORKSPACE
    )
    decision = policy.decide(_shell_tool(), {"command": FIRST})
    assert decision.outcome == ALLOW
    assert decision.rule == f"tool:{TIER_WORKSPACE}:run_shell"


# --------------------------------------------------------------------------- #
# 4. 端到端：老客户端硬传 scope=tool，任务必须再挂一次卡
# --------------------------------------------------------------------------- #
async def test_tool_scope_approval_still_asks_for_the_next_command(env, workspace: Path):  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["跑两段内联脚本"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 跑第一段\n2. 跑第二段"},
            {"content": "第一段", "tool_calls": [call("run_shell", {"command": FIRST})]},
            {"content": "第二段", "tool_calls": [call("run_shell", {"command": SECOND})]},
            {"content": "两段均已尝试"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task"]["status"] == "waiting_approval"
    pending = body["evidence"]["pending_approvals"]
    assert len(pending) == 1
    assert _scopes(pending[0]["approval_options"]) == [SCOPE_ONCE, SCOPE_PREFIX]

    # 旧版界面/安装包仍会传 scope=tool：卡上没有这颗钮，接口也不为它入账。
    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={"approve": True, "provider_id": provider_id, "scope": SCOPE_TOOL},
    )
    assert resp.status_code == 200, resp.text
    again = resp.json()
    assert again["task"]["status"] == "waiting_approval", again["task"]
    assert again["evidence"]["pending_approvals"][0]["arguments"]["command"] == SECOND

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    granted = [event for event in events if event["type"] == "approval.granted"]
    assert len(granted) == 1
    assert granted[0]["payload"]["scope"] == SCOPE_TOOL
    # 留痕必须说实话：这条决定声称放行整个工具，账本上其实什么都没记。
    assert granted[0]["payload"]["tool_grant_recorded"] is False

    snapshots = [
        (event["payload"].get("permissions") or {}).get("approved_tools") or []
        for event in events
        if event["type"] == "turn.context"
    ]
    assert snapshots, "没有权限快照事件，第 4 条判定就成了空断言"
    for tools in snapshots:
        assert not [item for item in tools if str(item).endswith(":run_shell:.")], tools

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={"approve": True, "provider_id": provider_id, "scope": SCOPE_ONCE},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] != "waiting_approval"


@pytest.mark.parametrize(
    ("tool_name", "arguments", "too_wide"),
    [
        ("run_shell", {"command": "pytest -q"}, True),
        ("run_shell", {"command": "pytest -q", "path": "src/a.py"}, True),
        ("git", {"command": "git push origin main"}, True),
        ("spawn_agents", {"agents": []}, True),
        ("apply_patch", {"path": "src/a.py", "patch": "@@ x"}, False),
        ("web_fetch", {"url": "https://a.example"}, False),
    ],
)
def test_grant_width_table(tool_name: str, arguments: dict, too_wide: bool):
    """粒度表逐格给定值：带命令的调用一律不给工具档，路径类工具按路径框住。"""
    assert tool_grant_too_wide(tool_name, arguments) is too_wide

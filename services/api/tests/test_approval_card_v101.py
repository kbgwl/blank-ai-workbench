"""#101：把「审批卡文案 ↔ `tool_scope` ↔ 免问账本」这条链路钉成能失败的仓库用例。

三件事，每件都只在**实测读数**上写判据（读数记在本文件末尾的「实测记录」段里）：

1. **PowerShell 形状必须挂卡**（第 1 节）：`powershell -Command "… | Invoke-Expression"`、
   `pwsh -EncodedCommand`、反引号 / `%` / 嵌套引号这些形状，既不能被前缀免问规则静默放行，
   也不能在卡片上长出「总是允许」档位之后把免问范围扩到别的载荷上。分词与风险判定的实现读自
   :mod:`blank_api.services.permissions`（`split_segments` / `tokenize` / `memo_rule` /
   `_starts_with`）与 :func:`blank_api.services.tools.shell.assess_risk`。
2. **卡片文案与账本逐字一致**（第 2 节）：卡上的 `suggested_prefix`、`approval_options` 的
   `label`/`warning`，与批准落盘后从 `approvals.json` 读回的那条规则串**相等**（不是包含），
   两种命中形状 `prefix:{hit}`（会话档，permissions.py:753-755）与
   `prefix:persistent:{durable}`（落盘档，permissions.py:758-760）都要对上。
3. **缺 `tool_scope` 必须当场 `TypeError`**（第 3 节）：必填关键字（permissions.py:362，#88）
   在两个真实发卡点（`agent/graph.py:1623`、`tools/gateway.py:411`）都显式传了——
   运行时对账 + 源码静态对账，新增发卡点漏传就是红的，而不是界面上多出一颗不留账的钮。

**第 1 节末尾那两条原本是红的**（裸解释器规则 / 手写 `-EncodedCommand` 规则静默免问），
本轮已按它们补上产品侧闸门：`permissions._rule_admissible` 排在匹配之前，
`covered_by_rules` 只对免问那一路收紧，驳回那一路由 `test_the_new_gate_does_not_blunt_a_refusal`
钉住方向。
"""

import ast
import json
from pathlib import Path

import pytest

from blank_api.services import approval_rules
from blank_api.services.permissions import (
    ALLOW,
    ASK,
    DENY,
    SCOPE_ONCE,
    SCOPE_PREFIX,
    SCOPE_TOOL,
    PermissionPolicy,
    approval_options,
    covered_by_rules,
    memo_rule,
    memoizable_prefix,
    prefix_rule,
    render_rule,
)
from blank_api.services.tools.base import ToolDefinition
from blank_api.services.tools.gateway import ToolGateway
from blank_api.services.tools.shell import assess_risk

from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "blank_api"

# 前缀档的固定文案（permissions.py:384）——逐字比对用，不写"包含"。
PREFIX_WARNING = "只免问这一条命令：换正文、换参数、换模块或换脚本路径都会重新挂卡"
TOOL_WARNING = "该工具在本档位、这片路径内不再询问；换档位、换路径或不可逆命令仍逐条确认"

# --- PowerShell / cmd 形状（逐条见文末实测记录） -------------------------------- #
PS_PLAIN = 'powershell -Command "Get-ChildItem"'
PS_PLAIN_FLIPPED = "powershell -Command 'Get-ChildItem'"
PS_IEX = 'powershell -Command "Get-ChildItem | Invoke-Expression"'
PS_IEX_FLIPPED = "powershell -Command 'Get-ChildItem | Invoke-Expression'"
PS_FILE_IEX = 'powershell -NoProfile -Command "iex (Get-Content setup.ps1)"'
PS_PIPE_OUTSIDE = 'powershell -Command "Get-ChildItem" | Invoke-Expression'
PS_PCT = 'powershell -Command "Write-Host %TMP%"'
CMD_COMSPEC = 'cmd /c "%COMSPEC% /c echo x"'
PS_STRAY_QUOTE = 'powershell -Command "Get-Process|Stop-Process "k"'
PS_STRAY_SIBLING = 'powershell -Command "Get-Process|Stop-Process "k'
PS_BACKTICK = 'pwsh -Command "Write-Host `"$PWD`""'
PS_DOUBLED = 'powershell -Command "Write-Host ""danger"""'
PS_SINGLES = """powershell -Command "Write-Host ''x''\""""
PS_NESTED_ESC = 'powershell -Command "powershell -Command \\"Get-ChildItem\\""'
ENC_PWSH = "pwsh -EncodedCommand JAB4AD0AMQA="
ENC_PS_SHORT = "powershell -enc SGVsbG8="
ENC_PWSH_SHORT = "pwsh -enc ABC"

PY_TAIL = 'python -c "print(1)" https://a.example'
PY_TAIL_FLIPPED = "python -c 'print(1)' https://a.example"
PY_CARD = "python -c print(1) https://a.example"


def _shell_tool() -> ToolDefinition:
    """带 ``risk_for`` 的 run_shell：风险由命令正文算出来，与产品工具定义同一条路径。"""

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


def _policy(tier: str = "workspace") -> PermissionPolicy:
    return PermissionPolicy.build("default", sandbox_tier=tier)


def _decide(policy: PermissionPolicy, command: str):
    """不给 risk 参：让 ``decide`` 走 ``tool.resolve_risk`` = assess_risk，与网关同一条判定。"""
    return policy.decide(_shell_tool(), {"command": command})


def _scopes(options: list[dict]) -> list[str]:
    return [str(item.get("scope") or "") for item in options]


async def _gateway_card(env, root: Path, command: str) -> dict:
    """真网关发卡（``tools/gateway.py`` 那条路径），返回 ``awaiting_approval`` 的 data。"""
    from dataclasses import replace

    from blank_api.services.tools import build_default_tools

    tool = next(item for item in build_default_tools() if item.name == "run_shell")
    context = replace(
        env.context("t101", root),
        permissions=_policy(),
    )
    result = await ToolGateway([tool]).execute("run_shell", {"command": command}, context, "execute_action")
    assert result["status"] == "awaiting_approval", result
    return result["data"]


# --------------------------------------------------------------------------- #
# 1. PowerShell 形状：风险判定要挂卡，且卡上档位不得扩到别的载荷
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "command",
    [
        PS_IEX,
        PS_FILE_IEX,
        PS_PIPE_OUTSIDE,
        PS_PCT,
        CMD_COMSPEC,
        PS_STRAY_QUOTE,
        PS_BACKTICK,
        PS_DOUBLED,
        PS_SINGLES,
        PS_NESTED_ESC,
        ENC_PWSH,
        ENC_PS_SHORT,
        ENC_PWSH_SHORT,
    ],
    ids=[
        "ps-command-pipe-iex",
        "ps-command-iex-file",
        "ps-pipe-outside-quotes",
        "ps-percent-var",
        "cmd-comspec-percent",
        "ps-stray-quote",
        "ps-backtick-escaped-quote",
        "ps-doubled-quote",
        "ps-doubled-single-quote",
        "ps-nested-escaped-quote",
        "pwsh-encodedcommand",
        "powershell-enc",
        "pwsh-enc",
    ],
)
def test_powershell_shapes_are_high_risk_and_ask(command: str) -> None:
    """这些形状在空规则的策略上必须挂卡：``assess_risk`` 说 high，``decide`` 说 ask。"""
    assert assess_risk(command) == "high", command
    verdict = _decide(_policy(), command)
    assert verdict.outcome == ASK, verdict.as_dict()
    assert verdict.rule == "risk:ask", verdict.as_dict()


@pytest.mark.parametrize(
    "command",
    [ENC_PWSH, ENC_PS_SHORT, ENC_PWSH_SHORT, PS_BACKTICK, PS_DOUBLED, PS_SINGLES, PS_NESTED_ESC],
    ids=[
        "pwsh-encodedcommand",
        "powershell-enc",
        "pwsh-enc",
        "ps-backtick-escaped-quote",
        "ps-doubled-quote",
        "ps-doubled-single-quote",
        "ps-nested-escaped-quote",
    ],
)
async def test_opaque_or_lossy_payloads_get_no_prefix_tier(command: str, env, workspace: Path) -> None:  # noqa: F811
    """乱码载荷（base64）与读不回来的引号形状：卡上只有「仅本次允许」，一条规则都不记。

    依据是仓库成文契约：permissions.py:444-446（``-EncodedCommand`` 的载荷用户看不见，
    「和含密钥的规则一样，宁可不记」）与 permissions.py:472-476（``""`` / ``\\"`` 这类
    转义只剥一层会把载荷切残，切残的规则比原命令更宽，permissions.py:472-476）。
    """
    assert memo_rule(command) == (), command
    assert memoizable_prefix("run_shell", command) == "", command
    card = await _gateway_card(env, workspace, command)
    assert card["suggested_prefix"] == ""
    assert _scopes(card["approval_options"]) == [SCOPE_ONCE]


def test_memorized_powershell_command_does_not_exempt_other_payloads() -> None:
    """批准 ``powershell -Command "Get-ChildItem"`` 只免问这一条：换载荷/换壳/换旗标都要重问。

    两种命中形状一起对账：会话档（``approved_prefixes`` 走 ``remember``）与落盘档
    （``approvals.json`` 读回 ``persistent_prefixes``）。载荷型规则靠
    :func:`_starts_with` 的等长约束（permissions.py:254-257）收紧，所以「换正文」
    和「多一个 token」都免问不了。
    """
    rule = memo_rule(PS_PLAIN)
    assert rule == ("powershell", "-Command", "Get-ChildItem")
    card = memoizable_prefix("run_shell", PS_PLAIN)
    assert card == "powershell -Command Get-ChildItem"

    session = _policy()
    session.remember("run_shell", {"command": PS_PLAIN}, SCOPE_PREFIX)
    assert session.snapshot()["approved_prefixes"] == [card]

    durable = _policy()
    assert approval_rules.add_rule("run_shell", rule) is True
    durable.persistent_prefixes = approval_rules.load_prefix_rules()

    escapes = [PS_IEX, PS_FILE_IEX, PS_PIPE_OUTSIDE, PS_PCT, CMD_COMSPEC, PS_STRAY_QUOTE, ENC_PWSH, PS_BACKTICK, 'pwsh -Command "Get-ChildItem"']
    for command in escapes:
        assert covered_by_rules(command, [rule]) is None, f"{PS_PLAIN} 免问了 {command}"
        for policy, shape in ((session, "会话档"), (durable, "落盘档")):
            verdict = _decide(policy, command)
            assert verdict.outcome == ASK, f"{shape}：{PS_PLAIN} 免问了 {command} -> {verdict.as_dict()}"
            assert not verdict.rule.startswith("prefix:"), verdict.as_dict()

    # 对照格（必须真的能免问，否则上面的"不免问"就是空断言）
    assert _decide(session, PS_PLAIN_FLIPPED).outcome == ALLOW
    assert _decide(durable, PS_PLAIN_FLIPPED).outcome == ALLOW


def test_pipe_into_invoke_expression_never_learns_the_second_segment() -> None:
    """引号外的 ``| Invoke-Expression`` 是第二段：批准一次前缀档之后，同一条复合命令照样挂卡。

    ``memo_rule`` 只取首段（permissions.py:467-470 的分词），而 ``covered_by_rules``
    要求逐段命中（permissions.py:300-302），所以账本上永远没有一条能单独免问
    ``Invoke-Expression`` 这一段的规则。
    """
    policy = _policy()
    policy.remember("run_shell", {"command": PS_PIPE_OUTSIDE}, SCOPE_PREFIX)
    assert policy.snapshot()["approved_prefixes"] == ["powershell -Command Get-ChildItem"]
    # 逐字同一条命令靠 ``action:approved`` 免问（#82 的既有语义，test_tool_grant_scope_v52.py:167-169），
    # 不是靠一条覆盖了 ``Invoke-Expression`` 的规则——换个引号写法它就又挂卡。
    assert _decide(policy, PS_PIPE_OUTSIDE).rule == "action:approved"
    respelled = "powershell -Command 'Get-ChildItem' | Invoke-Expression"
    verdict = _decide(policy, respelled)
    assert verdict.outcome == ASK, verdict.as_dict()
    assert verdict.rule == "risk:ask", verdict.as_dict()

    # 第二段真被单独批过一次也不行：两段必须各自有规则才叫命中
    other = _policy()
    other.approved_prefixes = {("powershell", "-Command", "Get-ChildItem")}
    assert covered_by_rules(respelled, other.approved_prefixes) is None
    assert covered_by_rules(PS_PIPE_OUTSIDE, other.approved_prefixes) is None


def test_stray_quote_shape_rule_only_covers_itself() -> None:
    """嵌套/悬空引号：能记下来的那条只覆盖它自己，兄弟写法（少一个引号）不免问。

    ``_LOSSY_QUOTES``（permissions.py:448）挡掉的是相邻双引号那一类；
    剩下的悬空引号形状记下来的是**残 token 序列**，等长约束保证它盖不到别的写法。
    """
    rule = memo_rule(PS_STRAY_QUOTE)
    assert rule == ("powershell", "-Command", "Get-Process|Stop-Process ", 'k"'), rule
    card = memoizable_prefix("run_shell", PS_STRAY_QUOTE)
    assert prefix_rule(card) == rule
    assert covered_by_rules(PS_STRAY_SIBLING, [rule]) is None
    assert covered_by_rules(PS_PLAIN, [rule]) is None
    assert covered_by_rules(PS_STRAY_QUOTE, [rule]) == card


# --------------------------------------------------------------------------- #
# 1b. 匹配侧闸门：不合规的规则（裸解释器名 / opaque 载荷）配不上免问
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("tokens", "command"),
    [
        (("powershell",), PS_IEX),
        (("powershell",), PS_FILE_IEX),
        (("pwsh",), ENC_PWSH),
        (("python",), 'python -c "import os;os.system(\'calc\')"'),
        (("cmd",), CMD_COMSPEC),
    ],
    ids=["powershell-iex", "powershell-iex-file", "pwsh-encoded", "python-inline", "cmd-c"],
)
def test_bare_executor_rule_must_not_exempt_code(tokens: tuple, command: str) -> None:
    """单个可执行名的规则兑现不了「这一条命令」：它免问的是解释器收到的任意载荷。

    契约在仓库里已成文：permissions.py:428-432「记住一个裸名等于把整个会话的任意代码执行
    交给模型自选载荷，所以这类命令必须带到真正被执行的那一段」，:478-500 在**写入侧**照此
    拒了裸解释器；:255-258 与 :268-270 的 ``_payload_terminated`` 也要求载荷型规则整段等长——但它的
    前置条件是 ``len(rule) >= 2``，所以一条首段是 ``powershell`` / ``python`` 的**单 token
    规则**绕过这两道，落到普通的「以它开头」匹配上。
    ``approvals.json`` 按文档是「给人看得懂、也能手工编辑的」（approval_rules.py:25-26），
    读侧 ``_normalize``（:39-47）只查工具名与非空，``add_rule`` 也照收——
    于是文件里一行 ``["powershell"]`` 就让 ``-Command`` 的任意正文静默放行，
    卡片一张不发。这与 :87 的不可逆闸门是同一件事：闸门必须排在匹配之前，
    见 test_approval_persist_v43.py:218（手改的不可逆规则照样挂卡）与
    test_shell_quoting_v53.py:198（前缀规则不得越过不可逆闸）。
    """
    assert covered_by_rules(command, [tokens]) is None, f"{tokens} 免问了 {command}"

    session = _policy()
    session.approved_prefixes = {tokens}
    durable = _policy()
    durable.persistent_prefixes = {tokens}
    for policy, shape in ((session, f"prefix:{render_rule(tokens)}"), (durable, f"prefix:persistent:{render_rule(tokens)}")):
        verdict = _decide(policy, command)
        assert verdict.outcome == ASK, f"会话档/落盘档形状 {shape} 静默放行了 {command}: {verdict.as_dict()}"
        assert not verdict.rule.startswith("prefix:"), verdict.as_dict()


def test_hand_edited_opaque_rule_must_not_exempt_base64_payload() -> None:
    """``-EncodedCommand`` 的规则不该从文件里兑现：用户看不见的那串不是他能批准的东西。

    写入侧 permissions.py:491-494 遇到 ``_OPAQUE_CODE_FLAGS`` 直接返回空规则（「卡片上它是
    一串乱码，记进免问规则等于让他盲签」）。读侧与匹配侧没有同一道闸：手写的
    ``["pwsh","-EncodedCommand","…"]`` 整段等长命中，之后同一条 base64 永久静默放行。
    既有契约只写在写入侧，这一条是按同一条口径把闸门补到匹配侧（本轮已落产品码）：
    文件是手改的，写入闸门管不到它，只在写入侧设闸等于对着一扇没人走的门上锁。
    """
    encoded = ("pwsh", "-EncodedCommand", "JAB4AD0AMQA=")
    assert approval_rules.add_rule("run_shell", encoded) is True
    policy = _policy()
    policy.persistent_prefixes = approval_rules.load_prefix_rules()
    verdict = _decide(policy, ENC_PWSH)
    assert verdict.outcome == ASK, verdict.as_dict()
    assert not verdict.rule.startswith("prefix:"), verdict.as_dict()


def test_the_new_gate_does_not_blunt_a_refusal() -> None:
    """匹配侧那道闸门（`covered_by_rules` 的 `refuse_inadmissible`）只许收紧"免问"。

    驳回那一路必须照旧吃得住：用户在本会话里明说"powershell 别再造"之后，一条不配免问的
    裸前缀规则仍要拦住它。少了这一格，新闸门到底是"只关批准的缝"还是"把驳回也一起撤了"
    就分不清——后者是反方向的 fail-open（一次驳回被悄悄作废）。
    """
    bare = ("powershell",)
    denied = _policy()
    denied.denied_prefixes = {bare}
    verdict = _decide(denied, PS_IEX)
    assert verdict.outcome == DENY, verdict.as_dict()
    assert verdict.rule == "prefix:denied", verdict.as_dict()

    granted = _policy()
    granted.approved_prefixes = {bare}
    assert _decide(granted, PS_IEX).outcome == ASK, "同一串规则在批准那一路该失效，这一格钉方向"


# --------------------------------------------------------------------------- #
# 2. 卡片文案 ↔ 账本：逐字相等，会话档与落盘档两种命中形状都要对上# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("command", "flipped", "card"),
    [
        (PS_PLAIN, PS_PLAIN_FLIPPED, "powershell -Command Get-ChildItem"),
        (PS_IEX, PS_IEX_FLIPPED, 'powershell -Command "Get-ChildItem | Invoke-Expression"'),
        (PY_TAIL, PY_TAIL_FLIPPED, PY_CARD),
    ],
    ids=["ps-plain", "ps-iex", "python-inline-with-tail"],
)
def test_card_text_equals_ledger_rule(command: str, flipped: str, card: str) -> None:
    """同一条规则在四个位置必须一字不差：卡面 X、按钮 label、会话档命中串、落盘档命中串。

    ``flipped`` 是换个引号写法的同一条命令（``approval_key`` 不同，所以走不到
    ``action:approved`` 那一层，只能靠前缀规则命中）——两种命中形状的归因串都得等于卡面。
    """
    assert memoizable_prefix("run_shell", command) == card, command
    assert render_rule(memo_rule(command)) == card
    assert prefix_rule(card) == memo_rule(command)

    options = approval_options("run_shell", card, tool_scope=False)
    assert _scopes(options) == [SCOPE_ONCE, SCOPE_PREFIX]
    assert options[0]["label"] == "仅本次允许"
    assert options[1]["label"] == f"本会话总是允许 {card}"
    assert options[1]["warning"] == PREFIX_WARNING

    policy = _policy()
    policy.remember("run_shell", {"command": command}, SCOPE_PREFIX)
    assert policy.snapshot()["approved_prefixes"] == [card]
    session_verdict = _decide(policy, flipped)
    assert session_verdict.outcome == ALLOW, session_verdict.as_dict()
    assert session_verdict.rule == f"prefix:{card}", session_verdict.as_dict()

    assert approval_rules.add_rule("run_shell", memo_rule(command), source_task="t101") is True
    raw = json.loads(approval_rules.path().read_text(encoding="utf-8"))["rules"]
    assert raw[0]["tokens"] == list(memo_rule(command)), raw
    assert raw[0]["source_task"] == "t101"
    # 落盘的 token 数组重新渲染出来，必须还是卡面那一串
    assert render_rule(tuple(raw[0]["tokens"])) == card

    durable = _policy()
    durable.persistent_prefixes = approval_rules.load_prefix_rules()
    assert durable.snapshot()["persistent_prefixes"] == [card]
    durable_verdict = _decide(durable, flipped)
    assert durable_verdict.outcome == ALLOW, durable_verdict.as_dict()
    assert durable_verdict.rule == f"prefix:persistent:{card}", durable_verdict.as_dict()


async def test_gateway_card_matches_the_policy_decision(env, workspace: Path) -> None:  # noqa: F811
    """网关卡（``tools/gateway.py:411``）与 :meth:`PermissionPolicy.decide` 的 ``memo`` 同源。

    两处发卡若各算各的，「卡上写的」与「账上记的」就会分叉，所以这里拿同一条命令把
    两侧读数并排一次：``suggested_prefix`` == ``Decision.memo``，档位列表 == ``approval_options``。
    """
    command = PS_IEX
    card = await _gateway_card(env, workspace, command)
    verdict = _decide(_policy(), command)
    assert card["suggested_prefix"] == verdict.memo == 'powershell -Command "Get-ChildItem | Invoke-Expression"'
    assert card["command_prefix"] == verdict.prefix == "powershell"
    assert card["approval_options"] == approval_options("run_shell", verdict.memo, tool_scope=False)
    assert _scopes(card["approval_options"]) == [SCOPE_ONCE, SCOPE_PREFIX]
    assert card["approval_options"][1]["label"] == f"本会话总是允许 {card['suggested_prefix']}"
    assert card["arguments"]["command"] == command

    # 卡上说的这一条，批准之后账上就是这一条，且换个引号写法免问的是同一件事
    policy = _policy()
    policy.remember("run_shell", {"command": command}, SCOPE_PREFIX)
    assert policy.snapshot()["approved_prefixes"] == [card["suggested_prefix"]]
    assert _decide(policy, PS_IEX_FLIPPED).rule == f"prefix:{card['suggested_prefix']}"


def test_tool_scope_tier_text_is_verbatim_when_it_is_offered() -> None:
    """工具档只有 ``tool_scope=True`` 才出现，文案与 permissions.py:390-396 逐字一致。"""
    options = approval_options("apply_patch", "", tool_scope=True)
    assert _scopes(options) == [SCOPE_ONCE, SCOPE_TOOL]
    assert options[1]["label"] == "本会话总是允许 apply_patch"
    assert options[1]["warning"] == TOOL_WARNING
    # ``memo`` 为空时不给前缀档：卡上少一档是「这一条记不下来」，不是把档位吞了
    assert _scopes(approval_options("run_shell", "", tool_scope=False)) == [SCOPE_ONCE]


# --------------------------------------------------------------------------- #
# 3. 缺 tool_scope = 当场 TypeError：两个真实发卡点都得显式表态
# --------------------------------------------------------------------------- #
def test_missing_tool_scope_raises_typeerror() -> None:
    """必填关键字（permissions.py:362）：漏传或位置传都当场 TypeError。

    报错文案分两种：漏传时 Python 点名字 ``tool_scope``（实测
    ``approval_options() missing 1 required keyword-only argument: 'tool_scope'``）；
    位置传时先撞在实参个数上（实测
    ``approval_options() takes from 1 to 2 positional arguments but 3 were given``）。
    两种都必须红在发卡那一刻，而不是发出一颗不留账的钮。
    """
    with pytest.raises(TypeError) as first:
        approval_options("run_shell", "python -c print(1)")
    assert str(first.value) == "approval_options() missing 1 required keyword-only argument: 'tool_scope'", str(first.value)
    with pytest.raises(TypeError) as second:
        approval_options("run_shell", "python -c print(1)", False)
    assert "positional" in str(second.value), str(second.value)
    with pytest.raises(TypeError) as third:
        approval_options("run_shell")
    assert str(third.value) == "approval_options() missing 1 required keyword-only argument: 'tool_scope'", str(third.value)


def test_every_card_site_in_source_passes_tool_scope() -> None:
    """源码静态对账：``src/blank_api`` 里每一个 ``approval_options(...)`` 调用点都必须带
    ``tool_scope=``。新增发卡点漏传在跑起来之前就红，而不是等界面长出一颗不留账的钮。
    """
    sites: list[tuple[str, int, bool]] = []
    for file in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "approval_options":
                continue
            keywords = {str(kw.arg) for kw in node.keywords if kw.arg is not None}
            sites.append((file.name, node.lineno, "tool_scope" in keywords))
    assert sites, "源码里找不到任何发卡点：这条对账已经变成空断言"
    missing = [f"{name}:{line}" for name, line, ok in sites if not ok]
    assert not missing, f"这些发卡点漏传 tool_scope（会 TypeError 或长出按下去不留账的钮）：{missing}"
    assert {name for name, _, _ in sites} == {"graph.py", "gateway.py"}, sites


async def test_graph_card_site_passes_tool_scope_at_runtime(env, workspace: Path, monkeypatch):  # noqa: F811
    """图发卡点（``agent/graph.py:1623``）运行时实参对账：``tool_scope`` 必须是关键字且为 False。

    run_shell 带命令，:func:`tool_grant_too_wide` 判宽，工具档给不起——卡上就只能有两档。
    """
    import blank_api.services.agent.graph as graph_module

    recorded: list[dict] = []
    real = graph_module.approval_options

    def spy(tool_name: str, memo: str = "", **kwargs):
        recorded.append({"tool": tool_name, "memo": memo, **kwargs})
        return real(tool_name, memo, **kwargs)

    monkeypatch.setattr(graph_module, "approval_options", spy)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["跑一条 PowerShell 命令"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 列目录"},
            {"content": "列目录", "tool_calls": [call("run_shell", {"command": PS_PLAIN})]},
            {"content": "已完成"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task"]["status"] == "waiting_approval", body["task"]
    pending = body["evidence"]["pending_approvals"][0]
    assert recorded, "图发卡点没有调用 approval_options，这条对账是空的"
    assert [item["tool"] for item in recorded] == ["run_shell"], recorded
    assert recorded[-1]["memo"] == pending["suggested_prefix"] == "powershell -Command Get-ChildItem"
    assert recorded[-1]["tool_scope"] is False, recorded[-1]
    assert _scopes(pending["approval_options"]) == [SCOPE_ONCE, SCOPE_PREFIX]
    assert pending["approval_options"][1]["label"] == f"本会话总是允许 {pending['suggested_prefix']}"


async def test_gateway_card_site_passes_tool_scope_at_runtime(env, workspace: Path, monkeypatch) -> None:  # noqa: F811
    """网关发卡点（``tools/gateway.py:411``）运行时实参对账：同样必须显式表态。"""
    import blank_api.services.tools.gateway as gateway_module

    recorded: list[dict] = []
    real = gateway_module.approval_options

    def spy(tool_name: str, memo: str = "", **kwargs):
        recorded.append({"tool": tool_name, "memo": memo, **kwargs})
        return real(tool_name, memo, **kwargs)

    monkeypatch.setattr(gateway_module, "approval_options", spy)
    card = await _gateway_card(env, workspace, PS_IEX)
    assert recorded, "网关发卡点没有调用 approval_options"
    assert recorded[-1]["tool"] == "run_shell"
    assert recorded[-1]["tool_scope"] is False, recorded[-1]
    assert recorded[-1]["memo"] == card["suggested_prefix"]
    assert _scopes(card["approval_options"]) == [SCOPE_ONCE, SCOPE_PREFIX]


# --------------------------------------------------------------------------- #
# 2b. 端到端：卡面那一串走完「批准 → 落盘 → 新任务命中」全程不变形
# --------------------------------------------------------------------------- #
async def test_card_prefix_survives_the_whole_ledger_chain(env, workspace: Path):  # noqa: F811
    """同一串在六个位置必须逐字相同：卡面 X、label、``approval.granted.prefix``、
    ``approval.rule_persisted.prefix``、``approvals.json`` 的 token 数组渲染、新任务的命中归因。
    """
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    first_task = await create_task(env, project_id, ["批准一条内联脚本的前缀档"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 跑第一段\n2. 换个引号再跑同一段"},
            {"content": "跑第一段", "tool_calls": [call("run_shell", {"command": PY_TAIL})]},
            {"content": "换个引号再跑", "tool_calls": [call("run_shell", {"command": PY_TAIL_FLIPPED})]},
            {"content": "两段都跑完"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{first_task}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task"]["status"] == "waiting_approval", body["task"]
    pending = body["evidence"]["pending_approvals"][0]
    assert pending["suggested_prefix"] == PY_CARD, pending
    assert pending["approval_options"][1]["label"] == f"本会话总是允许 {PY_CARD}"
    assert pending["approval_options"][1]["warning"] == PREFIX_WARNING

    resp = await env.client.post(
        f"{API}/tasks/{first_task}/approval",
        json={"approve": True, "provider_id": provider_id, "scope": SCOPE_PREFIX},
    )
    assert resp.status_code == 200, resp.text
    resumed = resp.json()
    assert resumed["task"]["status"] != "waiting_approval", resumed["task"]

    events = (await env.client.get(f"{API}/tasks/{first_task}/events")).json()
    granted = [event for event in events if event["type"] == "approval.granted"]
    assert len(granted) == 1, granted
    assert granted[0]["payload"]["prefix"] == PY_CARD, granted[0]["payload"]
    persisted = [event for event in events if event["type"] == "approval.rule_persisted"]
    assert len(persisted) == 1, persisted
    assert persisted[0]["payload"]["prefix"] == PY_CARD, persisted[0]["payload"]
    assert persisted[0]["payload"]["persisted"] is True

    raw = json.loads(approval_rules.path().read_text(encoding="utf-8"))["rules"]
    assert raw[0]["tokens"] == ["python", "-c", "print(1)", "https://a.example"], raw
    assert render_rule(tuple(raw[0]["tokens"])) == PY_CARD

    # 会话档命中（同一任务里的换引号写法）：归因串 == 卡面串，前缀是 ``prefix:``
    session_hits = [
        (event["payload"].get("permission") or {}).get("rule", "")
        for event in events
        if event["type"] == "tool.completed" and event["payload"].get("tool") == "run_shell"
    ]
    assert [item for item in session_hits if item.startswith("prefix:")] == [f"prefix:{PY_CARD}"], session_hits

    # 落盘档命中（新任务）：同一个串，前缀换成 ``prefix:persistent:``
    second_task = await create_task(env, project_id, ["新任务里同一条命令不再挂卡"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 跑同一段"},
            {"content": "跑同一段", "tool_calls": [call("run_shell", {"command": PY_TAIL_FLIPPED})]},
            {"content": "已完成"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{second_task}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] != "waiting_approval", resp.json()["task"]
    events = (await env.client.get(f"{API}/tasks/{second_task}/events")).json()
    assert not [event for event in events if event["type"] == "approval.granted"], events
    durable_hits = [
        (event["payload"].get("permission") or {}).get("rule", "")
        for event in events
        if event["type"] == "tool.completed" and event["payload"].get("tool") == "run_shell"
    ]
    assert durable_hits == [f"prefix:persistent:{PY_CARD}"], durable_hits


# --------------------------------------------------------------------------- #
# 实测记录（本批跑出来的读数，判据全部来自这里；改产品代码的人请一并更新）
# --------------------------------------------------------------------------- #
# assess_risk / memo_rule / card(memoizable_prefix) 三条一组的读数：
#   PS_IEX                    high  ('powershell','-Command','Get-ChildItem | Invoke-Expression')
#                             'powershell -Command "Get-ChildItem | Invoke-Expression"'
#   PS_FILE_IEX               high  ('powershell','-NoProfile','-Command','iex (Get-Content setup.ps1)')
#   PS_PCT                    high  ('powershell','-Command','Write-Host %TMP%')
#   CMD_COMSPEC               high  ('cmd','/c','%COMSPEC% /c echo x')
#   PS_STRAY_QUOTE            high  ('powershell','-Command','Get-Process|Stop-Process ','k"')
#   PS_BACKTICK / PS_DOUBLED / PS_SINGLES / PS_NESTED_ESC / ENC_*   high  ()  ''
#   PS_PLAIN                  high  ('powershell','-Command','Get-ChildItem')  'powershell -Command Get-ChildItem'
# 裸可执行名规则（当前红）：durable ["powershell"] + PS_IEX -> ('allow','prefix:persistent:powershell')；
#   ["python"] + python -c 内联 -> ('allow','prefix:persistent:python')；
#   ["pwsh"] + ENC_PWSH -> ('allow','prefix:persistent:pwsh')；["cmd"] + CMD_COMSPEC -> ('allow','…:cmd')。
# 手写的 ["pwsh","-EncodedCommand","JAB4AD0AMQA="] 整段等长命中 -> ('allow','prefix:persistent:pwsh -EncodedCommand JAB4AD0AMQA=')。

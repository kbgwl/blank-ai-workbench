"""判定的唯一入口（卡 2b 第一刀）。

一次判定只有一条顺序，写死在 `authorization.authorize`：

    能力边界（safety） → 策略（permissions） → 严格复核

以前这条顺序在网关与图里各拼了一遍，"要不要问人"于是有两个答案。这里钉三件事：

1. 顺序只有一处：网关只是把它挂上去，源码里不许再出现第二份 `permissions.decide` 调用；
2. 边界先于策略：能力边界拒绝的动作，策略说放行也不算数（否则就是"点了允许还是失败"）；
3. 资源化：每次判定都带上 `path:` / `cmd:` 清单，审批卡与审计读同一份。
"""

from pathlib import Path
from types import SimpleNamespace

from blank_api.services.authorization import authorize, resources_for
from blank_api.services.permissions import ASK, approve
from blank_api.services.tools.gateway import ToolGateway

SRC = Path(__file__).resolve().parents[1] / "src" / "blank_api"


def _tool(name: str = "run_shell", risk: str = "medium"):
    return SimpleNamespace(name=name, resolve_risk=lambda _arguments: risk)


def _context(**kw):
    defaults = dict(
        root=Path("."),
        permissions=None,
        safety=None,
        approval_policy=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _safety(*, denied: bool = False, asks: bool = False):
    verdict = SimpleNamespace(denied=denied, asks=asks, rule="sandbox:test", reason="越界", prefix="")
    return SimpleNamespace(gate=lambda *_args, **_kw: verdict)


class _AutoPolicy:
    """策略替身：自动放行（rule 落在自动清单里，才会被严格复核抓）。"""

    mode = "default"

    def decide(self, _tool, _arguments, risk):
        return approve(risk, "policy:auto")


def test_the_order_is_written_in_exactly_one_place() -> None:
    """源码级门禁：除了 authorization，谁都不许自己调 permissions.decide 拼顺序。"""
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "permissions.decide(" not in text:
            continue
        if path.name == "authorization.py":
            continue
        offenders.append(str(path.relative_to(SRC)))
    assert offenders == [], f"判定顺序又长出了第二份：{offenders}"


def test_the_capability_boundary_wins_over_the_policy() -> None:
    """边界拒绝的动作，策略放行也不算数——卡上点"允许"也不该执行。"""
    decision = authorize(
        _tool(risk="medium"),
        {"command": "echo hi"},
        _context(permissions=_AutoPolicy(), safety=_safety(denied=True)),
    )
    assert decision.denied and decision.rule == "sandbox:test"


def test_a_boundary_that_wants_a_second_look_raises_the_risk() -> None:
    """「每条命令都要确认」不另造状态机：抬高风险，后面的审批逻辑自然接手。"""
    decision = authorize(
        _tool(risk="medium"),
        {"command": "echo hi"},
        _context(permissions=_AutoPolicy(), safety=_safety(asks=True)),
    )
    assert decision.risk == "high", "风险没抬上去，后面那道闸就形同虚设"


def test_an_automatic_allow_of_a_high_risk_action_becomes_a_question() -> None:
    """严格复核排在策略之后：自动放行的高危动作必须升级成问人。"""
    decision = authorize(
        _tool(risk="high"),
        {"command": "echo hi"},
        _context(permissions=_AutoPolicy()),
    )
    assert decision.outcome == ASK and decision.rule == "policy:strict_review"


def test_resources_are_paths_and_commands() -> None:
    assert resources_for("write_file", {"path": "src/x.py"}) == ("path:src/x.py",)
    assert resources_for("apply_patch", {"path": "./src/../src/y.py"}) == ("path:src/y.py",)
    assert resources_for("run_shell", {"command": "curl -s http://127.0.0.1/x"}) == ("cmd:curl",)
    assert resources_for("run_shell", {"command": "  "}) == ("cmd:*",)
    assert resources_for("read_file", {"path": "a.txt"}) == ("tool:read_file",)


def test_every_decision_carries_its_resources() -> None:
    decision = authorize(
        _tool(name="write_file", risk="medium"),
        {"path": "src/x.py"},
        _context(permissions=_AutoPolicy()),
    )
    assert decision.resources == ("path:src/x.py",)
    assert decision.as_dict()["resources"] == ["path:src/x.py"]


def test_the_gateway_and_the_plain_entry_give_the_same_answer() -> None:
    """网关只是挂载点：同一输入必须同一答案，否则又回到两套判定。"""
    gateway = ToolGateway([])
    tool = _tool(risk="high")
    arguments = {"command": "echo hi"}
    context = _context(permissions=_AutoPolicy())
    via_gateway = gateway.decide(tool, arguments, context)
    direct = authorize(tool, arguments, context)
    assert (via_gateway.outcome, via_gateway.rule, via_gateway.risk, via_gateway.resources) == (
        direct.outcome,
        direct.rule,
        direct.risk,
        direct.resources,
    )

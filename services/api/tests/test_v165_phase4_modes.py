"""v165 §8/§13：三种自动化模式不能绕过审批矩阵。"""

from __future__ import annotations

from blank_api.services.agent.automation import (
    DECISION_ALLOW,
    DECISION_ASK,
    DECISION_DENY,
    approval_matrix,
    classify_action,
    effective_policy,
    is_high_risk,
    resolve_mode,
)
from blank_api.services.permissions import ALLOW, ASK, MODE_BYPASS, PermissionPolicy
from blank_api.services.tools.base import ToolDefinition


def test_resolve_mode_prefers_settings_and_rejects_dirty_values():
    assert resolve_mode({"background": {"automation_mode": "autonomous"}}) == "autonomous"
    assert resolve_mode({"background": {"automation_mode": "bypassPermissions"}}, "manual") == "manual"
    assert resolve_mode({}, "bogus") == "assist"
    assert resolve_mode(None) == "assist"


def test_effective_policy_separates_auto_start_from_allow_write():
    manual = effective_policy("manual")
    assert manual["auto_start"] is False and manual["allow_write"] is True
    plan = effective_policy("autonomous", "plan")
    # plan = 只读，不是「别动」：分析照样自动开跑，写动作由 PermissionPolicy 拒绝。
    assert plan["auto_start"] is True and plan["allow_write"] is False
    auto = effective_policy("autonomous", "bypass")
    assert auto["auto_start"] is True
    assert auto["allow_background"] is True and auto["allow_auto_repair"] is True
    assert auto["high_risk_always_asks"] is True


def test_autonomous_and_bypass_cannot_skip_high_risk_approval():
    """v165 §13 的硬闸：这一条是整个自动驾驶最重要的边界。"""
    verdict = approval_matrix(
        mode="autonomous", permission_mode="bypass", risk="high", action="git_push"
    )
    assert verdict["decision"] == DECISION_ASK and verdict["bypassable"] is False
    irreversible = approval_matrix(
        mode="autonomous", permission_mode="bypass", risk="low", irreversible=True
    )
    assert irreversible["decision"] == DECISION_ASK and irreversible["bypassable"] is False


def test_default_off_actions_are_denied_not_merely_asked():
    for action in ("mass_delete", "external_send", "prod_deploy", "outside_workspace"):
        verdict = approval_matrix(mode="autonomous", permission_mode="bypass", risk="low", action=action)
        assert verdict["decision"] == DECISION_DENY, action
        assert verdict["bypassable"] is False


def test_plan_mode_stays_readonly_even_in_autonomous():
    verdict = approval_matrix(mode="autonomous", permission_mode="plan", risk="low", action="apply_patch")
    assert verdict["decision"] == DECISION_ASK and verdict["bypassable"] is False


def test_low_risk_is_automatic_and_manual_medium_risk_asks():
    assert approval_matrix(mode="assist", risk="low")["decision"] == DECISION_ALLOW
    assert approval_matrix(mode="manual", risk="low")["decision"] == DECISION_ALLOW
    assert approval_matrix(mode="manual", risk="medium")["decision"] == DECISION_ASK
    assert approval_matrix(mode="assist", risk="medium")["decision"] == DECISION_ALLOW
    assert approval_matrix(risk="low", automation_allowed=False)["decision"] == DECISION_DENY


def test_is_high_risk_looks_at_the_command_not_just_the_tool_name():
    assert is_high_risk("run_shell", {"command": "git push origin main"}) is True
    assert is_high_risk("run_shell", {"command": "git status"}) is False
    assert is_high_risk("delete_file", {"path": "src/a.py"}) is True
    assert is_high_risk("read_file", {"command": "git push"}) is False


def _tool(name: str = "run_shell", risk: str = "high") -> ToolDefinition:
    async def handler(context, arguments):  # pragma: no cover - 只用于构造定义
        raise AssertionError("handler should not be called")

    return ToolDefinition(
        name=name, description="d", input_schema={"type": "object"},
        risk_level=risk, handler=handler,
    )


def test_bypass_cannot_skip_a_v165_high_risk_action_but_keeps_ordinary_bypass():
    """接线层：矩阵真的插进了 PermissionPolicy，不是只活在纯函数里。"""
    policy = PermissionPolicy(mode=MODE_BYPASS, automation_mode="autonomous")
    push = policy.decide(_tool(), {"command": "git push origin main"})
    assert push.outcome == ASK and push.rule == "v165:high_risk"
    # 普通命令仍按用户选的「全放行」走，不能被这道闸门偷偷改成逐步审批。
    assert policy.decide(_tool(risk="high"), {"command": "curl https://x"}).outcome == ALLOW
    assert policy.decide(_tool(risk="medium"), {"command": "echo hi"}).rule == "mode:bypass"


def test_approve_high_risk_flag_is_a_real_approval_not_a_mode_bypass():
    """§13 闸的是「模式」，不是「人当场说可以」。

    ``approve_high_risk`` 是调用方在本次运行请求体里写下的授权，它本身就是那次
    审批；挡掉它就会重演「我授权同意了，界面说我没同意」。全放行档位才是
    「设完就忘、无声放行」的那一类，由上一个用例负责。
    """
    policy = PermissionPolicy(approve_high_risk=True, automation_mode="assist")
    allowed = policy.decide(_tool(), {"command": "pip install requests"})
    assert allowed.outcome == ALLOW and allowed.rule == "flag:approve_high_risk"
    assert policy.decide(_tool(risk="low"), {"command": "echo hi"}).rule == "flag:approve_high_risk"


def test_classify_action_only_names_actions_the_matrix_understands():
    assert classify_action("run_shell", {"command": "git push"}) == "git_push"
    assert classify_action("run_shell", {"command": "pip install -r req.txt"}) == "install_dependency"
    assert classify_action("run_shell", {"command": "pytest -q"}) == ""
    assert classify_action("read_file", {"command": "git push"}) == ""

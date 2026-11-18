"""高危动作的强制复核闸门（对齐 Codex 的 strictReviewRequired）。

这个闸门只解决一件事：**没有依据的自动放行不许放行高危动作**。
它必须同时满足两个方向，缺一个就会出事：

- 只拦不放：用户点过「总是允许」、settings 写了 allow、会话批准记忆、
  钩子放行，全被再问一遍 → 逼用户去点"全部允许"，结果更不安全；
- 只放不拦：`rm -rf` 在 guardian auto 下无痕执行 → 我们的卖点（可追溯）是假的。

上一版补丁就是只做了前者，回归里 18 个测试当场变红。
"""

from __future__ import annotations

from blank_api.services.permissions import approve, ask, deny
from blank_api.services.tools.gateway import enforce_strict_review


def test_automatic_allow_of_a_high_risk_action_is_escalated() -> None:
    """策略默认放行 ≠ 有人批过：高危必须回到人面前。"""
    out = enforce_strict_review(approve("high", "policy:auto"), "high", "default")
    assert out.outcome == "ask"
    assert out.rule == "policy:strict_review"


def test_critical_risk_is_escalated_too() -> None:
    assert enforce_strict_review(approve("critical", "legacy:auto"), "critical", "default").outcome == "ask"


def test_empty_rule_counts_as_no_basis() -> None:
    """没有 rule 的 allow 等于"没人说过可以"，不能当依据。"""
    assert enforce_strict_review(approve("high", ""), "high", "default").outcome == "ask"


def test_settings_allow_is_respected() -> None:
    """settings 里显式 allow 是用户写的，必须放行。"""
    assert enforce_strict_review(approve("high", "settings:allow"), "high", "default").outcome == "allow"


def test_prefix_grant_is_respected() -> None:
    """「本会话总是允许 rm -rf」是用户按过的按钮，必须放行。"""
    out = enforce_strict_review(approve("high", "session:prefix", prefix="rm -rf"), "high", "default")
    assert out.outcome == "allow"


def test_hook_allow_is_respected() -> None:
    out = enforce_strict_review(approve("high", "hook:permission_request"), "high", "default")
    assert out.outcome == "allow"


def test_low_and_medium_risk_are_untouched() -> None:
    for risk in ("low", "medium"):
        assert enforce_strict_review(approve(risk, "policy:auto"), risk, "default").outcome == "allow"


def test_bypass_mode_is_not_escalated() -> None:
    """bypass 是用户显式说"别问我"，那不是静默放行。"""
    assert enforce_strict_review(approve("high", "policy:auto"), "high", "bypass").outcome == "allow"


def test_existing_ask_and_deny_are_preserved() -> None:
    """闸门只处理"该问却没问"的情况，不改写已有判定。"""
    assert enforce_strict_review(ask("high", "policy:confirm_all"), "high", "default").outcome == "ask"
    assert enforce_strict_review(deny("high", "capability:shell_disabled", "当前安全设置不允许执行命令"), "high", "default").outcome == "deny"

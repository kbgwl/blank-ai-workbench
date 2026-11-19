"""驳回时用户说的那句话，要原样交到模型手里。

原来只有两种结局：批 / 拒。用户想说的"先把测试方案补上""范围太大了"没有出口，
模型拿到的永远是同一句"用户驳回了"，于是它只能原样重试或换个等价写法——
三四种瞎试，本来一句话就能避免。

协议上加了一个可选 `feedback`（TaskApprovalIn → decision → 工具回执）。
这里钉住回执文案的两种形状。
"""

from __future__ import annotations

from blank_api.schemas.task import TaskApprovalIn
from blank_api.services.agent.graph import _denial_reason


def test_feedback_is_passed_through_verbatim():
    reason = _denial_reason("exit_plan_mode", "先把测试方案补上再来")

    assert "先把测试方案补上再来" in reason
    assert "不要原样重试" in reason
    assert "exit_plan_mode" in reason


def test_without_feedback_it_stays_the_generic_wording():
    """没有反馈时不能编一句用户没说过的话。"""
    reason = _denial_reason("run_shell", "")

    assert "用户驳回了" in reason
    assert "不要原样重试" in reason
    assert "并说" not in reason


def test_whitespace_only_feedback_counts_as_none():
    assert "并说" not in _denial_reason("run_shell", "   \n  ")


def test_the_protocol_accepts_feedback_and_defaults_to_empty():
    assert TaskApprovalIn(approve=False).feedback == ""
    assert TaskApprovalIn(approve=False, feedback="范围太大，只改一个文件").feedback == "范围太大，只改一个文件"

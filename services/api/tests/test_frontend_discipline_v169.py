"""玻璃题里四处前端覆盖缺陷，要在系统提示里先立规矩（#10/#11/#12/#17/#27）。

真机实物（runs-glass/ui-ours）：
- ``body > *:not(#bg-canvas){position:relative}`` 一条全局规则把导航 sticky、
  进度条 fixed、回到顶部 fixed 全压成 relative —— 三条需求一起失效；
- ``.palette-item{display:flex}`` 盖掉 ``[hidden]``，Ctrl+K 过滤看起来没反应；
- 窄屏徽标 ``0.75rem`` 换算成 12px，低于题面 14px；
- 自测脚本断言了实现里不存在的 ``data-yearly``/``.price-amount``，把正确实现判成红，
  预算花在「修一个不存在的 bug」上。

这四条都不是模型能力问题，是「交付前没有按后果检查」的纪律问题。判据钉在系统提示
原文上，并按输出样式语义随编程段走。
"""

from __future__ import annotations

from blank_api.services.agent.graph import SYSTEM_PROMPT, compose_system_prompt


def _prompt() -> str:
    return SYSTEM_PROMPT


def test_global_selector_must_not_clobber_positioning() -> None:
    text = _prompt()
    # 一条全局选择器能压掉 sticky/fixed —— 提示里必须点名这个后果。
    assert "全局选择器" in text or "body > *" in text
    assert "fixed" in text and "sticky" in text
    assert "position:relative" in text or "退化成 relative" in text


def test_hidden_attribute_needs_display_guard() -> None:
    text = _prompt()
    assert "[hidden]" in text
    assert "display:none" in text
    # 光说「注意 hidden」不够：要说明自定义 display 会盖掉它。
    assert "隐藏失效" in text or "盖掉" in text or "压掉" in text


def test_responsive_rules_must_be_checked_at_the_breakpoint() -> None:
    text = _prompt()
    assert "390px" in text
    assert "14px" in text
    assert "横向滚动" in text or "横滚" in text


def test_self_test_selection_must_match_the_implementation() -> None:
    text = _prompt()
    assert "自测脚本" in text
    assert "断言" in text
    # 核心语义：断言写错就改断言，不要为错断言改坏实现。
    assert "不要为了一条错断言" in text or "改断言" in text


def test_four_rules_follow_output_style_semantics() -> None:
    replaced = compose_system_prompt(
        {"name": "review", "prompt": "只做评审。", "keep_coding_instructions": False}
    )
    for needle in ("全局选择器", "[hidden]", "390px", "自测脚本"):
        assert needle not in replaced
    kept = compose_system_prompt(
        {"name": "review", "prompt": "只做评审。", "keep_coding_instructions": True}
    )
    for needle in ("全局选择器", "[hidden]", "390px", "自测脚本"):
        assert needle in kept

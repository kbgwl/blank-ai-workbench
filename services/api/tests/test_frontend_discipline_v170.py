"""事故指挥台复杂题里五类前端缺陷，要在系统提示里先立规矩（#402）。

真机实物（runs-torture/ui-ours，38 条探针判据 28 绿、10 红）：
- 每次重渲染整块重建 tbody，47ms 内焦点掉回 body，真实 Enter 到不了行，
  键盘完全打不开详情抽屉（7.1/7.2/9.1 三条一起红）；
- 窗口化只有尾部 spacer、没有顶部占位，滚动条比例缩短、滚不到真实最后一条（6.2）；
- 批量标记解决只改数据源不重算视图，「只看未解决」里仍留着已解决记录（10.2）；
- 视图状态只在启动时写 localStorage，筛选/搜索后刷新即丢（11.2）；
- 空筛选结果启动时 `buildRow(view[0])` 抛异常，后面的 bind* 全部被跳过，整页失联（17.1）；
- GLM 那格则是 HTML id 与 JS 选择器对不上，首屏 `TypeError`，同样整页失效。

这六条都不是模型能力问题，是「交付前没有按后果检查」的纪律问题。判据钉在系统提示
原文上，并按输出样式语义随编程段走。
"""

from __future__ import annotations

from blank_api.services.agent.graph import SYSTEM_PROMPT, compose_system_prompt


def _prompt() -> str:
    return SYSTEM_PROMPT


def test_rerender_must_restore_focus_and_keyboard() -> None:
    text = _prompt()
    # 光说「注意焦点」不够：要点名整块重建、焦点掉 body、Enter 打不开的后果。
    assert "重渲染" in text
    assert "焦点" in text
    assert "innerHTML" in text or "整块重建" in text
    assert "body" in text
    assert "Enter" in text
    # 给出可执行修法：保留连接或恢复 roving 焦点，鼠标与键盘同一条路径。
    assert "roving" in text
    assert "同一条打开路径" in text


def test_windowed_list_needs_leading_and_trailing_spacers() -> None:
    text = _prompt()
    assert "窗口化" in text
    assert "占位" in text
    assert "总高度" in text
    assert "最后一条" in text
    # 缺陷形态：只有尾部 spacer 会让滚动条比例缩短、滚不到底。
    assert "滚动条" in text
    assert "滚不到底" in text


def test_batch_actions_recompute_derived_views() -> None:
    text = _prompt()
    assert "派生" in text or "重算" in text
    assert "筛选" in text
    assert "计数" in text
    # 点名事故里的真实形态，避免只写一句抽象口号。
    assert "只看未解决" in text or "批量" in text


def test_view_state_is_persisted_on_every_change() -> None:
    text = _prompt()
    assert "刷新" in text
    assert "每次变化" in text
    assert "localStorage" in text or "hash" in text
    # 只说「要持久化」不够：要说明「只在启动时写一次等于没写」。
    assert "启动时" in text


def test_bootstrap_survives_empty_result_set() -> None:
    text = _prompt()
    assert "空数据" in text
    assert "view[0]" in text
    assert "事件绑定" in text
    assert "失联" in text
    # 可执行修法：先绑定再渲染，或两段各自兜底。
    assert "先绑定再渲染" in text or "各自兜底" in text


def test_renamed_ids_must_be_found_in_js_and_css() -> None:
    text = _prompt()
    assert "getElementById" in text
    assert "querySelector" in text
    assert "TypeError" in text
    # 缺陷形态：HTML id 与 JS 选择器对不上，首屏即空引用。
    assert "旧 id" in text or "已改名" in text


def test_six_rules_follow_output_style_semantics() -> None:
    needles = (
        "重渲染",
        "窗口化",
        "派生",
        "每次变化",
        "空数据",
        "getElementById",
    )
    replaced = compose_system_prompt(
        {"name": "review", "prompt": "只做评审。", "keep_coding_instructions": False}
    )
    for needle in needles:
        assert needle not in replaced
    kept = compose_system_prompt(
        {"name": "review", "prompt": "只做评审。", "keep_coding_instructions": True}
    )
    for needle in needles:
        assert needle in kept

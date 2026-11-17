"""运维控制台题（Nebula Ops）暴露的六类前端缺陷，要在系统提示里先立规矩。

真机实物（E:/tmp/productbench，30 条判据，Qoder 22/30）：

- `.table-wrap` 没写固定高度，`.table-scroll` 被 spacer 撑到 200651px，
  `clientHeight` 变成 800→5002 行全渲染，FPS 18.4；
- 真实滚动体是 document，`#tableScroll.scrollTop` 写上去不变，内部窗口停在 0；
- `buildBaseEvents()` 每条随机 30–209 秒，编号与时间不单调，升序首行 EVT-0003；
- 初始 `focusId=null` 使所有行 `tabindex=-1`，`tabbable=0`；重建移除焦点行后
  `focusInTable` 被 `focusout` 清掉，恢复逻辑走不到；
- `rowsEl` 的 keydown 首句 `if (!focusId) return`，而 focusId 从无到有的路径没被接线；
- `persist()` 在 unresolvedOnly=false 时不写 `unres`，hash 里缺这个键时旧存档胜出；
- `styles.css` 没有 `.is-hidden` 规则，WebGL 正常时 2D 兜底画布仍 `display:block` 全屏叠着。

这些都不是模型能力问题，是「改完没有按后果量一次」的纪律问题。判据钉在系统提示原文上，
并按输出样式语义随编程段走。
"""

from __future__ import annotations

from blank_api.services.agent.graph import SYSTEM_PROMPT, compose_system_prompt


def _prompt() -> str:
    return SYSTEM_PROMPT


def test_virtualized_list_container_must_have_its_own_height() -> None:
    text = _prompt()
    # 缺陷形态：容器不定高，spacer 把 clientHeight 撑成内容高度。
    assert "确定高度" in text
    assert "max-height" in text
    assert "clientHeight" in text
    assert "5000 行" in text
    # 验收口径：DOM 行数 + 滚到底，不是「看起来像虚拟化」。
    assert "DOM 行数远小于总行数" in text
    assert "滚到底能看到最后一条" in text


def test_scroll_must_target_the_real_scroller() -> None:
    text = _prompt()
    assert "scrollHeight" in text
    assert "clientHeight" in text
    assert "真实滚动体是 document" in text
    # 写 scrollTop 不生效的判定：写完读回来确认。
    assert "写上去不会变化" in text
    assert "滚动监听也要挂在同一个滚动体上" in text


def test_seed_data_must_keep_the_required_order() -> None:
    text = _prompt()
    assert "演示/种子数据" in text
    assert "30–209 秒" in text
    assert "恒定或递增" in text
    # 交付前自己按排序字段核对，而不是等探针发现。
    assert "排序字段核对正序首尾" in text


def test_roving_tabindex_starts_from_a_tabbable_row() -> None:
    text = _prompt()
    assert "roving tabindex 的初始值不能是 null" in text
    assert "tabindex=0" in text
    # 缺陷形态：keydown 首句早退，焦点状态没有写入路径。
    assert "if (!focusId) return" in text
    assert "focusin/pointerdown" in text
    # 重渲染移除焦点行后的恢复条件。
    assert "relatedTarget 为 null" in text
    assert "按 id 把焦点补回去" in text


def test_url_state_outranks_the_local_snapshot() -> None:
    text = _prompt()
    assert "URL 明确压过本地存档" in text
    assert "hash 里没有某个键" in text
    assert "默认值也写进 hash" in text


def test_fallback_layer_is_hidden_by_a_real_rule() -> None:
    text = _prompt()
    assert "样式表里不存在的类名" in text
    assert ".is-hidden{display:none}" in text
    assert "display:block" in text
    # 两种状态都要取一次样式或截图，不能只看默认态。
    assert "正常态与降级态" in text


def test_six_rules_follow_output_style_semantics() -> None:
    needles = (
        "确定高度",
        "真实滚动体是 document",
        "30–209 秒",
        "roving tabindex 的初始值不能是 null",
        "URL 明确压过本地存档",
        ".is-hidden{display:none}",
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

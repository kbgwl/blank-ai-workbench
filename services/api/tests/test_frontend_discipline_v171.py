"""玻璃题第二轮暴露的五类前端缺陷，要在系统提示里先立规矩。

真机实物（E:/tmp/productbench/runs-glass2，31 条判据第二轮）：

- ours 的 WebGL 片元着色器引用了顶点着色器才有的 `aPos` 且未声明、也没有 varying，
  编译失败后代码只把画布 `display:none`，首屏背景直接消失（canvas 0×0、draw 恒 0）；
- ours 的视差循环首次 `parallaxTick()` 在 `pointer.active=false` 时 early-return，
  `pointermove` 只置 active 不重启循环，循环永远停在第一帧；
- ours 的 `.hero__stage` 没写 `grid-template-columns`，auto 轨道按 `.hero__panel` 的
  max-content 撑到 394px，390px 视口出现横向滚动，导航被连带撑到 395px；
- ours 的装饰浮签 `aria-hidden` 里仍放着可见文字，移动端 0.78rem=12.5px，绕过 14px 下限；
- WB 与 Qoder 都只交了 index.html + styles.css，页面 `<script src>` 指向的 app.js
  没交，静态服务器 404×5，24 条行为判据连片红、reveal 停在 opacity 0。

这五条都不是模型能力问题，是「交付前没有按后果检查」的纪律问题。判据钉在系统提示
原文上，并按输出样式语义随编程段走。
"""

from __future__ import annotations

from blank_api.services.agent.graph import SYSTEM_PROMPT, compose_system_prompt


def _prompt() -> str:
    return SYSTEM_PROMPT


def test_shader_failure_must_not_silently_hide_canvas() -> None:
    text = _prompt()
    # 失败要留证据：读编译日志，并点名片元着色器/attribute/varying 这个真实错误形态。
    assert "getShaderInfoLog" in text
    assert "片元着色器" in text
    assert "attribute" in text
    assert "varying" in text
    # 不许只把画布藏掉：要么说清原因，要么留可见兜底。
    assert "display:none" in text
    assert "静态背景" in text or "兜底" in text
    assert "静默" in text or "吞掉" in text


def test_raf_loop_must_restart_when_gate_changes() -> None:
    text = _prompt()
    assert "requestAnimationFrame" in text
    assert "常驻循环" in text
    # 缺陷形态：首次因状态未就绪 early-return，之后没人再调它。
    assert "early-return" in text
    assert "重新启动" in text
    assert "第一帧" in text


def test_mobile_overflow_is_measured_and_grid_tracks_are_bounded() -> None:
    text = _prompt()
    # 先量再猜：scrollWidth 对视口宽，确认溢出后逐块二分定位。
    assert "scrollWidth" in text
    assert "二分" in text
    # 真实根因：auto 轨道按 max-content 撑破；给出可执行的修法。
    assert "max-content" in text
    assert "minmax(0, 1fr)" in text
    assert "min-width:0" in text


def test_decorative_layer_text_still_counts_toward_min_font() -> None:
    text = _prompt()
    assert "装饰层里的文字" in text
    assert "aria-hidden" in text
    assert "14px" in text
    # 判定门槛是「可见可读」，不是标签藏在装饰层就能豁免。
    assert "可见" in text


def test_entry_html_references_must_exist_before_delivery() -> None:
    text = _prompt()
    assert "<script src>" in text
    assert "<link href>" in text
    assert "交付目录" in text
    # 验证方式与真实后果：静态服务器看一眼控制台，404 会让整页脚本失联。
    assert "静态服务器" in text
    assert "404" in text
    assert "app.js" in text


def test_five_rules_follow_output_style_semantics() -> None:
    needles = (
        "getShaderInfoLog",
        "常驻循环",
        "scrollWidth",
        "装饰层里的文字",
        "<script src>",
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

"""上下文水位：按 token 阈值触发压缩，并用真实用量校准估算器。

字符启发式只能保证"看起来没超"，真正决定成败的是模型窗口还剩多少。
这里的四组断言对应四个必须成立的命题：

1. 水位是按窗口算的，不是按字符数；
2. 窗口未知时退回旧行为，不改变既有语义；
3. 校准会被真实 prompt_tokens 拉回正轨，但不会被一次异常读数带偏；
4. 触发之后先做免费的第一层裁剪，仍然超标才花钱总结。
"""

from __future__ import annotations

from blank_api.services.agent.context import (
    DEFAULT_CONTEXT_CHARS,
    CompactionReport,
    ContextCompactor,
    estimate_tokens,
    message_tokens,
)

BIG = "背景说明。\n" * 400


async def _summary(folded, previous):
    return f"{previous}\n- 已折叠 {len(folded)} 条"


def test_water_line_uses_the_window_ratio():
    compactor = ContextCompactor(context_window=128_000, trigger_ratio=0.8, reserve_tokens=16_384)
    # 80% = 102400，比"窗口减预留" = 111616 小，取比例那个
    assert compactor.trigger_tokens() == 102_400


def test_water_line_stops_before_the_reserve_is_eaten():
    compactor = ContextCompactor(context_window=32_000, trigger_ratio=0.95, reserve_tokens=16_384)
    # 比例算出来 30400，但窗口减预留只剩 15616：必须在写不出总结之前就压
    assert compactor.trigger_tokens() == 15_616


def test_reserve_larger_than_the_window_does_not_collapse_the_line():
    compactor = ContextCompactor(context_window=8_000, trigger_ratio=0.8, reserve_tokens=16_384)
    # 预留比窗口还大时不能把水位压到 1（那等于每轮都压缩），退回比例值
    assert compactor.trigger_tokens() == 6_400


def test_unknown_window_keeps_the_character_budget():
    compactor = ContextCompactor()
    assert compactor.context_window == 0
    assert compactor.trigger_tokens() == 0


async def test_small_history_is_left_alone_even_with_a_small_window():
    compactor = ContextCompactor(context_window=16_000, trigger_ratio=0.8, reserve_tokens=4_096)
    messages = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "任务目标"},
        {"role": "assistant", "content": "好的"},
    ]
    built = await compactor.build(messages, _summary)
    assert built == messages
    report = compactor.last_report
    # 16000 的 80% 是 12800，但窗口减预留只剩 11904 —— 取更小的那个
    assert report.trigger_at == 11_904
    assert report.folded == 0 and report.triggered_by == ""


async def test_crossing_the_water_line_folds_and_reports_tokens():
    compactor = ContextCompactor(context_window=4_000, trigger_ratio=0.8, reserve_tokens=512)
    messages = [{"role": "system", "content": "系统提示"}, {"role": "user", "content": "任务目标"}]
    for index in range(20):
        messages.append({"role": "assistant", "content": f"第 {index} 步"})
        # 每条内容都不一样，避免第一层的去重把小样本直接折叠掉（那样就测不到总结层）
        messages.append({"role": "tool", "content": f"第 {index} 次调用：{BIG}"})

    built = await compactor.build(messages, _summary)
    report = compactor.last_report
    assert report.folded > 0, "越过水位必须真正折叠，而不是只记一个标记"
    assert report.tokens_after < report.tokens_before
    assert report.context_window == 4_000 and report.trigger_at == 3_200
    assert report.triggered_by.startswith("tokens")
    assert any("已折叠" in str(item.get("content")) for item in built)


async def test_calibration_follows_real_prompt_tokens():
    compactor = ContextCompactor()
    messages = [{"role": "user", "content": "一段中文说明"}]
    estimated = message_tokens(messages)
    assert compactor.measure(messages) == estimated

    # 模型说这次实际是估算的两倍
    compactor.observe(prompt_tokens=estimated * 2, estimated_tokens=estimated)
    assert compactor.calibration == 2.0
    assert compactor.measure(messages) == estimated * 2
    assert compactor.observed_tokens == estimated * 2


async def test_calibration_ignores_absurd_readings():
    compactor = ContextCompactor()
    messages = [{"role": "user", "content": "短"}]
    estimated = message_tokens(messages)
    compactor.observe(prompt_tokens=estimated * 100, estimated_tokens=estimated)
    assert compactor.calibration == 1.0, "异常比例（缓存命中/换模型）不能永久带偏水位"
    compactor.observe(prompt_tokens=0, estimated_tokens=estimated)
    assert compactor.calibration == 1.0


async def test_calibration_moves_by_average_not_by_the_last_reading():
    compactor = ContextCompactor()
    compactor.observe(prompt_tokens=200, estimated_tokens=100)  # 2.0
    compactor.observe(prompt_tokens=100, estimated_tokens=100)  # 一次回落
    assert 1.0 < compactor.calibration < 2.0
    compactor.observe(prompt_tokens=300, estimated_tokens=100)  # 3.0 被 5.0 上限挡住
    assert compactor.calibration <= 5.0


async def test_level_exposes_a_usable_ratio():
    compactor = ContextCompactor(context_window=10_000, trigger_ratio=0.8)
    messages = [{"role": "user", "content": "x" * 8_000}]
    level = compactor.level(messages)
    assert level["context_window"] == 10_000
    assert level["trigger_at"] == 8_000
    assert 0 < level["ratio"] <= 1


async def test_duplicate_tool_output_is_absorbed_by_the_free_layer():
    """同样的输出重复 20 次时，第一层就够用，不该花钱去调摘要模型。"""
    compactor = ContextCompactor(context_window=4_000, trigger_ratio=0.8, reserve_tokens=512)
    messages = [{"role": "system", "content": "系统提示"}, {"role": "user", "content": "目标"}]
    for _ in range(20):
        messages.append({"role": "tool", "content": BIG})
    await compactor.build(messages, _summary)
    report = compactor.last_report
    assert report.micro.duplicates > 0
    assert report.triggered_by == "tokens:micro"
    assert report.folded == 0


async def test_character_mode_is_byte_for_byte_the_old_behaviour():
    """窗口未知时不能有行为漂移：老测试与老部署都依赖这套字符预算。"""
    compactor = ContextCompactor()
    small = [{"role": "user", "content": "x" * 100}]
    assert await compactor.build(small, _summary) == small
    assert compactor.last_report.triggered_by == ""

    # 每条 8000 字（正好是单条工具结果的归一化上限），14 条 ≈ 112k 字符，远超 48k 预算
    huge = [{"role": "system", "content": "系统"}, {"role": "user", "content": "目标"}]
    huge += [{"role": "tool", "content": f"{index}" + "y" * 7_999} for index in range(14)]
    built = await compactor.build(huge, _summary)
    report = compactor.last_report
    assert len(built) < len(huge)
    assert report.triggered_by.startswith("chars")
    assert report.folded > 0
    # 窗口未知时水位字段保持 0，前端据此知道"这次不是按窗口判定的"
    assert report.trigger_at == 0 and report.context_window == 0


def test_estimate_tokens_counts_cjk_differently_from_latin():
    chinese = "中" * 100
    latin = "a" * 100
    assert estimate_tokens(chinese) == 100
    assert estimate_tokens(latin) == 25


def test_report_serialises_the_water_line_for_the_event_log():
    report = CompactionReport(
        tokens_before=9_600, tokens_after=3_100, context_window=12_000, trigger_at=9_840
    )
    payload = report.as_dict()
    assert payload["context_window"] == 12_000
    assert payload["trigger_at"] == 9_840
    assert payload["water_ratio"] == 0.8
    assert payload["tokens_saved"] == 6_500

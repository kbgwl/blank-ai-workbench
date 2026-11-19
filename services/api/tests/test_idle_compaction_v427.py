"""空闲压缩：百分比水位在大窗口上等于没有水位。

真机数据（任务 `36d86ed2`）：26 步、prompt 从 19K 涨到 83K，累计 1.79M token——
而压缩阈值是 `0.82 × 1M = 820K`，**一次都没触发过**。模型一路拖着自己几十步前
写的代码往前走，这也是"后面 14 步全在原地打转"的土壤。

参照 Claude Code 的口径（200K+ token 的会话、空闲约 54 分钟后压缩，赶在 1 小时
提示缓存过期之前），这里改成三条绝对条件，任一命中就压：

    tokens ≥ 200K     —— 量够了
    steps  ≥ 40       —— 拖太久了
    idle   ≥ 45 分钟  —— 人走开了，正好整理

外加一条地板：不到 40K token 就不压——那种会话压了也没多少可压，
白牺牲细节。

判据要钉住三件事：该压的压、不该压的不压、以及"压了就别走微压缩那条短路"
（微压缩在没超水位时几乎削不动东西，会变成"说要压、其实没压"）。
"""

from __future__ import annotations

import asyncio

from blank_api.services.agent.context import (
    IDLE_ABSOLUTE_TOKENS,
    IDLE_MIN_TOKENS,
    IDLE_SECONDS,
    IDLE_STEPS,
    ContextCompactor,
)


def _bulk(messages: int, size: int = 4_000) -> list[dict]:
    return [{"role": "user", "content": f"第{i}段 " + "x" * size} for i in range(messages)]


def _compactor() -> ContextCompactor:
    return ContextCompactor(context_window=1_000_000)


def test_the_water_line_alone_would_never_fire_here():
    """先把病根钉住：这么多内容离 820K 还差得远，旧判定一律不压。"""
    compactor = _compactor()
    messages = _bulk(20)  # 约 20 × 4K 字符 ≈ 2 万 token 左右

    assert compactor.measure(messages) < compactor.trigger_tokens()
    assert compactor.idle_reason(compactor.measure(messages), steps_since_compact=0) == ""


def test_enough_steps_trigger_compaction_even_below_the_water_line():
    compactor = _compactor()
    messages = _bulk(45)  # 要过 40K token 的地板，否则测的是"太小不压"
    tokens = compactor.measure(messages)
    assert IDLE_MIN_TOKENS <= tokens < IDLE_ABSOLUTE_TOKENS

    report_before = len(messages)
    out = asyncio.run(compactor.build(messages, None, steps_since_compact=IDLE_STEPS))

    assert len(out) < report_before, "步数到了却没折，等于没这个功能"
    assert compactor.last_report.triggered_by.startswith("idle:steps"), compactor.last_report.triggered_by
    assert compactor.last_report.boundary > 0


def test_absolute_tokens_trigger_regardless_of_steps():
    compactor = _compactor()
    messages = _bulk(200)  # 约 20 万 token
    assert compactor.measure(messages) >= IDLE_ABSOLUTE_TOKENS

    asyncio.run(compactor.build(messages, None, steps_since_compact=1))

    assert compactor.last_report.triggered_by.startswith("idle:tokens"), compactor.last_report.triggered_by


def test_a_quiet_session_compacts_by_idle_time():
    compactor = _compactor()
    messages = _bulk(45)

    asyncio.run(compactor.build(messages, None, steps_since_compact=2, idle_seconds=IDLE_SECONDS + 1))

    assert compactor.last_report.triggered_by.startswith("idle:seconds"), compactor.last_report.triggered_by


def test_small_sessions_are_left_alone():
    """地板以下不压：压了没多少可压，只丢细节。"""
    compactor = _compactor()
    messages = _bulk(3, 2_000)

    asyncio.run(compactor.build(messages, None, steps_since_compact=99, idle_seconds=IDLE_SECONDS * 3))

    assert compactor.last_report.boundary == 0
    assert compactor.last_report.triggered_by == ""


def test_idle_compaction_does_not_stop_at_the_micro_step():
    """微压缩在没超水位时几乎不动东西。空闲触发必须直接走到整折，
    否则报告会写着"触发了"、上下文其实没变——那种假压缩比不压更坏。"""
    compactor = _compactor()
    messages = _bulk(45)
    before = compactor.measure(messages)

    out = asyncio.run(compactor.build(messages, None, steps_since_compact=IDLE_STEPS))

    assert compactor.measure(out) < before, "上下文没变小，说明停在了微压缩那一步"
    assert not compactor.last_report.triggered_by.endswith(":micro")


def test_water_line_still_wins_when_it_actually_fires():
    """新条件不能把老路径挤掉：真到水位线时仍然按 tokens 记。"""
    compactor = ContextCompactor(context_window=10_000, trigger_ratio=0.5, reserve_tokens=0)
    messages = _bulk(20)

    asyncio.run(compactor.build(messages, None, steps_since_compact=1))

    assert compactor.last_report.triggered_by.startswith("tokens"), compactor.last_report.triggered_by

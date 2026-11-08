"""定时解析的口径。

这一层的错误没法事后重放：客户机器上半夜跑一次任务、凌晨发一条提醒，
第二天只能看见结果、看不见原因。所以每个形态都钉一条。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from blank_api.services.agent.schedule import (
    KIND_DAILY,
    KIND_ONCE,
    KIND_WEEKLY,
    next_fire,
    parse_when,
)

CST = timedelta(hours=8)
# 2026-11-08 是周日。
NOW = datetime(2026, 11, 8, 10, 0)


def _when(text: str, *, now: datetime = NOW) -> object:
    return parse_when(text, now=now, offset=CST)


def test_tonight_is_evening_not_morning():
    """「今晚 8 点」是 20:00。按 12 小时制读成 08:00 就会在早上八点响。"""
    got = _when("今晚8点提醒我看一下部署")
    assert got is not None
    assert (got.hour, got.minute) == (20, 0)
    # 本地 20:00 (UTC+8) → 库里 UTC 12:00
    assert got.fire_at == datetime(2026, 11, 8, 12, 0)


def test_tomorrow_morning():
    got = _when("明天上午9点跑一遍回归")
    assert got is not None
    assert got.fire_at == datetime(2026, 11, 9, 1, 0)  # 09:00 CST = 01:00 UTC


def test_afternoon_bumps_to_pm():
    got = _when("下午3点开会")
    assert got is not None
    assert (got.hour, got.minute) == (15, 0)
    assert got.kind == KIND_ONCE


def test_relative_minutes():
    got = _when("30分钟后提醒我")
    assert got is not None
    assert got.fire_at == datetime(2026, 11, 8, 2, 30)  # 10:30 CST


def test_relative_hours():
    got = _when("2小时后检查一下")
    assert got is not None
    assert got.fire_at == datetime(2026, 11, 8, 4, 0)


def test_daily_repeat():
    got = _when("每天早上8点汇总一次")
    assert got is not None
    assert got.kind == KIND_DAILY
    assert got.at == "08:00"
    assert got.repeat_payload() == {"kind": "daily", "at": "08:00"}


def test_weekly_repeat():
    got = _when("每周一9点站会提醒")
    assert got is not None
    assert got.kind == KIND_WEEKLY
    assert got.weekday == 0
    assert got.repeat_payload() == {"kind": "weekly", "at": "09:00", "weekday": 0}


def test_bare_hour_already_passed_rolls_to_tomorrow():
    """只说「9 点」而现在 10 点：取最近的未来，不算顺延。"""
    got = _when("9点提醒我")
    assert got is not None
    assert got.fire_at == datetime(2026, 11, 9, 1, 0)  # 明天 09:00 CST
    assert got.shifted is False


def test_explicit_past_time_shifts_and_says_so():
    """说了「今天下午 3 点」而现在 4 点：顺延到明天，并且标出来。"""
    got = _when("今天下午3点提醒我", now=datetime(2026, 11, 8, 16, 0))
    assert got is not None
    assert got.shifted is True
    assert got.fire_at == datetime(2026, 11, 9, 7, 0)  # 明天 15:00 CST


def test_half_hour():
    got = _when("8点半叫我")
    assert got is not None
    assert (got.hour, got.minute) == (8, 30)  # 已过 → 明天 8:30
    assert got.fire_at == datetime(2026, 11, 9, 0, 30)


def test_clock_with_colon():
    got = _when("晚上 21:45 提醒")
    assert got is not None
    assert (got.hour, got.minute) == (21, 45)


def test_morning_word_defaults_hour():
    """只说「明早」不说几点：用默认 9 点，这是明确的一天内时间，不算猜。"""
    got = _when("明早提醒我备份")
    assert got is not None
    assert got.hour == 9


def test_plain_work_request_parses_to_nothing():
    """没有时间的话绝不能凭空造一个到点时间。"""
    for text in ("帮我把 README 里的命令跑一遍", "这个项目是干什么的", "", "盯着构建结果"):
        assert _when(text) is None, text


def test_weekday_rolls_forward():
    """今天周日说「周五 9 点」：指下一个周五，不是昨天。"""
    got = _when("周五9点发周报")
    assert got is not None
    assert got.fire_at == datetime(2026, 11, 13, 1, 0)  # 11-13 是周五


def test_next_fire_advances_one_period():
    base = datetime(2026, 11, 8, 1, 0)
    assert next_fire(base, {"kind": "daily", "at": "09:00"}) == datetime(2026, 11, 9, 1, 0)
    assert next_fire(base, {"kind": "weekly", "at": "09:00"}) == datetime(2026, 11, 15, 1, 0)
    assert next_fire(base, {}) is None
    assert next_fire(base, {"kind": "乱写"}) is None

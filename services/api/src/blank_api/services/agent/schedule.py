"""把「今晚 9 点」「每天 8 点」「30 分钟后」变成库里的 UTC 时间。

为什么单独一层：

* 触发器只认 ``fire_at``（UTC naive），它不知道人话。而 defer 路径以前一律写
  ``fire_at=None``，于是「定时」永远是监控页上那句「等一个不会到来的事件」——
  登记得整整齐齐，到点一声不响。
* 解析必须是纯函数。调度错一次是在客户机器上错，事后无法重放，只能靠单测钉住。
* **宁可返回 None 也不猜。** 猜错时间的代价（半夜跑任务、凌晨发提醒）比
  「没解析出来就当场执行」大得多：后者用户看得见，前者看不见。

时区口径：用户说的「9 点」是他墙上的 9 点。库里存 UTC naive，所以这里先按
**本地时间**组装，再减掉当前时区偏移。反过来做（按 UTC 理解）会整体偏 8 小时——
这个坑上一版在监控页上已经踩过一次。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

# 没写钟点时默认几点。选 9 点是刻意的：早上开工时间，不是半夜。
DEFAULT_HOUR = 9

KIND_ONCE = "once"
KIND_DAILY = "daily"
KIND_WEEKLY = "weekly"

_CN_DIGIT = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


@dataclass(frozen=True)
class When:
    """一条解析结果。``fire_at`` 是可直接入库的 UTC naive。"""

    fire_at: datetime
    kind: str = KIND_ONCE
    hour: int = 0
    minute: int = 0
    weekday: int = -1
    shifted: bool = False
    source: str = ""

    @property
    def at(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    def repeat_payload(self) -> dict:
        """写进触发器 payload 的重复说明；一次性返回空 dict。"""
        if self.kind == KIND_ONCE:
            return {}
        data = {"kind": self.kind, "at": self.at}
        if self.kind == KIND_WEEKLY:
            data["weekday"] = self.weekday
        return data


def _num(raw: str) -> int | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text in _CN_DIGIT:
        return _CN_DIGIT[text]
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_DIGIT.get(left, 1) if left else 1
        ones = _CN_DIGIT.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


_REL_RE = re.compile(
    r"(\d{1,3}|[零一二两三四五六七八九十]{1,3})\s*"
    r"(秒钟|秒|分钟|分|小时|钟头|天|日|周|星期)\s*(?:之后|以后|后)"
)
_DAILY_RE = re.compile(r"每(?:天|日)|天天")
_WEEKLY_RE = re.compile(r"每(?:个)?(?:周|星期|礼拜)\s*([一二三四五六日天1-7])")
_DAY_RE = re.compile(r"(今天|今日|今晚|今夜|明天|明日|明早|明晚|后天|大后天)")
_WEEKDAY_RE = re.compile(r"(?<![每])(?:这|本|下)?(?:周|星期|礼拜)\s*([一二三四五六日天])")
_PERIOD_RE = re.compile(r"(凌晨|清晨|早上|早晨|早|上午|中午|下午|傍晚|晚上|夜里|夜间|晚)")
_CLOCK_RE = re.compile(
    r"(\d{1,2}|[零一二两三四五六七八九十]{1,3})\s*"
    r"(?:[:：]\s*(\d{1,2})(?!\d)|(点|时)\s*(半|(\d{1,2})(?!\d))?)"
)
_BARE_HOUR_RE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:点钟|点整)(?!\d)")


def _offset() -> timedelta:
    moment = datetime.now().astimezone()
    return moment.utcoffset() or timedelta()


def _to_utc_naive(local: datetime, offset: timedelta) -> datetime:
    return (local - offset).replace(tzinfo=None)


def _period_hour(hour: int, period: str, *, evening_day: bool) -> int:
    """按时段词把 12 小时制补成 24 小时制。"""
    if period in ("下午", "傍晚", "晚上", "夜里", "夜间", "晚"):
        return hour + 12 if hour < 12 else hour
    if period == "中午":
        return hour + 12 if hour < 12 else hour
    if period == "凌晨":
        return 0 if hour == 12 else hour
    if period in ("清晨", "早上", "早晨", "早", "上午"):
        return 0 if hour == 12 else hour
    # 没有时段词：「今晚 8 点」里的「晚」在日期词里，不在时段词里。
    if evening_day and hour < 12:
        return hour + 12
    return hour


def _weekday_of(word: str) -> int:
    if word.isdigit():
        return (int(word) - 1) % 7
    return _WEEKDAY_CN.get(word, 0)


def _next_weekday(today: date, target: int, *, include_today: bool) -> date:
    delta = (target - today.weekday()) % 7
    if delta == 0 and not include_today:
        delta = 7
    return today + timedelta(days=delta)


def parse_when(text: str, *, now: datetime | None = None, offset: timedelta | None = None) -> When | None:
    """从一句人话里解析出一个到点时间。解析不出就返回 None。

    ``now`` 是**本地** naive 时间，测试注入用；生产传 None 取系统本地时间。
    """
    raw = str(text or "")
    if not raw.strip():
        return None

    local_now = now or datetime.now()
    off = offset if offset is not None else _offset()

    # ── 1. 相对时间：30 分钟后 / 2 小时后 / 3 天后 ────────────────────────
    rel = _REL_RE.search(raw)
    if rel:
        amount = _num(rel.group(1))
        unit = rel.group(2)
        if amount is None or amount <= 0:
            return None
        if unit in ("秒钟", "秒"):
            delta = timedelta(seconds=amount)
        elif unit in ("分钟", "分"):
            delta = timedelta(minutes=amount)
        elif unit in ("小时", "钟头"):
            delta = timedelta(hours=amount)
        elif unit in ("周", "星期"):
            delta = timedelta(weeks=amount)
        else:
            delta = timedelta(days=amount)
        target = local_now + delta
        return When(
            fire_at=_to_utc_naive(target, off),
            kind=KIND_ONCE,
            hour=target.hour,
            minute=target.minute,
            source=rel.group(0),
        )

    # ── 2. 重复修饰 ──────────────────────────────────────────────────────
    kind = KIND_ONCE
    weekday = -1
    weekly = _WEEKLY_RE.search(raw)
    if weekly:
        kind = KIND_WEEKLY
        weekday = _weekday_of(weekly.group(1))
        raw = raw.replace(weekly.group(0), " ", 1)
    elif _DAILY_RE.search(raw):
        kind = KIND_DAILY
        raw = _DAILY_RE.sub(" ", raw, count=1)

    # ── 3. 日期词 ───────────────────────────────────────────────────────
    day_shift: int | None = None
    weekday_target: int | None = None
    evening_day = False
    day_match = _DAY_RE.search(raw)
    if day_match:
        word = day_match.group(1)
        if word in ("今天", "今日"):
            day_shift = 0
        elif word in ("今晚", "今夜"):
            day_shift, evening_day = 0, True
        elif word in ("明天", "明日"):
            day_shift = 1
        elif word == "明早":
            day_shift = 1
        elif word == "明晚":
            day_shift, evening_day = 1, True
        elif word == "后天":
            day_shift = 2
        else:
            day_shift = 3
        raw = raw.replace(word, " ", 1)
    elif not weekly:
        wd = _WEEKDAY_RE.search(raw)
        if wd:
            weekday_target = _weekday_of(wd.group(1))
            raw = raw.replace(wd.group(0), " ", 1)

    # ── 4. 钟点 ─────────────────────────────────────────────────────────
    hour: int | None = None
    minute = 0
    clock = _CLOCK_RE.search(raw)
    if clock:
        hour = _num(clock.group(1))
        if clock.group(2) is not None:
            minute = _num(clock.group(2)) or 0
        elif clock.group(4) == "半":
            minute = 30
        elif clock.group(5) is not None:
            minute = _num(clock.group(5)) or 0
    else:
        bare = _BARE_HOUR_RE.search(raw)
        if bare:
            hour = _num(bare.group(1))
    if hour is not None:
        if hour > 23 or minute > 59:
            return None
        period = _PERIOD_RE.search(raw)
        hour = _period_hour(hour, period.group(1) if period else "", evening_day=evening_day)

    # 只说了「明早」没说几点：用默认时刻，不算猜——这是明确的一天内时间。
    if kind == KIND_ONCE and hour is None and day_shift is None and weekday_target is None:
        if not day_match and not weekly:
            return None
        hour = DEFAULT_HOUR

    if hour is None:
        hour = DEFAULT_HOUR

    # ── 5. 组装本地时间并处理"已经过去" ─────────────────────────────────
    today = local_now.date()
    if weekday_target is not None:
        base = _next_weekday(today, weekday_target, include_today=False)
        target = datetime.combine(base, time(hour, minute))
        if target <= local_now:
            target += timedelta(days=7)
        return When(
            fire_at=_to_utc_naive(target, off),
            kind=kind, hour=hour, minute=minute, weekday=weekday_target,
            source=raw.strip() or str(text)[:40],
        )

    base_day = today + timedelta(days=day_shift or 0)
    target = datetime.combine(base_day, time(hour, minute))
    shifted = False
    if target <= local_now:
        if day_shift is not None:
            # 说了「今天下午 3 点」而现在 4 点：顺延到明天同一时刻，并且如实标记，
            # 界面能说"你说的时间已过，我定到明天"——不静默改成"现在跑"。
            target += timedelta(days=1)
            shifted = True
        else:
            # 没说日期只说「9 点」：取最近的未来，这是自然语义，不算顺延。
            target += timedelta(days=1)
    if kind == KIND_DAILY and target <= local_now:
        target += timedelta(days=1)

    return When(
        fire_at=_to_utc_naive(target, off),
        kind=kind,
        hour=hour,
        minute=minute,
        weekday=weekday,
        shifted=shifted,
        source=raw.strip() or str(text)[:40],
    )


def next_fire(prev: datetime, repeat: dict, *, offset: timedelta | None = None) -> datetime | None:
    """重复任务放行后算下一次的 UTC naive 时间。

    ``prev`` 是上一次的 UTC naive 到点时间。返回 None 表示这条重复说明读不懂——
    调用方应当停止再登记，而不是按错误的周期狂刷。
    """
    kind = str((repeat or {}).get("kind") or "")
    if kind == KIND_DAILY:
        return prev + timedelta(days=1)
    if kind == KIND_WEEKLY:
        return prev + timedelta(days=7)
    return None

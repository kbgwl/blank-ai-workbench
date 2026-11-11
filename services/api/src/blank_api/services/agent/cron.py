"""5 段 cron 的解析与到点计算，对齐 Claude CronCreate 的时间语义。

为什么单独一层：触发器只认 ``fire_at``（UTC naive），它不知道 cron 表达式；
而「0 9 * * 1-5」错一次是错在用户机器上，事后无法重放，只能靠纯函数单测钉住。

三条口径：

1. **标准 5 段、本地时区。** 用户在墙上时钟说「每天 9 点」，库里存 UTC naive。
   按 UTC 理解会整体偏 8 小时——这个坑在 :mod:`services.agent.schedule` 里踩过。
2. **宁可报错也不猜。** 表达式解析不了就抛 ``CronError``，不静默降级成"每分钟"。
3. **抖动是确定性的。** Claude 的调度器会加小抖动（重复任务最多晚 10%、封顶
   15 分钟；落在 :00/:30 的一次性任务最多提前 90 秒），目的是打散全舰队同一秒
   的请求。这里用 ``sha256(job_id + 到点时刻)`` 生成同样的偏移：同一份输入永远
   得到同一个时间，测试才可复现。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

KIND_CRON = "cron"
KIND_WAKEUP = "wakeup"

# Claude 的重复任务 7 天后自动结束：最后一次照常触发，然后删除。
RECURRING_LIFETIME = timedelta(days=7)
# 到点后留给"最后一次"的宽限：expire_stale 按 expires_at 判过期，若卡在第 7 天
# 整点，最后一次会被当成过期吞掉。多给一小时，保证窗口内的最后一次真的跑。
EXPIRY_GRACE = timedelta(hours=1)
# 一次性任务的补跑窗口：到点后这么久之内重启还能补跑一次；再晚就不跑了
# （一条几周前的提醒突然开跑，比不跑更吓人）。
ONESHOT_CATCHUP = timedelta(hours=24)
# 重复任务抖动的封顶（Claude：周期的 10%，最多 15 分钟）。
MAX_RECURRING_JITTER_SECONDS = 15 * 60
# 一次性任务落在整点/半点时最多提前多少秒（Claude：90 秒）。
MAX_ONESHOT_EARLY_SECONDS = 90
# 到点时绑定的任务还在跑：顺延这么久再看，而不是把这次触发丢掉。
BUSY_DEFER_SECONDS = 60
# 未来多少天里找下一次匹配。上限挡住「2 月 30 日」这类永远不成立、却又合法到
# 能通过字段校验的表达式，避免扫描循环无限跑。
SEARCH_HORIZON_DAYS = 3660

_FIELD_NAMES = ("分钟", "小时", "日", "月", "星期")
_FIELD_LIMITS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


class CronError(ValueError):
    """表达式不成立。消息直接回给模型，必须说清是哪一段、哪个值。"""


@dataclass(frozen=True)
class CronSpec:
    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    # 「这一段的原文是不是从 * 开头」决定日/星期同时受限时用 OR 还是 AND
    # （Vixie cron 的标准语义：两者都受限时任一命中即执行）。
    day_restricted: bool
    weekday_restricted: bool

    def day_matches(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        dom_ok = day.day in self.days
        dow_ok = (day.weekday() + 1) % 7 in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def matches(self, moment: datetime) -> bool:
        return (
            moment.minute in self.minutes
            and moment.hour in self.hours
            and self.day_matches(moment.date())
        )


def _int_value(text: str, name: str) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        raise CronError(f"{name}段的 {text!r} 不是整数") from None


def _parse_field(text: str, low: int, high: int, name: str) -> frozenset[int]:
    if not text:
        raise CronError(f"{name}段不能为空")
    values: set[int] = set()
    for chunk in text.split(","):
        if not chunk:
            raise CronError(f"{name}段里有空的取值（多余的逗号？）：{text}")
        step = 1
        base = chunk
        if "/" in chunk:
            base, _, step_text = chunk.partition("/")
            if not step_text.isdigit() or int(step_text) <= 0:
                raise CronError(f"{name}段的步长必须是正整数：{chunk}")
            step = int(step_text)
            if not base:
                raise CronError(f"{name}段的步长缺少起点：{chunk}")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            left, _, right = base.partition("-")
            start, end = _int_value(left, name), _int_value(right, name)
            if start > end:
                raise CronError(f"{name}段的区间起点大于终点：{chunk}")
        else:
            start = _int_value(base, name)
            # 「5/10」在 cron 里是「从 5 开始、每隔 10」；不给步长时就是一个值。
            end = high if step != 1 else start
        if start < low or end > high:
            raise CronError(f"{name}段超出范围 {low}-{high}：{chunk}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


def parse(expr: str) -> CronSpec:
    text = str(expr or "").strip()
    if not text:
        raise CronError("cron 表达式不能为空")
    parts = text.split()
    if len(parts) != 5:
        raise CronError(f"cron 必须是 5 段（分 时 日 月 周），收到 {len(parts)} 段：{text}")
    fields = [
        _parse_field(part, low, high, _FIELD_NAMES[index])
        for index, (part, (low, high)) in enumerate(zip(parts, _FIELD_LIMITS))
    ]
    # 星期允许 7 表示周日（Claude 的 5 段 cron 里 0 和 7 都指周日）。
    weekdays = frozenset(0 if value == 7 else value for value in fields[4])
    return CronSpec(
        expr=text,
        minutes=fields[0],
        hours=fields[1],
        days=fields[2],
        months=fields[3],
        weekdays=weekdays,
        day_restricted=not parts[2].startswith("*"),
        weekday_restricted=not parts[4].startswith("*"),
    )


def next_local(spec: CronSpec | str, after: datetime) -> datetime:
    """严格晚于 ``after`` 的下一个匹配时刻（本地 naive，精确到分钟）。"""
    parsed = parse(spec) if isinstance(spec, str) else spec
    start = (after.replace(second=0, microsecond=0) + timedelta(minutes=1))
    first_day = start.date()
    hours = sorted(parsed.hours)
    minutes = sorted(parsed.minutes)
    for offset in range(SEARCH_HORIZON_DAYS):
        day = first_day + timedelta(days=offset)
        if not parsed.day_matches(day):
            continue
        for hour in hours:
            if offset == 0 and hour < start.hour:
                continue
            for minute in minutes:
                if offset == 0 and hour == start.hour and minute < start.minute:
                    continue
                return datetime.combine(day, time(hour, minute))
    raise CronError(f"未来 {SEARCH_HORIZON_DAYS} 天内没有匹配的时间：{parsed.expr}")


def local_offset() -> timedelta:
    """当前时区相对 UTC 的偏移。库里一律存 UTC naive，墙上的时间由这一层换算。"""
    return datetime.now().astimezone().utcoffset() or timedelta()


def _fraction(seed: str) -> float:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def jittered(
    fire_at: datetime,
    *,
    job_id: str,
    recurring: bool,
    spec: CronSpec,
    offset: timedelta | None = None,
) -> datetime:
    """给一个到点时刻加上 Claude 式抖动。同一 (job_id, 时刻) 永远得到同一结果。

    ``fire_at`` 是 UTC naive；``offset`` 是本地时区偏移（判 :00/:30 用）。
    """
    moment = fire_at
    seed = f"{job_id}|{moment:%Y%m%d%H%M%S}"
    fraction = _fraction(seed)
    if recurring:
        zone = offset if offset is not None else local_offset()
        local = moment + zone
        try:
            following = next_local(spec, local)
        except CronError:
            return moment
        period = max(60.0, (following - local).total_seconds())
        delay = int(fraction * min(float(MAX_RECURRING_JITTER_SECONDS), period * 0.1))
        return moment + timedelta(seconds=delay)
    zone = offset if offset is not None else local_offset()
    local = moment + zone
    if local.minute in (0, 30):
        return moment - timedelta(seconds=int(fraction * MAX_ONESHOT_EARLY_SECONDS))
    return moment


def format_local(moment: datetime, offset: timedelta | None = None) -> str:
    zone = offset if offset is not None else local_offset()
    return (moment + zone).strftime("%Y-%m-%d %H:%M")


__all__ = [
    "BUSY_DEFER_SECONDS",
    "CronError",
    "CronSpec",
    "EXPIRY_GRACE",
    "KIND_CRON",
    "KIND_WAKEUP",
    "ONESHOT_CATCHUP",
    "RECURRING_LIFETIME",
    "format_local",
    "jittered",
    "local_offset",
    "next_local",
    "parse",
]

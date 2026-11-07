"""v165 §14/§15：让 ``settings.background`` 真正被消费。

v0.9.1 的实测教训：`background` 段八个字段里七个只被 normalize / schema / limits
读过，业务侧一个消费点都没有——用户在设置里改「最大步数 / 自动恢复次数」，
执行链照旧用硬编码常量。这里是唯一的解析口径：归一化、夹到硬上限、折算进
派发预算、给出 watchdog 告警的处置动作。全部纯函数，不读盘、不查库。

「只缩不放」是硬规则：设置能把预算调小，不能调大。调大等于让一份设置文件
绕过发布时钉死的成本闸门。
"""

from __future__ import annotations

from typing import Any

from blank_api.services.settings import (
    BACKGROUND_NOTIFY_POLICIES,
    DEFAULTS,
)

# 与 dispatch.py / tasks.py 的既有硬上限保持一致。数值故意不 import 那些模块：
# 它们依赖 FastAPI 与 ORM，这里应当能被任何一层零成本调用。
HARD_MAX_SECONDS = 6 * 3600
HARD_MAX_STEPS = 200
HARD_TOKEN_BUDGET = 1_500_000
HARD_AUTO_RECOVERIES = 3
AUTONOMOUS_TICK_SECONDS = 30

# watchdog.inspect() 的 action → 后台循环该做什么。
ENFORCE_NONE = "none"
ENFORCE_RECORD = "record"
ENFORCE_NOTIFY = "notify"
ENFORCE_REQUEUE = "requeue"
ENFORCE_ABANDON = "abandon"

_ENFORCEMENT = {
    "ok": ENFORCE_NONE,
    "diagnose": ENFORCE_RECORD,
    "notify": ENFORCE_NOTIFY,
    "pause": ENFORCE_NOTIFY,
    "release": ENFORCE_REQUEUE,
    "terminate": ENFORCE_ABANDON,
}


def _int(raw: Any, default: int, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def resolve_background(settings: dict[str, Any] | None) -> dict[str, Any]:
    """把 settings 字典解析成一份可直接用的后台策略，缺项回落默认值。"""
    raw = dict((settings or {}).get("background") or {})
    defaults = DEFAULTS["background"]
    notify = str(raw.get("notify_policy") or defaults["notify_policy"])
    if notify not in BACKGROUND_NOTIFY_POLICIES:
        notify = "approval_only"
    model_policy = str(raw.get("default_model_policy") or "default")
    return {
        "enabled": bool(raw.get("enabled", defaults["enabled"])),
        "notify_policy": notify,
        "quiet_hours": str(raw.get("quiet_hours") or ""),
        "max_seconds": _int(raw.get("max_seconds"), 1800, 60, HARD_MAX_SECONDS),
        "max_steps": _int(raw.get("max_steps"), 200, 1, HARD_MAX_STEPS),
        "token_budget": _int(raw.get("token_budget"), HARD_TOKEN_BUDGET, 1000, HARD_TOKEN_BUDGET),
        "auto_recoveries": _int(raw.get("auto_recoveries"), 2, 0, HARD_AUTO_RECOVERIES),
        "default_model_policy": model_policy,
        "automation_mode": str(raw.get("automation_mode") or "assist"),
    }


def recovery_limit(policy: dict[str, Any] | None) -> int:
    """替换 tasks.py 里硬编码的 MAX_RECOVERY_ATTEMPTS。"""
    return _int((policy or {}).get("auto_recoveries"), 2, 0, HARD_AUTO_RECOVERIES)


def budget_override(policy: dict[str, Any] | None, base: dict[str, Any]) -> dict[str, Any]:
    """把后台设置折进派发快照的预算；每一项只能更紧，不能更松。"""
    p = policy or {}
    out = dict(base)
    for key, setting_key, high in (
        ("max_steps", "max_steps", HARD_MAX_STEPS),
        ("max_seconds", "max_seconds", HARD_MAX_SECONDS),
        ("token_budget", "token_budget", HARD_TOKEN_BUDGET),
        ("max_recoveries", "auto_recoveries", HARD_AUTO_RECOVERIES),
    ):
        current = _int(out.get(key), high, 1, high)
        configured = _int(p.get(setting_key), high, 0, high)
        out[key] = min(current, configured) if configured else current
    return out


def autonomous_enabled(policy: dict[str, Any] | None, *, inprocess: bool) -> bool:
    """后台循环只在「API 进程自己执行队列」时启动。

    独立 worker 形态下它自己会扫租约与触发器；两边一起跑就会出现两个执行者，
    租约只能挡住其中一个，另一个会把同一条任务重跑一次。
    """
    return bool(inprocess and (policy or {}).get("enabled", True))


def enforcement(action: str) -> str:
    """watchdog 的告警等级 → 后台循环允许做的动作。

    观察者不直接改 run 状态：``requeue`` 只是把过期租约放掉，让既有的
    ``recover_pending_runs`` 重新认领；判死同样由那条路径的恢复次数上限负责。
    """
    return _ENFORCEMENT.get(str(action or ""), ENFORCE_RECORD)

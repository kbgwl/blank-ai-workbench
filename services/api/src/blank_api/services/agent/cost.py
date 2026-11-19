"""把 token 折成钱，并给一个"花到多少就停"的闸。

为什么要有这一层：token 闸管的是"别烧太多"，但它不是一个用户能直接理解的数。
「这个任务花了 1.5M token」——用户不知道这是 3 毛还是 300 块，取决于模型和读写比例。
金额闸把这件事变成"这次最多花 2 块钱"。

**两家外网 harness 都没有这个闸**（DeepSeek Harness 的 Known Limitations 直接写
"No built-in turn budget"，token meter 明说 "not a billing or gating input"；
opencode 同样只有 token）。所以这块只能自建。

三个刻意的设计：

1. **默认关**（`cost_limit = 0` = 不限）。没有价目表就凭空算钱，等于编一个数去吓人。
2. **分三段计价**：未命中缓存的输入、缓存读、输出，单价差好几倍。只乘一个"综合价"
   在长上下文场景会偏出好几倍——而长上下文恰恰是最费钱、最需要闸的那种任务。
3. **算的是估价，不是账单**：价目表是用户填的，可能过期。事件里写的是 `estimated`，
   免得有人拿它去对账。
"""

from __future__ import annotations

from typing import Any

# 每百万 token 的单价。键是 provider 名或 model 名（精确匹配，小写），
# 匹配不上就用 "default"；连 default 都没有 → 不估价。
PER_MILLION = 1_000_000


def _settings_pricing() -> tuple[dict, float]:
    """从设置里读价目表与上限。读不出来就当没配。"""
    try:
        from blank_api.services.settings import SettingsStore

        budget = (SettingsStore().read() or {}).get("budget") or {}
    except Exception:  # noqa: BLE001 - 读不到设置不该让预算判断炸掉
        return {}, 0.0
    pricing = budget.get("pricing") if isinstance(budget.get("pricing"), dict) else {}
    try:
        limit = float(budget.get("cost_limit") or 0)
    except (TypeError, ValueError):
        limit = 0.0
    return pricing, max(0.0, limit)


def price_for(pricing: dict, *, provider: str = "", model: str = "") -> dict[str, float]:
    """这次调用按哪一档单价算。

    匹配顺序：model → provider → default。先 model 是因为同一家不同型号价差可以到十倍
    （推理模型 vs 小模型），而 provider 级只有一个价时它俩只能二选一。
    """
    keys = [str(model or "").strip().lower(), str(provider or "").strip().lower()]
    for key in keys:
        if key and isinstance(pricing.get(key), dict):
            return _clean_prices(pricing[key])
    if isinstance(pricing.get("default"), dict):
        return _clean_prices(pricing["default"])
    return {}


def _clean_prices(raw: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for key in ("input", "cached_input", "output"):
        try:
            value = float(raw.get(key) or 0)
        except (TypeError, ValueError):
            value = 0.0
        out[key] = max(0.0, value)
    return out


def estimate_cost(usage: dict, pricing: dict, *, provider: str = "", model: str = "") -> dict[str, Any]:
    """把一份 usage 折成金额。

    返回 `{amount, currency, priced, breakdown}`。`priced=False` 表示这次没有单价，
    金额恒为 0——调用方要据此判断"这条闸到底有没有在工作"，而不是拿 0 当"免费"。
    """
    provider = provider or str((usage or {}).get("provider") or "")
    model = model or str((usage or {}).get("model") or "")
    price = price_for(pricing, provider=provider, model=model)
    if not price:
        return {"amount": 0.0, "currency": "", "priced": False, "breakdown": {}}

    prompt = int((usage or {}).get("prompt_tokens") or 0)
    cached = int((usage or {}).get("cached_tokens") or 0)
    completion = int((usage or {}).get("completion_tokens") or 0)
    # 缓存命中的部分包含在 prompt_tokens 里，要减出来按缓存价算，否则会重复计价。
    fresh_input = max(0, prompt - cached)

    input_cost = fresh_input / PER_MILLION * price["input"]
    cached_cost = cached / PER_MILLION * price["cached_input"]
    output_cost = completion / PER_MILLION * price["output"]
    return {
        "amount": round(input_cost + cached_cost + output_cost, 6),
        "currency": str((price or {}).get("currency") or ""),
        "priced": True,
        "breakdown": {
            "input_tokens": fresh_input,
            "cached_tokens": cached,
            "output_tokens": completion,
            "input": round(input_cost, 6),
            "cached": round(cached_cost, 6),
            "output": round(output_cost, 6),
        },
    }


def cost_limit() -> float:
    """用户设的金额上限；0 = 不限。"""
    return _settings_pricing()[1]


def current_pricing() -> dict:
    return _settings_pricing()[0]

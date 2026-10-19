"""模型成本估算：按每百万 Token 单价换算，用于阶段7 的成本统计。

单价只用于估算，真实账单以供应商为准；未收录的模型按 0 计并在结果里标注 estimated=False。
"""

from __future__ import annotations

from dataclasses import dataclass

PRICING_PER_MTOK: dict[str, dict[str, float]] = {
    # cached = 命中缓存后的输入单价倍率（Anthropic/DeepSeek 约为 0.1，OpenAI 约为 0.5）
    "gpt-4o-mini": {"prompt": 0.15, "completion": 0.60, "cached": 0.5, "currency": "USD"},
    "gpt-4o": {"prompt": 2.50, "completion": 10.00, "cached": 0.5, "currency": "USD"},
    "deepseek-chat": {"prompt": 0.27, "completion": 1.10, "cached": 0.1, "currency": "USD"},
    "deepseek-reasoner": {"prompt": 0.55, "completion": 2.19, "cached": 0.1, "currency": "USD"},
    "qwen-plus": {"prompt": 0.40, "completion": 1.20, "currency": "USD"},
    "qwen-turbo": {"prompt": 0.05, "completion": 0.20, "currency": "USD"},
    "claude-3-5-sonnet": {"prompt": 3.00, "completion": 15.00, "cached": 0.1, "currency": "USD"},
    "claude-3-5-haiku": {"prompt": 0.80, "completion": 4.00, "cached": 0.1, "currency": "USD"},
    "ollama": {"prompt": 0.0, "completion": 0.0, "currency": "USD"},
}


@dataclass(frozen=True, slots=True)
class Cost:
    amount: float
    currency: str
    priced: bool

    def as_dict(self) -> dict:
        return {"amount": round(self.amount, 6), "currency": self.currency, "priced": self.priced}


def price_for(model: str) -> dict[str, float] | None:
    if not model:
        return None
    exact = PRICING_PER_MTOK.get(model)
    if exact:
        return exact
    for name, table in PRICING_PER_MTOK.items():
        if model.startswith(name):
            return table
    return None


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> Cost:
    """按单价估算。cached_tokens 是 prompt_tokens 里命中缓存的那部分，按倍率打折。

    把缓存单独计价不是为了算得更准，而是为了让"缓存到底省了多少钱"可见 ——
    没有这个数字，优化提示词前缀就只是信仰。
    """
    table = price_for(model)
    if table is None:
        return Cost(amount=0.0, currency="USD", priced=False)
    total_prompt = max(0, int(prompt_tokens or 0))
    hit = min(total_prompt, max(0, int(cached_tokens or 0)))
    billed_prompt = total_prompt - hit
    discount = float(table.get("cached", 1.0))
    amount = (
        billed_prompt / 1_000_000 * table["prompt"]
        + hit / 1_000_000 * table["prompt"] * discount
        + (completion_tokens or 0) / 1_000_000 * table["completion"]
    )
    return Cost(amount=amount, currency=table["currency"], priced=True)

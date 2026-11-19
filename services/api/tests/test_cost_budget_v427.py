"""金额闸：把 token 折成钱，花到上限就收。

token 数用户没法直觉判断——"这个任务花了 1.5M token"，是 3 毛还是 300 块取决于模型
和读写比例。钱可以。

两家外网 harness 都没有这个闸（DSH 明写 "No built-in turn budget"，token meter
也说 "not a billing or gating input"），所以这块是自己造的，判据得自己钉紧。
"""

from __future__ import annotations

from blank_api.services.agent import cost as cost_module
from blank_api.services.agent.cost import estimate_cost, price_for
from blank_api.services.settings import normalize

PRICING = {
    "default": {"input": 2.0, "cached_input": 0.5, "output": 8.0},
    "claude-sonnet": {"input": 3.0, "cached_input": 0.3, "output": 15.0},
    "deepseek": {"input": 0.28, "cached_input": 0.028, "output": 0.42},
}


def test_cached_tokens_are_not_billed_twice():
    """缓存命中的部分包含在 prompt_tokens 里，要减出来按缓存价算。

    不减的话，一次 100k 上下文、命中 99% 的调用会被按全价计一遍——
    而这正是长任务最常见的形状（每步重发整段上下文）。
    """
    usage = {"prompt_tokens": 100_000, "cached_tokens": 99_000, "completion_tokens": 1_000}
    result = estimate_cost(usage, PRICING)

    assert result["breakdown"]["input_tokens"] == 1_000
    assert result["breakdown"]["cached_tokens"] == 99_000
    expected = 1_000 / 1_000_000 * 2.0 + 99_000 / 1_000_000 * 0.5 + 1_000 / 1_000_000 * 8.0
    assert abs(result["amount"] - expected) < 1e-9


def test_the_model_beats_the_provider():
    """同一家不同型号价差能到十倍，所以先按 model 匹配、再回落 provider。"""
    assert price_for(PRICING, provider="anthropic", model="claude-sonnet")["output"] == 15.0
    assert price_for(PRICING, provider="deepseek", model="deepseek-flash")["input"] == 0.28
    # 都不匹配 → default
    assert price_for(PRICING, provider="whatever", model="unknown")["input"] == 2.0


def test_no_pricing_means_not_priced_rather_than_free():
    """没有价目表时 amount=0，但 priced=False——调用方不能把 0 读成"免费"。"""
    result = estimate_cost({"prompt_tokens": 1000, "completion_tokens": 100}, {})

    assert result["priced"] is False
    assert result["amount"] == 0.0


def test_the_same_usage_costs_very_different_amounts_across_models():
    """这条不是为了断言具体数值，是为了钉住"必须分档"这个前提。"""
    usage = {"prompt_tokens": 120_000, "cached_tokens": 100_000, "completion_tokens": 3_000}
    cheap = estimate_cost(dict(usage, provider="deepseek"), PRICING)["amount"]
    dear = estimate_cost(dict(usage, provider="anthropic", model="claude-sonnet"), PRICING)["amount"]

    assert dear > cheap * 5, "同样的用量在不同模型上价差很大，一个'综合单价'算不准"


def test_the_configured_limit_is_read_from_settings(monkeypatch):
    monkeypatch.setattr(cost_module, "_settings_pricing", lambda: (PRICING, 2.5))
    assert cost_module.cost_limit() == 2.5
    assert cost_module.current_pricing() == PRICING

    monkeypatch.setattr(cost_module, "_settings_pricing", lambda: ({}, 0.0))
    assert cost_module.cost_limit() == 0.0, "0 表示不限——没有价目表就不该有闸"


def test_the_settings_layer_sanitises_prices():
    """价目表是手写的 JSON：写坏一条不该让整份配置失效。"""
    cleaned = normalize({
        "budget": {
            "cost_limit": -3,
            "pricing": {
                "DeepSeek": {"input": 0.28, "output": 0.42, "cached_input": 0.028},
                "broken": "not-an-object",
                "negative": {"input": -1, "output": "abc"},
            },
        }
    })["budget"]

    assert cleaned["cost_limit"] == 0.0, "负数当不限"
    assert cleaned["pricing"]["deepseek"]["input"] == 0.28, "键要小写，否则估不到价"
    assert "broken" not in cleaned["pricing"]
    assert cleaned["pricing"]["negative"] == {"input": 0.0, "cached_input": 0.0, "output": 0.0}


def test_a_default_is_off():
    budget = normalize({})["budget"]

    assert budget["cost_limit"] == 0.0
    assert budget["pricing"] == {}

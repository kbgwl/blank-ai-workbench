"""输出预算：请求前夹紧、失败后放大。

这套口径是从 Claude Code 的请求构造里反推出来的（`THs` 与模型表
`max_output_tokens: {default, upper}`）：

- 每次请求的 ``max_tokens`` 先跟**模型自己的上限**取小，不把配置值当真理。
  用户从别家抄来一个 65536 填在只吃 8192 的模型上，供应商不会报错，
  只会把回包切短——切在工具调用参数中间就是「参数是空对象」，报错指向
  工具而不是预算，排查成本极高。
- 思考预算的天花板是 ``max_tokens - 1``（同 ``effort.thinking_budget_ceiling``）。
  两者必须一起夹，否则供应商直接 400。
- 失败后放大是**只升不降**的单向动作：推理模型把预算烧在思考上时，
  重发同一份请求等于重跑同一段思考，唯一有效的变量就是给它更多空间。

上限表按模型族给，认不出来就用保守值。它只用于「取小」与「放大到哪为止」，
不用于「把配置调大」——配置写多少仍然是用户的决定。
"""

from __future__ import annotations

# 认不出模型时的上限。取 32768 而不是某个大值：宁可少给，也不能让一次请求
# 因为超上限被供应商拒掉（那会变成整轮失败，比截断严重）。
DEFAULT_CEILING = 32768

# 模型族 → 单次响应输出上限（tokens）。只收我们实际接过的族；
# 名字里带版本号的用前缀匹配，先长后短（`gpt-4o` 不能吃掉 `gpt-4o-mini` 的判断）。
_CEILINGS: tuple[tuple[str, int], ...] = (
    ("claude-opus-4", 64000),
    ("claude-sonnet-4", 64000),
    ("claude-3-7-sonnet", 64000),
    ("claude-haiku-4", 64000),
    ("claude-3-5", 8192),
    ("gpt-5", 128000),
    ("gpt-4.1", 32768),
    ("gpt-4o", 16384),
    ("o4-mini", 100000),
    ("o3", 100000),
    ("o1", 100000),
    ("deepseek-reasoner", 65536),
    ("deepseek-r1", 65536),
    ("deepseek-chat", 8192),
    ("qwen3", 32768),
    ("qwen-max", 8192),
    ("spark-x", 65536),
    ("glm-4", 32768),
    ("kimi", 32768),
    ("moonshot", 32768),
    ("gemini-2.5", 65536),
    ("gemini", 8192),
)

# 放大一步至少加这么多。翻倍在 8192 档只多 8k，常常还不够一段思考；
# 取「翻倍」与「+8192」的较大者，让小配置也能一次跨过思考的消耗。
MIN_GROWTH = 8192


def output_ceiling(model: str) -> int:
    """这个模型的单次输出上限。认不出来给 :data:`DEFAULT_CEILING`。"""
    name = (model or "").strip().lower()
    if not name:
        return DEFAULT_CEILING
    for prefix, ceiling in _CEILINGS:
        if prefix in name:
            return ceiling
    return DEFAULT_CEILING


def clamp_max_tokens(configured: int, model: str, *, override: int = 0) -> int:
    """这次请求实际发出去的 ``max_tokens``。

    ``override`` 是恢复动作给的临时值（比配置大），它同样要受模型上限约束：
    恢复不能把请求推到供应商会拒的那一侧去。返回值至少 1——0 会被当成
    「不限制」，与我们的意图相反。
    """
    wanted = int(override or configured or 0)
    return max(1, min(wanted, output_ceiling(model)))


def grown_max_tokens(current: int, model: str, *, steps: int = 1) -> int:
    """把当前预算放大 ``steps`` 步，返回放大后的值。

    已经顶到模型上限时原样返回——调用方据此判断「再放大没有意义」，
    不必自己跟上限表比。
    """
    ceiling = output_ceiling(model)
    value = max(1, int(current or 0))
    for _ in range(max(0, int(steps))):
        value = min(ceiling, max(value * 2, value + MIN_GROWTH))
    return min(value, ceiling)

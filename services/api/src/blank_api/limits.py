"""Agent runtime limits shared by API schemas, ORM models and the graph.

Kept dependency-free on purpose: schemas and models must be importable without
pulling in LangGraph or the model adapters.
"""

from __future__ import annotations

# 默认步数预算：要跑得完真实长任务（读几十个文件、多轮修复、来回验证）。
DEFAULT_MAX_STEPS = 200

# 安全上限：防止失控循环无限烧钱。超过这个值的请求直接拒绝。
HARD_MAX_STEPS = 500

# 距离预算耗尽还剩这么多步时，开始要求模型收尾，而不是继续开新工作面。
WRAP_UP_STEPS = 3

# 单任务 token 预算（prompt + completion，含缓存命中的部分）。
#
# 步数不是成本的代理指标：每步都会把上下文整段重发一次，同样的 50 步，上下文
# 30k 和 120k 差四倍。实测线上最惨的一次是 116 步、prompt 近 4M token，
# 最后还判失败——这中间的步数预算一次都没拦住。
#
# 1.5M 约等于 40 步 × 35k 上下文，正常的读改测任务足够；真跑不完的会在这里
# 被要求收尾并如实说没做完，而不是继续默默烧。可用 BLANK_TOKEN_BUDGET 覆盖。
DEFAULT_TOKEN_BUDGET = 1_500_000

# 用到这个比例就开始收尾提示，留出一段余地让模型把当前这一步做完。
TOKEN_WRAP_UP_RATIO = 0.85

# 只读调研的步数上限：征求意见类请求最多看这么几步就该给判断了。
# 它的目的不是把活干完，而是别在没看过任何东西时下结论。
READONLY_MAX_STEPS = 8

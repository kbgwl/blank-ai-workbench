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

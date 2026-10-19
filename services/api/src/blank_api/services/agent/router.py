"""按需选模型：先用便宜的打分器算「这一问有多难」，够用就行。

可以借 MoE 的地方不是「专家」本身——专家是预训练出来的，靠 prompt 拼不出来。
能借的是它那套调度：一个极便宜的打分器决定每个 token 走哪条分支，判不准才升级。
这里把「专家」换成模型池，把「token 路由」换成「节点路由」。

三条约束，少一条就会亏：

1. **打分器必须是纯函数。** 路由自己再调一次模型的话，省下的钱不够付这次调用，
   而且一旦判错就是两个模型的错叠在一起，排查时分不清是谁判错的。
2. **够用优先，便宜其次。** 反过来先挑最便宜的再祈祷它够用，就是「路由错了比
   不路由更糟」——难任务发给弱模型，失败重跑一次的成本远高于一开始就用强模型。
3. **淘汰原因必须能解释。** 用户看到「这一步换了便宜模型」时，得能看到另外几个
   为什么没被选中，否则路由就是个黑盒，出问题只能靠猜。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

LIGHT = "light"
STANDARD = "standard"
HEAVY = "heavy"

TIER_ORDER = {LIGHT: 1, STANDARD: 2, HEAVY: 3}
TIER_LABELS = {LIGHT: "轻量", STANDARD: "标准", HEAVY: "重型"}

# —— 打分权重。数字本身不重要，重要的是「谁比谁重」以及能被单测固定住 ——
# 节点基线：写计划要拆解，执行动作最模板化，审查要判断质量所以最重。
_NODE_BASE = {
    "create_plan": 45,
    "execute_action": 35,
    "review_result": 60,
    "summarize_result": 50,
}
_DEFAULT_BASE = 45
CONTEXT_FREE_TOKENS = 12_000
CONTEXT_STEP_TOKENS = 20_000
CONTEXT_STEP_POINTS = 8
CONTEXT_MAX_POINTS = 24
LONG_STEP_AT = 10
LONG_STEP_POINTS = 10
VERY_LONG_STEP_AT = 20
VERY_LONG_STEP_POINTS = 18
CRITERIA_POINTS = 5
# 不需要工具的纯问答，复杂度直接减一截：闲聊用不着重模型。
CHAT_RELIEF = 15
# 失败一次提一档。30 分足以跨过一个档位边界（<40 轻量 / <70 标准 / 其余重型）。
ESCALATION_POINTS = 30
MAX_ESCALATION = 2

# 能力阈值（单位：十亿激活参数）。见 tier_for_params 里为什么看激活而不看总参数。
_HEAVY_ACTIVE = 90.0
_STANDARD_ACTIVE = 18.0

# 参数量未知时的成本当量：不冒充便宜，按各档的中位水平估。
_TIER_COST = {LIGHT: 8.0, STANDARD: 50.0, HEAVY: 200.0}


def _clamp(value: int, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, value))


def tier_for_complexity(score: int) -> str:
    if score < 40:
        return LIGHT
    if score < 70:
        return STANDARD
    return HEAVY


def tier_for_params(total_b: float = 0.0, active_b: float = 0.0) -> str:
    """按参数量推能力档。

    **能力看激活参数，不看总参数。** 671B/37B 的稀疏模型本事接近一个 37B 的模型，
    不是接近 dense 的 671B：总参数吃掉的是显存，不是每一问都要跑一遍。把它当重型
    会让所有 MoE 模型都被判「贵得只能用在最难的地方」，正好和它们便宜的事实相反。
    只有拿不到激活参数时才退回总参数（稠密模型两者相等，退回也不会错）。

    没填（0）按 standard 处理，**不能按 light**：把「没填」当成「很小」，路由就会
    把难任务发给一个没人知道多大的模型，而且事后从事件里看不出原因。
    """
    active = float(active_b or 0)
    total = float(total_b or 0)
    scale = active if active > 0 else total
    if scale <= 0:
        return STANDARD
    if scale >= _HEAVY_ACTIVE:
        return HEAVY
    if scale >= _STANDARD_ACTIVE:
        return STANDARD
    return LIGHT


def cost_of(total_b: float = 0.0, active_b: float = 0.0, tier: str = STANDARD) -> float:
    """成本当量：优先用激活参数——解码时每个 token 只走激活的那部分，
    单价与激活量近似成正比。只有一个数可用时用总参数。都没有就用档位中位值。"""
    if float(active_b or 0) > 0:
        return float(active_b)
    if float(total_b or 0) > 0:
        return float(total_b)
    return float(_TIER_COST.get(tier, _TIER_COST[STANDARD]))


@dataclass(frozen=True)
class Demand:
    """这一步「需要多强的模型」。每一分都有来历，进事件流给用户看。"""

    node: str
    complexity: int
    tier: str
    needs_tools: bool
    context_tokens: int
    step: int
    escalated: int
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "node": self.node,
            "complexity": self.complexity,
            "tier": self.tier,
            "needs_tools": self.needs_tools,
            "context_tokens": self.context_tokens,
            "step": self.step,
            "escalated": self.escalated,
            "notes": list(self.notes),
        }


def assess(
    *,
    node: str,
    context_tokens: int = 0,
    step: int = 0,
    needs_tools: bool = True,
    has_criteria: bool = False,
    escalated: int = 0,
) -> Demand:
    """从已有状态推导需求档位。纯函数：同样的输入永远同样的输出。"""
    notes: list[str] = []
    base = _NODE_BASE.get(node, _DEFAULT_BASE)
    score = base
    notes.append(f"节点 {node} 基线 {base}")

    tokens = max(0, int(context_tokens or 0))
    if tokens > CONTEXT_FREE_TOKENS:
        # 12k 以内不算复杂——绝大多数单步任务的上下文都在这个量级，
        # 从这里就开始加分的话，每一步都会被判成「标准档」，便宜模型永远用不上。
        extra = tokens - CONTEXT_FREE_TOKENS
        buckets = extra // CONTEXT_STEP_TOKENS + (1 if extra % CONTEXT_STEP_TOKENS else 0)
        points = min(CONTEXT_MAX_POINTS, buckets * CONTEXT_STEP_POINTS)
        if points:
            score += points
            notes.append(f"上下文 {tokens} tokens 加 {points}")

    steps = max(0, int(step or 0))
    if steps >= VERY_LONG_STEP_AT:
        score += VERY_LONG_STEP_POINTS
        notes.append(f"第 {steps} 步（长任务）加 {VERY_LONG_STEP_POINTS}")
    elif steps >= LONG_STEP_AT:
        score += LONG_STEP_POINTS
        notes.append(f"第 {steps} 步加 {LONG_STEP_POINTS}")

    if has_criteria:
        score += CRITERIA_POINTS
        notes.append(f"有验收标准加 {CRITERIA_POINTS}")

    if not needs_tools:
        score -= CHAT_RELIEF
        notes.append(f"纯问答减 {CHAT_RELIEF}")

    bumps = max(0, min(MAX_ESCALATION, int(escalated or 0)))
    if bumps:
        points = bumps * ESCALATION_POINTS
        score += points
        notes.append(f"同节点已失败 {bumps} 次，升级加 {points}")

    complexity = _clamp(score)
    return Demand(
        node=node,
        complexity=complexity,
        tier=tier_for_complexity(complexity),
        needs_tools=bool(needs_tools),
        context_tokens=tokens,
        step=steps,
        escalated=bumps,
        notes=tuple(notes),
    )


@dataclass(frozen=True)
class Candidate:
    """一个可选模型。tier 决定「够不够用」，cost 只在同为够用时用来比价。"""

    provider_id: str
    name: str
    tier: str
    cost: float
    supports_tools: bool = True

    def as_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "name": self.name,
            "tier": self.tier,
            "cost": self.cost,
            "supports_tools": self.supports_tools,
        }


def candidate_from_provider(row: Any) -> Candidate:
    """从 model_providers 的一行造候选。老库没有参数字段时按「未填写」处理。"""
    total = float(getattr(row, "total_params_b", 0) or 0)
    active = float(getattr(row, "active_params_b", 0) or 0)
    tier = tier_for_params(total, active)
    return Candidate(
        provider_id=str(row.id),
        name=str(row.name),
        tier=tier,
        cost=cost_of(total, active, tier),
        supports_tools=bool(getattr(row, "supports_tools", False)),
    )


@dataclass(frozen=True)
class Decision:
    provider_id: str
    name: str
    tier: str
    demand: Demand
    reason: str
    rejected: tuple[tuple[str, str], ...] = ()
    # 池子里最强的也不够这一步用。此时仍然选了它，但要在事件里说清楚，
    # 否则出了错会有人以为是路由判错，其实是池子里没有更强的模型。
    shortfall: bool = False

    @property
    def routed(self) -> bool:
        return bool(self.provider_id)

    def as_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "name": self.name,
            "tier": self.tier,
            "reason": self.reason,
            "shortfall": self.shortfall,
            "rejected": [{"name": name, "why": why} for name, why in self.rejected],
            **self.demand.as_dict(),
        }


_EMPTY = Decision("", "", "", assess(node=""), "没有可路由的模型，沿用默认", ())


def pick(demand: Demand, candidates: Iterable[Candidate]) -> Decision:
    """够用且最便宜的那个。

    排序键是 (档位, 成本, 名字)：先按档位从低到高——同等够用下低档更便宜；
    再按成本；最后按名字，保证同价时结果稳定可复现（否则测试会随机飘）。
    """
    pool = [item for item in candidates if item.provider_id]
    if not pool:
        return _EMPTY

    needed = TIER_ORDER.get(demand.tier, TIER_ORDER[STANDARD])
    usable: list[Candidate] = []
    rejected: list[tuple[str, str]] = []

    for item in pool:
        if demand.needs_tools and not item.supports_tools:
            rejected.append((item.name, "不支持工具调用，这一步必须调工具"))
            continue
        rank = TIER_ORDER.get(item.tier, TIER_ORDER[STANDARD])
        if rank < needed:
            rejected.append(
                (
                    item.name,
                    f"{TIER_LABELS.get(item.tier, item.tier)}档不够这一步用"
                    f"（需要{TIER_LABELS.get(demand.tier, demand.tier)}档）",
                )
            )
            continue
        usable.append(item)

    if not usable:
        # 没有够用的：退到池子里最强的一个，并标明「不够用」。
        # 直接返回空会退回默认模型，而默认模型未必是池子里最强的。
        fallback = sorted(
            pool,
            key=lambda c: (-TIER_ORDER.get(c.tier, TIER_ORDER[STANDARD]), c.cost, c.name),
        )[0]
        return Decision(
            provider_id=fallback.provider_id,
            name=fallback.name,
            tier=fallback.tier,
            demand=demand,
            reason=f"池子里没有达到{TIER_LABELS.get(demand.tier, demand.tier)}档的模型，只能用它",
            rejected=tuple(rejected),
            shortfall=True,
        )

    usable.sort(key=lambda c: (TIER_ORDER.get(c.tier, TIER_ORDER[STANDARD]), c.cost, c.name))
    chosen = usable[0]
    for other in usable[1:]:
        rank = TIER_ORDER.get(other.tier, TIER_ORDER[STANDARD])
        if rank > TIER_ORDER.get(chosen.tier, TIER_ORDER[STANDARD]):
            rejected.append(
                (other.name, f"{TIER_LABELS.get(other.tier, other.tier)}档超出这一步所需，且更贵（{other.cost:g}）")
            )
        else:
            rejected.append((other.name, f"同档但更贵（{other.cost:g} vs {chosen.cost:g}）"))

    return Decision(
        provider_id=chosen.provider_id,
        name=chosen.name,
        tier=chosen.tier,
        demand=demand,
        reason=f"需要的{TIER_LABELS.get(demand.tier, demand.tier)}档里最便宜（成本当量 {chosen.cost:g}）",
        rejected=tuple(rejected),
    )


@dataclass
class Router:
    """有状态的薄壳：池子是配置，失败记忆是运行时状态，决策逻辑全在上面两个纯函数里。"""

    pool: tuple[Candidate, ...] = ()
    failures: dict[str, int] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)

    @property
    def active(self) -> bool:
        # 只有一个候选时没有"选"可言，直接走原路径，行为和没开路由完全一致。
        return len([item for item in self.pool if item.provider_id]) > 1

    def note_failure(self, node: str) -> int:
        """同一个节点失败了就把它的需求档位往上提一档。

        刻意做成粘性的：失败过的节点后面每一步都按更难的算。来回抖动（这步升级、
        下步降回来）花的钱比一直用强模型还多，而且从事件流里看不出发生过什么。
        """
        current = self.failures.get(node, 0)
        bumped = min(MAX_ESCALATION, current + 1)
        self.failures[node] = bumped
        return bumped

    def escalation(self, node: str) -> int:
        return self.failures.get(node, 0)

    def choose(
        self,
        *,
        node: str,
        context_tokens: int = 0,
        step: int = 0,
        needs_tools: bool = True,
        has_criteria: bool = False,
    ) -> Decision:
        demand = assess(
            node=node,
            context_tokens=context_tokens,
            step=step,
            needs_tools=needs_tools,
            has_criteria=has_criteria,
            escalated=self.escalation(node),
        )
        decision = pick(demand, self.pool)
        # 只留最近 50 条，长任务跑几百步也不会把内存吃出问题
        self.history.append(decision.as_dict())
        del self.history[:-50]
        return decision

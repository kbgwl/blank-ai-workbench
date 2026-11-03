"""Router：在任务真正开跑之前，把「这句话要干什么、允许动什么、怎么打扰用户」定下来。

这一层存在的理由不是多一次判断，而是**把判断的产物变成一份不可变快照**。以前的
判断散在两次调用里：路由层决定用哪个供应商、图里第一步再让模型判意图、工具层再
各自看场景决定禁不禁写。三处都在解释"用户到底要什么"，于是任何一处判错，事后都
无法回答「当时按什么权限跑的」——因为没有一个东西叫"当时的决定"。

这里把决定收进 `ExecutionSnapshot`：

* **纯函数**。``route()`` 只吃字符串和场景，不查库、不调模型。路由自己再调一次
  模型的话，省不了钱（还得付这次调用），判错时两个模型的错叠在一起更难查。
* **安全默认**。反序列化时缺字段、写坏值一律收紧到只读 / 记忆关闭 / 需要审批，
  **绝不**因为数据缺失而放开权限。宁可让任务多问一句，也不能让它多写一个文件。
* **快照不可变**。``snapshot_id`` 是字段的哈希；执行侧只用这一份，不重新解释
  用户权限。想改策略必须重新路由，留得下一条新快照。

和 ``intent.py`` 的分工：那边判的是**这一轮怎么答**（answer / advise / act，决定
要不要进工具循环），这边判的是**这次怎么派**（立即执行 / 排队 / 等事件 / 先问人）。
两者不互相替代：快照里的 intent 只影响派发与打扰策略，图里仍然会按模型判断细分类；
但快照给出的工具边界是硬约束，执行侧不能放宽。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping

from blank_api.limits import DEFAULT_MAX_STEPS, DEFAULT_TOKEN_BUDGET
from blank_api.services.agent.intent import INTENT_ACT, INTENT_ADVISE, guess_intent, unwrap_goal
from blank_api.services.agent.scenarios import resolve as resolve_scenario

# ── 协议取值（任务书 §3）。任何一项写坏都按"最保守"的那一项处理 ──────────────
INTENT_ANSWER = "answer"
INTENT_EXECUTE = "execute"
INTENT_BRAINSTORM = "brainstorm"
INTENT_SCHEDULE = "schedule"
INTENT_WATCH = "watch"
INTENT_ASK_USER = "ask_user"
INTENTS = (
    INTENT_ANSWER,
    INTENT_EXECUTE,
    INTENT_BRAINSTORM,
    INTENT_SCHEDULE,
    INTENT_WATCH,
    INTENT_ASK_USER,
)

SCENARIOS = ("dev", "docs", "daily", "brainstorm", "reverse")

PRIORITY_LOW = "low"
PRIORITY_NORMAL = "normal"
PRIORITY_HIGH = "high"
PRIORITY_URGENT = "urgent"
PRIORITIES = (PRIORITY_LOW, PRIORITY_NORMAL, PRIORITY_HIGH, PRIORITY_URGENT)

MODEL_POLICIES = ("default", "fast", "quality", "local", "user_selected")

TOOL_READ_ONLY = "read_only"
TOOL_WORKSPACE_EDIT = "workspace_edit"
TOOL_SHELL_WITH_APPROVAL = "shell_with_approval"
TOOL_EXTERNAL_CONNECTORS = "external_connectors"
TOOL_POLICIES = (TOOL_READ_ONLY, TOOL_WORKSPACE_EDIT, TOOL_SHELL_WITH_APPROVAL, TOOL_EXTERNAL_CONNECTORS)

NOTIFY_SILENT = "silent"
NOTIFY_MILESTONE = "milestone"
NOTIFY_APPROVAL_ONLY = "approval_only"
NOTIFY_ALWAYS = "always"
NOTIFY_POLICIES = (NOTIFY_SILENT, NOTIFY_MILESTONE, NOTIFY_APPROVAL_ONLY, NOTIFY_ALWAYS)

MEMORY_OFF = "off"
MEMORY_READ_ONLY = "read_only"
MEMORY_OPT_IN_WRITE = "opt_in_write"
MEMORY_POLICIES = (MEMORY_OFF, MEMORY_READ_ONLY, MEMORY_OPT_IN_WRITE)

# ── 派发方式。Router 回答的是"现在就跑，还是排进队列/等事件/先问人" ──────────
DISPATCH_IMMEDIATE = "immediate"  # 现在就跑（问答类，跑完即交付）
DISPATCH_QUEUE = "queue"  # 进后台队列，由 Worker 领取
DISPATCH_DEFER = "defer"  # 等事件或到点再来（watch / schedule）
DISPATCH_ASK = "ask_user"  # 先问清楚，不创建执行

# ── 预算。步数按意图给，而不是所有人一个 200：纯问答给 200 步等于允许它把
# 「你好」跑成一整轮工具循环，而真跑不完的长任务也不该被 8 步卡死。──────────
BUDGET_BY_INTENT: dict[str, dict[str, int]] = {
    INTENT_ANSWER: {"max_steps": 1, "max_seconds": 120, "max_recoveries": 0},
    INTENT_BRAINSTORM: {"max_steps": 6, "max_seconds": 300, "max_recoveries": 0},
    INTENT_SCHEDULE: {"max_steps": 1, "max_seconds": 60, "max_recoveries": 1},
    INTENT_WATCH: {"max_steps": 1, "max_seconds": 60, "max_recoveries": 1},
    INTENT_ASK_USER: {"max_steps": 1, "max_seconds": 60, "max_recoveries": 0},
    INTENT_EXECUTE: {"max_steps": DEFAULT_MAX_STEPS, "max_seconds": 1800, "max_recoveries": 2},
}

# 安全上限：快照里的预算可以比默认值小，不能比硬上限大——否则一条脏快照就能
# 让任务跑 500 步以上，而步数预算是唯一挡在成本前面的闸门。
HARD_MAX_SECONDS = 6 * 3600
HARD_MAX_RECOVERIES = 3

# 具体到「排不排队」之外，还要能解释为什么。这些词只作为规则快判的输入，
# 判不准就按场景与已有意图走，不硬猜。
_SCHEDULE_HINTS = ("每天", "每周", "每小时", "每月", "定时", "定时器", "日程", "预约", "提醒我", "cron", "every day")
_WATCH_HINTS = ("盯着", "监控", "一旦", "每当", "有变化", "文件变化", "watch", "发生就")
_BRAINSTORM_HINTS = ("头脑风暴", "几个方案", "几种方案", "发散", "列几个想法", "想几个方向")
# 高风险动作：出现这些词时，不管优先级多高，审批都不能绕过（任务书 §3）。
_HIGH_RISK_HINTS = ("删除", "删掉", "rm -rf", "清空", "重置数据库", "drop table", "生产", "线上", "force push", "发邮件")


class UnsafeSnapshot(ValueError):
    """快照本身违反安全不变量。构造期就抛出，不等到执行到一半才发现。"""


def _one_of(value: Any, allowed: Iterable[str], fallback: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else fallback


def _int_in(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def _clean_steps(steps: Any) -> tuple[str, ...]:
    if isinstance(steps, str):
        steps = [steps]
    if not isinstance(steps, (list, tuple)):
        return ()
    cleaned: list[str] = []
    for item in steps:
        if isinstance(item, dict):
            item = item.get("text") or item.get("step") or item.get("title") or ""
        line = str(item or "").strip()
        if line:
            cleaned.append(line[:60])
        if len(cleaned) >= 8:
            break
    return tuple(cleaned)


@dataclass(frozen=True)
class Budget:
    """一条任务的四道闸门。缺一项就不算预算完整。"""

    max_steps: int = 40
    max_seconds: int = 1800
    max_recoveries: int = 2
    token_budget: int = DEFAULT_TOKEN_BUDGET

    def as_dict(self) -> dict:
        return {
            "max_steps": self.max_steps,
            "max_seconds": self.max_seconds,
            "max_recoveries": self.max_recoveries,
            "token_budget": self.token_budget,
        }

    @classmethod
    def from_dict(cls, payload: Any, *, intent: str) -> "Budget":
        default = BUDGET_BY_INTENT.get(intent, BUDGET_BY_INTENT[INTENT_EXECUTE])
        data = payload if isinstance(payload, Mapping) else {}
        return cls(
            max_steps=_int_in(data.get("max_steps"), 1, DEFAULT_MAX_STEPS, default["max_steps"]),
            max_seconds=_int_in(data.get("max_seconds"), 1, HARD_MAX_SECONDS, default["max_seconds"]),
            max_recoveries=_int_in(
                data.get("max_recoveries"), 0, HARD_MAX_RECOVERIES, default["max_recoveries"]
            ),
            token_budget=_int_in(
                data.get("token_budget"), 1, 10 * DEFAULT_TOKEN_BUDGET, DEFAULT_TOKEN_BUDGET
            ),
        )


@dataclass(frozen=True)
class ExecutionSnapshot:
    """一次派发的完整决定。字段名与任务书协议一一对应。"""

    intent: str
    scenario: str
    priority: str
    model_policy: str
    tool_policy: str
    notify_policy: str
    memory_policy: str
    steps: tuple[str, ...] = ()
    risk_summary: str = ""
    needs_user_decision: bool = False
    budget: Budget = field(default_factory=Budget)
    # 审批永远是硬约束：快照里没有"关掉审批"这个取值，序列化时也不接受 False。
    # 写成一个恒 True 的字段而不是省略，是为了让测试和证据都能直接看到它。
    approval_required: bool = True
    # 命中了高风险动作词。只用来提示与收紧，不用来放行。
    high_risk: bool = False
    notes: tuple[str, ...] = ()
    # 版本号：策略语义变更时 +1，读旧快照的进程能据此判断要不要重路由。
    version: int = 1

    # ── 派生属性 ────────────────────────────────────────────────────────────
    @property
    def dispatch(self) -> str:
        """现在就跑 / 排队 / 等事件 / 先问人。"""
        if self.intent == INTENT_ASK_USER or self.needs_user_decision:
            return DISPATCH_ASK
        if self.intent in (INTENT_WATCH, INTENT_SCHEDULE):
            return DISPATCH_DEFER
        if self.intent == INTENT_EXECUTE:
            return DISPATCH_QUEUE
        return DISPATCH_IMMEDIATE

    @property
    def readonly(self) -> bool:
        """执行侧唯一该问的问题：这一趟允许写工作区吗？"""
        return self.tool_policy == TOOL_READ_ONLY

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "intent": self.intent,
            "scenario": self.scenario,
            "priority": self.priority,
            "model_policy": self.model_policy,
            "tool_policy": self.tool_policy,
            "notify_policy": self.notify_policy,
            "memory_policy": self.memory_policy,
            "steps": list(self.steps),
            "risk_summary": self.risk_summary,
            "needs_user_decision": self.needs_user_decision,
            "budget": self.budget.as_dict(),
            "approval_required": self.approval_required,
            "high_risk": self.high_risk,
            "notes": list(self.notes),
            "snapshot_id": self.snapshot_id,
            "dispatch": self.dispatch,
        }

    @property
    def snapshot_id(self) -> str:
        """字段哈希。同样的决定永远得到同一个 id，"快照有没有被改过"能对上账。"""
        body = {
            "version": self.version,
            "intent": self.intent,
            "scenario": self.scenario,
            "priority": self.priority,
            "model_policy": self.model_policy,
            "tool_policy": self.tool_policy,
            "notify_policy": self.notify_policy,
            "memory_policy": self.memory_policy,
            "steps": list(self.steps),
            "risk_summary": self.risk_summary,
            "needs_user_decision": self.needs_user_decision,
            "budget": self.budget.as_dict(),
            "approval_required": self.approval_required,
            "high_risk": self.high_risk,
            "notes": list(self.notes),
        }
        raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "dsp-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def from_dict(cls, payload: Any) -> "ExecutionSnapshot":
        """把库里的快照读回来。**任何异常值都收紧，不抛错。**

        读不回来就抛错会让一条老任务永远打不开；而放宽成"看着像什么就算什么"
        等于让脏数据决定权限。这里的折中是：结构永远能读出来，权限永远取最紧的
        那一档，理由写进 ``notes`` 里给证据页看。
        """
        notes: list[str] = []
        data: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
        if not isinstance(payload, Mapping):
            notes.append("快照不是对象，按安全默认处理")

        raw_intent = str(data.get("intent") or "").strip().lower()
        intent = _one_of(raw_intent, INTENTS, INTENT_ASK_USER)
        if raw_intent and intent != raw_intent:
            notes.append(f"意图 {raw_intent!r} 不认识，按先问人处理")

        raw_scenario = str(data.get("scenario") or "").strip().lower()
        scenario = _one_of(raw_scenario, SCENARIOS, "dev")
        if raw_scenario and scenario != raw_scenario:
            notes.append(f"场景 {raw_scenario!r} 不认识，按开发场景处理")

        budget = Budget.from_dict(data.get("budget"), intent=intent)

        raw_tool = str(data.get("tool_policy") or "").strip().lower()
        tool_policy = _one_of(raw_tool, TOOL_POLICIES, TOOL_READ_ONLY)
        if raw_tool and tool_policy != raw_tool:
            notes.append(f"工具策略 {raw_tool!r} 不认识，按只读处理")
        # 只读场景不能被一份脏快照放成可写：场景权限是下限，不是建议。
        if not resolve_scenario(scenario).allow_write and tool_policy != TOOL_READ_ONLY:
            notes.append(f"场景 {scenario} 不允许写工作区，工具策略收紧为只读")
            tool_policy = TOOL_READ_ONLY

        raw_memory = str(data.get("memory_policy") or "").strip().lower()
        memory_policy = _one_of(raw_memory, MEMORY_POLICIES, MEMORY_OFF)
        if raw_memory and memory_policy != raw_memory:
            notes.append(f"记忆策略 {raw_memory!r} 不认识，按关闭处理")
        if memory_policy == MEMORY_OPT_IN_WRITE and scenario != "daily":
            # 只有日常场景能写记忆（scenarios.py 的红线）。这条不接受数据覆盖。
            notes.append(f"场景 {scenario} 不允许写记忆，记忆策略收紧为只读")
            memory_policy = MEMORY_READ_ONLY

        raw_notify = str(data.get("notify_policy") or "").strip().lower()
        notify_policy = _one_of(raw_notify, NOTIFY_POLICIES, NOTIFY_APPROVAL_ONLY)
        if raw_notify and notify_policy != raw_notify:
            notes.append(f"通知策略 {raw_notify!r} 不认识，按仅在需要审批时通知")

        priority = _one_of(data.get("priority"), PRIORITIES, PRIORITY_NORMAL)
        model_policy = _one_of(data.get("model_policy"), MODEL_POLICIES, "default")

        return cls(
            intent=intent,
            scenario=scenario,
            priority=priority,
            model_policy=model_policy,
            tool_policy=tool_policy,
            notify_policy=notify_policy,
            memory_policy=memory_policy,
            steps=_clean_steps(data.get("steps")),
            risk_summary=str(data.get("risk_summary") or "").strip()[:400],
            needs_user_decision=bool(data.get("needs_user_decision")),
            budget=budget,
            # 数据里写 False 也照样是 True：审批开关不属于快照的可配项。
            approval_required=True,
            high_risk=bool(data.get("high_risk")),
            notes=tuple([*notes, *[str(item) for item in (data.get("notes") or [])]][:12]),
            version=_int_in(data.get("version"), 1, 99, 1),
        )

    def tightened(self, *, max_steps: int | None = None, readonly: bool | None = None) -> "ExecutionSnapshot":
        """再收紧一档。只允许往更严的方向走，所以调用方不需要知道自己会不会放宽。"""
        update: dict[str, Any] = {}
        if readonly and self.tool_policy != TOOL_READ_ONLY:
            update["tool_policy"] = TOOL_READ_ONLY
            update["notes"] = tuple([*self.notes, "执行侧收紧为只读"])[:12]
        if max_steps is not None:
            steps = max(1, min(self.budget.max_steps, int(max_steps)))
            if steps != self.budget.max_steps:
                update["budget"] = replace(self.budget, max_steps=steps)
        return replace(self, **update) if update else self

    def guard(self) -> None:
        """把不变量写死在这里。任何构造路径都要过一遍，不是只靠 from_dict。"""
        if self.intent not in INTENTS:
            raise UnsafeSnapshot(f"未知意图 {self.intent!r}")
        if self.scenario not in SCENARIOS:
            raise UnsafeSnapshot(f"未知场景 {self.scenario!r}")
        if self.tool_policy not in TOOL_POLICIES:
            raise UnsafeSnapshot(f"未知工具策略 {self.tool_policy!r}")
        if self.memory_policy not in MEMORY_POLICIES:
            raise UnsafeSnapshot(f"未知记忆策略 {self.memory_policy!r}")
        if self.priority not in PRIORITIES:
            raise UnsafeSnapshot(f"未知优先级 {self.priority!r}")
        if self.notify_policy not in NOTIFY_POLICIES:
            raise UnsafeSnapshot(f"未知通知策略 {self.notify_policy!r}")
        if self.memory_policy == MEMORY_OPT_IN_WRITE and self.scenario != "daily":
            raise UnsafeSnapshot("只有日常场景可以写记忆")
        if not resolve_scenario(self.scenario).allow_write and self.tool_policy != TOOL_READ_ONLY:
            raise UnsafeSnapshot(f"场景 {self.scenario} 不允许写工作区")
        if not self.approval_required:
            raise UnsafeSnapshot("审批不能被快照关闭")
        if self.budget.max_steps < 1 or self.budget.max_steps > DEFAULT_MAX_STEPS:
            raise UnsafeSnapshot("步数预算越界")


def _has(text: str, hints: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(hint.lower() in lowered for hint in hints)


def classify(goal: str, *, scenario: str = "dev") -> tuple[str, tuple[str, ...]]:
    """规则快判这次要"派成什么"。返回 (intent, notes)。

    刻意不在这里调模型：路由发生在**创建任务**这一刻，而用户点发送之后界面上
    应该立刻有反应。一次额外调用的延迟会直接变成"点了没反应"，而规则判错的代价
    很小——执行图里还会再判一次细类，这里只决定派发方式与权限边界。
    """
    text = unwrap_goal(str(goal or ""))
    notes: list[str] = []
    if not text.strip():
        return INTENT_ASK_USER, ("没有可执行的目标，先问清楚",)

    # 顺序：定时 / 监控 > 头脑风暴 > 意图判定。前两类说的是"什么时候做"，
    # 拿意图去判会把"每天帮我跑一次测试"判成 execute，于是当场就跑一遍。
    if _has(text, _SCHEDULE_HINTS):
        return INTENT_SCHEDULE, ("出现定时/提醒类说法，按等待触发派发",)
    if _has(text, _WATCH_HINTS):
        return INTENT_WATCH, ("出现监控/一旦类说法，按等待事件派发",)
    if scenario == "brainstorm" or _has(text, _BRAINSTORM_HINTS):
        return INTENT_BRAINSTORM, ("发散类请求，只读且不自动改工作区",)

    quick = guess_intent(text)
    if quick == "answer":
        return INTENT_ANSWER, ()
    if quick in (INTENT_ACT, INTENT_ADVISE):
        return INTENT_EXECUTE, ()
    # 判不准：按"要动工作区"派发，交由图里的模型判定收窄。
    # 反过来按闲聊派发会把一次真改动变成一句"好的"，而用户以为已经做完了。
    notes.append("规则判不准，按执行派发，由图内意图判定收窄")
    return INTENT_EXECUTE, tuple(notes)


def _tool_policy(intent: str, scenario: str, quick: str | None, memory_opt_in: bool) -> str:
    scenario_obj = resolve_scenario(scenario)
    if not scenario_obj.allow_write:
        return TOOL_READ_ONLY
    if intent in (INTENT_ANSWER, INTENT_BRAINSTORM, INTENT_SCHEDULE, INTENT_WATCH, INTENT_ASK_USER):
        return TOOL_READ_ONLY
    # 征求意见：要读，不要写。和执行图里 advise 的处理保持一致。
    if quick == INTENT_ADVISE:
        return TOOL_READ_ONLY
    return TOOL_WORKSPACE_EDIT


def _memory_policy(intent: str, scenario: str, memory_opt_in: bool) -> str:
    scenario_obj = resolve_scenario(scenario)
    if intent in (INTENT_ANSWER, INTENT_BRAINSTORM, INTENT_SCHEDULE, INTENT_WATCH, INTENT_ASK_USER):
        # 纯问答/发散不写记忆：这类请求里"记住"的往往是一句临时信息。
        return MEMORY_READ_ONLY if scenario_obj.memory_allowed else MEMORY_OFF
    if scenario_obj.memory_allowed and memory_opt_in:
        return MEMORY_OPT_IN_WRITE
    return MEMORY_READ_ONLY if scenario_obj.memory_allowed else MEMORY_OFF


def _notify_policy(intent: str, priority: str) -> str:
    if intent in (INTENT_ANSWER, INTENT_BRAINSTORM):
        return NOTIFY_SILENT
    if intent == INTENT_ASK_USER:
        return NOTIFY_ALWAYS
    if intent in (INTENT_WATCH, INTENT_SCHEDULE):
        return NOTIFY_APPROVAL_ONLY
    # 执行类默认里程碑播报：既不刷屏，也不让用户以为它没动。
    return NOTIFY_MILESTONE if priority in (PRIORITY_NORMAL, PRIORITY_HIGH, PRIORITY_URGENT) else NOTIFY_APPROVAL_ONLY


def _risk(intent: str, text: str) -> tuple[str, bool]:
    high = _has(text, _HIGH_RISK_HINTS)
    if high:
        return "包含高风险动作（删除/生产/外发），每一步都要审批", True
    if intent == INTENT_EXECUTE:
        return "会读写工作区，命令执行仍需审批", False
    return "", False


def route(
    goal: str,
    *,
    scenario: str = "dev",
    memory_opt_in: bool = False,
    priority: str | None = None,
    steps: Iterable[str] | None = None,
    max_steps: int | None = None,
) -> ExecutionSnapshot:
    """生成一份执行快照。同样的入参永远得到同样的快照（含 id）。"""
    scenario_key = resolve_scenario(scenario).key
    intent, notes = classify(goal, scenario=scenario_key)
    text = unwrap_goal(str(goal or ""))
    quick = guess_intent(text) if text.strip() else None
    priority_key = _one_of(priority, PRIORITIES, PRIORITY_NORMAL)
    risk_summary, high_risk = _risk(intent, text)
    budget_default = BUDGET_BY_INTENT[intent]
    budget = Budget(
        max_steps=int(budget_default["max_steps"]),
        max_seconds=int(budget_default["max_seconds"]),
        max_recoveries=int(budget_default["max_recoveries"]),
        token_budget=DEFAULT_TOKEN_BUDGET,
    )
    if max_steps:
        budget = replace(budget, max_steps=max(1, min(budget.max_steps, int(max_steps))))
    plan = _clean_steps(steps) if steps else ()
    snapshot = ExecutionSnapshot(
        intent=intent,
        scenario=scenario_key,
        priority=priority_key,
        model_policy="user_selected",
        tool_policy=_tool_policy(intent, scenario_key, quick, memory_opt_in),
        notify_policy=_notify_policy(intent, priority_key),
        memory_policy=_memory_policy(intent, scenario_key, memory_opt_in),
        steps=plan,
        risk_summary=risk_summary,
        # 只有"先问人"才需要决策；执行类不需要在派发阶段就打断用户。
        needs_user_decision=intent == INTENT_ASK_USER,
        budget=budget,
        high_risk=high_risk,
        notes=tuple(notes)[:12],
    )
    snapshot.guard()
    return snapshot


def explain(snapshot: ExecutionSnapshot) -> str:
    """一句话说明这次为什么这么派。给事件流用，不直接进用户正文。"""
    reason = {
        DISPATCH_IMMEDIATE: "现在执行，结果直接交付",
        DISPATCH_QUEUE: "排队交给后台执行",
        DISPATCH_DEFER: "登记为待触发，到点或有事件时再执行",
        DISPATCH_ASK: "先问清楚再执行",
    }.get(snapshot.dispatch, snapshot.dispatch)
    tools = {
        TOOL_READ_ONLY: "只读",
        TOOL_WORKSPACE_EDIT: "可读写工作区（命令仍需审批）",
        TOOL_SHELL_WITH_APPROVAL: "可执行命令（需审批）",
        TOOL_EXTERNAL_CONNECTORS: "可调用外部连接器",
    }.get(snapshot.tool_policy, snapshot.tool_policy)
    return f"{reason}；工具：{tools}；通知：{snapshot.notify_policy}；步数上限 {snapshot.budget.max_steps}"

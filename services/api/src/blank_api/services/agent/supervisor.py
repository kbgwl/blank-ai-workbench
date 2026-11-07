"""Chat Supervisor：每条用户消息先在这里判一次，产出结构化判断（v165 §2）。

为什么是纯规则、不额外调一次模型
----------------------------------
监督者跑在**每条**消息上。再挂一次模型调用，等于每轮多一次全量上下文和一次
网络往返；而这里要判的是「继续 / 暂停 / 批准 / 只是问问」这类边界清晰的词，
规则比模型更稳、更便宜、还能单测。真正需要语义理解的中间地带，仍然交给执行侧
原有的 ``guess_intent`` / ``parse_intent``——那边手里有完整任务上下文。

这里只判「这句话该怎么处理」，不改用户原话、不拼任何前缀（v0.9.1 的红线）：
原话必须原样进意图判定与模型，否则闲聊会被判成改代码任务。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

INTENT_ANSWER = "answer"
INTENT_INSPECT = "inspect"
INTENT_EXECUTE = "execute"
INTENT_CONTINUE = "continue"
INTENT_RETRY = "retry"
INTENT_RESUME = "resume"
INTENT_PAUSE = "pause"
INTENT_CANCEL = "cancel"
INTENT_APPROVE = "approve"
INTENT_REJECT = "reject"
INTENT_BRAINSTORM = "brainstorm"
INTENT_SCHEDULE = "schedule"
INTENT_WATCH = "watch"
INTENT_REMEMBER = "remember"
INTENT_FORGET = "forget"
INTENT_ASK_USER = "ask_user"

INTENTS = (
    INTENT_ANSWER, INTENT_INSPECT, INTENT_EXECUTE, INTENT_CONTINUE, INTENT_RETRY,
    INTENT_RESUME, INTENT_PAUSE, INTENT_CANCEL, INTENT_APPROVE, INTENT_REJECT,
    INTENT_BRAINSTORM, INTENT_SCHEDULE, INTENT_WATCH, INTENT_REMEMBER,
    INTENT_FORGET, INTENT_ASK_USER,
)

# 控制动作不新建任务：它们作用于**已经存在**的那一轮。
CONTROL_ACTIONS = frozenset(
    {INTENT_CONTINUE, INTENT_RETRY, INTENT_RESUME, INTENT_PAUSE, INTENT_CANCEL,
     INTENT_APPROVE, INTENT_REJECT}
)


@dataclass(frozen=True)
class Judgement:
    """一条消息的结构化判断（字段对齐 v165 §2.3，另加两个内部路由字段）。"""

    intent: str
    reason: str
    control_action: str = ""
    needs_task: bool = True
    # 这句话是上一目标的补充（要开新一轮，但挂回同一个目标）。
    continues_goal: bool = False
    needs_execution: bool = False
    needs_approval: bool = False
    needs_clarification: bool = False
    priority: str = "normal"
    confidence: float = 0.8
    message_id: str = ""
    goal_id: str = ""
    related_task_id: str = ""
    scenario: str = "dev"
    automation_mode: str = "assist"

    def as_dict(self) -> dict:
        return asdict(self)


# 整句控制词。顺序有意义：先精确短句，再前缀长句，避免「取消暂停」被拆错。
_EXACT_CONTROL: tuple[tuple[str, re.Pattern[str]], ...] = (
    (INTENT_CANCEL, re.compile(r"^\s*(?:取消|算了|不做了|别做了|不用做了|终止|放弃)\s*[!！。.~～,，]*\s*$")),
    (INTENT_PAUSE, re.compile(r"^\s*(?:暂停|停一下|先停|先别做|歇一下)\s*[!！。.~～,，]*\s*$")),
    (INTENT_RETRY, re.compile(r"^\s*(?:重试|再试一次|再试下|重跑|重新来|从头再来)\s*[!！。.~～,，]*\s*$")),
    (INTENT_RESUME, re.compile(r"^\s*(?:恢复|继续刚才的任务|解除暂停|接着跑)\s*[!！。.~～,，]*\s*$")),
    (INTENT_APPROVE, re.compile(r"^\s*(?:同意|批准|允许|通过|确认执行|可以执行|行|好)\s*[!！。.~～,，]*\s*$")),
    (INTENT_REJECT, re.compile(r"^\s*(?:拒绝|不同意|不批准|不通过|驳回)\s*[!！。.~～,，]*\s*$")),
    (INTENT_CONTINUE, re.compile(r"^\s*(?:继续|接着做|接着改|继续任务|按刚才的继续|再来一轮)\s*[!！。.~～,，]*\s*$")),
)

# 带补充内容的控制句：「继续，把按钮换成蓝色」「暂停这个任务」。
_PREFIX_CONTROL: tuple[tuple[str, re.Pattern[str]], ...] = (
    (INTENT_CANCEL, re.compile(r"^\s*(?:取消|终止|放弃)(?:这个|那个|当前|刚才的)?(?:任务|目标|这一轮)")),
    (INTENT_PAUSE, re.compile(r"^\s*(?:暂停|先停)(?:这个|那个|当前|一下)?")),
    (INTENT_CONTINUE, re.compile(r"^\s*(?:继续|接着做|接着改|接着|按刚才的?(?:要求|方案)?继续)")),
    (INTENT_RETRY, re.compile(r"^\s*(?:重试|重跑|再试一次)")),
)

# 明确指向上一轮目标的补充要求（v165 §3.3）。
_FOLLOWUP = re.compile(
    r"(刚才|上次|上一轮|前面|之前|刚才的|这个还没|这个问题|还没解决|按刚才|按之前|"
    r"再测试|再跑一次|再改|改成|把它|把这个|接着|继续)",
)
# 明确要另起一个目标。
_NEW_GOAL = re.compile(r"(新任务|新目标|换个|换一个|重新开始|重开|别管刚才|另外做|另做)")

# 讨论而不是派活（v165 §2.4：只讨论方案不能自动执行）。
_DISCUSS = re.compile(r"(能不能|可不可以|是否|要不要|值不值得|会不会|为什么|是什么|什么意思|"
                      r"你觉得|你认为|你怎么看|有什么(?:可以|需要)?(?:优化|改|改进|问题))")
_ACTION = re.compile(
    r"(改|修改|修复|重写|重构|实现|新增|添加|加一个|加上|删除|移除|替换|接入|升级|安装|"
    r"部署|打包|构建|编译|运行|执行|跑一下|测试|验证|排查|调试|优化|清理|整理|迁移|"
    r"提交|回滚|生成|创建|新建|写一个|做一个|写入|导出|转换|"
    r"refactor|fix|implement|build|deploy|migrate|test|debug|run|create|generate)",
    re.IGNORECASE,
)
_INSPECT = re.compile(r"(看一下|看看|检查一下|检查|分析|读一下|读读|搜一下|找一下|列出|统计|对比|排查)")
_BRAINSTORM = re.compile(r"(头脑风暴|发散|给几个方案|多给几个|有哪些思路|想想办法)")
_SCHEDULE = re.compile(r"(定时|每天|每周|每天几点|设个闹钟|到点)")
_WATCH = re.compile(r"(持续监控|盯着|文件(?:一)?变化|一直看着|监控这个)")
_REMEMBER = re.compile(r"^(?:记住|记一下|以后都|记住这个)")
_FORGET = re.compile(r"^(?:忘掉|别记了|删除这个习惯|忽略这个习惯)")
_CHITCHAT = re.compile(r"^\s*(?:你好|您好|hi|hello|hey|在吗|谢谢|多谢|感谢|thanks|ok|好的|收到|明白|嗯+)\s*[!！。.~～,，]*\s*$", re.IGNORECASE)


def _judgement(intent: str, reason: str, **kwargs) -> Judgement:
    control = kwargs.pop("control_action", intent if intent in CONTROL_ACTIONS else "")
    needs_task = kwargs.pop("needs_task", not bool(control))
    needs_execution = kwargs.pop(
        "needs_execution",
        bool(control) or intent in (INTENT_EXECUTE, INTENT_INSPECT, INTENT_BRAINSTORM),
    )
    return Judgement(
        intent=intent,
        reason=reason,
        control_action=control,
        needs_task=needs_task,
        needs_execution=needs_execution,
        **kwargs,
    )


def observe(
    message: str,
    *,
    has_active_goal: bool = False,
    active_task_status: str = "",
    scenario: str = "dev",
    automation_mode: str = "assist",
    message_id: str = "",
    goal_id: str = "",
    related_task_id: str = "",
) -> Judgement:
    """判一条用户消息，返回结构化结果。

    ``active_task_status`` 参与审批类控制词判定：没有任务在等审批时说「同意」
    只是一句附和，不是批准——否则一次闲聊就会去点一个不存在的审批卡。
    """
    text = str(message or "").strip()
    common = {
        "scenario": scenario,
        "automation_mode": automation_mode,
        "message_id": message_id,
        "goal_id": goal_id,
        "related_task_id": related_task_id,
    }
    if not text:
        return _judgement(INTENT_ANSWER, "空消息", needs_execution=False, **common)

    for intent, pattern in _EXACT_CONTROL:
        if not pattern.match(text):
            continue
        # 批准 / 拒绝只在真的有审批挂着时才成立。
        if intent in (INTENT_APPROVE, INTENT_REJECT) and active_task_status != "waiting_approval":
            return _judgement(INTENT_ANSWER, "没有待审批操作，按附和处理", needs_execution=False, **common)
        if intent in (INTENT_CONTINUE, INTENT_RETRY, INTENT_RESUME) and not has_active_goal:
            return _judgement(INTENT_ANSWER, "没有可继续的旧目标", needs_execution=False, confidence=0.7, **common)
        return _judgement(intent, f"控制词：{text[:20]}", confidence=0.96, **common)

    if _CHITCHAT.match(text):
        return _judgement(INTENT_ANSWER, "寒暄/附和", needs_execution=False, confidence=0.95, **common)

    for intent, pattern in _PREFIX_CONTROL:
        if not pattern.match(text):
            continue
        if intent == INTENT_CONTINUE and not has_active_goal:
            break
        # 「继续」后面还带着具体要求（继续，把按钮换成蓝色）不是催促，是补充：
        # 直接恢复旧一轮会把这句新要求丢掉，必须开新一轮、挂回同一目标。
        if intent == INTENT_CONTINUE and len(text) > 10:
            return _judgement(
                INTENT_CONTINUE, "沿用上一目标的补充要求",
                control_action="", needs_task=True, continues_goal=True,
                needs_execution=True, confidence=0.84, **common,
            )
        return _judgement(intent, f"控制句：{text[:24]}", confidence=0.92, **common)

    if _FORGET.search(text):
        return _judgement(INTENT_FORGET, "要求删除习惯", needs_execution=False, confidence=0.9, **common)
    if _REMEMBER.search(text):
        return _judgement(INTENT_REMEMBER, "要求记住偏好", needs_execution=False, confidence=0.9, **common)
    if _SCHEDULE.search(text):
        return _judgement(INTENT_SCHEDULE, "定时意图", confidence=0.85, **common)
    if _WATCH.search(text):
        return _judgement(INTENT_WATCH, "监控意图", confidence=0.85, **common)
    if _BRAINSTORM.search(text):
        return _judgement(INTENT_BRAINSTORM, "发散多个方案", confidence=0.9, **common)

    # 先判「只是讨论」：含动作词但句式是提问时，不能自动执行（v165 §2.4）。
    if _DISCUSS.search(text) and not re.search(r"(帮我|给我|直接|现在就|马上|然后|顺便)", text):
        intent = INTENT_INSPECT if re.search(r"(你觉得|你认为|哪里|有什么)", text) else INTENT_ANSWER
        return _judgement(
            intent, "用户只在讨论/征询，没有派活",
            needs_execution=intent == INTENT_INSPECT,
            confidence=0.86, **common,
        )

    if has_active_goal and _FOLLOWUP.search(text) and not _NEW_GOAL.search(text):
        return _judgement(
            INTENT_CONTINUE, "沿用上一目标的补充要求",
            control_action="", needs_task=True, continues_goal=True,
            needs_execution=True, confidence=0.82, **common,
        )
    if _INSPECT.search(text) and not _ACTION.search(text):
        return _judgement(INTENT_INSPECT, "只读调研", confidence=0.86, **common)
    if _ACTION.search(text):
        return _judgement(INTENT_EXECUTE, "明确的动手目标", confidence=0.9, **common)
    if len(text) <= 12 and re.search(r"[?？]$|^(什么|怎么|为什么|哪)", text):
        return _judgement(INTENT_ANSWER, "短问句", needs_execution=False, confidence=0.75, **common)
    # 一句自然语言默认就是目标（v165 Phase 1 验收）：不要求用户点「开始执行」。
    return _judgement(INTENT_EXECUTE, "自然语言目标", confidence=0.7, **common)


def execution_override(judgement: dict | None, quick: str | None) -> str | None:
    """把监督者判定折进执行侧的三分类预判。

    返回 ``None`` 表示不覆盖，否则是 ``act`` / ``advise``。只在规则预判为
    ``None``（拿不准）或 ``answer`` / ``advise`` 时生效：用户明明在问概念、
    规则已经判成闲聊时，监督者不会把它硬掰成改代码任务。

    典型场景是「这个问题还没解决」「再测试一下」——没有动作词，旧链路会判
    ``answer`` 直接答一句就结束，用户只能再催一次。监督者知道这句话挂在
    一个未完成的目标上，因此把它交回执行循环。
    """
    sup = judgement if isinstance(judgement, dict) else {}
    if not sup.get("needs_execution"):
        return None
    intent = str(sup.get("intent") or "")
    if not (sup.get("continues_goal") or intent in (INTENT_EXECUTE, INTENT_CONTINUE, INTENT_INSPECT, INTENT_BRAINSTORM)):
        return None
    if quick not in (None, INTENT_ANSWER, "advise"):
        return None
    # 返回的是执行侧 intent.py 的三分类值（act / advise），不是本模块的 execute。
    return "advise" if intent in (INTENT_INSPECT, INTENT_BRAINSTORM) else "act"


def wants_new_goal(message: str) -> bool:
    """这句话是不是明确要另起一个目标（v165 §3.4 目标冲突处理）。

    默认不另起：同一会话里的大多数补充都指向上一件事。只有用户明确说
    「新任务 / 换个 / 重新开始」才把旧目标归档——宁可多合并一次，也不要
    用户每说一句就丢一次上下文。
    """
    return bool(_NEW_GOAL.search(str(message or "")))

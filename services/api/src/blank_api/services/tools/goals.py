"""propose_goal: 把「这一轮到底要达成什么」交给用户确认（对齐 Claude 的 ProposeGoal）。

Claude Code 的 ProposeGoal 不是一个普通工具：它产出 ``{condition, askUser}``，
``askUser`` 为真时通过 AskUserQuestion 让用户确认，为假时直接设置会话目标。
这个区别必须保住——模型不能靠一句 ``ask_user: false`` 把目标偷偷写进会话，
那是用户从没点头的约束。

本仓库的落点：
- 目标条件存在 ``threads.session_goal``（会话级，跨轮保留）；
- ``ask_user`` 默认为真；只有用户在本轮明确说过"不用问"时模型才可以传 false；
- 确认走同一条问题中断通道，合成一张「会话目标」问题卡，不另开审批口子。
"""

from __future__ import annotations

from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail

CONDITION_CHARS = 500
ACCEPT_LABEL = "设为本次会话目标"
DECLINE_LABEL = "不用"

# 用户用自由文本作答时的口径：明确的否定才算拒绝，其余非空答案都当作
# 「同意，并把这句话作为修订后的目标」——用户重新写一遍目标本来就是在回答"是什么"。
_DECLINE_WORDS = frozenset(
    {
        "不用",
        "不用了",
        "不设置",
        "不需要",
        "不要",
        "否",
        "取消",
        "算了",
        "no",
        "n",
        "skip",
    }
)
_ACCEPT_WORDS = frozenset({"是", "好", "可以", "设置", "设", "同意", "ok", "yes", "y"})


def _as_bool(value: object) -> bool:
    return value is True or str(value).strip().lower() in ("1", "true", "yes", "on")


def normalize_proposal(arguments: dict) -> str:
    """取目标条件。兼容 condition / objective / goal 三种键，统一截到上限。"""
    for key in ("condition", "objective", "goal"):
        text = str(arguments.get(key) or "").strip()
        if text:
            return text[:CONDITION_CHARS]
    return ""


def wants_confirmation(arguments: dict) -> bool:
    """``ask_user`` 默认 true。只有显式的 false 才直接设置。"""
    raw = arguments.get("ask_user", arguments.get("askUser"))
    return True if raw is None else _as_bool(raw)


def validate_proposal(arguments: dict) -> str:
    raw = ""
    for key in ("condition", "objective", "goal"):
        candidate = str(arguments.get(key) or "").strip()
        if candidate:
            raw = candidate
            break
    if not raw:
        return "propose_goal 需要 condition：这一轮到底要达成什么。"
    if len(raw) > CONDITION_CHARS:
        return f"condition 超过 {CONDITION_CHARS} 字符，请把目标写短、写具体。"
    return ""


def goal_question(condition: str, qid: str) -> dict:
    """把目标提案变成一张普通问题卡。

    不另开一种中断类型：``_finalize`` 只认 ``kind == "question"``，多造一种
    "goal 提案"会让恢复值在两条门之间错位，表现为答案串到别的问题上。
    """
    return {
        "id": qid,
        "question": f"要把这条设为本次会话目标吗？\n{condition}",
        "header": "会话目标",
        "options": [
            {
                "label": ACCEPT_LABEL,
                "description": "之后每一轮都按这个目标约束执行",
                "recommended": False,
                "preview": "",
            },
            {
                "label": DECLINE_LABEL,
                "description": "只按当前这一轮的任务目标执行",
                "recommended": False,
                "preview": "",
            },
        ],
        # 目标不是"用户不答就替他定"的偏好：没有默认值，不答即视为不设置。
        "default": "",
        "multiSelect": False,
        "allow_free_text": True,
    }


def goal_decision(answer: str, condition: str) -> tuple[str, str, str]:
    """答案 → (accepted|declined, 最终条件, 理由)。

    - 选中"设为本次会话目标" → 用提案原文；
    - 选"不用"或明确否定 → 拒绝；
    - 其他自由文本 → 当作修订后的目标（用户重写了一遍），接受；
    - 没答 → 拒绝：目标没有默认值，替他设一个就是替他做主。
    """
    text = str(answer or "").strip()
    if not text:
        return "declined", "", "用户没有回答"
    lowered = text.lower()
    if text == ACCEPT_LABEL or lowered in _ACCEPT_WORDS:
        return "accepted", condition, "用户确认"
    if text in _DECLINE_WORDS or lowered in _DECLINE_WORDS:
        return "declined", "", "用户选择不设置"
    return "accepted", text[:CONDITION_CHARS], "用户改写后接受"


async def _propose_goal(context: ToolContext, arguments: dict) -> ToolOutcome:
    problem = validate_proposal(arguments)
    if problem:
        return fail("error", problem)
    condition = normalize_proposal(arguments)
    if wants_confirmation(arguments):
        # 与 request_user_input 同一条口径：没有提问门就不能假装用户点过头。
        return fail(
            "unanswered",
            "目标提案未送达用户：当前运行时没有可用的确认通道，请按当前任务目标继续。",
            output=f"待确认的会话目标：{condition}",
        )
    # 直接设置也必须在运行时落库；单测直接调 handler 时没有存储通道，不能谎称已设置。
    return fail("error", "当前运行时没有会话目标存储，无法直接设置会话目标。")


def goal_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="propose_goal",
            description=(
                "把「这次会话要达成什么」写成一个可判定的条件，交给用户确认（对齐 Claude 的 ProposeGoal）。"
                "当用户的请求背后有一个跨多轮的目标（例如「把这个项目整理成可发布状态」），"
                "而当前目标还不够具体、或你判断接下来会围绕它连着做很多轮时使用。\n"
                "condition 是**条件**不是任务清单：写成「什么算达成」的一句话（≤500 字），"
                "例如「发布包能安装、启动后能连上后端、三条核心路径都点验过」。\n"
                "ask_user 默认 true：合成一张问题卡让用户点头。只有用户在本轮明确说过"
                "「不用问 / 直接设」时才可以传 false——那是用户的授权，不是你的省事开关。"
                "用户确认后目标会跨轮保留，并在后续每一轮作为会话级约束注入；"
                "用户随时可以在界面上清除它。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "condition": {
                        "type": "string",
                        "description": "会话目标条件：什么算达成（≤500 字）",
                    },
                    "ask_user": {
                        "type": "boolean",
                        "description": "是否先让用户确认，默认 true；仅当用户明确要求不问时传 false",
                    },
                },
                "required": ["condition"],
            },
            risk_level="low",
            handler=_propose_goal,
        )
    ]

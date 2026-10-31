"""request_user_input: the agent asks instead of guessing.

Codex and Claude Code both expose a question tool that blocks the run and hands
control back to the human with a small set of options. We reuse the same
interrupt channel as approvals, so a question and an approval look identical to
the transport and to the UI — only the payload differs.

The handler itself never asks anything: the graph's question gate resolves the
answer and settles the call, which keeps the tool deterministic and testable.
"""

from __future__ import annotations

from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail

MAX_QUESTIONS = 4
MAX_OPTIONS = 6
QUESTION_CHARS = 500
HEADER_CHARS = 24
OPTION_CHARS = 120


def normalize_questions(arguments: dict) -> list[dict]:
    """Accept one question or a small batch; drop anything unusable.

    推荐项（``recommended``）与问题级 ``default`` 是同一种东西的两种写法：
    都表示「用户不答就按它走」。每题**最多留一个**——留两个等于没留，模型和界面
    都无法判断该用哪个。
    """
    raw = arguments.get("questions")
    if not isinstance(raw, list) or not raw:
        raw = [
            {
                "id": arguments.get("id"),
                "question": arguments.get("question"),
                "options": arguments.get("options"),
                "header": arguments.get("header"),
                "allow_free_text": arguments.get("allow_free_text"),
                "default": arguments.get("default"),
            }
        ]
    questions: list[dict] = []
    for index, item in enumerate(raw):
        if isinstance(item, str):
            item = {"question": item}
        if not isinstance(item, dict):
            continue
        text = str(item.get("question") or item.get("text") or "").strip()
        if not text:
            continue
        explicit = str(item.get("default") or "").strip()
        options: list[dict] = []
        picked = ""
        for option in item.get("options") or []:
            if isinstance(option, str):
                label, description, flagged = option, "", False
            elif isinstance(option, dict):
                label = str(option.get("label") or option.get("value") or "").strip()
                description = str(option.get("description") or "").strip()
                flagged = bool(option.get("recommended"))
            else:
                continue
            if not label:
                continue
            # 只认第一个：显式 default 优先，其次才是模型自己标的 recommended。
            is_default = False
            if not picked:
                if explicit:
                    is_default = label == explicit
                elif flagged:
                    is_default = True
            if is_default:
                picked = label
            options.append(
                {
                    "label": label[:OPTION_CHARS],
                    "description": description[:OPTION_CHARS],
                    "recommended": is_default,
                }
            )
        kept = options[:MAX_OPTIONS]
        if picked and picked not in {option["label"] for option in kept}:
            picked = ""
        questions.append(
            {
                "id": str(item.get("id") or f"q{index + 1}"),
                "question": text[:QUESTION_CHARS],
                "header": str(item.get("header") or "").strip()[:HEADER_CHARS],
                "options": kept,
                "default": picked,
                "allow_free_text": bool(item.get("allow_free_text", True)),
            }
        )
    return questions[:MAX_QUESTIONS]


def fill_defaults(
    questions: list[dict], answers: dict[str, str]
) -> tuple[dict[str, str], list[dict]]:
    """用户没答的**偏好题**按推荐项补上；事实题一个字都不补。

    「没答」有两种含义，方向相反：偏好题是「你看着办」，事实题是「这处留空」。
    把两者混成一种，要么用户回来发现事情卡死，要么服务器替他编了个电话号码——
    后者正是上一波刚堵掉的东西。

    返回 (补全后的答案, 代决定清单)。清单是要写进交付正文的：用了默认值却不说，
    用户会把它当成自己的选择。
    """
    filled = {key: str(value) for key, value in (answers or {}).items() if str(value).strip()}
    defaulted: list[dict] = []
    for question in questions or []:
        qid = str(question.get("id") or "")
        if not qid or qid in filled:
            continue
        value = str(question.get("default") or "").strip()
        if not value:
            continue
        filled[qid] = value
        defaulted.append(
            {
                "id": qid,
                "header": str(question.get("header") or ""),
                "question": str(question.get("question") or ""),
                "value": value,
            }
        )
    return filled, defaulted


def parse_answers(answer: object, questions: list[dict]) -> dict[str, str]:
    """Map whatever the transport gave us onto question ids.

    Accepts a plain string, a positional list, ``{"answers": {...}}`` or a dict
    keyed by question id — an approval card, a text box and a scripted client all
    end up in the same shape.
    """
    if answer is None:
        return {}
    if isinstance(answer, str):
        return {questions[0]["id"]: answer.strip()} if questions and answer.strip() else {}
    if isinstance(answer, list):
        values = [str(item).strip() for item in answer]
        return {
            question["id"]: value
            for question, value in zip(questions, values)
            if value
        }
    if isinstance(answer, dict):
        nested = answer.get("answers")
        if isinstance(nested, dict):
            return {
                str(key): str(value).strip()
                for key, value in nested.items()
                if str(value).strip()
            }
        if isinstance(nested, list):
            return parse_answers(nested, questions)
        for key in ("answer", "text", "value", "content"):
            value = answer.get(key)
            if isinstance(value, str) and value.strip():
                return {questions[0]["id"]: value.strip()} if questions else {}
        keyed = {
            question["id"]: str(answer[question["id"]]).strip()
            for question in questions
            if isinstance(answer.get(question["id"]), str) and str(answer[question["id"]]).strip()
        }
        if keyed:
            return keyed
    return {}


def render_answers(
    questions: list[dict], answers: dict[str, str], defaulted: object = ()
) -> str:
    """把答案渲染成模型能读的一段。

    ``defaulted`` 是被代答的问题 id 集合。标出来是要紧的：不标，模型会把
    「星河科技」当成用户的要求；标了，它知道这是自己按推荐项定的，写进交付时
    会说明。用户真答的那条绝不能标成默认值——那等于把用户的决定说成猜测。
    """
    assumed = {str(item) for item in (defaulted or ())}
    lines = ["【用户回答】"]
    for question in questions:
        qid = str(question.get("id") or "")
        value = answers.get(qid) or ""
        if value and qid in assumed:
            value = f"{value}（用户没答，按推荐默认值继续）"
        prefix = f"{question['header']}：" if question.get("header") else ""
        lines.append(f"问：{prefix}{question['question']}")
        lines.append(f"答：{value or '（用户未回答）'}")
    return "\n".join(lines)


async def _request_user_input(context: ToolContext, arguments: dict) -> ToolOutcome:
    questions = normalize_questions(arguments)
    if not questions:
        return fail("error", "request_user_input 至少需要一个有效问题")
    # 走到这里说明上游没有装配提问门（例如被单测直接调用）：不能假装用户答过。
    return fail(
        "unanswered",
        "问题未送达用户：当前运行时没有可用的提问通道，请自行做出合理假设并说明。",
        output="\n".join(f"待确认：{item['question']}" for item in questions),
    )


def ask_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="request_user_input",
            description=(
                "向用户提出 1-4 个问题并等待回答。交付物依赖只有用户才知道的信息时"
                "（名字、给谁看、要哪些栏目、联系方式、接哪个服务、放哪个目录）用它问清，"
                "不要拿占位值顶替——猜错的代价通常是整段重做。"
                "只问「要什么」，不问「怎么做」：技术方案与实现细节自己定，"
                "读一遍工作区能查到的也自己查；显而易见的确认不要问。\n"
                "偏好类问题请给推荐项：把最合适的那一项标 recommended=true（每题最多一个），"
                "用户不答就按它继续，你会被记一笔「替你定的」并在交付里说明。"
                "事实类问题（真实电话、邮箱、具体地址）不要标 recommended——"
                "它们没有默认值，用户不答的含义是「这处留空」。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "单个问题的正文"},
                    "header": {"type": "string", "description": "问题的短标签，最多 12 个汉字"},
                    "options": {
                        "type": "array",
                        "description": "可选项；给出 2-4 个时用户点一下就能回答",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "description": {"type": "string"},
                                "recommended": {
                                    "type": "boolean",
                                    "description": "偏好题里最合适的那一项；用户不答就按它继续。每题最多一个",
                                },
                            },
                            "required": ["label"],
                        },
                    },
                    "default": {
                        "type": "string",
                        "description": "问题级默认值，写某个选项的 label；与 recommended 等价",
                    },
                    "allow_free_text": {"type": "boolean", "description": "是否允许自由输入，默认 true"},
                    "questions": {
                        "type": "array",
                        "description": "批量提问（最多 4 个），与 question 二选一",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "question": {"type": "string"},
                                "header": {"type": "string"},
                                "options": {"type": "array", "items": {"type": "object"}},
                                "allow_free_text": {"type": "boolean"},
                            },
                            "required": ["question"],
                        },
                    },
                },
            },
            risk_level="low",
            handler=_request_user_input,
            timeout_seconds=15.0,
        )
    ]

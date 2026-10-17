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
    """Accept one question or a small batch; drop anything unusable."""
    raw = arguments.get("questions")
    if not isinstance(raw, list) or not raw:
        raw = [
            {
                "question": arguments.get("question"),
                "options": arguments.get("options"),
                "header": arguments.get("header"),
                "allow_free_text": arguments.get("allow_free_text"),
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
        options: list[dict] = []
        for option in item.get("options") or []:
            if isinstance(option, str):
                label, description = option, ""
            elif isinstance(option, dict):
                label = str(option.get("label") or option.get("value") or "").strip()
                description = str(option.get("description") or "").strip()
            else:
                continue
            if label:
                options.append(
                    {"label": label[:OPTION_CHARS], "description": description[:OPTION_CHARS]}
                )
        questions.append(
            {
                "id": str(item.get("id") or f"q{index + 1}"),
                "question": text[:QUESTION_CHARS],
                "header": str(item.get("header") or "").strip()[:HEADER_CHARS],
                "options": options[:MAX_OPTIONS],
                "allow_free_text": bool(item.get("allow_free_text", True)),
            }
        )
    return questions[:MAX_QUESTIONS]


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


def render_answers(questions: list[dict], answers: dict[str, str]) -> str:
    lines = ["【用户回答】"]
    for question in questions:
        value = answers.get(question["id"]) or "（用户未回答）"
        prefix = f"{question['header']}：" if question.get("header") else ""
        lines.append(f"问：{prefix}{question['question']}")
        lines.append(f"答：{value}")
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
                "向用户提出 1-4 个问题并等待回答，用于目标不清、方案有分歧或需要用户决策时。"
                "不要用它确认显而易见的事；能自己查证的就先查证。"
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
                            },
                            "required": ["label"],
                        },
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

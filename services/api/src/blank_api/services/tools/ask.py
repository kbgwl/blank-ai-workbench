"""request_user_input: the agent asks instead of guessing.

Codex and Claude Code both expose a question tool that blocks the run and hands
control back to the human with a small set of options. We reuse the same
interrupt channel as approvals, so a question and an approval look identical to
the transport and to the UI — only the payload differs.

The handler itself never asks anything: the graph's question gate resolves the
answer and settles the call, which keeps the tool deterministic and testable.

契约对齐 Claude Code 的 AskUserQuestion（2.1.x）：
- questions 1-4；每题 options 2-4（我们仍接受 0-1 项，见 README 偏离项）；
- header ≤12；选项可带 preview（一段 Markdown/HTML 片段），multiSelect 题不支持 preview；
- 答案形状 ``{answers: {问题文本: "a, b"}, response?, annotations?}``。
"""

from __future__ import annotations

import re

from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail

MAX_QUESTIONS = 4
# Claude 的 AskUserQuestion 是 2-4 个选项。上界必须卡住：选项越多，用户越难选，
# 模型也越容易把"选项"写成一段话。下界不卡——事实题可以没有选项，既有用例
# 与真实调用里都有 0-1 项的形状。
MAX_OPTIONS = 4
QUESTION_CHARS = 500
HEADER_CHARS = 12
OPTION_CHARS = 120
# preview 会原样交给前端渲染。上限不是审美：一段几万字的"预览"进事件与检查点，
# 每一步都在为它付存储与传输成本。
PREVIEW_CHARS = 4000

_TAG = re.compile(r"<([a-zA-Z][a-zA-Z0-9-]*)\b")
# 预览只接受**片段**：整页文档会把宿主的页面结构顶掉，脚本/样式则直接扩宽了
# 工具作者能做的事。这不是"内容审查"，是让预览停在"展示一段信息"的范围内。
_FORBIDDEN_PREVIEW_TAGS = frozenset(
    {
        "html",
        "head",
        "body",
        "script",
        "style",
        "iframe",
        "object",
        "embed",
        "link",
        "meta",
        "base",
        "form",
    }
)


def _as_bool(value: object) -> bool:
    return value is True or str(value).strip().lower() in ("1", "true", "yes", "on")


def _value_text(value: object) -> str:
    """答案值 → 一段文本。多选答案可能以列表/逗号串两种形状到达。

    Claude 的回执是逗号分隔字符串；HTTP 客户端与脚本更自然的是列表。两种都收，
    落账时统一成同一个形状，事件里就不会出现"同一个答案两种写法"。
    """
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def preview_problem(text: str) -> str:
    """返回 preview 不合格的原因；合格时返回空串。

    `validate_model_arguments` 用它拒绝非法调用；`normalize_questions` 用它丢弃
    单条坏预览而不是整题作废——展示层坏一段，不该让用户连问题都答不了。
    """
    body = str(text or "").strip()
    if not body:
        return "preview 为空"
    if len(body) > PREVIEW_CHARS:
        return f"preview 超过 {PREVIEW_CHARS} 字符"
    lowered = body.lower()
    if "<!doctype" in lowered:
        return "preview 不能带 <!doctype>"
    if "<!--" in body:
        return "preview 不能带 HTML 注释"
    if body.startswith("```"):
        return "preview 不要用 Markdown 代码围栏包起来"
    tags = {name.lower() for name in _TAG.findall(body)}
    if not tags:
        return "preview 至少要包含一个 HTML 标签"
    forbidden = sorted(tags & _FORBIDDEN_PREVIEW_TAGS)
    if forbidden:
        return f"preview 不能使用 {', '.join(forbidden)} 这些标签"
    return ""


def clean_preview(value: object, *, multi_select: bool) -> str:
    if multi_select:
        # Claude 明确不支持多选 + 预览：选多个时预览区该显示哪一个没有定义。
        return ""
    body = str(value or "").strip()
    return body if body and not preview_problem(body) else ""


def _raw_questions(arguments: dict) -> list:
    raw = arguments.get("questions")
    if isinstance(raw, list) and raw:
        return raw
    return [
        {
            "id": arguments.get("id"),
            "question": arguments.get("question"),
            "options": arguments.get("options"),
            "header": arguments.get("header"),
            "allow_free_text": arguments.get("allow_free_text"),
            "default": arguments.get("default"),
            "multiSelect": arguments.get("multiSelect", arguments.get("multi_select")),
        }
    ]


def _default_labels(item: dict, explicit: str) -> list[str]:
    """显式 default → 要标成推荐项的 label 列表。

    单选仍只认一个（第一个匹配）；多选的默认值允许逗号分隔多个——"都按推荐走"
    在多选题里必须能把每一项都标出来，否则用户不答时只会得到半个答案。
    """
    if not explicit:
        return []
    if _as_bool(item.get("multiSelect", item.get("multi_select"))):
        return [part.strip() for part in explicit.split(",") if part.strip()]
    return [explicit]


def normalize_questions(arguments: dict) -> list[dict]:
    """Accept one question or a small batch; drop anything unusable.

    推荐项（``recommended``）与问题级 ``default`` 是同一种东西的两种写法：
    都表示「用户不答就按它走」。单选每题**最多留一个**——留两个等于没留，模型和界面
    都无法判断该用哪个；多选则允许把每个推荐项都标出来。
    """
    questions: list[dict] = []
    used_ids: set[str] = set()
    for index, item in enumerate(_raw_questions(arguments)):
        if isinstance(item, str):
            item = {"question": item}
        if not isinstance(item, dict):
            continue
        text = str(item.get("question") or item.get("text") or "").strip()
        if not text:
            continue
        multi = _as_bool(item.get("multiSelect", item.get("multi_select")))
        explicit = str(item.get("default") or "").strip()
        allowed = set(_default_labels(item, explicit))
        options: list[dict] = []
        picked: list[str] = []
        for option in item.get("options") or []:
            if isinstance(option, str):
                label, description, flagged, preview = option, "", False, ""
            elif isinstance(option, dict):
                label = str(option.get("label") or option.get("value") or "").strip()
                description = str(option.get("description") or "").strip()
                flagged = _as_bool(option.get("recommended"))
                preview = clean_preview(option.get("preview"), multi_select=multi)
            else:
                continue
            if not label:
                continue
            # 显式 default 优先；没有显式值时，单选只认第一个 recommended，
            # 多选则把所有 recommended 都收下。
            is_default = False
            if explicit:
                is_default = label in allowed
            elif flagged and (multi or not picked):
                is_default = True
            if is_default:
                picked.append(label)
            options.append(
                {
                    "label": label[:OPTION_CHARS],
                    "description": description[:OPTION_CHARS],
                    "recommended": is_default,
                    "preview": preview,
                }
            )
        kept = options[:MAX_OPTIONS]
        kept_labels = {option["label"] for option in kept}
        picked = [label for label in picked if label in kept_labels]
        if multi:
            for option in kept:
                option["recommended"] = option["label"] in picked
        else:
            picked = picked[:1]
            for option in kept:
                option["recommended"] = bool(picked) and option["label"] == picked[0]
        qid = str(item.get("id") or f"q{index + 1}")
        base = qid
        suffix = 2
        while qid in used_ids:
            qid = f"{base}-{suffix}"
            suffix += 1
        used_ids.add(qid)
        questions.append(
            {
                "id": qid,
                "question": text[:QUESTION_CHARS],
                "header": str(item.get("header") or "").strip()[:HEADER_CHARS],
                "options": kept,
                "default": ", ".join(picked),
                # Claude 把「这道题会不会收多个答案」放在问题本身上；回执还要原样带回，
                # 前端才能决定画单选还是多选。
                "multiSelect": multi,
                "allow_free_text": _as_bool(item.get("allow_free_text", True)),
            }
        )
    return questions[:MAX_QUESTIONS]


def validate_model_arguments(arguments: dict) -> str:
    """模型入参的硬校验：不合格就拒掉这次调用，并告诉它怎么改。

    只卡 Claude 契约的**上界与结构**，不卡下界（2-4 个选项、header 必填）：
    事实题没有选项、单选项的确认题都是真实存在的形状，强行补齐反而逼模型编选项。
    下界差异记在 README 偏离项里，而不是用校验把它藏起来。
    """
    raw = _raw_questions(arguments)
    if not raw or (len(raw) == 1 and not isinstance(raw[0], (dict, str))):
        return "request_user_input 需要 question 或 questions。"
    if len(raw) > MAX_QUESTIONS:
        return f"一次最多问 {MAX_QUESTIONS} 个问题，收到 {len(raw)} 个；请合并或拆成两轮。"
    for index, item in enumerate(raw):
        label = f"第 {index + 1} 题"
        if isinstance(item, str):
            item = {"question": item}
        if not isinstance(item, dict):
            return f"{label}不是对象。"
        text = str(item.get("question") or item.get("text") or "").strip()
        if not text:
            return f"{label}缺少 question 正文。"
        if len(text) > QUESTION_CHARS:
            return f"{label}的 question 超过 {QUESTION_CHARS} 字符，请精简。"
        header = str(item.get("header") or "").strip()
        if len(header) > HEADER_CHARS:
            return f"{label}的 header 超过 {HEADER_CHARS} 字符，请改短。"
        multi = _as_bool(item.get("multiSelect", item.get("multi_select")))
        options = item.get("options")
        if options is None:
            options = []
        if not isinstance(options, list):
            return f"{label}的 options 必须是数组。"
        if len(options) > MAX_OPTIONS:
            return (
                f"{label}有 {len(options)} 个选项，超过上限 {MAX_OPTIONS}；"
                "请合并到 4 个以内，把其余内容放进 description。"
            )
        labels: set[str] = set()
        has_preview = False
        for position, option in enumerate(options):
            if isinstance(option, str):
                if not option.strip():
                    return f"{label}的第 {position + 1} 个选项为空。"
                labels.add(option.strip())
                continue
            if not isinstance(option, dict):
                return f"{label}的第 {position + 1} 个选项不是对象。"
            option_label = str(option.get("label") or option.get("value") or "").strip()
            if not option_label:
                return f"{label}的第 {position + 1} 个选项缺少 label。"
            labels.add(option_label)
            if option.get("preview") is not None:
                problem = preview_problem(str(option.get("preview") or ""))
                if problem:
                    return f"{label}的选项「{option_label}」{problem}。"
                has_preview = True
        if multi and has_preview:
            return f"{label}是多选题，多选不支持 preview；请去掉 preview 或改成单选。"
        explicit = str(item.get("default") or "").strip()
        if explicit and labels:
            missing = [part for part in _default_labels(item, explicit) if part not in labels]
            if missing:
                return (
                    f"{label}的 default 指向不存在的选项：{'、'.join(missing)}；"
                    "default 必须写某个选项的 label 原文。"
                )
    return ""


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
    filled = {key: _value_text(value) for key, value in (answers or {}).items() if _value_text(value)}
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


def _question_key_map(questions: list[dict]) -> dict[str, str]:
    """问题文本 / header → id。Claude 的回执按问题文本做键，我们的内部账按 id。"""
    mapping: dict[str, str] = {}
    for question in questions or ():
        qid = str(question.get("id") or "")
        if not qid:
            continue
        for key in (question.get("question"), question.get("header")):
            text = str(key or "").strip()
            if text:
                mapping.setdefault(text, qid)
    return mapping


def _mapped_answers(raw: dict, questions: list[dict]) -> dict[str, str]:
    """把任意键的答案字典落回问题 id。认 id、问题原文与 header 三种键。"""
    by_text = _question_key_map(questions)
    ids = {str(question.get("id") or "") for question in questions or ()}
    mapped: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip()
        qid = name if name in ids else by_text.get(name, "")
        if not qid:
            continue
        text = _value_text(value)
        if text:
            mapped[qid] = text
    return mapped


def parse_answers(answer: object, questions: list[dict]) -> dict[str, str]:
    """Map whatever the transport gave us onto question ids.

    Accepts a plain string, a positional list, ``{"answers": {...}}`` or a dict
    keyed by question id / question text / header — an approval card, a text box
    and a scripted client all end up in the same shape.
    """
    if answer is None:
        return {}
    if isinstance(answer, str):
        return {questions[0]["id"]: answer.strip()} if questions and answer.strip() else {}
    if isinstance(answer, list):
        return {
            question["id"]: _value_text(value)
            for question, value in zip(questions, answer)
            if _value_text(value)
        }
    if isinstance(answer, dict):
        nested = answer.get("answers")
        if isinstance(nested, dict):
            return _mapped_answers(nested, questions)
        if isinstance(nested, list):
            return parse_answers(nested, questions)
        for key in ("answer", "text", "value", "content"):
            value = answer.get(key)
            if isinstance(value, str) and value.strip():
                return {questions[0]["id"]: value.strip()} if questions else {}
        return _mapped_answers(answer, questions)
    return {}


def parse_answer_meta(
    answer: object, questions: list[dict]
) -> tuple[dict[str, str], str, dict[str, dict]]:
    """解析答案 + 自由文本 + 注解（Claude 的 response / annotations）。

    注解按问题 id 落：``{qid: {"preview": "...", "notes": "..."}}``。preview 只用于
    展示（用户选中时看到的片段），notes 是用户自己写的补充；两者分开存，渲染时
    只把 notes 交给模型——把预览再贴回模型是白付 token。
    """
    answers = parse_answers(answer, questions)
    response = ""
    annotations: dict[str, dict] = {}
    if not isinstance(answer, dict):
        return answers, response, annotations
    raw_response = answer.get("response")
    if isinstance(raw_response, str):
        response = raw_response.strip()[:4000]
    raw_annotations = answer.get("annotations")
    if isinstance(raw_annotations, dict):
        by_text = _question_key_map(questions)
        ids = {str(question.get("id") or "") for question in questions or ()}
        for key, value in raw_annotations.items():
            if not isinstance(value, dict):
                continue
            name = str(key).strip()
            qid = name if name in ids else by_text.get(name, "")
            if not qid:
                continue
            item: dict[str, str] = {}
            preview = str(value.get("preview") or "").strip()
            notes = str(value.get("notes") or "").strip()
            if preview:
                item["preview"] = preview[:PREVIEW_CHARS]
            if notes:
                item["notes"] = notes[:4000]
            if item:
                annotations[qid] = item
    return answers, response, annotations


def render_answers(
    questions: list[dict],
    answers: dict[str, str],
    defaulted: object = (),
    *,
    response: str = "",
    annotations: dict[str, dict] | None = None,
) -> str:
    """把答案渲染成模型能读的一段。

    ``defaulted`` 是被代答的问题 id（兼容旧的 id 列表与新的 {id, ...} 字典）。
    标出来是要紧的：不标，模型会把「星河科技」当成用户的要求；标了，它知道这是
    自己按推荐项定的，写进交付时会说明。用户真答的那条绝不能标成默认值——
    那等于把用户的决定说成猜测。
    """
    assumed: set[str] = set()
    for item in defaulted or ():
        if isinstance(item, dict):
            assumed.add(str(item.get("id") or ""))
        else:
            assumed.add(str(item))
    notes = annotations or {}
    lines = ["【用户回答】"]
    for question in questions:
        qid = str(question.get("id") or "")
        value = _value_text(answers.get(qid))
        if value and qid in assumed:
            value = f"{value}（用户没答，按推荐默认值继续）"
        prefix = f"{question['header']}：" if question.get("header") else ""
        lines.append(f"问：{prefix}{question['question']}")
        lines.append(f"答：{value or '（用户未回答）'}")
        note = str((notes.get(qid) or {}).get("notes") or "").strip()
        if note:
            lines.append(f"备注：{note}")
    if response:
        lines.append(f"补充：{response}")
    return "\n".join(lines)


async def _request_user_input(context: ToolContext, arguments: dict) -> ToolOutcome:
    problem = validate_model_arguments(arguments)
    if problem:
        return fail("error", problem)
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
                "向用户提出 1-4 个问题并等待回答（对齐 Claude AskUserQuestion）。"
                "交付物依赖只有用户才知道的信息（名字、给谁看、栏目、联系方式、接哪个服务、"
                "放哪个目录）时用它问清，不要拿占位值顶替。只问「要什么」，不问「怎么做」；"
                "能自己查、自己定的不要问。\n"
                "每题给 header（≤12 字）与 2-4 个选项；选项 = label + description，"
                "需要示意/对比时给 preview（HTML/Markdown 片段，不要整页文档、脚本或样式）。"
                "多选用 multiSelect=true（此时不要 preview）。\n"
                "偏好题给推荐项 recommended=true（单选每题最多一个，多选可多个）；"
                "事实题（电话、邮箱、地址）不要标 recommended。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "单个问题的正文"},
                    "header": {"type": "string", "description": "问题的短标签，最多 12 个字符"},
                    "multiSelect": {"type": "boolean", "description": "true 时可选多个；多选不支持 preview"},
                    "options": {
                        "type": "array",
                        "description": "2-4 个选项时用户点一下就能回答",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "description": {"type": "string"},
                                "preview": {
                                    "type": "string",
                                    "description": "选中时展示的 HTML/Markdown 片段；不要整页文档或脚本样式；多选不支持",
                                },
                                "recommended": {
                                    "type": "boolean",
                                    "description": "偏好题最合适的一项；用户不答就按它继续；单选每题最多一个",
                                },
                            },
                            "required": ["label"],
                        },
                    },
                    "default": {
                        "type": "string",
                        "description": "问题级默认值，写选项 label；多选用逗号分隔",
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
                                "multiSelect": {"type": "boolean"},
                                "options": {"type": "array", "items": {"type": "object"}},
                                "default": {"type": "string"},
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

"""#147：两类未知，处理方式必须相反。

Codex plan 模板里那条最值得抄的不是"多问"，而是**先分类**：

1. 查得到的（discoverable）：自己去查，不许拿去问人；
2. 偏好/取舍（preference）：早点问，给推荐项，**不答就按推荐项继续，并记成一次假设**。

我们原来是一整块"先问再干"，两个毛病：

- 会把能从文件里读出来的事拿去问人（用户点一次还要骂一句"这不就在文件里"）；
- 真该问的偏好，用户没答（关掉卡片/超时）就**卡死**——答案是空的，模型要么干等，
  要么自己拍一个却不说是自己拍的。

这一组钉住的是第 2 条落到代码上的样子：**偏好问题有默认值，事实问题没有**。
事实问题（真实电话）如果也给了默认值，那等于服务器替用户编号码——正是上一波刚堵掉的东西。
"""

from __future__ import annotations

import json
import uuid

import pytest

from blank_api.db import get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent import graph
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import (
    AgentSession,
    SYSTEM_PROMPT,
    _with_delivery_notes,
    execute_action_node,
)
from blank_api.services.agent.unknowns import facts, questions_for
from blank_api.services.tools.ask import (
    ask_tools,
    fill_defaults,
    normalize_questions,
    render_answers,
)
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome
from blank_api.services.tools.gateway import ToolGateway

TASK_ID = "task-kinds-1"


# --------------------------------------------------------------------------- #
# 1. 提示词：两类必须分开写，且排在做事的规则前面
# --------------------------------------------------------------------------- #
def test_the_prompt_separates_the_two_kinds_of_unknowns() -> None:
    assert "先问再干" in SYSTEM_PROMPT
    # 两类各自的关键词都在，且不是同一句话里含糊带过。
    assert "能查到的" in SYSTEM_PROMPT
    assert "只有用户才知道的" in SYSTEM_PROMPT
    # 分类段必须排在「做事规则」之前：靠后的规则在长上下文里会被执行规则盖掉。
    assert SYSTEM_PROMPT.index("能查到的") < SYSTEM_PROMPT.index("做事规则")
    # 推荐项是"不被答也能往下走"的机制，提示词必须说清它、也要说清什么时候不许给。
    assert "recommended" in SYSTEM_PROMPT
    assert SYSTEM_PROMPT.index("推荐项", SYSTEM_PROMPT.index("recommended")) > 0


def test_the_tool_description_tells_the_model_what_a_default_is_for() -> None:
    description = ask_tools()[0].description or ""
    assert "recommended" in description
    # 反过来的那半句更重要：事实类不许给默认值。
    assert "不要标 recommended" in description or "不要标" in description


# --------------------------------------------------------------------------- #
# 2. 问题结构：推荐项只留一个
# --------------------------------------------------------------------------- #
def test_at_most_one_option_is_kept_as_the_default() -> None:
    questions = normalize_questions(
        {
            "questions": [
                {
                    "id": "style",
                    "question": "首页走哪种风格？",
                    "options": [
                        {"label": "简约", "recommended": True},
                        {"label": "商务", "recommended": True},
                        {"label": "活泼", "recommended": True},
                    ],
                }
            ]
        }
    )
    assert questions[0]["default"] == "简约"
    assert [item["recommended"] for item in questions[0]["options"]] == [True, False, False]


def test_a_question_level_default_is_the_same_thing() -> None:
    questions = normalize_questions(
        {
            "question": "放哪个目录？",
            "options": ["站点根目录", "docs/"],
            "default": "docs/",
        }
    )
    assert questions[0]["default"] == "docs/"
    assert [item["recommended"] for item in questions[0]["options"]] == [False, True]


def test_no_recommended_option_means_no_default() -> None:
    questions = normalize_questions(
        {"question": "公司真实电话是多少？", "options": ["我填一个", "先留空"]}
    )
    assert questions[0]["default"] == ""


# --------------------------------------------------------------------------- #
# 3. 兜底：没答的偏好按推荐走；没答的事实仍然空着
# --------------------------------------------------------------------------- #
def test_an_unanswered_preference_falls_back_to_the_recommendation() -> None:
    questions = normalize_questions(
        {
            "questions": [
                {
                    "id": "style",
                    "header": "风格",
                    "question": "首页走哪种风格？",
                    "options": [{"label": "简约", "recommended": True}, "商务"],
                },
                {"id": "phone", "header": "电话", "question": "真实电话？", "options": ["我填一个"]},
            ]
        }
    )

    answers, defaulted = fill_defaults(questions, {})

    # 偏好题被填了默认值，并且记了一笔"替你定的"。
    assert answers["style"] == "简约"
    assert [item["id"] for item in defaulted] == ["style"]
    assert defaulted[0]["value"] == "简约"
    # 事实题一个字都没填：不答的含义是"这处留空"，不是"你自己编一个"。
    assert "phone" not in answers


def test_an_answer_from_the_user_beats_the_recommendation() -> None:
    questions = normalize_questions(
        {"id": "style", "question": "风格？", "options": ["简约", "商务"], "default": "简约"}
    )
    answers, defaulted = fill_defaults(questions, {"style": "商务"})
    assert answers == {"style": "商务"}
    assert defaulted == []


def test_the_fact_gate_never_offers_a_default() -> None:
    """闸门问的是"这个号码从哪来"，不是偏好——给它默认值等于服务器替用户编号码。"""
    questions = questions_for(facts("138-1234-5678"), start=1)
    assert questions
    assert all(not question.get("default") for question in questions)
    assert all(
        not any(option.get("recommended") for option in question["options"])
        for question in questions
    )
    answers, defaulted = fill_defaults(questions, {})
    assert answers == {}
    assert defaulted == []


def test_the_model_is_told_which_answers_it_made_up() -> None:
    questions = normalize_questions(
        {"id": "style", "header": "风格", "question": "风格？", "options": ["简约"], "default": "简约"}
    )
    text = render_answers(questions, {"style": "简约"}, {"style"})
    assert "用户没答，按推荐默认值继续" in text
    # 用户真答的不能被标成默认值，否则模型会把用户的要求当成自己的猜测。
    assert "用户没答" not in render_answers(questions, {"style": "简约"}, set())


# --------------------------------------------------------------------------- #
# 4. 交付：代决定必须说出来
# --------------------------------------------------------------------------- #
def test_the_delivery_says_what_was_decided_for_the_user() -> None:
    from types import SimpleNamespace as NS

    session = NS(assumptions=[{"id": "style", "header": "风格", "value": "简约"}],
                 recorded_questions={"call_1"}, asked_facts=set())
    text = _with_delivery_notes("首页做好了", session, {"messages": []})
    assert "首页做好了" in text
    assert "替你定的" in text
    assert "风格：简约" in text

    # 没代决定就一个字都不加：这句话只有在真发生时才该出现。
    none_made = NS(assumptions=[], recorded_questions=set(), asked_facts=set())
    assert _with_delivery_notes("首页做好了", none_made, {"messages": []}) == "首页做好了"


# --------------------------------------------------------------------------- #
# 5. 节点级：卡片被关掉之后，任务继续走下去而不是卡死
# --------------------------------------------------------------------------- #
def _schema() -> dict:
    return {"type": "object", "properties": {}, "additionalProperties": False}


class Counter:
    def __init__(self) -> None:
        self.calls: list[dict] = []


def _write_tool(counter: Counter) -> ToolDefinition:
    async def handler(context: ToolContext, arguments: dict) -> ToolOutcome:
        counter.calls.append(dict(arguments))
        return ToolOutcome(ok=True, output="ok", data={"path": arguments.get("path", "")})

    return ToolDefinition(
        name="write_file",
        description="写文件",
        input_schema=_schema(),
        risk_level="low",
        handler=handler,
        timeout_seconds=5.0,
    )


def _ask_call() -> dict:
    return {
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {
            "name": "request_user_input",
            "arguments": json.dumps(
                {
                    "questions": [
                        {
                            "id": "name",
                            "header": "公司名",
                            "question": "公司叫什么名字？",
                            "options": [
                                {"label": "星河科技", "recommended": True},
                                {"label": "我另给一个"},
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        },
    }


def _write_call(content: str) -> dict:
    return {
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {
            "name": "write_file",
            "arguments": json.dumps({"path": "index.html", "content": content}, ensure_ascii=False),
        },
    }


async def _seed_task() -> None:
    async with get_session_factory()() as session:
        session.add(Project(id="project-kinds", name="两类未知"))
        session.add(
            Task(
                id=TASK_ID,
                project_id="project-kinds",
                title="做个官网",
                goal="做个官网",
                max_steps=10,
            )
        )
        await session.commit()


@pytest.fixture
async def seeded():
    from blank_api.db import Base, get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await _seed_task()


def _session(tmp_path, tools: list[ToolDefinition]) -> AgentSession:
    session = AgentSession(
        adapter=None,  # type: ignore[arg-type]  # 本用例不走到模型调用
        gateway=ToolGateway(tools),
        context=ToolContext(
            root=tmp_path,
            task_id=TASK_ID,
            session_factory=get_session_factory(),
            approval_policy=lambda *_: True,
        ),
        recorder=EventRecorder(get_session_factory(), TASK_ID),
        session_factory=get_session_factory(),
        provider_id="provider-1",
        provider_name="mock",
        goal="做个公司官网",
        max_steps=10,
    )

    async def fake_ask(state, prompt, node, *, messages=None):
        return {
            "text": "继续",
            "tool_calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "calls": 1},
            "messages": list(messages or []),
        }

    session.ask = fake_ask  # type: ignore[method-assign]
    return session


def _state(pending: list[dict]) -> dict:
    return {
        "messages": [{"role": "user", "content": "做个公司官网"}],
        "steps": 0,
        "pending": pending,
    }


async def _events() -> list[AgentEvent]:
    from sqlalchemy import select

    async with get_session_factory()() as session:
        rows = await session.execute(select(AgentEvent).order_by(AgentEvent.seq))
        return list(rows.scalars())


async def test_a_dismissed_question_does_not_stall_the_run(seeded, tmp_path, monkeypatch) -> None:
    """用户直接把卡片关掉（interrupt 返回 None）：按推荐项继续，并记下"我替你定的"。"""
    counter = Counter()
    session = _session(tmp_path, [_write_tool(counter), *ask_tools()])
    # None 与真实超时/关卡片同形：原来这里会让 answers 变成 {}，然后整轮停在这一步。
    monkeypatch.setattr(graph, "interrupt", lambda payload: None)

    result = await execute_action_node(_state([_ask_call()]), session)

    assert session.assumptions == [
        {"id": "name", "header": "公司名", "question": "公司叫什么名字？", "value": "星河科技"}
    ]
    # 模型看到的是"用户没答、按推荐继续"，不是"用户要求星河科技"。
    assert "用户没答，按推荐默认值继续" in json.dumps(result["tool_results"], ensure_ascii=False)

    events = await _events()
    answered = [event for event in events if event.type == "question.answered"]
    assert answered, [event.type for event in events]
    assert answered[0].payload["answered"] == 0
    assert answered[0].payload["defaulted"][0]["value"] == "星河科技"

    # 下一步照常执行：卡住的那个问题不再挡路。
    monkeypatch.setattr(graph, "interrupt", lambda payload: pytest.fail("不该再问"))
    await execute_action_node(_state([_write_call("星河科技 首页")]), session)
    assert len(counter.calls) == 1


async def test_a_dismissed_question_without_a_default_still_blocks_the_value(
    seeded, tmp_path, monkeypatch
) -> None:
    """模型自己问"真实电话是多少"、又没有推荐项：用户不答，值仍然不许编。

    这条是两条线交汇处：偏好题可以替用户默认，事实题不行——不答就是留空。
    """
    counter = Counter()
    session = _session(tmp_path, [_write_tool(counter), *ask_tools()])
    monkeypatch.setattr(graph, "interrupt", lambda payload: None)

    ask = {
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {
            "name": "request_user_input",
            "arguments": json.dumps(
                {
                    "questions": [
                        {
                            "id": "phone",
                            "header": "电话",
                            "question": "公司真实电话是多少？",
                            "options": ["我填一个", "先留空"],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        },
    }
    await execute_action_node(_state([ask]), session)
    assert session.assumptions == []

    # 它接着编了一个号码 → 被事实闸门拦下，工具不执行。
    await execute_action_node(_state([_write_call("电话 137-0000-9999")]), session)
    assert counter.calls == []


# --------------------------------------------------------------------------- #
# 6. 答案接口的判据：有推荐项才允许空答案
# --------------------------------------------------------------------------- #
def test_only_recommended_questions_may_be_skipped() -> None:
    from blank_api.services.agent.evidence import questions_can_default

    assert questions_can_default(
        [{"id": "a", "options": [{"label": "简约", "recommended": True}]}]
    )
    # 事实题：没有推荐项 → 不许跳过（否则等于服务器替用户编号码）。
    assert not questions_can_default([{"id": "a", "options": [{"label": "我填一个"}]}])
    # 老数据与纯文本选项：没有字段就是没有推荐项，不能猜。
    assert not questions_can_default([{"id": "a", "options": ["甲", "乙"]}])
    assert not questions_can_default([])
    assert not questions_can_default(None)


async def test_one_answered_question_is_enough_for_the_card_to_count_as_answered(
    seeded, tmp_path, monkeypatch
) -> None:
    """第一题没答、第二题按默认答了：工具必须回「用户回答」，不能回「未回答」。

    真机实测到过这个形状：三题里第一题是公司名（没有默认值），后两题有默认值，
    结果工具回「用户未回答该问题」，模型据此以为整张卡被忽略，把已经定好的两个
    默认值也当成没定——那这个默认机制就白做了。
    """
    counter = Counter()
    session = _session(tmp_path, [_write_tool(counter), *ask_tools()])
    monkeypatch.setattr(graph, "interrupt", lambda payload: None)

    ask = {
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {
            "name": "request_user_input",
            "arguments": json.dumps(
                {
                    "questions": [
                        {"id": "name", "header": "公司名", "question": "公司叫什么名字？"},
                        {
                            "id": "style",
                            "header": "风格",
                            "question": "首页走哪种风格？",
                            "options": [{"label": "简约", "recommended": True}],
                        },
                    ]
                },
                ensure_ascii=False,
            ),
        },
    }

    result = await execute_action_node(_state([ask]), session)

    blob = json.dumps(result["tool_results"], ensure_ascii=False)
    assert "用户未回答该问题" not in blob, blob
    assert "简约" in blob and "按推荐默认值继续" in blob
    # 没答的那题仍然标着"未回答"，模型知道公司名还没定。
    assert "公司名：公司叫什么名字？" in blob
    assert "（用户未回答）" in blob


def test_the_assumption_ledger_keeps_only_the_last_word_per_question() -> None:
    """同一道题只留最后一次结论：后来答了 → 抹掉；又按默认了 → 替换而不是追加。"""
    from types import SimpleNamespace as NS

    from blank_api.services.agent.graph import _remember_assumptions

    style = {"id": "style", "header": "风格", "question": "首页走哪种风格？"}
    session = NS(assumptions=[])

    # 第一次：没答，按默认「简约」。
    _remember_assumptions(
        session, [style], {"style": "简约"}, [{**style, "value": "简约"}]
    )
    assert [item["value"] for item in session.assumptions] == ["简约"]

    # 第二次：用户真答了「商务」——前面那条"我替你定的"不成立了。
    _remember_assumptions(session, [style], {"style": "商务"}, [])
    assert session.assumptions == []

    # 第三次：又没答、又按默认「活泼」——替换，不是并排两行。
    _remember_assumptions(
        session, [style], {"style": "活泼"}, [{**style, "value": "活泼"}]
    )
    assert [item["value"] for item in session.assumptions] == ["活泼"]


def test_two_questions_sharing_an_id_do_not_overwrite_each_other() -> None:
    """模型两次提问都叫 q1：题面不同就是两道题，不能互相顶掉。"""
    from types import SimpleNamespace as NS

    from blank_api.services.agent.graph import _remember_assumptions

    first = {"id": "q1", "header": "风格", "question": "首页走哪种风格？"}
    second = {"id": "q1", "header": "栏目", "question": "要哪些栏目？"}
    session = NS(assumptions=[])

    _remember_assumptions(session, [first], {"q1": "简约"}, [{**first, "value": "简约"}])
    _remember_assumptions(session, [second], {"q1": "标准版"}, [{**second, "value": "标准版"}])

    assert [item["header"] for item in session.assumptions] == ["风格", "栏目"]

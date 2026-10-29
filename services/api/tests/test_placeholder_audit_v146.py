"""信息不足时先问，而不是自己编一份占位内容交差。

用户的原话是「碰到不知道的问题也不提问，就自己瞎干」：一句「帮我创建个公司官网」，
它没问公司叫什么、要哪些栏目、联系方式是什么，直接写出四个文件，结论里再补一句
「请替换占位名称、电话、邮箱和案例数据」。用户拿到的是"看起来做完了"，实际每个
关键字段都得自己重填——比一开始问三句话贵得多。

三道闸门，各管一段：

1. ``SYSTEM_PROMPT`` 里「先问再干」必须存在，且排在「做事规则」之前（靠后的规则
   在被长上下文冲淡时最先失效）。
2. 工具描述不能把提问往「别用」的方向推：它原来只写「不要用它确认显而易见的事」，
   缺了另一半「关键信息只有用户知道时必须问」。
3. 提示词是概率性的，所以再加一层确定性审计：本轮写进工具参数的占位值要被找出来，
   进了 ``review.completed`` 的证据里，并在交付正文末尾补一句"这些是我编的占位"。
   **模型改不掉那句话**，用户就不会再以为那是自己的信息。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from blank_api.services.agent.graph import (
    SYSTEM_PROMPT,
    _with_placeholder_note,
)
from blank_api.services.agent.placeholders import describe, scan, scan_messages
from blank_api.services.tools.ask import ask_tools


def _assistant(*calls: tuple[str, dict]) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": f"call_{index}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
            for index, (name, args) in enumerate(calls)
        ],
    }


# --------------------------------------------------------------------------- #
# 提示词与工具描述：模型行为那一半
# --------------------------------------------------------------------------- #


def test_ask_before_act_comes_before_the_execution_rules() -> None:
    """「先问再干」必须排在做事规则之前：排在后面时，长了就被上下文冲淡。"""
    assert "先问再干" in SYSTEM_PROMPT
    assert SYSTEM_PROMPT.index("先问再干") < SYSTEM_PROMPT.index("做事规则")
    # 规则要指名那个工具，否则模型只知道"该问"，不知道用什么问。
    assert "request_user_input" in SYSTEM_PROMPT
    # 两条边界都必须在：只写"要问"会让它问"用什么框架"这种自己能定的事。
    assert "只问「要什么」，不问「怎么做」" in SYSTEM_PROMPT
    assert "已经问过的不要再问" in SYSTEM_PROMPT


def test_the_question_tool_description_pushes_both_ways() -> None:
    """工具描述既有「该问」也有「别乱问」，缺一半就会跑偏。"""
    description = next(
        item.description for item in ask_tools() if item.name == "request_user_input"
    )
    assert "不要拿占位值顶替" in description
    assert "只问「要什么」，不问「怎么做」" in description


# --------------------------------------------------------------------------- #
# 确定性审计：事实那一半
# --------------------------------------------------------------------------- #


def test_placeholders_are_found_and_normal_code_is_not() -> None:
    found = {hit["token"] for hit in scan("联系 hello@example.com 或 138-0000-0000")}
    assert "example.com" in found
    assert "138-0000-0000" in found

    for text in ("某某公司官网", "lorem ipsum dolor", "电话：400-000-0000", "标题待填写"):
        assert scan(text), text

    # 正常代码里的这些词不算占位：把它们算进来，审计就没人信了。
    for text in (
        "// TODO: 接后端",
        '<input placeholder="请输入手机号">',
        "import xxx from 'yyy'",
        "const maxRetries = 3;",
        "prefix: __placeholder__",
    ):
        assert scan(text) == [], text


def test_only_write_calls_are_audited() -> None:
    """读类工具的参数是查询词：搜「example.com 这个域名怎么写」不是占位。"""
    messages = [
        {"role": "user", "content": "帮我写个官网"},
        _assistant(
            ("search_code", {"query": "example.com"}),
            ("write_file", {"path": "index.html", "content": "<a href='https://example.com'>x</a>"}),
        ),
    ]
    hits = scan_messages(messages)
    assert [hit["tool"] for hit in hits] == ["write_file"]


def test_scan_does_not_repeat_the_same_token_on_every_line() -> None:
    """同一个占位值在十个文件位置出现，是同一个问题，不是十个。"""
    text = "\n".join("email: hi@example.com" for _ in range(20))
    assert len(scan(text)) == 1


def test_the_delivery_note_names_what_is_missing() -> None:
    note = describe([{"token": "example.com"}, {"token": "138-0000-0000"}])
    assert "example.com" in note and "138-0000-0000" in note
    assert "我编的占位" in note


def test_a_question_already_asked_silences_the_note() -> None:
    """问过之后那些值就是用户给的，再报"这是占位"是冤枉它。"""
    state = {
        "messages": [
            _assistant(("write_file", {"path": "a.html", "content": "admin@example.com"}))
        ]
    }
    asked = SimpleNamespace(recorded_questions={"call_0"})
    silent = SimpleNamespace(recorded_questions=set())

    assert _with_placeholder_note("写好了", silent, state).startswith("写好了")
    assert "占位" in _with_placeholder_note("写好了", silent, state)
    assert _with_placeholder_note("写好了", asked, state) == "写好了"


def test_a_clean_delivery_gets_no_extra_sentence() -> None:
    state = {"messages": [_assistant(("write_file", {"path": "a.py", "content": "print(1)"}))]}
    session = SimpleNamespace(recorded_questions=set())
    assert _with_placeholder_note("跑通了", session, state) == "跑通了"

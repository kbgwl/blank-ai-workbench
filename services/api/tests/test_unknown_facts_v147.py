"""不知道该问的问题，现在由服务端拦下来问。

用户的第二句抱怨是「它自己不懂的问题也不知道问，这难道不是 bug 么」。是 bug。
上一轮只在提示词里写了「先问再干」——那是**改变概率**，不是**保证行为**：同一句
「帮我做个公司官网」，上下文一变它就不问了，然后编一个 138-0000-0000 写进页面。
电话、邮箱、地址这种值，编出来和真的长得一模一样，用户是那个要接电话的人。

所以这一层做成确定性的：**写入之前**比对"这个值有没有出处"。用户没说过、工作区
里没有、本会话也没确认过 → 不落盘，服务端自己弹问题。模型听话与否不再影响结果。

这组测试盯的是"拦得住、问得准、放得对"三件事：
- 拦：没有出处的电话/邮箱/地址，写调用被拒（不是被提醒）；
- 准：用户说过的、工作区里已有的、代码文件里的测试夹具，一个都不许误拦；
- 放：用户答"就用这个"才放行；答了别的值就按那个值重写；不答就留空。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from blank_api.services.agent.graph import _plan_fact_gate, _settle_fact_gate
from blank_api.services.agent.unknowns import decide, explain, facts, facts_of_arguments

PHONE = "138-1234-5678"


def _call(name: str, args: dict, index: int = 1) -> dict:
    return {
        "id": f"call_{index}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def _session(root, **overrides):
    data = {
        "context": SimpleNamespace(root=root),
        "asked_facts": set(),
        "allowed_facts": set(),
        "confirmed_text": [],
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _state(*messages) -> dict:
    return {"messages": list(messages)}


# --------------------------------------------------------------------------- #
# 拦：没有出处的值不许落盘
# --------------------------------------------------------------------------- #


def test_a_phone_nobody_ever_gave_is_blocked_and_asked(tmp_path) -> None:
    """这是用户遇到的那一幕：谁也没给过电话，它自己写了一个。"""
    pending = [
        _call("write_file", {"path": "index.html", "content": f"<p>电话：{PHONE}</p>"})
    ]
    gate, questions = _plan_fact_gate(
        _session(tmp_path), _state({"role": "user", "content": "帮我做个公司官网"}), pending,
        readonly=False,
    )

    assert list(gate.findings) == ["call_1"]
    assert len(questions) == 1
    # 问题里必须带着那个值本身，否则用户不知道在问哪一处。
    assert PHONE in questions[0]["question"]
    assert questions[0]["header"] == "电话出处"
    blocked = _settle_fact_gate(_session(tmp_path), gate, {})
    assert "已拦下这次写入" in blocked["call_1"]


def test_the_value_the_user_gave_is_never_flagged(tmp_path) -> None:
    """用户自己说了号码，就不该再问他一遍——问这个不叫严谨，叫没在听。"""
    pending = [_call("write_file", {"path": "a.md", "content": f"客服：{PHONE}"})]
    gate, questions = _plan_fact_gate(
        _session(tmp_path),
        _state({"role": "user", "content": f"我的电话是 {PHONE}，写进联系页"}),
        pending,
        readonly=False,
    )
    assert gate.findings == {}
    assert questions == []


def test_a_value_already_in_the_workspace_is_never_flagged(tmp_path) -> None:
    """工作区里早就写着这个号码，说明它是这个项目的事实，不是模型编的。"""
    (tmp_path / "contact.md").write_text(f"电话 {PHONE}", encoding="utf-8")
    pending = [_call("write_file", {"path": "index.html", "content": f"电话 {PHONE}"})]
    gate, _ = _plan_fact_gate(_session(tmp_path), _state(), pending, readonly=False)
    assert gate.findings == {}


def test_the_same_number_with_spaces_or_country_code_still_counts_as_known(tmp_path) -> None:
    """用户写 13812345678、模型写 +86 138-1234-5678，是同一件事。"""
    pending = [_call("write_file", {"path": "a.html", "content": "电话 +86 138 1234 5678"})]
    gate, _ = _plan_fact_gate(
        _session(tmp_path),
        _state({"role": "user", "content": "电话 13812345678"}),
        pending,
        readonly=False,
    )
    assert gate.findings == {}


def test_emails_and_addresses_are_covered_too(tmp_path) -> None:
    pending = [
        _call(
            "write_file",
            {
                "path": "about.md",
                "content": "邮箱 sales@madeup-corp.com\n地址：杭州市西湖区文三路 100 号",
            },
        )
    ]
    gate, questions = _plan_fact_gate(_session(tmp_path), _state(), pending, readonly=False)
    labels = {item.label for item in gate.findings["call_1"]}
    assert labels == {"邮箱", "地址"}
    assert {q["header"] for q in questions} == {"邮箱出处", "地址出处"}


# --------------------------------------------------------------------------- #
# 准：不许误拦
# --------------------------------------------------------------------------- #


def test_code_files_are_not_gated(tmp_path) -> None:
    """单测里写假号码是正常做法：拦它等于让"写测试"变成一轮一轮答问。"""
    pending = [
        _call(
            "write_file",
            {"path": "tests/test_form.py", "content": f'PHONE = "{PHONE}"  # 夹具'},
        )
    ]
    gate, _ = _plan_fact_gate(_session(tmp_path), _state(), pending, readonly=False)
    assert gate.findings == {}


def test_read_only_work_never_interrupts(tmp_path) -> None:
    """只读调研本来就不写文件，这里也不该冒出一张卡。"""
    pending = [_call("write_file", {"path": "a.html", "content": PHONE})]
    gate, questions = _plan_fact_gate(
        _session(tmp_path), _state(), pending, readonly=True
    )
    assert gate.findings == {} and questions == []


def test_paths_and_ports_are_not_mistaken_for_phones() -> None:
    """127.0.0.1:8000 和日期都不该被当成电话号码。"""
    for text in ("127.0.0.1:8000", "2026-10-30", "版本 1.2.3", "共 1234 行"):
        assert facts(text) == [], text


def test_only_content_arguments_are_read() -> None:
    """路径参数里出现数字串不是事实值。"""
    found = facts_of_arguments(
        "write_file", {"path": "a.html", "content": "<p>hi</p>", "note": PHONE}
    )
    assert [item.token for item in found] == [PHONE]


# --------------------------------------------------------------------------- #
# 放：确认才放行，给了别的值按别的写
# --------------------------------------------------------------------------- #


def test_confirming_the_value_lets_the_next_write_through(tmp_path) -> None:
    pending = [_call("write_file", {"path": "a.html", "content": PHONE})]
    session = _session(tmp_path)
    gate, questions = _plan_fact_gate(session, _state(), pending, readonly=False)
    blocked = _settle_fact_gate(session, gate, {questions[0]["id"]: "就用这个"})
    assert blocked == {}
    assert session.allowed_facts

    again, more = _plan_fact_gate(session, _state(), pending, readonly=False)
    assert again.findings == {} and more == []


def test_a_real_value_from_the_user_unblocks_the_rewrite(tmp_path) -> None:
    """用户给了真号码：原来那个仍然拦着，理由里得带上用户给的值。"""
    pending = [_call("write_file", {"path": "a.html", "content": PHONE})]
    session = _session(tmp_path)
    gate, questions = _plan_fact_gate(session, _state(), pending, readonly=False)
    blocked = _settle_fact_gate(session, gate, {questions[0]["id"]: "13900001111"})
    assert "13900001111" in blocked["call_1"]
    assert "13900001111" in session.confirmed_text[0]

    rewrite = [_call("write_file", {"path": "a.html", "content": "电话 139-0000-1111"})]
    again, _ = _plan_fact_gate(session, _state(), rewrite, readonly=False)
    assert again.findings == {}


def test_saying_leave_it_blank_keeps_blocking_without_asking_again(tmp_path) -> None:
    pending = [_call("write_file", {"path": "a.html", "content": PHONE})]
    session = _session(tmp_path)
    gate, questions = _plan_fact_gate(session, _state(), pending, readonly=False)
    blocked = _settle_fact_gate(session, gate, {questions[0]["id"]: "先留空"})
    assert "留空" in blocked["call_1"]

    # 同一个值再来一次：不再问，但仍然拦。
    again, more = _plan_fact_gate(session, _state(), pending, readonly=False)
    assert more == []
    assert _settle_fact_gate(session, again, {})


def test_a_dismissed_question_is_not_asked_twice(tmp_path) -> None:
    """用户按掉了卡：可以不再问，但不能因此放行。"""
    pending = [_call("write_file", {"path": "a.html", "content": PHONE})]
    session = _session(tmp_path)
    gate, questions = _plan_fact_gate(session, _state(), pending, readonly=False)
    assert questions
    _settle_fact_gate(session, gate, {})

    again, more = _plan_fact_gate(session, _state(), pending, readonly=False)
    assert more == []
    assert "已拦下这次写入" in _settle_fact_gate(session, again, {})["call_1"]


def test_two_calls_each_get_their_own_question(tmp_path) -> None:
    """两个文件各编了一处，是两处缺口，不能压成一个问题糊过去。"""
    pending = [
        _call("write_file", {"path": "a.html", "content": PHONE}, index=1),
        _call("write_file", {"path": "b.md", "content": "mail: hi@madeup-corp.com"}, index=2),
    ]
    gate, questions = _plan_fact_gate(_session(tmp_path), _state(), pending, readonly=False)
    assert len(gate.findings) == 2
    assert len({q["id"] for q in questions}) == len(questions) == 2
    assert len({q["question"] for q in questions}) == 2


# --------------------------------------------------------------------------- #
# 判定与措辞
# --------------------------------------------------------------------------- #


def test_decide_only_allows_what_the_user_actually_confirmed() -> None:
    item = facts(PHONE)[0]
    assert decide(item, "就用这个") == "allow"
    assert decide(item, "是的") == "allow"
    assert decide(item, PHONE) == "allow"
    assert decide(item, "先留空") == "blank"
    assert decide(item, "13900001111") == "value"
    assert decide(item, "我另给一个") == "none"
    assert decide(item, "") == "none"
    # "你看着办"不是值：把它当成用户给的值，理由就会变成"用户给的是：你看着办"。
    assert decide(item, "你看着办") == "delegate"
    assert decide(item, "随便") == "delegate"


def test_delegating_the_choice_does_not_become_a_value() -> None:
    item = facts(PHONE)[0]
    text = explain([item], {}, {item.key: "delegate"})
    assert "不要替他编" in text
    assert "待补充" in text


def test_the_refusal_reason_tells_the_model_what_to_do() -> None:
    item = facts(PHONE)[0]
    text = explain([item], {}, {})
    assert PHONE in text
    # 三种出口都写清楚，模型才不会拿"我假设了一下"糊过去。
    assert "不要填" in text
    assert "待补充" in text

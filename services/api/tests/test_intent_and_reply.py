"""意图识别与交付口径的回归测试。

这一层的职责是把"回复太啰嗦"变成可校验的规则：
先判这句话要不要动手——不需要就一次答完，问判断就只读看几眼再答，需要就拆步骤动手；
交付文本固定「结论 / 下一步」两段。
"""

from __future__ import annotations

import asyncio

from blank_api.limits import READONLY_MAX_STEPS
from blank_api.services.agent.graph import create_plan_node
from blank_api.services.agent.intent import (
    INTENT_ACT,
    INTENT_ADVISE,
    INTENT_ANSWER,
    as_report,
    format_report,
    guess_intent,
    normalize_answer,
    parse_intent,
    unwrap_goal,
    split_sections,
)


# ── 1. 规则快判：边界清晰的两头要判准 ──────────────────────────────


def test_greeting_is_answer() -> None:
    for text in ("你好", "在吗", "谢谢！", "ok", "hi", "收到"):
        assert guess_intent(text) == INTENT_ANSWER, text


def test_action_words_are_act() -> None:
    for text in (
        "帮我修复登录页的 bug",
        "给接口加一个限流",
        "运行一遍测试",
        "把这个组件重写一下",
        "部署到测试环境",
    ):
        assert guess_intent(text) == INTENT_ACT, text


def test_short_concept_question_is_answer() -> None:
    assert guess_intent("微服务和单体有什么区别") == INTENT_ANSWER
    assert guess_intent("什么是幂等") == INTENT_ANSWER


def test_ambiguous_text_is_left_to_model() -> None:
    # 既无动作词也不是短问句：不在这里拍板，交给模型判
    assert guess_intent("这个登录流程看起来不太对，你怎么看") is None


# ── 2. 意图 JSON 解析要容忍模型的写法差异 ───────────────────────────


def test_parse_intent_plain_json() -> None:
    parsed = parse_intent(
        '{"intent":"act","note":"修登录 bug","steps":["看日志","改代码"],"answer":""}'
    )
    assert parsed["intent"] == INTENT_ACT
    assert parsed["steps"] == ["看日志", "改代码"]


def test_parse_intent_tolerates_fence_and_aliases() -> None:
    parsed = parse_intent('好的：\n```json\n{"intent":"chat","answer":"直接答案"}\n```')
    assert parsed["intent"] == INTENT_ANSWER
    assert parsed["answer"] == "直接答案"


def test_parse_intent_rejects_unknown_intent() -> None:
    assert parse_intent('{"intent":"maybe"}') == {}
    assert parse_intent("这不是 JSON") == {}


def test_parse_intent_accepts_string_steps() -> None:
    parsed = parse_intent('{"intent":"act","steps":"先看代码"}')
    assert parsed["steps"] == ["先看代码"]


# ── 3. 答案去噪：只削废话，不改内容 ─────────────────────────────────


def test_normalize_strips_opening_and_closing_noise() -> None:
    raw = "好的，让我来分析一下。\n这个问题是因为缓存未命中。\n如需其他帮助请告诉我。"
    cleaned = normalize_answer(raw)
    assert cleaned == "这个问题是因为缓存未命中。"


def test_normalize_keeps_content_untouched() -> None:
    raw = "缓存未命中时回源，QPS 会瞬间翻倍。"
    assert normalize_answer(raw) == raw


def test_normalize_collapses_blank_lines() -> None:
    assert normalize_answer("第一行\n\n\n\n第二行") == "第一行\n\n第二行"


# ── 4. 交付文本：空段省略，不套空壳 ────────────────────────────────


def test_format_report_omits_empty_sections() -> None:
    """空段不写「无」。

    这条改的是刻意的取舍：原来空段填「无」，一份只有一句话的交付会变成
    「已完成 / - 那句话 / 未完成 / - 无 / 下一步 / - 无」六行，其中四行没有
    任何新信息，读者还得先确认是空还是漏写。而"都做完了"结论段自己会说，
    界面上「0 次失败」也在说。
    """
    text = format_report("登录校验改好了", "", "")
    assert text == "结论\n- 登录校验改好了"
    assert "未完成" not in text and "下一步" not in text


def test_format_report_keeps_non_empty_sections() -> None:
    text = format_report("登录校验改好了", "没跑集成测试", "")
    assert "结论\n- 登录校验改好了" in text
    assert "未完成\n- 没跑集成测试" in text
    assert "下一步" not in text


def test_split_sections_renames_done_to_conclusion() -> None:
    """旧标题「已完成」仍要认，但归到「结论」下。

    动作清单和结果结论得分开：前者界面上已经有一份事件流，后者才是这段文字
    存在的理由。旧标题继续识别，是因为模型偶尔还按老格式写，认不出来就白丢
    一次结构。
    """
    sections = split_sections("已完成\n- 补了单测\n未完成\n- 没跑集成测试\n下一步\n- 跑一次全量")
    assert sections["结论"] == ["补了单测"]
    assert sections["未完成"] == ["没跑集成测试"]
    assert sections["下一步"] == ["跑一次全量"]
    assert "已完成" not in sections


def test_split_sections_reads_new_titles() -> None:
    sections = split_sections("结论\n- 令牌改成 30 天\n下一步\n- 你确认下有效期")
    assert sections["结论"] == ["令牌改成 30 天"]
    assert sections["下一步"] == ["你确认下有效期"]


def test_as_report_returns_plain_text_as_is() -> None:
    """一句话结论不要被包成三段。

    「答案就是 42。」原样返回。套上「结论 / - 答案就是 42。/ 下一步 / - 无」
    只是把一行变成四行，没增加一个字的判断。
    """
    assert as_report("答案就是 42。") == "答案就是 42。"


def test_as_report_keeps_existing_structure() -> None:
    text = as_report("已完成\n- A\n未完成\n- B\n下一步\n- C")
    assert text == "结论\n- A\n未完成\n- B\n下一步\n- C"


def test_as_report_empty_stays_empty() -> None:
    """没话说就不要造一句话出来。

    原来空输入会产出「已完成 / - 无 / 未完成 / - 无 / 下一步 / - 无」，
    等于用五行字宣布"什么都没发生"。调用方有兜底文案，这里交白卷更诚实。
    """
    assert as_report("") == ""
    assert as_report("   \n\n ") == ""


# ── 5. 节点行为：直答不进工具循环 ──────────────────────────────────


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def add(self, event_type: str, node: str, payload: dict | None = None) -> None:
        self.events.append((event_type, node, payload or {}))


class _Session:
    """只实现 create_plan_node 用到的部分。"""

    def __init__(self, reply: str, tool_calls: list | None = None, goal: str = "") -> None:
        self.recorder = _Recorder()
        self.goal = goal
        self.acceptance_criteria: list[str] = []
        self.steer_metrics = {"count": 0, "steps": []}
        self.max_steps = 200
        self._reply = reply
        self._tool_calls = tool_calls or []
        self.snapshot_calls: list[str] = []
        self.prompts: list[str] = []

    async def ask(self, state, prompt, node, **kwargs):  # noqa: ANN001
        self.prompts.append(prompt)
        return {
            "text": self._reply,
            "tool_calls": self._tool_calls,
            "usage": {},
            "messages": list(state.get("messages") or []),
        }

    async def ensure_snapshot(self, node: str = "create_plan") -> str:
        # 真正的实现会去复制整个工作区；这个桩只需要说明"动作类任务确实走到了这一步"。
        # 问答类任务在 create_plan 里提前 return，永远走不到这里 —— 那正是 #46 要的结果。
        self.snapshot_calls.append(node)
        return "snap-1"


def _run(session: _Session, goal: str) -> dict:
    state = {"task_id": "t", "messages": [{"role": "user", "content": goal}]}
    return asyncio.run(create_plan_node(state, session))


def test_chitchat_returns_direct_answer() -> None:
    session = _Session('{"intent":"answer","note":"打招呼","steps":[],"answer":"在，需要我做什么？"}')
    result = _run(session, "你好")
    assert result["direct_answer"] is True
    assert result["summary"] == "在，需要我做什么？"
    assert result["pending"] == []
    event_types = [item[0] for item in session.recorder.events]
    assert "intent.classified" in event_types
    assert "plan.created" not in event_types
    # 纯问答从不写工作区，所以一份快照都不该拍：大仓库里那一下就是几十秒白等
    assert session.snapshot_calls == []


def test_task_request_is_split_into_steps() -> None:
    session = _Session(
        '{"intent":"act","note":"修登录","steps":["看日志","改代码","跑测试"],"answer":""}'
    )
    result = _run(session, "帮我修复登录 bug")
    assert result["direct_answer"] is False
    assert result["plan"] == ["看日志", "改代码", "跑测试"]
    assert "plan.created" in [item[0] for item in session.recorder.events]
    # 动作类任务：判完意图、动手之前才拍快照，而且只拍一次
    assert session.snapshot_calls == ["create_plan"]


def test_prose_reply_is_treated_as_answer() -> None:
    # 模型没按格式回 JSON，但也没调工具：说明它就是在回答，别再拉进执行回合
    session = _Session("这是一个不需要动手的问题，答案是 42。")
    result = _run(session, "这个登录流程看起来不太对，你怎么看")
    assert result["direct_answer"] is True
    assert "42" in result["summary"]


def test_tool_calls_force_act_even_without_json() -> None:
    session = _Session("我先看一下目录。", tool_calls=[{"id": "c1", "name": "list_files", "arguments": {}}])
    result = _run(session, "看看项目结构")
    assert result["direct_answer"] is False
    assert result["pending"] == [{"id": "c1", "name": "list_files", "arguments": {}}]


# ── 5. 判意图前先剥掉界面拼的前缀 ──────────────────────────────────
# 前缀里写着「实际修改文件并运行相关测试」，拿它判意图等于先设好结论再判：
# 问一句「你现在的工作目录是哪里」也会被判成 act，然后进工具循环。

_WRAPPED = (
    "执行规则：不要寒暄、不要夸奖或拍马屁、不要复述用户原话；只输出已执行动作、结果、阻塞和下一步。\n"
    "你处于开发场景。优先读取现有代码，实际修改文件并运行相关测试；回复只保留动作、结果和阻塞。\n"
    "\n用户任务：{raw}"
)


def test_unwrap_strips_the_ui_wrapper() -> None:
    assert unwrap_goal(_WRAPPED.format(raw="把标题改成蓝色")) == "把标题改成蓝色"


def test_a_question_is_no_longer_forced_into_act() -> None:
    # 这是本次要修的缺陷本身：同一句话，带前缀时被判 act，剥掉前缀后不再被强行拉去动手。
    wrapped = _WRAPPED.format(raw="你现在的工作目录是哪里")
    assert guess_intent(wrapped) == INTENT_ACT
    assert guess_intent(unwrap_goal(wrapped)) != INTENT_ACT


def test_plain_goal_is_returned_unchanged() -> None:
    # 独立任务、API 直接调用都没这层包装，不能因为剥前缀把原话改坏。
    assert unwrap_goal("把输入框改高三行") == "把输入框改高三行"


def test_user_quoting_the_marker_keeps_their_whole_sentence() -> None:
    # 用户原话里自己写了「用户任务：」，剥的必须是界面那个（在用户原话之前），
    # 不能连用户的话一起切掉。
    raw = "上次那个「用户任务：把标题改蓝」还没生效"
    assert unwrap_goal(_WRAPPED.format(raw=raw)) == raw


def test_no_wrapper_means_no_marker_surgery() -> None:
    # 没有包装时，用户自己写的「用户任务：」就是正文的一部分。
    raw = "说明一下 用户任务：是什么意思"
    assert unwrap_goal(raw) == raw


def test_wrapper_only_falls_back_to_input() -> None:
    # 剥完是空串会让调用方拿着空串去判意图，这时宁可退回原文。
    only = "执行规则：不要寒暄、不要夸奖或拍马屁。"
    assert unwrap_goal(only) == only


# ── 6. 问判断，不是派活 ────────────────────────────────────────────
# 「你觉得界面有什么可以优化的」里带「优化」，被动作词一命中就判 act，
# 于是它真去改了一通，最后审查说意图不对齐——116 次工具调用，全部作废。
#
# 但也不能判 answer：一次模型调用直接答，一行代码都没看，只能说「界面要简洁」
# 这类放之四海皆准的话。判 advise：先只读看几处，再给有依据的判断。


def test_asking_for_an_opinion_is_readonly_research() -> None:
    for text in (
        "你自己看看你的工作环境 你觉得你的界面比codex有什么可以优化的",
        "我们的应用你觉得就只有这些需要改么",
        "你觉得还有哪些可以优化的",
        "值不值得重构一下",
    ):
        assert guess_intent(text) == INTENT_ADVISE, text


def test_an_actual_assignment_is_still_act() -> None:
    # 反向守卫：句子在征求意见、但明确要你动手时，不能被降级成闲聊。
    for text in (
        "你觉得哪里有问题，帮我改掉",
        "先看看代码有什么问题，然后修掉",
        "把输入框改短一点",
        "跑一下前端测试",
        "实现一个会话列表分叉入口",
        "我给你开启执行命令了 你开始优化吧",
    ):
        assert guess_intent(text) == INTENT_ACT, text


def test_plan_prompt_points_at_the_real_user_request() -> None:
    # 前缀不只骗规则，也骗模型：它读到的整段消息里写着「实际修改文件并运行相关测试」。
    # 所以除了剥前缀，还必须把用户原话明写一次，让模型按原话判。
    session = _Session('{"intent":"answer","steps":[],"answer":"三处：输入框、依据条、分叉"}')
    result = _run(session, _WRAPPED.format(raw="你觉得界面有什么可以优化的"))
    assert result["direct_answer"] is True
    # 问答类不该拍快照：那是"确定要动工作区"的动作。
    assert session.snapshot_calls == []
    sent = session.prompts[0]
    assert "界面附带的固定前缀" in sent
    assert "你觉得界面有什么可以优化的" in sent
    # 规则已经认出这是问判断：提示里要明说"必须给依据"，否则模型照样能直接口答。
    assert "intent 必须是 advise" in sent


def test_opinion_runs_readonly_instead_of_answering_blind() -> None:
    """问判断 → 只读调研：不拍快照、不放写工具、步数收紧。"""
    session = _Session('{"intent":"advise","note":"问界面优化点","steps":["读取 panel.tsx"]}')
    result = _run(session, "你觉得界面有什么可以优化的")

    assert result["readonly"] is True
    # 不走 direct_answer：那条路一次调用就结束，等于没看任何东西就下结论。
    assert result["direct_answer"] is False
    assert session.readonly is True
    assert session.max_steps == READONLY_MAX_STEPS
    assert session.snapshot_calls == []
    events = [item[0] for item in session.recorder.events]
    assert "readonly.started" in events


def test_readonly_call_from_the_planning_step_is_kept_as_research() -> None:
    """判定阶段就顺手读了文件：只要全是只读，就还在调研，不该被抬成 act。"""
    session = _Session(
        "",
        tool_calls=[{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
    )
    result = _run(session, "你觉得界面有什么可以优化的")

    assert result["readonly"] is True
    assert result["direct_answer"] is False
    assert len(result["pending"]) == 1


def test_a_write_during_research_is_treated_as_act() -> None:
    """同一句话，模型却抬手要写文件 —— 那就是真要动手，按 act 走，拍照留底。"""
    session = _Session(
        "",
        tool_calls=[{"id": "c1", "type": "function", "function": {"name": "apply_patch", "arguments": "{}"}}],
    )
    result = _run(session, "你觉得界面有什么可以优化的")

    assert result.get("readonly") is not True
    assert session.snapshot_calls == ["create_plan"]

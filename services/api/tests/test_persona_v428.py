"""使用者模型：学他怎么干活，不是学这个世界。

这组判据守的是**同一条不变量**：只有用户自己说过的话才能变成他的一贯要求，
而且重复不等于一贯——得看重复的是不是同一个人的同一个意图。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from blank_api.services.agent.persona import (  # noqa: E402
    MAX_INJECTED,
    MIN_OCCURRENCES,
    PersonaStore,
    Preference,
    _same_subject,
    extract_instructions,
    injectable,
    lint,
    preference_from_quote,
    render,
)


def _store():
    root = Path(tempfile.mkdtemp())
    return PersonaStore(root), root / "ws"


def _say(store, ws, text, *, task_id="T", times=1):
    out = []
    for index in range(times):
        out = store.learn_from_text(ws, text, task_id=f"{task_id}{index}")
    return out


# ── 抽取：任务步骤不是要求 ────────────────────────────────────────────────
def test_task_steps_are_not_requirements():
    """`然后跑 run_tests 验证` 是这一步做什么，不是他一贯怎么验收。"""
    goal = "在 deliverables/x.html 写一个页面，然后跑 run_tests 验证"

    assert extract_instructions(goal) == ["然后跑 run_tests 验证"]
    assert extract_instructions(goal, task_specific=True) == []


def test_explicit_requirements_survive_the_task_filter():
    goal = "改完必须跑全量回归，不要只看类型检查通过"

    kept = extract_instructions(goal, task_specific=True)

    assert "改完必须跑全量回归" in kept
    assert "不要只看类型检查通过" in kept


def test_concrete_artifacts_mark_a_clause_as_task_specific():
    """带路径、反引号命令、具体工具名的分句是任务细节——工具名只在**驳回反馈**里算要求。"""
    assert extract_instructions("必须用 run_shell 执行 echo smoke-ok", task_specific=True) == []
    assert extract_instructions("写完后必须用 browser_check 自检：url 指向文件", task_specific=True) == []
    # 同一个工具名，从驳回反馈里说出来就是要求——两个分句都留
    kept = extract_instructions("先别列目录，直接用 list_files")
    assert "直接用 list_files" in kept
    assert "先别列目录" in kept


def test_negation_beats_a_positive_word_inside_it():
    """'不需要' 里含 '需要'——第一版会把它读成正向要求。"""
    from blank_api.services.agent.persona import polar

    assert polar("命名用中文也不需要解释") == -1
    assert polar("必须跑回归") == 1


# ── 一次不算数 ────────────────────────────────────────────────────────────
def test_one_mention_is_not_a_habit():
    store, ws = _store()
    _say(store, ws, "改完必须跑全量回归")

    entries = store.load(ws)
    assert entries[0].status == "candidate"
    assert entries[0].occurrences == 1
    assert store.persona_block(ws) == "", "单次发言不该进提示"


def test_two_mentions_make_it_a_habit():
    store, ws = _store()
    _say(store, ws, "改完必须跑全量回归", times=MIN_OCCURRENCES)

    entry = store.load(ws)[0]
    assert entry.status == "active"
    assert entry.occurrences == MIN_OCCURRENCES
    assert "改完必须跑全量回归" in store.persona_block(ws)


def test_confidence_never_reaches_certainty():
    """同一条被证实很多次也只到 0.9——'永远正确'不存在。"""
    store, ws = _store()
    _say(store, ws, "改完必须跑全量回归", times=12)

    assert store.load(ws)[0].confidence == 0.9


# ── 重复不等于一贯 ────────────────────────────────────────────────────────
def test_harvested_candidates_never_activate_by_themselves():
    """拿 111 个真实任务描述试出来的教训：同一段冒烟脚本跑了 30 遍，
    '不要调用任何工具' 出现 30 次——那是脚本重复，不是他 30 次提要求。"""
    store, ws = _store()
    for index in range(30):
        store.learn_from_text(
            ws,
            "不要调用任何工具",
            task_id=f"probe{index}",
            source="goal",
            origin="harvested",
            task_specific=True,
        )

    entry = store.load(ws)[0]
    assert entry.origin == "harvested"
    assert entry.occurrences >= 30
    assert entry.status == "candidate", "从描述里摘的重复再多也要人确认"
    assert store.persona_block(ws) == ""


def test_explicit_origin_is_the_only_path_to_automatic_activation():
    store, ws = _store()
    _say(store, ws, "改完必须跑全量回归", times=2)

    assert store.load(ws)[0].origin == "explicit"
    assert store.load(ws)[0].status == "active"


# ── 两条打架 ──────────────────────────────────────────────────────────────
def test_opposing_rules_on_the_same_subject_disable_both():
    """实测：两条矛盾记忆同时被检索时遵从率 63–72%，遵从后成功率塌到 17–31%。
    这种情况不是让模型自己挑——两条都不注入，报出来让人定。"""
    store, ws = _store()
    _say(store, ws, "必须跑测试", times=2)
    _say(store, ws, "不要跑测试", times=2)

    entries = store.load(ws)
    assert {item.status for item in entries} == {"conflict"}
    assert store.persona_block(ws) == ""


def test_opposing_rules_on_different_subjects_coexist():
    """只判方向相反是不够的。这两条来自两段不同的冒烟脚本，根本不相干。"""
    store, ws = _store()
    _say(store, ws, "不要调用任何工具", times=2)
    _say(store, ws, "参数 command 必须是这一条", times=2)

    assert {item.status for item in store.load(ws)} == {"active"}


def test_same_subject_needs_real_overlap():
    assert _same_subject("必须跑测试", "不要跑测试") is True
    assert _same_subject("不要调用任何工具", "参数 command 必须是这一条") is False


# ── 注入 ──────────────────────────────────────────────────────────────────
def test_injection_takes_one_rule_per_dimension():
    """同维度全塞进去会互相稀释（提示里每多一条，就多一条可能被误用的东西）。"""
    rules = [
        "改完必须跑全量回归",
        "写完后必须跑判据",
        "改完必须跑一遍验证",
    ]
    store, ws = _store()
    for rule in rules:
        _say(store, ws, rule, task_id=rule, times=2)

    block = store.persona_block(ws)

    assert block.count("\n- ") == 1, block


def test_injection_is_capped():
    store, ws = _store()
    # 分别落在不同维度：验证 / 边界 / 表达 / 交付 / 工具选择
    for rule in (
        "改完必须跑全量回归",
        "改数据库之前必须先问我",
        "注释不要写废话",
        "最后要附一份总结",
        "直接用 list_files 不要用 dir",
        "一次只改一个文件",
    ):
        _say(store, ws, rule, task_id=rule, times=2)

    assert len(injectable(store.load(ws))) <= MAX_INJECTED


def test_the_block_says_the_current_task_wins():
    """使用者模型是伞不是锁。把它当硬规则，agent 就变成固执——那是漫画，不是像他。"""
    block = render([Preference(rule="先跑测试再实现", status="active", occurrences=2)])

    assert "以本次为准" in block
    # 不要求引用、不要求复述（实测强制引用会导致过度依赖）
    assert "引用" not in block and "复述" not in block


def test_no_entries_means_no_block():
    """读不到就返回空串，绝不挡任务。"""
    store, ws = _store()

    assert store.persona_block(ws) == ""


# ── 可见与可撤 ────────────────────────────────────────────────────────────
def test_every_entry_carries_the_users_actual_words():
    """没有原话的条目就是"我们推断的"，不该存在——这是整个模块的核心不变量。"""
    store, ws = _store()
    _say(store, ws, "改完必须跑全量回归", times=2)

    entry = store.load(ws)[0]
    assert entry.quotes
    assert entry.quotes[0]["text"]


def test_lint_flags_an_entry_without_a_quote():
    bare = Preference(rule="他应该喜欢简洁", status="active", occurrences=2)

    assert any(item["kind"] == "ungrounded" for item in lint([bare]))


def test_retiring_keeps_the_entry_visible():
    """退役不是删除：条目还在文件里，只是不再被注入。删掉之后连'学过什么'都不知道了。"""
    store, ws = _store()
    _say(store, ws, "改完必须跑全量回归", times=2)
    target = store.load(ws)[0]

    entries = store.load(ws)
    entries[0].status = "retired"
    store.save(ws, entries)

    assert store.persona_block(ws) == ""
    assert store.load(ws)[0].rule == "改完必须跑全量回归"


# ── 接线：进 system，不进 user 消息 ───────────────────────────────────────
def test_persona_goes_into_the_system_prompt_not_the_task_message():
    """它是一贯约定，不是这次任务的情境。压缩时保留的是「第一条 system + 当前目标」，
    塞进 user 消息会在压缩后消失——那条学会的偏好就只能用一次，正好和'越来越像他'相反。"""
    from blank_api.services.agent.graph import initial_messages

    messages = initial_messages("改分页", [], persona_block="[验证] 改完必须跑回归")
    system = "\n".join(
        str(item.get("content", "")) for item in messages if item.get("role") == "system"
    )
    user = "\n".join(
        str(item.get("content", "")) for item in messages if item.get("role") == "user"
    )

    assert "改完必须跑回归" in system
    assert "改完必须跑回归" not in user


# ── 独立任务才算是"又一次" ────────────────────────────────────────────────
def test_the_same_task_repeating_itself_does_not_make_a_habit():
    """同一个任务里重试两次、或者我把同一条命令跑了两遍，只说明"这次重复了"。
    第一版只计数不看来源任务，一次任务内重放两次就能顶成 active——那是自问自答。"""
    store, ws = _store()
    for _ in range(3):
        store.learn_from_text(ws, "改完必须跑全量回归", task_id="同一个任务")

    entry = store.load(ws)[0]
    assert entry.occurrences == 1
    assert entry.status == "candidate"
    assert store.persona_block(ws) == ""


def test_a_second_task_is_what_makes_it_a_habit():
    store, ws = _store()
    store.learn_from_text(ws, "改完必须跑全量回归", task_id="任务A")
    store.learn_from_text(ws, "改完必须跑全量回归", task_id="任务B")

    entry = store.load(ws)[0]
    assert entry.occurrences == 2
    assert entry.status == "active"


# ── 失效：字段建了必须有人读 ──────────────────────────────────────────────
def test_an_old_preference_stops_being_injected():
    """`expires_after_days` 如果只写在 dataclass 里没人读，条目就永久生效——
    他早换了做法，我们还在按两年前的规矩办，那时它就不是"像他"，是"像过去的他"。"""
    from datetime import datetime, timedelta, timezone

    from blank_api.services.agent.persona import is_expired

    stale = Preference(
        rule="改完必须跑全量回归",
        status="active",
        occurrences=2,
        expires_after_days=180,
        last_confirmed=(datetime.now(timezone.utc) - timedelta(days=400)).isoformat(),
    )

    assert is_expired(stale) is True
    assert injectable([stale]) == []


def test_a_missing_timestamp_does_not_kill_it():
    """宁可多留一条，不要因为缺个字段误杀。"""
    from blank_api.services.agent.persona import is_expired

    assert is_expired(Preference(rule="x", status="active", occurrences=2, last_confirmed="")) is False
    assert is_expired(Preference(rule="x", status="active", occurrences=2, last_confirmed="不是时间")) is False


def test_being_injected_does_not_extend_the_life():
    """续期只认"在另一个任务里又出现了"，被注入本身不算。"""
    from datetime import datetime, timedelta, timezone

    from blank_api.services.agent.persona import is_expired

    store, ws = _store()
    store.learn_from_text(ws, "改完必须跑全量回归", task_id="任务A")
    store.learn_from_text(ws, "改完必须跑全量回归", task_id="任务B")
    entries = store.load(ws)
    entries[0].last_confirmed = (
        datetime.now(timezone.utc) - timedelta(days=400)
    ).isoformat()
    store.save(ws, entries)

    block = store.persona_block(ws)  # 读一次

    assert block == ""
    assert is_expired(store.load(ws)[0]) is True


# ── 当面说的：一次就够 ────────────────────────────────────────────────────
def test_what_he_tells_you_directly_counts_once():
    """"出现两次"那个门槛是给**自动推断**用的：从行为里猜他要什么，猜错代价高。
    他当面说明白话是另一回事——那是最强的证据本身。"""
    store, ws = _store()

    touched = store.record_assertion(ws, "做事要严谨")

    assert touched and touched[0].status == "active"
    assert "严谨" in store.persona_block(str(ws))


def test_a_direct_statement_does_not_claim_certainty():
    store, ws = _store()

    touched = store.record_assertion(ws, "做事要严谨")

    assert touched[0].confidence == 0.8, "连他本人都可能是这次这么想"


def test_a_vague_statement_extracts_nothing():
    """听不出要求就返回空，不要硬凑一条——那正是"替他发明主张"。"""
    store, ws = _store()

    assert store.record_assertion(ws, "嗯") == []
    assert store.load(ws) == []


def test_a_direct_rule_actually_reaches_the_prompt():
    """置成 active 但 occurrences 还是 1 时，injectable 会按门槛把它滤掉——
    状态看着生效、提示里一条没有。这是今天第四次遇到"字段设了没人用"的形状。"""
    store, ws = _store()

    store.record_assertion(ws, "做数据库改动必须先问我")

    assert "做数据库改动必须先问我" in store.persona_block(str(ws))


def test_a_standard_is_expanded_not_listed_as_a_flat_rule():
    """标准要展开成工序；再并列一条"做事要严谨"只是重复。"""
    store, ws = _store()

    store.record_assertion(ws, "做事要严谨")
    block = store.persona_block(str(ws))

    assert "工序应该是" in block
    assert block.count("做事要严谨") == 0

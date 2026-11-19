"""自学习的存储形状与四条硬约束。

这不是"加个记忆功能"，是修一个**负收益**的现状：57 条记忆里 45 条是冒烟流水账，
同一条事实重复 13 次，两条互相矛盾的结论并存，而且执行时根本不注入。

每条规则都能追到实测出处（见 `docs/self-improvement-methods.md`）：
- 抽象子任务级经验正迁移，整任务总结负迁移（2604.27003 / 2608.20274）
- 注入 1 条最好，2 条、4 条更差（SWE-Exp k 消融：42.0 / 39.8 / 39.0）
- 失败经验必须绑外部证据，纯反思会污染（Reflexion 去掉测试：0.52 < 基线 0.60）
- 冲突记忆同时被检索，遵从后成功率 17–31%（2607.10608）
"""

from __future__ import annotations

from blank_api.services.agent.experience import (
    Experience,
    admission,
    merge,
    rank,
    render,
)


# ── 准入 ──────────────────────────────────────────────────────────────────
def test_a_smoke_test_task_does_not_become_memory():
    """冒烟任务占了我们现有记忆的 79%——正是它把记忆变成了噪音。"""
    verdict = admission(changed_paths=[], validation={"status": "skipped"}, readonly=True)

    assert verdict.learn is False
    assert "只读" in verdict.reason


def test_success_without_verification_is_not_learned():
    """"成功了但没人验证"写成"这样做能成"，是最危险的假阳性。"""
    verdict = admission(changed_paths=["a.py"], validation={}, succeeded=True)

    assert verdict.learn is False
    assert "没有验证" in verdict.reason


def test_a_failure_without_a_signal_is_not_learned():
    verdict = admission(changed_paths=["a.py"], failures=[{"tool": "run_tests"}], succeeded=False)

    assert verdict.learn is False


def test_a_failure_with_an_exit_code_is_learned_and_outranks_success():
    verdict = admission(
        changed_paths=[], failures=[{"tool": "run_tests", "exit_code": 1}], succeeded=False
    )

    assert verdict.learn is True
    assert verdict.kind == "pitfall"
    assert verdict.strength > 0.5, "踩坑的价值通常高于顺利"


def test_a_verified_change_is_learned():
    verdict = admission(changed_paths=["src/a.py"], validation={"status": "ok", "exit_code": 0})

    assert verdict.learn is True
    assert verdict.kind == "procedure"


# ── 去重与覆盖 ────────────────────────────────────────────────────────────
def _entry(statement: str = "改分页参数要同步更新 OpenAPI schema", **over) -> Experience:
    base = {
        "statement": statement,
        "trigger_paths": ["src/api/**"],
        "task_terms": ["pagination"],
        "error_signature": "expected 200 got 422",
        "confidence": 0.6,
    }
    base.update(over)
    return Experience(**base)


def test_the_same_claim_with_different_keywords_is_still_the_same_claim():
    """第一版把 trigger 混进指纹，关键词多写一个就成了两条——去重等于没做。"""
    a = _entry(task_terms=["pagination", "cursor"])
    b = _entry(task_terms=["pagination"])

    assert a.fingerprint == b.fingerprint


def test_repeating_the_same_lesson_confirms_instead_of_duplicating():
    entries, action = merge([_entry()], _entry())

    assert action == "confirmed"
    assert len(entries) == 1
    assert entries[0].confidence > 0.6
    assert entries[0].hits == 1


def test_confidence_never_reaches_certainty():
    """"从来没错过"在真实工程里不存在；写满了就再也降不下来。"""
    entries = [_entry()]
    for _ in range(20):
        entries, _ = merge(entries, _entry())

    assert entries[0].confidence <= 0.9


def test_a_contradicting_lesson_retires_the_old_one():
    """两条矛盾的记忆同时被检索到，遵从率 63–72%，遵从后成功率 17–31%。"""
    old = _entry("改分页参数要同步更新 OpenAPI schema")
    new = _entry("分页参数其实不用改 schema", error_signature="expected 200 got 422")

    entries, action = merge([old], new)

    assert action == "replaced"
    assert [e.status for e in entries] == ["retired", "candidate"]
    # 现行版本只有一条能在检索里出现
    assert all(e.status != "retired" for e in rank(entries, goal="改分页 cursor", paths=["src/api/x.py"]))


# ── 检索与剂量 ────────────────────────────────────────────────────────────
def test_only_one_experience_is_injected():
    """SWE-Exp 消融：1 条 42.0、2 条 39.8、4 条 39.0。多给只会互相干扰。"""
    entries = [_entry(f"经验 {i}", task_terms=["pagination"]) for i in range(4)]

    hits = rank(entries, goal="给列表加分页", paths=["src/api/list.py"])

    assert len(hits) == 1


def test_an_unrelated_task_gets_nothing():
    """想起一条不相干的经验，比想不起来更坏——它会带偏方向。"""
    hits = rank([_entry()], goal="改登录页配色", paths=["web/src/app.css"])

    assert hits == []


def test_low_confidence_entries_are_not_injected():
    hits = rank([_entry(confidence=0.2)], goal="改分页", paths=["src/api/x.py"])

    assert hits == []


def test_retired_entries_never_come_back():
    entries, _ = merge([_entry()], _entry("分页其实不用改 schema", error_signature="expected 200 got 422"))

    assert rank(entries, goal="改分页 cursor", paths=["src/api/x.py"])[0].status != "retired"


# ── 注入措辞 ──────────────────────────────────────────────────────────────
def test_the_wording_does_not_demand_obedience():
    """说"必须遵守"会让模型放弃验证；强制引用还会导致过度依赖（SWE-Exp）。"""
    text = render(_entry(evidence=[{"signal": "测试由 fail 转 pass", "pointer": "src/api/schemas.py#L88"}]))

    assert "可能不适用" in text
    assert "请自行验证" in text
    assert "必须" not in text
    assert "src/api/schemas.py#L88" in text, "要给得出证据指针，模型才能自己核对"


# ── 匹配的宽严 ────────────────────────────────────────────────────────────
def test_keywords_alone_are_not_enough_to_recall():
    """实测：路径完全不同的任务也曾想起同一条经验——靠的是 `deliverables`/`html`
    这类出现在几乎每个任务里的词。关键词只加分，硬条件只有路径或报错签名。"""
    entry = _entry(trigger_paths=["deliverables/_probe/a/index.html"], task_terms=["html"])

    assert rank([entry], goal="在 deliverables/other.html 写页面", paths=[]) == []


def test_a_path_from_the_goal_can_trigger_recall_on_the_first_turn():
    """任务刚开始还没有 changed_paths，但目标里常直接写着路径。"""
    from blank_api.services.agent.experience import paths_from_goal

    goal = "在 deliverables/_probe/learn-test/index.html 写一个页面，然后跑 run_tests"
    assert "_probe/learn-test/index.html" in " ".join(paths_from_goal(goal))

    entry = _entry(trigger_paths=["deliverables/_probe/learn-test/index.html"])
    assert rank([entry], goal=goal, paths=paths_from_goal(goal))


def test_path_fragments_are_not_used_as_keywords():
    """`deliverables` / `html` 这类路径碎片进了关键词，等于永远命中。"""
    from blank_api.services.agent.experience import _terms

    terms = _terms("在 deliverables/_probe/x/index.html 写页面并跑 run_tests", ["deliverables/_probe/x/index.html"])

    assert "deliverables" not in terms
    assert "html" not in terms
    assert "run_tests" in terms


# ── 读后反馈：hits/misses 要真的驱动晋级与退役 ────────────────────────────
def test_using_it_twice_well_promotes_it():
    """没有这一步，条目永远停在 candidate——写了、读了，但从不回溯。"""
    import tempfile
    from pathlib import Path

    from blank_api.services.agent.experience import ExperienceStore

    root = Path(tempfile.mkdtemp())
    store = ExperienceStore(root)
    store.record(root / "ws", _entry(confidence=0.5))
    fp = store.load(root / "ws")[0].fingerprint

    assert store.record_outcome(root / "ws", fp, success=True) == "candidate"
    assert store.record_outcome(root / "ws", fp, success=True) == "active"


def test_failing_twice_retires_it():
    """两次都没帮上忙就退役。留着只会继续被检索到、继续干扰。"""
    import tempfile
    from pathlib import Path

    from blank_api.services.agent.experience import ExperienceStore

    root = Path(tempfile.mkdtemp())
    store = ExperienceStore(root)
    store.record(root / "ws", _entry(confidence=0.6))
    fp = store.load(root / "ws")[0].fingerprint

    store.record_outcome(root / "ws", fp, success=False)
    assert store.record_outcome(root / "ws", fp, success=False) == "retired"
    assert store.recall(root / "ws", goal="改分页", paths=["src/api/x.py"]) == []


def test_the_step_sizes_are_asymmetric_on_purpose():
    """成功 +0.05、失败 −0.12：归因不对称，代价更该被记住。"""
    import tempfile
    from pathlib import Path

    from blank_api.services.agent.experience import ExperienceStore

    root = Path(tempfile.mkdtemp())
    store = ExperienceStore(root)
    store.record(root / "ws", _entry(confidence=0.6))
    fp = store.load(root / "ws")[0].fingerprint

    store.record_outcome(root / "ws", fp, success=False)
    after_miss = store.load(root / "ws")[0].confidence
    store.record_outcome(root / "ws", fp, success=True)
    after_hit = store.load(root / "ws")[0].confidence

    assert after_hit < 0.6, "一次失败 + 一次成功不该回到原点"
    assert after_hit > after_miss


# ── 体检：只报告，不自动修 ─────────────────────────────────────────────────
def test_lint_flags_two_entries_pointing_opposite_ways():
    """同一片路径上有人说"要"、有人说"不要"，检索到哪条都可能错。"""
    from blank_api.services.agent.experience import lint

    yes = Experience(statement="改分页时要同步更新 schema", trigger_paths=["src/api/"], status="active")
    no = Experience(statement="不要改分页的 schema", trigger_paths=["src/api/"], status="active")

    kinds = {item["kind"] for item in lint([yes, no])}

    assert "contradiction" in kinds


def test_lint_flags_a_crowded_area():
    """检索只能取 1 条，同路径堆了 4 条 active 等于随机挑一条。"""
    from blank_api.services.agent.experience import lint

    group = [
        Experience(statement=f"做法 {i}", trigger_paths=["src/api/"], status="active")
        for i in range(4)
    ]

    findings = lint(group)

    assert any(item["kind"] == "crowded" for item in findings)


def test_lint_reports_but_never_removes():
    """一条错记忆被自动删掉，用户不会知道；留在库里至少能被看见。
    （2610.04083：拿掉记忆工具后 agent 会改用文件系统，靠"删"挡不住。）"""
    from blank_api.services.agent.experience import lint

    entries = [Experience(statement="要不要改 schema", trigger_paths=["src/"], status="active"),
               Experience(statement="不要改 schema", trigger_paths=["src/"], status="active")]

    before = [item.status for item in entries]
    lint(entries)

    assert [item.status for item in entries] == before, "lint 不该改动任何条目"

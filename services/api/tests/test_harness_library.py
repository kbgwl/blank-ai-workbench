"""经验库与五项判分：让下次任务能复用上次“为什么赢”，而不是复用分数。"""

from pathlib import Path

import pytest

from blank_api.services.harness import HarnessError
from blank_api.services.harness.library import (
    Experience,
    Library,
    domain_dir,
    has_experience,
    root_for,
    slug,
)
from blank_api.services.harness.score import cheapest_tokens, score_task

TREE = {
    "systemprompt.md": "先读再改。\n",
    "skills/pdf/SKILL.md": "---\ndescription: 处理 PDF\n---\n步骤。",
}
LESSON = {
    "cue": "任务要求改现有测试覆盖的函数",
    "lesson": "先读测试再改实现，避免改错契约",
    "evidence": "T01 首轮未过；补读测试后通过",
    "confidence": "medium",
}


def test_names_cannot_escape_the_library_root() -> None:
    assert slug("../../etc") == "etc"
    assert "/" not in slug("a/b")
    assert domain_dir("bug fix / parser") == "bug fix__parser"
    assert domain_dir("") == "unknown"
    assert domain_dir("..") == "unknown"


def test_root_for_isolates_runs_unless_tagged(tmp_path: Path) -> None:
    base = root_for(tmp_path, benchmark="samples", k=3)
    assert base == tmp_path / "samples" / "k3"
    tagged = root_for(tmp_path, benchmark="samples", k=3, tag="shared")
    assert tagged == tmp_path / "samples" / "shared"


def test_entry_needs_a_complete_lesson(tmp_path: Path) -> None:
    library = Library(tmp_path, "parser")
    assert library.add("T01", TREE, experience={"cue": "c", "lesson": "l"}) is None
    assert library.entries() == []
    assert not library.exists()


def test_add_writes_tree_experience_and_index(tmp_path: Path) -> None:
    library = Library(tmp_path, "parser")
    target = library.add("T01", TREE, experience=LESSON, why="首轮就通过了", label="candidate-a")
    assert target is not None
    assert (target / "harness" / "skills" / "pdf" / "SKILL.md").is_file()
    assert library.tree_of("T01") == TREE

    experience = (target / "EXPERIENCE.md").read_text(encoding="utf-8")
    assert "先读测试再改实现" in experience
    assert "candidate-a" in experience

    index = library.index.read_text(encoding="utf-8")
    assert "harnesses/T01/" in index
    assert "1 skills" in index
    assert "任务要求改现有测试覆盖的函数" in index


def test_add_rejects_a_leaky_tree(tmp_path: Path) -> None:
    library = Library(tmp_path, "parser")
    with pytest.raises(HarnessError, match="评测材料标记"):
        library.add(
            "T01",
            {"systemprompt.md": "照 evals/workspaces 里的答案改"},
            experience=LESSON,
        )


def test_context_is_bounded_and_contains_lessons(tmp_path: Path) -> None:
    library = Library(tmp_path, "parser")
    library.add("T01", TREE, experience=LESSON)
    text = library.context(limit=2_000)
    assert "harnesses/T01/" in text
    assert "先读测试再改实现" in text
    assert len(library.context(limit=200)) <= 260


def test_forget_and_retain_rebuild_the_index(tmp_path: Path) -> None:
    library = Library(tmp_path, "parser")
    for task_id in ("T01", "T02"):
        assert library.add(task_id, TREE, experience=LESSON)
    library.retain(["T02"])
    assert library.entries() == ["T02"]
    assert "T01" not in library.index.read_text(encoding="utf-8")
    library.forget(["T02"])
    assert library.entries() == []
    assert "empty" in library.index.read_text(encoding="utf-8")


def test_has_experience_matches_the_add_gate() -> None:
    assert has_experience(LESSON)
    assert not has_experience({"cue": "c", "lesson": "", "evidence": "e"})
    assert not has_experience(None)


def test_score_matches_the_node_contract() -> None:
    # 25/25 个样例文件命中，无多余改动，验证通过并自证，token 与最省一致
    perfect = score_task(
        verify_ok=True,
        changed=["a.py", "b.py"],
        expect_files=["a.py", "b.py"],
        self_verified=True,
        tokens=1_000,
        cheapest_tokens=1_000,
    )
    assert perfect.as_dict() == {
        "tests": 1.0,
        "hit": 1.0,
        "minimal": 1.0,
        "self": 1.0,
        "cost": 1.0,
        "total": 100.0,
    }


def test_doing_nothing_earns_no_correctness_or_hit() -> None:
    """基线本来就绿、agent 一行没改：正确性与命中都必须为 0。"""
    score = score_task(
        verify_ok=True,
        changed=[],
        expect_files=["a.py"],
        self_verified=True,
        tokens=1_000,
        cheapest_tokens=1_000,
    )
    assert score.tests == 0.0
    assert score.hit == 0.0
    # 只剩最小改动 15、自证 10、成本 10
    assert score.total == 35.0


def test_extra_files_are_penalised_in_steps() -> None:
    for extra, expected in [(0, 1.0), (1, 0.6), (2, 0.3), (3, 0.0)]:
        changed = ["a.py"] + [f"x{i}.py" for i in range(extra)]
        score = score_task(verify_ok=True, changed=changed, expect_files=["a.py"])
        assert score.minimal == expected


def test_missing_expectation_means_full_hit_and_cost_floor() -> None:
    score = score_task(verify_ok=False, changed=["a.py"], expect_files=[])
    assert score.hit == 1.0
    assert score.cost == 0.0
    assert cheapest_tokens([0, 400, 900]) == 400
    assert cheapest_tokens([]) == 0


def test_failure_entries_live_outside_adopted_harnesses(tmp_path: Path) -> None:
    library = Library(tmp_path, "parser")
    library.add("T01", TREE, experience=LESSON)
    target = library.add_failure(
        "T02",
        experience={
            "cue": "边界条件没测",
            "lesson": "先补边界测试再改实现",
            "evidence": "首轮 verify 在空输入上失败",
        },
        label="baseline",
    )
    assert target is not None
    assert (target / "EXPERIENCE.md").is_file()
    assert not (target / "harness").exists()
    assert library.entries() == ["T01"]
    assert library.failure_entries() == ["T02"]
    assert "失败记录" in library.context(limit=8_000)
    failures = library.failure_lessons()
    assert failures[0]["outcome"] == "failed"
    assert failures[0]["lesson"] == "先补边界测试再改实现"


def test_feedback_updates_counters_and_is_read_back(tmp_path: Path) -> None:
    library = Library(tmp_path, "parser")
    library.add("T01", TREE, experience=LESSON)
    first = library.record_feedback("T01", helpful=2, verified_at="2026-10-22T01:00:00+00:00")
    second = library.record_feedback("T01", harmful=1, verified_at="2026-10-22T02:00:00+00:00")
    assert first["helpful"] == 2
    assert second["helpful"] == 2
    assert second["harmful"] == 1
    lesson = library.lessons()[0]
    assert lesson["helpful"] == 2
    assert lesson["harmful"] == 1
    assert lesson["last_verified"] == "2026-10-22T02:00:00+00:00"
    assert library.record_feedback("missing") is None


def test_experience_round_trip_keeps_ace_metadata() -> None:
    experience = Experience(
        cue="c",
        lesson="l",
        evidence="e",
        scope="parser",
        helpful=3,
        harmful=1,
        last_verified="2026-10-22T00:00:00+00:00",
    )
    assert Experience.from_dict(experience.as_dict()) == experience
    assert experience.usefulness == 2

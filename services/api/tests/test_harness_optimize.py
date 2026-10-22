"""推荐器与 Propose–Probe–Compose：候选必须可校验，赢家必须被测量过。"""

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from blank_api.services.harness import spec
from blank_api.services.harness.library import Library
from blank_api.services.harness.optimize import optimize
from blank_api.services.harness.proposer import (
    Candidate,
    ChatProposer,
    ProposalRequest,
    RuleProposer,
)

LESSON = {
    "cue": "T01 要求改现有函数",
    "lesson": "先读测试再改实现，避免改错契约",
    "evidence": "T01 首轮没过，补读测试后通过",
    "confidence": "medium",
}

BASE = {"systemprompt.md": "已有约定：保持提交信息简短。\n"}


def _library(tmp_path: Path) -> Library:
    library = Library(tmp_path, "解析器")
    library.add("T01", {"systemprompt.md": "先读再改。"}, experience=LESSON)
    return library


async def test_rule_proposer_returns_valid_distinct_candidates() -> None:
    proposer = RuleProposer()
    request = ProposalRequest(base=BASE, domain="解析器", count=4, library=None)
    candidates = await proposer.propose(request)

    assert [item.label for item in candidates] == ["domain", "guided", "workflow", "guarded"]
    assert len({item.fingerprint() for item in candidates}) == 4
    for item in candidates:
        spec.validate(item.tree)
        assert "保持提交信息简短" in item.tree["systemprompt.md"]
        assert "先读现状" in item.tree["systemprompt.md"]


async def test_domain_candidate_uses_the_library_without_leaking_task_ids(tmp_path: Path) -> None:
    proposer = RuleProposer()
    candidates = await proposer.propose(
        ProposalRequest(base={}, domain="解析器", count=1, library=_library(tmp_path))
    )
    tree = candidates[0].tree
    assert "先读测试再改实现" in tree["LongTermMEMORY.md"]
    assert "T01" not in tree["LongTermMEMORY.md"]
    assert "skills/task-playbook/SKILL.md" in tree


async def test_count_one_is_the_domain_candidate() -> None:
    candidates = await RuleProposer().propose(ProposalRequest(base={}, count=1))
    assert [item.label for item in candidates] == ["domain"]


class _Recorder:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply


async def test_chat_proposer_parses_fenced_json_and_skips_invalid() -> None:
    reply = """说明文字
```json
{"candidates":[
 {"label":"ok","why":"先读","files":{"systemprompt.md":"先读再改。"}},
 {"label":"empty","why":"空文件","files":{"systemprompt.md":""}}
]}
```"""
    chat = _Recorder(reply)
    candidates = await ChatProposer(chat).propose(
        ProposalRequest(base=BASE, domain="解析器", count=2)
    )
    assert [item.label for item in candidates] == ["ok"]
    assert candidates[0].origin == "model"
    assert "保持提交信息简短" in chat.prompts[0]


async def test_chat_proposer_falls_back_on_garbage() -> None:
    chat = _Recorder("这不是 JSON")
    candidates = await ChatProposer(chat, fallback=RuleProposer()).propose(
        ProposalRequest(base={}, count=2)
    )
    assert [item.label for item in candidates] == ["domain", "guided"]


@dataclass
class FakeRecord:
    verify_ok: bool
    changed: list[str]
    tokens: int
    self_verified: bool = True
    harness_dirty: bool = False
    status: str = "succeeded"
    verify_output: str = ""
    trajectory: str = ""
    error: str = ""


class FakeRunner:
    """The harness text is the only difference between runs."""

    def __init__(self, *, dirty_labels: tuple[str, ...] = (), baseline_green: bool = False) -> None:
        self.dirty_labels = dirty_labels
        self.baseline_green = baseline_green
        self.calls: list[str] = []

    async def run(self, sample, tree):
        prompt = tree.get("systemprompt.md", "")
        good = "先读现状" in prompt or (self.baseline_green and not prompt)
        dirty = any(label in prompt for label in self.dirty_labels)
        self.calls.append(sample["id"])
        if not good:
            return FakeRecord(
                verify_ok=False,
                changed=[],
                tokens=60,
                self_verified=False,
                verify_output="AssertionError: a.py missing",
                trajectory="[ACTION] write\n[OBSERVATION] wrote nothing useful",
            )
        return FakeRecord(
            verify_ok=True,
            changed=list(sample.get("expect_files") or []),
            tokens=100,
            harness_dirty=dirty,
            verify_output="1 passed",
            trajectory="[ACTION] read\n[OBSERVATION] read tests",
        )


class FixedProposer:
    def __init__(self, candidates: list[Candidate]) -> None:
        self.candidates = candidates

    async def propose(self, request):
        return self.candidates


GOOD = Candidate("good", {"systemprompt.md": "# 工作方式\n- 先读现状再改。\n"}, "先读再改")
BAD = Candidate("bad", {"systemprompt.md": "# 工作方式\n- 直接写。\n"}, "直接写")

SAMPLES = [
    {
        "id": "T01",
        "title": "加函数",
        "expect_files": ["a.py"],
    }
]


async def test_optimizer_adopts_the_measured_winner(tmp_path: Path) -> None:
    library = Library(tmp_path, "解析器")
    report = await optimize(
        SAMPLES,
        runner=FakeRunner(),
        proposer=FixedProposer([GOOD, BAD]),
        library=library,
        domain="解析器",
        run_id="r1",
    )

    assert report.winner is not None
    assert report.winner.candidate.label == "good"
    assert report.adopted is True
    assert report.gain > 50
    assert "good" in report.reason
    assert library.entries() == ["T01"]
    stored = library.tree_of("T01")
    assert stored == GOOD.tree
    assert "先读再改" in (library.entry_dir("T01") / "EXPERIENCE.md").read_text(encoding="utf-8")
    assert report.as_dict()["winner_tree"] == GOOD.tree


async def test_optimizer_keeps_baseline_when_gain_is_too_small(tmp_path: Path) -> None:
    report = await optimize(
        SAMPLES,
        runner=FakeRunner(baseline_green=True),
        proposer=FixedProposer([GOOD]),
        library=Library(tmp_path, "解析器"),
        domain="解析器",
        run_id="r2",
    )
    assert report.winner is None
    assert report.adopted is False
    assert "低于阈值" in report.reason


async def test_candidate_that_edits_its_own_harness_is_disqualified(tmp_path: Path) -> None:
    dirty = Candidate(
        "dirty",
        {"systemprompt.md": "# 工作方式\n- 先读现状再改。\n- DIRTY_MARKER\n"},
        "会改自己",
    )
    runner = FakeRunner(dirty_labels=("DIRTY_MARKER",))
    report = await optimize(
        SAMPLES,
        runner=runner,
        proposer=FixedProposer([dirty]),
        library=Library(tmp_path, "解析器"),
        domain="解析器",
        run_id="r3",
    )
    assert report.runs[0].disqualified
    assert report.winner is None
    assert "正确性守门" in report.reason


async def test_failed_runner_does_not_abort_the_round(tmp_path: Path) -> None:
    class Exploding:
        async def run(self, sample, tree):
            raise RuntimeError("boom")

    report = await optimize(
        SAMPLES,
        runner=Exploding(),
        proposer=FixedProposer([GOOD]),
        library=Library(tmp_path, "解析器"),
        domain="解析器",
        run_id="r4",
    )
    assert len(report.baseline.records) == 1
    assert report.baseline.records[0].status == "error"
    assert report.winner is None


async def test_token_budget_stops_before_probing_candidates(tmp_path: Path) -> None:
    report = await optimize(
        SAMPLES,
        runner=FakeRunner(),
        proposer=FixedProposer([GOOD]),
        library=Library(tmp_path, "解析器"),
        domain="解析器",
        max_tokens=1,
        run_id="r5",
    )
    assert report.budget_exhausted is True
    assert report.runs == []
    assert report.adopted is False


async def test_optimizer_rejects_an_empty_sample_list(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        await optimize([], runner=FakeRunner(), proposer=FixedProposer([GOOD]))


PRIMARY = [{"id": "T01", "title": "主任务", "expect_files": ["a.py"]}]
REGRESSION = [{"id": "R01", "title": "旧任务", "expect_files": ["r.py"]}]
RISKY = Candidate("risky", {"systemprompt.md": "# 工作方式\n- RISKY\n"}, "牺牲旧任务换主任务")
SAFE = Candidate("safe", {"systemprompt.md": "# 工作方式\n- SAFE\n"}, "两边都过")
EXPENSIVE = Candidate("expensive", {"systemprompt.md": "# 工作方式\n- EXPENSIVE\n"}, "token 很贵")


class GateRunner:
    """Baseline fails the primary task; RISKY trades regression for primary."""

    async def run(self, sample, tree):
        prompt = tree.get("systemprompt.md", "")
        primary = str(sample.get("id") or "").startswith("T")
        if "RISKY" in prompt:
            ok = primary
        elif "SAFE" in prompt or "EXPENSIVE" in prompt:
            ok = True
        else:
            ok = not primary
        tokens = 1_000 if "EXPENSIVE" in prompt else 100
        if ok:
            return FakeRecord(
                verify_ok=True,
                changed=list(sample.get("expect_files") or []),
                tokens=tokens,
                verify_output="ok",
            )
        return FakeRecord(
            verify_ok=False,
            changed=[],
            tokens=tokens,
            self_verified=False,
            verify_output="AssertionError: expected file missing",
        )


async def test_regression_gate_rejects_a_candidate_that_breaks_old_tasks(tmp_path: Path) -> None:
    report = await optimize(
        PRIMARY,
        runner=GateRunner(),
        proposer=FixedProposer([RISKY]),
        library=Library(tmp_path, "解析器"),
        regression_samples=REGRESSION,
        domain="解析器",
        run_id="g1",
    )
    assert report.winner is None
    assert report.adopted is False
    assert report.regression_count == 1
    assert "回归集掉分" in report.reason
    assert any("`risky` 未通过采纳门禁" in note for note in report.notes)


async def test_regression_tolerance_can_allow_a_known_tradeoff(tmp_path: Path) -> None:
    report = await optimize(
        PRIMARY,
        runner=GateRunner(),
        proposer=FixedProposer([RISKY]),
        library=Library(tmp_path, "解析器"),
        regression_samples=REGRESSION,
        max_regression_drop=80.0,
        domain="解析器",
        run_id="g2",
    )
    assert report.winner is not None
    assert report.winner.candidate.label == "risky"
    assert report.adopted is True
    assert report.regression_drop == 75.0


async def test_cost_gate_rejects_a_token_blowup(tmp_path: Path) -> None:
    report = await optimize(
        PRIMARY,
        runner=GateRunner(),
        proposer=FixedProposer([EXPENSIVE]),
        library=Library(tmp_path, "解析器"),
        max_token_growth=1.0,
        domain="解析器",
        run_id="g3",
    )
    assert report.winner is None
    assert "token 增长" in report.reason


class SplitRunner:
    """ONLY_A passes the first task, ONLY_B passes the second; neither is whole."""

    async def run(self, sample, tree):
        prompt = tree.get("systemprompt.md", "")
        sid = str(sample.get("id") or "")
        ok = ("ONLY_A" in prompt and sid == "T01") or (
            "ONLY_B" in prompt and sid == "T02"
        )
        if ok:
            return FakeRecord(
                verify_ok=True,
                changed=list(sample.get("expect_files") or []),
                tokens=100,
                verify_output="ok",
            )
        return FakeRecord(
            verify_ok=False,
            changed=[],
            tokens=100,
            self_verified=False,
            verify_output="missing",
        )


async def test_pareto_archive_keeps_incomparable_candidates(tmp_path: Path) -> None:
    only_a = Candidate("only-a", {"systemprompt.md": "# 工作方式\n- ONLY_A\n"}, "只过第一题")
    only_b = Candidate("only-b", {"systemprompt.md": "# 工作方式\n- ONLY_B\n"}, "只过第二题")
    samples = [
        {"id": "T01", "title": "第一题", "expect_files": ["a.py"]},
        {"id": "T02", "title": "第二题", "expect_files": ["b.py"]},
    ]
    report = await optimize(
        samples,
        runner=SplitRunner(),
        proposer=FixedProposer([only_a, only_b]),
        library=Library(tmp_path, "解析器"),
        domain="解析器",
        run_id="g4",
    )
    assert report.winner is None
    assert {item["label"] for item in report.pareto} == {"only-a", "only-b"}


class RecordingReflector:
    def __init__(self, candidates: list[Candidate]) -> None:
        self.candidates = candidates
        self.requests: list = []

    async def propose(self, request):
        self.requests.append(request)
        return self.candidates


REFLECTED = Candidate(
    "reflected",
    {"systemprompt.md": "# 工作方式\n- 先读现状再改。\n"},
    "按 AssertionError 补上先读测试",
    origin="reflection",
)


async def test_reflection_uses_failure_evidence_and_remeasures(tmp_path: Path) -> None:
    reflector = RecordingReflector([REFLECTED])
    report = await optimize(
        SAMPLES,
        runner=FakeRunner(),
        proposer=FixedProposer([BAD]),
        reflector=reflector,
        library=Library(tmp_path, "解析器"),
        domain="解析器",
        run_id="g5",
    )
    assert reflector.requests, "首轮失败后必须触发一次反思"
    evidence = reflector.requests[0].failures
    assert evidence
    assert any("AssertionError" in item["verify_output"] for item in evidence)
    assert report.reflections == 1
    assert report.winner is not None
    assert report.winner.candidate.label == "reflected"
    assert report.winner.candidate.origin == "reflection"
    assert report.adopted is True


async def test_reflection_is_skipped_when_nothing_is_unresolved(tmp_path: Path) -> None:
    reflector = RecordingReflector([REFLECTED])
    report = await optimize(
        SAMPLES,
        runner=FakeRunner(baseline_green=True),
        proposer=FixedProposer([GOOD]),
        reflector=reflector,
        library=Library(tmp_path, "解析器"),
        domain="解析器",
        run_id="g6",
    )
    assert reflector.requests == []
    assert report.reflections == 0
    assert any("没有仍未通过" in note for note in report.notes)

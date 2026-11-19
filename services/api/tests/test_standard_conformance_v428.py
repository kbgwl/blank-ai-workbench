"""标准（做事的方式）→ 工序 → 可核对。

这条线要回答的问题和"任务成不成功"不一样：

> 一个任务可以成功，但用的不是他要的做法。

比如他说"做事要严谨"，而这次我们改了文件、没跑验证、结论写"应该没问题"——
任务算成功了，但那**不是他要的方式**。所以判据从"成功率"换成"符合度"。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from blank_api.services.agent.persona import (  # noqa: E402
    PersonaStore,
    conformance,
    extract_instructions,
    render_standards,
    standards_of,
)


def _with_standard(tmp_path, phrase="做事要严谨", times=2):
    store = PersonaStore(tmp_path / "persona")
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    for index in range(times):
        store.learn_from_text(str(ws), phrase, task_id=f"T{index}", scope_ref=str(ws))
    return store, ws


# ── 认得出来 ──────────────────────────────────────────────────────────────
def test_a_standard_sentence_has_no_imperative_verb_and_still_counts():
    """"做事要严谨"里没有"必须/不要/先/再"——按祈使词筛会把这类全丢掉，
    而它们恰恰最影响"像不像他"。"""
    assert extract_instructions("做事要严谨") == ["做事要严谨"]


def test_a_standard_lands_in_its_own_dimension():
    from blank_api.services.agent.persona import dimension_of

    assert dimension_of("做事要严谨") == "standard"
    # 普通要求不该被误判成标准
    assert dimension_of("改完必须跑全量回归") == "verify"


def test_the_expansion_is_labelled_as_our_interpretation():
    """展开的条款是我们的理解，不是他的原话。混淆这两件事就是另一种跑偏。"""
    block = render_standards(["rigor"])

    assert "改完必须验证" in block
    assert "不是他的原话" in block


def test_a_candidate_standard_is_not_used_yet(tmp_path):
    store, ws = _with_standard(tmp_path, times=1)

    assert standards_of(store.load(ws)) == []
    assert store.persona_block(str(ws)) == ""


def test_two_mentions_make_the_standard_live(tmp_path):
    store, ws = _with_standard(tmp_path, times=2)

    assert standards_of(store.load(ws)) == ["rigor"]
    block = store.persona_block(str(ws))
    assert "工序应该是" in block
    assert "逐条核对" in block


# ── 核对：符合度 ──────────────────────────────────────────────────────────
def _by_clause(results):
    return {item["clause"]: item for item in results}


def test_a_successful_task_can_still_fail_his_standard():
    """这是整条线存在的理由：跑通了不等于按他的方式做的。"""
    results = _by_clause(
        conformance(
            ["rigor"],
            {
                "changed_paths": ["src/a.py"],
                "validation": {},
                "report": "已完成，应该没问题。",
                "failures": 0,
            },
        )
    )

    assert results["change_is_verified"]["status"] == "failed"
    assert results["no_bare_claims"]["status"] == "failed"
    assert results["report_is_traceable"]["status"] == "failed"


def test_a_rigorous_delivery_passes():
    results = _by_clause(
        conformance(
            ["rigor"],
            {
                "changed_paths": ["src/a.py"],
                "validation": {"exit_code": 0},
                "report": "改了 src/a.py，跑 `pytest -q tests/test_a.py` 退出码 0；"
                "未验证：移动端。",
                "failures": 1,
            },
        )
    )

    assert all(item["status"] == "passed" for item in results.values()), results


def test_admitting_a_gap_is_not_a_bare_claim():
    """说"未验证"是严谨，不是含糊——两者必须区分开，否则会惩罚诚实。"""
    results = _by_clause(
        conformance(
            ["rigor"],
            {
                "changed_paths": ["a.py"],
                "validation": {"exit_code": 0},
                "report": "改了 a.py，`pytest` 退出码 0。移动端未验证，可能会错位。",
                "failures": 0,
            },
        )
    )

    assert results["no_bare_claims"]["status"] == "passed"


def test_unknown_is_not_failed():
    """没改文件就无从核对——记 unverifiable，不记 failed。和 done_when 同一条纪律。"""
    results = _by_clause(
        conformance(["rigor"], {"changed_paths": [], "report": "看了一下，没改。", "failures": 0})
    )

    assert results["change_is_verified"]["status"] == "unverifiable"
    assert results["report_is_traceable"]["status"] == "unverifiable"
    assert results["failures_are_admitted"]["status"] == "unverifiable"


def test_a_failure_that_is_hidden_from_the_report_is_caught():
    results = _by_clause(
        conformance(
            ["rigor"],
            {
                "changed_paths": ["a.py"],
                "validation": {"exit_code": 0},
                "report": "改了 a.py，一切正常，全部通过。",
                "failures": 2,
            },
        )
    )

    assert results["failures_are_admitted"]["status"] == "failed"


def test_concise_standard_checks_length_and_padding():
    long_report = "已完成如下工作。" + "这里是一大段过程叙述。" * 100
    results = _by_clause(
        conformance(["concise"], {"changed_paths": [], "report": long_report, "failures": 0})
    )

    assert results["report_is_short"]["status"] == "failed"
    assert results["no_padding"]["status"] == "failed"


# ── 接线：符合度必须真的记下来 ────────────────────────────────────────────
class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def add(self, kind, node, payload):
        self.events.append((kind, payload))


@pytest.mark.asyncio
async def test_conformance_is_not_recorded_again_during_consolidation(tmp_path, monkeypatch):
    """符合度在**落状态之前**就算过了。收尾这里再记一次，同一个交付会被核两遍，
    指标的分母直接翻倍——所以这条钉的是"不许重复记"。

    （真正的记录时机由 `test_handoff_e2e_v12` 那条全链路用例保证。）
    """
    monkeypatch.setenv("BLANK_PERSONA_DIR", str(tmp_path / "persona"))
    monkeypatch.setenv("BLANK_EXPERIENCE_DIR", str(tmp_path / "experience"))

    from blank_api.services.agent.runtime import AgentRuntime

    store = PersonaStore(tmp_path / "persona")
    for index in range(2):
        store.learn_from_text(str(tmp_path), "做事要严谨", task_id=f"T{index}", scope_ref=str(tmp_path))

    recorder = _Recorder()
    scope = SimpleNamespace(
        task_id="task-1",
        session=SimpleNamespace(goal="改个页面", context=SimpleNamespace(root=tmp_path)),
        recorder=recorder,
    )
    result = {
        "tool_results": [
            {
                "ok": True,
                "tool": "write_file",
                "data": {"path": "a.py", "change_type": "created", "diff": "+x"},
            }
        ],
        "validation": {},
        "summary": "已完成，应该没问题。",
    }

    await AgentRuntime.__dict__["_consolidate"](None, scope, result, title="t")

    assert "standard.conformance" not in [kind for kind, _ in recorder.events]

"""轨迹反思提案器：证据进提示、基座被保留、泄题候选被拒绝。"""

import json
from pathlib import Path

from blank_api.services.harness import spec
from blank_api.services.harness.library import Library
from blank_api.services.harness.proposer import (
    ChatProposer,
    ProposalRequest,
    ReflectionProposer,
    RuleProposer,
    render_evidence,
)

BASE = {"systemprompt.md": "# 旧规则\n- 保持提交信息简短。\n"}


class Recorder:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply


EVIDENCE = {
    "sample_id": "任务A",
    "title": "修复解析器",
    "label": "bad",
    "verify_ok": False,
    "changed": [],
    "expect_files": ["parser.py"],
    "score": 25.0,
    "tokens": 120,
    "verify_output": "AssertionError: parser.py::test_edge failed",
    "trajectory": "[ACTION] apply_patch\n[OBSERVATION] patched the wrong branch",
    "error": "",
}


async def test_reflection_prompt_carries_evidence_and_merges_the_base_tree() -> None:
    reply = json.dumps(
        {
            "candidates": [
                {
                    "label": "fix-root-cause",
                    "why": "证据里的 AssertionError 指向边界分支，先补读测试再改。",
                    "files": {"guardrails.md": "# 硬性约束\n- 改边界前先读对应测试。\n"},
                }
            ]
        },
        ensure_ascii=False,
    )
    chat = Recorder(reply)
    candidates = await ReflectionProposer(chat).propose(
        ProposalRequest(base=BASE, domain="解析器", title="修复解析器", count=1, failures=[EVIDENCE])
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.origin == "reflection"
    assert candidate.tree["systemprompt.md"] == BASE["systemprompt.md"]
    assert "guardrails.md" in candidate.tree
    spec.validate(candidate.tree)

    prompt = chat.prompts[0]
    assert "AssertionError" in prompt
    assert "apply_patch" in prompt
    assert "parser.py" in prompt


async def test_reflection_without_evidence_falls_back_to_rules() -> None:
    chat = Recorder("不应被调用")
    candidates = await ReflectionProposer(chat, fallback=RuleProposer()).propose(
        ProposalRequest(base={}, domain="解析器", count=2)
    )
    assert chat.prompts == []
    assert [item.label for item in candidates] == ["domain", "guided"]


async def test_reflection_rejects_a_leaky_candidate() -> None:
    reply = json.dumps(
        {
            "candidates": [
                {
                    "label": "leaky",
                    "why": "照抄",
                    "files": {"systemprompt.md": "照 evals/workspaces 里的答案改。"},
                }
            ]
        },
        ensure_ascii=False,
    )
    candidates = await ReflectionProposer(Recorder(reply)).propose(
        ProposalRequest(base=BASE, failures=[EVIDENCE], count=1)
    )
    assert candidates == []


async def test_chat_proposer_also_merges_with_the_base_tree() -> None:
    reply = json.dumps(
        {
            "candidates": [
                {
                    "label": "partial",
                    "why": "只改工具说明",
                    "files": {"tool_descriptions/tool_guidance.md": "# 工具说明\n- 先搜再读。\n"},
                }
            ]
        },
        ensure_ascii=False,
    )
    candidates = await ChatProposer(Recorder(reply)).propose(
        ProposalRequest(base=BASE, count=1)
    )
    assert candidates[0].tree["systemprompt.md"] == BASE["systemprompt.md"]
    assert "tool_descriptions/tool_guidance.md" in candidates[0].tree


async def test_rule_proposer_injects_failures_as_warnings_not_rules(tmp_path: Path) -> None:
    library = Library(tmp_path, "解析器")
    library.add_failure(
        "T99",
        experience={
            "cue": "T99 缺少边界测试",
            "lesson": "先读测试再改边界",
            "evidence": "T99 首轮验证失败",
        },
    )
    candidates = await RuleProposer().propose(
        ProposalRequest(base={}, domain="解析器", count=1, library=library)
    )
    memory = candidates[0].tree["LongTermMEMORY.md"]
    assert "失败教训（仅作警示）" in memory
    assert "避免（同类任务 缺少边界测试）：先读测试再改边界" in memory
    assert "T99" not in memory


async def test_memory_ranking_prefers_lessons_with_positive_feedback(tmp_path: Path) -> None:
    library = Library(tmp_path, "解析器")
    library.add(
        "T01",
        {"systemprompt.md": "普通做法。\n"},
        experience={"cue": "普通任务", "lesson": "普通做法：先写代码", "evidence": "e1"},
    )
    library.add(
        "T02",
        {"systemprompt.md": "被点赞做法。\n"},
        experience={"cue": "被点赞任务", "lesson": "被点赞做法：先读测试", "evidence": "e2"},
    )
    library.record_feedback("T02", helpful=3, verified_at="2026-10-22T00:00:00+00:00")

    lessons = {item["task"]: item for item in library.lessons()}
    assert lessons["T02"]["helpful"] == 3
    assert lessons["T02"]["last_verified"] == "2026-10-22T00:00:00+00:00"

    candidates = await RuleProposer().propose(
        ProposalRequest(base={}, domain="解析器", count=1, library=library)
    )
    memory = candidates[0].tree["LongTermMEMORY.md"]
    assert memory.index("被点赞做法") < memory.index("普通做法")


def test_render_evidence_redacts_task_ids_and_eval_markers() -> None:
    text = render_evidence(
        [
            {
                "sample_id": "T01",
                "label": "candidate",
                "verify_output": "T01 failed; expect_files not met",
                "trajectory": "T01 -> missing",
            }
        ]
    )
    assert "T01" not in text
    assert "expect_files" not in text

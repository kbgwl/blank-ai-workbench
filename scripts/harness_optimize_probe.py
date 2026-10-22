"""Offline probe for the adoption gates, reflection loop and failure library.

No server and no model: a scripted agent reacts to markers in the installed
harness, while everything that decides the outcome is real — the adapter builds
workspaces and runs the verify command as a subprocess, the optimizer measures
every candidate, the reflection proposer receives the actual verify output, and
the library writes the adopted tree to disk.

The three checks that must fail if the code regresses:

1. A candidate that fixes the primary task but breaks a regression task is
   rejected by the regression gate; a safe candidate is adopted.
2. A failed baseline is stored under ``failures/`` and is never listed as an
   adopted harness.
3. A reflection candidate is proposed from the real ``CHECK-FAILED`` output,
   re-measured, and adopted; feedback counters are then read back.

    uv run --project services/api python scripts/harness_optimize_probe.py

Set KEEP_PROBE=1 to keep the temp library/workspaces for inspection.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from blank_api.services.harness import spec
from blank_api.services.harness.adapter import BlankEvalAdapter
from blank_api.services.harness.library import Library
from blank_api.services.harness.optimize import optimize
from blank_api.services.harness.proposer import (
    Candidate,
    ProposalRequest,
    ReflectionProposer,
    RuleProposer,
)

TMP = Path(tempfile.mkdtemp(prefix="blank-optimize-probe-"))

CHECK_PRIMARY = """import pathlib
import sys

path = pathlib.Path("primary.txt")
if not path.exists() or path.read_text(encoding="utf-8").strip() != "fixed":
    print("CHECK-FAILED: primary.txt must contain fixed")
    sys.exit(1)
"""

CHECK_LEGACY = """import pathlib
import sys

path = pathlib.Path("legacy.txt")
if not path.exists() or path.read_text(encoding="utf-8").strip() != "kept":
    print("CHECK-FAILED: legacy.txt must contain kept")
    sys.exit(1)
"""

PRIMARY = {
    "id": "probe-primary",
    "title": "修复主任务",
    "prompt": "让 primary.txt 等于 fixed",
    "acceptance": ["primary.txt 内容为 fixed"],
    "files": {"check_primary.py": CHECK_PRIMARY},
    "verify": "python check_primary.py",
    "expect_files": ["primary.txt"],
}

REGRESSION = {
    "id": "probe-regression",
    "title": "不破坏旧任务",
    "prompt": "保持 legacy.txt 等于 kept",
    "acceptance": ["legacy.txt 内容为 kept"],
    "files": {"check_legacy.py": CHECK_LEGACY},
    "verify": "python check_legacy.py",
    "expect_files": ["legacy.txt"],
}

BASE = {"systemprompt.md": "# 基线\n"}


class FakeApi:
    """Just enough of the task API for the adapter; the agent is scripted."""

    def __init__(self, agent) -> None:
        self.agent = agent
        self.workspace: Path | None = None
        self.calls: list[tuple[str, str]] = []

    async def request(self, method: str, path: str, body=None):
        self.calls.append((method, path))
        if method == "POST" and path == "/projects":
            return {"id": "p1"}
        if method == "POST" and path == "/projects/p1/workspaces":
            self.workspace = Path(body["path"])
            return {"id": "w1"}
        if method == "POST" and path == "/projects/p1/tasks":
            return {"id": "t1"}
        if method == "POST" and path == "/tasks/t1/start":
            if self.workspace is None:
                raise AssertionError("workspace not bound")
            self.agent(self.workspace)
            return {"task_id": "t1", "status": "queued"}
        if method == "GET" and path.startswith("/tasks/t1/evidence"):
            return {
                "task": {"status": "succeeded", "result_summary": "已完成"},
                "usage": {"total_tokens": 100, "calls": 1, "models": ["scripted"]},
                "tools": [
                    {
                        "name": "run_tests",
                        "node": "act",
                        "status": "ok",
                        "arguments": {},
                    }
                ],
            }
        if method == "GET" and path.startswith("/tasks/t1/events"):
            return [
                {
                    "seq": 1,
                    "type": "tool.requested",
                    "payload": {"tool": "apply_patch", "arguments": {"path": "work"}},
                },
                {
                    "seq": 2,
                    "type": "tool.completed",
                    "payload": {"tool": "run_tests", "status": "ok", "summary": "1 passed"},
                },
            ]
        if method == "DELETE" and path == "/projects/p1":
            return None
        raise AssertionError(f"unexpected call: {method} {path}")

    async def close(self) -> None:
        return None


def scripted_agent(workspace: Path) -> None:
    installed = spec.read_tree(workspace / spec.HARNESS_DIR)
    blob = "\n".join(installed.values())
    if (workspace / "check_legacy.py").is_file():
        legacy = "broken" if "BREAK-LEGACY" in blob else "kept"
        (workspace / "legacy.txt").write_text(legacy, encoding="utf-8")
    if (workspace / "check_primary.py").is_file() and "PRIMARY-FIX" in blob:
        (workspace / "primary.txt").write_text("fixed", encoding="utf-8")


class GateProposer:
    async def propose(self, request: ProposalRequest) -> list[Candidate]:
        return [
            Candidate(
                "risky",
                {"systemprompt.md": "# 工作方式\n- PRIMARY-FIX\n- BREAK-LEGACY\n"},
                why="修主任务，但顺手改了旧逻辑",
            ),
            Candidate(
                "safe",
                {"systemprompt.md": "# 工作方式\n- PRIMARY-FIX\n"},
                why="只改主任务要求的产物",
            ),
        ]


class NoopProposer:
    async def propose(self, request: ProposalRequest) -> list[Candidate]:
        return [
            Candidate(
                "noop",
                {"systemprompt.md": "# 基线\n- 先读再改。\n"},
                why="未针对失败证据做任何修改",
            )
        ]


def check(name: str, condition: bool, detail: str = "", failures: list[str] | None = None) -> None:
    print(f"{'PASS' if condition else 'FAIL'} {name}{(' — ' + detail) if detail else ''}")
    if not condition and failures is not None:
        failures.append(name)


async def gate_round(failures: list[str]) -> None:
    library = Library(TMP / "library-gates", "探针门禁")
    api = FakeApi(scripted_agent)
    adapter = BlankEvalAdapter(
        api,
        root=TMP / "runs-gates",
        python_executable=sys.executable,
        poll_interval=0.1,
        timeout=60.0,
    )
    report = await optimize(
        [PRIMARY],
        runner=adapter,
        proposer=GateProposer(),
        library=library,
        domain="探针门禁",
        base_tree=BASE,
        regression_samples=[REGRESSION],
        min_gain=0.5,
        max_regression_drop=0.0,
        max_token_growth=1.0,
        count=2,
        run_id="gates",
    )

    check("gate: safe candidate adopted", report.adopted and report.winner is not None,
          report.reason, failures)
    check("gate: winner is the safe tree", bool(report.winner and report.winner.candidate.label == "safe"),
          report.winner.candidate.label if report.winner else "none", failures)
    check("gate: risky candidate hit the regression gate",
          any("回归集掉分" in note for note in report.notes), str(report.notes), failures)
    check("gate: regression baseline is 100",
          bool(report.baseline and report.baseline.regression_mean == 100.0),
          str(report.baseline.regression_mean if report.baseline else None), failures)
    check("gate: winner kept the regression task green",
          bool(report.winner and report.winner.regression_mean == 100.0),
          str(report.winner.regression_mean if report.winner else None), failures)

    adopted_tree = library.tree_of("probe-primary")
    check("gate: adopted tree is on disk with the measured marker",
          "PRIMARY-FIX" in adopted_tree.get("systemprompt.md", ""), str(sorted(adopted_tree)),
          failures)
    check("gate: failed baseline is not an adopted harness",
          library.entries() == ["probe-primary"]
          and library.failure_entries() == ["probe-primary-baseline"],
          f"adopted={library.entries()} failures={library.failure_entries()}", failures)
    failure_text = (library.failure_dir("probe-primary-baseline") / "EXPERIENCE.md").read_text(
        encoding="utf-8"
    )
    check("gate: failure record carries the real verify output",
          "CHECK-FAILED" in failure_text or "验证未通过" in failure_text, failure_text[:200],
          failures)


async def reflection_round(failures: list[str]) -> None:
    library = Library(TMP / "library-reflection", "探针反思")
    api = FakeApi(scripted_agent)
    adapter = BlankEvalAdapter(
        api,
        root=TMP / "runs-reflection",
        python_executable=sys.executable,
        poll_interval=0.1,
        timeout=60.0,
    )
    prompts: list[str] = []

    async def chat(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps(
            {
                "candidates": [
                    {
                        "label": "reflected",
                        "why": "验证输出显示 primary.txt 缺失，先创建验收要求的产物再收尾。",
                        "files": {"systemprompt.md": "# 工作方式\n- PRIMARY-FIX\n"},
                    }
                ]
            },
            ensure_ascii=False,
        )

    report = await optimize(
        [PRIMARY],
        runner=adapter,
        proposer=NoopProposer(),
        reflector=ReflectionProposer(chat),
        library=library,
        domain="探针反思",
        base_tree=BASE,
        regression_samples=[REGRESSION],
        reflect_rounds=1,
        count=1,
        run_id="reflection",
    )

    check("reflection: prompt received the real CHECK-FAILED output",
          bool(prompts) and "CHECK-FAILED" in prompts[0],
          prompts[0][:180] if prompts else "no prompt", failures)
    check("reflection: one candidate was proposed and measured", report.reflections == 1,
          str(report.reflections), failures)
    check("reflection: reflected candidate adopted",
          report.adopted and bool(report.winner and report.winner.candidate.origin == "reflection"),
          report.reason, failures)
    check("reflection: adopted tree contains the evidence-driven edit",
          "PRIMARY-FIX" in library.tree_of("probe-primary").get("systemprompt.md", ""),
          str(sorted(library.tree_of("probe-primary"))), failures)
    check("reflection: baseline failure still recorded",
          library.failure_entries() == ["probe-primary-baseline"],
          str(library.failure_entries()), failures)

    feedback = library.record_feedback("probe-primary", helpful=2)
    lessons = library.lessons()
    check("feedback: counters are written and read back",
          bool(feedback) and lessons and lessons[0]["helpful"] == 2,
          str(lessons[0] if lessons else None), failures)

    proposal = await RuleProposer().propose(
        ProposalRequest(
            base={"guardrails.md": "# 硬性约束\n"},
            domain="探针反思",
            title=PRIMARY["title"],
            count=1,
            library=library,
        )
    )
    memory = proposal[0].tree.get("LongTermMEMORY.md", "") if proposal else ""
    check("feedback: lesson enters always-on memory",
          "同类任务" in memory and "primary.txt" in memory, memory[:300], failures)
    check("feedback: failure record is injected as a warning",
          "失败教训（仅作警示）" in memory and "避免" in memory, memory[:300], failures)


async def main() -> int:
    failures: list[str] = []
    await gate_round(failures)
    await reflection_round(failures)

    print(f"tmp={TMP}")
    if failures:
        print(f"FAILURES: {failures}")
        return 1
    print("OPTIMIZE PROBE OK")
    if os.environ.get("KEEP_PROBE") != "1":
        shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

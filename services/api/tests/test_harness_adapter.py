"""评测适配器：真实验证、改动归属、harness 自改检测与超时取消。"""

import sys
from pathlib import Path

import pytest

from blank_api.services.harness.adapter import (
    BlankEvalAdapter,
    changed_files,
    render_trajectory,
)

CHECKER = """import pathlib
import sys

sys.exit(0 if pathlib.Path("done.txt").exists() else 1)
"""

SAMPLE = {
    "id": "T01",
    "title": "写一个文件",
    "prompt": "创建 done.txt",
    "acceptance": ["done.txt 存在"],
    "verify": "python check.py",
    "expect_files": ["done.txt"],
    "files": {
        "check.py": CHECKER,
        "src/a.py": "VALUE = 1\n",
    },
}

TREE = {"systemprompt.md": "# 工作方式\n- 先读再改。\n", "guardrails.md": "# 硬性约束\n"}


class FakeApi:
    def __init__(self, *, statuses=("succeeded",), on_run=None) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.statuses = list(statuses)
        self.status = self.statuses.pop(0) if self.statuses else "succeeded"
        self.workspace: Path | None = None
        self.on_run = on_run
        self.events = [
            {
                "seq": 1,
                "type": "tool.requested",
                "payload": {"tool": "apply_patch", "arguments": {"path": "done.txt"}},
            },
            {
                "seq": 2,
                "type": "tool.completed",
                "payload": {"tool": "run_tests", "status": "ok", "summary": "3 passed"},
            },
        ]

    async def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST" and path == "/projects":
            return {"id": "p1"}
        if method == "POST" and path == "/projects/p1/workspaces":
            self.workspace = Path(body["path"])
            return {"id": "w1"}
        if method == "POST" and path == "/projects/p1/tasks":
            return {"id": "t1"}
        if method == "POST" and path == "/tasks/t1/start":
            if self.on_run is not None and self.workspace is not None:
                self.on_run(self.workspace)
            return {"task_id": "t1", "status": "queued"}
        if method == "GET" and path.startswith("/tasks/t1/evidence"):
            return {
                "task": {"status": self.status, "result_summary": "完成了"},
                "usage": {"total_tokens": 123, "calls": 2, "models": ["fake-model"]},
                "tools": [{"name": "run_tests", "status": "ok", "arguments": {}}],
            }
        if method == "POST" and path == "/tasks/t1/approval":
            self.status = "succeeded"
            return {"task": {"status": self.status}}
        if method == "POST" and path == "/tasks/t1/cancel":
            self.status = "cancelled"
            return {"status": "cancelled"}
        if method == "GET" and path.startswith("/tasks/t1/events"):
            return self.events
        if method == "DELETE" and path == "/projects/p1":
            return None
        raise AssertionError(f"unexpected call: {method} {path}")

    async def close(self):
        return None

    def called(self, method: str, path: str) -> bool:
        return any(m == method and p == path for m, p, _ in self.calls)


async def test_run_collects_evidence_and_verifies_itself(tmp_path: Path) -> None:
    def agent(workspace: Path) -> None:
        (workspace / "done.txt").write_text("ok\n", encoding="utf-8")

    api = FakeApi(on_run=agent)
    adapter = BlankEvalAdapter(
        api,
        root=tmp_path / "work",
        python_executable=sys.executable,
        poll_interval=0.01,
    )
    record = await adapter.run(SAMPLE, TREE)

    assert record.status == "succeeded"
    assert record.changed == ["done.txt"]
    assert record.verify_ok is True
    assert record.tokens == 123
    assert record.turns == 2
    assert record.self_verified is True
    assert record.harness_dirty is False
    assert "[ACTION] apply_patch" in record.trajectory
    assert "[OBSERVATION] run_tests ok 3 passed" in record.trajectory
    assert not (tmp_path / "work" / "T01").exists()


async def test_harness_edit_during_run_marks_the_record_dirty(tmp_path: Path) -> None:
    def agent(workspace: Path) -> None:
        (workspace / "done.txt").write_text("ok\n", encoding="utf-8")
        (workspace / ".blank" / "harness" / "systemprompt.md").write_text(
            "# 改过的\n", encoding="utf-8"
        )

    adapter = BlankEvalAdapter(
        FakeApi(on_run=agent),
        root=tmp_path / "work",
        python_executable=sys.executable,
        poll_interval=0.01,
    )
    record = await adapter.run(SAMPLE, TREE)
    assert record.harness_dirty is True
    assert "修改" in record.harness_note
    assert ".blank/harness/systemprompt.md" not in record.changed


async def test_waiting_approval_is_approved_and_the_run_continues(tmp_path: Path) -> None:
    def agent(workspace: Path) -> None:
        (workspace / "done.txt").write_text("ok\n", encoding="utf-8")

    api = FakeApi(statuses=["waiting_approval"], on_run=agent)
    adapter = BlankEvalAdapter(
        api,
        root=tmp_path / "work",
        python_executable=sys.executable,
        poll_interval=0.01,
    )
    record = await adapter.run(SAMPLE, TREE)
    assert record.status == "succeeded"
    assert api.called("POST", "/tasks/t1/approval")


async def test_timeout_cancels_the_task_before_verifying(tmp_path: Path) -> None:
    api = FakeApi(statuses=["running"])
    adapter = BlankEvalAdapter(
        api,
        root=tmp_path / "work",
        python_executable=sys.executable,
        poll_interval=0.01,
        timeout=0.05,
    )
    record = await adapter.run(SAMPLE, TREE)
    assert record.status == "cancelled"
    assert api.called("POST", "/tasks/t1/cancel")


async def test_no_verify_command_is_not_silently_green(tmp_path: Path) -> None:
    def agent(workspace: Path) -> None:
        (workspace / "done.txt").write_text("ok\n", encoding="utf-8")

    sample = dict(SAMPLE)
    sample["verify"] = ""
    adapter = BlankEvalAdapter(
        FakeApi(on_run=agent),
        root=tmp_path / "work",
        python_executable=sys.executable,
        poll_interval=0.01,
    )
    record = await adapter.run(sample, TREE)
    assert record.verify_ok is False
    assert "未配置验证命令" in record.verify_output


def test_changed_files_counts_added_modified_and_removed() -> None:
    before = {"a.py": b"1", "b.py": b"2"}
    after = {"a.py": b"1", "c.py": b"3"}
    assert changed_files(before, after) == ["b.py", "c.py"]


def test_trajectory_has_action_observation_and_error_lines() -> None:
    text = render_trajectory(
        [
            {"type": "tool.requested", "payload": {"tool": "read_file", "arguments": {"path": "a"}}},
            {"type": "tool.completed", "payload": {"tool": "read_file", "status": "ok", "summary": "1 line"}},
            {"type": "task.failed", "payload": {"error": "boom"}},
        ]
    )
    assert text.splitlines()[:3] == [
        '[ACTION] read_file {"path": "a"}',
        "[OBSERVATION] read_file ok 1 line",
        "[ERROR] boom",
    ]


@pytest.mark.parametrize("raw", ["python check.py", "python3.12 check.py", "python3 check.py"])
def test_verify_command_uses_the_project_interpreter(raw: str, tmp_path: Path) -> None:
    adapter = BlankEvalAdapter(
        FakeApi(), root=tmp_path / "unused", python_executable=r"C:\venv\python.exe"
    )
    assert adapter._verify_command(raw) == rf'"C:\venv\python.exe" check.py'

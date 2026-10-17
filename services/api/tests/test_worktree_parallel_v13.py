"""多副本并行编排：并发分派、改动清单与保守合并。

核心主张只有一句：**并行是为了分头探索，不是为了自动改写主工作区。**
所以这里测的不是"能不能并行"（能），而是"并行之后什么条件下才允许落盘"：

* 多个子代理改了同一个文件 → 冲突，谁的都不写；
* 目标文件在主工作区里已经和原始内容不同（有未提交改动）→ 跳过；
* 删除类改动 → 一律交回人工。

另外验证并发上限、事件里的 agent_path 可区分、副本跑完会被清掉。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from blank_api.services.models.base import AdapterConfig, ChatMessage, ModelAdapter, ModelResponse, Usage
from blank_api.services.subagent import (
    MAX_PARALLEL_AGENTS,
    SubAgentRequest,
    SubAgentResult,
    SubAgentRunner,
)
from blank_api.services.tools.delegate import delegate_tools
from blank_api.services.tools.base import ToolContext
from blank_api.services.worktree import apply_changes, changed_files, create_worktree, remove_worktree


# --------------------------------------------------------------------------- #
# 假适配器 / 假会话：让并行调度可以脱离真实模型与数据库被验证
# --------------------------------------------------------------------------- #
class SlowAdapter(ModelAdapter):
    def __init__(self, delay: float = 0.06) -> None:
        super().__init__(
            AdapterConfig(
                name="fake",
                kind="openai",
                base_url="http://localhost",
                api_key="",
                model="fake",
                supports_tools=False,
            )
        )
        self.delay = delay
        self.in_flight = 0
        self.peak = 0

    async def invoke(self, messages, **kwargs) -> ModelResponse:  # pragma: no cover - 子代理不用它
        return ModelResponse(text="ok", usage=Usage(1, 1), model="fake")

    async def stream(self, messages, **kwargs):  # pragma: no cover
        yield ""

    async def bind_tools(self, specs, messages, **kwargs) -> ModelResponse:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        return ModelResponse(text=f"完成：{len(specs)} 个工具可用", usage=Usage(12, 4), model="fake")


class FakeRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def add(self, event: str, node: str, payload: dict) -> None:
        self.events.append((event, payload))

    def types(self) -> list[str]:
        return [event for event, _ in self.events]


class FakeSession:
    def __init__(self, root: Path, adapter: SlowAdapter) -> None:
        self.adapter = adapter
        self.recorder = FakeRecorder()
        self.trace_id = "thread-1"
        self.depth = 0
        self.agent_path = "root"
        self.session_factory = None
        self.context = ToolContext(root=root, task_id="task-1", session_factory=None)


# --------------------------------------------------------------------------- #
# 1. 改动清单与保守合并
# --------------------------------------------------------------------------- #
def test_changed_files_lists_new_and_modified_text_files(workspace: Path):
    tree = create_worktree(workspace, label="changes")
    try:
        (tree.path / "src" / "core.py").write_text("VALUE = 2\n", encoding="utf-8")
        (tree.path / "notes.md").write_text("# 新文件\n", encoding="utf-8")
        entries = {item["path"]: item for item in changed_files(tree)}
        assert entries["src/core.py"]["change_type"] == "modify"
        assert entries["notes.md"]["change_type"] == "add"
        assert entries["notes.md"]["content"] == "# 新文件\n"
        # 没动过的文件不该出现在清单里
        assert "README.md" not in entries
    finally:
        remove_worktree(tree)


def test_merge_writes_new_files_but_not_uncommitted_overwrites(workspace: Path):
    """非 git 工作区：新文件可以落盘，覆盖已有文件必须交回人工。"""
    tree = create_worktree(workspace, label="merge")
    try:
        (tree.path / "notes.md").write_text("# 新文件\n", encoding="utf-8")
        (tree.path / "README.md").write_text("# 改过\n", encoding="utf-8")
        report = apply_changes(tree)
        assert report.applied == ["notes.md"]
        assert (workspace / "notes.md").read_text(encoding="utf-8") == "# 新文件\n"
        # 原来的 README 没有被覆盖
        assert (workspace / "README.md").read_text(encoding="utf-8") == "# demo project\n"
        reasons = {item["path"]: item["reason"] for item in report.skipped}
        assert "README.md" in reasons and "人工合并" in reasons["README.md"]
    finally:
        remove_worktree(tree)


def test_two_agents_touching_the_same_file_is_a_conflict(workspace: Path):
    first = create_worktree(workspace, label="a")
    second = create_worktree(workspace, label="b")
    try:
        (first.path / "shared.md").write_text("方案 A\n", encoding="utf-8")
        (second.path / "shared.md").write_text("方案 B\n", encoding="utf-8")
        report = apply_changes(first, blocked={"shared.md"})
        assert report.conflicts == ["shared.md"]
        assert report.applied == []
        assert not (workspace / "shared.md").exists(), "冲突时谁的版本都不能写进主工作区"
    finally:
        remove_worktree(first)
        remove_worktree(second)


def test_deletions_are_never_applied_automatically(workspace: Path):
    tree = create_worktree(workspace, label="del")
    try:
        (tree.path / "main.py").unlink()
        report = apply_changes(tree)
        assert report.applied == []
        assert (workspace / "main.py").exists()
    finally:
        remove_worktree(tree)


# --------------------------------------------------------------------------- #
# 2. 并行分派
# --------------------------------------------------------------------------- #
async def test_fan_out_runs_children_concurrently(workspace: Path):
    adapter = SlowAdapter(delay=0.08)
    session = FakeSession(workspace, adapter)
    runner = SubAgentRunner().bind(session)
    requests = [SubAgentRequest(goal=f"子任务 {index}", isolate=True) for index in range(3)]

    started = time.perf_counter()
    results = await runner.fan_out(requests, concurrency=3)
    elapsed = time.perf_counter() - started

    assert len(results) == 3
    assert all(result.ok for result in results), [r.error for r in results]
    # 有 2 个以上的模型调用同时在飞，才叫并行（墙钟时间不可靠：建副本本身要耗时）
    assert adapter.peak >= 2, "子代理是串行跑完的"
    assert elapsed > 0
    # 每个子代理有自己的路径，事件里能还原"谁在哪个分支上干活"
    assert len({result.agent_path for result in results}) == 3
    assert all(result.agent_path.startswith("root/par-") for result in results)
    assert "subagents.planned" in session.recorder.types()


async def test_fan_out_caps_the_number_of_children(workspace: Path):
    adapter = SlowAdapter(delay=0.01)
    session = FakeSession(workspace, adapter)
    runner = SubAgentRunner().bind(session)
    requests = [SubAgentRequest(goal=f"子任务 {index}", isolate=True) for index in range(9)]
    results = await runner.fan_out(requests, concurrency=2)
    assert len(results) == MAX_PARALLEL_AGENTS


async def test_fan_out_cleans_up_worktrees(workspace: Path):
    adapter = SlowAdapter(delay=0.01)
    session = FakeSession(workspace, adapter)
    runner = SubAgentRunner().bind(session)
    results = await runner.fan_out([SubAgentRequest(goal="看一眼", isolate=True) for _ in range(2)])
    assert all(result.tree is None for result in results)
    for result in results:
        path = (result.worktree or {}).get("path", "")
        assert path and not Path(path).exists(), "跑完必须清掉副本，不能把临时目录留给下一次"


async def test_merge_orchestration_writes_only_uncontested_files(workspace: Path):
    """merge=True 的编排层：两个子代理各改各的文件 → 都落盘；改同一个 → 谁的都不落。"""
    session = FakeSession(workspace, SlowAdapter(delay=0.01))
    runner = SubAgentRunner().bind(session)

    first = create_worktree(workspace, label="a")
    second = create_worktree(workspace, label="b")
    try:
        (first.path / "only-a.md").write_text("A\n", encoding="utf-8")
        (first.path / "shared.md").write_text("来自 A\n", encoding="utf-8")
        (second.path / "only-b.md").write_text("B\n", encoding="utf-8")
        (second.path / "shared.md").write_text("来自 B\n", encoding="utf-8")

        results = [
            SubAgentResult(ok=True, agent_path="root/par-1"),
            SubAgentResult(ok=True, agent_path="root/par-2"),
        ]
        results[0].tree = first
        results[1].tree = second
        await runner._merge(results, "grp")

        assert (workspace / "only-a.md").read_text(encoding="utf-8") == "A\n"
        assert (workspace / "only-b.md").read_text(encoding="utf-8") == "B\n"
        assert not (workspace / "shared.md").exists(), "同一文件被两个子代理改过，算冲突，不写入"
        assert results[0].merge["applied"] == ["only-a.md"]
        assert results[0].merge["conflicts"] == ["shared.md"]
        assert results[1].merge["applied"] == ["only-b.md"]
        assert "worktree.merged" in session.recorder.types()
    finally:
        remove_worktree(first)
        remove_worktree(second)


# --------------------------------------------------------------------------- #
# 3. spawn_agents 工具的对外形态
# --------------------------------------------------------------------------- #
async def test_spawn_agents_tool_aggregates_results(workspace: Path):
    adapter = SlowAdapter(delay=0.01)
    session = FakeSession(workspace, adapter)
    runner = SubAgentRunner().bind(session)
    tool = {item.name: item for item in delegate_tools()}["spawn_agents"]
    context = ToolContext(
        root=workspace, task_id="task-1", session_factory=None, subagent_runner=runner
    )

    outcome = await tool.handler(
        context, {"tasks": ["查一下 src 目录", {"goal": "看 README", "isolate": True}], "merge": False}
    )
    assert outcome.ok
    assert outcome.data["count"] == 2
    assert outcome.data["succeeded"] == 2
    assert len(outcome.data["agents"]) == 2
    assert "并行执行了 2 个子代理" in outcome.output
    # 每个子代理的结论都要出现在汇总里，不能只给最后一条
    assert all("完成" in item["summary"] or item["summary"] for item in outcome.data["agents"])


async def test_spawn_agents_rejects_empty_or_malformed_tasks(workspace: Path):
    adapter = SlowAdapter(delay=0.01)
    session = FakeSession(workspace, adapter)
    tool = {item.name: item for item in delegate_tools()}["spawn_agents"]
    context = ToolContext(
        root=workspace, task_id="task-1", session_factory=None, subagent_runner=SubAgentRunner().bind(session)
    )
    for payload in ({}, {"tasks": []}, {"tasks": [{"goal": "  "}]}, {"tasks": [123]}):
        outcome = await tool.handler(context, payload)
        assert not outcome.ok
        assert outcome.message


async def test_spawn_agents_without_runner_says_so(workspace: Path):
    tool = {item.name: item for item in delegate_tools()}["spawn_agents"]
    context = ToolContext(root=workspace, task_id="task-1", session_factory=None)
    outcome = await tool.handler(context, {"tasks": ["随便看看"]})
    assert not outcome.ok and "没有启用子代理" in outcome.message


def test_merging_back_into_the_workspace_needs_approval():
    """只探索不定稿：merge=True 会把文件写进主工作区，必须升级为高风险。"""
    tool = {item.name: item for item in delegate_tools()}["spawn_agents"]
    assert tool.resolve_risk({"tasks": ["看看"], "merge": False}) == "medium"
    assert tool.resolve_risk({"tasks": ["看看"], "merge": True}) == "high"


def test_parallel_tool_is_deferred_until_asked_for():
    """并行分派属于"用得着才需要"的能力：默认不下发给模型，靠 tool_search 拉出来。"""
    from blank_api.services.tools.registry import DEFERRED_TOOLS

    assert "spawn_agents" in DEFERRED_TOOLS


@pytest.mark.parametrize("goal", ["", "   "])
def test_empty_goal_is_rejected_before_spawning(goal: str):
    request = SubAgentRequest(goal=goal).normalized()
    assert request.goal == ""

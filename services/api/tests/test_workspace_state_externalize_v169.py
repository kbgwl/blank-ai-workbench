"""harness 自己的状态不许留在交付目录（玻璃题 #31）。

真机实物：3D 玻璃官网交付目录里除了 index.html/styles.css/app.js，还躺着
``.blank/scratchpad/lumen-selftest.js`` 与 21 个 ``.blank_backups/<task>/*``。
用户按题面「交付目录只有页面文件」验货时，这些就是我们留下的垃圾。

收尾时整体外移到 ``data/workspace-state/<workspace>/<task>/``：只移动不删除，
用户配置（``.blank/AGENTS.md``、``.blank/harness``、``.blank/output-styles``）一个不动。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.task import AgentEvent
from blank_api.services.agent import graph
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.runtime import AgentRuntime
from blank_api.services.agent.workspace_state import externalize, workspace_slug

from tests.test_agent_v04 import create_project, create_task, env  # noqa: F401  (env 是夹具)


def _page_files(root: Path) -> list[str]:
    return sorted(
        str(path.relative_to(root)).replace("\\", "/")
        for path in root.rglob("*")
        if path.is_file()
    )


def _seed_workspace(root: Path) -> None:
    (root / "index.html").write_text("<html></html>", encoding="utf-8")
    (root / "styles.css").write_text("body{}", encoding="utf-8")
    (root / "app.js").write_text("console.log(1)", encoding="utf-8")
    (root / ".blank" / "scratchpad").mkdir(parents=True)
    (root / ".blank" / "scratchpad" / "selftest.js").write_text("assert(1)", encoding="utf-8")
    (root / ".blank" / "context" / "scratchpad").mkdir(parents=True)
    (root / ".blank" / "context" / "scratchpad" / "servetest.js").write_text("x", encoding="utf-8")
    backups = root / ".blank_backups"
    for task, stamp in (("task-1", "aaaa"), ("task-2", "bbbb"), ("task-glass", "cccc")):
        (backups / task / stamp / "index__html").parent.mkdir(parents=True)
        (backups / task / stamp / "index__html").write_text(task, encoding="utf-8")
    # 用户自己的 .blank 配置：不许被当成临时目录收走。
    (root / ".blank" / "AGENTS.md").write_text("项目约定", encoding="utf-8")
    (root / ".blank" / "harness").mkdir()
    (root / ".blank" / "harness" / "c1.md").write_text("harness", encoding="utf-8")
    (root / ".blank" / "output-styles").mkdir()
    (root / ".blank" / "output-styles" / "review.md").write_text("style", encoding="utf-8")


def test_harness_state_leaves_the_delivery_dir_but_user_config_stays(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    state_root = tmp_path.parent / "state"

    result = externalize(tmp_path, "task-1", state_root=state_root)

    assert {item["path"] for item in result["moved"]} == {
        ".blank/scratchpad",
        ".blank/context/scratchpad",
        ".blank_backups/task-1",
    }
    assert _page_files(tmp_path) == [
        ".blank/AGENTS.md",
        ".blank/harness/c1.md",
        ".blank/output-styles/review.md",
        ".blank_backups/task-2/bbbb/index__html",
        ".blank_backups/task-glass/cccc/index__html",
        "app.js",
        "index.html",
        "styles.css",
    ]
    # 外移后的文件真在硬盘上，不是删了了事。
    moved_to = {item["path"]: Path(item["to"]) for item in result["moved"]}
    assert (moved_to[".blank/scratchpad"] / "selftest.js").read_text(encoding="utf-8") == "assert(1)"
    assert (moved_to[".blank_backups/task-1"] / "aaaa" / "index__html").read_text(encoding="utf-8") == "task-1"


def test_other_tasks_backups_are_not_ours_to_move(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    result = externalize(tmp_path, "task-1", state_root=tmp_path.parent / "state")
    assert not any(item["path"] == ".blank_backups/task-2" for item in result["moved"])
    assert (tmp_path / ".blank_backups" / "task-2" / "bbbb" / "index__html").exists()


def test_second_run_is_a_noop(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    state_root = tmp_path.parent / "state"
    externalize(tmp_path, "task-1", state_root=state_root)
    again = externalize(tmp_path, "task-1", state_root=state_root)
    assert again["moved"] == []
    assert again["root"] == ""


def test_existing_archive_is_not_overwritten(tmp_path: Path) -> None:
    """同名存档已经存在时换名，上一轮的备份不能被这一轮覆盖掉。"""
    _seed_workspace(tmp_path)
    state_root = tmp_path.parent / "state"
    archive = state_root / workspace_slug(tmp_path) / "task-1" / ".blank" / "scratchpad"
    archive.mkdir(parents=True)
    (archive / "old.js").write_text("old", encoding="utf-8")

    result = externalize(tmp_path, "task-1", state_root=state_root)

    moved = {item["path"]: Path(item["to"]) for item in result["moved"]}
    assert (archive / "old.js").read_text(encoding="utf-8") == "old"
    assert (moved[".blank/scratchpad"] / "selftest.js").exists()
    assert moved[".blank/scratchpad"] != archive


def test_missing_workspace_is_not_an_error(tmp_path: Path) -> None:
    assert externalize(tmp_path / "nope", "task-1", state_root=tmp_path / "state") == {
        "moved": [],
        "root": "",
    }


def test_task_id_cannot_escape_the_state_root(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    state_root = tmp_path / "state"
    result = externalize(tmp_path, "../../evil", state_root=state_root)
    # 没有叫这个名字的备份子树；scratchpad 照常外移，但落点必须仍在 state 根内。
    assert not any(item["path"].startswith(".blank_backups") for item in result["moved"])
    for item in result["moved"]:
        assert Path(item["to"]).resolve().is_relative_to(state_root.resolve())
    from blank_api.services.agent.workspace_state import _safe_component

    assert "/" not in _safe_component("../../evil")
    assert ".." not in _safe_component("../../evil")


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def add(self, kind: str, node: str, payload: dict) -> None:
        self.events.append((kind, payload))


class _Context:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.task_id = "task-glass"
        self.subagent_runner = None


class _Hooks:
    async def run(self, *args, **kwargs):
        return []


class _Session:
    readonly = False
    acceptance_criteria: list[str] = []
    acceptance_links: list[dict] = []
    recorded_questions: list[str] = []
    turn_effort = None

    def __init__(self, root: Path) -> None:
        self.context = _Context(root)
        self.recorder = _Recorder()
        self.hooks = _Hooks()

    async def ask(self, state, prompt, node, *, messages=None):
        return {
            "text": "结论\n- 页面已交付",
            "tool_calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "calls": 1},
            "messages": list(messages or []),
        }


def test_summarize_result_externalizes_before_writing_delivery(tmp_path: Path, monkeypatch) -> None:
    """接线条：收尾节点必须真的把状态挪走，而不是只在模块里躺着一个函数。"""
    _seed_workspace(tmp_path)
    state_root = tmp_path / "state"
    monkeypatch.setenv("BLANK_WORKSPACE_STATE_DIR", str(state_root))
    session = _Session(tmp_path)

    async def _no_wait(_session):
        return {"waited_seconds": 0.0, "finished": [], "cancelled": []}

    monkeypatch.setattr(graph, "_settle_background_subagents", _no_wait)
    state = {
        "messages": [{"role": "user", "content": "做个官网"}],
        "tool_results": [
            {
                "tool": "write_file",
                "status": "ok",
                "ok": True,
                "data": {"path": "index.html", "change_type": "created", "diff": "@@ -0,0 +1,1 @@\n+a\n"},
            }
        ],
    }

    result = asyncio.run(graph.summarize_result_node(state, session))

    assert "页面已交付" in result["summary"]
    assert not (tmp_path / ".blank" / "scratchpad").exists()
    assert not (tmp_path / ".blank_backups" / "task-glass").exists()
    kinds = [kind for kind, _ in session.recorder.events]
    assert "workspace.state_externalized" in kinds


async def _event_types(task_id: str) -> list[str]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(AgentEvent.type).where(AgentEvent.task_id == task_id)
            )
        ).scalars().all()
    return list(rows)


def _run_scope(factory, task_id: str, workspace: Path) -> SimpleNamespace:
    return SimpleNamespace(
        store=factory,
        task_id=task_id,
        recorder=EventRecorder(factory, task_id),
        session=SimpleNamespace(
            router=None,
            context=SimpleNamespace(root=workspace, lsp=None),
        ),
        subagents=None,
    )


async def test_runtime_finish_externalizes_even_when_the_task_failed(
    env, workspace: Path, monkeypatch
) -> None:  # noqa: F811
    """玻璃题是超时收场：走到不了 summarize_result 的那条路也必须清场。"""
    _seed_workspace(workspace)
    project_id = await create_project(env, workspace)
    task_id = await create_task(env, project_id, ["交付页面"])
    (workspace / ".blank_backups" / task_id / "dddd").mkdir(parents=True)
    (workspace / ".blank_backups" / task_id / "dddd" / "index__html").write_text("x", encoding="utf-8")
    monkeypatch.setenv("BLANK_WORKSPACE_STATE_DIR", str(workspace.parent / "state"))

    factory = get_session_factory()
    await AgentRuntime(factory)._finish(
        _run_scope(factory, task_id, workspace), "failed", summary="", reason="task_timeout"
    )

    assert not (workspace / ".blank" / "scratchpad").exists()
    assert not (workspace / ".blank_backups" / task_id).exists()
    # 用户配置与别的任务的历史备份都不能被顺走。
    assert (workspace / ".blank" / "AGENTS.md").exists()
    assert (workspace / ".blank_backups" / "task-2" / "bbbb" / "index__html").exists()
    kinds = await _event_types(task_id)
    assert "workspace.state_externalized" in kinds


async def test_waiting_approval_is_not_a_terminal_state(env, workspace: Path, monkeypatch) -> None:  # noqa: F811
    """等审批不是终点：恢复后还要在同一个工作区接着干，清场必须等到真收尾。"""
    _seed_workspace(workspace)
    project_id = await create_project(env, workspace)
    task_id = await create_task(env, project_id, ["交付页面"])
    monkeypatch.setenv("BLANK_WORKSPACE_STATE_DIR", str(workspace.parent / "state"))

    factory = get_session_factory()
    await AgentRuntime(factory)._finish(
        _run_scope(factory, task_id, workspace), "waiting_approval", summary="等批准", reason=""
    )

    assert (workspace / ".blank" / "scratchpad").exists()
    assert "workspace.state_externalized" not in await _event_types(task_id)

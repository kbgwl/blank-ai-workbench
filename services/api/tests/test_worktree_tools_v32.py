# -*- coding: utf-8 -*-
"""Q2：enter_worktree / exit_worktree 对齐 Claude 的 EnterWorktree / ExitWorktree。

钉住七件事：

1. **工具面**：两个工具按需加载登记、有中文别名、不进并行/只读集合；
2. **换根是真的**：进入之后 `read_file` 读到的是新树里的文件，原工作区纹丝不动；
3. **会话按任务隔离**，且审批恢复（`runtime._prepare`）后仍在同一棵树上；
4. **只有自己建的树能 remove**：path 进入的树只能 keep；
5. **remove 的闸门**：未提交改动或未合并提交默认拒绝，并说清是哪几个文件；
6. **任务收尾只摘记录**：工作树留在盘上，删不删由 exit_worktree 决定；
7. **创建位置与卫生**：`.blank-worktrees/<name>`、`blank/worktree-<name>` 分支、
   `.git/info/exclude` 里有一行，`git status` 不会把整棵树报成未跟踪。

每条都对应一处会退回去的实现；改坏它，这一格会红在具体断言上。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.db import get_session_factory
from blank_api.services import worktree_session
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import READONLY_TOOLS, SYSTEM_PROMPT
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.models.base import AdapterConfig, ModelAdapter, ModelResponse, Usage
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.filesystem import filesystem_tools
from blank_api.services.tools.registry import (
    ALIASES,
    DEFERRED_TOOLS,
    PARALLEL_SAFE_TOOLS,
    ToolRegistry,
)
from blank_api.services.tools.worktree import worktree_tools
from blank_api.services.workspace import TREE_IGNORE
from blank_api.services.worktree import COPY_IGNORE

from tests.test_agent_v04 import create_project, create_task, env  # noqa: F401  (env 是夹具)

ENTER = next(tool for tool in worktree_tools() if tool.name == "enter_worktree")
EXIT = next(tool for tool in worktree_tools() if tool.name == "exit_worktree")
READ_FILE = next(tool for tool in filesystem_tools() if tool.name == "read_file")


# --------------------------------------------------------------------------- #
# 装置
# --------------------------------------------------------------------------- #
class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def add(self, event: str, node: str, payload: dict) -> None:
        self.events.append((event, node, payload))


def _context(root: Path, task_id: str, **extra) -> ToolContext:
    return ToolContext(root=root, task_id=task_id, session_factory=None, **extra)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _commit(root: Path, message: str) -> None:
    identity = [
        "-c", "user.name=blank-test",
        "-c", "user.email=blank-test@example.com",
        "-c", "commit.gpgsign=false",
    ]
    _git(root, *identity, "add", "-A")
    done = _git(root, *identity, "commit", "-qm", message)
    assert done.returncode == 0, done.stderr


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    (root / "main.py").write_text("print('old')\n", encoding="utf-8")
    _commit(root, "init")
    return root


@pytest.fixture(autouse=True)
def _clear_sessions():
    """用例之间不共享工作树会话：一个漏掉的 release 会让下一个用例进不去。"""
    yield
    for task_id in list(worktree_session._SESSIONS):
        worktree_session.release(task_id)


async def _enter(context: ToolContext, **arguments) -> dict:
    outcome = await ENTER.handler(context, arguments)
    assert outcome.ok, outcome.message
    return outcome.data


async def _exit(context: ToolContext, **arguments) -> tuple[bool, dict, str]:
    outcome = await EXIT.handler(context, arguments)
    # 成功时正文在 output 里，失败时在 message 里：两种情况都要能对文案断言。
    return outcome.ok, outcome.data, outcome.message or outcome.output


# --------------------------------------------------------------------------- #
# 1. 工具面
# --------------------------------------------------------------------------- #
def test_tools_are_registered_on_demand_with_aliases() -> None:
    assert ENTER.name == "enter_worktree" and EXIT.name == "exit_worktree"
    # 只登记一个参数、另一个留空：两个都传由实现拒绝。
    assert set(ENTER.input_schema["properties"]) == {"name", "path"}
    assert ENTER.input_schema.get("required") in (None, [])
    assert EXIT.input_schema["required"] == ["action"]
    assert EXIT.input_schema["properties"]["action"]["enum"] == ["keep", "remove"]
    assert EXIT.input_schema["properties"]["discard_changes"]["type"] == "boolean"

    assert {"enter_worktree", "exit_worktree"} <= DEFERRED_TOOLS
    assert "工作树" in ALIASES["enter_worktree"] and "工作树" in ALIASES["exit_worktree"]
    # 换根会改变后续所有工具的行为，不能混进"并发安全"批次。
    assert not ({"enter_worktree", "exit_worktree"} & PARALLEL_SAFE_TOOLS)
    assert not ({"enter_worktree", "exit_worktree"} & READONLY_TOOLS)

    names = {tool.name for tool in build_default_tools()}
    assert {"enter_worktree", "exit_worktree"} <= names


def test_remove_approval_cannot_be_remembered_as_a_whole_tool_grant() -> None:
    from blank_api.services.permissions import ARGUMENT_SCOPED_TOOLS, tool_grant_too_wide

    assert "exit_worktree" in ARGUMENT_SCOPED_TOOLS
    # 卡片上批的是「这一次 remove」，不是「以后所有 remove」：参数决定的风险不能按整个工具免问。
    assert tool_grant_too_wide("exit_worktree", {"action": "remove"}) is True
    assert tool_grant_too_wide("exit_worktree", {"action": "keep"}) is True


def test_tool_search_finds_worktrees_by_chinese_alias() -> None:
    registry = ToolRegistry(build_default_tools())
    found, activated = registry.search("工作树")
    assert {"enter_worktree", "exit_worktree"} <= {tool.name for tool in found}
    # 搜到只是列表展示，必须真的激活：否则模型下一轮还是看不到这两个工具。
    assert {"enter_worktree", "exit_worktree"} <= set(activated)


def test_no_worktree_action_is_low_risk() -> None:
    # remove 会删目录与分支：必须是 high，走审批；enter 与 keep 只是切根。
    assert ENTER.resolve_risk({}) == "medium"
    assert EXIT.resolve_risk({"action": "keep"}) == "medium"
    assert EXIT.resolve_risk({"action": "remove"}) == "high"


def test_system_prompt_tells_the_model_when_to_use_worktrees() -> None:
    assert "enter_worktree" in SYSTEM_PROMPT
    assert "exit_worktree" in SYSTEM_PROMPT
    assert "discard_changes" in SYSTEM_PROMPT


def test_workspace_walkers_skip_the_worktree_directory() -> None:
    # 工作树建在仓库里：快照/搜索/子代理整树复制不跳过它，就会把同一份代码遍历两次。
    assert ".blank-worktrees" in TREE_IGNORE
    assert ".blank-worktrees" in COPY_IGNORE


# --------------------------------------------------------------------------- #
# 2. 建树、换根、事件
# --------------------------------------------------------------------------- #
async def test_enter_creates_branch_tree_and_moves_the_root(repo: Path) -> None:
    task_id = "q2-enter"
    recorder = _Recorder()
    context = _context(repo, task_id, recorder=recorder)

    data = await _enter(context, name="feat")

    tree = repo / ".blank-worktrees" / "feat"
    assert context.root == tree and tree.is_dir()
    assert data == {
        "name": "feat",
        "branch": "blank/worktree-feat",
        # 这个仓库没有远端跟踪分支：fresh 默认回落本地 HEAD。
        "base_ref": "HEAD",
        "path": ".blank-worktrees/feat",
        "owned": True,
        # 写者认领的结果也要回给模型：unavailable 时它得知道自己没有独占。
        "owner_status": "owned",
    }
    assert _git(repo, "rev-parse", "--verify", "refs/heads/blank/worktree-feat").returncode == 0
    assert len(recorder.events) == 1
    event, node, payload = recorder.events[0]
    assert (event, node) == ("worktree.entered", "worktree")
    assert payload["mode"] == "name" and payload["root"] == str(tree)
    assert payload["origin"] == str(repo)


async def test_entered_tree_is_excluded_from_git_status(repo: Path) -> None:
    context = _context(repo, "q2-exclude")
    await _enter(context, name="clean")
    status = _git(repo, "status", "--porcelain")
    assert status.returncode == 0
    assert "blank-worktrees" not in status.stdout
    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "/.blank-worktrees/" in exclude


async def test_file_tools_read_from_the_new_tree_and_reset_read_state(repo: Path) -> None:
    from blank_api.services.tools.file_state import FileStateCache

    state = FileStateCache()
    state.record_read("main.py", size=13, mtime_ns=1)
    context = _context(repo, "q2-read", file_state=state)
    await _enter(context, name="reader")

    # 相对路径在两棵树里指向不同文件："在旧树里读过"不能当"在新树里读过"。
    assert context.file_state.snapshot() == {}

    (context.root / "main.py").write_text("print('inside worktree')\n", encoding="utf-8")
    outcome = await READ_FILE.handler(context, {"path": "main.py"})
    assert outcome.ok, outcome.message
    assert "inside worktree" in outcome.output
    assert (repo / "main.py").read_text(encoding="utf-8") == "print('old')\n"


# --------------------------------------------------------------------------- #
# 3. 拒绝路径
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["a/b", "..", "名字", "x" * 65])
async def test_bad_names_are_rejected(repo: Path, name: str) -> None:
    context = _context(repo, f"q2-name-{len(name)}")
    outcome = await ENTER.handler(context, {"name": name})
    assert not outcome.ok and outcome.status == "error"
    assert "名字" in outcome.message
    assert context.root == repo


async def test_enter_requires_a_git_repository_with_a_commit(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    context = _context(plain, "q2-plain")
    outcome = await ENTER.handler(context, {"name": "x"})
    assert not outcome.ok and "不在 git 仓库" in outcome.message

    empty = tmp_path / "empty"
    empty.mkdir()
    assert _git(empty, "init", "-q").returncode == 0
    outcome = await ENTER.handler(_context(empty, "q2-empty"), {"name": "x"})
    assert not outcome.ok and "没有任何提交" in outcome.message


async def test_second_enter_without_exit_is_rejected(repo: Path) -> None:
    context = _context(repo, "q2-twice")
    await _enter(context, name="one")
    outcome = await ENTER.handler(context, {"name": "two"})
    assert not outcome.ok and "已经在 worktree" in outcome.message
    # 会话没被顶掉：根还指着第一棵树。
    assert context.root == repo / ".blank-worktrees" / "one"


async def test_name_and_path_are_mutually_exclusive(repo: Path) -> None:
    outcome = await ENTER.handler(_context(repo, "q2-both"), {"name": "x", "path": str(repo)})
    assert not outcome.ok and "只能给一个" in outcome.message


# --------------------------------------------------------------------------- #
# 4. path 模式
# --------------------------------------------------------------------------- #
def _manual_worktree(repo: Path, target: Path, branch: str) -> Path:
    done = _git(repo, "worktree", "add", "-q", "-b", branch, str(target))
    assert done.returncode == 0, done.stderr
    return target


async def test_path_mode_enters_existing_tree_and_can_only_keep(repo: Path, tmp_path: Path) -> None:
    manual = _manual_worktree(repo, tmp_path / "manual-wt", "manual")
    context = _context(repo, "q2-path")

    data = await _enter(context, path=str(manual))
    assert context.root == manual
    assert data["owned"] is False
    assert data["branch"] == "manual"
    assert not Path(data["path"]).is_absolute(), data["path"]

    ok, _data, message = await _exit(context, action="remove")
    assert not ok and "不能 remove" in message
    assert manual.is_dir() and worktree_session.get("q2-path") is not None

    ok, data, _message = await _exit(context, action="keep")
    assert ok and data["removed"] is False
    assert context.root == repo
    assert manual.is_dir()
    assert worktree_session.get("q2-path") is None


async def test_path_must_be_a_registered_worktree(repo: Path, tmp_path: Path) -> None:
    context = _context(repo, "q2-path-invalid")
    # 在仓库里但不是工作树：过得了"在 git 仓库里"，过不了"worktree list"。
    plain = repo / "plain-dir"
    plain.mkdir()
    outcome = await ENTER.handler(context, {"path": str(plain)})
    assert not outcome.ok and "不在 git worktree list 里" in outcome.message

    outside = tmp_path / "outside-dir"
    outside.mkdir()
    outcome = await ENTER.handler(context, {"path": str(outside)})
    assert not outcome.ok and "不在任何 git 仓库里" in outcome.message

    outcome = await ENTER.handler(context, {"path": str(tmp_path / "missing")})
    assert not outcome.ok and "不存在" in outcome.message

    outcome = await ENTER.handler(context, {"path": str(repo)})
    assert not outcome.ok and "就是当前工作区" in outcome.message


async def test_detached_head_does_not_report_a_branch_called_head(repo: Path, tmp_path: Path) -> None:
    manual = _manual_worktree(repo, tmp_path / "detached-wt", "detached")
    assert _git(manual, "checkout", "--detach", "-q").returncode == 0

    context = _context(repo, "q2-detached")
    outcome = await ENTER.handler(context, {"path": str(manual)})
    assert outcome.ok, outcome.message
    assert outcome.data["branch"] == ""
    assert "分支 HEAD" not in outcome.output


async def test_path_rejects_an_unrelated_repository(repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    assert _git(other, "init", "-q").returncode == 0
    (other / "other.py").write_text("X = 1\n", encoding="utf-8")
    _commit(other, "init")
    unrelated = _manual_worktree(other, tmp_path / "other-wt", "side")

    outcome = await ENTER.handler(_context(repo, "q2-unrelated"), {"path": str(unrelated)})
    assert not outcome.ok and "既不是当前工作区所在仓库" in outcome.message


async def test_nested_repository_is_accepted(repo: Path) -> None:
    nested = repo / "nested"
    nested.mkdir()
    assert _git(nested, "init", "-q").returncode == 0
    (nested / "n.py").write_text("Y = 2\n", encoding="utf-8")
    _commit(nested, "nested init")
    inner = _manual_worktree(nested, nested / "inner-wt", "inner")

    context = _context(repo, "q2-nested")
    data = await _enter(context, path=str(inner))
    assert context.root == inner
    assert data["owned"] is False
    session = worktree_session.get("q2-nested")
    assert session is not None
    assert session.repo == nested.resolve()
    assert session.origin == repo.resolve()
    await _exit(context, action="keep")


# --------------------------------------------------------------------------- #
# 5. keep / remove 的闸门
# --------------------------------------------------------------------------- #
async def test_exit_without_a_session_is_a_noop(repo: Path) -> None:
    context = _context(repo, "q2-noop")
    ok, data, message = await _exit(context, action="keep")
    assert ok and data["noop"] is True
    assert "无需退出" in message
    assert context.root == repo


async def test_keep_leaves_the_tree_and_restores_the_origin(repo: Path) -> None:
    recorder = _Recorder()
    context = _context(repo, "q2-keep", recorder=recorder)
    await _enter(context, name="kept")
    tree = context.root
    (tree / "feature.py").write_text("VALUE = 3\n", encoding="utf-8")

    ok, data, _message = await _exit(context, action="keep")
    assert ok and data["removed"] is False
    assert data["path"] == ".blank-worktrees/kept"
    assert context.root == repo
    assert (tree / "feature.py").is_file()
    assert worktree_session.get("q2-keep") is None
    # 分支也还在：keep 不碰任何 git 东西。
    assert _git(repo, "rev-parse", "--verify", "refs/heads/blank/worktree-kept").returncode == 0
    assert [event for event, _node, _payload in recorder.events] == [
        "worktree.entered",
        "worktree.exited",
    ]
    payload = recorder.events[1][2]
    assert payload["action"] == "keep" and payload["removed"] is False


async def test_remove_refuses_dirty_tree_and_names_the_files(repo: Path) -> None:
    context = _context(repo, "q2-dirty")
    await _enter(context, name="dirty")
    tree = context.root
    (tree / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

    ok, _data, message = await _exit(context, action="remove")
    assert not ok
    assert "未提交改动" in message and "dirty.txt" in message
    assert "discard_changes=true" in message
    # 拒绝不是退出：树还在、会话还活着，用户可以改成 keep 或确认丢弃。
    assert tree.is_dir()
    assert worktree_session.get("q2-dirty") is not None
    assert context.root == tree


async def test_remove_with_discard_deletes_tree_and_branch(repo: Path) -> None:
    context = _context(repo, "q2-discard")
    await _enter(context, name="discard")
    tree = context.root
    (tree / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

    ok, data, _message = await _exit(context, action="remove", discard_changes=True)
    assert ok and data["removed"] is True and data["branch_deleted"] is True
    assert context.root == repo
    assert not tree.exists()
    assert _git(repo, "rev-parse", "--verify", "refs/heads/blank/worktree-discard").returncode != 0
    assert worktree_session.get("q2-discard") is None


async def test_remove_refuses_unmerged_commits(repo: Path) -> None:
    context = _context(repo, "q2-ahead")
    await _enter(context, name="ahead")
    tree = context.root
    (tree / "committed.py").write_text("Z = 4\n", encoding="utf-8")
    _commit(tree, "work")

    ok, _data, message = await _exit(context, action="remove")
    assert not ok and "未合并的提交" in message
    assert tree.is_dir()

    ok, data, _message = await _exit(context, action="remove", discard_changes=True)
    assert ok and data["removed"] is True
    assert not tree.exists()


async def test_clean_remove_deletes_branch(repo: Path) -> None:
    context = _context(repo, "q2-clean")
    await _enter(context, name="clean")
    tree = context.root

    ok, data, _message = await _exit(context, action="remove")
    assert ok and data["removed"] is True and data["branch_deleted"] is True
    assert not tree.exists()
    assert _git(repo, "branch", "--list", "blank/worktree-clean").stdout.strip() == ""


async def test_invalid_action_is_rejected(repo: Path) -> None:
    outcome = await EXIT.handler(_context(repo, "q2-action"), {"action": "merge"})
    assert not outcome.ok and "keep 或 remove" in outcome.message


# --------------------------------------------------------------------------- #
# 6. 会话生命周期
# --------------------------------------------------------------------------- #
async def test_sessions_are_isolated_per_task(repo: Path) -> None:
    first = _context(repo, "q2-a")
    second = _context(repo, "q2-b")
    await _enter(first, name="alpha")
    await _enter(second, name="beta")
    assert first.root != second.root
    assert first.root.name == "alpha" and second.root.name == "beta"
    assert worktree_session.active_root("q2-a") == first.root
    await _exit(first, action="keep")
    # 摘掉 a 的记录不影响 b。
    assert worktree_session.active_root("q2-a") is None
    assert worktree_session.active_root("q2-b") == second.root


async def test_release_keeps_the_directory_on_disk(repo: Path) -> None:
    context = _context(repo, "q2-release")
    await _enter(context, name="survivor")
    tree = context.root

    worktree_session.release("q2-release")

    assert worktree_session.get("q2-release") is None
    assert tree.is_dir()


async def test_active_root_drops_a_directory_deleted_outside(repo: Path) -> None:
    context = _context(repo, "q2-gone")
    await _enter(context, name="gone")
    tree = context.root

    shutil.rmtree(tree)

    assert worktree_session.active_root("q2-gone") is None
    assert worktree_session.get("q2-gone") is None


# --------------------------------------------------------------------------- #
# 7. 与运行时接线
# --------------------------------------------------------------------------- #
def _finish_scope(factory, task_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        store=factory,
        task_id=task_id,
        recorder=EventRecorder(factory, task_id),
        session=SimpleNamespace(router=None),
        subagents=None,
    )


async def test_finishing_the_task_releases_but_keeps_the_worktree(env, repo: Path) -> None:  # noqa: F811
    project_id = await create_project(env, repo)
    task_id = await create_task(env, project_id, ["收尾不删工作树"])
    factory = get_session_factory()
    session = worktree_session.enter(repo, task_id, name="kept")
    assert session.root.is_dir()

    await AgentRuntime(factory)._finish(
        _finish_scope(factory, task_id), "succeeded", summary="完成", reason=""
    )

    assert worktree_session.get(task_id) is None
    assert session.root.is_dir(), "任务收尾替用户删了还没合并的工作"


async def test_waiting_approval_keeps_the_worktree_session(env, repo: Path) -> None:  # noqa: F811
    project_id = await create_project(env, repo)
    task_id = await create_task(env, project_id, ["审批中仍停在树里"])
    factory = get_session_factory()
    session = worktree_session.enter(repo, task_id, name="paused")

    await AgentRuntime(factory)._finish(
        _finish_scope(factory, task_id), "waiting_approval", summary="等待批准", reason=""
    )

    assert worktree_session.get(task_id) is session
    assert worktree_session.active_root(task_id) == session.root


class _TinyAdapter(ModelAdapter):
    """只为把运行时装配起来：这条用例断言的是根落在哪，不是模型行为。"""

    def __init__(self) -> None:
        super().__init__(
            AdapterConfig(
                name="tiny",
                kind="openai",
                base_url="http://localhost",
                api_key="",
                model="tiny",
                context_window=32_000,
                supports_tools=True,
            )
        )

    async def invoke(self, messages, **kwargs) -> ModelResponse:  # pragma: no cover
        return ModelResponse(text="{}", usage=Usage(1, 1), model="tiny")

    async def stream(self, messages, **kwargs):  # pragma: no cover
        yield ""

    async def bind_tools(self, specs, messages, **kwargs) -> ModelResponse:  # pragma: no cover
        return ModelResponse(text="ok", usage=Usage(1, 1), model="tiny")


async def test_prepare_resumes_inside_the_recorded_worktree(repo: Path) -> None:
    task_id = "q2-prepare"
    session = worktree_session.enter(repo, task_id, name="resume")
    runtime = AgentRuntime(session_factory=get_session_factory())
    request = RunRequest(
        task_id=task_id,
        provider_id="tiny",
        provider_name="Tiny",
        adapter=_TinyAdapter(),
        root=repo,
        goal="恢复后还在工作树里",
        max_steps=2,
    )

    scope = await runtime._prepare(request, "q2-prepare:1")

    assert scope.session.context.root == session.root
    assert scope.session.context.task_id == task_id
    # 事件记录器也陪到底：恢复后的 worktree.exited 还能写进去。
    assert scope.session.context.recorder is scope.recorder


async def test_prepare_falls_back_when_the_recorded_tree_is_gone(repo: Path) -> None:
    task_id = "q2-prepare-gone"
    session = worktree_session.enter(repo, task_id, name="vanished")
    shutil.rmtree(session.root)

    runtime = AgentRuntime(session_factory=get_session_factory())
    request = RunRequest(
        task_id=task_id,
        provider_id="tiny",
        provider_name="Tiny",
        adapter=_TinyAdapter(),
        root=repo,
        goal="树被删了就回共享工作区",
        max_steps=2,
    )
    scope = await runtime._prepare(request, "q2-prepare-gone:1")

    assert Path(scope.session.context.root).resolve() == repo.resolve()

"""基准线与回滚：改错了要有回去的路，包括已经提交的改动。

来历：Better Harness 判「回滚只覆盖工具改过的文件」——任务在工作树里改文件，异常时
harness 只把中途产物隔离（``_quarantine_scratch``），提交之后发现问题只能人手动
revert；``worktree_session`` 记了 root，却没记基准，也没有按任务恢复的入口。
风险不在"改错了能不能发现"（校验会报），而在"发现了能不能退回去"。

判据钉在"拿掉实现就红"的位置：

* 不写 baseline → 第 1 条红（认领记录里没有基准，回滚无从算起）；
* release 连基准一起删 → 第 2 条红（任务收尾后最需要回滚时反而查不到基准）；
* 只用 ``git diff`` 算改动 → 第 3 条红（shell 新建的文件漏掉，回滚完还留着）；
* 回滚不动提交 → 第 4 条红（已提交的改动仍然没有回去的路）；
* 整棵回滚不区分"基准之前就有的未跟踪文件" → 第 6 条红（把别人的草稿删了）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from blank_api.services import worktree_session
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.worktree import worktree_tools
from tests.test_worktree_tools_v32 import repo  # noqa: F401  (git 仓库夹具)


ENTER = next(tool for tool in worktree_tools() if tool.name == "enter_worktree")
RESTORE = next(tool for tool in worktree_tools() if tool.name == "restore_worktree")


@pytest.fixture(autouse=True)
def _clear_sessions():
    yield
    for task_id in list(worktree_session._SESSIONS):
        worktree_session.release(task_id)


def _context(root: Path, task_id: str) -> ToolContext:
    return ToolContext(root=root, task_id=task_id, session_factory=None)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _commit(root: Path, message: str) -> str:
    identity = [
        "-c", "user.name=blank-test",
        "-c", "user.email=blank-test@example.com",
        "-c", "commit.gpgsign=false",
    ]
    _git(root, *identity, "add", "-A")
    done = _git(root, *identity, "commit", "-qm", message)
    assert done.returncode == 0, done.stderr
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _enter(root: Path, task_id: str, *, name: str = "task-a") -> tuple[ToolContext, Path]:
    import asyncio

    context = _context(root, task_id)
    result = asyncio.run(ENTER.handler(context, {"name": name}))
    assert result.ok, result.message
    session = worktree_session.get(task_id)
    assert session is not None
    return context, session.root


def _restore(context: ToolContext, **arguments) -> dict:
    import asyncio

    outcome = asyncio.run(RESTORE.handler(context, arguments))
    assert outcome.ok, outcome.message
    return outcome.data


# --------------------------------------------------------------------------- #
# 1. 基准线记在哪、活多久
# --------------------------------------------------------------------------- #
def test_enter_records_baseline_in_owner_and_survives_release(repo: Path) -> None:  # noqa: F811
    context, root = _enter(repo, "task-baseline")
    head = _git(root, "rev-parse", "HEAD").stdout.strip()

    owner_file = worktree_session._owner_file(root)
    assert owner_file is not None
    owner = json.loads(owner_file.read_text(encoding="utf-8"))
    assert owner["baseline"] == head, "基准线要进认领记录，事后在同一个文件里就能核对"

    baseline_file = worktree_session._baseline_file(root, owner_file)
    assert baseline_file is not None and baseline_file.is_file()
    record = worktree_session.read_baseline(root)
    assert record["baseline"] == head and record["task_id"] == "task-baseline"

    # 任务收尾只松认领，基准线留着——用户往往在收尾之后才发现改错了。
    worktree_session.release("task-baseline")
    assert not owner_file.exists()
    assert baseline_file.is_file()
    assert worktree_session.read_baseline(root)["baseline"] == head


# --------------------------------------------------------------------------- #
# 2. 改动集合：跟踪的与新建的都要算
# --------------------------------------------------------------------------- #
def test_changed_paths_include_shell_created_files(repo: Path) -> None:  # noqa: F811
    context, root = _enter(repo, "task-changed")
    record = worktree_session.read_baseline(root)
    (root / "main.py").write_text("print('changed')\n", encoding="utf-8")
    (root / "added.py").write_text("x = 1\n", encoding="utf-8")

    facts = worktree_session.changed_paths(root, record["baseline"])
    assert "main.py" in facts["tracked"]
    assert "added.py" in facts["added"], "只用 git diff 会漏掉 shell 新建的文件"

    plan = _restore(context, action="plan")
    assert set(plan["changed"]) == {"main.py", "added.py"}
    # plan 只读：内容不能被动过。
    assert (root / "main.py").read_text(encoding="utf-8") == "print('changed')\n"


# --------------------------------------------------------------------------- #
# 3. 整棵回滚：内容回基准、新建的删掉、工作树干净
# --------------------------------------------------------------------------- #
def test_restore_all_returns_content_and_removes_new_files(repo: Path) -> None:  # noqa: F811
    context, root = _enter(repo, "task-restore-all")
    before = (root / "main.py").read_text(encoding="utf-8")
    (root / "main.py").write_text("print('broken')\n", encoding="utf-8")
    (root / "scratch_module.py").write_text("pass\n", encoding="utf-8")

    data = _restore(context, action="restore")
    assert data["clean"] is True
    assert (root / "main.py").read_text(encoding="utf-8") == before
    assert not (root / "scratch_module.py").exists()
    assert data["restored"] == ["main.py"]
    assert data["removed"] == ["scratch_module.py"]
    assert _git(root, "status", "--porcelain", "-uall").stdout.strip() == ""


# --------------------------------------------------------------------------- #
# 4. 已提交的改动也要有回去的路
# --------------------------------------------------------------------------- #
def test_restore_drops_commits_made_in_the_worktree(repo: Path) -> None:  # noqa: F811
    context, root = _enter(repo, "task-committed")
    baseline = worktree_session.read_baseline(root)["baseline"]
    (root / "main.py").write_text("print('v2')\n", encoding="utf-8")
    committed = _commit(root, "task commit")
    assert committed != baseline

    data = _restore(context, action="restore")
    assert data["commits_dropped"] == 1
    assert data["revision"] == baseline
    assert _git(root, "rev-parse", "HEAD").stdout.strip() == baseline
    assert (root / "main.py").read_text(encoding="utf-8") == "print('old')\n"
    assert data["clean"] is True


# --------------------------------------------------------------------------- #
# 5. 部分回滚：只退点名的路径
# --------------------------------------------------------------------------- #
def test_restore_selected_paths_only(repo: Path) -> None:  # noqa: F811
    context, root = _enter(repo, "task-partial")
    (root / "main.py").write_text("print('one')\n", encoding="utf-8")
    (root / "other.py").write_text("print('two')\n", encoding="utf-8")

    data = _restore(context, action="restore", paths=["main.py"])
    assert data["restored"] == ["main.py"]
    assert (root / "main.py").read_text(encoding="utf-8") == "print('old')\n"
    assert (root / "other.py").read_text(encoding="utf-8") == "print('two')\n"
    # 还有别的改动没退：不能报"干净"，那是谎报。
    assert data["clean"] is False
    assert data["dirty_after"] == ["other.py"]


def test_restore_rejects_paths_without_changes(repo: Path) -> None:  # noqa: F811
    import asyncio

    context, _root = _enter(repo, "task-missing-path")
    outcome = asyncio.run(
        RESTORE.handler(context, {"action": "restore", "paths": ["never_touched.py"]})
    )
    assert outcome.ok is False
    assert "无需回滚" in (outcome.message or "")


# --------------------------------------------------------------------------- #
# 6. 基准之前就存在的未跟踪文件不能动
# --------------------------------------------------------------------------- #
def test_preexisting_untracked_files_survive_restore(repo: Path) -> None:  # noqa: F811
    manual = repo / ".blank-worktrees" / "manual"
    _git(repo, "worktree", "add", "-q", "-b", "blank/worktree-manual", str(manual), "HEAD")
    (manual / "draft.txt").write_text("not mine\n", encoding="utf-8")

    context = _context(repo, "task-path-enter")
    import asyncio

    result = asyncio.run(ENTER.handler(context, {"path": str(manual)}))
    assert result.ok, result.message
    (manual / "main.py").write_text("print('intruded')\n", encoding="utf-8")

    data = _restore(context, action="restore")
    assert (manual / "draft.txt").exists(), "别人没提交的草稿不是本任务改的，不能删"
    assert (manual / "main.py").read_text(encoding="utf-8") == "print('old')\n"
    assert data["clean"] is True, "未跟踪文件不算脏——它本来就在，回滚也没动它"


# --------------------------------------------------------------------------- #
# 7. 拒绝的两种情况：没有基准、别人还在写
# --------------------------------------------------------------------------- #
def test_restore_refuses_without_baseline(repo: Path) -> None:  # noqa: F811
    import asyncio

    context = _context(repo, "task-no-baseline")
    outcome = asyncio.run(RESTORE.handler(context, {"action": "restore", "worktree": "ghost"}))
    assert outcome.ok is False
    assert "不存在" in (outcome.message or "") or "没有基准线" in (outcome.message or "")


def test_restore_refuses_when_another_live_task_holds_the_tree(repo: Path) -> None:  # noqa: F811
    _context_, root = _enter(repo, "task-holder")
    owner_file = worktree_session._owner_file(root)
    assert owner_file is not None
    payload = json.loads(owner_file.read_text(encoding="utf-8"))
    payload["task_id"] = "task-other"
    payload["pid"] = __import__("os").getpid()
    owner_file.write_text(json.dumps(payload), encoding="utf-8")
    # 另一个任务确实还在本进程里干活：认领写着别人、而那个人还活着。
    # 只把 pid 改成 1 在 Windows 上会被判成"没有这个进程"（87），模拟不出"活着"。
    live = worktree_session.get("task-holder")
    assert live is not None
    worktree_session._SESSIONS["task-other"] = live

    import asyncio

    outcome = asyncio.run(
        RESTORE.handler(_context(root, "task-intruder"), {"action": "restore", "worktree": "task-a"})
    )
    assert outcome.ok is False
    assert "归任务" in (outcome.message or "") or "不能回滚" in (outcome.message or "")


# --------------------------------------------------------------------------- #
# 8. 工具面：按需加载、参数分档、计划模式不可用
# --------------------------------------------------------------------------- #
def test_restore_tool_is_registered_and_argument_scoped() -> None:
    from blank_api.services.agent.graph import READONLY_TOOLS
    from blank_api.services.permissions import ARGUMENT_SCOPED_TOOLS
    from blank_api.services.tools.registry import ALIASES, DEFERRED_TOOLS, PARALLEL_SAFE_TOOLS

    assert RESTORE.name == "restore_worktree"
    assert RESTORE.input_schema["required"] == ["action"]
    assert RESTORE.input_schema["properties"]["action"]["enum"] == ["plan", "restore"]
    assert "restore_worktree" in DEFERRED_TOOLS
    assert "回滚" in ALIASES["restore_worktree"]
    assert "restore_worktree" not in PARALLEL_SAFE_TOOLS
    assert "restore_worktree" not in READONLY_TOOLS
    # plan 只读、restore 会退提交：必须按参数分档，不能"批准一次以后都免问"。
    assert "restore_worktree" in ARGUMENT_SCOPED_TOOLS
    assert RESTORE.risk_for({"action": "plan"}) == "low"
    assert RESTORE.risk_for({"action": "restore"}) == "high"
    assert "restore_worktree" in {tool.name for tool in build_default_tools()}

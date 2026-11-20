"""写者唯一：同一棵 worktree 同时只能有一个任务在写。

来历：``docs/external-qwen-code-2026-11.md`` 可移植清单第 4 项（``worktree-owner-state``）。
不是"对齐外部项目"，而是这里真的会出两个写者——``enter_worktree(path=...)`` 允许
进入任何已注册的工作树，任务 A 在里面干活时任务 B 也能进，两边的 diff 与验证结论
都建立在对方随时会改的树上，最后的"改完了"对谁都不算数。

判据都钉在"拿掉实现就红"的位置：
- 删掉 enter 里的认领检查 → 第 1 条红（第二个任务能进）；
- 删掉 exit 里的归属检查 → 第 3 条红（别人能删你正在用的树）；
- 把 reclaimed 自愈去掉 → 第 4 条红（死在半路的任务永久占住这棵树）；
- 把 unavailable 当成 owned → 第 5 条红（非 git 目录也敢说"独占"）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from blank_api.services import worktree_session

from tests.test_worktree_tools_v32 import repo  # noqa: F401  (git 仓库夹具)


@pytest.fixture(autouse=True)
def _clear_sessions():
    """用例之间不共享工作树会话：漏一个 release，下一个用例就进不去。"""
    yield
    for task_id in list(worktree_session._SESSIONS):
        worktree_session.release(task_id)


def _enter(root: Path, task_id: str, name: str = "") -> worktree_session.WorktreeSession:
    return worktree_session.enter(root, task_id, name=name or f"wt-{task_id}")


def test_a_second_task_cannot_write_in_the_same_tree(repo: Path):  # noqa: F811
    """A 进来之后，B 进同一棵树必须被拒，并说清为什么。"""
    first = _enter(repo, "task-a")
    assert first.owner_status == worktree_session.OWNER_OWNED

    with pytest.raises(ValueError) as excinfo:
        worktree_session.enter(repo, "task-b", path=str(first.root))
    message = str(excinfo.value)
    assert "两个写者" in message, message
    # 报的是持有者，不是"这个路径不行"——用户要知道在等谁。
    assert "task-a"[:8] in message, message

    # 被拒的任务不能留下会话记录：否则它的写入会绕过 exit 的归属检查。
    assert worktree_session.get("task-b") is None


def test_the_tree_is_free_again_after_the_owner_releases_it(repo: Path):  # noqa: F811
    first = _enter(repo, "task-a")
    worktree_session.release("task-a")
    # 释放要连认领一起松：只摘内存记录的话，下一任会读到"别人的任务"。
    assert worktree_session.read_owner(first.root) == {}

    second = worktree_session.enter(repo, "task-b", path=str(first.root))
    assert second.owner_status == worktree_session.OWNER_OWNED


def test_a_tree_held_by_another_task_cannot_be_removed(repo: Path):  # noqa: F811
    """别人持有的树，本任务不能删——删掉等于把他正在做的工作从盘上抽走。"""
    first = _enter(repo, "task-a")
    # 手工造出"B 持有"的状态：A 已经进去了，不能再让 B 正常进入。
    owner_file = worktree_session._owner_file(first.root)
    assert owner_file is not None
    owner_file.write_text(
        json.dumps({"task_id": "task-b", "pid": os.getpid(), "claim_id": "x", "at": ""}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        worktree_session.exit_session("task-a", action="remove")
    assert "不能由本任务删除" in str(excinfo.value)
    # 树还在：拒绝之后什么都没发生。
    assert first.root.is_dir()


def test_a_claim_left_by_a_dead_task_is_reclaimed(repo: Path):  # noqa: F811
    """持有者在本进程里但会话已经没了（进程被杀/异常退出）→ 可以接手。

    没有这一档，一次崩溃会把整棵树永久锁死：文件在、任务不在，谁都进不去。
    """
    first = _enter(repo, "task-a")
    # 模拟"task-a 中途死了"：会话摘掉，认领文件留着（release 没来得及跑）。
    worktree_session._SESSIONS.pop("task-a", None)

    status, owner = worktree_session.claim_owner(first.root, "task-b")
    assert status == worktree_session.OWNER_RECLAIMED, (status, owner)
    assert owner["task_id"] == "task-b"

    fresh = worktree_session.enter(repo, "task-b", path=str(first.root))
    assert fresh.owner_status == worktree_session.OWNER_RECLAIMED
    assert worktree_session.read_owner(fresh.root)["task_id"] == "task-b"


def test_a_non_git_directory_is_reported_as_unavailable_not_owned(tmp_path: Path):
    """读不到 git 目录时不能假装拿到了独占——那等于静默放弃写者唯一。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    status, _ = worktree_session.claim_owner(plain, "task-a")
    assert status == worktree_session.OWNER_UNAVAILABLE
    assert status != worktree_session.OWNER_OWNED


def test_the_claim_file_never_lands_in_the_working_tree(repo: Path):  # noqa: F811
    """认领写进 git 元数据，不能写进工作树。

    写进工作树根会让 ``git status`` 多一个未跟踪文件：我们自己的 remove 闸门
    会把它算成"还有未提交改动"，于是用户永远删不掉这棵树。
    """
    session = _enter(repo, "task-a")
    assert not (session.root / worktree_session.OWNER_FILENAME).exists()
    owner_file = worktree_session._owner_file(session.root)
    assert owner_file is not None and owner_file.is_file()
    # 元数据目录在工作树之外。
    assert session.root not in owner_file.parents

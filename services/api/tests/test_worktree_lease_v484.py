"""认领的租约：崩溃留下的认领不能把整棵树永久锁死。

来历：Better Harness 那一轮判「认领文件没有存活探测」——跨进程只认「pid 不同即他人
所有」，于是别的进程被杀之后，那棵树会一直被记成"正被某个任务使用"，直到有人手
去删 ``.git/worktrees/<name>/blank-owner.json``。单进程部署碰不到，桌面端 + CLI
同时跑就会变成"明明没人用却进不去"。

判据钉在"拿掉实现就红"的位置：

* 去掉 ``_pid_alive`` 的死活判定 → 第 1 条红（死进程的认领永远收不回来）；
* 去掉租约 → 第 2 条红（判断不了死活的旧认领永久占位）；
* 去掉"新鲜就拒绝" → 第 3 条红（两个活着的进程同时进一棵树，正是写者唯一要防的）；
* 把续约写成无条件重写 → 第 4 条红（claim_id 变了，等于换了个持有者）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blank_api.services import worktree_session

from tests.test_worktree_tools_v32 import repo  # noqa: F401  (git 仓库夹具)


@pytest.fixture(autouse=True)
def _clear_sessions():
    yield
    for task_id in list(worktree_session._SESSIONS):
        worktree_session.release(task_id)


def _write_claim(root: Path, **fields) -> Path:
    """直接写认领文件：模拟"另一个进程留下的记录"，不经过 enter()。"""
    path = worktree_session._owner_file(root)
    assert path is not None
    payload = {
        "task_id": "task-ghost",
        "pid": 999_999,
        "claim_id": "ghostclaim",
        "at": datetime.now(timezone.utc).isoformat(),
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(fields)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _stale(seconds: float | None = None) -> str:
    age = worktree_session.LEASE_SECONDS + 60 if seconds is None else seconds
    return (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()


def test_a_claim_whose_process_is_gone_is_reclaimed(repo: Path, monkeypatch):  # noqa: F811
    """pid 已经不在了 → 立刻可以接手，不用等租约到期。

    心跳还新鲜也要接手：进程都没了，它不可能再续。
    """
    worktree_session.enter(repo, "task-a", name="wt-lease-dead")
    root = worktree_session.get("task-a").root
    worktree_session.release("task-a")
    _write_claim(root, pid=999_999, heartbeat_at=datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(worktree_session, "_pid_alive", lambda pid: False)

    status, owner = worktree_session.claim_owner(root, "task-b")

    assert status == worktree_session.OWNER_RECLAIMED, (status, owner)
    assert owner["task_id"] == "task-b"


def test_a_claim_past_its_lease_is_reclaimed_even_when_liveness_is_unknown(repo: Path, monkeypatch):  # noqa: F811
    """判断不了死活（权限不足 / 别的机器）时，租约是唯一的兜底。

    这条正是报告里那一条："超过 N 分钟未续即视为 stale 可接手"。
    """
    worktree_session.enter(repo, "task-a", name="wt-lease-stale")
    root = worktree_session.get("task-a").root
    worktree_session.release("task-a")
    _write_claim(root, pid=999_999, heartbeat_at=_stale())
    monkeypatch.setattr(worktree_session, "_pid_alive", lambda pid: None)

    status, owner = worktree_session.claim_owner(root, "task-b")

    assert status == worktree_session.OWNER_RECLAIMED, (status, owner)
    assert owner["task_id"] == "task-b"


def test_a_fresh_claim_from_a_live_process_still_blocks(repo: Path, monkeypatch):  # noqa: F811
    """活着 + 心跳新鲜 → 仍然拒绝。租约不能变成放行的通道。"""
    worktree_session.enter(repo, "task-a", name="wt-lease-fresh")
    root = worktree_session.get("task-a").root
    worktree_session.release("task-a")
    _write_claim(root, pid=999_999, heartbeat_at=datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(worktree_session, "_pid_alive", lambda pid: True)

    status, owner = worktree_session.claim_owner(root, "task-b")

    assert status == worktree_session.OWNER_OTHER, (status, owner)
    assert owner["task_id"] == "task-ghost"
    # 拒绝时不能把文件改成自己的：那等于"拒了但已经把树占了"。
    assert worktree_session.read_owner(root)["task_id"] == "task-ghost"


def test_an_old_claim_without_timestamps_is_refused_not_guessed(repo: Path, monkeypatch):  # noqa: F811
    """时间戳认不出（老版本写的文件）→ 保守拒绝。

    放行的代价是两个写者同时改一棵树，那比"多拒绝一次、换个名字"贵得多。
    """
    worktree_session.enter(repo, "task-a", name="wt-lease-legacy")
    root = worktree_session.get("task-a").root
    worktree_session.release("task-a")
    _write_claim(root, pid=999_999, at="", heartbeat_at="")
    monkeypatch.setattr(worktree_session, "_pid_alive", lambda pid: None)

    status, _ = worktree_session.claim_owner(root, "task-b")

    assert status == worktree_session.OWNER_OTHER


def test_entering_a_tree_freed_by_a_crash_says_reclaimed(repo: Path, monkeypatch):  # noqa: F811
    """接手要如实标成 reclaimed，模型才知道这棵树上可能留着别人的半截改动。

    走 enter 这条真实路径，而不是只手搓 claim_owner——函数对了但没接线是最常见的漏。
    """
    worktree_session.enter(repo, "task-a", name="wt-lease-enter")
    root = worktree_session.get("task-a").root
    worktree_session.release("task-a")
    _write_claim(root, pid=999_999, heartbeat_at=_stale())
    monkeypatch.setattr(worktree_session, "_pid_alive", lambda pid: None)

    session = worktree_session.enter(repo, "task-b", path=str(root))

    assert session.owner_status == worktree_session.OWNER_RECLAIMED
    assert worktree_session.read_owner(root)["task_id"] == "task-b"


# ── 存活探测本身：不能只靠 monkeypatch 证明它有用 ────────────────────────


def test_the_liveness_probe_tells_our_own_process_apart_from_a_finished_one():
    """真跑一次探测：本进程活着、刚退出的子进程已经不在。

    这一条防的是"判据全靠 monkeypatch"——那种测试下 ``_pid_alive`` 就算永远返回
    ``None``，上面几条也照样绿（它们把函数本身替换掉了），于是租约永远等不到
    判定，崩溃留下的锁还是锁着。
    """
    assert worktree_session._pid_alive(os.getpid()) is True

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    # 子进程确实退出了；反过来问它还在不在，答案必须是"不在"。
    assert worktree_session._pid_alive(child.pid) is False
    # pid 0 不是"死进程"，是"问不出结果"。
    assert worktree_session._pid_alive(0) is None


def test_renewing_keeps_the_same_holder(repo: Path):  # noqa: F811
    """续约只动心跳：claim_id 变了就等于换了个持有者，别人的释放会失败。"""
    worktree_session.enter(repo, "task-a", name="wt-lease-renew")
    session = worktree_session.get("task-a")
    before = worktree_session.read_owner(session.root)
    old_stamp = _stale(seconds=worktree_session.LEASE_SECONDS)
    path = _write_claim(
        session.root,
        task_id="task-a",
        pid=before["pid"],
        claim_id=before["claim_id"],
        at=before["at"],
        heartbeat_at=old_stamp,
    )
    assert path.is_file()

    assert worktree_session.renew_owner(session.root, "task-a", path=session.owner_file, interval=0)

    after = worktree_session.read_owner(session.root)
    assert after["claim_id"] == before["claim_id"]
    assert after["at"] == before["at"]
    assert after["heartbeat_at"] > old_stamp


def test_renewing_never_touches_another_tasks_claim(repo: Path, monkeypatch):  # noqa: F811
    worktree_session.enter(repo, "task-a", name="wt-lease-other")
    session = worktree_session.get("task-a")
    _write_claim(session.root, task_id="task-b", heartbeat_at=_stale())

    assert worktree_session.renew_owner(session.root, "task-a", path=session.owner_file) is False
    assert worktree_session.read_owner(session.root)["task_id"] == "task-b"


def test_working_in_the_tree_renews_the_lease(repo: Path):  # noqa: F811
    """接线的判据：``active_root`` 是"正要在这棵树里干活"的信号，得顺手续约。

    只测 ``renew_owner`` 会漏掉"函数写对了但没人调用"。
    """
    worktree_session.enter(repo, "task-a", name="wt-lease-wired")
    session = worktree_session.get("task-a")
    old_stamp = _stale(seconds=worktree_session.LEASE_SECONDS)
    before = worktree_session.read_owner(session.root)
    _write_claim(
        session.root, task_id="task-a", claim_id=before["claim_id"], at=before["at"], heartbeat_at=old_stamp
    )

    assert worktree_session.active_root("task-a") == session.root

    assert worktree_session.read_owner(session.root)["heartbeat_at"] > old_stamp

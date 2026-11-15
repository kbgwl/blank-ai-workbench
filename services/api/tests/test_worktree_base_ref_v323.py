"""#323 ``worktree.baseRef``：enter_worktree 新建工作树的基准引用。

上游设置项（settings 面板 ``worktreeBaseRef``，35550204）与建树消费点
（26601962：``r?.fromHead ?? (!r?.prNumber && Ge().worktree?.baseRef==="head")``）：

* ``fresh``（默认）：从 ``origin/<默认分支>`` 建树；默认分支先看
  ``refs/remotes/origin/HEAD`` 的符号引用，再看 ``origin/main`` / ``origin/master``，
  都认不出时按 ``main``；
* ``head``：从当前本地 HEAD 建树（旧行为）。

本机偏离：上游在远端跟踪分支超过 24h 时 ``git fetch``（5s 超时，失败告警并回落
本地 HEAD）。这里**不主动 fetch**——建树是工具层动作，网络出口统一归沙箱策略管，
不在这条路径上偷偷开一个口子；只读已有的远端跟踪分支，没有就回落本地 HEAD。

判据钉住：设置归一（驼峰/下划线、坏值回落）、层级合并、两条基准路径、默认分支
解析、无远端回落、项目层读取、工具描述。改回「恒用 HEAD」，红在 fresh 两条；
把 fresh 当 head，红在 fresh 与默认分支两条；默认分支只认 main，红在 trunk 一条；
工具不读设置层，红在 head 一条。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from blank_api.services import worktree_session
from blank_api.services.settings import normalize
from blank_api.services.settings_layers import load_layered_settings
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.worktree import worktree_tools

ENTER = next(tool for tool in worktree_tools() if tool.name == "enter_worktree")


@pytest.fixture(autouse=True)
def _isolated_user_settings(tmp_path, monkeypatch):
    # 每个用例用一份空的用户层，项目层写在各用例自己的仓库里。
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(tmp_path / "user-settings.json"))


# --------------------------------------------------------------------------- #
# 装置
# --------------------------------------------------------------------------- #
def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(root / ".gitconfig"),
            "PATH": os.environ.get("PATH", ""),
        },
    )


def _commit(root: Path, name: str, text: str) -> str:
    (root / name).write_text(text, encoding="utf-8")
    _git(root, "add", name)
    result = _git(root, "commit", "-q", "-m", name)
    assert result.returncode == 0, result.stderr
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert _git(root, "init", "-q", "-b", "main").returncode == 0
    return root


def _setting(root: Path, value: str) -> None:
    (root / ".blank").mkdir(exist_ok=True)
    (root / ".blank" / "settings.json").write_text(
        '{"worktree": {"baseRef": "%s"}}' % value, encoding="utf-8"
    )


def _context(root: Path, **extra) -> ToolContext:
    # 会话按 task_id 存在进程内；每个用例的仓库都是新目录，用路径当 task_id
    # 才不会让上一个用例的会话挡住这一次的 enter。
    return ToolContext(root=root, task_id=f"task:{root}", session_factory=None, **extra)


async def _enter(root: Path, name: str = "wt") -> dict:
    outcome = await ENTER.handler(_context(root), {"name": name})
    assert outcome.ok, outcome.message
    return outcome.data


def _worktree_head(root: Path, name: str = "wt") -> str:
    return _git(root / ".blank-worktrees" / name, "rev-parse", "HEAD").stdout.strip()


# --------------------------------------------------------------------------- #
# 1. 设置归一与层级
# --------------------------------------------------------------------------- #
def test_normalize_defaults_to_fresh_and_accepts_aliases():
    assert normalize({})["worktree"]["base_ref"] == "fresh"
    assert normalize({"worktree": {"baseRef": "head"}})["worktree"]["base_ref"] == "head"
    assert normalize({"worktree": {"base_ref": " HEAD "}})["worktree"]["base_ref"] == "head"
    # 认不出的值回落默认，而不是让一个坏词决定建树基准。
    assert normalize({"worktree": {"baseRef": "origin"}})["worktree"]["base_ref"] == "fresh"


def test_project_layer_carries_worktree_base_ref(tmp_path: Path):
    root = _repo(tmp_path)
    _setting(root, "head")
    layered = load_layered_settings(root, user_path=tmp_path / "missing-user.json", env={})
    assert layered.settings["worktree"]["base_ref"] == "head"
    assert not any(item.get("section") == "worktree" for item in layered.layers[1].ignored)


# --------------------------------------------------------------------------- #
# 2. 两条基准路径
# --------------------------------------------------------------------------- #
async def test_fresh_uses_origin_tracking_ref(tmp_path: Path):
    root = _repo(tmp_path)
    base = _commit(root, "a.txt", "a")
    _git(root, "update-ref", "refs/remotes/origin/main", base)
    _git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    ahead = _commit(root, "b.txt", "b")
    assert ahead != base
    _setting(root, "fresh")

    data = await _enter(root)
    assert data["base_ref"] == "origin/main", data
    assert _worktree_head(root) == base


async def test_head_uses_local_head(tmp_path: Path):
    root = _repo(tmp_path)
    base = _commit(root, "a.txt", "a")
    _git(root, "update-ref", "refs/remotes/origin/main", base)
    ahead = _commit(root, "b.txt", "b")
    _setting(root, "head")

    data = await _enter(root)
    assert data["base_ref"] == "HEAD", data
    assert _worktree_head(root) == ahead


async def test_fresh_falls_back_to_local_head_without_origin(tmp_path: Path):
    root = _repo(tmp_path)
    _commit(root, "a.txt", "a")
    head = _commit(root, "b.txt", "b")
    # 不写设置：默认 fresh。没有远端跟踪分支，回落本地 HEAD。
    data = await _enter(root)
    assert data["base_ref"] == "HEAD", data
    assert _worktree_head(root) == head


async def test_default_branch_follows_origin_head(tmp_path: Path):
    root = _repo(tmp_path)
    base = _commit(root, "a.txt", "a")
    _git(root, "update-ref", "refs/remotes/origin/trunk", base)
    _git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    _commit(root, "b.txt", "b")
    _setting(root, "fresh")

    data = await _enter(root)
    assert data["base_ref"] == "origin/trunk", data
    assert _worktree_head(root) == base


async def test_default_branch_falls_back_to_master(tmp_path: Path):
    root = _repo(tmp_path)
    base = _commit(root, "a.txt", "a")
    # 没有 origin/HEAD；只有 origin/master。候选表要能认出来。
    _git(root, "update-ref", "refs/remotes/origin/master", base)
    _commit(root, "b.txt", "b")
    _setting(root, "fresh")

    data = await _enter(root)
    assert data["base_ref"] == "origin/master", data
    assert _worktree_head(root) == base


async def test_unknown_base_ref_value_treated_as_fresh(tmp_path: Path):
    root = _repo(tmp_path)
    base = _commit(root, "a.txt", "a")
    _git(root, "update-ref", "refs/remotes/origin/main", base)
    _git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    _commit(root, "b.txt", "b")
    _setting(root, "weird")

    data = await _enter(root)
    assert data["base_ref"] == "origin/main", data
    assert _worktree_head(root) == base


def test_service_treats_unknown_base_ref_as_fresh(tmp_path: Path):
    # 工具路径上 normalize 已经把坏值折成 fresh；服务层是公开入口，直接调用时
    # 也不能把认不出的词当 head 用。
    root = _repo(tmp_path)
    base = _commit(root, "a.txt", "a")
    _git(root, "update-ref", "refs/remotes/origin/main", base)
    _git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    _commit(root, "b.txt", "b")

    session = worktree_session.enter(root, f"task:{root}", name="svc", base_ref="weird")
    assert session.base_ref == "origin/main"
    assert _worktree_head(root, "svc") == base


def test_enter_description_names_the_setting():
    text = ENTER.description
    assert "worktree.baseRef" in text, text
    assert "fresh" in text and "head" in text, text

"""闭合记录：改动、校验、验收三样绑在同一个 revision 上。

来历：Better Harness 判「8 个 episode 里没有一条走到交付已验收」——改过的都记成
edited，没有一条被记成 closed。产品这边对应的事实是散的：改动在交付文本里，校验在
库字段里，验收是模型自己填的 covered。三者没有共同锚点，于是"改完并且被验收"这件
事在证据上一次都没发生过。

判据钉在这些位置：

* 记录里不写 revision → 第 1 条红（无法回答"这一版是哪一版"）；
* 默认 decision 写成 accepted → 第 3 条红（模型说完成不等于验收）；
* decide 不校验 revision → 第 5 条红（旧决定被挂到新提交上）；
* 台账不追加 → 第 6 条红（决定只在单个文件里，事后无法排序复核）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.services.agent import closure, graph


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, encoding="utf-8"
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


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    (root / "main.py").write_text("print('old')\n", encoding="utf-8")
    _commit(root, "init")
    return root


# --------------------------------------------------------------------------- #
# 1. 收尾时就写：revision + 改动 + 校验，验收位空着
# --------------------------------------------------------------------------- #
def test_build_binds_current_revision(repo: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    record = closure.build(
        task_id="task-1",
        root=repo,
        changed_paths=["main.py", "gen/x.py"],
        verify_command="pytest -q",
        verify_exit=0,
        verify_covers=["main.py"],
    )
    assert record.revision == head
    assert record.branch != ""
    assert record.decision == closure.DECISION_PENDING
    assert record.closed is False, "模型说完成不是验收：记录不能自己算闭合"

    path = closure.write(repo, record)
    assert path == repo / ".blank" / "closures" / "task-1.json"
    stored = closure.read(repo, "task-1")
    assert stored["changed_paths"] == ["main.py", "gen/x.py"]
    assert stored["verify"] == {"command": "pytest -q", "exit": 0, "covers": ["main.py"]}
    assert stored["closed"] is False


def test_graph_writes_closure_after_delivery(repo: Path) -> None:
    """交付那一刻写闭合记录：再晚工作区就变了，revision 对不上。"""
    events: list[tuple[str, str, dict]] = []

    class _Recorder:
        async def add(self, event: str, node: str, payload: dict) -> None:
            events.append((event, node, payload))

    session = SimpleNamespace(
        context=SimpleNamespace(task_id="task-wire", root=repo),
        recorder=_Recorder(),
        quarantined_paths=(),
    )
    state = {
        "tool_results": [
            {
                "ok": True,
                "tool": "write_file",
                "data": {"path": "main.py", "change_type": "modified", "diff": "@@ -1 +1 @@\n"},
            }
        ],
        "validation": {"command": "pytest -q", "status": "ok", "exit_code": 0, "covered_mutations": 1},
    }
    import asyncio

    asyncio.run(graph._write_closure(session, state, "改好了。\n", [{"criterion": "x", "covered": True}]))

    closed = [item for item in events if item[0] == "task.closure"]
    assert closed, "交付之后必须留一份闭合记录"
    payload = closed[0][2]
    assert payload["revision"] == _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert payload["changed_paths"] == ["main.py"]
    assert payload["decision"] == closure.DECISION_PENDING
    assert payload["acceptance"] == [{"criterion": "x", "covered": True}]
    assert Path(payload["path"]).is_file()


def test_readonly_answer_writes_no_closure(repo: Path) -> None:
    """纯问答没有可闭合的交付：写一条空记录只会稀释"闭过"这件事。"""
    events: list[tuple[str, str, dict]] = []

    class _Recorder:
        async def add(self, event: str, node: str, payload: dict) -> None:
            events.append((event, node, payload))

    session = SimpleNamespace(
        context=SimpleNamespace(task_id="task-answer", root=repo),
        recorder=_Recorder(),
        quarantined_paths=(),
    )
    import asyncio

    asyncio.run(graph._write_closure(session, {"tool_results": []}, "这是一段回答。\n"))
    assert not [item for item in events if item[0] == "task.closure"]


# --------------------------------------------------------------------------- #
# 2. 验收：谁签的、绑哪一版、进台账
# --------------------------------------------------------------------------- #
def test_decide_records_who_and_appends_ledger(repo: Path) -> None:
    record = closure.build(task_id="task-2", root=repo, changed_paths=["main.py"])
    closure.write(repo, record)

    decided = closure.decide(repo, "task-2", decision="accepted", decided_by="user:owner", note="看了 diff")
    assert decided["decision"] == "accepted"
    assert decided["decided_by"] == "user:owner"
    assert decided["closed"] is True

    rows = closure.ledger(repo)
    assert len(rows) == 1
    assert rows[0]["task_id"] == "task-2"
    assert rows[0]["revision"] == record.revision
    assert rows[0]["decided_by"] == "user:owner"
    assert rows[0]["changed_files"] == 1


def test_decide_rejects_when_revision_moved(repo: Path) -> None:
    record = closure.build(task_id="task-3", root=repo, changed_paths=["main.py"])
    closure.write(repo, record)
    (repo / "main.py").write_text("print('v2')\n", encoding="utf-8")
    _commit(repo, "moved on")

    with pytest.raises(ValueError) as exc:
        closure.decide(repo, "task-3", decision="accepted", decided_by="user:owner")
    assert "revision 已经变了" in str(exc.value)
    # 拒绝之后台账不能留一条"已验收"：那样等于把过期决定记成事实。
    assert closure.ledger(repo) == []


def test_decide_needs_a_record_and_a_signer(repo: Path) -> None:
    with pytest.raises(ValueError) as exc:
        closure.decide(repo, "task-missing", decision="accepted", decided_by="user:owner")
    assert "没有闭合记录" in str(exc.value)

    closure.write(repo, closure.build(task_id="task-4", root=repo, changed_paths=["main.py"]))
    with pytest.raises(ValueError) as exc:
        closure.decide(repo, "task-4", decision="accepted", decided_by="  ")
    assert "谁做的决定" in str(exc.value)

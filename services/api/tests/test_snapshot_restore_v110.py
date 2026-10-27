"""#110 快照恢复的三种真实形状：整树可恢复 / 只有清单 / 清单与副本不一致。

以前只有清单的快照在路由层抛 ``FileNotFoundError``，HTTP 上是 500 —— 与
「快照不存在」（404）和「后端坏了」（500，值得重试）都混在一起。这里钉住：
恢复不了返回 409 + 当初跳过复制的原因，能恢复时如实区分 restored / skipped。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from blank_api.db import get_session_factory
from blank_api.models.task import Task
from blank_api.services.sandbox import SnapshotNotRestorable, SnapshotStore
from blank_api.services.sandbox import snapshot as snapshot_module
from tests.test_agent_v04 import API, create_project, create_task, env  # noqa: F401


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("A", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("B", encoding="utf-8")
    return root


async def _finish_task(task_id: str) -> None:
    async with get_session_factory()() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        task.status = "succeeded"
        await session.commit()


def _manifest_only(monkeypatch, workspace: Path, task_id: str | None = None) -> dict:
    """让 capture 走真实的产品分支：扫描总数超过上限 → 只记录清单。"""
    monkeypatch.setattr(snapshot_module, "MAX_SNAPSHOT_FILES", 0)
    return SnapshotStore().capture(str(workspace), reason="覆盖前快照", task_id=task_id)


def test_manifest_only_snapshot_raises_typed_error(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    record = _manifest_only(monkeypatch, workspace)

    assert record["copied"] is False
    assert "工作区过大" in record["skipped_reason"]
    assert not (Path(os.environ["BLANK_SNAPSHOT_DIR"]) / record["id"] / "tree").exists()

    restorable, reason = SnapshotStore().restorable(record["id"])
    assert restorable is False
    assert reason == record["skipped_reason"]

    with pytest.raises(SnapshotNotRestorable) as info:
        SnapshotStore().restore(record["id"], str(workspace))
    assert info.value.reason == record["skipped_reason"]
    assert not isinstance(info.value, FileNotFoundError)

    with pytest.raises(SnapshotNotRestorable):
        SnapshotStore().restore_report(record["id"], str(workspace))

    # 恢复失败不许动工作区：a.txt 还在，sub/b.txt 也还在。
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "A"
    assert (workspace / "sub" / "b.txt").read_text(encoding="utf-8") == "B"


async def test_restore_manifest_only_snapshot_returns_409(env, workspace, monkeypatch):  # noqa: F811
    project_id = await create_project(env, workspace)
    task_id = await create_task(env, project_id, ["能恢复"])
    await _finish_task(task_id)
    record = _manifest_only(monkeypatch, workspace, task_id)

    listed = await env.client.get(f"{API}/tasks/{task_id}/snapshots")
    assert listed.status_code == 200, listed.text
    item = next(row for row in listed.json() if row["id"] == record["id"])
    assert item["restorable"] is False
    assert item["skipped_reason"] == record["skipped_reason"]

    resp = await env.client.post(f"{API}/tasks/{task_id}/snapshots/{record['id']}/restore")
    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["code"] == "snapshot_not_restorable"
    assert record["skipped_reason"] in error["message"]


async def test_restore_reports_restored_and_skipped(env, workspace):  # noqa: F811
    project_id = await create_project(env, workspace)
    task_id = await create_task(env, project_id, ["能恢复"])
    await _finish_task(task_id)
    record = SnapshotStore().capture(str(workspace), reason="覆盖前快照", task_id=task_id)
    expected = set(record["files"])
    assert expected  # 工作区夹具非空，避免空集合让下面的断言永真

    (workspace / "main.py").write_text("changed\n", encoding="utf-8")
    (workspace / "extra.txt").write_text("created after snapshot\n", encoding="utf-8")

    # 扫描见到了、复制时没进 tree/ 的文件：必须点名跳过，不能混进“已恢复”。
    meta_path = Path(os.environ["BLANK_SNAPSHOT_DIR"]) / record["id"] / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["files"].append("ghost.txt")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    resp = await env.client.post(f"{API}/tasks/{task_id}/snapshots/{record['id']}/restore")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body["restored"]) == expected
    assert body["skipped"] == ["ghost.txt"]
    assert (workspace / "main.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert not (workspace / "extra.txt").exists()

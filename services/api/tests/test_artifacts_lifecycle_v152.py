# -*- coding: utf-8 -*-
"""工具产物的生命周期：限额、压缩、统计、按引用清理。

这一组判据的共同点：**每一个都在问"会不会把还在用的东西删掉"**。清理逻辑出错
最坏的结果不是没删干净，而是回滚材料和证据被删了、任务从此打不开——所以每条
"该删"的判定都配一条"不该删"的反面，只有两侧都成立才算判据成立。
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import Task, ToolCall
from blank_api.services import maintenance
from blank_api.services.agent.artifacts import (
    COMPRESS_ABOVE_BYTES,
    MAX_ARTIFACT_BYTES,
    ArtifactStore,
)

# asyncio_mode = auto：同步用例不能挂 asyncio 标记，否则 pytest 会警告"标了但不是协程"。
@pytest.fixture(autouse=True)
async def schema() -> AsyncIterator[None]:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


async def _seed_call(artifact_path: str) -> tuple[str, str]:
    """建一条带 artifact 引用的 ToolCall 行，返回 (task_id, call_id)。"""
    stamp = uuid.uuid4().hex[:8]
    async with get_session_factory()() as session:
        session.add(Project(id=f"p-{stamp}", name="p"))
        session.add(Task(id=f"t-{stamp}", project_id=f"p-{stamp}", title="t", goal="g"))
        await session.flush()
        call_id = f"c-{stamp}"
        session.add(
            ToolCall(
                id=call_id,
                task_id=f"t-{stamp}",
                trace_id=f"t-{stamp}",
                node="execute_action",
                name="run_shell",
                arguments={},
                result_summary="ok",
                artifact_path=artifact_path,
                status="ok",
            )
        )
        await session.commit()
    return f"t-{stamp}", call_id


def _age(path: Path, days: float) -> None:
    """把文件的 mtime 往前推，免得用例要睡上一天。"""
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))


# ── 限额 ────────────────────────────────────────────────────────────────────


async def test_oversized_artifact_is_truncated_and_marked(tmp_path):
    store = ArtifactStore(tmp_path, max_bytes=4096)
    payload = {"tool": "run_shell", "status": "ok", "output": "甲" * 200_000, "message": ""}
    key, meta = store.write("t1", "c1", payload)

    assert meta["truncated"] is True
    assert (tmp_path / key).stat().st_size <= 4096
    loaded = store.load("t1", "c1")
    # 读侧必须能分辨"这份被削过"：否则模型会以为自己读到了完整输出。
    assert loaded["truncated"] is True
    assert loaded["original_bytes"] > 300_000
    assert len(loaded["output"]) < len(payload["output"])


async def test_small_artifact_is_not_touched(tmp_path):
    store = ArtifactStore(tmp_path, max_bytes=4096)
    payload = {"tool": "read_file", "status": "ok", "output": "hello", "data": {"lines": 1}}
    key, meta = store.write("t1", "c1", payload)

    assert meta["truncated"] is False
    assert store.load("t1", "c1") == payload
    assert key == "t1/c1.json"


async def test_default_cap_is_bounded(monkeypatch):
    """默认上限本身要是个有界的数，否则"有上限"只是句口号。"""
    assert 0 < MAX_ARTIFACT_BYTES <= 64 * 1024 * 1024


# ── 压缩 ────────────────────────────────────────────────────────────────────


async def test_large_artifact_is_compressed_and_reads_back_identically(tmp_path):
    store = ArtifactStore(tmp_path)
    repetitive = "the same log line\n" * (COMPRESS_ABOVE_BYTES // 10)
    payload = {"tool": "run_tests", "status": "failed", "output": repetitive}
    key, meta = store.write("t1", "c1", payload)

    assert meta["compressed"] is True
    assert key == "t1/c1.json.gz"
    # 压缩只影响磁盘：读回来必须逐字一致。
    assert store.load("t1", "c1") == payload
    assert (tmp_path / key).stat().st_size < len(repetitive.encode("utf-8"))


async def test_legacy_plain_json_still_reads(tmp_path):
    """老版本写下的 `.json` 不会被升级弄丢——读侧先找 .gz 再退回 .json。"""
    legacy = tmp_path / "t1" / "c1.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"output": "old"}), encoding="utf-8")

    assert ArtifactStore(tmp_path).load("t1", "c1") == {"output": "old"}


async def test_corrupt_gzip_is_reported_not_silently_empty(tmp_path):
    broken = tmp_path / "t1" / "c1.json.gz"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(gzip.compress(b'{"output": "x"}')[:-4])

    with pytest.raises((ValueError, OSError)):
        ArtifactStore(tmp_path).load("t1", "c1")


async def test_resaving_same_id_drops_the_stale_copy(tmp_path):
    """同一个 id 换后缀重写时，旧那份要删掉，否则占用统计会翻倍。"""
    store = ArtifactStore(tmp_path)
    store.write("t1", "c1", {"output": "tiny"})
    store.write("t1", "c1", {"output": "x" * (COMPRESS_ABOVE_BYTES * 2)})

    names = sorted(item.path.name for item in store.iter_files())
    assert names == ["c1.json.gz"]


# ── 统计 ────────────────────────────────────────────────────────────────────


async def test_stats_counts_only_real_artifacts(tmp_path):
    store = ArtifactStore(tmp_path)
    store.write("t1", "c1", {"output": "a"})
    store.write("t1", "c2", {"output": "b" * (COMPRESS_ABOVE_BYTES * 2)})
    store.write("t2", "c3", {"output": "c"})
    # 临时文件不是产物：写一半的中间态被算进占用会让人以为磁盘用在了正事上。
    (tmp_path / "t1" / "c9.json.tmp").write_text("{}", encoding="utf-8")

    stats = store.stats()
    assert stats["files"] == 3
    assert stats["tasks"] == 2
    assert stats["compressed_files"] == 1
    assert stats["bytes"] >= stats["compressed_bytes"] > 0


# ── 清理：引用保护 ──────────────────────────────────────────────────────────


async def test_purge_never_deletes_a_referenced_artifact(tmp_path):
    store = ArtifactStore(tmp_path)
    referenced_key = store.save("t-ref", "c-ref", {"output": "still needed"})
    orphan_key = store.save("t-old", "c-old", {"output": "nobody points here"})
    await _seed_call(referenced_key)
    referenced_path = tmp_path / referenced_key
    orphan_path = tmp_path / orphan_key
    _age(referenced_path, days=90)
    _age(orphan_path, days=90)

    report = await maintenance.purge_artifacts(
        get_session_factory(), ttl_days=30, dry_run=False, root=tmp_path
    )

    assert referenced_path.exists(), "仍被 ToolCall 引用的产物不许删"
    assert not orphan_path.exists(), "过期且无人引用的产物该删"
    assert report["deleted"] == 1
    assert report["referenced_kept"] == 1


async def test_purge_dry_run_changes_nothing(tmp_path):
    store = ArtifactStore(tmp_path)
    orphan = tmp_path / store.save("t-old", "c-old", {"output": "x"})
    _age(orphan, days=90)

    report = await maintenance.purge_artifacts(
        get_session_factory(), ttl_days=30, dry_run=True, root=tmp_path
    )

    assert orphan.exists()
    assert report["would_free_bytes"] > 0
    assert report["deleted"] == 0
    assert report["freed_bytes"] == 0


async def test_fresh_artifact_is_kept_even_without_reference(tmp_path):
    """TTL 没到就一律不动：引用检查管"能不能删"，TTL 管"该不该删"，两条都得满足。"""
    store = ArtifactStore(tmp_path)
    fresh = tmp_path / store.save("t-new", "c-new", {"output": "x"})

    report = await maintenance.purge_artifacts(
        get_session_factory(), ttl_days=30, dry_run=False, root=tmp_path
    )

    assert fresh.exists()
    assert report["candidates"] == 0


async def test_purge_keeps_artifact_for_active_task(tmp_path):
    """清理任务不能把仍在执行中的任务产物当成孤儿删掉。"""
    store = ArtifactStore(tmp_path)
    artifact_key = store.save("t-active", "c-active", {"output": "still running"})
    artifact = tmp_path / artifact_key
    _age(artifact, days=90)

    async with get_session_factory()() as session:
        session.add(Project(id="p-active", name="active"))
        session.add(
            Task(
                id="t-active",
                project_id="p-active",
                title="active task",
                goal="keep working",
                status="running",
            )
        )
        await session.commit()

    report = await maintenance.purge_artifacts(
        get_session_factory(), ttl_days=30, dry_run=False, root=tmp_path
    )

    assert artifact.exists()
    assert report["candidates"] == 0
    assert report["deleted"] == 0


async def test_artifact_report_separates_referenced_expired(tmp_path):
    store = ArtifactStore(tmp_path)
    referenced = store.save("t-ref", "c-ref", {"output": "keep"})
    await _seed_call(referenced)
    _age(tmp_path / referenced, days=90)

    report = await maintenance.artifact_report(
        get_session_factory(), root=tmp_path, ttl_days=30
    )

    assert report["expired_files"] == 1
    # 过期但被引用 = "该涨保留期"，不是"该删"。
    assert report["expired_but_referenced"] == 1
    assert report["reclaimable_files"] == 0


async def test_purge_tolerates_empty_root(tmp_path):
    report = await maintenance.purge_artifacts(
        get_session_factory(), ttl_days=30, dry_run=False, root=tmp_path / "missing"
    )
    assert report["scanned"] == 0
    assert report["deleted"] == 0


# ── 快照 ────────────────────────────────────────────────────────────────────


def _make_snapshot(root: Path, task_id: str, created_at: float, payload: str = "x") -> str:
    snapshot_id = f"{task_id}-{uuid.uuid4().hex[:6]}"
    tree = root / snapshot_id / "tree"
    tree.mkdir(parents=True)
    (tree / "a.txt").write_text(payload * 100, encoding="utf-8")
    (root / snapshot_id / "meta.json").write_text(
        json.dumps(
            {"id": snapshot_id, "task_id": task_id, "created_at": created_at, "files": ["a.txt"]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return snapshot_id


def test_snapshot_purge_keeps_the_most_recent_per_task(tmp_path):
    old = time.time() - 90 * 86400
    ids = [_make_snapshot(tmp_path, "t1", old + index) for index in range(5)]

    report = maintenance.purge_snapshots(
        root=tmp_path, keep_per_task=2, ttl_days=30, dry_run=False
    )

    assert report["candidates"] == 3
    survivors = {item.name for item in tmp_path.iterdir() if item.is_dir()}
    # 每个任务最近两份无条件保留：删光等于让用户"改坏了回不去"。
    assert survivors == set(ids[-2:])


def test_snapshot_purge_dry_run_keeps_everything(tmp_path):
    old = time.time() - 90 * 86400
    ids = [_make_snapshot(tmp_path, "t1", old + index) for index in range(3)]

    report = maintenance.purge_snapshots(root=tmp_path, keep_per_task=1, ttl_days=30)

    assert report["dry_run"] is True
    assert report["deleted"] == 0
    assert {item.name for item in tmp_path.iterdir()} == set(ids)


def test_snapshot_purge_keeps_active_task_history(tmp_path):
    old = time.time() - 90 * 86400
    ids = [_make_snapshot(tmp_path, "t-active", old + index) for index in range(4)]

    report = maintenance.purge_snapshots(
        root=tmp_path,
        keep_per_task=1,
        ttl_days=30,
        dry_run=False,
        protected_task_ids={"t-active"},
    )

    assert report["candidates"] == 0
    assert report["deleted"] == 0
    assert {item.name for item in tmp_path.iterdir()} == set(ids)


def test_orphan_snapshot_purge_requires_explicit_task_list(tmp_path):
    _make_snapshot(tmp_path, "t1", time.time() - 86400)
    # 默认空集合会等于"所有任务都不在了"——这种默认值是事故，必须显式给。
    with pytest.raises(ValueError):
        maintenance.purge_orphan_snapshots(root=tmp_path, dry_run=False)

    report = maintenance.purge_orphan_snapshots(root=tmp_path, alive_task_ids={"t2"}, dry_run=False)
    assert report["orphans"] == 1
    assert report["deleted"] == 1


# ── 孤儿行 ──────────────────────────────────────────────────────────────────


async def test_orphan_rows_are_counted_and_purged():
    task_id, _ = await _seed_call("")
    async with get_session_factory()() as session:
        # 造一条指向不存在任务的事件：任务删除路径漏下的形状。
        from blank_api.models.task import AgentEvent

        session.add(
            AgentEvent(id=str(uuid.uuid4()), task_id="t-gone", seq=1, type="task.created", payload={})
        )
        await session.commit()

    report = await maintenance.orphan_report(get_session_factory())
    assert report["orphan_events"] == 1

    dry = await maintenance.purge_orphans(get_session_factory(), dry_run=True)
    assert dry["deleted"]["events"] == 0

    real = await maintenance.purge_orphans(get_session_factory(), dry_run=False)
    assert real["deleted"]["events"] == 1
    async with get_session_factory()() as session:
        left = (await session.execute(select(AgentEvent.task_id))).scalars().all()
    assert "t-gone" not in left
    # 活任务本身一条都不许动：清的是"没有人指向的行"，不是"看着旧的行"。
    async with get_session_factory()() as session:
        alive = (await session.execute(select(Task.id))).scalars().all()
    assert task_id in alive

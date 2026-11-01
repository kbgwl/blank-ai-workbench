"""磁盘与数据库的生命周期：算清占用，按引用关系安全清理。

四类数据会随着使用无限增长：**artifact**（工具完整输出）、**snapshot**（工作区副本）、
**event**（时间线）、**usage**（模型账单）。它们不该各自长出一套清理逻辑，更不该
由某个业务路径顺手删——所以全部收在这一个模块里，规则也只有一条：

> **只删"没人引用"的东西。** 判断依据是数据库里的外键关系，不是文件时间。
> 一次清理出错的最坏结果不是"没删干净"，而是"把还能用的回滚材料删了"。

第二道闸是默认值：所有清理**默认 dry-run**（`dry_run=True`），只报"会删什么"，
要真删必须显式传 `dry_run=False`。这个默认不是保守，是必要的——这些文件里躺着
用户排障时要用的东西，自动删掉之后"为什么这条任务打不开"就再也查不出来了。

第三道闸是快照的「每任务保留最近 N 份」：快照是回滚材料，删光等于把用户按在
"改坏了也回不去"的位置上。保留份数按任务计，不是全局按时间——一个昨天跑完的
任务，它的最后一份快照仍然有用。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from blank_api.models.task import AgentEvent, FileChange, Task, ToolCall
from blank_api.services import metrics
from blank_api.services.agent.artifacts import ArtifactStore
from blank_api.services.sandbox.snapshot import SnapshotStore

# 快照每个任务至少留这么多份：删到 1 份以后，"改坏了"就只剩一个可能已经被覆盖的状态。
DEFAULT_KEEP_SNAPSHOTS_PER_TASK = 3
# artifact 的默认保留期。工具输出是排障材料，不是归档——30 天足够回看一次事故。
DEFAULT_ARTIFACT_TTL_DAYS = 30
# 事件的默认保留期：超过这个天数的**终态**任务，其事件 payload 会被裁短（行不删）。
DEFAULT_EVENT_RETENTION_DAYS = 90
DEFAULT_EVENT_VALUE_LIMIT = 400
DEFAULT_EVENT_BLOB_LIMIT = 2000
# 只压缩这三种状态之后的事件。`running` / `queued` / `waiting_approval` / `paused`
# 全是"还能继续跑"的状态，压缩它们的 payload 会直接把崩溃续跑所需的执行日志裁短。
COMPACTABLE_TASK_STATUSES = ("succeeded", "failed", "cancelled")
# 运行中的任务不能被维护动作当作孤儿或过期材料处理；它们可能还要续跑、回滚或
# 从 Artifact 重放工具结果。
ACTIVE_TASK_STATUSES = ("queued", "running", "waiting_approval", "paused")
# 这几个键永远原样保留：它们是**身份**（哪条调用、哪个 trace、产物在哪），
# 而不是内容，本身也短。压掉它们会让重放认不出这条调用（见 graph._replayed_tool_results）。
COMPACT_KEEP_KEYS = frozenset(
    {
        "trace_id",
        "call_id",
        "tool_call_id",
        "artifact_path",
        "run_id",
        "role",
        "status",
        "code",
        "name",
    }
)


def _cutoff(days: float) -> float:
    return time.time() - max(0.0, float(days)) * 86400


def artifact_key(value: str) -> str:
    """把任意形状的 ``artifact_path`` 归一成「任务/文件」这个身份。

    库里可能存在三种写法：``<task>/<call>.json``（现在）、
    ``data/artifacts/<task>/<call>.json``（早期相对路径）、
    ``D:/Softwares/kbg/data/artifacts/<task>/<call>.json``（把产物根指到别处之后）。
    三者指的是同一份文件，而清理必须认出这种同一性——认不出的代价是把**仍被引用**
    的文件删掉。所以这里只取最后两段，并去掉 ``.gz``（压缩与否不改变身份）。
    """
    parts = [part for part in str(value or "").replace("\\", "/").split("/") if part not in ("", ".")]
    if len(parts) < 2:
        return ""
    name = parts[-1][:-3] if parts[-1].endswith(".gz") else parts[-1]
    return f"{parts[-2]}/{name}".lower()


def _normalize(path: str) -> str:
    """磁盘路径的引用键：同样只认最后两段。"""
    return artifact_key(path)


async def referenced_artifacts(
    session_factory: async_sessionmaker[AsyncSession],
) -> set[str]:
    """数据库里仍被引用的 artifact 身份集合（``<task>/<name>``）。

    引用只有一处：``ToolCall.artifact_path``。一条 ToolCall 行在，就说明"这次工具
    调用有完整输出可读"，崩溃续跑（``_replayed_tool_results``）、证据页、
    ``GET /tasks/{id}/artifacts/{id}`` 三条读路径都靠它。行没了才轮到文件。
    """
    async with session_factory() as session:
        rows = (await session.execute(select(ToolCall.artifact_path))).scalars().all()
    return {key for key in (artifact_key(row) for row in rows) if key}


async def artifact_report(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    root: Path | str | None = None,
    ttl_days: float = DEFAULT_ARTIFACT_TTL_DAYS,
) -> dict:
    """artifact 的占用与可清理量。只读，不改任何文件。"""
    store = ArtifactStore(root) if root is not None else ArtifactStore()
    stats = store.stats()
    referenced = await referenced_artifacts(session_factory)
    async with session_factory() as session:
        active_task_ids = {
            str(task_id)
            for task_id in (
                await session.execute(
                    select(Task.id).where(Task.status.in_(ACTIVE_TASK_STATUSES))
                )
            ).scalars().all()
        }
    files = store.iter_files()
    cutoff = _cutoff(ttl_days)
    expired = [item for item in files if item.mtime < cutoff]
    orphans = [
        item
        for item in expired
        if item.task_id not in active_task_ids
        and _normalize(f"{item.task_id}/{item.path.name}") not in referenced
    ]
    return {
        **stats,
        "ttl_days": ttl_days,
        "expired_files": len(expired),
        "expired_bytes": sum(item.size for item in expired),
        # 过期但**仍被引用**的数量单独报：它说明"该涨的保留期"，不是"该删的文件"。
        "expired_but_referenced": len(expired) - len(orphans),
        "reclaimable_files": len(orphans),
        "reclaimable_bytes": sum(item.size for item in orphans),
    }


async def purge_artifacts(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ttl_days: float = DEFAULT_ARTIFACT_TTL_DAYS,
    dry_run: bool = True,
    root: Path | str | None = None,
) -> dict:
    """删掉过期且**无人引用**的 artifact。

    两条同时满足才删：文件太旧（TTL）**且**没有任何 ToolCall 指向它。只满足一条
    都不删——旧文件可能正是用户正在回看的那条任务，被引用的文件删了就直接断链。
    """
    store = ArtifactStore(root) if root is not None else ArtifactStore()
    referenced = await referenced_artifacts(session_factory)
    async with session_factory() as session:
        active_task_ids = {
            str(task_id)
            for task_id in (
                await session.execute(
                    select(Task.id).where(Task.status.in_(ACTIVE_TASK_STATUSES))
                )
            ).scalars().all()
        }
    cutoff = _cutoff(ttl_days)
    expired = [item for item in store.iter_files() if item.mtime < cutoff]
    candidates = [
        item
        for item in expired
        if item.task_id not in active_task_ids
        and _normalize(f"{item.task_id}/{item.path.name}") not in referenced
    ]
    freed = sum(item.size for item in candidates)
    deleted: list[str] = []
    failed: list[dict] = []
    if not dry_run:
        for item in candidates:
            try:
                item.path.unlink()
                deleted.append(item.path.as_posix())
            except OSError as error:
                failed.append({"path": item.path.as_posix(), "error": str(error)})
        if deleted:
            metrics.inc(metrics.ARTIFACT_PURGED, len(deleted))
        # 删空的任务目录一起收掉，免得根目录下留一堆空壳。
        for directory in {item.path.parent for item in candidates}:
            try:
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
            except OSError:
                pass
    return {
        "dry_run": dry_run,
        "ttl_days": ttl_days,
        "scanned": len(store.iter_files()),
        "referenced_kept": len(expired) - len(candidates),
        "candidates": len(candidates),
        "deleted": len(deleted) if not dry_run else 0,
        "freed_bytes": freed if not dry_run else 0,
        "would_free_bytes": freed,
        "failed": failed,
    }


def snapshot_report(
    *,
    root: Path | str | None = None,
    keep_per_task: int = DEFAULT_KEEP_SNAPSHOTS_PER_TASK,
    ttl_days: float = 30,
    protected_task_ids: set[str] | None = None,
) -> dict:
    """快照占用与可清理量。``keep_per_task`` 是每个任务无条件保留的最近份数。"""
    store = SnapshotStore(root) if root is not None else SnapshotStore()
    records = store.list()
    cutoff = _cutoff(ttl_days)
    by_task: dict[str, list[dict]] = {}
    for record in records:
        by_task.setdefault(str(record.get("task_id") or ""), []).append(record)
    candidates: list[dict] = []
    protected = {str(task_id) for task_id in (protected_task_ids or set())}
    for task_id, group in by_task.items():
        if task_id in protected:
            continue
        group.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
        for index, record in enumerate(group):
            if index < max(0, keep_per_task):
                continue
            if float(record.get("created_at") or 0) >= cutoff:
                continue
            candidates.append(record)
    sizes = {str(record.get("id")): _dir_size(store.root / str(record.get("id"))) for record in candidates}
    return {
        "root": store.root.as_posix(),
        "snapshots": len(records),
        "tasks": len([task for task in by_task if task]),
        "bytes": sum(_dir_size(store.root / str(record.get("id"))) for record in records),
        "keep_per_task": keep_per_task,
        "ttl_days": ttl_days,
        "candidates": len(candidates),
        "reclaimable_bytes": sum(sizes.values()),
        "candidate_ids": [str(record.get("id")) for record in candidates],
    }


def purge_snapshots(
    *,
    root: Path | str | None = None,
    keep_per_task: int = DEFAULT_KEEP_SNAPSHOTS_PER_TASK,
    ttl_days: float = 30,
    dry_run: bool = True,
    protected_task_ids: set[str] | None = None,
) -> dict:
    """删掉超出保留份数且过期的快照目录。

    只动 ``keep_per_task`` 之外的份：每个任务最近那几份快照是"改坏了还能回去"的
    唯一凭据，任何策略都不该碰。被引用这件事在这里体现为"任务还在不在"——
    任务被删时它的快照由 ``purge_orphan_snapshots`` 收，不在这里猜。
    """
    import shutil

    report = snapshot_report(
        root=root,
        keep_per_task=keep_per_task,
        ttl_days=ttl_days,
        protected_task_ids=protected_task_ids,
    )
    store = SnapshotStore(root) if root is not None else SnapshotStore()
    deleted = 0
    if not dry_run:
        for snapshot_id in report["candidate_ids"]:
            try:
                shutil.rmtree(store.root / snapshot_id, ignore_errors=False)
                deleted += 1
            except OSError:
                continue
    return {
        **report,
        "dry_run": dry_run,
        "deleted": deleted,
        "freed_bytes": report["reclaimable_bytes"] if not dry_run else 0,
    }


def purge_orphan_snapshots(
    *, alive_task_ids: set[str] | None = None, root: Path | str | None = None, dry_run: bool = True
) -> dict:
    """删掉 ``task_id`` 已不存在的快照。任务被删后它留下的树没有任何读路径。

    ``alive_task_ids`` 必须由调用方显式给出。这个参数刻意没有默认值：默认成空集合
    等于"所有任务都不在了"，一次误调用就会把全部回滚材料删光——这类默认值不是
    方便，是事故。
    """
    import shutil

    if alive_task_ids is None:
        raise ValueError("purge_orphan_snapshots 需要 alive_task_ids（现存任务 id 集合）")
    store = SnapshotStore(root) if root is not None else SnapshotStore()
    alive = {str(item) for item in alive_task_ids}
    records = store.list()
    orphans = [record for record in records if str(record.get("task_id") or "") not in alive]
    freed = sum(_dir_size(store.root / str(record.get("id"))) for record in orphans)
    deleted = 0
    if not dry_run:
        for record in orphans:
            try:
                shutil.rmtree(store.root / str(record.get("id")), ignore_errors=False)
                deleted += 1
            except OSError:
                continue
    return {
        "dry_run": dry_run,
        "snapshots": len(records),
        "orphans": len(orphans),
        "deleted": deleted,
        "freed_bytes": freed if not dry_run else 0,
        "would_free_bytes": freed,
    }


async def orphan_report(session_factory: async_sessionmaker[AsyncSession]) -> dict:
    """没有任何任务行指向的事件/工具行/改动行/用量行有多少条。

    任务删除路径（``DELETE /tasks/{id}``、删项目级联）已经按 task_id 清过一遍，
    这里量的是**历史遗留**：早期版本删任务时漏下的行，或迁移过程中断留下的半截。
    它们不会自己消失，也不会被任何读路径看到——只占库。
    """
    async with session_factory() as session:
        alive = set((await session.execute(select(Task.id))).scalars().all())
        events = (await session.execute(select(AgentEvent.task_id))).scalars().all()
        calls = (await session.execute(select(ToolCall.task_id))).scalars().all()
        changes = (await session.execute(select(FileChange.task_id))).scalars().all()
    return {
        "tasks": len(alive),
        "orphan_events": sum(1 for task_id in events if task_id not in alive),
        "orphan_tool_calls": sum(1 for task_id in calls if task_id not in alive),
        "orphan_file_changes": sum(1 for task_id in changes if task_id not in alive),
    }


async def purge_orphans(
    session_factory: async_sessionmaker[AsyncSession], *, dry_run: bool = True, commit: bool = True
) -> dict:
    """删掉上一条报出来的孤儿行。默认 dry-run。"""
    async with session_factory() as session:
        alive = set((await session.execute(select(Task.id))).scalars().all())
    counts = await orphan_report(session_factory)
    deleted = {"events": 0, "tool_calls": 0, "file_changes": 0}
    if not dry_run:
        async with session_factory() as session:
            for model, key in (
                (AgentEvent, "events"),
                (ToolCall, "tool_calls"),
                (FileChange, "file_changes"),
            ):
                # 用 NOT IN 子查询而不是把 id 全拉回内存：生产库里任务数可能上万。
                result = await session.execute(
                    delete(model).where(model.task_id.notin_(select(Task.id)))
                )
                deleted[key] = int(result.rowcount or 0)
            if commit:
                await session.commit()
    return {"dry_run": dry_run, **counts, "deleted": deleted}


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


async def storage_report(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    artifact_root: Path | str | None = None,
    snapshot_root: Path | str | None = None,
    ttl_days: float = DEFAULT_ARTIFACT_TTL_DAYS,
    event_retention_days: float = DEFAULT_EVENT_RETENTION_DAYS,
) -> dict[str, Any]:
    """一张表回答"这个产品在我机器上占了多少"。设置页与排障脚本读同一个数。"""
    artifacts = await artifact_report(
        session_factory, root=artifact_root, ttl_days=ttl_days
    )
    snapshots = snapshot_report(root=snapshot_root)
    orphans = await orphan_report(session_factory)
    events = await event_report(session_factory, retention_days=event_retention_days)
    usage = await usage_report(session_factory)
    return {
        "artifacts": artifacts,
        "snapshots": snapshots,
        "database": orphans,
        "events": events,
        "usage": usage,
        "total_bytes": int(artifacts.get("bytes") or 0) + int(snapshots.get("bytes") or 0),
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "cutoff_days": ttl_days,
    }


def iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


# ── 事件与用量：只瘦身，不删行 ────────────────────────────────────────────────
#
# 为什么这两类不能像 artifact 那样"过期就删"：
#
# * **事件**是时间线本身。一条任务的事件删掉一行，界面上那一步就凭空消失，
#   而"那一步到底做了什么"正是排障时要看的东西。所以对旧任务做的是**瘦身**：
#   事件行、类型、序号、时间全部保留，只把 payload 里的大字符串（命令输出、
#   文件正文、diff 正文）裁成头一段并留明确标记。全文本来就在 artifact 里，
#   这里裁掉的不是证据，是重复。
# * **用量是账本**。`model_usage` 每一行是"这一次调用花了多少 token"的历史事实，
#   删行等于改账（`provider_id` 悬空都特意不做外键，见 models/model_provider.py）。
#   所以这里只出读数：增长多快、最老一行是哪天、哪一档在膨胀。要不要归档、
#   归档到哪，是运维决定，不是这个模块替用户决定。
#
# 两道闸与 artifact 一致：只碰**终态任务**（跑着的、等审批的、暂停的都还能续跑，
# 它们的 payload 是重放材料），并且默认 dry-run。
# 常量（保留期、裁剪阈值、可压缩状态、必须保留的键）统一定义在文件顶部，
# 因为它们也被 ``storage_report`` 的默认参数用到——定义在用它的函数之后会在导入时就炸。


def _payload_bytes(payload: Any) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return 0


# 裁过的值带这个尾巴。它同时是一个**幂等标记**：第二次清理要认得出"这条已经裁过"，
# 否则每跑一次都会再裁一遍、再记一次省下的字节数——账面越跑越好看，实际一个字没省。
_COMPACTED_SUFFIX = re.compile(r"…〈已压缩：原 \d+ 字〉$")


def _already_compacted(value: str) -> bool:
    return bool(_COMPACTED_SUFFIX.search(value))


def compact_payload(
    payload: Any,
    *,
    value_limit: int = DEFAULT_EVENT_VALUE_LIMIT,
    blob_limit: int = DEFAULT_EVENT_BLOB_LIMIT,
) -> tuple[Any, bool]:
    """把一条事件 payload 里的大值裁短，**键名一个都不删**。

    返回 ``(新载荷, 是否改动过)``。裁过的值都带 ``〈已压缩：原 N 字〉`` 标记，
    界面/证据包里能一眼看出"这里被裁过、原文在哪"，而不是看起来本来就是空的。
    """
    if not isinstance(payload, dict):
        return payload, False
    changed = False
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key in COMPACT_KEEP_KEYS:
            out[key] = value
            continue
        if isinstance(value, str):
            if len(value) > value_limit and not _already_compacted(value):
                out[key] = f"{value[:value_limit]}…〈已压缩：原 {len(value)} 字〉"
                changed = True
            else:
                out[key] = value
            continue
        if isinstance(value, (dict, list)):
            size = _payload_bytes(value)
            if size > blob_limit:
                out[key] = {"compacted": True, "bytes": size}
                changed = True
            else:
                out[key] = value
            continue
        out[key] = value
    return out, changed


async def event_report(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    retention_days: float = DEFAULT_EVENT_RETENTION_DAYS,
    value_limit: int = DEFAULT_EVENT_VALUE_LIMIT,
    blob_limit: int = DEFAULT_EVENT_BLOB_LIMIT,
    sample_limit: int = 2000,
) -> dict:
    """事件表的增长读数：多少条、多大、哪些已经超期且可瘦身。只读。

    ``sample_limit`` 是为了不把整张表拉进内存：读数按最多这么多条抽样统计
    payload 体积与可压缩量，样本不够时把 ``sampled`` 标出来，别把估算读成实测。
    """
    from sqlalchemy import func

    async with session_factory() as session:
        total = int((await session.execute(select(func.count()).select_from(AgentEvent))).scalars().one() or 0)
        oldest = (await session.execute(select(func.min(AgentEvent.created_at)))).scalars().one()
        cutoff_dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max(0.0, float(retention_days)))
        stale_ids = (
            await session.execute(
                select(Task.id).where(
                    Task.status.in_(COMPACTABLE_TASK_STATUSES), Task.updated_at < cutoff_dt
                )
            )
        ).scalars().all()
        stale = []
        if stale_ids:
            stale = (
                await session.execute(
                    select(AgentEvent)
                    .where(AgentEvent.task_id.in_(stale_ids))
                    .order_by(AgentEvent.seq)
                    .limit(sample_limit)
                )
            ).scalars().all()
    before = 0
    after = 0
    compactable = 0
    for event in stale:
        raw = event.payload
        compacted, changed = compact_payload(raw, value_limit=value_limit, blob_limit=blob_limit)
        before += _payload_bytes(raw)
        after += _payload_bytes(compacted)
        compactable += 1 if changed else 0
    return {
        "events": total,
        "oldest": oldest.isoformat() if oldest else None,
        "retention_days": retention_days,
        "stale_tasks": len(stale_ids),
        "sampled_events": len(stale),
        "sampled_bytes": before,
        "sample_truncated": len(stale) >= sample_limit,
        "compactable_events": compactable,
        "reclaimable_bytes": max(0, before - after),
        "value_limit": value_limit,
        "blob_limit": blob_limit,
    }


async def compact_events(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    retention_days: float = DEFAULT_EVENT_RETENTION_DAYS,
    value_limit: int = DEFAULT_EVENT_VALUE_LIMIT,
    blob_limit: int = DEFAULT_EVENT_BLOB_LIMIT,
    dry_run: bool = True,
    batch: int = 200,
    commit: bool = True,
) -> dict:
    """把终态任务里超期事件的 payload 裁短。行不删、类型不删、序号不变。"""
    from sqlalchemy import update

    report = await event_report(
        session_factory,
        retention_days=retention_days,
        value_limit=value_limit,
        blob_limit=blob_limit,
    )
    cutoff_dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max(0.0, float(retention_days)))
    updated = 0
    freed = 0
    if not dry_run:
        async with session_factory() as session:
            stale_ids = (
                await session.execute(
                    select(Task.id).where(
                        Task.status.in_(COMPACTABLE_TASK_STATUSES), Task.updated_at < cutoff_dt
                    )
                )
            ).scalars().all()
            if stale_ids:
                rows = (
                    await session.execute(
                        select(AgentEvent.id, AgentEvent.payload).where(
                            AgentEvent.task_id.in_(stale_ids)
                        )
                    )
                ).all()
                pending = 0
                for event_id, payload in rows:
                    compacted, changed = compact_payload(
                        payload, value_limit=value_limit, blob_limit=blob_limit
                    )
                    if not changed:
                        continue
                    freed += max(0, _payload_bytes(payload) - _payload_bytes(compacted))
                    await session.execute(
                        update(AgentEvent).where(AgentEvent.id == event_id).values(payload=compacted)
                    )
                    updated += 1
                    pending += 1
                    if pending >= batch:
                        await session.flush()
                        pending = 0
            if commit:
                await session.commit()
    return {
        **report,
        "dry_run": dry_run,
        "compacted": updated,
        "freed_bytes": freed if not dry_run else 0,
        "would_free_bytes": report["reclaimable_bytes"],
    }


async def usage_report(session_factory: async_sessionmaker[AsyncSession]) -> dict:
    """用量账本的增长读数。**只读**：账本不自动删行，删多少由运维决定。"""
    from sqlalchemy import func

    from blank_api.models.model_provider import ModelUsage

    async with session_factory() as session:
        total = int((await session.execute(select(func.count()).select_from(ModelUsage))).scalars().one() or 0)
        oldest = (await session.execute(select(func.min(ModelUsage.created_at)))).scalars().one()
        newest = (await session.execute(select(func.max(ModelUsage.created_at)))).scalars().one()
        tokens = (
            await session.execute(
                select(
                    func.coalesce(func.sum(ModelUsage.prompt_tokens), 0),
                    func.coalesce(func.sum(ModelUsage.completion_tokens), 0),
                )
            )
        ).one()
        by_model = (
            await session.execute(
                select(ModelUsage.model, func.count())
                .group_by(ModelUsage.model)
                .order_by(func.count().desc())
                .limit(8)
            )
        ).all()
    days = 0.0
    if oldest and newest:
        days = max(0.0, (newest - oldest).total_seconds() / 86400)
    return {
        "rows": total,
        "oldest": oldest.isoformat() if oldest else None,
        "newest": newest.isoformat() if newest else None,
        "span_days": round(days, 1),
        "rows_per_day": round(total / days, 2) if days > 0 else None,
        "prompt_tokens": int(tokens[0] or 0),
        "completion_tokens": int(tokens[1] or 0),
        "top_models": [{"model": str(model), "rows": int(count)} for model, count in by_model],
        # 这句话要跟着读数一起出现，否则下一个人会以为"没删"是漏了。
        "policy": "账本不自动清理：行即历史事实，删除等于改账；需要归档时按 provider/model 自行导出后处理",
    }


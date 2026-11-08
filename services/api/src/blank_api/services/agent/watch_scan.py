"""让「盯着 X，一有变化就告诉我」真的有东西在盯。

现状（v0.10.x）：「盯」被判定成 ``intent=watch``，登记成一条 ``pending`` 触发器，
然后没有任何东西会唤醒它——``triggers.due()`` 只按 ``fire_at`` 扫时间，而
``workspace_changed`` 这个事件类型在整个仓库里**没有任何生产者**。于是监控页上
那条记录永远停在「等外部事件」，直到 ``expires_at`` 把它作废。

这里补上那个生产者，口径只有一条：**登记之后，工作区里真的有文件变了。**

为什么用 ``mtime > watch_since`` 而不是"全量指纹对比"：
- 指纹对比要先有一份基线，而基线只能在登记之后建立——建立之前发生的变化会被
  吸收进基线，等于漏掉用户最关心的"我刚一交代就改的那一下"。
- ``since`` 是登记时刻，语义就是"从现在起"。少一层状态，也少一种对不上的可能。

代价是"改完又改回去"看不出来（mtime 仍然变过，会触发）——这恰好是想要的：
内容回到原样也是一次变化，值得说一句。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.project import Workspace
from blank_api.models.trigger import AgentTrigger
from blank_api.services.agent import triggers as triggers_module

log = logging.getLogger(__name__)

# 依赖目录与构建产物不是"工作区变化"：一次 npm install 就能造出上万条 mtime，
# 报出来全是噪音，而且把真正的改动淹掉。
IGNORED_DIRS = {
    "node_modules", ".git", ".svn", ".hg", "__pycache__", ".venv", "venv", "env",
    ".next", "dist", "build", "out", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".turbo", ".idea", ".vscode", "coverage", ".cache", "target", "bin", "obj",
}
IGNORED_SUFFIXES = (
    ".pyc", ".pyo", ".log", ".tmp", ".swp", ".swo", ".lock", ".DS_Store",
    ".tsbuildinfo", ".map",
)
MAX_DEPTH = 4
MAX_ENTRIES = 4000
MAX_REPORT_FILES = 5
# 「盯着」比"定时"更需要活着的窗口：定时是到点就结束，盯着可能要等好几天。
WATCH_WINDOW = timedelta(days=7)


def _epoch(moment: datetime) -> float:
    """库里的 UTC naive → epoch 秒（与 ``st_mtime`` 同一把尺）。"""
    return moment.replace(tzinfo=timezone.utc).timestamp()


def changed_files(
    root: str | Path,
    since: datetime,
    *,
    limit: int = MAX_REPORT_FILES,
    max_entries: int = MAX_ENTRIES,
) -> list[str]:
    """工作区里在 ``since`` 之后被改过的文件（相对路径，最多 ``limit`` 条）。

    扫不动（目录不存在、权限不足）就当"没有变化"：一条盯不上的监控不该
    把整轮巡检带走。
    """
    base = Path(root)
    if not base.is_dir():
        return []
    since_ts = _epoch(since)
    found: list[str] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(base):
        depth = 0 if dirpath == str(base) else os.path.relpath(dirpath, base).count(os.sep) + 1
        if depth >= MAX_DEPTH:
            dirnames[:] = []
        else:
            dirnames[:] = [name for name in dirnames if name.lower() not in IGNORED_DIRS and not name.startswith(".")]
        for name in filenames:
            if name.startswith(".") or name.lower().endswith(IGNORED_SUFFIXES):
                continue
            path = os.path.join(dirpath, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            scanned += 1
            if scanned > max_entries:
                return found
            if stat.st_mtime > since_ts:
                try:
                    found.append(os.path.relpath(path, base).replace("\\", "/"))
                except ValueError:
                    continue
                if len(found) >= limit:
                    return found
    return found


async def watch_once(*, now: datetime | None = None) -> int:
    """扫一轮所有 pending 的 watch 触发器，有文件变化就放行。返回放行条数。"""
    from blank_api.worker import release_one

    moment = now or triggers_module.utc_now()
    released = 0
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(AgentTrigger).where(
                    AgentTrigger.status == triggers_module.STATUS_PENDING,
                    AgentTrigger.fire_at.is_(None),
                )
            )
        ).scalars().all()
        targets = [
            row
            for row in rows
            if str((row.payload or {}).get("intent") or "") == "watch"
        ]
        if not targets:
            return 0

        from blank_api.models.task import Task

        for row in targets:
            body = dict(row.payload or {})
            since_raw = str(body.get("watch_since") or "")
            try:
                since = datetime.fromisoformat(since_raw.replace("Z", ""))
            except ValueError:
                # 没有登记时刻就无法判断"之后"：补一个，下一拍开始算。
                row.payload = {**body, "watch_since": moment.isoformat()}
                await session.commit()
                continue
            task = await session.get(Task, row.task_id) if row.task_id else None
            if task is None:
                continue
            # 优先用登记时固化的路径：之后用户切换活动目录，不该让这条盯着换地方。
            root = str(body.get("watch_root") or "").strip()
            if not root:
                workspace = (
                    await session.execute(
                        select(Workspace)
                        .where(Workspace.project_id == task.project_id)
                        .order_by(Workspace.is_active.desc(), Workspace.created_at.asc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                root = (workspace.path or "").strip() if workspace is not None else ""
            if not root:
                continue
            hits = changed_files(root, since)
            if not hits:
                continue
            row.payload = {
                **body,
                "watch_hit_at": moment.isoformat(),
                "changed": hits,
                "changed_count": len(hits),
            }
            await session.commit()
            try:
                if await release_one(row.id, start_runner=True):
                    released += 1
            except Exception:  # noqa: BLE001 - 单条放行失败不该带走整轮
                log.warning("watch 放行失败 trigger=%s", row.id, exc_info=True)
    return released

"""把 harness 自己写进工作区的运行时状态移出交付目录。

Claude Code 把 file-history / todos 放在 ``~/.claude``，项目里不留自己的账本；
我们却把改动备份（``.blank_backups/``）与自测脚本暂存（``.blank/scratchpad/``）
写在工作区根上。任务结束后用户翻交付目录，会看到二十几个不属于他的备份目录
（3D 玻璃题真机：交付目录 3 个页面文件 + 21 个 ``.blank_backups/<task>/*``）。

执行期间必须留在工作区：沙箱只允许写工作区，挪到外面会被边界拒绝。所以收尾时
由宿主进程（不受沙箱限制）整体外移——只移动、不删除，路径写进事件，可复查。

只处理 harness 自己建的目录：``.blank/harness``、``.blank/AGENTS.md``、
``.blank/output-styles`` 这类是用户的配置，一个字节都不碰。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path
from typing import Any

STATE_DIR_ENV = "BLANK_WORKSPACE_STATE_DIR"
DEFAULT_STATE_ROOT = Path("data/workspace-state")
BACKUP_DIRNAME = ".blank_backups"

# 只收「本工具链自己创建」的暂存目录；用户级的 .blank 配置不在名单里。
SCRATCH_DIRS = (".blank/scratchpad", ".blank/context/scratchpad")


def _safe_component(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "").strip()).strip(".-")
    return cleaned[:80] or "task"


def workspace_slug(root: Path) -> str:
    """工作区标识：目录名 + 绝对路径哈希。

    同一个目录名（都叫 ``ui-bench``）在不同项目下不能撞车；哈希只取 8 位，
    够区分且人眼可读。
    """
    try:
        resolved = str(Path(root).resolve())
    except OSError:
        resolved = str(root)
    digest = hashlib.sha256(resolved.encode("utf-8", "replace")).hexdigest()[:8]
    return f"{_safe_component(Path(root).name)}-{digest}"


def _prune_empty(directory: Path, stop: Path) -> None:
    """向上删掉空目录，到 stop 为止；stop 本身不删（工作区永远保留）。"""
    current = directory
    stop = Path(stop)
    while current != stop and current != current.parent:
        try:
            next(current.iterdir())
            return
        except StopIteration:
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent
        except OSError:
            return


def _unique_target(target: Path) -> Path:
    """目标已存在时换一个名字，不覆盖上一次的存档。"""
    if not target.exists():
        return target
    for index in range(2, 100):
        candidate = target.with_name(f"{target.name}-{index}")
        if not candidate.exists():
            return candidate
    return target


def externalize(
    root: str | Path,
    task_id: str,
    *,
    state_root: str | Path | None = None,
) -> dict[str, Any]:
    """把本任务的 harness 状态移出工作区，返回移动清单。

    幂等：第二次调用时源目录已不存在，返回空清单。任何一步失败只跳过该项，
    不让收尾因为清理失败而改判任务结论。
    """
    base = Path(root)
    if not base.is_dir():
        return {"moved": [], "root": ""}
    task = _safe_component(task_id)
    root_override = state_root or os.environ.get(STATE_DIR_ENV) or DEFAULT_STATE_ROOT
    dest_root = Path(root_override) / workspace_slug(base) / task
    moved: list[dict[str, str]] = []

    for rel in SCRATCH_DIRS:
        source = base / rel
        if not source.is_dir():
            continue
        target = _unique_target(dest_root / rel)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
        except OSError:
            continue
        moved.append({"path": rel, "to": str(target)})
        _prune_empty(source.parent, base)

    # 只挪本任务的备份子树：别的任务目录可能是用户还没看过的历史，
    # 不属于这一轮该动的东西。
    backups = base / BACKUP_DIRNAME
    task_backups = backups / task
    if task_backups.is_dir():
        target = _unique_target(dest_root / BACKUP_DIRNAME / task)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(task_backups), str(target))
        except OSError:
            pass
        else:
            moved.append({"path": f"{BACKUP_DIRNAME}/{task}", "to": str(target)})
            _prune_empty(backups, base)

    return {"moved": moved, "root": str(dest_root) if moved else ""}

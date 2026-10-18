"""工作区快照：覆盖或删除前留一份可恢复副本。"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path

from blank_api.services.workspace import TREE_IGNORE, ignored_name

DEFAULT_SNAPSHOT_ROOT = Path("data/snapshots")

# 单次快照的上限：超过就不做整树复制。
# 起因：工作区指到本仓库时，`desktop/dist-*` 里躺着整份 Python 运行时，几万个文件，
# copytree 撞上 Windows 的 MAX_PATH 直接抛错，而任务又把这个异常当成致命错误——
# 表现就是「输入之后没有任何返回」。整树复制本来也不该是启动任务的前置条件。
MAX_SNAPSHOT_FILES = 5000
MAX_SNAPSHOT_BYTES = 200 * 1024 * 1024
# 扫描预算：再大的仓库也不能让"拍快照"把任务启动拖成几分钟。
# 这个函数是同步的，调用方必须放进线程（见 runtime），否则会卡住整个后端的事件循环。
SCAN_BUDGET_SECONDS = 3.0


def _ignored(part: str) -> bool:
    """忽略口径与搜索共用一份（workspace.ignored_name），否则两处会各漏一半。"""
    return ignored_name(part) or part == ".git"


def _prune(directory: str, names: list[str]) -> set[str]:
    """copytree 的 ignore 回调：目录按前缀剪枝，文件只认精确名。

    不能对文件名也套前缀规则——``build-all.sh`` 这类脚本不是产物目录，
    照抄前缀规则会把它悄悄漏掉，回滚时就少了一个文件。
    """
    dropped: set[str] = set()
    for name in names:
        if name in TREE_IGNORE or name == ".git":
            dropped.add(name)
            continue
        try:
            is_dir = os.path.isdir(os.path.join(directory, name))
        except OSError:
            is_dir = False
        if is_dir and _ignored(name):
            dropped.add(name)
    return dropped


class SnapshotStore:
    def __init__(self, root: Path | str = DEFAULT_SNAPSHOT_ROOT) -> None:
        self.root = Path(root)

    def capture(self, workspace: str, *, reason: str, task_id: str | None = None,
                rel_paths: list[str] | None = None) -> dict:
        source = Path(workspace)
        snapshot_id = f"{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        target = self.root / snapshot_id / "tree"
        self.root.mkdir(parents=True, exist_ok=True)

        files, total_bytes, truncated = self._scan(source)
        too_big = truncated or len(files) > MAX_SNAPSHOT_FILES or total_bytes > MAX_SNAPSHOT_BYTES
        copied = False
        reason_skipped = ""
        if too_big:
            if truncated and len(files) <= MAX_SNAPSHOT_FILES and total_bytes <= MAX_SNAPSHOT_BYTES:
                reason_skipped = (
                    f"扫描超过 {SCAN_BUDGET_SECONDS:g}s 预算（已见到 {len(files)} 个文件 / "
                    f"{total_bytes // (1024 * 1024)} MB），本次只记录已扫到的清单、不做整树复制；"
                    "单个文件的回滚仍由写入前备份保证"
                )
            else:
                reason_skipped = (
                    f"工作区过大（{len(files)} 个文件 / {total_bytes // (1024 * 1024)} MB），"
                    "本次只记录文件清单、不做整树复制；单个文件的回滚仍由写入前备份保证"
                )
        else:
            try:
                shutil.copytree(source, target, ignore=_prune, dirs_exist_ok=True)
                copied = True
            except (OSError, shutil.Error) as error:
                # 路径过长、权限不足、磁盘满都不该让任务直接失败
                reason_skipped = f"快照复制失败：{error}"

        selected = files if rel_paths is None else [f for f in files if f in set(rel_paths)]
        record = {
            "id": snapshot_id,
            "workspace": str(source),
            "reason": reason,
            "task_id": task_id,
            "created_at": time.time(),
            "files": selected,
            "copied": copied,
            "skipped_reason": reason_skipped,
        }
        # 跳过整树复制时不会有 tree/，目录要自己建，否则写 meta.json 直接 ENOENT
        snapshot_dir = self.root / snapshot_id
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        (snapshot_dir / "meta.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return record

    @staticmethod
    def _scan(source: Path) -> tuple[list[str], int, bool]:
        """列出工作区文件清单。返回 (清单, 总字节, 是否被截断)。

        用 `os.walk` 而不是 `rglob`：后者会先遍历再过滤，`dist-*` 里几万个文件一个都跳不掉，
        在 Windows 上足够把任务启动拖到几十秒。这里直接剪枝，被忽略的目录根本不进入。

        时间与数量都有上限，超了就带着已有清单返回 —— 调用方据此降级为"只记录清单"。
        """
        files: list[str] = []
        total = 0
        deadline = time.monotonic() + SCAN_BUDGET_SECONDS
        for directory, dirnames, filenames in os.walk(source):
            dirnames[:] = sorted(name for name in dirnames if not _ignored(name))
            for name in filenames:
                if name in TREE_IGNORE:
                    continue
                path = Path(directory) / name
                try:
                    total += path.stat().st_size
                    relative = path.relative_to(source)
                except (OSError, ValueError):
                    continue
                files.append(str(relative).replace("\\", "/"))
                if len(files) > MAX_SNAPSHOT_FILES or total > MAX_SNAPSHOT_BYTES:
                    return files, total, True
            if time.monotonic() > deadline:
                return files, total, True
        return files, total, False

    def meta(self, snapshot_id: str) -> dict:
        path = self.root / snapshot_id / "meta.json"
        if not path.exists():
            raise FileNotFoundError(f"快照不存在：{snapshot_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def content(self, snapshot_id: str, relative: str) -> bytes:
        return (self.root / snapshot_id / "tree" / relative).read_bytes()

    def restore(self, snapshot_id: str, workspace: str, relative: str | None = None) -> list[str]:
        """relative 为空时整树回滚，否则只回滚单个文件；返回恢复的文件清单。"""
        source = self.root / snapshot_id / "tree"
        if not source.exists():
            raise FileNotFoundError(f"快照不存在：{snapshot_id}")
        target = Path(workspace)
        if relative is not None:
            shutil.copyfile(source / relative, target / relative)
            return [relative]
        snapshot_files = {
            path.relative_to(source).as_posix()
            for path in source.rglob("*")
            if path.is_file()
        }
        # A full rollback must remove files created after the snapshot too.
        for path in sorted(target.rglob("*"), reverse=True):
            if not path.is_file():
                continue
            rel = path.relative_to(target).as_posix()
            if rel in snapshot_files or any(part in TREE_IGNORE or part == ".git" for part in Path(rel).parts):
                continue
            path.unlink()
        restored: list[str] = []
        for path in source.rglob("*"):
            if not path.is_file():
                continue
            rel = str(path.relative_to(source)).replace("\\", "/")
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target / rel)
            restored.append(rel)
        return restored

    def list(self, task_id: str | None = None) -> list[dict]:
        if not self.root.exists():
            return []
        records = []
        for meta in sorted(self.root.glob("*/meta.json")):
            record = json.loads(meta.read_text(encoding="utf-8"))
            if task_id is None or record.get("task_id") == task_id:
                records.append(record)
        return records

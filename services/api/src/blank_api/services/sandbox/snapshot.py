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


class SnapshotNotRestorable(RuntimeError):
    """这份快照只有清单、没有副本，恢复不了。

    以前这种情况在 ``restore()`` 里抛 ``FileNotFoundError``，路由没接住，用户
    看到 500 —— 与「快照不存在」和「服务坏了」两种事实都分不开。恢复不了本身
    是正常结果（工作区过大时我们主动跳过整树复制），必须带着当初的原因返回。
    """

    def __init__(self, snapshot_id: str, reason: str = "") -> None:
        super().__init__(reason or f"快照 {snapshot_id} 没有可恢复的文件副本")
        self.snapshot_id = snapshot_id
        self.reason = reason or "这份快照只记录了文件清单，没有复制文件内容"


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
    def __init__(self, root: Path | str | None = None) -> None:
        # 显式传 root 优先（测试与工具都用它）；否则认 BLANK_SNAPSHOT_DIR，
        # 再退回仓库内的 data/snapshots。测试不该往开发机的 data/ 里写快照。
        if root is not None:
            self.root = Path(root)
        else:
            self.root = Path(os.environ.get("BLANK_SNAPSHOT_DIR") or DEFAULT_SNAPSHOT_ROOT)

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

    def restorable(self, snapshot_id: str) -> tuple[bool, str]:
        """这份快照能不能恢复；不能时把当初跳过的原因带回来。

        判据是 ``tree/`` 在不在，而不是 ``meta.copied``：复制失败、复制到一半被
        外部删除，两种形状都要落在这里。``copied=true`` 但目录没了同样不可恢复。
        """
        source = self.root / snapshot_id / "tree"
        if source.is_dir():
            return True, ""
        reason = ""
        try:
            reason = str(self.meta(snapshot_id).get("skipped_reason") or "")
        except (FileNotFoundError, OSError, ValueError):
            reason = ""
        return False, reason or "这份快照只记录了文件清单，没有复制文件内容"

    def restore_report(
        self, snapshot_id: str, workspace: str, relative: str | None = None
    ) -> dict:
        """恢复并说清"哪些回来了、哪些没回来"。

        与 :meth:`restore` 的区别是失败不再抛 ``FileNotFoundError``：
        不能恢复时抛 :class:`SnapshotNotRestorable`，调用方据此回 409。
        """
        ok, reason = self.restorable(snapshot_id)
        if not ok:
            raise SnapshotNotRestorable(snapshot_id, reason)

        source = self.root / snapshot_id / "tree"
        record = self.meta(snapshot_id)
        inventory = [str(item) for item in record.get("files") or []]
        if relative is not None:
            restored = self.restore(snapshot_id, workspace, relative)
            # 单文件恢复只承诺这一个文件，其余文件"没动"是语义，不是跳过。
            return {"restored": restored, "skipped": [], "skipped_reason": ""}

        restored = self.restore(snapshot_id, workspace, None)
        present = set(restored)
        # 清单里有、副本里没有的文件：扫描时见到了，但复制被预算或权限挡掉。
        # 这些文件保持原样，必须点名，不能混在「恢复成功」里。
        skipped = [item for item in inventory if item not in present]
        return {"restored": restored, "skipped": skipped, "skipped_reason": ""}

    def restore(self, snapshot_id: str, workspace: str, relative: str | None = None) -> list[str]:
        """relative 为空时整树回滚，否则只回滚单个文件；返回恢复的文件清单。

        没有副本时抛 :class:`SnapshotNotRestorable`（带原因），不再是
        ``FileNotFoundError`` —— 后者在路由层会被当成未捕获异常，变成 500。
        """
        ok, reason = self.restorable(snapshot_id)
        source = self.root / snapshot_id / "tree"
        if not ok or not source.exists():
            raise SnapshotNotRestorable(snapshot_id, reason)
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

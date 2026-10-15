"""工作区快照：覆盖或删除前留一份可恢复副本。"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

from blank_api.services.workspace import TREE_IGNORE

DEFAULT_SNAPSHOT_ROOT = Path("data/snapshots")


class SnapshotStore:
    def __init__(self, root: Path | str = DEFAULT_SNAPSHOT_ROOT) -> None:
        self.root = Path(root)

    def capture(self, workspace: str, *, reason: str, task_id: str | None = None,
                rel_paths: list[str] | None = None) -> dict:
        source = Path(workspace)
        snapshot_id = f"{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        target = self.root / snapshot_id / "tree"
        self.root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            source,
            target,
            ignore=shutil.ignore_patterns(*TREE_IGNORE, ".git"),
            dirs_exist_ok=True,
        )
        files = sorted(
            str(path.relative_to(source)).replace("\\", "/")
            for path in source.rglob("*")
            if path.is_file()
        )
        selected = files if rel_paths is None else [f for f in files if f in set(rel_paths)]
        record = {
            "id": snapshot_id,
            "workspace": str(source),
            "reason": reason,
            "task_id": task_id,
            "created_at": time.time(),
            "files": selected,
        }
        (self.root / snapshot_id / "meta.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return record

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

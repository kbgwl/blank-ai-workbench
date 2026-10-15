"""审计日志：一次任务的工具调用过程必须可还原，且落盘前脱敏。"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from blank_api.services.log_safety import redact
from blank_api.services.workspace import mask_path

DEFAULT_AUDIT_DIR = Path("data/audit")


@dataclass(slots=True)
class AuditRecord:
    action: str
    tool: str
    task_id: str | None = None
    node: str | None = None
    project_id: str | None = None
    workspace: str | None = None
    detail: dict = field(default_factory=dict)
    exit_code: int | None = None
    duration_ms: int | None = None
    backend: str | None = None
    status: str = "ok"
    ts: float = field(default_factory=time.time)


class AuditLog:
    def __init__(self, directory: Path | str = DEFAULT_AUDIT_DIR) -> None:
        self.directory = Path(directory)

    def record(self, entry: AuditRecord) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / "tool-calls.jsonl"
        payload = asdict(entry)
        payload["workspace"] = mask_path(entry.workspace) if entry.workspace else None
        payload["detail"] = json.loads(redact(json.dumps(entry.detail, ensure_ascii=False, default=str)))
        payload["action"] = redact(entry.action)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return path

    def read(self, task_id: str | None = None) -> list[dict]:
        path = self.directory / "tool-calls.jsonl"
        if not path.exists():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if task_id is None or entry.get("task_id") == task_id:
                records.append(entry)
        return records

    def trace(self, task_id: str) -> list[str]:
        """按顺序还原一次任务的工具调用链，供验收「根据审计日志还原工具调用过程」。"""
        return [f"{r['ts']:.0f} {r['tool']} {r['action']} -> {r['status']}" for r in self.read(task_id)]

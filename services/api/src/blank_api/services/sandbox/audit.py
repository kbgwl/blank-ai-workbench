"""审计日志：一次任务的工具调用过程必须可还原，且落盘前脱敏。"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from blank_api.services.log_safety import redact
from blank_api.services.workspace import mask_path

DEFAULT_AUDIT_DIR = Path("data/audit")


def audit_log() -> "AuditLog":
    """审计日志的唯一入口。

    目录用 BLANK_AUDIT_DIR 指出来，默认落在 `data/audit`（已被 .gitignore 覆盖）。
    留一个函数而不是各处 `AuditLog()`：将来要按任务分文件、要加轮转，只有这一处要改。
    """
    import os

    return AuditLog(Path(os.environ.get("BLANK_AUDIT_DIR") or DEFAULT_AUDIT_DIR))


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

    def record(self, entry: AuditRecord, secrets: Sequence[str] = ()) -> Path:
        """落盘前脱敏：运行时密钥换成 `****`，主机绝对路径换成工作区掩码。

        路径口径直接取记录里自己的 `workspace`：命令和参数里最常出现的绝对路径
        就是工作区根，而这一列本来就要掩掉，两处必须同一个说法。
        `secrets` 由调用方传（那份密钥清单只有网关有），否则审计文件会成为一个
        绕过所有日志脱敏的旁路通道。
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / "tool-calls.jsonl"
        raw_roots = [str(entry.workspace)] if entry.workspace else []
        if entry.workspace:
            resolved = str(Path(str(entry.workspace)).resolve())
            if resolved not in raw_roots:
                raw_roots.append(resolved)
        masked = mask_path(entry.workspace) if entry.workspace else ""

        def shield(text: str) -> str:
            out = redact(text, secrets)
            if masked:
                for raw in raw_roots:
                    out = out.replace(raw, masked)
            return out

        def shield_value(value):
            """逐个字段脱敏，而不是先 json.dumps 再整串替换。

            Windows 路径序列化成 JSON 后反斜杠是成对的（`C:\\Users\\…`），
            拿真实路径去整串替换永远匹配不上，主机路径就这么原样落进了审计文件。
            """
            if isinstance(value, str):
                return shield(value)
            if isinstance(value, dict):
                return {key: shield_value(item) for key, item in value.items()}
            if isinstance(value, list):
                return [shield_value(item) for item in value]
            if value is None or isinstance(value, (bool, int, float)):
                return value
            return shield(str(value))

        payload = asdict(entry)
        payload["workspace"] = masked or None
        payload["detail"] = shield_value(entry.detail)
        payload["action"] = shield(entry.action)
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

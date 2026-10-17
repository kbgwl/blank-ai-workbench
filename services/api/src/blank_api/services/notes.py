"""Cross-window handoff notes.

Long runs outlive their context window. Summarisation keeps the *story* alive
but loses the operational details the next window actually needs: which file is
half-edited, what the failing assertion was, which command was already ruled
out. Codex solves this with a notes file the model writes as it goes and which
is re-injected after every compaction.

The protocol implemented here:

* ``write_notes`` — the model records durable state (append keeps a log).
* ``read_notes``  — the model can re-read it after a compaction.
* on every compaction the notes block is prepended to the summary, so the
  handoff survives even if the model forgot to look.

Notes live outside the workspace on purpose: they are run state, not project
content, and must not pollute the user's repository or their diffs.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = Path("data/notes")
MAX_NOTE_CHARS = 8_000
MAX_ENTRY_CHARS = 2_000
HANDOFF_LIMIT = 3_000

_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class NoteError(ValueError):
    pass


def _root() -> Path:
    override = str(os.environ.get("BLANK_NOTES_DIR") or "").strip()
    return Path(override) if override else DEFAULT_ROOT


@dataclass(frozen=True)
class NoteState:
    task_id: str
    content: str = ""
    updated_at: float = 0.0

    @property
    def exists(self) -> bool:
        return bool(self.content)

    @property
    def chars(self) -> int:
        return len(self.content)

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "chars": self.chars,
            "exists": self.exists,
            "updated_at": self.updated_at,
        }

    def handoff(self) -> str:
        """Block injected into the context after a compaction or on resume."""
        if not self.content:
            return ""
        body = self.content
        if len(body) > HANDOFF_LIMIT:
            body = "…（较早的笔记已截断）\n" + body[-HANDOFF_LIMIT:]
        return f"## 跨窗口交接笔记（此前写下，仍有效）\n{body}"


class NoteStore:
    """File-backed note store, one markdown file per task."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else _root()

    def path_for(self, task_id: str) -> Path:
        clean = str(task_id or "").strip()
        if not _TASK_ID.match(clean):
            raise NoteError("笔记的文件名不合法：task_id 只允许字母、数字、下划线和短横线")
        return self.root / f"{clean}.md"

    def read(self, task_id: str) -> NoteState:
        path = self.path_for(task_id)
        try:
            text = path.read_text(encoding="utf-8")
            stamp = path.stat().st_mtime
        except (FileNotFoundError, NotADirectoryError, OSError, UnicodeDecodeError):
            return NoteState(task_id=task_id)
        return NoteState(task_id=task_id, content=text, updated_at=stamp)

    def write(
        self,
        task_id: str,
        content: str,
        *,
        mode: str = "replace",
        step: int = 0,
    ) -> NoteState:
        text = str(content or "").replace("\r\n", "\n").strip()
        if not text:
            raise NoteError("笔记内容不能为空")
        if mode not in ("replace", "append"):
            raise NoteError("mode 只能是 replace 或 append")

        path = self.path_for(task_id)
        if mode == "append":
            previous = self.read(task_id).content
            stamp = time.strftime("%H:%M:%S")
            entry = text[:MAX_ENTRY_CHARS]
            prefix = f"- [{stamp} · 第 {max(1, int(step))} 步] "
            text = f"{previous}\n{prefix}{entry}".strip() if previous else f"{prefix}{entry}"

        if len(text) > MAX_NOTE_CHARS:
            # 笔记是"最近状态优先"：超预算时丢最早的，留最新的
            text = "…（较早的笔记已截断）\n" + text[-MAX_NOTE_CHARS:]

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        return NoteState(task_id=task_id, content=text, updated_at=path.stat().st_mtime)

    def clear(self, task_id: str) -> None:
        try:
            self.path_for(task_id).unlink()
        except (FileNotFoundError, OSError):
            pass


def handoff_block(store: NoteStore, task_id: str) -> str:
    """Empty string when the task never wrote notes — callers can concatenate."""
    try:
        return store.read(task_id).handoff()
    except NoteError:
        return ""

"""Memory consolidation — turning a finished run into durable project memory.

Compaction keeps a run alive; notes carry state across windows; neither survives
the task. When a task ends successfully we distil the three things a future run
would otherwise have to rediscover:

* what the task was and how it ended (summary),
* the operational residue (notes the model wrote),
* the facts the run proved (acceptance results, touched files, commands).

The store is deliberately deterministic — no model call, no surprises. It is a
markdown file per workspace, capped in size, deduplicated at bullet level, so
repeated runs against the same repo do not inflate it.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOT = Path("data/memory")
MAX_MEMORY_CHARS = 12_000
MAX_BULLET_CHARS = 300
MAX_BULLETS_PER_RUN = 12

_HEADER = "# 项目长期记忆（自动固化）\n\n> 由空白格在任务成功后自动归纳。手动编辑安全，重跑只会追加新条目。\n"
_WS = re.compile(r"\s+")


def _root() -> Path:
    override = str(os.environ.get("BLANK_MEMORY_DIR") or "").strip()
    return Path(override) if override else DEFAULT_ROOT


def _normalize(text: str) -> str:
    return _WS.sub(" ", str(text or "")).strip()


def _fingerprint(text: str) -> str:
    return _normalize(text).lower()


@dataclass(frozen=True)
class MemoryHit:
    bullet: str
    section: str
    score: int

    def as_dict(self) -> dict:
        return {"bullet": self.bullet, "section": self.section, "score": self.score}


@dataclass
class ConsolidationResult:
    path: str
    added: list[str] = field(default_factory=list)
    skipped: int = 0
    dropped: int = 0
    total_chars: int = 0

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "added": len(self.added),
            "skipped": self.skipped,
            "dropped": self.dropped,
            "total_chars": self.total_chars,
        }


class MemoryStore:
    """Per-workspace markdown memory."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else _root()

    def path_for(self, workspace: str | Path) -> Path:
        resolved = str(Path(workspace).resolve())
        slug = re.sub(r"[^A-Za-z0-9\u4e00-\u9fa5]+", "-", Path(resolved).name or "root").strip("-")
        digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:8]
        return self.root / f"{slug or 'root'}-{digest}.md"

    def load(self, workspace: str | Path) -> str:
        try:
            return self.path_for(workspace).read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, OSError, UnicodeDecodeError):
            return ""

    def consolidate(
        self,
        workspace: str | Path,
        *,
        task_id: str,
        title: str = "",
        summary: str = "",
        notes: str = "",
        facts: list[str] | tuple[str, ...] = (),
        at: datetime | None = None,
    ) -> ConsolidationResult:
        """Append one run's distilled knowledge. Never raises on bad input."""
        path = self.path_for(workspace)
        existing = self.load(workspace)
        known = {
            _fingerprint(line[2:]) for line in existing.splitlines() if line.startswith("- ")
        }

        stamp = (at or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        heading = _normalize(title) or f"任务 {task_id[:8]}"
        bullets: list[str] = []

        head = _normalize(summary)
        if head:
            bullets.append(f"结果：{head[:MAX_BULLET_CHARS]}")

        for line in notes.splitlines():
            cleaned = _normalize(line.lstrip("-*").strip())
            if len(cleaned) >= 8:
                bullets.append(cleaned[:MAX_BULLET_CHARS])

        for fact in facts:
            cleaned = _normalize(fact)
            if len(cleaned) >= 4:
                bullets.append(cleaned[:MAX_BULLET_CHARS])

        result = ConsolidationResult(path=str(path))
        fresh: list[str] = []
        for bullet in bullets[:MAX_BULLETS_PER_RUN]:
            key = _fingerprint(bullet)
            if key in known:
                result.skipped += 1
                continue
            known.add(key)
            fresh.append(bullet)
            result.added.append(bullet)

        if not fresh:
            result.total_chars = len(existing)
            return result

        section = f"\n## {stamp} · {heading}\n" + "\n".join(f"- {item}" for item in fresh) + "\n"
        merged = (existing or _HEADER).rstrip() + "\n" + section

        if len(merged) > MAX_MEMORY_CHARS:
            merged, dropped = _trim(merged, MAX_MEMORY_CHARS)
            result.dropped = dropped

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(merged, encoding="utf-8")
        tmp.replace(path)
        result.total_chars = len(merged)
        return result

    def search(self, workspace: str | Path, query: str, limit: int = 5) -> list[MemoryHit]:
        """Naive but honest retrieval: token overlap over bullets, newest first on ties."""
        tokens = [item for item in _normalize(query).lower().split(" ") if len(item) > 1]
        if not tokens:
            return []
        text = self.load(workspace)
        if not text:
            return []

        hits: list[MemoryHit] = []
        section = ""
        for line in text.splitlines():
            if line.startswith("## "):
                section = line[3:].strip()
                continue
            if not line.startswith("- "):
                continue
            body = line[2:].strip()
            lowered = body.lower()
            score = sum(3 if token in lowered else 0 for token in tokens)
            if score:
                hits.append(MemoryHit(bullet=body, section=section, score=score))
        hits.sort(key=lambda item: item.score, reverse=True)
        return hits[: max(1, int(limit))]


def _trim(text: str, limit: int) -> tuple[str, int]:
    """Drop the oldest sections until the file fits; keep the header."""
    lines = text.splitlines()
    header_end = 0
    for index, line in enumerate(lines):
        if line.startswith("## "):
            header_end = index
            break
    header = "\n".join(lines[:header_end]).rstrip() if header_end else _HEADER.rstrip()
    body = lines[header_end:] if header_end else []

    sections: list[list[str]] = []
    current: list[str] = []
    for line in body:
        if line.startswith("## ") and current:
            sections.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append(current)

    dropped = 0
    while sections and len(header) + sum(len("\n".join(s)) for s in sections) > limit:
        sections.pop(0)
        dropped += 1
    kept = "\n".join(header.splitlines() + [item for section in sections for item in section])
    return kept.rstrip() + "\n", dropped

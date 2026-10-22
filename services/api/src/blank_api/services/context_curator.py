"""上下文管家：每次任务开始前，确定性地挑选该带上的背景资料。

不调用模型，也不把整个工作区塞进提示词。它只回答四个问题：

1. 项目长期记忆里有没有和这次目标相关的条目（共享记忆，带来源）；
2. 工作区里有没有名字直接相关的文件（只给路径，让模型自己决定读不读）；
3. 最近几轮会话已经由 ``history_for`` 带上了，本次带了几轮；
4. 这些内容加起来超没超预算，超了就按优先级截断。

所有决定都写进 ``context.curated`` 事件，事后能回答"模型当时到底看到了什么"。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from blank_api.services.memory import MemoryHit, MemoryStore
from blank_api.services.text_index import tokenize

DEFAULT_MAX_CHARS = 2_200
MAX_MEMORY_HITS = 4
MAX_FILE_CANDIDATES = 6
MAX_SCAN_ENTRIES = 4_000
MAX_DEPTH = 4
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".next", ".nuxt", "dist", "build", "target", "coverage", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".idea", ".vscode", "data", "logs", "_backup",
    "_verify", ".tox", "site-packages",
}


@dataclass
class CuratedContext:
    text: str = ""
    memory_hits: list[dict] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    history_turns: int = 0
    chars: int = 0
    truncated: bool = False

    @property
    def empty(self) -> bool:
        return not self.text

    def as_dict(self) -> dict:
        return {
            "text": self.text[:4_000],
            "chars": self.chars,
            "memory_hits": self.memory_hits,
            "files": self.files,
            "history_turns": self.history_turns,
            "truncated": self.truncated,
            "sources": sorted(
                {
                    *(hit.get("kind", "记忆") for hit in self.memory_hits),
                    *(["文件路径"] if self.files else []),
                }
            ),
        }


def _memory_lines(hits: list[MemoryHit], used: int) -> list[str]:
    lines: list[str] = []
    for hit in hits:
        if used + len(hit.bullet) > DEFAULT_MAX_CHARS // 2:
            break
        where = hit.section or "未知时间"
        lines.append(f"- [共享记忆·{hit.kind}｜{where}] {hit.bullet}")
        used += len(lines[-1])
    return lines


def _file_candidates(root: Path, goal: str) -> list[str]:
    """按文件名与目标的 token 重合度找候选文件。只看名字，不读内容。"""
    if not root or not root.is_dir():
        return []
    tokens = [token for token in tokenize(goal) if len(token) >= 2]
    if not tokens:
        return []
    scored: list[tuple[int, str]] = []
    scanned = 0
    root_str = str(root)
    for dirpath, dirnames, filenames in os.walk(root_str):
        rel_dir = os.path.relpath(dirpath, root_str)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        if depth >= MAX_DEPTH:
            dirnames[:] = []
        else:
            dirnames[:] = [
                name
                for name in dirnames
                if name.lower() not in SKIP_DIRS and not name.startswith(".")
            ]
        for name in filenames:
            scanned += 1
            if scanned > MAX_SCAN_ENTRIES:
                return [path for _, path in sorted(scored, reverse=True)[:MAX_FILE_CANDIDATES]]
            lowered = name.lower()
            value = sum(3 if len(token) >= 3 else 1 for token in tokens if token in lowered)
            if value:
                relative = os.path.join(rel_dir, name) if rel_dir != "." else name
                scored.append((value, relative.replace("\\", "/")))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [path for _, path in scored[:MAX_FILE_CANDIDATES]]


def curate(
    *,
    root: str | Path,
    goal: str,
    memory: MemoryStore | None = None,
    history: list[dict] | tuple = (),
    max_chars: int = DEFAULT_MAX_CHARS,
) -> CuratedContext:
    store = memory if isinstance(memory, MemoryStore) else MemoryStore()
    query = str(goal or "").strip()
    result = CuratedContext(history_turns=max(0, len(history) // 2))

    hits: list[MemoryHit] = []
    if query:
        try:
            hits = store.search(root, query, limit=MAX_MEMORY_HITS)
        except Exception:
            hits = []
    result.memory_hits = [hit.as_dict() for hit in hits]
    result.files = _file_candidates(Path(root), query)

    blocks: list[str] = []
    budget = max(600, int(max_chars))
    used = 0
    if hits:
        lines = _memory_lines(hits, used)
        if lines:
            blocks.append("共享项目记忆（由历史任务固化，可能过时，引用前先核对）：\n" + "\n".join(lines))
            used += sum(len(line) + 1 for line in lines)
    if result.files:
        header = "工作区里名字相关的文件（只是候选，未读内容）："
        lines = [f"- {path}" for path in result.files if used + len(path) + 4 <= budget]
        if lines:
            blocks.append(header + "\n" + "\n".join(lines))
            used += sum(len(line) + 1 for line in lines)
    if result.history_turns:
        blocks.append(f"最近 {result.history_turns} 轮会话已经作为对话历史附上，不要重复询问。")

    text = "\n\n".join(blocks)
    if len(text) > budget:
        text = text[: budget - 1].rstrip() + "…"
        result.truncated = True
    result.text = text
    result.chars = len(text)
    return result

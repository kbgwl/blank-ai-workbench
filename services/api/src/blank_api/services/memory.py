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
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from blank_api.services.text_index import score as text_score
from blank_api.services.text_index import tokenize

DEFAULT_ROOT = Path("data/memory")
MAX_MEMORY_CHARS = 12_000
MAX_BULLET_CHARS = 300
MAX_BULLETS_PER_RUN = 12

# 记忆类型对标 Claude Code 的四类：谁在什么时候需要它，决定了该不该带进上下文。
#   user      用户是谁、怎么工作（长期）
#   feedback  对工作方式的纠正与确认（长期）
#   project   这个项目的事实、决定、坑（会随项目变化）
#   reference 外部资源指针（看板、文档、工单系统）
# 自动固化的旧条目没有类型标记，一律按 project 读——它们本来就是任务留下的项目知识。
MEMORY_TYPES = ("user", "feedback", "project", "reference")
DEFAULT_MEMORY_TYPE = "project"

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
    # 记忆条目的来源：任务结论 / 交接笔记 / 自报验收 / 验证命令。
    # 检索结果带上它，人和模型都能区分"哪句是结论、哪句只是过程记录"。
    kind: str = "记忆"
    task_id: str = ""
    # user / feedback / project / reference。旧条目没有标记，读作 project。
    memory_type: str = DEFAULT_MEMORY_TYPE

    @property
    def source_task(self) -> str:
        return self.task_id

    def as_dict(self) -> dict:
        return {
            "bullet": self.bullet,
            "section": self.section,
            "score": self.score,
            "kind": self.kind,
            "type": self.memory_type,
            "task_id": self.task_id,
            "source_task": self.source_task,
            "source": "workspace",
        }


# 模型写入的条目带显式类型标记；自动固化的条目保持旧格式，按 project 读。
_TYPED_BULLET = re.compile(r"^\[(user|feedback|project|reference)\]\s+(.+)$", re.IGNORECASE)


def _parse_bullet(text: str) -> tuple[str, str]:
    """返回 (去掉标记的正文, 类型)。"""
    match = _TYPED_BULLET.match(text)
    if match:
        return match.group(2).strip(), match.group(1).lower()
    return text, DEFAULT_MEMORY_TYPE


_KIND_PREFIXES = (
    ("结果：", "任务结论"),
    ("笔记：", "交接笔记"),
    ("自报验收：", "自报验收"),
    ("验证命令：", "验证命令"),
    ("事实：", "事实"),
)


def _kind_of(bullet: str) -> str:
    for prefix, kind in _KIND_PREFIXES:
        if bullet.startswith(prefix):
            return kind
    return "记忆"


def _task_of(section: str) -> str:
    # 任务 id 是 uuid4（含连字符）。旧文件里只有前 8 位，读出来就是 8 位——
    # 那是当时真写下的事实，不补全、不编造；新条目写完整 id，审批/取证时能直接定位。
    match = re.search(r"任务\s+([0-9a-fA-F][0-9a-fA-F-]{5,})", section or "")
    return match.group(1) if match else ""


# 同一进程内对同一个记忆文件串行化"读-改-写"。consolidate 目前是同步函数，
# 单事件循环里不会交错；但测试、子代理与未来可能的工作线程会并发调用。
_LOCK_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _lock_for(path: Path) -> threading.RLock:
    # 不能拿 resolve() 做键：路径所在目录创建前后，realpath 对 junction / 短文件名的
    # 解析结果可能不同，同一个文件会拿到两把锁，读改写重新交错。abspath 只做词法
    # 归一化，与文件是否已存在无关。
    key = os.path.normcase(os.path.abspath(str(path)))
    with _LOCK_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def _retry(action, *, attempts: int = 6, delay: float = 0.02):
    """Windows 上杀毒/索引服务会短暂占用刚写完的文件，replace/read 可能被拒。

    这类失败是瞬时的，退避重试比让整次记忆固化失败更合理；锁仍然保证同一文件
    的读改写是串行的，重试只是在等外部句柄释放。
    """
    last: OSError | None = None
    for attempt in range(attempts):
        try:
            return action()
        except PermissionError as exc:
            last = exc
            time.sleep(delay * (attempt + 1))
    assert last is not None
    raise last


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
        source: str = "",
        at: datetime | None = None,
    ) -> ConsolidationResult:
        """Append one run's distilled knowledge. Never raises on bad input."""
        path = self.path_for(workspace)
        # 读改写必须在同一把锁里完成：两个并发任务都读到旧文件、各自追加、
        # 后写的覆盖先写的，就是悄无声息地丢条目。
        with _lock_for(path):
            return self._consolidate_locked(
                path,
                task_id=task_id,
                title=title,
                summary=summary,
                notes=notes,
                facts=facts,
                source=source,
                at=at,
            )

    def _consolidate_locked(
        self,
        path: Path,
        *,
        task_id: str,
        title: str,
        summary: str,
        notes: str,
        facts: list[str] | tuple[str, ...],
        source: str,
        at: datetime | None,
    ) -> ConsolidationResult:
        heading = _normalize(title) or f"任务 {task_id[:8]}"
        bullets: list[str] = []

        head = _normalize(summary)
        if head:
            bullets.append(f"结果：{head[:MAX_BULLET_CHARS]}")

        for line in notes.splitlines():
            cleaned = _normalize(line.lstrip("-*").strip())
            if len(cleaned) >= 8:
                bullets.append(f"笔记：{cleaned[:MAX_BULLET_CHARS]}")

        for fact in facts:
            cleaned = _normalize(fact)
            if len(cleaned) >= 4:
                bullets.append(f"事实：{cleaned[:MAX_BULLET_CHARS]}")

        # 完整 id 写进 heading：检索结果要能直接定位来源任务（旧文件只有前 8 位）。
        origin = f" · 任务 {_normalize(task_id)}" if _normalize(task_id) else ""
        if _normalize(source):
            origin += f" · 来源 {_normalize(source)[:60]}"
        return self._append_locked(
            path,
            heading=heading,
            origin=origin,
            bullets=bullets[:MAX_BULLETS_PER_RUN],
            at=at,
        )

    def remember(
        self,
        workspace: str | Path,
        *,
        memory_type: str,
        content: str,
        task_id: str = "",
        source: str = "",
        at: datetime | None = None,
    ) -> ConsolidationResult:
        """模型显式写下的一条带类型记忆。

        与自动固化共用同一份去重账本（按整条正文取指纹）与同一个字符上限：
        同一句话第二次写只会记一笔，反复写也不会把文件撑大。
        """
        kind = str(memory_type or "").strip().lower()
        if kind not in MEMORY_TYPES:
            raise ValueError(f"未知的记忆类型：{memory_type}（可选 {', '.join(MEMORY_TYPES)}）")
        body = _normalize(content)
        if not body:
            raise ValueError("记忆内容不能为空")
        path = self.path_for(workspace)
        origin = f" · 任务 {_normalize(task_id)}" if _normalize(task_id) else ""
        if _normalize(source):
            origin += f" · 来源 {_normalize(source)[:60]}"
        with _lock_for(path):
            return self._append_locked(
                path,
                heading="模型记忆",
                origin=origin,
                bullets=[f"[{kind}] {body[:MAX_BULLET_CHARS]}"],
                at=at,
            )

    def _append_locked(
        self,
        path: Path,
        *,
        heading: str,
        origin: str,
        bullets: list[str],
        at: datetime | None,
    ) -> ConsolidationResult:
        try:
            existing = _retry(lambda: path.read_text(encoding="utf-8"))
        except (FileNotFoundError, NotADirectoryError, UnicodeDecodeError):
            existing = ""
        # PermissionError / 其他 OSError 不能当成空文件：那会在读失败时用新内容
        # 覆盖整个记忆文件，并发下表现为"部分任务凭空消失"。宁可让本次固化失败。
        known = {
            _fingerprint(line[2:]) for line in existing.splitlines() if line.startswith("- ")
        }

        stamp = (at or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        result = ConsolidationResult(path=str(path))
        fresh: list[str] = []
        for bullet in bullets:
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

        section = (
            f"\n## {stamp} · {heading}{origin}\n"
            + "\n".join(f"- {item}" for item in fresh)
            + "\n"
        )
        merged = (existing or _HEADER).rstrip() + "\n" + section

        if len(merged) > MAX_MEMORY_CHARS:
            merged, dropped = _trim(merged, MAX_MEMORY_CHARS)
            result.dropped = dropped

        path.parent.mkdir(parents=True, exist_ok=True)
        # 固定 `.tmp` 在多进程下会互相覆盖；带上 pid 与随机串，再把 replace 当提交点。
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            def _write() -> None:
                with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                    handle.write(merged)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, path)

            _retry(_write)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        result.total_chars = len(merged)
        return result

    def search(
        self,
        workspace: str | Path,
        query: str,
        limit: int = 5,
        memory_type: str = "",
    ) -> list[MemoryHit]:
        """Token-overlap retrieval over bullets, newest first on ties.

        ``memory_type`` 非空时只返回该类型；旧的无标记条目按 project 算——
        自动固化的本来就是项目知识，不该因为它们写得早就不出现在 project 检索里。
        """
        wanted = str(memory_type or "").strip().lower()
        if wanted and wanted not in MEMORY_TYPES:
            raise ValueError(f"未知的记忆类型：{memory_type}（可选 {', '.join(MEMORY_TYPES)}）")
        tokens = tokenize(query)
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
            body, kind = _parse_bullet(line[2:].strip())
            if not body:
                continue
            if wanted and kind != wanted:
                continue
            value = text_score(body, tokens)
            if value:
                hits.append(
                    MemoryHit(
                        bullet=body,
                        section=section,
                        score=value,
                        kind=_kind_of(body),
                        task_id=_task_of(section),
                        memory_type=kind,
                    )
                )
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

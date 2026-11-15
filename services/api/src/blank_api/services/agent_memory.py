"""子代理定义里的 ``memory``：持久记忆的作用域、目录与提示词。

上游（Claude Code）给声明了 ``memory`` 的 agent 一套文件型记忆：派发前把
``MEMORY.md`` 读进子代理的系统提示，并给一段按 scope 定制的指导语。

本机对齐的部分（2026-11-15 现场取证）：

* scope 只认 ``user`` / ``project`` / ``local``；名字先去非法字符
  （``[^a-zA-Z0-9\\-_]`` → ``-``，空串 → ``unknown``）；
* ``project`` → ``<项目根>/.blank/agent-memory/<name>``，
  ``local`` → ``<项目根>/.blank/agent-memory-local/<name>``，
  ``user`` → ``<用户配置根>/agent-memory/<name>``；
* 派发时建目录，``MEMORY.md`` 有内容就内联、为空写空态文案，三条 scope
  指导语逐字保留。

本地偏离（写进 README 与本模块）：

* 不自动补 Read/Write/Edit 工具——子代理工具池不因定义扩大；改为提供一个
  只能写这个作用域 ``MEMORY.md`` 的 ``memory_write`` 工具；
* 上游「一文件一记忆 + MEMORY.md 索引」的布局压缩成单一 ``MEMORY.md``
  条目流（与主代理的项目长期记忆同一种 ``- [type] ...`` 格式）；
* 远程记忆目录（``CLAUDE_CODE_REMOTE_MEMORY_DIR``）与 primed entrypoint
  未移植：本机没有远程记忆存储。
"""

from __future__ import annotations

import os
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from blank_api.services.memory import MEMORY_TYPES, MAX_BULLET_CHARS
from blank_api.services.tools.base import ToolDefinition, ToolOutcome, fail, ok

MEMORY_SCOPES = ("user", "project", "local")
ENTRYPOINT_NAME = "MEMORY.md"
MAX_AGENT_MEMORY_CHARS = 12_000
# 手动改大的文件读进来也要有上界：记忆段是每次派发都进系统提示的。
TRIM_MARKER = "（较早的记忆已截断）"

_INVALID_NAME = re.compile(r"[^A-Za-z0-9\-_]")
_WORKSPACE = re.compile(r"\s+")

# 上游 oNt 的三条 scope 指导语，逐字保留。
SCOPE_GUIDELINES = {
    "user": (
        "- Since this memory is user-scope, keep learnings general since they "
        "apply across all projects"
    ),
    "project": (
        "- Since this memory is project-scope and shared with your team via "
        "version control, tailor your memories to this project"
    ),
    "local": (
        "- Since this memory is local-scope (not checked into version control), "
        "tailor your memories to this project and machine"
    ),
}

_TYPE_LINES = (
    "- **user** — the user's role, expertise, or working preferences",
    "- **feedback** — a correction or confirmation of how you should approach "
    "work. Confirmations ('yes, good call') are quieter than corrections — "
    "watch for them",
    "- **project** — ongoing work, deadlines, or decisions not derivable from "
    "code or git history",
    "- **reference** — where to find information in an external system "
    "(issue tracker, dashboard, channel)",
)

_NOT_TO_SAVE = (
    "- Code patterns, conventions, architecture, file paths, or project "
    "structure — these can be derived by reading the current project state.",
    "- Git history, recent changes, or who-changed-what — `git log` / "
    "`git blame` are authoritative.",
    "- Debugging solutions or fix recipes — the fix is in the code; the commit "
    "message has the context.",
    "- Anything already documented in CLAUDE.md files.",
    "- Ephemeral task details: in-progress work, temporary state, current "
    "conversation context.",
)

_WHEN_TO_ACCESS = (
    "- When memories seem relevant, or the user references prior-conversation work.",
    "- You MUST access memory when the user explicitly asks you to check, "
    "recall, or remember.",
    "- If the user says to *ignore* or *not use* memory: Do not apply "
    "remembered facts, cite, compare against, or mention memory content.",
    "- Memory records can become stale over time. Use memory as context for "
    "what was true at a given point in time. Before answering the user or "
    "building assumptions based solely on information in memory records, "
    "verify that the memory is still correct and up-to-date by reading the "
    "current state of the files or resources. If a recalled memory conflicts "
    "with current information, trust what you observe now — and update or "
    "remove the stale memory rather than acting on it.",
)

_BEFORE_RECOMMEND = (
    "A memory that names a specific function, file, or flag is a claim that it "
    "existed *when the memory was written*. It may have been renamed, removed, "
    "or never merged. Before recommending it:",
    "",
    "- If the memory names a file path: check the file exists.",
    "- If the memory names a function or flag: grep for it.",
    "- If the user is about to act on your recommendation (not just asking "
    "about history), verify first.",
    "",
    '"The memory says X exists" is not the same as "X exists now."',
    "",
    "A memory that summarizes repo state (activity logs, architecture "
    "snapshots) is frozen in time. If the user asks about *recent* or *current* "
    "state, prefer `git log` or reading the code over recalling the snapshot.",
)


def sanitize_agent_name(name: str) -> str:
    """上游 ``nl``：非 ``[A-Za-z0-9-_]`` 一律换成 ``-``，空串读作 unknown。"""
    cleaned = _INVALID_NAME.sub("-", str(name or ""))
    return cleaned or "unknown"


def _user_root() -> Path:
    override = str(os.environ.get("BLANK_USER_AGENT_MEMORY_DIR") or "").strip()
    return Path(override) if override else Path.home() / ".blank" / "agent-memory"


def agent_memory_dir(scope: str, agent_name: str, project_dir: str | Path) -> Path:
    """scope → 目录。非法 scope 直接报错：调用方已在解析层挡过一遍。"""
    if scope not in MEMORY_SCOPES:
        raise ValueError(f"未知的记忆作用域：{scope}（可选 {', '.join(MEMORY_SCOPES)}）")
    leaf = sanitize_agent_name(agent_name)
    if scope == "project":
        return Path(project_dir) / ".blank" / "agent-memory" / leaf
    if scope == "local":
        return Path(project_dir) / ".blank" / "agent-memory-local" / leaf
    return _user_root() / leaf


def entrypoint_path(directory: str | Path) -> Path:
    return Path(directory) / ENTRYPOINT_NAME


def _read_entrypoint(directory: str | Path) -> str:
    try:
        text = entrypoint_path(directory).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, NotADirectoryError, OSError, UnicodeDecodeError):
        return ""
    if len(text) <= MAX_AGENT_MEMORY_CHARS:
        return text
    # 记忆按时间追加，最新的在末尾：截断保留尾部，并留一行明示前面还有内容。
    tail = text[-MAX_AGENT_MEMORY_CHARS:]
    cut = tail.find("\n")
    if cut != -1:
        tail = tail[cut + 1 :]
    return f"{TRIM_MARKER}\n{tail}"


def render_memory_section(*, scope: str, agent_name: str, directory: str | Path) -> str:
    """上游 ``Pa``：标题 + 目录 + 类型/禁忌/访问 + scope 指导语 + MEMORY.md。"""
    folder = str(Path(directory))
    entrypoint = _read_entrypoint(directory)
    lines = [
        "# Persistent Agent Memory",
        "",
        f"You have a persistent, file-based memory system at `{folder}`.",
        "",
        "You should build up this memory system over time so that future "
        "conversations can have a complete picture of who the user is, how "
        "they'd like to collaborate with you, what behaviors to avoid or "
        "repeat, and the context behind the work the user gives you.",
        "",
        "If the user explicitly asks you to remember something, save it "
        "immediately as whichever type fits best. If they ask you to forget "
        "something, find and remove the relevant entry.",
        "",
        "## Types of memory",
        "",
        "Save a memory when you learn one of the following — pick the matching "
        "`type:`",
        "",
        *_TYPE_LINES,
        "",
        "## What NOT to save in memory",
        "",
        *_NOT_TO_SAVE,
        "",
        "These exclusions apply even when the user explicitly asks you to save.",
        "",
        "## How to save memories",
        "",
        "Call the `memory_write` tool with a `type` (one of the four above) and "
        "a one-line `content`. The entry is appended to `MEMORY.md` in the "
        "directory above and is loaded into your context on your next run. Do "
        "not save duplicates; when a memory turns out to be wrong, replace the "
        "relevant line in `MEMORY.md` instead of piling on corrections.",
        "",
        "## When to access memories",
        "",
        *_WHEN_TO_ACCESS,
        "",
        "## Before recommending from memory",
        "",
        *_BEFORE_RECOMMEND,
        "",
        SCOPE_GUIDELINES[scope],
        "",
        f"## {ENTRYPOINT_NAME}",
        "",
        entrypoint
        or f"Your {ENTRYPOINT_NAME} is currently empty. When you save new "
        "memories, they will appear here.",
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class AppendResult:
    path: str
    added: int
    skipped: int
    total_chars: int


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


def _lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(str(path)))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _normalize(text: str) -> str:
    return _WORKSPACE.sub(" ", str(text or "")).strip()


def append_memory(
    directory: str | Path,
    *,
    memory_type: str,
    content: str,
    source: str = "",
) -> AppendResult:
    """把一条记忆追加进这个作用域的 ``MEMORY.md``；同一句只记一笔。"""
    kind = str(memory_type or "").strip().lower()
    if kind not in MEMORY_TYPES:
        raise ValueError(f"未知的记忆类型：{memory_type}（可选 {', '.join(MEMORY_TYPES)}）")
    body = _normalize(content)
    if not body:
        raise ValueError("记忆内容不能为空")
    bullet = f"- [{kind}] {body[:MAX_BULLET_CHARS]}"
    if _normalize(source):
        bullet += f"（来源：{_normalize(source)[:120]}）"

    folder = Path(directory)
    path = entrypoint_path(folder)
    with _lock_for(path):
        try:
            existing = path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, UnicodeDecodeError):
            existing = ""
        known = {
            _normalize(line[2:]).lower()
            for line in existing.splitlines()
            if line.startswith("- ")
        }
        if _normalize(bullet[2:]).lower() in known:
            return AppendResult(str(path), 0, 1, len(existing))

        merged = (existing.rstrip() + "\n" if existing.strip() else "") + f"{bullet}\n"
        if len(merged) > MAX_AGENT_MEMORY_CHARS:
            merged = _trim_file(merged)
        folder.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(merged, encoding="utf-8", newline="\n")
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return AppendResult(str(path), 1, 0, len(merged))


def _trim_file(text: str) -> str:
    """超上限时丢最旧的条目，保留一行截断标记。"""
    lines = [line for line in text.splitlines() if line.strip()]
    dropped = False
    while len("\n".join(lines)) > MAX_AGENT_MEMORY_CHARS and len(lines) > 1:
        lines.pop(0)
        dropped = True
    if dropped and (not lines or lines[0] != TRIM_MARKER):
        lines.insert(0, TRIM_MARKER)
    return "\n".join(lines) + "\n"


_MEMORY_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": list(MEMORY_TYPES),
            "description": "user 用户是谁；feedback 对工作方式的纠正/确认；"
            "project 项目事实与决定；reference 外部资源指针",
        },
        "content": {"type": "string", "description": "要长期记住的一件事，一句话写清"},
        "source": {"type": "string", "description": "可选来源：文件路径、链接或依据"},
    },
    "required": ["type", "content"],
}


def agent_memory_tools(directory: str | Path) -> list[ToolDefinition]:
    """只给声明了 ``memory`` 的子代理用的写入工具，作用域钉死在目录上。"""
    folder = Path(directory)

    async def _write(_context, arguments: dict) -> ToolOutcome:
        try:
            result = append_memory(
                folder,
                memory_type=str(arguments.get("type") or ""),
                content=str(arguments.get("content") or ""),
                source=str(arguments.get("source") or ""),
            )
        except ValueError as exc:
            return fail("error", str(exc))
        except OSError as exc:
            return fail("error", f"记忆写入失败：{exc}")
        if not result.added:
            return ok("这条记忆已经在 MEMORY.md 里，未重复写入。", added=0)
        return ok(
            "已写入 MEMORY.md，下次派发会自动带进上下文。",
            added=1,
            path=str(folder / ENTRYPOINT_NAME),
        )

    return [
        ToolDefinition(
            name="memory_write",
            description=(
                "把这个子代理值得长期记住的一件事写进它的 MEMORY.md。"
                "只记跨会话仍有价值的结论、偏好或资源指针，别写过程流水账。"
            ),
            input_schema=_MEMORY_WRITE_SCHEMA,
            risk_level="low",
            handler=_write,
        )
    ]

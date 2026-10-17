"""Notes and memory tools.

Three small tools that together make long runs survivable:

* ``write_notes``  — record durable state so a later context window can pick up.
* ``read_notes``   — read it back.
* ``memory_search`` — query what previous runs already learned about this repo.

They are read-only with respect to the workspace, which is why they sit at low
risk and do not need approval. The notes file is capped; a model that dumps its
entire transcript into it wastes the very context the notes exist to protect,
so the cap is enforced in the store, not trusted to the caller.
"""

from __future__ import annotations

from blank_api.services.memory import MemoryStore
from blank_api.services.notes import MAX_NOTE_CHARS, NoteError, NoteStore
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok

WRITE_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "description": "要记住的内容：当前进度、半成品改动、已排除的方案"},
        "mode": {
            "type": "string",
            "enum": ["replace", "append"],
            "description": "replace 覆盖整份笔记；append 追加一条带时间戳的记录（默认）",
        },
    },
    "required": ["content"],
}

READ_NOTE_SCHEMA = {"type": "object", "properties": {}}

MEMORY_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "要用语义关键词查什么"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
    },
    "required": ["query"],
}


def _store(context: ToolContext) -> NoteStore:
    store = getattr(context, "notes", None)
    return store if isinstance(store, NoteStore) else NoteStore()


async def _write_notes(context: ToolContext, arguments: dict) -> ToolOutcome:
    content = str(arguments.get("content") or "").strip()
    mode = str(arguments.get("mode") or "append").strip().lower()
    step = int(arguments.get("step") or 0) or 0
    try:
        state = _store(context).write(context.task_id, content, mode=mode, step=step)
    except NoteError as exc:
        return fail("error", str(exc))
    except OSError as exc:
        return fail("error", f"笔记写入失败：{exc}")
    return ok(
        f"笔记已保存（{state.chars} 字，{'覆盖' if mode == 'replace' else '追加'}）",
        chars=state.chars,
        mode=mode,
        max_chars=MAX_NOTE_CHARS,
    )


async def _read_notes(context: ToolContext, arguments: dict) -> ToolOutcome:
    state = _store(context).read(context.task_id)
    if not state.exists:
        return ok("本任务还没有笔记。可以在关键节点用 write_notes 记下进度。", chars=0, exists=False)
    return ok(state.content, chars=state.chars, exists=True)


async def _memory_search(context: ToolContext, arguments: dict) -> ToolOutcome:
    query = str(arguments.get("query") or "").strip()
    if not query:
        return fail("error", "query 不能为空")
    limit = max(1, min(20, int(arguments.get("limit") or 5)))
    store = getattr(context, "memory", None)
    store = store if isinstance(store, MemoryStore) else MemoryStore()
    hits = store.search(context.root, query, limit=limit)
    if not hits:
        return ok("项目长期记忆里没有匹配的条目。", hits=[], total=0)
    lines = [f"- [{hit.section or '未知时间'}] {hit.bullet}" for hit in hits]
    return ok("\n".join(lines), hits=[hit.as_dict() for hit in hits], total=len(hits))


def memory_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="write_notes",
            description=(
                "把当前进度、半成品改动、已排除的方案写进本任务的交接笔记。"
                "上下文被压缩或换窗口时会自动带回来，长任务里应当主动使用。"
            ),
            input_schema=WRITE_NOTE_SCHEMA,
            risk_level="low",
            handler=_write_notes,
        ),
        ToolDefinition(
            name="read_notes",
            description="读回本任务的交接笔记，用于确认此前已经做过什么。",
            input_schema=READ_NOTE_SCHEMA,
            risk_level="low",
            handler=_read_notes,
        ),
        ToolDefinition(
            name="memory_search",
            description="在当前项目的长期记忆里检索历史任务留下的结论与坑，避免重复排查。",
            input_schema=MEMORY_SEARCH_SCHEMA,
            risk_level="low",
            handler=_memory_search,
        ),
    ]

"""Bounded context assembly for long-running agent runs."""

from __future__ import annotations

from collections.abc import Sequence

DEFAULT_CONTEXT_CHARS = 48_000
TOOL_RESULT_CHARS = 8_000


def _clip(content: str, limit: int) -> str:
    if len(content) <= limit:
        return content
    return content[:limit] + "\n…（工具结果已截断，完整结果保存在任务证据中）"


def compact_messages(messages: Sequence[dict], max_chars: int = DEFAULT_CONTEXT_CHARS) -> list[dict]:
    """Keep the instruction/goal and the newest execution window within budget.

    Tool results are bounded independently so one command cannot consume the
    whole context. The function is deterministic and does not call a model,
    making it safe to run before every model request and easy to test.
    """
    if not messages:
        return []
    normalized: list[dict] = []
    for item in messages:
        current = dict(item)
        content = str(current.get("content") or "")
        if current.get("role") == "tool":
            content = _clip(content, TOOL_RESULT_CHARS)
        current["content"] = content
        normalized.append(current)
    if sum(len(str(item.get("content") or "")) for item in normalized) <= max_chars:
        return normalized

    system = next((item for item in normalized if item.get("role") == "system"), None)
    goal = next((item for item in normalized if item.get("role") == "user"), None)
    prefix = [item for item in (system, goal) if item is not None]
    prefix_chars = sum(len(str(item.get("content") or "")) for item in prefix)
    budget = max(1000, max_chars - prefix_chars)
    tail: list[dict] = []
    used = 0
    for item in reversed(normalized):
        if item in prefix:
            continue
        size = len(str(item.get("content") or ""))
        if tail and used + size > budget:
            break
        tail.append(item)
        used += size
    tail.reverse()
    omitted = max(0, len(normalized) - len(prefix) - len(tail))
    marker = {"role": "system", "content": f"上下文已压缩：省略 {omitted} 条较早执行消息；以最近状态和任务证据为准。"}
    return [*prefix, marker, *tail]

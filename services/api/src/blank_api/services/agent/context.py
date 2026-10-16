"""Bounded context assembly for long-running agent runs.

Two layers, deliberately separated:

1. `compact_messages` — deterministic, cheap, model-free. Clips oversized tool
   results and hard-truncates as a last resort. Always safe to call.
2. `ContextCompactor` — model-assisted. When the history outgrows the budget it
   folds the messages that just slid out of the window into an incremental
   five-section summary (goal / constraints / done / failures / open), so the
   model keeps knowing what it already did instead of seeing "N messages
   omitted".
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

DEFAULT_CONTEXT_CHARS = 48_000
TOOL_RESULT_CHARS = 8_000

SUMMARY_TRANSCRIPT_CHARS = 1_200
SUMMARY_TOTAL_CHARS = 24_000

FIVE_SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "done": {"type": "array", "items": {"type": "string"}},
        "failures": {"type": "array", "items": {"type": "string"}},
        "open": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["goal", "constraints", "done", "failures", "open"],
}

SUMMARY_INSTRUCTION = (
    "你在压缩一段 Agent 的执行历史。目标是把已经滑出上下文窗口的旧消息，"
    "压成一份能让人继续干活的摘要。只输出 JSON，字段固定为："
    '{"goal": "任务目标", "constraints": ["必须遵守的约束"], "done": ["已经完成的动作及其结果"], '
    '"failures": ["失败的动作、报错和原因"], "open": ["还没做完、需要继续处理的事项"]}。'
    "要求：不要复述大段代码；每条一句话；失败原因和未决事项绝对不能丢；没有内容就给空数组。"
)

# 摘要回调：把「新滑出窗口的消息」合并进「已有摘要」，返回新的摘要正文。
SummarizeFn = Callable[[Sequence[dict], str], Awaitable[str]]


def _clip(content: str, limit: int) -> str:
    if len(content) <= limit:
        return content
    return content[:limit] + "\n…（工具结果已截断，完整结果保存在任务证据中）"


def _chars(item: dict) -> int:
    return len(str(item.get("content") or ""))


def _total_chars(messages: Sequence[dict]) -> int:
    return sum(_chars(item) for item in messages)


def _normalize(messages: Sequence[dict]) -> list[dict]:
    normalized: list[dict] = []
    for item in messages:
        current = dict(item)
        content = str(current.get("content") or "")
        if current.get("role") == "tool":
            content = _clip(content, TOOL_RESULT_CHARS)
        current["content"] = content
        normalized.append(current)
    return normalized


def _split_prefix(messages: Sequence[dict]) -> tuple[list[dict], list[dict]]:
    """系统提示与任务目标永远保留，其余算作可折叠的历史。"""
    system = next((item for item in messages if item.get("role") == "system"), None)
    goal = next((item for item in messages if item.get("role") == "user"), None)
    prefix = [item for item in (system, goal) if item is not None]
    history = [item for item in messages if item not in prefix]
    return prefix, history


def compact_messages(messages: Sequence[dict], max_chars: int = DEFAULT_CONTEXT_CHARS) -> list[dict]:
    """Deterministic fallback: keep the goal and the newest window, drop the middle.

    The function does not call a model, making it safe to run before every model
    request and easy to test. `ContextCompactor` uses it whenever a model-backed
    summary is unavailable or fails.
    """
    if not messages:
        return []
    normalized = _normalize(messages)
    if _total_chars(normalized) <= max_chars:
        return normalized

    prefix, history = _split_prefix(normalized)
    prefix_chars = _total_chars(prefix)
    budget = max(1000, max_chars - prefix_chars)
    tail: list[dict] = []
    used = 0
    for item in reversed(history):
        size = _chars(item)
        if tail and used + size > budget:
            break
        tail.append(item)
        used += size
    tail.reverse()
    omitted = max(0, len(history) - len(tail))
    marker = {
        "role": "system",
        "content": f"上下文已压缩：省略 {omitted} 条较早执行消息；以最近状态和任务证据为准。",
    }
    return [*prefix, marker, *tail]


def render_summary(payload: dict) -> str:
    """把五要素 JSON 渲染成一段紧凑的文本，放进 system 位置。"""
    sections = (
        ("constraints", "约束"),
        ("done", "已完成"),
        ("failures", "失败与原因"),
        ("open", "未决事项"),
    )
    parts = ["【执行历史摘要 · 较早消息已折叠，摘要由模型生成】"]
    goal = str(payload.get("goal") or "").strip()
    if goal:
        parts.append(f"目标：{goal}")
    for key, label in sections:
        value = payload.get(key) or []
        if isinstance(value, str):
            value = [value]
        items = [str(item).strip() for item in value if str(item).strip()]
        if items:
            parts.append(f"{label}：\n" + "\n".join(f"- {item}" for item in items))
    return "\n".join(parts)


def load_summary_json(text: str) -> dict:
    """容错解析模型返回的摘要 JSON。"""
    stripped = (text or "").strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("模型未返回摘要 JSON")
    parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("摘要 JSON 不是对象")
    return parsed


def build_summary_prompt(folded: Sequence[dict], previous: str) -> list[dict]:
    transcript = "\n".join(
        f"[{item.get('role')}] {str(item.get('content') or '')[:SUMMARY_TRANSCRIPT_CHARS]}" for item in folded
    )
    if len(transcript) > SUMMARY_TOTAL_CHARS:
        transcript = transcript[:SUMMARY_TOTAL_CHARS] + "\n…（更早内容已省略）"
    instruction = SUMMARY_INSTRUCTION
    if previous.strip():
        instruction += "\n已有摘要如下，请在其基础上增量合并，已有的条目不要丢失：\n" + previous
    return [
        {"role": "system", "content": instruction},
        {"role": "user", "content": f"需要折叠的执行历史：\n{transcript}"},
    ]


@dataclass
class ContextCompactor:
    """Rolling five-section summary, updated incrementally.

    `covered` counts how many history messages have already been folded into
    `summary`, so a long run pays for summarization only when the window slides
    forward — not on every single model call.
    """

    max_chars: int = DEFAULT_CONTEXT_CHARS
    summary: str = ""
    covered: int = 0
    fold_failures: int = field(default=0)

    async def build(self, messages: Sequence[dict], summarize: SummarizeFn | None = None) -> list[dict]:
        normalized = _normalize(messages)
        if not normalized:
            return []
        if _total_chars(normalized) <= self.max_chars:
            return normalized

        prefix, history = _split_prefix(normalized)
        if not history:
            return normalized

        budget = max(1000, self.max_chars - _total_chars(prefix) - len(self.summary))
        tail: list[dict] = []
        used = 0
        for item in reversed(history):
            size = _chars(item)
            if tail and used + size > budget:
                break
            tail.append(item)
            used += size
        tail.reverse()
        cut = len(history) - len(tail)

        if cut > self.covered and summarize is not None:
            folded = history[self.covered : cut]
            try:
                self.summary = await summarize(folded, self.summary)
                self.covered = cut
            except Exception:
                # 摘要模型不可用时退回确定性截断，绝不让上下文压缩本身拖垮任务。
                self.fold_failures += 1

        if self.summary.strip():
            head = {"role": "system", "content": self.summary}
        else:
            head = {
                "role": "system",
                "content": f"上下文已压缩：省略 {cut} 条较早执行消息；以最近状态和任务证据为准。",
            }
        return [*prefix, head, *tail]

"""Bounded context assembly for long-running agent runs.

Three layers, deliberately separated by cost:

1. `microcompact` — deterministic, cheap, model-free, runs on almost every turn.
   Shrinks the parts that carry no information: oversized tool results far from
   the head, byte-identical duplicate outputs, inline base64 blobs. Nothing the
   model still needs is dropped, so it never changes the answer.
2. `compact_messages` — deterministic hard truncation, the last-resort fallback
   when a model-backed summary is unavailable.
3. `ContextCompactor` — model-assisted. When the history still outgrows the
   budget it folds the messages that just slid out of the window into an
   incremental five-section summary (goal / constraints / done / failures /
   open), so the model keeps knowing what it already did instead of seeing
   "N messages omitted".

Every pass reports token accounting and the compaction boundary, so cost is
explainable instead of magic.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

DEFAULT_CONTEXT_CHARS = 48_000
TOOL_RESULT_CHARS = 8_000
# 第一层在预算过半时就开工，避免每次都在快撑爆时才手忙脚乱地压缩。
MICRO_TRIGGER_RATIO = 0.55
MICRO_TOOL_RESULT_CHARS = 2_000
MICRO_KEEP_RECENT = 8
MICRO_DUPLICATE_MIN_CHARS = 400
BASE64_PATTERN = re.compile(r"data:[^;,\s]{0,60};base64,[A-Za-z0-9+/=\s]{200,}")

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


def estimate_tokens(text: str) -> int:
    """Cheap token estimate: one token per CJK glyph, four chars per token otherwise.

    Deliberately approximate. The number exists to explain cost and to decide
    when to compact — not to bill anything — so it never calls a tokenizer.
    """
    if not text:
        return 0
    cjk = 0
    for char in text:
        code = ord(char)
        if 0x4E00 <= code <= 0x9FFF or 0x3000 <= code <= 0x303F or 0xFF00 <= code <= 0xFFEF:
            cjk += 1
    return cjk + (len(text) - cjk + 3) // 4


def message_tokens(messages: Sequence[dict]) -> int:
    # 每条消息有固定开销（role / 分隔符），按 4 tokens 计，与主流口径一致。
    return sum(estimate_tokens(str(item.get("content") or "")) + 4 for item in messages)


@dataclass
class MicroStats:
    """第一层的产出明细，用来回答"这一步省了什么"。"""

    clipped: int = 0
    duplicates: int = 0
    base64_removed: int = 0
    chars_saved: int = 0

    @property
    def applied(self) -> bool:
        return bool(self.clipped or self.duplicates or self.base64_removed)

    def as_dict(self) -> dict:
        return {
            "clipped": self.clipped,
            "duplicates": self.duplicates,
            "base64_removed": self.base64_removed,
            "chars_saved": self.chars_saved,
        }


@dataclass
class CompactionReport:
    """一次上下文组装的完整账本。"""

    tokens_before: int = 0
    tokens_after: int = 0
    micro: MicroStats = field(default_factory=MicroStats)
    folded: int = 0
    boundary: int = 0
    summary_chars: int = 0
    covered: int = 0

    def as_dict(self) -> dict:
        return {
            "active_context_tokens_before": self.tokens_before,
            "active_context_tokens_after": self.tokens_after,
            "tokens_saved": max(0, self.tokens_before - self.tokens_after),
            "micro": self.micro.as_dict(),
            "folded": self.folded,
            "boundary": self.boundary,
            "summary_chars": self.summary_chars,
            "covered": self.covered,
        }


def _microcompact(
    messages: Sequence[dict],
    keep_recent: int = MICRO_KEEP_RECENT,
    result_chars: int = MICRO_TOOL_RESULT_CHARS,
) -> tuple[list[dict], MicroStats]:
    """Fine-grained trim. Never removes a message, so tool-call pairing stays valid."""
    stats = MicroStats()
    before = _total_chars(messages)
    cutoff = max(0, len(messages) - keep_recent)
    seen: dict[str, int] = {}
    result: list[dict] = []
    for index, item in enumerate(messages):
        current = dict(item)
        content = str(current.get("content") or "")
        cleaned = BASE64_PATTERN.sub("[内联 base64 数据已移除]", content)
        if len(cleaned) != len(content):
            stats.base64_removed += 1
            content = cleaned
        if current.get("role") == "tool":
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            earlier = seen.get(digest)
            if earlier is not None and len(content) >= MICRO_DUPLICATE_MIN_CHARS:
                content = f"[与第 {earlier} 条工具输出完全相同，已折叠]"
                stats.duplicates += 1
            else:
                seen[digest] = index
            if index < cutoff and len(content) > result_chars:
                content = _clip(content, result_chars)
                stats.clipped += 1
        current["content"] = content
        result.append(current)
    stats.chars_saved = max(0, before - _total_chars(result))
    return result, stats


def microcompact(
    messages: Sequence[dict], max_chars: int = DEFAULT_CONTEXT_CHARS
) -> tuple[list[dict], MicroStats]:
    """Run layer one only when the window is already past the trigger ratio."""
    if _total_chars(messages) <= int(max_chars * MICRO_TRIGGER_RATIO):
        return list(messages), MicroStats()
    return _microcompact(messages)


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

    Layer one (`microcompact`) always runs first: it is free, and it often
    removes enough noise that the expensive model-backed fold is not needed.
    `last_report` carries the token accounting of the most recent assembly.
    """

    max_chars: int = DEFAULT_CONTEXT_CHARS
    summary: str = ""
    covered: int = 0
    fold_failures: int = field(default=0)
    last_report: CompactionReport = field(default_factory=CompactionReport)

    async def build(self, messages: Sequence[dict], summarize: SummarizeFn | None = None) -> list[dict]:
        normalized = _normalize(messages)
        if not normalized:
            self.last_report = CompactionReport()
            return []

        tokens_before = message_tokens(normalized)
        trimmed, micro = microcompact(normalized, self.max_chars)
        if _total_chars(trimmed) <= self.max_chars:
            self.last_report = CompactionReport(
                tokens_before=tokens_before,
                tokens_after=message_tokens(trimmed),
                micro=micro,
                boundary=0,
                covered=self.covered,
                summary_chars=len(self.summary),
            )
            return trimmed

        prefix, history = _split_prefix(trimmed)
        if not history:
            self.last_report = CompactionReport(
                tokens_before=tokens_before,
                tokens_after=message_tokens(trimmed),
                micro=micro,
                covered=self.covered,
                summary_chars=len(self.summary),
            )
            return trimmed

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

        folded_now = 0
        if cut > self.covered and summarize is not None:
            folded = history[self.covered : cut]
            try:
                self.summary = await summarize(folded, self.summary)
                self.covered = cut
                folded_now = len(folded)
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
        result = [*prefix, head, *tail]
        self.last_report = CompactionReport(
            tokens_before=tokens_before,
            tokens_after=message_tokens(result),
            micro=micro,
            folded=folded_now,
            boundary=cut,
            summary_chars=len(self.summary),
            covered=self.covered,
        )
        return result

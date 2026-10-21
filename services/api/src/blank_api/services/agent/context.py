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

**Water level.** Compaction is triggered by a *token* level, not by a character
count, whenever the provider tells us the model's context window (Codex does the
same with ``auto_compact_fallback_buffer_tokens``). The estimate is calibrated
against the real ``prompt_tokens`` the provider reports for the same prompt, so
the water line means the same thing for a 32k model and a 200k one. Without a
known window the character budget is used, exactly as before.
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

# 水位默认值：到窗口的 82% 开始压缩，给模型回复预留 16k tokens。
# 与设置页 data/settings.json 的 compaction 段一一对应，这里是缺省兜底。
DEFAULT_TRIGGER_RATIO = 0.82
DEFAULT_RESERVE_TOKENS = 16_384
# 校准系数的合理区间：超出这个范围说明测到的不是同一件事（例如缓存命中），
# 不要拿它去缩放水位，否则一次异常读数就能把压缩永久推到错误位置。
CALIBRATION_MIN = 0.2
CALIBRATION_MAX = 5.0

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


# 一张图折算的 token 数。见 message_tokens 里的说明。
IMAGE_TOKEN_ESTIMATE = 900


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
    total = 0
    for item in messages:
        total += estimate_tokens(str(item.get("content") or "")) + 4
        # 图片按张计：base64 不是文本，字符启发式算出来是几十万，会把水位一次性顶爆。
        # 一张 1024×1024 在主流供应商那里约 700~1200 tokens，取 900：宁可高估。
        # 低估会让压缩晚一步触发，而晚一步的代价是整轮上下文按原样重发一遍。
        total += IMAGE_TOKEN_ESTIMATE * len(item.get("images") or ())
    return total


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
    # 水位相关：窗口未知时 trigger_at 为 0，表示这次是按字符预算判定的
    context_window: int = 0
    trigger_at: int = 0
    calibration: float = 1.0
    observed_tokens: int = 0
    triggered_by: str = ""

    @property
    def water_ratio(self) -> float:
        if not self.trigger_at or not self.context_window:
            return 0.0
        denom = max(1, self.context_window)
        return round(self.tokens_before / denom, 4)

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
            "context_window": self.context_window,
            "trigger_at": self.trigger_at,
            "water_ratio": self.water_ratio,
            "calibration": self.calibration,
            "observed_tokens": self.observed_tokens,
            "triggered_by": self.triggered_by,
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

    ``context_window`` (0 = unknown) switches the trigger from characters to
    tokens: the water line sits at ``context_window * trigger_ratio`` and the
    fold happens before the window minus ``reserve_tokens`` is exhausted, which
    is the last point where a summary can still be written.
    """

    max_chars: int = DEFAULT_CONTEXT_CHARS
    summary: str = ""
    covered: int = 0
    fold_failures: int = field(default=0)
    last_report: CompactionReport = field(default_factory=CompactionReport)
    # 模型窗口：0 表示未知，退回字符预算（旧行为）
    context_window: int = 0
    trigger_ratio: float = DEFAULT_TRIGGER_RATIO
    reserve_tokens: int = DEFAULT_RESERVE_TOKENS
    # 估算校准：真实 prompt_tokens / 估算值，用滑动平均收敛
    calibration: float = 1.0
    calibration_samples: int = field(default=0)
    observed_tokens: int = 0

    # -- 水位 ---------------------------------------------------------------
    def trigger_tokens(self) -> int:
        """水位线：窗口比例与"窗口减预留"取更小的那个。

        预留大于窗口（小窗口配大预留）时不能把水位压到 1 ——那等于每一轮都压缩。
        这种配置下比例值才是可用的那个，宁可少留余量也不要把上下文打成筛子。
        """
        if self.context_window <= 0:
            return 0
        by_ratio = max(1, int(self.context_window * self.trigger_ratio))
        by_reserve = self.context_window - self.reserve_tokens
        if by_reserve <= 0:
            return by_ratio
        return max(1, min(by_ratio, by_reserve))

    def measure(self, messages: Sequence[dict]) -> int:
        return max(0, int(round(message_tokens(messages) * self.calibration)))

    def observe(self, prompt_tokens: int, estimated_tokens: int) -> None:
        """用供应商回报的真实 prompt_tokens 校准估算器。

        同一个提示词，模型说的 token 数才是准的；我们的估算只用来决定"什么时候压"。
        第一次直接采用，之后按 0.5 滑动平均，避免单次异常把水位带偏。
        """
        if prompt_tokens <= 0 or estimated_tokens <= 0:
            return
        ratio = prompt_tokens / estimated_tokens
        if not (CALIBRATION_MIN <= ratio <= CALIBRATION_MAX):
            return
        if self.calibration_samples <= 0:
            self.calibration = round(ratio, 4)
        else:
            self.calibration = round(self.calibration * 0.5 + ratio * 0.5, 4)
        self.calibration_samples += 1
        self.observed_tokens = int(prompt_tokens)

    def level(self, messages: Sequence[dict]) -> dict:
        """当前水位，供事件日志与前端"上下文 12.3k / 32k"展示。"""
        tokens = self.measure(messages)
        trigger = self.trigger_tokens()
        return {
            "tokens": tokens,
            "trigger_at": trigger,
            "context_window": self.context_window,
            "ratio": round(tokens / self.context_window, 4) if self.context_window else 0.0,
            "trigger_ratio": self.trigger_ratio,
            "reserve_tokens": self.reserve_tokens,
            "calibration": self.calibration,
        }

    def _chars_per_token(self, messages: Sequence[dict]) -> float:
        chars = _total_chars(messages)
        estimate = message_tokens(messages)
        if estimate <= 0 or chars <= 0:
            return 4.0
        # 校准后一个"真实 token"大约对应多少字符
        return max(0.2, (chars / estimate) / max(self.calibration, 0.01))

    def _budget_chars(self, messages: Sequence[dict], trigger: int) -> int:
        """把 token 水位换算成这一轮的消息字符预算。"""
        return max(2_000, int(trigger * self._chars_per_token(messages)))

    async def build(self, messages: Sequence[dict], summarize: SummarizeFn | None = None) -> list[dict]:
        normalized = _normalize(messages)
        if not normalized:
            self.last_report = CompactionReport()
            return []

        tokens_before = self.measure(normalized)
        token_mode = self.context_window > 0
        trigger = self.trigger_tokens()
        # token 模式按水位判定；窗口未知时退回字符预算，行为与旧版一致
        char_budget = self._budget_chars(normalized, trigger) if token_mode else self.max_chars
        triggered_by = "tokens" if token_mode else "chars"
        base_report = {
            "tokens_before": tokens_before,
            "context_window": self.context_window,
            "trigger_at": trigger,
            "calibration": self.calibration,
            "observed_tokens": self.observed_tokens,
        }
        over_trigger = tokens_before > trigger if token_mode else _total_chars(normalized) > char_budget
        if not over_trigger:
            self.last_report = CompactionReport(
                **base_report,
                tokens_after=tokens_before,
                boundary=0,
                covered=self.covered,
                summary_chars=len(self.summary),
            )
            return normalized

        trimmed, micro = _microcompact(normalized) if token_mode else microcompact(normalized, self.max_chars)
        after_micro_chars = _total_chars(trimmed)
        after_micro_tokens = self.measure(trimmed)
        still_over = after_micro_tokens > trigger if token_mode else after_micro_chars > char_budget
        if not still_over:
            self.last_report = CompactionReport(
                **base_report,
                tokens_after=after_micro_tokens,
                micro=micro,
                boundary=0,
                covered=self.covered,
                summary_chars=len(self.summary),
                triggered_by=f"{triggered_by}:micro",
            )
            return trimmed

        prefix, history = _split_prefix(trimmed)
        if not history:
            self.last_report = CompactionReport(
                **base_report,
                tokens_after=after_micro_tokens,
                micro=micro,
                covered=self.covered,
                summary_chars=len(self.summary),
                triggered_by=f"{triggered_by}:micro",
            )
            return trimmed

        budget = max(1000, char_budget - _total_chars(prefix) - len(self.summary))
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
            **base_report,
            tokens_after=self.measure(result),
            micro=micro,
            folded=folded_now,
            boundary=cut,
            summary_chars=len(self.summary),
            covered=self.covered,
            triggered_by=f"{triggered_by}:fold",
        )
        return result


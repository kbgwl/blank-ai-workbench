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
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from blank_api.services.instructions import content_sha

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


def message_ref(
    item: dict,
    index: int,
    *,
    summary_text: str = "",
    handoff_text: str = "",
    user_order: int | None = None,
) -> dict:
    """历史里的一条换成"指认得出来、但不抄正文"的引用。

    Codex 的 `replacement_history` 把整段新历史原样写进本机日志（实测一条 30 万字节，里面有
    `E:\\projects\\…` 这种主机路径）。我们的事件会进证据包、被同项目其他人看到、还进全文索引，
    所以借它的**结构**（逐条 + 出处 + 指纹），不借它**抄正文**那一手——与 #41 对指令层同一口径。
    `kind` 是判定不是搬运：`system` 那两条到底是"压缩摘要"还是"交接笔记"，界面和用户都需要分开看。
    """
    content = str(item.get("content") or "")
    role = str(item.get("role") or "")
    if item.get("tool_call_id"):
        kind = "tool_result"
    elif item.get("tool_calls"):
        kind = "tool_call"
    elif role == "system" and summary_text and content == summary_text:
        kind = "summary"
    elif role == "system" and handoff_text and content == handoff_text:
        kind = "handoff"
    else:
        kind = role or "unknown"
    return {
        "index": index,
        "role": role,
        "kind": kind,
        "chars": len(content),
        "sha": content_sha(content),
        # 出生那一轮（对标 Codex 每条历史都带 internal_chat_message_metadata_passthrough.turn_id）。
        # 空串就留着空串：这条消息是谁写的、哪一轮写的没记过，不能替它编一个第 0 轮。
        "run": str(item.get("run_id") or ""),
        # 第几次用户输入（对标 guardian_metadata.user_input_order）。非 user 条目是 None，
        # 前端读成"没说"，不是"第 0 次"。
        "user_order": user_order,
    }


def history_refs(
    items: Sequence[dict],
    *,
    summary_text: str = "",
    handoff_text: str = "",
) -> list[dict]:
    refs: list[dict] = []
    order = 0
    for index, item in enumerate(items):
        user_order: int | None = None
        if str(item.get("role") or "") == "user":
            user_order = order
            order += 1
        refs.append(
            message_ref(
                item,
                index,
                summary_text=summary_text,
                handoff_text=handoff_text,
                user_order=user_order,
            )
        )
    return refs


def replacement_record(
    before: Sequence[dict],
    after: Sequence[dict],
    *,
    summary_text: str = "",
    handoff_text: str = "",
    retention: dict[str, dict] | None = None,
    window_number: int | None = None,
) -> dict:
    """一次替换换进了什么、丢掉了什么。

    比的是**内容指纹**，不是消息身份——我们没有任何持久化的消息行可以指（会话状态存在 LangGraph
    检查点里，Codex 的 `retained_source.message_id` 那种出处我们给不出来）。因此"同一条被裁剪过"
    会算成"旧的一条丢了、新的一条进来了"，这是事实而不是缺陷：裁剪确实改了模型看到的内容。
    `dropped` 只列引用不列正文，但它足以回答 §4 里那个我们此前答不出的问题：这次压缩丢了哪些内容。

    `retention` + `window_number` 给留下的条目补上 Codex `guardian_metadata.retained_source` 那一半
    语义：一条内容**从第几窗起留下**、**已跨过几窗**。身份用内容指纹派生（`ret:<sha12>`）而不是
    另发一个 uuid——我们没有消息行可指，但同一份内容在任何一窗都该认成同一条，这点比 Codex 更硬。
    同一窗里出现两次相同内容（复读的工具输出）只算跨了一窗。
    """
    after_refs = history_refs(after, summary_text=summary_text, handoff_text=handoff_text)
    kept_shas = {item["sha"] for item in after_refs}
    before_refs = history_refs(before)
    dropped = [item for item in before_refs if item["sha"] not in kept_shas]
    added = [item for item in after_refs if item["sha"] not in {b["sha"] for b in before_refs}]
    for ref in [*after_refs, *dropped]:
        ref["retained_id"] = f"ret:{str(ref['sha'])[:12]}"
    if retention is not None and window_number is not None:
        touched: set[str] = set()
        for ref in after_refs:
            sha = str(ref["sha"])
            entry = retention.get(sha)
            if entry is None:
                entry = retention[sha] = {"first_window": window_number, "windows_held": 1}
            elif sha not in touched:
                entry["windows_held"] = int(entry.get("windows_held") or 0) + 1
            touched.add(sha)
            ref["retained_since"] = int(entry["first_window"])
            ref["retained_windows"] = int(entry["windows_held"])
        for ref in dropped:
            entry = retention.get(str(ref["sha"]))
            if entry is None:
                continue
            ref["retained_since"] = int(entry["first_window"])
            ref["retained_windows"] = int(entry["windows_held"])
    return {
        "before_items": len(before_refs),
        "after_items": len(after_refs),
        "retained": len(after_refs) - len(added),
        "added": added,
        "dropped": dropped,
        "items": after_refs,
    }


def guardian_trail(
    before: Sequence[dict],
    after: Sequence[dict],
    *,
    reviews: Sequence[dict] = (),
    in_effect: dict[str, Any] | None = None,
) -> dict:
    """审阅侧的轨迹清单（对标 §4.1 的 `guardian_history`）。

    实测 Codex 一条 `compacted` 里两侧是**不对称**的：`replacement_history` 18 / 36 / 39 项，
    `guardian_history` 96 / 151 / 133 项、228,102 字节起——压缩丢的是**模型**的历史，
    审阅侧一条不丢，而且原样抄正文（`retained_context` 里连主机路径都在）。我们借这个
    "两侧条数各记一遍"的结构，不借抄正文：`items` 是替换**前**那份全量历史的逐条引用，
    构造路径与 `replacement_record` 里的 `before_refs` 完全同一（所以被折掉的每一条
    都能在这里按 `retained_id` 对上），「审阅人本来看得见几条、模型现在只剩几条」答得出，
    「原文是什么」仍然只能回工作区文件与工具事件——这是与 Codex 的口径差别，不是遗漏。

    `reviews` 是这次压缩之前发生过的独立审查判定（真源是 `guardian.reviewed` 事件，
    由调用方把事件 seq 一起带进来，两处必须逐 seq 相等）。`in_effect` 记当时生效的预算与
    模式，对标 Codex 条目上的 `fallback_token_limit_override`：那条审阅是在哪个窗口、
    哪套审批模式下做出来的，事后要问得出来。
    """
    items = history_refs(before)
    for ref in items:
        ref["retained_id"] = f"ret:{str(ref['sha'])[:12]}"
    return {
        "items": items,
        "items_reviewed": len(items),
        "items_in_model_context": len(list(after)),
        "reviewed_chars": sum(int(ref["chars"] or 0) for ref in items),
        "reviews": [dict(item) for item in reviews],
        "in_effect": dict(in_effect or {}),
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


def _safe_cut(history: Sequence[dict], cut: int) -> int:
    """把切点退回到不会拆散工具调用配对的位置。

    `role="tool"` 的结果必须紧跟在声明了对应 `tool_calls` 的那条 assistant 之后，
    OpenAI 与 DeepSeek 都按这条校验；切点落在配对中间，折叠后发给供应商的就是
    一条孤儿工具结果，供应商回 400、整条任务失败（2026-10-22 真实链路实测到）。
    往回退只多留几条原文，代价是省得少一点；往下扔会把结果连同证据一起丢掉。
    """
    while 0 < cut < len(history) and (history[cut] or {}).get("role") == "tool":
        cut -= 1
    if cut == 0 and history and (history[0] or {}).get("role") == "tool":
        # 退到段头仍然是一条没有归属的工具结果：往回退等于什么都不折，水位压不下去。
        # 这一段是最老的内容，整段推过去才是对的。
        while cut < len(history) and (history[cut] or {}).get("role") == "tool":
            cut += 1
    return cut


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
    cut = _safe_cut(history, len(history) - len(tail))
    tail = list(history[cut:])
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
    # 窗口链（对标 Codex `compacted` 的 window_number / first_window_id / previous_window_id / window_id）。
    # 出生就是第 0 窗并已有 id，所以第一次压缩时 previous_window_id == first_window_id，
    # 与 Codex 实测的 window 1（prev 与 first 同为 01a13d6e-fc65…）形状一致。
    window_number: int = 0
    window_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    first_window_id: str = ""
    # 留存台账（对标 guardian_metadata.retained_source）：内容指纹 → 从第几窗留下、已跨几窗。
    # 只在压缩这一刻记账，不当主键；条目按 sha 累积，长对话里也就是一堆四十来字节的数。
    retention: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # default_factory 的字段没法被另一个字段的默认值引用，出生窗口在这里补登记。
        self.first_window_id = self.first_window_id or self.window_id

    def open_window(self) -> dict:
        """压缩真的发生了才换窗口：与 `context.compacted` 同频，不是每次组装都算一窗。

        窗口号是给"现在活的是第几段历史"这个问句的答案；id 只用来把同一次执行的
        多条记录串起来，不当主键用（我们的事件主键是 `seq`）。
        """
        previous = self.window_id
        self.window_number += 1
        self.window_id = str(uuid.uuid4())
        return {**self.window, "previous_window_id": previous}

    @property
    def window(self) -> dict:
        """当前窗口的身份。链上任何一条记录都能用这三个字段回到"我替换掉了谁"。"""
        return {
            "window_number": self.window_number,
            "window_id": self.window_id,
            "first_window_id": self.first_window_id,
        }

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

    async def build(
        self, messages: Sequence[dict], summarize: SummarizeFn | None = None, *, author_run_id: str = ""
    ) -> list[dict]:
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
        # 切点不能落在工具调用的配对中间：往回退会把发起那条 assistant 一起留在尾部，
        # 代价是少省几条，换来的是发出去的消息供应商认。
        cut = _safe_cut(history, len(history) - len(tail))
        tail = list(history[cut:])

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
            head = {"role": "system", "content": self.summary, "run_id": author_run_id}
        else:
            head = {
                "role": "system",
                "content": f"上下文已压缩：省略 {cut} 条较早执行消息；以最近状态和任务证据为准。",
                "run_id": author_run_id,
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


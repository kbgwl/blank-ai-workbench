"""Claude Code 的思考强度（effort）解析、钳制与关键词升档。

对标上游 dump 里 Qc / iR / S / yHt / BAr / jAr / j / hct / Vwe / FJ / _1 / K /
zAr / WAr / H / Y / gDe / dKe / pKe / _Ht / fDe / TC / lxr 这一整套：档位词表、
模型封顶、静默降级、关键词升档，以及 thinking 被关时的回落。

纯函数与数据类，不碰 I/O：调用方（运行时、适配器、提示词展开）各自决定
从哪里取模型名与封顶表，这里只保证「同一个输入永远得到同一个结论」。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "high"
# thinking 被关时，高于这一档的请求一律压回它（上游 BAr 与 jAr 的合成结果）。
THINKING_DISABLED_EFFORT_CAP = "high"

EFF_RANK: dict[str, int] = {"low": 1, "medium": 2, "high": 3, "xhigh": 4, "max": 5}

# 上游 H={med:"medium"}：--effort 的缩写别名。
LEVEL_ALIASES: dict[str, str] = {"med": "medium"}
# 上游 Y={ultracode:"xhigh"}：关键词 → 档位。
KEYWORD_EFFORTS: dict[str, str] = {"ultracode": "xhigh"}

THINKING_MODES: tuple[str, ...] = ("auto", "extended", "off")

ULTRATHINK_REMINDER = (
    'The user included the keyword "ultrathink", requesting deeper reasoning on this turn. '
    "Reason as thoroughly as the task warrants."
)
ULTRACODE_REMINDER = (
    'The user included the keyword "ultracode", opting this turn into multi-agent orchestration '
    "— use the Workflow tool to fulfill the request."
)
# ultrathink 与 ultracode 都把本轮抬到 xhigh；两者的差别只在提醒文案。
KEYWORD_LEVEL = "xhigh"


def is_level(value: object) -> bool:
    return value in EFF_RANK


def rank(level: str) -> int:
    return EFF_RANK[level]


def exceeds_default(level: object) -> bool:
    """上游 BAr：档位有效，且高于默认档。"""
    return is_level(level) and rank(level) > rank(DEFAULT_EFFORT)


def effort_for_prompt(declared: object) -> str:
    """``${CLAUDE_EFFORT}`` 的替换值：声明的档位，认不出回落默认。

    这里只认档位、不做模型封顶钳制——那一步发生在真正调用模型时
    （``output_config.effort``），展开命令/Skill 正文时手上还没有模型能力，
    无从按模型封顶。命令/Skill 声明什么，正文里就写什么。
    """
    return declared if is_level(declared) else DEFAULT_EFFORT


# ---- 文本解析（上游 gDe / dKe / pKe / _Ht）---------------------------------


def parse_keyword(text: object) -> str | None:
    """上游 gDe：整条文本就是某个关键词时返回它，否则 None。"""
    if not isinstance(text, str):
        return None
    raw = text.strip().lower()
    return raw if raw in KEYWORD_EFFORTS else None


def keyword_level(text: object) -> str | None:
    """上游 dKe：关键词对应的档位。"""
    keyword = parse_keyword(text)
    return KEYWORD_EFFORTS[keyword] if keyword is not None else None


def parse_level(text: object) -> str | None:
    """上游 pKe：大小写与空白不敏感，认 ``med`` 这类别名，认不出返回 None。"""
    if not isinstance(text, str):
        return None
    raw = text.strip().lower()
    level = LEVEL_ALIASES.get(raw, raw)
    return level if is_level(level) else None


@dataclass(frozen=True)
class EffortParse:
    """上游 _Ht 的返回：解析出的档位，或一条「认不出、按默认来」的告警。"""

    level: str | None
    warning: str | None = None


def parse(text: object) -> EffortParse:
    level = parse_level(text)
    if level is not None:
        return EffortParse(level)
    level = keyword_level(text)
    if level is not None:
        return EffortParse(level)
    return EffortParse(
        None,
        f"Unknown --effort value '{text}' — ignoring it and using the default effort. "
        f"Valid values: {', '.join(EFFORT_LEVELS)}.",
    )


def detect_keyword(text: object) -> str | None:
    """按词边界找关键词（上游 Iut 的 ``\\bultrathink\\b`` 口径）。

    与 :func:`parse_keyword` 的区别：这里看的是**正文里有没有出现**，
    而不是「整条文本是不是关键词」——用户说一句「请用 ultrathink 想」也要认。
    """
    if not isinstance(text, str) or not text:
        return None
    lowered = text.lower()
    for keyword in KEYWORD_EFFORTS:
        if _has_word(lowered, keyword):
            return keyword
    if _has_word(lowered, "ultrathink"):
        return "ultrathink"
    return None


def _has_word(lowered: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", lowered) is not None


@dataclass(frozen=True)
class KeywordEffort:
    """关键词命中后的处置：抬到哪一档、要不要给模型递一句提醒。"""

    effort: str | None
    reminder: str | None = None


def keyword_effort(text: object) -> KeywordEffort:
    """对标上游 xni / Cni：ultrathink 要更深的推理，ultracode 要多代理编排。

    两个关键词都把本轮抬到 :data:`KEYWORD_LEVEL`；认不出关键词时返回空，
    调用方按原样继续——关键词是「加码」，不是「唯一来源」。
    """
    keyword = detect_keyword(text)
    if keyword is None:
        return KeywordEffort(None)
    if keyword == "ultrathink":
        return KeywordEffort(KEYWORD_LEVEL, ULTRATHINK_REMINDER)
    return KeywordEffort(KEYWORD_LEVEL, ULTRACODE_REMINDER)


# ---- 封顶策略（上游 j / hct / Vwe / FJ / _1 / K / zAr / WAr）-----------------


def _norm_cap(value: object) -> str | None:
    """把封顶值归一：``max`` 表示不封顶，认不出的值当作没配。"""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    raw = value.strip().lower()
    if raw == "max":
        return None
    return raw if is_level(raw) else None


@dataclass(frozen=True)
class EffortPolicy:
    """一次运行的 effort 封顶策略。

    ``model_caps`` 是模型名 → 该模型的 ``maxEffortLevel``（对标上游 j(e) 的
    模型族映射）；``provider_cap`` 是供应商侧上限；``default_cap`` 是两者都没
    配时的兜底。三者都没配就是**不封顶**——第三方模型没有上游那套族表，
    凭空给一个上限等于替模型编能力。

    ``no_xhigh`` / ``no_max`` 对标上游的 gV / y1：模型本身支不支持这两档。
    它只决定「被压下来算不算损失」，不参与封顶计算。
    """

    model_caps: Mapping[str, str] = field(default_factory=dict)
    provider_cap: str | None = None
    default_cap: str | None = None
    no_xhigh: frozenset[str] = frozenset()
    no_max: frozenset[str] = frozenset()

    def family_cap(self, model: str) -> str | None:
        """上游 j(e)：模型自身的上限。"""
        return _norm_cap(self.model_caps.get(model, self.default_cap))

    def effective_cap(self, model: str) -> str | None:
        """上游 hct(e)：模型上限与供应商上限取更严的一个；``max`` 视为不封顶。"""
        family = self.family_cap(model)
        provider = _norm_cap(self.provider_cap)
        if family is None:
            return provider
        if provider is None:
            return family
        return family if rank(family) <= rank(provider) else provider

    def allows(self, level: str, model: str) -> bool:
        """上游 Vwe：这一档在模型上能不能用。"""
        cap = self.effective_cap(model)
        return cap is None or rank(level) <= rank(cap)

    def allowed_levels(self, model: str) -> tuple[str, ...]:
        """上游 FJ：这个模型全部可用的档位，按词表顺序。"""
        return tuple(level for level in EFFORT_LEVELS if self.allows(level, model))

    def clamp(self, level: str, model: str) -> str:
        """上游 _1：超过封顶就压到封顶，否则原样。"""
        cap = self.effective_cap(model)
        if cap is not None and rank(level) > rank(cap):
            return cap
        return level

    def supports_xhigh(self, model: str) -> bool:
        """上游 gV：模型本身用得上 xhigh 吗。"""
        return model not in self.no_xhigh

    def supports_max(self, model: str) -> bool:
        """上游 y1：模型本身用得上 max 吗。"""
        return model not in self.no_max

    def is_desirable_above_cap(self, level: str, model: str) -> bool:
        """上游 K：这一档被压下来，算不算丢掉了模型本可用的能力。"""
        if level == "xhigh":
            return self.supports_xhigh(model)
        if level == "max":
            return self.supports_max(model)
        return True

    def has_desirable_above_cap(self, model: str) -> bool:
        """上游 zAr：封顶之上是否还存在模型本可用的档位。"""
        cap = self.effective_cap(model)
        if cap is None:
            return False
        return any(
            self.is_desirable_above_cap(level, model)
            for level in EFFORT_LEVELS
            if rank(level) > rank(cap)
        )

    def warning(self, level: object, model: str) -> str | None:
        """上游 WAr：静默降级时给人看的那句话；没有降级就不说。"""
        if not is_level(level):
            return None
        cap = self.effective_cap(model)
        if cap is None or rank(level) <= rank(cap):
            return None
        return (
            f"Effort '{level}' exceeds the cap for {model} set by your settings or "
            f"organization; using '{self.clamp(level, model)}'."
        )

    def fallback_level(self, model: str) -> str:
        """上游 fDe：没有指定档位时，这个模型该用哪一档。"""
        wanted = DEFAULT_EFFORT
        if self.supports_max(model) and self.allows("max", model):
            wanted = "max"
        return self.clamp(wanted, model)


# ---- thinking 侧（上游 JWn / lxr 与 output_config.effort 的钳制）------------


@dataclass(frozen=True)
class ThinkingConfig:
    """本轮的思考配置（对标上游的 thinking / output_config.thinking 两处）。

    ``mode``：``auto`` 由模型自己决定想多久，``extended`` 明确要更长的思考，
    ``off`` 完全关闭。``budget_tokens`` 为 0 表示用模型上限。
    """

    mode: str = "auto"
    budget_tokens: int = 0

    @property
    def disabled(self) -> bool:
        return self.mode == "off"

    @classmethod
    def parse(cls, value: object) -> "ThinkingConfig":
        """认 ``{"type": "enabled"|"disabled"|"adaptive", "budget_tokens": n}`` 与
        ``"off"`` / ``"auto"`` / ``"extended"`` 两种写法；认不出按 auto 处理。
        """
        if value is None or value == "":
            return cls()
        if isinstance(value, str):
            mode = value.strip().lower()
            return cls(mode=mode if mode in THINKING_MODES else "auto")
        if isinstance(value, Mapping):
            kind = str(value.get("type") or "").strip().lower()
            if kind in {"disabled", "off"}:
                return cls(mode="off")
            if kind == "adaptive":
                return cls(mode="auto")
            if kind in {"enabled", "extended"}:
                return cls(mode="extended", budget_tokens=int(value.get("budget_tokens") or 0))
            mode = str(value.get("mode") or "").strip().lower()
            return cls(mode=mode if mode in THINKING_MODES else "auto",
                       budget_tokens=int(value.get("budget_tokens") or 0))
        return cls()


def clamp_without_thinking(
    effort: str,
    *,
    model: str,
    policy: EffortPolicy,
    thinking: ThinkingConfig,
    model_rejects_higher_without_thinking: bool = True,
) -> tuple[str, str | None]:
    """thinking 被关时的回落（上游 output_config.effort 那一段）。

    只有「thinking 确实关了」「这个模型在 thinking 关闭时拒绝高档」且
    「请求的档位高于 high」三者同时成立才压；压了就返回一句可读的说明，
    否则调用方无从知道档位被改过。
    """
    if not thinking.disabled:
        return effort, None
    if not model_rejects_higher_without_thinking:
        return effort, None
    if not exceeds_default(effort):
        return effort, None
    return (
        THINKING_DISABLED_EFFORT_CAP,
        f"output_config.effort '{effort}' clamped to '{THINKING_DISABLED_EFFORT_CAP}': "
        "thinking is disabled for this request, and this model rejects higher effort "
        "when thinking is disabled",
    )


def thinking_budget_ceiling(max_output_tokens: int) -> int:
    """上游 lxr(e)：思考预算的天花板是模型输出上限减一。"""
    return max(0, int(max_output_tokens) - 1)


def resolve_turn_effort(
    requested: object,
    *,
    model: str,
    policy: EffortPolicy,
    thinking: ThinkingConfig,
    keyword_text: str = "",
    model_rejects_higher_without_thinking: bool = True,
) -> "TurnEffort":
    """一轮的完整裁决：关键词 → 请求档位 → 封顶 → thinking 关闭回落。

    顺序不能换：关键词是「用户这一轮明确要加码」，先认它，再让封顶与
    thinking 关断去压；反过来的话，压完再认关键词，关键词就被模型封顶
    悄悄吃掉了，而用户看不出任何提示。
    """
    keyword = keyword_effort(keyword_text)
    level = keyword.effort or parse_level(requested) or policy.fallback_level(model)
    clamped = policy.clamp(level, model)
    warning = policy.warning(level, model) if clamped != level else None
    clamped, thinking_warning = clamp_without_thinking(
        clamped,
        model=model,
        policy=policy,
        thinking=thinking,
        model_rejects_higher_without_thinking=model_rejects_higher_without_thinking,
    )
    if thinking_warning is not None:
        warning = thinking_warning
    return TurnEffort(
        level=clamped,
        requested=level,
        warning=warning,
        reminder=keyword.reminder,
        from_keyword=keyword.effort is not None,
        allowed=policy.allowed_levels(model),
    )


@dataclass(frozen=True)
class TurnEffort:
    """一轮裁决的结果：实际用的档位、被压之前的档位、以及要递给人/模型的话。"""

    level: str
    requested: str
    warning: str | None = None
    reminder: str | None = None
    from_keyword: bool = False
    allowed: tuple[str, ...] = EFFORT_LEVELS

    @property
    def clamped(self) -> bool:
        return self.level != self.requested

    @property
    def is_xhigh(self) -> bool:
        """上游 TC：本轮是不是 xhigh（决定要不要挂「深度推理」那条提醒）。"""
        return self.level == "xhigh"

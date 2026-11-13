"""effort 批的可失败判据（#277）。

钉的不是「函数存在」——那用 ``hasattr`` 也能过。每条判据都指向一个**会走形的具体行为**：
档位词表被改、封顶取反、钳制不压、关键词被吞、占位符不替换、环境键不写、adapter
载荷不下发，对应那条就红。改前（没接 effort 时）这些用例要么 ImportError、要么断言失败。

覆盖七块：档位词表 / 封顶 / 钳制 / 关键词升档 / ``${CLAUDE_EFFORT}`` /
``CLAUDE_EFFORT`` 环境变量 / adapter thinking 载荷。
"""

from __future__ import annotations

from blank_api.services.effort import (
    DEFAULT_EFFORT,
    EFFORT_LEVELS,
    KEYWORD_EFFORTS,
    KEYWORD_LEVEL,
    LEVEL_ALIASES,
    THINKING_DISABLED_EFFORT_CAP,
    ULTRACODE_REMINDER,
    ULTRATHINK_REMINDER,
    EffortPolicy,
    ThinkingConfig,
    clamp_without_thinking,
    detect_keyword,
    effort_for_prompt,
    exceeds_default,
    is_level,
    keyword_effort,
    parse,
    parse_level,
    rank,
    resolve_turn_effort,
    thinking_budget_ceiling,
)
from blank_api.services.prompt_args import render_prompt, substitute_paths
from blank_api.services.shell_env import CLAUDE_EFFORT_ENV, apply_effort


# ---- 档位词表 -------------------------------------------------------------


def test_level_vocabulary_is_the_five_canonical_levels_in_rank_order() -> None:
    """词表就是这五档，且 ``rank`` 严格随词表位置递增。

    词表被塞进第六档、或 ``rank`` 与词表顺序脱节（``rank("max") < rank("xhigh")``），
    后面所有封顶/钳制/升档都按错的尺子量，这条先红。
    """
    assert EFFORT_LEVELS == ("low", "medium", "high", "xhigh", "max"), EFFORT_LEVELS
    assert [rank(level) for level in EFFORT_LEVELS] == [1, 2, 3, 4, 5]


def test_is_level_only_accepts_vocabulary_members() -> None:
    """``is_level`` 认词表内、拒词表外——认不出的值不能被当成有效档位。

    把 ``is_level`` 放宽成「非空字符串就算」，封顶就压不住编造的档位。
    """
    assert is_level("low") and is_level("max")
    assert not is_level("ultra") and not is_level("") and not is_level(None)


def test_parse_level_is_case_and_whitespace_insensitive_and_honors_alias() -> None:
    """``parse_level`` 大小写/空白不敏感，认 ``med`` 这类别名，认不出返回 None。

    去掉 ``.strip().lower()``，``" XHIGH "`` 就认不出；去掉别名映射，``"med"`` 落到 None。
    """
    assert parse_level("XHIGH") == "xhigh"
    assert parse_level("  Medium ") == "medium"
    assert parse_level("med") == "medium"
    assert LEVEL_ALIASES == {"med": "medium"}
    assert parse_level("ultra") is None
    assert parse_level(None) is None
    assert parse_level(123) is None


def test_parse_reports_unknown_value_with_valid_vocabulary_in_warning() -> None:
    """``parse`` 认不出值时给一条带词表的告警，不静默吞掉。

    告警里不带有效档位，用户就不知道能填什么；认不出还返回档位，等于替用户编值。
    """
    result = parse("turbo")
    assert result.level is None
    assert result.warning is not None
    assert "turbo" in result.warning
    for level in EFFORT_LEVELS:
        assert level in result.warning


# ---- 封顶 ---------------------------------------------------------------


def test_family_cap_reads_model_table_then_falls_back_to_default() -> None:
    """模型表里有就取模型的，没有就取默认兜底；``max`` 归一成不封顶。

    把 ``model_caps.get`` 的兜底链改错（不认 default_cap），未登记模型就凭空不封顶；
    ``_norm_cap`` 不把 ``max`` 归一成 None，``max`` 会被当成一个真实封顶档。
    """
    policy = EffortPolicy(model_caps={"m1": "high", "m2": "max"}, default_cap="medium")
    assert policy.family_cap("m1") == "high"
    assert policy.family_cap("m2") is None  # "max" = 不封顶
    assert policy.family_cap("unknown-model") == "medium"


def test_effective_cap_takes_stricter_of_family_and_provider() -> None:
    """模型上限与供应商上限取更严的一个；任一侧没配就取另一侧。

    取成「更松」或取成「只认一侧」，封顶方向就反了或漏了一侧。
    """
    policy = EffortPolicy(model_caps={"m": "xhigh"}, provider_cap="high")
    assert policy.effective_cap("m") == "high"  # provider 更严
    policy = EffortPolicy(model_caps={"m": "high"}, provider_cap="xhigh")
    assert policy.effective_cap("m") == "high"  # family 更严
    policy = EffortPolicy(model_caps={"m": "xhigh"}, provider_cap="max")
    assert policy.effective_cap("m") == "xhigh"  # provider 不封顶，取 family
    policy = EffortPolicy(model_caps={"m": "max"}, provider_cap="high")
    assert policy.effective_cap("m") == "high"  # family 不封顶，取 provider


def test_allows_and_allowed_levels_follow_the_effective_cap() -> None:
    """``allows`` 按封顶判档位可否，``allowed_levels`` 按词表顺序列全可用档。

    ``allows`` 取反，封顶之上的档会被放行；``allowed_levels`` 不按词表顺序，
    调用方拿到的档位顺序就乱了。
    """
    policy = EffortPolicy(provider_cap="high")
    assert policy.allows("high", "m") is True
    assert policy.allows("xhigh", "m") is False
    assert policy.allows("max", "m") is False
    assert policy.allowed_levels("m") == ("low", "medium", "high")
    open_policy = EffortPolicy()
    assert open_policy.allowed_levels("m") == EFFORT_LEVELS


# ---- 钳制 ---------------------------------------------------------------


def test_clamp_pushes_down_to_cap_and_leaves_below_unchanged() -> None:
    """超过封顶压到封顶，没超原样返回——压过头或不压都算走形。

    ``clamp`` 把低于封顶的也压下去，等于凭空削模型能力；不压，封顶形同虚设。
    """
    policy = EffortPolicy(provider_cap="high")
    assert policy.clamp("max", "m") == "high"
    assert policy.clamp("xhigh", "m") == "high"
    assert policy.clamp("high", "m") == "high"
    assert policy.clamp("medium", "m") == "medium"
    open_policy = EffortPolicy()
    assert open_policy.clamp("max", "m") == "max"


def test_warning_is_none_when_not_clamped_and_names_model_cap_level() -> None:
    """静默降级时给人看的那句话：没降级就不说，降级了要点名模型、原档、封顶。

    没压也给告警，用户被无关提示轰炸；压了不给告警，用户看不出档位被改过。
    """
    policy = EffortPolicy(provider_cap="high")
    assert policy.warning("high", "m") is None
    assert policy.warning("medium", "m") is None
    warning = policy.warning("max", "m")
    assert warning is not None
    assert "max" in warning and "m" in warning and "high" in warning


def test_fallback_level_prefers_max_when_supported_and_allowed() -> None:
    """没指定档位时的兜底：模型支持 max 且封顶放行就用 max，否则默认档再钳制。

    不认 ``supports_max``/``allows("max")``，兜底永远停在 high，模型白给的 max 用不上；
    兜底不做钳制，模型不支持 max 时反而发一个 max 出去。
    """
    policy = EffortPolicy()
    assert policy.fallback_level("m") == "max"  # 支持且不封顶
    policy = EffortPolicy(provider_cap="high")
    assert policy.fallback_level("m") == "high"  # max 被封顶压下来
    policy = EffortPolicy(no_max=frozenset({"m"}))
    assert policy.fallback_level("m") == DEFAULT_EFFORT  # 模型本身不支持 max


def test_has_desirable_above_cap_tracks_model_capability_not_just_cap() -> None:
    """封顶之上是否还有「模型本可用」的档：``no_xhigh``/``no_max`` 决定算不算损失。

    cap=high 时 xhigh 与 max 都在封顶之上。``no_xhigh`` 只抹掉 xhigh，max 仍算模型本可用，
    所以仍有损失；两个都不支持才判「封顶之上无损失」。忽略这两个集合，模型本来就不支持的
    档会被算成「被压掉的能力」，告警与降级判断都偏离上游语义。
    """
    policy = EffortPolicy(provider_cap="high", no_xhigh=frozenset({"m"}))
    assert policy.has_desirable_above_cap("m") is True  # max 仍在封顶之上且模型支持
    policy = EffortPolicy(provider_cap="high", no_xhigh=frozenset({"m"}), no_max=frozenset({"m"}))
    assert policy.has_desirable_above_cap("m") is False  # 两个都不支持，封顶之上无损失
    policy = EffortPolicy(provider_cap="high")
    assert policy.has_desirable_above_cap("m") is True  # xhigh/max 模型本都支持
    open_policy = EffortPolicy()
    assert open_policy.has_desirable_above_cap("m") is False  # 不封顶就没有「之上」


# ---- 关键词升档 ----------------------------------------------------------


def test_keyword_effort_raises_to_xhigh_with_the_right_reminder() -> None:
    """ultrathink/ultracode 都把本轮抬到 xhigh，差别只在提醒文案。

    关键词不抬档，用户写 ``ultrathink`` 等于没写；两个关键词用错提醒，模型被引导向
    错误的编排方式。
    """
    think = keyword_effort("请用 ultrathink 想这道题")
    assert think.effort == KEYWORD_LEVEL == "xhigh"
    assert think.reminder == ULTRATHINK_REMINDER
    code = keyword_effort("ultracode 跑一遍")
    assert code.effort == "xhigh"
    assert code.reminder == ULTRACODE_REMINDER
    assert KEYWORD_EFFORTS == {"ultracode": "xhigh"}


def test_detect_keyword_matches_word_boundaries_not_substrings() -> None:
    """关键词按词边界找：正文里出现就认，但 ``ultrathinking`` 这种子串不认。

    不做词边界，``ultrathinking``、``ultrathinkx`` 会被误判成关键词；只认整条文本，
    用户说一句「请用 ultrathink 想」就漏了。
    """
    assert detect_keyword("think with ultrathink here") == "ultrathink"
    assert detect_keyword("ultracode now") == "ultracode"
    assert detect_keyword("ultrathinking is a word") is None
    assert detect_keyword("") is None
    assert detect_keyword(None) is None


def test_resolve_turn_effort_keyword_beats_request_then_gets_clamped() -> None:
    """裁决顺序：关键词先认，再让封顶压——压了要给告警，不能把关键词悄悄吃掉。

    顺序反过来（先压再认关键词），关键词抬的 xhigh 会被封顶压掉而用户看不出提示；
    压了不给告警，静默降级用户无从察觉。
    """
    policy = EffortPolicy(provider_cap="high")
    result = resolve_turn_effort(
        "medium",
        model="m",
        policy=policy,
        thinking=ThinkingConfig(),
        keyword_text="用 ultrathink 想",
    )
    assert result.from_keyword is True
    assert result.requested == "xhigh"  # 关键词先认
    assert result.level == "high"  # 再被封顶压下来
    assert result.clamped is True
    assert result.warning is not None  # 压了要说话
    assert result.reminder == ULTRATHINK_REMINDER


# ---- ${CLAUDE_EFFORT} ----------------------------------------------------


def test_substitute_paths_replaces_claude_effort_placeholder() -> None:
    """``${CLAUDE_EFFORT}`` 换成本轮生效档位；没给档位就一个字符都不动。

    不替换，Skill/命令正文里写着 ``${CLAUDE_EFFORT}`` 原样发给模型；给了空档也替换，
    等于把占位符抹成空串。
    """
    assert substitute_paths("think at ${CLAUDE_EFFORT}", effort="xhigh") == "think at xhigh"
    assert substitute_paths("think at ${CLAUDE_EFFORT}", effort="") == "think at ${CLAUDE_EFFORT}"
    assert substitute_paths("no placeholder", effort="xhigh") == "no placeholder"


def test_effort_for_prompt_uses_declared_level_or_default() -> None:
    """``${CLAUDE_EFFORT}`` 的替换值：声明的档位认，认不出回落默认，不做模型封顶。

    这里做封顶钳制，展开正文时手上还没有模型能力，无从按模型封顶；认不出还硬填一个
    档位，等于替声明方编值。
    """
    assert effort_for_prompt("low") == "low"
    assert effort_for_prompt("xhigh") == "xhigh"
    assert effort_for_prompt("turbo") == DEFAULT_EFFORT
    assert effort_for_prompt(None) == DEFAULT_EFFORT


def test_render_prompt_carries_effort_through_to_placeholder() -> None:
    """``render_prompt`` 把 effort 一路带到最终正文——占位符展开不能丢这一项。

    ``render_prompt`` 不把 effort 透传给 ``substitute_paths``，命令/Skill 正文里的
    ``${CLAUDE_EFFORT}`` 就替换不掉。
    """
    text = render_prompt("level=${CLAUDE_EFFORT}", args=None, effort="max")
    assert text == "level=max"


# ---- CLAUDE_EFFORT 环境变量 ----------------------------------------------


def test_apply_effort_writes_key_only_when_a_level_was_resolved() -> None:
    """有档位才写 ``CLAUDE_EFFORT``；没档位一个键都不加，不写空串、不留上轮残留。

    没档位也写键（空串或上轮值），钩子/命令里读到的就是假档位；键名写错，钩子脚本
    里 ``$CLAUDE_EFFORT`` 读不到。
    """
    env = apply_effort({"PATH": "/usr/bin"}, "xhigh")
    assert env[CLAUDE_EFFORT_ENV] == "xhigh"
    assert env["PATH"] == "/usr/bin"  # 不破坏原有环境
    env = apply_effort({"PATH": "/usr/bin"}, "")
    assert CLAUDE_EFFORT_ENV not in env  # 没档位就不给键
    env = apply_effort({}, "  ")
    assert CLAUDE_EFFORT_ENV not in env  # 空白也不算档位


# ---- adapter thinking 载荷 ----------------------------------------------


def _anthropic_adapter(model: str = "claude-x", **overrides):
    from blank_api.services.models.base import AdapterConfig
    from blank_api.services.models.anthropic import AnthropicAdapter

    config = AdapterConfig(
        name="test-anthropic",
        kind="anthropic",
        base_url="http://localhost",
        api_key="",
        model=model,
        **overrides,
    )
    return AnthropicAdapter(config)


def _openai_compat_adapter(model: str = "o3", **overrides):
    from blank_api.services.models.base import AdapterConfig
    from blank_api.services.models.openai_compat import OpenAICompatibleAdapter

    config = AdapterConfig(
        name="test-compat",
        kind="openai",
        base_url="http://localhost",
        api_key="",
        model=model,
        **overrides,
    )
    return OpenAICompatibleAdapter(config)


def _openai_responses_adapter(model: str = "gpt-5", **overrides):
    from blank_api.services.models.base import AdapterConfig
    from blank_api.services.models.openai_responses import OpenAIResponsesAdapter

    config = AdapterConfig(
        name="test-responses",
        kind="openai_responses",
        base_url="http://localhost",
        api_key="",
        model=model,
        **overrides,
    )
    return OpenAIResponsesAdapter(config)


def test_anthropic_payload_emits_output_config_effort_and_thinking() -> None:
    """Anthropic 适配器：有档位下发 ``output_config.effort``，有 thinking 下发 ``thinking``。

    空档也下发 ``output_config``，等于给供应商塞一个空值；thinking 载荷不下发，
    扩展思考的开关与预算就丢了。
    """
    adapter = _anthropic_adapter()
    payload = adapter._payload([], None, False, effort="xhigh", thinking={"type": "enabled", "budget_tokens": 1024})
    assert payload["output_config"] == {"effort": "xhigh"}
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    plain = adapter._payload([], None, False)
    assert "output_config" not in plain
    assert "thinking" not in plain


def test_openai_compat_maps_effort_to_reasoning_effort_clamping_above_high() -> None:
    """OpenAI 兼容适配器：档位映射到 ``reasoning_effort``，xhigh/max 收口到 high。

    不收口，xhigh/max 不在 OpenAI 词汇表里会被供应商拒；空档不回落配置静态值，
    配置里写的那份就丢了。
    """
    adapter = _openai_compat_adapter(reasoning_effort="medium")
    assert adapter._payload([], None, False, effort="xhigh")["reasoning_effort"] == "high"
    assert adapter._payload([], None, False, effort="max")["reasoning_effort"] == "high"
    assert adapter._payload([], None, False, effort="low")["reasoning_effort"] == "low"
    assert adapter._payload([], None, False, effort="")["reasoning_effort"] == "medium"  # 回落配置


def test_openai_responses_maps_effort_to_reasoning_with_encrypted_include() -> None:
    """Responses 适配器：档位映射到 ``reasoning.effort``，附带加密推理内容，且不下发 temperature。

    xhigh/max 不收口到 high，不在 Responses 词汇表里会 400；不挂 ``include``，
    推理内容不回传；推理模型还带 temperature 也会 400。
    """
    adapter = _openai_responses_adapter(reasoning_effort="low")
    payload = adapter._payload([], None, False, effort="xhigh")
    assert payload["reasoning"] == {"effort": "high"}
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert "temperature" not in payload
    payload = adapter._payload([], None, False, effort="medium")
    assert payload["reasoning"] == {"effort": "medium"}
    assert payload["include"] == ["reasoning.encrypted_content"]
    payload = adapter._payload([], None, False, effort="")
    assert payload["reasoning"] == {"effort": "low"}  # 回落配置静态值


# ---- thinking 关闭回落（钳制的另一支）------------------------------------


def test_clamp_without_thinking_only_clamps_when_all_three_conditions_hold() -> None:
    """thinking 关闭回落：三条件同时成立才压，压了给说明，否则原样。

    少判任一条（thinking 没关也压、模型不拒高档也压、没超 high 也压），回落就过头；
    压了不给说明，调用方无从知道档位被改。
    """
    policy = EffortPolicy()
    thinking = ThinkingConfig(mode="off")
    level, warning = clamp_without_thinking("max", model="m", policy=policy, thinking=thinking)
    assert level == THINKING_DISABLED_EFFORT_CAP == "high"
    assert warning is not None and "thinking is disabled" in warning

    # thinking 没关：不压
    level, warning = clamp_without_thinking(
        "max", model="m", policy=policy, thinking=ThinkingConfig(mode="auto")
    )
    assert level == "max" and warning is None

    # 模型不拒高档：不压
    level, warning = clamp_without_thinking(
        "max", model="m", policy=policy, thinking=thinking,
        model_rejects_higher_without_thinking=False,
    )
    assert level == "max" and warning is None

    # 没超 high：不压
    level, warning = clamp_without_thinking(
        "high", model="m", policy=policy, thinking=thinking
    )
    assert level == "high" and warning is None


def test_thinking_budget_ceiling_is_max_output_tokens_minus_one() -> None:
    """思考预算天花板是模型输出上限减一——不减一，思考会吃掉全部输出空间。

    不减一或取负数不兜底，预算与输出上限冲突，供应商会拒。
    """
    assert thinking_budget_ceiling(2048) == 2047
    assert thinking_budget_ceiling(1) == 0
    assert thinking_budget_ceiling(0) == 0  # 不产负数


def test_thinking_config_parse_recognizes_dict_and_string_forms() -> None:
    """``ThinkingConfig.parse`` 认字典与字符串两种写法，认不出按 auto。

    ``{"type": "disabled"}`` 不认成 off，thinking 关断回落就永远不触发；字符串认不出
    不回落 auto，会当成一个无效模式传下去。
    """
    assert ThinkingConfig.parse({"type": "disabled"}).disabled is True
    assert ThinkingConfig.parse({"type": "enabled", "budget_tokens": 512}).mode == "extended"
    assert ThinkingConfig.parse({"type": "adaptive"}).mode == "auto"
    assert ThinkingConfig.parse("off").disabled is True
    assert ThinkingConfig.parse("extended").mode == "extended"
    assert ThinkingConfig.parse("bogus").mode == "auto"
    assert ThinkingConfig.parse(None).mode == "auto"


def test_exceeds_default_only_true_for_levels_above_high() -> None:
    """``exceeds_default`` 只在档位高于默认档（high）时为真——它是回落的闸门。

    把 high 也算成「超过默认」，thinking 关闭时连 high 都会被压，回落过头。
    """
    assert exceeds_default("xhigh") is True
    assert exceeds_default("max") is True
    assert exceeds_default("high") is False
    assert exceeds_default("medium") is False
    assert exceeds_default("ultra") is False
    assert exceeds_default(None) is False

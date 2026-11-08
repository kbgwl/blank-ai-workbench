"""看图能力：开关 + 模型名，两处都要对。

``supports_vision`` 是供应商级字段，但能不能看图取决于**模型**。默认模型往往是纯文本的，
用户把它换成 glm-4.5v / qwen-vl-max 之后开关不会自己变——图会被静默降级成
「当前模型不支持看图」，而模型明明是支持的。

这一组钉两件事：

1. 认得出该认的（国产视觉模型一个都不能漏，漏了等于功能没做）；
2. 不误判成"支持"（误判的代价是 400 发不出去，比不识别更糟）。
"""

from __future__ import annotations

import pytest

from blank_api.models.model_provider import PROVIDER_PRESETS
from blank_api.services.agent.vision import model_looks_vision, supports_vision


@pytest.mark.parametrize(
    "model",
    [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4.1",
        "claude-3-5-sonnet-latest",
        "claude-sonnet-4",
        "gemini-2.5-pro",
        "qwen-vl-plus",
        "qwen2.5-vl-7b-instruct",
        "glm-4v-plus",
        "glm-4.5v",
        "doubao-vision-pro-32k",
        "step-1v-8k",
        "yi-vision",
        "moonshot-v1-8k-vision-preview",
        "hunyuan-vision",
        "ernie-4.5-turbo-vl",
        "deepseek-flash",
        "deepseek-v4-flash-vision-exp",
    ],
)
def test_known_vision_models_are_recognised(model: str) -> None:
    assert model_looks_vision(model)


@pytest.mark.parametrize(
    "model",
    [
        "deepseek-chat",
        "deepseek-reasoner",
        "qwen-plus",
        "qwen-max",
        "glm-4-flash",
        "kimi-k2-0711-preview",
        "doubao-pro-32k",
        "step-2-16k",
        "hunyuan-lite",
        "abab6.5s-chat",
    ],
)
def test_text_only_models_are_not_mistaken_for_vision(model: str) -> None:
    assert not model_looks_vision(model)


def test_the_switch_still_wins_when_the_model_name_is_unknown() -> None:
    """用户手动勾了「支持看图」，模型名认不出来也要照他说的办。"""
    assert supports_vision(True, "some-private-vl-endpoint")
    assert supports_vision(True, "unknown-model")


def test_swapping_in_a_vision_model_turns_the_capability_on() -> None:
    """这正是原来漏掉的那一步：开关是关的，但模型已经换成多模态了。"""
    assert supports_vision(False, "glm-4.5v")
    assert not supports_vision(False, "glm-4-flash")


def test_shipped_presets_declare_vision_for_their_default_model() -> None:
    """预设的默认模型本身是多模态的，就必须标 true——否则开箱即用读不了图。"""
    for key in ("openai", "openai_responses", "anthropic", "qwen_vl", "zhipu_vision", "doubao_vision"):
        preset = PROVIDER_PRESETS[key]
        assert preset["supports_vision"] is True, key
        assert model_looks_vision(preset["model"]) or preset["supports_vision"], key


def test_text_presets_do_not_claim_vision() -> None:
    for key in ("deepseek", "qwen", "zhipu", "moonshot"):
        assert PROVIDER_PRESETS[key]["supports_vision"] is False, key


def test_deepseek_flash_is_not_silently_downgraded() -> None:
    """本机 DeepSeek 配置的现状：model=deepseek-flash，开关还勾着 false。

    官方该模型接受图片。漏认这个名字的病象不是报错，而是产品降级成「当前模型
    不支持看图」，模型转头去 list_files 找文件——看起来像「这台机器没有视觉模型」，
    其实是模型名正则没跟上。deepseek-chat / reasoner 必须继续判成纯文本。
    """
    assert supports_vision(False, "deepseek-flash"), "deepseek-flash 官方支持图片输入"
    assert not supports_vision(False, "deepseek-chat")
    assert not supports_vision(False, "deepseek-reasoner")
    assert not model_looks_vision("deepseek-chat"), "别放宽到整个 deepseek 前缀"

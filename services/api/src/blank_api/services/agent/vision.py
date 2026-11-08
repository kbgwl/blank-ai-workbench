"""能不能看图：provider 的声明 + 模型名判定。

``supports_vision`` 是**供应商**级字段，但能不能看图其实取决于**模型**。默认模型
往往是纯文本的（glm-4-flash、qwen-plus、deepseek-chat），用户把模型名换成
glm-4.5v / qwen-vl-max 之后，那个开关不会自己变——结果就是图被静默降级成
「当前模型不支持看图」，而模型明明是支持的。

所以这里做一次加法：声明为真就算真；声明为假但模型名明显是多模态的，也算真。

**只做加法，不做减法。** 用户明明勾了「支持看图」却被一个正则推翻，代价比不识别更大：
前者是"功能白勾了"，后者最多是一次 400，而 400 会立刻告诉他哪里不对。
"""

from __future__ import annotations

import re

# 只列明确的特征串。认不出来就交给声明，不猜。
_VISION_MODEL = re.compile(
    r"(?:"
    r"gpt-4o|gpt-4\.1|gpt-4-turbo|gpt-5|o4-mini"  # OpenAI 多模态一线
    r"|claude-3|claude-4|claude-sonnet|claude-opus|claude-haiku"  # Claude 3 起都支持
    r"|gemini"
    r"|[-_]vl\b|vl-\d|qwen.*vl"  # Qwen-VL / 任何 *-vl 变体
    r"|glm-4v|glm-4\.\dv|glm-4\.\d-v"  # 智谱视觉
    # DeepSeek 当前多模态模型叫 deepseek-flash（官方 vision 文档，2026-11-08 核实）：
    # 名字里既没有 vision 也没有 vl，但官方明确说它接受图片。legacy 名
    # deepseek-v4-flash-vision-exp 由下面的 |vision 接住，两者都认。
    # 注意 deepseek-chat / deepseek-reasoner 仍是纯文本，别放宽到 deepseek 前缀。
    r"|deepseek-flash"
    r"|vision"  # doubao-vision / yi-vision / moonshot-*-vision / legacy deepseek-*-vision-exp
    r"|kimi-latest|kimi-vl"
    r"|hunyuan.*vision|step-1[vo]|step-2-vision"
    r"|ernie.*vl|yi-vl|internvl|llava|minicpm-v"
    r")",
    re.IGNORECASE,
)


def model_looks_vision(model: str) -> bool:
    """模型名看起来是多模态的吗。"""
    return bool(_VISION_MODEL.search(str(model or "")))


def supports_vision(declared: bool, model: str) -> bool:
    """本次运行能不能给模型塞图。"""
    return bool(declared) or model_looks_vision(model)

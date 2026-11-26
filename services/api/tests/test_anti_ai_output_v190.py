"""所有产出物都要去 AI 味：文档 / 网页 / PPT / 报告，不只聊天回复。

判据放在两个地方：

1. 风格段必须有可执行的禁令（套模板、空泛开场、流行词、编数据、满篇 bullet、
   emoji、AI 仪表盘、三栏图标 PPT）；
2. 编程与日常两个场景的系统提示都要带上这一段——日常生成的文档/PPT 不能漏。
"""

from __future__ import annotations

from blank_api.services.agent.graph import STYLE_PROMPT, compose_system_prompt


def test_style_prompt_bans_ai_slop_in_artifacts() -> None:
    for marker in (
        "产出物风格",
        "背景-意义-目标-挑战-方案-展望",
        "项目概况",
        "赋能、抓手、闭环",
        "不编数据",
        "每句话都拆成 bullet",
        "不用 emoji",
        "KPI 卡片墙",
        "PPT 一页一个观点",
        "图片和图表自己判断要不要加",
        "不要问用户「要不要加图」",
        "不编造截图",
        "不留「此处配图」占位",
        "希望本文对你有所帮助",
        "本函数用于",
    ):
        assert marker in STYLE_PROMPT, marker


def test_both_scenes_carry_the_artifact_style() -> None:
    for scene in ("coding", "daily"):
        prompt = compose_system_prompt(None, scene=scene)
        assert "产出物风格" in prompt, scene
        assert "PPT 一页一个观点" in prompt, scene
        assert "KPI 卡片墙" in prompt, scene

"""v401：两个场景（编程 / 日常）+ read_file 读图。

产品只做两个场景。这条边界要钉住的是：

1. 日常问答**不背**编程纪律。让"解释一下闭包"背着写盘闸门、测试验证流程去答，
   既费 token 又会把闲聊答成交付报告。首轮规则判明是答案类时换精简提示。
2. 判不准时**必须**走编程那套。宁可多花 token，也不能让"以为在闲聊、其实要改代码"
   的请求丢掉执行约束——这是这个开关唯一的风险方向，所以只往保守一侧默认。
3. read_file 遇到图片要给模型真的能看到图。Claude 的 Read 就是这个形状；
   我们此前直接报"二进制无法读取"，等于把看设计稿、看报错截图这类活判死。
4. 不支持视觉的模型必须降级成一句实话。塞 image part 进去是 400，
   假装没收到图则是让模型对着文件名瞎猜——两种都比"我看不到"更差。
"""

from __future__ import annotations

import base64
from pathlib import Path

from blank_api.services.agent.graph import DAILY_PROMPT, SYSTEM_PROMPT, compose_system_prompt
from blank_api.services.agent.intent import INTENT_ANSWER, INTENT_ACT, guess_intent
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.filesystem import (
    IMAGE_SUFFIXES,
    MAX_IMAGE_BYTES,
    _read_file,
    filesystem_tools,
)

# 1x1 的 PNG，够小又能被识别成真图片。
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _context(root: Path) -> ToolContext:
    return ToolContext(root=root, task_id="t", session_factory=None)  # type: ignore[arg-type]


def _write_png(root: Path, relative: str = "shot.png") -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_PNG)
    return target


# --------------------------------------------------------------------------- #
# 场景边界
# --------------------------------------------------------------------------- #


def test_daily_prompt_drops_the_coding_discipline() -> None:
    daily = compose_system_prompt(None, scene="daily")

    # 日常走 Codex 通用 agent 口径，不背 Claude 编程纪律那一整段（写盘闸门、
    # run_tests、工具纪律编号）——它讲的是 Claude 的编程流程，不是通用 agent 方法论。
    assert "写之前先读" not in daily
    assert "run_tests" not in daily
    assert "工具纪律" not in daily
    # Codex 方法论六块都在：Autonomy / 授权持久 / 进展沟通 / 技术表达 / 最终答案 / 工具纪律。
    assert "真正做完" in daily
    assert "把事做到完成" in daily
    assert "会话里已经授权过的动作不要再问第二遍" in daily
    assert "长任务中途给简短进展" in daily
    assert "先给结论和理由" in daily
    assert "引用真实文件写成" in daily
    assert "搜文件和搜内容优先用 rg" in daily


def test_daily_prompt_keeps_the_style_rules() -> None:
    daily = compose_system_prompt(None, scene="daily")

    # 风格是跨场景的：说人话、不写套话、给判断，两套提示都得有。
    assert "值得注意的是" in daily
    assert "不是 X，而是 Y" in daily


def test_coding_prompt_is_the_full_one() -> None:
    coding = compose_system_prompt(None, scene="coding")

    assert coding == compose_system_prompt(None)
    assert "工具纪律" in coding
    assert len(coding) > len(DAILY_PROMPT) * 3


def test_unknown_scene_falls_back_to_coding() -> None:
    # 拼错的场景名不能悄悄降级成日常——那会让编程任务丢掉执行约束。
    assert compose_system_prompt(None, scene="daily2") == compose_system_prompt(None, scene="coding")


def test_chitchat_is_predicted_as_answer_and_coding_work_is_not() -> None:
    # 这两个判据决定场景走哪套提示，判错方向比判错本身更贵。
    assert guess_intent("你好") == INTENT_ANSWER
    assert guess_intent("解释一下什么是闭包") == INTENT_ANSWER
    assert guess_intent("把 store.py 的抢占逻辑改成整条") == INTENT_ACT


def test_opinion_questions_are_not_treated_as_daily() -> None:
    # 「你觉得界面有什么能优化的」要只读看几处再答，属于编程侧，不能走日常精简提示。
    assert guess_intent("你觉得这个界面有什么可以优化的") != INTENT_ANSWER


# --------------------------------------------------------------------------- #
# read_file 读图
# --------------------------------------------------------------------------- #


def test_reading_a_png_returns_the_image(tmp_path: Path) -> None:
    _write_png(tmp_path)

    outcome = _read_as(_context(tmp_path), "shot.png")

    assert outcome.ok
    image = outcome.data["image"]
    assert image["media_type"] == "image/png"
    assert base64.b64decode(image["data"]) == _PNG
    # 回执要说清"图已经给你了"，否则模型会再想办法读一次。
    assert "已读取图片" in outcome.output


def test_every_supported_suffix_maps_to_a_media_type() -> None:
    assert IMAGE_SUFFIXES[".png"] == "image/png"
    assert IMAGE_SUFFIXES[".jpg"] == IMAGE_SUFFIXES[".jpeg"] == "image/jpeg"
    # SVG 不在表里：它是文本，按文本读更有用。
    assert ".svg" not in IMAGE_SUFFIXES
    assert ".pdf" not in IMAGE_SUFFIXES


def test_oversized_images_are_refused_with_the_numbers(tmp_path: Path) -> None:
    target = tmp_path / "huge.png"
    target.write_bytes(b"\x89PNG" + b"0" * (MAX_IMAGE_BYTES + 10))

    outcome = _read_as(_context(tmp_path), "huge.png")

    assert not outcome.ok
    # 只报"太大了"模型只会换个写法再试；要给上限和下一步。
    assert "上限" in outcome.message
    assert "裁切" in outcome.message


def test_missing_image_says_so(tmp_path: Path) -> None:
    outcome = _read_as(_context(tmp_path), "nope.png")

    assert not outcome.ok
    # 不存在的路径由 paths 层报错；重点是别把「没这个文件」说成「读图失败」，
    # 两种说法对应两种下一步：换路径 vs 换做法。
    assert "nope.png" in (outcome.message or "")


def test_image_path_escape_is_refused(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(_PNG)

    outcome = _read_as(_context(tmp_path), "../outside.png")

    assert not outcome.ok


def test_text_files_still_go_through_the_text_path(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("print(1)\n", encoding="utf-8")

    outcome = _read_as(_context(tmp_path), "a.py")

    assert outcome.ok
    assert "image" not in outcome.data
    assert "print(1)" in outcome.output


def _read_as(context: ToolContext, path: str):
    """同步跑 _read_file（除 broker 分支外都是纯同步逻辑）。"""
    import asyncio

    return asyncio.run(_read_file(context, {"path": path}))


def test_read_file_is_registered() -> None:
    names = {tool.name for tool in filesystem_tools()}
    assert "read_file" in names


# --------------------------------------------------------------------------- #
# 图片怎么进上下文
# --------------------------------------------------------------------------- #


class _Session:
    """只带 _append_tool_response 真正读到的几格。"""

    def __init__(self, supports_vision: bool) -> None:
        self.supports_vision = supports_vision
        self.run_id = "task:t:attempt:1"
        self.output_seen: dict = {}
        self.trimmed_chars = 0
        self.trim_events: list = []


def _payload() -> dict:
    return {
        "ok": True,
        "status": "ok",
        "output": "已读取图片：shot.png（image/png，1 KB）。",
        "data": {"path": "shot.png", "image": {"media_type": "image/png", "data": "AAA", "path": "shot.png", "bytes": 3}},
    }


def _call() -> dict:
    return {"id": "call_1", "function": {"name": "read_file", "arguments": "{}"}}


async def test_vision_model_gets_a_follow_up_message_with_the_image() -> None:
    from blank_api.services.agent.graph import _append_tool_response

    messages = await _append_tool_response([], _call(), _payload(), session=_Session(True), step=1)

    assert [item["role"] for item in messages] == ["tool", "user"]
    assert messages[1]["images"] == [{"media_type": "image/png", "data": "AAA"}]
    # 文案必须点明"这是刚读的那张图"，否则模型会当成用户新上传的附件。
    assert "read_file" in messages[1]["content"]


async def test_text_only_model_is_told_it_cannot_see() -> None:
    from blank_api.services.agent.graph import _append_tool_response

    messages = await _append_tool_response([], _call(), _payload(), session=_Session(False), step=1)

    assert [item["role"] for item in messages] == ["tool"]
    assert "不支持看图" in messages[0]["content"]
    # 不给图就不能留下"图在这儿"的暗示，否则模型会开始编。
    assert "AAA" not in messages[0]["content"]


async def test_plain_results_are_unchanged() -> None:
    from blank_api.services.agent.graph import _append_tool_response

    payload = {"ok": True, "status": "ok", "output": "1\tprint(1)", "data": {"path": "a.py"}}
    messages = await _append_tool_response([], _call(), payload, session=_Session(True), step=1)

    assert [item["role"] for item in messages] == ["tool"]
    assert "images" not in messages[0]

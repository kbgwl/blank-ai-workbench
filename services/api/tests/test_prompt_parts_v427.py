"""系统提示按段计量：删提示之前，得先知道删的是哪一段。

依据（`docs/harness-borrowings.md`）：Anthropic 给新一代模型删掉了 Claude Code
**80% 以上**的 system prompt，编码评测无可测损失；结论是 harness 该随模型演进被修剪，
而不是只累积指令。我们现在的系统提示 10,132 字符，历史上只加过没删过。

要做这个实验，第一步不是删，是**能看见每一段多大**：

    head      2,003  19%   开场与先问再干
    coding    5,120  50%   编程纪律（最大的一块）
    tail      2,435  24%   工具与收尾
    style       574   5%   文风

判据钉两件事：分段与整体**逐字节一致**（拆段不能顺手改了提示词），以及
「哪几段被拼进去」跟着 output_style 的语义走。第二条最容易写错：
`keep_coding_instructions` 为假时，编程纪律那段应当**整段不出现**。
"""

from __future__ import annotations

from blank_api.services.agent.graph import (
    CODING_INSTRUCTIONS,
    STYLE_PROMPT,
    _SYSTEM_HEAD,
    _SYSTEM_TAIL,
    compose_system_prompt,
    compose_system_prompt_parts,
    compose_system_prompt_parts_sizes,
)


def _join(parts) -> str:
    return "".join(glue + text for _name, glue, text in parts)


def test_the_parts_join_back_into_the_same_prompt():
    """拆段只是"看得见"，不能顺手改了提示词——差一个换行就是每次请求都变前缀。"""
    assert _join(compose_system_prompt_parts(None, scene="coding")) == compose_system_prompt(None, scene="coding")
    assert _join(compose_system_prompt_parts(None, scene="daily")) == compose_system_prompt(None, scene="daily")


def test_sizes_add_up_to_the_whole_prompt():
    sizes = compose_system_prompt_parts_sizes(None, scene="coding")
    whole = compose_system_prompt(None, scene="coding")

    assert sum(sizes.values()) + 1 == len(whole), "少了那个样式段前的换行"


def test_a_style_without_keep_drops_the_coding_block():
    """选了输出样式又不要求保留纪律时，那 5,120 字符整段不出现——这正是修剪的入口。"""
    style = {"prompt": "用短句回答", "keep_coding_instructions": False}
    names = [name for name, _glue, _text in compose_system_prompt_parts(style)]

    assert "coding" not in names
    assert "output_style" in names


def test_a_style_with_keep_retains_the_coding_block():
    style = {"prompt": "用短句回答", "keep_coding_instructions": True}
    names = [name for name, _glue, _text in compose_system_prompt_parts(style)]

    assert "coding" in names


def test_the_coding_block_is_the_biggest_slice():
    """这条不是审美判断：如果哪天它不再最大，说明提示词的重心变了，值得看一眼。"""
    sizes = compose_system_prompt_parts_sizes(None, scene="coding")

    assert max(sizes, key=sizes.get) == "coding"
    assert sizes["coding"] > len(_SYSTEM_HEAD + _SYSTEM_TAIL)
    assert sizes["style"] == len(STYLE_PROMPT)

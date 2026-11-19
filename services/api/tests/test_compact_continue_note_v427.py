"""压缩之后的续写点：必须明说「别复述」。

社区里对这条的经验高度一致（Claude Code 系 harness 的压缩实现）：
摘要之后如果不写一句"这是续写点，不要复述，接着做"，模型下次回复会先把摘要
礼貌地再总结一遍——白烧一轮上下文，而这一轮往往是压缩刚省下来的。

我们原来只写了「以最近状态和任务证据为准」：说了以什么为准，没说别复述。
这里钉三件事：

1. 确定性压缩（`compact_messages`）产出的续写点必须带上这句；
2. LLM 摘要那一路（`ContextCompactor.build` 的 head）也必须带——两条路不能分叉；
3. 两处都引用同一个常量，改一处等于改两处。
"""

from __future__ import annotations

import inspect

from blank_api.services.agent import context as ctx


def _many() -> list[dict]:
    return [{"role": "system", "content": "sys"}] + [
        {"role": "user", "content": "x" * 2_000} for _ in range(20)
    ]


def test_the_deterministic_compaction_says_dont_recap():
    out = ctx.compact_messages(_many(), max_chars=4_000)

    marker = next((m["content"] for m in out if "上下文已压缩" in str(m.get("content"))), "")
    assert "不要复述" in marker, marker[:120]
    assert "接着做" in marker


def test_both_compaction_paths_share_one_constant():
    """两条路各写一遍文案，就一定会有一处忘了改——这里让它们共用一个常量。"""
    source = inspect.getsource(ctx.ContextCompactor.build)
    assert "COMPACT_CONTINUE_NOTE" in source, "LLM 摘要那一路漏了续写指令"


def test_the_note_is_a_single_module_level_constant():
    assert isinstance(ctx.COMPACT_CONTINUE_NOTE, str)
    assert ctx.COMPACT_CONTINUE_NOTE.count("不要复述") == 1

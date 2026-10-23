"""折叠切点不能把 tool_calls 的配对切散。

2026-10-22 真实 DeepSeek 链路上实测到的失败：水位压缩折掉 6 条后，下一次调用
发出的历史里有一条孤儿 `role="tool"`，供应商直接回 400
（`Messages with role 'tool' must be a response to a preceding message with 'tool_calls'`），
整条任务判 failed。压缩本身省了 token，却把任务弄丢了。

这里的三组断言对应三个命题：

1. 切点落在工具结果段中间时，往回退到声明这批调用的那条 assistant；
2. 字符预算模式（确定性兜底）产出的历史里没有无锚点的工具结果段；
3. token 水位模式在几种窗口下都不产出，且确实发生过折叠（否则装置没测到路径）。
"""

from __future__ import annotations

import re

from blank_api.services.agent.context import ContextCompactor, _safe_cut, compact_messages


def _history(rounds: int = 4) -> list[dict]:
    messages: list[dict] = [
        {"role": "system", "content": "你是开发 Agent"},
        {"role": "user", "content": "任务目标：把三个文件读完再汇总"},
    ]
    for index in range(rounds):
        messages.append(
            {"role": "assistant", "content": f"第 {index} 步", "tool_calls": [{"id": f"call_{index}"}]}
        )
        messages.append(
            {"role": "tool", "tool_call_id": f"call_{index}", "content": f"第 {index} 次结果：" + "z" * 1_500}
        )
    # 一条 assistant 声明三个调用：这三条结果要么整段留下，要么整段折掉。
    messages.append(
        {
            "role": "assistant",
            "content": "还要再读三个文件",
            "tool_calls": [{"id": "c1"}, {"id": "c2"}, {"id": "c3"}],
        }
    )
    messages.append({"role": "tool", "tool_call_id": "c1", "content": "一" + "x" * 3_000})
    messages.append({"role": "tool", "tool_call_id": "c2", "content": "二" + "y" * 3_000})
    messages.append({"role": "tool", "tool_call_id": "c3", "content": "三" + "w" * 3_000})
    messages.append({"role": "assistant", "content": "三个都读完了，可以汇总"})
    return messages


def _unanchored_tool_runs(messages: list[dict]) -> list[int]:
    """每一段连续的 tool 结果，前一条必须是声明了足够多 tool_calls 的 assistant。

    这就是供应商的校验口径，也是这次 400 的根因所在。
    """
    bad: list[int] = []
    index = 0
    while index < len(messages):
        if messages[index].get("role") != "tool":
            index += 1
            continue
        start = index
        while index < len(messages) and messages[index].get("role") == "tool":
            index += 1
        previous = messages[start - 1] if start else None
        declared = len(previous.get("tool_calls") or []) if previous and previous.get("role") == "assistant" else 0
        if declared < index - start:
            bad.append(start)
    return bad


async def _summary(folded, previous):
    return f"{previous}\n- 已折叠 {len(folded)} 条"


def test_safe_cut_backs_up_to_the_assistant_that_declared_the_calls():
    history = _history()[2:]
    # 尾部窗口只装得下最后一条工具结果与收尾那条 assistant：切点落在 c3 上。
    assert history[11].get("role") == "tool"
    assert _safe_cut(history, 11) == 8
    assert (history[8].get("role"), len(history[8].get("tool_calls") or [])) == ("assistant", 3)
    # 边界：已经在头部不再往负数退，切点等于长度时原样返回
    assert _safe_cut(history, 0) == 0
    assert _safe_cut(history, len(history)) == len(history)


def test_unanchored_head_is_pushed_forward_instead_of_folding_nothing():
    # 段头本身就是一段没有归属的工具结果：往回退等于什么都不折，
    # 水位压不下去；这种内容是最老的，整段推过去才对。
    history = [
        {"role": "tool", "tool_call_id": "old_1", "content": "x" * 200},
        {"role": "tool", "tool_call_id": "old_2", "content": "y" * 200},
        {"role": "assistant", "content": "接着做"},
    ]
    assert _safe_cut(history, 1) == 2
    assert _safe_cut(history, 0) == 2
    assert _safe_cut(history, len(history)) == len(history)


def test_character_budget_never_emits_an_orphan_tool_result():
    # 预算 3500：从尾部数过去正好收得下"收尾 assistant + 第三条结果"，
    # 装不下第二条 —— 不保护切点的话，发出去的就是一个孤儿工具结果。
    result = compact_messages(_history(), max_chars=3_500)
    assert _unanchored_tool_runs(result) == []
    assert any((item.get("tool_calls") or []) and len(item["tool_calls"]) == 3 for item in result), (
        "发起那三个调用的 assistant 必须和它们的结果一起留在切点之后"
    )
    marker = next(item for item in result if str(item.get("content", "")).startswith("上下文已压缩"))
    claimed = int(re.search(r"省略 (\d+) 条", str(marker["content"])).group(1))
    tail_len = len(result) - 3  # 前两条永远保留，第三条是这条标记
    assert claimed == len(_history()) - 2 - tail_len, "标记里的条数必须等于真的折掉的条数"
    assert tail_len < len(_history()) - 2, "这个装置必须真的折掉过内容，否则什么都没测到"


async def test_watermark_mode_never_emits_an_orphan_tool_result():
    folded_at_least_once = False
    for window in (800, 1_200, 2_000, 3_000):
        compactor = ContextCompactor(context_window=window, trigger_ratio=0.8, reserve_tokens=0)
        built = await compactor.build(_history(), _summary)
        assert _unanchored_tool_runs(built) == [], f"窗口 {window} 下切点把配对切散了"
        folded_at_least_once = folded_at_least_once or compactor.last_report.folded > 0
    assert folded_at_least_once, "四种窗口都没进入折叠层，说明这个装置已经失效"

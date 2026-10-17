"""P1-2：两层上下文压缩与 token 记账。

第一层（microcompact）是确定性的、不花钱的：裁剪远离头部的超长工具结果、折叠
逐字节相同的重复输出、移除内联 base64。第二层（模型摘要）仍然只在窗口真的
超出预算时触发。两者都要留下可解释的账。
"""

from blank_api.services.agent.context import (
    ContextCompactor,
    estimate_tokens,
    message_tokens,
    microcompact,
)


def _bulk(steps: int = 12, size: int = 5_000) -> list[dict]:
    messages = [
        {"role": "system", "content": "你是开发 Agent"},
        {"role": "user", "content": "任务目标：改 main.py"},
    ]
    for index in range(steps):
        messages.append({"role": "assistant", "content": f"第 {index} 步"})
        messages.append(
            {
                "role": "tool",
                "content": "x" * size + f"-{index}",
                "tool_call_id": f"call_{index}",
            }
        )
    return messages


def test_estimate_tokens_treats_cjk_as_one_token_per_glyph() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("你好") == 2
    assert estimate_tokens("你好abcd") == 3
    assert message_tokens([{"role": "user", "content": "abcd"}]) == 5  # 4 字符 + 4 固定开销


def test_microcompact_clips_only_old_tool_results() -> None:
    messages = _bulk()
    trimmed, stats = microcompact(messages, max_chars=1_000)
    assert stats.clipped > 0
    assert stats.chars_saved > 0
    # 最近 8 条原样保留：模型正在推理的窗口不能被裁剪
    assert all(len(item["content"]) > 5_000 for item in trimmed[-8:] if item["role"] == "tool")
    # 消息条数不变，tool_call_id 配对依然完整
    assert len(trimmed) == len(messages)
    assert [item.get("tool_call_id") for item in trimmed] == [
        item.get("tool_call_id") for item in messages
    ]


def test_microcompact_folds_byte_identical_tool_outputs() -> None:
    same = "y" * 800
    messages = [
        {"role": "tool", "content": same, "tool_call_id": "a"},
        {"role": "tool", "content": same, "tool_call_id": "b"},
    ]
    trimmed, stats = microcompact(messages, max_chars=100)
    assert stats.duplicates == 1
    assert "完全相同" in trimmed[1]["content"]
    assert trimmed[0]["content"] == same


def test_microcompact_removes_inline_base64() -> None:
    blob = "data:image/png;base64," + "A" * 400
    trimmed, stats = microcompact([{"role": "tool", "content": blob}], max_chars=10)
    assert stats.base64_removed == 1
    assert "A" * 100 not in trimmed[0]["content"]
    assert "数据已移除" in trimmed[0]["content"]


def test_microcompact_does_nothing_below_the_trigger_ratio() -> None:
    messages = [{"role": "user", "content": "hi"}]
    trimmed, stats = microcompact(messages, max_chars=48_000)
    assert trimmed == messages
    assert stats.applied is False


async def test_report_accounts_tokens_boundary_and_fold() -> None:
    calls: list[int] = []

    async def summarize(folded, previous):
        calls.append(len(folded))
        return "【摘要】目标：改 main.py"

    compactor = ContextCompactor(max_chars=6_000)
    built = await compactor.build(_bulk(20), summarize)
    report = compactor.last_report

    assert calls, "超出预算时必须调用模型摘要"
    assert report.folded > 0
    assert report.boundary > 0
    assert report.tokens_after < report.tokens_before
    assert report.micro.applied is True
    payload = report.as_dict()
    assert payload["active_context_tokens_before"] == report.tokens_before
    assert payload["active_context_tokens_after"] == report.tokens_after
    assert payload["tokens_saved"] > 0
    assert any(item["role"] == "system" and "摘要" in item["content"] for item in built)


async def test_report_is_still_emitted_when_nothing_was_compacted() -> None:
    compactor = ContextCompactor(max_chars=48_000)
    await compactor.build([{"role": "user", "content": "短任务"}])
    report = compactor.last_report
    assert report.folded == 0
    assert report.boundary == 0
    assert report.tokens_before == report.tokens_after
    assert report.as_dict()["micro"]["clipped"] == 0

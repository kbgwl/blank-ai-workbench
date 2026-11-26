"""P1-2：两层上下文压缩与 token 记账。

第一层（microcompact）是确定性的、不花钱的：裁剪远离头部的超长工具结果、折叠
逐字节相同的重复输出、移除内联 base64。第二层（模型摘要）仍然只在窗口真的
超出预算时触发。两者都要留下可解释的账。
"""

import json

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


def _write_call(index: int, content: str, name: str = "write_file") -> dict:
    return {
        "role": "assistant",
        "content": "写文件",
        "tool_calls": [
            {
                "id": f"call_{index}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(
                        {"path": f"f{index}.txt", "content": content}, ensure_ascii=False
                    ),
                },
            }
        ],
    }


def test_message_tokens_counts_tool_call_arguments() -> None:
    """工具参数也是发出去的上下文，水位不能只数 content。"""
    message = _write_call(0, "x" * 4_000)
    assert message_tokens([message]) > 500


def test_microcompact_shrinks_old_write_args_but_keeps_recent_full() -> None:
    big = "x" * 5_000
    messages = [_write_call(index, big) for index in range(10)]
    trimmed, stats = microcompact(messages, max_chars=1_000)

    assert stats.args_shrunk == 2, stats.as_dict()
    old_args = trimmed[0]["tool_calls"][0]["function"]["arguments"]
    assert "已折叠" in old_args and "read_file" in old_args
    # 最近 8 条原样保留：模型正在推理的窗口不能被抽走。
    recent_args = trimmed[-1]["tool_calls"][0]["function"]["arguments"]
    assert big in recent_args
    # 不修改调用方传进来的原对象。
    assert big in messages[0]["tool_calls"][0]["function"]["arguments"]
    # 消息条数不变，tool_call id 还在，配对不破。
    assert len(trimmed) == len(messages)
    assert trimmed[0]["tool_calls"][0]["id"] == "call_0"


def test_microcompact_shrinks_edit_file_strings() -> None:
    big = "y" * 2_000
    arguments = json.dumps(
        {"path": "a.py", "old_string": big, "new_string": big + "z"}, ensure_ascii=False
    )
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_e",
                "type": "function",
                "function": {"name": "edit_file", "arguments": arguments},
            }
        ],
    }
    # 填满 cut：这条要在最近窗口之外才会被折。
    messages = [message] + [{"role": "user", "content": "继续"} for _ in range(9)]
    trimmed, stats = microcompact(messages, max_chars=100)
    assert stats.args_shrunk == 1
    parsed = json.loads(trimmed[0]["tool_calls"][0]["function"]["arguments"])
    assert parsed["old_string"].startswith("[较早的工具参数已折叠")
    assert parsed["new_string"].startswith("[较早的工具参数已折叠")


def test_microcompact_keeps_small_tool_args() -> None:
    message = _write_call(0, "短内容")
    messages = [message] + [{"role": "user", "content": "继续"} for _ in range(9)]
    trimmed, stats = microcompact(messages, max_chars=100)
    assert stats.args_shrunk == 0
    assert json.loads(trimmed[0]["tool_calls"][0]["function"]["arguments"])["content"] == "短内容"


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

from blank_api.services.agent.context import compact_messages


def test_context_compaction_preserves_rules_and_latest_execution():
    messages = [
        {"role": "system", "content": "系统规则"},
        {"role": "user", "content": "任务目标"},
        *({"role": "tool", "content": f"旧结果 {index} " + "x" * 100} for index in range(8)),
        {"role": "assistant", "content": "最后一步"},
    ]
    compacted = compact_messages(messages, max_chars=500)
    contents = [item["content"] for item in compacted]
    assert contents[0] == "系统规则"
    assert "任务目标" in contents
    assert "最后一步" in contents
    assert any("上下文已压缩" in content for content in contents)


def test_tool_output_is_bounded_without_other_context_overflow():
    compacted = compact_messages([{"role": "tool", "content": "x" * 20_000}], max_chars=50_000)
    assert len(compacted[0]["content"]) < 9_000
    assert "工具结果已截断" in compacted[0]["content"]

"""大文件必须分次写：一次 write_file 塞 3 万字符会被网关截断。

真机 23f93669：write_file 的 app.js 参数 31009 字，流在第 31009 个字符处断开，
JSON 不完整；即便供应商不截断，GLM 这类单次输出上限 8000 token 的模型也写不完。
纪律要求：正文超过约 8000 字符先写骨架，再用 edit_file 按函数分几次追加。
"""

from __future__ import annotations

from blank_api.services.agent.graph import SYSTEM_PROMPT, compose_system_prompt


def test_large_file_must_be_written_in_chunks() -> None:
    text = SYSTEM_PROMPT
    assert "8000 字符" in text
    assert "先写骨架" in text
    assert "edit_file 按函数" in text
    assert "截断" in text


def test_rule_follows_output_style_semantics() -> None:
    replaced = compose_system_prompt(
        {"name": "review", "prompt": "只做评审。", "keep_coding_instructions": False}
    )
    assert "8000 字符" not in replaced
    kept = compose_system_prompt(
        {"name": "review", "prompt": "只做评审。", "keep_coding_instructions": True}
    )
    assert "8000 字符" in kept

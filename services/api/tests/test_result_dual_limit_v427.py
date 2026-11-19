"""工具结果的双上限：字符 + 行数，取更早的切点。

只有字符上限时，两头都切得不对：
- 一行 10 万字符的压缩 JSON 切得太晚 —— 模型收到一段断在字符串中间的 JSON，
  既解析不了，也看不出该去哪里取；
- 2000 行正常日志切得太早 —— 4 万字符够 1500 行，它以为读完了，其实后面还有一半。

opencode 的做法是 2000 行 / 50 KiB 双上限取更早的切点，这里照抄。
"""

from __future__ import annotations

from blank_api.services.tools.gateway import (
    MAX_RESULT_CHARS,
    MAX_RESULT_LINES,
    _dual_clip,
    _truncation_note,
)


def test_a_long_single_line_is_cut_by_chars():
    text = "x" * (MAX_RESULT_CHARS + 5_000)
    out, kept, reason = _dual_clip(text)

    assert reason == "chars"
    assert kept == MAX_RESULT_CHARS
    assert len(out) == kept


def test_many_short_lines_are_cut_by_lines():
    text = "\n".join("line" for _ in range(MAX_RESULT_LINES + 500))
    out, kept, reason = _dual_clip(text)

    assert reason == "lines", "3000 行的日志还按字符切，等于让模型以为读完了"
    assert out.count("\n") == MAX_RESULT_LINES - 1
    assert kept < MAX_RESULT_CHARS


def test_whichever_limit_hits_first_wins():
    """两条都很宽的内容（每行 500 字、只有 100 行）该按字符切。"""
    text = "\n".join("y" * 500 for _ in range(100))
    _, kept, reason = _dual_clip(text)

    assert reason == "chars"
    assert kept == MAX_RESULT_CHARS


def test_small_outputs_are_left_alone():
    text = "a\nb\nc"
    out, kept, reason = _dual_clip(text)

    assert reason == ""
    assert out == text and kept == len(text)


def test_the_note_says_which_limit_was_hit():
    by_lines = _truncation_note("run_shell", 9_000, 90_000, reason="lines")
    by_chars = _truncation_note("run_shell", 40_000, 90_000, reason="chars")

    assert "行数太多" in by_lines
    assert "太长" in by_chars and "行数" not in by_chars
    # 两条都必须说清"还有多少没显示"和"下一步怎么办"
    for note in (by_lines, by_chars):
        assert "未显示" in note
        assert "不要因为没看到就重复执行" in note

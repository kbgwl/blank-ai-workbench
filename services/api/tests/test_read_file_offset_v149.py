"""read_file 的 offset / limit：让模型能读到文件后半段，而不是靠 search_code 摸行号。

0.8.16 的建站实测里，产物 520 行、12,364 字符，进上下文的读文件结果被裁到
6,000 字符，只露到第 293 行。模型要改页面尾部，搜的 8 个锚点（</style> 364、
</head> 365、<body> 366、</main> 486、site-footer 488、</script> 518、</html> 520）
全在截断区——29 步里 13 步 search_code 就是这么来的。

所以这里要证的不是"有个 offset 参数"，而是**裁剪时给的那句提示真能接着读**。
提示里写 offset 却不支持，等于把模型骗回去重新摸一遍（`payload.py` 原先那句
「带 offset 重读」正是这种空头支票）。
"""

from __future__ import annotations

import json
import re

from blank_api.services.agent.payload import render
from blank_api.services.workspace import read_text_file

TOTAL = 500


def _long_line(index: int) -> str:
    return f"line{index:04d} " + "x" * 40


def _make_big(workspace) -> None:
    (workspace / "big.txt").write_text(
        "\n".join(_long_line(i) for i in range(1, TOTAL + 1)) + "\n", encoding="utf-8"
    )


def _output_of(rendered: str) -> str:
    """render 回的是 JSON 串，换行是转义过的；断言要看它真正塞进上下文的那段。"""
    return json.loads(rendered)["output"]


def test_offset_and_limit_slice_by_line(workspace):
    _make_big(workspace)

    got = read_text_file(workspace, "big.txt", offset=101, limit=50)

    body = got["content"].splitlines()
    assert got["start_line"] == 101 and got["end_line"] == 150
    assert got["total_lines"] == TOTAL
    assert body[0].startswith("line0101")
    assert len(body) == 50


def test_default_still_reads_whole_file(workspace):
    """不传参数的老调用方行为不能变。"""
    _make_big(workspace)

    got = read_text_file(workspace, "big.txt")

    assert got["start_line"] == 1 and got["end_line"] == TOTAL
    assert len(got["content"].splitlines()) == TOTAL


def test_offset_past_end_says_so_instead_of_silently_empty(workspace):
    _make_big(workspace)

    got = read_text_file(workspace, "big.txt", offset=TOTAL + 10)

    assert got["content"] == ""
    assert got["start_line"] == TOTAL + 10
    assert got["total_lines"] == TOTAL


def test_limit_zero_reads_nothing_but_stays_consistent(workspace):
    _make_big(workspace)

    got = read_text_file(workspace, "big.txt", offset=1, limit=0)

    assert got["content"] == ""
    assert got["end_line"] == 0


def test_payload_clip_tells_which_lines_and_how_to_continue(workspace):
    """裁剪后的提示必须带「共几行 / 现在是第几行 / 下一步 offset」，缺一不可。"""
    _make_big(workspace)
    whole = read_text_file(workspace, "big.txt")
    assert len(whole["content"]) > 6000, "样本不够长，测不到裁剪分支"

    text, _ = render(
        "read_file",
        {
            "ok": True,
            "status": "ok",
            "output": whole["content"],
            "data": {
                "path": "big.txt",
                "start_line": whole["start_line"],
                "end_line": whole["end_line"],
                "total_lines": whole["total_lines"],
            },
        },
    )

    assert f"共 {TOTAL} 行" in text
    shown = _output_of(text)
    assert re.search(r"第 1–\d+ 行", shown)
    assert re.search(r"offset=\d+", shown)


def test_hint_offset_actually_continues_the_file(workspace):
    """闭环：照提示里的 offset 再读一次，必须从断点下一行接上，且两段覆盖全文。"""
    _make_big(workspace)
    whole = read_text_file(workspace, "big.txt")

    text, _ = render(
        "read_file",
        {
            "ok": True,
            "status": "ok",
            "output": whole["content"],
            "data": {
                "path": "big.txt",
                "start_line": 1,
                "end_line": whole["total_lines"],
                "total_lines": whole["total_lines"],
            },
        },
    )
    shown = _output_of(text)
    match = re.search(r"offset=(\d+)", shown)
    assert match, f"提示里没有可用的 offset：{shown[-160:]}"
    next_offset = int(match.group(1))

    rest = read_text_file(workspace, "big.txt", offset=next_offset)

    assert rest["content"].startswith(f"{_long_line(next_offset)}\r\n") or rest["content"].startswith(
        f"{_long_line(next_offset)}\n"
    )
    assert rest["end_line"] == TOTAL

    shown_end = int(re.search(r"第 1–(\d+) 行", shown).group(1))
    assert next_offset == shown_end + 1, "提示的 offset 必须紧接显示的最后一行，不能跳也不能重"


def test_clip_keeps_line_boundary(workspace):
    """切开的位置要在行尾，不能让模型读到半行。"""
    _make_big(workspace)
    whole = read_text_file(workspace, "big.txt")

    text, _ = render(
        "read_file",
        {
            "ok": True,
            "status": "ok",
            "output": whole["content"],
            "data": {"start_line": 1, "end_line": whole["total_lines"], "total_lines": whole["total_lines"]},
        },
    )

    body = _output_of(text).split("…（本文件共")[0].rstrip("\r\n")
    assert body.endswith("x" * 40), f"切口不在行尾：{body[-60:]!r}"

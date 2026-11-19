"""读文件的单次预算：不该由"文件有多大"之外的东西决定。

真实缺陷：``read_file`` 的头部预算是个常数 6,000 字符，与文件大小无关。
一份 800 行的源码约 24k 字符，模型要连读四次；而每一次续读都要把整段上下文
重新发一遍——省下的 18k 上下文，换来的是三倍输入 token 和三轮等待。

所以这里证的不是"预算变大了"，而是三件可以分别失效的事：

1. **整份装得下的文件一次给全**（不再被 6k 切开）。
2. **中等文件的预算上调到上限**，但续读的 offset 仍然精确 —— 给多了却给错行，
   比给少了更糟：模型会以为中间那段不存在。
3. **巨物保持克制**（2MB 的日志多给 6k 也只是多 6k 行前奏），其它工具的预算一格不动。
"""

from __future__ import annotations

import json
import re

from blank_api.services.agent.payload import (
    READ_HEAD_CAP,
    TOOL_BUDGETS,
    render,
)

NOTE = re.compile(r"此处显示第 (\d+)–(\d+) 行，继续请用 offset=(\d+) 重读")


def _lines(count: int, width: int = 46) -> str:
    """造一份"每行都不一样"的源码：去数字后形态也分散，不会被当成噪音日志。"""
    return "".join(f"line{i:06d} " + "x" * width + "\n" for i in range(1, count + 1))


def _payload(body: str, total: int, size: int, start: int = 1) -> dict:
    return {
        "ok": True,
        "status": "ok",
        "output": body,
        "data": {
            "path": "src/big.py",
            "size": size,
            "truncated": False,
            "start_line": start,
            "end_line": start + body.count("\n") - 1,
            "total_lines": total,
        },
    }


def test_file_that_fits_in_one_read_is_not_cut():
    """150 行 / 8.7k 字符：比旧常数大，但整份装得下，就不该被切成两半。"""
    body = _lines(150)
    assert len(body) > 6_000, "样本要比旧预算大，否则这条测试在旧实现下也是绿的"
    text, stats = render("read_file", _payload(body, total=150, size=len(body)))

    assert stats["saved"] == 0
    assert stats["read_head"] == READ_HEAD_CAP
    assert "省略" not in text and "共 150 行" not in text
    assert body.splitlines()[-1] in text


def test_medium_file_gets_a_larger_head_than_the_old_constant():
    """800 行 / 40k 字符：旧预算只给得下 130 行左右，模型要读四遍。"""
    body = _lines(800)
    text, stats = render("read_file", _payload(body, total=800, size=40_000))

    kept = text.count("\\n")  # JSON 里换行是转义的
    assert stats["read_head"] == READ_HEAD_CAP
    assert kept > 200, f"只留下 {kept} 行，等于没扩容"
    assert "共 800 行" in text, "裁了却不说共几行，模型会以为文件就这么长"


def test_offset_after_a_larger_head_still_points_at_the_next_unread_line():
    """给多了也必须给对：提示里的行号、offset、正文三者要严丝合缝。

    差一行不是"无所谓"：少报一行，模型会把最后那行再读一遍；多报一行，
    它会跳过一行真没看的内容——而那条提示存在的全部意义就是别再多读。
    """
    body = _lines(800)
    text, _ = render("read_file", _payload(body, total=800, size=40_000))

    start, end, offset = (int(part) for part in NOTE.search(text).groups())
    assert (start, offset) == (1, end + 1)

    output = json.loads(text)["output"]
    displayed = output.split("…（本文件共")[0].rstrip("\n").splitlines()
    assert len(displayed) == end - start + 1, "提示说的行数与正文实际给的行数不一致"
    assert displayed[-1] == body.splitlines()[end - 1], "最后显示的那一行与提示不符"
    assert body.splitlines()[end] not in displayed, "提示说没显示，正文里却已经有了"


def test_monster_file_keeps_the_old_conservative_head():
    """2MB 的日志：多给 6k 字符只是多 6k 行前奏，该做的是定位或跳读。"""
    body = _lines(800)
    text, stats = render("read_file", _payload(body, total=800, size=2_000_000))

    assert stats["read_head"] == TOOL_BUDGETS["read_file"][0]
    assert "共 800 行" in text


def test_missing_size_falls_back_to_what_was_actually_read():
    """没有 size 的回执（老 broker、外部调用方）：读进来不超上限的就别裁。"""
    small = _lines(80)
    _, stats = render("read_file", {"ok": True, "status": "ok", "output": small})
    assert stats["saved"] == 0

    big = _lines(800)
    _, stats = render("read_file", {"ok": True, "status": "ok", "output": big})
    # 没有 size 就不知道文件多大，只能退回常数预算——不许拿"读进来的长度"冒充文件大小。
    assert stats["read_head"] == TOOL_BUDGETS["read_file"][0]
    assert stats["saved"] > 0


def test_other_tools_keep_their_budgets():
    """这次只动 read_file：shell 仍然是 6,000 + 8,000，不能被"顺手统一"。"""
    output = "".join(f"row {i}\n" for i in range(4_000))
    text, stats = render("run_shell", {"ok": True, "status": "ok", "exec": {"exit_code": 0}, "output": output})

    assert "read_head" not in stats
    assert len(text) < 18_000
    assert output.splitlines()[0] in text and output.splitlines()[-1] in text

"""大文件的 window 读：把"前 512KB"当成"整个文件"这件事修掉。

真实缺陷：4 万行的日志（2MB）只读前 512KB，然后**自称共 9893 行**，
``offset=30000`` 直接返回空。模型据此得出的结论是"文件到这儿就结束了"。

而引擎自己在截断提示里教模型："把输出重定向到工作区内的文件（如 ``> out.log``）
再用 read_file 分批读"——分批读到 512KB 就断，等于把这条建议骗回去。
所以这里要证的不是"能读大文件"，而是**行号、总行数、续读位置始终与真实字节对得上**。

一条额外的边界：一行本身就超过单次上限时，"用 offset 续读"是空话——
必须说清这一行不能用 offset 接着读，而不是给一个读不到东西的 offset=2。
"""

from __future__ import annotations

import pytest

from blank_api.services.workspace import MAX_READ_BYTES, read_text_file


def _pad(index: int) -> str:
    return f"line{index:06d} " + "x" * 40


def _big(workspace, relative: str = "big.log", count: int = 40_000, trailer: bool = True) -> int:
    body = "\n".join(_pad(i) for i in range(1, count + 1)) + ("\n" if trailer else "")
    (workspace / relative).write_text(body, encoding="utf-8")
    return count


def test_deep_offset_reaches_lines_beyond_the_first_512kb(workspace):
    _big(workspace)

    got = read_text_file(workspace, "big.log", offset=30_000, limit=3)

    assert got["start_line"] == 30_000 and got["end_line"] == 30_002
    assert got["content"].splitlines()[0].startswith("line030000")


def test_total_lines_is_the_real_count_not_the_prefix_count(workspace):
    """旧实现报的是前 512KB 里的行数（9893），那是错的事实，不是近似。"""
    _big(workspace, count=40_000)

    got = read_text_file(workspace, "big.log", offset=1, limit=1)

    assert got["total_lines"] == 40_000


def test_pagination_hint_actually_continues_the_file(workspace):
    """闭环：照提示里的 offset 再读，必须从断点下一行接上。"""
    _big(workspace)
    first = read_text_file(workspace, "big.log")

    assert first["message"], "大文件整读必须给出续读提示"

    import re

    next_offset = int(re.search(r"offset=(\d+)", first["message"]).group(1))
    rest = read_text_file(workspace, "big.log", offset=next_offset, limit=1)

    assert rest["content"].splitlines()[0].startswith(f"line{next_offset:06d}")
    assert next_offset == first["end_line"] + 1, "提示的 offset 必须紧接最后一行"


def test_offset_past_the_end_reports_the_true_total(workspace):
    _big(workspace, count=40_000)

    got = read_text_file(workspace, "big.log", offset=99_999)

    assert got["content"] == ""
    assert got["total_lines"] == 40_000
    assert "40000" in got["message"]


def test_file_without_trailing_newline_counts_the_last_line(workspace):
    _big(workspace, relative="noeol.log", count=20_000, trailer=False)

    got = read_text_file(workspace, "noeol.log", offset=20_000, limit=1)

    assert got["total_lines"] == 20_000
    assert got["content"].startswith("line020000")


def test_file_with_trailing_newline_does_not_count_a_phantom_line(workspace):
    """以换行结尾时不能多算一行——那一行永远读不到，却会出现在"共 N 行"里。"""
    _big(workspace, count=20_000, trailer=True)

    got = read_text_file(workspace, "big.log")

    assert got["total_lines"] == 20_000


def test_chinese_file_is_not_mistaken_for_binary(workspace):
    """取头 512KB 时正好切在汉字中间：严格解码会失败，不能因此把文件判成二进制。"""
    (workspace / "zh.txt").write_text(
        "\n".join(f"第{i:06d}行：这一行是中文注释，用来测字符边界" for i in range(1, 20_001)),
        encoding="utf-8",
    )

    got = read_text_file(workspace, "zh.txt", offset=15_000, limit=2)

    assert got["binary"] is False
    assert got["content"].splitlines()[0].startswith("第015000行")
    assert "\ufffd" not in got["content"]


def test_gbk_large_file_keeps_its_encoding(workspace):
    (workspace / "gbk.txt").write_text("中文一行\n" * 100_000, encoding="gbk")

    got = read_text_file(workspace, "gbk.txt", offset=99_999, limit=2)

    assert got["encoding"] == "gbk"
    assert got["content"].splitlines() == ["中文一行", "中文一行"]


def test_binary_large_file_is_still_binary(workspace):
    (workspace / "bin.dat").write_bytes(b"\x00\x01\x02" * 400_000)

    got = read_text_file(workspace, "bin.dat")

    assert got["binary"] is True
    assert got["content"] == ""


def test_single_huge_line_does_not_promise_a_next_offset(workspace):
    """一行 1.2MB：这一行没有"下一行"，不许给一个读不到东西的 offset。"""
    (workspace / "one.txt").write_text("中" * 400_000, encoding="utf-8")

    got = read_text_file(workspace, "one.txt")

    assert got["content"], "至少要给出前半段"
    assert "offset=" not in got["message"]
    assert "不能用 offset 续读" in got["message"]


def test_partial_read_carries_no_hash_and_is_marked_truncated(workspace):
    """只读了窗口就拿不到全文哈希：读后写闸门只能退回比 stat。"""
    _big(workspace)

    got = read_text_file(workspace, "big.log", offset=30_000, limit=2)

    assert got["sha256"] == ""
    assert got["truncated"] is True
    assert got["size"] > MAX_READ_BYTES


def test_window_never_ends_with_half_a_line(workspace):
    """预算用完时丢半行：模型照着半行去改，改的是一段残句。"""
    _big(workspace)

    got = read_text_file(workspace, "big.log")

    lines = got["content"].splitlines()
    assert lines, "整读不该是空"
    assert all(line.endswith("x" * 40) for line in lines), "最后一行被切断了"


def test_limit_is_honoured_exactly(workspace):
    _big(workspace)

    got = read_text_file(workspace, "big.log", offset=100, limit=7)

    assert len(got["content"].splitlines()) == 7
    assert got["end_line"] == 106


@pytest.mark.parametrize("offset,limit", [(1, None), (1, 1), (40_000, 1)])
def test_window_edges_stay_consistent(workspace, offset, limit):
    """三个边界（首、尾、单行）上 start/end 的关系不能翻。"""
    _big(workspace)

    got = read_text_file(workspace, "big.log", offset=offset, limit=limit)

    assert got["start_line"] == offset
    assert got["end_line"] == offset + len(got["content"].splitlines()) - 1

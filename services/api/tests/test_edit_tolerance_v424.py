"""读→改→写这条主轴上的三处暗坑（对标 Claude Code 的 Edit/Write 契约）。

这三件事都不是新功能，而是"模型明明做对了、工具却判错"的地方：

1. **引号形态**。模型从 read_file 里抄一段中文注释，原文是 ``“”``，它发的是 ``""``。
   两边肉眼一样、字节不同，精确匹配必失败，模型于是把整段重发一遍——还是错。
   Claude 的 Edit 在精确匹配失败后会折叠引号再找一次，命中的是**文件里的原文片段**。
2. **转义字面量**。模型把 ``\\u4e2d`` 当原文发过来（JSON 里被转义过一次的常见残留）。
3. **删除整行留空行**。``old_string`` 不带行尾换行、文件里是 ``old_string\\n``，
   只删前半段就会剩一行空白——用户看到的是"删了一行，却多了一行空的"。

另外把 ``write_file`` 的两处形态丢失钉住：CRLF 与 BOM。模型给的 content 几乎总是
LF、无 BOM，照原样写下去，改两行会报成整文件重写（实测 6 行变更），
在 Windows 项目里足以把"这次改了什么"完全埋掉。

容错的方向必须钉死：**只放宽"看起来一样"的差异，不放过真正的不同**。
所以第 4 条判据是反向的——内容真的不一样时仍然必须失败。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from blank_api.services.tools.editing import (
    _conform_written,
    _edit_on_disk,
    _match_once,
    _write_on_disk,
)


def _put(root: Path, relative: str, raw: bytes) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return target


def _edit(root: Path, **arguments) -> dict:
    return _edit_on_disk(root, "task-1", arguments)


def test_curly_quotes_in_file_accept_straight_quotes_from_model(workspace):
    """文件里是中文弯引号、模型发的是直引号：按原文替换，而不是判"找不到"。"""
    _put(workspace, "a.py", 'NOTE = “先看这里”\n'.encode("utf-8"))

    result = _edit(workspace, path="a.py", old_string='NOTE = "先看这里"', new_string='NOTE = "改过了"')

    assert "not_found" not in result, result
    assert result["replacements"] == 1
    assert (workspace / "a.py").read_text(encoding="utf-8") == 'NOTE = "改过了"\n'


def test_straight_quotes_in_file_accept_curly_quotes_from_model(workspace):
    """反向同样成立：折叠是双向的，不是只往一个方向猜。"""
    _put(workspace, "a.py", 'NOTE = "原文"\n'.encode("utf-8"))

    result = _edit(workspace, path="a.py", old_string="NOTE = “原文”", new_string='NOTE = "新文"')

    assert "not_found" not in result, result
    assert (workspace / "a.py").read_text(encoding="utf-8") == 'NOTE = "新文"\n'


def test_quote_match_is_reported_back_to_the_model(workspace):
    """命中的方式要写进回执：不写，模型以为自己发对了，下一处还会这么发。"""
    _put(workspace, "a.py", 'NOTE = “先看这里”\n'.encode("utf-8"))

    result = _edit(workspace, path="a.py", old_string='NOTE = "先看这里"', new_string="NOTE = 2")

    assert result["matched"] == ["引号形态"]


def test_unicode_escape_literal_is_decoded_before_matching(workspace):
    """模型把 ``\\u4e2d`` 当原文发过来时先还原，再找。"""
    _put(workspace, "a.py", 'X = "中"\n'.encode("utf-8"))

    result = _edit(workspace, path="a.py", old_string='X = "\\u4e2d"', new_string='X = "文"')

    assert "not_found" not in result, result
    assert (workspace / "a.py").read_text(encoding="utf-8") == 'X = "文"\n'


def test_tolerance_does_not_swallow_real_differences(workspace):
    """反向判据：内容真的不一样时必须失败，容错不能变成"随便改一处"。"""
    _put(workspace, "a.py", 'NOTE = “原文甲”\n'.encode("utf-8"))

    result = _edit(workspace, path="a.py", old_string='NOTE = "原文乙"', new_string='NOTE = "新"')

    assert result.get("not_found") is True
    assert (workspace / "a.py").read_text(encoding="utf-8") == 'NOTE = “原文甲”\n'


def test_tolerance_covers_quotes_only_not_whitespace(workspace):
    """容错的边界：只有"看起来一样"的引号差异被放过。

    多一个空格、少一行缩进都属于真的不同——放宽到空白就等于让工具替模型猜
    它想改哪儿，猜错的代价是改到另一处。
    """
    assert _match_once('a  = “一”\n', 'a = "一"') is None
    # 只是没带缩进不算差异：子串匹配本来就不要求对齐行首，替换后缩进仍然在。
    assert _match_once('    a = “一”\n', 'a = "一"') == ('a = “一”', "引号形态")


def test_deleting_a_line_takes_its_newline_with_it(workspace):
    """删一行不留空行：文件里是 ``needle\\n`` 时，换行要一起删。"""
    _put(workspace, "a.py", "one\ntwo\nthree\n".encode("utf-8"))

    result = _edit(workspace, path="a.py", old_string="two", new_string="")

    assert "not_found" not in result, result
    assert (workspace / "a.py").read_text(encoding="utf-8") == "one\nthree\n"


def test_deleting_part_of_a_line_keeps_the_newline(workspace):
    """删的是行内一段（不是整行）时，换行必须留着，否则两行会被粘成一行。"""
    _put(workspace, "a.py", "keep two\nthree\n".encode("utf-8"))

    result = _edit(workspace, path="a.py", old_string="two", new_string="")

    assert "not_found" not in result, result
    assert (workspace / "a.py").read_text(encoding="utf-8") == "keep \nthree\n"


def test_write_keeps_crlf_so_a_two_line_change_stays_two_lines(workspace):
    """CRLF 文件用 LF 内容写回：换行保持 CRLF，diff 只报真正改的行。"""
    target = _put(workspace, "crlf.py", b"a = 1\r\nb = 2\r\nc = 3\r\n")

    result = _write_on_disk(workspace, "task-1", {"path": "crlf.py", "content": "a = 1\nb = 9\nc = 3\n"})

    assert target.read_bytes() == b"a = 1\r\nb = 9\r\nc = 3\r\n"
    assert result["changed_lines"] == 2, result["diff"]


def test_write_keeps_bom(workspace):
    """带 BOM 的文件写回后仍然带 BOM：BOM 是文件身份的一部分，不是空白。"""
    target = _put(workspace, "bom.py", "\ufeffx = 1\n".encode("utf-8"))

    _write_on_disk(workspace, "task-1", {"path": "bom.py", "content": "x = 2\n"})

    assert target.read_bytes() == "\ufeffx = 2\n".encode("utf-8")


def test_write_with_only_line_ending_difference_is_not_a_change(workspace):
    """内容一样、只是换行风格不同：判未变更，不写盘、不留备份。"""
    _put(workspace, "crlf.py", b"a = 1\r\nb = 2\r\n")

    result = _write_on_disk(workspace, "task-1", {"path": "crlf.py", "content": "a = 1\nb = 2\n"})

    assert result.get("unchanged") is True, result


def test_conform_picks_the_majority_line_ending(workspace):
    """混合换行按多数走：一处 CRLF 不该把整个 LF 文件改成 CRLF。"""
    original = "a\nb\nc\n"

    assert _conform_written("x\ny\n", original) == "x\ny\n"
    # 两个 CRLF 对一个 LF：多数是 CRLF，按 CRLF 写。
    assert _conform_written("x\ny\n", "a\r\nb\r\nc\n") == "x\r\ny\r\n"
    # 一个 CRLF 对两个 LF：多数是 LF，不许因为一处 CRLF 把整个文件翻过来。
    assert _conform_written("x\ny\n", "a\r\nb\nc\n") == "x\ny\n"


def test_edit_on_crlf_file_does_not_report_whole_file_changed(workspace):
    """CRLF 文件改一处，diff 里只有那一行——回归判据。"""
    _put(workspace, "crlf.py", b"a = 1\r\nb = 2\r\nc = 3\r\n")

    result = _edit(workspace, path="crlf.py", old_string="b = 2", new_string="b = 9")

    assert result["changed_lines"] == 2
    assert (workspace / "crlf.py").read_bytes() == b"a = 1\r\nb = 9\r\nc = 3\r\n"


def test_sha_guard_still_sees_the_written_bytes(workspace):
    """写完重记的状态必须对得上磁盘：容错改的是匹配，不是校验强度。"""
    from blank_api.services.tools.file_state import FileStateCache

    _put(workspace, "a.py", 'NOTE = “原文”\n'.encode("utf-8"))
    cache = FileStateCache()
    cache.record_read(
        "a.py",
        size=(workspace / "a.py").stat().st_size,
        mtime_ns=(workspace / "a.py").stat().st_mtime_ns,
        sha256=hashlib.sha256((workspace / "a.py").read_bytes()).hexdigest(),
    )

    _edit(workspace, path="a.py", old_string='NOTE = "原文"', new_string="NOTE = 1")
    cache.record_write(workspace, "a.py")

    assert cache.guard("a.py")["sha256"] == hashlib.sha256((workspace / "a.py").read_bytes()).hexdigest()

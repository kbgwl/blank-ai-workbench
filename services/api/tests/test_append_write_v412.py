# -*- coding: utf-8 -*-
"""write_file(append=true)：长文件分段写入的磁盘语义。

真实台账：一次 6567 字的 write_file 参数在流里断掉，整次写入失败后重试。
分段写入是这条路的正解；这里的判据覆盖接缝：换行、BOM、空追加、二进制拒绝。
"""
from __future__ import annotations

from pathlib import Path

from blank_api.services.tools.editing import _write_on_disk


def _append(root: Path, relative: str, content: str) -> dict:
    return _write_on_disk(
        root, "task-1", {"path": relative, "content": content, "append": True}
    )


def test_append_extends_existing_file(tmp_path):
    target = tmp_path / "plan.md"
    target.write_text("第一节\n", encoding="utf-8")

    result = _append(tmp_path, "plan.md", "第二节\n")

    assert target.read_text(encoding="utf-8") == "第一节\n第二节\n"
    assert result["existed"] is True
    assert result["changed_lines"] > 0
    assert result["backup_path"]


def test_append_adds_newline_when_original_has_none(tmp_path):
    target = tmp_path / "plan.md"
    target.write_text("第一节", encoding="utf-8")

    _append(tmp_path, "plan.md", "第二节")

    assert target.read_text(encoding="utf-8") == "第一节\n第二节"


def test_append_keeps_crlf_style(tmp_path):
    target = tmp_path / "plan.md"
    target.write_bytes("第一节\r\n".encode("utf-8"))

    _append(tmp_path, "plan.md", "第二节\n")

    assert target.read_bytes() == "第一节\r\n第二节\r\n".encode("utf-8")


def test_append_does_not_duplicate_bom(tmp_path):
    target = tmp_path / "plan.md"
    target.write_bytes("﻿第一节\n".encode("utf-8"))

    _append(tmp_path, "plan.md", "﻿第二节\n")

    data = target.read_bytes()
    assert data.count(b"\xef\xbb\xbf") == 1
    assert data.decode("utf-8") == "﻿第一节\n第二节\n"


def test_append_creates_missing_file(tmp_path):
    result = _append(tmp_path, "new.md", "内容\n")

    assert (tmp_path / "new.md").read_text(encoding="utf-8") == "内容\n"
    assert result["existed"] is False


def test_empty_append_is_unchanged(tmp_path):
    target = tmp_path / "plan.md"
    target.write_text("内容\n", encoding="utf-8")

    result = _append(tmp_path, "plan.md", "")

    assert result.get("unchanged") is True
    assert target.read_text(encoding="utf-8") == "内容\n"


def test_append_refuses_binary_file(tmp_path):
    target = tmp_path / "blob.bin"
    target.write_bytes(b"\xff\xfe\x00\x01")

    result = _append(tmp_path, "blob.bin", "文本")

    assert "guard_error" in result
    assert target.read_bytes() == b"\xff\xfe\x00\x01"

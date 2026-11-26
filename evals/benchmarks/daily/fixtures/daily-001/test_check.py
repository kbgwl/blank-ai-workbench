# -*- coding: utf-8 -*-
from pathlib import Path


def test_text_copies_keep_content():
    for name in ("发票-一月.txt", "发票-二月.txt", "合同-甲.txt", "会议纪要.md"):
        source = Path(name)
        target = Path("整理") / "文本" / name
        assert target.exists(), f"缺少副本：{target}"
        assert target.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


def test_image_copy_keeps_content():
    source = Path("logo.png")
    target = Path("整理") / "图片" / "logo.png"
    assert target.exists(), "缺少图片副本"
    assert target.read_bytes() == source.read_bytes()


def test_originals_are_kept():
    for name in ("发票-一月.txt", "发票-二月.txt", "合同-甲.txt", "会议纪要.md", "logo.png"):
        assert Path(name).exists(), f"原文件被删：{name}"


def test_index_lists_every_file():
    text = Path("整理说明.md").read_text(encoding="utf-8")
    for name in ("发票-一月.txt", "发票-二月.txt", "合同-甲.txt", "会议纪要.md", "logo.png"):
        assert name in text, f"整理说明没有记录：{name}"

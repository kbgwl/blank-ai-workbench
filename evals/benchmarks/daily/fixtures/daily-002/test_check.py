# -*- coding: utf-8 -*-
import re
from pathlib import Path

from docx import Document

TITLE = "数据智能分析平台"
SECTIONS = ("背景", "目标", "里程碑")


def paragraphs():
    doc = Document("项目说明.docx")
    return [p.text.strip() for p in doc.paragraphs if p.text.strip()]


def test_docx_opens_with_title():
    texts = paragraphs()
    assert texts, "文档里没有任何文字"
    assert TITLE in texts[0], f"首段不是大标题：{texts[0]!r}"


def test_sections_exist():
    texts = paragraphs()
    for name in SECTIONS:
        assert any(name in t for t in texts), f"缺少小标题：{name}"


def test_each_section_has_enough_text():
    texts = paragraphs()
    marks = []
    for index, text in enumerate(texts):
        for name in SECTIONS:
            if name in text and all(name not in texts[i] for i in range(index)):
                marks.append((index, name))
    assert len(marks) == len(SECTIONS), f"小标题定位失败：{marks}"
    marks.sort()
    for pos, (index, name) in enumerate(marks):
        end = marks[pos + 1][0] if pos + 1 < len(marks) else len(texts)
        block = "\n".join(texts[index:end]).replace(name, "", 1)
        count = len(re.findall(r"[一-鿿]", block))
        assert count >= 30, f"「{name}」正文只有 {count} 个汉字，要求至少 30 个"

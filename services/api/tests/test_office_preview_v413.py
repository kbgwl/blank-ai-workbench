# -*- coding: utf-8 -*-
"""Office 产物预览：docx / xlsx / pptx 的文本层抽取。

产物能生成却看不见，等于没交付。这里锁住三条：能抽到标题/段落/表格/工作表/
幻灯片文字；坏文件降级成空预览而不是抛错；表格按代码块给出可读对齐。
"""
from __future__ import annotations

from blank_api.services.office_preview import extract_office, is_office


def test_docx_preview_keeps_headings_paragraphs_and_tables(tmp_path):
    from docx import Document

    document = Document()
    document.add_heading("项目计划书", level=1)
    document.add_paragraph("第一段内容。")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "名称"
    table.cell(0, 1).text = "数量"
    table.cell(1, 0).text = "LR10D"
    table.cell(1, 1).text = "3"
    path = tmp_path / "plan.docx"
    document.save(path)

    result = extract_office(path)

    assert "# 项目计划书" in result["text"]
    assert "第一段内容。" in result["text"]
    assert "```text" in result["text"]
    assert "LR10D" in result["text"]
    assert "Word 文档" in result["summary"]
    assert result["truncated"] is False


def test_xlsx_preview_lists_sheets_and_cells(tmp_path):
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "数据"
    sheet.append(["型号", "数量"])
    sheet.append(["LR10D", 3])
    path = tmp_path / "book.xlsx"
    workbook.save(path)

    result = extract_office(path)

    assert "## 工作表：数据" in result["text"]
    assert "LR10D" in result["text"]
    assert "Excel 工作簿" in result["summary"]


def test_pptx_preview_lists_slides_and_text(tmp_path):
    from pptx import Presentation

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[0])
    slide.shapes.title.text = "年度汇报"
    slide.placeholders[1].text = "第一页内容"
    path = tmp_path / "deck.pptx"
    presentation.save(path)

    result = extract_office(path)

    assert "## 第 1 页：年度汇报" in result["text"]
    assert "第一页内容" in result["text"]
    assert "PPT 演示文稿" in result["summary"]


def test_broken_office_file_degrades_to_empty_preview(tmp_path):
    path = tmp_path / "broken.docx"
    path.write_bytes(b"not a zip")

    result = extract_office(path)

    assert result["text"] == ""
    assert result["summary"] == ""


def test_non_office_and_detection():
    assert is_office("a/b/c.docx")
    assert is_office("DECK.PPTX")
    assert not is_office("notes.txt")
    assert extract_office("notes.txt")["text"] == ""

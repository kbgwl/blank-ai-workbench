# -*- coding: utf-8 -*-
"""Office 产物预览：把 docx / xlsx / pptx 抽成 markdown 文本给结果面板看。

真实痛点：任务会生成 Word/Excel/PPT，但文件面板只认文本，点开就是一句
「二进制文件，无法在结果区预览」——产物做出来了却看不见，等于没交付。

这里只做**文本层预览**：标题、段落、表格、工作表、幻灯片文字。公式、图表、
样式不还原——那需要一整套渲染器，不是这一层的职责。抽取有硬上限，坏文件/
超大文件都只返回已经拿到的那一段，绝不让预览接口把后端拖垮。
"""
from __future__ import annotations

from pathlib import Path

MAX_CHARS = 20_000
MAX_TABLE_ROWS = 30
MAX_TABLE_COLS = 12
MAX_SHEETS = 3
MAX_SLIDES = 40
MAX_DOC_PARAGRAPHS = 2_000

OFFICE_SUFFIXES = frozenset({".docx", ".xlsx", ".pptx"})


def is_office(path: str) -> bool:
    return Path(str(path or "")).suffix.lower() in OFFICE_SUFFIXES


def _clip(parts: list[str]) -> tuple[str, bool]:
    text = "\n\n".join(part for part in parts if part.strip())
    if len(text) <= MAX_CHARS:
        return text, False
    return text[:MAX_CHARS] + "\n\n…（预览已截断）", True


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _table(rows: list[list[object]]) -> str:
    if not rows:
        return ""
    header = rows[0]
    body = rows[1:]
    width = max(len(row) for row in rows)
    header = header + [""] * (width - len(header))
    lines = [
        "| " + " | ".join(_cell(item) for item in header[:MAX_TABLE_COLS]) + " |",
        "|" + "|".join([" --- "] * min(width, MAX_TABLE_COLS)) + "|",
    ]
    for row in body[:MAX_TABLE_ROWS]:
        cells = list(row) + [""] * (width - len(row))
        lines.append("| " + " | ".join(_cell(item) for item in cells[:MAX_TABLE_COLS]) + " |")
    # 前端 Markdown 渲染器不认表格语法；包进代码块，至少保证按行列对齐可读，
    # 而不是把每一行挤成一段带竖线的正文。
    return "```text\n" + "\n".join(lines) + "\n```"


def _docx(path: Path) -> tuple[list[str], str]:
    from docx import Document

    document = Document(str(path))
    parts: list[str] = []
    paragraphs = 0
    for paragraph in document.paragraphs[:MAX_DOC_PARAGRAPHS]:
        text = (paragraph.text or "").strip()
        if not text:
            continue
        paragraphs += 1
        style = ""
        try:
            style = (paragraph.style.name or "").lower() if paragraph.style is not None else ""
        except Exception:  # noqa: BLE001 - 个别文档的样式对象读不出来
            style = ""
        if style.startswith("heading") or style.startswith("标题"):
            level = "".join(ch for ch in style if ch.isdigit())
            try:
                hashes = "#" * min(max(int(level or "1"), 1), 6)
            except ValueError:
                hashes = "#"
            parts.append(f"{hashes} {text}")
        elif style.startswith("list") or style.startswith("列表"):
            parts.append(f"- {text}")
        else:
            parts.append(text)
    for index, table in enumerate(document.tables[:10]):
        rows = []
        for row in table.rows[: MAX_TABLE_ROWS + 1]:
            rows.append([cell.text for cell in row.cells[:MAX_TABLE_COLS]])
        rendered = _table(rows)
        if rendered:
            parts.append(f"### 表格 {index + 1}\n\n{rendered}")
    summary = f"Word 文档 · {paragraphs} 段 · {len(document.tables)} 个表格"
    return parts, summary


def _xlsx(path: Path) -> tuple[list[str], str]:
    from openpyxl import load_workbook

    workbook = load_workbook(str(path), read_only=True, data_only=True)
    parts: list[str] = []
    try:
        sheets = list(workbook.worksheets)[:MAX_SHEETS]
        for sheet in sheets:
            rows = []
            for row in sheet.iter_rows(
                min_row=1, max_row=MAX_TABLE_ROWS + 1, max_col=MAX_TABLE_COLS, values_only=True
            ):
                rows.append(list(row))
            rendered = _table(rows)
            parts.append(f"## 工作表：{sheet.title}\n\n{rendered or '（空表）'}")
        summary = f"Excel 工作簿 · {len(workbook.worksheets)} 个工作表 · 预览前 {len(sheets)} 个"
    finally:
        workbook.close()
    return parts, summary


def _pptx(path: Path) -> tuple[list[str], str]:
    from pptx import Presentation

    presentation = Presentation(str(path))
    parts: list[str] = []
    count = 0
    for index, slide in enumerate(presentation.slides):
        if index >= MAX_SLIDES:
            break
        count += 1
        title = ""
        try:
            if slide.shapes.title is not None:
                title = (slide.shapes.title.text or "").strip()
        except Exception:  # noqa: BLE001 - 占位符缺失的幻灯片
            title = ""
        parts.append(f"## 第 {index + 1} 页" + (f"：{title}" if title else ""))
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            for paragraph in shape.text_frame.paragraphs:
                text = (paragraph.text or "").strip()
                if text and text != title:
                    indent = "  " * min(int(getattr(paragraph, "level", 0) or 0), 4)
                    parts.append(f"{indent}- {text}")
    summary = f"PPT 演示文稿 · {len(presentation.slides)} 页 · 预览前 {count} 页"
    return parts, summary


_EXTRACTORS = {".docx": _docx, ".xlsx": _xlsx, ".pptx": _pptx}


def extract_office(path: Path | str) -> dict:
    """抽取 Office 文件的文本预览。返回空 text 表示这份文件抽不出来。"""
    target = Path(path)
    extractor = _EXTRACTORS.get(target.suffix.lower())
    if extractor is None:
        return {"text": "", "truncated": False, "summary": ""}
    try:
        parts, summary = extractor(target)
    except Exception:  # noqa: BLE001 - 坏文件/加密文件一律降级成"看不了"
        return {"text": "", "truncated": False, "summary": ""}
    text, truncated = _clip(parts)
    return {"text": text, "truncated": truncated, "summary": summary}

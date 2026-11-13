"""Markdown → DOCX，用于"把这次回答下载成 Word"。

只覆盖本产品回答里真实出现的语法：标题、段落、列表、表格、粗体、行内代码。
不做完整 Markdown 解析——过度支持反而会在流式输出里产生奇怪的半成品文档。

图片按本地路径嵌入（回答里的插图由工具产生，落在工作区），远程 URL 不下载：
下载外部图片既慢又会把第三方内容写进用户文件，宁可不带图也不静默替换。
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path

from docx import Document
from docx.shared import Inches, Pt, RGBColor

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|!\[[^\]]*\]\([^)]+\)|\[[^\]]+\]\([^)]+\))")
_QUOTE = re.compile(r"^>\s?(.*)$")


def _add_inline(paragraph, text: str) -> None:
    """处理 **粗体**、`代码`、[文字](链接)、![](图片) 这几种行内元素。"""
    for piece in _INLINE.split(text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**") and len(piece) > 4:
            run = paragraph.add_run(piece[2:-2])
            run.bold = True
        elif piece.startswith("`") and piece.endswith("`") and len(piece) > 2:
            run = paragraph.add_run(piece[1:-1])
            run.font.name = "Menlo"
            run.font.size = Pt(9)
        elif piece.startswith("!["):
            match = re.match(r"!\[([^\]]*)\]\(([^)]+)\)", piece)
            if match:
                _add_image(paragraph, match.group(1), match.group(2))
        elif piece.startswith("["):
            match = re.match(r"\[([^\]]+)\]\(([^)]+)\)", piece)
            if match:
                run = paragraph.add_run(match.group(1))
                run.font.color.rgb = RGBColor(0x1A, 0x4F, 0x8B)
                run.underline = True
            else:
                paragraph.add_run(piece)
        else:
            paragraph.add_run(piece)


def _add_image(paragraph, alt: str, source: str) -> None:
    """只嵌入工作区内的本地图片；远程 URL 退化成一行说明。"""
    path = Path(os.path.expanduser(source))
    if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".bmp"}:
        try:
            paragraph.add_run().add_picture(str(path), width=Inches(6.2))
            return
        except Exception:  # noqa: BLE001 - 坏图不该让导出失败
            pass
    run = paragraph.add_run(f"（图片：{alt or source}）")
    run.italic = True


def _flush_table(document, rows: list[list[str]]) -> None:
    if not rows:
        return
    width = max(len(row) for row in rows)
    table = document.add_table(rows=0, cols=width)
    table.style = "Table Grid"
    for row in rows:
        cells = table.add_row().cells
        for index in range(width):
            text = row[index] if index < len(row) else ""
            cells[index].text = ""
            _add_inline(cells[index].paragraphs[0], text)


def markdown_to_docx(markdown: str, *, title: str = "", subtitle: str = "") -> bytes:
    """把一段 Markdown 转成 .docx 字节。"""
    document = Document()
    style = document.styles["Normal"]
    style.font.name = "Helvetica"
    style.font.size = Pt(10.5)

    if title:
        document.add_heading(title, level=0)
    if subtitle:
        paragraph = document.add_paragraph()
        run = paragraph.add_run(subtitle)
        run.italic = True
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)

    table_rows: list[list[str]] = []

    def flush() -> None:
        nonlocal table_rows
        if table_rows:
            _flush_table(document, table_rows)
            table_rows = []

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if _TABLE_ROW.match(line):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if _TABLE_SEP.match(line):
                continue
            table_rows.append(cells)
            continue
        flush()

        stripped = line.strip()
        if not stripped:
            continue
        heading = _HEADING.match(stripped)
        if heading:
            level = min(len(heading.group(1)), 4)
            document.add_heading(heading.group(2).strip(), level=level)
            continue
        quote = _QUOTE.match(stripped)
        if quote:
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.left_indent = Inches(0.3)
            _add_inline(paragraph, quote.group(1))
            continue
        bullet = _BULLET.match(line)
        if bullet:
            paragraph = document.add_paragraph(style="List Bullet")
            _add_inline(paragraph, bullet.group(1))
            continue
        ordered = _ORDERED.match(line)
        if ordered:
            paragraph = document.add_paragraph(style="List Number")
            _add_inline(paragraph, ordered.group(2))
            continue
        paragraph = document.add_paragraph()
        _add_inline(paragraph, stripped)

    flush()
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()

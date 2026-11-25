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
    """只嵌入工作区内的本地图片；远程 URL 退化成一行说明。

    ``alt`` 支持 ``说明|5.6`` 这种写法指定图片宽度（英寸）：长条形图（期刊榜、关键词榜）
    按 6.2in 铺满会撑掉整页，报告里需要按图型给不同宽度。
    """
    label = alt
    width = 6.2
    if "|" in alt:
        label, _, raw = alt.rpartition("|")
        try:
            width = min(max(float(raw), 2.0), 6.2)
        except ValueError:
            width = 6.2
    path = Path(os.path.expanduser(source))
    if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".bmp"}:
        try:
            paragraph.add_run().add_picture(str(path), width=Inches(width))
            return
        except Exception:  # noqa: BLE001 - 坏图不该让导出失败
            pass
    run = paragraph.add_run(f"（图片：{label or source}）")
    run.italic = True


#: 表格总宽（英寸）。Word 默认页宽 8.5in，左右各 1in 边距，留一点余量。
_TABLE_WIDTH_INCHES = 6.2


def _column_inches(rows: list[list[str]], width: int) -> list[float]:
    """按每列最长内容分配列宽。

    平均分配会把“标题”列压成一条竖排字（文献表尤其明显），所以用内容长度做权重，
    每列至少 0.55in，最长内容截到 60 字符以免一列吃掉整张表。
    """
    weights: list[float] = []
    for column in range(width):
        longest = max((len(str(row[column])) if column < len(row) else 0) for row in rows)
        weights.append(float(min(max(longest, 6), 60)))
    total = sum(weights) or 1.0
    raw = [_TABLE_WIDTH_INCHES * weight / total for weight in weights]
    floor = 0.55
    scaled = [max(value, floor) for value in raw]
    overflow = sum(scaled) - _TABLE_WIDTH_INCHES
    if overflow > 0:
        # 从小列里按比例扣回来，扣不动就让它超一点（Word 会自动压缩）。
        flexible = [index for index, value in enumerate(scaled) if value > floor]
        for index in flexible:
            scaled[index] = max(floor, scaled[index] - overflow * (scaled[index] / sum(scaled[i] for i in flexible)))
    return scaled


def _fixed_layout(table) -> None:
    """告诉 Word 用固定列宽：只设 cell.width 不够，autofit 会把宽度再算回去。"""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    properties = table._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    properties.append(layout)


def _repeat_header(row) -> None:
    """跨页时重复表头：长表翻页后没有列名，读者就不知道在看什么。"""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    properties = row._tr.get_or_add_trPr()
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "true")
    properties.append(header)


def _flush_table(document, rows: list[list[str]]) -> None:
    if not rows:
        return
    width = max(len(row) for row in rows)
    table = document.add_table(rows=0, cols=width)
    table.style = "Table Grid"
    table.autofit = False
    _fixed_layout(table)
    inches = _column_inches(rows, width)
    for index, value in enumerate(inches):
        table.columns[index].width = Inches(value)
    for row in rows:
        cells = table.add_row().cells
        for index in range(width):
            text = row[index] if index < len(row) else ""
            cells[index].text = ""
            cells[index].width = Inches(inches[index])
            _add_inline(cells[index].paragraphs[0], text)
    if len(rows) > 1:
        _repeat_header(table.rows[0])


def _add_page_number(document) -> None:
    """页脚居中页码：用 PAGE 域，Word/LibreOffice 都会自动算。"""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    footer = document.sections[0].footer
    paragraph = footer.paragraphs[0]
    paragraph.alignment = 1  # center
    run = paragraph.add_run()
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    run._r.addnext(field)
    paragraph.add_run("")


def _add_toc(document, entries: list[tuple[int, str, int]]) -> None:
    """静态目录：标题 + 右对齐点线 + 页码（页码由调用方从 PDF 里量出来）。"""
    from docx.enum.text import WD_TAB_ALIGNMENT, WD_TAB_LEADER

    document.add_heading("目录", level=1)
    for level, text, page in entries:
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(2)
        paragraph.paragraph_format.left_indent = Inches(0.18 * max(level - 1, 0))
        paragraph.paragraph_format.tab_stops.add_tab_stop(
            Inches(6.2), WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS
        )
        run = paragraph.add_run(text.strip())
        run.font.size = Pt(9.5)
        page_run = paragraph.add_run("" if page in (None, "") else f"\t{page}")
        page_run.font.size = Pt(9.5)
    document.add_page_break()


def _set_cjk_font(document, name: str = "PingFang SC") -> None:
    """把东亚字体写进 docDefaults **和每个段落样式**。

    只改 docDefaults 不够：Word 默认模板的 Normal / Heading / Title 样式里各自带
    ``w:eastAsiaTheme``，主题字体优先级更高，LibreOffice 于是拿一个没有中文字形的
    西文字体去排中文——PDF 里中文整段消失（文字层还在，字形没有）。
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.enum.style import WD_STYLE_TYPE

    theme_attributes = ("w:eastAsiaTheme", "w:asciiTheme", "w:hAnsiTheme", "w:cstheme")

    def apply(rpr) -> None:
        if rpr is None:
            return
        fonts = rpr.find(qn("w:rFonts"))
        if fonts is None:
            fonts = OxmlElement("w:rFonts")
            rpr.insert(0, fonts)
        for attribute in theme_attributes:
            fonts.attrib.pop(qn(attribute), None)
        fonts.set(qn("w:eastAsia"), name)
        if not fonts.get(qn("w:ascii")):
            fonts.set(qn("w:ascii"), "Helvetica")
        if not fonts.get(qn("w:hAnsi")):
            fonts.set(qn("w:hAnsi"), "Helvetica")

    styles_root = document.styles.element
    apply(styles_root.find(qn("w:docDefaults") + "/" + qn("w:rPrDefault") + "/" + qn("w:rPr")))
    for style in document.styles:
        if style.type != WD_STYLE_TYPE.PARAGRAPH:
            continue
        element = style.element
        rpr = element.find(qn("w:rPr"))
        if rpr is None:
            rpr = OxmlElement("w:rPr")
            element.append(rpr)
        apply(rpr)


def markdown_to_docx(markdown: str, *, title: str = "", subtitle: str = "",
                     toc: list[tuple[int, str, int]] | None = None,
                     page_numbers: bool = False) -> bytes:
    """把一段 Markdown 转成 .docx 字节。"""
    document = Document()
    if page_numbers:
        _add_page_number(document)
    if toc:
        _add_toc(document, toc)
    style = document.styles["Normal"]
    style.font.name = "Helvetica"
    style.font.size = Pt(10.5)
    _set_cjk_font(document)

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

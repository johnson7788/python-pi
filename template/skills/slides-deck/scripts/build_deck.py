#!/usr/bin/env python3
"""Build an editable .pptx from a JSON deck spec. Python standard library only.

Why hand-rolled OOXML instead of python-pptx: this script ships inside a skill and
must run with whatever ``python3`` the deployment has.  A pptx is a zip of XML, so
the writer below assembles a small, fixed package skeleton (master / blank layout /
theme) and appends one slide part per page.  All geometry is computed here, never
by the model: coordinates and font sizes do not round-trip through tokens.

Subcommands::

    build_deck.py build spec.json -o deck.pptx
    build_deck.py check spec.json          # no file written; prints metrics
    build_deck.py layouts                  # list layout names + required fields
    build_deck.py doctor                   # environment / theme asset self-check
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from dataclasses import dataclass, field
from typing import Any, Sequence

EMU = 914400
SLIDE_W = 12192000  # exact 16:9 (13.333 in)
SLIDE_W_IN = SLIDE_W / EMU
SLIDE_H = 6858000
SLIDE_H_IN = SLIDE_H / EMU  # 7.5


def E(inches: float) -> int:
    return int(round(inches * EMU))


PT = 12700  # EMU per point

ML = 0.9
MR = 0.9
CONTENT_W = SLIDE_W_IN - ML - MR
CONTENT_BOTTOM = 6.75
FOOTER_RULE_Y = 6.98
FOOTER_TEXT_Y = 7.06

DEFAULT_FONTS = {"latin": "Arial", "ea": "Microsoft YaHei"}

# One dominant colour, one supporting tone, one accent — never equal weight.  The
# palettes are content-informed rather than a single brand blue: pick the one that
# matches the subject, then the whole deck (cover, section, cards, charts) follows.
PRESETS: dict[str, dict[str, str]] = {
    "midnight": {
        "deep": "1E2761", "deep_alt": "171E4E",
        "canvas": "FFFFFF", "tint": "F4F5F9",
        "ink": "12162B", "muted": "5F6577",
        "accent": "31418A", "accent_light": "AAB6E0", "accent_soft": "E7EAF6",
        "line": "E2E5EC", "on_deep": "FFFFFF",
    },
    "forest": {
        "deep": "1F3D2B", "deep_alt": "183222",
        "canvas": "FFFFFF", "tint": "F3F6F2",
        "ink": "16241B", "muted": "5C6B60",
        "accent": "2C5F2D", "accent_light": "9FC08F", "accent_soft": "E8F0E4",
        "line": "E1E7DF", "on_deep": "FFFFFF",
    },
    "terracotta": {
        "deep": "4A2A22", "deep_alt": "3C221B",
        "canvas": "FFFFFF", "tint": "F8F4F2",
        "ink": "241A17", "muted": "6E5B54",
        "accent": "B85042", "accent_light": "E0B3AA", "accent_soft": "F6E8E3",
        "line": "EADFD9", "on_deep": "FFFFFF",
    },
    "teal": {
        "deep": "04364A", "deep_alt": "032C3D",
        "canvas": "FFFFFF", "tint": "F1F5F6",
        "ink": "0B2027", "muted": "55707A",
        "accent": "028090", "accent_light": "8FC7CE", "accent_soft": "E0F0F1",
        "line": "DDE7E9", "on_deep": "FFFFFF",
    },
    "charcoal": {
        "deep": "22262B", "deep_alt": "1A1D21",
        "canvas": "FFFFFF", "tint": "F4F5F6",
        "ink": "17191C", "muted": "646A72",
        "accent": "36454F", "accent_light": "AEB8C0", "accent_soft": "ECEEF0",
        "line": "E2E4E7", "on_deep": "FFFFFF",
    },
}

LAYOUTS = {
    "cover": ("title", ["subtitle", "eyebrow", "meta", "image"]),
    "section": ("title", ["number", "subtitle"]),
    "agenda": ("items", ["title"]),
    "bullets": ("bullets", ["title", "kicker", "lede", "note"]),
    "two_col": ("title", ["left", "right", "note"]),
    "stats": ("stats", ["title", "kicker", "caption", "note"]),
    "big_number": ("number", ["title", "kicker", "caption", "bullets", "stats", "note"]),
    "quote": ("text", ["source"]),
    "timeline": ("items", ["title", "kicker"]),
    "table": ("columns", ["title", "kicker", "rows", "note"]),
    "image_text": ("title", ["image", "kicker", "paragraphs", "note"]),
    "closing": ("title", ["subtitle", "meta"]),
}

CONTENT_LAYOUTS = {"agenda", "bullets", "two_col", "stats", "big_number", "quote", "timeline", "table", "image_text"}


# ── text metrics ───────────────────────────────────────────────────────────────


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x2E80 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0xFF00 <= code <= 0xFF60
        or 0x3000 <= code <= 0x303F
    )


def _char_em(ch: str) -> float:
    if ch == " ":
        return 0.30
    if _is_cjk(ch):
        return 1.0
    if ch in "ilj|.,:;'!`":
        return 0.30
    if ch in "mwMW@%":
        return 0.86
    if ch.isupper() or ch.isdigit():
        return 0.62
    return 0.52


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    buf = ""
    for ch in text:
        if _is_cjk(ch) or ch.isspace():
            if buf:
                out.append(buf)
                buf = ""
            out.append(" " if ch.isspace() else ch)
        else:
            buf += ch
    if buf:
        out.append(buf)
    return out


def wrap_lines(text: str, width_emu: int, size_pt: float, bold: bool = False) -> int:
    if not text:
        return 1
    lines = 1
    used = 0.0
    bold_k = 1.04 if bold else 1.0
    for token in _tokens(text):
        weight = sum(_char_em(c) for c in token) * size_pt * PT * bold_k
        if token == " ":
            if used:
                used += weight
            continue
        if used + weight > width_emu and used > 0:
            lines += 1
            used = 0.0
        used += weight
        while used > width_emu:
            lines += 1
            used -= width_emu
    return lines


def text_height(text: str, width_emu: int, size_pt: float, factor: float, bold: bool = False) -> float:
    return wrap_lines(text, width_emu, size_pt, bold) * size_pt * factor / 72.0


def fit_size(
    paragraphs: Sequence[tuple[str, bool]],
    width_emu: int,
    height_emu: int,
    max_pt: float,
    min_pt: float,
    line_factor: float = 1.42,
    gap_pt: float = 0.0,
) -> tuple[float, int]:
    size = float(int(max_pt))
    while size >= min_pt:
        lines = sum(wrap_lines(t, width_emu, size, b) for t, b in paragraphs)
        needed = lines * size * line_factor * PT + gap_pt * PT * max(0, len(paragraphs) - 1)
        if needed <= height_emu:
            return size, lines
        size -= 1.0
    lines = sum(wrap_lines(t, width_emu, min_pt, b) for t, b in paragraphs)
    return min_pt, lines


def split_lead(text: str) -> tuple[str, str]:
    for sep in ("：", ":"):
        if sep in text:
            head, tail = text.split(sep, 1)
            if 0 < len(head.strip()) <= 26 and tail.strip():
                return head.strip(), tail.strip()
    return text.strip(), ""


# ── xml helpers ───────────────────────────────────────────────────────────────


def esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def runs(text: str) -> list[tuple[str, bool]]:
    parts = str(text).split("**")
    out: list[tuple[str, bool]] = []
    for index, part in enumerate(parts):
        if part:
            out.append((part, index % 2 == 1))
    return out or [("", False)]


def color(value: str, alpha: int | None = None) -> str:
    body = f'<a:srgbClr val="{value}"/>'
    if alpha is not None:
        body = f'<a:srgbClr val="{value}"><a:alpha val="{int(alpha)}"/></a:srgbClr>'
    return f"<a:solidFill>{body}</a:solidFill>"


@dataclass
class Style:
    fonts: dict[str, str]
    size_pt: float = 18
    bold: bool = False
    color: str = "000000"
    align: str = "l"
    line: float = 1.4
    space_before: float = 0
    bullet: str = ""
    bullet_color: str = ""
    italic: bool = False
    caps: bool = False
    spc: float = 0

    def rpr(self, run_color: str, run_bold: bool) -> str:
        attrs = f'sz="{int(round(self.size_pt * 100))}"'
        if run_bold:
            attrs += ' b="1"'
        if self.italic:
            attrs += ' i="1"'
        if self.caps:
            attrs += ' cap="all"'
        if self.spc:
            attrs += f' spc="{int(self.spc * 100)}"'
        attrs += ' dirty="0" lang="zh-CN"'
        return (
            f"<a:rPr {attrs}>{color(run_color)}"
            f'<a:latin typeface="{esc(self.fonts["latin"])}"/>'
            f'<a:ea typeface="{esc(self.fonts["ea"])}"/>'
            f"</a:rPr>"
        )


def paragraph(text: str, style: Style) -> str:
    attrs = f'algn="{style.align}"'
    if style.bullet:
        attrs += ' marL="257175" indent="-257175"'
    ppr = f"<a:pPr {attrs}>"
    if style.line:
        ppr += f'<a:lnSpc><a:spcPct val="{int(style.line * 100000)}"/></a:lnSpc>'
    if style.space_before:
        ppr += f'<a:spcBef><a:spcPts val="{int(style.space_before * 100)}"/></a:spcBef>'
    if style.bullet:
        fill = color(style.bullet_color or style.color)
        ppr += (
            "<a:buClr>"
            + fill.replace("<a:solidFill>", "").replace("</a:solidFill>", "")
            + "</a:buClr>"
            f'<a:buFont typeface="Arial"/><a:buChar char="{esc(style.bullet)}"/>'
        )
    ppr += "</a:pPr>"
    body = ""
    for part, bold in runs(text):
        body += f"<a:r>{style.rpr(style.color, bold or style.bold)}<a:t>{esc(part)}</a:t></a:r>"
    if not body:
        body = f'<a:endParaRPr lang="zh-CN" sz="{int(style.size_pt * 100)}"/>'
    return f"<a:p>{ppr}{body}</a:p>"


def text_block(
    shapes: list[str],
    shape_id: int,
    name: str,
    x: float,
    y: float,
    w: float,
    h: float,
    paragraphs_xml: str,
    anchor: str = "t",
    insets: tuple[float, float, float, float] = (0, 0, 0, 0),
) -> None:
    left, top, right, bottom = insets
    shapes.append(
        f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id + 1}" name="{esc(name)}"/>'
        '<p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        "<p:spPr>"
        f'<a:xfrm><a:off x="{E(x)}" y="{E(y)}"/><a:ext cx="{E(w)}" cy="{E(h)}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/>'
        "</p:spPr>"
        "<p:txBody>"
        f'<a:bodyPr wrap="square" lIns="{E(left)}" tIns="{E(top)}" rIns="{E(right)}" bIns="{E(bottom)}" '
        f'anchor="{anchor}" rtlCol="0"><a:noAutofit/></a:bodyPr>'
        "<a:lstStyle/>"
        f"{paragraphs_xml}"
        "</p:txBody></p:sp>"
    )


def rect(
    shapes: list[str],
    shape_id: int,
    name: str,
    x: float,
    y: float,
    w: float,
    h: float,
    fill: str | None = None,
    alpha: int | None = None,
    line: str | None = None,
    line_w: float = 0.75,
    prst: str = "rect",
) -> None:
    fill_xml = color(fill, alpha) if fill else "<a:noFill/>"
    line_xml = (
        f'<a:ln w="{int(round(line_w * PT))}">{color(line)}</a:ln>'
        if line
        else "<a:ln><a:noFill/></a:ln>"
    )
    shapes.append(
        f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id + 1}" name="{esc(name)}"/>'
        "<p:cNvSpPr/><p:nvPr/></p:nvSpPr>"
        "<p:spPr>"
        f'<a:xfrm><a:off x="{E(x)}" y="{E(y)}"/><a:ext cx="{max(1, E(w))}" cy="{max(1, E(h))}"/></a:xfrm>'
        f'<a:prstGeom prst="{prst}"><a:avLst/></a:prstGeom>'
        f"{fill_xml}{line_xml}"
        "</p:spPr>"
        '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="zh-CN"/></a:p></p:txBody>'
        "</p:sp>"
    )


def hairline(shapes: list[str], shape_id: int, x: float, y: float, w: float, fill: str) -> None:
    rect(shapes, shape_id, "Hairline", x, y, w, 0.009, fill=fill)


# ── images ────────────────────────────────────────────────────────────────────


def image_size(path: str) -> tuple[int, int]:
    """Width/height from PNG or JPEG headers — no Pillow in a stdlib-only script."""
    with open(path, "rb") as handle:
        head = handle.read(32)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
        if head[:2] == b"\xff\xd8":
            handle.seek(2)
            while True:
                marker = handle.read(2)
                if len(marker) < 2:
                    break
                if marker[0] != 0xFF:
                    continue
                code = marker[1]
                if code in (0xD8, 0xD9):
                    continue
                length = int.from_bytes(handle.read(2), "big")
                if code in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    data = handle.read(5)
                    return int.from_bytes(data[3:5], "big"), int.from_bytes(data[1:3], "big")
                handle.seek(length - 2, 1)
    raise SystemExit(f"unsupported image (need PNG or JPEG): {path}")


def crop_for(frame_w: float, frame_h: float, image: tuple[int, int]) -> tuple[int, int, int, int]:
    """srcRect values (1000ths of a percent) that cover the frame without distortion."""
    iw, ih = image
    frame_ratio = frame_w / frame_h
    image_ratio = iw / ih
    if image_ratio > frame_ratio:
        excess = 1 - frame_ratio / image_ratio
        side = int(round(excess * 50000))
        return side, 0, side, 0
    excess = 1 - image_ratio / frame_ratio
    side = int(round(excess * 50000))
    return 0, side, 0, side


def picture(
    shapes: list[str],
    shape_id: int,
    x: float,
    y: float,
    w: float,
    h: float,
    rel_id: str,
    image: tuple[int, int],
    alpha: int | None = None,
) -> None:
    left, top, right, bottom = crop_for(w, h, image)
    blip = f'<a:blip r:embed="{rel_id}"/>'
    shapes.append(
        f'<p:pic><p:nvPicPr><p:cNvPr id="{shape_id + 1}" name="Picture"/>'
        '<p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>'
        "<p:blipFill>"
        f"{blip}<a:srcRect l=\"{left}\" t=\"{top}\" r=\"{right}\" b=\"{bottom}\"/>"
        "<a:stretch><a:fillRect/></a:stretch></p:blipFill>"
        "<p:spPr>"
        f'<a:xfrm><a:off x="{E(x)}" y="{E(y)}"/><a:ext cx="{E(w)}" cy="{E(h)}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>'
    )


# ── table ─────────────────────────────────────────────────────────────────────


def table_xml(
    shape_id: int,
    x: float,
    y: float,
    w: float,
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
    st: dict[str, str],
    fonts: dict[str, str],
) -> str:
    ncol = max(1, len(columns))
    col_w = w / ncol
    row_h = 0.56
    total_h = row_h * (len(rows) + 1)

    def cell(text: str, header: bool, row_index: int) -> str:
        size = 11.0 if header else 14.0
        run_color = st["muted"] if header else st["ink"]
        parts: list[str] = []
        for part, run_bold in runs(text):
            b_attr = "1" if (header or run_bold) else "0"
            parts.append(
                '<a:r><a:rPr sz="%d" b="%s" dirty="0" lang="zh-CN">%s'
                '<a:latin typeface="%s"/><a:ea typeface="%s"/></a:rPr>'
                "<a:t>%s</a:t></a:r>"
                % (int(size * 100), b_attr, color(run_color),
                   esc(fonts["latin"]), esc(fonts["ea"]), esc(part))
            )
        body = "".join(parts) or '<a:endParaRPr lang="zh-CN"/>'
        border = (
            f'<a:lnB w="19050">{color(st["accent"])}</a:lnB>'
            if header
            else f'<a:lnB w="6350">{color(st["line"])}</a:lnB>'
        )
        return (
            "<a:tc>"
            f'<a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:pPr algn="l"/>'
            f"{body}</a:p></a:txBody>"
            '<a:tcPr marL="0" marR="101600" marT="82550" marB="82550" anchor="b">'
            '<a:lnL w="0"><a:noFill/></a:lnL><a:lnR w="0"><a:noFill/></a:lnR>'
            f'<a:lnT w="0"><a:noFill/></a:lnT>{border}<a:noFill/></a:tcPr>'
            "</a:tc>"
        )

    grid = "".join(f'<a:gridCol w="{E(col_w)}"/>' for _ in range(ncol))
    head = '<a:tr h="%d">%s</a:tr>' % (
        E(row_h),
        "".join(cell(str(c), True, 0) for c in list(columns) + [""] * (ncol - len(columns))),
    )
    body_rows = "".join(
        '<a:tr h="%d">%s</a:tr>'
        % (E(row_h), "".join(cell(str(row[i]) if i < len(row) else "", False, r) for i in range(ncol)))
        for r, row in enumerate(rows)
    )
    return (
        "<p:graphicFrame>"
        f'<p:nvGraphicFramePr><p:cNvPr id="{shape_id + 1}" name="Table"/>'
        '<p:cNvGraphicFramePr><a:graphicFrameLocks noGrp="1"/></p:cNvGraphicFramePr>'
        "<p:nvPr/></p:nvGraphicFramePr>"
        f'<p:xfrm><a:off x="{E(x)}" y="{E(y)}"/><a:ext cx="{E(w)}" cy="{E(total_h)}"/></p:xfrm>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">'
        '<a:tbl><a:tblPr firstRow="0" bandRow="0"/><a:tblGrid>'
        f"{grid}</a:tblGrid>{head}{body_rows}</a:tbl>"
        "</a:graphicData></a:graphic></p:graphicFrame>"
    )


# ── page chrome ───────────────────────────────────────────────────────────────


def canvas(shapes: list[str], st: dict[str, str]) -> None:
    rect(shapes, 1, "Canvas", 0, 0, SLIDE_W_IN, SLIDE_H_IN, fill=st["canvas"])


def footer(shapes: list[str], sid: int, st, fonts, index: int, total: int, label: str) -> None:
    hairline(shapes, sid, ML, FOOTER_RULE_Y, CONTENT_W, st["line"])
    style = Style(fonts=fonts, size_pt=9, color=st["muted"], line=1.0)
    text_block(shapes, sid + 1, "Footer left", ML, FOOTER_TEXT_Y, CONTENT_W * 0.65, 0.26,
               paragraph(label, style))
    style.align = "r"
    text_block(shapes, sid + 2, "Footer page", ML + CONTENT_W * 0.65, FOOTER_TEXT_Y,
               CONTENT_W * 0.35, 0.26, paragraph(f"{index:02d} / {total:02d}", style))



def balance_lines(text: str, size_pt: float, width_emu: int) -> list[str]:
    """Break a long title into two balanced lines, preferring a punctuation break."""
    if wrap_lines(text, width_emu, size_pt, True) <= 1:
        return [text]
    n = len(text)

    def fits_at(i: int) -> tuple[float, list[str]] | None:
        left, right = text[:i].strip(), text[i:].strip()
        if not left or not right:
            return None
        w1 = sum(_char_em(c) for c in left) * size_pt * PT * 1.04
        w2 = sum(_char_em(c) for c in right) * size_pt * PT * 1.04
        if max(w1, w2) > width_emu:
            return None
        return max(w1, w2), [left, right]

    for i in range(max(1, int(n * 0.3)), min(n - 1, int(n * 0.72)) + 1):
        if text[i - 1] in "，、：；。！？":
            candidate = fits_at(i)
            if candidate is not None:
                return candidate[1]

    best: tuple[float, list[str]] | None = None
    for i in range(max(1, int(n * 0.3)), min(n - 1, int(n * 0.72)) + 1):
        candidate = fits_at(i)
        if candidate is None:
            continue
        if best is None or candidate[0] < best[0]:
            best = candidate
    return best[1] if best else [text]


def fit_title(
    text: str,
    width_in: float,
    max_pt: float,
    min_pt: float,
    max_lines: int = 2,
    prefer_one_line: bool = False,
) -> tuple[float, list[str]]:
    width = E(width_in)
    if prefer_one_line:
        size = float(int(max_pt))
        while size >= max(min_pt, max_pt * 0.66):
            if wrap_lines(text, width, size, True) == 1:
                return size, [text]
            size -= 1
    size = float(int(max_pt))
    while size >= min_pt:
        lines = balance_lines(text, size, width)
        if len(lines) <= max_lines and all(wrap_lines(line, width, size, True) == 1 for line in lines):
            return size, lines
        size -= 1
    return min_pt, balance_lines(text, min_pt, width)[:max_lines]


def title_block(shapes: list[str], sid: int, st, fonts, page: dict, max_pt: float = 46) -> float:
    width = CONTENT_W * 0.94
    kicker = str(page.get("kicker") or "").strip()
    y = 0.62
    if kicker:
        ks = Style(fonts=fonts, size_pt=10.5, color=st["accent"], bold=True, caps=True, spc=1.8, line=1.0)
        text_block(shapes, sid, "Kicker", ML, y, width, 0.28, paragraph(kicker, ks))
        sid += 1
        y += 0.40
    text = str(page.get("title") or "").strip()
    size, lines = fit_title(text, CONTENT_W * 0.94, max_pt, 28, max_lines=2, prefer_one_line=True)
    ts = Style(fonts=fonts, size_pt=size, color=st["ink"], bold=True, line=1.14)
    title_h = len(lines) * size * 1.14 / 72.0 + 0.04
    text_block(shapes, sid, "Title", ML, y, width, title_h,
               "".join(paragraph(line, ts) for line in lines))
    # Deliberately no accent rule: an underline under a title is the single most
    # recognisable "AI slide" tell.  Whitespace separates the title from the body.
    return max(2.1, y + title_h + 0.62)


# ── layouts ───────────────────────────────────────────────────────────────────


def slide_cover(page, st, fonts, meta, image=None) -> tuple[list[str], dict[str, str]]:
    shapes: list[str] = []
    rect(shapes, 1, "Background", 0, 0, SLIDE_W_IN, SLIDE_H_IN, fill=st["deep"])
    sid = 10
    if image:
        picture(shapes, 3, 0, 0, SLIDE_W_IN, SLIDE_H_IN, image["rel"], image["size"])
        rect(shapes, 4, "Scrim", 0, 0, SLIDE_W_IN, SLIDE_H_IN, fill="000000", alpha=58000)
        on_deep = "FFFFFF"
    else:
        on_deep = st["on_deep"]

    eyebrow = str(page.get("eyebrow") or meta.get("brand") or "").strip()
    if eyebrow:
        es = Style(fonts=fonts, size_pt=11, color=st["accent_light"], bold=True, caps=True, spc=2.4, line=1.0)
        text_block(shapes, sid, "Eyebrow", ML, 1.5, 8.0, 0.3, paragraph(eyebrow, es))
        sid += 1

    title = str(page.get("title") or meta.get("title") or "").strip()
    size, lines = fit_title(title, 10.4, 60, 38, max_lines=2)
    ts = Style(fonts=fonts, size_pt=size, color=on_deep, bold=True, line=1.1)
    title_h = len(lines) * size * 1.1 / 72.0 + 0.05
    text_block(shapes, sid, "Title", ML, 1.95, 10.4, title_h,
               "".join(paragraph(line, ts) for line in lines))
    sid += 1

    subtitle = str(page.get("subtitle") or meta.get("subtitle") or "").strip()
    if subtitle:
        ss = Style(fonts=fonts, size_pt=18, color=on_deep, line=1.5)
        text_block(shapes, sid, "Subtitle", ML, 1.95 + title_h + 0.42, 9.2, 1.0,
                   paragraph(subtitle, ss))
        shapes[-1] = shapes[-1].replace(color(on_deep), color(on_deep, 78000), 1)
        sid += 1

    meta_text = str(page.get("meta") or meta.get("date") or "").strip()
    if meta_text:
        fs = Style(fonts=fonts, size_pt=11, color=on_deep, line=1.8)
        body = "".join(paragraph(line, fs) for line in meta_text.splitlines() if line.strip())
        text_block(shapes, sid, "Meta", ML, 5.95, 7.0, 0.9, body)
        shapes[-1] = shapes[-1].replace(color(on_deep), color(on_deep, 68000), 1)
    return shapes, st


def slide_section(page, st, fonts, index) -> tuple[list[str], dict[str, str]]:
    shapes: list[str] = []
    rect(shapes, 1, "Background", 0, 0, SLIDE_W_IN, SLIDE_H_IN, fill=st["accent"])
    number = str(page.get("number") or f"{index:02d}").strip()
    ns = Style(fonts=fonts, size_pt=300, color="FFFFFF", bold=True, align="r", line=0.9)
    text_block(shapes, 2, "Ghost number", 7.2, 0.85, 5.6, 4.4, paragraph(number, ns))
    shapes[-1] = shapes[-1].replace(color("FFFFFF"), color("FFFFFF", 16000), 1)

    ks = Style(fonts=fonts, size_pt=10.5, color="FFFFFF", bold=True, caps=True, spc=2.4, line=1.0)
    text_block(shapes, 3, "Kicker", ML, 1.95, 6.4, 0.3, paragraph(f"Section {number}", ks))
    shapes[-1] = shapes[-1].replace(color("FFFFFF"), color("FFFFFF", 72000), 1)

    title = str(page.get("title") or "").strip()
    size, lines = fit_title(title, 6.6, 46, 30, max_lines=2, prefer_one_line=True)
    ts = Style(fonts=fonts, size_pt=size, color="FFFFFF", bold=True, line=1.16)
    title_h = len(lines) * size * 1.16 / 72.0 + 0.05
    text_block(shapes, 4, "Title", ML, 2.38, 6.6, title_h,
               "".join(paragraph(line, ts) for line in lines))
    subtitle = str(page.get("subtitle") or "").strip()
    if subtitle:
        ss = Style(fonts=fonts, size_pt=16, color="FFFFFF", line=1.55)
        text_block(shapes, 5, "Subtitle", ML, 2.38 + title_h + 0.34, 6.2, 1.1, paragraph(subtitle, ss))
        shapes[-1] = shapes[-1].replace(color("FFFFFF"), color("FFFFFF", 76000), 1)
    return shapes, st


def slide_agenda(page, st, fonts, index, total, label) -> tuple[list[str], dict[str, str]]:
    shapes: list[str] = []
    canvas(shapes, st)
    top = title_block(shapes, 10, st, fonts, page, max_pt=44)
    items = [str(item) for item in page.get("items") or []] or ["（暂无条目）"]
    columns = 1 if len(items) <= 5 else 2
    per_col = (len(items) + columns - 1) // columns
    col_w = (CONTENT_W - (columns - 1) * 0.9) / columns
    avail = CONTENT_BOTTOM - top
    row_h = min(1.24, avail / per_col)
    for col in range(columns):
        chunk = items[col * per_col:(col + 1) * per_col]
        x = ML + col * (col_w + 0.9)
        y = top + max(0.0, (avail - row_h * len(chunk)) / 2)
        for offset, item in enumerate(chunk):
            number = f"{col * per_col + offset + 1:02d}"
            lead, sub = split_lead(item)
            base = 30 + col * 40 + offset * 6
            ns = Style(fonts=fonts, size_pt=34, color=st["accent"], bold=True, line=1.0)
            text_block(shapes, base, "Agenda number", x, y - 0.04, 1.0, 0.6, paragraph(number, ns))
            ts = Style(fonts=fonts, size_pt=22, color=st["ink"], bold=True, line=1.2)
            text_block(shapes, base + 1, "Agenda item", x + 1.1, y, col_w - 1.2, 0.44,
                       paragraph(lead, ts))
            if sub:
                ss = Style(fonts=fonts, size_pt=13.5, color=st["muted"], line=1.35)
                text_block(shapes, base + 2, "Agenda sub", x + 1.1, y + 0.44, col_w - 1.2, 0.34,
                           paragraph(sub, ss))
            hairline(shapes, base + 3, x, y + row_h - 0.18, col_w, st["line"])
            y += row_h
    footer(shapes, 120, st, fonts, index, total, label)
    return shapes, st


def slide_bullets(page, st, fonts, index, total, label) -> tuple[list[str], dict[str, str]]:
    shapes: list[str] = []
    canvas(shapes, st)
    top = title_block(shapes, 10, st, fonts, page)
    lead_text = str(page.get("lede") or "").strip()
    if lead_text:
        ls = Style(fonts=fonts, size_pt=16.5, color=st["muted"], line=1.65)
        lead_h = text_height(lead_text, E(CONTENT_W * 0.72), 16.5, 1.65)
        text_block(shapes, 20, "Lede", ML, top - 0.22, CONTENT_W * 0.72, lead_h, paragraph(lead_text, ls))
        top += lead_h + 0.34

    items = [str(b) for b in page.get("bullets") or []] or [""]
    note = str(page.get("note") or "").strip()
    avail = CONTENT_BOTTOM - top - (0.5 if note else 0.0)
    scale = 1.0
    while scale > 0.72:
        heights = []
        for item in items:
            lead, detail = split_lead(item)
            h = text_height(lead, E(CONTENT_W - 1.5), 20 * scale, 1.3, bold=True)
            if detail:
                h += 0.10 + text_height(detail, E(CONTENT_W - 1.5), 14.5 * scale, 1.5)
            heights.append(max(0.86, h + 0.34))
        if sum(heights) + 0.06 * (len(items) - 1) <= avail:
            break
        scale -= 0.04
    y = top + 0.04
    for i, item in enumerate(items):
        lead, detail = split_lead(item)
        base = 30 + i * 4
        ns = Style(fonts=fonts, size_pt=22, color=st["accent"], bold=True, line=1.0)
        text_block(shapes, base, "Point number", ML, y + 0.02, 1.1, 0.5, paragraph(f"{i + 1:02d}", ns))
        lead_pt = 20 * scale
        lead_h = text_height(lead, E(CONTENT_W - 1.5), lead_pt, 1.3, bold=True)
        ts = Style(fonts=fonts, size_pt=lead_pt, color=st["ink"], bold=True, line=1.3)
        text_block(shapes, base + 1, "Point lead", ML + 1.15, y, CONTENT_W - 1.5, lead_h,
                   paragraph(lead, ts))
        block_h = lead_h + 0.34
        if detail:
            ds = Style(fonts=fonts, size_pt=14.5 * scale, color=st["muted"], line=1.5)
            detail_h = text_height(detail, E(CONTENT_W - 1.5), 14.5 * scale, 1.5)
            text_block(shapes, base + 2, "Point detail", ML + 1.15, y + lead_h + 0.10,
                       CONTENT_W - 1.5, detail_h, paragraph(detail, ds))
            block_h = lead_h + 0.10 + detail_h + 0.34
        if i < len(items) - 1:
            hairline(shapes, base + 3, ML, y + block_h - 0.12, CONTENT_W, st["line"])
        y += block_h + 0.06

    if note:
        note_y = CONTENT_BOTTOM - 0.3
        ns = Style(fonts=fonts, size_pt=11.5, color=st["muted"], line=1.0)
        text_block(shapes, 80, "Note", ML, note_y, CONTENT_W, 0.26, paragraph(note, ns))
    footer(shapes, 120, st, fonts, index, total, label)
    return shapes, st


def slide_two_col(page, st, fonts, index, total, label) -> tuple[list[str], dict[str, str]]:
    shapes: list[str] = []
    canvas(shapes, st)
    top = title_block(shapes, 10, st, fonts, page)
    gap = 0.9
    col_w = (CONTENT_W - gap) / 2
    bottom = CONTENT_BOTTOM - (0.5 if page.get("note") else 0.0)
    mid_x = ML + col_w + gap / 2
    rect(shapes, 20, "Column rule", mid_x, top, 0.009, bottom - top, fill=st["line"])

    for col, key in enumerate(("left", "right")):
        block = page.get(key) or {}
        x = ML + col * (col_w + gap)
        y = top
        heading = str(block.get("heading") or "").strip()
        if heading:
            hs = Style(fonts=fonts, size_pt=21, color=st["ink"], bold=True, line=1.25)
            text_block(shapes, 30 + col * 10, "Column heading", x, y, col_w, 0.5, paragraph(heading, hs))
            y += 0.72
        bullets = [str(b) for b in block.get("bullets") or []]
        paras = [str(p) for p in block.get("paragraphs") or []]
        if bullets:
            bs = Style(fonts=fonts, size_pt=15, color=st["ink"], line=1.55, space_before=10,
                       bullet="—", bullet_color=st["accent"])
            text_block(shapes, 31 + col * 10, "Column bullets", x, y, col_w, bottom - y,
                       "".join(paragraph(b, bs) for b in bullets))
        if paras:
            ps = Style(fonts=fonts, size_pt=15, color=st["muted"], line=1.6, space_before=10)
            text_block(shapes, 32 + col * 10, "Column body", x, y, col_w, bottom - y,
                       "".join(paragraph(p, ps) for p in paras))
    note = str(page.get("note") or "").strip()
    if note:
        ns = Style(fonts=fonts, size_pt=11.5, color=st["muted"], line=1.0)
        text_block(shapes, 80, "Note", ML, CONTENT_BOTTOM - 0.3, CONTENT_W, 0.26, paragraph(note, ns))
    footer(shapes, 120, st, fonts, index, total, label)
    return shapes, st


def _stats_row(shapes, top, st, fonts, stats, height=1.72) -> None:
    n = max(1, min(4, len(stats)))
    gap = 0.5
    w = (CONTENT_W - gap * (n - 1)) / n
    for i in range(n):
        item = stats[i] if isinstance(stats[i], dict) else {"value": str(stats[i])}
        x = ML + i * (w + gap)
        value = str(item.get("value") or "").strip()
        label = str(item.get("label") or "").strip()
        vs = Style(fonts=fonts, size_pt=76 if len(value) <= 5 else 54, color=st["ink"], bold=True,
                   align="l", line=1.0)
        text_block(shapes, 30 + i * 3, "Stat value", x, top, w, 1.1, paragraph(value, vs))
        if label:
            ls = Style(fonts=fonts, size_pt=13, color=st["muted"], align="l", line=1.4)
            text_block(shapes, 31 + i * 3, "Stat label", x, top + 1.12, w, 0.4, paragraph(label, ls))
        hairline(shapes, 32 + i * 3, x, top + height, w - 0.1, st["line"])


def slide_stats(page, st, fonts, index, total, label) -> tuple[list[str], dict[str, str]]:
    shapes: list[str] = []
    canvas(shapes, st)
    top = title_block(shapes, 10, st, fonts, page)
    stats = page.get("stats") or []
    caption = str(page.get("caption") or "")
    avail = CONTENT_BOTTOM - top
    block_h = 1.72 + (0.98 if caption else 0.0)
    y0 = top + max(0.0, (avail - block_h) / 2)
    _stats_row(shapes, y0, st, fonts, stats)
    if caption:
        cs = Style(fonts=fonts, size_pt=16, color=st["ink"], line=1.6)
        text_block(shapes, 60, "Caption", ML, y0 + 1.72 + 0.52, CONTENT_W * 0.86, 0.9,
                   paragraph(caption, cs))
    footer(shapes, 120, st, fonts, index, total, label)
    return shapes, st


def slide_big_number(page, st, fonts, index, total, label) -> tuple[list[str], dict[str, str]]:
    shapes: list[str] = []
    canvas(shapes, st)
    top = title_block(shapes, 10, st, fonts, page)
    stats = page.get("stats") or []
    if stats:
        caption = str(page.get("caption") or "")
        avail = CONTENT_BOTTOM - top
        block_h = 2.2 + (0.9 if caption else 0.0)
        y0 = top + max(0.0, (avail - block_h) / 2)
        _stats_row(shapes, y0, st, fonts, stats)
        if caption:
            cs = Style(fonts=fonts, size_pt=16, color=st["ink"], line=1.6)
            text_block(shapes, 60, "Caption", ML, y0 + 1.72 + 0.52, CONTENT_W * 0.86, 0.9,
                       paragraph(caption, cs))
    else:
        number = str(page.get("number") or "").strip()
        caption = str(page.get("caption") or "").strip()
        avail = CONTENT_BOTTOM - top
        y0 = top + max(0.0, (avail - 2.6) / 2)
        ns = Style(fonts=fonts, size_pt=150 if len(number) <= 5 else 96, color=st["accent"], bold=True,
                   line=1.0)
        text_block(shapes, 30, "Big number", ML, y0, CONTENT_W * 0.62, 2.0, paragraph(number, ns))
        if caption:
            cs = Style(fonts=fonts, size_pt=18, color=st["ink"], line=1.6)
            text_block(shapes, 31, "Caption", ML, y0 + 2.05, CONTENT_W * 0.7, 0.9,
                       paragraph(caption, cs))
        bullets = [str(b) for b in page.get("bullets") or []]
        if bullets:
            bs = Style(fonts=fonts, size_pt=15, color=st["muted"], line=1.6, space_before=10,
                       bullet="—", bullet_color=st["accent"])
            text_block(shapes, 32, "Support", ML + CONTENT_W * 0.74, y0, CONTENT_W * 0.26, 2.6,
                       "".join(paragraph(b, bs) for b in bullets), anchor="ctr")
    footer(shapes, 120, st, fonts, index, total, label)
    return shapes, st


def slide_quote(page, st, fonts, index, total, label) -> tuple[list[str], dict[str, str]]:
    shapes: list[str] = []
    rect(shapes, 1, "Background", 0, 0, SLIDE_W_IN, SLIDE_H_IN, fill=st["deep"])
    text = str(page.get("text") or page.get("title") or "").strip()
    width = CONTENT_W * 0.86
    size, lines = fit_size([(text, False)], E(width), E(3.0), 44, 26, line_factor=1.45)
    ts = Style(fonts=fonts, size_pt=size, color=st["on_deep"], line=1.45)
    height = lines * size * 1.45 / 72.0 + 0.1
    source = str(page.get("source") or "").strip()
    block_h = height + (0.78 if source else 0.0)
    y0 = max(1.6, (SLIDE_H_IN - block_h) / 2)
    text_block(shapes, 4, "Quote", ML, y0, width, height, paragraph(text, ts))
    if source:
        ss = Style(fonts=fonts, size_pt=12, color=st["accent_light"], line=1.3, spc=0.6)
        text_block(shapes, 5, "Source", ML, y0 + height + 0.52, CONTENT_W, 0.4, paragraph(source, ss))
    footer(shapes, 120, st, fonts, index, total, label)
    return shapes, st


def slide_timeline(page, st, fonts, index, total, label) -> tuple[list[str], dict[str, str]]:
    shapes: list[str] = []
    canvas(shapes, st)
    top = title_block(shapes, 10, st, fonts, page)
    items = [item for item in page.get("items") or [] if isinstance(item, dict)] or [{"when": "", "what": ""}]
    items = items[:5]
    n = len(items)
    avail = CONTENT_BOTTOM - top
    track_y = top + avail * 0.46
    rect(shapes, 20, "Track", ML, track_y, CONTENT_W, 0.012, fill=st["line"])
    step = CONTENT_W / n
    for i, item in enumerate(items):
        cx = ML + step * i
        rect(shapes, 30 + i * 4, "Dot", cx - 0.055, track_y - 0.055, 0.11, 0.11, fill=st["accent"], prst="ellipse")
        when = str(item.get("when") or "").strip()
        if when:
            ws = Style(fonts=fonts, size_pt=17, color=st["ink"], bold=True, line=1.2)
            text_block(shapes, 31 + i * 4, "When", cx, track_y - 0.78, step - 0.3, 0.4,
                       paragraph(when, ws))
        what = str(item.get("what") or "").strip()
        if what:
            bs = Style(fonts=fonts, size_pt=14, color=st["muted"], line=1.5)
            text_block(shapes, 32 + i * 4, "What", cx, track_y + 0.3, step - 0.3,
                       text_height(what, E(step - 0.34), 14, 1.5) + 0.1, paragraph(what, bs))
    footer(shapes, 120, st, fonts, index, total, label)
    return shapes, st


def slide_table(page, st, fonts, index, total, label) -> tuple[list[str], dict[str, str]]:
    shapes: list[str] = []
    canvas(shapes, st)
    top = title_block(shapes, 10, st, fonts, page)
    columns = [str(c) for c in page.get("columns") or []] or ["列 1", "列 2"]
    rows = [[str(c) for c in row] for row in page.get("rows") or []] or [["—" for _ in columns]]
    height = 0.56 * (len(rows) + 1)
    avail = CONTENT_BOTTOM - top
    y = top + 0.34 if avail > height + 0.6 else top + 0.1
    shapes.append(table_xml(30, ML, y, CONTENT_W, columns, rows, st, fonts))
    footer(shapes, 120, st, fonts, index, total, label)
    return shapes, st


def slide_image_text(page, st, fonts, index, total, label, image=None) -> tuple[list[str], dict[str, str]]:
    shapes: list[str] = []
    rect(shapes, 1, "Canvas", 0, 0, SLIDE_W_IN, SLIDE_H_IN, fill=st["canvas"])
    panel_x = SLIDE_W_IN * 0.47
    if image:
        picture(shapes, 3, 0, 0, panel_x, SLIDE_H_IN, image["rel"], image["size"])
    else:
        rect(shapes, 3, "Image placeholder", 0, 0, panel_x, SLIDE_H_IN, fill=st["accent_soft"])
    x = panel_x + 0.75
    width = SLIDE_W_IN - x - 0.75
    kicker = str(page.get("kicker") or "").strip()
    y = 1.35
    if kicker:
        ks = Style(fonts=fonts, size_pt=10.5, color=st["accent"], bold=True, caps=True, spc=1.8, line=1.0)
        text_block(shapes, 10, "Kicker", x, y, width, 0.3, paragraph(kicker, ks))
        y += 0.42
    title = str(page.get("title") or "").strip()
    size, lines = fit_title(title, width, 34, 24, max_lines=3)
    ts = Style(fonts=fonts, size_pt=size, color=st["ink"], bold=True, line=1.2)
    title_h = len(lines) * size * 1.2 / 72.0 + 0.05
    text_block(shapes, 11, "Title", x, y, width, title_h,
               "".join(paragraph(line, ts) for line in lines))
    y += title_h + 0.38
    paras = [str(p) for p in page.get("paragraphs") or []]
    if paras:
        ps = Style(fonts=fonts, size_pt=14.5, color=st["muted"], line=1.7, space_before=10)
        text_block(shapes, 12, "Body", x, y, width, CONTENT_BOTTOM - y,
                   "".join(paragraph(p, ps) for p in paras))
    note = str(page.get("note") or "").strip()
    if note:
        ns = Style(fonts=fonts, size_pt=11, color=st["muted"], line=1.0)
        text_block(shapes, 13, "Note", x, CONTENT_BOTTOM - 0.16, width, 0.24, paragraph(note, ns))
    # footer stays on the white panel: a rule across the photo reads as a mistake
    hairline(shapes, 120, x, FOOTER_RULE_Y, width, st["line"])
    fs = Style(fonts=fonts, size_pt=9, color=st["muted"], line=1.0)
    text_block(shapes, 121, "Footer left", x, FOOTER_TEXT_Y, width * 0.6, 0.26, paragraph(label, fs))
    fs.align = "r"
    text_block(shapes, 122, "Footer page", x + width * 0.6, FOOTER_TEXT_Y, width * 0.4, 0.26,
               paragraph(f"{index:02d} / {total:02d}", fs))
    return shapes, st


def slide_closing(page, st, fonts, meta) -> tuple[list[str], dict[str, str]]:
    shapes: list[str] = []
    rect(shapes, 1, "Background", 0, 0, SLIDE_W_IN, SLIDE_H_IN, fill=st["deep"])
    title = str(page.get("title") or "谢谢").strip()
    ts = Style(fonts=fonts, size_pt=56, color=st["on_deep"], bold=True, line=1.15)
    text_block(shapes, 3, "Title", ML, 2.5, CONTENT_W, 1.0, paragraph(title, ts))
    subtitle = str(page.get("subtitle") or meta.get("brand") or "").strip()
    if subtitle:
        ss = Style(fonts=fonts, size_pt=17, color=st["on_deep"], line=1.6)
        text_block(shapes, 5, "Subtitle", ML, 3.55, CONTENT_W * 0.8, 1.0, paragraph(subtitle, ss))
        shapes[-1] = shapes[-1].replace(color(st["on_deep"]), color(st["on_deep"], 78000), 1)
    meta_text = str(page.get("meta") or meta.get("date") or "").strip()
    if meta_text:
        ms = Style(fonts=fonts, size_pt=10.5, color=st["on_deep"], line=1.0)
        text_block(shapes, 6, "Meta", ML, 6.5, CONTENT_W, 0.3, paragraph(meta_text, ms))
        shapes[-1] = shapes[-1].replace(color(st["on_deep"]), color(st["on_deep"], 60000), 1)
    return shapes, st



# ── package assembly ──────────────────────────────────────────────────────────


def slide_xml(shapes: list[str], background: str | None = None) -> str:
    bg = ""
    if background:
        bg = f'<p:bg><p:bgPr>{color(background)}<a:effectLst/></p:bgPr></p:bg>'
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        f"<p:cSld>{bg}<p:spTree>"
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
        '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        + "".join(shapes)
        + "</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>"
    )


MASTER = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
    '<p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg>'
    '<p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
    '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
    '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
    "</p:spTree></p:cSld>"
    '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" '
    'accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" '
    'hlink="hlink" folHlink="folHlink"/>'
    '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
    "<p:txStyles><p:titleStyle><a:lvl1pPr><a:defRPr sz=\"3200\"/></a:lvl1pPr></p:titleStyle>"
    '<p:bodyStyle><a:lvl1pPr><a:defRPr sz="1800"/></a:lvl1pPr></p:bodyStyle>'
    '<p:otherStyle><a:lvl1pPr><a:defRPr sz="1800"/></a:lvl1pPr></p:otherStyle></p:txStyles>'
    "</p:sldMaster>"
)

LAYOUT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank" preserve="1">'
    '<p:cSld name="Blank"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
    '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
    '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
    "</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"
)

PRES_PROPS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p:presentationPr xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>'
)

VIEW_PROPS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p:viewPr xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
    '<p:normalViewPr><p:restoredLeft sz="15620"/><p:restoredTop sz="94660"/></p:normalViewPr>'
    '<p:slideViewPr><p:cSldViewPr><p:cViewPr varScale="1"><p:scale><a:sx n="100" d="100"/>'
    '<a:sy n="100" d="100"/></p:scale><p:origin x="0" y="0"/></p:cViewPr>'
    "<p:guideLst/></p:cSldViewPr></p:slideViewPr>"
    "<p:notesTextViewPr><p:cViewPr><p:scale><a:sx n=\"100\" d=\"100\"/><a:sy n=\"100\" d=\"100\"/></p:scale>"
    '<p:origin x="0" y="0"/></p:cViewPr></p:notesTextViewPr>'
    '<p:gridSpacing cx="72008" cy="72008"/></p:viewPr>'
)

TABLE_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<a:tblStyleLst xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'def="{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}"/>'
)

CONTENT_TYPES_HEAD = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Default Extension="png" ContentType="image/png"/>'
    '<Default Extension="jpeg" ContentType="image/jpeg"/>'
    '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
    '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
    '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
    '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
    '<Override PartName="/ppt/presProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presProps+xml"/>'
    '<Override PartName="/ppt/viewProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.viewProps+xml"/>'
    '<Override PartName="/ppt/tableStyles.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.tableStyles+xml"/>'
    '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
)

ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
    '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
    "</Relationships>"
)


def presentation_xml(count: int) -> str:
    ids = "".join(
        f'<p:sldId id="{256 + i}" r:id="rId{6 + i}"/>' for i in range(count)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'saveSubsetFonts="1" autoCompressPictures="0">'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        f"<p:sldIdLst>{ids}</p:sldIdLst>"
        f'<p:sldSz cx="{SLIDE_W}" cy="{SLIDE_H}" type="screen16x9"/>'
        '<p:notesSz cx="6858000" cy="9144000"/>'
        "</p:presentation>"
    )


def presentation_rels(count: int) -> str:
    rels = [
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>',
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/presProps" Target="presProps.xml"/>',
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/viewProps" Target="viewProps.xml"/>',
        '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>',
        '<Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/tableStyles" Target="tableStyles.xml"/>',
    ]
    for i in range(count):
        rels.append(
            f'<Relationship Id="rId{6 + i}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
            f'Target="slides/slide{i + 1}.xml"/>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(rels)
        + "</Relationships>"
    )


def master_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>'
        "</Relationships>"
    )


def layout_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>'
        "</Relationships>"
    )


def slide_rels(media: str | None = None) -> str:
    image_rel = ""
    if media:
        image_rel = (
            '<Relationship Id="rId2" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
            f'Target="../media/{media}"/>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
        f"{image_rel}</Relationships>"
    )


def core_xml(title: str, author: str, created: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{esc(title)}</dc:title><dc:creator>{esc(author)}</dc:creator>"
        '<cp:lastModifiedBy>' + esc(author) + "</cp:lastModifiedBy>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{esc(created)}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{esc(created)}</dcterms:modified>'
        "</cp:coreProperties>"
    )


def app_xml(count: int) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        "<Application>slides-deck</Application>"
        f"<Slides>{count}</Slides><PresentationFormat>宽屏</PresentationFormat>"
        "</Properties>"
    )


# ── validation & build ────────────────────────────────────────────────────────


@dataclass
class Issue:
    page: int
    level: str  # error | warn
    message: str


@dataclass
class BuildResult:
    pages: int
    warnings: list[Issue] = field(default_factory=list)
    layouts_used: dict[str, int] = field(default_factory=dict)


def load_spec(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        spec = json.load(handle)
    if not isinstance(spec, dict):
        raise SystemExit("spec must be a JSON object")
    return spec


def validate(spec: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    pages = spec.get("pages")
    if not isinstance(pages, list) or not pages:
        issues.append(Issue(0, "error", "spec.pages must be a non-empty array"))
        return issues
    for i, page in enumerate(pages, 1):
        if not isinstance(page, dict):
            issues.append(Issue(i, "error", "page must be an object"))
            continue
        layout = str(page.get("layout") or "").strip()
        if layout not in LAYOUTS:
            issues.append(Issue(i, "error", f"unknown layout {layout!r}; valid: {', '.join(LAYOUTS)}"))
            continue
        required, optional = LAYOUTS[layout]
        if not str(page.get(required) or "").strip():
            if layout not in ("quote", "agenda", "table", "timeline", "two_col"):
                issues.append(Issue(i, "error", f"layout {layout!r} requires {required!r}"))
        if layout == "quote" and not (page.get("text") or page.get("title")):
            issues.append(Issue(i, "error", "layout 'quote' requires 'text' or 'title'"))
        if layout in ("agenda", "timeline") and not page.get("items"):
            issues.append(Issue(i, "error", f"layout {layout!r} requires 'items'"))
        if layout == "table" and not page.get("columns"):
            issues.append(Issue(i, "error", "layout 'table' requires 'columns'"))
        if layout == "stats" and not page.get("stats"):
            issues.append(Issue(i, "error", "layout 'stats' requires a non-empty 'stats' array"))
        bullets = page.get("bullets") or []
        if len(bullets) > 7:
            issues.append(Issue(i, "warn", f"{len(bullets)} bullets; 5-7 read best — split the page"))
        for b in bullets:
            if len(str(b)) > 90:
                issues.append(Issue(i, "warn", "a bullet is longer than 90 chars; move detail to 'note'"))
                break
    # consecutive identical content layouts read as monotony
    run = 0
    prev = ""
    for i, page in enumerate(pages, 1):
        layout = str(page.get("layout") or "")
        if layout in CONTENT_LAYOUTS and layout == prev:
            run += 1
            if run >= 2:
                issues.append(Issue(i, "warn", f"layout {layout!r} used 3+ times in a row; vary it"))
        else:
            run = 0
        prev = layout
    return issues


def resolve_theme(spec: dict[str, Any]) -> tuple[dict[str, str], dict[str, str], str]:
    theme = spec.get("theme") or {}
    preset = str(theme.get("preset") or "midnight")
    if preset not in PRESETS:
        raise SystemExit(f"unknown theme preset {preset!r}; valid: {', '.join(PRESETS)}")
    st = dict(PRESETS[preset])
    accent = str(theme.get("accent") or "").strip().lstrip("#")
    if accent:
        if not re.fullmatch(r"[0-9a-fA-F]{6}", accent):
            raise SystemExit("theme.accent must be a 6-digit hex color")
        st["accent"] = accent.upper()
    fonts = dict(DEFAULT_FONTS)
    fonts.update({k: str(v) for k, v in (theme.get("fonts") or {}).items() if v})
    return st, fonts, preset


def theme_asset(fonts: dict[str, str] | None = None) -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "assets", "theme1.xml")
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    if fonts:
        text = text.replace('<a:latin typeface="Calibri"/>', f'<a:latin typeface="{esc(fonts["latin"])}"/>')
        text = text.replace('<a:ea typeface=""/>', f'<a:ea typeface="{esc(fonts["ea"])}"/>')
        text = re.sub(
            r'<a:font script="Hans" typeface="[^"]*"/>',
            f'<a:font script="Hans" typeface="{esc(fonts["ea"])}"/>',
            text,
        )
    return text


def render_page(
    page: dict[str, Any],
    st: dict[str, str],
    fonts: dict[str, str],
    index: int,
    total: int,
    meta: dict[str, Any],
    image: dict[str, Any] | None = None,
) -> tuple[list[str], str | None, str]:
    layout = str(page.get("layout") or "bullets")
    label = str(meta.get("label") or meta.get("title") or "").strip()
    bg: str | None = None
    if layout == "cover":
        shapes, _ = slide_cover(page, st, fonts, meta, image)
    elif layout == "section":
        shapes, _ = slide_section(page, st, fonts, index)
    elif layout == "agenda":
        shapes, _ = slide_agenda(page, st, fonts, index, total, label)
    elif layout == "bullets":
        shapes, _ = slide_bullets(page, st, fonts, index, total, label)
    elif layout == "two_col":
        shapes, _ = slide_two_col(page, st, fonts, index, total, label)
    elif layout == "stats":
        shapes, _ = slide_stats(page, st, fonts, index, total, label)
    elif layout == "big_number":
        shapes, _ = slide_big_number(page, st, fonts, index, total, label)
    elif layout == "quote":
        shapes, _ = slide_quote(page, st, fonts, index, total, label)
    elif layout == "timeline":
        shapes, _ = slide_timeline(page, st, fonts, index, total, label)
    elif layout == "table":
        shapes, _ = slide_table(page, st, fonts, index, total, label)
    elif layout == "image_text":
        shapes, _ = slide_image_text(page, st, fonts, index, total, label, image)
    elif layout == "closing":
        shapes, _ = slide_closing(page, st, fonts, meta)
    else:
        raise SystemExit(f"unknown layout {layout!r}")
    return shapes, bg, layout


def build(spec: dict[str, Any], out_path: str, base_dir: str = ".") -> BuildResult:
    issues = validate(spec)
    errors = [i for i in issues if i.level == "error"]
    if errors:
        for issue in errors:
            print(f"[error] page {issue.page}: {issue.message}", file=sys.stderr)
        raise SystemExit(1)

    st, fonts, _preset = resolve_theme(spec)
    pages: list[dict[str, Any]] = spec["pages"]
    meta = {
        "title": str(spec.get("title") or "").strip(),
        "subtitle": str(spec.get("subtitle") or "").strip(),
        "brand": str(spec.get("brand") or "").strip(),
        "date": str(spec.get("date") or "").strip(),
        "label": str(spec.get("label") or spec.get("title") or "").strip(),
    }
    created = str(spec.get("created") or "2026-01-01T00:00:00Z")
    theme = theme_asset(fonts)

    # Image pages: resolve, measure and assign a media part once, before rendering.
    images: dict[int, dict[str, Any]] = {}
    for i, page in enumerate(pages, 1):
        raw = str(page.get("image") or "").strip()
        if not raw:
            continue
        path = raw if os.path.isabs(raw) else os.path.join(base_dir, raw)
        if not os.path.isfile(path):
            raise SystemExit(f"page {i}: image not found: {raw}")
        lower = path.lower()
        if lower.endswith(".png"):
            ext = "png"
        elif lower.endswith((".jpg", ".jpeg")):
            ext = "jpeg"
        else:
            raise SystemExit(f"page {i}: image must be PNG or JPEG: {raw}")
        name = f"image{len(images) + 1}.{ext}"
        with open(path, "rb") as handle:
            data = handle.read()
        images[i] = {"rel": "rId2", "name": name, "size": image_size(path), "data": data}

    result = BuildResult(pages=len(pages), warnings=[i for i in issues if i.level == "warn"])
    slides_xml: list[str] = []
    for i, page in enumerate(pages, 1):
        shapes, bg, layout = render_page(page, st, fonts, i, len(pages), meta, images.get(i))
        slides_xml.append(slide_xml(shapes, bg))
        result.layouts_used[layout] = result.layouts_used.get(layout, 0) + 1

    count = len(slides_xml)
    parent = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(parent, exist_ok=True)
    content_types = CONTENT_TYPES_HEAD + "".join(
        f'<Override PartName="/ppt/slides/slide{i + 1}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for i in range(count)
    ) + "</Types>"

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", ROOT_RELS)
        zf.writestr("docProps/core.xml", core_xml(meta["title"] or "deck", "slides-deck", created))
        zf.writestr("docProps/app.xml", app_xml(count))
        zf.writestr("ppt/presentation.xml", presentation_xml(count))
        zf.writestr("ppt/_rels/presentation.xml.rels", presentation_rels(count))
        zf.writestr("ppt/presProps.xml", PRES_PROPS)
        zf.writestr("ppt/viewProps.xml", VIEW_PROPS)
        zf.writestr("ppt/tableStyles.xml", TABLE_STYLES)
        zf.writestr("ppt/theme/theme1.xml", theme)
        zf.writestr("ppt/slideMasters/slideMaster1.xml", MASTER)
        zf.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", master_rels())
        zf.writestr("ppt/slideLayouts/slideLayout1.xml", LAYOUT)
        zf.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", layout_rels())
        written: set[str] = set()
        for i, xml in enumerate(slides_xml, 1):
            image = images.get(i)
            zf.writestr(f"ppt/slides/slide{i}.xml", xml)
            zf.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", slide_rels(image["name"] if image else None))
            if image and image["name"] not in written:
                zf.writestr(f"ppt/media/{image['name']}", image["data"])
                written.add(image["name"])
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────


def cmd_layouts() -> int:
    print("layout        required    optional")
    for name, (required, optional) in LAYOUTS.items():
        print(f"{name:<13} {required:<11} {', '.join(optional) or '-'}")
    return 0


def cmd_doctor() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    theme = os.path.join(os.path.dirname(here), "assets", "theme1.xml")
    ok = True
    print(f"python      {sys.version.split()[0]}")
    if os.path.isfile(theme):
        print(f"theme asset {os.path.getsize(theme)} bytes")
    else:
        print(f"theme asset MISSING at {theme}", file=sys.stderr)
        ok = False
    print(f"layouts     {len(LAYOUTS)}")
    return 0 if ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a .pptx deck from a JSON spec.")
    sub = parser.add_subparsers(dest="command")

    build_p = sub.add_parser("build", help="build a pptx")
    build_p.add_argument("spec")
    build_p.add_argument("-o", "--output", default="deck.pptx")

    check_p = sub.add_parser("check", help="validate a spec without writing")
    check_p.add_argument("spec")

    sub.add_parser("layouts", help="list layouts")
    sub.add_parser("doctor", help="self-check")

    args = parser.parse_args(argv)
    if args.command == "layouts":
        return cmd_layouts()
    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "check":
        spec = load_spec(args.spec)
        issues = validate(spec)
        for issue in issues:
            print(f"[{issue.level}] page {issue.page}: {issue.message}")
        errors = [i for i in issues if i.level == "error"]
        print(f"{len(spec.get('pages') or [])} pages, {len(issues)} issue(s)")
        return 1 if errors else 0
    if args.command == "build":
        spec = load_spec(args.spec)
        result = build(spec, args.output, os.path.dirname(os.path.abspath(args.spec)))
        for issue in result.warnings:
            print(f"[warn] page {issue.page}: {issue.message}")
        summary = ", ".join(f"{k}×{v}" for k, v in sorted(result.layouts_used.items()))
        print(f"built {args.output}: {result.pages} pages ({summary})")
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

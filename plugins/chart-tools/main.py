"""chart-tools：把数据画成可交付的 ECharts HTML。

三个工具，全部无副作用（只有 ``chart_render_html`` 写文件）：

- ``chart_data_profile``  看数据：列类型、缺失、基数、数值范围，给选型和一份可改的 spec 骨架
- ``chart_check``         只校验 spec，不写文件（改完先跑一遍）
- ``chart_render_html``   绑定数据 + 生成 HTML 产物

为什么图必须由工具生成，而不是让模型直接写 HTML：

1. **模型会编数字**。只要允许它写 ``series.data``，就会出现"图很好看但数对不上"；
   这里 spec 里禁止写数据（``dataset.source`` 直接报错），值一律由 ``bind`` 从
   CSV/XLSX/JSON 绑定，并把文件 sha256、行数、列名写进页脚。
2. **手写 HTML 等于执行任意脚本**。产物面板的 iframe 给了 ``allow-scripts``（不然图是白的），
   所以 HTML 必须来自固定模板：模型只能提供 option（纯 JSON），不能插脚本。
3. **样例里的图有一半错在"看起来没毛病"**。校验器把断轴、双轴、饼图超分类、
   3D/GL 缺依赖这些点拦在渲染之前，而不是等用户看图时发现。

spec 文件（``charts/<名字>.spec.json``）的形状见 ``_skeleton`` 的产出；页脚所需的
``__meta`` / ``__data`` 由工具在渲染时注入，不写回 spec。
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import math
import os
import re
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from pi_coding_agent.plugins import PluginAPI, workspace_path

SCHEMA = "pi.chart/1"

#: 输入上限：业务表格远小于这个数；超过说明该先聚合。
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_SCAN_ROWS = 200_000
#: 绑定后进入 HTML 的行数上限。产物面板按 512KB 截断预览，数据多了页面就白屏。
MAX_BOUND_ROWS = 5_000
MAX_COLUMNS = 24
MAX_CHARTS = 8
#: 留出模板与样式的位置，别贴着 512KB 的预览上限。
MAX_HTML_BYTES = 480_000

_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?$")
_GROUPED_RE = re.compile(r"^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
_PERCENT_RE = re.compile(r"^-?\d+(?:\.\d+)?\s*%$")
_CURRENCY_RE = re.compile(r"^[¥￥$€£]\s*-?\d")
_DATE_LIKE = re.compile(
    r"^(?:\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2}日?)?|\d{1,2}月|第?[一二三四1-4]季度|Q[1-4]|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?)$",
    re.I,
)

#: 需要 echarts-gl 的系列：内置包只有核心，画出来是空白。
GL_TYPES = {
    "bar3D", "line3D", "lines3D", "scatter3D", "surface", "map3D", "globe",
    "graphGL", "scatterGL", "linesGL", "flowGL", "pictorialBar3D",
}

#: 断轴/双轴这类"图表会说谎"的选项，只警告不拦——用户明确要求时可以用，但要在图注里说明。
_NOTE_FIELDS = ("unit", "source", "caveat")


class ParamError(Exception):
    """模型传错了参数：当作工具结果返回，不抛给运行时。"""


# -- 小工具 ---------------------------------------------------------------------------


def _squash(value: Any, limit: int = 0) -> str:
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    return text[:limit] if limit and len(text) > limit else text


def _resolve(api: PluginAPI, raw: Any, *, must_exist: bool = True) -> Path:
    text = str(raw or "").strip()
    if not text:
        raise ParamError("需要文件路径（相对工作区，如 data/sales.csv）")
    try:
        target = workspace_path(api, text)
    except ValueError as error:
        raise ParamError(str(error)) from None
    if must_exist and not target.is_file():
        raise ParamError(f"文件不存在：{text}")
    return target


def _read_bytes(path: Path) -> bytes:
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ParamError(f"文件超过 {MAX_FILE_BYTES // 1024 // 1024}MB，先聚合或拆分")
    with open(path, "rb") as handle:
        return handle.read(MAX_FILE_BYTES + 1)


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _sniff(text: str, explicit: str | None) -> str:
    if explicit:
        if len(explicit) != 1:
            raise ParamError("delimiter 必须是单个字符，如 , 或 ;")
        return explicit
    head = "\n".join(text.splitlines()[:20])
    try:
        return csv.Sniffer().sniff(head, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def _coerce(value: Any) -> Any:
    """把单元格转成 ECharts 能直接画的值。

    只做**无损**转换：整千分位、货币符号、百分号会被剥掉，但不在数值里做单位换算。
    百分比记成数字（3.5 而不是 3.5%），单位提示由 profile 给出，免得轴上出现 "3.5%" 画不出来。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(float(value)) else None
    text = str(value).strip()
    if not text:
        return None
    if _INT_RE.match(text):
        return int(text)
    if _FLOAT_RE.match(text):
        return float(text)
    if _GROUPED_RE.match(text):
        try:
            return float(text.replace(",", ""))
        except ValueError:
            return text
    if _PERCENT_RE.match(text):
        try:
            return float(text.rstrip("%").strip())
        except ValueError:
            return text
    if _CURRENCY_RE.match(text):
        cleaned = re.sub(r"[¥￥$€£,\s]", "", text)
        if _INT_RE.match(cleaned):
            return int(cleaned)
        if _FLOAT_RE.match(cleaned):
            return float(cleaned)
    return text


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _looks_like_date(value: Any) -> bool:
    return isinstance(value, str) and bool(_DATE_LIKE.match(value.strip()))


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _cell_index(reference: str) -> int:
    letters = "".join(ch for ch in reference if ch.isalpha()).upper()
    value = 0
    for ch in letters:
        value = value * 26 + (ord(ch) - 64)
    return max(0, value - 1)


# -- 读表 -----------------------------------------------------------------------------


def _read_csv(path: Path, delimiter: str | None) -> dict[str, Any]:
    raw = _read_bytes(path)
    text = _decode(raw)
    sep = _sniff(text, delimiter)
    reader = csv.reader(io.StringIO(text), delimiter=sep)
    rows: list[list[str]] = []
    for index, row in enumerate(reader):
        if index >= MAX_SCAN_ROWS:
            break
        if not row or all(not str(cell).strip() for cell in row):
            continue
        rows.append([str(cell) for cell in row])
    if not rows:
        raise ParamError("表是空的，没有表头")
    columns = [f"列{i + 1}" if not str(c).strip() else str(c).strip() for i, c in enumerate(rows[0])]
    body = [row + [""] * (len(columns) - len(row)) for row in rows[1:]]
    body = [row[: len(columns)] for row in body]
    return {"columns": columns, "rows": body, "meta": {"kind": "CSV", "delimiter": sep, "encoding": "text"}}


def _xlsx_targets(archive: zipfile.ZipFile) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Return ``[(sheetName, partPath)]`` in workbook order and the shared-string list."""
    shared: list[str] = []
    if "xl/sharedStrings.xml" in archive.namelist():
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
        for item in root:
            if item.tag.rsplit("}", 1)[-1] != "si":
                continue
            shared.append("".join(node.text or "" for node in item.iter() if node.tag.rsplit("}", 1)[-1] == "t"))
    rels: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in archive.namelist():
        root = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        for node in root:
            rels[node.attrib.get("Id", "")] = node.attrib.get("Target", "")
    sheets: list[tuple[str, str]] = []
    if "xl/workbook.xml" in archive.namelist():
        root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] != "sheet":
                continue
            name = node.attrib.get("name") or ""
            rid = ""
            for key, value in node.attrib.items():
                if key.rsplit("}", 1)[-1] == "id":
                    rid = value
            target = rels.get(rid, "")
            if target:
                sheets.append((name, target))
    return sheets, shared


def _read_xlsx(path: Path, sheet: str | None) -> dict[str, Any]:
    raw_path = path
    with zipfile.ZipFile(raw_path) as archive:
        sheets, shared = _xlsx_targets(archive)
        if not sheets:
            raise ParamError("这个 xlsx 里没有工作表")
        names = [name for name, _target in sheets]
        chosen = sheet or names[0]
        if chosen not in names:
            raise ParamError(f"没有名为 {chosen} 的工作表；可选：{names}")
        target = dict(sheets)[chosen]
        part = target.lstrip("/")
        if not part.startswith("xl/"):
            part = "xl/" + part
        if part not in archive.namelist():
            raise ParamError(f"工作表文件缺失：{part}")

        grid: list[list[str]] = []
        root = ElementTree.fromstring(archive.read(part))
        for row in root.iter():
            if row.tag.rsplit("}", 1)[-1] != "row":
                continue
            cells: dict[int, str] = {}
            for cell in row:
                if cell.tag.rsplit("}", 1)[-1] != "c":
                    continue
                index = _cell_index(cell.attrib.get("r", "A1"))
                kind = cell.attrib.get("t", "")
                value = ""
                if kind == "inlineStr":
                    value = "".join(
                        node.text or "" for node in cell.iter() if node.tag.rsplit("}", 1)[-1] == "t"
                    )
                else:
                    raw_value = ""
                    for node in cell:
                        if node.tag.rsplit("}", 1)[-1] == "v":
                            raw_value = node.text or ""
                            break
                    if kind == "s":
                        try:
                            value = shared[int(raw_value)]
                        except (ValueError, IndexError):
                            value = raw_value
                    elif kind == "b":
                        value = "true" if raw_value == "1" else "false"
                    else:
                        value = raw_value
                cells[index] = value
            if not cells:
                continue
            width = max(cells) + 1
            grid.append([cells.get(i, "") for i in range(width)])
            if len(grid) >= MAX_SCAN_ROWS:
                break

    if not grid:
        raise ParamError(f"工作表 {chosen} 是空的")
    header = grid[0]
    columns = [f"列{i + 1}" if not str(c).strip() else str(c).strip() for i, c in enumerate(header)]
    body = [row + [""] * (len(columns) - len(row)) for row in grid[1:]]
    body = [row[: len(columns)] for row in body]
    return {
        "columns": columns,
        "rows": body,
        "meta": {"kind": "XLSX", "sheet": chosen, "sheets": names},
    }


def _read_json(path: Path) -> dict[str, Any]:
    text = _decode(_read_bytes(path))
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ParamError(f"JSON 解析失败：{error}") from None
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        payload = payload["rows"]
    if not isinstance(payload, list) or not payload:
        raise ParamError("JSON 需要是对象数组，或 {\"rows\": [...]}")
    if not all(isinstance(item, dict) for item in payload[:20]):
        raise ParamError("JSON 数组的元素必须是对象（每行一个对象）")
    columns: list[str] = []
    for item in payload[:MAX_SCAN_ROWS]:
        for key in item:
            if key not in columns:
                columns.append(str(key))
    if len(columns) > MAX_COLUMNS:
        columns = columns[:MAX_COLUMNS]
    rows = [[item.get(column, "") for column in columns] for item in payload[:MAX_SCAN_ROWS]]
    return {"columns": columns, "rows": rows, "meta": {"kind": "JSON"}}


def read_table(path: Path, *, delimiter: str | None = None, sheet: str | None = None) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv", ".txt"):
        return _read_csv(path, delimiter or ("\t" if suffix == ".tsv" else None))
    if suffix in (".xlsx", ".xlsm"):
        return _read_xlsx(path, sheet)
    if suffix == ".xls":
        raise ParamError("旧版 .xls 读不了，请另存为 .xlsx 或 .csv")
    if suffix == ".json":
        return _read_json(path)
    raise ParamError(f"不支持的表格式：{suffix}；支持 .csv/.tsv/.xlsx/.json")


# -- 画像 -----------------------------------------------------------------------------


def _column_stats(values: list[Any]) -> dict[str, Any]:
    non_empty = [value for value in values if value not in ("", None)]
    numbers = [_coerce(value) for value in non_empty]
    numeric = [value for value in numbers if _is_number(value)]
    unique: list[str] = []
    seen: set[str] = set()
    for value in non_empty:
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        unique.append(key)
        if len(unique) >= 200:
            break
    dates = sum(1 for value in non_empty[:500] if _looks_like_date(value))
    if non_empty and len(numeric) == len(non_empty):
        kind = "number"
    elif non_empty and dates >= max(2, int(len(non_empty[:500]) * 0.6)):
        kind = "date"
    else:
        kind = "category"
    percent = any(_PERCENT_RE.match(str(value)) for value in non_empty[:500])
    currency = any(_CURRENCY_RE.match(str(value)) for value in non_empty[:500])
    return {
        "kind": kind,
        "missing": len(values) - len(non_empty),
        "unique": unique,
        "numeric": numeric,
        "percent": percent,
        "currency": currency,
    }


def _fmt_num(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if abs(value) >= 1:
        return f"{value:.3g}"
    return f"{value:.3g}"


def _display_width(text: str) -> int:
    """东亚宽字符按 2 列算：否则中文列名一多，表格就错位。"""
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _clip(text: str, width: int) -> str:
    out = ""
    used = 0
    for char in text:
        step = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used + step > width:
            return out[:-1] + "…" if out else "…"
        out += char
        used += step
    return out


def _pad(text: str, width: int) -> str:
    text = _clip(text, width)
    return text + " " * max(0, width - _display_width(text))


def profile_text(table: dict[str, Any], rel: str) -> str:
    columns: list[str] = table["columns"]
    rows: list[list[Any]] = table["rows"]
    stats = {column: _column_stats([row[i] for row in rows]) for i, column in enumerate(columns)}

    header = ["列", "类型", "缺失", "不同值", "范围 / 示例"]
    lines = [f"数据画像：{rel}（{table['meta'].get('kind', 'table')}，{len(rows):,} 行 × {len(columns)} 列）", ""]
    cells: list[list[str]] = []
    for column in columns:
        info = stats[column]
        if info["kind"] == "number":
            values = info["numeric"]
            span = f"{_fmt_num(min(values))} – {_fmt_num(max(values))}" if values else "—"
            if info["percent"]:
                span += "（含 %，绑定后请在轴或标签上补 %）"
            if info["currency"]:
                span += "（含货币符号，已剥掉）"
        else:
            span = " / ".join(info["unique"][:4]) or "—"
        missing = f"{info['missing'] / max(1, len(rows)) * 100:.0f}%"
        cells.append([column, info["kind"], missing, str(len(info["unique"])), span])

    widths = [
        min(26, max(_display_width(str(row[i])) for row in [header] + cells))
        for i in range(len(header))
    ]
    for row in [header] + cells:
        lines.append("  ".join(_pad(str(row[i]), widths[i]) for i in range(len(header))).rstrip())

    lines.append("")
    lines.extend(_suggestions(columns, stats))
    lines.append("")
    lines.append("下一步：把下面的骨架存成 charts/<名字>.spec.json，改 series 的 type/encode，然后 chart_render_html。")
    lines.append("")
    lines.append(_skeleton(columns, stats, rel))
    return "\n".join(lines)


def _suggestions(columns: list[str], stats: dict[str, Any]) -> list[str]:
    cats = [c for c in columns if stats[c]["kind"] in ("category", "date")]
    nums = [c for c in columns if stats[c]["kind"] == "number"]
    out = ["建议（按数据结构，不按个人偏好）："]
    dated = [c for c in cats if stats[c]["kind"] == "date"]
    if dated and nums:
        out.append(f"- {dated[0]} × {nums[0]}：时间序列 → 折线（趋势）或柱状（逐期对比）")
    if cats and nums and not dated:
        out.append(f"- {cats[0]} × {nums[0]}：分类比较 → 柱状；类目超过 12 个改横向条形")
    if len(nums) >= 2:
        out.append(f"- {nums[0]} × {nums[1]}：两个数值 → 散点（相关性）；量纲差得多用双轴并标注")
    if cats and nums:
        top = stats[cats[0]]["unique"]
        if 2 <= len(top) <= 6:
            out.append(f"- {cats[0]} 只有 {len(top)} 类且是整体占比 → 饼/环（超过 6 类不要用饼图）")
    if cats and len(nums) >= 2:
        out.append(f"- {cats[0]} × 多个数值：分组柱（同类对比）或堆叠柱（看构成），堆叠不要混正负")
    return out


def _skeleton(columns: list[str], stats: dict[str, Any], rel: str) -> str:
    cats = [c for c in columns if stats[c]["kind"] in ("category", "date")]
    nums = [c for c in columns if stats[c]["kind"] == "number"]
    x = cats[0] if cats else (columns[0] if columns else "x")
    y = nums[0] if nums else (columns[1] if len(columns) > 1 else "y")
    chart_type = "line" if stats.get(x, {}).get("kind") == "date" else ("bar" if len(stats[x]["unique"]) <= 12 else "line")
    spec = {
        "schema": SCHEMA,
        "page": {
            "title": f"{y} 随 {x} 的变化",
            "subtitle": "副标题写口径：人群/时间范围/筛选条件",
            "columns": 1,
            "theme": "auto",
        },
        "bind": {"path": rel, "sheet": None, "columns": [x, y]},
        "charts": [
            {
                "title": f"{x} × {y}",
                "option": {
                    "dataset": {},
                    "grid": {"left": 64, "right": 28, "top": 40, "bottom": 56},
                    "tooltip": {"trigger": "axis"},
                    "xAxis": {"type": "category", "name": x, "axisLabel": {"hideOverlap": True}},
                    "yAxis": {"type": "value", "name": y},
                    "series": [
                        {
                            "type": chart_type,
                            "encode": {"x": x, "y": y},
                            "smooth": chart_type == "line",
                        }
                    ],
                },
                "note": "这一张要回答的问题，以及不让读者误读的边界",
            }
        ],
        "notes": {"unit": "单位（如 万元 / 例 / %）", "source": "数据出处", "caveat": "口径与已知缺口"},
    }
    return json.dumps(spec, ensure_ascii=False, indent=2)


# -- spec 校验 ------------------------------------------------------------------------


def _load_spec(api: PluginAPI, raw: Any) -> tuple[Path, dict[str, Any]]:
    path = _resolve(api, raw)
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ParamError(f"spec 读不了：{error}") from None
    if not isinstance(spec, dict):
        raise ParamError("spec 顶层必须是对象")
    return path, spec


def _bind_columns(spec: dict[str, Any]) -> list[str]:
    bind = spec.get("bind")
    if not isinstance(bind, dict):
        return []
    columns = bind.get("columns")
    return [str(c) for c in columns] if isinstance(columns, list) else []


def check_spec(spec: dict[str, Any], *, base_dir: Path, api: PluginAPI) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if spec.get("schema") != SCHEMA:
        errors.append(f"schema 必须是 {SCHEMA!r}")
    page = spec.get("page")
    if not isinstance(page, dict):
        errors.append("缺 page 对象（title/subtitle/columns/theme）")
        page = {}
    if not str(page.get("title") or "").strip():
        warnings.append("page.title 为空：页面上会没有标题")
    columns = page.get("columns", 1)
    if not isinstance(columns, int) or not 1 <= columns <= 3:
        errors.append("page.columns 只能是 1 / 2 / 3")

    charts = spec.get("charts")
    if not isinstance(charts, list) or not charts:
        errors.append("charts 不能为空：至少要有一张图")
        charts = []
    if len(charts) > MAX_CHARTS:
        errors.append(f"charts 最多 {MAX_CHARTS} 张；再多请拆成两个页面")

    bind = spec.get("bind")
    bound_columns = _bind_columns(spec)
    if bind is not None and not isinstance(bind, dict):
        errors.append("bind 必须是对象（path / sheet / columns / aggregate）")
    if isinstance(bind, dict):
        rel = str(bind.get("path") or "").strip()
        if not rel:
            errors.append("bind.path 不能为空")
        else:
            try:
                _resolve(api, rel)
            except ParamError as error:
                errors.append(str(error))
        if not bound_columns:
            warnings.append("bind.columns 为空：会把所有列都绑定进去（列多时页面会很大）")
        elif len(bound_columns) > MAX_COLUMNS:
            errors.append(f"bind.columns 最多 {MAX_COLUMNS} 列")

    notes = spec.get("notes")
    if not isinstance(notes, dict):
        warnings.append("缺 notes：单位、出处、口径要写在页脚，否则读者无法判断这张图")
        notes = {}
    for field in _NOTE_FIELDS:
        if not str(notes.get(field) or "").strip():
            warnings.append(f"notes.{field} 为空")

    for index, chart in enumerate(charts, start=1):
        label = f"第 {index} 张图"
        if not isinstance(chart, dict):
            errors.append(f"{label} 不是对象")
            continue
        if not str(chart.get("title") or "").strip():
            warnings.append(f"{label} 没有 title")
        option = chart.get("option")
        if not isinstance(option, dict):
            errors.append(f"{label} 缺 option 对象")
            continue

        dataset = option.get("dataset")
        if isinstance(dataset, dict) and dataset.get("source"):
            errors.append(
                f"{label} 的 dataset.source 不能写在 spec 里：数据由 bind 从数据文件绑定。"
                "删掉 source，只留 {}，工具会填。"
            )
        if isinstance(dataset, dict) and dataset.get("transform"):
            warnings.append(f"{label} 用了 dataset.transform：需要 echarts-stat，内置包没有，会报错")

        series = option.get("series")
        if not isinstance(series, list) or not series:
            errors.append(f"{label} 缺 series")
            series = []

        # 数据来源：dataset/encode 必须有 bind 兜着，否则渲染出来是空白或报错。
        has_encode = any(isinstance(item, dict) and isinstance(item.get("encode"), dict) for item in series)
        has_literal_data = any(
            isinstance(item, dict) and isinstance(item.get("data"), list) and item.get("data") for item in series
        )
        if (isinstance(option.get("dataset"), dict) or has_encode) and not isinstance(bind, dict):
            errors.append(
                f"{label} 用 dataset/encode 但没有 bind：数据没有来源。加 bind.path（和 columns）。"
            )
        if not isinstance(option.get("dataset"), dict) and not has_encode and not has_literal_data:
            warnings.append(f"{label} 的 series 既没有 dataset/encode，也没有 data：这张图会是空白")

        for item in series:
            if not isinstance(item, dict):
                errors.append(f"{label} 的 series 里有非对象")
                continue
            kind = item.get("type")
            if not str(kind or "").strip():
                errors.append(f"{label} 的 series 缺 type")
                continue
            if kind == "custom":
                errors.append(
                    f"{label} 用了 custom 系列：renderItem 是函数，JSON 存不了。换内置类型，"
                    "或让模板库提供现成的渲染器。"
                )
            if kind in GL_TYPES:
                errors.append(
                    f"{label} 用了 {kind}：需要 echarts-gl（WebGL），内置包只有核心，画出来是空白。"
                    "3D/GL 图请在单独的页面里做，或用二维表达替代。"
                )
            encode = item.get("encode")
            if isinstance(encode, dict) and bound_columns:
                for axis, field in encode.items():
                    names = field if isinstance(field, list) else [field]
                    for name in names:
                        if isinstance(name, str) and name not in bound_columns:
                            errors.append(
                                f"{label} 的 encode.{axis} 引用了未绑定的列 {name!r}；"
                                f"bind.columns = {bound_columns}"
                            )
            data = item.get("data")
            if isinstance(data, list) and data and all(
                isinstance(value, (int, float)) for value in data[:10]
            ):
                warnings.append(
                    f"{label} 的 series.data 是手写数字：图里的值和数据文件对不上。"
                    "改用 dataset + encode，让工具绑定。"
                )
            if item.get("renderItem") and not isinstance(item.get("renderItem"), str):
                errors.append(f"{label} 的 renderItem 只能是字符串 id（函数无法进 JSON）")

        if isinstance(option.get("yAxis"), list) and len(option["yAxis"]) > 1:
            warnings.append(f"{label} 用了双轴：必须在图注里说明两条轴各自的单位和为什么放在一起")
        if option.get("dataZoom"):
            warnings.append(f"{label} 用了 dataZoom：静态截图里看不到被缩掉的部分，图注要说明")

        kinds = {str(item.get("type")) for item in series if isinstance(item, dict)}
        if "pie" in kinds and bound_columns:
            try:
                table = read_table(_resolve(api, bind.get("path")), sheet=bind.get("sheet"))
                for position, name in enumerate(bound_columns):
                    if name == bound_columns[0]:
                        categories = {row[position] for row in table["rows"]}
                        if len(categories) > 6:
                            warnings.append(
                                f"{label} 是饼图但有 {len(categories)} 类：超过 6 类改用条形图，"
                                "或者合并成长尾「其他」"
                            )
                        break
            except (ParamError, IndexError):
                pass

    return errors, warnings


# -- 绑定 -----------------------------------------------------------------------------


def _aggregate(rows: list[list[Any]], columns: list[str], rule: dict[str, Any]) -> tuple[list[list[Any]], list[str]]:
    by = str(rule.get("by") or "").strip()
    op = str(rule.get("op") or "sum").strip().lower()
    if by not in columns:
        raise ParamError(f"aggregate.by 必须是 bind.columns 里的列：{by!r}")
    if op not in ("sum", "mean", "count", "min", "max"):
        raise ParamError("aggregate.op 只能是 sum / mean / count / min / max")
    by_index = columns.index(by)
    numeric_indexes = [i for i, name in enumerate(columns) if i != by_index]
    groups: dict[str, list[list[Any]]] = {}
    order: list[str] = []
    for row in rows:
        key = str(row[by_index])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    out_columns = [by] + (["count"] if op == "count" else [columns[i] for i in numeric_indexes])
    out: list[list[Any]] = []
    for key in order:
        group = groups[key]
        if op == "count":
            out.append([key, len(group)])
            continue
        record: list[Any] = [key]
        for index in numeric_indexes:
            values = [_coerce(row[index]) for row in group]
            numbers = [value for value in values if _is_number(value)]
            if not numbers:
                # 维度列（如地区名）在聚合后保留第一个非空值，而不是变成 null。
                first = next((value for value in values if value not in (None, "")), None)
                record.append(first)
            elif op == "sum":
                record.append(sum(numbers))
            elif op == "mean":
                record.append(sum(numbers) / len(numbers))
            elif op == "min":
                record.append(min(numbers))
            else:
                record.append(max(numbers))
        out.append(record)
    return out, out_columns


def bind_data(spec: dict[str, Any], *, api: PluginAPI) -> dict[str, Any]:
    bind = spec.get("bind")
    if not isinstance(bind, dict) or not str(bind.get("path") or "").strip():
        return {"columns": [], "rows": [], "meta": {}, "warnings": []}

    path = _resolve(api, bind.get("path"))
    table = read_table(path, sheet=bind.get("sheet"))
    all_columns: list[str] = table["columns"]
    wanted = _bind_columns(spec) or all_columns[:MAX_COLUMNS]
    missing = [name for name in wanted if name not in all_columns]
    if missing:
        raise ParamError(f"bind.columns 里有不存在的列：{missing}；可用列：{all_columns}")
    indexes = [all_columns.index(name) for name in wanted]
    rows = [[row[i] for i in indexes] for row in table["rows"] if len(row) > max(indexes, default=-1)]
    warnings: list[str] = []

    aggregate = bind.get("aggregate")
    if isinstance(aggregate, dict) and aggregate:
        before = len(rows)
        rows, wanted = _aggregate(rows, wanted, aggregate)
        warnings.append(f"已按 {aggregate.get('by')} 做 {aggregate.get('op')} 聚合：{before} 行 → {len(rows)} 行")

    limit = bind.get("limit")
    if isinstance(limit, int) and limit > 0:
        if len(rows) > limit:
            warnings.append(f"按 bind.limit={limit} 截断：{len(rows)} 行只剩前 {limit} 行（图注要说明）")
            rows = rows[:limit]
    if len(rows) > MAX_BOUND_ROWS:
        raise ParamError(
            f"绑定后 {len(rows)} 行超过上限 {MAX_BOUND_ROWS}：加 bind.aggregate（按类目聚合）"
            "或 bind.limit（明确截断），不要默默截断"
        )

    coerced = [[_coerce(value) for value in row] for row in rows]
    digest = hashlib.sha256(_read_bytes(path)).hexdigest()
    return {
        "columns": wanted,
        "rows": coerced,
        "meta": {
            "path": str(bind.get("path")),
            "rows": len(coerced),
            "sha256": digest,
            "sheet": bind.get("sheet") or table["meta"].get("sheet"),
            "kind": table["meta"].get("kind"),
        },
        "warnings": warnings,
    }


# -- 渲染 -----------------------------------------------------------------------------


HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="color-scheme" content="light dark" />
<!-- 只挡"往站外送数据"和嵌入：不设 default-src，脚本/样式才不受影响。 -->
<meta http-equiv="Content-Security-Policy" content="connect-src 'none'; img-src data: blob:; media-src 'none'; frame-src 'none'; object-src 'none'; form-action 'none'; base-uri 'none'" />
<meta name="referrer" content="no-referrer" />
<title>图表</title>
<style>
  :root {
    --bg: #ffffff; --fg: #111827; --muted: #6b7280; --line: #e5e7eb; --card: #ffffff;
    --accent: #2563eb; --shadow: 0 1px 2px rgba(16, 24, 40, .06), 0 1px 3px rgba(16, 24, 40, .1);
    color-scheme: light;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #0b1220; --fg: #e5e7eb; --muted: #9ca3af; --line: #1f2937; --card: #111827; --shadow: none; color-scheme: dark; }
  }
  html.dark { --bg: #0b1220; --fg: #e5e7eb; --muted: #9ca3af; --line: #1f2937; --card: #111827; --shadow: none; color-scheme: dark; }
  html.light { --bg: #ffffff; --fg: #111827; --muted: #6b7280; --line: #e5e7eb; --card: #ffffff; --shadow: 0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.1); color-scheme: light; }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--fg); }
  body { font: 14px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif; }
  .page { max-width: 1180px; margin: 0 auto; padding: 22px 24px 40px; }
  .head { display: flex; gap: 16px; align-items: flex-start; justify-content: space-between; margin-bottom: 18px; }
  .head h1 { font-size: 20px; line-height: 1.3; margin: 0 0 4px; letter-spacing: .01em; }
  .head .sub { margin: 0; color: var(--muted); font-size: 13px; }
  .actions { display: flex; gap: 8px; flex-shrink: 0; }
  button { font: inherit; font-size: 12px; color: var(--fg); background: var(--card); border: 1px solid var(--line);
    border-radius: 7px; padding: 5px 10px; cursor: pointer; box-shadow: var(--shadow); }
  button:hover { border-color: var(--accent); color: var(--accent); }
  .grid { display: grid; gap: 18px; grid-template-columns: repeat(var(--cols, 1), minmax(0, 1fr)); }
  .panel { margin: 0; background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 14px 14px 10px; box-shadow: var(--shadow); min-width: 0; }
  .panel-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 6px; }
  .panel-head b { font-size: 13px; font-weight: 600; letter-spacing: .01em; }
  .chart { width: 100%; height: 320px; }
  .grid[data-count="1"] .chart { height: 480px; }
  .panel figcaption { margin-top: 6px; color: var(--muted); font-size: 12px; min-height: 1em; }
  .foot { margin-top: 22px; padding-top: 12px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px;
    display: flex; flex-wrap: wrap; gap: 6px 18px; }
  .foot code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
  .err { margin: 24px; padding: 16px; border: 1px solid #fca5a5; border-radius: 10px; background: #fef2f2; color: #991b1b;
    font: 13px/1.7 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; }
</style>
</head>
<body>
<main class="page">
  <header class="head">
    <div>
      <h1 id="title">图表</h1>
      <p class="sub" id="subtitle"></p>
    </div>
    <div class="actions">
      <button type="button" id="theme" title="切换深色/浅色">深色</button>
      <button type="button" id="csv" title="导出图中使用的原始数据">导出数据 CSV</button>
    </div>
  </header>
  <section class="grid" id="grid" data-count="0"></section>
  <footer class="foot" id="foot"></footer>
</main>
<script id="pi-chart" type="application/json">__SPEC__</script>
<script src="/vendor/echarts.min.js"></script>
<script>
(function () {
  'use strict';
  var node = document.getElementById('pi-chart');
  var spec;
  try { spec = JSON.parse(node.textContent); } catch (error) {
    document.body.innerHTML = '<pre class="err">图表数据解析失败：' + error.message + '</pre>';
    return;
  }
  var meta = spec.__meta || {};
  var data = spec.__data || null;
  var page = spec.page || {};
  var charts = Array.isArray(spec.charts) ? spec.charts : [];
  var entries = [];
  var dark = null;

  function esc(value) { return String(value == null ? '' : value); }
  function pad(value) { return value < 10 ? '0' + value : String(value); }
  function safeName(text) {
    return (String(text || 'chart').replace(/[\\\\/:*?"<>|\\s]+/g, '-').replace(/^-+|-+$/g, '') || 'chart').slice(0, 60);
  }
  function preferredDark() {
    if (page.theme === 'dark') return true;
    if (page.theme === 'light') return false;
    return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  }
  function fail(message) {
    var box = document.createElement('pre');
    box.className = 'err';
    box.textContent = message;
    document.querySelector('.page').appendChild(box);
  }
  function buildOption(source) {
    var option = source || {};
    if (!option.backgroundColor) option.backgroundColor = 'transparent';
    if (option.dataset && !option.dataset.source) option.dataset.source = data || [];
    // 标记线的默认标签在容器右边缘会被裁掉（实测「月均 21x」只剩「月均 2」）。
    // 放进图内的左上，是这一版唯一强制的样式默认；模型可以显式覆盖。
    var series = option.series;
    if (series && series.length) {
      (Array.isArray(series) ? series : [series]).forEach(function (item) {
        var line = item && item.markLine;
        if (!line) return;
        var label = line.label || (line.label = {});
        if (!label.position) label.position = 'insideEndTop';
        if (label.distance == null) label.distance = 4;
      });
    }
    // 轴名默认放在轴末端，长名字会被裁；有 name 且没指定位置时放到中间。
    ['xAxis', 'yAxis'].forEach(function (key) {
      var axis = option[key];
      if (!axis) return;
      (Array.isArray(axis) ? axis : [axis]).forEach(function (item) {
        if (!item || !item.name || item.nameLocation) return;
        if (key === 'xAxis') { item.nameLocation = 'middle'; item.nameGap = 30; }
        else { item.nameLocation = 'end'; item.nameGap = 12; }
      });
    });
    return option;
  }
  function render() {
    if (!window.echarts) return;
    entries.forEach(function (entry) {
      if (entry.instance) entry.instance.dispose();
      entry.instance = window.echarts.init(entry.el, dark ? 'dark' : null, { renderer: 'canvas' });
      entry.instance.setOption(buildOption(entry.option), true);
    });
  }
  function exportPng(entry, index) {
    if (!entry.instance) return;
    var url = entry.instance.getDataURL({
      type: 'png',
      pixelRatio: 2,
      backgroundColor: dark ? '#0b1220' : '#ffffff'
    });
    var link = document.createElement('a');
    link.href = url;
    link.download = safeName(entry.title || page.title || ('chart-' + (index + 1))) + '.png';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }
  function exportCsv() {
    if (!data || !data.length) { fail('这个页面没有绑定数据，无法导出 CSV。'); return; }
    var lines = data.map(function (row) {
      return row.map(function (cell) {
        var text = cell == null ? '' : String(cell);
        return /[",\\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
      }).join(',');
    });
    // 用 data: URL 而不是 blob:：iframe 是不透明源，部分浏览器会拒绝导航到 blob URL。
    var csv = '\\ufeff' + lines.join('\\r\\n');
    var link = document.createElement('a');
    link.href = 'data:text/csv;charset=utf-8,' + encodeURIComponent(csv);
    link.download = safeName(page.title || 'chart') + '-data.csv';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);  }
  function mount() {
    document.title = esc(page.title) || '图表';
    document.getElementById('title').textContent = esc(page.title) || '未命名图表';
    var sub = document.getElementById('subtitle');
    sub.textContent = esc(page.subtitle);
    if (!page.subtitle) sub.style.display = 'none';
    var cols = Math.max(1, Math.min(3, parseInt(page.columns, 10) || 1));
    if (charts.length === 1) cols = 1;
    var grid = document.getElementById('grid');
    grid.style.setProperty('--cols', String(cols));
    grid.setAttribute('data-count', String(charts.length));

    charts.forEach(function (chart, index) {
      var figure = document.createElement('figure');
      figure.className = 'panel';
      var head = document.createElement('div');
      head.className = 'panel-head';
      var title = document.createElement('b');
      title.textContent = esc(chart.title) || ('图 ' + (index + 1));
      var button = document.createElement('button');
      button.type = 'button';
      button.textContent = 'PNG';
      button.title = '导出这张图（2 倍像素）';
      head.appendChild(title);
      head.appendChild(button);
      var box = document.createElement('div');
      box.className = 'chart';
      box.setAttribute('role', 'img');
      box.setAttribute('aria-label', esc(chart.title) || ('图 ' + (index + 1)));
      figure.appendChild(head);
      figure.appendChild(box);
      if (chart.note) {
        var caption = document.createElement('figcaption');
        caption.textContent = esc(chart.note);
        figure.appendChild(caption);
      }
      grid.appendChild(figure);
      var entry = { el: box, option: chart.option || {}, title: chart.title, instance: null };
      button.addEventListener('click', function () { exportPng(entry, index); });
      entries.push(entry);
    });

    var foot = document.getElementById('foot');
    var parts = [];
    if (meta.path) {
      parts.push('数据来源：' + meta.path + (meta.sheet ? ' / ' + meta.sheet : '') +
        ' · ' + (meta.rows || 0) + ' 行' + (data && data[0] ? ' × ' + data[0].length + ' 列' : ''));
    }
    if (meta.sha256) parts.push('sha256 ' + String(meta.sha256).slice(0, 12));
    ['unit', 'source', 'caveat'].forEach(function (key) {
      var value = (spec.notes || {})[key];
      if (value) parts.push({ unit: '单位：', source: '出处：', caveat: '口径：' }[key] + value);
    });
    if (meta.generatedAt) parts.push('生成于 ' + meta.generatedAt + ' · pi chart-tools');
    parts.forEach(function (part) {
      var span = document.createElement('span');
      span.textContent = part;
      foot.appendChild(span);
    });

    document.getElementById('csv').addEventListener('click', exportCsv);
    document.getElementById('theme').addEventListener('click', function () {
      dark = !dark;
      document.documentElement.classList.toggle('dark', dark);
      document.documentElement.classList.toggle('light', !dark);
      this.textContent = dark ? '浅色' : '深色';
      render();
    });
    window.addEventListener('resize', function () {
      entries.forEach(function (entry) { if (entry.instance) entry.instance.resize(); });
    });

    dark = preferredDark();
    document.documentElement.classList.toggle('dark', dark);
    document.documentElement.classList.toggle('light', !dark);
    document.getElementById('theme').textContent = dark ? '浅色' : '深色';
    render();
  }
  if (window.echarts) { mount(); return; }
  // 产物面板里由服务提供 /vendor/echarts.min.js；文件被单独下载后用 CDN 兜底。
  var fallback = document.createElement('script');
  fallback.src = 'https://unpkg.com/echarts@6.1.0/dist/echarts.min.js';
  fallback.onload = mount;
  fallback.onerror = function () {
    fail('图表引擎没加载：请在 pi 工作台的「产物」面板里打开这个文件（/vendor/echarts.min.js 由服务提供），'
      + '或联网后重新打开。图表数据本身已经在文件里，不需要联网。');
  };
  document.head.appendChild(fallback);
})();
</script>
</body>
</html>
"""


def render_html(spec: dict[str, Any], *, bound: dict[str, Any]) -> str:
    payload = dict(spec)
    # encode 依赖 dataset：模型漏写 dataset 时补一个空对象，渲染时由 JS 填 source。
    charts: list[dict[str, Any]] = []
    for chart in payload.get("charts") or []:
        if not isinstance(chart, dict):
            continue
        option = dict(chart.get("option") or {})
        series = option.get("series")
        needs_dataset = isinstance(series, list) and any(
            isinstance(item, dict) and isinstance(item.get("encode"), dict) for item in series
        )
        if needs_dataset and not isinstance(option.get("dataset"), dict):
            option["dataset"] = {}
        charts.append({**chart, "option": option})
    payload["charts"] = charts
    payload["__meta"] = {
        **(bound.get("meta") or {}),
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "tool": "chart-tools",
    }
    payload["__data"] = [bound["columns"]] + bound["rows"] if bound.get("columns") else None
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return HTML_TEMPLATE.replace("__SPEC__", blob)


# -- 工具 -----------------------------------------------------------------------------


def _default_out(spec_path: Path) -> Path:
    name = spec_path.name
    if name.endswith(".spec.json"):
        return spec_path.with_name(name[: -len(".spec.json")] + ".html")
    return spec_path.with_suffix(".html")


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.2f} MB"


# -- 模板库 ---------------------------------------------------------------------------

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

FAMILIES: dict[str, str] = {
    "compare": "比较",
    "trend": "趋势",
    "composition": "构成",
    "distribution": "分布",
    "relation": "关系",
    "special": "专用",
}


def load_templates() -> list[dict[str, Any]]:
    """读 ``templates/*.json``。

    模板是**可执行资产**而不是文档片段：测试会把每个模板渲染一遍，所以「模板里写的
    option 能跑」是有保证的；技能里只留选型表，具体 option 由 ``chart_templates`` 给，
    两边不会各写一份然后漂移。
    """
    items: list[dict[str, Any]] = []
    if not TEMPLATE_DIR.is_dir():
        return items
    for path in sorted(TEMPLATE_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("id") and isinstance(data.get("option"), dict):
            items.append(data)
    return items


def template_spec(template: dict[str, Any], *, data_path: str | None = None) -> dict[str, Any]:
    """把一个模板展开成可编辑的 spec：只留 bind.path / title 需要人工改。"""
    bind = template.get("bind")
    title = str(template.get("title") or template.get("name") or "图表")
    spec: dict[str, Any] = {
        "schema": SCHEMA,
        "page": {"title": title, "subtitle": "", "columns": 1, "theme": "auto"},
        "charts": [
            {
                "title": title,
                "option": copy.deepcopy(template.get("option") or {}),
                "note": str(template.get("note") or ""),
            }
        ],
    }
    if isinstance(bind, dict) and bind.get("columns"):
        spec["bind"] = {
            "path": data_path or "<把这里改成你的数据文件>",
            "sheet": None,
            "columns": list(bind["columns"]),
        }
    notes = template.get("notes") or {}
    spec["notes"] = {
        "unit": str(notes.get("unit") or ""),
        "source": "",
        "caveat": str(notes.get("caveat") or ""),
    }
    return spec


def setup(api: PluginAPI) -> None:  # noqa: C901 - tools, one closure each
    @api.register_tool(
        name="chart_templates",
        description=(
            "取图表模板（37 种，全部经过渲染测试）：柱/分组柱/横向条形/堆叠/百分比/子弹/瀑布、"
            "折线/多指标/面积/阶梯/堆叠面积/区间带/目标线/主题河流、饼/环/玫瑰/双层环/矩形树/"
            "旭日/桑基/和弦、直方/箱线/蜂群/热力/日历/平行坐标、散点/气泡/双轴/关系图/矩阵、"
            "仪表盘/极坐标/K线。不传参列出全部；传 id 取完整 spec 骨架（改 bind.path 和 title 即可）。"
            "不要凭记忆写 option，先取模板再改。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "模板 id，如 bar-basic / waterfall / area-band"},
                "family": {
                    "type": "string",
                    "description": "只看某一族：compare/trend/composition/distribution/relation/special",
                },
                "path": {
                    "type": "string",
                    "description": "可选：你的数据文件；取模板时顺便校验列名，并把路径填进 bind.path",
                },
            },
        },
    )
    async def chart_templates(tool_call_id, args, signal, on_update):
        payload = args or {}
        templates = load_templates()
        if not templates:
            return "模板库为空：plugins/chart-tools/templates 下没有可读的 *.json。"
        wanted = str(payload.get("id") or "").strip()
        family = str(payload.get("family") or "").strip()
        data_path = str(payload.get("path") or "").strip() or None

        if wanted:
            found = next((item for item in templates if item.get("id") == wanted), None)
            if found is None:
                return f"没有模板 {wanted!r}。可用：{', '.join(str(t['id']) for t in templates)}"
            bind = found.get("bind")
            columns = [str(c) for c in bind.get("columns") or []] if isinstance(bind, dict) else []
            if data_path and columns:
                try:
                    table = read_table(_resolve(api, data_path))
                except ParamError as error:
                    return f"参数错误：{error}"
                missing = [name for name in columns if name not in table["columns"]]
                if missing:
                    return (
                        f"{wanted} 需要这些列：{columns}；{data_path} 里缺少 {missing}。"
                        f"该文件实际列：{table['columns']}。换模板，或先整理出一份带这些列的数据。"
                    )
            spec = template_spec(found, data_path=data_path)
            lines = [
                f"模板：{found.get('name')}（{wanted} / {FAMILIES.get(str(found.get('family')), found.get('family'))}）",
                f"适用：{found.get('use')}",
                f"不要用：{found.get('avoid')}",
            ]
            if columns:
                lines.append(f"需要列：{', '.join(columns)}（放进 bind.columns）")
            if found.get("dataNote"):
                lines.append(f"数据：{found['dataNote']}")
            pitfalls = [str(item) for item in found.get("pitfalls") or []]
            if pitfalls:
                lines.append("坑：")
                lines.extend(f"- {item}" for item in pitfalls)
            lines.append("")
            lines.append("把下面的 JSON 存成 charts/<名字>.spec.json，改 bind.path 和 title，")
            lines.append("然后 chart_check → chart_render_html：")
            lines.append("")
            lines.append(json.dumps(spec, ensure_ascii=False, indent=2))
            return "\n".join(lines)

        groups: dict[str, list[dict[str, Any]]] = {}
        for item in templates:
            groups.setdefault(str(item.get("family") or "other"), []).append(item)
        if family:
            if family not in groups:
                return f"没有这一族：{family}；可选 {list(FAMILIES)}"
            groups = {family: groups[family]}
        lines = [f"可用模板 {sum(len(items) for items in groups.values())} 个："]
        for key, items in groups.items():
            lines.append("")
            lines.append(f"{FAMILIES.get(key, key)}（{len(items)}）")
            for item in items:
                suffix = "" if item.get("bind") else " · 结构性数据"
                lines.append(f"- {item['id']}  {item.get('name')} — {item.get('use')}{suffix}")
        lines.append("")
        lines.append('用 chart_templates id="<id>" 取完整 spec；选型先看「适用 / 不要用」。')
        return "\n".join(lines)

    @api.register_tool(
        name="chart_data_profile",
        description=(
            "看一份数据表（CSV/TSV/XLSX/JSON）：列类型、缺失、不同值、数值范围，"
            "给出按数据结构的图表建议和一份可改的 spec 骨架。画任何图之前先调用它，"
            "不要凭表名猜结构。返回里的骨架存成 charts/<名字>.spec.json 再改。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "数据文件路径（相对工作区），如 data/sales.csv"},
                "delimiter": {"type": "string", "description": "CSV 分隔符，默认自动识别"},
                "sheet": {"type": "string", "description": "XLSX 工作表名，默认第一个"},
            },
            "required": ["path"],
        },
    )
    async def chart_data_profile(tool_call_id, args, signal, on_update):
        payload = args or {}
        try:
            path = _resolve(api, payload.get("path"))
            table = read_table(path, delimiter=(payload.get("delimiter") or None), sheet=(payload.get("sheet") or None))
            try:
                rel = path.relative_to(Path(getattr(api, "cwd", "") or os.getcwd()).resolve()).as_posix()
            except ValueError:
                rel = str(path)
            if len(table["rows"]) > MAX_SCAN_ROWS:
                table["rows"] = table["rows"][:MAX_SCAN_ROWS]
            return profile_text(table, rel)
        except ParamError as error:
            return f"参数错误：{error}"

    @api.register_tool(
        name="chart_check",
        description=(
            "只校验 charts/<名字>.spec.json，不写文件。检查 schema、绑定列、encode 引用的列、"
            "custom/3D 这类画不出来的系列、手写数字、饼图分类过多、双轴未说明等。"
            "改完 spec 先跑它，再 chart_render_html。"
        ),
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "spec 路径，如 charts/sales.spec.json"}},
            "required": ["path"],
        },
    )
    async def chart_check(tool_call_id, args, signal, on_update):
        try:
            path, spec = _load_spec(api, (args or {}).get("path"))
            errors, warnings = check_spec(spec, base_dir=path.parent, api=api)
            if not errors and not warnings:
                return "校验通过：没有 error，也没有 warning。可以 chart_render_html 了。"
            lines = [f"{len(errors)} 个错误、{len(warnings)} 个警告："]
            lines += [f"- 错误：{item}" for item in errors]
            lines += [f"- 警告：{item}" for item in warnings]
            if errors:
                lines.append("\n错误必须改，否则不会生成 HTML。")
            else:
                lines.append("\n只有警告：可以渲染，但每条都要在图注或 notes 里交代。")
            return "\n".join(lines)
        except ParamError as error:
            return f"参数错误：{error}"

    @api.register_tool(
        name="chart_render_html",
        description=(
            "把 spec 渲染成交互式 HTML 产物（ECharts 6）：从 bind.path 绑定真实数据、写页脚溯源、"
            "带每张图的 PNG 导出和整页 CSV 导出。这是唯一允许的产图方式——不要手写 HTML/JS。"
            "生成后把 html 和 spec 都 present 给用户。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "spec 路径，如 charts/sales.spec.json"},
                "out": {"type": "string", "description": "输出 html 路径，默认与 spec 同名"},
            },
            "required": ["path"],
        },
    )
    async def chart_render_html(tool_call_id, args, signal, on_update):
        payload = args or {}
        try:
            spec_path, spec = _load_spec(api, payload.get("path"))
            errors, warnings = check_spec(spec, base_dir=spec_path.parent, api=api)
            if errors:
                return "没有生成 HTML，先改这些错误：\n" + "\n".join(f"- {item}" for item in errors)
            bound = bind_data(spec, api=api)
            warnings = warnings + list(bound.get("warnings") or [])
            html = render_html(spec, bound=bound)
            size = len(html.encode("utf-8"))
            if size > MAX_HTML_BYTES:
                return (
                    f"没有写文件：HTML {_format_bytes(size)} 超过 {_format_bytes(MAX_HTML_BYTES)}，"
                    "产物面板会在 512KB 处截断、图就不渲染了。"
                    "减少 bind.columns、加 bind.aggregate 聚合成类目级，或拆成两页。"
                )
            out_path = _resolve(api, payload.get("out"), must_exist=False) if payload.get("out") else _default_out(spec_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(html, encoding="utf-8")
            try:
                rel_out = out_path.relative_to(Path(getattr(api, "cwd", "") or os.getcwd()).resolve()).as_posix()
            except ValueError:
                rel_out = str(out_path)
            charts = len(spec.get("charts") or [])
            meta = bound.get("meta") or {}
            lines = [
                f"已生成 {rel_out}（{_format_bytes(size)}，{charts} 张图）",
                f"数据：{meta.get('path')} · {meta.get('rows', 0)} 行 · sha256 {str(meta.get('sha256') or '')[:12]}",
                "在「产物」面板打开即可交互；每张图右上角 PNG 导出，页头可导出 CSV。",
            ]
            if warnings:
                lines.append("")
                lines.append("警告（要在图注或 notes 里交代）：")
                lines += [f"- {item}" for item in warnings]
            lines.append("")
            lines.append("下一步：present 这个 html 和 spec（spec 是这份图的可改源文件）。")
            return "\n".join(lines)
        except ParamError as error:
            return f"参数错误：{error}"

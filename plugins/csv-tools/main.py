"""csv-tools：工作区 CSV 的探查与条件查询。

只读、只用标准库。路径一律通过 ``workspace_path`` 解析，防止读到别的租户的目录。
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
import re
from typing import Any

from pi_coding_agent.plugins import PluginAPI, workspace_path

MAX_ROWS = 20_000
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_COLUMNS = 12
CELL_LIMIT = 120
csv.field_size_limit(1_000_000)

_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T].*)?$")


class ParamError(Exception):
    pass


def _read_text(path: str) -> tuple[str, str]:
    with open(path, "rb") as handle:
        raw = handle.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ParamError(f"文件超过 {MAX_FILE_BYTES // 1024 // 1024}MB 上限")
    for encoding in ("utf-8-sig", "gb18030", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "utf-8"


def _sniff(text: str, delimiter: str | None) -> str:
    if delimiter:
        if len(delimiter) != 1:
            raise ParamError("delimiter 必须是单个字符")
        return delimiter
    head = "\n".join(text.splitlines()[:20])
    try:
        return csv.Sniffer().sniff(head, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def _load(path: str, delimiter: str | None) -> tuple[list[str], list[list[str]], str, str, bool]:
    text, encoding = _read_text(path)
    sep = _sniff(text, delimiter)
    reader = csv.reader(io.StringIO(text), delimiter=sep)
    rows: list[list[str]] = []
    truncated = False
    for row in reader:
        if not row:
            continue
        rows.append(row)
        if len(rows) >= MAX_ROWS + 1:
            truncated = True
            break
    if not rows:
        raise ParamError("文件是空的")
    header = [str(cell).strip() or f"col_{index + 1}" for index, cell in enumerate(rows[0])]
    body = rows[1:]
    # 补/裁到表头宽度，避免不规则 CSV 让后续逻辑错位
    normalized: list[list[str]] = []
    for row in body:
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        normalized.append(row[: len(header)])
    return header, normalized, sep, encoding, truncated


def _infer(values: list[str]) -> str:
    nonempty = [value.strip() for value in values if value.strip() != ""]
    if not nonempty:
        return "empty"
    if all(_INT_RE.match(value) for value in nonempty):
        return "int"
    if all(_INT_RE.match(value) or _FLOAT_RE.match(value) for value in nonempty):
        return "float"
    if all(_DATE_RE.match(value) for value in nonempty):
        return "date"
    if all(value.lower() in ("true", "false", "yes", "no") for value in nonempty):
        return "bool"
    return "string"


def _number(value: str) -> float | None:
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _cell(value: str) -> str:
    text = value.replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()
    return text[:CELL_LIMIT] + ("…" if len(text) > CELL_LIMIT else "")


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(_cell(name) for name in header) + " |"]
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for row in rows:
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    return "\n".join(lines)


def profile(args: dict[str, Any], api: PluginAPI) -> str:
    path = _path(args, api)
    header, rows, sep, encoding, truncated = _load(path, args.get("delimiter"))
    columns = header[:MAX_COLUMNS]
    indexes = list(range(len(columns)))

    summary_lines = [
        f"文件 {os.path.basename(path)} · {len(rows)} 行 × {len(header)} 列"
        f"（分隔符 {sep!r}，编码 {encoding}{'，已截断到 %d 行' % MAX_ROWS if truncated else ''}）",
        "",
    ]
    table_header = ["列", "类型", "非空", "空", "唯一值", "范围 / 高频值"]
    table_rows: list[list[str]] = []
    for index in indexes:
        name = columns[index]
        values = [row[index] for row in rows]
        kind = _infer(values)
        nonempty = sum(1 for value in values if value.strip() != "")
        unique = {value.strip() for value in values if value.strip() != ""}
        unique_text = str(len(unique)) if len(unique) <= 2000 else ">2000"
        detail = ""
        if kind in ("int", "float"):
            numbers = [_number(value) for value in values]
            numbers = [value for value in numbers if value is not None]
            if numbers:
                detail = f"{min(numbers):g} ~ {max(numbers):g}"
        elif len(unique) <= 8 and unique:
            counts: dict[str, int] = {}
            for value in values:
                key = value.strip()
                if key:
                    counts[key] = counts.get(key, 0) + 1
            top = sorted(counts.items(), key=lambda item: -item[1])[:3]
            detail = "、".join(f"{key}({count})" for key, count in top)
        table_rows.append([name, kind, str(nonempty), str(len(values) - nonempty), unique_text, detail])

    summary_lines.append(_table(table_header, table_rows))
    if len(header) > MAX_COLUMNS:
        summary_lines.append(f"\n（只显示前 {MAX_COLUMNS} 列，共 {len(header)} 列）")

    sample_count = _bounded_int(args, "sample_rows", 3, 1, 10)
    summary_lines.append("")
    summary_lines.append(f"样例（前 {sample_count} 行）：")
    summary_lines.append(_table(columns, [row[: len(columns)] for row in rows[:sample_count]]))
    return "\n".join(summary_lines)


def _bounded_int(args: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    raw = args.get(key)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ParamError(f"{key} 必须是整数") from None
    if not lo <= value <= hi:
        raise ParamError(f"{key}={value} 超出范围 [{lo}, {hi}]")
    return value


def _path(args: dict[str, Any], api: PluginAPI) -> str:
    relative = str(args.get("path") or "").strip()
    if not relative:
        raise ParamError("缺少 path（相对工作区的 CSV 路径）")
    try:
        candidate = workspace_path(api, relative)
    except ValueError as error:
        raise ParamError(str(error)) from None
    if not candidate.is_file():
        raise ParamError(f"文件不存在：{relative}")
    if candidate.suffix.lower() not in (".csv", ".tsv", ".txt"):
        raise ParamError("只支持 .csv / .tsv / .txt")
    return str(candidate)


def _condition(text: str) -> tuple[str, str, str]:
    contains = re.match(r"^\s*(.+?)\s+contains\s+(.+?)\s*$", text, re.IGNORECASE)
    if contains:
        return contains.group(1).strip(), "contains", contains.group(2).strip()
    match = re.match(r"^\s*(.+?)\s*(>=|<=|!=|==|=|>|<)\s*(.*?)\s*$", text)
    if not match:
        raise ParamError(f"无法解析条件 {text!r}（支持 col=值、col>=值、col contains 文本）")
    operator = "=" if match.group(2) == "==" else match.group(2)
    return match.group(1).strip(), operator, match.group(3).strip()


def _matches(value: str, operator: str, literal: str) -> bool:
    if operator == "contains":
        return literal.lower() in value.lower()
    left, right = _number(value), _number(literal)
    if left is not None and right is not None:
        if operator == "=":
            return left == right
        if operator == "!=":
            return left != right
        if operator == ">":
            return left > right
        if operator == ">=":
            return left >= right
        if operator == "<":
            return left < right
        if operator == "<=":
            return left <= right
    if operator == "=":
        return value.strip() == literal
    if operator == "!=":
        return value.strip() != literal
    raise ParamError(f"文本列不支持 {operator} 比较，请用 = / != / contains")


def query(args: dict[str, Any], api: PluginAPI) -> str:
    path = _path(args, api)
    header, rows, sep, encoding, truncated = _load(path, args.get("delimiter"))
    lowered = [name.lower() for name in header]

    conditions: list[tuple[int, str, str]] = []
    where = str(args.get("where") or "").strip()
    if where:
        for chunk in re.split(r"\s+and\s+", where, flags=re.IGNORECASE):
            if not chunk.strip():
                continue
            name, operator, literal = _condition(chunk)
            if name.lower() not in lowered:
                raise ParamError(f"没有列 {name!r}；可用列：{', '.join(header[:20])}")
            conditions.append((lowered.index(name.lower()), operator, literal))

    matched = [
        row
        for row in rows
        if all(_matches(row[index], operator, literal) for index, operator, literal in conditions)
    ]

    selected = list(range(min(len(header), MAX_COLUMNS)))
    wanted = str(args.get("columns") or "").strip()
    if wanted:
        selected = []
        for name in (item.strip() for item in wanted.split(",") if item.strip()):
            if name.lower() not in lowered:
                raise ParamError(f"没有列 {name!r}")
            selected.append(lowered.index(name.lower()))
        if len(selected) > MAX_COLUMNS:
            raise ParamError(f"columns 最多 {MAX_COLUMNS} 列")

    limit = _bounded_int(args, "limit", 20, 1, 100)

    shown = matched[:limit]
    lines = [
        f"文件 {os.path.basename(path)} · 共 {len(rows)} 行，匹配 {len(matched)} 行"
        f"（分隔符 {sep!r}，编码 {encoding}{'，已截断' if truncated else ''}）",
        "",
    ]
    if not shown:
        lines.append("没有匹配的行。")
        return "\n".join(lines)
    lines.append(_table([header[index] for index in selected], [[row[index] for index in selected] for row in shown]))
    if len(matched) > limit:
        lines.append(f"\n（显示前 {limit} 行；可提高 limit 或缩小 where）")
    return "\n".join(lines)


def setup(api: PluginAPI) -> None:

    @api.register_tool(
        name="csv_profile",
        description=(
            "探查工作区里的 CSV/TSV：行列数、每列类型、空值数、唯一值、数值范围或高频值，并给前几行样例。"
            "拿到一个陌生表格、需要先了解结构时调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作区的路径，如 data/patients.csv"},
                "delimiter": {"type": "string", "description": "分隔符，默认自动识别（, ; 制表符 |）"},
                "sample_rows": {"type": "integer", "description": "样例行数，1–10，默认 3"},
            },
            "required": ["path"],
        },
    )
    async def csv_profile_tool(tool_call_id, args, signal, on_update):
        try:
            return await asyncio.to_thread(profile, args or {}, api)
        except ParamError as error:
            return f"参数错误：{error}"
        except Exception as error:  # noqa: BLE001
            return f"读取失败：{type(error).__name__}: {error}"

    @api.register_tool(
        name="csv_query",
        description=(
            "对工作区 CSV 做简单条件查询，返回 Markdown 表格。"
            "where 支持 col=值、col>=值、col contains 文本，多个条件用 and 连接；"
            "不传 where 就是预览前 N 行。需要从表格里挑出某类记录时调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作区的 CSV 路径"},
                "where": {
                    "type": "string",
                    "description": "条件，如 \"age>=60 and city contains 北京\"；空则预览",
                },
                "columns": {"type": "string", "description": "要返回的列，逗号分隔；默认前 12 列"},
                "limit": {"type": "integer", "description": "返回行数，1–100，默认 20"},
                "delimiter": {"type": "string", "description": "分隔符，默认自动识别"},
            },
            "required": ["path"],
        },
    )
    async def csv_query_tool(tool_call_id, args, signal, on_update):
        try:
            return await asyncio.to_thread(query, args or {}, api)
        except ParamError as error:
            return f"参数错误：{error}"
        except Exception as error:  # noqa: BLE001
            return f"读取失败：{type(error).__name__}: {error}"

"""pubmed：NCBI E-utilities 的只读检索与摘要。

为什么要包一层：检索式构造、限流、字段裁剪、错误文本化。模型不需要看到整页 XML，
也不需要自己拼 URL。一次 search 最多 10 条，一次 abstract 只取一篇。
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from pi_coding_agent.plugins import PluginAPI

DEFAULT_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
REQUEST_TIMEOUT = 20
MAX_ABSTRACT_CHARS = 8000
#: NCBI 无 key 时 3 次/秒；留一点余量。
MIN_INTERVAL = 0.34

_LAST_CALL = 0.0
_LOCK = asyncio.Lock()


class ParamError(Exception):
    pass


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_all(root: ET.Element, name: str) -> list[ET.Element]:
    return [node for node in root.iter() if _local(node.tag) == name]


def _first(root: ET.Element, name: str) -> ET.Element | None:
    for node in root.iter():
        if _local(node.tag) == name:
            return node
    return None


async def _throttle(interval: float) -> None:
    global _LAST_CALL
    async with _LOCK:
        wait = _LAST_CALL + interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _LAST_CALL = time.monotonic()


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "python-pi-pubmed/1.0 (plugin)"}
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.read(2_000_000)
    except urllib.error.HTTPError as error:
        if error.code in (429, 500, 502, 503, 504):
            time.sleep(0.6)
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return response.read(2_000_000)
        raise
    except urllib.error.URLError:
        time.sleep(0.6)
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.read(2_000_000)


def _params(config: dict[str, Any], **extra: str) -> dict[str, str]:
    params = {"tool": "python-pi"}
    email = str(config.get("email") or "").strip()
    if email:
        params["email"] = email
    api_key = str(config.get("apiKey") or "").strip()
    if api_key:
        params["api_key"] = api_key
    params.update(extra)
    return params


def _search_term(query: str, min_year: int | None, max_year: int | None) -> str:
    if min_year is None and max_year is None:
        return query
    start = min_year if min_year is not None else 1900
    end = max_year if max_year is not None else 2100
    return f"({query}) AND ({start}[PDAT] : {end}[PDAT])"


def _int_arg(args: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    raw = args.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ParamError(f"{key} 必须是整数") from None
    if not lo <= value <= hi:
        raise ParamError(f"{key}={value} 超出范围 [{lo}, {hi}]")
    return value


def _year_arg(args: dict[str, Any], key: str) -> int | None:
    raw = args.get(key)
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ParamError(f"{key} 必须是年份，例如 2023") from None
    if not 1900 <= value <= 2100:
        raise ParamError(f"{key}={value} 不像一个年份")
    return value


def _doi(item: dict[str, Any]) -> str:
    for entry in item.get("articleids") or []:
        if isinstance(entry, dict) and entry.get("idtype") == "doi":
            value = str(entry.get("value") or "").strip()
            if value:
                return value
    elocation = str(item.get("elocationid") or "").strip()
    if elocation.lower().startswith("doi:"):
        return elocation.split(":", 1)[1].strip()
    return ""


async def _search(
    config: dict[str, Any],
    query: str,
    retmax: int,
    sort: str,
    min_year: int | None,
    max_year: int | None,
) -> str:
    base = str(config.get("baseUrl") or DEFAULT_BASE_URL).rstrip("/")
    term = _search_term(query, min_year, max_year)
    sort_param = "pub+date" if sort == "pub_date" else "relevance"

    search_url = f"{base}/esearch.fcgi?" + urllib.parse.urlencode(
        _params(
            config,
            db="pubmed",
            term=term,
            retmode="json",
            retmax=str(retmax),
            sort=sort_param,
        )
    )
    await _throttle(MIN_INTERVAL)
    try:
        raw = await asyncio.to_thread(_fetch, search_url)
        payload = json.loads(raw.decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        return f"检索失败：NCBI 返回 HTTP {error.code}。稍后重试或缩短检索式。"
    except urllib.error.URLError as error:
        return f"检索失败：无法连接 NCBI（{error.reason}）。"
    except (json.JSONDecodeError, ValueError) as error:
        return f"检索失败：无法解析 NCBI 响应（{type(error).__name__}）。"

    ids = (payload.get("esearchresult") or {}).get("idlist") or []
    if not ids:
        return f"没有检索到结果。检索式：{term}"

    summary_url = f"{base}/esummary.fcgi?" + urllib.parse.urlencode(
        _params(config, db="pubmed", id=",".join(ids), retmode="json")
    )
    await _throttle(MIN_INTERVAL)
    try:
        raw = await asyncio.to_thread(_fetch, summary_url)
        summary = json.loads(raw.decode("utf-8", "replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, ValueError) as error:
        return f"检索到了 {len(ids)} 条，但取详情失败（{type(error).__name__}）。可改用 pubmed_abstract 按 PMID 逐条读取。"

    result = summary.get("result") or {}
    lines = [f"检索式：{term}", f"命中 {len(ids)} 条（显示前 {len(ids)} 条）："]
    for index, uid in enumerate(ids, start=1):
        item = result.get(uid) or {}
        title = " ".join(str(item.get("title") or "").split())
        journal = str(item.get("fulljournalname") or item.get("source") or "").strip()
        pubdate = str(item.get("pubdate") or "").strip()
        year = pubdate.split(" ")[0] if pubdate else ""
        authors = [
            str(author.get("name") or "").strip()
            for author in (item.get("authors") or [])
            if isinstance(author, dict) and author.get("name")
        ]
        author_text = ", ".join(authors[:3]) + (", et al." if len(authors) > 3 else "")
        lines.append("")
        lines.append(f"{index}. PMID {uid} · {journal} {year}".rstrip())
        lines.append(f"   {title or '(无标题)'}")
        if author_text:
            lines.append(f"   {author_text}")
        doi = _doi(item)
        if doi:
            lines.append(f"   DOI: {doi}")
    lines.append("")
    lines.append("需要摘要时调用 pubmed_abstract(pmid=...)。")
    return "\n".join(lines)


async def _abstract(config: dict[str, Any], pmid: str) -> str:
    base = str(config.get("baseUrl") or DEFAULT_BASE_URL).rstrip("/")
    url = f"{base}/efetch.fcgi?" + urllib.parse.urlencode(
        _params(config, db="pubmed", id=pmid, retmode="xml")
    )
    await _throttle(MIN_INTERVAL)
    try:
        raw = await asyncio.to_thread(_fetch, url)
    except urllib.error.HTTPError as error:
        return f"读取失败：NCBI 返回 HTTP {error.code}（PMID {pmid}）。"
    except urllib.error.URLError as error:
        return f"读取失败：无法连接 NCBI（{error.reason}）。"

    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return f"读取失败：PMID {pmid} 的响应不是合法 XML。"

    article = _first(root, "Article")
    if article is None:
        return f"没有找到 PMID {pmid} 的记录。"

    title = _text(_first(article, "ArticleTitle"))
    journal = _text(_first(article, "Title"))
    year = _text(_first(article, "Year")) or _text(_first(article, "MedlineDate"))

    authors: list[str] = []
    author_list = _first(article, "AuthorList")
    if author_list is not None:
        for author in author_list:
            if _local(author.tag) != "Author":
                continue
            last = _text(_first(author, "LastName"))
            initials = _text(_first(author, "Initials"))
            collective = _text(_first(author, "CollectiveName"))
            if last:
                authors.append(f"{last} {initials}".strip())
            elif collective:
                authors.append(collective)

    doi = ""
    for identifier in _find_all(article, "ELocationID"):
        if identifier.attrib.get("EIdType") == "doi":
            doi = _text(identifier)
            break

    sections: list[str] = []
    for node in _find_all(article, "AbstractText"):
        label = node.attrib.get("Label") or ""
        text = _text(node)
        if text:
            sections.append(f"{label}: {text}" if label else text)

    lines = [f"PMID {pmid} · {journal} {year}".rstrip(), title or "(无标题)"]
    if authors:
        lines.append("作者：" + ", ".join(authors[:6]) + (" 等" if len(authors) > 6 else ""))
    if doi:
        lines.append(f"DOI: {doi}")
    lines.append("")
    if sections:
        lines.extend(sections)
    else:
        lines.append("(该记录没有摘要)")
    text = "\n".join(lines)
    if len(text) > MAX_ABSTRACT_CHARS:
        text = text[:MAX_ABSTRACT_CHARS] + "\n…（摘要已截断）"
    return text


def setup(api: PluginAPI) -> None:
    config = getattr(api, "config", {}) or {}

    @api.register_tool(
        name="pubmed_search",
        description=(
            "检索 PubMed，返回 PMID、标题、期刊、年份、前三位作者与 DOI。"
            "需要找文献、确认某主题有哪些研究、按年份筛文献时调用。可按相关度或发表时间排序。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索式，支持 PubMed 语法（如 \"SGLT2 inhibitor AND heart failure\"），≤500 字符",
                },
                "retmax": {"type": "integer", "description": "返回条数，1–10，默认 5"},
                "sort": {
                    "type": "string",
                    "enum": ["relevance", "pub_date"],
                    "description": "relevance（默认）或 pub_date（最新优先）",
                },
                "min_year": {"type": "integer", "description": "最早发表年份（可选）"},
                "max_year": {"type": "integer", "description": "最晚发表年份（可选）"},
            },
            "required": ["query"],
        },
    )
    async def pubmed_search_tool(tool_call_id, args, signal, on_update):
        try:
            query = str((args or {}).get("query") or "").strip()
            if len(query) < 2:
                raise ParamError("query 太短")
            if len(query) > 500:
                raise ParamError("query 超过 500 字符")
            retmax = _int_arg(args or {}, "retmax", 5, 1, 10)
            sort = str((args or {}).get("sort") or "relevance")
            if sort not in ("relevance", "pub_date"):
                raise ParamError("sort 只能是 relevance 或 pub_date")
            min_year = _year_arg(args or {}, "min_year")
            max_year = _year_arg(args or {}, "max_year")
            if min_year and max_year and min_year > max_year:
                raise ParamError("min_year 不能大于 max_year")
            return await _search(config, query, retmax, sort, min_year, max_year)
        except ParamError as error:
            return f"参数错误：{error}"

    @api.register_tool(
        name="pubmed_abstract",
        description=(
            "按 PMID 读取 PubMed 摘要，按 Background/Methods/Results 等小节输出，附期刊、年份、作者与 DOI。"
            "需要精读某篇文献的摘要时调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "pmid": {"type": "string", "description": "PubMed ID，纯数字"},
            },
            "required": ["pmid"],
        },
    )
    async def pubmed_abstract_tool(tool_call_id, args, signal, on_update):
        pmid = str((args or {}).get("pmid") or "").strip()
        if not pmid.isdigit():
            return "参数错误：pmid 必须是纯数字。"
        if len(pmid) > 12:
            return "参数错误：pmid 太长。"
        return await _abstract(config, pmid)

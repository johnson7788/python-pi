"""web-fetch：把一个公开网页抓成可读文本。

安全边界（按重要性排序）：
1. 只允许 http/https，拒绝用户名密码、非标准端口不限但会校验目标 IP；
2. 解析域名后逐个 IP 校验，拒绝 private/loopback/link-local/multicast/reserved；
3. 重定向逐跳复检，最多 5 跳；
4. 硬上限：1MB 响应、20 秒超时。

残余风险：DNS rebinding（校验与请求之间 DNS 可变）。真正的出网管控应在网络层做。
"""

from __future__ import annotations

import asyncio
import html as html_module
import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from pi_coding_agent.plugins import PluginAPI

MAX_BYTES = 1_000_000
TIMEOUT = 20
MAX_REDIRECTS = 5
USER_AGENT = "python-pi-web-fetch/1.0 (+plugin)"

_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
_SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "form", "nav", "footer", "aside"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise _Redirect(newurl)


class _Redirect(Exception):
    def __init__(self, location: str) -> None:
        self.location = location


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3", "h4"):
            self.parts.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
        else:
            self.parts.append(data)


def _validate_url(url: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("只支持 http/https")
    if not parsed.hostname:
        raise ValueError("URL 缺少主机名")
    if parsed.username or parsed.password:
        raise ValueError("URL 不能带用户名/密码")
    if not parsed.hostname.isascii():
        # 非 ASCII 域名要求调用方先转 punycode，避免同形字钓鱼
        raise ValueError("域名必须是 ASCII（请使用 punycode）")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        raise ValueError(f"无法解析域名：{parsed.hostname}") from None
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ValueError(f"拒绝访问内网或保留地址：{address}")
    return parsed


def _open_once(url: str) -> Any:
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    return opener.open(request, timeout=TIMEOUT)


def _fetch(url: str) -> tuple[str, str, bytes]:
    """返回 (最终 URL, content-type, body)。"""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _validate_url(current)
        try:
            with _open_once(current) as response:
                final_url = response.geturl()
                content_type = response.headers.get("Content-Type", "")
                body = response.read(MAX_BYTES + 1)
                return final_url, content_type, body
        except _Redirect as redirect:
            current = urllib.parse.urljoin(current, redirect.location)
            continue
        except urllib.error.HTTPError as error:
            if 300 <= error.code < 400:
                location = error.headers.get("Location") if error.headers else None
                if not location:
                    raise ValueError(f"HTTP {error.code} 重定向缺少 Location") from None
                current = urllib.parse.urljoin(current, location)
                continue
            raise ValueError(f"目标返回 HTTP {error.code}") from None
    raise ValueError(f"重定向超过 {MAX_REDIRECTS} 次")


def _html_to_text(raw: str) -> tuple[str, str]:
    parser = _Extractor()
    parser.feed(raw)
    parser.close()
    lines = []
    for line in "".join(parser.parts).replace("\r", "\n").split("\n"):
        cleaned = " ".join(line.split())
        if cleaned:
            lines.append(cleaned)
    title = html_module.unescape(parser.title).strip()
    return title, "\n".join(lines)


def _decode(body: bytes, content_type: str) -> str:
    charset = ""
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            charset = value.strip().strip('"')
    for candidate in (charset, "utf-8", "gb18030"):
        if not candidate:
            continue
        try:
            return body.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")


def render(url: str, max_chars: int, fmt: str) -> str:
    final_url, content_type, body = _fetch(url)
    if len(body) > MAX_BYTES:
        body = body[:MAX_BYTES]
    media_type = content_type.split(";")[0].strip().lower()

    if fmt == "json" or media_type in ("application/json", "application/ld+json") or media_type.endswith("+json"):
        text = _decode(body, content_type)
        try:
            formatted = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            formatted = text
        return f"URL：{final_url}\n类型：{media_type}\n\n" + formatted[:max_chars]

    if media_type in ("text/html", "application/xhtml+xml"):
        title, text = _html_to_text(_decode(body, content_type))
        header = f"标题：{title or '(无标题)'}\nURL：{final_url}\n"
        return header + "\n" + (text[:max_chars] or "(没有可读正文)")

    if media_type.startswith("text/") or media_type in (
        "application/xml",
        "application/x-yaml",
        "application/yaml",
    ):
        return f"URL：{final_url}\n类型：{media_type}\n\n" + _decode(body, content_type)[:max_chars]

    return (
        f"URL：{final_url}\n类型：{media_type or '(未知)'}\n"
        "这个类型不是文本，未解析。可改用 bash 下载后用相应工具处理。"
    )


def setup(api: PluginAPI) -> None:
    @api.register_tool(
        name="web_fetch",
        description=(
            "抓取一个公开网页并返回可读文本（标题 + 正文）或格式化 JSON。"
            "需要读网页、查文档、核对某个链接内容时调用。只支持公网 http/https，拒绝内网地址。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "完整 URL，必须 http/https"},
                "max_chars": {
                    "type": "integer",
                    "description": "正文最多返回多少字符，500–20000，默认 6000",
                },
                "format": {
                    "type": "string",
                    "enum": ["text", "json"],
                    "description": "text（默认，HTML 转文本）或 json（请求 JSON 时格式化）",
                },
            },
            "required": ["url"],
        },
    )
    async def web_fetch_tool(tool_call_id, args, signal, on_update):
        url = str((args or {}).get("url") or "").strip()
        if not url:
            return "参数错误：缺少 url。"
        max_chars = 6000
        raw_max = (args or {}).get("max_chars")
        if raw_max not in (None, ""):
            try:
                max_chars = int(raw_max)
            except (TypeError, ValueError):
                return "参数错误：max_chars 必须是整数。"
            if not 500 <= max_chars <= 20000:
                return "参数错误：max_chars 超出范围 [500, 20000]。"
        fmt = str((args or {}).get("format") or "text").lower()
        if fmt not in ("text", "json"):
            return "参数错误：format 只能是 text 或 json。"

        try:
            return await asyncio.to_thread(render, url, max_chars, fmt)
        except ValueError as error:
            return f"抓取失败：{error}"
        except urllib.error.URLError as error:
            return f"抓取失败：{error.reason}"
        except TimeoutError:
            return f"抓取失败：超过 {TIMEOUT} 秒。"
        except Exception as error:  # noqa: BLE001 - 下游什么都可能抛
            return f"抓取失败：{type(error).__name__}: {error}"

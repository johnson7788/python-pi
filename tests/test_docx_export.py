"""Markdown → DOCX 导出。

只测两件事：转换结果是合法 OOXML、正文内容真的进去了。前端的"下载 Word"按钮直接
调这个接口，坏一次用户就拿到一个打不开的文件——比报错更糟。
"""

from __future__ import annotations

import io
import zipfile

from pi_server.docx_export import markdown_to_docx

SAMPLE = """# 结论

**优先联合**：PFS HR 0.62（0.49–0.79）[E1]。

## 依据

| 试验 | 人群 | HR |
|---|---|---|
| FLAURA2 | 19del/L858R | 0.62 |

- 要点一：`HR 0.62`
- 要点二：见 [原文](https://example.com)
"""


def test_markdown_to_docx_is_a_valid_package():
    data = markdown_to_docx(SAMPLE, title="回答", subtitle="2026-11-12 · EGFR 19del")
    assert data[:2] == b"PK"
    archive = zipfile.ZipFile(io.BytesIO(data))
    names = archive.namelist()
    assert "word/document.xml" in names
    assert "[Content_Types].xml" in names


def test_markdown_to_docx_keeps_the_text_and_tables():
    data = markdown_to_docx(SAMPLE, title="回答")
    document = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode("utf-8")
    assert "优先联合" in document
    assert "FLAURA2" in document
    assert "HR 0.62" in document
    assert "要点一" in document
    # 表格用真正的 <w:tbl>，不是一坨文本
    assert "<w:tbl>" in document


def test_empty_markdown_still_produces_an_openable_file():
    data = markdown_to_docx("", title="空")
    assert zipfile.ZipFile(io.BytesIO(data)).namelist().count("word/document.xml") == 1

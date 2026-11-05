"""Curated skill/plugin marketplace catalog.

V1 ships a small curated catalog with the deployment.  Items carry provenance
(``source``/``author``) and optional install payloads:

- skills: a SKILL.md body written into the user's agent skills directory;
- plugins: a local package directory copied into the deployment plugin root.

A remote registry can replace this module later without changing the API shape.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]


@dataclass(frozen=True)
class MarketItem:
    kind: str
    id: str
    name: str
    description: str
    version: str
    author: str
    source: str
    tags: tuple[str, ...] = ()
    content: str | None = None
    package: str | None = None


def _skill(name: str, description: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body.strip()}\n"


ITEMS: tuple[MarketItem, ...] = (
    MarketItem(
        kind="skill",
        id="evidence-table",
        name="证据表",
        description="把纳入研究整理成可追溯的证据表，包含研究、样本、干预、结局和偏倚风险。",
        version="1.0.0",
        author="平台团队",
        source="团队",
        tags=("研究", "综述", "表格"),
        content=_skill(
            "evidence-table",
            "把纳入研究整理成可追溯的证据表。",
            """
## 何时使用
当用户需要从多篇研究中提取可比信息，或要求“证据表/证据总结”时。

## 输出格式
先给一张 Markdown 表，列固定为：
研究（作者+年份）、设计、样本量、人群、干预/暴露、对照、主要结局、效应量、偏倚风险、来源链接。

## 规则
- 每个单元格必须来自原文；缺失写“未报告”，不要推断。
- 效应量保留原文单位和置信区间。
- 表格后列出 3-5 条跨研究结论，并注明证据强度。
""",
        ),
    ),
    MarketItem(
        kind="skill",
        id="meeting-notes",
        name="会议纪要",
        description="把会议记录整理成结论、决策、待办和风险，默认输出简洁的中文纪要。",
        version="1.0.0",
        author="平台团队",
        source="团队",
        tags=("办公", "会议", "总结"),
        content=_skill(
            "meeting-notes",
            "把会议记录整理成结论、决策、待办和风险。",
            """
## 输出结构
1. 一句话结论
2. 已达成的决策（最多 5 条）
3. 待办：事项 / 负责人 / 截止时间
4. 未决问题与风险
5. 需要补充的信息

## 规则
- 不把讨论过程写成结论；没有负责人的待办明确标注“待指派”。
- 时间不明确时不要编造日期。
""",
        ),
    ),
    MarketItem(
        kind="skill",
        id="weekly-report",
        name="周报",
        description="按“进展、结果、问题、下周计划”生成可汇报的周报。",
        version="1.0.0",
        author="社区贡献",
        source="社区",
        tags=("办公", "汇报"),
        content=_skill(
            "weekly-report",
            "生成结构化的周报。",
            """
## 结构
- 本周进展（按项目分组，每条带结果）
- 关键数据（有数字就列数字）
- 问题与风险（含影响和建议）
- 下周计划（可验收的动作）

## 规则
- 每条进展尽量包含“做了什么 → 结果是什么”。
- 没有数据时不要编造；标注“暂无数据”。
""",
        ),
    ),
    MarketItem(
        kind="skill",
        id="literature-screening",
        name="文献筛选",
        description="按纳排标准逐条判断文献，输出纳入/排除理由和不确定项。",
        version="1.0.0",
        author="社区贡献",
        source="社区",
        tags=("研究", "文献"),
        content=_skill(
            "literature-screening",
            "按纳排标准筛选文献并说明理由。",
            """
## 流程
1. 先复述纳排标准，确认没有歧义。
2. 对每条文献输出：标题、判断（纳入/排除/不确定）、对应标准、理由。
3. 汇总排除原因分布。
4. 列出需要人工复核的“不确定”条目。

## 规则
- 只根据标题/摘要无法判断时，标“不确定”，不要强行纳入。
- 理由必须引用具体标准编号。
""",
        ),
    ),
    MarketItem(
        kind="plugin",
        id="hello-pi",
        name="Hello Pi",
        description="最小插件示例：注册 hello_plugin 工具，演示 manifest 与 setup(api)。",
        version="0.1.0",
        author="平台团队",
        source="官方示例",
        tags=("示例", "工具"),
        package=str(REPO_ROOT / "examples" / "plugins" / "hello-pi"),
    ),
)

_BY_KEY = {(item.kind, item.id): item for item in ITEMS}


def list_items(kind: str | None = None) -> list[MarketItem]:
    return [item for item in ITEMS if kind is None or item.kind == kind]


def find_item(kind: str, item_id: str) -> MarketItem | None:
    return _BY_KEY.get((kind, item_id))


def search_items(kind: str, query: str = "", tag: str = "") -> list[MarketItem]:
    needle = query.strip().lower()
    wanted_tag = tag.strip()
    items = [item for item in ITEMS if item.kind == kind]
    if wanted_tag:
        items = [item for item in items if wanted_tag in item.tags]
    if not needle:
        return items
    return [
        item
        for item in items
        if needle in item.name.lower()
        or needle in item.description.lower()
        or needle in item.author.lower()
        or needle in item.source.lower()
        or any(needle in item_tag.lower() for item_tag in item.tags)
    ]


def readme_for(item: MarketItem) -> str:
    """Human-readable detail body: skill markdown, or the package README."""
    if item.content:
        return item.content
    if item.package:
        readme = os.path.join(item.package, "README.md")
        try:
            with open(readme, "r", encoding="utf-8") as handle:
                return handle.read()[:20000]
        except OSError:
            return ""
    return ""


def item_json(item: MarketItem) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "version": item.version,
        "author": item.author,
        "source": item.source,
        "tags": list(item.tags),
    }

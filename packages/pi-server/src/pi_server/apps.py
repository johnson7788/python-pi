"""Built-in app / work-mode / scene-capsule catalog.

Apps are declarative compositions: a list of work modes, each with a prompt and an
optional skill binding, plus the capsules shown above the composer.  The runtime turns
one mode into a frozen :class:`Composition` when a worker is created.

V1 keeps the catalog in code so every deployment has a usable set out of the box.  The
same shapes can later move to a tenant-editable table without changing the resolver.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_APP_ID = "general"
DEFAULT_MODE_ID = "office"


@dataclass(frozen=True)
class Capsule:
    id: str
    name: str
    prompt: str


@dataclass(frozen=True)
class Mode:
    id: str
    name: str
    description: str
    prompt: str
    #: ``None`` means every discovered skill; a tuple binds the mode to a subset.
    skills: tuple[str, ...] | None = None
    capsules: tuple[Capsule, ...] = field(default_factory=tuple)
    #: Example prompts shown on the home screen; empty means "no examples".
    examples: tuple[str, ...] = ()
    #: Composer placeholder for this mode.
    placeholder: str = ""
    #: Icon name from the web icon set (``spark``/``code``/``palette``/...).
    icon: str = ""


@dataclass(frozen=True)
class App:
    id: str
    name: str
    description: str
    accent: str
    modes: tuple[Mode, ...]


GENERAL = App(
    id="general",
    name="通用工作台",
    description="日常办公、开发与设计任务；不限定行业。",
    accent="#3b5fba",
    modes=(
        Mode(
            id="office",
            name="办公",
            description="文档、研究、汇报与日常事务。",
            prompt=(
                "当前应用是「通用工作台 · 办公」。优先给出可直接使用的结论和材料；"
                "涉及事实时标注来源，涉及文件时说明产物路径。"
            ),
            capsules=(
                Capsule("make-ppt", "制作 PPT", "帮我制作一份演示文稿，先给大纲，再逐页生成内容。"),
                Capsule("deep-research", "深度研究", "对这个主题做一次深度研究，给出结论、证据和来源。"),
                Capsule("plan", "策划方案", "帮我策划一个可执行的方案，包含目标、步骤、风险和验收标准。"),
                Capsule("schedule", "定时任务", "帮我设计一个可以定时执行的自动化任务。"),
            ),
            examples=(
                "这个周末，附近有什么值得去",
                "最近 7 天，短视频都在流行什么",
                "每月定投 1000 元，5 年后能有多少钱",
                "最近 AI 行业变了什么，给我一份变更日志",
            ),
            placeholder="/调用技能与指令",
            icon="spark",
        ),
        Mode(
            id="dev",
            name="开发",
            description="读代码、定位问题、补测试、准备发布。",
            prompt=(
                "当前应用是「通用工作台 · 开发」。先读相关代码再下结论；改动要最小，"
                "说明验证方式，并遵守仓库现有风格。"
            ),
            capsules=(
                Capsule("repo-map", "读仓库结构", "先梳理这个仓库的结构、入口和最该关注的文件。"),
                Capsule("bug", "定位一个 bug", "根据我给的报错定位根因，先解释再修复。"),
                Capsule("tests", "补测试", "找出这块逻辑的边界条件并补上测试。"),
                Capsule("release", "准备发布", "检查发布前需要确认的配置、迁移和回滚步骤。"),
            ),
            examples=(
                "把服务端的排队消息改成持久化",
                "这个 traceback 是什么原因，帮我修掉",
                "给会话模块补 5 个边界测试",
                "看看这个仓库最该先补的 3 个能力",
            ),
            placeholder="描述要改的功能，或贴一段报错",
            icon="code",
        ),
        Mode(
            id="design",
            name="设计",
            description="界面、视觉方向与竞品拆解。",
            prompt=(
                "当前应用是「通用工作台 · 设计」。先明确目标用户与使用场景，再给具体界面方案；"
                "视觉判断要可落地，避免空泛形容。"
            ),
            capsules=(
                Capsule("prototype", "做原型", "把这个需求做成一个可点击的 HTML 原型。"),
                Capsule("palette", "配色方案", "给这个产品定一套克制的浅色配色，并说明使用规则。"),
                Capsule("competitor", "竞品分析", "拆解三个同类产品的首屏和关键流程差异。"),
                Capsule("icon", "图标", "为这个功能设计一个简洁的图标方向。"),
            ),
            examples=(
                "把首页做成新版那样，先出 HTML",
                "给这个产品定一套克制的浅色配色",
                "拆解 3 个同类产品的首屏差异",
                "这套界面还缺什么状态，列给我",
            ),
            placeholder="描述你想做的界面或视觉方向",
            icon="palette",
        ),
    ),
)

RESEARCH = App(
    id="research",
    name="研究综述",
    description="循证检索、文献筛选、证据表与综述写作。",
    accent="#2f7d6b",
    modes=(
        Mode(
            id="review",
            name="综述",
            description="从 PICO 到证据表，再到综述初稿。",
            prompt=(
                "当前应用是「研究综述」。按循证流程工作：先明确 PICO 与纳排标准，"
                "再检索和筛选；每个结论都要能追溯到具体文献，不确定的地方明确标注。"
            ),
            capsules=(
                Capsule("pico", "明确 PICO", "先把研究问题拆成 PICO，并给出纳排标准。"),
                Capsule("search", "制定检索式", "为这个 PICO 制定中英文检索式和数据库范围。"),
                Capsule("evidence", "提取证据表", "把纳入研究整理成可追溯的证据表。"),
                Capsule("outline", "综述提纲", "基于现有证据给出综述提纲和每节要点。"),
            ),
            examples=(
                "帮我评估这个临床问题值不值得做系统评价",
                "把这三篇 RCT 整理成证据表",
                "为这个 PICO 写中英文检索式",
                "这份综述初稿的逻辑缺什么",
            ),
            placeholder="输入研究问题、PICO 或文献片段",
            icon="book",
        ),
    ),
)

SLIDES = App(
    id="slides",
    name="演示文稿",
    description="从大纲到逐页内容与讲稿。",
    accent="#b56b2a",
    modes=(
        Mode(
            id="deck",
            name="制作",
            description="先结构，再页面，最后讲稿。",
            prompt=(
                "当前应用是「演示文稿」。先确认受众、时长和核心结论，再给大纲；"
                "每一页只表达一个观点，正文不超过要点式短句。"
            ),
            capsules=(
                Capsule("outline-10", "10 页大纲", "给这个主题做一份 10 页的演示大纲。"),
                Capsule("story", "故事线", "把这份材料改造成有起承转合的汇报故事线。"),
                Capsule("notes", "讲稿", "为这套幻灯片写逐页讲稿，控制在 15 分钟。"),
            ),
            examples=(
                "给季度复盘做一份 10 页汇报",
                "把这份文档改成 15 分钟的汇报故事线",
                "这套幻灯片每页只留一个观点，帮我改",
                "给这页配一段讲稿",
            ),
            placeholder="输入主题、受众和时长",
            icon="slides",
        ),
    ),
)

APPS: tuple[App, ...] = (GENERAL, RESEARCH, SLIDES)
_BY_ID = {app.id: app for app in APPS}


def find_app(app_id: str | None) -> App | None:
    return _BY_ID.get(app_id or DEFAULT_APP_ID)


def find_mode(app_id: str | None, mode_id: str | None) -> tuple[App, Mode] | None:
    app = find_app(app_id)
    if app is None:
        return None
    wanted = mode_id or app.modes[0].id
    for mode in app.modes:
        if mode.id == wanted:
            return app, mode
    return None


def composition_for(app_id: str | None, mode_id: str | None) -> tuple[str, tuple[str, ...] | None]:
    """Return ``(prompt, skill_allowlist)`` for a validated app/mode pair."""
    found = find_mode(app_id, mode_id)
    if found is None:
        found = find_mode(DEFAULT_APP_ID, DEFAULT_MODE_ID)
        if found is None:  # pragma: no cover - the catalog always has a default
            return "", None
    _app, mode = found
    return mode.prompt, mode.skills


def app_json(app: App) -> dict[str, Any]:
    return {
        "id": app.id,
        "name": app.name,
        "description": app.description,
        "accent": app.accent,
        "modes": [
            {
                "id": mode.id,
                "name": mode.name,
                "description": mode.description,
                "skillBinding": "all" if mode.skills is None else list(mode.skills),
                "prompt": mode.prompt,
                "examples": list(mode.examples),
                "placeholder": mode.placeholder,
                "icon": mode.icon,
                "capsules": [
                    {"id": capsule.id, "name": capsule.name, "prompt": capsule.prompt}
                    for capsule in mode.capsules
                ],
            }
            for mode in app.modes
        ],
    }


def catalog_json() -> dict[str, Any]:
    return {
        "apps": [{**app_json(app), "builtin": True} for app in APPS],
        "defaultApp": DEFAULT_APP_ID,
        "defaultMode": DEFAULT_MODE_ID,
    }

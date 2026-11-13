#!/usr/bin/env python3
"""生成 docs/tree.md：顶层说明 + 完整目录树 + 生成物清单 + 清理记录。

用法::

    .venv/bin/python scripts/gen_tree.py

跳过 `.git` / `.venv` / `node_modules` / `__pycache__` / Rust `target` 与已知构建产物，
它们在「生成物与不入库」一节单列。改目录结构后重跑一次即可。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs/tree.md"

SKIP_DIRS = {
    ".git",
    ".venv",
    ".local",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".DS_Store",
}
#: 生成物目录：树里只留一行汇总，不展开。
SKIP_PATHS = {
    "office/xlsx-engine/target",
    "office-dist",
    "web/dist",
    "output",
}

DESCRIPTIONS: list[tuple[str, str]] = [
    ("packages/", "4 个 Python 包：`pi-ai`（模型层）、`pi-agent-core`（agent 循环）、`pi-coding-agent`（工具/扩展/skills）、`pi-server`（HTTP+WS 服务、多租户、应用目录）"),
    ("web/", "浏览器前端（React + Vite）：`src/workbench/` 是主界面，另有 `pages/`、`components/`、`lib/`"),
    ("office/", "三个编辑器（doc / slides / sheets）的前端工程 + Rust `xlsx-engine`；构建产物写到 `office-dist/`"),
    ("plugins/", "内置插件：`pubmed`、`clinical-calc`、`csv-tools`、`web-fetch`、`chart-tools`"),
    ("template/", "team 模式的租户模板：`skills/` 软链给每个新用户"),
    ("tests/", "pytest：规则层单测 + 真起服务的端到端测试（`test_sandbox_end_to_end.py` 需要 Docker）"),
    ("deploy/", "部署：systemd unit、Dockerfile、sandbox provisioner、备份、压测、smoke"),
    ("docs/", "设计文档、差距分析、截图、mockup、本文档"),
    ("apps/", "应用的**非代码交付物**（技能、安装脚本、示例答案）；应用定义在 `packages/pi-server/src/pi_server/apps.py`"),
    ("plugin-kit/", "插件开发者工具：`new` / `check` / `dev` / `pack`（`pikit.py`）"),
    ("scripts/", "构建脚本：office、xlsx-engine sidecar，以及生成本文档的 `gen_tree.py`"),
    ("examples/", "可运行的示例：离线 agent、假模型服务、示例插件"),
]

UNTRACKED_NOTE = {
    "office-dist/": "office 三个编辑器的构建产物，FastAPI 挂到 `/office`",
    ".venv/": "Python 依赖（uv）",
    ".env": "本机配置：`PI_API_KEY`、`MODEL`（gitignore）",
}


def tracked_counts() -> dict[str, int]:
    """每个顶层目录下被 git 跟踪的文件数（用 -z 避开非 ASCII 路径的转义）。"""
    try:
        raw = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        ).stdout.decode("utf-8")
    except (OSError, subprocess.CalledProcessError):
        return {}
    counts: dict[str, int] = {}
    for path in filter(None, raw.split("\0")):
        counts[path.split("/", 1)[0]] = counts.get(path.split("/", 1)[0], 0) + 1
    return counts


def count_files(path: Path) -> int:
    return sum(1 for item in path.rglob("*") if item.is_file())


def walk(directory: Path, prefix: str = "") -> list[str]:
    lines: list[str] = []
    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name.lower())
    except PermissionError:
        return lines
    shown = [entry for entry in entries if entry.name not in SKIP_DIRS]
    for index, entry in enumerate(shown):
        last = index == len(shown) - 1
        connector = "└── " if last else "├── "
        rel = entry.relative_to(ROOT).as_posix()
        if entry.is_dir():
            if rel in SKIP_PATHS:
                lines.append(f"{prefix}{connector}{entry.name}/  ({count_files(entry)} 个文件，生成物，略)")
                continue
            lines.append(f"{prefix}{connector}{entry.name}/")
            lines.extend(walk(entry, prefix + ("    " if last else "│   ")))
        elif entry == TARGET:
            lines.append(f"{prefix}{connector}{entry.name}  (本文档自身)")
        else:
            lines.append(f"{prefix}{connector}{entry.name}  ({entry.stat().st_size:,} B)")
    return lines


def header() -> str:
    counts = tracked_counts()
    rows = []
    for name, description in DESCRIPTIONS:
        key = name.rstrip("/")
        rows.append(f"| `{name}` | {description} | 是（{counts.get(key, 0)}） |")
    for name, description in UNTRACKED_NOTE.items():
        rows.append(f"| `{name}` | {description} | 否（生成/本机） |")
    return (
        "# 目录地图\n\n"
        "python-pi 仓库的文件清单与用途说明。回答三个问题：**这个文件是什么、能不能删、删了怎么恢复**。\n\n"
        "> 由 `scripts/gen_tree.py` 遍历生成，跳过 `.git` / `.venv` / `node_modules` / `__pycache__` /\n"
        "> Rust `target` 等生成物——它们在文末「生成物与不入库」一节单列。改目录结构后重跑：\n"
        "> `.venv/bin/python scripts/gen_tree.py`\n\n"
        "## 顶层一览\n\n"
        "| 目录 / 文件 | 是什么 | 入库（文件数） |\n|---|---|---|\n"
        + "\n".join(rows)
        + "\n\n## 完整树\n\n```\n"
    )


FOOTER = """```

## 应用（App）放在哪里

应用定义**不在** `apps/` 目录里。三个来源，各管一段：

| 来源 | 位置 | 谁改 | 说明 |
|---|---|---|---|
| **内置应用** | `packages/pi-server/src/pi_server/apps.py` 里的 `App(...)` 常量（9 个） | 代码，随版本发布 | `general` / `research` / `drug` / `slides` / `stats` / `grant` / `market` / `patient-edu` / `chart` |
| **team 模式的运行时应用** | `<data-root>/pi.db` → `team_apps` + `team_app_modes` + `team_app_capsules` | 管理员在「应用管理」页增删改 | 启动后第一次读应用列表时把内置目录播种进来（`ensure_apps()` → `seed_team_apps()`） |
| **`apps/` 目录** | `apps/<name>/`：技能、安装脚本、示例、说明 | 应用作者 | 只放"写不进代码又要跟着应用交付"的东西；**不是应用定义** |

```
apps.py（内置定义）──seed──▶ pi.db: team_apps（运行时真身）──▶ 前端「应用管理」
        │                         ▲
        └── 单机模式直接用 ────────┘  （没有 DB、没有应用管理页）
```

所以 `apps/` 下只有 `chart/` 是正常的——它是唯一带专属技能包的应用；
其它 8 个的提示词、模式、胶囊全在 `apps.py` 里，不需要目录。详见 `apps/README.md`。

**播种不覆盖**：`seed_team_apps()` 只补缺失的 id，不会覆盖已有行。所以改了 `apps.py` 里的
提示词，已播种过的部署**不会**自动更新——要在应用管理里改一次，或删掉那一行让它重新播种。

### 可插拔部分放在哪里

| 东西 | 仓库里 | 部署后 | 怎么装 |
|---|---|---|---|
| 插件源码 | `plugins/<plugin-id>/` | `<data-root>/plugins/<plugin-id>/` | `install.sh` 复制 / 插件页上传 zip（`POST /api/plugins/install`） |
| 插件启用 | `App(plugins=(...))` 声明，或 DB `plugin_enable` | 同一份 DB | 应用声明的自动加载；其余在插件页勾选 |
| 技能 | `template/skills/<skill>/`（团队共享技能目录） | `$PI_TEMPLATE_AGENT_DIR/skills/`（用户目录软链过去） | `apps/<name>/install.sh --team` |
| 用户级技能 | — | `~/.pi/agent/skills/` 或项目 `.pi/skills/` | `install.sh` / `install.sh --project` |

## 生成物与不入库

| 路径 | 大小 | 文件数 | 重建方式 | 能删吗 |
|---|---|---|---|---|
| `.venv/` | 39M | 2080 | `uv sync` | 能，但要重新装依赖 |
| `office/node_modules/` | 606M | 71966 | `cd office && npm ci` | 能，office 开发/构建前要重装 |
| `office/xlsx-engine/target/` | 329M | 1097 | `scripts/build-xlsx-sidecar.sh`（cargo） | **别删**：`xlsx_sidecar.py` 直接调 `target/release/xlsx-sidecar` |
| `web/node_modules/` | 96M | 4262 | `cd web && npm ci` | 能，同样要重装 |
| `office-dist/` | 38M | 237 | `scripts/build-office.sh` | **别删**：FastAPI 从这里挂 `/office`，删了编辑器白屏 |
| `web/dist/` | 1.7M | 13 | `cd web && npm run build` | 能，服务端靠 `web/dist` 时删了就白屏 |
| `.env` / `airead.md` / `output/` | — | — | 自己建 | 本机产物，已 gitignore |

`.gitignore` 覆盖：Python 缓存、`.venv`、`node_modules`、`office-dist/`、`office/xlsx-engine/target/`、
`web/dist`、`.env`、`output/`、`airead.md`。

## 本次清理（2026-11-12）

| 删除项 | 为什么 | 怎么恢复 |
|---|---|---|
| `.DS_Store` ×3 | macOS 目录元数据 | 不用恢复 |
| `__pycache__/` ×22、`.pyc` ×145 | Python 字节码缓存 | 下次 import 自动生成 |
| `.pytest_cache/`、`.ruff_cache/` | 测试 / lint 缓存 | 下次跑 pytest / ruff 自动生成 |

## 判断"能不能删"的规则

1. **入库的源文件不要删**——`git ls-files` 里没有的，一律是生成物或本机文件；
2. **生成物要看有没有被运行时直接引用**：`office-dist/`（静态挂载 `/office`）和
   `office/xlsx-engine/target/release/xlsx-sidecar`（子进程调用）属于「生成但运行时要」，别删；
3. **缓存随便删**：`__pycache__`、`.pytest_cache`、`.ruff_cache`、`.DS_Store`；
4. **依赖目录**（`.venv`、`node_modules`）删了要重装，空间换时间自己权衡；
5. 删之前先 `git ls-files --error-unmatch <path>`：报错说明它没入库，删了 git 也救不回来。
"""


def build() -> str:
    return header() + "\n".join(walk(ROOT)) + "\n" + FOOTER


def main() -> None:
    TARGET.write_text(build(), encoding="utf-8")
    # 第二遍：树里要包含刚写出来的 docs/tree.md（否则它永远是"不存在"）。
    TARGET.write_text(build(), encoding="utf-8")
    print(f"写出 {TARGET.relative_to(ROOT)}：{len(TARGET.read_text().splitlines())} 行")


if __name__ == "__main__":
    main()

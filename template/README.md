# 默认 agent 模板

这个目录是 `PI_TEMPLATE_AGENT_DIR` 的仓库内版本：新用户的 agent 目录从它播种
（`ServerConfig._seed_agent_dir`），`skills/` 是**软链**而不是拷贝，所以一份技能
对所有租户生效。

```
template/
└── skills/               # 默认技能，每个子目录一个 skill（含 SKILL.md）
    ├── skill-creator/    # 怎么写一个新技能
    ├── plugin-creator/   # 怎么让 agent 按访谈生成一个可安装的插件
    └── documents/        # DOCX 创建/编辑/修订/批注 + 渲染核对
```

## 接线方式

`start.sh` 在启动时做一次幂等链接，把 `template/skills` 挂到实际的模板 agent 目录
（默认是 `$PI_AGENT_DIR`，团队模式是 `PI_TEMPLATE_AGENT_DIR`）：

```
<template agent dir>/skills -> <repo>/template/skills
```

新用户登录时，`ensure_user_layout()` 再把这一条链进
`<data_root>/agents/<uid>/skills`。已有的链不会被覆盖，方便本地临时替换成自己的版本。

等价于手工执行：

```bash
ln -sfn "$PWD/template/skills" .local/agent-litellm/skills
```

## 加一个默认技能

1. 建目录 `template/skills/<name>/SKILL.md`（`name` 必须匹配 `^[a-z0-9-]+$`）。
2. `description` 写成**字符串**，1–1024 字符。`description: [TODO: ...]` 会被 YAML
   解析成数组，加载器会直接跳过这个技能。
3. 验证：

```bash
.venv/bin/python template/skills/skill-creator/scripts/quick_validate.py template/skills/<name>
```

## 注意

- 这里的技能是**共享**的：团队模式下改一个技能等于改所有人的。用户私有的技能放在
  各自的 `<agent_dir>/skills/`，不要放进来。
- 会话加载器用 `include_home_skills=False`，所以只会看到模板里的技能，不会把服务器
  账号的 `~/.agents/skills` 塞进每次请求。
- 如果 `PI_NO_SKILLS=1`（或启动参数 `--no-skills`），技能完全不注入，这里的目录不会
  进 系统提示词。
- `documents` 是从外部 WorkBuddy/Codex 技能直接拷来的，仍带着几处宿主假设
  （容器依赖加载器、`mark_artifact_operation_started.mjs`、
  `mcp__codex_apps__google_drive_import_document`）。纯 OOXML/写作部分与宿主无关，
  可以直接用；涉及上述三处的段落需要按本仓库改。

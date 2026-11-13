# apps/ —— 应用的"非代码交付物"

这个目录**不是**应用的定义位置。应用（App）的定义在
`packages/pi-server/src/pi_server/apps.py`，是代码里的 `App(...)` 常量。

`apps/<name>/` 只放那些写不进代码、又必须跟着应用一起交付的东西：

```
apps/chart/
├── install.sh         把技能装进一个部署
└── README.md          这个应用怎么装、怎么配、验证过什么
```

技能**不在 apps/ 下**：所有技能都在仓库根的 `template/skills/<skill>/`，和内建模板放在一起。

所以这里只有 `chart/` 一个目录是正常的：另外 8 个内置应用（通用工作台、研究综述、
药品说明书、演示文稿、临床统计、标书与项目、市场情报、患者教育）没有专属技能包，
它们的定义、提示词、场景胶囊全部在 `apps.py` 里，不需要目录。

## 一个应用由哪几块组成

| 组成 | 放在哪 | 谁维护 |
|---|---|---|
| 应用定义（模式、提示词、胶囊、示例） | `packages/pi-server/src/pi_server/apps.py` | 代码，随版本发布 |
| 应用独占的插件 | `plugins/<plugin-id>/`，并在 `App(plugins=(...))` 里声明 | 代码 + 管理员安装 |
| 技能 | 仓库根 `template/skills/<skill>/` → `install.sh` 复制到部署的模板 agent 目录 | 技能文件（数据） |
| 安装 / 配置说明 | `apps/<name>/README.md` + `install.sh` | 文档 |

## 三个容易踩的点

1. **内置应用第一次被读到时写进数据库**（`ensure_apps()` → `seed_team_apps()`），之后
   team 模式读的是 `~/.pi/team/pi.db` 的 `team_apps` 表。
   `seed_team_apps` 只补缺失的 id，**不覆盖已存在的行**——所以改了 `apps.py` 里的提示词，
   现网不会自动更新，要在「应用管理」里改一次，或者删掉那一行让它重新播种。
2. **单机模式（`PI_AUTH_MODE=single`）没有应用管理页**，应用列表直接来自 `apps.py`，
   改代码就生效，也没有"导入/导出"。
3. **技能装在哪决定它会不会被读到**：服务端的模板目录是 `PI_TEMPLATE_AGENT_DIR`
   （`start.sh` 默认 `$PI_AGENT_DIR`，通常 `~/.pi/agent`），每个新用户的 `skills/`
   软链到它的 `skills/`。单机模式则读项目 `.pi/skills`。装错地方不报错，
   只是静默变成"没有技能"。

更完整的目录地图见 [`docs/tree.md`](../docs/tree.md)。

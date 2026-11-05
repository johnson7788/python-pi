# 工作台 M5（Python 插件 SDK）

> 2026-11-03。按“不做 DSH 兼容、用户用 python-pi 自己写插件”的决策落地。

## 已完成

### SDK

- 新增 `pi_coding_agent.plugins`：
  - `PluginAPI`（复用 ExtensionAPI 的注册面）；
  - `ToolResult`、事件类型等 re-export；
  - 插件入口 `setup(api)`，可注册工具、命令、事件。
- 插件目录：`<data_root>/plugins/<id>/`，包含 `plugin.json` + `main.py`；
  - `id` 小写字母/数字/连字符；
  - `entry` 必须在目录内且存在；
  - 同一 id 一个版本（不做多版本并存）。
- 示例：[examples/plugins/hello-pi](../../examples/plugins/hello-pi)
- 文档：[docs/PLUGINS.md](../PLUGINS.md)

### 加载与启用

- 新增 `plugins.py` 目录扫描与 manifest 校验；
- **DB v6**：`plugin_enable(uid, app_id, plugin_id, enabled)`，按用户/应用记录；
- `BootstrapOptions.plugin_paths`：把启用的插件入口加进 worker 的 builtin 扩展路径；
  - 平台治理扩展仍先加载，插件随后；
  - 未启用的插件不会 import。
- `SessionRegistry` 新增 `plugin_paths_lookup(uid, app_id)` 与 `evict(uid, app)`；
- 启停插件会重建该应用的 worker，下一条消息加载新工具集。

### API / 前端

- `GET /api/plugins?app=general`：已安装插件、版本、能力、当前启用状态、`canManage`；
- `PATCH /api/plugins/{id}/enable`：按应用启停；未知插件 404；
- 插件页：卡片、版本、能力标签、开关；空状态给出安装路径与示例；
- 单机模式可查看但不可持久化启停。

## 验证

- `npm run typecheck`、`npm run build` 通过；
- `tests/test_plugins.py`：目录扫描/坏 manifest、示例插件注册 `hello_plugin`、DB 启用、registry 注入路径；
- 非 Docker 全量：**623 passed**（含 4 个插件用例）；
- 手工 API：复制示例 → 列表 → 启用 → 列表已启用 → 未知插件 404；
- Playwright：2 个插件、开关切换、无 console 报错。

## 已知边界

- 安装 = 管理员把目录放到 `<data_root>/plugins`，没有上传/审核/签名；
- 插件与 server 同进程、同权限，只安装可信代码；崩溃/死循环仍可能影响服务；
- 插件工具没有命名空间前缀，作者需保证全局唯一；
- 没有热加载；启停通过 worker 重建生效，可能中断当前回合；
- 不支持插件贡献前端界面；
- `permissions` 只是声明，v1 不强制执行。

## 下一刀

1. 产物模型与预览/编辑（PPT/表格）；
2. 定时任务；
3. 应用/模式/胶囊配置编辑（从代码目录迁到租户表）。

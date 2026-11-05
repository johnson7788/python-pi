# 工作台 M4（应用 / 工作模式 / 场景胶囊 + Composition）

> 2026-11-03。应用层从“空状态”变成真实的 Composition：选中的应用/模式决定 worker 的系统提示词、技能绑定和会话恢复位置。

## 已完成

### 清理

- 删除旧的 `web/src/pages/Files.tsx`；
- 删除旧 `/files` 路由和旧 TopBar「文件」标签；
- 资料库只保留 `/workbench/files`（M3 的网格）。

### 应用目录

- 新增 `packages/pi-server/src/pi_server/apps.py`：
  - `App` / `Mode` / `Capsule` 三个声明式结构；
  - 内置 3 个应用：通用工作台（办公/开发/设计）、研究综述（综述）、演示文稿（制作）；
  - 每个模式带提示词、可选技能绑定和场景胶囊。
- `GET /api/apps` 返回目录、默认应用/模式。
- 当前是部署内置目录，不是租户可编辑表；下一阶段可平滑迁到 `apps` 表。

### Composition

- `BootstrapOptions` 新增 `app_prompt`、`app_skills`；
- `ResourceLoader` 支持 `skill_allowlist`（`None` = 全部技能）；
- `AgentSession` 新增 `extra_system_prompt`，在基础提示词之后追加应用提示词；工具/技能重建时不会丢；
- Worker key 从 `(uid, workspace)` 改为 `(uid, workspace, app, mode)`：
  - 不同应用/模式是不同 worker、不同系统提示词；
  - `/api/ws`、`/api/sessions`、`/api/state`、`/api/models` 接受 `app` / `mode` 查询参数；
  - 非法组合返回 404 / WS 4404。
- **DB v5**：`composition_session_pointers(uid, workspace, app, mode, session_file)`，迁移旧指针到 `general/office`；
  - 每个应用/模式有自己的恢复位置，切应用不会续到另一个应用的会话。

### 前端

- 应用页：卡片列出应用、模式、胶囊数量，可选中并进入；
- 首页：模式来自当前应用，快捷入口来自当前模式的场景胶囊，点击胶囊把完整 prompt 填入输入框；
- 会话头部显示「空间 · 应用 / 模式」；
- `useAgent` 增加 `appId` / `modeId`，WebSocket 和会话列表都带组合参数；
- 切应用/模式会重连到对应 worker，首页和后续消息使用新 Composition。

## 验证

- `npm run typecheck`、`npm run build` 通过；
- 非 Docker 全量：**619 passed**；sandbox 单元：26 passed；
- 手工 API：`GET /api/apps` 200 返回 3 个应用；`GET /api/sessions?app=missing&mode=missing` 404；
- Playwright：应用页 3 张卡片、切到研究综述后首页模式变为「综述」、无 console 报错。

## 已知边界

- 应用目录在代码里，租户不能编辑模式提示词/胶囊/技能绑定；
- 所有模式当前 `skillBinding=all`，还没有“只绑定某几个技能”的 UI；
- 会话列表仍是空间级，会显示该空间所有应用的会话；恢复指针按 composition 分开；
- 切应用/模式会重启 worker（预期），正在流式生成时应先结束当前回合；
- 每个应用/模式组合是一个 worker，频繁切换会占用 `max_active_users` 槽位，靠空闲回收；
- 胶囊只填入 prompt，不自动发送。

## 下一刀

按优先级：
1. Python 插件 SDK（manifest + 工具/命令 + 租户/应用启用）；
2. 产物模型与预览/编辑；
3. 定时任务；
4. 应用配置编辑（把内置目录迁到 DB）。

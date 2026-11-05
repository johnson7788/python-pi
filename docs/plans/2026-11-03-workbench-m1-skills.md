# 工作台 M1（技能）：注册表与启停

> 2026-11-03。承接 M0 并行新壳 `/workbench`。
> 这一刀把“技能”从只读命令列表变成可管理的真实注册表；工作空间留到下一刀。

## 已完成

### 后端

- **DB v3**：`skill_prefs(uid, workspace, name, enabled, updated_at)`，按用户/工作空间记录启停，缺省为启用。
- **技能发现 API**
  - `GET /api/skills`：返回当前用户的技能、描述、启用状态。
  - `PATCH /api/skills/{name}`：切换启用状态；未知技能 404；单机模式 400（无持久化）。
- **会话生效**
  - `ResourceLoader` 增加 `disabled_skills` 过滤；`GET /api/skills` 仍返回全部，便于再次打开。
  - `AgentSession.set_disabled_skills()` 重建系统提示词，**下一条消息立即生效**，不丢会话。
  - Worker 启动时从 DB 读取偏好，写入 `BootstrapOptions.disabled_skills`。
- **团队模式隔离**
  - 新增 `include_home_skills`：团队会话不再把服务器操作员的 `~/.agents/skills` 注入每个租户。
  - 团队共享技能仍来自部署模板 `template/skills`（登录时软链到每个用户的 `agent/skills`）。
  - 修复了实际测试中“技能页返回整台机器上百个技能”的问题。

### 前端

- 技能页改为调用 `/api/skills`，显示真实技能、启用数量、开关。
- `PATCH` 成功后本地即时更新；失败展示错误（单机模式会提示需要团队模式）。
- 保留 `disableModelInvocation` 标记（“仅手动”）。

### 测试

- 新增 `tests/test_skill_prefs.py`：loader 过滤、`include_home_skills`、DB 读写与按用户隔离。
- 全量测试：**633 passed**。
- 手工 API 验证：登录 → 写 SKILL.md → 列表 → 关闭 → 列表仍可见但 `enabled=false`。

## 已知边界

- 目前只覆盖 `workspace='default'`；工作空间接入后按空间隔离。
- 单机模式只读，不能持久化启停。
- 技能详情页、来源/版本、项目级信任状态尚未展示。
- 已打开的其他标签页不会自动刷新，提示用户刷新。

## 下一刀

M1 剩余：`workspaces` 表 + 默认空间 + 列表/创建 API + 会话按空间启动；随后资料库网格、App/Composition。

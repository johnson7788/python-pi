# 工作台 M13（应用变更审计 + 恢复默认）

> 2026-11-03。应用配置的修改开始留痕，内置应用支持一键恢复默认。

## 已完成

### 后端

- **DB v10**：`app_audit(id, uid, app_id, action, actor, before_json, after_json, created_at)`；
- 应用操作自动记录：
  - `create`：before=null，after=新应用；
  - `update`：before/after 完整快照；
  - `delete`：before=旧应用，after=null；
  - `restore`：恢复内置默认，before/after；
- API：
  - `GET /api/apps/{id}/audit`：最近 30 条（删除后仍可查）；
  - `POST /api/apps/{id}/restore`：仅内置应用；从代码内置目录恢复，写审计并重启 worker。
- 审计 actor 使用当前用户的 displayName/username。

### 前端

- 应用卡片底部新增「变更记录」「恢复默认」；
- 变更记录弹窗显示动作（创建/修改/删除/恢复默认）、操作者、时间；
- 恢复默认二次确认，成功后刷新应用列表；
- 自定义应用不显示“恢复默认”。

## 验证

- `npm run typecheck`、`npm run build` 通过；
- `tests/test_apps.py` 新增用例：
  - 修改内置应用 → 审计 update；
  - restore 后 prompt 回到默认，审计 restore/update；
  - 自定义应用 restore 400，删除后审计仍可查；
- 非 Docker 全量：**639 passed**；
- Playwright：3 个应用都有“变更记录”，弹窗 2 条记录，无 console 报错。

## 已知边界

- 审计只显示动作/操作者/时间，不展示 before/after diff；
- 不能回滚到任意历史版本，只能恢复代码内置默认；
- 审计没有分页/导出/保留策略；
- 非内置应用删除后无法恢复；
- 审计 actor 只是名称，没有角色/IP。

## 下一刀

技能/插件市场（浏览、来源、评分）。

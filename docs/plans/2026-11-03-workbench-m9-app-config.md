# 工作台 M9（应用配置编辑）

> 2026-11-03。内置应用目录迁到租户表，应用/模式/胶囊可以在工作台里编辑，Composition 从租户配置读取。

## 已完成

### 后端

- **DB v9**
  - `apps(uid, id, name, description, accent, builtin, ...)`；
  - `app_modes(uid, app_id, id, name, description, prompt, skills_json, sort)`；
  - `app_capsules(uid, app_id, mode_id, id, name, prompt, sort)`；
  - 首次访问为租户 seed 内置目录（通用工作台/研究综述/演示文稿），之后以 DB 为准。
- API：
  - `GET /api/apps`：当前租户应用 + `canManage`；
  - `POST /api/apps`：新建应用；
  - `PUT /api/apps/{id}`：整体替换应用（名称、描述、色、模式、提示词、技能绑定、胶囊）；
  - `DELETE /api/apps/{id}`：删除自定义应用；内置应用拒绝删除。
- Composition 改为租户级：
  - `SessionRegistry` 增加 `composition_lookup(uid, app, mode)`；
  - `resolve_app_mode(uid, ...)` 从租户表校验；
  - worker 的 `app_prompt` / `app_skills` 来自租户模式；
  - 更新应用会 `registry.evict(uid, app)`，下一条消息重建 worker。
- 内置应用 seed 仍来自 `apps.py`，但只是初始值；编辑后不会覆盖。

### 前端

- 应用页支持“新建应用”和“编辑”；
- 编辑弹窗：
  - 名称、描述、标识色；
  - 每个模式的名称、提示词、技能绑定（全部/指定技能）；
  - 场景胶囊增删与 prompt；
  - 增删模式；
  - 删除自定义应用；
- 保存后重新拉取应用列表；内置应用显示“内置”且不可删除。

## 验证

- `npm run typecheck`、`npm run build` 通过；
- 新增 `tests/test_apps.py` 两个用例：
  - 租户 seed 后编辑只影响自己；
  - HTTP 创建/修改/删除，内置应用删除 400；
- 非 Docker 全量：**634 passed**；
- Playwright：应用页 3 张卡片、打开编辑弹窗、修改办公提示词并保存，无 console 报错。

## 已知边界

- 内置应用不能删除，但可以编辑；没有“恢复默认”按钮；
- 没有应用版本/发布/回滚/审计；
- 每个模式只有一个 `skillBinding`，没有可视化技能多选（当前是逗号分隔文本）；
- 模式模型策略还没有（模型仍走全局默认）；
- 应用配置只存在 team 模式（单机使用代码内置目录）；
- 编辑会重启该应用 worker，正在流式生成时应先结束当前回合。

## 下一刀

产物结构化编辑（PPT/XLSX）或技能/插件市场；也可以补应用“恢复默认”和变更审计。

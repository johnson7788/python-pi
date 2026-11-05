# 工作台 M2（工作空间）

> 2026-11-03。M1 技能之后，补齐“空间”这条主线：每个空间有自己的目录、会话、技能偏好和 Agent worker。

## 已完成

### 后端

- **DB v4**
  - `workspaces(uid, id, name, path, is_default)`，主键 `(uid, id)`，同一用户内名称唯一；
  - `workspace_session_pointers(uid, workspace, session_file)`，迁移时把旧的单用户指针复制到 `default`。
- **目录布局**
  - `default` 仍使用原来的 `<data_root>/workspaces/<uid>`，已有文件与会话不迁移；
  - 命名空间是同级目录 `<data_root>/workspaces/<uid>-<workspace_id>`，**不嵌套在默认空间里**，避免默认空间的文件树看到其它空间；
  - workspace id 只允许 `[a-z0-9-]`，防止路径穿越。
- **API**
  - `GET /api/workspaces`：默认 + 命名空间，带 `canCreate`；
  - `POST /api/workspaces`：创建目录并落库，重名 409；
  - `PATCH /api/workspaces/{id}`：改名。
- **会话按空间隔离**
  - `SessionRegistry` 的 key 从 `uid` 改为 `(uid, workspace)`；
  - `options_for(uid, workspace)` 选目录，`agent_dir` 仍按用户共享（模型配置、设置不重复）；
  - `/api/ws?workspace=`、`/api/sessions?workspace=`、`/api/state?workspace=`、`/api/models?workspace=` 全部走对应 worker；
  - 文件 API 与技能 API 同样接受 `workspace` 查询参数；
  - 每个空间独立保存 `resume` 指针，关掉再回来能接上原会话；
  - 沙箱 thread 按空间区分（默认仍用原 `session` 槽，避免已有容器失效）。

### 前端

- `useAgent` 增加 `workspace` 选项：WebSocket、会话列表、重连都带空间；
- 工作台侧栏从 API 读取空间列表，默认空间 + 命名空间，可切换；
- “空间”标题右侧的 `+` 创建空间（团队模式显示；当前用原生 prompt 输入名称）；
- 首页 Composer、会话头部、技能页都会跟随当前空间；
- 切换空间会清空当前视图并重连，不会把 A 空间的会话显示在 B 空间。

## 验证

- `npm run typecheck`、`npm run build` 通过；
- 新增 `tests/test_workspaces.py`：路径、防穿越、DB 读写、按空间 resume、registry options；
- 非 Docker 全量：**615 passed**；sandbox 单元：**26 passed**；
- 手工 API：登录 → 列表 → 创建“研究空间” → 改名 → 文件树 200 → 未知空间 404；
- Playwright：三个空间渲染、切换到“研究空间”后 `/api/sessions?workspace=research` 发出、无 console 报错。

> 说明：`tests/test_sandbox_end_to_end.py` 的 4 个容器用例在本机 Docker 上启动超时，未计入上述结果；默认空间的 sandbox thread 未改动（仍为 `session`），命名空间才用 `ws-<id>`。

## 已知边界

- 旧“资料库”页面仍只读默认空间；资料库网格下一刀一起改；
- 没有删除空间（避免误删数据，后续做归档/确认）；
- 最大活跃数按 worker 数计算，一个用户开多个空间会占多个槽位；
- 创建空间用原生 prompt，后续换成工作台内弹窗；
- 空间列表不显示每个空间的任务数（只显示当前空间）。

## 下一刀

1. 资料库改成原型网格，并支持当前空间；
2. 应用（App / 工作模式 / 场景胶囊）+ Composition；
3. Python 插件 SDK；
4. 产物预览/编辑、定时任务、消息反馈。

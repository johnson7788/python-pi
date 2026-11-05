# 在线 Office 编辑接入计划（参考 AIOffice）

> 状态：**已审核**。P0 见 `2026-11-05-office-p0.md`；P1 进行中。
> 参考仓库：`/Users/admin/git/AIOffice`
> 目标：在 python-pi 工作台的产物面板里，直接在线编辑 `.docx` / `.xlsx` / `.pptx`，保存回当前工作空间，并保留版本/锁/回滚能力。

---

## 1. 结论与推荐

**不把 AIOffice 的编辑器直接合并进 `web/src`。**

推荐做成 **同源独立编辑器微前端**：

```
python-pi workbench (React 18 / Vite 5)
  └─ 产物面板
       └─ <iframe src="/office/docs?doc=<id>&embed=1">
            └─ AIOffice docs / slides / sheets (React 19, 独立构建)
                 ├─ docx-engine / pptx-engine / Univer
                 └─ web-adapter → /api/office/*
  python-pi FastAPI
       └─ workspace blob / 版本 / 单写锁 / 鉴权
```

原因：

1. AIOffice 编辑器是 **React 19 + Tiptap 3 + Univer + Vite 7**，当前工作台是 **React 18 + Vite 5**；直接合并会牵动整个前端依赖树。
2. AIOffice 有 5 个独立 app、Node shim、Rust xlsx sidecar；分开构建可以把复杂度关在 `/office` 里。
3. 同源 iframe 可以直接复用现有登录 cookie，不需要跨域 CORS。
4. 编辑引擎崩溃/卡死不会拖垮工作台主界面。

---

## 2. 边界与前置决策

### 2.1 在线编辑 ≠ 多人实时协同

AIOffice 是 **单写锁 + 版本历史**，不是 Yjs/CRDT 协同。它能做到：

- 一个用户在线打开、编辑、保存；
- 另一个用户打开时提示“有人正在编辑”；
- 保存后新版本可回滚。

它**不能**做到两个用户同时改同一段并实时合并。若必须多人实时协同，需要：

- OnlyOffice DocumentServer（产品化但重），或
- Yjs/Liveblocks + 自己实现 Office 协同模型（很难）。

**本计划按“单写锁 + 版本”设计。** 需要你确认是否接受。

### 2.2 格式顺序

建议：

1. **DOCX**（最高价值、docx-engine 最成熟）
2. **XLSX**（Univer + Rust sidecar，需要构建/部署 sidecar）
3. **PPTX**（自研 canvas，保真度和工作量最高，最后做）

Markdown/CSV 继续用现有编辑器，不接 AIOffice。

### 2.3 接入方式

采用 iframe 嵌入，而不是把编辑器组件内联。理由见 §1；代价是：

- 编辑器有独立构建产物和独立 UI 风格；
- 父页面/编辑器通过 `postMessage` 协调保存、关闭、全屏；
- iframe 同源意味着编辑器代码能访问父页面，必须只加载自己构建的 `/office`，不允许任意第三方 URL。

---

## 3. 目标架构

### 3.1 仓库布局

新增独立前端工程（不加入现有 `web` workspace）：

```
python-pi/
├─ office/                         # 从 AIOffice fork 的编辑器微前端
│  ├─ apps/docs/                   # 只保留编辑器，复用 Shell 以外部分
│  ├─ apps/slides/
│  ├─ apps/sheets/
│  ├─ packages/docx-engine/
│  ├─ packages/pptx-engine/
│  ├─ packages/pptx-render/
│  ├─ packages/file-parse/
│  ├─ src/embed/                   # 新增 embed 入口
│  │   ├─ main.tsx                 # 读 ?doc&ticket&embed，直接挂 App
│  │   └─ adapter.ts               # 指向 /api/office
│  └─ vite.config.ts
├─ packages/pi-server/
│  └─ src/pi_server/office.py      # blob/版本/锁 API
└─ web/src/workbench/
   └─ OfficeEditor.tsx             # iframe + postMessage
```

构建产物放到 `office/dist/`，由 FastAPI 以 `/office` 静态目录提供（或构建到 `web/dist/office`，随主前端发布）。

AIOffice 采用 Apache-2.0；fork 时保留 `LICENSE` / `NOTICE`，并在 `office/README.md` 说明来源和改动。

### 3.2 鉴权

- iframe 与主站**同源**，默认带 HttpOnly session cookie；
- 若后续需要 iframe sandbox，则父页面调用 `POST /api/office/ticket` 换一个 60 秒、单文档、单用户的 editor ticket，通过 URL fragment 传给 iframe，adapter 用 `X-Office-Ticket` 调 API；
- AIOffice 原 `web-adapter` 的 `aioffice_token` / Bearer 逻辑在 fork 中替换为 cookie/ticket；
- 不使用 AIOffice 的 `/auth`、Organization、JWT。

### 3.3 后端 API（新增）

不复制 AIOffice 整套 `documents` 模型；给编辑器一个窄接口：

```
GET    /api/office/documents/{doc_id}                元数据
GET    /api/office/documents/{doc_id}/blob           原始 bytes
PUT    /api/office/documents/{doc_id}/blob           保存（If-Match: sha256）
POST   /api/office/documents/{doc_id}/lock           获取/续租单写锁
DELETE /api/office/documents/{doc_id}/lock           释放
GET    /api/office/documents/{doc_id}/versions       版本列表
POST   /api/office/documents/{doc_id}/versions/{v}/restore
```

`doc_id` 设计：

- **不暴露绝对路径**；
- 用 `base64url(workspace 相对路径)`，或 DB `office_document` 表映射；
- 服务端用现有 `Workspace.path_for()` 做越界校验；
- 只允许当前租户 workspace 下的文件。

保存语义：

- `PUT` 校验 `If-Match` 的 sha，不匹配返回 409（防覆盖）；
- 写入前把旧文件备份到 `<workspace>/.pi/versions/<doc_id>/<timestamp>`（隐藏目录，不进入资料库）；
- 大小上限（建议 50MB）、只允许 `.docx/.xlsx/.pptx`；
- 原子写（临时文件 + `os.replace`）。

锁：

- DB 表 `office_locks(doc_id, uid, token, expires_at)`；
- TTL 建议 120 秒，编辑器定时续租；
- 其他用户打开时返回 409 + 当前持有人；前端提供“只读打开/强制接管（管理员）”。

版本：

- P1 可以先用文件备份，列表从目录读；
- P2 再迁到 DB `office_versions`，支持回滚和保留策略。

---

## 4. AIOffice 代码复用清单

| 路径 | 复用方式 |
|---|---|
| `frontend/packages/docx-engine` | 直接复用；DOCX 解析/序列化核心 |
| `frontend/apps/docs/src/renderer/App.tsx` | 整体挂载；不要拆成小组件 |
| `frontend/apps/docs/src/renderer/file-actions.ts` | 打开/保存/窄 patch 流程 |
| `frontend/apps/docs/src/renderer/web-adapter.ts` | fork 后改 API base、鉴权、文档 id |
| `frontend/packages/pptx-engine`、`pptx-render` | PPT 阶段复用 |
| `frontend/apps/slides` | PPT 阶段整体挂载 |
| `frontend/apps/sheets` + Univer | XLSX 阶段整体挂载 |
| `backend/app/xlsx_sidecar.py` + `xlsx-engine` | XLSX 只读/recalc sidecar，P2 接入 |
| `backend/app/routers/documents.py` | 只参考 blob/version/lock 语义，不搬模型 |

**暂不复用**：`project-store`、`agent-core`、`ai-provider`、AIOffice 登录/组织/首页。我们的 agent 和租户体系已经存在。

编辑器 fork 的必要改动：

1. 新增 `office/src/embed/main.tsx`，绕过 `Shell`：
   - 读 `doc`、`embed`、`ticket`；
   - 直接挂载 `<App />`，用 adapter 打开指定文档；
   - 隐藏 Home/Login/分享入口；
2. adapter：
   - `API_BASE = '/api/office'`；
   - `authFetch` 改 `credentials: 'same-origin'` + ticket；
   - `openDocxPath` → `/documents/{id}/blob`；`saveDocx` → `PUT`；
   - 不再调用 `/auth`、`/projects`；
3. 隐藏/禁用暂时不支持的 AI 面板（或后续桥到 python-pi 会话）；
4. 加 `postMessage`：
   - `{type:'office:ready', docId}`；
   - `{type:'office:saved', docId, sha}`；
   - `{type:'office:close'}`。

---

## 5. 工作台集成

### 5.1 产物面板

当前 `ArtifactPane` 对 `.docx/.xlsx/.pptx` 是下载/只读。改为：

- `.docx`：显示「在线编辑」按钮；点击打开右侧 iframe 或全屏编辑器；
- `.xlsx`、`.pptx`：P2/P3 接入；
- 保留「下载」作为兜底；
- 编辑中关闭时提示未保存；
- 保存成功后刷新产物列表和大小/时间。

### 5.2 路由

- `/office/docs?doc=<id>&embed=1`；
- `/office/slides?...`、`/office/sheets?...`；
- 生产由 FastAPI 静态挂载；开发用独立 Vite dev server（`:3590` 起）或主 Vite 代理。

### 5.3 与现有 Agent 的关系

P1 先不把编辑器 AI 面板接到 python-pi，避免同时改两条链路。编辑器只负责“打开/编辑/保存”。

P4 再考虑：

- 用 `postMessage` 把编辑器选区/文档摘要发给 python-pi；
- 或者把 AIOffice 的 `aiStream` 桥到 python-pi 的 WS/SSE；
- 需要单独评审，不阻塞 P1。

---

## 6. 分阶段计划

### P0：DOCX Spike（3–5 天）

- fork `apps/docs` + `packages/docx-engine` 到 `office/`；
- 增加 embed 入口和最小 adapter；
- 用一份真实 docx（表格、图片、页眉）做打开→编辑→保存→重开；
- 记录保真度问题（字体、分页、图片、批注、修订）；
- 产出：可运行的 `/office/docs?doc=...&embed=1`，不接工作台。

**验收**：真实 docx 往返保存不损坏；刷新后能重新打开；控制台无错误。

### P1：DOCX 产品化（1–2 周）

- `/api/office` 元数据/blob/If-Match/锁/版本备份；
- 产物面板 iframe + 保存/关闭/全屏 + `postMessage`；
- 登录 cookie / editor ticket；
- 并发：第二个标签页只读/409；
- 版本列表 + 回滚（先文件备份）；
- 测试：API 单测 + Playwright 打开/编辑/保存/重开 + 并发锁。

**验收**：用户从资料库/产物面板打开 docx，编辑保存，刷新可见；另一个用户打开提示占用；保存失败有明确错误。

### P2：XLSX（1–2 周）

- fork `apps/sheets` + Univer；
- 接入 `xlsx-engine` Rust sidecar（构建、镜像、进程管理、超时）；
- 支持打开/编辑/保存 xlsx，验证公式/多 sheet/样式；
- 无 sidecar 时降级为只读预览（现有 M11）。

**验收**：真实 xlsx 多 sheet、公式、数字格式往返；sidecar 崩溃可恢复；单容器资源可控。

### P3：PPTX（2–3 周）

- fork `apps/slides` + `pptx-engine`/`pptx-render`；
- canvas 编辑、缩略图、保存；
- 保真度测试（母版、图片、表格、字体）；
- 性能/内存上限。

**验收**：真实 pptx 打开/编辑/保存；复杂版式偏差有清单；大文件不崩。

### P4（可选）：编辑器 AI 桥接

- 选区/文档上下文 → python-pi；
- 编辑器内 AI 面板隐藏或替换；
- 单独评审。

---

## 7. 测试计划

### 后端

- `tests/test_office_api.py`：
  - 路径越界 404/403；
  - 非 office 扩展名 422；
  - GET/PUT blob roundtrip；
  - `If-Match` 冲突 409；
  - 锁：获取/续租/过期/他人 409/释放；
  - 版本备份/恢复；
  - 跨租户不可见。
- 大文件 413、并发写。

### 前端

- `office` 构建 typecheck；
- Playwright：
  - 从产物面板打开 docx；
  - 修改文字/表格；
  - 保存，父页面收到 `office:saved`；
  - 重新打开看到修改；
  - 第二个上下文只读/409；
  - 关闭未保存提示。

### 保真度

- 固定 fixtures：带表格/图片/页眉页脚/批注/修订的 docx；多 sheet/公式 xlsx；母版/图片 pptx；
- 每次保存后记录 diff（字节级）和人工检查清单。

---

## 8. 工作量与风险

| 阶段 | 估算 |
|---|---|
| P0 DOCX spike | 3–5 天 |
| P1 DOCX 产品化 | 1–2 周 |
| P2 XLSX | 1–2 周 |
| P3 PPTX | 2–3 周 |
| P4 AI 桥接 | 另评 |

总计 DOCX 可用约 **2–3 周**；三格式全量约 **5–8 周**（单人）。

主要风险：

1. **保真度**：自研引擎对复杂 Word/PPT 的支持有限；需要真实文档清单逐项验收。
2. **依赖冲突**：必须保持 `office/` 独立构建，不要合并到 `web`。
3. **XLSX sidecar**：Rust 二进制构建、容器镜像、进程生命周期、超时/崩溃恢复。
4. **iframe 安全**：同源 iframe 可访问父页面；只允许自家 `/office`，加 CSP，不接受外链。
5. **许可**：AIOffice Apache-2.0，保留 NOTICE。
6. **协同预期**：单写锁不是实时协同，需要产品明确说明。

---

## 9. 需要你确认的问题

1. **只要单人编辑 + 锁，还是必须多人实时协同？**（后者不建议走 AIOffice）
2. 格式优先级是否按 **DOCX → XLSX → PPTX**？
3. 是否接受 **iframe 独立 `/office`**，而不是把编辑器组件内联到工作台？
4. XLSX 是否允许引入 **Rust sidecar**（会增加构建/运维）？
5. 编辑器里的 AI 面板 P1 先隐藏，是否可以？
6. 保真度要求：能接受“常用文档可用，复杂版式逐项修”，还是必须 Office 级保真（那就应评估 OnlyOffice）？

---

## 10. 审核后第一步

先做 **P0 DOCX spike**，不改现有产物编辑逻辑：

- 建 `office/`，fork docs + docx-engine；
- embed 入口 + 最小 adapter；
- 拿 3 份真实 docx 做往返测试；
- 给出保真度报告和 P1 是否继续的建议。

P0 完成并验收后，再决定是否进入 `/api/office` + 产物面板接入。

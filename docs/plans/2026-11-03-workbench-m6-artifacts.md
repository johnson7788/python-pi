# 工作台 M6（产物面板）

> 2026-11-03。会话右侧的产物面板先做“可编辑的文本/Markdown/CSV + 图片预览 + 幻灯片只读下载”，不假装在线编辑 PPT。

## 已完成

### 后端

- `GET /api/artifacts?workspace=`：递归列出当前空间内的产物文件；
  - 支持 md/mdx/txt/html/pdf、csv/tsv/xls(x)、ppt(x)、json/yaml、图片等；
  - 按修改时间倒序，最多 40 个；
  - 返回 `path/name/kind/extension/size/modified/editable`；
  - 复用 M2 的 `workspace_for()`，因此按空间隔离。
- 产物内容读写复用现有 `/api/workspace/file`、`/api/workspace/download`，没有新增存储层。

### 前端

- 会话头部新增「产物」开关，右侧打开产物面板；
- 产物列表显示类型图标、名称、类型；
- 文本/Markdown/CSV/JSON 可直接编辑并保存（`PUT /api/workspace/file`，`{content}`）；
- 图片用同源下载 URL 直接预览；
- 幻灯片/PDF 等二进制产物显示“下载文件”，不伪装成可编辑；
- 有未保存修改提示，保存后刷新列表。

### 验证

- `npm run typecheck`、`npm run build` 通过；
- 新增 `tests/test_artifacts.py`：过滤与 `editable`、按命名空间隔离；
- 非 Docker 全量：**625 passed**；
- Playwright：4 个产物、打开 notes.md 编辑、切到 data.csv 修改并保存、pptx 进入只读下载态，无 console 报错。

## 已知边界

- 产物是“当前空间内的文件”，不是按会话/消息精确归属；
- 没有产物数据库、版本历史、来源标注；
- CSV 是原文编辑，不是表格网格；
- PPT/XLSX 没有结构化解析或在线编辑（需要 python-pptx / OnlyOffice 等）；
- 图片只预览，不裁剪/替换；
- 保存不做冲突检测（多人同时编辑会覆盖）。

## 下一刀

定时任务：cron/间隔/一次性、运行历史、按租户配额与通知。

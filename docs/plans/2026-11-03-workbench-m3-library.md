# 工作台 M3（资料库网格）

> 2026-11-03。把 M0 里临时复用的旧 Files 页面替换成工作台自己的资料库，按当前空间读取。

## 已完成

- **按空间读取**：所有请求都带 `workspace=<id>`，文件 API 复用 M2 的 `workspace_for()`。
- **网格 / 列表**：目录优先排序，卡片显示图标、名称、大小/“目录”；可切换视图。
- **目录导航**：面包屑进入子目录、回到根目录。
- **预览**：点击文件读取 `/api/workspace/file`，右侧面板显示文本；二进制/超大文件给出明确提示。
- **上传**：选择文件后 `PUT /api/workspace/file?path=...&workspace=...`，刷新列表。
- **新建目录**：`POST /api/workspace/directory`，当前用原生 prompt 输入名称。
- **下载**：预览面板提供 `/api/workspace/download` 链接，带空间参数。
- 旧 `/files` 页面仍保留，旧路由不受影响。

实现：[Workbench.tsx](web/src/workbench/Workbench.tsx)、[workbench.css](web/src/workbench/workbench.css)。

## 验证

- `npm run typecheck`、`npm run build` 通过；
- Playwright（mock 数据）：5 个条目渲染、点击 `notes.md` 右侧预览、无 console 报错；
- 文件 API 本身已由 M2 的 workspace 测试与手工 API 覆盖（`tree` 200 / 未知空间 404）。

## 已知边界

- 只预览 UTF-8 文本；图片/PPT/PDF 暂不渲染缩略图；
- 没有重命名/删除/移动；
- 没有拖拽上传和分片上传；
- 新建目录用原生 prompt，后续换成工作台弹窗；
- 列表没有搜索/排序控件。

## 下一刀

应用（App / 工作模式 / 场景胶囊）+ Composition：
1. apps / app_modes / app_capsules 表与 API；
2. 应用绑定提示词与技能白名单；
3. 选择应用/模式后按会话冻结 Composition；
4. 前端应用页与首页模式联动。

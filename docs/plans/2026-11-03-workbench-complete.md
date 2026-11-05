# 工作台改造完成报告

> 原型：`docs/prototype-workbuddy.html`
> 方式：并行新壳 `/workbench`，旧界面逐步迁移；当前核心工作台已完成。
> 最终测试：**666 passed**（含 22 个 Docker sandbox 用例），前端 typecheck/build 通过。

## 里程碑

| 阶段 | 内容 |
|---|---|
| M0 | `/workbench` 并行壳：侧栏、首页、会话、真实发送/流式、技能只读 |
| M1 | 技能注册表：列表/启停/按空间，团队不再注入操作员 `~/.agents/skills` |
| M2 | 工作空间：DB、API、按空间 worker/会话/技能/文件、空间切换 |
| M3 | 资料库网格：目录、预览、上传、新建目录、下载 |
| M4 | 应用/模式/胶囊：内置目录、Composition、worker 按 app/mode 隔离 |
| M5 | Python 插件 SDK：manifest、目录扫描、按应用启用、示例插件 |
| M6 | 产物面板：文本/Markdown/CSV 编辑、图片预览、PPT 只读 |
| M7 | 定时任务：一次性/间隔/每天/每周、历史、手动运行 |
| M8 | 通知中心：定时任务结果、未读数、已读 |
| M9 | 应用配置编辑：租户表、模式提示词、技能绑定、胶囊 |
| M10 | 产物结构化：CSV 网格、Markdown 预览、PPTX 文本提取 |
| M11 | XLSX 只读预览：工作表 tabs、共享字符串/内联字符串 |
| M12 | 插件 zip 上传安装：管理员、路径校验、覆盖回滚 |
| M13 | 应用变更审计 + 恢复内置默认 |
| M14 | 技能/插件市场：来源、评分、安装 |
| M15 | 市场搜索 + 详情（SKILL/README） |
| M16 | 插件卸载：删除目录/启用记录、evict 已加载 worker |
| M17 | 市场标签筛选 + 排序 |
| M18 | 消息反馈：👍/👎 按会话/消息持久化 |

## 最终架构

- **多租户**：每用户 workspace/agent dir；worker key 为 `(uid, workspace, app, mode)`。
- **配置**：应用/模式/胶囊在租户 DB；技能启停、插件启停、评分、反馈都在 DB。
- **扩展点**：Python 插件复用 ExtensionAPI，按应用挂载；未启用不 import。
- **任务**：定时任务复用普通 worker 的无头 prompt，结果写通知。
- **产物**：文件系统为唯一来源；文本可编辑，CSV 表格，PPTX/XLSX 只读解析。
- **旧 `/files` 页面与路由已删除**；资料库在 `/workbench/files`。

## 验证

```bash
cd web && npm run typecheck && npm run build
.venv/bin/python -m pytest -q
```

- 完整套件：**666 passed**，2 warnings（Starlette/httpx 弃用提示）；
- 前端生产构建成功；
- 关键端到端：mock 后端 + Playwright 覆盖首页/会话/应用/技能/插件/资料库/产物/定时任务/通知/市场/反馈。

## 明确不做/仍欠缺

- PPT 在线编辑与版式/图片预览（当前只提取文本；XLSX 只读）；
- 应用变更 diff/回滚（按你的要求暂不做，只保留审计列表）；
- 插件多版本并存（决策为同一 id 一个版本）；
- MCP/CLI Connector、WorkBuddy Expert 包、消息附件上传、Web 搜索等早期分析中的能力；
- 应用市场/技能远程 registry，当前市场目录是部署内置；
- 插件与 server 同进程，仍只适合可信内部代码。

## 演示视频

- 视频索引：[docs/videos/index.html](../videos/index.html)
- 视频清单：[docs/videos/README.md](../videos/README.md)
- 17 个功能短片，1440×900，无声 + 屏幕字幕；录屏使用 mock 后端，真实后端由 666 个测试覆盖。

## 相关文档

- 隔离设计：`docs/plans/2026-11-02-multi-tenant-app-isolation.md`
- 插件运行时：`docs/plans/2026-11-03-python-plugin-runtime.md`
- 各阶段：`docs/plans/2026-11-03-workbench-m0.md` … `m18-feedback.md`
- 插件开发：`docs/PLUGINS.md`
- 示例：`examples/plugins/hello-pi`

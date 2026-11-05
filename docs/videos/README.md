# 工作台功能演示视频

录制时间：2026-11-05
录制方式：Playwright 驱动 1440×900 Chromium，mock 后端 + Vite；无声，关键步骤以屏幕顶部字幕说明。

> 视频用于逐项检查交互；18 号使用真实 LiteLLM 模型，其余使用 mock 数据。真实后端已由完整测试套件覆盖（`666 passed`，含 Docker sandbox）。

| # | 视频 | 内容 | 时长 |
|---|---|---|---|
| 01 | [01-home-chat.mp4](01-home-chat.mp4) | 首页、模式/胶囊、发送消息、流式回复、工具卡片、反馈按钮 | 13s |
| 02 | [02-workspaces.mp4](02-workspaces.mp4) | 工作空间切换、新建空间 | 11s |
| 03 | [03-skills.mp4](03-skills.mp4) | 已安装技能、启停 | 7s |
| 04 | [04-skill-market.mp4](04-skill-market.mp4) | 技能市场、搜索、详情、评分、安装 | 12s |
| 05 | [05-app-editor.mp4](05-app-editor.mp4) | 应用编辑：模式提示词、技能绑定、胶囊 | 9s |
| 06 | [06-app-audit.mp4](06-app-audit.mp4) | 应用变更记录、恢复默认 | 8s |
| 07 | [07-plugins.mp4](07-plugins.mp4) | 插件列表、启用/停用、卸载 | 7s |
| 08 | [08-plugin-market.mp4](08-plugin-market.mp4) | 插件市场、详情、评分 | 9s |
| 09 | [09-library.mp4](09-library.mp4) | 资料库目录、文件预览 | 9s |
| 10 | [10-artifacts-table.mp4](10-artifacts-table.mp4) | 产物面板、CSV 表格编辑、Markdown 预览 | 11s |
| 11 | [11-artifacts-office.mp4](11-artifacts-office.mp4) | PPTX 只读文本、XLSX 工作表 | 9s |
| 12 | [12-schedules-notify.mp4](12-schedules-notify.mp4) | 新建定时任务、历史、通知中心 | 13s |
| 13 | [13-feedback.mp4](13-feedback.mp4) | 消息 👍/👎 反馈 | 10s |
| 14 | [14-plugin-upload.mp4](14-plugin-upload.mp4) | 管理员上传 zip 安装插件 | 6s |
| 15 | [15-app-create.mp4](15-app-create.mp4) | 新建应用 | 7s |
| 16 | [16-schedule-run.mp4](16-schedule-run.mp4) | 立即运行、运行历史 | 7s |
| 17 | [17-home-modes.mp4](17-home-modes.mp4) | 办公/开发/设计模式切换 | 4s |
| 18 | [18-real-model.mp4](18-real-model.mp4) | **真实模型** LiteLLM / deepseek-v4-flash 的回复 | 9s |
| 19 | [19-collapse-sections.mp4](19-collapse-sections.mp4) | 点击“任务/空间”标题折叠/展开列表 | 8s |

也可以打开 [index.html](index.html) 连续播放。

## 说明

- 演示数据来自 `web/mock-server.mjs`，不是生产数据；聊天回复是固定脚本。
- 真实后端的登录、租户隔离、定时调度、插件安装等由 `666 passed` 的测试覆盖。
- 视频不含语音讲解；字幕标注了当前功能和操作。

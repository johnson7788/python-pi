# 工作台 M8（通知中心）

> 2026-11-03。定时任务完成后不再“静默”：结果进入通知中心，侧栏铃铛显示未读数。

## 已完成

### 后端

- **DB v8**：`notifications(id, uid, kind, title, body, link, read, created_at)`；
- 通知 API：
  - `GET /api/notifications`：最近 50 条 + `unread`；
  - `POST /api/notifications/{id}/read`；
  - `POST /api/notifications/read-all`。
- 调度器在任务结束时写通知：
  - 成功：`schedule_success`，「定时任务完成：name」，正文「已生成新会话」；
  - 失败：`schedule_failed`，正文为错误；
  - link 指向 `/workbench/scheduled`，不暴露服务器绝对路径。

### 前端

- 侧栏铃铛显示未读数（>9 显示 9+）；
- 点击打开通知面板，列出标题、正文、时间；
- 单条点击标记已读并跳转到对应页面；
- 全部已读；
- 每 30 秒轮询一次；单机/接口不可用时静默隐藏。

## 验证

- `npm run typecheck`、`npm run build` 通过；
- `tests/test_notifications.py`：DB 新建/未读/单条已读/全部已读；HTTP 列表/已读；
- 调度器测试断言成功后生成通知；
- Playwright：铃铛未读数 2、面板 2 条、点击后跳转 `/workbench/scheduled`、未读数下降；
- 非 Docker 全量：**632 passed**。

## 已知边界

- 只有定时任务事件，技能/插件/额度告警还没有；
- 没有邮件/IM/浏览器推送；
- 轮询 30 秒，不是 WebSocket 实时推送；
- 通知链接只到定时任务页，不能直接打开对应会话；
- 通知面板是侧栏浮层，会遮挡空间列表；
- 单机模式无账号，通知 API 不可用。

## 下一刀

应用/模式/胶囊配置编辑：把内置目录迁到租户表，支持模式提示词、技能绑定和胶囊编辑。

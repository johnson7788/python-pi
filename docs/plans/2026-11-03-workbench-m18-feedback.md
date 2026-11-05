# 工作台 M18（消息反馈）

> 2026-11-03。助手消息支持“有帮助/没帮助”，按用户、会话、消息时间戳持久化。

## 已完成

### 后端

- **DB v12**：`feedback(uid, session_file, message_timestamp, rating, comment, ...)`，唯一键 `(uid, session_file, timestamp)`；
- API：
  - `POST /api/feedback`：rating 只允许 `1` / `-1`，同一消息重复提交会覆盖；
  - `GET /api/feedback?session=`：返回该会话的反馈；
  - 会话文件必须位于当前用户的 agent 目录内，否则 403。
- 单机模式无账号，反馈 API 不可用（503）。

### 前端

- 助手消息底部显示 👍 / 👎；
- 点击后提交并显示“已反馈”；
- 打开会话时拉取已有反馈并回显；
- 没有 session file 时不显示。

## 验证

- `npm run typecheck`、`npm run build` 通过；
- `tests/test_feedback.py`：提交/覆盖、非法 rating 422、`/etc/passwd` 403；
- 非 Docker 全量：见最终报告；
- Playwright：助手消息出现 2 个反馈按钮，点击后显示“已反馈”，无 console 报错。

## 边界

- 只有二元评价，没有评论输入/原因标签；
- 不进入用量/审计，也没有管理端汇总；
- 反馈以消息时间戳为键；同一时间戳的消息极少见但理论上会冲突；
- 不校验消息是否真实存在（只校验会话路径）。

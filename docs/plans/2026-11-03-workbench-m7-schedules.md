# 工作台 M7（定时任务）

> 2026-11-03。定时任务从占位页变成可运行：一次性/间隔/每天/每周，按空间+应用+模式执行，
> 记录运行历史，支持手动触发。

## 已完成

### 后端

- **DB v7**
  - `schedules`：uid、workspace、app_id、mode_id、name、prompt、kind、run_at、hour/minute/weekday、interval_minutes、enabled、next_run_at、last_*；
  - `schedule_runs`：每次执行的开始/结束、状态、session file、错误。
- `schedules.py`：
  - `next_run_at()`：once / interval / daily / weekly（服务器本地时区 → UTC 存储）；
  - `ScheduleRunner`：20 秒 tick，取到期任务，标记 running，派发；
  - 执行复用正常 worker：`registry.acquire(uid, workspace, app, mode)` → `worker.run_prompt(prompt)`；
  - 成功/失败写 `schedule_runs`，并计算下一次；一次性成功后自动停用；
  - worker 忙时本次失败，非一次性任务按频率顺延。
- `RpcMode.wait_for_prompt()`：无头调用可以等一轮 turn 真正结束；
- `SessionWorker.run_prompt()`：给调度器用的 headless prompt。
- API：
  - `GET/POST /api/schedules`；
  - `PATCH /api/schedules/{id}`：启停；
  - `DELETE /api/schedules/{id}`；
  - `POST /api/schedules/{id}/run`：立即运行；
  - `GET /api/schedules/{id}/runs`：运行历史。
- 调度器随 FastAPI lifespan 启动/停止。

### 前端

- 定时任务页：列表、状态、下次运行、启停、立即运行、历史、删除；
- 新建表单：名称、空间、应用、模式、频率、时间/间隔、prompt；
- 运行历史展开显示时间、状态、错误。

## 验证

- `npm run typecheck`、`npm run build` 通过；
- `tests/test_schedules.py`：
  - once/interval/daily/weekly 的下一次时间；
  - DB due/mark running；
  - Fake worker 的无头执行与 run 记录；
  - HTTP 创建/列表/停用/删除/非法 kind 422；
- 非 Docker 全量：**630 passed**；
- Playwright：2 个任务、打开表单、展开历史，无 console 报错。

## 已知边界

- 只有 once/interval/daily/weekly，还没有原始 5 段 cron 表达式；
- daily/weekly 按服务器本地时区解释，不按每个任务单独配置时区；
- 调度器在进程内，重启后从 `next_run_at` 继续；停机期间错过的任务不会补跑；
- worker 忙时本次失败（不排队），非一次性任务等下一个周期；
- 没有通知（邮件/IM/站内红点只是标记）；
- 无头执行遇到需要确认的扩展对话框会等待/超时；
- 单机模式没有账号，定时任务 API 不可用。

## 下一刀

1. 应用/模式/胶囊配置编辑（从代码目录迁到租户表）；
2. 产物结构化预览（PPT/XLSX）或表格网格；
3. 通知与运行告警。

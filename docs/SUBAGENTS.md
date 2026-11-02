# 后台 subagent（spawn / fork）

本移植版新增的一等能力：模型可以把一个任务**委派给后台子 agent**，自己继续干活，子 agent
完成后结果以一条通知回到父对话。上游 pi 没有这个能力；设计参照 DeepSeek Harness 的 subagent
seam，但只实现两个进程内后端。

- 代码：`packages/pi-coding-agent/src/pi_coding_agent/subagents/`
- 工具名：`subagent`（spawn）、`subagent_fork`（fork）
- 测试：`tests/test_subagents.py`（离线集成）、`tests/test_subagent_end_to_end.py`（真实 CLI + HTTP provider）

## 1. 两个后端

| 后端 | 子 agent 看到什么 | 适合 |
|---|---|---|
| `spawn` | **空对话**。任务 prompt 必须自洽 | 独立、可并行、互不依赖的子任务 |
| `fork` | 父对话**已完成轮次**的快照（不含正在跑的这一轮） | 评审、深挖、续写等需要现有上下文的子任务 |

fork 的种子边界是一条硬规则：委派发生在父的**开放轮次**里，所以种子截到"最后一条 user
消息之前"——本轮的 prompt、工具调用、半截工具结果都不会被复制。种子是一次性快照，之后父的
新轮次不会进入子 agent。

## 2. 一次委派发生了什么

```
父模型调用 subagent(prompt=...)
  → 立刻返回 "started subagent <id>"        （工具调用结束，父继续）
  → manager 建一个子 Session（JSONL，header.parentSession = 父 session id）
  → 子 agent 用同一模型/思考级别/工具跑任务
  → 结束时向父对话注入一条 custom 通知（含结果、用量、子 session 路径）
  → 父若在流式中：通知进 steering 队列，本轮边界被消费
    父若已空闲：通知落盘后自动续一轮（continue_turn），模型据此汇报
```

关键取舍：

- **工具不阻塞**。schema 里没有 `wait` 开关——否则父会被慢子任务串行化，并行委派就没有意义。
- **子 agent 是真正的 Session**，不是旁路通道。它有独立的 JSONL 文件，会被 Web 会话列表列出，
  父对话结束后仍可打开检查。
- **通知是持久化的 custom message**（`customType: "subagent"`），因此重启、重放、导出都不会丢。

## 3. 配置

settings 的 `subagent` 段（全局或项目作用域）：

```jsonc
{
  "subagent": {
    "maxDepth": 1,             // 允许的委派层数；0 = 完全禁止，1 = 只允许直接子 agent
    "maxActiveSubagents": 8,   // 同时存活的子 agent 上限
    "autoContinue": true       // 父空闲时，收到通知是否自动续一轮
  }
}
```

上限是**快速失败**而不是排队：超深度返回 `SUBAGENT_DEPTH_LIMIT`，超并发返回
`SUBAGENT_CAPACITY`。理由与 DSH 一致——正在等子 agent 的父不能去等自己占用的槽位。

拒绝都带稳定错误码，工具的 `isError` 为真：

| 代码 | 含义 |
|---|---|
| `SUBAGENT_DEPTH_LIMIT` | 已到 `maxDepth`，要求直接完成任务 |
| `SUBAGENT_CAPACITY` | `maxActiveSubagents` 已满 |
| `SUBAGENT_EMPTY_PROMPT` | prompt 为空 |
| `SUBAGENT_UNAVAILABLE` | 没有配置子 Session 工厂（只在使用 `SubagentManager` SDK 时可能出现） |

## 4. 生命周期与取消

- `AgentSession.dispose()` 会取消它名下所有子 agent（含孙 agent），并等待清理。
- `Bootstrapped.shutdown()` 调用 `SubagentManager.dispose_all()`，服务端 worker 被回收时不会留下
  跑着的子任务。
- 服务端 `SessionWorker.busy` 把"有子 agent 在跑"算作忙，空闲淘汰不会杀掉正在工作的子 agent。
- one-shot 模式（`--mode print` / `--mode json`）在返回前调用
  `session.wait_for_background_work()`，所以 CLI 不会在子 agent 完成前退出。

## 5. 在代码里直接用

```python
from pi_coding_agent.subagents import SubagentManager

manager = SubagentManager(
    child_factory,              # async (session_manager, depth, parent) -> AgentSession
    max_depth=1,
    max_active=8,
    auto_continue=False,        # 测试里通常关掉，避免多跑一轮
)
manager.attach(parent_session, depth=0)   # 加工具 + 生命周期钩子

record = await manager.start("spawn", parent_session, 0, "compute 6*7", label="math")
await manager.wait_all()
print(record.result.status, record.result.text, record.result.session_file)
```

`bootstrap()` 已经替你接好：每个 bootstrapped 树一个 manager，子 Session 与父共用
agent_dir / sessions 目录 / 凭据 / 设置 / 资源加载器 / 治理扩展。

## 6. 已知限制（刻意留到下一阶段）

- **只有进程内后端**。没有 DSH 的 `dsh-sdk` / ACP / Claude Code / Codex 后端；跨进程隔离要另做。
- **子 agent 不能续聊**。没有 `send_message` / 冷恢复；每个委派是一次性执行。DSH 的本地子 agent
  支持续聊，这里留到控制面（`list_agents` / `send_message` / `interrupt_agent`）一起做。
- **没有按调用选模型**。子 agent 继承父的模型与思考级别；`list_subagent_models` 之类的路由面未实现。
- **子 agent 的 token 不计入团队用量账本**。结果里的 usage 会写进通知与 `record.result.usage`，
  但服务端按用户记账的链路目前只观察根 session 的 `message_end`。这是接入计费前必须补的一环。
- **共享扩展 runner**。子 agent 与父共用同一个扩展 host，因此治理策略（policy）对子 agent 生效，
  但扩展看到的 `ctx.session` 仍指向父会话。
- **工具集是默认内置集 + 扩展工具 + 委派工具**。父如果通过 `set_active_tools` 临时启用了
  `grep`/`find`/`ls`，子 agent 不会继承这份临时选择（它按默认工具集构造，再由 manager 追加委派工具）。
- **子会话在会话列表里是平铺的**。它以 label 命名、header 带 `parentSession`，但 Web 侧栏还没有
  父子树；做控制面时一起补。

## 7. 验证方式

```bash
uv run pytest tests/test_subagents.py -q          # 9 个离线用例
uv run pytest tests/test_subagent_end_to_end.py -q # 3 个真实进程用例（CLI print / CLI rpc / Web 服务端）
```

离线用例覆盖：spawn 的子会话与通知落盘、fork 的种子边界（含"不含本轮"）、深度拒绝、并发拒绝、
失败子 agent 的结算、父 dispose 取消子 agent、提示词可见性。

端到端用例覆盖：真实子进程 + 真实 OpenAI wire 格式 + 真实子 Session 文件。
spawn 走 CLI print 模式，验证最终回答、父子 `parentSession` 关联与通知落盘；
fork 走 RPC 模式先完成一轮，再验证子 agent 的首个请求确实带着"已完成的第一轮"、
且消息序列严格是 `system/user/assistant/user`（没有本轮的工具调用混进种子）；
第三条走真实 Web 服务端 + WebSocket，验证通知事件被推送到标签页（而不是只落盘）。

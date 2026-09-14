# 02 — pi `packages/agent` 深读研究笔记（Python 移植用）

> 目标仓：`/tmp/pi-ref`（pi by Earendil），包：`@earendil-works/pi-agent-core@0.85.1`。
> 说明：`pi.dev/docs/latest` 只覆盖 *coding-agent* CLI 的使用面（SDK / RPC / session format / compaction），**没有** agent-core 内部文档（`https://pi.dev/docs/latest/agent-core` 是 404）。因此本笔记一律以 `/tmp/pi-ref/packages/agent/src/**` 源码为准，并引用 `packages/agent/README.md`、`packages/agent/docs/harness.md`、`packages/agent/test/agent-loop.test.ts` 作为佐证。

---

## 0. 地图：这个包里到底有什么

### 0.1 包形态与对外入口

```jsonc
// packages/agent/package.json
"name": "@earendil-works/pi-agent-core",
"version": "0.85.1",
"type": "module",
"dependencies": {
  "@earendil-works/chord": "^0.85.1",        // Context / ContextKey / JsonRepresentation
  "@earendil-works/pi-ai": "^0.85.1",        // 消息类型、Model、Models、EventStream、validateToolArguments
  "@earendil-works/pi-telemetry": "^0.85.1", // telemetry schema helper
  "typebox": "1.3.27",                       // 工具参数 JSON Schema
  "yaml": "2.9.0", "diff": "8.0.4", "ignore": "7.0.8"
}
```

`exports` 暴露 6 个子入口，移植时要注意**公开面比 `index.ts` 更窄**：

| specifier | 文件 | 内容 |
|---|---|---|
| `.` | `dist/index.js` | 全部核心 + harness 门面（见下） |
| `./node` | `src/node.ts`（2 行，仅 re-export nodejs env） | Node 执行环境 |
| `./harness/context` | `harness/context.ts` | `Context` 及派生 helper |
| `./harness/env/nodejs` | `harness/env/nodejs.ts` | `NodeExecutionEnv` |
| `./harness/runtime/reducer` | `harness/runtime/reducer.ts` | `reduceLaneSnapshot` |
| `./harness/session`、`./harness/session/testing` | session 子系统（含 conformance 测试工具） | 存储后端契约 |

### 0.2 分层（关键判断：core 与 harness 完全解耦）

```
L2  host: packages/coding-agent, server, client
      ├─ src/core/sdk.ts            → new Agent({...})            （简单路径）
      └─ src/experimental/session-worker.ts → AgentHarness.create()（生产路径）
L1  harness/  ← 可选、增量、持久化运行时（约 12k LOC）
      session/ · runtime/ · compaction/ · execution/ · tools/ · env/ · hooks/events
L0  core loop（无 harness 依赖，约 1.6k LOC）
      types.ts · agent-loop.ts · agent.ts · stream-fn.ts
L-1 @earendil-works/pi-ai  （Message/Model/EventStream/validateToolArguments）
```

**证据**：`agent.ts` / `agent-loop.ts` / `types.ts` / `stream-fn.ts` 的 import 只有 `@earendil-works/pi-ai`、`typebox` 与彼此（`./types.ts`、`./agent-loop.ts`、`./stream-fn.ts`），**没有任何 `harness/` import**。这是本次移植最重要的结论（见 §7.4）。

### 0.3 `harness/` 子树逐文件职责

| 文件 | 职责一句话 |
|---|---|
| `agent-harness.ts` | **公开契约门面**：`AgentHarness` / `AgentLane` / `AgentHarnessOptions` / `HarnessEventPayload`(~30 种) / `HookMap`(13 个 hook) / `OperationRequest` |
| `context.ts` | 复导出 chord 的 `Context`/`ContextKey`/`withAbortSignal`/`withCancel`，加 `getTelemetryContext`/`withTelemetryContext` |
| `events.ts` | `HarnessEventBus`：按类型订阅 + watch 订阅；`structuredClone` 隔离；单一 delivery tail 串行投递；handler 异常降级为 `handler_error` 事件 |
| `hooks.ts` | `HookRegistry`：有序注册、按 hook 各自的聚合语义、`gate.admit()`、工具 hook 每个注册一个 telemetry span |
| `messages.ts` | 应用层消息类型（`bashExecution`/`custom`/`branchSummary`/`compactionSummary`）+ 默认 `convertToLlm` |
| `prompt-templates.ts` | `.md` + YAML frontmatter 加载，`$1` / `$@` / `${@:N:L}` / `$ARGUMENTS` 替换 |
| `skills.ts` | `SKILL.md` 发现与加载、`formatSkillInvocation` |
| `system-prompt.ts` | `formatSkillsForSystemPrompt`（XML 块） |
| `result.ts` | `Result<T,E>`、`TaggedError` 工厂、`LaneBusy`/`Closed`/`InvalidMessage`… 错误分类 |
| `config.ts` | `DEFAULT_RETRY_POLICY`、`validateToolNames`/`validateRetryPolicy`/`validateCompactionSettings` |
| `types.ts` | `Skill`/`PromptTemplate`/`Resources`、`AgentHarnessTool`（**execute 签名与 AgentTool 不同**）、`AgentHarnessStreamOptions`、`ExecutionEnv`(FileSystem+Shell)、错误码枚举 |
| `telemetry.ts` | 两个声明式 schema（`AI_TELEMETRY_SCHEMA`、`HARNESS_TELEMETRY_SCHEMA`）+ `startAiSpan`/`startHarnessSpan` |
| `tools/{bash,read,write,edit,image,path-utils,file-mutation-queue}.ts` | 内置工具实现（读写编辑 + bash + 图片 + 串行文件变更队列） |
| `utils/{truncate,output-capture,shell-output,adaptive-publisher,usage}.ts` | 截断、shell 输出限流/落盘、用量累加 |
| `execution/effect-gate.ts` | `Gate`（同步准入，可抛 `AbortRequested`）/`GateControl`（owner 面） |
| `execution/tools.ts` | 工具执行的**纯函数分相**：prepare → before 决策 → execute → finalize |
| `execution/assistant.ts` | 单次 assistant 请求：context transform → provider messages → 流消费 + observer + after_response |
| `compaction/compaction.ts` | token 估算、cut point、split turn、摘要生成/更新、`compact()` |
| `compaction/branch-summarization.ts` | 分支 LCA 收集、预算裁剪、分支摘要生成 |
| `compaction/utils.ts` | 文件读写操作抽取/格式化、`serializeConversation` |
| `session/types.ts` | Entry 树 / Value & List / usage ledger / `Storage` / `Session` / `Branch` / 13 叶 `OperationState` |
| `session/{session,memory,commit,values,context,mutation-line,fork,fork-policy}.ts` | StorageBackedSession、内存后端、提交物化、地址值、上下文构建、单写线、fork |
| `session/jsonl/*` | JSONL(v4) 后端：header/事务编解码、legacy v3 兼容、原子发布、repo |
| `session/testing/*` | storage/session-repo 的 conformance 与 benchmark 套件（移植时可作对照测试清单） |
| `runtime/types.ts` | `Config`、`LaneState`、`LaneCommand`/`OperationCommand`、`Drive` |
| `runtime/harness.ts` | `Harness` 实现 + `createAgentHarness`（恢复 session、重建 lane） |
| `runtime/lane.ts`（2012 行） | 单 lane 全部行为：accept/drive/abort/queue/snapshot/watch/config |
| `runtime/drive.ts` | `driveOperation`：按 `operation.state.at` 分派到具体 procedure，循环推进直至 settled/waiting |
| `runtime/drive/*.ts` | `checkpoint`(startRun/runCheckpoint)、`generation`(LLM 调用+重试)、`tools`(工具批)、`deferred`(异步 provider)、`structural`(compaction/navigation)、`recovery`/`reconcile`(崩溃恢复)、`boundary`(边界 drain)、`terminal`(结果记录)、`tool-placement`、`response`、`retry` |
| `runtime/{restore,transcript,progress,reducer}.ts` | 启动恢复、entry 事件、流帧进度、快照归约 |
| `env/nodejs.ts` | `NodeExecutionEnv`：Result 化 fs、bash 探测、进程树 kill、输出 spill |

---

## 1. Agent loop 算法（精确伪码）

### 1.1 三个入口

```ts
// agent-loop.ts
agentLoop(prompts, context, config, signal, streamFn): EventStream<AgentEvent, AgentMessage[]>
agentLoopContinue(context, config, signal, streamFn): EventStream<AgentEvent, AgentMessage[]>
runAgentLoop(prompts, context, config, emit, signal, streamFn): Promise<AgentMessage[]>
runAgentLoopContinue(context, config, emit, signal, streamFn): Promise<AgentMessage[]>
```

三者关系：

- `agentLoop` 构造 `EventStream`（终止条件 `event.type === "agent_end"`，终值取 `event.messages`），后台 `void runAgentLoop(...).then(m => stream.end(m))`，立即返回 stream。
- `runAgentLoop(prompts, ctx, …)`：
  ```ts
  const newMessages = [...prompts];
  const currentContext = { ...context, messages: [...context.messages, ...prompts] };
  await emit({type:"agent_start"}); await emit({type:"turn_start"});
  for (const prompt of prompts) { await emit({type:"message_start",message:prompt});
                                  await emit({type:"message_end",message:prompt}); }
  await runLoop(currentContext, newMessages, config, signal, emit, streamFn ?? getDefaultStreamFn());
  return newMessages;
  ```
- `runAgentLoopContinue` 不追加 prompt、不发 prompt 的 message 事件，但仍发 `agent_start` + `turn_start`；`newMessages = []`，`currentContext = { ...context }`（**消息数组是同一个引用**，会就地 push！与 `agentLoop` 的浅拷贝不同）。
- 两个 `agentLoopContinue` 版本都前置校验：`messages.length === 0` 或 `last.role === "assistant"` 直接 **throw**（同步抛出，不是事件）。

### 1.2 `runLoop` 精确伪码

```python
async def run_loop(initial_context, new_messages, initial_config, signal, emit, stream_fn):
    current_context = initial_context
    config = initial_config
    last_completed_turn = None
    # 起始时先拉一次 steering（用户可能在等待期间已经在打字）
    pending = await config.get_steering_messages() or []

    while True:                                   # ===== OUTER LOOP：follow-up 的再入 =====
        has_more_tool_calls = True

        while has_more_tool_calls or len(pending) > 0:   # ===== INNER LOOP：工具批 + steering =====
            if last_completed_turn is not None:
                # (a) 下一轮开始前，允许 host 替换 context/model/thinking
                update = await config.prepare_next_turn(last_completed_turn)   # 传的是 ShouldStopAfterTurnContext
                if update:
                    current_context = update.context or current_context
                    config = merge(config,
                        model       = update.model          or config.model,
                        reasoning   = None if update.thinking_level == "off"
                                      else (update.thinking_level or config.reasoning))
                # (b) prepareNextTurn 可能很慢（如 compaction），期间入队的 steering 要补拉；
                #     但如果上一轮已拉到东西，就不要再拉（否则 one-at-a-time 会一次投递两条）
                if len(pending) == 0:
                    pending = await config.get_steering_messages() or []
                await emit({"type": "turn_start"})

            # (c) 注入 pending（steering 或 follow-up）
            for m in pending:
                await emit({"type": "message_start", "message": m})
                await emit({"type": "message_end",   "message": m})
                current_context.messages.append(m)
                new_messages.append(m)
            pending = []

            # (d) 流式 assistant 响应
            message = await stream_assistant_response(current_context, config, signal, emit, stream_fn)
            new_messages.append(message)

            # (e) 终结性停止原因：立刻收尾，不执行任何工具
            if message.stop_reason in ("error", "aborted"):
                await emit({"type": "turn_end", "message": message, "toolResults": []})
                await emit({"type": "agent_end", "messages": new_messages})
                return

            # (f) 工具批
            tool_calls = [c for c in message.content if c["type"] == "toolCall"]
            tool_results = []
            has_more_tool_calls = False
            if tool_calls:
                if message.stop_reason == "length":
                    batch = await fail_tool_calls_from_truncated_message(tool_calls, emit)  # 全部失败，不执行
                else:
                    batch = await execute_tool_calls(current_context, message, config, signal, emit)
                tool_results = batch.messages
                has_more_tool_calls = not batch.terminate
                for r in tool_results:
                    current_context.messages.append(r)
                    new_messages.append(r)

            await emit({"type": "turn_end", "message": message, "toolResults": tool_results})

            last_completed_turn = {"message": message, "toolResults": tool_results,
                                   "context": current_context, "newMessages": new_messages}

            # (g) 优雅停机：在 turn_end 之后、任何 queue poll 之前
            if await config.should_stop_after_turn(last_completed_turn):
                await emit({"type": "agent_end", "messages": new_messages})
                return

            pending = await config.get_steering_messages() or []

        # ===== 内层退出：agent 本来会停在这里，检查 follow-up =====
        follow_ups = await config.get_follow_up_messages() or []
        if follow_ups:
            pending = follow_ups
            continue                      # 回到 OUTER 顶部，重新进入 INNER
        break

    await emit({"type": "agent_end", "messages": new_messages})
```

### 1.3 边界与调用点（最常搞错的部分）

| 语义 | 精确位置 |
|---|---|
| `turn_start` | 第一次由 `runAgentLoop`/`…Continue` 在进入 `runLoop` 前发；后续每次内层迭代开头，且**仅在 `last_completed_turn` 存在时**发（含 follow-up 触发的迭代） |
| `turn_end` | 每个 assistant 响应之后（含 error/aborted 路径），带该轮的 `toolResults` |
| `prepareNextTurn` | 内层每次迭代开头、`turn_start` 之前、`lastCompletedTurn` 非空时。收到的是**上一轮的 `ShouldStopAfterTurnContext`**，返回 `AgentLoopTurnUpdate` |
| `shouldStopAfterTurn` | `turn_end` 之后、`getSteeringMessages` 之前。返回 true ⇒ 立即 `agent_end` 并 return（**不 poll follow-up**） |
| steering drain | ① `runLoop` 起始一次；② `prepareNextTurn` 之后补一次（仅当 pending 为空）；③ 每轮 `turn_end` 之后一次 |
| follow-up drain | **仅**在内层循环因 `has_more_tool_calls == False && pending == []` 退出后调用一次；有结果则作为 `pending` 重新进入内层 |
| 工具批终止 | `hasMoreToolCalls = !batch.terminate`；`batch.terminate = finalizedCalls.length > 0 && every(result.terminate === true)` |
| `error`/`aborted` | `turn_end` → `agent_end` → return；不再看工具、不看 steering、不看 follow-up |
| `agent_end` | 唯一出口三处：error/aborted、shouldStopAfterTurn、外层 break |

`terminate` 的“全真”语义（源码注释）：

> Early termination only happens when every finalized tool result in the batch sets this to true.

并且 `beforeToolCall` 返回 `{block: true, terminate: true}` 时该 blocked 结果也算一个 `terminate: true` 的 finalized result；混合批中只要有一个非 true 就继续下一轮（测试 `should continue after a mixed batch with one terminating blocked call` 验证）。

### 1.4 典型一轮的精确事件序列

来自 `test/agent-loop.test.ts`（`shouldStopAfterTurn` 用例）**逐字**断言：

```
user prompt, 一次 toolCall, shouldStopAfterTurn=true：

agent_start
turn_start
message_start   (user)
message_end     (user)
message_start   (assistant)
message_end     (assistant)
tool_execution_start
tool_execution_end
message_start   (toolResult)
message_end     (toolResult)
turn_end        { message: assistant, toolResults: [toolResult] }
agent_end
```

流式助手的形态：

```
message_start   (assistant, 含 start 事件时的 partial 快照)
message_update  × N      （每个 text_*/thinking_*/toolcall_* delta 一次，附原始 assistantMessageEvent）
message_end     (最终 assistant)
```

多轮（有工具、继续下一轮）时，第二轮之前插入：

```
turn_start
message_start (assistant)
...
turn_end
```

无工具、无 steering、无 follow-up 时：`agent_end.messages` = 初始 prompts + 每轮 assistant（+ toolResults）。

### 1.5 `streamAssistantResponse`（LLM 边界）逐行语义

```ts
let messages = context.messages;
if (config.transformContext) messages = await config.transformContext(messages, signal);  // AgentMessage[] -> AgentMessage[]
const llmMessages = await config.convertToLlm(messages);                                  // AgentMessage[] -> Message[]
const llmContext: Context = { systemPrompt: context.systemPrompt, messages: llmMessages, tools: context.tools };
const resolvedApiKey = (config.getApiKey ? await config.getApiKey(config.model.provider) : undefined) || config.apiKey;
const response = await streamFunction(config.model, llmContext, { ...config, apiKey: resolvedApiKey, signal });
```

要点：

- `transformContext` **先于** `convertToLlm`，且只作用于 `AgentMessage[]`。
- **整个 `config` 被 spread 进 stream options**（`convertToLlm`、`beforeToolCall`、`shouldStopAfterTurn` 等都会出现在 options 上）。TypeScript 结构化类型 + provider 只读它认识的字段所以无害；Python 移植应显式构造 options（见 §8）。
- `getApiKey` 每轮重新解析（应对短时 OAuth token），无 key 时回退到 `config.apiKey`。

流消费循环（**移植最关键的 20 行**）：

```ts
for await (const event of response) {
  switch (event.type) {
    case "start":
      partialMessage = event.partial;
      context.messages.push(partialMessage);      // ① 先 push 占位
      addedPartial = true;
      await emit({ type: "message_start", message: { ...partialMessage } });   // ② 只发浅拷贝
      break;
    case "text_start": case "text_delta": case "text_end":
    case "thinking_start": case "thinking_delta": case "thinking_end":
    case "toolcall_start": case "toolcall_delta": case "toolcall_end":
      if (partialMessage) {
        partialMessage = event.partial;
        context.messages[context.messages.length - 1] = partialMessage;         // ③ 就地替换最后一个元素（换对象）
        await emit({ type: "message_update", assistantMessageEvent: event,
                     message: { ...partialMessage } });
      }
      break;
    case "done": case "error": {
      const finalMessage = await response.result();                             // ④ 终稿从 stream 取
      if (addedPartial) context.messages[messages.length - 1] = finalMessage;
      else context.messages.push(finalMessage);
      if (!addedPartial) await emit({ type: "message_start", message: { ...finalMessage } });
      await emit({ type: "message_end", message: finalMessage });
      return finalMessage;
    }
  }
}
// 流自然结束（未收到 done/error）时的兜底分支：同 done 处理
```

**三个不可忽略的性质**：
1. **同一个 `partialMessage` 对象不会原地变异**——每个 delta 事件带来一个新对象，循环用下标替换，旧对象仍然是旧快照。
2. 送给 listener 的消息是**浅拷贝** `{...partialMessage}`，但 `content` 数组是共享引用。
3. `response.result()` 在 `done`/`error` 事件被 push 时就已经 resolve（`EventStream.push` 里先 resolve 再投递），因此 ④ 不会死等。

---

## 2. `AgentContext` 与 `AgentLoopConfig`

### 2.1 `AgentContext`（loop 的输入快照）

```ts
export interface AgentContext {
  systemPrompt: string;
  messages: AgentMessage[];
  tools?: AgentTool<any>[];
}
```

注意 `AgentContext` **不持有 model / thinkingLevel**——那些在 `AgentLoopConfig` 里，可以被 `prepareNextTurn` 替换。

### 2.2 `AgentMessage`

```ts
export type AgentMessage = Message | CustomAgentMessages[keyof CustomAgentMessages];
// Message = UserMessage | AssistantMessage | ToolResultMessage  (pi-ai)
```

`CustomAgentMessages` 是空的 declaration-merging 接口，应用/包通过 `declare module` 扩展。`harness/messages.ts` 就这样注册了 4 种：

```ts
declare module "../types.ts" {
  interface CustomAgentMessages {
    bashExecution: BashExecutionMessage;   // { role, command, output, exitCode?, cancelled, truncated, fullOutputPath?, timestamp, excludeFromContext? }
    custom: CustomMessage;                 // { role:"custom", customType, content, display, details?, timestamp }
    branchSummary: BranchSummaryMessage;   // { role, summary, fromId, timestamp }
    compactionSummary: CompactionSummaryMessage; // { role, summary, tokensBefore, timestamp }
  }
}
```

Python 对应方案见 §8.3。核心不变式：**loop 只认识 `role` 字段**，`convertToLlm` 负责把不认识的 role 过滤/转换掉（`agent.ts` 的 `defaultConvertToLlm` 只保留 `user|assistant|toolResult`）。

### 2.3 `AgentLoopConfig` 全字段

`AgentLoopConfig extends SimpleStreamOptions`，也就是同时携带 provider 选项。逐字段：

| 字段 | 类型 | 角色 / 调用时机 |
|---|---|---|
| `model` | `Model<any>`（必填） | 每轮传给 `streamFn`；可被 `prepareNextTurn.model` 替换 |
| `convertToLlm` | `(AgentMessage[]) => Message[] \| Promise<Message[]>`（必填） | **每次** LLM 调用前，`transformContext` 之后。契约：不得 throw；不可转换的消息应过滤掉 |
| `transformContext` | `(AgentMessage[], signal?) => Promise<AgentMessage[]>` | 可选，`convertToLlm` 之前。用于裁剪/外部注入。契约：不得 throw |
| `getApiKey` | `(provider: string) => string \| undefined \| Promise<…>` | 每次请求前解析；覆盖 `config.apiKey` |
| `beforeToolCall` | `(ctx: BeforeToolCallContext, signal?) => Promise<BeforeToolCallResult \| undefined>` | 参数校验之后、执行之前。返回 `{block:true, reason?, terminate?}` 阻止执行并产生 error tool result |
| `afterToolCall` | `(ctx: AfterToolCallContext, signal?) => Promise<AfterToolCallResult \| undefined>` | 工具执行后、`tool_execution_end` **之前**。字段级整体替换（`content`/`details`/`isError`/`usage`/`terminate`），**无深合并** |
| `getSteeringMessages` | `() => Promise<AgentMessage[]>` | 见 §1.3 的三个 drain 点。契约：不得 throw，无消息返回 `[]` |
| `getFollowUpMessages` | `() => Promise<AgentMessage[]>` | 仅在内层“本来要停”时调用一次 |
| `prepareNextTurn` | `(ctx: PrepareNextTurnContext) => AgentLoopTurnUpdate \| undefined \| Promise<…>` | 见 §1.3。`PrepareNextTurnContext extends ShouldStopAfterTurnContext`（同名同形） |
| `shouldStopAfterTurn` | `(ctx: ShouldStopAfterTurnContext) => boolean \| Promise<boolean>` | 见 §1.3。契约：不得 throw |
| `toolExecution` | `"sequential" \| "parallel"`，默认 `"parallel"` | 全局工具执行模式；被任一工具的 `executionMode: "sequential"` 强制覆盖 |
| `reasoning` | `ThinkingLevel`（来自 `SimpleStreamOptions`） | 由 `Agent` 从 `thinkingLevel` 映射：`"off"` ⇒ `undefined` |
| 其他 provider 选项 | `apiKey`、`signal`、`sessionId`、`onPayload`、`onResponse`、`transport`、`thinkingBudgets`、`maxRetryDelayMs`、`toolChoice`、`deferred` … | 直接透传给 `streamFn` |

相关结构：

```ts
export type ThinkingLevel = "off" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max";
export type QueueMode = "all" | "one-at-a-time";
export type ToolExecutionMode = "sequential" | "parallel";

export interface AgentLoopTurnUpdate { context?: AgentContext; model?: Model<any>; thinkingLevel?: ThinkingLevel; }

export interface ShouldStopAfterTurnContext {
  message: AssistantMessage;         // 刚完成的 assistant
  toolResults: ToolResultMessage[];  // 传给最近一次 turn_end 的结果
  context: AgentContext;             // 已追加 assistant + toolResults 之后的上下文
  newMessages: AgentMessage[];       // 若此刻退出，本次调用将返回的消息（prompt 版本含初始 prompt，continue 版本不含旧上下文）
}

export interface BeforeToolCallResult { block?: boolean; reason?: string; terminate?: boolean; }
export interface AfterToolCallResult {
  content?: (TextContent|ImageContent)[]; details?: unknown; isError?: boolean; usage?: Usage; terminate?: boolean;
}
export interface BeforeToolCallContext { assistantMessage; toolCall: AgentToolCall; args: unknown; context: AgentContext; }
export interface AfterToolCallContext  { assistantMessage; toolCall; args; result: AgentToolResult<any>; isError: boolean; context: AgentContext; }
```

`reasoning` 与 `thinkingLevel` 的映射细节（`Agent.createLoopConfig`）：

```ts
reasoning: this._state.thinkingLevel === "off" ? undefined : this._state.thinkingLevel
```

而 `prepareNextTurn` 返回的 `thinkingLevel` 回写时是反向：

```ts
reasoning: next.thinkingLevel === undefined ? config.reasoning
         : next.thinkingLevel === "off"       ? undefined
         : next.thinkingLevel
```

---

## 3. `AgentTool` 接口与参数校验

### 3.1 定义

```ts
export interface AgentTool<TParameters extends TSchema = TSchema, TDetails = any> extends Tool<TParameters> {
  /** UI 展示名 */
  label: string;
  /** 原始参数的兼容 shim，在 schema 校验之前跑；必须返回符合 TParameters 的对象 */
  prepareArguments?: (args: unknown) => Static<TParameters>;
  /** 执行；失败必须 throw，而不是把错误编码进 content */
  execute: (
    toolCallId: string,
    params: Static<TParameters>,
    signal?: AbortSignal,
    onUpdate?: AgentToolUpdateCallback<TDetails>,
  ) => Promise<AgentToolResult<TDetails>>;
  /** 不确定结果时的恢复策略 */
  replay?: "never" | "safe";
  /** 单工具执行模式覆盖 */
  executionMode?: ToolExecutionMode;
}
// 继承自 pi-ai Tool：{ name, description, parameters: TSchema }
```

```ts
export interface AgentToolResult<T> {
  content: (TextContent | ImageContent)[];   // 回给模型的内容
  details: T;                                 // 给 UI/日志的结构化细节
  usage?: Usage;                              // 工具自身消耗，不计入主上下文
  addedToolNames?: string[];                  // 本次结果之后可用的工具名（provider 延迟加载用）
  terminate?: boolean;                        // 建议本批结束后停机
}
export type AgentToolUpdateCallback<T = any> = (partialResult: AgentToolResult<T>) => void;
```

### 3.2 校验流水线（`prepareToolCall`）

```ts
const tool = currentContext.tools?.find(t => t.name === toolCall.name);
if (!tool) return { kind:"immediate", result: createErrorToolResult(`Tool ${toolCall.name} not found`), isError: true };

try {
  const preparedToolCall = prepareToolCallArguments(tool, toolCall);   // (1) prepareArguments
  const validatedArgs = validateToolArguments(tool, preparedToolCall); // (2) pi-ai 校验
  if (config.beforeToolCall) {                                          // (3) 同步 hook
    const r = await config.beforeToolCall({assistantMessage, toolCall, args: validatedArgs, context}, signal);
    if (signal?.aborted) return immediate("Operation aborted");
    if (r?.block) { const res = createErrorToolResult(r.reason || "Tool execution was blocked");
                    if (r.terminate === true) res.terminate = true;
                    return { kind:"immediate", result: res, isError: true }; }
  }
  if (signal?.aborted) return immediate("Operation aborted");
  return { kind:"prepared", toolCall, tool, args: validatedArgs };
} catch (error) {
  return { kind:"immediate", result: createErrorToolResult(msg(error)), isError: true };  // 校验失败/任何异常都变成 error result
}
```

(1) `prepareToolCallArguments`：若 `prepareArguments` 存在且返回值与入参不是同一引用，则复制 `toolCall` 并替换 `arguments`（**原始 `toolCall` 保持不变**，之后事件里 `args` 用原始参数）。真实用例：`harness/tools/edit.ts` 的 `prepareArguments: prepareEditArguments`，把旧式 `{oldText,newText}` 转成 `{edits:[...]}`。

(2) `pi-ai validateToolArguments`（`packages/ai/src/utils/validation.ts:317`）：
```ts
const args = structuredClone(toolCall.arguments);              // 克隆！
normalizeOptionalNulls(args, tool.parameters);                 // 显式 null -> 删除
Value.Convert(tool.parameters, args);                          // 类型强制转换（"1" -> 1 等）
const validator = getValidator(tool.parameters);
if (!validator.Check(args)) throw new Error(
  `Validation failed for tool "${toolCall.name}":\n${errors}\n\nReceived arguments:\n${JSON.stringify(toolCall.arguments, null, 2)}`);
return args;                                                   // 返回的是克隆 + 转换后的对象
```
两个后果（测试覆盖）：① 校验后的 `args` 是**克隆**，模型给的原始 `toolCall.arguments` 不被修改；② `beforeToolCall` 收到的 `args` 就是这个克隆，**就地修改它会被直接执行且不再重新校验**（用例 `should execute mutated beforeToolCall args without revalidation` 断言执行到 `123`）。

(3) 关于 `onUpdate`（`executePreparedToolCall`）：

```ts
let acceptingUpdates = true;
const updateEvents: Promise<void>[] = [];
const result = await prepared.tool.execute(id, args, signal, (partialResult) => {
  if (!acceptingUpdates) return;                 // 工具 promise settle 之后的调用被忽略
  updateEvents.push(Promise.resolve(emit({ type:"tool_execution_update", toolCallId, toolName, args, partialResult })));
});
acceptingUpdates = false;
await Promise.all(updateEvents);                 // 等待所有 update 事件投递完，再算完成
```
即：`onUpdate` 是**同步回调**，事件投递是异步的，被收集后统一 await；工具 resolve/reject 之后到达的 update 被静默丢弃（`finally { acceptingUpdates = false }`）。错误处理：`execute` throw ⇒ `{result: createErrorToolResult(msg), isError:true}`，`onUpdate` 事件仍然会被 await。

---

## 4. 顺序执行 vs 并行执行

### 4.1 模式选择

```ts
const hasSequentialToolCall = toolCalls.some(tc =>
  currentContext.tools?.find(t => t.name === tc.name)?.executionMode === "sequential");
if (config.toolExecution === "sequential" || hasSequentialToolCall) → executeToolCallsSequential(...)
else                                                                  → executeToolCallsParallel(...)
```

任一工具声明 `executionMode: "sequential"` ⇒ **整批**切换为顺序（测试 `should force sequential execution when one of multiple tools has executionMode=sequential`）。

### 4.2 顺序模式

```ts
for (const toolCall of toolCalls) {
  await emit({type:"tool_execution_start", toolCallId, toolName, args});   // 先发 start（即使工具不存在）
  const preparation = await prepareToolCall(...);
  let finalized;
  if (preparation.kind === "immediate") {
    finalized = { toolCall, result: preparation.result, isError: preparation.isError };
  } else {
    const executed  = await executePreparedToolCall(preparation, signal, emit);
    finalized       = await finalizeExecutedToolCall(context, assistantMessage, preparation, executed, config, signal);
  }
  await emitToolExecutionEnd(finalized, emit);                 // tool_execution_end 在该工具结束时立即发
  const msg = createToolResultMessage(finalized);
  await emitToolResultMessage(msg, emit);                      // 紧接着 message_start/message_end
  finalizedCalls.push(finalized); messages.push(msg);
  if (signal?.aborted) break;                                  // 中止：剩余调用不再开始
}
return { messages, terminate: shouldTerminateToolBatch(finalizedCalls) };
```

### 4.3 并行模式（“promise 技巧”）

```ts
const finalizedCalls: (FinalizedToolCallOutcome | (() => Promise<FinalizedToolCallOutcome>))[] = [];

for (const toolCall of toolCalls) {
  await emit({type:"tool_execution_start", ...});              // 预检阶段仍是顺序的
  const preparation = await prepareToolCall(...);              // beforeToolCall 也仍是顺序的！
  if (preparation.kind === "immediate") {                       // 未知工具/校验失败/被 block/已 abort
    const finalized = { toolCall, result, isError };
    await emitToolExecutionEnd(finalized, emit);                // immediate 的 end 也在预检期发
    finalizedCalls.push(finalized);
    if (signal?.aborted) break;
    continue;
  }
  finalizedCalls.push(async () => {                             // ★ 存 thunk，不存 promise
    if (signal?.aborted) {
      const finalized = { toolCall, result: createErrorToolResult("Operation aborted"), isError: true };
      await emitToolExecutionEnd(finalized, emit);
      return finalized;
    }
    const executed  = await executePreparedToolCall(preparation, signal, emit);
    const finalized = await finalizeExecutedToolCall(...);
    await emitToolExecutionEnd(finalized, emit);                // ✔ 完成顺序
    return finalized;
  });
  if (signal?.aborted) break;
}

// ★★ 关键：map 时按数组顺序同步调用所有 thunk ⇒ 同一 tick 内全部 execute() 启动 ⇒ 真并发
const orderedFinalizedCalls = await Promise.all(
  finalizedCalls.map(entry => (typeof entry === "function" ? entry() : Promise.resolve(entry))),
);

// 然后才按 assistant 源码顺序产 toolResult 消息
const messages = [];
for (const finalized of orderedFinalizedCalls) {
  const msg = createToolResultMessage(finalized);
  await emitToolResultMessage(msg, emit);                        // ✔ 源码顺序
  messages.push(msg);
}
return { messages, terminate: shouldTerminateToolBatch(orderedFinalizedCalls) };
```

为什么必须是 thunk：如果直接 `push(execute(...))`，`await prepareToolCall` 的每个 await 间隙里先前启动的 promise 早已开始跑，预检阶段（尤其 `beforeToolCall`）就不再严格顺序。用 thunk + `Promise.all(map(...))`，**预检严格顺序、执行同时启动、结果数组按输入序**。

### 4.4 顺序性保证汇总

| 事件/产物 | sequential | parallel |
|---|---|---|
| `tool_execution_start` | 源码顺序，且与 prepare 交错 | 源码顺序，全部在预检期发出 |
| `beforeToolCall` | 与工具执行交错 | 全部先跑完（顺序） |
| 工具 `execute()` 启动 | 严格一个接一个 | 同一 tick 全部启动 |
| `tool_execution_end` | 源码顺序 | **完成顺序**（测试断言 `["tool-2","tool-1"]`） |
| `toolResult` 消息（`message_start/end`） | 源码顺序 | **源码顺序**（测试断言 `["tool-1","tool-2"]`） |
| `turn_end.toolResults` | 源码顺序 | 源码顺序 |

### 4.5 中止（abort）语义

- 预检阶段：`prepareToolCall` 内部两处 `signal.aborted` 检查返回 `createErrorToolResult("Operation aborted")`；完成后若已 abort，循环 `break`，**剩余调用完全不启动**。
- 并行 thunk：执行前再查一次 `signal.aborted`，命中则产生 "Operation aborted" 的 error result（**但仍会发 `tool_execution_end`**）。
- 已经在跑的并行工具不会被强制取消；`signal` 被传入 `execute`，由工具自行响应（内置工具在关键点反复 `if (context.abortSignal?.aborted) throw new Error("Operation aborted")`）。
- abort 不会让批次 `terminate`（`terminate` 只看 `result.terminate`）。

### 4.6 错误 → tool result

| 失败点 | 结果 |
|---|---|
| 工具名不存在 | `createErrorToolResult("Tool <name> not found")`，`isError: true` |
| `prepareArguments` / 校验抛错 | error result，消息为异常 message（`prepareToolCall` 的整个 try/catch 覆盖） |
| `beforeToolCall` 抛错 | 同样被 `prepareToolCall` 的 catch 捕获 → error result（消息为异常 message）。**harness 版本不同**：hook registry 会把异常转成 `handler_error` 事件并视作 block |
| `beforeToolCall` 返回 block | `createErrorToolResult(reason ?? "Tool execution was blocked")` |
| `execute` throw | error result（message 来自异常） |
| `afterToolCall` throw | **丢弃工具的原始结果**，替换为 error result，`isError = true`（`finalizeExecutedToolCall` 的 catch） |
| assistant `stopReason === "length"` | 该消息的**所有** toolCall 都不执行，逐一发 `tool_execution_start/end` + 错误 result（文案含 `the response hit the output token limit`），且 `hasMoreToolCalls = false`（`terminate: false`），循环继续让模型重发 |

`createErrorToolResult(message)`：

```ts
{ content: [{ type: "text", text: message }], details: {} }     // 注意 details 是空对象
```

`createToolResultMessage(finalized)`：

```ts
{ role: "toolResult", toolCallId, toolName,
  content: result.content ?? [],           // 容忍无 content 的工具（JS 扩展）
  details, usage,
  ...(result.addedToolNames?.length ? { addedToolNames } : {}),
  isError, timestamp: Date.now() }
```

---

## 5. `AgentEvent` 联合类型与生命周期

```ts
export type AgentEvent =
  // agent 生命周期
  | { type: "agent_start" }
  | { type: "agent_end"; messages: AgentMessage[] }
  // turn 生命周期：一次 assistant 响应 + 其工具调用/结果
  | { type: "turn_start" }
  | { type: "turn_end"; message: AgentMessage; toolResults: ToolResultMessage[] }
  // 消息生命周期（user / assistant / toolResult 都会发）
  | { type: "message_start"; message: AgentMessage }
  | { type: "message_update"; message: AgentMessage; assistantMessageEvent: AssistantMessageEvent }  // 仅 assistant 流式
  | { type: "message_end"; message: AgentMessage }
  // 工具执行生命周期
  | { type: "tool_execution_start"; toolCallId: string; toolName: string; args: any }
  | { type: "tool_execution_update"; toolCallId: string; toolName: string; args: any; partialResult: any }
  | { type: "tool_execution_end"; toolCallId: string; toolName: string; result: any; isError: boolean };
```

`AssistantMessageEvent`（pi-ai，`message_update` 的 payload）：

```ts
| { type:"start"; partial } 
| { type:"text_start"|"text_delta"|"text_end"|"thinking_start"|"thinking_delta"|"thinking_end"|"toolcall_start"|"toolcall_delta"|"toolcall_end"; contentIndex; ...; partial }
| { type:"done";  reason: "stop"|"length"|"toolUse"|"deferred"; message }
| { type:"error"; reason: "aborted"|"error"; error: AssistantMessage }
```

顺序规则（README + 测试）：

1. `agent_start` 一定最先，`agent_end` 一定最后（每个 run 恰好一次）。
2. `turn_start` 先于该轮所有 message/tool 事件；`turn_end` 在所有 toolResult 消息之后。
3. 每条消息 `message_start` → （assistant 才有若干 `message_update`）→ `message_end`。
4. 工具：`tool_execution_start` → 若干 `tool_execution_update` → `tool_execution_end` → toolResult 的 `message_start/end`。
5. parallel 模式下 **`tool_execution_end` 按完成顺序，toolResult 消息按源码顺序**。
6. `agent_end` 是最后一个事件；但 **awaited 的 `Agent.subscribe` listener 仍属于本次 run 的 settle 范围**——`waitForIdle()` 要等 `agent_end` 的 listener 全部完成。

`EventStream` 机制（`packages/ai/src/utils/event-stream.ts`）：`push` 先判 `isComplete(event)`（对 agent 流是 `type === "agent_end"`），命中即 `resolveFinalResult(extractResult(event))`（对 agent 流取 `event.messages`），然后投递给等待的 consumer 或入队；`result()` 返回这个 promise；`[Symbol.asyncIterator]` 从队列取，`end()` 后被消费完即结束。**陷阱**：如果 `runAgentLoop` 抛异常，`.then(...)` 不会执行，`stream.end()` 永不调用 ⇒ `result()` 永久 pending、迭代永不结束（TS 侧真实风险，Python 必须在 `finally` 里收尾）。

---

## 6. `Agent` 类

### 6.1 状态

```ts
export interface AgentState {
  systemPrompt: string;
  model: Model<any>;
  thinkingLevel: ThinkingLevel;
  set tools(tools: AgentTool<any>[]);   get tools(): AgentTool<any>[];      // 赋值拷贝顶层数组
  set messages(messages: AgentMessage[]); get messages(): AgentMessage[];  // 同上
  readonly isStreaming: boolean;
  readonly streamingMessage?: AgentMessage;
  readonly pendingToolCalls: ReadonlySet<string>;
  readonly errorMessage?: string;
}
```

内部 `MutableAgentState` 用闭包变量 + getter/setter 实现 `tools`/`messages` 的**顶层浅拷贝**（`nextTools.slice()`），因此 `agent.state.messages.push(x)` 会改到真实数组，但 `agent.state.messages = arr` 是拷贝。

其余 host 侧配置（可直接读写的公开字段）：`convertToLlm`、`transformContext`、`streamFunction`、`getApiKey`、`onPayload`、`onResponse`、`beforeToolCall`、`afterToolCall`、`shouldStopAfterTurn`、`prepareNextTurn`（旧式，只有 signal）、`prepareNextTurnWithContext`（新式，带 context）、`sessionId`、`thinkingBudgets`、`transport`（默认 `"auto"`）、`maxRetryDelayMs`、`toolExecution`（默认 `"parallel"`）、`steeringMode`/`followUpMode`。

`DEFAULT_MODEL` 是 `{ id:"unknown", provider:"unknown", api:"unknown", contextWindow:0, maxTokens:0, … }` 的哨兵；构造时 `options` 可整省（源码注释提到旧编译产物会省略 options / streamFn）。

### 6.2 运行生命周期

```ts
private async runWithLifecycle(executor: (signal: AbortSignal) => Promise<void>): Promise<void> {
  if (this.activeRun) throw new Error("Agent is already processing.");
  const abortController = new AbortController();
  let resolvePromise = () => {};
  const promise = new Promise<void>(resolve => { resolvePromise = resolve; });
  this.activeRun = { promise, resolve: resolvePromise, abortController };
  this._state.isStreaming = true;
  this._state.streamingMessage = undefined;
  this._state.errorMessage = undefined;
  try { await executor(abortController.signal); }
  catch (error) { await this.handleRunFailure(error, abortController.signal.aborted); }
  finally { this.finishRun(); }
}
```

- `prompt(input)`：`activeRun` 存在则 throw；字符串 → `{role:"user", content:[{type:"text",text}, ...images], timestamp: Date.now()}`；然后 `runPromptMessages(messages)` → `runAgentLoop`。
- `continue()`：最后一条是 `assistant` 时，**先尝试 drain steering**（用 `skipInitialSteeringPoll: true` 跑 prompt），再 drain follow-up，都没有才 throw `"Cannot continue from message role: assistant"`；否则 `runAgentLoopContinue`。
- `abort()`：`this.activeRun?.abortController.abort()` —— 只是置 signal，不做别的；中止由 loop/工具/stream 观察 signal 完成。
- `waitForIdle()`：`return this.activeRun?.promise ?? Promise.resolve()` —— 该 promise 在 `finishRun()` 里 resolve，即 **`agent_end` 的 listener 全部 await 完之后**。
- `signal` getter：`this.activeRun?.abortController.signal`。

### 6.3 事件归约与投递

```ts
private async processEvents(event: AgentEvent): Promise<void> {
  switch (event.type) {
    case "message_start": this._state.streamingMessage = event.message; break;      // 含 user/toolResult，短暂设置又清空
    case "message_update": this._state.streamingMessage = event.message; break;
    case "message_end": this._state.streamingMessage = undefined;
                        this._state.messages.push(event.message); break;            // ★ 只有 end 才落进 state.messages
    case "tool_execution_start": this._state.pendingToolCalls = new Set([...prev, id]); break;   // 每次新建 Set（不可变风格）
    case "tool_execution_end":  this._state.pendingToolCalls = new Set([...prev].filter(x=>x!==id)); break;
    case "turn_end": if (event.message.role === "assistant" && event.message.errorMessage)
                       this._state.errorMessage = event.message.errorMessage; break;
    case "agent_end": this._state.streamingMessage = undefined; break;
  }
  const signal = this.activeRun?.abortController.signal;
  if (!signal) throw new Error("Agent listener invoked outside active run");
  for (const listener of this.listeners) await listener(event, signal);             // 按订阅顺序串行 await
}
```

- `subscribe(listener)` 返回退订函数；listener 签名 `(event, signal) => void | Promise<void>`，**同步 listener 也 OK**。
- `runPromptMessages` 传给 loop 的 sink 就是 `event => this.processEvents(event)`，所以 “先归约状态、再 await 全部 listener”。README 强调：**assistant `message_end` 处理是工具预检前的 barrier**，因此 `beforeToolCall` 看到的 agent state 已经包含发起该调用的 assistant 消息。

### 6.4 失败路径（合成消息）

`handleRunFailure(error, aborted)` 会**伪造一条 assistant 消息**并把完整事件序列补上：

```
message_start (assistant, content:[{type:"text",text:""}], usage: EMPTY_USAGE,
               stopReason: aborted ? "aborted" : "error", errorMessage: msg, timestamp)
message_end
turn_end   { message, toolResults: [] }
agent_end  { messages: [failureMessage] }
```

注意 `agent_end.messages` 只有这一条失败消息（不是整段 transcript）——host 不能把它当成 `newMessages` 全量。

### 6.5 消息队列

```ts
class PendingMessageQueue {
  enqueue(m); hasItems(); clear();
  drain(): AgentMessage[] {
    if (mode === "all") { return 全部并清空; }
    const first = this.messages[0]; if (!first) return []; this.messages = this.messages.slice(1); return [first];
  }
}
```

默认 `steeringMode = "one-at-a-time"`、`followUpMode = "one-at-a-time"`（注意：**harness 的默认是 `"all"`**，见 `runtime/harness.ts:66-67`，两侧默认不同！）。`createLoopConfig` 把 drain 包成 `getSteeringMessages`/`getFollowUpMessages`；`skipInitialSteeringPoll` 是一个一次性闭包标志，让 `continue()` 场景下不会被起始 poll 重复投递。

---

## 7. Harness 层：在裸 loop 之上加了什么

### 7.1 定位

`agent-harness.ts` 的 `AgentHarness` 不是 Agent 的子类，而是**另一个运行时**：把 loop 从“内存里的递归函数”重写成“可持久化的操作状态机 + 三存储”。核心设计规则（`docs/harness.md` §0.3）：

1. **三存储一个不变式**：entries（append-only 会话树）/ values & lists（可变当前态）/ usage ledger（append-only）。任何 payload 必在这三处之一。
2. **原子事务**：一次 commit 内 entry/usage 插入与 value/list 写入全有或全无，序号严格递增；不存在“半写”状态。
3. **持久重启点**：每次状态迁移后用**完整状态**覆写 `pi.op.state/{operationId}`，恢复时不需要 journal、也不靠“缺什么”推断位置。
4. **intent / settlement 两段提交**：provider 请求与真实工具执行都先写 intent（“即将做 X，输出 id 为 R/U”），再执行不确定的外部效果，再写 settlement。崩溃窗口内的不确定效果由 `replay: "never" | "safe"` 策略处理。

### 7.2 loop 语义在 harness 里的对应物

| 裸 loop 概念 | harness 对应 |
|---|---|
| 内层 while 迭代 | `driveOperation` 按 `operation.state.at` 分派：`starting` → `checkpoint` → `assistant.ready` → `tools` → `checkpoint` → … |
| 一次 LLM 调用 | `assistant.ready` / `assistant.effect_pending` / `assistant.retry_wait` 三个叶 + `runGeneration` |
| 工具批 | `at: "tools"` + `ToolBatch`/`ToolCall` 嵌套状态机（`planned` → `effect_pending` → `outcome_ready` → `completed`，`sourceIndex` 记录在 assistant content 中的**原始下标**用于源码序物化） |
| steering/follow-up drain | `planBoundaryInbox`（`drive/boundary.ts`）：按 `steeringMode`/`followUpMode` 选择 `pi.pending.entry` 中的 `steer`/`followUp`/`write`，物化成 entry，写 `branchTip`，并 emit `queue_update` |
| `shouldStopAfterTurn` | `before_run_end` hook（`finishRunBoundary`）+ checkpoint 的 `continuation`（`need_assistant` / `may_finish`）；hook 返回 `{followUp: string}` 相当于注入一条 follow-up |
| `prepareNextTurn` | 每次 `assistantReadyAtBoundary` 重新快照 `LaneConfiguration`（model/thinkingLevel/activeTools）与 `streamOptions` |
| abort | `requestAbort` 写 `control.status = "cancel_requested"` → `reconcileOperation`；进程内的 `Context.abortSignal` **不等于** `requestAbort`（文档明确：abort context 不会写 `cancel_requested`） |
| provider 异步完成 | `deferred.suspended` / `deferred.effect_pending` 叶 + `DeferredHandle`（`streamOptions.deferred`）+ `pollDeferred` |

### 7.3 主要增量能力清单

- **Hooks（13 个）**：`before_run`（可注入消息）、`before_drive`（fail-closed）、`before_run_end`（可给 followUp 字符串）、`transform_context`（改 messages/systemPrompt）、`before_request`（改 stream options patch）、`before_payload`（改 provider payload）、`after_response`（改落定的 assistant 消息）、`before_tool`（改 args / block+terminate）、`after_tool`（字段级 patch）、`before_compaction`、`before_navigation`。
  聚合语义各不同：`before_run` 累加注入；`before_tool` 串行可改 args，遇 block 立即 break，**handler 抛错 ⇒ 视作 block**；`after_tool` 逐字段覆盖并维护 `current`；`before_compaction`/`before_navigation` 取**第一个**返回 `{decline}` 或 `{compaction|summary}` 的结果；`before_drive` 任一失败即向上抛。
- **EventBus**：按类型 + watch 双通道；emit 时 `structuredClone` 快照 payload 并**绑定当前收件人**；全局 `deliveryTail` 保证投递串行；handler 异常不会打断别人，而是再发一个 `handler_error` 事件。`BufferedEventWatcher` 支持 `start()` 前缓冲、`resnapshot()` 的 dropping/holding 边界。
- **工具持久化**（`docs/tool-durability.md`）：intent（含 `replay` 策略与 `pi.op.tool_args`）→ 执行 → `pi.pending.entry/{resultId}` 暂存 → 按 `sourceIndex` 顺序物化成 entry；进度更新写 `pi.pending.tool_output/{id}`，`onUpdate(partial, {checkpoint: true})` 请求**替换恢复检查点**（内置 bash 工具每 2s 请求一次）；崩溃后用检查点内容 + `INTERRUPTION_MARKER` 合成“被中断”结果。
- **deferred provider 响应**：`streamOptions.deferred` 让有能力的 provider 返回 `DeferredHandle`；`runDeferred` 轮询/恢复，`pollDeferred` 允许一次立即轮询。
- **重试**：`RetryPolicy { enabled, maxRetries, baseDelayMs, maxAgentDelayMs? }` 归一化为 `{maxAttempts, baseDelayMs, maxAgentDelayMs}`（默认 60s 上限）；`retryNotBefore` 采用退避 + 饱和算术；`retry_scheduled/start/end` 事件。
- **compaction / branch summarization**（纯算法，可独立移植）：
  - `estimateTokens(msg)`：字符数 / 4（图片按 4800 字符）。
  - `estimateContextTokens(messages)`：找最后一条有有效 usage 的 assistant，`usageTokens + 其后消息估算`。
  - `shouldCompact(tokens, window, settings)`：`tokens > window - reserveTokens`；默认 `{enabled:true, reserveTokens:16384, keepRecentTokens:20000}`。
  - `findCutPoint` / `findValidCutPoints` / `findTurnStartIndex`：从尾部累计 token 到 `keepRecentTokens`，取最近合法切点；切在 turn 中间则 `isSplitTurn`，`turnStartIndex` 指向该 turn 的 user 消息。
  - `prepareCompaction(pathEntries, settings)`：把上一次 compaction 的 `retainedTail` 虚拟成 entries 拼回，得到 `messagesToSummarize` / `turnPrefixMessages` / `retainedTail` / `fileOps` / `previousSummary`。
  - `generateSummaryWithRequest`：`<conversation>…</conversation>` + 固定格式摘要 prompt；有 `previousSummary` 时改用 *UPDATE* prompt 并要求保留旧信息；`maxTokens = min(0.8*reserveTokens, model.maxTokens)`。
  - `compact()`：split turn 时分别生成 history summary 与 turn-prefix summary，用 `\n\n---\n\n**Turn Context (split turn):**\n\n` 拼接；最后附加文件操作清单。
  - `collectEntriesForBranchSummary`（LCA 回溯取被放弃的分支路径）+ `prepareBranchEntries`（从新到旧按 token 预算累积，compaction/branch_summary 条目在临界处按 0.9 预算放宽）。
- **Session/存储**：Entry 树（`message`/`compaction`/`branch_summary`/`custom`，`id/parentId/seq/timestamp`），值地址 `value(ns,key)`/`list(ns,key)`，内置地址 `pi.branch.tip/{branch}`、`pi.lane.config/{lane}`、`pi.lane.state/{lane}`、`pi.pending.entry/{id}`、`pi.result/{opId}` 等；`Storage` 接口 11 个方法；内存后端 `MemorySessionRepo` 与 JSONL(v4) 后端 `JsonlSessionRepo`（含 legacy v3 读取）；`session/testing/*` 提供 conformance 套件（移植时的黄金测试清单）。
- **执行环境**：`ExecutionEnv = FileSystem + Shell`，全部返回 `Result<T, FileError|ExecutionError>`，**约定不得 throw**；`NodeExecutionEnv` 处理 bash 探测（Windows Git Bash / WSL `-s` stdin 模式）、进程树 kill（`process.kill(-pid)`）、输出限流（`OUTPUT_MIN_EMIT_INTERVAL_MS=100`、`OUTPUT_TARGET_BYTES_PER_SECOND=100KB`）与超限落盘（spill，`SPILL_HIGH_WATER_MARK=8MB`）。
- **工具与基础件**：`createBashTool`/`createReadTool`/`createWriteTool`/`createEditTool`、`withFileMutationQueue`（同一文件的读改写串行）、`truncateHead/Tail`（默认 2000 行 / 50KB）、路径工具、image MIME 探测。
- **harness 版 AgentTool**：`AgentHarnessTool.execute(toolCallId, params, onUpdate, toolContext, invocation, context)` —— **没有 `signal` 参数**（取消通过 `context.abortSignal`）；多了 `toolContext`（每轮解析的宿主上下文）、`invocation`（`invocationId`/`operationId`/`turnId` + 持久 `getMemo`/`setMemo`）。`onUpdate` 变体还接受 `{ checkpoint?: true }`。

### 7.4 判断：harness 对最小移植是否必需？

**不是必需的，是“高级增量层”。**理由：

1. 核心 loop 代码（`types.ts` + `agent-loop.ts` + `agent.ts` + `stream-fn.ts`，约 1.6k LOC）**零 harness 依赖**，可独立移植并完整实现：流式、工具（含并行）、steering/follow-up、abort、prepareNextTurn/shouldStopAfterTurn、事件序列。
2. 宿主集成所需的 hook 点已经全部在 `Agent`/`AgentLoopConfig` 上（`transformContext`/`convertToLlm`/`before|afterToolCall`/`getApiKey`/`onPayload`/`onResponse`）。持久化可由宿主订阅 `message_start`/`message_end`/`turn_end` 自行负责（coding-agent 的简单路径 `src/core/sdk.ts` 正是这样用 `Agent`）。
3. harness 的价值集中在三件事：**崩溃可恢复**、**单 lane 串行 + 多 lane 并发**、**provider/工具的 intent–settlement 记账**。若产品一开始不需要“进程被杀后恢复未完成的工具/provider 调用”，移植它属于过度工程（约 12k LOC，其中 `runtime/lane.ts` 单文件 2012 行、`drive/structural.ts` 1222 行）。
4. 建议分三段：
   - **P1（必做）** core loop + `Agent` + `streamFn` 抽象 + `EventStream`。
   - **P2（强烈建议）** 从 harness 里摘出**纯函数/无存储**部分：`messages.convertToLlm`、4 种应用消息类型、`compaction.ts` + `branch-summarization.ts` + `compaction/utils.ts`、`skills.ts`、`prompt-templates.ts`、`utils/truncate.ts`、`types.Result`/错误分类、内置 bash/read/write/edit 工具 + `ExecutionEnv`。这些是产品可用性的主体，且几乎不依赖 session/runtime。
   - **P3（按需）** session 存储（先内存，后 JSONL）+ lane/operation 状态机 + hooks/events + deferred/retry + telemetry。

---

## 8. Python 移植要点

### 8.1 流边界：async generator vs callback sink

TS 的 `StreamFn` 返回一个既有 **async 迭代器**（事件）又有 **`result()` promise**（终值）的对象，且 `result()` 在终止事件 push 时即 resolve。Python 里最贴合的两种实现：

**(A) 推荐：手写 `EventStream[T, R]`（`asyncio`）**

```python
class EventStream(Generic[T, R]):
    def __init__(self, is_complete: Callable[[T], bool], extract_result: Callable[[T], R]) -> None:
        self._queue: asyncio.Queue[T | None] = asyncio.Queue()
        self._done = False
        self._result: asyncio.Future[R] = asyncio.get_running_loop().create_future()
        ...
    def push(self, event: T) -> None:
        if self._done: return
        if self._is_complete(event) and not self._result.done():
            self._result.set_result(self._extract_result(event))
        self._queue.put_nowait(event)
    def end(self, result: R | None = None) -> None:
        if result is not None and not self._result.done(): self._result.set_result(result)
        self._done = True
        self._queue.put_nowait(None)          # sentinel
    def __aiter__(self): return self
    async def __anext__(self) -> T:
        item = await self._queue.get()
        if item is None: raise StopAsyncIteration
        return item
    def result(self) -> Awaitable[R]: return self._result
```

`agent_loop()` 用 `asyncio.create_task(run_agent_loop(...))` 启动，task 的 `finally` 必须 `stream.end()`（TS 缺这一步，异常时 stream 悬挂——不要照抄）。

**(B) 不推荐：纯 async generator**，因为 `async for` 拿不到 generator 的 `return` 值（需要手动 `asend`/捕获 `StopAsyncIteration.value`），且无法在事件与终值之间解耦。若确实想要 `async for event in agent_loop(...)`，就把终值放到 `EventStream` 之外的对象上（`stream.result()`），形状同 (A)。

**sink**：`Emit = Callable[[AgentEvent], Awaitable[None]]`，统一 `await emit(...)`。TS 允许同步 sink，Python 里若宿主想同步处理，用 `async def emit(e): handler(e)` 包一层即可；不要引入“有时 sync 有时 async”的联合类型。

**`onUpdate`**：保持 TS 语义——同步回调、`accepting_updates` 标志、事件收集到 list 后 `await asyncio.gather(*pending)`（gather 会保持顺序，相当于 `Promise.all`）。若工具可能从别的线程回调，用 `loop.call_soon_threadsafe`。

### 8.2 取消：AbortSignal 而不是 CancelledError

TS 的 `AbortSignal` 是**被观察的布尔量**：loop/工具在关键点 `if signal.aborted: return 合成错误结果`，然后**正常发事件**。若 Python 用 `asyncio.Task.cancel()` / `CancelledError`，会从任意 await 点展开，你必须在 loop 边界捕获、合成 aborted 的 assistant 消息、补 `turn_end` + `agent_end`，且无法在“工具已经产生一半结果”的位置优雅收敛。

推荐：

```python
class AbortSignal:
    def __init__(self) -> None: self._aborted = False; self._event = asyncio.Event()
    @property
    def aborted(self) -> bool: return self._aborted
    def abort(self) -> None:
        if not self._aborted: self._aborted = True; self._event.set()
    def throw_if_aborted(self) -> None:
        if self._aborted: raise AbortError("Operation aborted")
    async def wait(self) -> None: await self._event.wait()
```

配套：`Agent.abort()` 只置位；`waitForIdle()` 等 `active_run` 的 task；硬停机（宿主 Ctrl-C）再叠加 task cancel，并在 `finally` 收尾事件。若团队坚持用 `anyio`/`asyncio.timeout` 的 cancel scope，请把它包在工具执行的最外层，并在 loop 里转成 `stop_reason="aborted"` 的 assistant 消息，保持对外事件序列不变。

### 8.3 消息类型：dataclass 还是 pydantic

- **推荐组合**：`@dataclass`（或 `pydantic.BaseModel` 但只做序列化）定义 `TextContent` / `ThinkingContent` / `ImageContent` / `ToolCall` / `Usage` / `UserMessage` / `AssistantMessage` / `ToolResultMessage` / 4 种应用消息，用 `role: Literal[...]` 做**判别式联合**：

```python
AgentMessage = Union[UserMessage, AssistantMessage, ToolResultMessage,
                     BashExecutionMessage, CustomMessage, BranchSummaryMessage, CompactionSummaryMessage]
```

- **工具参数校验**：TS 用 TypeBox + `Value.Convert` + 显式 null 归一化 + 克隆。Python 首选把工具参数声明为 pydantic 模型或 `TypedDict` + JSON Schema，然后：
  1. `args = copy.deepcopy(tool_call.arguments)`（对应 `structuredClone`）；
  2. 删除值为 `None` 的可选字段（`normalizeOptionalNulls`）；
  3. 做类型强制转换（`"1"` → `1`，`"true"` → `True`；pydantic 的 lax 模式接近，但**不完全一致**，需要对照测试）；
  4. 校验失败时**逐字**复制 TS 的错误文本，因为它是模型可见的：
     ```
     Validation failed for tool "<name>":
       - <path>: <message>

     Received arguments:
     <json indent=2>
     ```
- `convertToLlm` 契约“不得抛异常”：Python 里用 `try/except` + 安全兜底（返回原列表），并在开发模式下把异常写成日志/事件，避免静默。
- 时间戳统一 `int(time.time() * 1000)`。

### 8.4 必须显式处理的陷阱清单

1. **最后一条消息的“下标替换”**（§1.5 ③）。TS 每个 delta 都换新对象并写回 `context.messages[-1]`；`Agent.streamingMessage` 因 `message_update` 每次拿到新对象。Python 若用可变 dataclass 原地 mutate，会破坏“旧快照不可变”的假设（任何持有引用的宿主/测试会看到未预期的变化）。**忠实做法**：`context.messages[-1] = partial`；对 listener 传 `copy.copy(msg)`（注意浅拷贝下 `content` 列表共享——如需安全请 `copy.deepcopy(msg)`，但要标为有意偏离并评估性能）。不要忘了 `message_start` 的 assistant 也是同一对象的快照。
2. **`result()` 与事件流的解耦**：终止事件 push 时先 resolve 终值。Python 的 `Future.set_result` 也应先于 `queue.put_nowait`。
3. **异常时必须 `stream.end()`**：TS 有悬挂风险；Python 在 task 的 `finally` 里补 `agent_end`（或至少 `end()`），并让 `result()` 以异常完成。
4. **并行工具 = thunk + `asyncio.gather`**：`gather(*[thunk() for thunk in entries])` 会同步地把所有 coroutine 注册、保持输入顺序返回结果；**thunk 内部必须吞掉所有异常**（TS 的 thunk 内部 `executePreparedToolCall` / `finalizeExecutedToolCall` 都自行 try/catch，所以 `Promise.all` 实际不会 reject）。若用 `asyncio.TaskGroup`，语义会变成“任一异常取消全体”——**不要**。
5. **`terminate` 的“全部为真”**：`terminate = len(finalized) > 0 and all(r.result.terminate is True for r in finalized)`。`afterToolCall` 的 patch 覆盖后才是最终值；blocked 结果也可能带 `terminate`。
6. **`stop_reason == "length"`**：不执行任何工具调用，但**仍逐一**发 `tool_execution_start`/`tool_execution_end`/toolResult，且 `has_more_tool_calls = False`（继续下一轮让模型重发）。
7. **`shouldStopAfterTurn` / `prepareNextTurn` 的位置**：前者在 `turn_end` 之后、poll steering 之前，返回 true 就直接 `agent_end`（**不 poll follow-up**）；后者在下一轮开头、`turn_start` 之前，**第一轮不调用**（用 `last_completed_turn is None` 判断）。
8. **起始与补拉的 steering poll**：`runLoop` 开头一次；`prepareNextTurn` 之后仅当 `pending` 为空时再拉一次（否则 `one-at-a-time` 会一次性投递两条）。`Agent.continue()` 用 `skipInitialSteeringPoll` 避免重复。
9. **error/aborted 提前 return**：不会执行工具、不 poll steering/follow-up；`agent_end.messages` 是本次 `newMessages`（continue 场景不包含旧上下文）。
10. **`afterToolCall` 抛异常会丢弃工具结果**（替换成 error result）；**`beforeToolCall` 抛异常则只是变成一条 error tool result**（因为它在 `prepareToolCall` 的 try 内），两者行为不同，不要合并处理。
11. **`beforeToolCall` 的 args 是校验后的克隆**：原地修改会被执行且不再校验；重新给 `args` 字段（在 harness 里）会重新校验。
12. **`{...config}` 被整体透传**给 `streamFn`：Python 侧应显式构造 stream options dataclass，避免把回调/消息历史塞给 provider 客户端。
13. **`isStreaming` 的生命周期**：从 `runWithLifecycle` 开始置 true，到 `finishRun()`（即 `agent_end` listener 全部 await 完成后）才置 false；`waitForIdle()` 等的是这个点，而不是 `agent_end` 事件本身。
14. **`AgentState.messages` 只在 `message_end` 追加**；增量信息只在 `streamingMessage`。宿主如果只在 `message_update` 里拼文本，需要在 `message_end` 再从权威消息重建。
15. **harness 与 Agent 的默认 `QueueMode` 不同**（`one-at-a-time` vs `all`），照抄默认值时要显式统一。
16. **`structuredClone` → `copy.deepcopy`**（harness 事件与 watch 快照为了隔离反复深拷贝；大数据量下 Python 深拷贝明显更慢，可考虑只拷贝浅层 + 冻结约定）。
17. **事件投递是串行 await 的**：`for listener in listeners: await listener(...)`。Python 保持插入顺序（`dict`/list），**不要**用 `asyncio.gather` 并发通知 listener，否则事件的因果顺序会被打乱。
18. **`prepareArguments` 是必需的**：真实世界里模型会给出旧式/扁平参数（如 `edit` 的 `{oldText,newText}`），扫描器/兼容 shim 必须在校验前跑，并保证返回值满足 schema。

### 8.5 建议的 Python 模块划分（对齐 TS 分层）

```
pi_agent/
  types.py          # AgentMessage/AgentTool/AgentContext/AgentLoopConfig/AgentEvent/...
  stream.py         # StreamFn 协议 + EventStream + AssistantMessageEventStream
  loop.py           # run_agent_loop / run_agent_loop_continue / agent_loop / agent_loop_continue
  agent.py          # Agent, PendingMessageQueue, AgentState
  tools.py          # 校验流水线 + 顺序/并行执行（纯函数，可单测）
  harness/          # P2/P3
    messages.py prompt_templates.py skills.py compaction.py branch_summary.py
    result.py types.py tools/ env/ session/ runtime/
```

单测建议直接对齐 `test/agent-loop.test.ts` 的用例名（事件序列断言、parallel 完成序 vs 源码序、`terminate` 混合批、`length` 截断、`prepareArguments`、`prepareNextTurn` 快照、`shouldStopAfterTurn` 跳过 follow-up）——那 18 个用例几乎就是本文 §1–§5 的可执行规格。

---

## 9. 一句话总结

`packages/agent` = **一个 1.6k LOC 的、与持久化无关的 agent loop 内核**（`types.ts` + `agent-loop.ts` + `agent.ts` + `stream-fn.ts`，含流式、顺序/并行工具、steering/follow-up、abort、turn 前后的可插拔决策点），外挂一个 **约 12k LOC 的可选持久化运行时（`harness/`）**，后者把同一套 loop 语义重写为“会话树 + 操作状态机 + intent/settlement”的可恢复实现。Python 移植应先逐字复刻内核的事件语义（尤其是 partial message 的下标替换、并行工具的 thunk/gather 顺序、`terminate` 的全真规则与 `length` 截断规则），再按需摘取 harness 中无存储依赖的部分（消息转换、压缩/分支摘要、技能与模板、内置工具、执行环境）。

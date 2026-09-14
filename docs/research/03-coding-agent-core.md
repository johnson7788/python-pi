# pi `coding-agent` 核心研究报告（面向 Python 移植）

研究对象：`/tmp/pi-ref/packages/coding-agent/src/core/**`（对应上游 `packages/coding-agent`）。
文档来源：源码精读 + `packages/coding-agent/docs/*.md`（`session-format.md`、`settings.md`、`compaction.md`、`extensions.md` 等）。
本文只描述 **core**：`AgentSession`、`SessionManager`、内置工具、`ResourceLoader`、system prompt、`SettingsManager`、model runtime，以及移植取舍。

> 注：任务描述里的 `src/tools/` 在仓库里实际是 **`src/core/tools/`**；`packages/agent/src/harness/tools/` 是更底层的 harness 版本（`read/bash/edit/write/image`），coding-agent 自己实现了一套更丰富的工具定义（`ToolDefinition`，带 `promptSnippet` / `promptGuidelines` / renderer）。移植应以 `core/tools` 为准。

---

## 1. AgentSession

### 1.1 职责

`packages/coding-agent/src/core/agent-session.ts`（3552 行）是所有运行模式（interactive / print / rpc）共享的核心抽象。它把下面这些东西粘在一起：

- **Agent 生命周期**：持有 `Agent` 实例（来自 `@earendil-works/pi-agent-core`），订阅其事件流；
- **事件订阅 + 自动持久化**：把 agent 的 `message_end` 事件写成 session entry；
- **模型与 thinking level 管理**：`setModel/cycleModel/setThinkingLevel`，并可持久化到 global settings；
- **Compaction**（手动 / 阈值 / overflow）与 **branch summarization**；
- **Bash 执行**（`!` 前缀命令）与其消息注入；
- **队列 / steering / follow-up**；
- **重试（auto retry，指数退避）**；
- **扩展系统**（`ExtensionRunner`，工具注册表、hook、command、resource 注入）；
- **session 切换与分支**（`navigateTree`、fork、`createReplacedSessionContext`）；
- **system prompt 的构造与刷新**。

它唯一的 I/O 出口是 `AgentSessionEvent` 事件流；模式层（TUI / print / RPC）在其上做渲染。

### 1.2 构造与依赖

```ts
export interface AgentSessionConfig {
  agent: Agent;
  sessionManager: SessionManager;
  settingsManager: SettingsManager;
  cwd: string;
  scopedModels?: Array<{ model: Model<any>; thinkingLevel?: ThinkingLevel }>;
  resourceLoader: ResourceLoader;
  customTools?: ToolDefinition[];
  modelRuntime: ModelRuntime;
  initialActiveToolNames?: string[];   // 默认 [read, bash, edit, write]
  allowedToolNames?: string[];
  excludedToolNames?: string[];
  baseToolsOverride?: Record<string, AgentTool>;
  extensionRunnerRef?: { current?: ExtensionRunner };
  sessionStartEvent?: SessionStartEvent;
}
```

`constructor` 做四件事：

1. `this.agent.subscribe(this._handleAgentEvent)` —— 内部持久化 / compaction / retry 全靠它；
2. `_installAgentToolHooks()` —— 给 `agent.beforeToolCall` / `agent.afterToolCall` 挂扩展 hook。`beforeToolCall` 返回 `block` 就阻止执行（这是唯一的"审批"挂点）；`afterToolCall` 会调用 `normalizeToolResultImages(content, { autoResizeImages })` 规范化扩展注入的图片；
3. `_installAgentNextTurnRefresh()` —— 包一层 `agent.prepareNextTurnWithContext`，在每次 LLM 调用前做 **阈值 compaction**，并把最新的 `systemPrompt` / `tools` 塞进 context；
4. `_buildRuntime(...)` —— 用 `createAllToolDefinitions(cwd, {...})` 建立 8 个内置工具（read/bash/powershell/edit/write/grep/find/ls），构造 `ExtensionRunner`，然后 `_refreshToolRegistry()` 决定 active tools。

关键内部状态（移植时必须保留语义）：

```ts
_isAgentRunActive            // isStreaming 的值
_steeringMessages: string[]  // 队列展示用（交付时按文本匹配移除）
_followUpMessages: string[]
_pendingNextTurnMessages: CustomMessage[]   // deliverAs:"nextTurn"，随下一条 user 消息注入
_pendingCustomMessages: CustomMessage[]     // 运行中排队，turn_end 后 flush
_compactionAbortController / _autoCompactionAbortController / _branchSummaryAbortController
_overflowRecoveryAttempted   // overflow 只允许一次 compact-and-retry
_retryAbortController / _retryAttempt
_bashAbortControllers: Set<AbortController> / _pendingBashMessages
_toolRegistry: Map<string, AgentTool>            // 实际给 agent 的对象
_toolDefinitions: Map<string, {definition, sourceInfo}>
_toolPromptSnippets: Map<string,string>          // 用于重建 system prompt
_toolPromptGuidelines: Map<string, string[]>
_baseSystemPrompt / _baseSystemPromptOptions / _systemPromptOverride
```

### 1.3 公共 API 全貌（分组）

**状态读取（getter）**

| 成员 | 说明 |
|---|---|
| `state: AgentState` | 直接透传 `agent.state` |
| `model: Model \| undefined` | `agent.state.model` |
| `thinkingLevel: ThinkingLevel` | |
| `isStreaming: boolean` | `_isAgentRunActive` |
| `isIdle: boolean` | `!_isAgentRunActive && !isCompacting` |
| `systemPrompt: string` | 当前生效 prompt（含每轮扩展修改） |
| `retryAttempt: number` | |
| `isCompacting: boolean` | 三个 abort controller 任一存在 |
| `messages: AgentMessage[]` | 含 bashExecution / custom 等自定义类型 |
| `steeringMode` / `followUpMode`: `"all" \| "one-at-a-time"` | |
| `sessionFile` / `sessionId` / `sessionName` | 转发 SessionManager |
| `scopedModels` / `setScopedModels()` | `--models` 的候选集 |
| `promptTemplates: readonly PromptTemplate[]` | 来自 resourceLoader |
| `resourceLoader` / `modelRuntime` / `extensionRunner` | |
| `autoCompactionEnabled` / `autoRetryEnabled` / `isRetrying` | |
| `isBashRunning` / `hasPendingBashMessages` / `pendingMessageCount` | |

**工具**

- `getActiveToolNames(): string[]`
- `getAllTools(): ToolInfo[]`（name/description/parameters/promptGuidelines/sourceInfo）
- `getToolDefinition(name): ToolDefinition | undefined`
- `setActiveToolsByName(toolNames)`：只接受注册表里存在的名字；**会重建 system prompt**（`_rebuildSystemPrompt`），改动下一轮生效。

**Prompt / 队列**

- `prompt(text, options?: PromptOptions): Promise<void>`
- `steer(text, images?, {source?})` / `followUp(text, images?, {source?})`
- `sendCustomMessage<T>(message, {triggerTurn?, deliverAs?: "steer"|"followUp"|"nextTurn"})`
- `sendUserMessage(content, {deliverAs?, expandPromptTemplates?})` —— 内部就是 `prompt()`，`source: "extension"`
- `clearQueue(): {steering: string[]; followUp: string[]}`
- `getSteeringMessages()` / `getFollowUpMessages()`

**运行控制**：`abort()`（`agent.abort()` 后 `await waitForIdle()`）、`waitForIdle()`（基于 `_idleWaitPromise` 的 Promise 门闩）、`subscribe(listener): () => void`、`dispose()`。

**模型 / thinking**：`setModel(model, {persist?})`、`cycleModel(direction, {persist?}): ModelCycleResult`、`setThinkingLevel(level, {persist?})`、`cycleThinkingLevel()`、`getAvailableThinkingLevels()`、`supportsThinking()`。

**Compaction / 分支**：`compact(customInstructions?): Promise<CompactionResult>`、`abortCompaction()`、`abortBranchSummary()`、`setAutoCompactionEnabled(bool)`。

**扩展**：`bindExtensions(bindings: ExtensionBindings)`、`reload({beforeSessionStart?})`、`hasExtensionHandlers(eventType)`。

**重试**：`abortRetry()`、`setAutoRetryEnabled(bool)`。

**Bash**：`executeBash(command, onChunk?, {excludeFromContext?, id?, operations?}): Promise<BashResult>`、`recordBashResult(command, result, {excludeFromContext?})`、`abortBash()`。

**会话**：`setSessionName(name)`、`navigateTree(targetId, {summarize?, customInstructions?, replaceInstructions?, label?})`、`getUserMessagesForForking()`、`getSessionStats(): SessionStats`、`getContextUsage(): ContextUsage | undefined`、`exportToHtml(outputPath?, {themeName?})`、`exportToJsonl(outputPath?)`、`getLastAssistantText()`、`createReplacedSessionContext()`。

**事件类型** `AgentSessionEvent`（供模式层消费）：

```
AgentEvent（去掉 agent_end） | {type:"agent_end"; messages; willRetry}
| {type:"agent_settled"}
| {type:"queue_update"; steering; followUp}
| {type:"compaction_start"; reason:"manual"|"threshold"|"overflow"}
| {type:"entry_appended"; entry: SessionEntry}
| {type:"session_info_changed"; name}
| {type:"thinking_level_changed"; level}
| {type:"compaction_end"; reason; result?; aborted; willRetry; errorMessage?}
| {type:"auto_retry_start"; attempt; maxAttempts; delayMs; errorMessage}
| {type:"auto_retry_end"; success; attempt; finalError?}
| {type:"summarization_retry_scheduled"| "summarization_retry_attempt_start" | "summarization_retry_finished"}
| {type:"bash_execution_update"; id?; delta}
```

### 1.4 `prompt()` 逐段精读（入口 → 完成）

`PromptOptions`：

```ts
{ expandPromptTemplates?: boolean (默认 true);
  images?: ImageContent[];
  streamingBehavior?: "steer" | "followUp";   // streaming 时必填
  source?: InputSource;                        // 默认 "interactive"
  preflightResult?: (success: boolean) => void }
```

流程（源码 1175–1326）：

1. **扩展命令优先**：若 `text.startsWith("/")` 且 `expandPromptTemplates`，调 `_tryExecuteExtensionCommand(text)`。命中则执行 `command.handler(args, ctx)` 并直接 `return`（`preflightResult(true)`），**不发 LLM**。命令执行抛错会被捕获、经 `extensionRunner.emitError({event:"command"})` 上报，但仍返回 `true`。
2. **compaction 冲突检查**：`_compactionAbortController !== undefined` 时抛 `"Cannot submit a prompt while compaction is in progress..."`。
3. **`input` 扩展事件**：`_runInputHandlers(text, images, source, streamingBehavior?)`。若扩展返回 `{action:"handled"}` 则 `return`（不发 LLM）；`{action:"transform"}` 则替换 text/images。
4. **模板展开**：先 `_expandSkillCommand()`（`/skill:<name> args` → 读取 `SKILL.md`、`stripFrontmatter`，包成
   `<skill name="..." location="...">\nReferences are relative to <baseDir>.\n\n<body>\n</skill>`，若有 args 则再拼 `\n\n<args>`），再 `expandPromptTemplate(text, promptTemplates)`（`/name args` → `substituteArgs`，支持 `$1 $@ $ARGUMENTS ${N:-default} ${@:N} ${@:N:L}`）。
5. **streaming 分支**：若 `isStreaming`：
   - 缺少 `streamingBehavior` → 抛错；
   - `"followUp"` → `_queueFollowUp()`；否则 `_queueSteer()`；随后 `preflightResult(true)` 并 `return`。
6. **非 streaming 正式路径**：
   - `_flushPendingBashMessages()` + `_flushPendingCustomMessages()`；
   - **校验 model**：无 model → `formatNoModelSelectedMessage()` 抛错；
   - **校验 auth**：`modelRuntime.hasConfiguredAuth(provider) || await checkAuth(provider) !== undefined`，否则 OAuth 错误信息或 `formatNoApiKeyFoundMessage()`；
   - **发送前 compaction**：`_findLastAssistantMessage()` 若存在则 `_checkCompaction(lastAssistant, /*skipAbortedCheck*/ false)`（覆盖被中断的响应）；
   - 组装 `messages`：`[{role:"user", content:[{type:"text",text:expandedText}, ...images], timestamp:Date.now()}]`，随后追加 `_pendingNextTurnMessages` 并清空；
   - **`before_agent_start` 扩展事件**：`emitBeforeAgentStart(expandedText, images, _baseSystemPrompt, _baseSystemPromptOptions)`。返回值可带 `messages`（追加为 `role:"custom"` 消息）与 `systemPrompt`（覆盖本轮 prompt；否则重置回 `_baseSystemPrompt`）。
   - 全过程包在 try/catch 中，失败 → `preflightResult(false)` 并 rethrow。
7. **运行**：`preflightResult(true)` → `await _runAgentPrompt(messages)`。

`_runAgentPrompt`：

```ts
this._isAgentRunActive = true;
try {
  await this.agent.prompt(messages);
  while (await this._handlePostAgentRun()) {
    await this.agent.continue();
  }
} finally {
  this._systemPromptOverride = undefined;
  this._flushPendingBashMessages();
  this._flushPendingCustomMessages();
  await this._emitAgentSettled();   // emits extension "agent_settled" + session event, 解开 idle 门闩
}
```

**`_handlePostAgentRun()` 是"运行后循环"的核心**（1116–1144），返回 `true` 表示需要再 `agent.continue()`：

1. 取 `_lastAssistantMessage`（`message_end` 时记录的）并清空；
2. `_isRetryableError(msg) && await _prepareRetry(msg)` → retry 分支，`return true`；
3. 若 `stopReason === "error"` 且 `_retryAttempt > 0` → 发 `auto_retry_end{success:false}`，计数归零；
4. `await _checkCompaction(msg)` → compaction 分支；
5. 兜底：`return this.agent.hasQueuedMessages()`（agent loop 已在 `agent_end` 前排空队列，这里剩下的通常是 `agent_end` 扩展 handler 新排的）。

**持久化**：不在 `prompt()` 里，而在 `_handleAgentEvent` 的 `message_end` 分支：

```ts
if (event.message.role === "custom")   sessionManager.appendCustomMessageEntry(customType, content, display, details);
else if (role === "user" | "assistant" | "toolResult") sessionManager.appendMessage(event.message);
// bashExecution / compactionSummary / branchSummary 在别处持久化
```

同时记录 `_lastAssistantMessage`、重置 `_overflowRecoveryAttempted`、以及在成功响应时清零 `_retryAttempt` 并发 `auto_retry_end{success:true}`。`turn_end` 时 `_flushPendingCustomMessages()`（保证不会插在 toolCall 和它的 toolResult 之间）。

**队列去重**：`message_start`（role=user）时按文本在 `_steeringMessages` / `_followUpMessages` 里 `indexOf` 删除并 `_emitQueueUpdate()`，保证 UI 看到的是"已投递"状态。这是"文本匹配"语义——Python 移植时如果保留，要接受重复文本会被误删的风险。

### 1.5 队列语义

- `steer()`：在当前 assistant 轮次执行完 tool calls 之后、下一次 LLM 调用之前投递 → `agent.steer(msg)`；
- `followUp()`：只有在 agent 没有更多 tool call / steering 消息时才投递 → `agent.followUp(msg)`；
- 二者都先做 `_throwIfExtensionCommand(text)`：`/` 开头的扩展命令**不能入队**，会抛错；
- `_queueUserInput()` 会重跑 input handler + skill/template 展开；
- 模式由 `setSteeringMode/setFollowUpMode` 写入 settings。

### 1.6 Compaction 触发（三种自动 + 一种手动）

`_checkCompaction(assistantMessage, skipAbortedCheck = true)`（2154–2258）：

前置早退条件：
- `settings.enabled === false` → false；
- `skipAbortedCheck && stopReason === "aborted"` → false；
- **模型一致性**：只有 `assistantMessage.provider/model` 与当前 model 相同才做 overflow 判定（切换到大上下文模型后，旧模型的 overflow 不应触发）；
- **陈旧边界**：`getLatestCompactionEntry(getBranch())` 存在且 `assistantMessage.timestamp <= compactionEntry.timestamp` → false（避免刚 compact 完就被旧的 usage 再次触发）。

三种情况：

1. **Case 1 — overflow 且可重试**：`isContextOverflow(msg, contextWindow)` 或 `isRecoverableLength(msg, model.maxTokens)`，且 `stopReason !== "stop"`。
   - 若 `_overflowRecoveryAttempted` 已为真 → 发 `compaction_end{reason:"overflow", errorMessage:"Context overflow recovery failed after one compact-and-retry attempt..."}` + `session_compact_failed`，返回 false；
   - 否则把最后一条 assistant 从 `agent.state.messages` 移除（session 里仍保留），置 `_overflowRecoveryAttempted = true`，`_runAutoCompaction("overflow", true)`。
2. **Case 2 — overflow 但响应已完成**（`stopReason === "stop"`）：compact，但不 retry（`agent.continue()` 无法从已完成的 assistant 继续）。
3. **Case 3 — 阈值**：`contextTokens > contextWindow - reserveTokens`（`shouldCompact`）。`contextTokens` 优先用 `calculateContextTokens(assistantMessage.usage)`；当 `stopReason==="error"` 或 usage 全 0 时改用 `estimateContextTokens(messages).tokens`（chars/4 启发式，图片按 4800 chars 计）。usage 来源若早于最近 compaction 边界则跳过。命中 → `_runAutoCompaction("threshold", false)`。

另外两条触发路径：
- **每轮 LLM 调用前**：`_compactBeforeNextAssistantResponse(context)`（挂在 `prepareNextTurnWithContext` 上），用 `estimateContextTokens(context.messages)` 判定阈值并 `_runAutoCompaction("threshold", false)`；
- **手动**：`compact(customInstructions?)`，先 `await abort()`，发 `compaction_start{reason:"manual"}`，永不 retry。

`_runAutoCompaction(reason, willRetry)`（2270–2453）步骤：
1. `_getSummarizationRequestAuth(model)` 取 `{model, apiKey, headers, env}`；
2. `prepareCompaction(sessionManager.getBranch(), settings)` —— 返回 undefined 就直接放弃（例如最后一条就是 compaction，或没有可总结的消息）；
3. 发 `compaction_start`，建 `_autoCompactionAbortController`；
4. `session_before_compact` 扩展 hook：可 `cancel`（发 aborted 事件）或直接提供 `compaction` 结果（`fromExtension = true`）；
5. 否则 `_runDefaultCompaction(...)` → 底层 `compact(...)`（共享同一份 summarization 逻辑，带 summarization retry callbacks）；
6. `sessionManager.appendCompaction(summary, firstKeptEntryId, tokensBefore, details, fromExtension, usage)`，然后用 `sessionManager.buildSessionContext().messages` **整体替换** `agent.state.messages`；
7. 发 `session_compact` 扩展事件 + `compaction_end`；
8. `willRetry` 时，如果重建后的最后一条是 `stopReason==="error"|"length"` 的 assistant，再移除一次（否则 `agent.continue()` 会拒绝）→ `return true`；否则 `return agent.hasQueuedMessages()`。

**CompactionSettings**（`compaction.ts`）：

```ts
{ enabled: true, reserveTokens: 16384, keepRecentTokens: 20000 }
```

`prepareCompaction()` 的关键算法：从最近的 compaction 边界开始，`findCutPoint()` 从最新往回累加 token 直到 `keepRecentTokens`，允许"切在 turn 中间"（`isSplitTurn`，需要额外生成 turn-prefix summary）。同时 `extractFileOperations()` 抽取 `{readFiles, modifiedFiles}` 放进 `details`。

Summarization prompt（`SUMMARIZATION_PROMPT`）要求 **固定格式**：`## Goal / ## Constraints & Preferences / ## Progress(Done/In Progress/Blocked) / ## Key Decisions / ## Next Steps / ## Critical Context`，并强调 "Preserve exact file paths, function names, and error messages"。二次压缩走 `UPDATE_SUMMARIZATION_PROMPT`（把 `previousSummary` 放进 `<previous-summary>`，规则是"保留旧信息、增量更新"）。`getSummarizationFailure()` 拒绝 `stopReason==="error"|"length"` 的结果（避免把不完整摘要写成 checkpoint）。

压缩后注入上下文的消息：`CompactionSummaryMessage`，前缀/后缀是

```
The conversation history before this point was compacted into the following summary:

<summary>
...summary...
</summary>
```

（分支摘要同理，前缀为 "The following is a summary of a branch that this conversation came back from:"）

### 1.7 Retry 逻辑

```ts
_isRetryableError(msg) =>
  !isContextOverflow(msg, model.contextWindow) && isRetryableAssistantError(msg)   // 后者来自 pi-ai

_prepareRetry(message) {
  settings = settingsManager.getRetrySettings();          // {enabled:true, maxRetries:3, baseDelayMs:2000, maxAgentDelayMs:60000}
  if (!settings.enabled) return false;
  this._retryAttempt++;
  if (this._retryAttempt > settings.maxRetries) { this._retryAttempt--; return false; }
  const delayMs = retryDelayMs(settings, this._retryAttempt);   // 指数退避，封顶 maxAgentDelayMs
  emit auto_retry_start{attempt, maxAttempts, delayMs, errorMessage}
  // 移除最后一条 assistant（保留在 session 历史里）
  this._retryAbortController = new AbortController();
  try { await sleep(delayMs, this._retryAbortController.signal); }
  catch { _retryAttempt = 0; emit auto_retry_end{success:false, finalError:"Retry cancelled"}; return false; }
  return true;
}
```

成功响应的处理在 `_handleAgentEvent` 的 `message_end`：只要 `stopReason !== "error"` 就发 `auto_retry_end{success:true, attempt}` 并把 `_retryAttempt` 归零（避免多轮 LLM 调用间累加）。`_willRetryAfterAgentEnd()` 用于给 `agent_end` 打上 `willRetry` 标记，让 UI 不把这次结束当成最终态。

compaction / branch summary 的 summarization 调用共用同一套 retry 预算，回调映射成 `summarization_retry_*` 事件（由 `_summarizationRetryCallbacks(source)` 生成）。

### 1.8 Bash 与树导航

- `executeBash()`：应用 `settings.shellCommandPrefix` 前缀，cwd 用 `sessionManager.getCwd()`，默认走 `createLocalBashOperations({ shellPath })`；`onChunk` 会同时转发 `bash_execution_update` 事件。结果经 `recordBashResult()` 变成 `BashExecutionMessage`：**streaming 时进 `_pendingBashMessages` 延后插入**（避免破坏 toolCall/toolResult 顺序），否则直接 push 到 `agent.state.messages` 并 `appendMessage` 持久化。
- `navigateTree(targetId, options)`：先 `collectEntriesForBranchSummary(sessionManager, oldLeafId, targetId)` 求旧叶到公共祖先的条目；`session_before_tree` hook 可 cancel 或提供 summary；否则 `generateBranchSummary(...)`（带 summarization retry）。目标若是 user/custom_message，新叶指向其 `parentId` 并返回 `editorText` 供编辑器回填；否则叶指向目标本身。有摘要 → `branchWithSummary(newLeafId, ...)`；无摘要且 `newLeafId===null` → `resetLeaf()`；否则 `branch(newLeafId)`。最后用 `buildSessionContext()` 重建 `agent.state.messages` 并发 `session_tree`。

---

## 2. SessionManager 与 session 文件格式

### 2.1 文件位置与命名

```
~/.pi/agent/sessions/--<encoded-cwd>--/<ISO-timestamp-with-:.-replaced>_<session-id>.jsonl
```

- `agentDir` 由 `getAgentDir()` 决定：`process.env.PI_CODING_AGENT_DIR`（常量名 `${APP_NAME.toUpperCase()}_CODING_AGENT_DIR`）或 `~/.pi/agent`；
- `CONFIG_DIR_NAME = pkg.piConfig?.configDir || ".pi"`；
- cwd 编码 `getDefaultSessionDirPath()`：`--${resolvedCwd.replace(/^[/\\]/,"").replace(/[/\\:]/g,"-")}--`；
- 文件名时间戳：`new Date().toISOString().replace(/[:.]/g,"-")` + `_` + `sessionId`（默认 `uuidv7()`，可用 `--session-id` 自定义，规则 `^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$`）；
- 每次 append 一行 `JSON.stringify(entry) + "\n"`（append-only，JSONL）。

### 2.2 Header（第一行）

```json
{"type":"session","version":3,"id":"uuid","timestamp":"2024-12-03T14:00:00.000Z","cwd":"/path/to/project"}
```

fork / 子会话会多一个字段：

```json
{"type":"session","version":3,"id":"uuid","timestamp":"...","cwd":"...","parentSession":"/path/to/original.jsonl"}
```

`CURRENT_SESSION_VERSION = 3`。header 不参与树，没有 `id/parentId` 之外的树语义（它自己带的是 session id）。

### 2.3 所有 entry 类型与精确 JSON 形状

公共基类：

```ts
interface SessionEntryBase { type: string; id: string; parentId: string | null; timestamp: string }
```

`id` 是 **8 位 hex**（`randomUUID().slice(0,8)`，冲突时最多重试 100 次后回退完整 UUID）。

| `type` | 字段 | 是否进 LLM 上下文 | JSON 示例 |
|---|---|---|---|
| `message` | `message: AgentMessage` | ✅ | `{"type":"message","id":"a1b2c3d4","parentId":null,"timestamp":"...","message":{"role":"user","content":"Hello","timestamp":1733234401000}}` |
| `model_change` | `provider`, `modelId` | ❌（只影响 context 的 model 字段） | `{"type":"model_change","id":"...","parentId":"...","timestamp":"...","provider":"openai","modelId":"gpt-4o"}` |
| `thinking_level_change` | `thinkingLevel` | ❌（只影响 context.thinkingLevel） | `{"type":"thinking_level_change","id":"...","parentId":"...","timestamp":"...","thinkingLevel":"high"}` |
| `compaction` | `summary`, `firstKeptEntryId`, `tokensBefore`, `details?`, `usage?`, `fromHook?` | ✅ 转成 `compactionSummary` | `{"type":"compaction","id":"...","parentId":"...","timestamp":"...","summary":"...","firstKeptEntryId":"c3d4e5f6","tokensBefore":50000}` |
| `branch_summary` | `fromId`, `summary`, `details?`, `usage?`, `fromHook?` | ✅ 转成 `branchSummary`（仅当 summary 非空） | `{"type":"branch_summary","id":"...","parentId":"a1b2c3d4","timestamp":"...","fromId":"f6g7h8i9","summary":"Branch explored approach A..."}` |
| `custom` | `customType`, `data?` | ❌（纯扩展状态） | `{"type":"custom","id":"...","parentId":"...","timestamp":"...","customType":"my-extension","data":{"count":42}}` |
| `custom_message` | `customType`, `content`, `display`, `details?` | ✅ 转成 `custom` 消息 | `{"type":"custom_message","id":"...","parentId":"...","timestamp":"...","customType":"my-extension","content":"Injected context...","display":true}` |
| `label` | `targetId`, `label: string \| undefined` | ❌ | `{"type":"label","id":"...","parentId":"...","timestamp":"...","targetId":"a1b2c3d4","label":"checkpoint-1"}` |
| `session_info` | `name?` | ❌ | `{"type":"session_info","id":"...","parentId":"...","timestamp":"...","name":"Refactor auth module"}` |

`AgentMessage` 联合（来自 pi-ai + coding-agent 扩展）：

```ts
type AgentMessage = UserMessage | AssistantMessage | ToolResultMessage
                 | BashExecutionMessage | CustomMessage | BranchSummaryMessage | CompactionSummaryMessage
```

- `UserMessage`: `{role:"user", content: string | (TextContent|ImageContent)[], timestamp: number}`
- `AssistantMessage`: `{role:"assistant", content:(TextContent|ThinkingContent|ToolCall)[], api, provider, model, responseModel?, responseId?, usage, stopReason: "pending"|"stop"|"length"|"toolUse"|"error"|"aborted"|"deferred", errorMessage?, timestamp}`（`pending` 只存在于流式事件，落盘前必被替换）
- `ToolResultMessage`: `{role:"toolResult", toolCallId, toolName, content:(TextContent|ImageContent)[], details?, usage?, addedToolNames?, isError, timestamp}`
- `BashExecutionMessage`（coding-agent 独有）: `{role:"bashExecution", command, output, exitCode, cancelled, truncated, fullOutputPath?, excludeFromContext?, timestamp}`（`excludeFromContext` 对应 `!!` 前缀命令）
- `CustomMessage`: `{role:"custom", customType, content, display, details?, timestamp}`
- `BranchSummaryMessage`: `{role:"branchSummary", summary, fromId, timestamp}`
- `CompactionSummaryMessage`: `{role:"compactionSummary", summary, tokensBefore, timestamp}`
- `Usage`: `{input, output, cacheRead, cacheWrite, cacheWrite1h?, reasoning?, totalTokens, cost:{input,output,cacheRead,cacheWrite,total}}`

### 2.4 树结构、leaf 与上下文重建

- **树**：每个 entry 有 `parentId`；`parentId === null` 是根。`getTree()` 返回 `SessionTreeNode[]`，孤儿（父不存在）也当根，children 按 timestamp 升序排序。
- **leaf**：`SessionManager.leafId`，每次 append 后推进到新 entry。`branch(id)` 把 leaf 指回旧 entry，从而形成新分支；`resetLeaf()` 把 leaf 设成 `null`，使下一条成为新根（用于重新编辑第一条 user 消息）。
- **`getBranch(fromId?)`**：从 leaf（或指定 entry）沿 `parentId` 回溯到根，反转成路径顺序。

**上下文重建**（`buildContextEntries` + `buildSessionContext`）：

1. `buildSessionPath(entries, leafId)` 求路径；`leafId === null` 返回 `[]`，`leafId === undefined` 时取数组最后一条；
2. 路径上若存在 compaction，取**最后一个**：`contextEntries = [compaction]`，再从路径开头到该 compaction 之前，**从 `firstKeptEntryId` 开始**保留，最后追加 compaction 之后的全部 entries。被摘要的旧条目被丢弃；
3. `sessionEntryToContextMessages(entry)` 做投影：
   - `message` → 原样（content 为 null/missing 时补 `[]`）
   - `custom_message` → `createCustomMessage(customType, content ?? [], display, details, timestamp)`
   - `branch_summary` 且 `summary` 非空 → `createBranchSummaryMessage(summary, fromId, timestamp)`
   - `compaction` → `createCompactionSummaryMessage(summary, tokensBefore, timestamp)`
   - 其它 → `[]`
4. `getSessionContextSettings(path)` 扫描整条路径（**注意：用的是完整 path 而不是 compaction 裁剪后的 contextEntries**），取最后一个 `thinking_level_change` 的 level 和最后一个 `model_change` / assistant 消息的 `{provider, modelId}`。

### 2.5 版本迁移

```ts
CURRENT_SESSION_VERSION = 3
migrateV1ToV2: 补 id/parentId 线性链；compaction.firstKeptEntryIndex -> firstKeptEntryId
migrateV2ToV3: message.role "hookMessage" -> "custom"
migrateToCurrentVersion(entries): 读 header.version（缺省按 1），逐级迁移，返回是否发生迁移
```

迁移在 `_loadEntries()` 中执行，发生迁移则立即 `_rewriteFile()` 全量重写。

### 2.6 SessionManager 公共 API

**静态构造**

- `create(cwd, sessionDir?, options?: {id?, parentSession?})` —— 新会话，默认目录 `~/.pi/agent/sessions/<encoded-cwd>/`
- `open(path, sessionDir?, cwdOverride?)` —— cwd 优先级 `cwdOverride` > header.cwd > `process.cwd()`；无 `sessionDir` 时用文件所在目录
- `continueRecent(cwd, sessionDir?)` —— `findMostRecentSession()`（按 mtime，必要时按 header.cwd 过滤）
- `inMemory(cwd?, options?, entries?)` —— 不落盘
- `forkFrom(sourcePath, targetCwd, sessionDir?, options?)` —— 新 header（`parentSession` = 源文件）+ 拷贝全部非 header entry
- `list(cwd, sessionDir?, onProgress?)` / `listAll(onProgress?)` / `listAll(sessionDir?, onProgress?)` → `SessionInfo[]`

**实例：追加（返回 entry id）**

`appendMessage(message)`、`appendThinkingLevelChange(level)`、`appendModelChange(provider, modelId)`、`appendCompaction(summary, firstKeptEntryId, tokensBefore, details?, fromHook?, usage?)`、`appendCustomEntry(customType, data?)`、`appendSessionInfo(name)`（会 `name.replace(/[\r\n]+/g," ").trim()`）、`appendCustomMessageEntry(customType, content, display, details?)`、`appendLabelChange(targetId, label)`。

> 注意：`appendMessage()` 明确**不允许**直接写 `CompactionSummaryMessage` / `BranchSummaryMessage` —— 它们必须是顶层 entry，通过 `appendCompaction()` / `branchWithSummary()` 写入。

**实例：树与上下文**

`getLeafId()`、`getLeafEntry()`、`getEntry(id)`、`getChildren(parentId)`、`getLabel(id)`、`getBranch(fromId?)`、`getTree()`、`branch(id)`、`resetLeaf()`、`branchWithSummary(branchFromId|null, summary, details?, fromHook?, usage?)`、`createBranchedSession(leafId)`、`buildContextEntries()`、`buildSessionContext()`、`getEntries()`、`getHeader()`、`getSessionName()`、`getCwd()`、`getSessionDir()`、`usesDefaultSessionDir()`、`getSessionId()`、`getSessionFile()`、`isPersisted()`、`setSessionFile(path)`、`newSession(options?)`。

**持久化细节（移植时必须照抄的怪癖）**

- `_persist(entry)` 有一条"**首个 assistant 消息之前不落盘**"的规则：如果 `fileEntries` 里还没有 assistant 消息，则不写文件（只把 `flushed = false`）；一旦第一条 assistant 到达，就用 `open(..., "wx")` **全量重写** `fileEntries` 并置 `flushed = true`，之后才 append。目的是避免空会话文件 / 重复 header。
- `loadEntriesFromFile()` 用 1MB 缓冲 + `StringDecoder` 流式按行解析，跳过坏行；**解析后校验首行必须是合法 header，否则整份文件视为无效返回 `[]`**；若文件结尾没有换行会补写一个 `\n`。
- `readSessionHeader()` 只读前 1MB（`MAX_SESSION_HEADER_SCAN_BYTES`），超限抛 `SessionHeaderScanLimitError`（`open()` 会回退到全量加载）。
- `_rewriteFile()` / `createBranchedSession()` 会重建整个文件；`createBranchedSession` 会把 `label` entry 从路径里剔除并重新串 parent 链，再把 label 重建到尾部（因为 label 是真实树节点，其子节点不能被孤立）。

---

## 3. 内置工具

工具统一以 `ToolDefinition` 形式定义（`core/tools/*.ts`），公共字段：

```ts
{ name, label, description, parameters: TypeBoxSchema,
  promptSnippet?: string, promptGuidelines?: string[],
  constrainedSampling?: { type: "json_schema", strict: "prefer" },
  execute(toolCallId, input, signal?, onUpdate?, ctx?: ExtensionContext),
  ...renderers }
```

`createAllToolDefinitions(cwd, options)` 产出 8 个工具：`read, bash, powershell, edit, write, grep, find, ls`。默认 active = `["read","bash","edit","write"]`。

**关于 `ask_question`**：**本版本没有这个内置工具**。它只出现在 `--exclude-tools ask_question` 的文档示例与回归测试里，作为"扩展/自定义工具名"的占位符。移植时不要实现它。

**关于审批**：core 里**没有内置的 per-tool 审批/权限系统**。仅有两条安全相关机制：
1. **project trust**（决定是否加载 `.pi` 下的 settings/extensions/resources）；
2. **扩展的 `tool_call` hook**：`agent.beforeToolCall` 可返回 `block` 阻止执行、或改写 `args`。`bash` 工具自身也不做命令白名单——它直接跑 shell。

### 3.1 `read`

- **schema**：`{ path: string, offset?: number, limit?: number }`，`offset` 为 **1-indexed 起始行**，`limit` 为最大行数。
- **description**：`Read the contents of a file. Supports text files and images (jpg, png, gif, webp, bmp)... output is truncated to 2000 lines or 50KB (whichever is hit first).`
- **promptSnippet**：`Read file contents`；**guideline**：`Use read to examine files instead of cat or sed.`
- **图片**：先 `detectSupportedImageMimeTypeFromFile()`，命中则 `processImage(buffer, mimeType, {autoResizeImages})`，成功返回 `[{type:"text",text:"Read image file [mime]\n<hints>"}, {type:"image",data,mimeType}]`；非 vision 模型会附加 `"[Current model does not support images. The image will be omitted from this request.]"`。
- **文本语义**：读全文 → `split("\n")` → `startLine = offset ? max(0, offset-1) : 0`；`offset` 越界抛 `Offset N is beyond end of file (M lines total)`。指定 `limit` 时先切 `slice(startLine, startLine+limit)`；随后一律走 `truncateHead()`。
- **输出后缀**（可操作提示）：
  - 首行就超 50KB：`[Line N is <size>, exceeds 50.0KB limit. Use bash: sed -n 'Np' <path> | head -c 51200]`
  - 截断：`[Showing lines A-B of T. Use offset=B+1 to continue.]` 或 `[Showing lines A-B of T (50.0KB limit). Use offset=... to continue.]`
  - 用户 `limit` 提前结束但文件还有内容：`[R lines left. Use offset=... to continue.]`（原文 `[N more lines in file. Use offset=X to continue.]`）
- **details**：`{ truncation?: TruncationResult }`
- **路径解析**：`resolveReadPathAsync()` 在 macOS 上会尝试 AM/PM 窄空格、NFD、弯引号等变体。

### 3.2 `bash`（与 `powershell` 共用 `createShellToolDefinition`）

- **schema**：`{ command: string, timeout?: number /*秒*/ }`。`timeout` 必须有限且 > 0；上限 `MAX_TIMEOUT_MS = 2_147_483_647`（`resolveTimeoutMs` 换算成 ms）。**默认无超时**。
- **description**：`Execute a bash command in the current working directory. Returns stdout and stderr. Output is truncated to last 2000 lines or 50KB (whichever is hit first). If truncated, full output is saved to a temp file. Optionally provide a timeout in seconds.`
- **promptSnippet**：`Execute bash commands (ls, grep, find, etc.)`；**guideline**：`You can inspect PI_* environment variables for current model and session details.`
- **执行**：`spawn(shell, args, {cwd, detached: process.platform !== "win32", env: getShellEnv(), stdio:[...,"pipe","pipe"], windowsHide:true})`；stdout+stderr 合并流入 `OutputAccumulator`；进程组用 `killProcessTree(pid)` 结束；`waitForChildProcess()` 避免被 detached 后代句柄挂住。
- **环境变量注入**（`resolveSpawnContext`）：先 `delete` `PI_SESSION_ID/PI_SESSION_FILE/PI_PROVIDER/PI_MODEL/PI_REASONING_LEVEL`，当 `exposeSessionEnvironment`（默认 true）且有 `ctx` 时写入 `PI_SESSION_ID`、`PI_SESSION_FILE`、`PI_PROVIDER`、`PI_MODEL`、`PI_REASONING_LEVEL`。可用 `spawnHook` 改 command/cwd/env，`commandPrefix` 预置（来自 settings `shellCommandPrefix`，比如 `shopt -s expand_aliases`）。
- **流式更新**：`onUpdate` 节流 `BASH_UPDATE_THROTTLE_MS`；每次发 `snapshot({persistIfTruncated:true})`。
- **输出 / 截断**：`truncateTail()`（保留**末尾** 2000 行 / 50KB）。超限则把完整输出写入 `tmpdir()/pi-bash-<16hex>.log`，正文追加：
  - `[Showing lines A-B of T. Full output: <path>]`
  - 或 `[Showing lines A-B of T (50.0KB limit). Full output: <path>]`
  - 最后一行被截断时：`[Showing last <size> of line N (line is <size>). Full output: <path>]`
  - 空输出用 `(no output)`
- **失败语义**：非 0 退出码**抛错**，消息为 `"<输出>\n\nCommand exited with code N"`；超时抛 `"<输出>\n\nCommand timed out after N seconds"`；中断抛 `"<输出>\n\nCommand aborted"`。也就是说工具失败是通过异常（→ `isError: true` 的 toolResult）表达，而不是正常返回。
- **details**：`{ truncation?, fullOutputPath? }`
- **powershell**：同一实现，`name:"powershell"`，`shellName:"PowerShell"`，`prompt: "PS>"`，并给命令前面加 `try { [Console]::OutputEncoding=[System.Text.Encoding]::UTF8 } catch {}`。

### 3.3 `edit`

- **schema**：

```ts
{ path: string,
  edits: Array<{ oldText: string, newText: string }> }
```

- **description**：精确文本替换；每个 `oldText` 必须匹配**原文**中唯一且不重叠的区域；相邻改动要合并成一个 edit。
- **guidelines**：`Use edit for precise changes (edits[].oldText must match exactly)`、多条编辑用一个 `edit` 调用、`oldText` 相对于原文件而非增量、`oldText` 尽量小但唯一。
- **兼容处理 `prepareArguments`**：有的模型把 `edits` 发成 JSON 字符串 / 单个对象；还会把 legacy 的顶层 `oldText`/`newText` 合并进 `edits`。
- **执行语义**（`applyEditsToNormalizedContent`，`edit-diff.ts`）：
  1. 读文件 → `splitBom()` 分离 BOM → `detectLineEnding()` 记录原始换行 → `normalizeToLF()`；
  2. 所有 `oldText/newText` 也 `normalizeToLF`；空 `oldText` 直接报错；
  3. `fuzzyFindText()` 先精确匹配，失败再做**模糊匹配**（`normalizeForFuzzyMatch`：NFKC、逐行去尾空白、弯引号→ASCII、各种破折号→`-`、特殊空格→普通空格）。只要有任一 edit 走了模糊匹配，最终结果会用 `applyReplacementsPreservingUnchangedLines()` 保留未改动行的**原始字节**（避免归一化污染整文件）；
  4. 每个 `oldText` 必须匹配唯一：`countOccurrences() > 1` → `getDuplicateError()`；
  5. 匹配后按位置排序，检测重叠：`previous.matchIndex + previous.matchLength > current.matchIndex` → 抛 `edits[i] and edits[j] overlap in <path>. Merge them into one edit or target disjoint regions.`；
  6. `baseContent === newContent` → `getNoChangeError()`；
  7. 写回时恢复 BOM 与原始换行（`restoreLineEndings`）。
  8. 全程包在 `withFileMutationQueue(absolutePath, ...)` 里做**按文件串行化**，并只在每个 `await` 之后检查 `signal.aborted`（不在 abort 事件里 reject，避免提前释放队列锁）。
- **输出**：`content = [{type:"text", text: "Successfully replaced N block(s) in <path>."}]`。
- **details**：`{ diff: string, patch: string, firstChangedLine?: number }`。`diff` 来自 `diff.diffLines()`，格式为带行号的 `+<newLineNo> text` / `-<oldLineNo> text`，上下文 4 行；`patch` 是标准 unified patch（`createTwoFilesPatch`，`context: 4`，仅文件头）。

### 3.4 `write`

- **schema**：`{ path: string, content: string }`
- **description**：`Write content to a file. Creates the file if it doesn't exist, overwrites if it does. Automatically creates parent directories.`
- **guideline**：`Use write only for new files or complete rewrites.`
- **执行**：`resolveToCwd()` → `mkdir(dirname, {recursive:true})` → `writeFile(utf-8)`；同样走 `withFileMutationQueue` 与 abort 检查；**无截断、无 diff**。
- **输出**：`Successfully wrote to <path>`；`details: undefined`。

### 3.5 `grep`

- **schema**：

```ts
{ pattern: string, path?: string, glob?: string,
  ignoreCase?: boolean, literal?: boolean,
  context?: number /*默认0*/, limit?: number /*默认100*/ }
```

- **description**：返回带文件路径与行号的匹配行，尊重 `.gitignore`；截断到 100 条匹配或 50KB；单行截断到 500 字符。
- **执行**：`ensureTool("rg")` 取得 ripgrep（**不在 PATH 时会去 GitHub 下载到 `~/.pi/agent/bin`**，见 §7）。参数：

```
rg --json --line-number --color=never --hidden [--ignore-case] [--fixed-strings] [--glob G] -- <pattern> <searchPath>
```

- 以 `--json` 流式解析：只读 `event.type === "match"`，记录 `data.path.text`、`data.line_number`、`data.lines.text`；
- 达到 `effectiveLimit = max(1, limit ?? 100)` 立即 `child.kill()`（退出码 1/被杀不算错）；
- `context > 0` 时**匹配完成后**再读文件补齐上下文行（维护 `fileCache`），匹配行格式 `path:LINE: text`，上下文行 `path-LINE- text`；
- 路径相对化：搜索目录时用相对路径（`\` → `/`），否则用 `basename`。
- **输出**：无匹配 → `"No matches found"`；否则拼接的行 + 通知后缀 `[<N> matches limit reached. Use limit=<2N> for more, or refine pattern. 50.0KB limit reached. Some lines truncated to 500 chars. Use read tool to see full lines]`。
- **details**：`{ truncation?, matchLimitReached?, linesTruncated? }`。
- `truncateHead(raw, { maxLines: Number.MAX_SAFE_INTEGER })` —— 只按字节截断。

### 3.6 `find`

- **schema**：`{ pattern: string, path?: string, limit?: number /*默认1000*/ }`
- **description**：按 glob 找文件，返回相对搜索目录的路径，尊重 `.gitignore`，截断 1000 条或 50KB。
- **执行**：`ensureTool("fd")`；参数 `fd --glob --color=never --hidden [--no-require-git] --max-results N -- <effectivePattern> <searchPath>`。
  - `--no-require-git` 只在**不在 git 仓库内**时加（从 searchPath 向上找 `.git`）；在仓库内保留 fd 的 git 感知行为；
  - pattern 含 `/` 时加 `--full-path`，并自动补 `**/` 前缀（`src/**/*.spec.ts` → `**/src/**/*.spec.ts`）；Windows 下 `/` 替换为 `[/\\]`；
  - 自定义 `FindOperations.glob()` 存在时优先走自定义实现（忽略 `**/node_modules/**`、`**/.git/**`）。
- **输出**：每行先 `replace(/\r$/,"").trim()` 再 `relativizeFindResultPath()` 归一化为 posix 相对路径；无结果 → `"No files found matching pattern"`；通知后缀为 `[1000 results limit reached. Use limit=2000 for more, or refine pattern. 50.0KB limit reached]`（`resultLimitReached = results.length >= effectiveLimit`）；fd 非 0 退出且**无输出**时才抛 `fd exited with code N` / stderr。
- **details**：`{ truncation?, resultLimitReached? }`。

### 3.7 `ls`

- **schema**：`{ path?: string, limit?: number /*默认500*/ }`
- **description**：列出目录内容，按字母序（大小写不敏感），目录带 `/` 后缀，**包含 dotfiles**，截断 500 项或 50KB。
- **执行**：`pathExists` → `stat().isDirectory()`（否则 `Not a directory: <p>`）→ `readdir` → `sort((a,b)=>a.toLowerCase().localeCompare(b.toLowerCase()))` → 逐项 `stat` 加 `/` 后缀（stat 失败则跳过该项）。
- **输出**：空目录 → `"(empty directory)"`；否则换行拼接 + `[500 entries limit reached. Use limit=1000 for more. 50.0KB limit reached]`。
- **details**：`{ truncation?, entryLimitReached? }`。

### 3.8 截断工具（`truncate.ts`）

```ts
DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024      // 50KB
GREP_MAX_LINE_LENGTH = 500
TruncationResult = { content, truncated, truncatedBy: "lines"|"bytes"|null,
  totalLines, totalBytes, outputLines, outputBytes,
  lastLinePartial, firstLineExceedsLimit, maxLines, maxBytes }
```

- `truncateHead()`：按行累加，**永不返回半行**；首行超字节上限时返回空内容 + `firstLineExceedsLimit=true`；
- `truncateTail()`：从尾部往前累加；独有的 `lastLinePartial=true` 分支——一行都放不下时用 `truncateStringToBytesFromEnd()` 从末尾切，并向前找 UTF-8 字符边界；
- `truncateLine(line, maxChars=500)`：超长 → `${line.slice(0,500)}... [truncated]`；
- `formatSize(bytes)`：`B` / `KB`（1 位小数）/ `MB`。

所有路径都经 `path-utils.ts`：`resolveToCwd()`（`~` 展开、`@` 前缀剥离、Unicode 空格归一化）。`read` 额外走 `resolveReadPathAsync()` 的 macOS 变体兜底。

---

## 4. ResourceLoader

`DefaultResourceLoader`（`core/resource-loader.ts`，1097 行）负责发现并加载 6 类资源。

### 4.1 接口

```ts
interface ResourceLoader {
  getExtensions(): LoadExtensionsResult;                       // {extensions, errors, runtime}
  getSkills(): { skills: Skill[]; diagnostics: ResourceDiagnostic[] };
  getPrompts(): { prompts: PromptTemplate[]; diagnostics: ResourceDiagnostic[] };
  getThemes(): { themes: Theme[]; diagnostics: ResourceDiagnostic[] };
  getAgentsFiles(): { agentsFiles: Array<{ path: string; content: string }> };
  getSystemPrompt(): string | undefined;
  getSystemPromptSource(): { path: string } | undefined;
  getAppendSystemPrompt(): string[];
  getAppendSystemPromptSources(): Array<{ path: string }>;
  extendResources(paths: ResourceExtensionPaths): void;        // 扩展注入 skill/prompt/theme 路径
  reload(options?: { resolveProjectTrust? }): Promise<void>;
}
```

`diagnostics.ts`：

```ts
interface ResourceCollision { resourceType: "extension"|"skill"|"prompt"|"theme"; name; winnerPath; loserPath; winnerSource?; loserSource? }
interface ResourceDiagnostic { type: "warning"|"error"|"collision"; message: string; path?: string; collision?: ResourceCollision }
```

### 4.2 目录布局与优先级

**用户级 `agentDir`（默认 `~/.pi/agent`，可由 `PI_CODING_AGENT_DIR` 覆盖）**

```
~/.pi/agent/
├── settings.json                 # 全局设置
├── models.json                   # 自定义 provider / model
├── models-store.json             # 目录缓存（离线可用）
├── auth.json                     # 凭据
├── trust.json                    # 项目信任决策
├── keybindings.json              # 键盘绑定
├── SYSTEM.md                     # 自定义 system prompt（替换默认）
├── APPEND_SYSTEM.md              # 追加到 system prompt
├── AGENTS.md / AGENTS.override.md / CLAUDE.md   # 全局上下文文件
├── extensions/                   # *.ts / */index.ts
├── skills/                       # <name>/SKILL.md 或根目录 *.md
├── prompts/                      # *.md 提示词模板
├── themes/                       # *.json 主题
├── sessions/--<encoded-cwd>--/*.jsonl
├── bin/                          # 托管二进制（fd、rg）
├── npm/  git/<host>/<path>/      # pi 包安装位置
└── pi-debug.log
```

**项目级 `<cwd>/.pi/`**（`CONFIG_DIR_NAME` 可被 package.json 的 `piConfig.configDir` 覆盖）

```
<project>/.pi/
├── settings.json
├── SYSTEM.md / APPEND_SYSTEM.md
├── extensions/  skills/  prompts/  themes/
└── git/<host>/<path>/
```

**额外：`.agents/skills/`**（Agent Skills 约定）——用户级 `$HOME/.agents/skills`；项目级从 cwd 逐级向上收集 `<dir>/.agents/skills`，直到 git 仓库根为止（`collectAncestorAgentsSkillDirs`）。项目级的这几个目录**只在项目被信任时加载**。

**发现顺序（`package-manager.ts` 的 `DefaultPackageManager.resolve()` 组装的顺序，即优先级从低到高、后者覆盖前者）**

| 资源 | 顺序 |
|---|---|
| extensions | `~/.pi/agent/extensions/` → `.pi/extensions/` → CLI `-e <path>`（`temporary` scope） |
| skills | `~/.agents/skills/` → `~/.pi/agent/skills/` → 项目祖先 `.agents/skills/` → `.pi/skills/` → CLI/additional paths |
| prompts | `~/.pi/agent/prompts/` → `.pi/prompts/` → CLI |
| themes | `~/.pi/agent/themes/` → `.pi/themes/` → CLI |

settings 里的 `extensions` / `skills` / `prompts` / `themes` 数组是**启用/禁用 overrides**（`isEnabledByOverrides`），不是追加列表。

**合并与去重**：`mergePaths()` 用 `canonicalizePath`（realpath）去重；skills/prompts/themes 按名字去重，冲突记 `collision` 诊断（**先到者胜**，`winnerPath` = 先加载的）。

**CLI 路径**：`additionalExtensionPaths` / `additionalSkillPaths` / `additionalPromptTemplatePaths` / `additionalThemePaths` 通过 `resolveExtensionSources(..., {temporary:true})` 解析，metadata 记为 `{source:"cli", scope:"temporary", origin:"top-level"}`。

### 4.3 各类资源的具体规则

**Extensions**：`extensions/*.ts` 与 `extensions/*/index.ts`；由 `loadExtensionsCached(paths, cwd, eventBus)` 加载（Bun/Node 动态 import），另有 inline factories（`<inline:name>`）。冲突（同名 tool / flag）记为 error 诊断但不阻止加载。加载顺序决定优先级。

**Skills（`core/skills.ts`）**：

- 发现规则（`loadSkillsFromDirInternal`）：
  1. 若目录直接含 `SKILL.md` → 把它当 skill root，**不再向下递归**；
  2. 否则遍历子目录，递归找 `SKILL.md`；
  3. 递归中的**根目录**还会把直接子文件里的 `*.md` 当 skill（`includeRootFiles=true`）；
  4. 跳过点开头项、`node_modules`；支持软链接；支持 `.gitignore` / `.ignore` / `.fdignore`（`ignore` 包，前缀化规则）。
- `Skill` 结构：`{ name, description, filePath, baseDir, sourceInfo, disableModelInvocation }`；
- frontmatter（`SkillFrontmatter`）：`name?`、`description?`（**必填**，否则不加载）、`disable-model-invocation?: boolean`；
- 校验：name ≤64，`^[a-z0-9-]+$`，不以 `-` 开头/结尾，不含 `--`；description ≤1024。**校验失败只记 warning，仍加载**（除非缺 description）；name 缺省时取 SKILL.md 的父目录名。
- 去重：按 `canonicalizePath(filePath)` 跳过重复文件；同名冲突记 `collision`。

**Prompts**：`~/.pi/agent/prompts/*.md` + `.pi/prompts/*.md`；`PromptTemplate = { name, description, argumentHint?, content, sourceInfo, filePath }`；名字 = 文件名去 `.md`；description 取 frontmatter 或第一行（截断 60 字符）；`/name args` 触发 `substituteArgs`（支持 `$1 $2 $@ $ARGUMENTS ${N:-def} ${@:-def} ${@:N} ${@:N:L}`）。

**Themes**：`themes/*.json`；`loadThemeFromFile()` 解析；同名去重。

**Context files（AGENTS.md）**：`loadProjectContextFiles({cwd, agentDir})`：

1. 先收 `agentDir` 下的候选文件；
2. 从 `cwd` 逐级向上到根，收集每层的候选文件，**unshift 到前面**（所以最终顺序是"全局 → 最外层祖先 → … → cwd 最近"）；
3. 候选文件名顺序：`AGENTS.override.md` → `AGENTS.md` → `AGENTS.MD` → `CLAUDE.md` → `CLAUDE.MD`（每层取第一个命中的）；
4. 用 `seenPaths` 去重；还会用 git worktree 信息跳过被 shadow 的主仓库 context file（`findShadowedContextFile`）。

**SYSTEM.md / APPEND_SYSTEM.md 发现**（`discoverSystemPromptFile` / `discoverAppendSystemPromptFile`）：项目 `cwd/.pi/SYSTEM.md`（**仅当项目被信任**）优先，否则 `agentDir/SYSTEM.md`；`APPEND_SYSTEM.md` 同理。

### 4.4 项目信任与两阶段加载

`reload({ resolveProjectTrust })`：

1. `loadProjectTrustExtensions()`：先 `settingsManager.setProjectTrusted(false)` + `reload()`，做一次**只看 global/CLI** 的扩展加载，产生 `preTrustExtensions` —— 这是为了在信任决策前不执行项目代码；
2. `resolveProjectTrust({extensionsResult: preTrustExtensions})` 决定是否信任（见 §6.4）；
3. `setProjectTrusted(trusted)` + `reload()` 正式加载；`loadFinalExtensionSet()` 复用预加载结果，只加载新增路径（`preloadedByPath` / `failedPreloadPaths` 去重），并把 inline extensions 排到最后。

### 4.5 Skills 如何暴露给模型

Skill 内容**不会**全部塞进 system prompt。`formatSkillsForPrompt(skills, fileReadTool)` 只列出元数据（XML，符合 Agent Skills 规范），并排除 `disableModelInvocation=true` 的 skill：

```
The following skills provide specialized instructions for specific tasks.
Use the read tool to load a skill's file when the task matches its description.
When a skill file references a relative path, resolve it against the skill directory (parent of SKILL.md / dirname of the path) and use that absolute path in tool commands.

<available_skills>
  <skill>
    <name>foo</name>
    <description>...</description>
    <location>/abs/path/to/foo/SKILL.md</location>
  </skill>
</available_skills>
```

模型需要时用 `read` 打开 `SKILL.md`。另外用户可以显式 `/<skill:name> args`（或 `enableSkillCommands` 开启时），`_expandSkillCommand()` 直接把 skill 正文注入为 `<skill name="..." location="...">...</skill>` 块。`fileReadTool` 的取值：从 active tools 里按 `["read","bash"]` 顺序取第一个可用的。

---

## 5. System prompt 组装

### 5.1 入口

`AgentSession._rebuildSystemPrompt(toolNames)` → `buildSystemPrompt(options)`（`core/system-prompt.ts`，168 行）。

```ts
export interface BuildSystemPromptOptions {
  customPrompt?: string;                 // 来自 resourceLoader.getSystemPrompt()（SYSTEM.md）
  selectedTools?: string[];              // 默认 ["read","bash","edit","write"]
  toolSnippets?: Record<string, string>; // 只有提供了 snippet 的工具才会出现在 Available tools
  promptGuidelines?: string[];           // 各工具的 guidelines 汇聚
  appendSystemPrompt?: string;           // APPEND_SYSTEM.md 拼接
  cwd: string;
  contextFiles?: Array<{ path: string; content: string }>;
  skills?: Skill[];
}
```

`_rebuildSystemPrompt` 的组装顺序：
1. 过滤掉不在 `_toolRegistry` 里的工具名；
2. 从 `_toolPromptSnippets` / `_toolPromptGuidelines` 收集 `toolSnippets` 与 `promptGuidelines`；
3. `loaderAppendSystemPrompt.join("\n\n")`；
4. 传入 `skills` 与 `agentsFiles`；
5. 存下 `_baseSystemPromptOptions`（后续 `before_agent_start` 事件要用）并返回 `buildSystemPrompt(options)`。

### 5.2 默认 prompt 的完整结构

无 `customPrompt` 时：

```
You are an expert coding assistant operating inside pi, a coding agent harness. You help users by reading files, executing commands, editing code, and writing new files.

Available tools:
<每行 "- <name>: <snippet>"；没有任何可见工具时为 "(none)">

In addition to the tools above, you may have access to other custom tools depending on the project.

Guidelines:
<去重后的 bullet 列表>

Pi documentation (read only when the user asks about pi itself, its SDK, extensions, themes, skills, or TUI):
- Main documentation: <getReadmePath()>
- Additional docs: <getDocsPath()>
- Examples: <getExamplesPath()> (extensions, custom tools, SDK)
- When reading pi docs or examples, resolve docs/... under Additional docs and examples/... under Examples, not the current working directory
- When asked about: extensions (docs/extensions.md, examples/extensions/), themes (docs/themes.md), skills (docs/skills.md), prompt templates (docs/prompt-templates.md), TUI components (docs/tui.md), keybindings (docs/keybindings.md), SDK integrations (docs/sdk.md), custom providers (docs/custom-provider.md), adding models (docs/models.md), pi packages (docs/packages.md), environment variables (docs/environment-variables.md)
- When working on pi topics, read the docs and examples, and follow .md cross-references before implementing
- Always read pi .md files completely and follow links to related docs (e.g., tui.md for TUI API details)

<appendSystemPrompt>

<project_context>

Project-specific instructions and guidelines:

<project_instructions path="/abs/path/AGENTS.md">
...文件原文...
</project_instructions>

</project_context>

<skills 的 <available_skills> 块（见 §4.5）>

Current working directory: <cwd with \ -> />
```

有 `customPrompt`（SYSTEM.md）时是**替换**而非追加：`customPrompt` + appendSection + `<project_context>` + skills + `\nCurrent working directory: ...`。也就是说自定义 prompt 会丢掉默认的工具列表/Guidelines/文档段落。

### 5.3 动态 section

| section | 来源 | 何时变化 |
|---|---|---|
| `Available tools` | `selectedTools` ∩ 有 `promptSnippet` 的工具 | `setActiveToolsByName()` / 扩展改工具集 |
| `Guidelines` | 内置条件规则 + 各工具 `promptGuidelines` + 固定两条 | 同上 |
| 内置条件规则 | 当有 `bash`/`powershell` 但**没有** `grep`/`find`/`ls` 时加 `Use bash for file operations like ls, rg, find`（或 PowerShell 版本） | 工具集 |
| 固定 Guidelines | `"Be concise in your responses"`、`"Show file paths clearly when working with files"`（用 Set 去重，永远在最后追加） | 恒定 |
| `Pi documentation` 段 | `config.ts` 的 `getReadmePath()/getDocsPath()/getExamplesPath()` | 安装路径 |
| `appendSystemPrompt` | `APPEND_SYSTEM.md` 或 `--append-system-prompt` | 启动时加载 |
| `<project_context>` | `loadProjectContextFiles()` | 启动时加载 |
| `<available_skills>` | `resourceLoader.getSkills().skills` | 资源 reload |
| cwd 行 | `cwd.replace(/\\/g,"/")` | session cwd |

### 5.4 每轮的 prompt 覆盖

- 构造完 `_baseSystemPrompt` 后写入 `agent.state.systemPrompt`；
- `prepareNextTurnWithContext` 每轮把 `systemPrompt: this._systemPromptOverride ?? this._baseSystemPrompt` 塞进 context；
- `before_agent_start` 扩展事件可以返回 `systemPrompt`，`prompt()` 把它存进 `_systemPromptOverride` 并写入 `agent.state.systemPrompt`；下一轮 prompt 时若扩展不再返回就重置回 base。

### 5.5 工具描述如何注入模型

两条并行通道：
1. **`parameters`（JSON Schema）** 走 native tool/function calling —— 由 agent-core 组装请求；
2. **`description` / `promptSnippet` / `promptGuidelines`** 走 system prompt 文本：
   - `promptSnippet` → `Available tools` 的一行摘要；
   - `promptGuidelines` → `Guidelines` 的 bullet；
   - `description` **不进入 system prompt**，只作为 tool schema 的 description 发给 provider。

`promptSnippet` 会被 `_normalizePromptSnippet()` 压成单行（换行→空格、多空白合并）。`setActiveToolsByName()` 每次都会重建 prompt，所以工具集和 prompt 永远一致。

---

## 6. Settings

### 6.1 文件与优先级

| 位置 | 作用域 |
|---|---|
| `~/.pi/agent/settings.json` | 全局 |
| `<cwd>/.pi/settings.json` | 项目（需项目被信任才读） |

有效值优先级（高 → 低）：**CLI 运行期参数**（`--model/--provider/--thinking/--models/--theme/--session-dir/--approve/--tools/--exclude-tools/--api-key`）> **环境变量**（`PI_CODING_AGENT_DIR`、`PI_CODING_AGENT_SESSION_DIR`、`HTTP_PROXY/HTTPS_PROXY`、`VISUAL`/`EDITOR`）> **项目 settings** > **全局 settings**。

合并：`this.settings = deepMergeSettings(globalSettings, projectSettings)`；`deepMergeObjects` 只对**双方都是非数组对象**递归，数组和 null 直接整体替换；`undefined` 的 override 键会被跳过（不会抹掉 base）。

### 6.2 Schema（`Settings` 接口，节选完整字段）

```ts
interface Settings {
  // 模型 / thinking
  lastChangelogVersion?: string;
  defaultProvider?: string;
  defaultModel?: string;
  defaultThinkingLevel?: ThinkingLevel;
  modelThinkingLevels?: Record<string, ThinkingLevel>;   // key = "provider/modelId"
  transport?: "sse" | "websocket" | "websocket-cached" | "auto";   // 默认 "auto"
  hideThinkingBlock?: boolean;                            // 默认 false
  showCacheMissNotices?: boolean;                         // 默认 false
  thinkingBudgets?: { minimal?; low?; medium?; high? };   // number
  // 队列
  steeringMode?: "all" | "one-at-a-time";                 // 默认 "one-at-a-time"
  followUpMode?: "all" | "one-at-a-time";                 // 默认 "one-at-a-time"
  // 压缩 / 重试
  compaction?: { enabled?: boolean /*true*/; reserveTokens?: number /*16384*/;
                 keepRecentTokens?: number /*20000*/;
                 modelOverrides?: Record<string, { reserveTokens?; keepRecentTokens? }> };
  branchSummary?: { reserveTokens?: number /*16384*/; skipPrompt?: boolean /*false*/ };
  retry?: { enabled?: boolean /*true*/; maxRetries?: number /*3*/; baseDelayMs?: number /*2000*/;
            maxAgentDelayMs?: number /*60000*/;
            provider?: { timeoutMs?: number; maxRetries?: number; maxRetryDelayMs?: number /*60000*/ } };
  // 工具 / 资源
  defaultTools?: string[];                                // 只读，无 setter
  enabledModels?: string[];
  enableSkillCommands?: boolean;                          // 默认 true
  packages?: Array<string | { source: string; autoload?; extensions?; skills?; prompts?; themes? }>;
  extensions?: string[];  skills?: string[];  prompts?: string[];  themes?: string[];
  // shell
  shellPath?: string;
  shellCommandPrefix?: string;
  npmCommand?: string[];
  // 界面 / 终端
  theme?: string;                                         // 默认 "dark"
  externalEditor?: string;
  quietStartup?: boolean;  collapseChangelog?: boolean;
  defaultProjectTrust?: "ask" | "always" | "never";       // 默认 "ask"（仅 global 生效）
  doubleEscapeAction?: "fork" | "tree" | "none";          // 默认 "tree"
  treeFilterMode?: "default" | "no-tools" | "user-only" | "labeled-only" | "all";
  editorPaddingX?: number;   // 0..3，默认 0
  outputPad?: 0 | 1;         // 默认 1
  autocompleteMaxVisible?: number;  // 3..20，默认 5
  showHardwareCursor?: boolean;
  terminal?: { showImages?: boolean /*true*/; imageWidthCells?: number /*60*/; clearOnShrink?: boolean /*false*/;
               showTerminalProgress?: boolean /*false*/; hyperlinks?: boolean | "auto";
               images?: "kitty" | "iterm2" | "auto" | false; trueColor?: boolean | "auto" };
  images?: { autoResize?: boolean /*true, 2000x2000*/; blockImages?: boolean /*false*/ };
  markdown?: { codeBlockIndent?: string /*"  "*/; mermaid?: "off" | "final" | "streaming" /*streaming*/ };
  warnings?: { anthropicExtraUsage?: boolean /*true*/ };
  tuiMode?: "regular" | "fullscreen";                     // 默认 "regular"
  fullscreenExitOutput?: "transcript" | "resume-hint";    // 默认 "transcript"
  fullscreenScrollbar?: "auto" | "always" | "hidden";     // 默认 "auto"
  fullscreenCopyOnSelect?: boolean;                       // 默认 true
  // 会话 / 网络 / 遥测
  sessionDir?: string;
  httpProxy?: string;
  httpIdleTimeoutMs?: number;                             // 默认 300000，0 = 关闭
  websocketConnectTimeoutMs?: number;                     // 0 = 关闭
  enableInstallTelemetry?: boolean;                       // 默认 true
  enableAnalytics?: boolean;                              // 默认 false
  trackingId?: string;
}
```

**没有 `experimental` 设置组**，实验特性由环境变量 `PI_EXPERIMENTAL=1` 控制。

字段级迁移（读时对 global/project 都跑）：`queueMode`→`steeringMode`；`websockets: boolean`→`transport: "websocket"|"sse"`；旧 `skills` 对象→顶层 `enableSkillCommands` + `skills: string[]`；`retry.maxDelayMs`→`retry.provider.maxRetryDelayMs`。

### 6.3 SettingsManager API 与写回语义

- **无订阅/观察者 API**。getter 每次现读 `this.settings`；变更传播靠 `reload()`。错误通过 `drainErrors(): SettingsError[]` 拉取。约 70 对具名 getter/setter（`getDefaultModel/setDefaultModel`、`getCompactionSettings(model?): {enabled,reserveTokens,keepRecentTokens}`、`getRetrySettings(): {enabled,maxRetries,baseDelayMs,maxAgentDelayMs}`、`getShellCommandPrefix/getShellPath`、`getImageAutoResize/getBlockImages`、`getSteeringMode/getFollowUpMode`、`getSessionDir` …）。
- `static create(cwd, agentDir?, {projectTrusted?})`、`static fromStorage(storage)`、`static inMemory(settings?)`。
- **写回是按字段局部合并**：`persistScopedSettings()` 会重新读磁盘文件，只覆盖本会话修改过的顶层字段（以及嵌套字段子键），`JSON.stringify(merged, null, 2)`；用 `proper-lockfile` 加锁（10 次 × 20ms 重试 `ELOCKED`）；写操作串行化在 `writeQueue` 上，**异常只记入 errors 不冒泡**；若某 scope 加载时出错，则**拒绝写该 scope**（防止用内存态覆盖坏文件）。
- `setXxx()` 写全局；`setProjectXxx()` 写 `.pi/settings.json`，且未信任项目时抛 `"Project is not trusted; refusing to write project settings"`。
- 无效值：**文件级**（JSON.parse 或迁移失败）→ 该 scope 视为 `{}` 并记录错误、禁用写盘；**字段级** → 大多是静默 clamp/回落（`editorPaddingX` 0..3、`autocompleteMaxVisible` 3..20、`outputPad` 只认 0、`tuiMode` 只认 `"fullscreen"`、`treeFilterMode`/`fullscreenScrollbar`/`mermaid`/`defaultProjectTrust` 白名单），只有 `compaction.reserveTokens|keepRecentTokens`、`compaction.modelOverrides[*]`、`httpIdleTimeoutMs`、`websocketConnectTimeoutMs` 会**抛错**。

### 6.4 项目信任（trust）

- 存储：`~/.pi/agent/trust.json`，`Record<canonicalPath, true|false|null>`。
- 触发信任的资源（`TRUST_REQUIRING_PROJECT_CONFIG_RESOURCES`）：`.pi/{settings.json, extensions, skills, prompts, themes, SYSTEM.md, APPEND_SYSTEM.md}` 任一存在，或 cwd/祖先的 `.agents/skills`（`$HOME/.agents/skills` 视为用户级、永远可信）。
- 决策顺序（`resolveProjectTrusted`）：CLI `--approve/--no-approve`（`projectTrustOverride`）→ 无信任相关资源则 `true` → `project_trust` 扩展事件（可 `remember` 落盘）→ `trust.json` 最近祖先决策 → `defaultProjectTrust`（`always`/`never`/`ask`）→ 无 UI 则 `false` → UI 选择。
- 非交互模式（`-p`、`--mode json`、`--mode rpc`）**不弹信任提示**，直接按 `defaultProjectTrust` 走。

### 6.5 session cwd 校验

`session-cwd.ts` 不存状态，只做"会话记录里的 cwd 已不存在"的检查：
`getMissingSessionCwdIssue(sessionManager, fallbackCwd)`、`formatMissingSessionCwdError/formatMissingSessionCwdPrompt`、`assertSessionCwdExists()`、`MissingSessionCwdError`。

---

## 7. Model 层（`model-runtime` / `model-resolver` / `auth-storage`）

### 7.1 三层结构

真正的 provider 集合实现全在 pi-ai 的 `createModels(options?)`（`ModelsImpl implements MutableModels`）。coding-agent 只做外壳：

- `Models`（pi-ai）—— provider 集合 + auth 解析 + stream 分发；
- `ModelRuntime`（`core/model-runtime.ts`）—— 内置 catalog + `models.json` + 扩展注册 + 可用性快照 + 凭证同步；
- `ModelRegistry`（`core/model-registry.ts`）—— 给扩展用的**同步 getter 门面**，内部全部委托 `ModelRuntime`。

### 7.2 `ModelRuntime`

`class ModelRuntime implements Models`（私有构造，只有 `static create`）：

```ts
static async create(options: CreateModelRuntimeOptions = {}): Promise<ModelRuntime>
// { credentials?: CredentialStore; authPath?: string; modelsPath?: string | null;
//   modelsStore?: ModelsStore; modelsStorePath?: string; allowModelNetwork?: boolean;
//   modelRefreshTimeoutMs?: number; catalogBaseUrl?: string; signal?: AbortSignal;
//   refreshOnCreate?: boolean }
```

默认值链：`credentials = new RuntimeCredentials(authPath ? AuthStorage : ...)` → `modelsPath ?? join(getAgentDir(), "models.json")` → `ModelConfig.load(modelsPath)` → `modelsStore ?? FileModelsStore(join(dirname(modelsPath), "models-store.json"))` → 内置 catalog（每个 provider 除 `radius` 外都包一层 `withRemoteCatalog`）→ `modelNetworkEnabled = process.env.PI_OFFLINE === undefined` → `refreshOnCreate !== false` 时 `refresh({allowNetwork: modelNetworkEnabled && options.allowModelNetwork === true, signal})`。

`agent-session-services.ts` 里的实际构造：

```ts
ModelRuntime.create({ authPath: join(agentDir, "auth.json"),
                      modelsPath: join(agentDir, "models.json"),
                      signal });
```

公开方法（精确签名）：

```
getProviders() / getProvider(id) / getModels(providerId?) / getModel(providerId, modelId)
checkAuth(providerId, options?) / getAvailable(providerId?, options?) / getAvailableSnapshot()
getError() / getRegisteredProviderConfig(id) / getRegisteredProviderIds() / getRegisteredNativeProvider(id)
getCompatibilityRequestConfig(model) / isUsingOAuth(id) / isUsingSubscription(id) / hasConfiguredAuth(id)
getAuth(providerId | model, overrides?) / setRuntimeApiKey(id, key) / removeRuntimeApiKey(id)
listCredentials() / getProviderAuthStatus(id) / login(id, type, interaction) / logout(id)
stream(model, context, options?) / complete(...) / streamSimple(model, context, options?) / completeSimple(...)
streamDeferred(model, handle, options?) / fetchDeferred(...) / cancelDeferred(...)
refresh(options?: ModelsRefreshOptions) / registerNativeProvider(provider) / registerProvider(id, config) / unregisterProvider(id)
```

`ModelsRefreshResult = { aborted: boolean; errors: ReadonlyMap<string, Error> }`。`ModelRuntimeSnapshot = { all, available, configuredProviders, storedProviders, auth }`。

**模型 → provider 映射没有反向查表**：`Model.provider` 自带 provider id，一律 `models.getProvider(model.provider)`；错误统一用 `ModelsError(code: "model_source"|"model_validation"|"provider"|"stream"|"auth"|"oauth")`。

### 7.3 streaming 契约（Python 移植最关键的一点）

```ts
stream/streamSimple(model, context, options) {
  return lazyStream(model, async () => {
    const prepared = await this.prepareRequest(model, options);
    return prepared.provider.streamSimple(prepared.model, context, prepared.options);
  });
}
```

`lazyStream()` **同步返回** `AssistantMessageEventStream`，把异步 auth 解析 / 动态 import 藏在后面；setup 抛错时不 throw，而是 `push({type:"error", reason:"error", error})` 再 `end(message)`（一个 `stopReason:"error"` 的 `AssistantMessage`）。**"同步拿流、错误走流"** 是必须复刻的契约，否则上层 `streamSimple(...).result()` 的语义会变。`complete*` 就是 `stream*(...).result()`。

`prepareRequest()` 做：`getAuth(model, {apiKey, env, signal})` → 用 `resolution.auth.baseUrl` 覆盖 model → 拆出并执行 `transformHeaders` → `mergeHeaders` 合并 headers → `env = {...resolution.env, ...providerOptions.env}` → `apiKey = providerOptions.apiKey ?? resolution.auth.apiKey`。

`streamSimple` 是 **provider 中立**入口（只有 `reasoning` 这类统一参数），对照 `stream` 的 API 专属 `ApiStreamOptions<TApi>`。`sdk.ts` 把它包进 `agent.streamFn`，在那里叠加 `providerRetrySettings.timeoutMs` / `maxRetries` / `maxRetryDelayMs`、`websocketConnectTimeoutMs`、provider attribution headers 与 `before_provider_headers` 扩展 hook。`AgentSession` 自己用 `_modelRuntime.getAuth(model)` 拿 `{apiKey, headers, baseUrl}` 用于 summarization / compaction 调用。

**ModelRuntime 自身不做重试**，只做取消（`operationSignal`、`raceWithAbortSignal`）。可用性刷新用**序号作废**（`availabilityRefreshSeq` / `providerAvailabilitySeq`：结果落地前比序号，避免旧结果覆盖新状态）；凭证操作按 provider **串行化**（`credentialOperations` 链式 promise），然后 `synchronizeCredentialState()` = recompose provider → `refresh({allowNetwork:false, providers:[id]})` → 更新快照 → 刷新可用性，失败包成 `CredentialSynchronizationError`。

### 7.4 provider 组合优先级（低 → 高）

1. **内置 catalog**（`builtinProviders()`，随包发布的生成数据）；
2. **远端目录缓存**（`models-store.json`，`withRemoteCatalog`，默认 `catalogBaseUrl = "https://pi.dev"`，刷新间隔 4h，单次超时 4s，带 `etag`/`lastModified`）；
3. **`models.json` provider 层**（`baseUrl` / `headers` / `api` / `compat` / `apiKey` / `oauth:"radius"` / `authHeader`）；
4. **`models.json` 自定义 `models`**（按 `id` upsert；缺省 `name=id`、`reasoning=false`、`input=["text"]`、`cost` 全 0、`contextWindow=128000`、`maxTokens=16384`；缺 `api` / `baseUrl` 会报错）；
5. **扩展注册的 provider**（`ProviderConfigInput`；给了 `models` 就**整体替换**列表，否则只用 `baseUrl` 覆盖）；
6. **`modelOverrides`**（最后统一 patch；`cost` 逐字段回落、`samplingParams` 逐键合并、`compat` 浅合并 + 四个嵌套对象特判）。

`available` 只是 `all.filter(m => configuredProviders.has(m.provider))`。

### 7.5 配置文件与 JSON 形状

**`~/.pi/agent/models.json`**（支持 **JSONC**：`//` 注释 + 尾逗号；`id` 是唯一必填字段）：

```json
{
  "providers": {
    "ollama": {
      "baseUrl": "http://localhost:11434/v1",
      "api": "openai-completions",
      "apiKey": "ollama",
      "compat": { "supportsDeveloperRole": false, "supportsReasoningEffort": false },
      "models": [
        { "id": "llama3.1:8b", "name": "Llama 3.1 8B (Local)", "reasoning": false,
          "input": ["text"], "contextWindow": 128000, "maxTokens": 32000,
          "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 },
          "thinkingLevelMap": { "minimal": null, "low": null, "medium": null,
                                "high": "high", "xhigh": null, "max": "max" },
          "samplingParams": { "temperature": 1.0, "top_p": 0.95 } }
      ]
    },
    "anthropic": { "baseUrl": "https://my-proxy.example.com/v1",
                   "apiKey": "$ANTHROPIC_API_KEY", "authHeader": true }
  }
}
```

provider 级字段：`name?`、`baseUrl?`、`apiKey?`、`api?`、`oauth?: "radius"`、`headers?`、`compat?`、`authHeader?`、`models?: ModelDefinition[]`、`modelOverrides?: Record<string, ModelOverride>`（**不可覆盖 `api`/`baseUrl`**）。`apiKey`/`headers` 值支持三种解析：`"!command"`（执行 shell）、`"$ENV"` / `"${ENV}"`（环境插值，`$$`→`$`、`$!`→`!`）、字面量。校验失败信息形如 `Invalid models.json schema:\n  - <path>: <message>\n\nFile: <path>`。

**`~/.pi/agent/auth.json`**（0600 权限，明文 JSON，**无 keytar / 无系统钥匙串**）：

```json
{
  "anthropic": { "type": "api_key", "key": "sk-ant-...", "env": { "ACCOUNT_ID": "..." } },
  "github-copilot": { "type": "oauth", "refresh": "...", "access": "...", "expires": 1767225600000 },
  "openai": { "type": "api_key", "key": "!security find-generic-password -ws 'openai'" }
}
```

`AuthType = "api_key" | "oauth"`；`CredentialInfo = {providerId, type}`（不含密钥）。写入唯一路径是 `AuthStorage.modify(provider, fn)`；`ReadOnlyAuthStorage` 的 `modify/delete` 会抛错。加锁用 `proper-lockfile`（同步 `lockSync(path, {realpath:false})`，`ELOCKED` 最多 10 次 × 20ms 忙等；异步 `lock` 退避 `min(10·2^n,1000)` + 抖动，deadline 30s）。

**`~/.pi/agent/models-store.json`**（远端目录缓存）：`Record<providerId, { models, lastModified?, checkedAt?, etag? }>`，`etag` 原样保留引号用于 `If-None-Match`。

### 7.6 模型解析

- `findExactModelReferenceMatch(ref, models)`：`${provider}/${id}` 小写精确 → 恰好 1 个才返回（歧义即拒绝）；含 `/` 则按第一个 `/` 切分再匹配；裸 `id` 同样要求唯一。
- `tryMatchModel()`：精确失败后做 `id/name` 的 `includes` 模糊匹配，alias（`-latest` 或非 `-\d{8}` 结尾）优先于 dated，同组取字典序最大。
- `parseModelPattern(pattern, models, {allowInvalidThinkingLevelFallback})`：用 **`lastIndexOf(":")`** 切出 thinking level；合法则递归前缀；非法时 scope 模式（`allowInvalidThinkingLevelFallback ?? true`）只 warning 继续，CLI 模式（`false`）**整体不匹配**——这是为了不把 `model:exacto` 这类带冒号的 id 误解析。
- `resolveModelScopeFromModels(patterns, models)`：含 `*`/`?`/`[` 时走 glob（先剥 thinking level，先试精确 id，再用 **minimatch** 对 `${provider}/${id}` 或 `id` 匹配，`{nocase:true}`）。
- `resolveCliModel({cliProvider, cliModel, cliThinking, modelRuntime})`：**用 `getModels()` 而非 `getAvailable()`**（让 `--api-key` 首次配置可用）；支持 `--model provider/model`；多 provider 歧义时用 `hasConfiguredAuth` 过滤；仍未命中且已知 provider 时用 `buildFallbackModel()` **合成一个不存在的 model id**（`{...baseModel, id: modelId, name: modelId}`）并给 warning。
- `findInitialModel(...)` 的优先级：**CLI** → **scoped 首个**（且非 continuing）→ **settings `defaultProvider`+`defaultModel`**（要求 `hasConfiguredAuth`）→ **可用快照按 `defaultModelPerProvider` 顺序找，否则取 `availableModels[0]`** → 都没有则 `model: undefined`。

`sdk.ts` 里 `createAgentSession` 的实际顺序：

1. `options.model`；
2. 已有 session 数据时，从 `buildSessionContext().model` 还原（且必须 `hasConfiguredAuth`），失败给 `modelFallbackMessage`；
3. `findInitialModel(...)`；
4. thinking level：`options.thinkingLevel` → session 记录（仅当分支里有 `thinking_level_change`）→ `settings.modelThinkingLevels["provider/modelId"]` → `settings.defaultThinkingLevel` → `DEFAULT_THINKING_LEVEL`；
5. `clampThinkingLevel(model, level)` 夹到模型支持范围；无 model 时强制 `"off"`。

Session 恢复另走 `restoreModelFromSession(savedProvider, savedModelId, currentModel, shouldPrintMessages, modelRuntime)`（reason 为 `"model no longer exists"` / `"no auth configured"`，逐级回落）。

### 7.7 `RuntimeCredentials`

不落盘的凭证覆盖层，供 CLI `--api-key` 的"仅本次进程有效"语义使用：`setRuntimeApiKey/removeRuntimeApiKey/hasRuntimeApiKey` 是同步的；`read` 命中覆盖直接返回 `{type:"api_key", key}`，否则透传底层；`list` 按 providerId 去重合并（覆盖项 type 恒为 `"api_key"`）。`getProviderAuthStatus` 据此报 `source: "runtime"`。来源优先级：`runtime` > `stored` > `models_json_*`/`environment`/`fallback`。

---

## 8. 移植到 Python：最小可用子集与取舍

### 8.1 第一版必须有的（Phase 1）

| 模块 | 范围 |
|---|---|
| `SessionManager` | JSONL、header、**全部 9 种 entry**、`id/parentId` 树、`buildContextEntries`/`buildSessionContext`、append 系列、`getBranch/getTree/branch/resetLeaf/branchWithSummary`、v1→v3 迁移 |
| `SettingsManager` | global + project 两级、deep merge、`compaction`/`retry`/`defaultTools`/`shellPath`/`shellCommandPrefix`/`sessionDir`/`defaultProvider`/`defaultModel`/`defaultThinkingLevel`，以及**按字段局部写回**语义 |
| 工具 | `read`、`bash`、`edit`、`write`（默认 active 四件套）+ `grep`/`find`/`ls`（可先用纯 Python 实现） |
| `truncate` | `DEFAULT_MAX_LINES=2000`/`DEFAULT_MAX_BYTES=50KB`、`truncateHead`/`truncateTail`/`truncateLine`/`formatSize`，含 UTF-8 边界处理 |
| `system-prompt` | 默认模板 + tools/guidelines + `<project_context>` + skills 块 + cwd 行 |
| `ResourceLoader`（简化） | `AGENTS.md` 逐级发现、`SYSTEM.md`/`APPEND_SYSTEM.md`、`skills/*/SKILL.md`、`prompts/*.md`、`themes/*.json`（+ `settings.json`） |
| `AgentSession` | `prompt()` 全流程（含 extension-command 跳过、skill/template 展开、streaming 队列、preflight 校验）、`_handlePostAgentRun` 的 retry 与 compaction 分支、事件订阅与持久化 |
| Compaction | `estimateContextTokens`/`shouldCompact`/`prepareCompaction`/`compact`（可用同一套 summarization prompt） |
| Model | 至少 1 个 provider（Anthropic 或 OpenAI 兼容），API key from env/`auth.json` |

### 8.2 可以推迟（Phase 2+）

- **扩展系统**（`ExtensionRunner`、`pi.registerTool/registerCommand/registerProvider`、所有 hook 事件）—— 这是最大的子系统，第一版完全可以只保留一个空的 `ExtensionRunner` 接口。但注意几条 hook 语义会影响行为：`input`、`before_agent_start`、`tool_call`(block)、`session_before_compact`、`session_before_tree`、`project_trust`。
- **TUI / renderer**（`core/tools/renderers/*`、`modes/interactive`、themes、keybindings）—— 与 Python 后端的 core 无关，可整体丢弃。
- **RPC / SDK / server / plugin / telemetry / package-manager**。
- **`powershell` 工具**（Windows 专用）。
- **图片处理**（`processImage`、auto-resize、`normalizeToolResultImages`、非 vision 提示）—— 先只支持文本，图片相关字段保留但走降级路径。
- **`exportToHtml` / `exportToJsonl` / fork / `createBranchedSession` 的 label 重排** 等边缘功能。
- **provider attribution headers / telemetry / HTTP 代理 / websocket transport**。
- **`.agents/skills` 祖先扫描 + git worktree shadow 检测**（可直接用 `.pi/skills` + `~/.pi/agent/skills`）。
- **`edit` 的模糊匹配（`normalizeForFuzzyMatch` / 行保留写回）** —— 第一版可只做精确匹配 + 唯一性/重叠校验，但**必须**保留 BOM 剥离、换行保持和「按原文匹配而非增量」的语义，否则行为差异明显。

### 8.3 Node 特有 / 需要注意的点（移植红旗）

| 项 | 说明 | Python 对策 |
|---|---|---|
| **ripgrep / fd 捆绑下载** | `utils/tools-manager.ts` 在 PATH 找不到 `rg`/`fd` 时，会从 GitHub Release 下载到 `~/.pi/agent/bin`（`PI_OFFLINE=1` 时跳过）。`grep`/`find` 直接依赖这两个二进制，并用 `rg --json` 的流式 JSON 协议 | ①直接要求系统装 `rg`/`fd`；②或纯 Python 实现（`os.walk` + `re`，`.gitignore` 用 `pathspec`）；③若要保真，`rg --json` 的解析逻辑需要照抄 |
| **`node:child_process` spawn + 进程组 kill** | `detached: process.platform !== "win32"`、`killProcessTree(pid)`、`waitForChildProcess()` 处理 detached 后代的 stdio 句柄 | `subprocess.Popen(start_new_session=True)` + `os.killpg`；用线程或 `asyncio` 读流 |
| **`node:fs` sync API 遍地使用** | `existsSync/readFileSync/statSync/readdirSync/appendFileSync/openSync` 等同步调用贯穿 SessionManager / ResourceLoader | Python 同构（`os`/`pathlib`/`open(..., "a")`），语义基本一致 |
| **`proper-lockfile`** | settings 写回用 `lockSync` + 重试 | 可用 `filelock`，或先省略（单进程场景影响小） |
| **`Buffer.byteLength` / `StringDecoder`** | 截断按 UTF-8 字节；JSONL 流式解析用 `StringDecoder` 处理跨 chunk 的多字节字符 | `len(s.encode("utf-8"))`；`codecs.getincrementaldecoder("utf-8")` |
| **`diff` npm 包** | `edit` 的 `diffLines` / `createTwoFilesPatch` | Python `difflib.unified_diff` / `difflib.SequenceMatcher`（输出的行号格式要自己拼） |
| **`typebox` schema** | 工具参数 schema 用 TypeBox（TS 类型 + JSON Schema） | 直接写 JSON Schema dict；`constrainedSampling: {type:"json_schema", strict:"prefer"}` 是给支持该能力的 provider 的提示 |
| **`ignore` 包** | skills 发现用 `.gitignore`/`.ignore`/`.fdignore` 规则 | `pathspec`（gitwildmatch） |
| **`proper-lockfile` / `uuidv7`** | session id 用 `uuidv7()`；entry id 用 `randomUUID().slice(0,8)` | `uuid_utils`/`uuid7` 库，或 `uuid4` 前 8 位（注意碰撞检查逻辑要保留） |
| **动态 `import()` 加载扩展** | `loadExtensionsCached` 用运行时 import 加载 `.ts`（Bun/Node + tsx） | Python 版第一版不做扩展加载；若要做，用 `importlib` |
| **`EventEmitter`（event-bus）** | 扩展事件总线，`on()` 返回 disposer，handler 异常被吞并 `console.error` | `collections.defaultdict(list)` + 自己的 dispose 返回；异常吞掉并 log |
| **`node:os` tmpdir / `node:crypto` randomBytes** | bash 全量输出写 `tmpdir()/pi-bash-<hex>.log` | `tempfile.gettempdir()` + `secrets.token_hex` |
| **文件监听 / HMR** | 扩展有 `reload()` 与 hot reload 能力 | 第一版不做 |
| **`process.platform` 分支** | powershell、fd 的 Windows 路径分隔符、`windowsHide` | Python 用 `sys.platform`；Windows 分支可先 stub |
| **`process.env.PI_*`** | `PI_SESSION_ID/PI_SESSION_FILE/PI_PROVIDER/PI_MODEL/PI_REASONING_LEVEL` 注入 bash；`PI_CODING_AGENT_DIR`、`PI_OFFLINE`、`PI_EXPERIMENTAL`、`PI_SKIP_VERSION_CHECK` | 保持同名环境变量，便于脚本兼容 |

### 8.4 容易踩坑的语义细节（务必照抄）

1. **持久化怪癖**：首条 assistant 之前**不创建 session 文件**；首条 assistant 到达时全量重写并置 `flushed`。这影响"空会话不产生文件"的行为。
2. **`appendMessage()` 拒绝直接写 compaction/branch summary 消息** —— 它们必须是顶层 entry。
3. **`buildContextEntries` 的裁剪规则**：`[compaction] + path[firstKeptEntryId..compactionIdx) + path(compactionIdx..]`；**不是**简单的"保留 compaction 之后的"。
4. **`getSessionContextSettings` 用完整 path**，不是 compaction 裁剪后的 list。
5. **`message_start`(user) 时的队列去重是按文本 `indexOf`**，重复文本会误删。
6. **`_queueUserInput` 禁止扩展命令入队**（抛错），而 `prompt()` 里的扩展命令是**立即执行、不发 LLM**。
7. **`prompt()` 在 streaming 时如果没给 `streamingBehavior` 直接抛错**。
8. **`bash` 非 0 退出码抛异常**，输出被塞进异常 message，因此 toolResult 的 `isError=true` 而 `content` 里仍有完整输出。
9. **`_checkCompaction` 的三重早退**（模型一致性、aborted、陈旧 compaction 边界）缺一不可，否则会出现"刚 compact 完又立刻 compact"或"切换模型后误触发"。
10. **overflow 恢复只允许一次**（`_overflowRecoveryAttempted`）。
11. **`edit` 的所有 `oldText` 针对原始文件匹配**，不是串行增量。
12. **自动重试成功后 `_retryAttempt` 立即归零**，避免同一轮内多次 LLM 调用累加。
13. **`setActiveToolsByName` 会重建 system prompt** —— 工具集与 prompt 必须保持一致（`Allowed tools` 只列有 snippet 的）。
14. **system prompt 的 cwd 行用正斜杠**（`cwd.replace(/\\/g,"/")`）。
15. **`<project_instructions>` 的层级顺序是"全局 → 最外层祖先 → cwd 最近"**，不是反过来。

### 8.5 Model 层的额外移植注意

1. **`lazyStream` 契约**：Python 里要做一个"先同步返回一个事件流对象、后台 task 填充、错误也以事件形式发出"的结构（例如 `asyncio.Queue` + sentinel，或返回一个带 `.result()` 的包装对象），否则 `streamSimple(...).result()` / 错误语义会走样。
2. **`globalThis.fetch` / `undici`**：pi-ai 所有 provider adapter 都用 `options?.fetch ?? globalThis.fetch`；`core/http-dispatcher.ts` 装全局 dispatcher 做代理注入；`experimental/radius-relay.ts` 用 `undici.WebSocket`。Python 侧换 `httpx`（含 `proxies`）+ `websockets`，代理注入要自己实现。
3. **`AbortSignal.any([...])` / `AbortSignal.timeout()`** → `asyncio.timeout` + 组合取消；`structuredClone` → `copy.deepcopy`。
4. **`minimatch`（model scope glob）→ `fnmatch`/`pathspec`；`proper-lockfile` → `filelock`**。
5. **凭证写路径必须单点化**（只有 `modify` 能写）；OAuth 刷新要在锁内 double-check（5 分钟有效窗口，刷新超时 15s），否则并发双重刷新会让 refresh token 轮换失效。
6. **`defaultModelPerProvider` 是全覆盖 dict**，Python 侧应在启动时校验每个已知 provider 都有条目。
7. 第一版可以只支持 `api_key`（含 `!command` / `$ENV` 两种值形态），**OAuth login 流程与远端 catalog 刷新全部推迟**，但 `models.json` 的 JSONC 解析与 `modelOverrides` 可以照做——成本低、收益高。

---

## 附：关键常量速查

```ts
CURRENT_SESSION_VERSION = 3
DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024
GREP_MAX_LINE_LENGTH = 500
DEFAULT_LIMIT (grep) = 100
DEFAULT_LIMIT (find) = 1000
DEFAULT_LIMIT (ls)   = 500
DEFAULT_THINKING_LEVEL = "medium"
THINKING_LEVEL_OPTIONS = ["off","minimal","low","medium","high","xhigh","max"]
DEFAULT_COMPACTION_SETTINGS = { enabled: true, reserveTokens: 16384, keepRecentTokens: 20000 }
retry 默认 = { enabled: true, maxRetries: 3, baseDelayMs: 2000, maxAgentDelayMs: 60000 }
tool default active = ["read","bash","edit","write"]
所有内置工具 = ["read","bash","powershell","edit","write","grep","find","ls"]
MAX_TIMEOUT_MS (bash) = 2_147_483_647
CONFIG_DIR_NAME = ".pi"       // 可被 package.json 的 piConfig.configDir 覆盖
agentDir = $PI_CODING_AGENT_DIR ?? ~/.pi/agent
sessionDir = agentDir/sessions/--<cwd 编码>--
```

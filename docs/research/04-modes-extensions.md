# pi 研究报告 04：用户交互模式与扩展系统

> 研究对象：`/tmp/pi-ref/packages/coding-agent`（pi coding agent，Earendil Works，版本 `0.85.1`）
> 本文范围：CLI 表面、四种运行模式（interactive / print / json / rpc）、slash 命令、扩展系统（Extension API / 加载器 / 运行器）、skills / prompt templates / themes、以及 Python 移植建议。
> 所有标识符保持英文原样；`src/...` 路径均相对 `packages/coding-agent/`。

---

## 0. 阅读地图

| 关注点 | 关键文件 | 行数 |
|---|---|---|
| 入口分派 / 参数解析 / help | `src/main.ts`, `src/cli/args.ts` | 981 / 447 |
| 包管理（CLI + 实体） | `src/package-manager-cli.ts`, `src/core/package-manager.ts` | 1102 / 2699 |
| print / json / rpc | `src/modes/print-mode.ts`, `json-event.ts`, `src/modes/rpc/{rpc-mode,rpc-types,jsonl,rpc-client}.ts` | 169/61/821/297/58/609 |
| interactive（TUI） | `src/modes/interactive/interactive-mode.ts` | 6620 |
| 扩展系统 | `src/core/extensions/{types,loader,runner,index,wrapper}.ts` | 1797/809/1286/193/— |
| Session 门面 / 资源发现 | `src/core/agent-session.ts`, `src/core/resource-loader.ts` | 3552 / 1097 |
| skills / prompts / themes | `src/core/skills.ts`, `prompt-templates.ts`, `modes/interactive/theme/*` | 509 / 285 / — |
| 仓库内文档 | `docs/*.md`（`extensions.md` 3033 行、`rpc.md` 1618 行最权威） | — |

`docs/` 与源码高度同步，**移植时应当把 `docs/*.md` 当成规格书使用**，源码只是实现。

---

## 1. CLI 表面（CLI surface）

### 1.1 入口分派顺序（`main(args, options)`）

`main.ts` 的分派顺序非常讲究：**子命令先于参数解析**，因为子命令自己解析自己的参数。真实顺序如下：

```ts
export async function main(args: string[], options?: MainOptions) {
  resetTimings();
  const extensionFactories = [...builtInExtensions, ...(options?.extensionFactories ?? [])];
  const offlineMode = args.includes("--offline") || isTruthyEnvFlag(process.env.PI_OFFLINE);
  if (offlineMode) { process.env.PI_OFFLINE = "1"; process.env.PI_SKIP_VERSION_CHECK = "1"; }

  if (await runAuthCommand(args)) return;                 // ① auth 子命令
  if (process.platform === "win32") cleanupWindowsSelfUpdateQuarantine(getPackageDir());
  cleanupManagedInstall();                                // ② 清掉遗留的 managed 安装 staging

  const cwd = process.cwd();
  const agentDir = getAgentDir();
  const bootstrapSettingsManager = SettingsManager.create(cwd, agentDir, { projectTrusted: false });
  applyHttpProxySettings(...); configureHttpDispatcher();

  if (await handlePackageCommand(args, { extensionFactories })) { process.exit(exitCode); }   // ③ install/remove/uninstall/update/list
  if (await handleConfigCommand(args, { extensionFactories })) return;                        // ④ config TUI

  const parsed = parseArgs(args);                          // ⑤ 到这里才是主流程参数
  ...
}
```

①~④ 全部走「一次性命令」路径：执行完直接 `process.exit()`，不允许扩展持有事件循环（注释里明确写了 "so bad extensions cannot keep one-shot commands alive"）。Windows 上的 `pi update` 是唯一例外，让进程自然 drain，规避 Node teardown 断言。

### 1.2 子命令清单

| 命令 | 作用 | 关键实现 |
|---|---|---|
| `pi install <source> [-l] [--approve\|--no-approve]` | 安装 pi 包（npm/git/local）并写入 settings | `handlePackageCommand` |
| `pi remove <source> [-l]` / `pi uninstall <source>` | 从 settings 移除；`uninstall` 是别名 | 同上 |
| `pi update [source\|self\|pi] [--self\|--extensions\|--models\|--all] [--extension <src>] [--force]` | 更新 pi CLI / 扩展包 / 模型目录 | `updateTarget` |
| `pi list` | 列出 settings 中已安装的包 | `listConfiguredPackages()` |
| `pi config [-l]` | 打开资源开关 TUI（Tab 切换 global/project 作用域） | `handleConfigCommand` |
| `pi auth print-api-key [--provider <p>] [--model <m>]` | 打印 provider API key（供外部客户端用） | `resolveCredentialForPrint` |
| `pi auth print-bearer-token [--provider] [--model] [--min-expiry 30m]` | 打印 OAuth bearer token（过期自动刷新） | 同上 |
| `pi auth check [--provider\|--model] [--json] [--credentials] [--no-refresh]` | 检查 provider 可用性 | `checkProviderAuth` |
| `<cmd> --help` | install/remove/uninstall/update/list/config/auth 各自 help | `printPackageCommandHelp` / `printAuthCommandHelp` |

`auth check` 的退出码语义被严格定义：

```ts
process.exitCode = result.status === "ready" ? 0
                 : result.status === "not_ready" ? 1
                 : 2;   // invalid / invalid_state
```

`--min-expiry` 只允许 `print-bearer-token`，单位解析正则 `/^(\d+)(ms|s|m|h)$/iu`。`--json` / `--credentials` / `--no-refresh` 只允许 `check`。

### 1.3 顶层 flag 全表（`parseArgs`）

来源 `src/cli/args.ts`，`Args` 接口即权威。

| Flag | 别名 | 类型 | 说明 |
|---|---|---|---|
| `--help` | `-h` | bool | 打印 help（会带上扩展注册的 flag） |
| `--version` | `-v` | bool | 打印 `VERSION` 后 `exit(0)` |
| `--mode <text\|json\|rpc>` | | enum | 非法值直接忽略（不报错） |
| `--print` | `-p` | bool | 非交互模式；后面紧跟的非 `-`/`@` 参数会被吞为 message |
| `--continue` | `-c` | bool | 继续最近 session |
| `--resume` | `-r` | bool | 打开 session 选择器 |
| `--provider <name>` | | string | 默认 google |
| `--model <pattern>` | | string | 支持 `provider/id` 与 `:thinking` 简写 |
| `--api-key <key>` | | string | 运行时 key，**必须同时指定 model**，否则报错 |
| `--system-prompt <text>` | | string | 覆盖 system prompt |
| `--append-system-prompt <text>` | | repeatable | 追加；可多次 |
| `--name <name>` | `-n` | string | session 显示名；空串报错 |
| `--no-session` | | bool | 不落盘（in-memory） |
| `--session <path\|id>` | | string | 指定 session 文件或 id 前缀 |
| `--session-id <id>` | | string | 精确 project session id，不存在则创建 |
| `--fork <path\|id>` | | string | 从指定 session fork；与 `--session/--continue/--resume/--no-session` 冲突 |
| `--session-dir <dir>` | | string | session 存储目录（也受 `PI_SESSION_DIR` 影响） |
| `--models <patterns>` | | 逗号分隔 | Ctrl+P 循环的模型 scope，支持 glob 与 `:thinking` |
| `--no-tools` | `-nt` | bool | 禁用全部工具（含扩展工具） |
| `--no-builtin-tools` | `-nbt` | bool | 只禁用内建工具 |
| `--tools <names>` | `-t` | 逗号分隔 | 白名单 |
| `--exclude-tools <names>` | `-xt` | 逗号分隔 | 黑名单 |
| `--thinking <level>` | | enum | `off, minimal, low, medium, high, xhigh, max`；非法值只 warning |
| `--extension <path>` | `-e` | repeatable | 显式加载扩展 |
| `--no-extensions` | `-ne` | bool | 关闭扩展发现（`-e` 仍生效） |
| `--skill <path>` | | repeatable | 显式 skill |
| `--no-skills` | `-ns` | bool | 关闭 skill 发现（`--skill` 仍生效） |
| `--prompt-template <path>` | | repeatable | 显式 prompt 模板 |
| `--no-prompt-templates` | `-np` | bool | 关闭模板发现 |
| `--theme <path>` | | repeatable | 加载主题文件/目录 |
| `--use-theme <name[/name]>` | | string | 本次运行的初始主题（`light/dark` 表示跟随终端） |
| `--no-themes` | | bool | 关闭主题发现 |
| `--no-context-files` | `-nc` | bool | 关闭 `AGENTS.md` / `CLAUDE.md` 发现 |
| `--export <file>` | | string | 导出 session 为 HTML 并退出；第二个位置参数是输出路径 |
| `--list-models [search]` | | optional string | 列表模式；跟随参数若不是 flag/`@` 则作为模糊搜索词 |
| `--verbose` | | bool | 强制 verbose 启动（覆盖 `quietStartup`） |
| `--tui-mode <regular\|fullscreen>` | | enum | 非法值报 error |
| `--approve` | `-a` | bool | 本次运行信任 project-local 资源 |
| `--no-approve` | `-na` | bool | 本次运行忽略 project-local 资源 |
| `--offline` | | bool | 等价 `PI_OFFLINE=1`，同时置 `PI_SKIP_VERSION_CHECK=1` |
| `--` | | | 结束解析，后续 `@x` 当文件、其它当 message |
| `@path` | | | fileArg（喂给 `processFileArguments`） |
| 其它 `--xxx` | | | 进入 `unknownFlags: Map<string, boolean\|string>`，供扩展 flag 使用 |

解析细节（移植时要 1:1 对齐）：

- `--print` 会偷看下一个参数：`next !== undefined && !next.startsWith("@") && (!next.startsWith("-") || next.startsWith("---"))` 时才吞。
- `--xxx=value` 与 `--xxx value` 都会写入 `unknownFlags`；`--xxx` 后面跟 `-` 或 `@` 开头则记为 `true`。
- 单个 `-x` 未识别 → `diagnostics: [{type:"error"}]`，`main` 里会 `exit(1)`；`--xxx` 未识别 → **静默进 unknownFlags**。这是扩展 flag 能工作的基础。
- `--thinking` 非法值只 warning 不报错；`--name` 缺值报 error。

### 1.4 启动序列（config → trust → auth → resource → session → mode）

`main()` 中段是移植时最值得照抄的骨架：

1. **parseArgs + diagnostics**（error 直接 `exit(1)`）→ `--version` → `--export`。
2. **决定 AppMode**（§2.1）；非 interactive 且不是纯元数据命令时 `takeOverStdout()`。
3. RPC + `@file` 报错 → `validateForkFlags` / `validateSessionIdFlags`（互斥 + `assertValidSessionId`）→ `runMigrations(cwd)`。
4. `startupSettingsManager = SettingsManager.create(cwd, agentDir)`（bootstrap 时 `projectTrusted:false`）+ `collectSettingsDiagnostics`。
5. interactive 且 `shouldRunFirstTimeSetup()` → `showFirstTimeSetup()`；`--use-theme` → `applyOverrides({theme})`。
6. **sessionDir**：`--session-dir` > `PI_SESSION_DIR` > settings.sessionDir → `createSessionManager()`（§1.5）→ `getMissingSessionCwdIssue()`（interactive 弹选择器，否则报错退出）→ `--name` 写 `appendSessionInfo`。
7. `ProjectTrustStore(agentDir)`；构造**延迟执行**的 `createRuntime` 工厂（`--session/--resume` 可能选中别的项目，cwd 此时才确定）。
8. `createAgentSessionRuntime(createRuntime, ...)`，工厂内部依次：判断是否弹 trust → `SettingsManager.create(cwd, agentDir, {projectTrusted})` → `createAgentSessionServices({ ..., resourceLoaderReloadOptions.resolveProjectTrust })`（**扩展加载先于 trust 决断**，`project_trust` 事件即参与入口）→ `resolveModelScope()` → `buildSessionOptions()` → `--api-key` 注入 → `createAgentSessionFromServices()`。
9. `setCapabilityOverrides` / HTTP proxy；`--help` → `printHelp(extensionFlags)` 退出；`--list-models` → `listModels()` 退出。
10. 非 RPC：`readPipedStdin()`（**interactive + 有管道内容 → 降级 print**）→ `prepareInitialMessage()`（`@file` 文本/图片 + stdin + messages）。
11. `setThemeJsonValidator` / `initTheme` → deprecation warnings（interactive）→ diagnostics（有 error 则 `exit(1)`，扩展加载失败额外提示 `pi -ne`）→ 非 interactive 且无 model 则 `exit(1)`。
12. `PI_STARTUP_BENCHMARK` 仅 interactive；RPC 非 offline 时后台 `modelRuntime.refresh()`（15s 超时）。
13. 分派：`runRpcMode(runtime)` / `new InteractiveMode(runtime, opts).run()` / `runPrintMode(runtime, {mode, messages, initialMessage, initialImages})`。

节流打点（`time(label)` / `printTimings()`）被埋在关键步骤之间，`--verbose` 时可见。

### 1.5 `createSessionManager` 的 session 选择优先级

```ts
if (parsed.noSession || parsed.help || parsed.listModels !== undefined) return SessionManager.inMemory(...);
if (parsed.fork)      -> resolveSessionPath(--fork) -> forkSessionOrExit(...)      // 已存在同 id 报错
if (parsed.session)   -> resolveSessionPath(--session)
                          path/local -> open；global -> 询问 "Fork this session into current directory?"；not_found -> exit(1)
if (parsed.resume)    -> selectSession(...)  // TUI 选择器，取消则 exit(0)
if (parsed.continue)  -> SessionManager.continueRecent(cwd, sessionDir)
if (parsed.sessionId) -> 本地查同 id，命中则 open，否则 warning 后创建一个同 id 的新 session
else                  -> SessionManager.create(cwd, sessionDir, { id })
```

`resolveSessionPath` 的判定：含 `/`、`\` 或以 `.jsonl` 结尾 → 当路径；否则先在当前 project 精确匹配、再前缀匹配，然后 `SessionManager.listAll()` 全局同样两轮匹配。返回 `{type: "path"|"local"|"global"|"not_found"}`。

### 1.6 项目信任（project trust）

信任决策在 `core/project-trust.ts` + `core/trust-manager.ts`：

- 只有 `cwd/.pi/` 下出现 **需要信任的项目资源** 时才需要决策：

```ts
const TRUST_REQUIRING_PROJECT_CONFIG_RESOURCES = [
  "settings.json", "extensions", "skills", "prompts", "themes", "SYSTEM.md", "APPEND_SYSTEM.md",
] as const;
```

- 决策优先级：`--approve/--no-approve`（`projectTrustOverride`） > `project_trust` 扩展事件返回 `{trusted:"yes"|"no"|"undecided", remember?}` > trust store 落盘值 > `defaultProjectTrust`（`ask`/`always`/`never`）> 交互询问（`hasUI` 为 false 时默认 **不信任**）。
- trust store 查找是**向上递归**的：`findNearestTrustEntry` 从 cwd 一路 `dirname` 到根，第一个 true/false 生效；`null` 表示显式覆盖。
- 选项文案包括 `Trust`、`Trust parent folder (<path>)`、`Trust (this session only)`、`Do not trust`、`Do not trust (this session only)`。
- `docs/settings.md` 补充：非交互模式（`-p` / `--mode json` / `--mode rpc`）不弹 trust 提示，直接用 `defaultProjectTrust`：`ask`/`never` 表示不加载项目资源，`always` 表示信任。

### 1.7 `--export` / `--list-models` / `--offline` 的边界行为

- `--export` 在 **parseArgs 之后、takeOverStdout 之前**处理：`outputPath = parsed.messages[0]`（可选）。
- `--list-models` 属于 "plain runtime metadata command"（`!print && mode===undefined && (help || listModels)`），**不 takeOverStdout**，因此 `console.log` 直出。
- `--offline` 会写环境变量，影响后续 `ModelRuntime` 网络行为；RPC 下的 catalog refresh 也被跳过。

---

## 2. 四种运行模式

### 2.1 模式选择算法

两个不同的类型：

```ts
export type Mode = "text" | "json" | "rpc";                    // CLI 视角
export type AppMode = "interactive" | "print" | "json" | "rpc"; // 运行期视角
```

```ts
function resolveAppMode(parsed: Args, stdinIsTTY: boolean, stdoutIsTTY: boolean): AppMode {
  if (parsed.mode === "rpc") return "rpc";
  if (parsed.mode === "json") return "json";
  if (parsed.print || !stdinIsTTY || !stdoutIsTTY) return "print";
  return "interactive";
}
```

要点：

- **任何一端不是 TTY 就退化成 print**（管道/CI 天然安全）。
- 管道 stdin 有内容且判定为 interactive，会在第 19 步被改写为 print。
- `ExtensionMode` 是另一套值：`"tui" | "rpc" | "json" | "print"`，扩展只看这一套（`ctx.mode`）。

### 2.2 print mode（`-p` / `--mode text`）

`runPrintMode(runtime, options)`（`src/modes/print-mode.ts`，仅 169 行，建议完整对齐）：

- `rebindSession()` 里 `session.bindExtensions({ mode: mode === "json" ? "json" : "print", commandContextActions, onError })`。**print 模式扩展仍然运行**，只是不能弹窗。
- json 模式先输出 session header（`session.sessionManager.getHeader()`），再逐事件 `writeRawStdout(JSON.stringify(toJsonEvent(event)) + "\n")`。
- 用 `session.agent.subscribe(async () => waitForRawStdoutBackpressure())` 做背压。
- 顺序执行：`initialMessage`（带 images），随后 `for (const message of messages) await session.prompt(message)`。
- **text 模式只输出最后一条 assistant message 的 text 块**：

```ts
if (mode === "text") {
  const state = session.state;
  const lastMessage = state.messages[state.messages.length - 1];
  if (lastMessage?.role === "assistant") {
    const assistantMsg = lastMessage as AssistantMessage;
    if (assistantMsg.stopReason === "error" || assistantMsg.stopReason === "aborted") {
      console.error(assistantMsg.errorMessage || `Request ${assistantMsg.stopReason}`);
      exitCode = 1;
    } else {
      for (const content of assistantMsg.content) if (content.type === "text") writeRawStdout(`${content.text}\n`);
    }
  }
}
```

- 信号处理：`SIGTERM`（全平台）+ `SIGHUP`（非 win32），处理时 `killTrackedDetachedChildren()` → `disposeRuntime()` → `process.exit(143 / 129)`。
- finally 里 `disposeRuntime()` + `flushRawStdout()`。
- 返回值即 exit code，`main.ts` 里 `if (exitCode !== 0) process.exitCode = exitCode`。

### 2.3 JSON 事件流（`--mode json`）

协议定义在 `src/modes/json-event.ts`。核心是**剥掉累积快照**，让流大小线性：

```ts
type WithoutPartial<T> = T extends { partial: unknown } ? Omit<T, "partial"> : T;
type ToJsonAssistantMessageEvent<T> =
  T extends { type: "toolcall_start"; partial: unknown }
    ? WithoutPartial<T> & { id: string; toolName: string }
    : WithoutPartial<T>;

export type JsonMessageUpdateEvent = {
  type: "message_update";
  usage: Usage;
  assistantMessageEvent: ToJsonAssistantMessageEvent<MessageUpdateEvent["assistantMessageEvent"]>;
};
export type JsonAgentSessionEvent = Exclude<AgentSessionEvent, { type: "message_update" }> | JsonMessageUpdateEvent;
```

`toJsonEvent()` 规则：

- 非 `message_update` 事件原样返回。
- `message_update` 重写为 `{type, usage, assistantMessageEvent}`，**删除**顶层累积 `message` 与嵌套 `partial`：
  - `toolcall_start` 走特判：从 `event.partial.content[event.contentIndex]` 取出 `id` 与 `name`，额外补 `{ id, toolName }`（体量恒定，安全）。
  - 若 `contentIndex` 处不是 `toolCall` → 抛 `Error`。
- 若 `event.message.role !== "assistant"` → 抛 `Error("message_update message is not an assistant message")`。

第一行是 session header，随后是事件：

```json
{"type":"session","version":3,"id":"uuid","timestamp":"...","cwd":"/path"}
{"type":"agent_start"}
{"type":"turn_start","turnIndex":0,"timestamp":1700000000000}
{"type":"message_start","message":{"role":"assistant","content":[],...}}
{"type":"message_update","usage":{...},"assistantMessageEvent":{"type":"text_delta","contentIndex":0,"delta":"Hello"}}
{"type":"message_end","message":{...}}
{"type":"turn_end","message":{...},"toolResults":[]}
{"type":"agent_end","messages":[...],"willRetry":false}
{"type":"agent_settled"}
```

`docs/json.md` 明确：`message_update` 是 delta-only，客户端需用 `contentIndex` + `delta` 自行拼装；顶层 `usage` 是**最新累积 usage**，部分 provider 只在结束时给值，所以可能一直是 0；`message_end` 才是权威终态。

### 2.4 `AgentSessionEvent` 全集（json/rpc 共用的 wire 事件）

`AgentSessionEvent = Exclude<AgentEvent, {type:"agent_end"}> | (下面这些)`：

```ts
| { type: "agent_end"; messages: AgentMessage[]; willRetry: boolean }
| { type: "agent_settled" }
| { type: "queue_update"; steering: readonly string[]; followUp: readonly string[] }
| { type: "compaction_start"; reason: "manual" | "threshold" | "overflow" }
| { type: "compaction_end"; reason; result: CompactionResult | undefined; aborted: boolean; willRetry: boolean; errorMessage?: string }
| { type: "entry_appended"; entry: SessionEntry }
| { type: "session_info_changed"; name: string | undefined }
| { type: "thinking_level_changed"; level: ThinkingLevel }
| { type: "auto_retry_start"; attempt; maxAttempts; delayMs; errorMessage }
| { type: "auto_retry_end"; success: boolean; attempt: number; finalError?: string }
| { type: "summarization_retry_scheduled"; attempt; maxAttempts; delayMs; errorMessage }
| { type: "summarization_retry_attempt_start"; source: "branchSummary" | "compaction"; reason?: ... }
| { type: "summarization_retry_finished" }
| { type: "bash_execution_update"; id?: string; delta: string }
```

来自 `AgentEvent`（`packages/agent/src/types.ts:431`）的基础事件：`agent_start`、`agent_end`、`turn_start`、`turn_end`、`message_start`、`message_update`、`message_end`、`tool_execution_start`、`tool_execution_update`、`tool_execution_end`。

RPC 额外多一个 **不在** `AgentSessionEvent` 里的 `extension_error`（由 rpc-mode 的 `onError` 直接输出）：
`{ type: "extension_error", extensionPath, event, error }`。

### 2.5 RPC 模式完整协议

#### 2.5.1 启动与生命周期

```bash
pi --mode rpc [--provider p] [--model m] [--name n] [--no-session] [--session-dir d]
```

- `runRpcMode(runtimeHost): Promise<never>`，返回一个永不 resolve 的 Promise 保持进程存活。
- 启动即 `takeOverStdout()`；`--mode rpc` 下 `@file` 参数被拒绝。
- `process.stdin.on("end", shutdown)`：stdin 关闭即优雅退出。
- 信号：`SIGTERM` → exit 143；`SIGHUP` → exit 129（非 win32）。
- 扩展调用 `ctx.shutdown()` 时 rpc-mode 把 `shutdownRequested = true`（`bindExtensions({ shutdownHandler })`），在**下一次 `agent_settled` 事件**时执行 shutdown。
- `new_session` / `switch_session` / `fork` / `clone` 成功后会 `await rebindSession()` 重建订阅与 UI context。

#### 2.5.2 JSONL framing 规则（`src/modes/rpc/jsonl.ts`）

```
Framing is LF-only. Payload strings may contain other Unicode separators such as
U+2028 and U+2029. Clients must split records on `\n` only.
```

- 写：`serializeJsonLine(v) => `${JSON.stringify(v)}\n``。
- 读：自实现 `attachJsonlLineReader`（`StringDecoder("utf8")` + 手写 `indexOf("\n")` 切片），**刻意不用 Node `readline`**，因为 readline 也会在 `U+2028/U+2029` 处断行，而那些字符在 JSON 字符串里合法。
- 输入宽容：行尾 `\r` 会被剥掉（`line.endsWith("\r") ? line.slice(0,-1) : line`）。
- 流结束时若缓冲区还有残余，作为最后一行 flush。
- 每个响应输出后都 `await waitForRawStdoutBackpressure()`。

#### 2.5.3 命令全集（stdin，每行一个 JSON object）

所有命令都支持可选 `id`，响应用同一个 `id` 回关联（`bash_execution_update` 也带原始 `bash` 命令的 `id`）。

| 分类 | 命令 | 字段 | 响应 data |
|---|---|---|---|
| Prompting | `prompt` | `message: string`, `images?: ImageContent[]`, `streamingBehavior?: "steer"\|"followUp"` | 无（异步，preflight 成功后回 `{success:true}`） |
| | `steer` | `message`, `images?` | 无 |
| | `follow_up` | `message`, `images?` | 无 |
| | `abort` | — | 无 |
| | `clear_queue` | — | `{ steering: string[]; followUp: string[] }` |
| | `new_session` | `parentSession?: string` | `{ cancelled: boolean }` |
| State | `get_state` | — | `RpcSessionState` |
| Model | `set_model` | `provider`, `modelId` | `Model`（找不到 → error） |
| | `cycle_model` | — | `{ model, thinkingLevel, isScoped } \| null` |
| | `get_available_models` | — | `{ models: Model[] }` |
| Thinking | `set_thinking_level` | `level` | 无 |
| | `cycle_thinking_level` | — | `{ level } \| null` |
| | `get_available_thinking_levels` | — | `{ levels: ThinkingLevel[] }` |
| Queue | `set_steering_mode` | `mode: "all"\|"one-at-a-time"` | 无 |
| | `set_follow_up_mode` | `mode` | 无 |
| Compaction | `compact` | `customInstructions?` | `CompactionResult` |
| | `set_auto_compaction` | `enabled: boolean` | 无 |
| Retry | `set_auto_retry` | `enabled` | 无 |
| | `abort_retry` | — | 无 |
| Bash | `bash` | `command`, `excludeFromContext?` | `BashResult` |
| | `abort_bash` | — | 无 |
| Session | `get_session_stats` | — | `SessionStats` |
| | `export_html` | `outputPath?` | `{ path }` |
| | `switch_session` | `sessionPath` | `{ cancelled }` |
| | `fork` | `entryId` | `{ text, cancelled }` |
| | `clone` | — | `{ cancelled }`（无 leaf 时 error） |
| | `get_fork_messages` | — | `{ messages: [{ entryId, text }] }` |
| | `get_entries` | `since?` | `{ entries: SessionEntry[]; leafId }`（`since` 找不到 → error） |
| | `get_tree` | — | `{ tree: SessionTreeNode[]; leafId }` |
| | `get_last_assistant_text` | — | `{ text: string \| null }` |
| | `set_session_name` | `name` | 无（trim 后为空 → error） |
| Messages | `get_messages` | — | `{ messages: AgentMessage[] }` |
| Commands | `get_commands` | — | `{ commands: RpcSlashCommand[] }` |
| 未识别 | 任意 `type` | | `{ success:false, error: "Unknown command: <type>" }` |

`RpcSessionState`：

```ts
{
  model?: Model<any>;
  thinkingLevel: ThinkingLevel;
  isStreaming: boolean;
  isCompacting: boolean;
  steeringMode: "all" | "one-at-a-time";
  followUpMode: "all" | "one-at-a-time";
  sessionFile?: string;
  sessionId: string;
  sessionName?: string;
  autoCompactionEnabled: boolean;
  messageCount: number;
  pendingMessageCount: number;
}
```

`prompt` 的异步语义值得注意（Python 客户端必须处理）：

```ts
case "prompt": {
  let preflightSucceeded = false;
  void session.prompt(command.message, {
    images: command.images,
    streamingBehavior: command.streamingBehavior,
    source: "rpc",
    preflightResult: (didSucceed) => {
      if (didSucceed) { preflightSucceeded = true; output(success(id, "prompt")); }
    },
  }).catch((e) => { if (!preflightSucceeded) output(error(id, "prompt", e.message)); });
  return undefined;   // 不立刻回包
}
```

即：`prompt` 的 response 在 **preflight 成功**（校验通过、已入队或已开始）时发出，随后才是大量事件流；handler 返回 `undefined` 表示「本命令不产生即时响应」。

#### 2.5.4 响应形状

```ts
// 成功
{ id?, type: "response", command: "<cmd>", success: true }             // 无 data
{ id?, type: "response", command: "<cmd>", success: true, data: {...} }
// 失败（任意命令）
{ id?, type: "response", command: string, success: false, error: string }
// JSON 解析失败
{ id: undefined, type: "response", command: "parse", success: false, error: "Failed to parse command: ..." }
```

`handleCommand` 抛异常也会被 catch 成 `success:false`，`command` 用原始请求的 `type`。

#### 2.5.5 扩展 UI 子协议（RPC 下最重要的设计）

RPC 模式下 `ExtensionUIContext` 由 `createExtensionUIContext()` 实现（rpc-mode.ts:136-311），分成两类：

**A. 阻塞式 dialog**：`select` / `confirm` / `input` / `editor`
- 生成 `crypto.randomUUID()` 作 `id`，放进 `pendingExtensionRequests: Map<id, {resolve, reject}>`，输出 `extension_ui_request`，等 stdin 回 `extension_ui_response`。
- 支持 `opts.signal`（abort → 立刻 resolve 默认值）与 `opts.timeout`（超时 → resolve 默认值，countdown 由 TUI 端显示，**客户端不需要自己计时**）。
- 默认值：`select/input/editor` → `undefined`；`confirm` → `false`。

**B. fire-and-forget**：`notify` / `setStatus` / `setWidget` / `setTitle` / `set_editor_text`
- 只输出请求，不等响应。

请求形状（stdout）：

```jsonc
{"type":"extension_ui_request","id":"uuid-1","method":"select","title":"Allow dangerous command?","options":["Allow","Block"],"timeout":10000}
{"type":"extension_ui_request","id":"uuid-2","method":"confirm","title":"Clear session?","message":"All messages will be lost.","timeout":5000}
{"type":"extension_ui_request","id":"uuid-3","method":"input","title":"Enter a value","placeholder":"type something..."}
{"type":"extension_ui_request","id":"uuid-4","method":"editor","title":"Edit some text","prefill":"Line 1\nLine 2"}
{"type":"extension_ui_request","id":"uuid-5","method":"notify","message":"Command blocked by user","notifyType":"warning"}
{"type":"extension_ui_request","id":"uuid-6","method":"setStatus","statusKey":"my-ext","statusText":"Turn 3 running..."}
{"type":"extension_ui_request","id":"uuid-7","method":"setWidget","widgetKey":"my-ext","widgetLines":["Line 1"],"widgetPlacement":"aboveEditor"}
{"type":"extension_ui_request","id":"uuid-8","method":"setTitle","title":"pi - my project"}
{"type":"extension_ui_request","id":"uuid-9","method":"set_editor_text","text":"prefilled text"}
```

响应形状（stdin，仅 dialog 需要）：

```jsonc
{"type":"extension_ui_response","id":"uuid-1","value":"Allow"}          // select / input / editor
{"type":"extension_ui_response","id":"uuid-2","confirmed":true}           // confirm
{"type":"extension_ui_response","id":"uuid-3","cancelled":true}           // 任意：→ undefined / false
```

`handleInputLine` 会**先**识别 `type === "extension_ui_response"` 并 resolve pending，再当作普通命令解析；找不到 pending id 就静默丢弃。

**降级/不支持清单**（扩展作者与移植者都必须知道）：

| 方法 | RPC 行为 |
|---|---|
| `custom()` | 返回 `undefined` |
| `setWorkingMessage` / `setWorkingVisible` / `setWorkingIndicator` / `setHiddenThinkingLabel` | no-op |
| `setFooter` / `setHeader` / `setEditorComponent` / `addAutocompleteProvider` / `setToolsExpanded` | no-op |
| `getEditorText()` | 返回 `""` |
| `getToolsExpanded()` | 返回 `false` |
| `pasteToEditor(text)` | 退化为 `setEditorText(text)` |
| `getAllThemes()` / `getTheme()` | `[]` / `undefined` |
| `setTheme()` | `{ success:false, error:"Theme switching not supported in RPC mode" }` |
| `onTerminalInput()` | 返回空 unsubscribe |
| `setWidget` | 只支持 `string[]`，component factory 被忽略 |

注意 `ctx.mode === "rpc"` 但 **`ctx.hasUI === true`**（因为 dialog 和 notify 是通的）。要挡 TUI-specific 功能应判断 `ctx.mode === "tui"`。

#### 2.5.6 `get_commands` 的三类来源

```ts
for (const command of session.extensionRunner.getRegisteredCommands())
  commands.push({ name: command.invocationName, description, source: "extension", sourceInfo });
for (const template of session.promptTemplates)
  commands.push({ name: template.name, description, source: "prompt", sourceInfo });
for (const skill of session.resourceLoader.getSkills().skills)
  commands.push({ name: `skill:${skill.name}`, description, source: "skill", sourceInfo });
```

`RpcSlashCommand = { name: string; description?: string; source: "extension"|"prompt"|"skill"; sourceInfo: SourceInfo }`。

### 2.6 interactive mode 架构总览（6620 行，不求逐行）

#### 2.6.1 分层

```
main.ts
  └─ AgentSessionRuntime（持有 session + cwd-bound services，负责 session 替换）
       └─ InteractiveMode（TUI 呈现层，不持有业务逻辑）
            ├─ TuiMainScreen | TuiAltScreen（@earendil-works/pi-tui 的两种 renderer）
            ├─ createChatViewport() → VStack(ScrollView(document) + dock)
            │     dock = VStack(pendingMessages, status, widgetsAbove, editor, widgetsBelow, footer)
            ├─ 组件库 modes/interactive/components/*（约 50 个）
            └─ theme/（theme.ts、theme-controller.ts、theme-json.ts、dark.json、light.json）
```

关键字段（`interactive-mode.ts:376-504`）：`runtimeHost`、`renderer`、`ui: TUI`（经 `tui-renderer.ts` 的 Proxy 引用，允许替换 renderer）、七个 container（`document`/`chat`/`loadedResources`/`pendingMessages`/`status`/`editor`/`footer` + `widgetContainerAbove|Below`）、`headerContainer` + `builtInHeader`/`customHeader`/`customFooter`、`defaultEditor: CustomEditor` 与 `editor: EditorComponent`、`keybindings`、`pendingTools: Map<toolCallId, ToolExecutionComponent>`、`skillCommands: Map<commandName, filePath>`。

- `TuiMainScreen` = 常规模式（打印到主屏缓冲区）；`TuiAltScreen` = `--tui-mode fullscreen`（备用屏 + 可滚动区域 + 搜索 + 鼠标选择）。`tui-renderer.ts` 是唯一的组合根，`createInteractiveTui()` 按 `tuiMode` 分支，并注入 search 高亮样式、`scrollToEndIndicator`（"↓ Jump to latest message"）、`openUrl`、剪贴板回调。
- `switchTuiMode()` 允许运行期在两者间切换，而**组件树复用**（`mountInteractiveTui` 重新挂载同一批 container）。

#### 2.6.2 启动（`init()`）

1. `registerSignalHandlers()`；读 changelog（resumed session 跳过）；scoped models 非空且非 quiet → 打印 `Model scope: ...`。
2. `renderWidgets()` → `createChatViewport()` → `mountInteractiveTui()`；**先挂最小交互**（`app.clear` / Ctrl-D / submit→`handleStartupSubmit` 提示 "Startup is still in progress"）再 `ui.start()`，这样扩展的 `session_start` handler 也能弹 dialog。
3. `themeController.applyFromSettings()`；组装 header（logo + 折叠/展开两套 hints，`ExpandableText` 由 Ctrl+O 切换）。
4. `ensureTool("fd")` + `ensureTool("rg")`（缺失则下载并塞进 PATH）——先渲染 UI 再下载，避免启动看起来卡死。
5. `setupKeyHandlers()` + `setupEditorSubmitHandler()`。
6. `await this.rebindCurrentSession()` → `session.bindExtensions(...)`，绑定 `ExtensionUIContext`（`createExtensionUIContext()`）、`commandContextActions`（newSession/fork/navigateTree/switchSession/reload）与 `setupExtensionShortcuts()`。
7. `renderInitialMessages()`；注册 `onThemeChange` / `footerDataProvider.onBranchChange` 重绘；异步 `loadAllHighlightLanguages()`。

#### 2.6.3 渲染管线

- 事件入口是 `subscribeToAgent()` → `session.subscribe(async (event) => this.handleEvent(event))`。
- `handleEvent()` 是一个大 switch（`agent_start` / `turn_start` / `queue_update` / `entry_appended` / `session_info_changed` / `thinking_level_changed` / `message_start` / `message_update` / `message_end` / `bash_execution_update` / `tool_execution_start|update|end` / `agent_end` / `agent_settled` / `compaction_start|end` / `auto_retry_start|end` / `summarization_retry_*`）。
- 渲染策略：**增量构建持久组件**，而不是每帧重建整个 transcript。
  - `message_start`(assistant) → 创建 `AssistantMessageComponent` 记为 `streamingComponent`；
  - `message_update` → 基于 `assistantMessageEvent`（`text_delta`/`thinking_delta`/`toolcall_*`）就地更新该组件并 `ui.requestRender()`；
  - `message_end` → 固化；
  - `tool_execution_start/update/end` → `pendingTools: Map<toolCallId, ToolExecutionComponent>`，从 pending 提升为固定行；
  - `entry_appended` → `addCustomEntryToChat()`（走 `registerEntryRenderer`）。
- `rebuildChatFromMessages()` 用于 `/reload` 与 session 重建：从 session 消息全量重建 chat。
- `components/` 目录：`assistant-message`、`user-message`、`tool-execution`、`bash-execution`、`diff`、`markdown-transform`、`mermaid`、`custom-entry`、`custom-message`、`skill-invocation-message`、`branch-summary-message`、`compaction-summary-message`、`extension-{selector,input,editor}`、`session-selector(-search)`、`tree-selector`、`model-selector`、`scoped-models-selector`、`settings-selector(-submenu)`、`theme-selector`、`thinking-selector`、`trust-selector`、`login-dialog`、`oauth-selector`、`user-message-selector`、`footer`、`status-indicator`、`bordered-loader`、`dynamic-border`、`visual-truncate`、`keybinding-hints`、`first-time-setup` 等。

#### 2.6.4 keybindings

默认表定义在 `src/core/keybindings.ts`（`KEYBINDINGS = { ...TUI_KEYBINDINGS, ...AppKeybindings }`），用户覆盖文件 `~/.pi/agent/keybindings.json`，`/reload` 生效。

App 级 action（`AppKeybinding` 联合类型，共 43 个）：

| Action | 默认 | Action | 默认 |
|---|---|---|---|
| `app.interrupt` | `escape` | `app.model.select` | `ctrl+l` |
| `app.clear` | `ctrl+c` | `app.tools.expand` | `ctrl+o` |
| `app.exit` | `ctrl+d` | `app.thinking.toggle` | `ctrl+t` |
| `app.suspend` | `ctrl+z`（Windows 无） | `app.editor.external` | `ctrl+g` |
| `app.thinking.cycle` | `shift+tab` | `app.message.copy` | `ctrl+x` |
| `app.thinking.save` | `ctrl+s` | `app.message.followUp` | `alt+enter`（Win/WSL `ctrl+q`） |
| `app.model.cycleForward` | `ctrl+p` | `app.message.dequeue` | `alt+up`（Win/WSL `alt+q`） |
| `app.model.cycleBackward` | `shift+ctrl+p`（Win/WSL `alt+p`） | `app.clipboard.pasteImage` | `ctrl+v`（Win/WSL `alt+v`） |
| `app.session.new/tree/fork/resume` | **无默认键** | `app.session.toggleNamedFilter` | `ctrl+n` |
| `app.session.togglePath` | `ctrl+p`（选择器内） | `app.session.rename/delete` | `ctrl+r` / `ctrl+d` |
| `tui.input.submit` | `enter` | `tui.input.newLine` | `shift+enter` / `ctrl+j` |
| `tui.editor.undo` | `ctrl+-`（Win `ctrl+z`，WSL `alt+z`） | `tui.altScreen.search` | `ctrl+shift+f`（Win/WSL `ctrl+f`） |

`KeybindingsManager.create()` 读取并做旧 id 迁移（`cursorUp` → 命名空间 id）。扩展的 `registerShortcut` 与 keybindings.json 竞争时，一个**保留表**保护内建行为：

```ts
const RESERVED_KEYBINDINGS_FOR_EXTENSION_CONFLICTS = [
  "app.interrupt","app.clear","app.exit","app.suspend","app.thinking.cycle",
  "app.model.cycleForward","app.model.cycleBackward","app.model.select",
  "app.tools.expand","app.thinking.toggle","app.editor.external","app.message.copy",
  "app.message.followUp","tui.input.submit","tui.select.confirm","tui.select.cancel",
  "tui.input.copy","tui.editor.deleteToLineEnd",
] as const;
```

冲突诊断（`getShortcutDiagnostics()`）会报 `restrictOverride` 违规。

#### 2.6.5 `!` / `!!` bash 模式

`setupEditorSubmitHandler` 里：`text.startsWith("!")` → `!!` 表示 `excludeFromContext`，否则普通；若 `session.isBashRunning` 则警告并要求先 Esc；`handleBashCommand(command, isExcluded)`。这走的是 `user_bash` 扩展事件 + `session.executeBash()`。

---

## 3. Slash 命令

### 3.1 内建命令全表（`src/core/slash-commands.ts`）

```ts
export const BUILTIN_SLASH_COMMANDS: ReadonlyArray<BuiltinSlashCommand> = [
  { name: "settings",      description: "Open settings menu" },
  { name: "model",         description: "Select model (opens selector UI)", argumentHint: "<provider/model>" },
  { name: "tree",          description: "Navigate session tree (switch branches)" },
  { name: "thinking",      description: "Set thinking level", argumentHint: "<level>" },
  { name: "scoped-models", description: "Enable/disable models for Ctrl+P cycling" },
  { name: "export",        description: "Export session (HTML default, or specify path: .html/.jsonl)" },
  { name: "import",        description: "Import and resume a session from a JSONL file" },
  { name: "share",         description: "Share session as a secret GitHub gist" },
  { name: "copy",          description: "Copy last agent message to clipboard" },
  { name: "name",          description: "Set session display name" },
  { name: "session",       description: "Show session info and stats" },
  { name: "changelog",     description: "Show changelog entries" },
  { name: "hotkeys",       description: "Show all keyboard shortcuts" },
  { name: "fork",          description: "Create a new fork from a previous user message" },
  { name: "clone",         description: "Duplicate the current session at the current position" },
  { name: "trust",         description: "Save project trust decision for future sessions" },
  { name: "login",         description: "Configure provider authentication", argumentHint: "<provider>" },
  { name: "logout",        description: "Remove provider authentication" },
  { name: "new",           description: "Start a new session" },
  { name: "compact",       description: "Manually compact the session context" },
  { name: "resume",        description: "Resume a different session" },
  { name: "reload",        description: "Reload keybindings, extensions, skills, prompts, themes, and context files" },
  { name: "quit",          description: `Quit ${APP_NAME}` },
];
```

### 3.2 逐个行为

| 命令 | 行为 | 实现 |
|---|---|---|
| `/settings` | 打开设置选择器（主题、thinking 隐藏、quiet startup、outputPad、skill commands、终端能力覆盖…） | `showSettingsSelector` / `settings-submenu` |
| `/model [search]` | 模糊搜索模型选择器；写回 default provider/model | `handleModelCommand` → `showModelSelector` |
| `/thinking [query]` | 选择 thinking level，可持久化 | `handleThinkingCommand` → `showThinkingSelector` |
| `/scoped-models` | 勾选 Ctrl+P 循环用的模型集合 | `showModelsSelector` |
| `/tree` | session 树导航（切换分支、折叠、过滤、打 label） | `showTreeSelector` + `tree-selector.ts` |
| `/fork` | 从历史 user message 处 fork | `showUserMessageSelector` |
| `/clone` | 在当前位置复制 session | `handleCloneCommand` |
| `/resume` | session 选择器（跨 project） | `showSessionSelector` → `handleResumeSession` |
| `/new` | 新 session（**同时绑定 `app.session.new` 快捷键**） | `handleClearCommand` |
| `/compact [instructions]` | 手动压缩；可在流式中途排队 | `handleCompactCommand` |
| `/reload` | 重载 keybindings/extensions/skills/prompts/themes/context files | `handleReloadCommand` |
| `/login [provider]` / `/logout` | OAuth / API key 流程；有 provider → authType 选择器 → 登录 | `handleLoginCommand` / `showOAuthSelector` |
| `/trust` | 保存项目信任决定 | `showTrustSelector` |
| `/export [path]` | 默认 HTML；`.jsonl` 走 JSONL 导出 | `handleExportCommand` |
| `/import <path>` | 导入并恢复 JSONL session（文件不存在 → `SessionImportFileNotFoundError`） | `handleImportCommand` |
| `/share` | 分享为 secret GitHub gist（`PI_SHARE_VIEWER_URL`，默认 `https://pi.dev/session/`） | `handleShareCommand` |
| `/copy` | 复制最后一条 assistant 消息 | `handleCopyCommand` |
| `/name <name>` | 设置 session 显示名 | `handleNameCommand` |
| `/session` | 打印 session 信息与统计 | `handleSessionCommand` |
| `/changelog` | 显示新 changelog 条目 | `handleChangelogCommand` |
| `/hotkeys` | 打印所有快捷键（区分 app / editor 两套） | `handleHotkeysCommand` |
| `/quit` | 优雅退出 | `shutdown()` |
| **隐藏命令** | `/debug`（诊断）、`/arminsayshi`、`/dementedelves`、`/daxnuts`（彩蛋） | 各有 handler |
| **动态命令** | `/skill:<name> [args]`、prompt 模板 `/name [args]`、扩展 `registerCommand` | 见 §3.3 |

注意：**没有 `/clear`**。`app.clear` 是 keybinding action 名（Ctrl+C 清 editor），`/new` 复用了 `handleClearCommand`。

### 3.3 合并顺序与冲突

Autocomplete 的完整列表顺序（`createBaseAutocompleteProvider`）：

```
[...builtinSlashCommands, ...templateCommands, ...extensionCommands, ...skillCommandList]
```

- `templateCommands` ← `session.promptTemplates`，带 `sourceInfo` 前缀描述（`prefixAutocompleteDescription`）。
- `extensionCommands` ← `extensionRunner.getRegisteredCommands()`。
- `skillCommandList` ← 每个 skill 生成 `skill:<name>`，并记录 `this.skillCommands.set(commandName, skill.filePath)`；受 `settings.enableSkillCommands` 控制（默认 `true`）。
- **冲突诊断**：扩展命令名与内建同名时，`getBuiltInCommandConflictDiagnostics()` 产出 warning —— 若扩展名重复（`invocationName !== name`）则「以 `/name:2` 形式可用」，否则「从 autocomplete 中跳过」。

扩展命令的 `invocationName` 生成规则（`resolveRegisteredCommands`）：同名命令出现多次时第 N 个为 `name:N`，并保证 `takenInvocationNames` 不冲突。

### 3.4 交互模式的命令 dispatch

**内建命令是硬编码的 if 链**（`setupEditorSubmitHandler`，行 2964+），顺序即优先级，每条都以 `this.editor.setText("")` 清空输入：

```ts
if (text === "/settings") { ... }
if (text === "/model" || text.startsWith("/model ")) { ... }
...
if (text.startsWith("!")) { /* bash */ }
if (this.session.isCompacting) { /* 扩展命令立刻执行，其它 steer 排队 */ }
if (this.session.isStreaming) { await this.session.prompt(text, { streamingBehavior: "steer" }); }
// 正常提交
```

未命中的 `/xxx` 会落到 `session.prompt(text)`，由 `AgentSession.prompt()` 继续处理。

### 3.5 非交互模式的命令分发（`AgentSession.prompt`）

`agent-session.ts:1175` 是扩展/模板/skill 命令的统一入口：

```ts
async prompt(text: string, options?: PromptOptions): Promise<void> {
  const expandPromptTemplates = options?.expandPromptTemplates ?? true;
  // ① 扩展命令：立即执行（即使正在 streaming），自己管理 LLM 交互
  if (expandPromptTemplates && text.startsWith("/")) {
    const handled = await this._tryExecuteExtensionCommand(text);
    if (handled) { preflightResult?.(true); return; }
  }
  if (this._compactionAbortController !== undefined) throw new Error("Cannot submit a prompt while compaction is in progress...");
  // ② input 事件（可 transform / handled）
  const processedInput = await this._runInputHandlers(text, options?.images, options?.source ?? "interactive", ...);
  if (!processedInput) { preflightResult?.(true); return; }
  // ③ skill 命令 → prompt 模板
  let expandedText = currentText;
  if (expandPromptTemplates) {
    expandedText = this._expandSkillCommand(expandedText);           // /skill:name
    expandedText = expandPromptTemplate(expandedText, [...this.promptTemplates]); // /template
  }
  // ④ 流式中则 steer/followUp 排队；否则校验 model + auth，压缩检查，before_agent_start，最后 _runAgentPrompt
}
```

**优先级：extension command → input 事件 → skill → prompt template**（前者命中则短路）。

skill 展开格式（`_expandSkillCommand`）：

```
<skill name="brave-search" location="/abs/path/SKILL.md">
References are relative to /abs/path.

<SKILL.md body（已剥 frontmatter）>
</skill>

<args>
```

`parseSkillBlock()` 用正则反向解析：

```ts
/^<skill name="([^"]+)" location="([^"]+)">\n([\s\S]*?)\n<\/skill>(?:\n\n([\s\S]+))?$/
```

`steer()` / `followUp()` 会**拒绝**扩展命令（`_throwIfExtensionCommand`），但会展开 skill 与模板。

`AgentSession` 还暴露 `getCommands(): SlashCommandInfo[]`，返回 **extension + prompt + skill 三类**（不含内建，内建只存在于 interactive autocomplete 与硬编码 if 链中）。顺序固定为：

```ts
return [...extensionCommands, ...templates, ...skills];
```

RPC `get_commands` 与扩展 `pi.getCommands()` 都走这一份数据。

---

## 4. 扩展系统

### 4.1 加载：jiti + 发现顺序

扩展 = 一个默认导出工厂函数的 TS/JS 模块：

```ts
// ~/.pi/agent/extensions/hello.ts
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) { /* ... */ }
// 或 export default async function (pi) { ... }
```

加载器用 **jiti 2.7.0**（`jiti/static`）动态 import，因此 TS 无需编译：

```ts
const jiti = createJiti(import.meta.url, {
  moduleCache: false,
  ...(isBunBinary || isNodeSeaBinary || isBundledNode
    ? { virtualModules: VIRTUAL_MODULES, tryNative: false }
    : isTypeScriptSourceRuntime
      ? { virtualModules: VIRTUAL_MODULES, tsconfigPaths: true }
      : { alias: getAliases() }),
});
const module = await jiti.import(extensionPath, { default: true });
```

- `VIRTUAL_MODULES` 把 `typebox`、`@earendil-works/pi-{ai,agent-core,tui,coding-agent}`（含旧的 `@mariozechner/*` 别名）注入，供编译后的二进制使用；未编译时用 `alias`/`tsconfigPaths`。**这是「扩展 import 宿主包」的实现细节，Python 侧应对应提供 `pi` 包本身 + 一个 `sys.modules` 注入层。**
- 返回值必须是 function，否则报 `Extension does not export a valid factory function`。
- **缓存**：`extensionCache: Map<path, factory>`，key 由 `(resolvedCwd, generation)` 组成，cwd 变化自动 `clearExtensionCache()`。
- 工厂可以是 async；`await factory(load.api)`，成功后 `commit()`，失败 `discard()`（回滚 pending flag value 与 event bus 订阅）。
- 工厂注册期间 `registerProvider` 等副作用被放进 `pendingRuntimeChanges`，**等 runner bindCore 之后 flush**；`registerFlag` 的默认值进 `pendingFlagValues`。

发现顺序（`discoverAndLoadExtensions`，按 `seen` 集合去重，先到先得）：

1. `cwd/.pi/extensions/`（project-local，需 trust）
2. `<agentDir>/extensions/`（global，默认 `~/.pi/agent/extensions/`）
3. `configuredPaths`（settings `extensions` 数组 + CLI `-e`）

目录内发现规则（**只递归一层**）：

1. 直接文件 `extensions/*.ts` / `*.js`
2. 子目录含 `index.ts` / `index.js`
3. 子目录含 `package.json` 且有 `pi.extensions` 字段 → 加载其中声明的路径

`pi install` 装入的包（npm/git）由 `ResourceLoader` 通过 `DefaultPackageManager.resolve()` 展开，再按 manifest `pi.extensions` / 约定目录 `extensions/` 发现。

### 4.2 `ExtensionAPI` 全量方法（`types.ts:1252-1506`）

```ts
export interface ExtensionAPI {
  // ---- 事件订阅（33 个重载，事件全集见 §4.5） ----
  on(event: "project_trust", handler: ProjectTrustHandler): void;
  on(event: "resources_discover", handler: ExtensionHandler<ResourcesDiscoverEvent, ResourcesDiscoverResult>): void;
  on(event: "session_start" | "session_info_changed" | "session_compact" | "session_compact_failed"
         | "session_shutdown" | "session_tree", handler: ExtensionHandler<...>): void;
  on(event: "session_before_switch" | "session_before_fork" | "session_before_compact"
         | "session_before_tree", handler: ExtensionHandler<..., {cancel?: boolean}>): void;
  on(event: "context" | "before_provider_request" | "before_provider_headers" | "after_provider_response"
         | "before_agent_start" | "agent_start" | "agent_end" | "agent_settled"
         | "ui_prompt_start" | "ui_prompt_end"
         | "turn_start" | "turn_end"
         | "message_start" | "message_update" | "message_end"
         | "tool_execution_start" | "tool_execution_update" | "tool_execution_end"
         | "model_select" | "thinking_level_select" | "tool_call" | "tool_result"
         | "user_bash" | "input", handler: ExtensionHandler<...>): void;

  // ---- 工具 ----
  registerTool<TParams, TDetails, TState>(tool: ToolDefinition<...>): void;

  // ---- 命令 / 快捷键 / CLI flag ----
  registerCommand(name: string, options: Omit<RegisteredCommand, "name" | "sourceInfo">): void;
  registerShortcut(shortcut: KeyId, options: { description?; handler: (ctx: ExtensionContext) => Promise<void>|void }): void;
  registerFlag(name: string, options: { description?; type: "boolean"; default?: boolean } | { description?; type: "string"; default?: string }): void;
  getFlag(name: string): boolean | string | undefined;

  // ---- 渲染 ----
  registerMessageRenderer<T>(customType: string, renderer: MessageRenderer<T>): void;
  registerMarkdownTransformer(transformer: MarkdownTransformer): void;
  registerEntryRenderer<T>(customType: string, renderer: EntryRenderer<T>): void;

  // ---- 动作 ----
  sendMessage<T>(message: Pick<CustomMessage<T>, "customType"|"content"|"display"|"details">,
                 options?: { triggerTurn?: boolean; deliverAs?: "steer"|"followUp"|"nextTurn" }): void;
  sendUserMessage(content: string | (TextContent|ImageContent)[],
                  options?: { deliverAs?: "steer"|"followUp"; expandPromptTemplates?: boolean }): void;
  appendEntry<T>(customType: string, data?: T): void;

  // ---- session 元数据 / 执行 / 工具集 ----
  setSessionName(name: string): void;  getSessionName(): string | undefined;
  setLabel(entryId: string, label: string | undefined): void;
  exec(command: string, args: string[], options?: ExecOptions): Promise<ExecResult>;
  getActiveTools(): string[];  getAllTools(): ToolInfo[];  setActiveTools(toolNames: string[]): void;
  getCommands(): SlashCommandInfo[];

  // ---- model / thinking ----
  setModel(model: Model<any>): Promise<boolean>;   // 未配置 auth 时返回 false
  getThinkingLevel(): ThinkingLevel;  setThinkingLevel(level: ThinkingLevel): void;

  // ---- provider ----
  registerProvider(provider: Provider): void;
  registerProvider(name: string, config: ProviderConfig): void;
  unregisterProvider(name: string): void;

  // ---- 事件总线 ----
  events: EventBus;   // { emit(channel, data); on(channel, handler): () => void }
}
```

实现要点（`loader.ts:252-482` 的 `createExtensionAPI`）：

- 每个注册方法都先 `assertActive()`；工厂失败后 `state === "failed"`，再调用会抛 `Extension "<path>" failed to load and its API is no longer active.`
- `registerTool` 强校验 `parameters` 必须是非 null 非数组的 object（TypeBox schema）。
- `registerFlag` 校验 `typeof options.default === options.type`。
- `exec()` 走 `execCommand(command, args, options?.cwd ?? cwd, options)`。
- `events.on()` 的订阅被 `runtime.trackEventBusSubscription()` 追踪，扩展失效时统一 unsubscribe。
- 运行时替换后旧 API 变 stale，错误信息明确指导：`ctx.newSession()/fork()/switchSession()` 之后要改用 `withSession(ctx)` 提供的 ctx；`ctx.reload()` 之后不得再用旧 ctx。

#### 4.2.1 `ToolDefinition`（`registerTool` 的输入）

```ts
export interface ToolDefinition<TParams extends TSchema = TSchema, TDetails = unknown, TState = any> {
  name: string;                 // LLM 看到的工具名
  label: string;                // UI 标签
  description: string;          // 给 LLM
  promptSnippet?: string;       // 出现于系统提示 "Available tools"；缺省则不出现在其中
  promptGuidelines?: string[];  // 追加到系统提示 Guidelines
  parameters: TParams;          // TypeBox schema（必须是 object 根）
  constrainedSampling?: false | ConstrainedSamplingConfig;
  renderShell?: "default" | "self";
  prepareArguments?: (args: unknown) => Static<TParams>;   // schema 校验前的兼容 shim
  executionMode?: ToolExecutionMode;                        // "sequential" | "parallel"
  execute(toolCallId, params, signal, onUpdate, ctx: ExtensionContext): Promise<AgentToolResult<TDetails>>;
  renderCall?(args, theme, context: ToolRenderContext<TState, Static<TParams>>): Component;
  renderResult?(result, options: ToolRenderResultOptions, theme, context): Component;
}
export function defineTool<...>(tool): ...   // 保留参数类型推断
```

`ToolRenderContext` 提供 `args / toolCallId / invalidate() / lastComponent / state / cwd / executionStarted / argsComplete / isPartial / expanded / showImages / isError`——这些字段说明 TUI 是**增量渲染 + 状态复用**的。

工具覆盖内建：`pi registerTool` 的同名工具替换内建（`--no-builtin-tools` 时只剩扩展工具）；`--tools`/`--exclude-tools` 同时作用于内建、扩展和 SDK custom tools。

### 4.3 `ExtensionContext` / `ExtensionCommandContext` / `ReplacedSessionContext`

```ts
export type ExtensionMode = "tui" | "rpc" | "json" | "print";

export interface ExtensionContext {
  ui: ExtensionUIContext;          // 见 §4.4
  mode: ExtensionMode;             // ctx.mode === "tui" 才可假设有 TUI
  hasUI: boolean;                  // TUI + RPC 为 true
  cwd: string;
  sessionManager: ReadonlySessionManager;
  modelRegistry: ModelRegistry;
  model: Model<any> | undefined;
  scopedModels: readonly ScopedModel[];
  thinkingLevel?: ThinkingLevel;
  isIdle(): boolean;
  isProjectTrusted(): boolean;
  signal: AbortSignal | undefined;  // streaming 中才有
  abort(): void;
  hasPendingMessages(): boolean;
  shutdown(): void;                 // 所有模式可用（RPC 下在 agent_settled 后退出）
  getContextUsage(): ContextUsage | undefined;   // { tokens: number|null; contextWindow; percent: number|null }
  compact(options?: CompactOptions): void;       // { customInstructions?, onComplete?, onError? } 不 await
  getSystemPrompt(): string;
}
```

```ts
export interface ExtensionCommandContext extends ExtensionContext {
  getSystemPromptOptions(): BuildSystemPromptOptions;
  waitForIdle(): Promise<void>;
  newSession(options?: { parentSession?; setup?(sm: SessionManager): Promise<void>;
                         withSession?(ctx: ReplacedSessionContext): Promise<void> }): Promise<{cancelled: boolean}>;
  fork(entryId: string, options?: { position?: "before"|"at"; withSession? }): Promise<{cancelled: boolean}>;
  navigateTree(targetId: string, options?: { summarize?; customInstructions?; replaceInstructions?; label? }): Promise<{cancelled: boolean}>;
  switchSession(sessionPath: string, options?: { withSession? }): Promise<{cancelled: boolean}>;
  reload(): Promise<void>;
}
```

`ReplacedSessionContext extends ExtensionCommandContext` 额外提供 `sendMessage` / `sendUserMessage`，是 session 替换后**唯一合法**的 ctx。

`createContext()`（runner:723）全部用 getter + `assertActive()` 实现「惰性 + 失效检测」；`createCommandContext()` 特意用 `Object.defineProperties` 复制 descriptor 而非 spread，避免提前求值冻结旧值。

### 4.4 `ExtensionUIContext`（每个模式一套实现）

方法共 25 个（`types.ts:133-284`）：`select` / `confirm` / `input` / `notify` / `onTerminalInput` / `setStatus` / `setWorkingMessage` / `setWorkingVisible` / `setWorkingIndicator` / `setHiddenThinkingLabel` / `setWidget`（重载：`string[]` 或 component factory）/ `setFooter` / `setHeader` / `setTitle` / `custom<T>()` / `pasteToEditor` / `setEditorText` / `getEditorText` / `editor` / `addAutocompleteProvider` / `setEditorComponent` / `getEditorComponent` / `theme`(getter) / `getAllThemes` / `getTheme` / `setTheme` / `getToolsExpanded` / `setToolsExpanded`。

`ExtensionUIDialogOptions = { signal?: AbortSignal; timeout?: number }`；`timeout` 会显示 live countdown。
`WorkingIndicatorOptions = { frames?: string[]; intervalMs?: number }`，`frames: []` 完全隐藏指示器。
`WidgetPlacement = "aboveEditor" | "belowEditor"`（interactive 下 `MAX_WIDGET_LINES = 10`）。

### 4.5 完整事件表（payload / 是否可拦截）

| 事件 | payload 关键字段 | handler 返回值 | 语义 |
|---|---|---|---|
| `project_trust` | `{ type, cwd }` | `ProjectTrustResult = { trusted: "yes"\|"no"\|"undecided"; remember?: boolean }` | 参与信任决策；`undecided` 交给下一个 handler |
| `resources_discover` | `{ cwd, reason: "startup"\|"reload" }` | `{ skillPaths?, promptPaths?, themePaths? }` | 扩展注入额外资源目录（三路合并） |
| `session_start` | `{ reason: "startup"\|"reload"\|"new"\|"resume"\|"fork"; previousSessionFile? }` | — | session 建立/重载 |
| `session_info_changed` | `{ name: string \| undefined }` | — | 名称变化 |
| `session_before_switch` | `{ reason: "new"\|"resume"; targetSessionFile? }` | `{ cancel?: boolean }` | **可取消** |
| `session_before_fork` | `{ entryId; position: "before"\|"at" }` | `{ cancel?; skipConversationRestore? }` | **可取消** |
| `session_before_compact` | `{ preparation; branchEntries; customInstructions?; reason: "manual"\|"threshold"\|"overflow"; willRetry; signal }` | `{ cancel?; compaction?: CompactionResult }` | **可取消 / 可提供压缩结果** |
| `session_compact` | `{ compactionEntry; fromExtension; reason; willRetry }` | — | 通知 |
| `session_compact_failed` | `{ reason; errorMessage?; aborted; willRetry; fromExtension }` | — | 通知 |
| `session_shutdown` | `{ reason: "quit"\|"reload"\|"new"\|"resume"\|"fork"; targetSessionFile? }` | — | 清理时机；**不 yield 事件循环**（`beforeSessionInvalidate` 供 UI 拆组件） |
| `session_before_tree` | `{ preparation: TreePreparation; signal }` | `{ cancel?; summary?; customInstructions?; replaceInstructions?; label? }` | **可取消 / 可自定义摘要** |
| `session_tree` | `{ newLeafId; oldLeafId; summaryEntry?; fromExtension? }` | — | 通知 |
| `context` | `{ messages: AgentMessage[] }` | `{ messages?: AgentMessage[] }` | **可替换整个上下文**（handler 链式传递，输入先 `structuredClone`） |
| `before_provider_request` | `{ payload: unknown }` | `unknown`（非 undefined 则替换 payload） | **可替换 provider 请求体** |
| `before_provider_headers` | `{ headers: ProviderHeaders }` | 忽略返回值（**原地 mutate**；`null` 删除 header） | 注入 tracing/session header |
| `after_provider_response` | `{ status: number; headers: Record<string,string> }` | — | 响应到达、消费 body 之前 |
| `before_agent_start` | `{ prompt; images?; systemPrompt; systemPromptOptions }` | `{ message?: CustomMessage-like; systemPrompt?: string }` | **可注入自定义消息 + 替换系统提示**（多扩展的 systemPrompt 链式；messages 全部追加） |
| `agent_start` / `agent_end` / `agent_settled` | `{}` / `{ messages }` / `{}` | — | 生命周期；`agent_settled` 表示不会再有重试/压缩/排队 |
| `ui_prompt_start` / `ui_prompt_end` | `{ reason: "ui_prompt"; kind: "select"\|"confirm"\|"input"\|"editor"\|"custom"; title? }` | — | 内建 UI 阻塞提示（由 runner 自动配对 emit） |
| `turn_start` / `turn_end` | `{ turnIndex; timestamp }` / `{ turnIndex; message; toolResults }` | — | 轮次 |
| `message_start` / `message_update` / `message_end` | `{ message }` / `{ message; assistantMessageEvent }` | `message_end` 可返回 `{ message }`（**必须保持相同 role**） | 流式；`message_end` 可改终态 |
| `tool_execution_start` / `_update` / `_end` | `{ toolCallId; toolName; args }` / `+partialResult` / `+result; isError` | — | 执行生命周期（只读观察） |
| `model_select` | `{ model; previousModel; source: "set"\|"cycle"\|"restore" }` | — | |
| `thinking_level_select` | `{ level; previousLevel }` | — | |
| `user_bash` | `{ command; excludeFromContext; cwd }` | `{ operations?: BashOperations; result?: BashResult }` | **可完全接管执行** |
| `input` | `{ text; images?; source: "interactive"\|"rpc"\|"extension"; streamingBehavior? }` | `InputEventResult = {action:"continue"} \| {action:"transform"; text; images?} \| {action:"handled"}` | **可改写/吞掉用户输入**；`handled` 短路，`transform` 链式 |
| `tool_call` | `{ type:"tool_call"; toolCallId; toolName; input }`（按工具名细分为 BashToolCallEvent 等 9 种） | `{ block?: boolean; reason?: string; terminate?: boolean }` | **可阻断**；改参数用 **原地 mutate `event.input`**；`block` 立即返回并跳过后续 handler |
| `tool_result` | `{ toolCallId; toolName; input; content; isError; details; usage? }` | `{ content?; details?; isError?; usage? }` | **可改写工具结果** |

要点：

- **可取消**：`session_before_*` 系列（`cancel: true` 立即返回）。
- **可改写**：`context`、`before_provider_request`、`before_agent_start`、`message_end`、`tool_call`（mutate input）、`tool_result`、`input`。
- **可阻断**：`tool_call` 的 `block`；`tool_call` handler **抛错也等价于 block**（fail-safe，见 docs/extensions.md）。
- **短路语义**：`tool_call` 的 `block`、`input` 的 `handled`、`user_bash` 的第一个非空结果。
- **观察型事件**（`agent_*`、`turn_*`、`message_start/update`、`tool_execution_*`、`model_select`、`thinking_level_select`、`session_compact*`、`session_tree`）返回值被忽略。
- 除 `project_trust` 外所有 handler 都包在 try/catch 里，异常经 `emitError()` → `onError` listener 上报（TUI 显示、print 打到 stderr、RPC 输出 `extension_error` 事件），**agent 继续运行**。

类型守卫：`isBashToolResult` / `isEditToolResult` / `isReadToolResult` / `isWriteToolResult` / `isGrepToolResult` / `isFindToolResult` / `isLsToolResult` / `isPowerShellToolResult`，以及 `isToolCallEventType("bash", event)`（内建工具自动收窄；自定义工具需显式类型参数）。

### 4.6 `registerProvider` / `ProviderConfig`

```ts
export interface ProviderConfig {
  name?: string;
  baseUrl?: string;
  apiKey?: string;                 // 字面量、$ENV / ${ENV} 或 !command
  api?: Api;                       // 定义 models 时必填
  streamSimple?: (model, context, options?) => AssistantMessageEventStream;  // 自定义 API
  headers?: Record<string, string>;
  authHeader?: boolean;            // 自动加 Authorization: Bearer
  models?: ProviderModelConfig[];  // 提供则替换该 provider 全部模型
  refreshModels?(context: RefreshModelsContext): Promise<ProviderModelConfig[]>;
  oauth?: {
    name: string;
    isSubscription?: boolean;
    usesCallbackServer?: boolean;  // @deprecated
    login(callbacks: OAuthLoginCallbacks): Promise<OAuthCredentials>;
    refreshToken(credentials: OAuthCredentials, signal: AbortSignal): Promise<OAuthCredentials>;
    getApiKey(credentials: OAuthCredentials): string;
    modifyModels?(models: Model<Api>[], credentials: OAuthCredentials): Model<Api>[];
  };
}
```

语义（docs 明确）：只给 `baseUrl` → 覆盖已有模型的 URL；给 `models` → 整体替换；给 `oauth` → 使 `/login` 支持该 provider。加载期调用会入队，runner bind 后立即生效；之后调用立刻生效（无需 `/reload`）。

### 4.7 `ExtensionRunner` 的运行时职责

`src/core/extensions/runner.ts`：

- 组合：`extensions: Extension[]` + 共享 `ExtensionRuntime` + `uiContext` + `mode` + 各类 handler 注入（`bindCore` / `bindCommandContext` / `setUIContext`）。
- 查询：`getAllRegisteredTools()`（**同名先注册者胜**）、`getToolDefinition(name)`、`getShortcuts(resolvedKeybindings)`、`getFlags()`、`getMessageRenderer(customType)`、`getMarkdownTransformers()`、`getEntryRenderer(customType)`、`getRegisteredCommands()` / `getCommand(name)`、`getCommandDiagnostics()` / `getShortcutDiagnostics()`。
- 发射：`emit(event)`（通用 + session_before 取消）、`emitMessageEnd`、`emitToolResult`、`emitToolCall`、`emitUserBash`、`emitContext`、`emitBeforeProviderRequest`、`emitBeforeProviderHeaders`、`emitBeforeAgentStart`、`emitResourcesDiscover`、`emitInput`。
- 生命周期：`invalidate(message)` 让所有 ctx 变 stale；`onError(listener)` 注册错误监听；`shutdown()` 触发当前模式的 shutdownHandler。
- **handler 遍历顺序 = 扩展加载顺序**（project → global → configured，各层内按目录字典序 / 数组顺序），同一事件同一扩展内按注册顺序。

### 4.8 内建扩展（`src/extensions/index.ts`）

`builtInExtensions` 随 CLI 一起注入（`main.ts`: `const extensionFactories = [...builtInExtensions, ...(options?.extensionFactories ?? [])]`），`InlineExtension` 支持匿名函数或 `{ name, factory, hidden? }`（启动列表里显示为 `<inline:name>`）。SDK 用户也可用 `main(args, { extensionFactories })` 注入。

---

## 5. Skills / Prompt Templates / Themes

### 5.1 Skills

**磁盘格式**：一个目录 + `SKILL.md`：

```
my-skill/
├── SKILL.md      # frontmatter + 指令
├── scripts/      # 任意辅助文件
├── references/
└── assets/
```

`SKILL.md` frontmatter：

```yaml
---
name: brave-search
description: Web search via the Brave API.
disable-model-invocation: false
---
```

**发现位置**（`core/skills.ts` + docs）：

| 来源 | 路径 | 规则 |
|---|---|---|
| Global | `~/.pi/agent/skills/` | 根级 `.md`（有合法 skill frontmatter + 非空 description）也算独立 skill |
| Global | `~/.agents/skills/` | 根级 `.md` **忽略**，子目录里的 `.md` 若声明 frontmatter 才算 |
| Project（需 trust） | `.pi/skills/` | 同 global 规则 |
| Project | `.agents/skills/`（cwd 及祖先目录，直到 git 仓库根 / 文件系统根） | 同 `~/.agents` 规则 |
| Packages | `skills/` 约定目录或 `pi.skills` manifest | |
| Settings | `skills` 数组（文件或目录） | 支持指向 `~/.claude/skills`、`~/.codex/skills` |
| CLI | `--skill <path>`（可重复） | `--no-skills` 时仍然加载 |

递归规则：目录内有 `SKILL.md` → 视为 skill root，**不再向下递归**；否则递归子目录查找 `SKILL.md`。会遵守 `.gitignore` / `.ignore` / `.fdignore`（用 `ignore` npm 包，带目录前缀重写），跳过 `.` 开头目录与 `node_modules`。

**校验**（Agent Skills spec）：
- name ≤ 64 字符，`^[a-z0-9-]+$`，不以 `-` 开头/结尾，不含连续 `--`；缺省取父目录名。
- description 必填、≤ 1024 字符。
- 校验失败只产生 warning，**仍然加载**（除非 description 缺失）。
- 同名冲突：`addSkills` 里先到者胜，产出 `type: "collision"` diagnostic（含 `winnerPath` / `loserPath`）。

**注入**：`formatSkillsForPrompt()` 生成 XML（agentskills.io 规范）：

```xml
The following skills provide specialized instructions for specific tasks.
Use the read tool to load a skill's file when the task matches its description.
...
<available_skills>
  <skill>
    <name>brave-search</name>
    <description>...</description>
    <location>/abs/path/SKILL.md</location>
  </skill>
</available_skills>
```

`disableModelInvocation: true` 的 skill 不进 prompt，只能 `/skill:name` 显式调用。这是 progressive disclosure：系统提示只有 name/description/location，全文按需 read。

### 5.2 Prompt Templates

- 位置：`~/.pi/agent/prompts/*.md`（user）、`.pi/prompts/*.md`（project，需 trust）、packages `prompts/`、settings `prompts` 数组、`--prompt-template`。
- **非递归**，只取目录下直接 `.md`。
- frontmatter：

```markdown
---
description: Review staged git changes
argument-hint: "<PR-URL>"
---
Review the staged changes (`git diff --cached`). Focus on:
- Bugs and logic errors
```

- 命令名 = 文件名去 `.md`；`description` 缺省取第一个非空行（截断 60 字符 + `...`）。
- 参数替换（`substituteArgs`，支持 bash 风格）：

| 语法 | 语义 |
|---|---|
| `$1` `$2` … | 位置参数（1-indexed） |
| `$@` / `$ARGUMENTS` | 全部参数空格连接 |
| `${1:-default}` / `${@:-default}` / `${ARGUMENTS:-default}` | 有值用值（空也算无），否则 default |
| `${@:N}` | 从第 N 个起的所有参数 |
| `${@:N:L}` | 从第 N 个起 L 个 |

替换**只作用于模板串本身**，参数/default 里的 `$1` 不会被递归替换。参数解析 `parseCommandArgs` 支持 `"` 和 `'` 引号。

### 5.3 Themes

- 位置：内建 `dark` / `light`；`~/.pi/agent/themes/*.json`；`.pi/themes/*.json`（需 trust）；packages `themes/`；settings `themes`；`--theme`。
- 格式：

```json
{
  "$schema": "https://raw.githubusercontent.com/earendil-works/pi/main/packages/coding-agent/src/modes/interactive/theme/theme-schema.json",
  "name": "my-theme",
  "vars": { "primary": "#00aaff", "secondary": 242 },
  "colors": { "accent": "primary", "border": "primary", "mdHeading": "#ffaa00", ... }
}
```

- 必须有 **53 个必填 color token**；可选 `thinkingMax`（回退 `thinkingXhigh`）、`searchMatchBg`（回退 `selectedBg`）、`searchMatchText`（回退 `text`）。
- token 分组：Core UI 13、Backgrounds & Content 11(+2 可选)、Markdown 10、Tool Diffs 3、Syntax Highlighting 9、Thinking Level Borders 6(+1)、Bash Mode 1、HTML Export 可选。
- 值可为 `#rrggbb`、256 色数字（`242`）、或 `vars` 里的 key。
- `--use-theme light/dark` 表示跟随终端背景；`setThemeJsonValidator(validateThemeJson)` + `initTheme()` 在 main 中调用；编辑当前自定义主题文件会**热重载**（`onThemeChange`）。

### 5.4 Context files 与系统提示

- 项目/全局 `SYSTEM.md`、`APPEND_SYSTEM.md` 会被 `discoverSystemPromptFile()` / `discoverAppendSystemPromptFile()` 发现（`.pi/SYSTEM.md` 优先，其次 `<agentDir>/SYSTEM.md`）。
- `AGENTS.md` / `CLAUDE.md` 由 resource-loader 发现（`--no-context-files` 关闭），含 git worktree 上下文文件处理。
- `--system-prompt <text>` 直接替换；`--append-system-prompt <text>` 可多次追加（支持文件内容）。
- `before_agent_start` 事件里 `systemPromptOptions: BuildSystemPromptOptions` 暴露结构化构建选项，并允许扩展替换 systemPrompt。

### 5.5 Pi Packages（npm / git / local）

`pi install <source>` 接受三类：

| 类型 | 语法 | 安装位置 |
|---|---|---|
| npm | `npm:@scope/pkg@1.2.3`、`npm:pkg` | `~/.pi/agent/npm/`（global）/ `.pi/npm/`（project） |
| git | `git:github.com/user/repo@v1`、`git:git@github.com:user/repo@v1`、`https://github.com/user/repo@v1`、`ssh://git@github.com/user/repo@v1` | `~/.pi/agent/git/<host>/<path>` / `.pi/git/<host>/<path>` |
| local | `/abs/path`、`./rel/path` | 原地引用，不拷贝；相对路径相对其出现的 settings 文件解析 |

- 版本化 npm spec 被视为 pinned，`pi update --extensions/--all` 会跳过。
- git refs（tag/commit）是 pinned；`update` 只做对账（reconcile）不移动 ref；对账导致 checkout 变化时会 reset+clean 并在有 `package.json` 时跑 `npm install`。
- 安装后跑 `npm install --omit=dev`（`npmCommand` 配置时 git 包用 plain `install`）。
- 包声明方式：`package.json` 的 `pi` 字段或约定目录：

```json
{
  "name": "my-package",
  "keywords": ["pi-package"],
  "pi": { "extensions": ["./extensions"], "skills": ["./skills"], "prompts": ["./prompts"], "themes": ["./themes"] }
}
```

约定目录（无 manifest 时）：`extensions/`（`.ts`/`.js`）、`skills/`（递归找 `SKILL.md` + 顶层 `.md`）、`prompts/`、`themes/`。manifest 路径支持 glob 与 `!exclusion`。

- **依赖规则**：运行时依赖放 `dependencies`；核心包（`pi-ai` / `pi-agent-core` / `pi-coding-agent` / `pi-tui` / `typebox`）放 `peerDependencies` 且范围 `"*"`、**不要打包**；其它 pi 包必须 `bundledDependencies` 并在 manifest 里用 `node_modules/...` 路径引用。
- **过滤**（settings 对象形式）：`{ source, extensions, skills, prompts, themes }`，`[]` = 全不加载，`!pattern` 排除，`+path` 强制包含，`-path` 强制排除。
- **启用/禁用**：`pi config [-l]` 的 TUI，Tab 切换 global/project。
- **作用域与去重**：global + project 同时出现时 project 胜；`autoload: false` 表示对 global 做 delta 叠加。身份：npm 用包名、git 用不含 ref 的仓库 URL、local 用解析后的绝对路径。
- `-e/--extension <source>` 是「临时试用」：装到临时目录，仅本次运行有效。

`PackageManager` 接口（`core/package-manager.ts:112`）：`resolve(onMissing?)`、`install`、`installAndPersist`、`remove`、`removeAndPersist`、`update`、`listConfiguredPackages`、`resolveExtensionSources`、`addSourceToSettings`、`removeSourceFromSettings`、`setProgressCallback`、`getInstalledPath(source, scope)`。`resolve()` 的 `onMissing` 回调返回 `"install" | "skip" | "error"` 三态——启动时可以询问是否就地安装缺失包。

---

## 6. Python 移植建议

### 6.1 第一版（MVP）必备子集

**必须做（否则不算 faithful port）：**

1. **CLI 全部 flag 与子命令解析**：`argparse` 不能直接表达 pi 的语义（重复 flag、`--print` 吞参、`--` 分隔、`--xxx` 静默进 unknown）。建议**手写 parser**，返回与 `Args` 同构的 dataclass。`unknownFlags`、`diagnostics`、`fileArgs`、`messages` 四件套要一致。
2. **AppMode 选择算法**（20 行）+ `takeOverStdout` 等价物（把 `print` 重定向到 stderr，stdout 专供协议）。Python 里用 `contextlib.redirect_stdout(io.TextIOWrapper(...))` 或一个写入 `sys.stderr` 的代理。
3. **print 模式（text + json 两态）**：逻辑极少、价值极高（CI/脚本/管道）。
4. **JSON 事件流**：`message_update` 的「剥 partial」规则必须严格照抄，这是与 TS 客户端互操作的契约。
5. **RPC 模式完整实现**：命令表、响应形状、事件、**严格 LF-only JSONL framing**（不要用 `for line in sys.stdin`，Python 的 text-mode 迭代也会在 `\x1c/\x1d/\x1e/\x85/\u2028/\u2029` 处断行——用 `sys.stdin.buffer` + `BytesIO` 手动按 `\n` 切分并 UTF-8 decode）。**扩展 UI 子协议**（dialog + fire-and-forget）也要做，否则扩展生态不可用。
6. **扩展系统骨架**：`on` / `registerTool` / `registerCommand` / `registerShortcut` / `registerFlag` / `registerMessageRenderer` / `registerEntryRenderer` / `registerMarkdownTransformer` / `sendMessage` / `sendUserMessage` / `appendEntry` / `exec` / `setModel` / `setThinkingLevel` / `setActiveTools` / `getAllTools` / `getCommands` / `registerProvider` / `unregisterProvider` / `events`。事件集合可先做 `session_*` / `agent_*` / `message_*` / `tool_call` / `tool_result` / `input` / `before_agent_start` / `context` / `before_provider_request`。
7. **Slash 命令内建表 + 合并顺序 + `prompt()` 里的扩展命令/skill/模板展开链**。RPC 与 print 的 `get_commands` 依赖它。
8. **Skills + prompt templates 的发现与展开**（磁盘格式简单、porting 成本最低、用户价值高）。`formatSkillsForPrompt()` 的 XML 格式要逐字对齐。
9. **settings / trust store / session 选择**：`ProjectTrustStore` 的向上递归查找、`defaultProjectTrust`、`--approve/--no-approve`。
10. **`pi config` / `pi list`**：即便 TUI 没做好，也应有纯文本降级版本。

**可以推迟：**

- interactive 的完整 TUI（见下）。
- `--tui-mode fullscreen`、alt-screen search、鼠标选择、OSC 8 超链接。
- 图片粘贴 / 剪贴板 / 外部编辑器 / mermaid / 语法高亮（需 Pygments 或 tree-sitter）。
- git package 的自我更新（`pi update --self`）、managed install staging。
- HTML 导出（`export-html/template.html`）。
- `/share`（GitHub gist）。

### 6.2 Python 的 TUI 选型：textual vs prompt_toolkit

pi 的 TUI 需求可以拆成四层：

| 能力 | pi 的做法 | textual | prompt_toolkit |
|---|---|---|---|
| 内联（scrollback 保留）渲染 | `TuiMainScreen` | ✗ 以全屏 app 为主，inline 需自绘 | ✓ `full_screen=False` 天生内联，`patch_stdout` + `Application` |
| 全屏 + 可滚动 transcript | `TuiAltScreen` + `ScrollView` + search | ✓✓ 一等公民（`ScrollableContainer`、`VerticalScroll`、CSS、鼠标、`Ctrl+F` 搜索可自建） | △ 需手写布局/滚动，`Layout`/`Window` 能做但搜索、滚动条、鼠标要自己写 |
| 组件化 + 增量更新 | `Component` / `Container` / `VStack` / `invalidate()` | ✓✓ `Widget` / `compose()` / `reactive` / `refresh` | △ `FormattedText` + 手工重绘 |
| 键位绑定 + 可配置 keybindings.json | `KeybindingsManager`（命名空间 id） | ✓ `BINDINGS` + `Key`，但命名空间/多键/平台差异要自建 | ✓✓ `KeyBindings` / `key_bindings` 丰富、vi/emacs 模式、`@key_binding` |
| 多行编辑器（undo/kill-ring/历史/补全） | `CustomEditor` | △ `TextArea` 有补全，kill-ring 需自建 | ✓✓ `PromptSession` 的 `Buffer` 自带 undo/kill-ring/history/completion/auto-suggest |
| 弹出选择器 / dialog | 自建 selector components | ✓✓ `ModalScreen` / `OptionList` / `SelectionList` | △ `radiolist_dialog` 有，但样式与嵌入较受限 |
| 单次快照渲染（非交互选择器、`--resume` 选择器） | `startup-ui.ts` 临时 TUI | 重（要跑 App） | 轻（`Application.run()` 一次即可，或纯 `print` + `prompt`） |

**建议：混合使用，而不是二选一。**

- **主交互模式用 `textual`**：它是唯一能低成本复刻「固定的 editor/status/footer dock + 上方可滚动 transcript + overlay selector + 鼠标 + 搜索」的库。pi 的 `createChatViewport()`（`VStack(ScrollView, dock)`）几乎可以逐项翻译成 textual 的 `Container` + `VerticalScroll` + dock。
- **编辑器与会话选择器用 `prompt_toolkit` 风格实现，或直接内嵌 `PromptSession`**：pi 的 editor 功能密度很高（kill ring、undo、多行、历史、补全、外部编辑器），重写成本大。textual 的 `TextArea` 可用但需要补 kill-ring/undo 语义。
- **RPC / print 模式不依赖任何 TUI 库**：这条边界必须守住——`print-mode.ts` 与 `rpc-mode.ts` 都不 import TUI。Python 版应把 TUI 依赖做成 `pi[tui]` extra，`pi --mode rpc` 在无 textual 环境下可运行。
- **一次性选择器（`--resume`、trust 询问、`/model`）建议做成可降级**：交互 TTY → textual/prompt_toolkit；非 TTY 或 `--offline` → 纯文本 `input()`/默认值。pi 自己就是这么做的（`ProjectTrustContext.hasUI`）。

一个具体风险：textual 是异步（asyncio）驱动的，而 pi 的扩展事件模型是「handler 可 await、可弹阻塞 dialog」。Python 版必须选一个并发模型。建议 **asyncio 单一事件循环**，扩展 handler 声明为 `async def`，同步 handler 用 `asyncio.to_thread()` 兼容；RPC 的 stdin 读取用 `loop.add_reader()` 或 `asyncio` 的 stream reader，天然满足「解析一行 → 可能 await 扩展 dialog → 继续读」的顺序语义。

### 6.3 Python 的扩展形态设计

**目标**：让「写扩展」的体验接近 TS，同时让常见场景零样板。

推荐三层：

**层 1 — 装饰器/协议（推荐主路径）**

```python
# ~/.pi/agent/extensions/hello.py
from pi import ExtensionAPI, ToolDefinition, define_tool

def extension(pi: ExtensionAPI) -> None:      # 工厂：模块级函数即可
    pi.on("session_start", on_start)
    pi.register_tool(HelloTool())

def on_start(event, ctx):
    ctx.ui.notify(f"session_start: {event.reason}", "info")
```

不引入装饰器也能工作（等价于 TS 的 `export default function(pi)`），保持「一个模块 = 一个工厂」的直觉。可选再加 `@pi.extension` / `@pi.tool` / `@pi.command` 装饰器作为糖，但**不要**让装饰器成为唯一路径——否则动态加载时模块级副作用顺序难以控制。

**层 2 — 入口点发现（`importlib.metadata` entry points）**

对已安装的分发包（pip 生态等价于 `pi install npm:...`）：

```toml
[project.entry-points."pi.extensions"]
my_ext = "my_package.ext:extension"
[project.entry-points."pi.skills"]
my_skills = "my_package:skills_dir"
```

配合目录约定（`.pi/extensions/*.py`、`~/.pi/agent/extensions/*.py` 或包目录带 `pi.toml` / `pyproject.toml` 的 `[tool.pi]`），形成与 pi 的 `pi.extensions` manifest 一一对应的发现层。

**层 3 — 进程外扩展（RPC over stdio）**

给非 Python 作者（或需要隔离）用：扩展本身是一个子进程，遵守子集协议。**但要注意**：RPC 模式已经是「全文换」协议，扩展不能直接走它（扩展需要 `tool_call` 阻断、`context` 改写这些**同步语义**）。若要支持，需要另设一条内部 JSONL 通道，并把「可阻断/可改写」事件用「请求-应答」表达。**建议第一版不做**，只在文档里留接口。

其他设计要点：

- **类型化事件**：用 `TypedDict` 或 `dataclass(frozen=True)` 定义事件；事件注册用字符串字面量联合（`Literal[...]`），mypy 可查。
- **`ctx` 惰性 + 失效检测**：pi 的 `assertActive()` 模式非常值得抄——每个 session 替换/reload 都会让旧 ctx 抛错。Python 版用一个 `generation` 计数 + `ContextProxy` 即可。
- **错误处理**：除 `project_trust` 外，所有 handler 包 try/except，报错经 `onError` 上报后继续。`tool_call` handler 异常 = block（fail-safe）。必须显式实现，否则一个坏扩展会整死 agent。
- **加载器**：Python 无 jiti，用 `importlib.util.spec_from_file_location` + `module_from_spec` + `exec_module`。**热重载**需要 `importlib.reload` 或「每次重新构造模块」（pi 用 `moduleCache: false` + 自建 factory cache），建议直接自建 cache：`{path: (mtime, factory)}`。
- **虚拟模块**：TS 侧靠 `virtualModules`/`alias` 让扩展 import 宿主包。Python 侧天然解决（扩展 `import pi`），但要提供 `pi` 包的稳定公共 API 面，并考虑把 SDK 内部符号以 `_` 前缀隐藏。

**「跨语言兼容 TS 扩展」的成本评估**

结论：**不建议第一期做，且大概率永远不划算**。

- 要跑 TS 扩展必须内嵌一个 JS runtime（对 Python 而言是 `QuickJS`/`PyMiniRacer`/`PyV8` 或子进程 Node）。pi 的扩展 API 面积极大：`ExtensionAPI` 有 33 个 `on` 重载 + 20+ 方法，事件 payload 引用 `AgentMessage`/`ToolDefinition`/TUI `Component`（**React 式组件树**）。要跨语言就必须把 Component 树序列化/代理过去——这在 TUI 场景下等于重写渲染协议。
- `registerTool.execute` 是**同进程同步语义**（拿 `AbortSignal`、`onUpdate` 回调、返回 `AgentToolResult`）。跨 runtime 必须把这些变成 RPC，`AbortSignal`、`onUpdate` 流式回调、`ctx.ui.custom()` 的 React 组件都无法无损映射。
- `before_provider_headers` 的「原地 mutate headers」语义、`tool_call` 的「原地 mutate input」语义，跨语言只能做浅拷贝，行为会漂移。
- 现实可接受的折中：**兼容「纯数据」子集**——扩展导出 `pi.json`（声明 commands/flags/tools 的 schema 与一个可执行入口），或支持 `tools/*.json` + shell 脚本的声明式扩展。这覆盖了社区里一大半「只是一个 LLM 工具」的扩展，成本远低于内嵌 JS。
- 若确实必须跑 TS 扩展，隔离方案比兼容方案现实：把 TS 扩展跑在**独立 Node 子进程**里，通过自定义 JSONL 协议与 Python 宿主通信，并明确文档说明「仅支持观察型事件 + 工具注册 + 命令注册，不支持 UI 组件与同步改写事件」。

### 6.4 需要特别小心的移植陷阱

1. **JSONL 不能用 text-mode 行迭代**（Python 的 `TextIOWrapper` 在 `\x1c\x1d\x1e\x85\u2028\u2029` 断行，与 TS `readline` 同病）。用 `sys.stdin.buffer` 手动 split `b"\n"`，注意 UTF-8 多字节边界（对应 TS 的 `StringDecoder`）。
2. **`takeOverStdout`**：非交互模式下 `print()` 必须去 stderr，stdout 只承载协议。所有库的 logging 也要重定向（`logging.StreamHandler(sys.stderr)`），否则会污染 JSON 流。
3. **`--print` 吞下一个参数**的启发式、`--xxx` 静默进 unknownFlags、`@file` 语义，必须在解析层精确复刻，否则扩展 flag 机制失效。
4. **事件顺序**：`agent_settled` 是 RPC 里 shutdown 的触发器；`queue_update` 每次队列变化全量重发；`compaction_start/end` 覆盖手动与自动两种触发。
5. **`message_update` 的 `usage` 语义**：是「最新累积 usage」，可能长期为 0，客户端不得据此计算成本；成本要以 `message_end` 的终态 message 为准。
6. **`toolcall_start` 的 `id`/`toolName` 补齐**：JSON 流里这是**唯一**能拿到 toolCallId 与工具名的 delta 位置（因为 `partial` 被剥掉了），客户端必须依赖它。
7. **skill 展开后的 system/user 消息结构**：`<skill name=... location=...>` 块 + 相对路径提示，`parseSkillBlock` 反向解析依赖精确格式（含 `\n\n` 分隔），改一个空格就解析不出来。
8. **扩展命令在 streaming 期间立即执行**，而普通消息必须带 `streamingBehavior`（否则 `prompt()` 抛错）。这个差异在 TUI 与 RPC 两条路径上都要一致。
9. **`project_trust` 事件在资源加载之前**，此时很多服务尚未就绪——Python 版的 context 构造必须容忍这一点（pi 的 `ProjectTrustContext` 只暴露 `select/confirm/input/notify` 四个方法，就是这个原因）。
10. **扩展注册期的 `registerProvider` 必须入队**，等运行时绑定后再 flush；否则模型注册表还不存在。

---

## 7. 附：关键文件索引

```
packages/coding-agent/src/
├── main.ts                              # 启动序列（§1.4）
├── cli.ts                               # 6 行，仅 import main 并执行
├── package-manager-cli.ts               # install/remove/update/list/config
├── cli/                                 # args / auth-command / auth-check / credential-print /
│                                        # file-processor / initial-message / list-models /
│                                        # project-trust / session-picker / startup-ui
├── modes/
│   ├── print-mode.ts                    # -p / --mode json 执行体
│   ├── json-event.ts                    # wire 事件裁剪规则
│   ├── interactive/                     # interactive-mode.ts(6620) / tui-renderer / chat-viewport
│   │                                    # components/(~50) / theme/
│   └── rpc/                             # rpc-mode(821) / rpc-types(297) / rpc-client(609) / jsonl(58)
├── core/
│   ├── agent-session.ts                 # prompt() 展开链（§3.5）
│   ├── agent-session-runtime.ts         # session 替换
│   ├── extensions/{types,loader,runner,index,wrapper}.ts   # 扩展系统（§4）
│   ├── slash-commands.ts / skills.ts / prompt-templates.ts
│   ├── resource-loader.ts(1097) / package-manager.ts(2699)
│   ├── trust-manager.ts / project-trust.ts / keybindings.ts
│   └── settings-manager.ts(1417) / session-manager.ts(1746)
└── docs/                                # 与实现同步的规格文档
    extensions.md(3033) rpc.md(1618) sdk.md(1226) tui.md(961) models.md(574)
    session-format.md(450) compaction.md(444) settings.md(405) themes.md(322)
    providers.md(317) usage.md(311) keybindings.md(239) skills.md(232) packages.md(228)
    terminal-setup.md(192) quickstart.md(167) sessions.md(145) json.md(98)
    prompt-templates.md(96) ...
```

---

*本文基于源码静态阅读，未运行构建或测试。所有行号引用以 `/tmp/pi-ref` 当时快照为准（`@earendil-works/pi-coding-agent@0.85.1`）。*

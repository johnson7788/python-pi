# `@earendil-works/pi-ai` 深度研究笔记（Python 移植版）

> 研究对象：`/tmp/pi-ref/packages/ai`（版本 `0.85.1`，`packages/ai/package.json`）。
> 目标读者：负责把该层忠实移植为 Python 的实现者。
> 约定：所有类型名、字段名、事件名、环境变量、常量保持英文原文；叙述用中文。

---

## 0. 包的整体形状（先建立地图）

### 0.1 定位

`package.json` 描述：`"Unified LLM API with automatic model discovery and provider configuration"`。
它做四件事，且四件事互相独立：

1. **统一消息/内容/用量模型**（`src/types.ts`）——所有 provider 共享的 `Message` / `Content` / `Usage` / `Model`。
2. **API 适配器层**（`src/api/*.ts`）——一个 `api` 字符串对应一个模块，模块导出 `stream` / `streamSimple`。
3. **Provider 运行时**（`src/models.ts` + `src/providers/*.ts`）——`Models` 集合负责 auth 解析并分派到 `Provider`。
4. **凭证与 OAuth**（`src/auth/*`、`src/env-api-keys.ts`）。

### 0.2 入口（`package.json` 的 `exports`）

```jsonc
".":              dist/index.js          // 纯核心，无副作用（无 catalog / provider factory / api-registry）
"./compat":       dist/compat.js         // 旧的全局 API（api-dispatch + env key 注入 + catalog 读）
"./providers/*":  dist/providers/*.js    // 内置 provider 工厂，如 providers/all.js、providers/anthropic.js
"./api/*":        dist/api/*.js          // 单个 API 实现模块（可直接调用，见 §3）
"./utils/*":      dist/utils/*.js
"./oauth":        dist/oauth.js          // 仅类型，给 extension 声明用
"./bedrock-provider" / "./bun-oauth"
```

`src/index.ts` 的注释明确说明设计意图：

```ts
// Core only, side-effect free: no generated catalogs, no provider factories,
// no api-registry, no OAuth implementations, no compat.
```

这一点对 Python 移植很关键：**核心类型层不依赖任何具体 provider**，`types.ts` 对适配器的参数类型只做 `import type`（编译期擦除）。

### 0.3 运行时依赖（决定移植工作量）

```
@anthropic-ai/sdk 0.124.0      openai 6.40.0
@google/genai 2.21.0           @aws-sdk/client-bedrock-runtime 3.1127.0
@smithy/node-http-handler      http-proxy-agent / https-proxy-agent
partial-json 0.1.7             typebox 1.3.27
@earendil-works/pi-telemetry
```

Python 侧对应物：`anthropic`、`openai`、`google-genai`、`boto3`/`aioboto3`、`partial-json-parser`（或自写）、`pydantic`/`jsonschema`（替代 typebox）、自建 telemetry。

### 0.4 目录速查

| 路径 | 内容 |
|---|---|
| `src/types.ts` | 全部公共类型（881 行，唯一真源） |
| `src/models.ts` | `Models` / `Provider` / `createProvider` / `calculateCost` / thinking level 工具 |
| `src/api/<api>.ts` | 10 个 API 实现；`<api>.lazy.ts` 是 `lazyApi()` 包装 |
| `src/api/lazy.ts` | `lazyApi()` / `lazyStream()` |
| `src/api/transform-messages.ts` | provider 无关的消息规范化（所有适配器共用） |
| `src/api/constrained-sampling.ts` | strict JSON schema 与 grammar 约束采样 |
| `src/api/simple-options.ts` | `streamSimple` 共用：`buildBaseOptions` / 思考预算换算 |
| `src/providers/*.ts` | 每个 provider 的工厂（auth + models + api 绑定） |
| `src/providers/*.models.ts` | 生成物：`flattenModelCatalog(provider, values)` |
| `src/models.generated.ts` | 生成物：`MODELS` 全量目录表 |
| `src/auth/*` | credential store / resolve / oauth |
| `src/utils/*` | event-stream / validation / abort / retry / json-parse / overflow / estimate / diagnostics |

---

## 1. Message 与 Content 模型

全部定义在 `src/types.ts`。**没有 `system` role**——system prompt 是 `Context.systemPrompt`，由各适配器自行注入。

### 1.1 `Message` 联合

```ts
export type Message = UserMessage | AssistantMessage | ToolResultMessage;
```

### 1.2 `UserMessage`

```ts
export interface UserMessage {
  role: "user";
  content: string | (TextContent | ImageContent)[];
  timestamp: number; // Unix timestamp in milliseconds
}
```

- `content` 允许裸字符串（快速路径）或 content block 数组。
- 只允许 `TextContent` 和 `ImageContent`；不允许 thinking 和 toolCall。
- `timestamp` 是**毫秒** Unix 时间戳，全库统一（`Date.now()` 语义）。

### 1.3 `AssistantMessage`

```ts
export interface AssistantMessage {
  role: "assistant";
  content: (TextContent | ThinkingContent | ToolCall)[];
  api: Api;
  provider: ProviderId;
  model: string;
  responseModel?: string;   // 实际返回的 chunk.model（如 OpenRouter auto → anthropic/...）
  responseId?: string;      // provider 的响应/message id
  providerThinkingLevel?: string; // 本次响应实际使用的 provider 原生 effort 等级
  diagnostics?: AssistantMessageDiagnostic[];
  usage: Usage;
  stopReason: StopReason;
  deferred?: DeferredHandle;
  errorMessage?: string;
  rawStopReason?: string;   // provider 原始 stop reason 字符串
  endTurn?: boolean;        // provider 是否显式结束回合；仅调试用，不影响控制流
  timestamp: number;
}
```

语义要点：

- `content` 顺序即 UI 顺序，也是回放顺序；`contentIndex` 事件索引指向这个数组。
- `api` / `provider` / `model` 记录**生成它的身份**，用于 `transformMessages` 判断 `isSameModel`（见 §3.4）。
- `stopReason: "pending"` 是流式生成中的初始值；终结时必须是 `stop | length | toolUse | error | aborted | deferred` 之一。
- `errorMessage` 只在 `stopReason === "error" | "aborted"` 时有意义；适配器要求它**必须能解释失败**（README 明确 "Surface `error.message` only"）。
- `responseModel` 与 `model` 不同时说明发生了路由/降级（如 OpenRouter 的 auto 路由）。

### 1.4 `ToolResultMessage`

```ts
export interface ToolResultMessage<TDetails = any> {
  role: "toolResult";
  toolCallId: string;
  toolName: string;
  content: (TextContent | ImageContent)[]; // 支持文本和图片
  details?: TDetails;
  usage?: Usage;              // 工具自身执行的用量，不计入 LLM context 账本
  addedToolNames?: string[];  // 本结果之后变为可用的工具名（延迟工具加载的载入点）
  isError: boolean;
  timestamp: number;
}
```

- `content` 是**数组且必填**（不是 `string | array`），与 `UserMessage` 不同。
- `addedToolNames` 被 Anthropic 适配器转成 `tool_reference` block（§3.6），被 `splitDeferredTools` 用来决定哪些工具延迟加载（§8.4）。
- `usage` 存在但**不参与主 context 计费**。

### 1.5 Content block 四种

```ts
export interface TextContent {
  type: "text";
  text: string;
  textSignature?: string; // OpenAI responses 的 message metadata（legacy id string 或 TextSignatureV1 JSON）
}

export interface ThinkingContent {
  type: "thinking";
  thinking: string;
  thinkingSignature?: string; // provider 不透明的 reasoning 回放数据
  redacted?: boolean;         // 被安全过滤遮蔽；密文在 thinkingSignature，需回传以保持多轮连续性
}

export interface ImageContent {
  type: "image";
  data: string;     // base64 编码
  mimeType: string; // "image/jpeg" | "image/png" | ...
}

export interface ToolCall {
  type: "toolCall";
  id: string;
  name: string;
  arguments: Record<string, any>;
  thoughtSignature?: string; // Google 专用：复用 thought context 的不透明签名
  namespace?: string;        // OpenAI Responses 的命名空间/动态加载工具
}

export interface TextSignatureV1 {
  v: 1;
  id: string;
  phase?: "commentary" | "final_answer";
}
```

移植注意：

- `arguments` 是 `Record<string, any>`，**没有静态 schema**；验证走 `validateToolArguments`（§8）。
- `textSignature` 与 `thinkingSignature` 是**必须原样回传**的 provider 回放数据。Anthropic 用 `thinkingSignature` 承载 `signature`；OpenAI completions 用它承载序列化后的 `reasoning_details` JSON 数组；Google 用 `thoughtSignature`（在 ToolCall 上）。
- `redacted: true` 的 thinking block：`thinking` 常为占位文本（Anthropic 适配器写 `"[Reasoning redacted]"`），密文放 `thinkingSignature`。

### 1.6 `Context`

```ts
export interface Context {
  systemPrompt?: string;
  messages: Message[];
  tools?: Tool[];
}
```

`Context` 就是一次请求的全部输入。**没有** `maxTokens`、`temperature` 等——那些在 `StreamOptions`。

### 1.7 `StopReason`

```ts
export type StopReason = "pending" | "stop" | "length" | "toolUse" | "error" | "aborted" | "deferred";
```

- `done` 事件只带 `stop | length | toolUse | deferred`；`error` 事件只带 `aborted | error`（见 §4）。
- `deferred` 用于 provider 异步长轮询（`DeferredHandle`）。

### 1.8 `DeferredHandle` 与 deferred 响应

```ts
export interface DeferredHandle {
  provider: string;
  modelId: string;
  api: string;
  id: string;          // provider token，如 response id 或 batch id + row id
  expiresAt?: number;
  pollAfterMs?: number;
  data?: JsonValue;    // 重建最终 assistant message 所需的 provider 转换数据
}
export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
```

`SimpleStreamOptions.deferred?: boolean | { window?: "15m" | "1h" | "24h" }`。支持的两个方法是
`ProviderStreams.fetchDeferred?()` / `cancelDeferred?()`。

### 1.9 图片生成侧的类型（同文件，独立子系统）

```ts
export type ImagesInputContent  = TextContent | ImageContent;
export type ImagesOutputContent = TextContent | ImageContent;
export interface ImagesContext { input: ImagesInputContent[]; }
export type ImagesStopReason = "stop" | "error" | "aborted";
export interface AssistantImages {
  api: ImagesApi; provider: ImagesProviderId; model: string;
  output: ImagesOutputContent[]; responseId?: string; usage?: Usage;
  stopReason: ImagesStopReason; errorMessage?: string; timestamp: number;
}
export interface ImagesModel<TApi extends ImagesApi>
  extends Omit<Model<Api>, "api" | "provider" | "reasoning" | "contextWindow" | "maxTokens" | "compat"> {
  api: TApi; provider: ImagesProviderId; output: ("text" | "image")[];
}
export interface ProviderImages {
  generateImages(model: ImagesModel<ImagesApi>, context: ImagesContext, options?: ImagesOptions): Promise<AssistantImages>;
}
```

只有 `KnownImagesApi = "openrouter-images"`。图像路径是**非流式、Promise 返回**，与对话路径的流式接口是两套契约。

---

## 2. `Model` 描述符

### 2.1 完整定义

```ts
export interface Model<TApi extends Api> {
  id: string;             // provider 内的模型 id，请求里发这个
  name: string;           // 展示名
  api: TApi;              // 决定用哪个适配器（§3）
  provider: ProviderId;   // 决定用哪个 Provider（auth/baseUrl 归属）
  baseUrl: string;
  reasoning: boolean;     // 是否支持 thinking/reasoning
  thinkingLevelMap?: ThinkingLevelMap;
  input: ("text" | "image")[];  // 输入模态；决定是否降级图片（§3.4）
  cost: ModelCost;
  contextWindow: number;  // 输入侧上下文窗口（token）
  maxTokens: number;      // 默认输出上限
  samplingParams?: Record<string, unknown>; // 每模型默认采样参数
  headers?: Record<string, string>;
  compat?: ...;           // 按 api 收窄的兼容开关（见 §2.4）
}
```

### 2.2 定价

```ts
export interface ModelCostRates {
  input: number;      // $/million tokens
  output: number;     // $/million tokens
  cacheRead: number;  // $/million tokens
  cacheWrite: number; // $/million tokens
}
export interface ModelCostTier extends ModelCostRates {
  inputTokensAbove: number; // 当请求总输入用量超过该值时启用本档
}
export interface ModelCost extends ModelCostRates {
  tiers?: ModelCostTier[]; // 请求级分档；命中最高阈值的一档作用于整个请求
}
```

### 2.3 thinking level 语义

```ts
export type ThinkingLevel      = "minimal" | "low" | "medium" | "high" | "xhigh" | "max";
export type ModelThinkingLevel = "off" | ThinkingLevel;
export type ThinkingLevelMap   = Partial<Record<ModelThinkingLevel, string | null>>;
```

- `thinkingLevelMap[level]` 把 pi 的抽象等级映射到 provider/模型原生的 value（如 Anthropic 的 `effort`、OpenAI 的 `reasoning_effort`）。
- **`null` 表示该等级不受支持**；缺省（`undefined`）表示用 provider 默认。
- `streamSimple` 收到 `reasoning` 后一律经过 `clampThinkingLevel`。

`EXTENDED_THINKING_LEVELS`（顺序即强度序）：

```ts
["off", "minimal", "low", "medium", "high", "xhigh", "max"]
```

```ts
export function getSupportedThinkingLevels(model) {
  if (!model.reasoning) return ["off"];
  return EXTENDED_THINKING_LEVELS.filter((level) => {
    const mapped = model.thinkingLevelMap?.[level];
    if (mapped === null) return false;
    if (level === "xhigh" || level === "max") return mapped !== undefined; // 必须显式映射
    return true;
  });
}
```

`clampThinkingLevel(model, level)`：命中则返回；否则从请求等级向**更强**方向找第一个可用等级，再向更弱方向找，最后回退 `availableLevels[0] ?? "off"`。这个"就高不就低"的方向必须在 Python 里逐字复刻。

`SimpleStreamOptions.thinkingBudgets?: ThinkingBudgets`（`minimal/low/medium/high`），默认值（`api/simple-options.ts`）：

```ts
export const DEFAULT_THINKING_BUDGETS = { minimal: 1024, low: 2048, medium: 8192, high: 16384 };
export const MIN_ANSWER_TOKENS = 1024;
```

### 2.4 `compat`：按 `api` 收窄的兼容开关

`Model["compat"]` 是条件类型，按 `api` 取不同接口：

| `api` | compat 类型 |
|---|---|
| `openai-completions` | `OpenAICompletionsCompat` |
| `openai-responses` / `azure-openai-responses` / `openai-codex-responses` | `OpenAIResponsesCompat` |
| `anthropic-messages` | `AnthropicMessagesCompat` |
| `bedrock-converse-stream` | `BedrockCompat` |
| 其他 | `never` |

这些接口字段极多（`types.ts` 第 568–738 行）。Python 侧建议统一模型化为一个 `dict`/`Optional` 结构体，但**字段名与默认值必须一致**，因为它们是行为开关。重点字段会在 §3 逐 provider 展开。

### 2.5 生成目录（generated catalog）

- `src/models.generated.ts` 是生成物：`export const MODELS: { readonly "<provider>": typeof <P>_MODELS; ... }`，覆盖全部 39 个内置 provider。
- 每个 `src/providers/<name>.models.ts` 形如：

```ts
import values from "./data/<name>.json" with { type: "json" };
import { flattenModelCatalog, type ModelCatalog } from "../model-catalog.ts";

export const ANTHROPIC_MODELS: ModelCatalog<typeof values, "anthropic"> =
  flattenModelCatalog("anthropic", values);
```

- `model-catalog.ts` 的 `flattenModelCatalog` 就是 `Object.assign({}, ...Object.values(groups))`——即 JSON 是**按 api 分组**的 `Record<api, Record<modelId, ModelShape>>`，展平后得到按 modelId 索引的对象；`ModelCatalog` 用映射类型推断每个 model 的实际 `api`。
- 生成脚本：`scripts/generate-models.ts`（`npm run generate-models --strict`）、`scripts/model-data.ts`（schema 校验 + `.manifest.json` 的 `structureHash`/`files` 哈希）、`scripts/check-model-data.ts`。`npm run generate-model-catalog` 会额外导出 JSON catalog 到 `.artifacts/model-catalog`。
- **实际模型数据文件（`src/providers/data/*.json`）在本快照中缺失**（构建时从上游拉取并 hydrate）。Python 移植应把 catalog 当作**可替换的数据包 + 生成器**，而不是硬编码常量。
- `scripts/models-dev-reasoning-options.ts` / `openrouter-reasoning-options.ts` 说明 `thinkingLevelMap` 是由 models.dev 与 OpenRouter 的 reasoning 元数据派生的。

### 2.6 动态 provider 与 `ModelsStore`

- `Provider.getModels(): readonly Model<TApi>[]` 是**同步**读取"已知模型"；抛异常视为无模型。
- 有的 provider 实现 `refreshModels?(context: RefreshModelsContext)`（网络拉取），可 `publish({ persist, update })` 落盘。
- `ModelsStore`（`src/models-store.ts`）是持久化目录抽象：

```ts
export interface ModelsStore {
  read(providerId, options?): Promise<ModelsStoreEntry | undefined>;
  write(providerId, entry, options?): Promise<void>;
  delete(providerId, options?): Promise<void>;
}
export interface ModelsStoreEntry {
  models: readonly Model<Api>[];
  lastModified?: number; // 远端 Catalog 的 Last-Modified
  checkedAt?: number;
  etag?: string;         // 原样存、原样回送 If-None-Match（含引号）
}
```

`createProvider` 把 dynamic overlay 合并进 baseline：同 id 替换、新 id 追加（`currentModels()`）。

### 2.7 `Models` 集合与 `Provider` 接口

```ts
export interface Provider<TApi extends Api = Api> {
  readonly id: string;
  readonly name: string;
  readonly baseUrl?: string;
  readonly headers?: ProviderHeaders;   // Record<string, string | null>，null = 抑制默认头
  readonly auth: ProviderAuth;          // 必填：至少 apiKey/oauth 之一
  getModels(): readonly Model<TApi>[];
  refreshModels?(context: RefreshModelsContext): Promise<void>;
  filterModels?(models, credential): readonly Model<TApi>[];
  stream<T extends TApi>(model: Model<T>, context: Context, options?: ApiStreamOptions<T>): AssistantMessageEventStream;
  streamSimple(model: Model<TApi>, context: Context, options?: SimpleStreamOptions): AssistantMessageEventStream;
  fetchDeferred?(...): AssistantMessageEventStream;
  cancelDeferred?(...): Promise<void>;
}
```

`Models` 提供 `getProviders/getProvider/getModels/getModel/refresh/checkAuth/getAvailable/getAuth/login/logout`
以及 `stream/complete/streamSimple/completeSimple/streamDeferred/fetchDeferred/cancelDeferred`。

关键不变量（`ModelsImpl`）：

- `stream*` 一律返回 `lazyStream(model, async () => { ...applyAuth...; return provider.stream(...) })`，即**同步返回流、异步做 auth**。
- `complete*` 就是 `stream*(...).result()`。
- `applyAuth` 合并顺序：`apiKey = options.apiKey ?? auth.apiKey`；`headers = mergeHeaders(auth.headers, options.headers)` 然后 `transformHeaders(headers)`；`env = {...resolution.env, ...options.env}`；若 `auth.baseUrl` 存在则 `requestModel = {...model, baseUrl: auth.baseUrl}`。
- `mergeHeaders` 大小写不敏感地去重覆盖。

---

## 3. Provider / API 适配器架构

### 3.1 `api` 字符串 → 适配器

```ts
export type KnownApi =
  | "openai-completions" | "mistral-conversations"
  | "openai-responses" | "azure-openai-responses" | "openai-codex-responses"
  | "anthropic-messages" | "bedrock-converse-stream"
  | "google-generative-ai" | "google-vertex"
  | "pi-messages";
export type Api = KnownApi | (string & {});   // 允许自定义字符串
```

映射**不是全局注册表**（旧的 `api-registry` 只剩在 `compat.ts` 里）。新架构下映射发生在 provider 工厂的 `api` 参数：

```ts
// src/providers/anthropic.ts
return createProvider({
  id: "anthropic", name: "Anthropic", baseUrl: "https://api.anthropic.com",
  auth: { apiKey: anthropicApiKeyAuth(), oauth: lazyOAuth({...}) },
  models: Object.values(ANTHROPIC_MODELS),
  api: anthropicMessagesApi(),          // 单个 ProviderStreams → 所有模型共用
});
```

`createProvider` 的分派规则（`src/models.ts`）：

```ts
const single = typeof (input.api as ProviderStreams).stream === "function" ? input.api : undefined;
const byApi  = single ? undefined : (input.api as Partial<Record<string, ProviderStreams>>);
const apiFor = (model) => single ?? byApi?.[model.api];
```

- `api` 是单个对象 → 该 provider 全部模型用它。
- `api` 是 map（如 openai provider 混用 `openai-responses` 与 `openai-completions`）→ 按 `model.api` 取。
- **map 中没有对应条目** → 返回一个 `lazyStream`，内部抛 `ModelsError("stream", 'Provider X has no API implementation for "Y"')`。
- `fetchDeferred` / `cancelDeferred` 只在至少一个 `ProviderStreams` 提供时挂到 provider 上；分派时若该 model 的 api 不支持则抛 `ModelsError("provider", ...)`。

### 3.2 `ProviderStreams`：适配器模块的统一契约

```ts
export interface ProviderStreams {
  stream(model: Model<Api>, context: Context, options?: StreamOptions): AssistantMessageEventStream;
  streamSimple(model: Model<Api>, context: Context, options?: SimpleStreamOptions): AssistantMessageEventStream;
  fetchDeferred?(model, handle: DeferredHandle, options?: DeferredFetchOptions): AssistantMessageEventStream;
  cancelDeferred?(model, handle: DeferredHandle, options?: DeferredCancelOptions): Promise<void>;
}
```

带类型的流函数签名：

```ts
export type StreamFunction<TApi extends Api = Api, TOptions extends StreamOptions = StreamOptions> = (
  model: Model<TApi>,
  context: Context,
  options?: TOptions,
) => AssistantMessageEventStream;
```

**契约（`types.ts` 注释原文）**：

```
- Must return an AssistantMessageEventStream.
- Direct streamSimple() calls may throw synchronously when request auth is
  missing. Once a stream is returned, request/model/runtime failures should
  be encoded in that stream.
- Error termination must produce an AssistantMessage with stopReason
  "error" or "aborted" and errorMessage, emitted via the stream protocol.
```

各模块导出的名字固定为 `stream` 与 `streamSimple`（外加可选的 `fetchDeferred`/`cancelDeferred`），所以"模块本身就是 `ProviderStreams`"。

### 3.3 Options 体系（四层继承）

```ts
export interface ProviderRequestOptions<TModel = Model<Api>> {
  signal?: AbortSignal;
  telemetryContext?: TelemetryContext;
  apiKey?: string;
  fetch?: FetchFunction;            // 默认 globalThis.fetch
  env?: ProviderEnv;                // Record<string,string>，优先于 process.env
  onPayload?: (payload, model) => unknown | undefined | Promise<...>;  // 可替换请求体
  onResponse?: (response: ProviderResponse, model) => void | Promise<void>;
  headers?: ProviderHeaders;        // string | null；null 抑制同名默认头
  timeoutMs?: number;
  maxRetries?: number;
  maxRetryDelayMs?: number;         // 默认 60000；0 = 关闭上限
}

export interface StreamOptions extends ProviderRequestOptions<Model<Api>> {
  temperature?: number;
  samplingParams?: Record<string, unknown>; // OpenAI-compatible 才用；最后 merge，可覆盖上面所有具名字段
  maxTokens?: number;
  transport?: Transport;            // "sse" | "websocket" | "websocket-cached" | "auto"
  cacheRetention?: CacheRetention;  // "none" | "short" | "long"，默认 "short"
  sessionId?: string;               // 会话亲和/prompt cache key
  websocketConnectTimeoutMs?: number;
  metadata?: Record<string, unknown>;
}

export interface SimpleStreamOptions extends StreamOptions {
  toolChoice?: ToolChoice;                       // "auto" | "none"
  reasoning?: ThinkingLevel;
  deferred?: boolean | { window?: "15m" | "1h" | "24h" };
  thinkingBudgets?: ThinkingBudgets;
}
```

按 API 收窄：

```ts
export interface ApiOptionsMap {
  "anthropic-messages": AnthropicOptions;
  "openai-completions": OpenAICompletionsOptions;
  "openai-responses": OpenAIResponsesOptions;
  "openai-codex-responses": OpenAICodexResponsesOptions;
  "azure-openai-responses": AzureOpenAIResponsesOptions;
  "google-generative-ai": GoogleOptions;
  "google-vertex": GoogleVertexOptions;
  "mistral-conversations": MistralOptions;
  "bedrock-converse-stream": BedrockOptions;
  "pi-messages": PiMessagesOptions;
}
export type ApiStreamOptions<TApi extends Api> =
  TApi extends keyof ApiOptionsMap ? ApiOptionsMap[TApi] : StreamOptions & Record<string, unknown>;
```

各 API 的专有 options：

| API | 专有字段 |
|---|---|
| `anthropic-messages` | `thinkingEnabled?`, `thinkingBudgetTokens?`, `effort?: "low"\|"medium"\|"high"\|"xhigh"\|"max"`, `thinkingDisplay?: "summarized"\|"omitted"`, `interleavedThinking?`, `toolChoice?: "auto"\|"any"\|"none"\|{type:"tool",name}`, `client?: Anthropic` |
| `openai-completions` | `toolChoice?: ChatCompletionToolChoiceOption`, `reasoningEffort?: "minimal"\|"low"\|"medium"\|"high"\|"xhigh"\|"max"`, `thinkingBudgets?` |
| `openai-responses` | `reasoningEffort?`, `reasoningSummary?: "auto"\|"detailed"\|"concise"\|null`, `serviceTier?`, `toolChoice?` |
| `openai-codex-responses` | `reasoningEffort?: "none"\|"minimal"\|...`, `reasoningSummary?`, `serviceTier?`, `textVerbosity?: "low"\|"medium"\|"high"`, `toolChoice?: "auto"\|"none"\|"required"` |
| `google-generative-ai` | `toolChoice?: "auto"\|"none"\|"any"`, `thinking?: { enabled: boolean; budgetTokens?: number; level?: GoogleApiThinkingLevel }` |
| `mistral-conversations` | `toolChoice?: "auto"\|"none"\|"any"\|"required"\|{...}`, `promptMode?: "reasoning"`, `reasoningEffort?: "none"\|"high"` |
| `bedrock-converse-stream` | `region?`, `profile?`, `toolChoice?`, `reasoning?`, `thinkingBudgets?`, `interleavedThinking?`, `thinkingDisplay?`, `requestMetadata?`, `bearerToken?` |
| `pi-messages` | `reasoning?: ThinkingLevel`, `toolChoice?`, `debug?: boolean` |

### 3.4 消息变换：`transformMessages`（provider 无关）

`src/api/transform-messages.ts` 是所有适配器的前置公共步骤，`transformMessages(messages, model, normalizeToolCallId?)`。三件事：

**(a) 图片降级。** 若 `!model.input.includes("image")`：`user` 消息数组中的 image 替换为文本占位
`"(image omitted: model does not support images)"`；`toolResult` 中的是 `"(tool image omitted: model does not support images)"`。占位符会**去重合并**（连续 image 只产生一个占位文本）。

**(b) 跨模型内容规范化。** 判定 `isSameModel = assistantMsg.provider === model.provider && assistantMsg.api === model.api && assistantMsg.model === model.id`：

- thinking + `redacted`：仅同模型保留，否则**整块丢弃**。
- thinking 且同模型且有 `thinkingSignature`：保留（即使 `thinking` 为空，OpenAI 加密推理需要）。
- thinking 且空文本、无签名：丢弃。
- thinking 跨模型：转成 `{type:"text", text: block.thinking}`。
- `toolCall.thoughtSignature`：跨模型时删除。
- `toolCall.id`：跨模型且提供 `normalizeToolCallId` 时改写，并记入 `toolCallIdMap` 供后续 `toolResult` 同步改写。

**(c) 孤儿 tool call 补齐（第二遍扫描）。** 维护 `pendingToolCalls`；遇到新 assistant 消息/普通 user 消息/流结束时，为尚无对应 `toolResult` 的 tool call 插入合成结果：

```ts
{ role: "toolResult", toolCallId: tc.id, toolName: tc.name,
  content: [{ type: "text", text: "No result provided" }], isError: true, timestamp: Date.now() }
```

同时：`stopReason === "error" || "aborted"` 的 assistant 消息**整个跳过**（注释：可能有残缺内容，回放会引发 API 错误，模型应从最后一个有效状态重试）。

各 provider 的工具调用 id 规范化（作为第三参传入）：

- Anthropic：`id.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 64)`
- OpenAI completions：处理 Responses API 的 `{call_id}|{id}` 形式（`|` 前是 call_id、后是 item id，id 可能 400+ 字符且含 `+ / =`）；合成 `callId_itemId`，超 40 字符则 `prefix_shortHash8`；`provider === "openai"` 时截断到 40。
- Mistral：`MISTRAL_TOOL_CALL_ID_LENGTH = 9` 的规范化器。

### 3.5 `streamSimple`：抽象等级 → provider 原生参数

所有适配器的 `streamSimple` 都是同一套路：

```ts
const base = { ...buildBaseOptions(model, context, options, options?.apiKey), toolChoice: options?.toolChoice };
// 无 reasoning → 显式关闭 thinking
// 有 reasoning → clampThinkingLevel(...) + provider 专有映射
return stream(model, context, { ...base, <provider fields> });
```

`buildBaseOptions`（`api/simple-options.ts`）做几件必做之事：

```ts
export function buildBaseOptions(model, context, options?, apiKey?): StreamOptions {
  const samplingParams = model.samplingParams || options?.samplingParams
    ? { ...model.samplingParams, ...options?.samplingParams } : undefined;   // 按 key 覆盖
  return {
    temperature: options?.temperature,
    samplingParams,
    maxTokens: clampMaxTokensToContext(model, context, options?.maxTokens ?? model.maxTokens),
    signal, telemetryContext,
    apiKey: apiKey || options?.apiKey,
    fetch, transport, cacheRetention, sessionId, headers,
    onPayload, onResponse, timeoutMs, websocketConnectTimeoutMs,
    maxRetries, maxRetryDelayMs, metadata, env,
  };
}
```

`clampMaxTokensToContext`：

```ts
const CONTEXT_SAFETY_TOKENS = 4096, MIN_MAX_TOKENS = 1;
const available = model.contextWindow - estimateContextTokens(context).tokens - CONTEXT_SAFETY_TOKENS;
return Math.min(maxTokens, Math.max(MIN_MAX_TOKENS, available));
```

思考预算换算（token-based provider 用）：

```ts
adjustMaxTokensForThinking(baseMaxTokens, modelMaxTokens, reasoningLevel, customBudgets)
  → { maxTokens, thinkingBudget }
// baseMaxTokens 为 undefined 表示调用方未显式限长：用 model cap，把 thinking 塞进去
clampReasoning(effort) => effort === "xhigh" || effort === "max" ? "high" : effort
```

### 3.6 Anthropic Messages 适配器要点

**请求构造 `buildParams`**：

- `messages` 来自 `transformMessages` → `splitDeferredTools` → `convertMessages`。
- `max_tokens = options?.maxTokens ?? model.maxTokens`；`stream: true`；`betas` 按需：
  - OAuth token 时 `["claude-code-20250219", "oauth-2025-04-20"]`
  - `FINE_GRAINED_TOOL_STREAMING_BETA = "fine-grained-tool-streaming-2025-05-14"`（当 `!supportsEagerToolInputStreaming` 且有工具）
  - `INTERLEAVED_THINKING_BETA = "interleaved-thinking-2025-05-14"`
  - `SERVER_SIDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"`（当有 `allowedFallbackModels`）
  - `MID_CONVERSATION_OUTPUT_CONFIG_BETA` / `THINKING_BINDING_CONTROLS_BETA`（当 `supportsMidConvoEffort`）
- **OAuth 身份**：`isOAuthToken(apiKey) = apiKey.includes("sk-ant-oat")`。是 OAuth 时 `system` 第一块固定为
  `"You are Claude Code, Anthropic's official CLI for Claude."`，工具名映射到 Claude Code 规范大小写（`Read/Write/Edit/Bash/Grep/Glob/...`，见 `claudeCodeTools`）。
- `temperature` 仅在 `!thinkingEnabled && compat.supportsTemperature && !supportsMidConvoEffort` 时发送（与 extended thinking 不兼容；Opus 4.7+ 拒绝非默认 temperature）。
- thinking：
  - `supportsMidConvoEffort` → `thinking = { type: "adaptive", display, block_binding: { prefix_mismatch_behavior: "drop_block" } }`，`output_config = { effort: "high" }`，并插入 `role: "system", content: []` 的 effort 标记消息。
  - `forceAdaptiveThinking` → `{ type: "adaptive", display }` + 可选 `output_config.effort`。
  - 否则 → `{ type: "enabled", budget_tokens: options.thinkingBudgetTokens || 1024, display }`。
  - `thinkingEnabled === false && model.thinkingLevelMap?.off !== null` → `{ type: "disabled" }`。
- `metadata.user_id` 透传；`tool_choice` 映射；`fallbacks` 来自 `compat.allowedFallbackModels`。

**消息转换 `convertMessages`**：

- user：字符串非空则直接 `content`；数组则 text/image block 化，**过滤空文本块**，全空则整条跳过。
- assistant：text（丢空）、thinking（`redacted` → `{type:"redacted_thinking", data: signature}`；无签名时按 `allowEmptySignature` 决定 `signature:""` 还是降级为 text）、toolCall → `{type:"tool_use", id, name, input}`。
- **连续 toolResult 合并**为一个 `role:"user"` 消息里的多个 `tool_result` block（注释说这是 z.ai Anthropic endpoint 的需要）。
- `cache_control` 打在 system 块、最后一个 user/assistant/tool_result 文本块、以及（`supportsCacheControlOnTools` 时）**最后一个 tool 定义**上；`ttl: "1h"` 仅在 `cacheRetention === "long" && supportsLongCacheRetention`。

**工具 schema**：

```ts
name, description,
...(supportsEagerToolInputStreaming ? { eager_input_streaming: true } : {}),
...(strict === true ? { strict: true } : {}),
input_schema: strict === true ? { ...parameters, ...legacyInputSchema } : legacyInputSchema,
...(deferLoading ? { defer_loading: true } : {}),
...(cacheControl && index === tools.length - 1 ? { cache_control: cacheControl } : {})
// legacyInputSchema = { type: "object", properties: schema.properties ?? {}, required: schema.required ?? [] }
```

**流解析**：自己实现 SSE 解码（`decodeSseLine` / `flushSseEvent`，事件白名单 `ANTHROPIC_MESSAGE_EVENTS`），
对每个事件：`message_start`（取 `responseId`、`model`、usage、`input_transformations`）、`content_block_start`、
`content_block_delta`、`content_block_stop`、`message_delta`、`message_stop`。
`signature_delta` **不产生事件**，只累加到 `block.thinkingSignature`。
`input_json_delta` 累加 `block.partialJson` 并 `parseStreamingJson` 得到部分 `arguments`，推送 `toolcall_delta`。
流结束条件：`sawMessageStart && !sawMessageEnd` → 抛 `"Anthropic stream ended before message_stop"`。

`mapStopReason`：

| Anthropic | pi |
|---|---|
| `end_turn` / `pause_turn` / `stop_sequence` | `stop` |
| `max_tokens` | `length` |
| `tool_use` | `toolUse` |
| `refusal` | `error` + `stop_details.explanation` 或 `"The model refused to complete the request"` |
| `sensitive` | `error` + `"Provider stopped with: sensitive"` |
| 其他 | **抛异常** `Unhandled stop reason: X` |

### 3.7 OpenAI Chat Completions 适配器要点

**compat 自动探测**（`detectCompat(model)`，输入是 `provider` + `baseUrl` 字符串）：识别 `isZai / isTogether / isMoonshot / isOpenRouter / isCloudflareWorkersAI / isCloudflareAiGateway / isNvidia / isAntLing / isDeepSeek`，派生：

```ts
supportsStore: !isNonStandard,
supportsDeveloperRole: isOpenRouterDeveloperRoleModel || (!isNonStandard && !isOpenRouter),
supportsReasoningEffort: !isGrok && !isZai && !isMoonshot && !isTogether && !isCloudflareAiGateway && !isNvidia && !isAntLing,
maxTokensField: useMaxTokens ? "max_tokens" : "max_completion_tokens",
requiresReasoningContentOnAssistantMessages: isDeepSeek,
thinkingFormat: isDeepSeek ? "deepseek" : isZai ? "zai" : isTogether ? "together" : isAntLing ? "ant-ling" : isOpenRouter ? "openrouter" : "openai",
supportsStrictMode: !isMoonshot && !isTogether && !isCloudflareAiGateway && !isNvidia,
cacheControlFormat: provider === "openrouter" && model.id.startsWith("anthropic/") ? "anthropic" : undefined,
sessionAffinityFormat: isOpenRouter ? "openrouter" : "openai",
supportsLongCacheRetention: !(isTogether || isCloudflareWorkersAI || isCloudflareAiGateway || isNvidia || isAntLing),
```

然后 `getCompat(model)` 用 `model.compat` 的显式值 `??` 覆盖。

**`thinkingFormat` 的 11 种取值**（`OpenAICompletionsCompat.thinkingFormat`）决定 thinking 参数怎么发：
`"openai"`(reasoning_effort) / `"openrouter"`(`reasoning:{effort}`) / `"deepseek"`(`thinking:{type}` + reasoning_effort) / `"together"`(`reasoning:{enabled}` + reasoning_effort) / `"baseten"`(`chat_template_args` + reasoning_effort) / `"zai"`(`thinking:{type,clear_thinking}`) / `"qwen"`(顶层 `enable_thinking`) / `"qwen-chat-template"`(`chat_template_kwargs.enable_thinking` + `preserve_thinking`) / `"chat-template"`(`chat_template_kwargs`，值可由 `{"$var":"thinking.enabled"|"thinking.effort"|"thinking.budget"}` 占位) / `"string-thinking"`(顶层 `thinking` 为字符串) / `"ant-ling"`(`reasoning:{effort}`，仅映射非 null 时)。

**请求体构造**：`{ model, messages, stream: true, prompt_cache_key, prompt_cache_retention, stream_options: {include_usage:true}, store: false, max_completion_tokens|max_tokens, temperature, tools, tool_choice, priority? }`，最后 `Object.assign(params, options.samplingParams)`（**注释：最后 merge，自定义 key 覆盖具名字段**）。

- `prompt_cache_key` 在 `baseUrl.includes("api.openai.com") && cacheRetention !== "none"` 或 `cacheRetention==="long" && supportsLongCacheRetention` 时设置。
- 无活跃工具但历史里有 tool 消息 → 仍发 `params.tools = []`（某些 Anthropic 代理要求）。
- `custom` 工具：`resolveGrammarConstrainedSampling` 命中时发 `{type:"custom", custom:{name, description, format:{type:"grammar", grammar:{syntax, definition}}}}`。

**流解析**：块级状态机。text/thinking 是**单例块**（`ensureTextBlock` / `ensureThinkingBlock` 懒创建），tool call 同时按 `streamIndex` 和 `id` 索引（`toolCallBlocksByIndex` / `toolCallBlocksById`）以容忍两者交错缺失。reasoning 字段优先级 `["reasoning_content","reasoning","reasoning_text"]`，**取第一个非空**，并把字段名存进 `thinkingSignature`（`opencode-go` 的 `reasoning` 特判成 `"reasoning_content"`）。`reasoning_details` 数组是回放元数据，**不产生 delta**，只在块结束时 `JSON.stringify` 进 `thinkingSignature`。流结束后遍历所有块调 `finishBlock()` 补发 `*_end`（有些 provider 不发 stop）。

无 `finish_reason` 时的兜底：`!compat.supportsFinishReason` → `stopReason = 有 toolCall ? "toolUse" : "stop"`；否则抛 `"Stream ended without finish_reason"`。

**usage 解析**（`parseChunkUsage`）：

```ts
cacheRead = prompt_tokens_details?.cached_tokens ?? prompt_cache_hit_tokens ?? cached_tokens ?? 0;
cacheWrite = prompt_tokens_details?.cache_write_tokens || 0;
input = Math.max(0, prompt_tokens - cacheRead - cacheWrite);   // 注意是减法
output = completion_tokens;                                     // 已含 reasoning_tokens
reasoning = completion_tokens_details?.reasoning_tokens || 0;
totalTokens = input + output + cacheRead + cacheWrite;
```

同时支持 fallback：某些 provider（Moonshot）把 usage 放在 `choice.usage`。

### 3.8 OpenAI Responses / Azure / Codex

- `openai-responses` 请求是 `input: items`（不是 `messages`），`prompt_cache_key` / `prompt_cache_retention` / `prompt_cache_options`（GPT-5.6+ 的 `{mode:"explicit", ttl:"30m"}`），`store: false`，`max_output_tokens` 有最小值常量 `OPENAI_RESPONSES_MIN_OUTPUT_TOKENS = 16`。
- 工具：`convertResponsesTools` 产出**扁平**结构（`{type:"function", name, description, parameters, strict}`，没有嵌套 `function` 对象）或 grammar 版 `{type:"custom", name, description, format:{type:"grammar",...}}`，延迟工具加 `defer_loading: true`。
- 延迟工具两种模式：`supportsAdditionalTools → "additional-tools"`（消息级 `additional_tools` input item）、`supportsToolSearch → "tool-search"`。
- 解析在 `openai-responses-shared.ts` 的 `processResponsesStream`（793 行）里，另有 service tier 定价系数 `getServiceTierCostMultiplier` / `applyServiceTierPricing`。
- `openai-codex-responses` 独有 `transport: "sse" | "websocket" | "websocket-cached" | "auto"`，`websocket-cached` 会复用缓存上下文（`useCachedContext`），失败会记录 `provider_transport_failure` 诊断并可能回退。运行时无 WebSocket 能力时抛 `"WebSocket transport is not available in this runtime"`。

### 3.9 Google Generative AI / Vertex

- 用 `@google/genai` 的 `client.models.generateContentStream(params)`；**不支持自定义 fetch**（显式抛错 `"Custom fetch is not supported by the Google Generative AI adapter"`）。
- 解析 `chunk.candidates[0].content.parts[]`：`part.text` + `part.thought` 判定 text/thinking（`isThinkingPart`），文本块与 thinking 块**交替**时先关旧块再开新块；`part.thoughtSignature` 用 `retainThoughtSignature` 保留。
- `part.functionCall` → 一次性 `toolcall_start` + `toolcall_delta` + `toolcall_end`（Google 不流式化参数）。缺 id 或 id 重复时本地生成 `` `${name}_${Date.now()}_${++toolCallCounter}` ``。
- usage：`input = promptTokenCount - cachedContentTokenCount`；`output = candidatesTokenCount + thoughtsTokenCount`；`cacheWrite = 0`；`totalTokens = totalTokenCount`。
- 工具有两种序列化：`parametersJsonSchema`（默认）或 `parameters`（`useParameters=true`，先 `sanitizeForOpenApi`）。
- finishReason 映射在 `google-shared.ts` 的 `mapStopReason`（并有 `mapStopReasonString` 处理字符串形式）；若已有 toolCall 且映射为 `stop`，则改判 `toolUse`。
- `google-vertex` 复用 `google-shared.ts`，额外处理项目/位置与 ADC。

### 3.10 Mistral / Bedrock / pi-messages

- **Mistral**：请求体 `{ model, stream, messages, tools, temperature, maxTokens, toolChoice, promptMode, reasoningEffort, promptCacheKey }`；消息用 `toolCalls/toolCallId/name`（**驼峰**，非 OpenAI 的 `tool_calls/tool_call_id`）；content chunk 类型 `text | image_url | thinking`；tool call id 规范化为 9 字符。
- **Bedrock**：`bedrock-converse-stream.ts`（1325 行）走 AWS SDK `ConverseStream`；`BedrockOptions` 见 §3.3；自定义 headers 通过 Smithy `build` 中间件注入以被 SigV4 覆盖（保留头 `x-amz-*`/`authorization`/`host` 被忽略）；`bearerToken` 存在时绕过 SigV4 改发 `Authorization: Bearer`。
- **pi-messages**：这是 pi 自己的线协议，也是**最容易照抄参考**的适配器。请求是单个 POST：

```ts
POST `${model.baseUrl.replace(/\/+$/u,"")}/messages`
headers: { authorization: `Bearer ${apiKey}`, accept: "text/event-stream", "content-type": "application/json", ...options.headers }
body: { model: model.id, context, options: { temperature, maxTokens, reasoning, cacheRetention, sessionId, toolChoice } }
```

响应是 SSE，`data:` 行承载 `PiMessagesEvent` JSON，`[DONE]` 被忽略。`createEventConverter` 把每个事件**转换**成内部 `AssistantMessageEvent` 并同时维护一个 `partial`——这是"服务端已经做完 provider 适配、客户端只做事件重放"的形态：

```ts
export type PiMessagesEvent =
  | { type: "start" }
  | { type: "text_start"; contentIndex: number }
  | { type: "text_delta"; contentIndex: number; delta: string }
  | { type: "text_end"; contentIndex: number; content: string; contentSignature?: string }
  | { type: "thinking_start"; contentIndex: number }
  | { type: "thinking_delta"; contentIndex: number; delta: string }
  | { type: "thinking_end"; contentIndex: number; content: string; contentSignature?: string; redacted?: boolean }
  | { type: "toolcall_start"; contentIndex: number; id: string; toolName: string }
  | { type: "toolcall_delta"; contentIndex: number; delta: string }
  | { type: "toolcall_end"; contentIndex: number; toolCall: ToolCall }
  | { type: "done";  reason: "stop"|"length"|"toolUse"; usage: Usage; responseId?; providerThinkingLevel?; rewrite?: PiMessagesRewriteImpact }
  | { type: "error"; reason: "aborted"|"error"; usage: Usage; errorMessage?; responseId?; providerThinkingLevel?; rewrite? };
```

注意：pi-messages 的 `done`/`error` **自带 usage**（其它 provider 靠累加），且错误体解析成 `PiMessagesResponseError`（`code` + `diagnosticDetails`），并生成 `pi_messages_response_failure` 诊断。

### 3.11 `lazyApi` / `lazyStream`：延迟加载与错误包装

```ts
export function lazyStream(model, setup: () => Promise<AsyncIterable<AssistantMessageEvent>>): AssistantMessageEventStream {
  const outer = new AssistantMessageEventStream();
  setup()
    .then((inner) => forwardStream(outer, inner))
    .catch((error) => {
      const message = createSetupErrorMessage(model, error);   // stopReason: "error", usage 全 0
      outer.push({ type: "error", reason: "error", error: message });
      outer.end(message);
    });
  return outer;
}
```

`forwardStream` 把内层事件逐个 `target.push`，最后 `target.end(hasResult(source) ? await source.result() : undefined)`。

```ts
export function lazyApi(load: () => Promise<ProviderStreams>, capabilities?: LazyApiCapabilities): ProviderStreams
```

`capabilities` 是 `{ fetchDeferred?: boolean; cancelDeferred?: boolean }`，决定是否暴露那两个方法。每个 `<api>.lazy.ts` 只有 4 行：

```ts
export const anthropicMessagesApi = (): ProviderStreams =>
  lazyApi(() => import("./anthropic-messages.ts"));
```

**Python 移植建议**：换成 `importlib.import_module` 或 `functools.lru_cache` 包一层；`forwardStream` 对应 `async for event in inner: outer.push(event)`。

---

## 4. 流式事件协议

### 4.1 `AssistantMessageEvent` 全部 12 种（原文）

```ts
export type AssistantMessageEvent =
  | { type: "start";          partial: AssistantMessage }
  | { type: "text_start";     contentIndex: number; partial: AssistantMessage }
  | { type: "text_delta";     contentIndex: number; delta: string; partial: AssistantMessage }
  | { type: "text_end";       contentIndex: number; content: string; partial: AssistantMessage }
  | { type: "thinking_start"; contentIndex: number; partial: AssistantMessage }
  | { type: "thinking_delta"; contentIndex: number; delta: string; partial: AssistantMessage }
  | { type: "thinking_end";   contentIndex: number; content: string; partial: AssistantMessage }
  | { type: "toolcall_start"; contentIndex: number; partial: AssistantMessage }
  | { type: "toolcall_delta"; contentIndex: number; delta: string; partial: AssistantMessage }
  | { type: "toolcall_end";   contentIndex: number; toolCall: ToolCall; partial: AssistantMessage }
  | { type: "done";  reason: Extract<StopReason, "stop"|"length"|"toolUse"|"deferred">; message: AssistantMessage }
  | { type: "error"; reason: Extract<StopReason, "aborted"|"error">; error: AssistantMessage };
```

字段语义：

| 事件 | 独有字段 | 含义 |
|---|---|---|
| `start` | — | 生成开始；`partial` 是空 content 的 AssistantMessage |
| `text_start` | `contentIndex` | 新 text 块已 push 到 `partial.content[i]`，`text` 为空串 |
| `text_delta` | `contentIndex`, `delta` | `delta` 是新文本；`partial.content[i].text` 已增长 |
| `text_end` | `contentIndex`, `content` | `content` 是权威完整文本（覆盖 `partial` 里的值） |
| `thinking_*` | 同上 | 同上，字段是 `thinking`；`redacted` thinking 可能 start 时已完整且不发 delta |
| `toolcall_start` | `contentIndex` | 工具调用开始；`arguments` 此刻是 **provider-specific**（可能空、可能已有部分） |
| `toolcall_delta` | `contentIndex`, `delta` | `delta` 是 **JSON 片段**；`partial.content[i].arguments` 是已解析的部分对象 |
| `toolcall_end` | `contentIndex`, `toolCall` | `toolCall` 是完整但**未经 schema 校验**的工具调用 |
| `done` | `reason`, `message` | 终止；`message` 是最终 AssistantMessage = `partial` |
| `error` | `reason`, `error` | 终止；`error` 是带部分内容 + `errorMessage` 的 AssistantMessage |

### 4.2 `partial` 的精确语义（极易搞错，必须逐字复刻）

`types.ts` 注释原文：

```
`partial` is the shared live response-so-far helper, not an event-time
snapshot. Text and thinking blocks are empty when their `*_start` event is
emitted and grow only through their corresponding `*_delta` events until the
authoritative `*_end`. Redacted thinking may be complete at start and emit no
deltas. Tool-call arguments at `toolcall_start` are provider-specific;
`toolcall_delta` carries subsequent JSON updates.
```

README 补充：**同一个 `AssistantMessage` 对象在所有事件里被原地修改**；消费者不能把它当历史快照保存，必须在处理事件时立即读取。

实现上的统一模式（所有适配器一致）：

```ts
const output: AssistantMessage = { role:"assistant", content: [], api, provider, model: model.id,
  usage: {...全 0}, stopReason: "pending", timestamp: Date.now() };
// 事件全部携带同一个 output 引用；content block 也是原地 push/累加
stream.push({ type: "done", reason: output.stopReason, message: output });
stream.end();
```

Python 移植**必须**用可变 dataclass/dict 并在事件里传同一引用（不要 deep-copy）。这直接影响 `AssistantMessageFrameEncoder`（§4.5）的正确性。

### 4.3 事件顺序契约

```
start → updates* → done        (成功)
start → updates* → error       (生成开始后失败)
error                          (生成开始前 setup 失败，无 start)
```

- `done` 与 update 事件在 `start` 之前**非法**。
- 块之间的事件**不保证连续**：同一上游 chunk 可能同时含 text/thinking/toolcall。
  README 明确举例 `text_start, text_delta, toolcall_start, text_delta, toolcall_delta`。
  消费者必须用 `contentIndex` 关联，不能假设块序列不被打断。
- `error` 之前可能只有 `error`（lazy stream 的 setup 失败）——此时 `createSetupErrorMessage` 生成的 message `content: []`、`usage` 全 0、`stopReason: "error"`。

### 4.4 `EventStream` / `AssistantMessageEventStream` / `result()`

`src/utils/event-stream.ts` 只有 110 行，是一个 **push-based 异步队列**：

```ts
export class EventStream<T, R = T> implements AsyncIterable<T> {
  constructor(isComplete: (event: T) => boolean, extractResult: (event: T) => R) { ... }
  push(event: T): void             // 若 isComplete(event) → done=true 并 resolve finalResult
  end(result?: R): void            // 唤醒所有等待者（done:true）
  async *[Symbol.asyncIterator]()  // 队列非空 → 取出；done → return；否则挂起等待
  result(): Promise<R>             // finalResultPromise
}

export class AssistantMessageEventStream extends EventStream<AssistantMessageEvent, AssistantMessage> {
  constructor() {
    super(
      (event) => event.type === "done" || event.type === "error",
      (event) => {
        if (event.type === "done") return event.message;
        else if (event.type === "error") return event.error;
        throw new Error("Unexpected event type for final result");
      },
    );
  }
}
export function createAssistantMessageEventStream(): AssistantMessageEventStream { ... }
```

内部用 `FifoQueue<T>`（两个栈 `incoming`/`outgoing` 实现摊还 O(1) 队列）；`waiting` 存 `(result: IteratorResult<T>) => void` 的 resolver。

两个使用模式：

```ts
// 模式 A：迭代事件
for await (const event of stream) { ... }

// 模式 B：只要最终结果
const message: AssistantMessage = await stream.result();
```

`Models.complete()` / `completeSimple()` 就是 `.result()` 的语法糖。`result()` 在**首个** `done`/`error` 事件到达时 resolve（`push` 里检查 `isComplete`），不需要等到 `end()`。`push` 在 `done === true` 之后静默丢弃后续事件。

### 4.5 附加协议：`AssistantMessageFrameEncoder`（压缩帧）

`src/utils/assistant-message-frame.ts`（18KB）。它把一条流转换成**可持久化**的紧凑帧序列，用于把流式生成增量落盘：

- 每个流一个 encoder，按序喂入每个事件。
- 理解 `partial` 是 live 的：如果 block-start 事件在 provider 已排入后续 delta 之后才被消费，encoder 会对当前 block 快照一次；已被覆盖的排队 delta 不产生重复帧。
- `reduceAssistantMessageFrames()` 是规范纯 reducer：单遍重建 text/thinking/toolCall arguments，`undefined` 表示无 start 帧；**不**做工具参数 schema 校验。
- 拒绝：重复 start、start 前 update、start 前 `done`、终止事件后的事件、同块重复 start、块类型不匹配。**start 前 `error` 合法且不产生帧。**

这个模块是"流式协议 + 持久化"的桥，Python 侧如果要做 session 恢复/回放，需要单独移植。

---

## 5. Usage 与成本核算

### 5.1 `Usage` 结构

```ts
export interface Usage {
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
  cacheWrite1h?: number;   // cacheWrite 中按 1h 保留写入的子集，仅 Anthropic 报告
  reasoning?: number;      // output 的子集（output 已包含它）；不报告的 provider 留 undefined
  totalTokens: number;
  cost: { input: number; output: number; cacheRead: number; cacheWrite: number; total: number };
}
```

### 5.2 `calculateCost`（`src/models.ts`，必须逐字复刻）

```ts
export function calculateCost<TApi extends Api>(model: Model<TApi>, usage: Usage): Usage["cost"] {
  const inputTokens = usage.input + usage.cacheRead + usage.cacheWrite;
  let rates: ModelCostRates = model.cost;
  let matchedThreshold = -1;
  for (const tier of model.cost.tiers ?? []) {
    if (inputTokens > tier.inputTokensAbove && tier.inputTokensAbove > matchedThreshold) {
      rates = tier;
      matchedThreshold = tier.inputTokensAbove;
    }
  }
  // Anthropic charges 2x base input for 1h cache writes.
  const longWrite  = usage.cacheWrite1h ?? 0;
  const shortWrite = usage.cacheWrite - longWrite;
  usage.cost.input      = (rates.input      / 1000000) * usage.input;
  usage.cost.output     = (rates.output     / 1000000) * usage.output;
  usage.cost.cacheRead  = (rates.cacheRead  / 1000000) * usage.cacheRead;
  usage.cost.cacheWrite = (rates.cacheWrite * shortWrite + rates.input * 2 * longWrite) / 1000000;
  usage.cost.total = usage.cost.input + usage.cost.output + usage.cost.cacheRead + usage.cost.cacheWrite;
  return usage.cost;
}
```

三个容易错的点：

1. 分档阈值比较的是 `input + cacheRead + cacheWrite`（**不是** `usage.input`），且条件是**严格大于** `tier.inputTokensAbove`。
2. 命中多档时取 `inputTokensAbove` **最大**的一档（`> matchedThreshold` 保证）。
3. `cacheWrite1h` 的单价是 `rates.input * 2`，而不是 `rates.cacheWrite`。

### 5.3 各 provider 的 usage 映射差异

| API | 输入 | cacheRead | cacheWrite | reasoning |
|---|---|---|---|---|
| anthropic-messages | `input_tokens` | `cache_read_input_tokens` | `cache_creation_input_tokens`（另有 `cache_creation.ephemeral_1h_input_tokens` → `cacheWrite1h`） | `output_tokens_details.thinking_tokens` |
| openai-completions | `prompt_tokens - cacheRead - cacheWrite`（下限 0） | `prompt_tokens_details.cached_tokens ?? prompt_cache_hit_tokens ?? cached_tokens` | `prompt_tokens_details.cache_write_tokens` | `completion_tokens_details.reasoning_tokens` |
| google-generative-ai | `promptTokenCount - cachedContentTokenCount` | `cachedContentTokenCount` | `0` | `thoughtsTokenCount` |
| pi-messages | 由服务端在 `done`/`error` 事件里直接给整块 `usage` | | | |

Anthropic / google 都要自己算 `totalTokens = input + output + cacheRead + cacheWrite`（provider 不提供 total）；
OpenAI 的 `completion_tokens` **已包含** reasoning tokens（所以 `output` 不减）。

### 5.4 上下文估算（`src/utils/estimate.ts`）

```ts
const CHARS_PER_TOKEN = 4;
const ESTIMATED_IMAGE_CHARS = 4800;
export function calculateContextTokens(usage: Usage): number {
  return usage.totalTokens || usage.input + usage.output + usage.cacheRead + usage.cacheWrite;
}
```

`ContextUsageEstimate { tokens, usageTokens, trailingTokens, lastUsageIndex }`：如果历史里能找到最近的 assistant usage，就用它作为锚点，只估算其后的 trailing 消息，避免整段重算。

`src/utils/overflow.ts` 的 `isContextOverflow(message, contextWindow)` 通过匹配 provider 错误文案模式（`getOverflowPatterns()`）判断是否是上下文溢出，另有 `isRecoverableLength(message, desiredMaxOutput)`。

---

## 6. Auth 与凭证

### 6.1 凭证形状（`src/auth/types.ts`）

```ts
export interface ApiKeyCredential { type: "api_key"; key?: string; env?: ProviderEnv; }
export interface OAuthCredentials {
  refresh: string;
  access: string;
  expires: number;                 // Unix ms
  [key: string]: unknown;          // provider 额外字段
}
export interface OAuthCredential extends OAuthCredentials { type: "oauth"; }
export type Credential = ApiKeyCredential | OAuthCredential;
export interface CredentialInfo { providerId: string; type: Credential["type"]; }
```

`ModelAuth { apiKey?, headers?, baseUrl? }` 是解析结果；`AuthResult { auth, env?, source? }`，`source` 是给 UI 展示的标签（如 `"ANTHROPIC_API_KEY"` / `"OAuth"` / `"stored credential"` / `"~/.aws/credentials"`）。

### 6.2 `auth.json` 文件形状

`CredentialStore` 是抽象接口，持久化实现由上层 app 提供。coding-agent 的 `AuthStorage`（`packages/coding-agent/src/core/auth-storage.ts`）是参考实现：

- 路径：`getAgentDir()/auth.json`，默认 `~/.pi/agent/auth.json`。
- 结构：`Record<string, Credential>`（顶层 key = provider id）。
  ```json
  { "anthropic": { "type": "api_key", "key": "sk-..." } }
  ```
- 写入模式 `0o600`（仅创建时设置，保留管理员既有 mode/ACL）；父目录 `0o700`。
- 跨进程用 `proper-lockfile`（sync 版最多 10 次重试、间隔 20ms；async 版 `staleMs = 30_000`、`maxDelayMs = 2_000`）。
- 读取会用 revision 做共享缓存（`sharedAuthFileReadState`），可被外部改动失效。

### 6.3 `CredentialStore` 契约

```ts
export interface CredentialStore {
  read(providerId, options?): Promise<Credential | undefined>;   // 可能已过期；展示用
  list(options?): Promise<readonly CredentialInfo[]>;            // 不得解析 secret 或执行 key 命令
  modify(providerId, fn, options?): Promise<Credential | undefined>; // 唯一写路径，串行 read-modify-write
  delete(providerId, options?): Promise<void>;
}
```

`InMemoryCredentialStore` 用 per-provider promise chain 串行化（`enqueue`）。`modify` 的 `fn` 返回 `undefined` 表示不改。

### 6.4 解析顺序（`src/auth/resolve.ts` → `resolveProviderAuth`）

```
1. overrides.apiKey 非 undefined 且 provider.auth.apiKey 存在
   → 用 { type:"api_key", key: overrides.apiKey, env: overrides.env } 直接解析（最高优先级）
2. 读取 stored credential（CredentialStore.read）
   2a. stored.type === "oauth" 且 provider.auth.oauth 存在 → resolveStoredOAuth
   2b. stored.type === "api_key" 且 provider.auth.apiKey 存在 → resolveApiKey(stored)
   2c. 类型无匹配 handler → return undefined（不做 env 回退！）
3. 无 stored credential → ambient：provider.auth.apiKey.resolve(ctx, undefined)
```

**关键不变量**：`A stored credential owns the provider`。一旦有 stored credential，即使 refresh 失败也**不会**静默回退到环境变量。

`overrides.env`（`ProviderEnv`）会通过 `overlayEnvAuthContext` 覆盖 `AuthContext.env`（`env[name] || base.env(name)`）。

### 6.5 标准 api-key 解析（`envApiKeyAuth`）

```ts
export function envApiKeyAuth(name: string, envVars: readonly string[]): ApiKeyAuth {
  return {
    name,
    login: async (interaction) => ({ type: "api_key",
      key: await interaction.prompt({ type: "secret", message: `Enter ${name}` }) }),
    resolve: async ({ ctx, credential, signal }) => {
      signal.throwIfAborted();
      if (credential?.key) return { auth: { apiKey: credential.key }, env: credential.env, source: "stored credential" };
      for (const envVar of envVars) {
        const value = await ctx.env(envVar);
        signal.throwIfAborted();
        if (value) return { auth: { apiKey: value }, source: envVar };
      }
      return undefined;
    },
  };
}
```

**顺序即 `envVars` 数组顺序**，取第一个非空。Anthropic 是特例（`src/providers/anthropic.ts`）：

```
stored key
→ ANTHROPIC_AUTH_TOKEN  → { headers: { Authorization: `Bearer ${token}` } }   // 注意：不是 apiKey
→ ANTHROPIC_OAUTH_TOKEN → apiKey
→ ANTHROPIC_API_KEY     → apiKey
```

其它 provider 的环境变量映射表在 `src/env-api-keys.ts` 的 `envMap`（`github-copilot → COPILOT_GITHUB_TOKEN`；`anthropic` 特判；`google → GEMINI_API_KEY`；`huggingface → HF_TOKEN`；`vercel-ai-gateway → AI_GATEWAY_API_KEY` 等，见 §6.8）。

`AuthContext`：

```ts
export interface AuthContext {
  env(name: string): Promise<string | undefined>;
  fileExists(path: string): Promise<boolean>;   // 支持前导 "~"；浏览器恒 false
}
```

`defaultProviderAuthContext()` 从 `process.env` 读（空串/纯空白视为 undefined），`fileExists` 用 `node:fs/promises` 的 `access`。

### 6.6 OAuth 流程

`OAuthAuth` 接口把"刷新"与"派生请求 auth"拆开，让 `Models` 掌握加锁：

```ts
export interface OAuthAuth {
  name: string;
  isSubscription?: boolean;
  loginLabel?: string;
  login(interaction: ProviderAuthInteraction): Promise<OAuthCredential>;
  refresh(credential: OAuthCredential, signal: AbortSignal): Promise<OAuthCredential>;
  toAuth(credential: OAuthCredential): Promise<ModelAuth>;
}
```

刷新策略（`resolveStoredOAuth`，双检锁）：

```ts
const DEFAULT_OAUTH_MINIMUM_VALIDITY_MS = 5 * 60 * 1000;   // 5 分钟
const DEFAULT_OAUTH_REFRESH_TIMEOUT_MS = 15_000;
const expiresSoon = (c) => Date.now() + minimumValidityMs >= c.expires;
// 乐观检查过期 → 在 credentials.modify 内重新检查 → 只 refresh 一次 → 持久化轮换后的 token
// refresh signal = AbortSignal.any([callerSignal, AbortSignal.timeout(15_000)])
// 失败抛 ModelsError("oauth", `OAuth refresh failed for ${providerId}`)
```

`toAuth` 失败抛 `ModelsError("oauth", ...)`；`refresh` 返回的 token 若仍早于 `minOAuthValidityMs` 过期，抛
`"OAuth refresh returned a token that expires too soon for ${providerId}"`。

已实现的 OAuth flow（`src/auth/oauth/`）：`anthropic.ts`、`openai-codex.ts`、`github-copilot.ts`、`openrouter.ts`、`kimi-coding.ts`、`xai.ts`、`radius.ts`，共用 `pkce.ts`（PKCE）、`device-code.ts`（device code）、`oauth-page.ts`（本地回调页）。

Anthropic 参考（`anthropicOAuth`）：

```ts
{
  name: "Anthropic (Claude Pro/Max)",
  isSubscription: true,
  login: loginAnthropic,                                   // 本地回调服务器 + PKCE + state + manual_code 竞速
  refresh: (credential, signal) => refreshAnthropicToken(credential.refresh, signal),
  async toAuth(credential) { return { apiKey: credential.access }; },
}
// refresh 返回：
{ type: "oauth", refresh: data.refresh_token, access: data.access_token,
  expires: Date.now() + data.expires_in * 1000 - 5 * 60 * 1000 }   // 提前 5 分钟视为过期
```

交互协议（`AuthInteraction`）：

```ts
prompt(prompt: AuthPrompt): Promise<string>;   // 返回输入的字符串 / select 的 option id；取消时 reject
notify(event: AuthEvent): void;
// AuthPrompt = { signal?, type: "text"|"secret"|"select"|"manual_code", message, placeholder?, options? }
// AuthEvent  = info | auth_url | device_code | progress
```

`loadAnthropicOAuth()` 等通过**变量 specifier 的动态 import** 加载（`auth/oauth/load.ts`），并有 `registerBundledOAuthFlowLoaders()` 逃生口给单文件 Bun 二进制用。

### 6.7 运行时 API key 覆盖

优先级（从高到低）：

1. `options.apiKey`（每次请求显式传入）
2. `Models.getAuth(model, { apiKey })` 的 `overrides.apiKey`
3. `CredentialStore` 中的 credential
4. 环境变量 / ambient（仅当无 stored credential）

`Models.applyAuth` 里 `const apiKey = options?.apiKey ?? auth.apiKey;`，并把 `headers`/`env`/`baseUrl` 一并合并。

header 合并顺序（README 原文）：

```
provider auth headers -> model.headers -> explicit options.headers -> transformHeaders -> Provider.stream*()
```

大小写不敏感去重；`null` 值用于**抑制**下层同名默认头。`transformHeaders` 属于 `Models` 而非 `Provider`，调用 provider 前必须被消费掉。

### 6.8 环境变量总表（节选，完整见 `src/env-api-keys.ts` 与 README §Environment Variables）

| Provider | Env |
|---|---|
| anthropic | `ANTHROPIC_AUTH_TOKEN`（→ Bearer header）、`ANTHROPIC_OAUTH_TOKEN`、`ANTHROPIC_API_KEY` |
| openai | `OPENAI_API_KEY` |
| azure-openai-responses | `AZURE_OPENAI_API_KEY` + `AZURE_OPENAI_BASE_URL`/`AZURE_OPENAI_RESOURCE_NAME`（+ `AZURE_OPENAI_API_VERSION`、`AZURE_OPENAI_DEPLOYMENT_NAME_MAP`） |
| google | `GEMINI_API_KEY` |
| google-vertex | `GOOGLE_CLOUD_API_KEY` 或 `GOOGLE_CLOUD_PROJECT`/`GCLOUD_PROJECT` + `GOOGLE_CLOUD_LOCATION` + ADC |
| deepseek / nvidia / groq / cerebras / mistral / fireworks / together / baseten / openrouter | `<NAME>_API_KEY` |
| zai / zai-coding-cn | `ZAI_API_KEY` / `ZAI_CODING_CN_API_KEY` |
| minimax / minimax-cn | `MINIMAX_API_KEY` / `MINIMAX_CN_API_KEY` |
| moonshotai(-cn) | `MOONSHOT_API_KEY` |
| huggingface | `HF_TOKEN` |
| vercel-ai-gateway | `AI_GATEWAY_API_KEY` |
| github-copilot | `COPILOT_GITHUB_TOKEN` |
| cloudflare-* | `CLOUDFLARE_API_KEY`（AI Gateway 另需 `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_GATEWAY_ID`） |
| amazon-bedrock | 环境凭证链（`AWS_PROFILE`、access key 对、`AWS_BEARER_TOKEN_BEDROCK`、ECS/WebIdentity） |
| kimi-coding | `KIMI_API_KEY` |
| qwen-token-plan* | `QWEN_TOKEN_PLAN_API_KEY` / `QWEN_TOKEN_PLAN_CN_API_KEY` |
| xiaomi* | `XIAOMI_API_KEY` / `XIAOMI_TOKEN_PLAN_{CN,AMS,SGP}_API_KEY` |
| 通用 | `PI_CACHE_RETENTION=long`（legacy，把默认 `short` 改成 `long`） |

---

## 7. 工具定义与参数校验

### 7.1 `Tool`

```ts
export interface Tool<TParameters extends TSchema = TSchema> {
  name: string;
  description: string;
  parameters: TParameters;      // TypeBox schema（本质是 JSON Schema + Symbol.for("TypeBox.Kind") 标记）
  constrainedSampling?: false | ConstrainedSamplingConfig;
}

export type ConstrainedSamplingConfig =
  | { type: "json_schema"; strict: "prefer" | "require" }
  | { type: "grammar"; variants: GrammarVariants };   // GrammarVariants = Partial<Record<"openai_lark"|"openai_regex", string>>
```

- `name` 在 provider 侧可能被改写（Anthropic OAuth 的 Claude Code 大小写映射）。
- `parameters` 需要能**序列化成纯 JSON**（README 明确把它当作分布式友好的表示）。
- Python 侧：`parameters: dict[str, Any]`（JSON Schema），验证用 `jsonschema` 或 pydantic 的 `TypeAdapter`；`TypeBox.Kind` 标记在 Python 里可以换成"是否原生 JSON Schema"的布尔。

### 7.2 `validateToolArguments`（`src/utils/validation.ts`）

```ts
export function validateToolCall(tools: Tool[], toolCall: ToolCall): any {
  const tool = tools.find((t) => t.name === toolCall.name);
  if (!tool) throw new Error(`Tool "${toolCall.name}" not found`);
  return validateToolArguments(tool, toolCall);
}

export function validateToolArguments(tool: Tool, toolCall: ToolCall): any {
  const args = structuredClone(toolCall.arguments);
  normalizeOptionalNulls(args, tool.parameters as JsonSchemaObject);
  Value.Convert(tool.parameters, args);
  const validator = getValidator(tool.parameters);   // Compile(schema)，WeakMap 缓存
  if (!Object.getOwnPropertySymbols(tool.parameters).includes(TYPEBOX_KIND)) {
    const coerced = coerceWithJsonSchema(args, tool.parameters as JsonSchemaObject);  // 非 TypeBox 的兜底强转
    ...
  }
  if (validator.Check(args)) return args;
  const errors = validator.Errors(args)
    .map((error) => `  - ${formatValidationPath(error)}: ${error.message}`)
    .join("\n") || "Unknown validation error";
  throw new Error(
    `Validation failed for tool "${toolCall.name}":\n${errors}\n\nReceived arguments:\n${JSON.stringify(toolCall.arguments, null, 2)}`
  );
}
```

三个预处理步骤，顺序固定：

1. **`normalizeOptionalNulls`**：对**非 required**、且 schema 不允许 `null` 的属性，删除值为 `null` 的键（LLM 常发 `null` 表示"没填"）。递归处理 object/array；`$ref` 属性跳过。
2. **`Value.Convert(tool.parameters, args)`**：TypeBox 的类型强转（`"${number}"` → number 等）。
3. **非 TypeBox 兜底 `coerceWithJsonSchema`**：手写的一套 coercion，处理 `allOf/anyOf/oneOf`、`type` 数组联合、`additionalProperties` object 形式的递归。`coercePrimitiveByType` 的规则值得注意：
   - `number`：`null → 0`；非空字符串 `Number()` 有限则转；`boolean → 1/0`
   - `integer`：`null → 0`；字符串转整数；`boolean → 1/0`
   - `boolean`：`null → false`；`"true"/"false"`；`1/0`
   - `string`：`null → ""`；number/boolean → `String()`
   - `null`：`""` / `0` / `false` → `null`

错误信息格式（逐字）：`Validation failed for tool "<name>":` + 缩进两空格的 `- <path>: <message>` 列表 + `Received arguments:` + 2 空格缩进的原 JSON。

`formatValidationPath`：`instancePath` 去掉前导 `/`、`/` → `.`；`required` 关键字错误时额外拼上缺失属性名；空路径显示 `"root"`。

注意 README 的使用模式：校验失败**不抛给上层的终止错误**，而是把 `error.message` 作为 `isError: true` 的 `toolResult` 回灌给模型让它重试。

### 7.3 各 provider 的工具 schema 传递

| API | 形态 |
|---|---|
| anthropic-messages | `{name, description, eager_input_streaming?, strict?, input_schema, defer_loading?, cache_control?}`，`input_schema` 默认是精简的 `{type:"object", properties, required}`（**丢弃**其它 JSON Schema 关键字），仅 `strict===true` 时才带完整 schema |
| openai-completions | `{type:"function", function:{name, description, parameters, strict}}`；`strict` 仅在 `supportsStrictMode !== false` 时出现。grammar 工具走 `{type:"custom", custom:{name, description, format:{type:"grammar", grammar:{syntax, definition}}}}` |
| openai-responses | 扁平 `{type:"function", name, description, parameters, strict?, defer_loading?}` 或 `{type:"custom", name, description, format:{...}, defer_loading?}` |
| google-generative-ai | `[{functionDeclarations: [{name, description, parametersJsonSchema} | {name, description, parameters}]}]`；`useParameters=true` 时先 `sanitizeForOpenApi` |
| mistral-conversations | `{type:"function", function:{name, description, parameters, strict}}` |
| bedrock-converse-stream | Converse `toolConfig.tools[].toolSpec` |

### 7.4 约束采样（`src/api/constrained-sampling.ts`）

**strict JSON schema**（`makeStrictJsonSchema`）：把任意 schema 转成 provider 约束采样要求的严格子集。被拒绝的键：

```
$ref, $defs, definitions, allOf, oneOf, patternProperties, dependentSchemas,
dependencies, unevaluatedProperties, propertyNames, contains, prefixItems, not, if, then, else
```

规则：

- 布尔 schema 不支持；`anyOf` 至少一项且不能含 object/array 变体；`items` 不能是 tuple。
- `properties` 必须配 `type: "object"`；`additionalProperties` 必须是 `false`（显式或补上）。
- **所有 property 都进 `required`**；非 required 且不允许 null 的属性被改写为 `{anyOf:[prop, {type:"null"}]}`。
- 最后 `schema.required = propertyNames; schema.additionalProperties = false`。
- 根 schema 必须是 `type: "object"`。

`resolveJsonSchemaStrictSampling(tool, supportsStrictMode)`：

- 无 `constrainedSampling` 或不是 `json_schema` → `undefined`。
- `supportsStrictMode` 且转换成功 → `true`。
- 转换失败：`strict === "require"` → **抛错** `Tool "X" requires JSON-schema constrained sampling, but <原因>.`；否则 `undefined`（静默降级）。
- 不支持 strict 且 `strict === "require"` → 抛 `Tool "X" requires JSON-schema constrained sampling, but strict tools are unsupported.`

**grammar 约束采样**：仅 OpenAI completions/responses 且 `supportsOpenAIGrammarTools` 时启用。

```ts
resolveGrammarConstrainedSampling(tool, supportsOpenAIGrammarTools)
  → { format: "lark" | "regex", definition: string, inputProperty: string } | undefined
```

- 优先 `openai_lark`，其次 `openai_regex`；两者皆空则抛错。
- `inferGrammarInputProperty(tool)` 要求 `type: "object"` 且 `required` 恰好一个字符串属性、该属性 `type: "string"`，否则抛错。
- `getGrammarToolInput(toolName, args, inputProperty)`：从 arguments 取该字符串。
- `appendGrammarToolInputJsonDelta(buffer, inputProperty, nextInput, close)`：把单调增长的 grammar 输出**拼装成 JSON 片段**（首个 delta 是 `{"<prop>":"`，close 时补 `"}`），并校验单调性：
  - 已 closed 后再变 → 抛 `grammar tool input for property "X" changed after it was closed`
  - 非前缀增长 → 抛 `grammar tool input for property "X" changed non-monotonically`
- `createGrammarToolInputProperties(tools, supportsOpenAIGrammarTools): ReadonlyMap<string, string>` 供 convertMessages 用。

### 7.5 延迟工具（deferred tools）

`splitDeferredTools(context, enabled, normalizeName?)`：

- `enabled=false` → 全部 immediate。
- 收集历史中 assistant 消息里出现过的 toolCall 名字（`usedNames`），以及 toolResult 的 `addedToolNames`。
- `addedToolNames` 里**未在 usedNames 出现过**的名字 → deferred（Map 去重）；其余 immediate。

provider 侧对应：Anthropic `defer_loading` + 结果里的 `tool_reference` block；OpenAI Responses `defer_loading` + `additional_tools`/tool-search；OpenAI completions 只有 `deferredToolsMode: "kimi"` 特例（工具定义移到 system 消息里）。

---

## 8. Python 移植：必须复刻 vs 可以便宜行事

### 8.1 必须逐字复刻（外部可观察行为）

1. **事件名与字段名**：12 种 `type`（含下划线拼写 `toolcall_start` 而非 `tool_call_start`）、`contentIndex`、`delta`、`content`、`toolCall`、`partial`、`reason`、`message`、`error`。任何改名都会破坏与 session 文件/前端协议的兼容。
2. **`partial` 是共享可变对象**：所有事件携带同一个 `AssistantMessage` 实例，`content` block 原地增长。不要在每个事件里深拷贝。
3. **`stopReason` 字面量**：`"pending" | "stop" | "length" | "toolUse" | "error" | "aborted" | "deferred"`（注意 `toolUse` 是驼峰）。`done` 只能带 4 种，`error` 只能带 2 种。
4. **`calculateCost` 的数学**：分档阈值用 `input+cacheRead+cacheWrite`、严格大于、取最大命中档、1h 写入按 `2 × input` 单价。
5. **各 provider 的 usage 换算**：尤其 OpenAI 的 `input = prompt_tokens - cacheRead - cacheWrite`（下限 0）与 `output` 不减 reasoning。
6. **`transformMessages` 的三段行为**：图片占位符文案、跨模型 thinking/toolCall 处理、孤儿 toolResult 合成文案 `"No result provided"`、error/aborted assistant 整条跳过。这些直接影响能否重放历史。
7. **两个"必须抛错"的地方**：`streamSimple()` 在缺少 auth 时**同步抛异常**；`stream()` 返回流后一切错误编码进流。别再往里加一层捕获。
8. **`validateToolArguments` 的错误文案格式与 coercion 语义**（模型要靠这条消息自我修复）。
9. **`compat` 自动探测表**（`detectCompat`）与显式覆盖的 `??` 合并顺序——这是"同一份代码跑通 39 个 provider"的全部秘密。
10. **环境变量名、`auth.json` 的 `Record<providerId, Credential>` 形状与 `type` 判别值**，以及"stored credential 独占该 provider"的规则。
11. **cache_control / prompt_cache_key / prompt_cache_retention 的落点**（system、最后一个用户/助手/tool_result 文本块、最后一个工具；TTL `1h`/`24h`/`30m` 的触发条件）。写错会静默失去缓存，成本翻几倍。
12. **工具 call id 规范化规则**（Anthropic 64 字符白名单、OpenAI `|` 拆分与 40 字符上限、Mistral 9 字符）。
13. **`thinkingLevelMap` 的 `null` 语义与 `clampThinkingLevel` 的"就高"方向**。
14. **Anthropic 的 OAuth 特例**：`sk-ant-oat` 检测、强制 `"You are Claude Code..."` system 块、工具名 Claude Code 规范化。

### 8.2 可以按 Python 习惯改写

| TS 机制 | Python 建议 | 注意 |
|---|---|---|
| `AsyncIterable` + `for await` | `AsyncIterator` / `async for` / `AsyncGenerator` | 语义等价 |
| `EventStream<T,R>` 手写队列 | 用 `asyncio.Queue` + `asyncio.Future` 或自写同样的双栈队列 | 必须保证 `result()` 在首个 `done`/`error` 时 resolve，且 `push` 后终止会被丢弃 |
| `AbortSignal` | `asyncio.CancelledError` + `asyncio.Event`，或封装 `CancellationToken` | `AbortSignal.any` / `AbortSignal.timeout` 需自建组合器；注意 `throwIfAborted()` 的检查点 |
| `WeakMap` 校验器缓存 | `functools.lru_cache` / `weakref.WeakKeyDictionary` | schema 是 dict 不可弱引用，改用 `id()` 或序列化哈希做 key |
| TypeBox `TSchema` | `dict` + `jsonschema`，或 pydantic `TypeAdapter` | 需要保留"是否原生 TypeBox"的分支语义，Python 里等价于"是否原生 JSON Schema" |
| 动态 `import()` | `importlib.import_module` + `lru_cache` | 或直接改成模块级注册表（Python 启动开销低，见 §8.3） |
| `Promise` / `.then` | `asyncio.create_task` + `asyncio.Future` | `lazyStream` 的"同步返回 + 异步 setup"需要 `run_coroutine_threadsafe` 或显式 `Task` |
| `Date.now()` | `int(time.time() * 1000)` | 全库毫秒 |
| `structuredClone` | `copy.deepcopy` | 用于 `validateToolArguments` 与 store 读写 |
| `headers: Record<string,string\|null>` | `dict[str, Optional[str]]` | `None` 表示抑制默认头，不能丢语义 |
| `Record<string, unknown>` 的 `samplingParams` | `dict[str, Any]`，请求体最后 `update()` | 同上，顺序敏感 |

### 8.3 可以合理简化

- **延迟加载**：Python import 成本低且无 bundler/浏览器约束，`lazyApi` 可以退化成直接 import；但**保留 `lazyStream` 的语义**（同步返回流、setup 失败编码成 `error` 事件）很重要，因为 `Models.stream()` 依赖它。
- **`compat.ts` 整套 legacy API**：不必移植。它是迁移期兼容层，注释明说"deleted with the coding-agent ModelManager migration"。
- **`helpers.ts` 的 `lazyOAuth`**：可以直接返回实现对象。
- **`bun-oauth` / `registerBundledOAuthFlowLoaders` / `/proc/self/environ` 兜底**：Bun 特有，跳过。
- **`AssistantMessageFrameEncoder`**：只有在需要"边流边持久化 + 崩溃恢复"时才移植；如果 Python 侧直接攒完整 `AssistantMessage` 落盘，可以跳过。
- **`libsqlite`/`canvas` 之类的 devDependency 相关路径**：与核心无关。

### 8.4 真正困难的部分（按难度排序）

1. **compat 矩阵 + 11 种 `thinkingFormat`**。`openai-completions.ts` 有 1717 行，其中 `buildParams` 与 `detectCompat` 一半以上在伺候 vLLM / SGLang / llama.cpp / Qwen / Z.ai / DeepSeek / Together / Baseten / OpenRouter / 蚂蚁翎 的差异。**建议**：不要一开始就追平全部；先实现 `"openai"` 分支 + `supportsReasoningEffort/maxTokensField/supportsStore` 四五个开关，其余按报错逐个补。但 `detectCompat` 的判定谓词（provider 名 + baseUrl 子串）必须原样抄，否则自定义 baseUrl 会走错分支。
2. **Anthropic 的 thinking 三分支**（budget-based / adaptive / mid-conversation effort）+ beta header 组合 + `cache_control` 落点 + OAuth 隐身模式。信息密度最高，且都直接决定请求是否 400。
3. **约束采样**。`makeStrictJsonSchema` 的改写规则（全 required + null 联合 + `additionalProperties:false`）和 grammar 的 JSON 片段拼装（`appendGrammarToolInputJsonDelta` 的单调性校验）都必须手工实现；Python 没有 TypeBox 等价物。
4. **流式部分 JSON 解析**。依赖 `partial-json` + 自写的 `repairJson`（转义字符串内控制字符、非法转义前的反斜杠加倍）。Python 需要 `partial-json-parser` 之类库或自写容错解析器；工具参数在流式期间必须始终是"合法对象"，即使 JSON 被截断。
5. **prompt caching 的 provider 差异**。Anthropic 的 `cache_control` block 标记、OpenAI 的 `prompt_cache_key`/`prompt_cache_retention`/`prompt_cache_options`、OpenRouter 上 Anthropic 模型的 `cacheControlFormat: "anthropic"` 混用——同一份逻辑要落三种 wire 形态。
6. **Bedrock**。SigV4 签名、Smithy middleware 注入自定义 header、自定义 `fetch`、`bearerToken` 绕过签名、region/profile 解析、Converse API 与 Anthropic Messages 的字段差异。Python 建议直接用 `boto3` 的 `converse_stream` 并接受无法完全复刻 header 中间件行为。
7. **WebSocket transport**（`openai-codex-responses`）。`Transport = "sse" | "websocket" | "websocket-cached" | "auto"`，含缓存上下文复用与失败回退诊断。若 Python 侧只用 SSE，需要在 `compat` 里显式标注不支持并让上层知道能力缺失。
8. **deferred responses**（`fetchDeferred`/`cancelDeferred`/`DeferredHandle`）。涉及 provider 长轮询、`pollAfterMs`/`expiresAt` 调度、`perplexity`-式的 batch 语义。
9. **消息补全的边界情况**。`transformMessages` 的孤儿 toolResult 合成 + `requiresAssistantAfterToolResult` 的合成 assistant 消息（文案 `"I have processed the tool results."`）+ z.ai 的连续 toolResult 合并 —— 这些是"历史回放时偶发 400"的根因所在。
10. **Unicode 代理对清洗**。`sanitizeSurrogates` 删除未配对 surrogate（Python 3 的 `str` 本身允许 surrogate，JSON 序列化会炸），需要在**每一条**出站文本上调用（Anthropic 适配器在每个 text 上调用；OpenAI 亦然）。
11. **模型目录生成器**。39 个 provider × 按 api 分组的数据 + `.manifest.json` 哈希 + `--strict` 校验。Python 侧建议保留"JSON 数据包 + 生成器脚本"的结构，并复刻 `thinkingLevelMap` 的派生规则（models.dev / OpenRouter reasoning 元数据）。
12. **`auth.json` 的并发**。TS 侧用 `proper-lockfile` + 同步/异步两套加锁 + revision 缓存。Python 需要 `filelock` 或 `fcntl`，并决定是否保留"共享读取状态"优化。

### 8.5 移植优先级建议

```
P0  types.py（Message/Content/Usage/Model/Event/Context/Tool）
P0  event_stream.py（EventStream + AssistantMessageEventStream + result）
P0  adapters: anthropic-messages, openai-completions（openai 分支）, pi-messages（参考实现）
P0  models.py（Provider/Models/createProvider/calculateCost/clampThinkingLevel）
P0  auth（CredentialStore + resolve 顺序 + env_api_keys + auth.json 读写）
P0  validation.py（validateToolArguments 的完整 coercion 与错误文案）
P0  transform_messages.py
P1  openai-responses / google-generative-ai / mistral-conversations
P1  constrained_sampling.py（strict json schema + grammar）
P1  compat 矩阵补全（zai/deepseek/together/openrouter/qwen/baseten/...）
P2  bedrock / google-vertex / openai-codex（websocket）
P2  deferred responses、AssistantMessageFrameEncoder、images 子系统
P3  compat.py（legacy）、Bun 专用路径
```

---

## 附录 A：常量与魔数清单

| 常量 | 值 | 位置 |
|---|---|---|
| `CONTEXT_SAFETY_TOKENS` | `4096` | `api/simple-options.ts` |
| `MIN_MAX_TOKENS` | `1` | 同上 |
| `MIN_ANSWER_TOKENS` | `1024` | 同上 |
| `DEFAULT_THINKING_BUDGETS` | `{minimal:1024, low:2048, medium:8192, high:16384}` | 同上 |
| `CHARS_PER_TOKEN` | `4` | `utils/estimate.ts` |
| `ESTIMATED_IMAGE_CHARS` | `4800` | 同上 |
| `OPENAI_RESPONSES_MIN_OUTPUT_TOKENS` | `16` | `api/openai-responses.ts` |
| `MISTRAL_TOOL_CALL_ID_LENGTH` | `9` | `api/mistral-conversations.ts` |
| `MAX_MISTRAL_ERROR_BODY_CHARS` | `4000` | 同上 |
| `DEFAULT_OAUTH_MINIMUM_VALIDITY_MS` | `5*60*1000` | `auth/resolve.ts` |
| `DEFAULT_OAUTH_REFRESH_TIMEOUT_MS` | `15000` | 同上 |
| `maxRetryDelayMs` 默认 | `60000` | `types.ts` |
| Anthropic tool id 截断 | `64` | `api/anthropic-messages.ts` |
| OpenAI Chat tool id 截断 | `40` | `api/openai-completions.ts` |
| `claudeCodeVersion` | `"2.1.251"` | `api/anthropic-messages.ts` |
| `AUTH_FILE_WRITE_OPTIONS.mode` | `0o600` | coding-agent `auth-storage.ts` |
| OpenAI SDK 默认 timeout / maxRetries | `10min` / `2`（pi 内部用 `maxRetries: 0` 交给 `retryProviderRequest`） | 注释在 `types.ts` |

## 附录 B：`Model` 字段速查（Python dataclass 建议）

```python
@dataclass
class ModelCostRates:
    input: float          # $/million tokens
    output: float
    cacheRead: float
    cacheWrite: float

@dataclass
class ModelCostTier(ModelCostRates):
    inputTokensAbove: int

@dataclass
class ModelCost(ModelCostRates):
    tiers: list[ModelCostTier] | None = None

@dataclass
class Model:
    id: str
    name: str
    api: str
    provider: str
    baseUrl: str
    reasoning: bool
    input: list[Literal["text", "image"]]
    cost: ModelCost
    contextWindow: int
    maxTokens: int
    thinkingLevelMap: dict[str, str | None] | None = None
    samplingParams: dict[str, Any] | None = None
    headers: dict[str, str] | None = None
    compat: dict[str, Any] | None = None   # 按 api 解释
```

## 附录 C：关键文件行数（估算移植工作量）

```
types.ts                       881
models.ts                      957
api/openai-completions.ts     1717
api/openai-codex-responses.ts 1657
api/anthropic-messages.ts     1490
api/bedrock-converse-stream.ts 1325
api/mistral-conversations.ts   941
api/openai-responses-shared.ts 793
api/google-vertex.ts           598
api/google-generative-ai.ts    526
api/google-shared.ts           453
api/pi-messages.ts             441
api/openai-responses.ts        390
api/azure-openai-responses.ts  338
api/constrained-sampling.ts    277
api/transform-messages.ts      223
utils/validation.ts            350
utils/assistant-message-frame.ts 18KB
utils/overflow.ts / retry.ts / estimate.ts / json-parse.ts ...
```

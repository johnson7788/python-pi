/**
 * Single source of truth for the wire protocol.
 *
 * The backend is a Python WebSocket server (pi's RPC mode bridged to one socket
 * per tab). Field names were taken from the server implementation, not guessed:
 * `modes/rpc_mode.py`, `modes/json_event.py`, `messages.py` and
 * `extensions/rpc_ui.py`. Everything is camelCase, exactly like pi's RPC mode.
 *
 * One JSON object per WebSocket text frame, in both directions.
 */

/* -------------------------------------------------------------------------- */
/* Primitives                                                                  */
/* -------------------------------------------------------------------------- */

export interface Usage {
  input: number
  output: number
  cacheRead: number
  cacheWrite: number
  /** Present on usage_to_json from pi-ai/types.py. */
  cacheWrite1h?: number
  reasoning?: number
  totalTokens: number
  cost: CostBreakdown
}

export interface CostBreakdown {
  input?: number
  output?: number
  cacheRead?: number
  cacheWrite?: number
  total: number
}

export interface TextBlock {
  type: 'text'
  text: string
}

export interface ThinkingBlock {
  type: 'thinking'
  thinking: string
}

export interface ToolCallBlock {
  type: 'toolCall'
  id: string
  name: string
  /** Parsed JSON once the call is complete; may be a raw string mid-stream. */
  arguments?: unknown
  /**
   * Accumulated `toolcall_delta` text. The stream is delta-only, so the raw JSON
   * string is kept verbatim for pretty-printing while arguments are half-written
   * (i.e. not yet parseable).
   */
  argumentsRaw?: string
}

export interface ImageBlock {
  type: 'image'
  [key: string]: unknown
}

/** Blocks from a newer pi that this UI does not render specially. */
export interface UnknownBlock {
  type: string
  [key: string]: unknown
}

export type ContentBlock = TextBlock | ThinkingBlock | ToolCallBlock | ImageBlock | UnknownBlock

/**
 * Narrowing predicates.
 *
 * `UnknownBlock` carries an index signature, so a plain `block.type === 'text'`
 * check would not narrow the union (`.text` stays `unknown`). These predicates
 * assert the exact block type instead.
 */
export function isTextBlock(block: ContentBlock): block is TextBlock {
  return block.type === 'text'
}

export function isThinkingBlock(block: ContentBlock): block is ThinkingBlock {
  return block.type === 'thinking'
}

export function isToolCallBlock(block: ContentBlock): block is ToolCallBlock {
  return block.type === 'toolCall'
}

export interface Cost {
  input?: number
  output?: number
  cacheRead?: number
  cacheWrite?: number
  total?: number
  [key: string]: unknown
}

/* -------------------------------------------------------------------------- */
/* Messages                                                                    */
/* -------------------------------------------------------------------------- */

export interface UserMessage {
  role: 'user'
  content: string | ContentBlock[]
  timestamp?: number
}

export interface AssistantMessage {
  role: 'assistant'
  content: ContentBlock[]
  stopReason?: string
  usage?: Usage
  errorMessage?: string
  timestamp?: number
}

/** Content of a tool result: the server wraps it as `{content, details, terminate}`. */
export interface ToolResultPayload {
  content?: ContentBlock[]
  details?: unknown
  terminate?: boolean | null
  [key: string]: unknown
}

export interface ToolResultMessage {
  role: 'toolResult'
  toolCallId: string
  toolName?: string
  content: ContentBlock[]
  isError?: boolean
  details?: unknown
  timestamp?: number
}

export interface CustomMessage {
  role: 'custom'
  customType?: string
  content?: string | ContentBlock[]
  display?: boolean
  details?: unknown
  timestamp?: number
}

/** Session bookkeeping messages that show up in `get_messages` but are not chat. */
export interface SummaryMessage {
  role: 'branchSummary' | 'compactionSummary'
  summary: string
  fromId?: string
  tokensBefore?: number
  timestamp?: number
}

export interface BashExecutionMessage {
  role: 'bashExecution'
  command: string
  output?: string
  exitCode?: number
  cancelled?: boolean
  truncated?: boolean
  timestamp?: number
}

export type Message =
  | UserMessage
  | AssistantMessage
  | ToolResultMessage
  | CustomMessage
  | SummaryMessage
  | BashExecutionMessage

/* -------------------------------------------------------------------------- */
/* Client -> server commands                                                   */
/* -------------------------------------------------------------------------- */

export type StreamingBehavior = 'steer' | 'followUp'

export interface ImageAttachment {
  type?: string
  mimeType?: string
  /** base64 payload, path or url depending on how the server ingests images. */
  data?: string
  path?: string
  url?: string
  [key: string]: unknown
}

/** Every command carries an `id` so the reply can be correlated. */
interface CommandBase {
  id: string
}

export interface PromptCommand extends CommandBase {
  type: 'prompt'
  message: string
  images?: ImageAttachment[]
  /** Only meaningful while the agent is streaming. */
  streamingBehavior?: StreamingBehavior
}

export interface SteerCommand extends CommandBase {
  type: 'steer'
  message: string
}

export interface FollowUpCommand extends CommandBase {
  type: 'follow_up'
  message: string
}

export interface AbortCommand extends CommandBase {
  type: 'abort'
}

export interface ClearQueueCommand extends CommandBase {
  type: 'clear_queue'
}

export interface NewSessionCommand extends CommandBase {
  type: 'new_session'
}

export interface SwitchSessionCommand extends CommandBase {
  type: 'switch_session'
  sessionPath: string
}

export interface ForkCommand extends CommandBase {
  type: 'fork'
  entryId: string
}

export interface CloneCommand extends CommandBase {
  type: 'clone'
}

export interface GetStateCommand extends CommandBase {
  type: 'get_state'
}

export interface GetMessagesCommand extends CommandBase {
  type: 'get_messages'
}

export interface GetSessionStatsCommand extends CommandBase {
  type: 'get_session_stats'
}

export interface GetForkMessagesCommand extends CommandBase {
  type: 'get_fork_messages'
}

export interface GetCommandsCommand extends CommandBase {
  type: 'get_commands'
}

export interface GetAvailableModelsCommand extends CommandBase {
  type: 'get_available_models'
}

export interface SetModelCommand extends CommandBase {
  type: 'set_model'
  provider: string
  modelId: string
}

export interface CycleModelCommand extends CommandBase {
  type: 'cycle_model'
}

export interface SetThinkingLevelCommand extends CommandBase {
  type: 'set_thinking_level'
  level: string
}

export interface GetAvailableThinkingLevelsCommand extends CommandBase {
  type: 'get_available_thinking_levels'
}

export interface CompactCommand extends CommandBase {
  type: 'compact'
  customInstructions?: string
}

export interface SetAutoCompactionCommand extends CommandBase {
  type: 'set_auto_compaction'
  enabled: boolean
}

export interface SetSessionNameCommand extends CommandBase {
  type: 'set_session_name'
  name: string
}

/**
 * Answer to a blocking `extension_ui_request`.
 *
 * Note: `id` here is the *dialog* id from the request, not a fresh correlation
 * id. This is the only client frame whose `id` is not client-generated.
 */
export interface ExtensionUiResponse {
  type: 'extension_ui_response'
  id: string
  value?: string
  confirmed?: boolean
  cancelled?: true
}

/** Commands generated by the UI. `id` is added by the transport. */
export type ClientCommand =
  | PromptCommand
  | SteerCommand
  | FollowUpCommand
  | AbortCommand
  | ClearQueueCommand
  | NewSessionCommand
  | SwitchSessionCommand
  | ForkCommand
  | CloneCommand
  | GetStateCommand
  | GetMessagesCommand
  | GetSessionStatsCommand
  | GetForkMessagesCommand
  | GetCommandsCommand
  | GetAvailableModelsCommand
  | SetModelCommand
  | CycleModelCommand
  | SetThinkingLevelCommand
  | GetAvailableThinkingLevelsCommand
  | CompactCommand
  | SetAutoCompactionCommand
  | SetSessionNameCommand

/**
 * `Omit` collapses a union to its common keys, so it must be distributed over
 * each member first — otherwise `send({type:'prompt', message})` loses `message`.
 */
type DistributiveOmit<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never

export type ClientCommandInput = DistributiveOmit<ClientCommand, 'id'>

/* -------------------------------------------------------------------------- */
/* Server -> client                                                            */
/* -------------------------------------------------------------------------- */

/** Reply to a command. `prompt` replies at preflight, not on completion. */
export interface ResponseMessage {
  type: 'response'
  id?: string
  command: string
  success: boolean
  data?: unknown
  error?: string
}

export interface MessageStartEvent {
  type: 'message_start'
  message: Message
}

/**
 * Delta-only streaming update: there is no cumulative `message` and no `partial`.
 * `usage` is cumulative and may be null.
 */
export interface MessageUpdateEvent {
  type: 'message_update'
  usage?: Usage | null
  assistantMessageEvent: AssistantMessageEvent
}

export type AssistantMessageEventType =
  | 'text_start'
  | 'text_delta'
  | 'text_end'
  | 'thinking_start'
  | 'thinking_delta'
  | 'thinking_end'
  | 'toolcall_start'
  | 'toolcall_delta'
  | 'toolcall_end'

export interface AssistantMessageEvent {
  type: AssistantMessageEventType
  /** Correlates deltas with the content block they belong to. */
  contentIndex?: number
  /** `*_delta` only. */
  delta?: string
  /** `text_end` / `thinking_end` only. */
  content?: string
  /** `toolcall_start` only, because the cumulative partial was dropped. */
  id?: string
  /** `toolcall_start` only. */
  toolName?: string
  /**
   * `toolcall_end` only. The task description called this `content`, but the
   * server actually emits a `toolCall` object here — both are accepted.
   */
  toolCall?: ToolCallBlock
}

export interface MessageEndEvent {
  type: 'message_end'
  message: Message
}

export interface AgentStartEvent {
  type: 'agent_start'
}

export interface TurnStartEvent {
  type: 'turn_start'
}

export interface AgentEndEvent {
  type: 'agent_end'
  messages?: Message[]
  willRetry?: boolean
}

export interface TurnEndEvent {
  type: 'turn_end'
  message?: Message
  toolResults?: Message[]
}

export interface ToolExecutionStartEvent {
  type: 'tool_execution_start'
  toolCallId: string
  toolName: string
  args?: unknown
}

export interface ToolExecutionUpdateEvent {
  type: 'tool_execution_update'
  toolCallId: string
  toolName: string
  args?: unknown
  partialResult?: ToolResultPayload
}

export interface ToolExecutionEndEvent {
  type: 'tool_execution_end'
  toolCallId: string
  toolName: string
  result?: ToolResultPayload
  isError?: boolean
}

export interface ExtensionErrorEvent {
  type: 'extension_error'
  extensionPath?: string
  event?: string
  error?: string
}

/**
 * Emitted by the Python server right after the socket opens (it is not part of
 * pi's own RPC vocabulary). `warnings` are startup problems that should be shown
 * to the user rather than swallowed.
 */
export interface ReadyEvent {
  type: 'ready'
  version?: string
  cwd?: string
  warnings?: string[]
}

/* -- extension UI ---------------------------------------------------------- */

export type ExtensionUiMethod =
  | 'select'
  | 'confirm'
  | 'input'
  | 'editor'
  | 'notify'
  | 'setStatus'
  | 'setWidget'
  | 'setTitle'
  | 'set_editor_text'

export interface ExtensionUiRequest {
  type: 'extension_ui_request'
  id: string
  method: ExtensionUiMethod
  /** select / confirm / input / editor, and setTitle */
  title?: string
  /** confirm */
  message?: string
  /** select */
  options?: string[]
  /** input */
  placeholder?: string
  /** editor */
  prefill?: string
  /** The blocking dialogs carry a millisecond timeout... */
  timeout?: number
  /** notify */
  notifyType?: string
  /** setStatus */
  statusKey?: string
  /** null clears the status (a missing key means "not mentioned"). */
  statusText?: string | null
  /** setWidget */
  widgetKey?: string
  widgetLines?: string[] | null
  widgetPlacement?: string
  /** set_editor_text */
  text?: string
  /** Older/newer servers may add fields; unknown ones are ignored. */
  [key: string]: unknown
}

export type ServerEvent =
  | ResponseMessage
  | MessageStartEvent
  | MessageUpdateEvent
  | MessageEndEvent
  | AgentStartEvent
  | TurnStartEvent
  | AgentEndEvent
  | TurnEndEvent
  | ToolExecutionStartEvent
  | ToolExecutionUpdateEvent
  | ToolExecutionEndEvent
  | ExtensionUiRequest
  | ExtensionErrorEvent
  | ReadyEvent

/* -------------------------------------------------------------------------- */
/* REST payloads                                                               */
/* -------------------------------------------------------------------------- */

export interface HealthResponse {
  status: string
  version: string
}

export interface ModelInfo {
  id: string
  name: string
  api?: string
  provider: string
  baseUrl?: string
  reasoning?: boolean
  input?: string[]
  cost?: Cost
  contextWindow?: number
  maxTokens?: number
  thinkingLevelMap?: Record<string, string | null>
  [key: string]: unknown
}

export interface ModelsResponse {
  models: ModelInfo[]
}

export interface SessionInfo {
  path: string
  id: string
  cwd?: string
  name?: string | null
  /**
   * Epoch millis in pi's session listing, but the Python server serialises an
   * ISO-8601 string, so both are accepted.
   */
  modified?: number | string
  messageCount?: number
  firstMessage?: string | null
}

export interface SessionsResponse {
  sessions: SessionInfo[]
}

/** `get_state` data, and the body of `GET /api/state`. */
export interface AgentState {
  model?: ModelInfo | null
  thinkingLevel?: string
  isStreaming?: boolean
  isCompacting?: boolean
  steeringMode?: string
  followUpMode?: string
  sessionFile?: string
  sessionId?: string
  sessionName?: string
  autoCompactionEnabled?: boolean
  messageCount?: number
  pendingMessageCount?: number
  [key: string]: unknown
}

export interface SessionStats {
  sessionFile?: string
  sessionId?: string
  userMessages?: number
  assistantMessages?: number
  toolCalls?: number
  toolResults?: number
  totalMessages?: number
  tokens?: {
    input?: number
    output?: number
    cacheRead?: number
    cacheWrite?: number
    total?: number
  }
  cost?: number
  [key: string]: unknown
}

export interface CommandInfo {
  name: string
  description?: string | null
  source?: 'prompt' | 'skill' | 'extension' | string
  sourceInfo?: {
    path?: string
    source?: string
    scope?: string
    origin?: string
  }
}

export interface CommandsResponse {
  commands: CommandInfo[]
}

export interface MessagesResponse {
  messages: Message[]
}

export interface ThinkingLevelsResponse {
  levels: string[]
}

export interface ForkMessageInfo {
  entryId: string
  text: string
}

export interface ForkMessagesResponse {
  messages: ForkMessageInfo[]
}

/* -------------------------------------------------------------------------- */
/* Narrowing helpers                                                           */
/* -------------------------------------------------------------------------- */

export function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : {}
}

/** Extract plain text from a message's content without touching Markdown. */
export function contentToText(content: string | ContentBlock[] | undefined): string {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  const parts: string[] = []
  for (const block of content) {
    if (isTextBlock(block)) parts.push(block.text)
  }
  return parts.join('\n')
}

export function isUserMessage(message: Message): message is UserMessage {
  return message.role === 'user'
}

export function isAssistantMessage(message: Message): message is AssistantMessage {
  return message.role === 'assistant'
}

export function isToolResultMessage(message: Message): message is ToolResultMessage {
  return message.role === 'toolResult'
}

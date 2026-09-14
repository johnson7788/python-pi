import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react'
import type {
  AgentState,
  AssistantMessage,
  AssistantMessageEvent,
  ClientCommandInput,
  CommandInfo,
  ContentBlock,
  ExtensionUiRequest,
  ExtensionUiResponse,
  ForkMessageInfo,
  Message,
  ModelInfo,
  ServerEvent,
  SessionInfo,
  SessionStats,
  StreamingBehavior,
  ToolCallBlock,
  ToolResultPayload,
} from './protocol'
import { asRecord, isAssistantMessage, isTextBlock, isToolCallBlock } from './protocol'

/* -------------------------------------------------------------------------- */
/* Public view-model types                                                     */
/* -------------------------------------------------------------------------- */

export type ConnectionStatus = 'connecting' | 'open' | 'reconnecting'

export interface ToolExecution {
  toolCallId: string
  toolName: string
  args: unknown
  status: 'running' | 'done' | 'error'
  result?: ToolResultPayload
  partialResult?: ToolResultPayload
  isError: boolean
  startedAt: number
}

export interface TranscriptEntry {
  key: string
  message: Message
  /** True while this assistant message is still receiving deltas. */
  streaming: boolean
}

export type DialogMethod = 'select' | 'confirm' | 'input' | 'editor'

export interface ExtensionDialog {
  id: string
  method: DialogMethod
  title: string
  message?: string
  options?: string[]
  placeholder?: string
  prefill?: string
  /** Milliseconds; the server cancels the dialog itself once this elapses. */
  timeoutMs?: number
}

export interface Toast {
  id: string
  level: string
  message: string
}

export interface InlineError {
  id: string
  source: string
  message: string
}

interface AgentStore {
  connection: ConnectionStatus
  /** Gates the disconnect banner: no banner before the first successful connect. */
  hasConnected: boolean
  serverVersion: string | null
  transcript: TranscriptEntry[]
  toolExecutions: Record<string, ToolExecution>
  /** Entry key currently receiving `message_update` deltas. */
  streamingKey: string | null
  /** Optimistic user entries awaiting their `message_start` echo. */
  pendingUser: { key: string; text: string }[]
  agentRunning: boolean
  state: AgentState | null
  sessions: SessionInfo[]
  sessionsError: string | null
  models: ModelInfo[]
  thinkingLevels: string[]
  commands: CommandInfo[]
  forkMessages: ForkMessageInfo[]
  stats: SessionStats | null
  dialogs: ExtensionDialog[]
  toasts: Toast[]
  errors: InlineError[]
  statusTexts: Record<string, string>
  widgets: Record<string, string[]>
  editorText: string | null
  seq: number
}

const INITIAL_STORE: AgentStore = {
  connection: 'connecting',
  hasConnected: false,
  serverVersion: null,
  transcript: [],
  toolExecutions: {},
  streamingKey: null,
  pendingUser: [],
  agentRunning: false,
  state: null,
  sessions: [],
  sessionsError: null,
  models: [],
  thinkingLevels: [],
  commands: [],
  forkMessages: [],
  stats: null,
  dialogs: [],
  toasts: [],
  errors: [],
  statusTexts: {},
  widgets: {},
  editorText: null,
  seq: 0,
}

/* -------------------------------------------------------------------------- */
/* Reducer actions                                                             */
/* -------------------------------------------------------------------------- */

type Action =
  | { type: 'connection'; status: ConnectionStatus; hasConnected?: boolean }
  | { type: 'server_version'; version: string }
  | { type: 'server_event'; event: ServerEvent }
  | { type: 'sessions'; sessions: SessionInfo[] }
  | { type: 'sessions_error'; error: string }
  | { type: 'optimistic_user'; key: string; text: string }
  | { type: 'clear_editor_text' }
  | { type: 'dismiss_toast'; id: string }
  | { type: 'dismiss_error'; id: string }
  | { type: 'dismiss_dialog'; id: string }
  | { type: 'local_error'; id: string; source: string; message: string }
  | { type: 'reset_session' }
  | { type: 'optimistic_model'; model: ModelInfo }
  | { type: 'optimistic_thinking'; level: string }

/* -------------------------------------------------------------------------- */
/* Small helpers                                                               */
/* -------------------------------------------------------------------------- */

/** Plain text of a message, whichever shape its `content` arrived in. */
export function messageText(message: unknown): string {
  const content = asRecord(message).content
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content
    .map((block) => (isTextBlock(block) ? block.text : ''))
    .join('')
}

/** Turn a tool result payload (from an execution event or a message) into text. */
export function toolResultText(payload: ToolResultPayload | undefined | null): string {
  if (!payload) return ''
  const content = payload.content
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  const parts: string[] = []
  for (const block of content) {
    if (isTextBlock(block)) parts.push(block.text)
    else {
      const loose = asRecord(block)
      const text = loose.text ?? loose.content
      if (typeof text === 'string') parts.push(text)
    }
  }
  return parts.join('\n')
}

function resultFromMessage(message: Message): ToolResultPayload {
  const loose = asRecord(message)
  return {
    content: (loose.content as ContentBlock[] | undefined) ?? [],
    details: loose.details,
  }
}

/**
 * Grow a content array to `index` and return a copy with the block replaced.
 * `contentIndex` is the only correlation the delta stream offers, so a delta for
 * an index we have not seen yet must still land in the right slot.
 */
function withBlockAt(
  content: ContentBlock[],
  index: number,
  make: (existing: ContentBlock | undefined) => ContentBlock,
): ContentBlock[] {
  const next = content.slice()
  while (next.length <= index) next.push({ type: 'text', text: '' })
  next[index] = make(next[index])
  return next
}

function indexFor(content: ContentBlock[], requested: number | undefined, fallbackType: ContentBlock['type']): number {
  if (typeof requested === 'number' && requested >= 0) return requested
  for (let i = content.length - 1; i >= 0; i -= 1) {
    if (content[i].type === fallbackType) return i
  }
  return Math.max(0, content.length - 1)
}

/** Apply one delta-only assistant event to the in-progress assistant message. */
function applyAssistantEvent(
  message: AssistantMessage,
  event: AssistantMessageEvent,
): AssistantMessage {
  const content = Array.isArray(message.content) ? message.content : []
  switch (event.type) {
    case 'text_start': {
      const index = indexFor(content, event.contentIndex, 'text')
      return { ...message, content: withBlockAt(content, index, () => ({ type: 'text', text: '' })) }
    }
    case 'text_delta': {
      const index = indexFor(content, event.contentIndex, 'text')
      return {
        ...message,
        content: withBlockAt(content, index, (existing) => ({
          type: 'text',
          text: (existing?.type === 'text' ? existing.text : '') + (event.delta ?? ''),
        })),
      }
    }
    case 'text_end': {
      const index = indexFor(content, event.contentIndex, 'text')
      return {
        ...message,
        content: withBlockAt(content, index, (existing) => ({
          type: 'text',
          text: event.content ?? (existing?.type === 'text' ? existing.text : ''),
        })),
      }
    }
    case 'thinking_start': {
      const index = indexFor(content, event.contentIndex, 'thinking')
      return {
        ...message,
        content: withBlockAt(content, index, () => ({ type: 'thinking', thinking: '' })),
      }
    }
    case 'thinking_delta': {
      const index = indexFor(content, event.contentIndex, 'thinking')
      return {
        ...message,
        content: withBlockAt(content, index, (existing) => ({
          type: 'thinking',
          thinking: (existing?.type === 'thinking' ? existing.thinking : '') + (event.delta ?? ''),
        })),
      }
    }
    case 'thinking_end': {
      const index = indexFor(content, event.contentIndex, 'thinking')
      return {
        ...message,
        content: withBlockAt(content, index, (existing) => ({
          type: 'thinking',
          thinking: event.content ?? (existing?.type === 'thinking' ? existing.thinking : ''),
        })),
      }
    }
    case 'toolcall_start': {
      const index = indexFor(content, event.contentIndex, 'toolCall')
      return {
        ...message,
        content: withBlockAt(content, index, () => ({
          type: 'toolCall',
          id: event.id ?? '',
          name: event.toolName ?? '',
          argumentsRaw: '',
        })),
      }
    }
    case 'toolcall_delta': {
      const index = indexFor(content, event.contentIndex, 'toolCall')
      return {
        ...message,
        content: withBlockAt(content, index, (existing) => {
          const base: ToolCallBlock =
            existing && isToolCallBlock(existing)
              ? existing
              : { type: 'toolCall', id: '', name: '', argumentsRaw: '' }
          return { ...base, argumentsRaw: (base.argumentsRaw ?? '') + (event.delta ?? '') }
        }),
      }
    }
    case 'toolcall_end': {
      const index = indexFor(content, event.contentIndex, 'toolCall')
      return {
        ...message,
        content: withBlockAt(content, index, (existing) => {
          const base: ToolCallBlock =
            existing && isToolCallBlock(existing)
              ? existing
              : { type: 'toolCall', id: '', name: '', argumentsRaw: '' }
          // The reference server sends `toolCall`; be liberal and also accept the
          // `content` field the prose spec described.
          if (event.toolCall) return { ...base, ...event.toolCall, argumentsRaw: undefined }
          if (typeof event.content === 'string') {
            try {
              return { ...base, arguments: JSON.parse(event.content), argumentsRaw: undefined }
            } catch {
              return { ...base, argumentsRaw: event.content }
            }
          }
          return base
        }),
      }
    }
  }
}

function addOrphanToolExecution(
  tools: Record<string, ToolExecution>,
  message: Message,
): Record<string, ToolExecution> {
  if (message.role !== 'toolResult') return tools
  const existing = tools[message.toolCallId]
  return {
    ...tools,
    [message.toolCallId]: {
      toolCallId: message.toolCallId,
      toolName: message.toolName ?? existing?.toolName ?? 'tool',
      args: existing?.args,
      status: message.isError ? 'error' : 'done',
      result: resultFromMessage(message),
      partialResult: existing?.partialResult,
      isError: Boolean(message.isError),
      startedAt: existing?.startedAt ?? Date.now(),
    },
  }
}

/** Replace the whole transcript from an authoritative message list. */
function transcriptFromMessages(state: AgentStore, messages: Message[]): AgentStore {
  const transcript: TranscriptEntry[] = []
  let tools: Record<string, ToolExecution> = {}
  let seq = state.seq
  for (const message of messages) {
    seq += 1
    transcript.push({ key: `m${seq}`, message, streaming: false })
    // Tool results mirror `tool_execution_*` events that may not have been seen
    // (for example after a reload), so they always top up the execution map.
    if (message.role === 'toolResult') tools = addOrphanToolExecution(tools, message)
  }
  return { ...state, transcript, toolExecutions: tools, streamingKey: null, seq }
}

function pushError(state: AgentStore, source: string, message: string): AgentStore {
  return {
    ...state,
    errors: [...state.errors, { id: `e${state.seq}-${state.errors.length}`, source, message }].slice(-20),
  }
}

/* -------------------------------------------------------------------------- */
/* Reducer                                                                     */
/* -------------------------------------------------------------------------- */

function reduceServerEvent(state: AgentStore, event: ServerEvent): AgentStore {
  const loose = asRecord(event)
  switch (loose.type) {
    case 'response':
      return reduceResponse(state, event as Extract<ServerEvent, { type: 'response' }>)

    case 'agent_start':
      return { ...state, agentRunning: true }

    case 'turn_start':
      return { ...state, agentRunning: true }

    case 'message_start': {
      const message = (event as Extract<ServerEvent, { type: 'message_start' }>).message
      if (!message || typeof message.role !== 'string') return state
      const seq = state.seq + 1
      const key = `m${seq}`
      // Every role stays "streaming" until its own `message_end` arrives. The
      // server emits a start/end pair for *each* message (the user prompt, tool
      // results, injected custom messages — not just the assistant), and
      // `message_end` locates its entry by looking for one that is still
      // streaming. Marking only the assistant here made every other role append a
      // second copy of itself at `message_end`.
      const entry: TranscriptEntry = { key, message, streaming: true }

      let pendingUser = state.pendingUser
      let transcript = state.transcript
      if (message.role === 'user' && pendingUser.length > 0) {
        // Reconcile the optimistic echo with the server's authoritative message.
        //
        // Match by *text* rather than by position: with two sends in flight, blindly
        // dropping the oldest pending entry would drop it twice and let the newer one
        // leak through, rendering the same message twice. Text is the only identifier
        // available — the server sends `content` as a block array while our optimistic
        // entry holds a plain string, so both go through `messageText`.
        const incoming = messageText(message)
        const index = Math.max(
          0,
          pendingUser.findIndex((item) => item.text === incoming),
        )
        const drop = pendingUser[index]
        pendingUser = pendingUser.filter((_, position) => position !== index)
        transcript = transcript.filter((item) => item.key !== drop.key)
      }

      return {
        ...state,
        seq,
        transcript: [...transcript, entry],
        pendingUser,
        streamingKey: message.role === 'assistant' ? key : state.streamingKey,
      }
    }

    case 'message_update': {
      const update = event as Extract<ServerEvent, { type: 'message_update' }>
      const assistantEvent = update.assistantMessageEvent
      if (!assistantEvent) return state

      let seq = state.seq
      let key = state.streamingKey
      let transcript = state.transcript

      if (key === null || !transcript.some((item) => item.key === key)) {
        // No message_start was seen (or it was pruned): start one implicitly so
        // the deltas have somewhere to land.
        seq += 1
        key = `m${seq}`
        transcript = [
          ...transcript,
          { key, message: { role: 'assistant', content: [] }, streaming: true },
        ]
      }

      transcript = transcript.map((item) => {
        if (item.key !== key) return item
        const message = isAssistantMessage(item.message)
          ? item.message
          : ({ role: 'assistant', content: [] } as AssistantMessage)
        const updated = applyAssistantEvent(message, assistantEvent)
        return {
          ...item,
          streaming: true,
          message: update.usage ? { ...updated, usage: update.usage } : updated,
        }
      })

      return { ...state, seq, transcript, streamingKey: key }
    }

    case 'message_end': {
      const message = (event as Extract<ServerEvent, { type: 'message_end' }>).message
      if (!message || typeof message.role !== 'string') return state

      let transcript = state.transcript
      let streamingKey = state.streamingKey
      let tools = state.toolExecutions

      if (message.role === 'toolResult') tools = addOrphanToolExecution(tools, message)

      // The streaming assistant entry is the one being replaced; other roles append.
      let target: string | null = null
      if (message.role === 'assistant') {
        if (streamingKey && transcript.some((item) => item.key === streamingKey)) {
          target = streamingKey
        } else {
          for (let i = transcript.length - 1; i >= 0; i -= 1) {
            if (transcript[i].message.role === 'assistant' && transcript[i].streaming) {
              target = transcript[i].key
              break
            }
          }
        }
      } else {
        // Match the most recent same-role entry that has not been finalised.
        for (let i = transcript.length - 1; i >= 0; i -= 1) {
          const item = transcript[i]
          if (item.message.role === message.role && item.streaming) {
            target = item.key
            break
          }
        }
      }

      if (target) {
        transcript = transcript.map((item) =>
          item.key === target ? { ...item, message, streaming: false } : item,
        )
      } else if (message.role !== 'toolResult') {
        const seq = state.seq + 1
        transcript = [...transcript, { key: `m${seq}`, message, streaming: false }]
        return {
          ...state,
          seq,
          transcript,
          toolExecutions: tools,
          streamingKey: message.role === 'assistant' ? null : streamingKey,
        }
      }

      return {
        ...state,
        transcript,
        toolExecutions: tools,
        streamingKey: message.role === 'assistant' ? null : streamingKey,
      }
    }

    case 'turn_end': {
      const turnEnd = event as Extract<ServerEvent, { type: 'turn_end' }>
      let tools = state.toolExecutions
      for (const result of turnEnd.toolResults ?? []) {
        tools = addOrphanToolExecution(tools, result)
      }
      return { ...state, toolExecutions: tools }
    }

    case 'agent_end': {
      // A tool whose execution never reported an end (abort, crash) must not spin
      // forever; agent_end is the last point at which it can be closed out.
      const tools: Record<string, ToolExecution> = {}
      for (const [id, execution] of Object.entries(state.toolExecutions)) {
        tools[id] = execution.status === 'running' ? { ...execution, status: 'done' } : execution
      }
      return { ...state, agentRunning: false, streamingKey: null, toolExecutions: tools }
    }

    case 'tool_execution_start': {
      const start = event as Extract<ServerEvent, { type: 'tool_execution_start' }>
      return {
        ...state,
        toolExecutions: {
          ...state.toolExecutions,
          [start.toolCallId]: {
            toolCallId: start.toolCallId,
            toolName: start.toolName,
            args: start.args,
            status: 'running',
            isError: false,
            startedAt: Date.now(),
          },
        },
      }
    }

    case 'tool_execution_update': {
      const update = event as Extract<ServerEvent, { type: 'tool_execution_update' }>
      const existing = state.toolExecutions[update.toolCallId]
      return {
        ...state,
        toolExecutions: {
          ...state.toolExecutions,
          [update.toolCallId]: {
            toolCallId: update.toolCallId,
            toolName: update.toolName || existing?.toolName || 'tool',
            args: update.args ?? existing?.args,
            status: existing?.status === 'done' || existing?.status === 'error' ? existing.status : 'running',
            result: existing?.result,
            partialResult: update.partialResult,
            isError: existing?.isError ?? false,
            startedAt: existing?.startedAt ?? Date.now(),
          },
        },
      }
    }

    case 'tool_execution_end': {
      const end = event as Extract<ServerEvent, { type: 'tool_execution_end' }>
      const existing = state.toolExecutions[end.toolCallId]
      return {
        ...state,
        toolExecutions: {
          ...state.toolExecutions,
          [end.toolCallId]: {
            toolCallId: end.toolCallId,
            toolName: end.toolName || existing?.toolName || 'tool',
            args: existing?.args,
            status: end.isError ? 'error' : 'done',
            result: end.result ?? existing?.result,
            partialResult: existing?.partialResult,
            isError: Boolean(end.isError),
            startedAt: existing?.startedAt ?? Date.now(),
          },
        },
      }
    }

    case 'extension_ui_request':
      return reduceExtensionRequest(state, event as ExtensionUiRequest)

    case 'extension_error': {
      const failure = event as Extract<ServerEvent, { type: 'extension_error' }>
      const where = failure.extensionPath ?? '扩展'
      const detail = failure.error ?? failure.event ?? '未知错误'
      return pushError(state, '扩展错误', `${where}: ${detail}`)
    }

    case 'ready': {
      // The Python server announces itself (and any startup warnings) before the
      // client asks anything; warnings are surfaced, never fatal.
      const ready = event as Extract<ServerEvent, { type: 'ready' }>
      let next = ready.version ? { ...state, serverVersion: ready.version } : state
      for (const warning of ready.warnings ?? []) {
        next = pushError(next, '服务器', warning)
      }
      return next
    }

    default:
      // Unknown event types are ignored on purpose: the server may ship a newer
      // protocol and an unknown frame must never break rendering.
      return state
  }
}

function reduceResponse(
  state: AgentStore,
  response: Extract<ServerEvent, { type: 'response' }>,
): AgentStore {
  if (!response.success) {
    return pushError(state, response.command, response.error ?? '请求失败')
  }
  const data = asRecord(response.data)
  switch (response.command) {
    case 'get_state':
      return { ...state, state: response.data as AgentState }
    case 'get_messages': {
      const messages = Array.isArray(data.messages) ? (data.messages as Message[]) : []
      return transcriptFromMessages(state, messages)
    }
    case 'get_session_stats':
      return { ...state, stats: response.data as SessionStats }
    case 'get_available_models': {
      const models = Array.isArray(data.models) ? (data.models as ModelInfo[]) : []
      return { ...state, models }
    }
    case 'get_available_thinking_levels': {
      const levels = Array.isArray(data.levels) ? (data.levels as string[]) : []
      return { ...state, thinkingLevels: levels }
    }
    case 'get_commands': {
      const commands = Array.isArray(data.commands) ? (data.commands as CommandInfo[]) : []
      return { ...state, commands }
    }
    case 'get_fork_messages': {
      const messages = Array.isArray(data.messages) ? (data.messages as ForkMessageInfo[]) : []
      return { ...state, forkMessages: messages }
    }
    case 'set_model':
      // The reply is the model snapshot; using it avoids a state round-trip.
      return { ...state, state: { ...(state.state ?? {}), model: response.data as ModelInfo } }
    case 'cycle_model': {
      const model = data.model as ModelInfo | undefined
      if (!model) return state
      const level = typeof data.thinkingLevel === 'string' ? data.thinkingLevel : state.state?.thinkingLevel
      return { ...state, state: { ...(state.state ?? {}), model, thinkingLevel: level } }
    }
    case 'set_thinking_level': {
      const level = data.level
      return typeof level === 'string'
        ? { ...state, state: { ...(state.state ?? {}), thinkingLevel: level } }
        : state
    }
    case 'new_session':
    case 'switch_session':
    case 'clone':
      // Session identity changed; the caller re-fetches state and messages.
      return { ...state, transcript: [], toolExecutions: {}, streamingKey: null, stats: null }
    default:
      return state
  }
}

function reduceExtensionRequest(state: AgentStore, request: ExtensionUiRequest): AgentStore {
  switch (request.method) {
    case 'select':
    case 'confirm':
    case 'input':
    case 'editor': {
      const dialog: ExtensionDialog = {
        id: request.id,
        method: request.method,
        title: request.title ?? '',
        message: request.message,
        options: request.options,
        placeholder: request.placeholder,
        prefill: request.prefill,
        timeoutMs: typeof request.timeout === 'number' ? request.timeout : undefined,
      }
      return { ...state, dialogs: [...state.dialogs, dialog] }
    }
    case 'notify': {
      const level = typeof request.notifyType === 'string' ? request.notifyType : 'info'
      const toast: Toast = {
        id: request.id,
        level,
        message: typeof request.message === 'string' ? request.message : String(request.title ?? ''),
      }
      return { ...state, toasts: [...state.toasts, toast].slice(-6) }
    }
    case 'setStatus': {
      const key = request.statusKey ?? 'status'
      const statusTexts = { ...state.statusTexts }
      if (request.statusText === null || request.statusText === undefined) delete statusTexts[key]
      else statusTexts[key] = request.statusText
      return { ...state, statusTexts }
    }
    case 'setWidget': {
      const key = request.widgetKey ?? 'widget'
      const widgets = { ...state.widgets }
      if (request.widgetLines === null || request.widgetLines === undefined) delete widgets[key]
      else widgets[key] = request.widgetLines
      return { ...state, widgets }
    }
    case 'setTitle':
      // Handled as a side effect in the socket handler; the reducer stays pure.
      return state
    case 'set_editor_text':
      return { ...state, editorText: typeof request.text === 'string' ? request.text : '' }
    default:
      return state
  }
}

function reducer(state: AgentStore, action: Action): AgentStore {
  switch (action.type) {
    case 'connection':
      return {
        ...state,
        connection: action.status,
        hasConnected: action.hasConnected ?? (action.status === 'open' ? true : state.hasConnected),
      }
    case 'server_version':
      return { ...state, serverVersion: action.version }
    case 'server_event':
      return reduceServerEvent(state, action.event)
    case 'sessions':
      return { ...state, sessions: action.sessions, sessionsError: null }
    case 'sessions_error':
      return { ...state, sessionsError: action.error }
    case 'optimistic_user': {
      const seq = state.seq + 1
      const entry: TranscriptEntry = {
        key: action.key,
        message: { role: 'user', content: action.text, timestamp: Date.now() },
        streaming: false,
      }
      return {
        ...state,
        seq,
        transcript: [...state.transcript, entry],
        pendingUser: [...state.pendingUser, { key: action.key, text: action.text }],
      }
    }
    case 'clear_editor_text':
      return { ...state, editorText: null }
    case 'dismiss_toast':
      return { ...state, toasts: state.toasts.filter((toast) => toast.id !== action.id) }
    case 'dismiss_error':
      return { ...state, errors: state.errors.filter((error) => error.id !== action.id) }
    case 'dismiss_dialog':
      return { ...state, dialogs: state.dialogs.filter((dialog) => dialog.id !== action.id) }
    case 'local_error':
      return pushError(state, action.source, action.message)
    case 'reset_session':
      return {
        ...state,
        transcript: [],
        toolExecutions: {},
        streamingKey: null,
        pendingUser: [],
        stats: null,
      }
    case 'optimistic_model':
      return { ...state, state: { ...(state.state ?? {}), model: action.model } }
    case 'optimistic_thinking':
      return { ...state, state: { ...(state.state ?? {}), thinkingLevel: action.level } }
  }
}

/* -------------------------------------------------------------------------- */
/* Transport                                                                   */
/* -------------------------------------------------------------------------- */

export function resolveWsUrl(): string {
  const configured = import.meta.env.VITE_WS_URL
  if (typeof configured === 'string' && configured.length > 0) return configured
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${scheme}//${window.location.host}/api/ws`
}

function resolveApiBase(): string {
  const configured = import.meta.env.VITE_API_BASE
  return typeof configured === 'string' && configured.length > 0 ? configured : ''
}

let commandSeq = 0
function nextCommandId(): string {
  commandSeq += 1
  return `c${commandSeq}-${Date.now().toString(36)}`
}

let optimisticSeq = 0

/** Reconnect backoff: fast first retries, capped so a dead server stops hammering. */
function backoffDelay(attempt: number): number {
  const base = Math.min(15_000, 500 * 2 ** attempt)
  return base + Math.round(Math.random() * 250)
}

/* -------------------------------------------------------------------------- */
/* Hook                                                                        */
/* -------------------------------------------------------------------------- */

export interface AgentApi {
  connection: ConnectionStatus
  hasConnected: boolean
  serverVersion: string | null
  transcript: TranscriptEntry[]
  toolExecutions: Record<string, ToolExecution>
  isStreaming: boolean
  isCompacting: boolean
  state: AgentState | null
  sessions: SessionInfo[]
  sessionsError: string | null
  models: ModelInfo[]
  thinkingLevels: string[]
  commands: CommandInfo[]
  forkMessages: ForkMessageInfo[]
  stats: SessionStats | null
  dialogs: ExtensionDialog[]
  toasts: Toast[]
  errors: InlineError[]
  statusTexts: Record<string, string>
  widgets: Record<string, string[]>
  editorText: string | null
  send: (command: ClientCommandInput) => void
  sendPrompt: (text: string, behavior?: StreamingBehavior) => void
  steer: (text: string) => void
  followUp: (text: string) => void
  abort: () => void
  clearQueue: () => void
  newSession: () => void
  switchSession: (sessionPath: string) => void
  cloneSession: () => void
  fork: (entryId: string) => void
  setModel: (model: ModelInfo) => void
  cycleModel: () => void
  setThinkingLevel: (level: string) => void
  compact: (customInstructions?: string) => void
  setAutoCompaction: (enabled: boolean) => void
  setSessionName: (name: string) => void
  respondDialog: (id: string, response: Omit<ExtensionUiResponse, 'type' | 'id'>) => void
  dismissToast: (id: string) => void
  dismissError: (id: string) => void
  consumeEditorText: () => void
  refreshAll: () => void
  reconnectNow: () => void
}

export function useAgent(): AgentApi {
  const [store, dispatch] = useReducer(reducer, INITIAL_STORE)
  const socketRef = useRef<WebSocket | null>(null)
  const retryTimerRef = useRef<number | null>(null)
  const attemptRef = useRef(0)
  const closedByUsRef = useRef(false)
  // Kept in a ref so `connect` can stay mount-stable while still being able to
  // reconcile after a reconnect.
  const dispatchRef = useRef(dispatch)
  dispatchRef.current = dispatch

  const send = useCallback((command: ClientCommandInput) => {
    const socket = socketRef.current
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      dispatchRef.current({
        type: 'local_error',
        id: 'offline',
        source: 'connection',
        message: '连接未就绪，命令未发送',
      })
      return
    }
    socket.send(JSON.stringify({ ...command, id: nextCommandId() }))
  }, [])

  const loadSessions = useCallback(async () => {
    try {
      const response = await fetch(`${resolveApiBase()}/api/sessions`)
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const payload = (await response.json()) as { sessions?: SessionInfo[] }
      dispatchRef.current({ type: 'sessions', sessions: payload.sessions ?? [] })
    } catch (error) {
      dispatchRef.current({
        type: 'sessions_error',
        error: error instanceof Error ? error.message : String(error),
      })
    }
  }, [])

  const syncState = useCallback(() => {
    send({ type: 'get_state' })
    send({ type: 'get_messages' })
    send({ type: 'get_session_stats' })
    send({ type: 'get_available_models' })
    send({ type: 'get_available_thinking_levels' })
    send({ type: 'get_commands' })
  }, [send])

  const connect = useCallback(() => {
    if (retryTimerRef.current !== null) {
      window.clearTimeout(retryTimerRef.current)
      retryTimerRef.current = null
    }

    dispatchRef.current({ type: 'connection', status: attemptRef.current === 0 ? 'connecting' : 'reconnecting' })

    const socket = new WebSocket(resolveWsUrl())
    socketRef.current = socket

    socket.onopen = () => {
      // A socket replaced by a newer connection (StrictMode remount, manual
      // reconnect) must not drive state any more.
      if (socketRef.current !== socket) return
      attemptRef.current = 0
      dispatchRef.current({ type: 'connection', status: 'open', hasConnected: true })
      // Re-sync after every (re)connect: the server may have moved on while we
      // were away, and the reconnect must not show a stale transcript.
      syncState()
      void loadSessions()
      void fetch(`${resolveApiBase()}/api/health`)
        .then((response) => (response.ok ? response.json() : null))
        .then((payload: { version?: string } | null) => {
          if (payload?.version) dispatchRef.current({ type: 'server_version', version: payload.version })
        })
        .catch(() => undefined)
    }

    socket.onmessage = (event: MessageEvent<string>) => {
      if (socketRef.current !== socket) return
      let parsed: unknown
      try {
        parsed = JSON.parse(event.data)
      } catch {
        return
      }
      const record = asRecord(parsed)
      if (typeof record.type !== 'string') return
      // setTitle changes a document property rather than app state, so it is
      // applied here instead of inside the reducer.
      if (record.type === 'extension_ui_request' && record.method === 'setTitle' && typeof record.title === 'string') {
        document.title = record.title
      }
      dispatchRef.current({ type: 'server_event', event: parsed as ServerEvent })
    }

    socket.onclose = () => {
      // Only the live socket may schedule a retry; otherwise the deliberate close
      // of a replaced socket would spawn a duplicate connection.
      if (socketRef.current !== socket) return
      socketRef.current = null
      if (closedByUsRef.current) return
      const attempt = attemptRef.current
      attemptRef.current += 1
      dispatchRef.current({ type: 'connection', status: 'reconnecting' })
      retryTimerRef.current = window.setTimeout(connect, backoffDelay(attempt))
    }

    socket.onerror = () => {
      // `onclose` always follows; the retry lives there so there is one path.
    }
  }, [loadSessions, syncState])

  useEffect(() => {
    closedByUsRef.current = false
    connect()
    return () => {
      closedByUsRef.current = true
      if (retryTimerRef.current !== null) window.clearTimeout(retryTimerRef.current)
      socketRef.current?.close()
      socketRef.current = null
    }
  }, [connect])

  // Sessions and health are cheap; polling them keeps the sidebar honest when a
  // session is created by another client.
  useEffect(() => {
    void loadSessions()
    const timer = window.setInterval(() => {
      void loadSessions()
    }, 15_000)
    return () => window.clearInterval(timer)
  }, [loadSessions])

  // Stable across renders: the Composer depends on it in an effect, and a new
  // identity on every store update would re-run the injection logic.
  const consumeEditorText = useCallback(() => dispatch({ type: 'clear_editor_text' }), [])

  const sendPrompt = useCallback(
    (text: string, behavior?: StreamingBehavior) => {
      const trimmed = text.trim()
      if (!trimmed) return
      const key = `local-${(optimisticSeq += 1)}`
      dispatchRef.current({ type: 'optimistic_user', key, text: trimmed })
      send(behavior ? { type: 'prompt', message: trimmed, streamingBehavior: behavior } : { type: 'prompt', message: trimmed })
    },
    [send],
  )

  const api = useMemo<AgentApi>(
    () => ({
      connection: store.connection,
      hasConnected: store.hasConnected,
      serverVersion: store.serverVersion,
      transcript: store.transcript,
      toolExecutions: store.toolExecutions,
      isStreaming: store.agentRunning || Boolean(store.state?.isStreaming),
      isCompacting: Boolean(store.state?.isCompacting),
      state: store.state,
      sessions: store.sessions,
      sessionsError: store.sessionsError,
      models: store.models,
      thinkingLevels: store.thinkingLevels,
      commands: store.commands,
      forkMessages: store.forkMessages,
      stats: store.stats,
      dialogs: store.dialogs,
      toasts: store.toasts,
      errors: store.errors,
      statusTexts: store.statusTexts,
      widgets: store.widgets,
      editorText: store.editorText,
      send,
      sendPrompt,
      steer: (text: string) => send({ type: 'steer', message: text }),
      followUp: (text: string) => send({ type: 'follow_up', message: text }),
      abort: () => send({ type: 'abort' }),
      clearQueue: () => send({ type: 'clear_queue' }),
      newSession: () => {
        dispatchRef.current({ type: 'reset_session' })
        send({ type: 'new_session' })
        window.setTimeout(syncState, 400)
        window.setTimeout(() => void loadSessions(), 400)
      },
      switchSession: (sessionPath: string) => {
        dispatchRef.current({ type: 'reset_session' })
        send({ type: 'switch_session', sessionPath })
        // The server swaps its session object; state and messages must be
        // re-read afterwards, and the two sends are ordered on one socket.
        window.setTimeout(syncState, 400)
      },
      cloneSession: () => {
        dispatchRef.current({ type: 'reset_session' })
        send({ type: 'clone' })
        window.setTimeout(syncState, 400)
      },
      fork: (entryId: string) => {
        dispatchRef.current({ type: 'reset_session' })
        send({ type: 'fork', entryId })
        window.setTimeout(syncState, 400)
      },
      setModel: (model: ModelInfo) => {
        dispatchRef.current({ type: 'optimistic_model', model })
        send({ type: 'set_model', provider: model.provider, modelId: model.id })
        window.setTimeout(syncState, 400)
      },
      cycleModel: () => send({ type: 'cycle_model' }),
      setThinkingLevel: (level: string) => {
        dispatchRef.current({ type: 'optimistic_thinking', level })
        send({ type: 'set_thinking_level', level })
      },
      compact: (customInstructions?: string) =>
        send(customInstructions ? { type: 'compact', customInstructions } : { type: 'compact' }),
      setAutoCompaction: (enabled: boolean) => send({ type: 'set_auto_compaction', enabled }),
      setSessionName: (name: string) => send({ type: 'set_session_name', name }),
      respondDialog: (id: string, response: Omit<ExtensionUiResponse, 'type' | 'id'>) => {
        const socket = socketRef.current
        // A dialog answer must go out even while the agent is streaming: the agent
        // is parked on it, so dropping the reply would deadlock the turn.
        if (socket && socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: 'extension_ui_response', id, ...response }))
        } else {
          dispatchRef.current({
            type: 'local_error',
            id: 'dialog-offline',
            source: 'connection',
            message: '连接已断开，无法回复弹窗',
          })
        }
        dispatchRef.current({ type: 'dismiss_dialog', id })
      },
      dismissToast: (id: string) => dispatch({ type: 'dismiss_toast', id }),
      dismissError: (id: string) => dispatch({ type: 'dismiss_error', id }),
      consumeEditorText,
      refreshAll: () => {
        syncState()
        void loadSessions()
      },
      reconnectNow: () => {
        attemptRef.current = 0
        socketRef.current?.close()
        connect()
      },
    }),
    [store, send, sendPrompt, syncState, loadSessions, connect],
  )

  return api
}

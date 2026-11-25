/**
 * The transcript reducer: every server event becomes the next view state.
 *
 * This module is pure on purpose.  The socket, retries, reconnects and React all
 * live in ``useAgent``; what a ``message_update`` does to a streaming assistant
 * message, how a tool result attaches to its call, how the optimistic user echo is
 * reconciled with the server's authoritative message — that is all here, and it
 * can be exercised without a browser.  ``npm run verify:reducer`` does exactly
 * that.
 *
 * The rules worth knowing before editing:
 *
 * - ``seq`` is the reducer's own counter.  It names transcript entries (``m7``) and
 *   must only ever move forward; a key that is reused makes React render two
 *   different messages into one row.
 * - Every role stays ``streaming`` until its own ``message_end``.  The server emits
 *   a start/end pair for user messages and tool results too, and marking only the
 *   assistant as streaming appended a duplicate of everything else.
 * - ``message_update`` carries delta-only assistant events; the cumulative message
 *   is assembled here.
 */

import type {
  AgentState,
  AssistantMessage,
  BranchPoint,
  AssistantMessageEvent,
  CommandInfo,
  ContentBlock,
  ExtensionUiRequest,
  ForkMessageInfo,
  ImageAttachment,
  Message,
  ModelInfo,
  PendingQuestion,
  ServerEvent,
  SessionInfo,
  SessionStats,
  ToolCallBlock,
  ToolResultPayload,
  UserQuestionRequestEvent,
  UserQuestionSettledEvent,
} from './protocol.ts'
import { asRecord, isAssistantMessage, isTextBlock, isToolCallBlock } from './protocol.ts'
import {
  markSessionRunning,
  markSessionSettled,
  markSessionViewed,
  reconcileSessionActivity,
  syncSessionActivity,
  type SessionActivityMap,
} from './sessionActivity.ts'

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
  /** Last time the tool reported output; the UI uses this to detect a silent command. */
  lastActivityAt?: number
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

export interface AgentStore {
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
  /** Per-session running / "finished while you were elsewhere" markers. */
  sessionActivity: SessionActivityMap
  sessionsError: string | null
  models: ModelInfo[]
  thinkingLevels: string[]
  commands: CommandInfo[]
  forkMessages: ForkMessageInfo[]
  branchPoints: BranchPoint[]
  stats: SessionStats | null
  dialogs: ExtensionDialog[]
  /** Questions that can still be answered, live or replayed from the transcript. */
  questions: PendingQuestion[]
  /** Which question the composer card is currently showing. */
  activeQuestionId: string | null
  toasts: Toast[]
  errors: InlineError[]
  statusTexts: Record<string, string>
  widgets: Record<string, string[]>
  editorText: string | null
  seq: number
}

export const INITIAL_STORE: AgentStore = {
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
  sessionActivity: {},
  sessionsError: null,
  models: [],
  thinkingLevels: [],
  commands: [],
  forkMessages: [],
  branchPoints: [],
  stats: null,
  dialogs: [],
  questions: [],
  activeQuestionId: null,
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

export type Action =
  | { type: 'connection'; status: ConnectionStatus; hasConnected?: boolean }
  | { type: 'server_version'; version: string }
  | { type: 'server_event'; event: ServerEvent }
  | { type: 'sessions'; sessions: SessionInfo[] }
  | { type: 'sessions_error'; error: string }
  | { type: 'session_running'; path: string }
  | { type: 'session_settled'; path: string }
  | { type: 'session_viewed'; path: string }
  | { type: 'optimistic_user'; key: string; text: string; images?: ImageAttachment[] }
  | { type: 'clear_editor_text' }
  | { type: 'dismiss_toast'; id: string }
  | { type: 'dismiss_error'; id: string }
  | { type: 'dismiss_dialog'; id: string }
  | { type: 'local_error'; id: string; source: string; message: string }
  | { type: 'reset_session' }
  | { type: 'workspace_changed' }
  | { type: 'optimistic_model'; model: ModelInfo }
  | { type: 'optimistic_thinking'; level: string }
  | { type: 'questions_sync'; questions: PendingQuestion[] }
  | { type: 'question_upsert'; question: PendingQuestion }
  | { type: 'question_settled'; callId: string }
  | { type: 'question_activate'; callId: string | null }

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
      lastActivityAt: existing?.lastActivityAt ?? Date.now(),
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

  // A resync can land mid-turn — a reconnect, or coming back to a conversation
  // whose run is still going in the background. That trailing assistant message
  // (no `stopReason` yet) is where the next `message_update` deltas belong;
  // without pointing `streamingKey` at it they would open a second, truncated
  // entry and the reply would appear twice.
  let streamingKey: string | null = null
  const last = messages[messages.length - 1]
  if (last?.role === 'assistant' && !last.stopReason && state.state?.isStreaming) {
    const trailing = transcript[transcript.length - 1]
    transcript[transcript.length - 1] = { ...trailing, streaming: true }
    streamingKey = trailing.key
  }

  return { ...state, transcript, toolExecutions: tools, streamingKey, seq }
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

/** Replace the store's question list with the server's authoritative view. */
function syncQuestions(state: AgentStore, incoming: PendingQuestion[]): AgentStore {
  // The server derives this from the transcript, so it is complete: an `open`
  // call is in the log before its tool runs, and a settled one is gone. Trusting
  // it is what makes a reconnect drop a card whose answer was already delivered.
  const questions = [...incoming].sort((a, b) =>
    a.state === b.state ? 0 : a.state === 'open' ? -1 : 1,
  )
  const active =
    state.activeQuestionId && questions.some((question) => question.callId === state.activeQuestionId)
      ? state.activeQuestionId
      : questions.find((question) => question.state === 'open')?.callId ?? null
  return { ...state, questions, activeQuestionId: active }
}

/** Insert or replace one answerable question, newest live question first. */
function upsertQuestion(state: AgentStore, question: PendingQuestion): AgentStore {
  const others = state.questions.filter((item) => item.callId !== question.callId)
  const questions = [question, ...others].sort((a, b) =>
    a.state === b.state ? 0 : a.state === 'open' ? -1 : 1,
  )
  return {
    ...state,
    questions,
    // Only an unanswered (live) question takes over the composer; a question
    // that merely became answerable again must not steal focus.
    activeQuestionId: state.activeQuestionId ?? (question.state === 'open' ? question.callId : null),
  }
}

/** Drop a settled question and move the card to the next one. */
function removeQuestion(state: AgentStore, callId: string): AgentStore {
  const questions = state.questions.filter((question) => question.callId !== callId)
  return {
    ...state,
    questions,
    activeQuestionId: state.activeQuestionId === callId ? questions[0]?.callId ?? null : state.activeQuestionId,
  }
}

function reduceServerEvent(state: AgentStore, event: ServerEvent): AgentStore {
  const loose = asRecord(event)
  switch (loose.type) {
    case 'response':
      return reduceResponse(state, event as Extract<ServerEvent, { type: 'response' }>)

    case 'agent_start': {
      const path = state.state?.sessionFile
      const next = { ...state, agentRunning: true }
      return path ? { ...next, sessionActivity: markSessionRunning(next.sessionActivity, path) } : next
    }

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
      const path = state.state?.sessionFile
      const next = { ...state, agentRunning: false, streamingKey: null, toolExecutions: tools }
      return path
        ? {
            ...next,
            sessionActivity: markSessionSettled(next.sessionActivity, path, path),
          }
        : next
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
            lastActivityAt: Date.now(),
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
            lastActivityAt: Date.now(),
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
            lastActivityAt: existing?.lastActivityAt ?? Date.now(),
          },
        },
      }
    }

    case 'user_question_request': {
      const request = event as UserQuestionRequestEvent
      return upsertQuestion(state, {
        callId: request.callId,
        questions: request.questions,
        state: 'open',
        blocking: request.blocking,
        ...(typeof request.timeoutMs === 'number' ? { remainingMs: request.timeoutMs } : {}),
      })
    }

    case 'user_question_settled': {
      const settled = event as UserQuestionSettledEvent
      if (settled.outcome === 'pending') {
        // A timeout releases the model but not the question: keep the card,
        // drop the countdown, and let the user answer it later.
        const current = state.questions.find((question) => question.callId === settled.callId)
        if (!current) return state
        return upsertQuestion(state, { ...current, state: 'continued', remainingMs: undefined })
      }
      return removeQuestion(state, settled.callId)
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
    case 'answer_question': {
      const result = asRecord(response.data)
      const callId = typeof result.callId === 'string' ? result.callId : null
      // Dismissing a timed question settles as `pending`: the model is released
      // but the question stays answerable, so the card must not disappear.
      if (result.outcome === 'pending') return state
      return callId ? removeQuestion(state, callId) : state
    }
    case 'get_state': {
      const data = response.data as AgentState
      const questions = data.pendingQuestions ?? []
      // `isStreaming` is authoritative for the conversation this tab is attached to.
      // It also keeps a stale global "running" flag from a conversation we switched
      // away from making every later switch look like a run that just completed.
      const sessionActivity = data.sessionFile
        ? syncSessionActivity(state.sessionActivity, data.sessionFile, data.isStreaming === true)
        : state.sessionActivity
      return syncQuestions(
        {
          ...state,
          state: data,
          agentRunning: data.isStreaming === true,
          sessionActivity,
        },
        questions,
      )
    }
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
    case 'get_branch_points': {
      const points = Array.isArray(data.points) ? (data.points as BranchPoint[]) : []
      return { ...state, branchPoints: points }
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

export function reducer(state: AgentStore, action: Action): AgentStore {
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
      return {
        ...state,
        sessions: action.sessions,
        sessionsError: null,
        sessionActivity: reconcileSessionActivity(
          state.sessionActivity,
          action.sessions,
          state.state?.sessionFile,
        ),
      }
    case 'sessions_error':
      return { ...state, sessionsError: action.error }
    case 'session_running':
      return {
        ...state,
        sessionActivity: markSessionRunning(state.sessionActivity, action.path),
      }
    case 'session_settled':
      return {
        ...state,
        sessionActivity: markSessionSettled(
          state.sessionActivity,
          action.path,
          state.state?.sessionFile,
        ),
      }
    case 'session_viewed':
      return { ...state, sessionActivity: markSessionViewed(state.sessionActivity, action.path) }
    case 'optimistic_user': {
      const seq = state.seq + 1
      const images = action.images ?? []
      const content: string | ContentBlock[] =
        images.length > 0
          ? [
              { type: 'text', text: action.text },
              ...images.map((image) => ({
                type: 'image' as const,
                data: image.data,
                mimeType: image.mimeType,
              })),
            ]
          : action.text
      const entry: TranscriptEntry = {
        key: action.key,
        message: { role: 'user', content, timestamp: Date.now() },
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
    case 'questions_sync':
      return syncQuestions(state, action.questions)
    case 'question_upsert':
      return upsertQuestion(state, action.question)
    case 'question_settled':
      return removeQuestion(state, action.callId)
    case 'question_activate':
      return { ...state, activeQuestionId: action.callId }
    case 'local_error':
      return pushError(state, action.source, action.message)
    case 'workspace_changed':
      // A different workspace is a different agent; keep the transport status but
      // drop everything that belonged to the old one. Activity markers are keyed by
      // session path, which is unique across workspaces, so they survive: switching
      // workspace must not silently clear a green completion dot.
      return {
        ...INITIAL_STORE,
        connection: state.connection,
        hasConnected: state.hasConnected,
        serverVersion: state.serverVersion,
        sessionActivity: state.sessionActivity,
      }
    case 'reset_session':
      return {
        ...state,
        transcript: [],
        toolExecutions: {},
        streamingKey: null,
        pendingUser: [],
        questions: [],
        activeQuestionId: null,
        stats: null,
        // The new conversation is a different one; the old run's flag must not leak
        // into it. `get_state` re-asserts streaming if the adopted session is live.
        agentRunning: false,
      }
    case 'optimistic_model':
      return { ...state, state: { ...(state.state ?? {}), model: action.model } }
    case 'optimistic_thinking':
      return { ...state, state: { ...(state.state ?? {}), thinkingLevel: action.level } }
  }
}

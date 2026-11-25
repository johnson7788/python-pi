import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react'
import type {
  AgentState,
  BranchPoint,
  ClientCommandInput,
  CommandInfo,
  ExtensionUiResponse,
  ForkMessageInfo,
  ImageAttachment,
  ModelInfo,
  PendingQuestion,
  QuestionAnswer,
  ServerEvent,
  SessionInfo,
  SessionStats,
  StreamingBehavior,
} from './protocol'
import { asRecord } from './protocol'
import { resolveApiBase, resolveWsUrl } from './endpoints'
import {
  INITIAL_STORE,
  reducer,
  type ConnectionStatus,
  type ExtensionDialog,
  type InlineError,
  type Toast,
  type ToolExecution,
  type TranscriptEntry,
} from './streamReducer'
import type { SessionActivityMap } from './sessionActivity'

// The reducer and its public types live in their own module so they can be tested
// without React.  Re-exporting keeps every existing import path working.
export {
  type AgentStore,
  type ConnectionStatus,
  type DialogMethod,
  type ExtensionDialog,
  type InlineError,
  type Toast,
  type ToolExecution,
  type TranscriptEntry,
  messageText,
  toolResultText,
} from './streamReducer'

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
  /** Running / unread-completion markers, keyed by session path. */
  sessionActivity: SessionActivityMap
  sessionsError: string | null
  models: ModelInfo[]
  thinkingLevels: string[]
  commands: CommandInfo[]
  forkMessages: ForkMessageInfo[]
  branchPoints: BranchPoint[]
  stats: SessionStats | null
  dialogs: ExtensionDialog[]
  /** Questions that can still be answered, newest live one first. */
  questions: PendingQuestion[]
  activeQuestionId: string | null
  toasts: Toast[]
  errors: InlineError[]
  statusTexts: Record<string, string>
  widgets: Record<string, string[]>
  editorText: string | null
  send: (command: ClientCommandInput) => void
  sendPrompt: (text: string, behavior?: StreamingBehavior, images?: ImageAttachment[]) => void
  steer: (text: string) => void
  followUp: (text: string) => void
  abort: () => void
  clearQueue: () => void
  newSession: () => void
  switchSession: (sessionPath: string) => void
  cloneSession: () => void
  fork: (entryId: string, position?: 'before' | 'at') => void
  setModel: (model: ModelInfo) => void
  cycleModel: () => void
  setThinkingLevel: (level: string) => void
  compact: (customInstructions?: string) => void
  setAutoCompaction: (enabled: boolean) => void
  setSessionName: (name: string) => void
  respondDialog: (id: string, response: Omit<ExtensionUiResponse, 'type' | 'id'>) => void
  activateQuestion: (callId: string | null) => void
  claimQuestion: (callId: string) => void
  questionTimeout: (callId: string) => void
  answerQuestion: (callId: string, answers: QuestionAnswer[], cancelled?: boolean) => void
  dismissToast: (id: string) => void
  dismissError: (id: string) => void
  consumeEditorText: () => void
  refreshAll: () => void
  reconnectNow: () => void
}

export interface UseAgentOptions {
  /**
   * Whether to hold a socket at all. The app only enables the agent once a session is
   * known to exist, so a logged-out or still-checking browser does not open a connection
   * that is certain to be refused.
   */
  enabled?: boolean
  /** Workspace id to bind the socket and session list to. */
  workspace?: string
  /** App/mode composition to bind the worker to. */
  appId?: string
  modeId?: string
  /** Called when the server reports the session is gone (HTTP 401 or socket close 4401). */
  onUnauthorized?: () => void
}

export function useAgent(options: UseAgentOptions = {}): AgentApi {
  const { enabled = true, workspace = 'default', appId = 'general', modeId = 'office' } = options
  const workspaceRef = useRef(workspace)
  const appRef = useRef(appId)
  const modeRef = useRef(modeId)
  workspaceRef.current = workspace
  appRef.current = appId
  modeRef.current = modeId
  const [store, dispatch] = useReducer(reducer, INITIAL_STORE)
  const socketRef = useRef<WebSocket | null>(null)
  const retryTimerRef = useRef<number | null>(null)
  const attemptRef = useRef(0)
  const closedByUsRef = useRef(false)
  //: Highest server record already applied.  Sent as \`since\` on reconnect so the
  //: server replays only the tail this tab missed; a reload starts at 0 and gets
  //: whatever is not yet persisted.
  const serverSeqRef = useRef(0)
  // Held in a ref so `connect` can stay mount-stable while still being able to
  // reconcile after a reconnect.
  const dispatchRef = useRef(dispatch)
  dispatchRef.current = dispatch
  const unauthorizedRef = useRef(options.onUnauthorized)
  unauthorizedRef.current = options.onUnauthorized

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
      const response = await fetch(
        `${resolveApiBase()}/api/sessions?scope=all&workspace=${encodeURIComponent(
          workspaceRef.current,
        )}&app=${encodeURIComponent(appRef.current)}&mode=${encodeURIComponent(modeRef.current)}`,
        { credentials: 'same-origin' },
      )
      if (response.status === 401) {
        unauthorizedRef.current?.()
        return
      }
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
    send({ type: 'get_branch_points' })
  }, [send])

  const connect = useCallback(() => {
    if (retryTimerRef.current !== null) {
      window.clearTimeout(retryTimerRef.current)
      retryTimerRef.current = null
    }

    dispatchRef.current({ type: 'connection', status: attemptRef.current === 0 ? 'connecting' : 'reconnecting' })

    const socket = new WebSocket(
      resolveWsUrl(workspaceRef.current, appRef.current, modeRef.current, serverSeqRef.current),
    )
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
      // Reconnect replay can overlap what this tab already applied; the server's
      // sequence number is the only ordering that survives a socket swap.
      if (typeof record.seq === 'number') {
        if (record.seq <= serverSeqRef.current) return
        serverSeqRef.current = record.seq
      }
      // setTitle changes a document property rather than app state, so it is
      // applied here instead of inside the reducer.
      if (record.type === 'extension_ui_request' && record.method === 'setTitle' && typeof record.title === 'string') {
        document.title = record.title
      }
      dispatchRef.current({ type: 'server_event', event: parsed as ServerEvent })

      // A conversation left mid-turn keeps running on the server after the socket
      // has stopped forwarding its events. The worker announces that run's end on
      // this socket, which is the promptest way to raise the sidebar's green dot.
      if (record.type === 'session_activity' && typeof record.sessionFile === 'string') {
        dispatchRef.current({ type: 'session_settled', path: record.sessionFile })
      }

      // The turn is over locally: refresh the list so the row's spinner clears
      // without waiting for the next poll.
      if (record.type === 'agent_end') void loadSessions()

      // Branch points are the session entry ids behind the messages we render,
      // so they move whenever the transcript does. Refreshing here keeps the
      // "branch from this answer" action available without polling.
      if (record.type === 'message_end') {
        const role = asRecord(asRecord(record).message).role
        if (role === 'assistant' || role === 'user') send({ type: 'get_branch_points' })
      }
    }

    socket.onclose = (event: CloseEvent) => {
      // Only the live socket may schedule a retry; otherwise the deliberate close
      // of a replaced socket would spawn a duplicate connection.
      if (socketRef.current !== socket) return
      socketRef.current = null
      if (closedByUsRef.current) return
      // 4401 is the server saying "there is no session behind this socket". Retrying
      // would hammer a login wall forever, so the app is told to ask for credentials.
      if (event.code === 4401) {
        unauthorizedRef.current?.()
        dispatchRef.current({ type: 'connection', status: 'reconnecting' })
        return
      }
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
    if (!enabled) return
    closedByUsRef.current = false
    dispatchRef.current({ type: 'workspace_changed' })
    connect()
    return () => {
      closedByUsRef.current = true
      if (retryTimerRef.current !== null) window.clearTimeout(retryTimerRef.current)
      socketRef.current?.close()
      socketRef.current = null
    }
  }, [connect, enabled, workspace, appId, modeId])

  // Sessions and health are cheap; polling them keeps the sidebar honest when a
  // session is created by another client. While something is streaming, poll
  // faster: the sidebar has to notice the moment a background run ends, and a
  // 15-second lag would make the green completion dot feel broken. The local
  // activity map counts too, so a run that just started is not discovered late.
  const anySessionRunning =
    store.sessions.some((session) => session.running === true) ||
    Object.values(store.sessionActivity).some((activity) => activity.running)
  useEffect(() => {
    if (!enabled) return
    void loadSessions()
    const timer = window.setInterval(() => {
      void loadSessions()
    }, anySessionRunning ? 4_000 : 15_000)
    return () => window.clearInterval(timer)
  }, [anySessionRunning, loadSessions, enabled])

  // Stable across renders: the Composer depends on it in an effect, and a new
  // identity on every store update would re-run the injection logic.
  const consumeEditorText = useCallback(() => dispatch({ type: 'clear_editor_text' }), [])

  const sendPrompt = useCallback(
    (text: string, behavior?: StreamingBehavior, images?: ImageAttachment[]) => {
      const trimmed = text.trim()
      const attachments = images?.length ? images : undefined
      if (!trimmed && !attachments) return
      const key = `local-${(optimisticSeq += 1)}`
      dispatchRef.current({ type: 'optimistic_user', key, text: trimmed, images: attachments })
      send({
        type: 'prompt',
        message: trimmed,
        ...(attachments ? { images: attachments } : {}),
        ...(behavior ? { streamingBehavior: behavior } : {}),
      })
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
      sessionActivity: store.sessionActivity,
      sessionsError: store.sessionsError,
      models: store.models,
      thinkingLevels: store.thinkingLevels,
      commands: store.commands,
      forkMessages: store.forkMessages,
      branchPoints: store.branchPoints,
      stats: store.stats,
      dialogs: store.dialogs,
      questions: store.questions,
      activeQuestionId: store.activeQuestionId,
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
        // Leaving a running conversation retires it, it does not stop it. Show its
        // spinner right away instead of waiting for the next list poll to say so.
        //
        // Only *this* conversation's own activity counts: `agentRunning` describes
        // whichever run the tab is attached to, and after switching away from a
        // long-running conversation it used to stay true. Using it here marked old,
        // already-finished rows as running, and the next poll turned them into
        // bogus green completion dots.
        const leaving = store.state?.sessionFile
        const leavingRunning = leaving ? store.sessionActivity[leaving]?.running === true : false
        if (leaving && leaving !== sessionPath && leavingRunning) {
          dispatchRef.current({ type: 'session_running', path: leaving })
          // Let the server confirm: if that turn settled in the same instant there is
          // no background run, and the confirmation (or its absence) becomes the dot.
          window.setTimeout(() => void loadSessions(), 600)
        }
        dispatchRef.current({ type: 'reset_session' })
        dispatchRef.current({ type: 'session_viewed', path: sessionPath })
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
      fork: (entryId: string, position: 'before' | 'at' = 'before') => {
        dispatchRef.current({ type: 'reset_session' })
        send({ type: 'fork', entryId, position })
        // Forking swaps the live session for the branch, so the state and the
        // session list both move.
        window.setTimeout(syncState, 400)
        window.setTimeout(() => void loadSessions(), 400)
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
      activateQuestion: (callId: string | null) =>
        dispatchRef.current({ type: 'question_activate', callId }),
      claimQuestion: (callId: string) => send({ type: 'claim_question', callId }),
      questionTimeout: (callId: string) => send({ type: 'question_timeout', callId }),
      answerQuestion: (callId: string, answers: QuestionAnswer[], cancelled = false) => {
        // The card collapses from the server's own settled event (or the answer
        // response for a late reply).  Removing it optimistically would hide a
        // question that a dismissed timed wait keeps answerable.
        if (cancelled) dispatchRef.current({ type: 'question_activate', callId: null })
        send({ type: 'answer_question', callId, answers, ...(cancelled ? { cancelled: true } : {}) })
      },
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

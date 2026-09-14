#!/usr/bin/env node
/**
 * Stand-in for the Python backend.
 *
 * The real server does not exist yet, so this script implements the frozen
 * protocol surface the UI depends on: the REST endpoints the sidebar polls and
 * the `/api/ws` socket. On connect it replays one scripted session so every
 * awkward path is exercised without a keyboard: a thinking block, streamed text
 * deltas, a tool call with live partial output, and a blocking `confirm` dialog
 * that only completes after the client answers.
 *
 * Run:  node mock-server.mjs          (127.0.0.1:8787)
 *       MOCK_AUTOPLAY=0 node mock-server.mjs   (only replays on `prompt`)
 */

import { createServer } from 'node:http'
import { randomUUID } from 'node:crypto'
import { WebSocketServer } from 'ws'

const HOST = process.env.MOCK_HOST ?? '127.0.0.1'
const PORT = Number(process.env.MOCK_PORT ?? 8787)
const AUTOPLAY = process.env.MOCK_AUTOPLAY !== '0'
const VERSION = '0.1.0-mock'
const SESSION_FILE = '/tmp/pi-mock/session-mock.jsonl'

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/** `ws` exposes the readyState constants on the prototype, but a literal is clearer. */
const WS_OPEN = 1

/* ------------------------------- fixtures --------------------------------- */

const MODELS = [
  {
    id: 'claude-sonnet-4-5',
    name: 'Claude Sonnet 4.5',
    api: 'anthropic',
    provider: 'anthropic',
    baseUrl: 'https://api.anthropic.com',
    reasoning: true,
    input: ['text', 'image'],
    cost: { input: 3, output: 15, cacheRead: 0.3, cacheWrite: 3.75, total: 0 },
    contextWindow: 200000,
    maxTokens: 64000,
  },
  {
    id: 'gpt-5-codex',
    name: 'GPT-5 Codex',
    api: 'openai-responses',
    provider: 'openai',
    baseUrl: 'https://api.openai.com/v1',
    reasoning: true,
    input: ['text'],
    cost: { input: 1.25, output: 10, cacheRead: 0.125, cacheWrite: 1.25, total: 0 },
    contextWindow: 400000,
    maxTokens: 128000,
  },
  {
    id: 'deepseek-v3.2',
    name: 'DeepSeek V3.2',
    api: 'openai-completions',
    provider: 'deepseek',
    baseUrl: 'https://api.deepseek.com',
    reasoning: false,
    input: ['text'],
    cost: { input: 0.28, output: 0.42, cacheRead: 0.028, cacheWrite: 0.28, total: 0 },
    contextWindow: 128000,
    maxTokens: 8192,
  },
]

const THINKING_LEVELS = ['off', 'low', 'medium', 'high']

const SESSIONS = [
  {
    path: SESSION_FILE,
    id: 'mock-session-0001',
    cwd: '/Users/admin/git/python-pi',
    name: 'Mock 演示会话',
    modified: Date.now(),
    messageCount: 3,
    firstMessage: '请帮我看看 mock 会话',
  },
  {
    path: '/tmp/pi-mock/session-earlier.jsonl',
    id: 'mock-session-0000',
    cwd: '/Users/admin/git/python-pi',
    name: '重构 rpc_mode',
    modified: Date.now() - 3_600_000,
    messageCount: 42,
    firstMessage: '把 rpc_mode 的扩展 UI 抽出来',
  },
  {
    path: '/tmp/pi-mock/session-notes.jsonl',
    id: 'mock-session-0002',
    cwd: '/Users/admin/git/python-pi/web',
    name: null,
    modified: Date.now() - 86_400_000,
    messageCount: 7,
    firstMessage: '给 web/ 加一个深色主题',
  },
]

const COMMANDS = [
  { name: 'review', description: '审查当前改动', source: 'prompt' },
  { name: 'test', description: '运行测试并总结失败原因', source: 'prompt' },
  { name: 'commit', description: '生成提交信息', source: 'prompt' },
  { name: 'pdf', description: '处理 PDF 文件', source: 'skill' },
  { name: 'deploy', description: '部署到测试环境', source: 'extension' },
]

const USAGE = {
  input: 1840,
  output: 312,
  cacheRead: 0,
  cacheWrite: 0,
  totalTokens: 2152,
  cost: { input: 0.0055, output: 0.0047, cacheRead: 0, cacheWrite: 0, total: 0.0102 },
}

const THINKING_TEXT =
  '用户想看 mock 会话的状态。我先读取 rpc_mode.py，确认扩展 UI 的字段名，再按同一套协议回复。'
const ANSWER_TEXT =
  '这是 **mock 会话** 的演示回复。\n\n' +
  '它按冻结的协议回放：\n\n' +
  '1. 先流式发送思考块\n' +
  '2. 再流式发送正文（`text_delta`）\n' +
  '3. 接着发起一次工具调用，并推送实时输出\n' +
  '4. 最后弹出一个需要你确认的对话框\n\n' +
  '> 对话框在下方，回答后本次回合才会结束。\n'

/* --------------------------------- server --------------------------------- */

/** Per-connection state; the real server keeps one session per socket. */
function createSession() {
  return {
    state: {
      model: MODELS[0],
      thinkingLevel: 'medium',
      isStreaming: false,
      isCompacting: false,
      steeringMode: 'all',
      followUpMode: 'all',
      sessionFile: SESSION_FILE,
      sessionId: 'mock-session-0001',
      sessionName: 'Mock 演示会话',
      autoCompactionEnabled: false,
      messageCount: 0,
      pendingMessageCount: 0,
    },
    messages: [],
    replaying: false,
    aborted: false,
    pendingDialogs: new Map(),
  }
}

function send(socket, payload) {
  if (socket.readyState === WS_OPEN) socket.send(JSON.stringify(payload))
}

function ok(requestId, command, data) {
  const response = { type: 'response', command, success: true }
  if (requestId !== undefined) response.id = requestId
  if (data !== undefined) response.data = data
  return response
}

function fail(requestId, command, error) {
  return { type: 'response', id: requestId, command, success: false, error }
}

/** Ask the client something and resolve only when it answers (or the wire times out). */
function requestConfirm(socket, session, { title, message, timeoutMs = 60000 }) {
  const id = randomUUID()
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      session.pendingDialogs.delete(id)
      resolve({ timedOut: true })
    }, timeoutMs)
    session.pendingDialogs.set(id, (response) => {
      clearTimeout(timer)
      session.pendingDialogs.delete(id)
      resolve(response)
    })
    send(socket, { type: 'extension_ui_request', id, method: 'confirm', title, message, timeout: timeoutMs })
  })
}

/** Stream one scripted turn. */
async function replay(socket, session) {
  if (session.replaying) return
  session.replaying = true
  session.aborted = false
  session.state.isStreaming = true

  const emit = (payload) => send(socket, payload)
  const step = async (ms = 45) => {
    await sleep(ms)
    return !session.aborted
  }

  try {
    emit({ type: 'agent_start' })

    const userMessage = { role: 'user', content: '请帮我看看 mock 会话', timestamp: Date.now() }
    session.messages.push(userMessage)
    emit({ type: 'message_start', message: userMessage })
    emit({ type: 'message_end', message: userMessage })
    emit({ type: 'turn_start' })

    const assistant = { role: 'assistant', content: [], timestamp: Date.now() }
    emit({ type: 'message_start', message: assistant })

    // 1. thinking block at contentIndex 0
    emit({ type: 'message_update', usage: USAGE, assistantMessageEvent: { type: 'thinking_start', contentIndex: 0 } })
    for (const chunk of chunkText(THINKING_TEXT, 12)) {
      if (!(await step())) return
      emit({
        type: 'message_update',
        usage: USAGE,
        assistantMessageEvent: { type: 'thinking_delta', contentIndex: 0, delta: chunk },
      })
    }
    emit({
      type: 'message_update',
      usage: USAGE,
      assistantMessageEvent: { type: 'thinking_end', contentIndex: 0, content: THINKING_TEXT },
    })

    // 2. streamed answer at contentIndex 1
    emit({ type: 'message_update', usage: USAGE, assistantMessageEvent: { type: 'text_start', contentIndex: 1 } })
    for (const chunk of chunkText(ANSWER_TEXT, 9)) {
      if (!(await step())) return
      emit({
        type: 'message_update',
        usage: USAGE,
        assistantMessageEvent: { type: 'text_delta', contentIndex: 1, delta: chunk },
      })
    }
    emit({
      type: 'message_update',
      usage: USAGE,
      assistantMessageEvent: { type: 'text_end', contentIndex: 1, content: ANSWER_TEXT },
    })

    // 3. a tool call, streamed as an id/name then argument deltas
    const toolCallId = 'call_mock_1'
    const toolArgs = { path: 'packages/pi-coding-agent/src/pi_coding_agent/modes/rpc_mode.py', limit: 20 }
    emit({
      type: 'message_update',
      usage: USAGE,
      assistantMessageEvent: { type: 'toolcall_start', contentIndex: 2, id: toolCallId, toolName: 'read_file' },
    })
    const argsJson = JSON.stringify(toolArgs)
    for (const chunk of chunkText(argsJson, 16)) {
      if (!(await step(25))) return
      emit({
        type: 'message_update',
        usage: USAGE,
        assistantMessageEvent: { type: 'toolcall_delta', contentIndex: 2, delta: chunk },
      })
    }
    emit({
      type: 'message_update',
      usage: USAGE,
      assistantMessageEvent: {
        type: 'toolcall_end',
        contentIndex: 2,
        toolCall: { type: 'toolCall', id: toolCallId, name: 'read_file', arguments: toolArgs },
      },
    })

    // 4. authoritative assistant message: replaces everything accumulated above
    const finalAssistant = {
      role: 'assistant',
      content: [
        { type: 'thinking', thinking: THINKING_TEXT },
        { type: 'text', text: ANSWER_TEXT },
        { type: 'toolCall', id: toolCallId, name: 'read_file', arguments: toolArgs },
      ],
      stopReason: 'toolUse',
      usage: USAGE,
      timestamp: Date.now(),
    }
    session.messages.push(finalAssistant)
    emit({ type: 'message_end', message: finalAssistant })

    if (!(await step(60))) return

    // 5. tool execution, with live partial output before the result
    emit({ type: 'tool_execution_start', toolCallId, toolName: 'read_file', args: toolArgs })
    for (const partial of ['读取中…', '读取中… 120 行', '读取中… 480 行']) {
      if (!(await step(90))) return
      emit({
        type: 'tool_execution_update',
        toolCallId,
        toolName: 'read_file',
        args: toolArgs,
        partialResult: { content: [{ type: 'text', text: partial }], details: null, terminate: null },
      })
    }

    const toolText = [
      'def model_to_json(model: Model) -> dict[str, Any]:',
      '    payload: dict[str, Any] = {',
      '        "id": model.id,',
      '        "name": model.name,',
      '        ...',
    ].join('\n')
    emit({
      type: 'tool_execution_end',
      toolCallId,
      toolName: 'read_file',
      result: { content: [{ type: 'text', text: toolText }], details: { lines: 20 }, terminate: null },
      isError: false,
    })

    const toolResult = {
      role: 'toolResult',
      toolCallId,
      toolName: 'read_file',
      content: [{ type: 'text', text: toolText }],
      isError: false,
      timestamp: Date.now(),
    }
    session.messages.push(toolResult)
    emit({ type: 'message_start', message: toolResult })
    emit({ type: 'message_end', message: toolResult })

    // 6. a blocking dialog: the client must answer for the turn to finish
    const answer = await requestConfirm(socket, session, {
      title: '是否继续？',
      message: '工具调用已完成。要继续执行下一轮吗？',
    })
    if (answer?.cancelled || answer?.timedOut) {
      emit({
        type: 'extension_ui_request',
        id: randomUUID(),
        method: 'notify',
        message: answer?.timedOut ? '对话框超时，已按取消处理' : '用户取消了后续执行',
        notifyType: 'warning',
      })
    } else {
      emit({
        type: 'extension_ui_request',
        id: randomUUID(),
        method: 'notify',
        message: answer?.confirmed ? '已确认，继续执行' : '未确认',
        notifyType: 'success',
      })
    }

    emit({
      type: 'turn_end',
      message: finalAssistant,
      toolResults: [toolResult],
    })
    emit({ type: 'agent_end', messages: session.messages, willRetry: false })
  } finally {
    session.replaying = false
    session.state.isStreaming = false
    session.state.messageCount = session.messages.length
  }
}

/** Split text into chunks of at most `size` characters, deterministic. */
function chunkText(text, size) {
  const chunks = []
  for (let index = 0; index < text.length; index += size) chunks.push(text.slice(index, index + size))
  return chunks
}

/* -------------------------------- commands -------------------------------- */

async function handleCommand(socket, session, command) {
  const id = typeof command.id === 'string' ? command.id : undefined
  const type = command.type

  switch (type) {
    case 'get_state':
      session.state.messageCount = session.messages.length
      send(socket, ok(id, type, session.state))
      return
    case 'get_messages':
      send(socket, ok(id, type, { messages: session.messages }))
      return
    case 'get_session_stats':
      send(
        socket,
        ok(id, type, {
          sessionFile: session.state.sessionFile,
          sessionId: session.state.sessionId,
          userMessages: session.messages.filter((message) => message.role === 'user').length,
          assistantMessages: session.messages.filter((message) => message.role === 'assistant').length,
          toolCalls: 1,
          toolResults: session.messages.filter((message) => message.role === 'toolResult').length,
          totalMessages: session.messages.length,
          tokens: { input: 1840, output: 312, cacheRead: 0, cacheWrite: 0, total: 2152 },
          cost: 0.0102,
        }),
      )
      return
    case 'get_fork_messages':
      send(
        socket,
        ok(id, type, {
          messages: session.messages
            .filter((message) => message.role === 'user')
            .map((message, index) => ({ entryId: `entry-${index + 1}`, text: String(message.content) })),
        }),
      )
      return
    case 'get_commands':
      send(socket, ok(id, type, { commands: COMMANDS }))
      return
    case 'get_available_models':
      send(socket, ok(id, type, { models: MODELS }))
      return
    case 'get_available_thinking_levels':
      send(socket, ok(id, type, { levels: THINKING_LEVELS }))
      return
    case 'set_model': {
      const model = MODELS.find(
        (item) => item.provider === command.provider && item.id === command.modelId,
      )
      if (!model) {
        send(socket, fail(id, type, `Model ${command.provider}/${command.modelId} not found`))
        return
      }
      session.state.model = model
      send(socket, ok(id, type, model))
      return
    }
    case 'cycle_model': {
      const current = MODELS.findIndex((item) => item.id === session.state.model?.id)
      const next = MODELS[(current + 1) % MODELS.length]
      session.state.model = next
      send(socket, ok(id, type, { model: next, thinkingLevel: session.state.thinkingLevel, isScoped: false }))
      return
    }
    case 'set_thinking_level':
      if (!THINKING_LEVELS.includes(command.level)) {
        send(socket, fail(id, type, `未知的思考级别：${command.level}`))
        return
      }
      session.state.thinkingLevel = command.level
      send(socket, ok(id, type, { level: command.level }))
      return
    case 'compact':
      send(socket, ok(id, type, { summary: '（mock）上下文已压缩', firstKeptEntryId: 'entry-1', tokensBefore: 2152 }))
      return
    case 'set_auto_compaction':
      session.state.autoCompactionEnabled = Boolean(command.enabled)
      send(socket, ok(id, type))
      return
    case 'set_session_name':
      session.state.sessionName = command.name
      send(socket, ok(id, type))
      return
    case 'new_session':
    case 'clone':
      session.messages = []
      session.state.sessionId = `mock-session-${Math.floor(Math.random() * 9000 + 1000)}`
      session.state.sessionFile = `/tmp/pi-mock/session-${session.state.sessionId}.jsonl`
      session.state.sessionName = type === 'clone' ? '克隆的会话' : null
      session.state.messageCount = 0
      send(socket, ok(id, type, { cancelled: false }))
      return
    case 'switch_session': {
      const target = SESSIONS.find((item) => item.path === command.sessionPath)
      if (!target) {
        send(socket, fail(id, type, `找不到会话：${command.sessionPath}`))
        return
      }
      session.messages = []
      session.state.sessionId = target.id
      session.state.sessionFile = target.path
      session.state.sessionName = target.name
      session.state.messageCount = 0
      send(socket, ok(id, type, { cancelled: false }))
      return
    }
    case 'fork':
      session.messages = []
      session.state.sessionFile = `/tmp/pi-mock/fork-${Date.now()}.jsonl`
      send(socket, ok(id, type, { text: '（mock）已分叉', cancelled: false }))
      return
    case 'abort':
      session.aborted = true
      send(socket, ok(id, type))
      return
    case 'clear_queue':
      send(socket, ok(id, type))
      return
    case 'steer':
    case 'follow_up':
      send(socket, ok(id, type, { accepted: true }))
      return
    case 'prompt':
      // `prompt` acknowledges at preflight; the answer arrives as events.
      send(socket, ok(id, type, { accepted: true, streamingBehavior: command.streamingBehavior ?? null }))
      if (!session.replaying) void replay(socket, session)
      return
    default:
      send(socket, fail(id, type ?? 'unknown', `mock server 不支持命令：${String(type)}`))
  }
}

/* --------------------------------- wiring --------------------------------- */

const server = createServer((request, response) => {
  const url = new URL(request.url ?? '/', `http://${request.headers.host ?? `${HOST}:${PORT}`}`)
  const json = (payload, status = 200) => {
    const body = JSON.stringify(payload)
    response.writeHead(status, {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
    })
    response.end(body)
  }

  switch (url.pathname) {
    case '/api/auth/me':
      // The mock replays the personal, account-less deployment, so the SPA renders the
      // workspace straight away instead of a login form.
      json({ authMode: 'single', user: null })
      return
    case '/api/health':
      json({ status: 'ok', version: VERSION })
      return
    case '/api/models':
      json({ models: MODELS })
      return
    case '/api/sessions':
      json({ sessions: SESSIONS })
      return
    case '/api/state':
      json({
        model: MODELS[0],
        thinkingLevel: 'medium',
        isStreaming: false,
        isCompacting: false,
        sessionFile: SESSION_FILE,
        sessionId: 'mock-session-0001',
        sessionName: 'Mock 演示会话',
        autoCompactionEnabled: false,
        messageCount: 3,
        pendingMessageCount: 0,
      })
      return
    default:
      json({ error: 'not found', path: url.pathname }, 404)
  }
})

const wss = new WebSocketServer({ noServer: true })

server.on('upgrade', (request, socket, head) => {
  const url = new URL(request.url ?? '/', `http://${request.headers.host ?? `${HOST}:${PORT}`}`)
  if (url.pathname !== '/api/ws') {
    socket.destroy()
    return
  }
  wss.handleUpgrade(request, socket, head, (client) => {
    wss.emit('connection', client, request)
  })
})

wss.on('connection', (socket) => {
  const session = createSession()
  // The Python server announces itself before the client asks anything; this
  // frame is mirrored here so the client's `ready` path is exercised too.
  send(socket, { type: 'ready', version: VERSION, cwd: '/Users/admin/git/python-pi', warnings: [] })

  socket.on('message', (raw) => {
    let command
    try {
      command = JSON.parse(raw.toString())
    } catch {
      return
    }
    if (command?.type === 'extension_ui_response') {
      const pending = session.pendingDialogs.get(command.id)
      if (pending) pending(command)
      return
    }
    if (typeof command?.type !== 'string') return
    void handleCommand(socket, session, command).catch((error) => {
      send(socket, fail(command.id, command.type, String(error)))
    })
  })

  if (AUTOPLAY) {
    setTimeout(() => {
      if (!session.replaying) void replay(socket, session)
    }, 350)
  }
})

server.listen(PORT, HOST, () => {
  process.stdout.write(`mock server listening on http://${HOST}:${PORT} (ws: /api/ws)\n`)
})

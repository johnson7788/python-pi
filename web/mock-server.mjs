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
const MOCK_TREE = {
  '/': [
    { name: '研究资料', path: '/研究资料', isDir: true, size: 0, modified: 1762000000, hidden: false },
    { name: '演示文稿', path: '/演示文稿', isDir: true, size: 0, modified: 1762000000, hidden: false },
    { name: 'notes.md', path: '/notes.md', isDir: false, size: 1280, modified: 1762000000, hidden: false },
    { name: 'protocol.txt', path: '/protocol.txt', isDir: false, size: 640, modified: 1762000000, hidden: false },
    { name: 'demo.pptx', path: '/demo.pptx', isDir: false, size: 640000, modified: 1762000000, hidden: false },
    { name: 'data.csv', path: '/data.csv', isDir: false, size: 320, modified: 1762000000, hidden: false },
  ],
  '/研究资料': [
    { name: 'evidence.md', path: '/研究资料/evidence.md', isDir: false, size: 4200, modified: 1762000000, hidden: false },
    { name: 'search-notes.md', path: '/研究资料/search-notes.md', isDir: false, size: 2100, modified: 1762000000, hidden: false },
  ],
  '/演示文稿': [
    { name: 'demo.pptx', path: '/演示文稿/demo.pptx', isDir: false, size: 640000, modified: 1762000000, hidden: false },
  ],
}
const MOCK_FILES = {
  '/notes.md': '# 研究笔记\n\n- 已纳入 12 篇文献\n- 待补充 3 篇全文\n',
  '/data.csv': 'study,sample,effect\nA,320,0.42\nB,180,0.31\n',
  '/protocol.txt': 'PICO\nP: 成人 2 型糖尿病\nI: 每周一次 GLP-1\n',
  '/研究资料/evidence.md': '# 证据表\n\n| 研究 | 样本 | 结论 |\n| --- | --- | --- |\n| A | 320 | 有效 |\n',
  '/研究资料/search-notes.md': '# 检索记录\n\n检索式：GLP-1 AND T2DM\n',
}
let MOCK_NOTIFICATIONS = [
  {
    id: 'notif-1', kind: 'schedule_success', title: '定时任务完成：每天早报',
    body: '已生成新会话', link: '/workbench/scheduled', read: false,
    createdAt: '2026-11-04T02:00:00+00:00',
  },
  {
    id: 'notif-2', kind: 'schedule_failed', title: '定时任务失败：每 2 小时巡检',
    body: 'worker is busy with another turn', link: '/workbench/scheduled', read: false,
    createdAt: '2026-11-03T16:00:00+00:00',
  },
]
let MOCK_SCHEDULES = [
  {
    id: 'sched-daily', name: '每天早报', prompt: '汇总昨天的研究进展，生成一份早报。',
    workspace: 'default', appId: 'general', modeId: 'office', kind: 'daily',
    runAt: null, hour: 9, minute: 0, weekday: null, intervalMinutes: null,
    enabled: true, nextRunAt: '2026-11-05T01:00:00+00:00', lastRunAt: '2026-11-04T01:00:00+00:00',
    lastStatus: 'success', lastError: null, createdAt: '2026-11-01T00:00:00+00:00',
  },
  {
    id: 'sched-interval', name: '每 2 小时巡检', prompt: '检查工作区里有没有新的失败日志。',
    workspace: 'default', appId: 'general', modeId: 'dev', kind: 'interval',
    runAt: null, hour: null, minute: null, weekday: null, intervalMinutes: 120,
    enabled: false, nextRunAt: null, lastRunAt: null, lastStatus: null, lastError: null,
    createdAt: '2026-11-02T00:00:00+00:00',
  },
]
const MOCK_MARKET = [
  { kind: 'skill', id: 'evidence-table', name: '证据表', description: '把纳入研究整理成可追溯的证据表。', version: '1.0.0', author: '平台团队', source: '团队', tags: ['研究', '综述'], installed: false, rating: { average: 4.6, count: 12, mine: null } },
  { kind: 'skill', id: 'meeting-notes', name: '会议纪要', description: '整理结论、决策、待办和风险。', version: '1.0.0', author: '平台团队', source: '团队', tags: ['办公'], installed: true, rating: { average: 4.2, count: 8, mine: 5 } },
  { kind: 'skill', id: 'weekly-report', name: '周报', description: '按进展、结果、问题、计划生成周报。', version: '1.0.0', author: '社区贡献', source: '社区', tags: ['汇报'], installed: false, rating: { average: 4.0, count: 5, mine: null } },
  { kind: 'plugin', id: 'hello-pi', name: 'Hello Pi', description: '最小插件示例：注册 hello_plugin 工具。', version: '0.1.0', author: '平台团队', source: '官方示例', tags: ['示例'], installed: false, rating: { average: 4.8, count: 3, mine: null } },
]
const MOCK_PLUGINS = [
  { id: 'hello-pi', name: 'Hello Pi', version: '0.1.0', description: '最小示例：注册一个 hello_plugin 工具。', capabilities: ['tools'], enabled: false },
  { id: 'literature-tools', name: '文献工具', version: '0.3.1', description: '检索、去重与整理文献的示例工具集。', capabilities: ['tools', 'commands'], enabled: true },
]
const MOCK_APPS = [
  {
    id: 'general', name: '通用工作台', description: '日常办公、开发与设计任务。', accent: '#3b5fba', builtin: true,
    modes: [
      { id: 'office', name: '办公', description: '文档、研究、汇报与日常事务。', skillBinding: 'all', prompt: '你是一名严谨的办公助理，输出结构化、可直接使用。', capsules: [
        { id: 'ppt', name: '制作 PPT', prompt: '帮我制作一份演示文稿，先给大纲。' },
        { id: 'research', name: '深度研究', prompt: '对这个主题做一次深度研究。' },
        { id: 'plan', name: '策划方案', prompt: '帮我策划一个可执行的方案。' },
        { id: 'job', name: '定时任务', prompt: '帮我设计一个定时任务。' },
      ]},
      { id: 'dev', name: '开发', description: '读代码、定位问题、补测试。', skillBinding: 'all', prompt: '你是一名资深工程师，先读代码再改，改动最小。', capsules: [
        { id: 'map', name: '读仓库结构', prompt: '先梳理仓库结构。' },
        { id: 'bug', name: '定位 bug', prompt: '根据报错定位根因。' },
      ]},
      { id: 'design', name: '设计', description: '界面与视觉方向。', skillBinding: 'all', prompt: '你是一名产品设计师，先讲场景再给方案。', capsules: [
        { id: 'proto', name: '做原型', prompt: '把这个需求做成 HTML 原型。' },
        { id: 'palette', name: '配色方案', prompt: '给这个产品定一套浅色配色。' },
      ]},
    ],
  },
  {
    id: 'research', name: '研究综述', description: '循证检索、文献筛选、证据表与综述写作。', accent: '#2f7d6b', builtin: true,
    modes: [
      { id: 'review', name: '综述', description: '从 PICO 到证据表。', skillBinding: 'all', prompt: '按循证流程工作，每个结论可追溯。', capsules: [
        { id: 'pico', name: '明确 PICO', prompt: '先把研究问题拆成 PICO。' },
        { id: 'search', name: '制定检索式', prompt: '为这个 PICO 制定中英文检索式。' },
        { id: 'evidence', name: '提取证据表', prompt: '把纳入研究整理成证据表。' },
      ]},
    ],
  },
  {
    id: 'slides', name: '演示文稿', description: '从大纲到逐页内容与讲稿。', accent: '#b56b2a', builtin: true,
    modes: [
      { id: 'deck', name: '制作', description: '先结构，再页面。', skillBinding: 'all', prompt: '先确认受众和时长，再给结构，一页一个观点。', capsules: [
        { id: 'outline', name: '10 页大纲', prompt: '给这个主题做一份 10 页大纲。' },
        { id: 'notes', name: '讲稿', prompt: '为这套幻灯片写逐页讲稿。' },
      ]},
    ],
  },
]
const MOCK_WORKSPACES = [
  { id: 'default', name: '默认工作空间', isDefault: true },
  { id: 'research', name: '研究空间', isDefault: false },
  { id: 'writing', name: '写作空间', isDefault: false },
]
const MOCK_SKILLS = [
  { name: 'literature-screening', description: '按纳入排除标准筛选研究文献。', enabled: true, disableModelInvocation: false },
  { name: 'evidence-table', description: '把关键研究整理成可追溯的证据表。', enabled: true, disableModelInvocation: false },
  { name: 'report-writer', description: '按团队模板生成结构化报告。', enabled: false, disableModelInvocation: false },
]

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

    const userMessage = { role: 'user', content: session.pendingPrompt || '请帮我看看 mock 会话', timestamp: Date.now() }
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
    const presentBlock = (session.pendingPrompt || '').toLowerCase().includes('ppt')
      ? {
          type: 'toolCall',
          id: 'call_mock_present',
          name: 'present',
          arguments: { files: [{ path: 'demo.pptx', description: '一页生日祝福 PPT' }] },
        }
      : null
    const finalAssistant = {
      role: 'assistant',
      content: [
        { type: 'thinking', thinking: THINKING_TEXT },
        { type: 'text', text: ANSWER_TEXT },
        { type: 'toolCall', id: toolCallId, name: 'read_file', arguments: toolArgs },
        ...(presentBlock ? [presentBlock] : []),
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
      session.pendingPrompt = command.message
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
    case '/api/workspaces':
      if (request.method === 'GET') {
        json({ workspaces: MOCK_WORKSPACES, canCreate: true })
        return
      }
      {
        let raw = ''
        request.on('data', (chunk) => { raw += chunk })
        request.on('end', () => {
          const body = JSON.parse(raw || '{}')
          const workspace = { id: `ws-${Date.now().toString(36)}`, name: body.name || '新空间', isDefault: false }
          MOCK_WORKSPACES.push(workspace)
          json(workspace, 201)
        })
      }
      return
    case '/api/apps':
      if (request.method === 'GET') {
        json({ apps: MOCK_APPS, defaultApp: 'general', defaultMode: 'office', canManage: true })
        return
      }
      {
        let raw = ''
        request.on('data', (chunk) => { raw += chunk })
        request.on('end', () => {
          const body = JSON.parse(raw || '{}')
          const app = { ...body, id: `app-${Date.now().toString(36)}`, builtin: false }
          MOCK_APPS.push(app)
          json(app, 201)
        })
      }
      return
    case '/api/plugins':
      json({ app: url.searchParams.get('app') || 'general', canManage: true, canInstall: true, plugins: MOCK_PLUGINS })
      return
    case '/api/market': {
      const kind = url.searchParams.get('kind') || 'skill'
      const q = (url.searchParams.get('q') || '').toLowerCase()
      const tag = url.searchParams.get('tag') || ''
      const items = MOCK_MARKET.filter((item) => item.kind === kind)
        .filter((item) => !tag || item.tags.includes(tag))
        .filter((item) =>
          !q || item.name.toLowerCase().includes(q) || item.description.toLowerCase().includes(q) || item.tags.some((entry) => entry.toLowerCase().includes(q)))
      json({ kind, tags: [...new Set(MOCK_MARKET.filter((item) => item.kind === kind).flatMap((item) => item.tags))], canInstall: true, items })
      return
    }
    case '/api/plugins/install':
      {
        const plugin = { id: 'uploaded-plugin', name: 'Uploaded Plugin', version: '0.1.0', description: '通过 zip 上传的示例插件。', capabilities: ['tools'], enabled: false }
        if (!MOCK_PLUGINS.some((item) => item.id === plugin.id)) MOCK_PLUGINS.push(plugin)
        json({ ...plugin, installed: true }, 201)
      }
      return
    case '/api/feedback':
      if (request.method === 'GET') { json({ feedback: [] }); return }
      {
        let raw = ''
        request.on('data', (chunk) => { raw += chunk })
        request.on('end', () => {
          const body = JSON.parse(raw || '{}')
          json({ timestamp: body.timestamp, rating: body.rating })
        })
      }
      return
    case '/api/notifications':
      json({ notifications: MOCK_NOTIFICATIONS, unread: MOCK_NOTIFICATIONS.filter((item) => !item.read).length })
      return
    case '/api/notifications/read-all':
      MOCK_NOTIFICATIONS = MOCK_NOTIFICATIONS.map((item) => ({ ...item, read: true }))
      json({ read: true })
      return
    case '/api/schedules':
      if (request.method === 'GET') {
        json({ schedules: MOCK_SCHEDULES })
        return
      }
      {
        let raw = ''
        request.on('data', (chunk) => { raw += chunk })
        request.on('end', () => {
          const body = JSON.parse(raw || '{}')
          const schedule = {
            id: `sched-${Date.now().toString(36)}`, name: body.name || '新任务', prompt: body.prompt || '',
            workspace: body.workspace || 'default', appId: body.appId || 'general', modeId: body.modeId || 'office',
            kind: body.kind || 'interval', runAt: body.runAt || null, hour: body.hour ?? null, minute: body.minute ?? null,
            weekday: body.weekday ?? null, intervalMinutes: body.intervalMinutes ?? null, enabled: true,
            nextRunAt: '2026-11-06T01:00:00+00:00', lastRunAt: null, lastStatus: null, lastError: null,
            createdAt: new Date().toISOString(),
          }
          MOCK_SCHEDULES.unshift(schedule)
          json(schedule, 201)
        })
      }
      return
    case '/api/artifacts/xlsx':
      json({ path: url.searchParams.get('path'), sheets: [
        { name: '证据表', rows: [['研究', '样本', '效应'], ['A', '320', '0.42'], ['B', '180', '0.31']] },
        { name: '检索记录', rows: [['数据库', '命中'], ['PubMed', '128']] },
      ] })
      return
    case '/api/artifacts/pptx':
      json({ path: url.searchParams.get('path'), slides: [
        { number: 1, texts: ['研究综述', 'GLP-1 与 2 型糖尿病'] },
        { number: 2, texts: ['研究背景', '12 篇纳入研究'] },
        { number: 3, texts: ['主要结论', '每周一次 GLP-1 有效'] },
      ] })
      return
    case '/api/artifacts':
      json({ artifacts: [
        { path: '/notes.md', name: 'notes.md', kind: 'document', extension: 'md', size: 1280, modified: 1762000100, editable: true },
        { path: '/data.csv', name: 'data.csv', kind: 'sheet', extension: 'csv', size: 320, modified: 1762000050, editable: true },
        { path: '/研究资料/evidence.md', name: 'evidence.md', kind: 'document', extension: 'md', size: 4200, modified: 1762000000, editable: true },
        { path: '/demo.pptx', name: 'demo.pptx', kind: 'slides', extension: 'pptx', size: 640000, modified: 1761999900, editable: false },
        { path: '/evidence.xlsx', name: 'evidence.xlsx', kind: 'sheet', extension: 'xlsx', size: 18000, modified: 1761999800, editable: false },
      ] })
      return
    case '/api/workspace/tree': {
      const requested = url.searchParams.get('path') || '/'
      json({ path: requested, entries: MOCK_TREE[requested] || [], truncated: false })
      return
    }
    case '/api/workspace/file': {
      const requested = url.searchParams.get('path') || '/'
      if (request.method === 'PUT') {
        json({ path: requested, size: 0 })
        return
      }
      const text = MOCK_FILES[requested]
      json({
        path: requested,
        name: requested.split('/').pop(),
        size: text ? text.length : 0,
        modified: 1762000000,
        binary: !text,
        truncated: false,
        text: text || null,
      })
      return
    }
    case '/api/workspace/directory':
      json({ path: '/新目录', created: true })
      return
    case '/api/workspace/download':
      response.writeHead(200, { 'content-type': 'text/plain; charset=utf-8' })
      response.end(MOCK_FILES[url.searchParams.get('path') || ''] || 'mock file\n')
      return
    case '/api/skills':
      json({ workspace: 'default', skills: MOCK_SKILLS })
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
    default: {
      const marketDetail = /^\/api\/market\/([^/]+)\/([^/]+)$/.exec(url.pathname)
      if (marketDetail && request.method === 'GET') {
        const item = MOCK_MARKET.find((entry) => entry.kind === marketDetail[1] && entry.id === decodeURIComponent(marketDetail[2]))
        if (!item) { json({ error: 'unknown' }, 404); return }
        json({ ...item, readme: `# ${item.name}\n\n${item.description}\n\n来源：${item.source} · 作者：${item.author}` })
        return
      }
      const marketRating = /^\/api\/market\/([^/]+)\/([^/]+)\/rating$/.exec(url.pathname)
      if (marketRating && request.method === 'POST') {
        let raw = ''
        request.on('data', (chunk) => { raw += chunk })
        request.on('end', () => {
          const score = Number(JSON.parse(raw || '{}').score || 0)
          const item = MOCK_MARKET.find((entry) => entry.kind === marketRating[1] && entry.id === decodeURIComponent(marketRating[2]))
          if (!item) { json({ error: 'unknown' }, 404); return }
          item.rating = { average: score, count: item.rating.count + (item.rating.mine ? 0 : 1), mine: score }
          json(item.rating)
        })
        return
      }
      const marketInstall = /^\/api\/market\/([^/]+)\/([^/]+)\/install$/.exec(url.pathname)
      if (marketInstall && request.method === 'POST') {
        const item = MOCK_MARKET.find((entry) => entry.id === decodeURIComponent(marketInstall[2]))
        if (item) item.installed = true
        json({ id: decodeURIComponent(marketInstall[2]), installed: true })
        return
      }
      const auditMatch = /^\/api\/apps\/([^/]+)\/audit$/.exec(url.pathname)
      if (auditMatch) {
        json({ audit: [
          { id: 'audit-1', action: 'update', actor: '本地用户', createdAt: '2026-11-04T08:00:00+00:00' },
          { id: 'audit-2', action: 'create', actor: '本地用户', createdAt: '2026-11-01T08:00:00+00:00' },
        ] })
        return
      }
      const restoreMatch = /^\/api\/apps\/([^/]+)\/restore$/.exec(url.pathname)
      if (restoreMatch && request.method === 'POST') {
        const app = MOCK_APPS.find((item) => item.id === decodeURIComponent(restoreMatch[1]))
        if (!app) { json({ error: 'unknown app' }, 404); return }
        json(app)
        return
      }
      const appMatch = /^\/api\/apps\/([^/]+)$/.exec(url.pathname)
      if (appMatch) {
        const id = decodeURIComponent(appMatch[1])
        if (request.method === 'PUT') {
          let raw = ''
          request.on('data', (chunk) => { raw += chunk })
          request.on('end', () => {
            const body = JSON.parse(raw || '{}')
            const index = MOCK_APPS.findIndex((item) => item.id === id)
            if (index < 0) { json({ error: 'unknown app' }, 404); return }
            MOCK_APPS[index] = { ...MOCK_APPS[index], ...body, id, builtin: MOCK_APPS[index].builtin }
            json(MOCK_APPS[index])
          })
          return
        }
        if (request.method === 'DELETE') {
          const index = MOCK_APPS.findIndex((item) => item.id === id)
          if (index < 0) { json({ error: 'unknown app' }, 404); return }
          MOCK_APPS.splice(index, 1)
          json({ deleted: id })
          return
        }
      }
      if (url.pathname === '/api/apps' && request.method === 'POST') {
        let raw = ''
        request.on('data', (chunk) => { raw += chunk })
        request.on('end', () => {
          const body = JSON.parse(raw || '{}')
          const app = { ...body, id: `app-${Date.now().toString(36)}`, builtin: false }
          MOCK_APPS.push(app)
          json(app, 201)
        })
        return
      }
      const notificationMatch = /^\/api\/notifications\/([^/]+)\/read$/.exec(url.pathname)
      if (notificationMatch && request.method === 'POST') {
        const id = decodeURIComponent(notificationMatch[1])
        MOCK_NOTIFICATIONS = MOCK_NOTIFICATIONS.map((item) => item.id === id ? { ...item, read: true } : item)
        json({ id, read: true })
        return
      }
      const scheduleRunMatch = /^\/api\/schedules\/([^/]+)\/run$/.exec(url.pathname)
      if (scheduleRunMatch && request.method === 'POST') {
        json({ accepted: true, id: decodeURIComponent(scheduleRunMatch[1]) }, 202)
        return
      }
      const scheduleRunsMatch = /^\/api\/schedules\/([^/]+)\/runs$/.exec(url.pathname)
      if (scheduleRunsMatch) {
        json({ runs: [
          { id: 'run-1', startedAt: '2026-11-04T01:00:00+00:00', finishedAt: '2026-11-04T01:01:00+00:00', status: 'success', sessionFile: '/tmp/s.jsonl', error: null },
        ] })
        return
      }
      const scheduleMatch = /^\/api\/schedules\/([^/]+)$/.exec(url.pathname)
      if (scheduleMatch && request.method === 'PATCH') {
        let raw = ''
        request.on('data', (chunk) => { raw += chunk })
        request.on('end', () => {
          const body = JSON.parse(raw || '{}')
          const id = decodeURIComponent(scheduleMatch[1])
          const schedule = MOCK_SCHEDULES.find((item) => item.id === id)
          if (!schedule) { json({ error: 'unknown' }, 404); return }
          schedule.enabled = Boolean(body.enabled)
          json(schedule)
        })
        return
      }
      if (scheduleMatch && request.method === 'DELETE') {
        const id = decodeURIComponent(scheduleMatch[1])
        MOCK_SCHEDULES = MOCK_SCHEDULES.filter((item) => item.id !== id)
        json({ deleted: id })
        return
      }
      const pluginDelete = /^\/api\/plugins\/([^/]+)$/.exec(url.pathname)
      if (pluginDelete && request.method === 'DELETE') {
        const id = decodeURIComponent(pluginDelete[1])
        const index = MOCK_PLUGINS.findIndex((item) => item.id === id)
        if (index < 0) { json({ error: 'unknown plugin' }, 404); return }
        MOCK_PLUGINS.splice(index, 1)
        json({ deleted: id })
        return
      }
      const pluginMatch = /^\/api\/plugins\/([^/]+)\/enable$/.exec(url.pathname)
      if (pluginMatch && request.method === 'PATCH') {
        let raw = ''
        request.on('data', (chunk) => { raw += chunk })
        request.on('end', () => {
          let enabled = true
          try { enabled = Boolean(JSON.parse(raw || '{}').enabled) } catch {}
          const plugin = MOCK_PLUGINS.find((item) => item.id === decodeURIComponent(pluginMatch[1]))
          if (!plugin) { json({ error: 'unknown plugin' }, 404); return }
          plugin.enabled = enabled
          json({ id: plugin.id, enabled, app: url.searchParams.get('app') || 'general' })
        })
        return
      }
      const match = /^\/api\/skills\/([^/]+)$/.exec(url.pathname)
      if (match && request.method === 'PATCH') {
        let raw = ''
        request.on('data', (chunk) => { raw += chunk })
        request.on('end', () => {
          let enabled = true
          try { enabled = Boolean(JSON.parse(raw || '{}').enabled) } catch {}
          const skill = MOCK_SKILLS.find((item) => item.name === decodeURIComponent(match[1]))
          if (!skill) { json({ error: 'unknown skill' }, 404); return }
          skill.enabled = enabled
          json({ name: skill.name, enabled })
        })
        return
      }
      json({ error: 'not found', path: url.pathname }, 404)
    }
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

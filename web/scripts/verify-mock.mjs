#!/usr/bin/env node
/**
 * Smoke test for the mock server, and for the Vite proxy in front of it.
 *
 * Connects like the browser does, then asserts the replay actually produces the
 * two events the streaming UI is built around: a `message_update` carrying a
 * `text_delta`, followed by an authoritative `message_end`. It also answers the
 * blocking `confirm` dialog, which proves the round-trip works.
 *
 * Usage: node scripts/verify-mock.mjs [ws://127.0.0.1:5173/api/ws]
 */

import WebSocket from 'ws'

const url = process.argv[2] ?? 'ws://127.0.0.1:5173/api/ws'
const TIMEOUT_MS = 20000

const seen = {
  ready: 0,
  response: 0,
  message_start: 0,
  message_end: 0,
  text_delta: 0,
  thinking_delta: 0,
  toolcall_start: 0,
  tool_execution_start: 0,
  tool_execution_update: 0,
  tool_execution_end: 0,
  agent_end: 0,
}

let textDeltaIndex = -1
let messageEndIndex = -1
let confirmHandled = 0
let stateResponse = 0
let index = 0
let socket

const problems = []

const finish = (code, message) => {
  process.stdout.write(`${message}\n`)
  try {
    socket?.close()
  } catch {
    // already closed
  }
  process.exit(code)
}

socket = new WebSocket(url)

socket.on('open', () => {
  process.stdout.write(`connected: ${url}\n`)
  // Exercise the request/response half of the protocol, not just the event stream.
  socket.send(JSON.stringify({ id: 'verify-get-state', type: 'get_state' }))
})

socket.on('message', (raw) => {
  let event
  try {
    event = JSON.parse(raw.toString())
  } catch {
    return
  }
  index += 1

  if (event.type === 'ready') seen.ready += 1
  if (event.type === 'response') {
    seen.response += 1
    if (event.id === 'verify-get-state' && event.command === 'get_state' && event.success) {
      stateResponse += 1
      if (event.data?.sessionId === undefined) problems.push('get_state 响应缺少 sessionId')
    }
  }
  if (event.type === 'message_start') seen.message_start += 1
  if (event.type === 'agent_end') seen.agent_end += 1
  if (event.type === 'tool_execution_start') seen.tool_execution_start += 1
  if (event.type === 'tool_execution_update') seen.tool_execution_update += 1
  if (event.type === 'tool_execution_end') seen.tool_execution_end += 1

  if (event.type === 'message_update') {
    const inner = event.assistantMessageEvent ?? {}
    if (inner.type === 'text_delta' && textDeltaIndex === -1) textDeltaIndex = index
    if (inner.type === 'text_delta') seen.text_delta += 1
    if (inner.type === 'thinking_delta') seen.thinking_delta += 1
    if (inner.type === 'toolcall_start') seen.toolcall_start += 1
  }

  if (event.type === 'message_end' && event.message?.role === 'assistant') {
    seen.message_end += 1
    if (messageEndIndex === -1) messageEndIndex = index
  }

  if (event.type === 'extension_ui_request' && event.method === 'confirm') {
    confirmHandled += 1
    socket.send(JSON.stringify({ type: 'extension_ui_response', id: event.id, confirmed: true }))
  }

  // The scripted turn is over once the agent ends; nothing else is expected.
  if (event.type === 'agent_end') {
    if (seen.text_delta < 1) problems.push('未收到任何 text_delta')
    if (seen.message_end < 1) problems.push('未收到 assistant 的 message_end')
    if (textDeltaIndex !== -1 && messageEndIndex !== -1 && messageEndIndex < textDeltaIndex) {
      problems.push('message_end 出现在 text_delta 之前')
    }
    if (seen.thinking_delta < 1) problems.push('未收到 thinking_delta')
    if (seen.tool_execution_end < 1) problems.push('未收到 tool_execution_end')
    if (confirmHandled < 1) problems.push('未收到 confirm 弹窗')
    if (seen.ready < 1) problems.push('未收到 ready 帧')
    if (seen.response < 1) problems.push('未收到任何 response')
    if (stateResponse < 1) problems.push('get_state 未返回成功响应')

    const summary = `events=${index} ${JSON.stringify(seen)} confirm=${confirmHandled} stateResponse=${stateResponse} textDelta@${textDeltaIndex} messageEnd@${messageEndIndex}`
    if (problems.length > 0) finish(1, `FAIL: ${problems.join('; ')}\n${summary}`)
    finish(0, `PASS: text_delta 后紧跟 message_end\n${summary}`)
  }
})

socket.on('error', (error) => finish(1, `FAIL: 无法连接 ${url}: ${error.message}`))
socket.on('close', () => {
  if (index === 0) finish(1, `FAIL: 连接被关闭，且未收到任何事件（${url}）`)
})

setTimeout(() => finish(1, `FAIL: ${TIMEOUT_MS}ms 内未收到 agent_end（已收到 ${index} 个事件）`), TIMEOUT_MS)

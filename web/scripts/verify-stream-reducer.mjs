/**
 * Unit checks for the transcript reducer.
 *
 * The reducer is the whole reason a stream looks the way it does: which entry a
 * delta lands in, when a message stops streaming, whether the optimistic echo
 * survives, whether a tool result attaches to its call.  It is pure, so it is
 * checked here without a socket, a DOM or React.
 *
 *   npm run verify:reducer
 */

import { INITIAL_STORE, messageText, reducer } from "../src/lib/streamReducer.ts";

const failures = [];
let assertions = 0;
const check = (name, actual, expected) => {
  assertions += 1;
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) failures.push(`${name}\n  actual:   ${a}\n  expected: ${e}`);
};

const send = (state, event) => reducer(state, { type: "server_event", event });
const act = (state, action) => reducer(state, action);

/** Text of the nth transcript entry, for the compact assertions below. */
const text = (state, index = -1) => {
  const entry = state.transcript.at(index);
  return entry ? messageText(entry.message) : null;
};

/* -------------------------------------------------------------------------- */
/* Assistant streaming                                                        */
/* -------------------------------------------------------------------------- */

{
  let state = send(INITIAL_STORE, { type: "agent_start" });
  check("agent_start marks the run running", state.agentRunning, true);

  state = send(state, { type: "message_start", message: { role: "assistant", content: [] } });
  check("one streaming entry", state.transcript.length, 1);
  check("it is streaming", state.transcript[0].streaming, true);

  state = send(state, {
    type: "message_update",
    assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "Hello" },
  });
  state = send(state, {
    type: "message_update",
    assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: " world" },
  });
  check("deltas accumulate", text(state), "Hello world");
  check("still one entry", state.transcript.length, 1);

  state = send(state, {
    type: "message_end",
    message: { role: "assistant", content: [{ type: "text", text: "Hello world" }], stopReason: "stop" },
  });
  check("message_end finalises the same entry", state.transcript.length, 1);
  check("no longer streaming", state.transcript[0].streaming, false);
  check("final text", text(state), "Hello world");

  state = send(state, { type: "agent_end" });
  check("agent_end clears the running flag", state.agentRunning, false);
}

{
  // The provider can start talking before the tab saw message_start (a reconnect).
  let state = send(INITIAL_STORE, {
    type: "message_update",
    assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "late" },
  });
  check("a delta without a start opens an entry", text(state), "late");
  check("the entry is streaming", state.transcript[0].streaming, true);
}

/* -------------------------------------------------------------------------- */
/* The optimistic user echo                                                   */
/* -------------------------------------------------------------------------- */

{
  let state = act(INITIAL_STORE, { type: "optimistic_user", key: "pending-1", text: "do the thing" });
  check("the echo is shown immediately", text(state), "do the thing");

  state = send(state, {
    type: "message_start",
    message: { role: "user", content: [{ type: "text", text: "do the thing" }] },
  });
  check("the server message replaces the echo", state.transcript.length, 1);
  check("the text survived", text(state), "do the thing");
  check("nothing is left pending", state.pendingUser.length, 0);
}

{
  // Two sends in flight: matching by text, not by position, is what keeps the
  // second echo from being dropped twice.
  let state = act(INITIAL_STORE, { type: "optimistic_user", key: "p1", text: "first" });
  state = act(state, { type: "optimistic_user", key: "p2", text: "second" });
  state = send(state, { type: "message_start", message: { role: "user", content: "second" } });
  check("both echoes still present before their messages", state.transcript.length, 2);
  check("only the matching echo was consumed", state.pendingUser.length, 1);
}

/* -------------------------------------------------------------------------- */
/* Tool calls and tool execution                                              */
/* -------------------------------------------------------------------------- */

{
  let state = send(INITIAL_STORE, { type: "message_start", message: { role: "assistant", content: [] } });
  state = send(state, {
    type: "message_update",
    assistantMessageEvent: { type: "toolcall_start", contentIndex: 0, id: "call-1", toolName: "bash" },
  });
  state = send(state, {
    type: "message_update",
    assistantMessageEvent: { type: "toolcall_delta", contentIndex: 0, delta: '{"command":' },
  });
  state = send(state, {
    type: "message_update",
    assistantMessageEvent: { type: "toolcall_delta", contentIndex: 0, delta: '"ls"}' },
  });
  const partial = state.transcript[0].message.content[0];
  check("tool name is known before the arguments finish", partial.name, "bash");
  check("raw arguments accumulate", partial.argumentsRaw, '{"command":"ls"}');

  state = send(state, {
    type: "message_update",
    assistantMessageEvent: {
      type: "toolcall_end",
      contentIndex: 0,
      toolCall: { type: "toolCall", id: "call-1", name: "bash", arguments: { command: "ls" } },
    },
  });
  const done = state.transcript[0].message.content[0];
  check("parsed arguments replace the raw tail", done.arguments, { command: "ls" });
}

{
  let state = send(INITIAL_STORE, {
    type: "tool_execution_start",
    toolCallId: "call-1",
    toolName: "bash",
    args: { command: "ls" },
  });
  check("execution is running", state.toolExecutions["call-1"].status, "running");

  state = send(state, {
    type: "tool_execution_update",
    toolCallId: "call-1",
    toolName: "bash",
    partialResult: { content: [{ type: "text", text: "half" }] },
  });
  check("partial output is kept", state.toolExecutions["call-1"].partialResult.content[0].text, "half");

  state = send(state, {
    type: "tool_execution_end",
    toolCallId: "call-1",
    toolName: "bash",
    result: { content: [{ type: "text", text: "done" }] },
    isError: false,
  });
  check("execution finished", state.toolExecutions["call-1"].status, "done");
  check("the result is the final one", state.toolExecutions["call-1"].result.content[0].text, "done");
}

{
  // A tool result whose execution events were missed still becomes a card.
  const state = send(INITIAL_STORE, {
    type: "message_end",
    message: {
      role: "toolResult",
      toolCallId: "orphan-1",
      toolName: "read",
      content: [{ type: "text", text: "file body" }],
      isError: false,
    },
  });
  check("an orphan tool result is still shown", state.toolExecutions["orphan-1"].status, "done");
}

/* -------------------------------------------------------------------------- */
/* Thinking                                                                   */
/* -------------------------------------------------------------------------- */

{
  let state = send(INITIAL_STORE, { type: "message_start", message: { role: "assistant", content: [] } });
  state = send(state, {
    type: "thinking_start",
    assistantMessageEvent: { type: "thinking_start", contentIndex: 0 },
  });
  state = send(state, {
    type: "message_update",
    assistantMessageEvent: { type: "thinking_delta", contentIndex: 0, delta: "considering" },
  });
  state = send(state, {
    type: "message_update",
    assistantMessageEvent: { type: "text_delta", contentIndex: 1, delta: "answer" },
  });
  const blocks = state.transcript[0].message.content;
  check("thinking is its own block", blocks[0], { type: "thinking", thinking: "considering" });
  check("text is separate from it", blocks[1].text, "answer");
  check("visible text excludes thinking", messageText(state.transcript[0].message), "answer");
}

/* -------------------------------------------------------------------------- */
/* Entry keys                                                                 */
/* -------------------------------------------------------------------------- */

{
  let state = send(INITIAL_STORE, { type: "message_start", message: { role: "user", content: "one" } });
  state = send(state, { type: "message_end", message: { role: "user", content: "one" } });
  state = send(state, { type: "message_start", message: { role: "user", content: "two" } });
  const keys = state.transcript.map((entry) => entry.key);
  check("keys are unique", new Set(keys).size, keys.length);
  check("the counter only moved forward", state.seq >= 2, true);
}

/* -------------------------------------------------------------------------- */

if (failures.length > 0) {
  console.error(`stream reducer: ${failures.length} of ${assertions} checks failed\n`);
  for (const failure of failures) console.error(`- ${failure}\n`);
  process.exit(1);
}
console.log(`stream reducer: ${assertions} checks passed`);

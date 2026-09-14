/**
 * Reproduces the "user message shown twice" bug against the real bundle.
 *
 * The store's optimistic echo must be dropped when the server echoes the message
 * back as `message_start`. This script mounts the app, types a message, presses
 * Enter, then replays exactly the frames the Python server emits for a prompt, and
 * reports how many user bubbles end up in the DOM.
 *
 * Usage: node scripts/verify-transcript.mjs
 */

import { readFileSync, readdirSync, existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { JSDOM, VirtualConsole } from "jsdom";

const here = dirname(fileURLToPath(import.meta.url));
const dist = resolve(here, "..", "dist");
const assets = resolve(dist, "assets");

if (!existsSync(resolve(dist, "index.html"))) {
  console.error("dist/index.html is missing — run `npm run build` first");
  process.exit(1);
}
const bundleName = readdirSync(assets).find((name) => name.endsWith(".js"));

const errors = [];
const virtualConsole = new VirtualConsole();
virtualConsole.on("jsdomError", (error) => errors.push(String(error)));
virtualConsole.on("error", (message) => errors.push(String(message)));

const model = {
  id: "demo", name: "Demo", api: "openai-completions", provider: "local",
  baseUrl: "http://127.0.0.1:1/v1", reasoning: false, input: ["text"],
  cost: { input: 1, output: 2, cacheRead: 0, cacheWrite: 0, total: 0 },
  contextWindow: 128000, maxTokens: 4096,
};
const stateData = {
  model, thinkingLevel: "off", isStreaming: false, isCompacting: false,
  steeringMode: "one-at-a-time", followUpMode: "one-at-a-time",
  sessionFile: "/tmp/demo.jsonl", sessionId: "demo-session",
  autoCompactionEnabled: true, messageCount: 0, pendingMessageCount: 0, cwd: "/tmp/demo",
};

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  url: "http://127.0.0.1:8790/", pretendToBeVisual: true, virtualConsole,
});
const { window } = dom;

/** A socket the test drives: records what the app sends, delivers frames on demand. */
const sent = [];
const sockets = [];
class FakeWebSocket {
  static OPEN = 1;
  constructor() {
    this.readyState = 1;
    sockets.push(this);
    setTimeout(() => {
      this.onopen?.();
      this.deliver({ type: "ready", version: "test", cwd: "/tmp/demo", warnings: [] });
      this.deliver({ type: "response", command: "get_state", success: true, data: stateData });
    }, 0);
  }
  deliver(frame) {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }
  send(raw) {
    sent.push(JSON.parse(raw));
  }
  close() {}
  addEventListener() {}
  removeEventListener() {}
}
window.WebSocket = FakeWebSocket;
window.fetch = async (input) => {
  const url = String(input);
  const body = url.includes("/api/models") ? { models: [model] }
    : url.includes("/api/sessions") ? { sessions: [] }
    : url.includes("/api/state") ? stateData
    : { status: "ok", version: "test", cwd: "/tmp/demo" };
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
};
window.matchMedia ??= () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
window.ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} };
window.scrollTo ??= () => {};

globalThis.window = window;
globalThis.document = window.document;
for (const name of [
  "Node", "Element", "HTMLElement", "HTMLDivElement", "HTMLSpanElement", "HTMLParagraphElement",
  "HTMLAnchorElement", "HTMLButtonElement", "HTMLTextAreaElement", "HTMLInputElement",
  "HTMLFormElement", "HTMLCollection", "NodeList", "DocumentFragment", "Text", "Comment",
  "CSSStyleDeclaration", "Range", "Selection", "DOMParser", "XMLSerializer", "MutationObserver",
  "ResizeObserver", "IntersectionObserver", "DOMRect", "DOMRectReadOnly", "DOMException",
  "Event", "CustomEvent", "MouseEvent", "KeyboardEvent", "InputEvent", "FocusEvent",
  "ClipboardEvent", "PointerEvent", "navigator", "location", "history", "localStorage",
  "sessionStorage", "getComputedStyle", "requestAnimationFrame", "cancelAnimationFrame",
  "matchMedia", "queueMicrotask", "structuredClone", "performance", "crypto", "AbortController",
  "AbortSignal", "Blob", "File", "FileReader", "FormData", "Headers", "Request", "Response",
  "URL", "URLSearchParams", "TextEncoder", "TextDecoder", "DOMTokenList",
]) {
  if (name in globalThis) continue;
  try { if (name in window) globalThis[name] = window[name]; } catch { /* accessor */ }
}

// Node 22+ ships global `WebSocket` and `fetch`, so a "skip if present" copy loop
// would leave the app talking to the real network. The bundle runs in Node's
// global scope, so these two MUST be overridden explicitly.
globalThis.WebSocket = FakeWebSocket;
globalThis.fetch = window.fetch;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitFor(predicate, timeout = 8000) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    if (predicate()) return true;
    await sleep(30);
  }
  throw new Error("timed out");
}

/** Count leaf elements whose trimmed text equals `text`. */
function leafCount(text) {
  return [...window.document.querySelectorAll("*")].filter(
    (node) => node.children.length === 0 && (node.textContent ?? "").trim() === text,
  ).length;
}

const userBubbles = leafCount;

try {
  await import(pathToFileURL(resolve(assets, bundleName)).href);
  await waitFor(() => window.document.querySelector("textarea"));
  await sleep(200);

  const textarea = window.document.querySelector("textarea");
  const setValue = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
  setValue.call(textarea, "hi");
  textarea.dispatchEvent(new window.Event("input", { bubbles: true }));
  await sleep(50);

  textarea.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  await sleep(120);
  if (userBubbles("hi") !== 1) throw new Error(`expected 1 optimistic bubble, saw ${userBubbles("hi")}`);

  const socket = sockets.at(-1);
  const ts = Date.now();

  // --- the user prompt: the server brackets every message with start/end --------
  socket.deliver({ type: "agent_start" });
  socket.deliver({ type: "turn_start" });
  socket.deliver({ type: "message_start", message: { role: "user", content: [{ type: "text", text: "hi" }], timestamp: ts } });
  socket.deliver({ type: "message_end", message: { role: "user", content: [{ type: "text", text: "hi" }], timestamp: ts } });
  await sleep(120);
  const userAfter = userBubbles("hi");

  // --- the assistant turn ------------------------------------------------------
  const assistant = (text) => ({
    role: "assistant", content: [{ type: "text", text }], api: "x", provider: "p", model: "m",
    usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2, cost: { total: 0 } },
    stopReason: "stop", timestamp: ts,
  });
  socket.deliver({ type: "message_start", message: { role: "assistant", content: [], timestamp: ts } });
  socket.deliver({ type: "message_update", assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "pong" } });
  socket.deliver({ type: "message_end", message: assistant("pong") });
  socket.deliver({ type: "turn_end", message: assistant("pong"), toolResults: [] });
  socket.deliver({ type: "agent_end", messages: [] });
  await sleep(150);
  const assistantCount = leafCount("pong");

  // --- a tool result: also start/end bracketed ---------------------------------
  socket.deliver({ type: "message_start", message: { role: "toolResult", toolCallId: "c1", toolName: "bash", content: [{ type: "text", text: "RESULT" }], isError: false, timestamp: ts } });
  socket.deliver({ type: "message_end", message: { role: "toolResult", toolCallId: "c1", toolName: "bash", content: [{ type: "text", text: "RESULT" }], isError: false, timestamp: ts } });
  await sleep(150);
  // A tool result renders as a ToolCard (<article class="message--tool">),
  // so count blocks rather than the raw text.
  const toolResultCount = window.document.querySelectorAll("article.message--tool").length;

  const rows = [
    ["user  prompt", userAfter, 1],
    ["assistant", assistantCount, 1],
    ["toolResult", toolResultCount, 1],
  ];
  let bad = false;
  for (const [label, got, want] of rows) {
    const ok = got === want;
    if (!ok) bad = true;
    console.log(`  ${ok ? "ok  " : "FAIL"} ${label.padEnd(12)} rendered=${got} expected=${want}`);
  }

  if (bad) {
    console.error("\nFAILED: a message was rendered more than once");
    console.error("  console errors:", errors.length);
    process.exit(1);
  }
  console.log("\nPASS: every bracketed message renders exactly once");
  process.exit(0);
} catch (error) {
  console.error("FAILED:", error.message);
  for (const entry of errors.slice(0, 5)) console.error("  !", entry);
  process.exit(1);
}

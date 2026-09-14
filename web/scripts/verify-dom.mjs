/**
 * Boots the built SPA in jsdom and asserts it actually renders.
 *
 * `tsc` and `vite build` prove the app compiles; the mock-server check proves the
 * protocol layer works against a real socket. Neither proves React *mounts* — a
 * crash on first render (bad hook, missing global, undefined import) would sail
 * through both. This closes that gap.
 *
 * jsdom does not execute `<script type="module">`, so instead of loading
 * `index.html` we install the DOM globals and import the built bundle directly,
 * exactly as the browser would.
 *
 * Usage: node scripts/verify-dom.mjs
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
if (!bundleName) {
  console.error("no JS bundle in dist/assets — run `npm run build` first");
  process.exit(1);
}

const errors = [];
const virtualConsole = new VirtualConsole();
virtualConsole.on("jsdomError", (error) => errors.push(String(error)));
virtualConsole.on("error", (message) => errors.push(String(message)));

const model = {
  id: "demo",
  name: "Demo",
  api: "openai-completions",
  provider: "local",
  baseUrl: "http://127.0.0.1:1/v1",
  reasoning: false,
  input: ["text"],
  cost: { input: 1, output: 2, cacheRead: 0, cacheWrite: 0, total: 0 },
  contextWindow: 128000,
  maxTokens: 4096,
};

const frames = [
  { type: "ready", version: "test", cwd: "/tmp/demo", warnings: [] },
  {
    type: "response",
    command: "get_state",
    success: true,
    data: {
      model,
      thinkingLevel: "off",
      isStreaming: false,
      isCompacting: false,
      steeringMode: "one-at-a-time",
      followUpMode: "one-at-a-time",
      sessionFile: "/tmp/demo.jsonl",
      sessionId: "demo-session",
      autoCompactionEnabled: true,
      messageCount: 0,
      pendingMessageCount: 0,
      cwd: "/tmp/demo",
    },
  },
];

const dom = new JSDOM("<!doctype html><html><body><div id=\"root\"></div></body></html>", {
  url: "http://127.0.0.1:8790/",
  pretendToBeVisual: true,
  virtualConsole,
});

const { window } = dom;

class FakeWebSocket {
  constructor() {
    this.readyState = 1;
    setTimeout(() => {
      this.onopen?.();
      for (const frame of frames) this.onmessage?.({ data: JSON.stringify(frame) });
    }, 0);
  }
  send() {}
  close() {}
  addEventListener() {}
  removeEventListener() {}
}

window.WebSocket = FakeWebSocket;
window.fetch = async (input) => {
  const url = String(input);
  const body = url.includes("/api/models")
    ? { models: [model] }
    : url.includes("/api/sessions")
      ? { sessions: [] }
      : url.includes("/api/state")
        ? frames[1].data
        : { status: "ok", version: "test", cwd: "/tmp/demo" };
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
};
window.matchMedia ??= () => ({
  matches: false,
  addEventListener() {},
  removeEventListener() {},
  addListener() {},
  removeListener() {},
});
window.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
};
window.scrollTo ??= () => {};

// Expose the DOM the way a browser does before the bundle evaluates.
//
// A blanket copy of `window` blows the stack (it holds self-referential globals
// like `self`/`top`/`parent`), so this is an explicit list of the interfaces a
// React DOM app actually touches.
globalThis.window = window;
globalThis.document = window.document;
for (const name of [
  // Nodes and DOM plumbing
  "Node", "Element", "HTMLElement", "HTMLDivElement", "HTMLSpanElement",
  "HTMLParagraphElement", "HTMLAnchorElement", "HTMLButtonElement",
  "HTMLTextAreaElement", "HTMLInputElement", "HTMLFormElement",
  "HTMLCollection", "NodeList", "DocumentFragment", "Text", "Comment",
  "CSSStyleDeclaration", "Range", "Selection", "DOMParser", "XMLSerializer",
  "MutationObserver", "ResizeObserver", "IntersectionObserver",
  "DOMRect", "DOMRectReadOnly", "DOMException",
  // Events
  "Event", "CustomEvent", "MouseEvent", "KeyboardEvent", "InputEvent",
  "FocusEvent", "ClipboardEvent", "DragEvent", "PointerEvent", "WheelEvent",
  // Platform
  "navigator", "location", "history", "localStorage", "sessionStorage",
  "getComputedStyle", "requestAnimationFrame", "cancelAnimationFrame",
  "requestIdleCallback", "cancelIdleCallback", "scrollTo", "scrollBy",
  "matchMedia", "queueMicrotask", "structuredClone", "performance", "crypto",
  "AbortController", "AbortSignal", "Blob", "File", "FileReader", "FormData",
  "Headers", "Request", "Response", "URL", "URLSearchParams",
  "TextEncoder", "TextDecoder", "DOMTokenList",
]) {
  if (name in globalThis) continue;
  try {
    if (name in window) globalThis[name] = window[name];
  } catch {
    // Accessors that need a real browsing context are simply skipped.
  }
}

// Node 22+ ships global `WebSocket` and `fetch`, so the "skip if present" loop
// above leaves the app talking to the real network. The bundle executes in Node's
// global scope, so these two must be overridden explicitly.
globalThis.WebSocket = window.WebSocket = FakeWebSocket;
globalThis.fetch = window.fetch;
// NOTE: deliberately do NOT copy `setTimeout`/`setInterval` onto globalThis.
// jsdom's window implementation calls the ambient global timer, so pointing the
// global at jsdom's own version recurses until the stack blows. Node's timers are
// perfectly good for React, and `window.setTimeout` stays jsdom's.

function waitFor(predicate, { timeout = 8000, interval = 50 } = {}) {
  return new Promise((resolvePromise, reject) => {
    const started = Date.now();
    const tick = () => {
      try {
        if (predicate()) return resolvePromise(true);
      } catch (error) {
        return reject(error);
      }
      if (Date.now() - started > timeout) return reject(new Error("timed out waiting for the UI"));
      setTimeout(tick, interval);
    };
    tick();
  });
}

const text = () => window.document.body.textContent ?? "";

try {
  await import(pathToFileURL(resolve(assets, bundleName)).href);

  await waitFor(() => window.document.querySelector("textarea") || text().length > 40);
  await waitFor(() => text().includes("Pi") || text().includes("会话"));

  const textarea = window.document.querySelector("textarea");
  if (!textarea) throw new Error("composer textarea was not rendered after mount");

  await waitFor(() => text().includes("demo") || text().includes("Demo"), { timeout: 5000 });

  console.log("DOM verification PASS");
  console.log("  bundle:", bundleName);
  console.log("  rendered text length:", text().length);
  console.log("  elements rendered:", window.document.querySelectorAll("*").length);
  console.log("  composer present:", Boolean(textarea));
  console.log("  console errors:", errors.length);
  for (const error of errors.slice(0, 5)) console.log("   !", error);
  process.exit(errors.length === 0 ? 0 : 1);
} catch (error) {
  console.error("DOM verification FAILED:", error.message);
  if (error.stack) console.error(error.stack.split("\n").slice(0, 12).join("\n"));
  console.error("  body text head:", JSON.stringify(text().slice(0, 300)));
  for (const entry of errors.slice(0, 8)) console.error("  !", entry);
  process.exit(1);
}

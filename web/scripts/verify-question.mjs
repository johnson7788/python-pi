/**
 * Boots the built SPA in jsdom on the chat route and asserts the question card
 * actually works: it renders the model's question, takes over the composer, and
 * sends a well-formed `answer_question` frame when the user answers.
 *
 * `tsc` proves the types line up and the Python suite proves the server half;
 * neither proves the two meet. This is that seam.
 *
 * Run: node scripts/verify-question.mjs   (after `npm run build`)
 */

import { existsSync, readFileSync, readdirSync } from "node:fs";
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

const QUESTION = {
  type: "user_question_request",
  callId: "call_q1",
  blocking: false,
  timeoutMs: 120000,
  questions: [
    {
      id: "kind",
      header: "报告类型",
      question: "这份报告是哪一类？",
      options: [
        { label: "工作进度汇报", description: "结论先行，汇总进展、风险、下一步计划。" },
        { label: "技术调研", description: "背景、方案对比、风险与推荐选型。" },
      ],
    },
  ],
};

const initialFrames = [
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
      messageCount: 1,
      pendingMessageCount: 0,
      pendingQuestions: [],
      cwd: "/tmp/demo",
    },
  },
  {
    type: "response",
    command: "get_messages",
    success: true,
    data: {
      messages: [
        { role: "user", content: "我想写一个报告", timestamp: 1 },
        { role: "assistant", content: [{ type: "text", text: "好的。" }], timestamp: 2 },
      ],
    },
  },
];

const dom = new JSDOM("<!doctype html><html><body><div id=\"root\"></div></body></html>", {
  // The app routes on the hash; without one it lands on the home view.
  url: "http://127.0.0.1:8790/#/workbench/chat",
  pretendToBeVisual: true,
  virtualConsole,
});
const { window } = dom;

const sent = [];
class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  constructor(url) {
    this.readyState = 1;
    this.url = url;
    window.__socket = this;
    window.__constructed = (window.__constructed ?? 0) + 1;
    setTimeout(() => {
      window.__opened = (window.__opened ?? 0) + 1;
      this.onopen?.();
      for (const frame of initialFrames) this.onmessage?.({ data: JSON.stringify(frame) });
      // Arrive after the app has mounted, like a real model turn would.
      setTimeout(() => this.onmessage?.({ data: JSON.stringify(QUESTION) }), 150);
    }, 0);
  }
  send(data) {
    let frame;
    try {
      frame = JSON.parse(data);
      sent.push(frame);
    } catch {
      sent.push({ raw: String(data) });
      return;
    }
    // A real server settles the question when the answer lands; the card closes
    // from that event, not from an optimistic local guess.
    if (frame.type === "answer_question") {
      const outcome = frame.cancelled ? "pending" : "answers";
      setTimeout(() => {
        this.onmessage?.({
          data: JSON.stringify({ type: "user_question_settled", callId: frame.callId, outcome }),
        });
        this.onmessage?.({
          data: JSON.stringify({
            type: "response",
            command: "answer_question",
            success: true,
            data: { callId: frame.callId, outcome },
          }),
        });
      }, 0);
    }
  }
  close() {}
  addEventListener() {}
  removeEventListener() {}
}

window.WebSocket = FakeWebSocket;
window.fetch = async (input) => {
  const url = String(input);
  const body = url.includes("/api/auth/me")
    ? { authMode: "single", user: null }
    : url.includes("/api/models")
      ? { models: [model] }
      : url.includes("/api/sessions")
        ? { sessions: [] }
        : url.includes("/api/workspaces")
          ? { workspaces: [{ id: "default", name: "默认" }], active: "default" }
          : url.includes("/api/apps")
            ? { apps: [] }
            : url.includes("/api/state")
              ? initialFrames[1].data
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

globalThis.window = window;
globalThis.document = window.document;
for (const name of [
  "Node", "Element", "HTMLElement", "HTMLDivElement", "HTMLSpanElement",
  "HTMLParagraphElement", "HTMLAnchorElement", "HTMLButtonElement",
  "HTMLTextAreaElement", "HTMLInputElement", "HTMLFormElement",
  "HTMLCollection", "NodeList", "DocumentFragment", "Text", "Comment",
  "CSSStyleDeclaration", "Range", "Selection", "DOMParser", "XMLSerializer",
  "MutationObserver", "ResizeObserver", "IntersectionObserver",
  "DOMRect", "DOMRectReadOnly", "DOMException",
  "Event", "CustomEvent", "MouseEvent", "KeyboardEvent", "InputEvent",
  "FocusEvent", "ClipboardEvent", "DragEvent", "PointerEvent", "WheelEvent",
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
globalThis.WebSocket = window.WebSocket = FakeWebSocket;
globalThis.fetch = window.fetch;

function waitFor(predicate, { timeout = 8000, interval = 30 } = {}) {
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
const failures = [];
const check = (condition, message) => {
  if (!condition) failures.push(message);
};

try {
  await import(pathToFileURL(resolve(assets, bundleName)).href);

  await waitFor(() => window.document.querySelector(".question-card"));
  check(text().includes("这份报告是哪一类？"), "the question text renders");
  check(text().includes("工作进度汇报"), "option labels render");
  check(text().includes("结论先行"), "option descriptions render");
  check(text().includes("报告类型"), "the header renders");

  // The card must own the composer, not float beside it.
  check(
    window.document.querySelector(".wb-composer") === null,
    "the composer is replaced while a live question exists",
  );
  await waitFor(() => sent.some((frame) => frame.type === "claim_question"));
  check(
    sent.some((frame) => frame.type === "claim_question" && frame.callId === "call_q1"),
    "the card claims the countdown",
  );

  const option = [...window.document.querySelectorAll(".question-option")].find((node) =>
    (node.textContent ?? "").includes("工作进度汇报"),
  );
  check(Boolean(option), "the first option is clickable");
  option?.click();
  await waitFor(() =>
    [...window.document.querySelectorAll("button")].some((node) =>
      (node.textContent ?? "").trim() === "提交",
    ),
  );
  const submit = [...window.document.querySelectorAll("button")].find(
    (node) => (node.textContent ?? "").trim() === "提交",
  );
  submit?.click();

  await waitFor(() =>
    sent.some((frame) => frame.type === "answer_question" && frame.callId === "call_q1"),
  );
  const answer = sent.find((frame) => frame.type === "answer_question");
  check(answer?.answers?.[0]?.id === "kind", "the answer echoes the question id");
  check(
    JSON.stringify(answer?.answers?.[0]?.selected) === JSON.stringify(["工作进度汇报"]),
    "the selected label is the answer value",
  );

  // Answering closes the card and gives the composer back.
  await waitFor(() => window.document.querySelector(".question-card") === null);
  check(
    window.document.querySelector(".wb-composer") !== null,
    "the composer returns after the question is answered",
  );

  // A dismissed timed question releases the model but stays answerable: it must
  // collapse into the banner, not vanish.
  window.__socket.onmessage?.({
    data: JSON.stringify({
      type: "user_question_request",
      callId: "call_q2",
      blocking: false,
      timeoutMs: 120000,
      questions: [
        {
          id: "target",
          header: "读者",
          question: "这份报告给谁看？",
          options: [{ label: "老板" }, { label: "同事" }],
        },
      ],
    }),
  });
  await waitFor(() => window.document.querySelector(".question-card"));
  const close = window.document.querySelector(".question-card__close");
  check(Boolean(close), "the card can be dismissed");
  close?.click();

  await waitFor(() => window.document.querySelector(".question-card") === null);
  await waitFor(() => window.document.querySelector(".question-banner"));
  check(
    (window.document.querySelector(".question-banner")?.textContent ?? "").includes("还有 1 个问题"),
    "a dismissed timed question becomes a banner",
  );
  check(
    window.document.querySelector(".wb-composer") !== null,
    "the composer returns while the question stays answerable",
  );

  const reopen = window.document.querySelector(".question-banner__open");
  reopen?.click();
  await waitFor(() => window.document.querySelector(".question-card"));
  check(true, "the banner reopens the card");

  if (errors.length > 0) failures.push(`console errors: ${errors.join(" | ")}`);
} catch (error) {
  failures.push(error instanceof Error ? error.message : String(error));
  console.error("rendered:", text().slice(0, 400));
  console.error("frames sent:", JSON.stringify(sent.map((frame) => frame.type)));
}

if (failures.length > 0) {
  console.error("question card verification failed:");
  for (const failure of failures) console.error(`  - ${failure}`);
  process.exit(1);
}
console.log("question card verification passed");
// jsdom keeps timer handles alive; the assertions are done, so end the process.
process.exit(0);

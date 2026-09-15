# python-pi Web UI

A single-page React + TypeScript front end for the python-pi coding agent. It talks
to the Python backend over **one WebSocket per tab** at `/api/ws` (pi's RPC protocol,
one JSON object per frame) plus four REST endpoints for the sidebar.

There is no router, no state library and no component library: one page, a sidebar
and a transcript. All user-facing copy is Chinese; code and comments are English.

## Requirements

- Node 24, npm 11
- The Python backend on `http://127.0.0.1:8787`, **or** the bundled mock server

## Run

```bash
npm install

# terminal 1 — backend stand-in on 127.0.0.1:8787
npm run mock

# terminal 2 — dev server (proxies /api, including the WebSocket, to 8787)
npm run dev            # http://127.0.0.1:5173
```

If port 5173 is already taken, Vite prints the port it actually used (for example
`http://127.0.0.1:5174/`).

With your own Python server running on 8787, skip `npm run mock`; everything else
is identical.

### Other scripts

```bash
npm run typecheck      # tsc --noEmit (strict)
npm run build          # typecheck + vite build -> dist/
npm run preview        # serve dist/ (no API proxy: point VITE_WS_URL at the backend)
npm run verify:mock    # headless smoke test of the mock server over /api/ws
```

## Appearance

One stylesheet, one set of tokens. Light is the default; dark follows
`prefers-color-scheme`, and the sun/moon button in the toolbar writes
`html[data-theme]` (plus `localStorage`) to override it. `applyTheme(readStoredTheme())`
runs in `main.tsx` *before* the first render, so a forced theme never flashes the
other one on load. Below 900px the sidebar slides in from the left, driven by the
menu button in the toolbar.

## Configuration

The first two are read at build/dev time from the environment:

| Variable       | Default                          | Meaning                                  |
| -------------- | -------------------------------- | ---------------------------------------- |
| `VITE_WS_URL`  | same origin, `ws(s)://host/api/ws` | WebSocket endpoint                       |
| `VITE_API_BASE`| `''` (same origin)               | prefix for `/api/health` and friends     |
| `PI_API_TARGET`| `http://127.0.0.1:8787`          | dev server only: where `/api` is proxied |

`PI_API_TARGET` matters when 8787 is already taken by a real deployment and you want
to develop against the bundled mock instead:

```bash
MOCK_PORT=8899 npm run mock                       # terminal 1
PI_API_TARGET=http://127.0.0.1:8899 npm run dev   # terminal 2
```

`mock-server.mjs` also honours `MOCK_HOST`, `MOCK_PORT` and `MOCK_AUTOPLAY=0`
(connect only, do not replay the scripted turn until a `prompt` arrives).

## Layout

```
src/
  main.tsx                 React root
  App.tsx                  page shell: sidebar + transcript + composer + overlays
  styles.css               the only stylesheet; theme lives in :root custom properties
  lib/protocol.ts          TS types for every command, event and REST payload
  lib/useAgent.ts          the WebSocket client hook: connect, send, reduce, reconnect
  lib/markdown.ts          marked + DOMPurify (sanitized HTML)
  lib/useTheme.ts          light / dark: follow the system, or a stored override
  components/
    Sidebar.tsx            sessions, 新会话, model / thinking pickers, stats, statuses
    Transcript.tsx         scrolling list + auto-follow
    Message.tsx            one message: user text, Markdown, thinking, tool calls
    ToolCard.tsx           one tool call: spinner, pretty args, live + final output
    Composer.tsx           growing textarea, Enter/Shift+Enter, steering toggle, /
    Dialog.tsx             extension select / confirm / input / editor
    Toasts.tsx             extension notify
mock-server.mjs            protocol-faithful backend stand-in
scripts/verify-mock.mjs    headless assertion over the WS endpoint
```

## How it fits together

- **`lib/protocol.ts` is the single source of truth.** Field names were taken from
  the Python implementation (`modes/rpc_mode.py`, `modes/json_event.py`,
  `extensions/rpc_ui.py`), not from prose, so a change on the wire starts here.
- **`useAgent` owns all protocol state** in one reducer. Every server frame is
  dispatched in arrival order, so deltas, `message_end` and tool events can never
  race each other in the render.
- **Streaming is delta-only.** `message_update` carries no cumulative message; the
  hook keeps the in-progress assistant message in the transcript and appends each
  `text_delta` / `thinking_delta` to the block named by `contentIndex`. The
  authoritative `message_end` then *replaces* it, which is how thinking and
  tool-call blocks get their final shape.
- **A tool call is two streams joined by `toolCallId`.** The call block lives in the
  assistant message; its status and output live in the `tool_execution_*` events.
  `ToolCard` renders the join. Standalone `toolResult` messages are hidden when the
  matching card exists, and rendered on their own when it does not (for example
  after a history reload).
- **Dialogs never block.** An extension dialog is an overlay, not a modal gate: the
  agent is parked waiting for the answer, so the transcript and composer must stay
  usable while it is open.
- **Reconnect is an exponential backoff** (0.5 s doubling, capped at 15 s) followed
  by a full re-sync (`get_state`, `get_messages`, stats, models, commands, sessions),
  so a reconnect can never leave a stale transcript on screen.

## Mock server

`npm run mock` implements the protocol surface the UI needs and, on connect, replays
one scripted turn that exercises every awkward path:

1. a `ready` frame (version + warnings), mirroring the Python server's handshake
2. a user message, then a streamed **thinking** block (`thinking_start/delta/end`)
3. streamed answer text at `contentIndex: 1` (`text_start/delta/end`)
4. a **tool call** (`toolcall_start` with `id`/`toolName`, `toolcall_delta`, `toolcall_end`)
5. the authoritative `message_end` replacing everything accumulated above
6. `tool_execution_start` → three `tool_execution_update` partials → `tool_execution_end`
7. a blocking **`confirm`** dialog that only completes when the client answers
8. `turn_end` + `agent_end`

It answers every command in the frozen protocol (`get_state`, `get_messages`,
`get_available_models`, `set_model`, `cycle_model`, `switch_session`, `fork`,
`compact`, …) and serves `/api/health`, `/api/models`, `/api/sessions`,
`/api/state`.

## Verification

```bash
npm install
npx tsc --noEmit                 # strict, no errors
npm run build                    # dist/ produced

node mock-server.mjs &           # or: npm run mock
npm run dev &
curl -s http://127.0.0.1:5173/ | head
node scripts/verify-mock.mjs ws://127.0.0.1:5173/api/ws
```

`verify-mock.mjs` connects through the Vite proxy (so it also proves `ws: true`
works), asserts that a `message_update` carrying a `text_delta` is followed by an
authoritative `message_end`, answers the `confirm` dialog and exits non-zero on any
mismatch.

## Protocol notes

Details that differ from the written brief, and how they are handled:

- `toolcall_end` carries a **`toolCall`** object (`{type,id,name,arguments}`) rather
  than a `content` string. The reducer accepts both.
- `setWidget` / `setStatus` treat a **`null`** field as "clear this entry"; a missing
  key means the record does not mention it at all. `notify` carries its level in
  `notifyType`.
- The Python server sends `{"type":"ready","version":…,"warnings":[…]}` on connect,
  which is not in pi's own vocabulary. It is handled: the version lands in the
  sidebar and warnings become dismissible inline notices.
- `GET /api/sessions` returns `modified` as an **ISO-8601 string**, not epoch millis.
  Both are accepted.
- `GET /api/state` and the `get_state` response share one payload shape, so the two
  cannot drift.

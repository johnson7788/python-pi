/**
 * Sidebar activity state for conversations the browser is not watching.
 *
 * A pi server owns one live session per user, but a conversation left mid-turn
 * keeps streaming in the background. The list endpoint reports `running`; the
 * socket reports when such a background run settles. This module turns those two
 * signals into the two states the sidebar draws:
 *
 * - `running` — a spinner before the row (the conversation is streaming now).
 * - `unread`  — a green dot after a background run finished, cleared the moment
 *   the conversation is opened.
 *
 * Kept pure (and separate from the hook) so the transitions can be tested without
 * a DOM or a socket.
 */

export interface SessionActivity {
  /** The conversation is streaming right now, whether or not it is on screen. */
  running: boolean
  /** A background run finished since the user last looked at this conversation. */
  unread: boolean
}

export type SessionActivityMap = Record<string, SessionActivity>

/** One row of `GET /api/sessions`, as far as activity is concerned. */
export interface SessionRunningRow {
  path: string
  running?: boolean
}

/**
 * Fold a fresh session list into the previous activity state.
 *
 * Two transitions matter and neither can be read off a single payload:
 * a row that *was* running and is not any more has just finished, and a row that
 * is running now must drop an older reminder. Everything else keeps its flag, so
 * the green dot survives the many polls that follow a completion.
 */
export function reconcileSessionActivity(
  previous: SessionActivityMap,
  sessions: readonly SessionRunningRow[],
  activePath: string | undefined,
): SessionActivityMap {
  const next: SessionActivityMap = {}
  for (const session of sessions) {
    const before = previous[session.path]
    const running = session.running === true
    const unread = running
      ? false
      : before?.running
        ? session.path !== activePath
        : before?.unread ?? false
    next[session.path] = { running, unread }
  }
  return next
}

/** The live session started streaming: show the spinner and drop any reminder. */
export function markSessionRunning(map: SessionActivityMap, path: string): SessionActivityMap {
  const before = map[path]
  if (before?.running && !before.unread) return map
  return { ...map, [path]: { running: true, unread: false } }
}

/**
 * A turn settled. A background conversation earns the green dot; the one on
 * screen does not, because its answer is already there to read.
 */
export function markSessionSettled(
  map: SessionActivityMap,
  path: string,
  activePath: string | undefined,
): SessionActivityMap {
  const unread = path !== activePath
  const before = map[path]
  if (before && !before.running && before.unread === unread) return map
  return { ...map, [path]: { running: false, unread } }
}

/** The user opened this conversation: the reminder has done its job. */
export function markSessionViewed(map: SessionActivityMap, path: string): SessionActivityMap {
  const before = map[path]
  if (!before || !before.unread) return map
  return { ...map, [path]: { ...before, unread: false } }
}

/**
 * Reconcile one session with an authoritative `get_state`.
 *
 * `isStreaming` from the server is the truth for the conversation the tab is
 * attached to — a reconnect may have missed the `agent_start`, and a switch may
 * have left a stale global "running" flag behind. Applying it here stops an old,
 * already-finished conversation from being optimistically marked as running (and
 * then completing into a false green dot) when another conversation is loading.
 */
export function syncSessionActivity(
  map: SessionActivityMap,
  path: string,
  streaming: boolean,
): SessionActivityMap {
  if (streaming) return markSessionRunning(map, path)
  const before = map[path]
  if (before && !before.running && !before.unread) return map
  return { ...map, [path]: { running: false, unread: false } }
}

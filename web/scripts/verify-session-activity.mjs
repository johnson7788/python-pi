/**
 * Unit checks for the sidebar's session activity state.
 *
 * The sidebar's spinner and green completion dot are derived from two signals —
 * the session list's `running` flag and the socket's settle notice — so the
 * transitions that produce them are tested here without a DOM or a socket.
 *
 *   npm run verify:session-activity
 */

import {
  markSessionRunning,
  markSessionSettled,
  markSessionViewed,
  reconcileSessionActivity,
  syncSessionActivity,
} from "../src/lib/sessionActivity.ts";

const failures = [];
let assertions = 0;
const check = (name, actual, expected) => {
  assertions += 1;
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) failures.push(`${name}\n  actual:   ${a}\n  expected: ${e}`);
};

const A = "/sessions/a.jsonl";
const B = "/sessions/b.jsonl";
const row = (path, running) => ({ path, running });

/* -------------------------------------------------------------------------- */
/* First sight                                                               */
/* -------------------------------------------------------------------------- */

check(
  "a first payload records running without inventing a reminder",
  reconcileSessionActivity({}, [row(A, false), row(B, true)], A),
  { [A]: { running: false, unread: false }, [B]: { running: true, unread: false } },
);

/* -------------------------------------------------------------------------- */
/* Running -> finished                                                       */
/* -------------------------------------------------------------------------- */

const runningElsewhere = reconcileSessionActivity({}, [row(A, true), row(B, false)], B);
check("the background row is running", runningElsewhere[A], { running: true, unread: false });

check(
  "finishing off-screen earns the green dot",
  reconcileSessionActivity(runningElsewhere, [row(A, false), row(B, false)], B)[A],
  { running: false, unread: true },
);

check(
  "finishing on-screen earns nothing",
  reconcileSessionActivity(runningElsewhere, [row(A, false), row(B, false)], A)[A],
  { running: false, unread: false },
);

const unread = reconcileSessionActivity(runningElsewhere, [row(A, false), row(B, false)], B);
check(
  "the reminder survives later idle polls",
  reconcileSessionActivity(unread, [row(A, false), row(B, false)], B)[A],
  { running: false, unread: true },
);

check(
  "a new run clears the reminder",
  reconcileSessionActivity(unread, [row(A, true), row(B, false)], B)[A],
  { running: true, unread: false },
);

check(
  "rows removed from the list drop their state",
  reconcileSessionActivity(unread, [row(B, false)], B),
  { [B]: { running: false, unread: false } },
);

/* -------------------------------------------------------------------------- */
/* Socket settle notice                                                      */
/* -------------------------------------------------------------------------- */

check(
  "a settled background run is unread",
  markSessionSettled({}, A, B)[A],
  { running: false, unread: true },
);
check(
  "a settled run on screen is not unread",
  markSessionSettled({}, A, A)[A],
  { running: false, unread: false },
);

const settled = markSessionSettled({}, A, B);
check("an identical settle is a no-op", markSessionSettled(settled, A, B), settled);

const started = markSessionRunning(unread, A);
check("starting a run clears the reminder", started[A], { running: true, unread: false });
check("starting an already-running run is a no-op", markSessionRunning(started, A), started);

/* -------------------------------------------------------------------------- */
/* Opening                                                                   */
/* -------------------------------------------------------------------------- */

const read = markSessionViewed(settled, A);
check("opening the row clears the reminder", read[A], { running: false, unread: false });
check("viewing an unknown row is a no-op", markSessionViewed({}, A), {});
check(
  "viewing one row leaves the others alone",
  markSessionViewed({ [A]: { running: false, unread: true }, [B]: { running: false, unread: true } }, A)[B],
  { running: false, unread: true },
);

/* -------------------------------------------------------------------------- */
/* get_state resynchronisation                                               */
/* -------------------------------------------------------------------------- */

// The regression: after switching away from a still-running conversation the
// global "agentRunning" flag stayed true. Later switches marked an old, finished
// row as running, and the next poll turned that into a fake green dot.
check(
  "get_state says streaming: the row runs and drops its reminder",
  syncSessionActivity({ [A]: { running: false, unread: true } }, A, true)[A],
  { running: true, unread: false },
);
check(
  "get_state says idle: a stale running flag is cleared, not completed",
  syncSessionActivity({ [A]: { running: true, unread: false } }, A, false)[A],
  { running: false, unread: false },
);
check(
  "get_state says idle: an on-screen reminder is cleared",
  syncSessionActivity({ [A]: { running: false, unread: true } }, A, false)[A],
  { running: false, unread: false },
);
check(
  "get_state says idle twice is a no-op",
  (() => {
    const idle = syncSessionActivity({ [A]: { running: true, unread: false } }, A, false);
    return syncSessionActivity(idle, A, false);
  })(),
  { [A]: { running: false, unread: false } },
);

/* -------------------------------------------------------------------------- */

if (failures.length > 0) {
  console.error(`Session activity checks FAILED (${failures.length} of ${assertions})`);
  for (const failure of failures) console.error(" -", failure);
  process.exit(1);
}
console.log(`Session activity checks PASS (${assertions} assertions)`);

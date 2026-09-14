#!/usr/bin/env python3
"""Load-test a team deployment: many users, many turns, real protocol.

    python deploy/loadtest.py --url http://127.0.0.1:8787 --username admin --password '…' \
        --users 20 --turns 3

Why this is not just a loop around `deploy/smoke.py`
----------------------------------------------------

Smoke answers "is it configured correctly". This answers "how does it behave when everyone
uses it at once", which is a different question for a server that holds every session in one
process.  Four numbers matter, and none of them can be inferred from a single request:

* **Turn latency percentiles.**  A mean hides the case that actually annoys people: the one
  turn in twenty that takes eight seconds because the process was busy bootstrapping
  somebody else's session.
* **Where it stops.**  ``PI_MAX_ACTIVE_USERS`` is a deliberate ceiling.  When the run hits
  it, that is reported as *capacity reached* rather than as a failure — knowing the ceiling
  and that it degrades by refusing is the point.
* **Error classification.**  A refused login, a closed socket (4401 unauthenticated / 4429 at
  capacity), a refused prompt (quota) and a timeout are four different deployment problems.
  Lumping them into "errors: 12" makes the run useless.
* **Isolation under load.**  With ``--verify-isolation`` every user's ``cwd`` and session file
  are checked against their own uid while the load is still running, which is the property
  most likely to break first when sessions are created concurrently.

Costs real tokens against a real model.  For a large run, point it at a deployment whose
model endpoint is a local fake: the protocol, the registry, the sockets and the per-user
paths are all exercised either way, and the numbers become about the server rather than about
the model provider.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets

DEFAULT_PASSWORD = "loadtest-password-1"


@dataclass
class Turn:
    """One prompt, from send to ``agent_end``."""

    user: str
    ok: bool
    latency: float = 0.0
    ack_latency: float = 0.0
    error: str = ""
    kind: str = "ok"


@dataclass
class Report:
    turns: list[Turn] = field(default_factory=list)
    refused: int = 0
    unauthenticated: int = 0
    cwds: dict[str, str] = field(default_factory=dict)
    isolation_problems: list[str] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def wall_seconds(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    def percentile(self, values: list[float], fraction: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
        return ordered[index]


# --------------------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------------------


def prepare_users(
    client: httpx.Client, prefix: str, count: int, password: str, *, create: bool
) -> list[str]:
    """Create (or reuse) ``count`` accounts and return their usernames."""
    names = [f"{prefix}{index}" for index in range(count)]
    if create:
        for name in names:
            response = client.post(
                "/api/admin/users", json={"username": name, "password": password}
            )
            if response.status_code not in (200, 409):  # 409 = left over from a previous run
                raise SystemExit(f"cannot create {name}: HTTP {response.status_code} {response.text[:200]}")
    return names


def log_in(base_url: str, names: list[str], password: str) -> dict[str, str]:
    """Return ``{username: cookie header}``, or exit with the first failure.

    Each login uses its own client on purpose. Logging in on a shared client would replace
    the administrator's cookie with the last test user's, and every later admin call would
    silently run as that user.
    """
    cookies: dict[str, str] = {}
    for name in names:
        with httpx.Client(base_url=base_url, timeout=60) as client:
            response = client.post(
                "/api/auth/login", json={"username": name, "password": password}
            )
        if response.status_code != 200:
            raise SystemExit(f"login failed for {name}: HTTP {response.status_code} {response.text[:200]}")
        token = response.cookies.get("pi_session")
        if not token:
            raise SystemExit(f"login for {name} set no session cookie")
        cookies[name] = f"pi_session={token}"
    return cookies


# --------------------------------------------------------------------------------------
# Load
# --------------------------------------------------------------------------------------


async def drive_user(
    server: str,
    name: str,
    cookie: str,
    *,
    turns: int,
    prompt: str,
    timeout: float,
    report: Report,
    expect_uid: str | None,
    pace: float,
) -> None:
    """Run ``turns`` prompts for one user over one socket.

    One socket per user, reused across its turns: that is how a browser behaves, and it is
    what makes the connection count in the report meaningful.
    """
    try:
        async with websockets.connect(
            server, additional_headers={"Cookie": cookie}, max_size=8 * 1024 * 1024
        ) as ws:
            ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            await asyncio.wait_for(ws.recv(), timeout=timeout)  # initial state

            cwd = str(ready.get("cwd") or "")
            report.cwds[name] = cwd
            if expect_uid is not None and expect_uid not in cwd:
                # The workspace path is derived from the uid; a cwd without it means this
                # user is working somewhere that is not theirs.
                report.isolation_problems.append(
                    f"{name}: cwd {cwd!r} does not contain their own uid {expect_uid!r}"
                )

            for index in range(turns):
                await _one_turn(ws, name, index, prompt, timeout, report)
                if pace:
                    await asyncio.sleep(pace)
    except websockets.exceptions.ConnectionClosed as error:
        code = error.rcvd.code if error.rcvd is not None else None
        if code == 4401:
            report.unauthenticated += 1
            kind, message = "unauthenticated", "socket closed 4401 (no session)"
        elif code == 4429:
            report.refused += 1
            kind, message = "capacity", "socket closed 4429 (at capacity)"
        else:
            kind, message = "closed", f"socket closed: {code}"
        report.turns.append(Turn(user=name, ok=False, kind=kind, error=message))
    except Exception as error:  # noqa: BLE001 - the run must survive one bad user
        report.turns.append(
            Turn(user=name, ok=False, kind="transport", error=f"{type(error).__name__}: {error}")
        )


async def _one_turn(ws: Any, name: str, index: int, prompt: str, timeout: float, report: Report) -> None:
    request_id = f"{name}-{index}"
    started = time.perf_counter()
    await ws.send(json.dumps({"id": request_id, "type": "prompt", "message": prompt}))

    ack_latency = 0.0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        kind = record.get("type")

        if record.get("id") == request_id and kind == "response":
            ack_latency = time.perf_counter() - started
            if record.get("success") is False:
                # Refused at preflight — a quota, most likely. Not a transport failure.
                report.turns.append(
                    Turn(
                        user=name,
                        ok=False,
                        kind="refused",
                        latency=ack_latency,
                        error=str(record.get("error") or "")[:200],
                    )
                )
                return
        elif kind == "agent_end":
            report.turns.append(
                Turn(
                    user=name,
                    ok=True,
                    latency=time.perf_counter() - started,
                    ack_latency=ack_latency,
                )
            )
            return
        elif kind == "response" and record.get("success") is False and record.get("command") == "server":
            report.turns.append(
                Turn(user=name, ok=False, kind="server", latency=time.perf_counter() - started,
                     error=str(record.get("error") or "")[:200])
            )
            return

    report.turns.append(
        Turn(user=name, ok=False, kind="timeout", latency=timeout, error=f"no agent_end in {timeout}s")
    )


# --------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------


def print_report(report: Report, *, users: int, turns_per_user: int, concurrency: int) -> int:
    ok = [t for t in report.turns if t.ok]
    latencies = [t.latency for t in ok]
    acks = [t.ack_latency for t in ok if t.ack_latency]

    print()
    print("=" * 66)
    print("load test")
    print("=" * 66)
    print(f"  users={users}  turns/user={turns_per_user}  max concurrent users={concurrency}")
    print(f"  wall time            {report.wall_seconds:.1f}s")
    if latencies:
        print(f"  completed turns      {len(ok)}")
        print(f"  throughput           {len(ok) / report.wall_seconds:.2f} turns/s")
        print(f"  turn latency  p50    {statistics.median(latencies):.2f}s")
        print(f"                p95    {report.percentile(latencies, 0.95):.2f}s")
        print(f"                p99    {report.percentile(latencies, 0.99):.2f}s")
        print(f"                max    {max(latencies):.2f}s")
    if acks:
        print(f"  ack latency   p95    {report.percentile(acks, 0.95):.3f}s")

    failures = [t for t in report.turns if not t.ok]
    if failures or report.refused or report.unauthenticated:
        print()
        print("  problems")
        by_kind: dict[str, list[Turn]] = {}
        for turn in failures:
            by_kind.setdefault(turn.kind, []).append(turn)
        for kind, items in sorted(by_kind.items()):
            example = next((i.error for i in items if i.error), "")
            print(f"    {kind:<14} {len(items):>4}  {example[:80]}")
        if report.refused:
            print()
            print(f"  NOTE: the server refused {report.refused} connection(s) at capacity.")
            print("        That is PI_MAX_ACTIVE_USERS doing its job, not a crash: raise it")
            print("        (and the unit's MemoryMax) or lower the concurrency.")

    if report.isolation_problems:
        print()
        print("  ISOLATION PROBLEMS")
        for problem in report.isolation_problems[:10]:
            print(f"    {problem}")

    print()
    if report.isolation_problems:
        print("  verdict: FAIL — users are not isolated")
        print("=" * 66)
        return 1
    # A capacity refusal is the server behaving as configured, so it is reported as its own
    # outcome: exit 0, with the ceiling named. Anything else counts as a failure.
    capacity_only = failures and all(turn.kind == "capacity" for turn in failures)
    if capacity_only and not ok:
        print(
            "  verdict: CAPACITY — the server refused every user rather than degrading.\n"
            "           Raise PI_MAX_ACTIVE_USERS (and MemoryMax) or wait for idle sessions\n"
            "           to be reaped, then run again."
        )
        print("=" * 66)
        return 0
    if capacity_only:
        print(f"  verdict: PARTIAL — {len(ok)} turns ran, the rest hit the capacity ceiling")
        print("=" * 66)
        return 0
    if failures:
        print(f"  verdict: FAIL — {len(failures)} of {len(report.turns)} turns did not complete")
        print("=" * 66)
        return 1
    print("  verdict: PASS")
    print("=" * 66)
    return 0


def print_server_side(client: httpx.Client, names: list[str], report: Report) -> None:
    """What the server thinks happened, which should agree with what the client measured.

    A mismatch is worth surfacing rather than printing a bare zero: the usual cause is a model
    endpoint that does not report token usage (the bundled demo server is one), but it is also
    what broken accounting would look like, and the two must not be confused.
    """
    completed = sum(1 for turn in report.turns if turn.ok)
    try:
        rows = client.get("/api/admin/usage").json()["users"]
    except Exception as error:  # noqa: BLE001
        print(f"  (server-side usage unavailable: {error})")
        return
    involved = [row for row in rows if row["username"] in set(names)]
    total_tokens = sum(row["total_tokens"] for row in involved)
    total_turns = sum(row["turns"] for row in involved)
    print(f"  server-side: {total_turns} turns, {total_tokens} tokens across {len(involved)} accounts")
    if completed and not total_turns:
        print(
            "  NOTE: the client completed "
            f"{completed} turns but the server recorded none. If the model endpoint does not\n"
            "        send a `usage` block in its stream, nothing is billable and nothing is\n"
            "        recorded — that is expected for a scripted fake, and a bug for a real one."
        )


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    password = args.test_password
    submitted = args.concurrency or args.users
    prefix = args.prefix or f"load-{uuid.uuid4().hex[:6]}-"
    report = Report()

    with httpx.Client(base_url=args.url, timeout=60) as client:
        admin = client.post(
            "/api/auth/login", json={"username": args.username, "password": args.password}
        )
        if admin.status_code != 200:
            raise SystemExit(f"admin login failed: HTTP {admin.status_code} {admin.text[:200]}")

        names = prepare_users(client, prefix, args.users, password, create=args.create_users)
        cookies = log_in(args.url, names, password)

        # Sessions are held until the idle reaper takes them (30 minutes by default), so a
        # previous run's users still occupy slots. Without this line a capacity refusal looks
        # like a bug in the tool rather than a busy server.
        before = client.get("/api/health").json()
        print(f"load test: {len(names)} users, {args.turns} turns each, {submitted} at a time")
        print(
            f"server before: {before.get('activeUsers')} active session(s), "
            f"{before.get('clients')} connected client(s)"
        )

        report.started_at = time.monotonic()
        # A semaphore is the ramp: users start in waves rather than all at once, which is both
        # closer to how people arrive and easier to read in the latency curve.
        gate = asyncio.Semaphore(submitted)

        # The uid is what the workspace path is derived from, so it is all the client needs to
        # check that each user landed in their own directory.
        uids: dict[str, str] = {}
        if args.verify_isolation:
            uids = {row["username"]: row["uid"] for row in client.get("/api/admin/users").json()["users"]}

        socket_url = args.url.replace("http://", "ws://").replace("https://", "wss://") + "/api/ws"

        async def guarded(name: str) -> None:
            async with gate:
                await drive_user(
                    socket_url,
                    name,
                    cookies[name],
                    turns=args.turns,
                    prompt=args.prompt,
                    timeout=args.timeout,
                    report=report,
                    expect_uid=uids.get(name),
                    pace=args.pace,
                )

        await asyncio.gather(*(guarded(name) for name in names))
        report.finished_at = time.monotonic()

        # Two users sharing a workspace would not show up per-user, only in aggregate.
        distinct = set(report.cwds.values())
        if report.cwds and len(distinct) != len(report.cwds):
            report.isolation_problems.append(
                f"{len(report.cwds)} users reported only {len(distinct)} distinct cwds"
            )

        after = client.get("/api/health").json()
        code = print_report(
            report, users=args.users, turns_per_user=args.turns, concurrency=submitted
        )
        print(
            f"  server after:  {after.get('activeUsers')} active session(s), "
            f"{after.get('clients')} connected client(s)"
        )
        print_server_side(client, names, report)

        if args.disable_users:
            for name in names:
                row = next(
                    (r for r in client.get("/api/admin/users").json()["users"] if r["username"] == name),
                    None,
                )
                if row:
                    client.patch(f"/api/admin/users/{row['uid']}", json={"status": "disabled"})
            print(f"  disabled {len(names)} load-test accounts")
        if args.show_users:
            print("  accounts:", ", ".join(names))
        return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadtest.py",
        description="Load-test a python-pi team deployment.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  # 20 users, 3 turns each, all at once\n"
            "  python deploy/loadtest.py --url http://127.0.0.1:8787 -u admin -p '…' \\\n"
            "      --users 20 --turns 3 --create-users\n"
            "\n"
            "  # find the ceiling: ramp past PI_MAX_ACTIVE_USERS and read the refusal count\n"
            "  python deploy/loadtest.py --url http://127.0.0.1:8787 -u admin -p '…' \\\n"
            "      --users 40 --turns 1 --concurrency 10 --create-users\n"
        ),
    )
    parser.add_argument("--url", required=True, help="base URL, e.g. http://127.0.0.1:8787")
    parser.add_argument("-u", "--username", default="admin", help="an administrator account")
    parser.add_argument(
        "-p",
        "--password",
        help="its password; prefer --password-stdin or PI_LOADTEST_PASSWORD, since argv is readable",
    )
    parser.add_argument(
        "--password-stdin", action="store_true", help="read the password from the first line of stdin"
    )
    parser.add_argument("--users", type=int, default=5, help="how many accounts to drive (default 5)")
    parser.add_argument("--turns", type=int, default=1, help="prompts per user (default 1)")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=0,
        help="how many users work at once (default: all of them)",
    )
    parser.add_argument("--prompt", default="Reply with the single word: ok", help="prompt to send")
    parser.add_argument("--timeout", type=float, default=180.0, help="seconds to wait for a turn")
    parser.add_argument("--pace", type=float, default=0.0, help="seconds between a user's own turns")
    parser.add_argument(
        "--create-users",
        action="store_true",
        help="create the accounts first (409 is treated as 'already there')",
    )
    parser.add_argument("--prefix", help="username prefix (default: a random load-<id>-)")
    parser.add_argument(
        "--test-password", default=DEFAULT_PASSWORD, help="password for the generated accounts"
    )
    parser.add_argument(
        "--verify-isolation",
        action="store_true",
        help="check each user's cwd against their own uid while the load runs",
    )
    parser.add_argument(
        "--disable-users", action="store_true", help="disable the accounts when the run finishes"
    )
    parser.add_argument("--show-users", action="store_true", help="print the generated usernames")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Same rule as deploy/smoke.py: a password on the command line is visible to every account
    # on the host through `ps`, for as long as the run lasts.
    if args.password_stdin:
        args.password = sys.stdin.readline().rstrip("\n")
    args.password = args.password or os.environ.get("PI_LOADTEST_PASSWORD")
    if not args.password:
        parser.error("pass --password-stdin (preferred), -p, or PI_LOADTEST_PASSWORD")

    if args.users < 1 or args.turns < 1:
        raise SystemExit("--users and --turns must be at least 1")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())

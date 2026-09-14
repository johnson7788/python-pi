"""Many users at once: isolation, attribution, and the capacity ceiling.

Everything here is about *more than one* user, which is where a single-tenant design leaks.
Four properties are worth pinning, and each of them is silent when it breaks:

**Routing.**  Every event a browser receives must belong to that user's session. A broadcast
that fans out to the wrong worker would show one colleague another's transcript — the failure
would look like a UI glitch, not a leak.

**Filesystem.**  Workspaces and transcripts are per-uid. The test asserts the *paths* each
user is told about, not just that the requests succeeded.

**Attribution.**  Usage and audit rows must carry the right uid. Misfiled usage is invisible
until someone is billed for a colleague's work, and misfiled audit is worthless exactly when
it is needed.

**Capacity.**  One process holds every session, so the ceiling is a deliberate design
parameter (``PI_MAX_ACTIVE_USERS``), and it must refuse rather than degrade.

The turns run concurrently against a fake model, so this is deterministic and free; the
protocol is real, and so is the WebSocket.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from conftest import (
    TEAM_ADMIN_PASSWORD,
    TEAM_USER_PASSWORD,
    cookie_header,
    login,
    reset_fake_model,
    start_team_server,
    team_server,  # noqa: F401 - re-exported so pytest can see the fixture
)

#: Enough to exercise concurrency without making the suite slow. Five is also more than the
#: two-user case that a naive implementation passes by accident.
USERS = 5


def create_members(admin: httpx.Client, usernames: list[str]) -> dict[str, str]:
    """Create accounts and return ``{username: uid}``."""
    uids: dict[str, str] = {}
    for name in usernames:
        response = admin.post(
            "/api/admin/users", json={"username": name, "password": TEAM_USER_PASSWORD}
        )
        assert response.status_code == 200, response.text
        uids[name] = response.json()["user"]["uid"]
    return uids


async def prompt_concurrently(
    server, cookies: dict[str, str], message: str = "hello"
) -> dict[str, dict]:
    """Open one WebSocket per user, prompt them all at once, and collect each stream.

    All sockets are opened before any prompt is sent, and the prompts are sent in one pass —
    that is what makes this a concurrency test rather than a queue of sequential ones.
    """
    import websockets

    results: dict[str, dict] = {name: {"records": [], "error": None} for name in cookies}

    async def one(name: str, cookie: str) -> None:
        try:
            async with websockets.connect(
                server.ws_url, additional_headers={"Cookie": cookie}
            ) as ws:
                ready = json.loads(await ws.recv())
                state = json.loads(await ws.recv())
                results[name]["ready"] = ready
                results[name]["state"] = state

                await ws.send(json.dumps({"id": f"p-{name}", "type": "prompt", "message": message}))
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    record = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                    results[name]["records"].append(record)
                    if record.get("type") == "agent_end":
                        return
                raise AssertionError(f"{name}: no agent_end")
        except Exception as error:  # noqa: BLE001 - reported through the result, not raised
            results[name]["error"] = f"{type(error).__name__}: {error}"

    await asyncio.gather(*(one(name, cookie) for name, cookie in cookies.items()))
    return results


def test_users_run_at_the_same_time_without_interfering(fake_openai, tmp_path):
    """The core multi-user property: everyone works, nobody sees anyone else."""
    _, base_url = fake_openai
    server = start_team_server(tmp_path, base_url)
    try:
        admin = login(server, "admin", TEAM_ADMIN_PASSWORD)
        try:
            names = [f"user{i}" for i in range(USERS)]
            uids = create_members(admin, names)
            clients = {name: login(server, name, TEAM_USER_PASSWORD) for name in names}
            try:
                reset_fake_model()
                results = asyncio.run(
                    prompt_concurrently(server, {n: cookie_header(c) for n, c in clients.items()})
                )

                for name in names:
                    result = results[name]
                    assert result["error"] is None, f"{name}: {result['error']}"
                    kinds = [r.get("type") for r in result["records"]]
                    assert "agent_start" in kinds and "agent_end" in kinds, (name, kinds)

                    # The path it was told is its own, and the transcript it got is its own.
                    assert result["ready"]["cwd"] == str(server.data_root / "workspaces" / uids[name])
                    assert str(server.data_root / "agents" / uids[name]) in result["state"]["data"]["sessionFile"]

                    # The prompt was acknowledged, not dropped under load.
                    acks = [r for r in result["records"] if r.get("id") == f"p-{name}"]
                    assert acks and acks[0]["success"] is True, (name, acks)

                # No stream may contain another user's session id: a fan-out bug would show up
                # here rather than as a mysterious UI glitch.
                session_ids = {
                    name: results[name]["state"]["data"]["sessionId"] for name in names
                }
                assert len(set(session_ids.values())) == len(names), "sessions are shared"
                for name, session_id in session_ids.items():
                    others = {sid for other, sid in session_ids.items() if other != name}
                    assert session_id not in others
            finally:
                for client in clients.values():
                    client.close()
        finally:
            admin.close()
    finally:
        server.stop()


def test_each_user_sees_only_their_own_workspace_and_sessions(fake_openai, tmp_path):
    """Isolation is asserted on the paths, not merely on the requests succeeding."""
    _, base_url = fake_openai
    server = start_team_server(tmp_path, base_url)
    try:
        admin = login(server, "admin", TEAM_ADMIN_PASSWORD)
        try:
            names = ["ada", "bob", "cy"]
            uids = create_members(admin, names)
            clients = {name: login(server, name, TEAM_USER_PASSWORD) for name in names}
            try:
                # Each writes a file with a name nobody else uses.
                for name in names:
                    response = clients[name].put(
                        "/api/workspace/file",
                        params={"path": f"/{name}.txt"},
                        json={"content": f"written by {name}"},
                    )
                    assert response.status_code == 200, response.text

                for name in names:
                    entries = clients[name].get("/api/workspace/tree").json()["entries"]
                    assert [e["name"] for e in entries] == [f"{name}.txt"], name

                    sessions = clients[name].get("/api/sessions").json()["sessions"]
                    for listed in sessions:
                        assert str(server.data_root / "agents" / uids[name]) in listed["path"]

                # The file on disk is where that user's own workspace is, and nowhere else.
                for name in names:
                    expected = server.data_root / "workspaces" / uids[name] / f"{name}.txt"
                    assert expected.read_text(encoding="utf-8") == f"written by {name}"
            finally:
                for client in clients.values():
                    client.close()
        finally:
            admin.close()
    finally:
        server.stop()


def test_usage_and_audit_are_attributed_to_the_right_user(fake_openai, tmp_path):
    """Misfiled usage bills the wrong person; misfiled audit is worthless when it matters."""
    _, base_url = fake_openai
    server = start_team_server(tmp_path, base_url)
    try:
        admin = login(server, "admin", TEAM_ADMIN_PASSWORD)
        try:
            names = ["ada", "bob"]
            uids = create_members(admin, names)
            clients = {name: login(server, name, TEAM_USER_PASSWORD) for name in names}
            try:
                # One user runs a turn; the other only logs in. If attribution were broken,
                # both would show activity (or the wrong one would).
                reset_fake_model()
                results = asyncio.run(
                    prompt_concurrently(
                        server, {"ada": cookie_header(clients["ada"])}, message="go"
                    )
                )
                assert results["ada"]["error"] is None

                ada_usage = clients["ada"].get("/api/usage/me").json()["summary"]
                bob_usage = clients["bob"].get("/api/usage/me").json()["summary"]
                assert ada_usage["total_tokens"] > 0
                assert bob_usage.get("total_tokens", 0) == 0
                assert bob_usage.get("turns", 0) == 0

                by_user = {row["uid"]: row for row in admin.get("/api/admin/usage").json()["users"]}
                assert by_user[uids["ada"]]["total_tokens"] > 0
                assert by_user[uids["bob"]]["total_tokens"] == 0

                audit = admin.get("/api/admin/audit").json()["records"]
                assert audit, "expected at least one audited tool call"
                assert {row["uid"] for row in audit} == {uids["ada"]}, (
                    "audit rows are not attributed to the user who ran them"
                )
            finally:
                for client in clients.values():
                    client.close()
        finally:
            admin.close()
    finally:
        server.stop()


def test_the_capacity_ceiling_refuses_instead_of_degrading(fake_openai, tmp_path):
    """One process holds every session, so the ceiling is deliberate and must be visible.

    Note what the limit counts: a *live session*, not a connection. Closing a browser does not
    free a slot — only the idle reaper does, which is why the refusal is a clear error with a
    retry hint rather than a silent queue.
    """
    import websockets

    _, base_url = fake_openai
    server = start_team_server(tmp_path, base_url, extra_env={"PI_MAX_ACTIVE_USERS": "2"})
    try:
        admin = login(server, "admin", TEAM_ADMIN_PASSWORD)
        try:
            names = ["one", "two", "three"]
            create_members(admin, names)

            async def open_session(name: str) -> dict:
                client = login(server, name, TEAM_USER_PASSWORD)
                try:
                    async with websockets.connect(
                        server.ws_url, additional_headers={"Cookie": cookie_header(client)}
                    ) as ws:
                        ready = json.loads(await ws.recv())
                        if ready.get("type") == "ready":
                            return {"name": name, "opened": True}
                        return {"name": name, "opened": False, "record": ready}
                except websockets.exceptions.ConnectionClosed as error:
                    return {
                        "name": name,
                        "opened": False,
                        "code": error.rcvd.code if error.rcvd is not None else None,
                    }
                finally:
                    client.close()

            async def scenario() -> list[dict]:
                return [await open_session(name) for name in names]

            outcomes = asyncio.run(scenario())
            opened = [o for o in outcomes if o["opened"]]
            refused = [o for o in outcomes if not o["opened"]]

            assert len(opened) == 2, outcomes
            assert len(refused) == 1, outcomes
            # 4429 is this server's "at capacity" close code.
            assert refused[0].get("code") == 4429, refused[0]
        finally:
            admin.close()
    finally:
        server.stop()


def test_one_users_failure_does_not_disturb_the_others(fake_openai, tmp_path):
    """A shared process must not let one account's problem become everybody's.

    The quota is the cheapest way to make one user fail on demand: their turn is refused at
    preflight while their neighbours keep working.
    """
    _, base_url = fake_openai
    server = start_team_server(tmp_path, base_url)
    try:
        admin = login(server, "admin", TEAM_ADMIN_PASSWORD)
        try:
            uids = create_members(admin, ["blocked", "fine"])
            assert (
                admin.patch(
                    f"/api/admin/users/{uids['blocked']}", json={"monthlyTokenQuota": 0}
                ).status_code
                == 200
            )

            blocked = login(server, "blocked", TEAM_USER_PASSWORD)
            fine = login(server, "fine", TEAM_USER_PASSWORD)
            try:
                reset_fake_model()
                results = asyncio.run(
                    prompt_concurrently(
                        server,
                        {"blocked": cookie_header(blocked), "fine": cookie_header(fine)},
                        message="go",
                    )
                )

                # The blocked user is refused at preflight: no turn, no tokens.
                refusals = [
                    r for r in results["blocked"]["records"] if r.get("id") == "p-blocked"
                ]
                assert refusals and refusals[0]["success"] is False
                assert "额度" in refusals[0]["error"]
                assert not any(
                    r.get("type") == "agent_start" for r in results["blocked"]["records"]
                )
                assert blocked.get("/api/usage/me").json()["summary"].get("turns", 0) == 0

                # The neighbour is unaffected.
                assert results["fine"]["error"] is None
                assert any(r.get("type") == "agent_end" for r in results["fine"]["records"])
                assert fine.get("/api/usage/me").json()["summary"]["total_tokens"] > 0
            finally:
                blocked.close()
                fine.close()
        finally:
            admin.close()
    finally:
        server.stop()

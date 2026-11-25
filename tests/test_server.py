"""Tests for the browser UI server.

These run the **real server** — a uvicorn subprocess — and connect with a real
WebSocket client, because the point is that a browser speaks the same protocol as
``pi --mode rpc``.  Only the model is faked (a loopback OpenAI-compatible server),
so nothing here needs an API key or the internet.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import websockets
from conftest import FakeOpenAIHandler, write_agent_config

REPO_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class UiServer:
    """A running ``pi-py-web`` subprocess."""

    def __init__(self, proc: subprocess.Popen, port: int) -> None:
        self.proc = proc
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self.ws_url = f"ws://127.0.0.1:{port}/api/ws"

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)

    def stderr(self) -> str:
        if self.proc.stderr is None:
            return ""
        try:
            return self.proc.stderr.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return ""


def _start_server(tmp_path: Path, base_url: str, *extra: str) -> tuple[UiServer, Path, Path]:
    agent_dir = tmp_path / "agent"
    workdir = tmp_path / "work"
    workdir.mkdir()
    write_agent_config(agent_dir, base_url)

    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pi_server.cli",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--cwd",
            str(workdir),
            "--agent-dir",
            str(agent_dir),
            "--model",
            "local/local-test",
            *extra,
        ],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    server = UiServer(proc, port)

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"server exited early:\n{server.stderr()}")
        try:
            response = httpx.get(f"{server.base_url}/api/health", timeout=1.0)
            if response.status_code == 200:
                return server, workdir, agent_dir
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.1)
    server.stop()
    raise AssertionError(f"server never became healthy:\n{server.stderr()}")


@pytest.fixture()
def ui_server(fake_openai, tmp_path):
    _, base_url = fake_openai
    server, workdir, agent_dir = _start_server(tmp_path, base_url)
    try:
        yield server, workdir, agent_dir
    finally:
        server.stop()


# --------------------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------------------


def test_health_models_state_and_sessions(ui_server):
    server, workdir, _ = ui_server

    health = httpx.get(f"{server.base_url}/api/health", timeout=10).json()
    assert health["status"] == "ok"
    assert health["cwd"] == str(workdir)

    models = httpx.get(f"{server.base_url}/api/models", timeout=10).json()["models"]
    assert any(model["id"] == "local-test" for model in models)
    assert any(model["api"] == "openai-completions" for model in models)

    state = httpx.get(f"{server.base_url}/api/state", timeout=10).json()
    assert state["model"]["id"] == "local-test"
    assert state["isStreaming"] is False
    assert state["cwd"] == str(workdir)

    sessions = httpx.get(f"{server.base_url}/api/sessions", timeout=10).json()["sessions"]
    assert isinstance(sessions, list)


def test_root_reports_a_missing_build(ui_server):
    """Without a built SPA the server still explains itself instead of 404ing."""
    server, _, _ = ui_server
    response = httpx.get(f"{server.base_url}/", timeout=10)
    if response.headers.get("content-type", "").startswith("application/json"):
        assert response.status_code == 503
        assert "not built" in response.json()["error"]
    else:
        assert response.status_code == 200  # a build exists in this checkout


# --------------------------------------------------------------------------------------
# The command protocol over a real WebSocket
# --------------------------------------------------------------------------------------


async def _recv_until(ws, predicate, timeout: float = 30.0, limit: int = 800) -> list[dict]:
    seen: list[dict] = []
    deadline = time.monotonic() + timeout
    for _ in range(limit):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"timed out; got {[r.get('type') for r in seen]}")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        record = json.loads(raw)
        seen.append(record)
        if predicate(record):
            return seen
    raise AssertionError(f"never saw it; got {[r.get('type') for r in seen]}")


def test_websocket_turn_with_a_real_tool(ui_server):
    server, workdir, agent_dir = ui_server

    async def scenario() -> list[dict]:
        async with websockets.connect(server.ws_url) as ws:
            ready = json.loads(await ws.recv())
            assert ready["type"] == "ready"
            assert ready["cwd"] == str(workdir)

            initial_state = json.loads(await ws.recv())
            assert initial_state["command"] == "get_state"
            assert initial_state["data"]["model"]["id"] == "local-test"

            await ws.send(json.dumps({"id": "p1", "type": "prompt", "message": "create the file"}))
            return await _recv_until(ws, lambda r: r.get("type") == "agent_end")

    records = asyncio.run(scenario())

    kinds = [record.get("type") for record in records]
    assert "agent_start" in kinds and "turn_end" in kinds

    # `prompt` acknowledges at preflight, exactly like pi's RPC mode.
    acks = [r for r in records if r.get("id") == "p1"]
    assert acks and acks[0]["success"] is True and acks[0]["command"] == "prompt"

    # The real built-in tool ran against the real cwd.
    assert (workdir / "e2e.txt").read_text(encoding="utf-8") == "written by e2e"

    updates = [r for r in records if r.get("type") == "message_update"]
    assert updates
    for update in updates:
        # ``seq`` is the resume offset every WebSocket record now carries; the rest
        # of the shape is the RPC delta-only contract.
        assert set(update) == {"type", "usage", "assistantMessageEvent", "seq"}
        assert isinstance(update["seq"], int) and update["seq"] > 0
        assert "partial" not in update["assistantMessageEvent"]

    assert any(r.get("type") == "tool_execution_start" and r.get("toolName") == "write" for r in records)
    assert any(r.get("type") == "tool_execution_end" and r.get("isError") is False for r in records)

    # A v3 session file landed under the agent dir.
    sessions = list((agent_dir / "sessions").rglob("*.jsonl"))
    assert len(sessions) == 1
    assert json.loads(sessions[0].read_text(encoding="utf-8").splitlines()[0])["version"] == 3


def test_websocket_recovers_from_a_malformed_frame(ui_server):
    server, _, _ = ui_server

    async def scenario() -> tuple[dict, dict]:
        async with websockets.connect(server.ws_url) as ws:
            await ws.recv()  # ready
            await ws.recv()  # state

            await ws.send("{not json}")
            parsed = (await _recv_until(ws, lambda r: r.get("command") == "parse"))[-1]

            await ws.send(json.dumps({"id": "after", "type": "get_state"}))
            after = (await _recv_until(ws, lambda r: r.get("id") == "after"))[-1]
            return parsed, after

    parsed, after = asyncio.run(scenario())
    assert parsed["success"] is False
    assert after["success"] is True


def test_websocket_session_replacement_keeps_streaming(ui_server):
    server, workdir, agent_dir = ui_server

    async def scenario() -> tuple[str, dict, list[dict]]:
        async with websockets.connect(server.ws_url) as ws:
            await ws.recv()  # ready
            first_file = json.loads(await ws.recv())["data"]["sessionFile"]

            await ws.send(json.dumps({"id": "n1", "type": "new_session"}))
            (await _recv_until(ws, lambda r: r.get("id") == "n1"))[-1]

            await ws.send(json.dumps({"id": "s2", "type": "get_state"}))
            second_state = (await _recv_until(ws, lambda r: r.get("id") == "s2"))[-1]["data"]

            # Events must still arrive after the session object was swapped.
            await ws.send(json.dumps({"id": "p2", "type": "prompt", "message": "again"}))
            records = await _recv_until(ws, lambda r: r.get("type") == "agent_end")
            return first_file, second_state, records

    first_file, second_state, records = asyncio.run(scenario())
    assert second_state["messageCount"] == 0
    assert second_state["sessionFile"] != first_file
    assert any(r.get("type") == "message_update" for r in records)


def test_state_endpoint_reflects_the_shared_session(ui_server):
    """One session per process: HTTP sees what the WebSocket did."""
    server, workdir, _ = ui_server

    async def prompt_once() -> None:
        async with websockets.connect(server.ws_url) as ws:
            await ws.recv()
            await ws.recv()
            await ws.send(json.dumps({"id": "p", "type": "prompt", "message": "go"}))
            await _recv_until(ws, lambda r: r.get("type") == "agent_end")

    asyncio.run(prompt_once())

    state = httpx.get(f"{server.base_url}/api/state", timeout=10).json()
    # user + assistant + toolResult + assistant
    assert state["messageCount"] == 4
    assert state["isStreaming"] is False

    # The sidebar draws its spinner from this flag, so every row must carry it even
    # when nothing is running.
    listing = httpx.get(f"{server.base_url}/api/sessions?scope=all", timeout=10).json()["sessions"]
    assert listing, "the session that just ran is listed"
    assert all(session["running"] is False for session in listing)


def test_sessions_endpoint_reports_a_turn_in_flight(ui_server):
    """The sidebar's spinner is driven by `running` on the list endpoint, and the
    flag has to move while a turn is actually streaming — not only afterwards.

    The first turn is a warm-up: a session file only appears after the first
    assistant message (empty sessions deliberately leave no file to list), so the
    observed turn is the session's second.
    """
    server, _, _ = ui_server

    async def scenario() -> tuple[list[dict], list[dict]]:
        async with websockets.connect(server.ws_url) as ws:
            await ws.recv()  # ready
            await ws.recv()  # get_state
            await ws.send(json.dumps({"id": "warm", "type": "prompt", "message": "hello"}))
            await _recv_until(ws, lambda r: r.get("type") == "agent_end")

            # Stall the fake model so the next turn is still in flight when read.
            FakeOpenAIHandler.delay = 0.6
            try:
                await ws.send(json.dumps({"id": "slow", "type": "prompt", "message": "slow"}))
                await _recv_until(ws, lambda r: r.get("type") == "agent_start")
                await asyncio.sleep(0.2)
                in_flight = httpx.get(
                    f"{server.base_url}/api/sessions?scope=all", timeout=10
                ).json()["sessions"]
                await _recv_until(ws, lambda r: r.get("type") == "agent_end", timeout=60)
            finally:
                FakeOpenAIHandler.delay = 0.0
        settled = httpx.get(
            f"{server.base_url}/api/sessions?scope=all", timeout=10
        ).json()["sessions"]
        return in_flight, settled

    in_flight, settled = asyncio.run(scenario())
    assert any(row["running"] for row in in_flight), "a streaming turn is reported running"
    assert all(row["running"] is False for row in settled), "the flag clears when the turn ends"


def test_a_retired_turn_stays_running_and_announces_its_settle(ui_server):
    """Switching away mid-turn must not cancel the run, and the sidebar has to see
    both halves of it: running while it finishes, then a settle notice naming the
    conversation (which the client turns into the green completion dot)."""
    server, _, _ = ui_server

    def sessions() -> list[dict]:
        return httpx.get(f"{server.base_url}/api/sessions?scope=all", timeout=10).json()["sessions"]

    async def scenario() -> tuple[str, list[dict], dict, list[dict]]:
        async with websockets.connect(server.ws_url) as ws:
            await ws.recv()  # ready
            old_path = json.loads(await ws.recv())["data"]["sessionFile"]

            # Warm up: an empty session leaves no file to list.
            await ws.send(json.dumps({"id": "warm", "type": "prompt", "message": "hello"}))
            await _recv_until(ws, lambda r: r.get("type") == "agent_end")

            FakeOpenAIHandler.delay = 0.6
            try:
                await ws.send(json.dumps({"id": "slow", "type": "prompt", "message": "slow"}))
                await _recv_until(ws, lambda r: r.get("type") == "agent_start")
                # Navigate away while the model is still answering: the run is retired,
                # not cancelled.
                await ws.send(json.dumps({"id": "n", "type": "new_session"}))
                await _recv_until(ws, lambda r: r.get("id") == "n")
                in_flight = sessions()
                records = await _recv_until(
                    ws, lambda r: r.get("type") == "session_activity", timeout=30
                )
            finally:
                FakeOpenAIHandler.delay = 0.0
        return old_path, in_flight, records[-1], sessions()

    old_path, in_flight, settled_event, after = asyncio.run(scenario())

    def find_row(rows: list[dict]) -> dict:
        return next(item for item in rows if item["path"] == old_path)

    assert find_row(in_flight)["running"] is True, "the abandoned run still counts as running"
    assert settled_event["sessionFile"] == old_path
    assert settled_event["running"] is False
    assert find_row(after)["running"] is False, "the background run has finished"


def test_abort_is_prompt_and_clears_running(ui_server):
    """Stop must not feel like a hang, and the list flag must clear once it stops."""
    server, _, _ = ui_server

    async def scenario() -> tuple[float, list[dict], list[dict]]:
        async with websockets.connect(server.ws_url) as ws:
            await ws.recv()  # ready
            await ws.recv()  # get_state
            await ws.send(json.dumps({"id": "warm", "type": "prompt", "message": "hello"}))
            await _recv_until(ws, lambda r: r.get("type") == "agent_end")

            FakeOpenAIHandler.delay = 2.0
            try:
                await ws.send(json.dumps({"id": "slow", "type": "prompt", "message": "slow"}))
                await _recv_until(ws, lambda r: r.get("type") == "agent_start")
                started = time.monotonic()
                await ws.send(json.dumps({"id": "a", "type": "abort"}))
                records = await _recv_until(ws, lambda r: r.get("id") == "a", timeout=30)
                elapsed = time.monotonic() - started
            finally:
                FakeOpenAIHandler.delay = 0.0
        listing = httpx.get(f"{server.base_url}/api/sessions?scope=all", timeout=10).json()["sessions"]
        return elapsed, records, listing

    elapsed, records, listing = asyncio.run(scenario())
    types = [record.get("type") for record in records]
    assert "agent_end" in types, "abort must end the turn, not hang it"
    # The provider stream is polled for aborts every 0.5s; the old code waited for
    # response headers first (the fake model sleeps 2s), which is the regression.
    assert elapsed < 1.5, f"abort took {elapsed:.2f}s"
    assert all(row["running"] is False for row in listing), "the list flag clears after abort"

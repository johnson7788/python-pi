"""Shared fixtures.

The only external dependency any test has is a model server; this provides a local
fake one that speaks the streaming chat-completions wire format, so the whole stack
can be exercised without an API key or network access.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from pi_server.config import COOKIE_NAME


def sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def chunk(delta: dict, finish_reason: str | None = None) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "local-test",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


class FakeOpenAIHandler(BaseHTTPRequestHandler):
    """Serves one tool-calling turn, then one textual turn.

    The tool arguments are split across two deltas on purpose, so streamed
    argument assembly is exercised over a real socket.
    """

    calls = 0
    bodies: list[dict] = []

    def log_message(self, *args) -> None:  # keep test output clean
        pass

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).bodies.append(body)

        type(self).calls += 1
        payload = b""
        if type(self).calls == 1:
            payload += sse(
                chunk(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "write", "arguments": '{"path": "e2e.txt", '},
                            }
                        ],
                    }
                )
            )
            payload += sse(
                chunk(
                    {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '"content": "written by e2e"}'}}
                        ]
                    }
                )
            )
            payload += sse(chunk({}, finish_reason="tool_calls"))
        else:
            payload += sse(chunk({"role": "assistant", "content": "All done."}))
            payload += sse(chunk({}, finish_reason="stop"))
        payload += b"data: [DONE]\n\n"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.send_error(404)


@pytest.fixture()
def fake_openai():
    """A loopback OpenAI-compatible server; yields ``(server, base_url)``."""
    FakeOpenAIHandler.calls = 0
    FakeOpenAIHandler.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def write_agent_config(agent_dir: Path, base_url: str) -> None:
    """Create an agent dir with a credential and a local model pointing at ``base_url``."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "auth.json").write_text(
        json.dumps({"local": {"type": "api_key", "key": "test-key"}}), encoding="utf-8"
    )
    (agent_dir / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "local": {
                        "name": "Local Test",
                        "api": "openai-completions",
                        "baseUrl": base_url,
                        "models": [
                            {
                                "id": "local-test",
                                "name": "Local Test Model",
                                "reasoning": False,
                                "input": ["text"],
                                "contextWindow": 128000,
                                "maxTokens": 4096,
                                "cost": {"input": 1.0, "output": 2.0},
                            }
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------------------
# Team-mode server harness
#
# Shared by every test module that needs a real ``pi-py-web`` in ``--auth-mode team``:
# the workspace API (M2) and the governance layer (M3) both have to log a user in first.
# --------------------------------------------------------------------------------------

#: Credentials the harness provisions.  Weak on purpose — the server hashes them like any
#: other password; nothing here is a secret.
TEAM_ADMIN_PASSWORD = "admin-password-1"
TEAM_USER_PASSWORD = "user-password-1"

REPO_ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class TeamServer:
    """A running ``pi-py-web`` in team mode, plus its data root."""

    def __init__(self, proc: subprocess.Popen, port: int, data_root: Path) -> None:
        self.proc = proc
        self.port = port
        self.data_root = data_root
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

    def workspace(self, uid: str) -> Path:
        return self.data_root / "workspaces" / uid


def start_team_server(tmp_path: Path, base_url: str, *extra: str) -> TeamServer:
    """Boot the server in team mode against a fake model and a fresh data root."""
    agent_dir = tmp_path / "agent"
    workdir = tmp_path / "work"
    workdir.mkdir()
    write_agent_config(agent_dir, base_url)

    data_root = tmp_path / "data"
    # Team mode gives every user their own agent dir, so the provider catalog has to come
    # from the shared template.  `write_agent_config` also drops an auth.json there; the
    # seeder deliberately copies models.json only, which this setup exercises.
    write_agent_config(data_root / "template", base_url)

    port = free_port()
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
            # The shared company credential (D3): every user's session is handed this one.
            "--api-key",
            "shared-company-key",
            "--auth-mode",
            "team",
            "--data-root",
            str(data_root),
            "--admin-username",
            "admin",
            "--admin-password",
            TEAM_ADMIN_PASSWORD,
            *extra,
        ],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    server = TeamServer(proc, port, data_root)

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"server exited early:\n{server.stderr()}")
        try:
            if httpx.get(f"{server.base_url}/api/health", timeout=1.0).status_code == 200:
                return server
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.1)
    server.stop()
    raise AssertionError(f"server never became healthy:\n{server.stderr()}")


@pytest.fixture()
def team_server(fake_openai, tmp_path):
    _, base_url = fake_openai
    server = start_team_server(tmp_path, base_url)
    try:
        yield server
    finally:
        server.stop()


def login(server: TeamServer, username: str, password: str) -> httpx.Client:
    """A client that keeps the session cookie, like a browser."""
    client = httpx.Client(base_url=server.base_url, timeout=10)
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return client


def cookie_header(client: httpx.Client) -> str:
    return f"{COOKIE_NAME}={client.cookies[COOKIE_NAME]}"

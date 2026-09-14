"""End-to-end test of the real CLI against a local fake OpenAI-compatible server.

Nothing is mocked inside the process under test: a subprocess runs the actual
``pi-py`` entry point, which loads settings, resolves a model from ``models.json``,
reads credentials from ``auth.json``, builds the real tools, calls the provider
adapter over loopback HTTP, executes the ``write`` tool, and persists a session.

The only stand-in is the model server, which speaks the streaming chat-completions
wire format.  This is the closest we can get to a live run without an API key.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def _chunk(delta: dict, finish_reason: str | None = None) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "local-test",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


class _Handler(BaseHTTPRequestHandler):
    """Serves one tool-calling turn, then one textual turn."""

    calls = 0
    bodies: list[dict] = []

    def log_message(self, *args) -> None:  # keep the test output clean
        pass

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).bodies.append(body)

        type(self).calls += 1
        first = type(self).calls == 1

        payload = b""
        if first:
            # The tool arguments arrive split across two deltas on purpose, so the
            # test also proves streamed argument assembly works over a real socket.
            payload += _sse(
                _chunk(
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
            payload += _sse(
                _chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": '"content": "written by e2e"}'},
                            }
                        ]
                    }
                )
            )
            payload += _sse(_chunk({}, finish_reason="tool_calls"))
        else:
            payload += _sse(_chunk({"role": "assistant", "content": "All done."}))
            payload += _sse(_chunk({}, finish_reason="stop"))
        payload += b"data: [DONE]\n\n"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.send_error(404)


@pytest.fixture()
def fake_server():
    _Handler.calls = 0
    _Handler.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _write_agent_config(agent_dir: Path, base_url: str) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "auth.json").write_text(json.dumps({"local": {"type": "api_key", "key": "test-key"}}))
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
        )
    )


def _run_cli(agent_dir: Path, workdir: Path, *extra: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pi_coding_agent.cli",
            "--agent-dir",
            str(agent_dir),
            "--cwd",
            str(workdir),
            "--model",
            "local/local-test",
            *extra,
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def test_cli_runs_a_full_tool_calling_turn_against_a_local_provider(fake_server, tmp_path):
    server, base_url = fake_server
    agent_dir = tmp_path / "agent"
    workdir = tmp_path / "work"
    workdir.mkdir()
    _write_agent_config(agent_dir, base_url)

    result = _run_cli(agent_dir, workdir, "create e2e.txt")

    assert result.returncode == 0, f"stderr:\n{result.stderr}\nstdout:\n{result.stdout}"

    # The real tool ran and produced a real file in the requested cwd.
    created = workdir / "e2e.txt"
    assert created.read_text(encoding="utf-8") == "written by e2e"

    # stdout carries assistant text only.
    assert "All done." in result.stdout

    # Two provider round trips: the tool call, then the closing answer.
    assert _Handler.calls == 2

    # The first request carried the tool schemas and the system prompt.
    first_request = _Handler.bodies[0]
    tool_names = {t["function"]["name"] for t in first_request.get("tools", [])}
    assert {"read", "bash", "edit", "write"} <= tool_names
    assert any(m["role"] == "system" for m in first_request["messages"])

    # A session file was persisted under the agent dir.
    sessions = list((agent_dir / "sessions").rglob("*.jsonl"))
    assert len(sessions) == 1
    lines = sessions[0].read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert header["type"] == "session" and header["version"] == 3
    assert header["cwd"] == str(workdir)
    roles = [json.loads(line)["message"]["role"] for line in lines[1:]]
    assert roles == ["user", "assistant", "toolResult", "assistant"]


def test_cli_json_mode_emits_delta_only_events(fake_server, tmp_path):
    server, base_url = fake_server
    agent_dir = tmp_path / "agent"
    workdir = tmp_path / "work"
    workdir.mkdir()
    _write_agent_config(agent_dir, base_url)

    result = _run_cli(agent_dir, workdir, "create e2e.txt", "--mode", "json")

    assert result.returncode == 0, f"stderr:\n{result.stderr}"

    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert lines[0]["type"] == "session"
    assert lines[0]["model"] == "local-test"

    updates = [line for line in lines if line["type"] == "message_update"]
    assert updates, "expected at least one streaming update"
    for update in updates:
        # Delta-only: the cumulative message and partial must not be emitted.
        assert "message" not in update
        assert "partial" not in update["assistantMessageEvent"]

    assert any(line["type"] == "agent_end" for line in lines)


def test_cli_reports_missing_credentials_before_calling_the_provider(tmp_path):
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "local": {
                        "api": "openai-completions",
                        "baseUrl": "http://127.0.0.1:1/v1",
                        "models": [{"id": "local-test", "contextWindow": 1000, "maxTokens": 100}],
                    }
                }
            }
        )
    )
    workdir = tmp_path / "work"
    workdir.mkdir()

    result = _run_cli(agent_dir, workdir, "hello")

    assert result.returncode != 0
    assert "no credentials" in (result.stderr + result.stdout).lower()


# --------------------------------------------------------------------------------------
# RPC mode over real pipes
# --------------------------------------------------------------------------------------


class _JsonlFeed:
    """Reads LF-delimited records from a subprocess pipe on a background thread."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self._lines: "queue.Queue[dict | None]" = queue.Queue()
        threading.Thread(target=self._pump, name="rpc-reader", daemon=True).start()

    def _pump(self) -> None:
        buffer = b""
        while True:
            chunk = self._stream.read1(4096) if hasattr(self._stream, "read1") else self._stream.read(1)
            if not chunk:
                self._lines.put(None)
                return
            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                text = raw.decode("utf-8", errors="replace")
                self._lines.put(json.loads(text.rstrip("\r")))

    def next(self, timeout: float = 30.0) -> dict | None:
        return self._lines.get(timeout=timeout)

    def until(self, predicate, timeout: float = 30.0) -> list[dict]:
        """Collect records until ``predicate`` matches; returns everything seen."""
        seen: list[dict] = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"timed out waiting for record; saw {[r['type'] for r in seen]}")
            record = self._lines.get(timeout=remaining)
            if record is None:
                raise AssertionError(f"stream ended early; saw {[r['type'] for r in seen]}")
            seen.append(record)
            if predicate(record):
                return seen


def _start_rpc(agent_dir: Path, workdir: Path) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pi_coding_agent.cli",
            "--agent-dir",
            str(agent_dir),
            "--cwd",
            str(workdir),
            "--model",
            "local/local-test",
            "--mode",
            "rpc",
        ],
        cwd=str(REPO_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def _send(proc: subprocess.Popen, payload: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
    proc.stdin.flush()


def test_rpc_mode_serves_commands_over_pipes(fake_server, tmp_path):
    server, base_url = fake_server
    agent_dir = tmp_path / "agent"
    workdir = tmp_path / "work"
    workdir.mkdir()
    _write_agent_config(agent_dir, base_url)

    proc = _start_rpc(agent_dir, workdir)
    try:
        feed = _JsonlFeed(proc.stdout)

        # 1. A state query answers with the resolved model.
        _send(proc, {"id": "state-1", "type": "get_state"})
        response = feed.until(lambda r: r.get("id") == "state-1")
        state = response[-1]
        assert state["type"] == "response" and state["success"] is True
        assert state["data"]["model"]["id"] == "local-test"
        assert state["data"]["isStreaming"] is False

        # 2. A prompt is acknowledged at preflight, then streamed as events.
        _send(proc, {"id": "prompt-1", "type": "prompt", "message": "create e2e-rpc.txt"})
        records = feed.until(lambda r: r.get("type") == "agent_end")

        prompt_response = [r for r in records if r.get("id") == "prompt-1"]
        assert prompt_response and prompt_response[0]["success"] is True
        assert prompt_response[0]["command"] == "prompt"

        kinds = [r["type"] for r in records]
        assert "agent_start" in kinds and "turn_end" in kinds
        assert any(r["type"] == "tool_execution_start" and r["toolName"] == "write" for r in records)

        # The real tool ran against the real cwd.  The fake server scripts a write
        # of ``e2e.txt`` regardless of the prompt text.
        assert (workdir / "e2e.txt").read_text(encoding="utf-8") == "written by e2e"

        # 3. Cumulative usage rides along on every delta, no live partial.
        updates = [r for r in records if r["type"] == "message_update"]
        assert updates
        for update in updates:
            assert set(update) == {"type", "usage", "assistantMessageEvent"}
            assert "partial" not in update["assistantMessageEvent"]

        # 4. Stats reflect the persisted turn.
        _send(proc, {"id": "stats-1", "type": "get_session_stats"})
        stats = feed.until(lambda r: r.get("id") == "stats-1")[-1]
        assert stats["data"]["userMessages"] == 1
        assert stats["data"]["assistantMessages"] == 2
        assert stats["data"]["toolResults"] == 1

        # 5. A malformed line is reported without killing the session.
        assert proc.stdin is not None
        proc.stdin.write(b"{not json}\n")
        proc.stdin.flush()
        parsed = feed.until(lambda r: r.get("command") == "parse")
        assert parsed[-1]["success"] is False

        _send(proc, {"id": "still-alive", "type": "get_state"})
        alive = feed.until(lambda r: r.get("id") == "still-alive")[-1]
        assert alive["success"] is True

        # 6. Closing stdin shuts the process down cleanly.
        proc.stdin.close()
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)

    # The session written over RPC is a normal v3 session file.
    sessions = list((agent_dir / "sessions").rglob("*.jsonl"))
    assert len(sessions) == 1
    lines = sessions[0].read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["version"] == 3


def test_rpc_mode_reports_unsupported_commands(fake_server, tmp_path):
    server, base_url = fake_server
    agent_dir = tmp_path / "agent"
    workdir = tmp_path / "work"
    workdir.mkdir()
    _write_agent_config(agent_dir, base_url)

    proc = _start_rpc(agent_dir, workdir)
    try:
        feed = _JsonlFeed(proc.stdout)
        _send(proc, {"id": "n1", "type": "export_html"})
        response = feed.until(lambda r: r.get("id") == "n1")[-1]
        assert response["success"] is False
        assert "not supported" in response["error"]

        _send(proc, {"id": "u1", "type": "totally_unknown"})
        unknown = feed.until(lambda r: r.get("id") == "u1")[-1]
        assert unknown["success"] is False
        assert "Unknown command" in unknown["error"]

        assert proc.stdin is not None
        proc.stdin.close()
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def test_rpc_mode_replaces_the_session_and_keeps_streaming(fake_server, tmp_path):
    """new_session must swap the live session and re-attach event forwarding."""
    server, base_url = fake_server
    agent_dir = tmp_path / "agent"
    workdir = tmp_path / "work"
    workdir.mkdir()
    _write_agent_config(agent_dir, base_url)

    proc = _start_rpc(agent_dir, workdir)
    try:
        feed = _JsonlFeed(proc.stdout)

        # One real turn so the original session has history and a file.
        _send(proc, {"id": "p1", "type": "prompt", "message": "first"})
        feed.until(lambda r: r.get("type") == "agent_end")
        _send(proc, {"id": "s1", "type": "get_state"})
        first_state = feed.until(lambda r: r.get("id") == "s1")[-1]["data"]
        assert first_state["messageCount"] == 4  # user + assistant + toolResult + assistant
        first_file = first_state["sessionFile"]

        # Replace it.
        _send(proc, {"id": "n1", "type": "new_session"})
        replaced = feed.until(lambda r: r.get("id") == "n1")[-1]
        assert replaced["success"] is True
        assert replaced["data"] == {"cancelled": False}

        _send(proc, {"id": "s2", "type": "get_state"})
        second_state = feed.until(lambda r: r.get("id") == "s2")[-1]["data"]
        assert second_state["messageCount"] == 0
        assert second_state["sessionFile"] != first_file

        # Events still reach the client after the replacement (rebinding worked).
        _send(proc, {"id": "p2", "type": "prompt", "message": "second"})
        records = feed.until(lambda r: r.get("type") == "agent_end")
        assert any(r.get("id") == "p2" and r["success"] for r in records)
        assert any(r["type"] == "message_update" for r in records)

        # The clone command is reachable and produces a third session file.
        _send(proc, {"id": "c1", "type": "clone"})
        cloned = feed.until(lambda r: r.get("id") == "c1")[-1]
        assert cloned["success"] is True

        assert proc.stdin is not None
        proc.stdin.close()
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)

    # Both sessions are real v3 files; the clone records its parent.
    sessions = sorted((agent_dir / "sessions").rglob("*.jsonl"))
    assert len(sessions) >= 2
    headers = [json.loads(p.read_text(encoding="utf-8").splitlines()[0]) for p in sessions]
    assert all(h["version"] == 3 for h in headers)
    assert any(h.get("parentSession") for h in headers)


# --------------------------------------------------------------------------------------
# Extensions, loaded from a project directory by the real CLI
# --------------------------------------------------------------------------------------


def _trust_project(agent_dir: Path) -> None:
    (agent_dir / "settings.json").write_text(
        json.dumps({"defaultProjectTrust": "always"}), encoding="utf-8"
    )


def test_project_extension_is_discovered_and_guards_a_tool(fake_server, tmp_path):
    server, base_url = fake_server
    agent_dir = tmp_path / "agent"
    workdir = tmp_path / "work"
    workdir.mkdir()
    _write_agent_config(agent_dir, base_url)
    _trust_project(agent_dir)

    marker = tmp_path / "blocked.marker"
    extensions = workdir / ".pi" / "extensions"
    extensions.mkdir(parents=True)
    (extensions / "guard.py").write_text(
        "\n".join(
            [
                "from pi_coding_agent.extensions import ToolCallEventResult",
                "",
                f"MARKER = {str(marker)!r}",
                "",
                "def setup(pi):",
                "    pi.on('tool_call', guard)",
                "    pi.on('session_start', lambda event, ctx: None)",
                "",
                "def guard(event, ctx):",
                "    if event.tool_name == 'write':",
                "        with open(MARKER, 'w', encoding='utf-8') as handle:",
                "            handle.write(ctx.cwd)",
                "        return ToolCallEventResult(block=True, reason='writes are disabled here')",
                "    return None",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = _run_cli(agent_dir, workdir, "create the file")

    assert result.returncode == 0, f"stderr:\n{result.stderr}\nstdout:\n{result.stdout}"

    # Discovery worked against the project directory...
    assert marker.exists(), f"extension never ran; stderr:\n{result.stderr}"
    assert marker.read_text(encoding="utf-8") == str(workdir)
    # ...and the tool it guarded never executed.
    assert not (workdir / "e2e.txt").exists()
    assert "writes are disabled here" not in result.stdout  # the reason stays out of the text


def test_project_extension_is_ignored_when_the_project_is_untrusted(fake_server, tmp_path):
    server, base_url = fake_server
    agent_dir = tmp_path / "agent"
    workdir = tmp_path / "work"
    workdir.mkdir()
    _write_agent_config(agent_dir, base_url)  # default trust asks -> declines headless

    marker = tmp_path / "should-not-exist.marker"
    extensions = workdir / ".pi" / "extensions"
    extensions.mkdir(parents=True)
    (extensions / "guard.py").write_text(
        "\n".join(
            [
                f"MARKER = {str(marker)!r}",
                "",
                "def setup(pi):",
                "    with open(MARKER, 'w', encoding='utf-8') as handle:",
                "        handle.write('loaded')",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = _run_cli(agent_dir, workdir, "create the file")

    assert result.returncode == 0
    assert not marker.exists()
    assert "untrusted" in result.stderr
    # The real tool ran because nothing blocked it.
    assert (workdir / "e2e.txt").exists()


def test_extension_ui_dialog_round_trips_over_rpc(fake_server, tmp_path):
    """The whole headless UI channel: extension asks, RPC client answers."""
    server, base_url = fake_server
    agent_dir = tmp_path / "agent"
    workdir = tmp_path / "work"
    workdir.mkdir()
    _write_agent_config(agent_dir, base_url)
    _trust_project(agent_dir)

    answer_file = tmp_path / "answer.txt"
    extensions = workdir / ".pi" / "extensions"
    extensions.mkdir(parents=True)
    (extensions / "asker.py").write_text(
        "\n".join(
            [
                f"ANSWER = {str(answer_file)!r}",
                "",
                "def setup(pi):",
                "    pi.register_command('ask', 'Ask the user', ask)",
                "",
                "async def ask(args, ctx):",
                "    ctx.ui.notify('about to ask')",
                "    confirmed = await ctx.ui.confirm('Proceed?', 'Are you sure?')",
                "    with open(ANSWER, 'w', encoding='utf-8') as handle:",
                "        handle.write(str(confirmed))",
                "",
            ]
        ),
        encoding="utf-8",
    )

    proc = _start_rpc(agent_dir, workdir)
    try:
        feed = _JsonlFeed(proc.stdout)
        _send(proc, {"id": "ask-1", "type": "prompt", "message": "/ask"})

        # The extension's notification and then its blocking dialog.
        records = feed.until(
            lambda r: r.get("type") == "extension_ui_request" and r.get("method") == "confirm"
        )
        kinds = [r.get("method") or r.get("type") for r in records]
        assert "notify" in kinds

        dialog = records[-1]
        assert dialog["title"] == "Proceed?"
        assert dialog["message"] == "Are you sure?"

        # Answer it; the parked handler resumes.
        _send(proc, {"type": "extension_ui_response", "id": dialog["id"], "confirmed": True})

        deadline_records = feed.until(lambda r: r.get("id") == "ask-1")
        assert any(r.get("id") == "ask-1" and r.get("success") for r in deadline_records)

        # Wait for the resumed handler to actually finish before closing stdin:
        # shutdown cancels in-flight work, so closing first races the write.
        deadline = time.monotonic() + 15
        while not answer_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert answer_file.exists(), "the extension handler never resumed"

        assert proc.stdin is not None
        proc.stdin.close()
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)

    assert answer_file.read_text(encoding="utf-8") == "True"

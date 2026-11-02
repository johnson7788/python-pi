"""End-to-end subagent tests: real CLI, real HTTP provider, real child sessions.

Nothing inside the process under test is mocked.  A subprocess runs ``pi-py``,
which bootstraps settings and credentials, calls a local OpenAI-compatible server
over loopback, executes the real ``subagent``/``subagent_fork`` tool, and spawns a
second real ``AgentSession`` with its own JSONL file.

The model server is content-routed rather than call-counted, because the parent
and the child run concurrently and their request order is genuinely racy.  Each
scenario labels its prompts so the server can tell the four roles apart.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent

SPAWN_MARKER = "SPAWN-CHILD-TASK"
FORK_MARKER = "FORK-CHILD-TASK"


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def _chunk(delta: dict, finish_reason: str | None = None) -> dict:
    return {
        "id": "chatcmpl-subagent",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "local-test",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _text_of(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
    return "\n".join(parts)


def _has_started_tool(messages: list[dict[str, Any]]) -> bool:
    return any(
        message.get("role") == "tool" and "started subagent" in _text_of([message])
        for message in messages
    )


def _reply(messages: list[dict[str, Any]]) -> bytes:
    """Route one request by what it carries, not by call order."""
    text = _text_of(messages)

    if "[subagent fork]" in text:
        return _text_payload("parent saw the forked child")
    if "[subagent spawn]" in text:
        return _text_payload("parent saw child says 42")
    if _has_started_tool(messages):
        return _text_payload("acknowledged; waiting for the subagent")
    if FORK_MARKER in text:
        return _text_payload("fork child reviewed the conversation")
    if SPAWN_MARKER in text:
        return _text_payload("child says 42")
    if "second question" in text:
        return _tool_payload("subagent_fork", {"prompt": f"{FORK_MARKER}: review", "label": "fork child"})
    if "first question" in text:
        return _text_payload("first answer")
    return _tool_payload("subagent", {"prompt": f"{SPAWN_MARKER}: say 42", "label": "e2e child"})


def _text_payload(content: str) -> bytes:
    return (
        _sse(_chunk({"role": "assistant", "content": content}))
        + _sse(_chunk({}, finish_reason="stop"))
        + b"data: [DONE]\n\n"
    )


def _tool_payload(name: str, arguments: dict[str, Any]) -> bytes:
    return (
        _sse(
            _chunk(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                }
            )
        )
        + _sse(_chunk({}, finish_reason="tool_calls"))
        + b"data: [DONE]\n\n"
    )


class _SubagentHandler(BaseHTTPRequestHandler):
    calls = 0
    bodies: list[dict[str, Any]] = []

    def log_message(self, *args) -> None:  # keep test output clean
        pass

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).calls += 1
        type(self).bodies.append(body)

        payload = _reply(body.get("messages") or [])

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.send_error(404)


class _Server:
    def __enter__(self) -> "_Server":
        _SubagentHandler.calls = 0
        _SubagentHandler.bodies = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _SubagentHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._server.server_port}/v1"
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _write_agent_config(agent_dir: Path, base_url: str) -> None:
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


def _cli_command(agent_dir: Path, workdir: Path, *extra: str) -> list[str]:
    return [
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
    ]


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}


def _run_cli(agent_dir: Path, workdir: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        _cli_command(agent_dir, workdir, *extra),
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        env=_clean_env(),
    )


def _session_files(agent_dir: Path) -> list[Path]:
    return sorted((agent_dir / "sessions").rglob("*.jsonl"))


def _headers(paths: list[Path]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in paths:
        header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        result[header["id"]] = {"header": header, "path": path}
    return result


# --------------------------------------------------------------------------------------
# spawn, one-shot print mode
# --------------------------------------------------------------------------------------


def test_cli_spawns_a_real_child_agent_and_reports_its_result(tmp_path: Path):
    with _Server() as server:
        agent_dir = tmp_path / "agent"
        workdir = tmp_path / "work"
        workdir.mkdir()
        _write_agent_config(agent_dir, server.base_url)

        result = _run_cli(agent_dir, workdir, "delegate the arithmetic to a subagent")

        assert result.returncode == 0, f"stderr:\n{result.stderr}\nstdout:\n{result.stdout}"
        assert "parent saw child says 42" in result.stdout

        # The parent, the child, and the parent's reaction each produced a request.
        assert _SubagentHandler.calls >= 3
        bodies = _SubagentHandler.bodies
        assert any(SPAWN_MARKER in _text_of(body.get("messages") or []) for body in bodies)

        # The delegation tools were offered to the model.
        tool_names = {tool["function"]["name"] for tool in bodies[0].get("tools", [])}
        assert {"subagent", "subagent_fork"} <= tool_names

        # Two session files: the child records its parent in the header.
        sessions = _session_files(agent_dir)
        assert len(sessions) == 2, sessions
        by_id = _headers(sessions)
        child = next(item for item in by_id.values() if item["header"].get("parentSession"))
        parent = next(item for item in by_id.values() if not item["header"].get("parentSession"))
        assert child["header"]["parentSession"] == parent["header"]["id"]
        assert child["header"]["cwd"] == str(workdir)

        # The parent transcript persisted the delegation result and the notice.
        entries = [
            json.loads(line)
            for line in parent["path"].read_text(encoding="utf-8").splitlines()[1:]
        ]
        assert any(
            entry.get("type") == "custom_message"
            and entry.get("customType") == "subagent"
            and "child says 42" in entry.get("content", "")
            for entry in entries
        ), entries
        tool_results = [
            entry["message"]
            for entry in entries
            if entry.get("type") == "message"
            and entry["message"].get("role") == "toolResult"
        ]
        assert any("started subagent" in _text_of([m]) for m in tool_results)

        # The parent's final answer (the one printed to stdout) is durable too.
        assert any(
            entry.get("type") == "message"
            and entry["message"].get("role") == "assistant"
            and "parent saw child says 42" in _text_of([entry["message"]])
            for entry in entries
        )


# --------------------------------------------------------------------------------------
# fork, RPC mode (two prompts are needed to have a completed turn to inherit)
# --------------------------------------------------------------------------------------


class _JsonlFeed:
    def __init__(self, stream: Any) -> None:
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
                if text.strip():
                    self._lines.put(json.loads(text))

    def until(self, predicate: Callable[[dict], bool], timeout: float = 60.0) -> list[dict]:
        seen: list[dict] = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"timed out; saw {[r.get('type') for r in seen]}")
            record = self._lines.get(timeout=remaining)
            if record is None:
                raise AssertionError(f"stream ended; saw {[r.get('type') for r in seen]}")
            seen.append(record)
            if predicate(record):
                return seen


def test_cli_fork_child_inherits_the_completed_parent_turn(tmp_path: Path):
    with _Server() as server:
        agent_dir = tmp_path / "agent"
        workdir = tmp_path / "work"
        workdir.mkdir()
        _write_agent_config(agent_dir, server.base_url)

        process = subprocess.Popen(
            _cli_command(agent_dir, workdir, "--mode", "rpc"),
            cwd=str(REPO_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_clean_env(),
        )
        assert process.stdin is not None and process.stdout is not None
        feed = _JsonlFeed(process.stdout)
        try:
            process.stdin.write(
                (json.dumps({"id": "1", "type": "prompt", "message": "first question"}) + "\n").encode()
            )
            process.stdin.flush()
            feed.until(
                lambda record: record.get("type") == "message_end"
                and (record.get("message") or {}).get("role") == "assistant"
                and "first answer" in _text_of([record["message"]])
            )

            process.stdin.write(
                (json.dumps({"id": "2", "type": "prompt", "message": "second question"}) + "\n").encode()
            )
            process.stdin.flush()
            feed.until(
                lambda record: record.get("type") == "message_end"
                and (record.get("message") or {}).get("role") == "assistant"
                and "parent saw the forked child" in _text_of([record["message"]])
            )
        finally:
            try:
                process.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            process.wait(timeout=30)

        # The child's first request carries the finished first turn and nothing from
        # the in-flight second one.
        child_requests = [
            body
            for body in _SubagentHandler.bodies
            if FORK_MARKER in _text_of(body.get("messages") or [])
        ]
        assert child_requests, "the fork child never called the provider"
        child_text = _text_of(child_requests[0]["messages"])
        assert "first question" in child_text
        assert "first answer" in child_text
        assert "second question" not in child_text

        # Exactly the finished turn plus the fork task: no in-flight assistant
        # tool-call message leaked into the seed.
        roles = [message.get("role") for message in child_requests[0]["messages"]]
        assert roles == ["system", "user", "assistant", "user"], roles


        # And the fork child is a durable session that points at its parent.
        by_id = _headers(_session_files(agent_dir))
        assert len(by_id) == 2
        child = next(item for item in by_id.values() if item["header"].get("parentSession"))
        parent = next(item for item in by_id.values() if not item["header"].get("parentSession"))
        assert child["header"]["parentSession"] == parent["header"]["id"]

        entries = [
            json.loads(line)
            for line in child["path"].read_text(encoding="utf-8").splitlines()[1:]
        ]
        child_messages = [
            entry["message"] for entry in entries if entry.get("type") == "message"
        ]
        assert _text_of([child_messages[0]]).startswith("first question")
        assert any(
            entry.get("type") == "custom_message" and entry.get("customType") == "subagent"
            for entry in [
                json.loads(line)
                for line in parent["path"].read_text(encoding="utf-8").splitlines()[1:]
            ]
        )


# --------------------------------------------------------------------------------------
# web server: the same flow through the browser protocol
# --------------------------------------------------------------------------------------


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _start_web(tmp_path: Path, base_url: str) -> tuple[subprocess.Popen, str, Path, Path]:
    from conftest import write_agent_config

    agent_dir = tmp_path / "agent"
    workdir = tmp_path / "work"
    workdir.mkdir()
    write_agent_config(agent_dir, base_url)

    port = _free_port()
    env = _clean_env()
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
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
        ],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    deadline = time.monotonic() + 30
    import httpx

    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
            raise AssertionError(f"server exited early:\n{stderr}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1.0).status_code == 200:
                return process, f"ws://127.0.0.1:{port}/api/ws", agent_dir, workdir
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.1)
    process.terminate()
    raise AssertionError("server never became healthy")


async def _recv_until(ws: Any, predicate: Callable[[dict], bool], timeout: float = 60.0) -> list[dict]:
    import asyncio

    seen: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = json.loads(await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic()))
        seen.append(record)
        if predicate(record):
            return seen
    raise AssertionError(f"timed out; saw {[r.get('type') for r in seen]}")


def test_web_server_delivers_the_child_result_to_the_parent_tab(tmp_path: Path):
    import asyncio

    import websockets

    with _Server() as server:
        process, ws_url, agent_dir, workdir = _start_web(tmp_path, server.base_url)
        try:

            async def scenario() -> list[dict]:
                async with websockets.connect(ws_url) as ws:
                    ready = json.loads(await ws.recv())
                    assert ready["type"] == "ready"
                    await ws.recv()  # initial get_state
                    await ws.send(
                        json.dumps(
                            {
                                "id": "p1",
                                "type": "prompt",
                                "message": "delegate the arithmetic to a subagent",
                            }
                        )
                    )
                    return await _recv_until(
                        ws,
                        lambda record: record.get("type") == "message_end"
                        and (record.get("message") or {}).get("role") == "assistant"
                        and "parent saw child says 42"
                        in _text_of([record["message"]]),
                    )

            records = asyncio.run(scenario())

            # The tool ran in the server's worker and the tab saw its lifecycle.
            tool_ends = [r for r in records if r.get("type") == "tool_execution_end"]
            assert any(r.get("toolName") == "subagent" for r in tool_ends), tool_ends

            # The notice itself was pushed to the tab — not merely persisted (the
            # parent was idle when it arrived, so no agent loop announced it).
            notices = [
                record
                for record in records
                if record.get("type") == "message_end"
                and (record.get("message") or {}).get("role") == "custom"
                and (record.get("message") or {}).get("customType") == "subagent"
            ]
            assert notices, [r.get("type") for r in records]

            # Both sessions are listed by the same REST surface the sidebar uses.
            sessions = _session_files(agent_dir)
            assert len(sessions) == 2, sessions
            by_id = _headers(sessions)
            child = next(item for item in by_id.values() if item["header"].get("parentSession"))
            parent = next(item for item in by_id.values() if not item["header"].get("parentSession"))
            assert child["header"]["parentSession"] == parent["header"]["id"]
            assert child["header"]["cwd"] == str(workdir)

            # The notice is in the parent transcript the server persisted.
            entries = [
                json.loads(line)
                for line in parent["path"].read_text(encoding="utf-8").splitlines()[1:]
            ]
            assert any(
                entry.get("type") == "custom_message"
                and entry.get("customType") == "subagent"
                and "child says 42" in entry.get("content", "")
                for entry in entries
            )
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

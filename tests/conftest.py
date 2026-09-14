"""Shared fixtures.

The only external dependency any test has is a model server; this provides a local
fake one that speaks the streaming chat-completions wire format, so the whole stack
can be exercised without an API key or network access.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


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

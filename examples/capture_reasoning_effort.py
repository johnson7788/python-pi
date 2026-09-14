"""Capture what the real stack sends to the model for each thinking level.

Runs a loopback proxy in front of the configured provider, points a session at it,
and prints the `reasoning_effort` field of every request.  This is the only check
that proves the whole chain at once: UI/RPC command -> AgentSession -> agent loop ->
provider adapter -> wire.

    python examples/capture_reasoning_effort.py

Requires a key in .env (same as start.sh).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

UPSTREAM = "https://dashscope.aliyuncs.com/compatible-mode/v1"
captured: list[dict] = []


class Proxy(BaseHTTPRequestHandler):
    """Forwards to the real provider and records the request body."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            captured.append(json.loads(raw))
        except Exception:  # noqa: BLE001
            captured.append({})

        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() in ("authorization", "content-type", "accept")
        }
        with httpx.Client(timeout=120) as client:
            upstream = client.post(f"{UPSTREAM}/chat/completions", content=raw, headers=headers)

        body = upstream.content
        self.send_response(upstream.status_code)
        self.send_header("Content-Type", upstream.headers.get("content-type", "application/json"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def read_env_key() -> str:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("PI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get("PI_API_KEY", "")


async def main() -> int:
    key = read_env_key()
    if not key:
        print("no PI_API_KEY found (.env or environment)", file=sys.stderr)
        return 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proxy = f"http://127.0.0.1:{server.server_port}/v1"

    # A session whose provider points at the proxy but is otherwise identical.
    # The overlay goes through a real models.json file so this exercises the same
    # loading path the CLI uses (and so the camelCase keys are the documented ones).
    from pi_ai import AuthStorage, InMemoryCredentialStore, ModelRegistry
    from pi_ai.auth import ApiKeyCredential

    store = InMemoryCredentialStore()
    store.set("deepseek", ApiKeyCredential(key=key))
    auth = AuthStorage(store)

    import tempfile

    models_path = Path(tempfile.mkdtemp()) / "models.json"
    models_path.write_text(
        json.dumps(
            {
                "providers": {
                    "deepseek": {
                        "api": "openai-completions",
                        "baseUrl": proxy,
                        "models": [
                            {
                                "id": "deepseek-v4-flash",
                                "reasoning": True,
                                "input": ["text"],
                                "contextWindow": 100000,
                                "maxTokens": 512,
                            }
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    registry = ModelRegistry(auth, models_path=models_path)
    model = registry.get("deepseek", "deepseek-v4-flash")
    if not model or "127.0.0.1" not in model.base_url:
        print(f"overlay did not take effect: base_url={model.base_url if model else None}", file=sys.stderr)
        return 1

    from pi_agent import Agent
    from pi_coding_agent.messages import convert_to_llm

    print(f"proxy -> {UPSTREAM}")
    print(f"thinkingLevelMap: {model.thinking_level_map}\n")

    for level in ["off", "low", "high", "max"]:
        captured.clear()
        agent = Agent(
            model=model,
            system_prompt="You are a test harness. Answer in one word.",
            tools=[],
            convert_to_llm=convert_to_llm,
            thinking_level=level,
            get_api_key=lambda provider: _resolved(auth, provider),
        )
        await agent.prompt("What is 2+4?")
        if not captured:
            last = agent.state.messages[-1] if agent.state.messages else None
            reason = getattr(last, "error_message", None) or getattr(last, "stop_reason", "?")
            print(f"  thinking_level={level:5s} -> <no request reached the proxy: {reason}>")
            continue
        sent = captured[-1].get("reasoning_effort", "<not sent>")
        print(f"  thinking_level={level:5s} -> reasoning_effort={sent}")

    server.shutdown()
    return 0


async def _resolved(auth: AuthStorage, provider: str) -> str | None:
    return auth.resolve_api_key(provider)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

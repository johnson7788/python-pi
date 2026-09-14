"""A tiny OpenAI-compatible model server for local UI demos.

Speaks the streaming chat-completions wire format and answers with a scripted
flow: a tool call (``bash``) on the first request, then a Markdown answer.  Run it
alongside ``pi-py-web`` to click around the UI without an API key.

    python examples/demo_model_server.py --port 8791
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def chunk(delta: dict, finish_reason: str | None = None) -> dict:
    return {
        "id": "chatcmpl-demo",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "demo",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


ANSWER = (
    "> ⚠️ **这是一段固定回复。** 你连的是 `examples/demo_model_server.py` 这个假模型：\n"
    "> 它不看你的输入，只会回放脚本。要真实回答请配置真实 provider（见 README「浏览器 UI」）。\n\n"
    "我检查了一下当前目录：\n\n"
    "1. **文件结构**已经列在上面了\n"
    "2. 这是一个纯 Python 的 coding agent harness\n\n"
    "```python\n"
    "from pi_coding_agent.bootstrap import bootstrap\n"
    "```\n\n"
    "需要我继续看某个文件吗？"
)


class Handler(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)

        type(self).calls += 1
        payload = b""
        if type(self).calls % 2 == 1:
            payload += sse(
                chunk(
                    {
                        "role": "assistant",
                        "content": "我先看看目录里有什么。",
                    }
                )
            )
            payload += sse(
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": f"call_{type(self).calls}",
                                "type": "function",
                                "function": {"name": "bash", "arguments": '{"command": "ls -la"}'},
                            }
                        ]
                    }
                )
            )
            payload += sse(chunk({}, finish_reason="tool_calls"))
        else:
            for index in range(0, len(ANSWER), 12):
                payload += sse(chunk({"role": "assistant", "content": ANSWER[index : index + 12]}))
            payload += sse(chunk({}, finish_reason="stop"))
        payload += b"data: [DONE]\n\n"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"demo model server on http://{args.host}:{args.port}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

"""JSON mode: one JSON object per line on stdout.

Port of ``packages/coding-agent/src/modes/json-event.ts`` plus the json branch of
``print-mode.ts``.

The first line is the session header
(``{"type":"session","id":...,"cwd":...,"model":...}``); every following line is
one agent event.  ``message_update`` is rewritten to be **delta-only**: the
cumulative ``message`` snapshot and the live ``partial`` reference are dropped so
the stream size stays linear.  ``toolcall_start`` additionally carries the
``id``/``toolName`` of the call it starts.

The exit-code contract matches print mode: ``0`` normally, ``1`` when the final
assistant message stopped with ``error``/``aborted`` (or the prompt failed).
Diagnostics go to ``stderr``; stdout stays parseable JSONL.

The wire projection itself lives in :mod:`pi_coding_agent.modes.json_event`,
because RPC mode emits exactly the same event shapes.
"""

from __future__ import annotations

import sys
from typing import Any, TextIO

from ..agent_session import AgentSession
from .json_event import to_json_event
from .jsonl import write_json_line as _write_line


def _last_assistant_message(session: AgentSession) -> Any | None:
    for message in reversed(session.messages):
        if getattr(message, "role", None) == "assistant":
            return message
    return None


async def run_json_mode(session: AgentSession, prompt: str, *, output: TextIO | None = None) -> int:
    """Send ``prompt`` and emit the session header plus every agent event."""
    stream: TextIO = output if output is not None else sys.stdout

    _write_line(
        stream,
        {
            "type": "session",
            "id": session.state()["session_id"],
            "cwd": session.cwd,
            "model": session.model.id,
        },
    )

    def _on_event(event: Any) -> None:
        _write_line(stream, to_json_event(event))

    unsubscribe = session.subscribe(_on_event)
    try:
        await session.prompt(prompt)
        await session.wait_for_idle()
    except Exception as error:  # noqa: BLE001 - reported, never raised out of a mode
        print(f"pi-py: {error}", file=sys.stderr)
        return 1
    finally:
        unsubscribe()

    last = _last_assistant_message(session)
    if last is not None and getattr(last, "stop_reason", None) in ("error", "aborted"):
        message = getattr(last, "error_message", None) or f"Request {last.stop_reason}"
        print(message, file=sys.stderr)
        return 1
    return 0


__all__ = ["run_json_mode", "to_json_event"]

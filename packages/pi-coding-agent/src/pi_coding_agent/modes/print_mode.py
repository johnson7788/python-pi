"""Print mode: send one prompt and stream the assistant text to stdout.

Port of ``packages/coding-agent/src/modes/print-mode.ts``.  Upstream text mode
writes only the final assistant message; this port streams the text deltas as
they arrive and then returns the same exit-code contract:

* ``0`` -- the run finished with a normal stop reason;
* ``1`` -- the final assistant message stopped with ``error`` or ``aborted``,
  or the prompt itself failed.

``stdout`` carries assistant text only.  Every diagnostic goes to ``stderr``.
"""

from __future__ import annotations

import sys
from typing import Any, TextIO

from pi_ai import TextContent

from ..agent_session import AgentSession


def _last_assistant_message(session: AgentSession) -> Any | None:
    for message in reversed(session.messages):
        if getattr(message, "role", None) == "assistant":
            return message
    return None


async def run_print_mode(
    session: AgentSession, prompt: str, *, output: TextIO | None = None
) -> int:
    """Send ``prompt`` and stream the assistant reply to ``output`` (stdout)."""
    stream: TextIO = output if output is not None else sys.stdout
    wrote_text = False
    last_char = ""

    def _on_event(event: Any) -> None:
        nonlocal wrote_text, last_char
        if getattr(event, "type", None) != "message_update":
            return
        assistant_event = getattr(event, "assistant_message_event", None)
        if assistant_event is None or getattr(assistant_event, "type", None) != "text_delta":
            return
        delta = getattr(assistant_event, "delta", "") or ""
        if not delta:
            return
        stream.write(delta)
        stream.flush()
        wrote_text = True
        last_char = delta[-1]

    unsubscribe = session.subscribe(_on_event)
    try:
        await session.prompt(prompt)
        await session.wait_for_idle()
    except Exception as error:  # noqa: BLE001 - reported, never raised out of a mode
        print(f"pi-py: {error}", file=sys.stderr)
        return 1
    finally:
        unsubscribe()

    if wrote_text and last_char != "\n":
        stream.write("\n")
        stream.flush()

    last = _last_assistant_message(session)
    if last is not None and getattr(last, "stop_reason", None) in ("error", "aborted"):
        message = getattr(last, "error_message", None) or f"Request {last.stop_reason}"
        print(message, file=sys.stderr)
        return 1

    # A provider that only emits a final message (no deltas) would otherwise
    # print nothing; fall back to the finalized assistant text.
    if not wrote_text and last is not None:
        text = "".join(
            block.text for block in (getattr(last, "content", None) or []) if isinstance(block, TextContent)
        )
        if text:
            stream.write(text if text.endswith("\n") else f"{text}\n")
            stream.flush()
    return 0


__all__ = ["run_print_mode"]

"""Extension dialogs over pi's RPC sub-protocol.

Why this exists: a Python-native extension calls ``ctx.ui.select(...)`` and must
block until *its host process* answers.  In a headless port the only real user
channel is the RPC sub-protocol pi itself uses in ``--mode rpc``: the agent writes
``extension_ui_request`` records to stdout and the client answers with
``extension_ui_response`` records on stdin.  This module owns the agent-side half
of that exchange -- field for field the ``RpcExtensionUIRequest`` /
``RpcExtensionUIResponse`` unions from pi's ``modes/rpc/rpc-types.ts``.  The RPC
loop owns reading stdin and handing each response to :meth:`RpcUIContext.resolve`.

Two properties make the split work:

* every record leaves through the injected ``emit`` callable, so this object never
  touches stdout and stays trivially testable;
* a blocking call parks on a future whose *safe default* is decided up front
  (``None`` for select/input/editor, ``False`` for confirm), so timeout,
  cancellation and "session is shutting down" all take the same path to unblock a
  handler instead of deadlocking it.

Wire timeout unit: pi's dialog ``timeout`` field is **milliseconds** -- the union
types it as ``timeout?: number`` and ``docs/rpc.md`` shows ``"timeout": 10000`` for
ten seconds.  This Python API takes **seconds**, matching every other timeout in
the port, and converts exactly once in :meth:`RpcUIContext._timeout_ms`.  Note that
``editor`` has no ``timeout`` field upstream, so it never carries one.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..modes.jsonl import write_json_line
from .api import UIContext

#: Record discriminant shared by every request this object emits; the RPC loop
#: answers with ``extension_ui_response`` records carrying the same ``id``.
_REQUEST_TYPE = "extension_ui_request"


@dataclass
class _Pending:
    """One in-flight blocking request, plus how to settle it without a response."""

    future: asyncio.Future[Any]
    #: Value used when the request times out or is cancelled.
    default: Any
    #: ``"value"`` (select/input/editor) or ``"confirmed"`` (confirm).
    expect: str
    timer: asyncio.TimerHandle | None = None


class RpcUIContext(UIContext):
    """``UIContext`` that speaks pi's extension UI RPC records.

    ``emit`` receives one plain dict per record; the caller is responsible for
    framing it (see :func:`stream_emitter`) and for feeding responses back via
    :meth:`resolve`.  ``default_timeout`` is in seconds and applies to any blocking
    call that does not pass its own ``timeout``; both are optional, and a falsy
    timeout means "wait forever", mirroring pi's ``if (opts?.timeout)`` guard.
    """

    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None],
        *,
        default_timeout: float | None = None,
    ) -> None:
        self._emit = emit
        self._default_timeout = default_timeout
        self._pending: dict[str, _Pending] = {}

    # -- fire-and-forget ---------------------------------------------------------------
    #
    # These methods still carry an id: the wire union requires one, and the client
    # may use it for logging.  They never register a pending entry and never block.

    def notify(self, message: str, type: str = "info") -> None:  # noqa: A002 - pi's name
        self._send("notify", message=message, notifyType=type)

    def set_status(self, key: str, text: str | None) -> None:
        # ``None`` must survive as JSON null (not be dropped) so the client can tell
        # "clear this status" apart from "this record does not mention status".
        self._send("setStatus", statusKey=key, statusText=text)

    def set_widget(self, key: str, lines: list[str] | None, placement: str | None = None) -> None:
        fields: dict[str, Any] = {
            "widgetKey": key,
            "widgetLines": list(lines) if lines is not None else None,
        }
        # ``widgetPlacement`` is optional upstream; omitting it lets the client apply
        # its default instead of receiving an explicit null it would have to ignore.
        if placement is not None:
            fields["widgetPlacement"] = placement
        self._send("setWidget", **fields)

    def set_title(self, title: str) -> None:
        self._send("setTitle", title=title)

    def set_editor_text(self, text: str) -> None:
        self._send("set_editor_text", text=text)

    # -- blocking round-trips ----------------------------------------------------------

    async def select(
        self, title: str, options: Sequence[str], *, timeout: float | None = None
    ) -> str | None:
        return await self._dialog(
            "select", timeout, default=None, expect="value", title=title, options=list(options)
        )

    async def confirm(
        self, title: str, message: str, *, timeout: float | None = None
    ) -> bool:
        return await self._dialog(
            "confirm", timeout, default=False, expect="confirmed", title=title, message=message
        )

    async def input(
        self, title: str, placeholder: str | None = None, *, timeout: float | None = None
    ) -> str | None:
        return await self._dialog(
            "input", timeout, default=None, expect="value", title=title, placeholder=placeholder
        )

    async def editor(self, title: str, prefill: str | None = None) -> str | None:
        # No ``timeout`` field upstream for editor; the constructor's default still
        # bounds the wait locally, it just is not advertised on the wire.
        return await self._dialog(
            "editor", None, default=None, expect="value", include_timeout=False,
            title=title, prefill=prefill,
        )

    # -- driven by the RPC loop --------------------------------------------------------

    def resolve(self, response: dict[str, Any]) -> bool:
        """Complete the pending request named by ``response["id"]``.

        Returns ``True`` when the id matched.  ``False`` means the response is stale
        or unknown (it already timed out, or belongs to another session) and the RPC
        loop should keep going without disturbing anything else.
        """
        request_id = response.get("id")
        if not isinstance(request_id, str):
            return False
        pending = self._pending.pop(request_id, None)
        if pending is None:
            return False
        if pending.timer is not None:
            pending.timer.cancel()
        if not pending.future.done():
            pending.future.set_result(self._interpret(pending, response))
        return True

    def pending_count(self) -> int:
        """Number of blocking requests still waiting for an answer."""
        return len(self._pending)

    def cancel_all(self, reason: str = "session ended") -> None:
        """Unblock every pending request with its safe default.

        Called while shutting a session down: a handler parked on a dialog can no
        longer receive an answer, and leaving it parked would deadlock the loop that
        is waiting for it.  Idempotent -- a second call has nothing left to settle.
        """
        del reason  # Diagnostic only; the safe default is fixed per request method.
        for request_id in list(self._pending):
            self._expire(request_id)

    # -- internals ---------------------------------------------------------------------

    def _send(self, method: str, **fields: Any) -> str:
        request: dict[str, Any] = {
            "type": _REQUEST_TYPE,
            "id": str(uuid.uuid4()),
            "method": method,
        }
        request.update(fields)
        self._emit(request)
        return request["id"]

    async def _dialog(
        self,
        method: str,
        timeout: float | None,
        *,
        default: Any,
        expect: str,
        include_timeout: bool = True,
        **fields: Any,
    ) -> Any:
        effective = self._default_timeout if timeout is None else timeout
        if include_timeout and effective:
            fields["timeout"] = self._timeout_ms(effective)

        request_id = str(uuid.uuid4())
        request: dict[str, Any] = {
            "type": _REQUEST_TYPE,
            "id": request_id,
            "method": method,
        }
        request.update(fields)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        pending = _Pending(future=future, default=default, expect=expect)
        self._pending[request_id] = pending
        if effective:
            pending.timer = loop.call_later(effective, self._expire, request_id)

        try:
            # Inside the try so a raising emit still drops the pending entry.
            self._emit(request)
            return await future
        finally:
            # Whichever path settled the future, the entry must not outlive the call.
            if pending.timer is not None:
                pending.timer.cancel()
            self._pending.pop(request_id, None)

    def _expire(self, request_id: str) -> None:
        """Settle one request on the timeout/cancel path with its safe default."""
        pending = self._pending.pop(request_id, None)
        if pending is None or pending.future.done():
            return
        pending.future.set_result(pending.default)

    @staticmethod
    def _interpret(pending: _Pending, response: dict[str, Any]) -> Any:
        """Fold a client response into a return value, degrading to the default.

        A malformed or wrong-shaped response is treated like a cancellation: the
        caller gets a usable answer either way, so a buggy client cannot hang or
        crash an extension handler.
        """
        if response.get("cancelled"):
            return pending.default
        if pending.expect == "confirmed":
            if "confirmed" in response:
                return bool(response["confirmed"])
            return pending.default
        value = response.get("value")
        return value if isinstance(value, str) else pending.default

    @staticmethod
    def _timeout_ms(seconds: float) -> int:
        """Convert seconds to pi's millisecond timeout field."""
        return max(1, int(round(seconds * 1000)))


def stream_emitter(stream: Any) -> Callable[[dict[str, Any]], None]:
    """Build an ``emit`` callable that frames records LF-only onto ``stream``.

    The framing rule lives next to the records it frames: ``write_json_line`` always
    terminates with ``\\n`` and flushes, which is what the client's reader splits on.
    ``print`` is never used because it would honour platform line endings and could
    break mid-record framing.
    """

    def _emit(value: dict[str, Any]) -> None:
        write_json_line(stream, value)

    return _emit


__all__ = ["RpcUIContext", "stream_emitter"]

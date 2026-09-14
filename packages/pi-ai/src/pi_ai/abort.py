"""Cooperative cancellation.

The TypeScript implementation threads an ``AbortSignal`` through the whole agent
loop.  The signal is **observed**, never thrown: when it fires, in-flight work
keeps unwinding normally and the loop synthesises ``"Operation aborted"`` tool
results.  Python's ``asyncio.CancelledError`` is the wrong primitive here (it
unwinds through ``finally`` blocks that must still run, and it cannot be
inspected), so this module models the flag explicitly.
"""

from __future__ import annotations

import asyncio
from typing import Callable


class AbortSignal:
    """An observable cancellation flag."""

    __slots__ = ("_event", "_listeners", "reason")

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._listeners: list[Callable[[], None]] = []
        self.reason: str | None = None

    @property
    def aborted(self) -> bool:
        return self._event.is_set()

    def throw_if_aborted(self) -> None:
        if self.aborted:
            from .errors import AbortedError

            raise AbortedError(self.reason or "Operation aborted")

    def on_abort(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register ``listener``; returns a disposer."""
        if self.aborted:
            listener()
            return lambda: None
        self._listeners.append(listener)

        def _dispose() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return _dispose

    async def wait(self) -> None:
        await self._event.wait()

    def _fire(self) -> None:
        self._event.set()
        for listener in list(self._listeners):
            listener()
        self._listeners.clear()


class AbortController:
    """Owns a signal and can fire it once."""

    __slots__ = ("signal",)

    def __init__(self, signal: AbortSignal | None = None) -> None:
        self.signal = signal or AbortSignal()

    def abort(self, reason: str | None = None) -> None:
        if self.signal.aborted:
            return
        self.signal.reason = reason
        self.signal._fire()


def any_(*signals: AbortSignal | None) -> AbortSignal:
    """Combine signals into one that fires when any input fires."""
    present = [s for s in signals if s is not None]
    if not present:
        return AbortSignal()
    if len(present) == 1:
        return present[0]

    combined = AbortSignal()
    for signal in present:
        if signal.aborted:
            combined.reason = signal.reason
            combined._fire()
            return combined
        signal.on_abort(lambda s=signal: _propagate(combined, s))
    return combined


def _propagate(combined: AbortSignal, source: AbortSignal) -> None:
    if not combined.aborted:
        combined.reason = source.reason
        combined._fire()


__all__ = ["AbortController", "AbortSignal", "any_"]

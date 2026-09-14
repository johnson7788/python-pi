"""The streaming event protocol and the push-based ``EventStream``.

Two contracts carried over from the TypeScript source that a naive port breaks:

1. ``partial`` is a **shared, live** :class:`AssistantMessage`, not a snapshot.
   Every event carries the same object, mutated in place.  Consumers must read
   what they need while handling the event instead of retaining the reference.
2. ``result()`` resolves on the **first** ``done``/``error`` event, without
   waiting for ``end()``; events pushed after completion are discarded silently.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Generic, Literal, TypeVar, Union

from .types import AssistantMessage, ToolCall

T = TypeVar("T")
R = TypeVar("R")


# --------------------------------------------------------------------------------------
# EventStream
# --------------------------------------------------------------------------------------


class EventStream(Generic[T, R]):
    """A push-based async queue, mirroring ``pi-ai``'s ``EventStream<T, R>``.

    Producers call :meth:`push` (and finally :meth:`end` or :meth:`fail`);
    consumers either ``async for`` the events or ``await stream.result()``.

    ``push`` is deliberately synchronous, matching ``stream.push(event)`` in
    TypeScript: producing a token must never yield control to the event loop.
    """

    __slots__ = ("_is_complete", "_extract_result", "_buffer", "_done", "_result", "_getter", "_waiter")

    def __init__(
        self,
        is_complete: Callable[[T], bool],
        extract_result: Callable[[T], R],
    ) -> None:
        self._is_complete = is_complete
        self._extract_result = extract_result
        self._buffer: list[T] = []
        self._done = False
        self._result: R | None = None
        self._getter: asyncio.Future[None] | None = None
        self._waiter: asyncio.Future[R] | None = None

    # -- producer side -----------------------------------------------------------------

    def push(self, event: T) -> None:
        """Append an event; silently ignored once the stream has completed.

        The completing event is still delivered to iterators — only events
        pushed *after* completion are discarded.
        """
        if self._done:
            return
        if self._is_complete(event):
            self._done = True
            self._result = self._extract_result(event)
        self._buffer.append(event)
        self._wake()

    def end(self, result: R | None = None) -> None:
        """Mark the stream finished, optionally with an explicit final result."""
        if self._done:
            if result is not None and self._result is None:
                self._result = result
        else:
            self._done = True
            if result is not None:
                self._result = result
        self._wake()

    def fail(self, error: BaseException) -> None:
        """Terminate the stream by raising ``error`` out of iteration / ``result()``.

        This is a Python addition: TypeScript relies on unhandled rejections,
        which would silently hang an awaited ``result()``.
        """
        for waiter in (self._getter, self._waiter):
            if waiter is not None and not waiter.done():
                waiter.set_exception(error)
        self._getter = None
        self._waiter = None
        self._done = True
        self._buffer.clear()

    def _wake(self) -> None:
        if self._getter is not None and not self._getter.done():
            self._getter.set_result(None)
        if self._done and self._waiter is not None and not self._waiter.done():
            assert self._result is not None
            self._waiter.set_result(self._result)

    # -- consumer side -----------------------------------------------------------------

    def __aiter__(self) -> AsyncIterator[T]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[T]:
        while True:
            if self._buffer:
                yield self._buffer.pop(0)
                continue
            if self._done:
                return
            self._getter = asyncio.get_running_loop().create_future()
            await self._getter
            self._getter = None

    async def result(self) -> R:
        """Await the final result, resolved on the first completing event."""
        if self._done:
            assert self._result is not None
            return self._result
        self._waiter = asyncio.get_running_loop().create_future()
        return await self._waiter

    @property
    def done(self) -> bool:
        return self._done


# --------------------------------------------------------------------------------------
# Assistant message events
# --------------------------------------------------------------------------------------


@dataclass
class StartEvent:
    partial: AssistantMessage
    type: Literal["start"] = "start"


@dataclass
class TextStartEvent:
    content_index: int
    partial: AssistantMessage
    type: Literal["text_start"] = "text_start"


@dataclass
class TextDeltaEvent:
    content_index: int
    delta: str
    partial: AssistantMessage
    type: Literal["text_delta"] = "text_delta"


@dataclass
class TextEndEvent:
    content_index: int
    content: str
    partial: AssistantMessage
    type: Literal["text_end"] = "text_end"


@dataclass
class ThinkingStartEvent:
    content_index: int
    partial: AssistantMessage
    type: Literal["thinking_start"] = "thinking_start"


@dataclass
class ThinkingDeltaEvent:
    content_index: int
    delta: str
    partial: AssistantMessage
    type: Literal["thinking_delta"] = "thinking_delta"


@dataclass
class ThinkingEndEvent:
    content_index: int
    content: str
    partial: AssistantMessage
    type: Literal["thinking_end"] = "thinking_end"


@dataclass
class ToolCallStartEvent:
    content_index: int
    partial: AssistantMessage
    type: Literal["toolcall_start"] = "toolcall_start"


@dataclass
class ToolCallDeltaEvent:
    content_index: int
    delta: str
    partial: AssistantMessage
    type: Literal["toolcall_delta"] = "toolcall_delta"


@dataclass
class ToolCallEndEvent:
    content_index: int
    tool_call: ToolCall
    partial: AssistantMessage
    type: Literal["toolcall_end"] = "toolcall_end"


@dataclass
class DoneEvent:
    reason: str
    message: AssistantMessage
    type: Literal["done"] = "done"


@dataclass
class ErrorEvent:
    reason: str
    error: AssistantMessage
    type: Literal["error"] = "error"


AssistantMessageEvent = Union[
    StartEvent,
    TextStartEvent,
    TextDeltaEvent,
    TextEndEvent,
    ThinkingStartEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ToolCallStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    DoneEvent,
    ErrorEvent,
]

UPDATE_EVENT_TYPES = frozenset(
    {
        "text_start",
        "text_delta",
        "text_end",
        "thinking_start",
        "thinking_delta",
        "thinking_end",
        "toolcall_start",
        "toolcall_delta",
        "toolcall_end",
    }
)


def create_assistant_message_event_stream() -> EventStream[AssistantMessageEvent, AssistantMessage]:
    def _extract(event: AssistantMessageEvent) -> AssistantMessage:
        if isinstance(event, DoneEvent):
            return event.message
        if isinstance(event, ErrorEvent):
            return event.error
        raise TypeError(f"Unexpected event type for final result: {event.type}")

    return EventStream(
        lambda event: isinstance(event, (DoneEvent, ErrorEvent)),
        _extract,
    )


def event_to_json(event: AssistantMessageEvent) -> dict[str, Any]:
    """Serialise an assistant event for the JSON event-stream mode."""
    payload: dict[str, Any] = {"type": event.type}
    if isinstance(event, (TextStartEvent, ThinkingStartEvent, ToolCallStartEvent)):
        payload["contentIndex"] = event.content_index
    elif isinstance(event, (TextDeltaEvent, ThinkingDeltaEvent, ToolCallDeltaEvent)):
        payload["contentIndex"] = event.content_index
        payload["delta"] = event.delta
    elif isinstance(event, (TextEndEvent, ThinkingEndEvent)):
        payload["contentIndex"] = event.content_index
        payload["content"] = event.content
    elif isinstance(event, ToolCallEndEvent):
        payload["contentIndex"] = event.content_index
        payload["toolCall"] = {
            "type": "toolCall",
            "id": event.tool_call.id,
            "name": event.tool_call.name,
            "arguments": event.tool_call.arguments,
        }
    elif isinstance(event, DoneEvent):
        payload["reason"] = event.reason
        payload["message"] = event.message
    elif isinstance(event, ErrorEvent):
        payload["reason"] = event.reason
        payload["error"] = event.error
    return payload


__all__ = [
    "UPDATE_EVENT_TYPES",
    "AssistantMessageEvent",
    "DoneEvent",
    "ErrorEvent",
    "EventStream",
    "StartEvent",
    "TextDeltaEvent",
    "TextEndEvent",
    "TextStartEvent",
    "ThinkingDeltaEvent",
    "ThinkingEndEvent",
    "ThinkingStartEvent",
    "ToolCallDeltaEvent",
    "ToolCallEndEvent",
    "ToolCallStartEvent",
    "create_assistant_message_event_stream",
    "event_to_json",
]

"""Deterministic offline provider for tests and the agent loop.

Every response is scripted up front, so a test can assert on exact event
sequences without a network, a clock, or a random seed.  The output is chunked
at a **fixed** size rather than upstream's random token sizes: the whole point
of this provider is reproducibility, and a flaky chunk boundary would make
``text_delta`` assertions flaky too.

The critical contract this module exists to exercise is the shared ``partial``:
ONE :class:`AssistantMessage` is created per call and the *same object* is
placed in every event and mutated in place.  Consumers must read fields while
handling an event instead of retaining the reference: several tests depend on
observing that aliasing directly.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from typing import Sequence

from ..cost import calculate_cost
from ..events import (
    AssistantMessageEvent,
    DoneEvent,
    ErrorEvent,
    EventStream,
    StartEvent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    create_assistant_message_event_stream,
)
from ..stream import Context, StreamOptions, register_api
from ..transform import parse_streaming_json
from ..types import (
    AssistantContentBlock,
    AssistantMessage,
    Cost,
    Model,
    StopReason,
    TextContent,
    ThinkingContent,
    ToolCall,
    Usage,
)

#: Fixed delta size, in characters, for streamed text and tool arguments.
CHUNK_SIZE = 8


@dataclass
class FauxTurn:
    """One scripted assistant response.

    ``content`` overrides the block list entirely when set; otherwise the blocks
    are built in the order thinking, text, tool calls, which is the order models
    actually emit them in.
    """

    text: str | None = None
    thinking: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: StopReason = "stop"
    usage: Usage | None = None
    error_message: str | None = None
    content: list[AssistantContentBlock] | None = None
    response_id: str | None = None

    def blocks(self) -> list[AssistantContentBlock]:
        if self.content is not None:
            return list(self.content)
        blocks: list[AssistantContentBlock] = []
        if self.thinking:
            blocks.append(ThinkingContent(thinking=self.thinking))
        if self.text:
            blocks.append(TextContent(text=self.text))
        blocks.extend(self.tool_calls)
        return blocks


def _copy_usage(usage: Usage | None) -> Usage:
    """Own a private Usage so pricing one call cannot rewrite a scripted turn."""
    if usage is None:
        return Usage(cost=Cost())
    return replace(usage, cost=replace(usage.cost))


def _chunks(text: str) -> list[str]:
    """Split deterministically; a non-empty string always yields one chunk."""
    if text == "":
        return []
    return [text[index : index + CHUNK_SIZE] for index in range(0, len(text), CHUNK_SIZE)]


class FauxProvider:
    """A ``ProviderStreams`` that replays ``turns`` one per call.

    Usage is copied out of the turn and priced with :func:`calculate_cost` against
    the request model, exactly like the real adapters, so tests can assert on
    non-zero cost without a network round trip.
    """

    def __init__(self, turns: Sequence[FauxTurn] = ()) -> None:
        self.turns: list[FauxTurn] = list(turns)
        self.call_count = 0

    # -- ProviderStreams ---------------------------------------------------------------

    def stream(
        self, model: Model, context: Context, options: StreamOptions
    ) -> EventStream[AssistantMessageEvent, AssistantMessage]:
        self.call_count += 1
        stream = create_assistant_message_event_stream()
        turn = self.turns.pop(0) if self.turns else None

        # The single live message shared by every event of this call.
        output = AssistantMessage(
            content=[],
            api=model.api,
            provider=model.provider,
            model=model.id,
            usage=_copy_usage(turn.usage if turn is not None else None),
            stop_reason="pending",
            timestamp=int(time.time() * 1000),
        )
        if turn is not None and turn.response_id is not None:
            output.response_id = turn.response_id

        signal = options.signal
        if signal is not None and signal.aborted:
            calculate_cost(model, output.usage)
            self._abort(stream, output, signal.reason)
            return stream

        if turn is None:
            calculate_cost(model, output.usage)
            output.stop_reason = "error"
            output.error_message = "No more faux responses queued"
            stream.push(ErrorEvent(reason="error", error=output))
            stream.end(output)
            return stream

        stream.push(StartEvent(partial=output))

        for block in turn.blocks():
            if signal is not None and signal.aborted:
                calculate_cost(model, output.usage)
                self._abort(stream, output, signal.reason)
                return stream

            if isinstance(block, ThinkingContent):
                live = ThinkingContent(thinking="")
                output.content.append(live)
                index = len(output.content) - 1
                stream.push(ThinkingStartEvent(content_index=index, partial=output))
                for chunk in _chunks(block.thinking):
                    live.thinking += chunk
                    stream.push(
                        ThinkingDeltaEvent(content_index=index, delta=chunk, partial=output)
                    )
                live.thinking_signature = block.thinking_signature
                live.redacted = block.redacted
                stream.push(
                    ThinkingEndEvent(content_index=index, content=live.thinking, partial=output)
                )
                continue

            if isinstance(block, TextContent):
                live_text = TextContent(text="")
                output.content.append(live_text)
                index = len(output.content) - 1
                stream.push(TextStartEvent(content_index=index, partial=output))
                for chunk in _chunks(block.text):
                    live_text.text += chunk
                    stream.push(TextDeltaEvent(content_index=index, delta=chunk, partial=output))
                stream.push(TextEndEvent(content_index=index, content=live_text.text, partial=output))
                continue

            if isinstance(block, ToolCall):
                live_call = ToolCall(id=block.id, name=block.name, arguments={})
                output.content.append(live_call)
                index = len(output.content) - 1
                stream.push(ToolCallStartEvent(content_index=index, partial=output))
                serialized = json.dumps(block.arguments)
                partial = ""
                for chunk in _chunks(serialized):
                    partial += chunk
                    # Mirror the adapters: arguments grow as the JSON parses.
                    live_call.arguments = parse_streaming_json(partial)
                    stream.push(
                        ToolCallDeltaEvent(content_index=index, delta=chunk, partial=output)
                    )
                # The scripted arguments are authoritative; streaming only
                # approximated them.
                live_call.arguments = block.arguments
                live_call.thought_signature = block.thought_signature
                live_call.namespace = block.namespace
                stream.push(ToolCallEndEvent(content_index=index, tool_call=live_call, partial=output))
                continue

        output.stop_reason = turn.stop_reason
        calculate_cost(model, output.usage)
        if turn.stop_reason in ("error", "aborted"):
            if turn.error_message is not None:
                output.error_message = turn.error_message
            stream.push(ErrorEvent(reason=output.stop_reason, error=output))
            stream.end(output)
            return stream

        stream.push(DoneEvent(reason=output.stop_reason, message=output))
        stream.end(output)
        return stream

    def stream_simple(
        self, model: Model, context: Context, options: StreamOptions
    ) -> EventStream[AssistantMessageEvent, AssistantMessage]:
        return self.stream(model, context, options)

    # -- Helpers -----------------------------------------------------------------------

    def _abort(
        self,
        stream: EventStream[AssistantMessageEvent, AssistantMessage],
        output: AssistantMessage,
        reason: str | None,
    ) -> None:
        output.stop_reason = "aborted"
        output.error_message = reason or "Request was aborted"
        stream.push(ErrorEvent(reason="aborted", error=output))
        stream.end(output)


def register_faux_api(name: str, turns: Sequence[FauxTurn] = ()) -> FauxProvider:
    """Register a :class:`FauxProvider` under ``name`` via :func:`pi_ai.register_api`.

    Returns the provider so tests can inspect ``call_count`` or queue more turns.
    """
    provider = FauxProvider(turns)
    register_api(name, provider)
    return provider


__all__ = ["CHUNK_SIZE", "FauxProvider", "FauxTurn", "register_faux_api"]

"""Anthropic Messages adapter (port of ``api/anthropic-messages.ts``).

The request is ``POST {base_url}/v1/messages`` with ``anthropic-version``, and
the response is an SSE stream whose event names drive the state machine below.

Wire details that differ from every OpenAI-shaped provider and therefore live
here rather than in the shared layer:

* There is no ``system`` role: the system prompt is a top-level ``system`` field
  holding content blocks.
* Tool results are content blocks (``tool_result``) inside a ``user`` message,
  not their own role, so consecutive tool results are merged into one message.
* Tool schemas are ``{name, description, input_schema}``.
* ``signature_delta`` carries no user-visible content: it is accumulated into
  ``ThinkingContent.thinking_signature`` and must be replayed verbatim, because
  Anthropic rejects a thinking block whose signature changed.

Setup failures (missing credentials, transport construction) are encoded into
the returned stream as an ``error`` event by the caller; failures discovered
while the task runs are converted here.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Callable

import httpx

from ..cost import calculate_cost
from ..errors import AuthenticationError, ProviderError
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
from ..stream import (
    DEFAULT_THINKING_BUDGETS,
    MIN_ANSWER_TOKENS,
    Context,
    StreamOptions,
    create_error_stream,
)
from ..transform import parse_streaming_json, transform_messages
from ..types import (
    AssistantMessage,
    Cost,
    ImageContent,
    Message,
    Model,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)

ANTHROPIC_VERSION = "2023-06-01"
MAX_ERROR_BODY_CHARS = 4000
#: Anthropic requires ``^[a-zA-Z0-9_-]+$`` and at most 64 characters.
ANTHROPIC_TOOL_CALL_ID_MAX_LENGTH = 64

#: Only these SSE event names are parsed; Anthropic interleaves ping/other events.
_MESSAGE_EVENTS = frozenset(
    {
        "message_start",
        "message_delta",
        "message_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
    }
)

ClientFactory = Callable[[Model, StreamOptions], httpx.AsyncClient]


# --------------------------------------------------------------------------------------
# SSE decoding
# --------------------------------------------------------------------------------------


def _consume_line(text: str) -> tuple[str, str] | None:
    """Split off the first line, tolerating ``\\n``, ``\\r`` and ``\\r\\n``."""
    carriage = text.find("\r")
    newline = text.find("\n")
    if carriage == -1:
        index = newline
    elif newline == -1:
        index = carriage
    else:
        index = min(carriage, newline)
    if index == -1:
        return None
    next_index = index + 1
    if text[index] == "\r" and next_index < len(text) and text[next_index] == "\n":
        next_index += 1
    return text[:index], text[next_index:]


class SseDecoder:
    """Incremental ``event:``/``data:`` decoder (upstream ``decodeSseLine``)."""

    __slots__ = ("_buffer", "_event", "_data")

    def __init__(self) -> None:
        self._buffer = ""
        self._event: str | None = None
        self._data: list[str] = []

    def feed(self, text: str) -> list[tuple[str | None, str]]:
        self._buffer += text
        events: list[tuple[str | None, str]] = []
        while True:
            consumed = _consume_line(self._buffer)
            if consumed is None:
                break
            line, self._buffer = consumed
            event = self._decode_line(line)
            if event is not None:
                events.append(event)
        return events

    def flush(self) -> list[tuple[str | None, str]]:
        """Drain the trailing partial line and any pending event."""
        events: list[tuple[str | None, str]] = []
        if self._buffer:
            line, self._buffer = self._buffer, ""
            event = self._decode_line(line)
            if event is not None:
                events.append(event)
        trailing = self._flush_event()
        if trailing is not None:
            events.append(trailing)
        return events

    def _flush_event(self) -> tuple[str | None, str] | None:
        if self._event is None and not self._data:
            return None
        event = (self._event, "\n".join(self._data))
        self._event = None
        self._data = []
        return event

    def _decode_line(self, line: str) -> tuple[str | None, str] | None:
        if line == "":
            return self._flush_event()
        if line.startswith(":"):
            return None  # comment / keep-alive

        delimiter = line.find(":")
        if delimiter == -1:
            field, value = line, ""
        else:
            field, value = line[:delimiter], line[delimiter + 1 :]
        if value.startswith(" "):
            value = value[1:]

        if field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)
        return None


# --------------------------------------------------------------------------------------
# Request construction
# --------------------------------------------------------------------------------------


def _messages_url(base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    if base.endswith("/v1/messages"):
        return base
    return f"{base}/v1/messages"


def _assert_auth(model: Model, options: StreamOptions) -> None:
    """Raise unless a credential is present, mirroring ``assertRequestAuth``."""
    headers = {key.lower() for key in (options.headers or {})}
    if options.api_key or headers & {"authorization", "x-api-key", "cf-aig-authorization"}:
        return
    raise AuthenticationError(model.provider, f"No API key for provider: {model.provider}")


def _build_headers(model: Model, options: StreamOptions) -> dict[str, str]:
    headers: dict[str, str] = {
        "content-type": "application/json",
        "accept": "text/event-stream",
        "anthropic-version": ANTHROPIC_VERSION,
    }
    if model.headers:
        headers.update(model.headers)
    if options.headers:
        headers.update(options.headers)
    if options.api_key:
        headers["x-api-key"] = options.api_key
    return headers


def _default_client_factory(model: Model, options: StreamOptions) -> httpx.AsyncClient:
    timeout: Any = options.request_timeout if options.request_timeout is not None else 600.0
    return httpx.AsyncClient(timeout=timeout)


def _resolve_thinking(model: Model, options: StreamOptions) -> dict[str, Any] | None:
    """Map the abstract thinking level onto Anthropic's ``thinking`` field.

    ``None`` means "do not send the field at all", which is distinct from
    ``{"type": "disabled"}``: the former is the raw-``stream`` default, the
    latter is how ``streamSimple`` turns thinking off for a capable model.
    """
    provider_options = options.provider_options or {}
    enabled = provider_options.get("thinking_enabled")
    level = options.reasoning

    if enabled is None:
        if not model.reasoning or level is None:
            return None
        enabled = level != "off"

    if not enabled:
        # Upstream honours an explicit `null` in the map as "off is unsupported".
        level_map = model.thinking_level_map or {}
        if "off" in level_map and level_map["off"] is None:
            return None
        return {"type": "disabled"}

    budgets = dict(DEFAULT_THINKING_BUDGETS)
    budgets.update(options.thinking_budgets or {})
    budget = provider_options.get("thinking_budget_tokens")
    if budget is None:
        budget = budgets.get(level or "high") or DEFAULT_THINKING_BUDGETS["high"]
    return {
        "type": "enabled",
        "budget_tokens": int(budget),
        "display": provider_options.get("thinking_display", "summarized"),
    }


def _image_block(block: ImageContent) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": block.mime_type, "data": block.data},
    }


def _convert_block_content(content: list[TextContent | ImageContent]) -> str | list[dict[str, Any]]:
    """Text-only content collapses to a string (upstream ``convertContentBlocks``)."""
    if not any(isinstance(block, ImageContent) for block in content):
        return "\n".join(block.text for block in content if isinstance(block, TextContent))

    blocks: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextContent):
            blocks.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageContent):
            blocks.append(_image_block(block))
    if not any(block["type"] == "text" for block in blocks):
        blocks.insert(0, {"type": "text", "text": "(see attached image)"})
    return blocks


def _convert_messages(messages: list[Message]) -> list[dict[str, Any]]:
    params: list[dict[str, Any]] = []
    index = 0
    while index < len(messages):
        message = messages[index]

        if isinstance(message, UserMessage):
            if isinstance(message.content, str):
                if message.content.strip():
                    params.append({"role": "user", "content": message.content})
            else:
                blocks: list[dict[str, Any]] = []
                for item in message.content:
                    if isinstance(item, TextContent):
                        if item.text.strip():
                            blocks.append({"type": "text", "text": item.text})
                    elif isinstance(item, ImageContent):
                        blocks.append(_image_block(item))
                if blocks:
                    params.append({"role": "user", "content": blocks})
            index += 1
            continue

        if isinstance(message, AssistantMessage):
            blocks = []
            for block in message.content:
                if isinstance(block, TextContent):
                    if not block.text.strip():
                        continue
                    blocks.append({"type": "text", "text": block.text})
                elif isinstance(block, ThinkingContent):
                    signature = (block.thinking_signature or "").strip()
                    if block.redacted:
                        # Opaque payload must go back exactly as it arrived.
                        blocks.append(
                            {"type": "redacted_thinking", "data": block.thinking_signature or ""}
                        )
                        continue
                    if not block.thinking.strip() and not signature:
                        continue
                    if not signature:
                        # An interrupted stream leaves thinking without a
                        # signature; Anthropic rejects that, so degrade to text.
                        blocks.append({"type": "text", "text": block.thinking})
                    else:
                        blocks.append(
                            {
                                "type": "thinking",
                                "thinking": block.thinking,
                                "signature": block.thinking_signature,
                            }
                        )
                elif isinstance(block, ToolCall):
                    blocks.append(
                        {"type": "tool_use", "id": block.id, "name": block.name, "input": block.arguments or {}}
                    )
            if blocks:
                params.append({"role": "assistant", "content": blocks})
            index += 1
            continue

        if isinstance(message, ToolResultMessage):
            # Merge the run of tool results into a single user message; both
            # Anthropic and Anthropic-compatible proxies require this.
            tool_results: list[dict[str, Any]] = []
            while index < len(messages) and isinstance(messages[index], ToolResultMessage):
                result = messages[index]
                assert isinstance(result, ToolResultMessage)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": result.tool_call_id,
                        "content": _convert_block_content(result.content),
                        "is_error": result.is_error,
                    }
                )
                index += 1
            params.append({"role": "user", "content": tool_results})
            continue

        index += 1

    return params


def normalize_tool_call_id(tool_call_id: str) -> str:
    """Fold an id from another provider into Anthropic's accepted charset.

    OpenAI Responses/Codex ids can contain ``|``, ``+``, ``/`` and ``=`` and run
    past 400 characters; Anthropic rejects anything outside
    ``^[a-zA-Z0-9_-]+$`` longer than 64 characters.
    """
    return re.sub(r"[^a-zA-Z0-9_-]", "_", tool_call_id)[:ANTHROPIC_TOOL_CALL_ID_MAX_LENGTH]


def _normalize_ids(messages: list[Message]) -> list[Message]:
    """Rewrite tool call ids and the results referencing them, without mutating.

    ``transform_messages`` may return the caller's ``ToolResultMessage`` objects,
    so copies are made wherever an id changes.
    """
    replacements: dict[str, str] = {}
    normalized: list[Message] = []
    for message in messages:
        if isinstance(message, AssistantMessage):
            changed = False
            content: list[Any] = []
            for block in message.content:
                if isinstance(block, ToolCall):
                    new_id = normalize_tool_call_id(block.id)
                    if new_id != block.id:
                        replacements[block.id] = new_id
                        changed = True
                        content.append(
                            ToolCall(
                                id=new_id,
                                name=block.name,
                                arguments=block.arguments,
                                thought_signature=block.thought_signature,
                                namespace=block.namespace,
                            )
                        )
                        continue
                content.append(block)
            if changed:
                message = AssistantMessage(
                    content=content,
                    api=message.api,
                    provider=message.provider,
                    model=message.model,
                    usage=message.usage,
                    stop_reason=message.stop_reason,
                    timestamp=message.timestamp,
                    response_model=message.response_model,
                    response_id=message.response_id,
                    provider_thinking_level=message.provider_thinking_level,
                    diagnostics=message.diagnostics,
                    deferred=message.deferred,
                    error_message=message.error_message,
                    raw_stop_reason=message.raw_stop_reason,
                    end_turn=message.end_turn,
                )
            normalized.append(message)
        elif isinstance(message, ToolResultMessage):
            replacement = replacements.get(message.tool_call_id)
            if replacement is not None:
                message = ToolResultMessage(
                    tool_call_id=replacement,
                    tool_name=message.tool_name,
                    content=message.content,
                    is_error=message.is_error,
                    timestamp=message.timestamp,
                    details=message.details,
                    usage=message.usage,
                    added_tool_names=message.added_tool_names,
                )
            normalized.append(message)
        else:
            normalized.append(message)
    return normalized


def _convert_tools(tools: list[Any]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        parameters = tool.parameters or {"type": "object", "properties": {}}
        converted.append(
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": {
                    "type": "object",
                    "properties": parameters.get("properties") or {},
                    "required": parameters.get("required") or [],
                },
            }
        )
    return converted


def build_params(model: Model, context: Context, options: StreamOptions) -> dict[str, Any]:
    """Assemble the ``/v1/messages`` request body."""
    messages = transform_messages(context.messages, model)
    # Anthropic refuses ids minted by other providers, so they are sanitised
    # after the shared transform and before the wire encoding.
    messages = _normalize_ids(messages)
    thinking = _resolve_thinking(model, options)

    max_tokens = options.max_tokens if options.max_tokens is not None else model.max_tokens
    if thinking is not None and thinking.get("type") == "enabled":
        # Thinking and the answer share max_tokens.  A caller-provided cap is
        # widened by the thinking budget so the answer still has room, and the
        # budget is clamped so it never eats the whole allowance.
        budget = int(thinking["budget_tokens"])
        if options.max_tokens is None:
            max_tokens = model.max_tokens
        else:
            max_tokens = min(options.max_tokens + budget, model.max_tokens)
        if max_tokens <= budget:
            budget = min(budget, max(0, max_tokens - MIN_ANSWER_TOKENS))
        thinking["budget_tokens"] = budget

    params: dict[str, Any] = {
        "model": model.id,
        "messages": _convert_messages(messages),
        "max_tokens": max_tokens,
        "stream": True,
    }

    if context.system_prompt:
        params["system"] = [{"type": "text", "text": context.system_prompt}]

    # Extended thinking rejects a custom temperature on current Claude models.
    if options.temperature is not None and thinking is None:
        params["temperature"] = options.temperature

    if thinking is not None:
        params["thinking"] = thinking

    if context.tools:
        params["tools"] = _convert_tools(context.tools)

    return params


def map_stop_reason(reason: str, stop_details: dict[str, Any] | None = None) -> tuple[str, str | None]:
    """Anthropic ``stop_reason`` to pi ``StopReason`` (+ optional error message)."""
    if reason in ("end_turn", "pause_turn", "stop_sequence"):
        return "stop", None
    if reason == "max_tokens":
        return "length", None
    if reason == "tool_use":
        return "toolUse", None
    if reason == "refusal":
        explanation = (stop_details or {}).get("explanation")
        return "error", explanation or "The model refused to complete the request"
    if reason == "sensitive":
        return "error", "Provider stopped with: sensitive"
    # Unknown stop reasons are a hard failure: silently mapping them to "stop"
    # would let an incomplete response look complete.
    raise ProviderError(f"Unhandled stop reason: {reason}")


# --------------------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------------------


def _recompute_total(usage: Usage) -> None:
    usage.total_tokens = usage.input + usage.output + usage.cache_read + usage.cache_write


class AnthropicMessagesProvider:
    """``ProviderStreams`` for the ``anthropic-messages`` API.

    ``client_factory`` is injectable so tests can supply an ``httpx`` client
    backed by ``httpx.MockTransport`` and still exercise the real parsing code.
    An injected client is closed by the provider.
    """

    def __init__(self, client_factory: ClientFactory | None = None) -> None:
        self._client_factory = client_factory or _default_client_factory

    # -- ProviderStreams ---------------------------------------------------------------

    def stream(
        self, model: Model, context: Context, options: StreamOptions
    ) -> EventStream[AssistantMessageEvent, AssistantMessage]:
        """Never raises for setup problems: the failure becomes an ``error`` event."""
        stream = create_assistant_message_event_stream()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as error:
            # Without a loop the background task could never run; report it in-band
            # rather than raising out of a synchronous API.
            return create_error_stream(model, error)
        loop.create_task(self._run(stream, model, context, options))
        return stream

    def stream_simple(
        self, model: Model, context: Context, options: StreamOptions
    ) -> EventStream[AssistantMessageEvent, AssistantMessage]:
        """May raise synchronously (missing auth), which callers use for setup UX."""
        _assert_auth(model, options)
        return self.stream(model, context, options)

    # -- Implementation ----------------------------------------------------------------

    async def _run(
        self, stream: EventStream[AssistantMessageEvent, AssistantMessage], model: Model, context: Context, options: StreamOptions
    ) -> None:
        signal = options.signal
        output = AssistantMessage(
            content=[],
            api=model.api,
            provider=model.provider,
            model=model.id,
            usage=Usage(cost=Cost()),
            stop_reason="pending",
            timestamp=int(time.time() * 1000),
        )
        # provider content-block index -> (index into output.content, block)
        blocks: dict[int, tuple[int, Any]] = {}
        partial_json: dict[int, str] = {}
        saw_message_start = False
        saw_message_stop = False
        client: httpx.AsyncClient | None = None

        try:
            _assert_auth(model, options)
            body = build_params(model, context, options)
            client = self._client_factory(model, options)
            headers = _build_headers(model, options)
            url = _messages_url(model.base_url)

            async with client.stream("POST", url, json=body, headers=headers) as response:
                if response.status_code >= 400:
                    detail = await _read_error_body(response)
                    raise ProviderError(
                        f"Anthropic request failed with status {response.status_code}: {detail}",
                        provider=model.provider,
                        status=response.status_code,
                    )

                stream.push(StartEvent(partial=output))
                decoder = SseDecoder()

                def handle_events(events: list[tuple[str | None, str]]) -> None:
                    nonlocal saw_message_start, saw_message_stop
                    for event_name, data in events:
                        if event_name == "error":
                            raise ProviderError(data or "Anthropic stream error", provider=model.provider)
                        if event_name not in _MESSAGE_EVENTS:
                            continue
                        try:
                            payload = json.loads(data)
                        except ValueError as error:
                            raise ProviderError(
                                f"Could not parse Anthropic SSE event {event_name}: {error}",
                                provider=model.provider,
                            ) from error
                        event_type = payload.get("type")
                        if event_type == "message_start":
                            saw_message_start = True
                        elif event_type == "message_stop":
                            saw_message_stop = True
                        _apply_event(stream, output, blocks, partial_json, event_type, payload, model)

                async for chunk in response.aiter_text():
                    if signal is not None and signal.aborted:
                        raise ProviderError("Request was aborted", provider=model.provider)
                    handle_events(decoder.feed(chunk))
                handle_events(decoder.flush())

            if signal is not None and signal.aborted:
                raise ProviderError("Request was aborted", provider=model.provider)
            if saw_message_start and not saw_message_stop:
                raise ProviderError("Anthropic stream ended before message_stop", provider=model.provider)
            if output.stop_reason == "pending":
                raise ProviderError("Anthropic stream ended without a stop reason", provider=model.provider)
            if output.stop_reason in ("error", "aborted"):
                raise ProviderError(output.error_message or "An unknown error occurred", provider=model.provider)

            stream.push(DoneEvent(reason=output.stop_reason, message=output))
            stream.end(output)
        except Exception as error:  # noqa: BLE001 - the contract encodes failures in-band
            if signal is not None and signal.aborted:
                output.stop_reason = "aborted"
                output.error_message = signal.reason or "Request was aborted"
            else:
                output.stop_reason = "error"
                output.error_message = str(error) or error.__class__.__name__
            stream.push(ErrorEvent(reason=output.stop_reason, error=output))
            stream.end(output)
        finally:
            if client is not None:
                await client.aclose()


async def _read_error_body(response: httpx.Response) -> str:
    try:
        raw = await response.aread()
    except Exception:  # noqa: BLE001 - a body we cannot read must not hide the status
        return "<unreadable body>"
    text = raw.decode("utf-8", errors="replace").strip()
    return text[:MAX_ERROR_BODY_CHARS] or "<empty body>"


def _apply_event(
    stream: EventStream[AssistantMessageEvent, AssistantMessage],
    output: AssistantMessage,
    blocks: dict[int, tuple[int, Any]],
    partial_json: dict[int, str],
    event_type: str,
    payload: dict[str, Any],
    model: Model,
) -> None:
    """Mutate the shared ``output`` and push the matching protocol events."""
    if event_type == "message_start":
        message = payload.get("message") or {}
        if message.get("id"):
            output.response_id = message["id"]
        if message.get("model"):
            output.model = message["model"]
        usage = message.get("usage") or {}
        output.usage.input = usage.get("input_tokens") or 0
        output.usage.output = usage.get("output_tokens") or 0
        output.usage.cache_read = usage.get("cache_read_input_tokens") or 0
        output.usage.cache_write = usage.get("cache_creation_input_tokens") or 0
        output.usage.cache_write_1h = (usage.get("cache_creation") or {}).get(
            "ephemeral_1h_input_tokens"
        ) or 0
        _recompute_total(output.usage)
        calculate_cost(model, output.usage)
        return

    if event_type == "content_block_start":
        block_data = payload.get("content_block") or {}
        index = payload.get("index", 0)
        kind = block_data.get("type")
        content_index = len(output.content)

        if kind == "text":
            block = TextContent(text=block_data.get("text") or "")
            output.content.append(block)
            blocks[index] = (content_index, block)
            stream.push(TextStartEvent(content_index=content_index, partial=output))
        elif kind == "thinking":
            block = ThinkingContent(
                thinking=block_data.get("thinking") or "",
                thinking_signature=block_data.get("signature") or "",
            )
            output.content.append(block)
            blocks[index] = (content_index, block)
            stream.push(ThinkingStartEvent(content_index=content_index, partial=output))
        elif kind == "redacted_thinking":
            # The ciphertext is opaque; the visible thinking text is a marker.
            block = ThinkingContent(
                thinking="[Reasoning redacted]",
                thinking_signature=block_data.get("data") or "",
                redacted=True,
            )
            output.content.append(block)
            blocks[index] = (content_index, block)
            stream.push(ThinkingStartEvent(content_index=content_index, partial=output))
        elif kind == "tool_use":
            block = ToolCall(
                id=block_data.get("id") or "",
                name=block_data.get("name") or "",
                arguments=block_data.get("input") or {},
            )
            output.content.append(block)
            blocks[index] = (content_index, block)
            partial_json[index] = ""
            stream.push(ToolCallStartEvent(content_index=content_index, partial=output))
        return

    if event_type == "content_block_delta":
        index = payload.get("index", 0)
        delta = payload.get("delta") or {}
        delta_type = delta.get("type")
        entry = blocks.get(index)
        if entry is None:
            return
        content_index, block = entry

        if delta_type == "text_delta" and isinstance(block, TextContent):
            text = delta.get("text") or ""
            block.text += text
            stream.push(TextDeltaEvent(content_index=content_index, delta=text, partial=output))
        elif delta_type == "thinking_delta" and isinstance(block, ThinkingContent):
            thinking = delta.get("thinking") or ""
            block.thinking += thinking
            stream.push(
                                ThinkingDeltaEvent(content_index=content_index, delta=thinking, partial=output),
            )
        elif delta_type == "input_json_delta" and isinstance(block, ToolCall):
            fragment = delta.get("partial_json") or ""
            partial_json[index] = partial_json.get(index, "") + fragment
            block.arguments = parse_streaming_json(partial_json[index])
            stream.push(
                                ToolCallDeltaEvent(content_index=content_index, delta=fragment, partial=output),
            )
        elif delta_type == "signature_delta" and isinstance(block, ThinkingContent):
            # Not user-visible, and must be replayed byte-for-byte.
            block.thinking_signature = (block.thinking_signature or "") + (delta.get("signature") or "")
        return

    if event_type == "content_block_stop":
        index = payload.get("index", 0)
        entry = blocks.pop(index, None)
        if entry is None:
            return
        content_index, block = entry
        if isinstance(block, TextContent):
            stream.push(TextEndEvent(content_index=content_index, content=block.text, partial=output))
        elif isinstance(block, ThinkingContent):
            stream.push(
                                ThinkingEndEvent(content_index=content_index, content=block.thinking, partial=output),
            )
        elif isinstance(block, ToolCall):
            fragment = partial_json.pop(index, "")
            if fragment:
                block.arguments = parse_streaming_json(fragment)
            stream.push(ToolCallEndEvent(content_index=content_index, tool_call=block, partial=output))
        return

    if event_type == "message_delta":
        delta = payload.get("delta") or {}
        raw_reason = delta.get("stop_reason")
        if raw_reason:
            output.raw_stop_reason = raw_reason
            output.stop_reason, error_message = map_stop_reason(raw_reason, delta.get("stop_details"))
            if error_message:
                output.error_message = error_message
        usage = payload.get("usage")
        if usage:
            # Only present fields are applied: proxies routinely omit
            # input_tokens here and message_start must keep its value.
            if usage.get("input_tokens") is not None:
                output.usage.input = usage["input_tokens"]
            if usage.get("output_tokens") is not None:
                output.usage.output = usage["output_tokens"]
            if usage.get("cache_read_input_tokens") is not None:
                output.usage.cache_read = usage["cache_read_input_tokens"]
            if usage.get("cache_creation_input_tokens") is not None:
                output.usage.cache_write = usage["cache_creation_input_tokens"]
            thinking_tokens = (usage.get("output_tokens_details") or {}).get("thinking_tokens")
            if thinking_tokens is not None:
                output.usage.reasoning = thinking_tokens
        _recompute_total(output.usage)
        calculate_cost(model, output.usage)
        return

    # message_stop and anything else need no state change.


def anthropic_messages_api() -> AnthropicMessagesProvider:
    """Factory used by the provider registry."""
    return AnthropicMessagesProvider()


__all__ = [
    "ANTHROPIC_TOOL_CALL_ID_MAX_LENGTH",
    "ANTHROPIC_VERSION",
    "AnthropicMessagesProvider",
    "SseDecoder",
    "anthropic_messages_api",
    "build_params",
    "map_stop_reason",
    "normalize_tool_call_id",
]

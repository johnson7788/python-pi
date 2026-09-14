"""OpenAI Chat Completions adapter (port of ``api/openai-completions.ts``).

Works against ``POST {base_url}/chat/completions`` on any OpenAI-compatible
endpoint (OpenAI, OpenRouter, DeepSeek, Moonshot, llama.cpp, ...), so the
adapter is deliberately tolerant: the wire format is the common denominator and
provider quirks are switches in ``model.compat``.

Streaming is a block state machine.  Unlike Anthropic, the Completions protocol
has no block lifecycle events at all, so blocks are *invented* here:

* text and thinking are singletons, lazily created by the first delta that
  carries content;
* tool calls are keyed by both ``index`` and ``id`` because some providers omit
  one or the other;
* because providers may never emit a stop event, every still-open block is
  finished explicitly once the stream ends.

Usage is only trustworthy in the final chunk, and ``prompt_tokens`` *includes*
cached tokens, so the input count is a subtraction rather than a direct read.
"""

from __future__ import annotations

import asyncio

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
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
from ..stream import Context, StreamOptions, create_error_stream
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

MAX_ERROR_BODY_CHARS = 4000
#: Length cap the Chat Completions API documents for tool call ids.
OPENAI_TOOL_CALL_ID_MAX_LENGTH = 40

_REASONING_FIELDS = ("reasoning_content", "reasoning", "reasoning_text")

ClientFactory = Callable[[Model, StreamOptions], httpx.AsyncClient]


# --------------------------------------------------------------------------------------
# SSE decoding
# --------------------------------------------------------------------------------------


def _consume_line(text: str) -> tuple[str, str] | None:
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


class SseDataBuffer:
    """Collects ``data:`` payloads; the SDK normally does this, we do it by hand."""

    __slots__ = ("_buffer", "_data")

    def __init__(self) -> None:
        self._buffer = ""
        self._data: list[str] = []

    def feed(self, text: str) -> list[str]:
        self._buffer += text
        events: list[str] = []
        while True:
            consumed = _consume_line(self._buffer)
            if consumed is None:
                break
            line, self._buffer = consumed
            payload = self._line(line)
            if payload is not None:
                events.append(payload)
        return events

    def flush(self) -> list[str]:
        events: list[str] = []
        if self._buffer:
            line, self._buffer = self._buffer, ""
            payload = self._line(line)
            if payload is not None:
                events.append(payload)
        payload = self._flush_event()
        if payload is not None:
            events.append(payload)
        return events

    def _flush_event(self) -> str | None:
        if not self._data:
            return None
        payload = "\n".join(self._data)
        self._data = []
        return payload

    def _line(self, line: str) -> str | None:
        if line == "":
            return self._flush_event()
        if line.startswith(":"):
            return None
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data.append(value)
        return None


# --------------------------------------------------------------------------------------
# Compat switches
# --------------------------------------------------------------------------------------


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _compat_get(compat: dict[str, Any], key: str, default: Any) -> Any:
    """Read a compat switch by snake_case name, then by its upstream camelCase name."""
    if key in compat:
        return compat[key]
    camel = _camel(key)
    if camel in compat:
        return compat[camel]
    return default


def detect_compat(model: Model) -> dict[str, Any]:
    """Derive default switches from provider/base URL, as upstream ``detectCompat``."""
    provider = model.provider or ""
    base_url = (model.base_url or "").lower()
    is_openai = provider in ("openai", "azure-openai") or "api.openai.com" in base_url
    is_openrouter = provider == "openrouter" or "openrouter.ai" in base_url
    is_deepseek = provider == "deepseek" or "deepseek.com" in base_url

    return {
        # `store: false` is OpenAI-only; gateways reject unknown top-level fields.
        "supports_store": not is_openrouter,
        "supports_developer_role": is_openai,
        "supports_reasoning_effort": True,
        "supports_finish_reason": True,
        "supports_thinking_token_budget": False,
        "max_tokens_field": "max_completion_tokens" if is_openai else "max_tokens",
        "requires_reasoning_content_on_assistant_messages": is_deepseek,
        "requires_assistant_after_tool_result": False,
        "requires_tool_result_name": False,
        "thinking_as_text": False,
    }


def get_compat(model: Model) -> dict[str, Any]:
    """Detected defaults, overridden key by key by explicit ``model.compat``."""
    resolved = detect_compat(model)
    for key, value in (model.compat or {}).items():
        resolved[key] = value
    return resolved


# --------------------------------------------------------------------------------------
# Tool call id normalisation
# --------------------------------------------------------------------------------------


def _js_imul(a: int, b: int) -> int:
    return ((a & 0xFFFFFFFF) * (b & 0xFFFFFFFF)) & 0xFFFFFFFF


def _base36(value: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    out: list[str] = []
    while value:
        value, remainder = divmod(value, 36)
        out.append(digits[remainder])
    return "".join(reversed(out))


def short_hash(text: str) -> str:
    """Deterministic 32-bit hash, byte-compatible with the JS ``shortHash``."""
    h1 = 0xDEADBEEF
    h2 = 0x41C6CE57
    # JS charCodeAt walks UTF-16 code units, not code points.
    units = memoryview(text.encode("utf-16-le")).cast("H")
    for unit in units:
        h1 = _js_imul(h1 ^ unit, 2654435761)
        h2 = _js_imul(h2 ^ unit, 1597334677)
    h1 = _js_imul(h1 ^ (h1 >> 16), 2246822507) ^ _js_imul(h2 ^ (h2 >> 13), 3266489909)
    h2 = _js_imul(h2 ^ (h2 >> 16), 2246822507) ^ _js_imul(h1 ^ (h1 >> 13), 3266489909)
    return _base36(h2 & 0xFFFFFFFF) + _base36(h1 & 0xFFFFFFFF)


def normalize_tool_call_id(tool_call_id: str, model: Model) -> str:
    """Fold Responses-API/Codex ids into something Chat Completions accepts.

    Those ids look like ``{call_id}|{item_id}`` and the item half can exceed 400
    characters with ``+``/``/``/``=`` in it.  Both halves are sanitised and
    recombined so two calls sharing a ``call_id`` stay distinct.
    """
    if "|" in tool_call_id:
        separator = tool_call_id.index("|")
        call_id = re.sub(r"[^a-zA-Z0-9_-]", "_", tool_call_id[:separator])
        item_id = re.sub(r"[^a-zA-Z0-9_-]", "_", tool_call_id[separator + 1 :])
        combined = f"{call_id}_{item_id}" if item_id else call_id
        if len(combined) <= OPENAI_TOOL_CALL_ID_MAX_LENGTH:
            return combined
        digest = short_hash(tool_call_id)[:8]
        prefix = call_id[: max(1, OPENAI_TOOL_CALL_ID_MAX_LENGTH - len(digest) - 1)]
        return f"{prefix}_{digest}"

    if model.provider == "openai" and len(tool_call_id) > OPENAI_TOOL_CALL_ID_MAX_LENGTH:
        return tool_call_id[:OPENAI_TOOL_CALL_ID_MAX_LENGTH]
    return tool_call_id


# --------------------------------------------------------------------------------------
# Request construction
# --------------------------------------------------------------------------------------


def _completions_url(base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _assert_auth(model: Model, options: StreamOptions) -> None:
    headers = {key.lower() for key in (options.headers or {})}
    if options.api_key or "authorization" in headers:
        return
    raise AuthenticationError(model.provider, f"No API key for provider: {model.provider}")


def _build_headers(model: Model, options: StreamOptions) -> dict[str, str]:
    headers: dict[str, str] = {
        "content-type": "application/json",
        "accept": "text/event-stream",
    }
    if model.headers:
        headers.update(model.headers)
    if options.headers:
        headers.update(options.headers)
    if options.api_key:
        headers["authorization"] = f"Bearer {options.api_key}"
    return headers


#: How often a streaming response is checked for an abort while waiting for the next chunk.
#: Short enough that "stop" feels immediate, long enough to cost nothing.
_STREAM_POLL_SECONDS = 0.5

#: Give up if the model sends nothing for this long. The httpx read timeout (600s by default)
#: is the backstop for a dead connection; this is the bound a person actually waits out.
_STREAM_STALL_SECONDS = 180.0


def _default_client_factory(model: Model, options: StreamOptions) -> httpx.AsyncClient:
    timeout: Any = options.request_timeout if options.request_timeout is not None else 600.0
    return httpx.AsyncClient(timeout=timeout)


def _image_part(block: ImageContent) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": f"data:{block.mime_type};base64,{block.data}"}}


def _parse_reasoning_details(signature: str | None) -> list[Any] | None:
    """``thinking_signature`` doubles as the replay slot for ``reasoning_details``."""
    if not signature:
        return None
    try:
        details = json.loads(signature)
    except ValueError:
        return None
    return details if isinstance(details, list) else None


def _convert_user_message(message: UserMessage) -> dict[str, Any] | None:
    if isinstance(message.content, str):
        return {"role": "user", "content": message.content}
    parts: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, TextContent):
            parts.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageContent):
            parts.append(_image_part(block))
    if not parts:
        return None
    return {"role": "user", "content": parts}


def _convert_assistant_message(
    message: AssistantMessage, model: Model, compat: dict[str, Any]
) -> dict[str, Any] | None:
    requires_after_tool_result = bool(_compat_get(compat, "requires_assistant_after_tool_result", False))
    assistant: dict[str, Any] = {
        "role": "assistant",
        # Some providers reject a null content field.
        "content": "" if requires_after_tool_result else None,
    }

    text_parts = [
        block.text
        for block in message.content
        if isinstance(block, TextContent) and block.text.strip()
    ]
    assistant_text = "".join(text_parts)
    thinking_blocks = [b for b in message.content if isinstance(b, ThinkingContent)]
    tool_calls = [b for b in message.content if isinstance(b, ToolCall)]
    non_empty_thinking = [b for b in thinking_blocks if b.thinking.strip()]

    preserved_details: list[Any] | None = None
    for block in thinking_blocks:
        details = _parse_reasoning_details(block.thinking_signature)
        if details is not None:
            preserved_details = details
            break

    if non_empty_thinking:
        if _compat_get(compat, "thinking_as_text", False):
            # No tags: models tend to mimic markup they see in their own history.
            thinking_text = "\n\n".join(block.thinking for block in non_empty_thinking)
            assistant["content"] = [{"type": "text", "text": thinking_text}, *(
                {"type": "text", "text": part} for part in text_parts
            )]
        else:
            if assistant_text:
                assistant["content"] = assistant_text
            if preserved_details is None:
                # llama.cpp/gpt-oss style: the signature names the reasoning field.
                signature = non_empty_thinking[0].thinking_signature
                if signature in _REASONING_FIELDS:
                    assistant[signature] = "\n".join(block.thinking for block in non_empty_thinking)
    elif assistant_text:
        assistant["content"] = assistant_text

    if tool_calls:
        assistant["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in tool_calls
        ]

    if preserved_details is not None:
        assistant["reasoning_details"] = preserved_details

    if (
        _compat_get(compat, "requires_reasoning_content_on_assistant_messages", False)
        and model.reasoning
        and assistant.get("reasoning_content") is None
    ):
        assistant["reasoning_content"] = ""

    content = assistant["content"]
    has_content = bool(content) if isinstance(content, (str, list)) else content is not None
    if not has_content and not assistant.get("tool_calls"):
        # Providers require "content or tool_calls, but not neither".
        return None
    return assistant


def _convert_tool_result(
    message: ToolResultMessage, compat: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    text = "\n".join(block.text for block in message.content if isinstance(block, TextContent))
    images = [block for block in message.content if isinstance(block, ImageContent)]
    content = text or ("(see attached image)" if images else "(no tool output)")
    result: dict[str, Any] = {
        "role": "tool",
        "content": content,
        "tool_call_id": message.tool_call_id,
    }
    if _compat_get(compat, "requires_tool_result_name", False) and message.tool_name:
        result["name"] = message.tool_name
    return result, [_image_part(block) for block in images]


def _normalize_ids(messages: list[Message], model: Model) -> list[Message]:
    """Rewrite tool call ids (and the results that reference them) once each.

    Copies are produced instead of mutating, because ``transform_messages``
    reuses ``ToolResultMessage`` objects and the caller's transcript must stay
    untouched.
    """
    replacements: dict[str, str] = {}
    normalized: list[Message] = []
    for message in messages:
        if isinstance(message, AssistantMessage):
            changed = False
            content: list[Any] = []
            for block in message.content:
                if isinstance(block, ToolCall):
                    new_id = normalize_tool_call_id(block.id, model)
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


def _has_tool_history(messages: list[Message]) -> bool:
    for message in messages:
        if isinstance(message, ToolResultMessage):
            return True
        if isinstance(message, AssistantMessage) and any(
            isinstance(block, ToolCall) for block in message.content
        ):
            return True
    return False


def convert_messages(model: Model, context: Context, compat: dict[str, Any]) -> list[dict[str, Any]]:
    messages = _normalize_ids(transform_messages(context.messages, model), model)
    params: list[dict[str, Any]] = []

    if context.system_prompt:
        # Reasoning models on the first-party API expect `developer`; everyone
        # else only understands `system`.
        use_developer = bool(model.reasoning and _compat_get(compat, "supports_developer_role", False))
        params.append({"role": "developer" if use_developer else "system", "content": context.system_prompt})

    last_role: str | None = None
    index = 0
    while index < len(messages):
        message = messages[index]

        if _compat_get(compat, "requires_assistant_after_tool_result", False) and last_role == "toolResult" and isinstance(message, UserMessage):
            params.append({"role": "assistant", "content": "I have processed the tool results."})

        if isinstance(message, UserMessage):
            converted = _convert_user_message(message)
            if converted is not None:
                params.append(converted)
                last_role = "user"
            index += 1
            continue

        if isinstance(message, AssistantMessage):
            converted = _convert_assistant_message(message, model, compat)
            if converted is not None:
                params.append(converted)
                last_role = "assistant"
            index += 1
            continue

        if isinstance(message, ToolResultMessage):
            image_parts: list[dict[str, Any]] = []
            while index < len(messages) and isinstance(messages[index], ToolResultMessage):
                result = messages[index]
                assert isinstance(result, ToolResultMessage)
                converted, images = _convert_tool_result(result, compat)
                params.append(converted)
                if "image" in model.input:
                    image_parts.extend(images)
                index += 1

            if image_parts:
                # Tool messages carry no images in this protocol, so they are
                # re-attached as a following user turn.
                params.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Attached image(s) from tool result:"},
                            *image_parts,
                        ],
                    }
                )
                last_role = "user"
            else:
                last_role = "toolResult"
            continue

        index += 1

    return params


def _convert_tools(tools: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters or {"type": "object", "properties": {}},
            },
        }
        for tool in tools
    ]


def _resolve_reasoning_effort(model: Model, options: StreamOptions, compat: dict[str, Any]) -> str | None:
    """Map the abstract level onto ``reasoning_effort``.

    ``"off"`` means "do not ask for reasoning", so the field is **omitted** — sending
    the literal string ``"off"`` is rejected by real providers (DashScope answers
    ``'reasoning_effort' must be one of: 'low', 'medium', 'high', 'xhigh', 'max'``).
    Upstream does the same: ``clampedReasoning === "off" ? undefined : clampedReasoning``.
    An explicit ``thinkingLevelMap["off"]`` is the escape hatch for providers that
    spell "off" as their own value (``"none"``, ``"minimal"``, ...).
    """
    level = options.reasoning
    if level is None:
        return None

    level_map = model.thinking_level_map or {}

    if level == "off":
        mapped_off = level_map.get("off")
        return mapped_off if isinstance(mapped_off, str) else None

    if not _compat_get(compat, "supports_reasoning_effort", True):
        return None

    mapped = level_map.get(level, level)
    if not isinstance(mapped, str):
        return None
    return mapped


def build_params(model: Model, context: Context, options: StreamOptions, compat: dict[str, Any]) -> dict[str, Any]:
    messages = convert_messages(model, context, compat)

    params: dict[str, Any] = {"model": model.id, "messages": messages, "stream": True}
    if _compat_get(compat, "supports_usage_in_streaming", True) is not False:
        params["stream_options"] = {"include_usage": True}
    if _compat_get(compat, "supports_store", True):
        params["store"] = False

    if options.max_tokens:
        field_name = _compat_get(compat, "max_tokens_field", "max_tokens")
        params[field_name] = options.max_tokens
    if options.temperature is not None:
        params["temperature"] = options.temperature

    if context.tools:
        params["tools"] = _convert_tools(context.tools)
    elif _has_tool_history(context.messages):
        # Anthropic-via-proxy endpoints reject a tool message without the field.
        params["tools"] = []

    tool_choice = (options.provider_options or {}).get("tool_choice")
    if tool_choice is not None:
        params["tool_choice"] = tool_choice

    effort = _resolve_reasoning_effort(model, options, compat)
    if effort is not None:
        params["reasoning_effort"] = effort

    thinking_token_budget = (options.provider_options or {}).get("thinking_token_budget")
    budget_field = _compat_get(compat, "thinking_token_budget_field", None)
    if budget_field is None and _compat_get(compat, "supports_thinking_token_budget", False):
        budget_field = "thinking_token_budget"
    if thinking_token_budget is not None and budget_field:
        params[budget_field] = thinking_token_budget

    # Custom sampling parameters are merged last so they can override named
    # fields, exactly like upstream `Object.assign(params, samplingParams)`.
    sampling: dict[str, Any] = dict(model.sampling_params or {})
    sampling.update((options.provider_options or {}).get("sampling_params") or {})
    for key, value in sampling.items():
        if key in ("model", "messages", "stream"):
            continue
        params[key] = value

    return params


def map_stop_reason(reason: str | None) -> tuple[str, str | None]:
    if reason is None:
        return "stop", None
    if reason in ("stop", "end"):
        return "stop", None
    if reason == "length":
        return "length", None
    if reason in ("function_call", "tool_calls"):
        return "toolUse", None
    if reason == "content_filter":
        return "error", "Provider finish_reason: content_filter"
    if reason == "network_error":
        return "error", "Provider finish_reason: network_error"
    return "error", f"Provider finish_reason: {reason}"


def parse_chunk_usage(raw: dict[str, Any], model: Model) -> Usage:
    """``prompt_tokens`` includes cached tokens, so input is computed by subtraction."""
    prompt_tokens = raw.get("prompt_tokens") or 0
    details = raw.get("prompt_tokens_details") or {}
    cache_read = (
        details.get("cached_tokens")
        if details.get("cached_tokens") is not None
        else raw.get("prompt_cache_hit_tokens")
        if raw.get("prompt_cache_hit_tokens") is not None
        else raw.get("cached_tokens") or 0
    )
    cache_write = details.get("cache_write_tokens") or 0
    input_tokens = max(0, prompt_tokens - cache_read - cache_write)
    output_tokens = raw.get("completion_tokens") or 0
    completion_details = raw.get("completion_tokens_details") or {}

    usage = Usage(
        input=input_tokens,
        output=output_tokens,
        cache_read=cache_read,
        cache_write=cache_write,
        reasoning=completion_details.get("reasoning_tokens") or 0,
        total_tokens=input_tokens + output_tokens + cache_read + cache_write,
        cost=Cost(),
    )
    calculate_cost(model, usage)
    return usage


# --------------------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------------------


@dataclass
class _StreamState:
    text_block: TextContent | None = None
    thinking_block: ThinkingContent | None = None
    text_index: int | None = None
    thinking_index: int | None = None
    tool_calls_by_index: dict[int, ToolCall] = field(default_factory=dict)
    tool_calls_by_id: dict[str, ToolCall] = field(default_factory=dict)
    #: ``contentIndex`` per tool call, keyed by ``id(block)``.  Explicit indices
    #: are required because ``list.index`` compares by value and two freshly
    #: created empty ToolCalls would compare equal.
    tool_indices: dict[int, int] = field(default_factory=dict)
    #: JSON fragment buffer per tool call, keyed by ``id(block)``.
    partial_args: dict[int, str] = field(default_factory=dict)
    reasoning_details: list[Any] | None = None
    has_finish_reason: bool = False


class OpenAiCompletionsProvider:
    """``ProviderStreams`` for the ``openai-completions`` API."""

    def __init__(self, client_factory: ClientFactory | None = None) -> None:
        self._client_factory = client_factory or _default_client_factory

    # -- ProviderStreams ---------------------------------------------------------------

    def stream(
        self, model: Model, context: Context, options: StreamOptions
    ) -> EventStream[AssistantMessageEvent, AssistantMessage]:
        stream = create_assistant_message_event_stream()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as error:
            return create_error_stream(model, error)
        loop.create_task(self._run(stream, model, context, options))
        return stream

    def stream_simple(
        self, model: Model, context: Context, options: StreamOptions
    ) -> EventStream[AssistantMessageEvent, AssistantMessage]:
        _assert_auth(model, options)
        return self.stream(model, context, options)

    # -- Implementation ----------------------------------------------------------------

    async def _run(
        self, stream: EventStream[AssistantMessageEvent, AssistantMessage], model: Model, context: Context, options: StreamOptions
    ) -> None:
        signal = options.signal
        compat = get_compat(model)
        output = AssistantMessage(
            content=[],
            api=model.api,
            provider=model.provider,
            model=model.id,
            usage=Usage(cost=Cost()),
            stop_reason="pending",
            timestamp=int(time.time() * 1000),
        )
        state = _StreamState()
        client: httpx.AsyncClient | None = None

        try:
            _assert_auth(model, options)
            body = build_params(model, context, options, compat)
            client = self._client_factory(model, options)
            headers = _build_headers(model, options)
            url = _completions_url(model.base_url)

            async with client.stream("POST", url, json=body, headers=headers) as response:
                if response.status_code >= 400:
                    detail = await _read_error_body(response)
                    raise ProviderError(
                        f"OpenAI-compatible request failed with status {response.status_code}: {detail}",
                        provider=model.provider,
                        status=response.status_code,
                    )

                stream.push(StartEvent(partial=output))
                decoder = SseDataBuffer()

                def handle_payloads(payloads: list[str]) -> None:
                    for payload in payloads:
                        if payload.strip() == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(payload)
                        except ValueError:
                            # A malformed keep-alive/data line is skipped rather
                            # than aborting an otherwise healthy stream.
                            continue
                        if isinstance(chunk, dict):
                            self._handle_chunk(stream, output, state, chunk, model, compat)

                # Abort and a stalled socket both have to be noticed *between* chunks.
                # Polling only inside the loop body means a stream that stops sending ignores
                # "stop" until the read timeout — ten minutes by default — which a user
                # experiences as a hung turn they cannot cancel.
                iterator = response.aiter_text().__aiter__()
                idle = 0.0
                while True:
                    pending = asyncio.ensure_future(iterator.__anext__())
                    while True:
                        done, _ = await asyncio.wait({pending}, timeout=_STREAM_POLL_SECONDS)
                        if done:
                            break
                        if signal is not None and signal.aborted:
                            pending.cancel()
                            raise ProviderError("Request was aborted", provider=model.provider)
                        idle += _STREAM_POLL_SECONDS
                        if idle >= _STREAM_STALL_SECONDS:
                            pending.cancel()
                            raise ProviderError(
                                f"the model sent nothing for {int(idle)} seconds; giving up",
                                provider=model.provider,
                            )
                    idle = 0.0
                    try:
                        text = pending.result()
                    except StopAsyncIteration:
                        break
                    handle_payloads(decoder.feed(text))
                handle_payloads(decoder.flush())

            self._finish_blocks(stream, output, state)

            if signal is not None and signal.aborted:
                raise ProviderError("Request was aborted", provider=model.provider)
            if not state.has_finish_reason and not _compat_get(compat, "supports_finish_reason", True):
                # Some gateways never send finish_reason; infer it.
                output.stop_reason = (
                    "toolUse" if any(isinstance(b, ToolCall) for b in output.content) else "stop"
                )
            if output.stop_reason == "error":
                raise ProviderError(output.error_message or "Provider returned an error stop reason", provider=model.provider)
            if (
                _compat_get(compat, "supports_finish_reason", True)
                and not state.has_finish_reason
            ) or output.stop_reason == "pending":
                raise ProviderError("Stream ended without finish_reason", provider=model.provider)

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

    # -- Chunk handling ----------------------------------------------------------------

    def _handle_chunk(
        self,
        stream: EventStream[AssistantMessageEvent, AssistantMessage],
        output: AssistantMessage,
        state: _StreamState,
        chunk: dict[str, Any],
        model: Model,
        compat: dict[str, Any],
    ) -> None:
        if chunk.get("id"):
            output.response_id = output.response_id or chunk["id"]
        chunk_model = chunk.get("model")
        if isinstance(chunk_model, str) and chunk_model and chunk_model != model.id:
            output.response_model = output.response_model or chunk_model

        if chunk.get("usage"):
            output.usage = parse_chunk_usage(chunk["usage"], model)

        choices = chunk.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        if not isinstance(choice, dict):
            return

        # Moonshot-style fallback: usage parked on the choice instead of the chunk.
        if not chunk.get("usage") and choice.get("usage"):
            output.usage = parse_chunk_usage(choice["usage"], model)

        finish_reason = choice.get("finish_reason")
        if finish_reason is not None:
            output.raw_stop_reason = finish_reason
            output.stop_reason, error_message = map_stop_reason(finish_reason)
            if error_message:
                output.error_message = error_message
            state.has_finish_reason = True

        delta = choice.get("delta")
        if not isinstance(delta, dict):
            return

        content = delta.get("content")
        if isinstance(content, str) and content:
            text_index = self._ensure_text_block(stream, output, state)
            block = state.text_block
            assert block is not None
            block.text += content
            stream.push(TextDeltaEvent(content_index=text_index, delta=content, partial=output))

        # Use the first non-empty reasoning field: some servers send two copies.
        reasoning_field = next(
            (field for field in _REASONING_FIELDS if isinstance(delta.get(field), str) and delta[field]),
            None,
        )
        if reasoning_field is not None:
            signature = reasoning_field
            if model.provider == "opencode-go" and reasoning_field == "reasoning":
                signature = "reasoning_content"
            thinking_index = self._ensure_thinking_block(stream, output, state, signature)
            block = state.thinking_block
            assert block is not None
            fragment = delta[reasoning_field]
            block.thinking += fragment
            stream.push(ThinkingDeltaEvent(content_index=thinking_index, delta=fragment, partial=output))

        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                block, tool_index = self._ensure_tool_call_block(stream, output, state, tool_call)
                if not block.id and tool_call.get("id"):
                    block.id = tool_call["id"]
                    state.tool_calls_by_id[tool_call["id"]] = block

                function = tool_call.get("function") or {}
                name = function.get("name")
                if not block.name and name:
                    block.name = name

                fragment = ""
                if function.get("arguments"):
                    fragment = function["arguments"]
                    key = id(block)
                    state.partial_args[key] = state.partial_args.get(key, "") + fragment
                    block.arguments = parse_streaming_json(state.partial_args[key])

                stream.push(ToolCallDeltaEvent(content_index=tool_index, delta=fragment, partial=output))

        details = delta.get("reasoning_details")
        if isinstance(details, list):
            # Replay metadata, not user-visible text: buffered and serialised
            # into the thinking signature when the block is finished.
            self._ensure_thinking_block(stream, output, state, "")
            if state.reasoning_details is None:
                state.reasoning_details = []
            state.reasoning_details.extend(details)

    def _ensure_text_block(
        self,
        stream: EventStream[AssistantMessageEvent, AssistantMessage],
        output: AssistantMessage,
        state: _StreamState,
    ) -> int:
        if state.text_block is None:
            block = TextContent(text="")
            output.content.append(block)
            state.text_block = block
            state.text_index = len(output.content) - 1
            stream.push(TextStartEvent(content_index=state.text_index, partial=output))
        assert state.text_index is not None
        return state.text_index

    def _ensure_thinking_block(
        self,
        stream: EventStream[AssistantMessageEvent, AssistantMessage],
        output: AssistantMessage,
        state: _StreamState,
        signature: str,
    ) -> int:
        if state.thinking_block is None:
            block = ThinkingContent(thinking="", thinking_signature=signature)
            output.content.append(block)
            state.thinking_block = block
            state.thinking_index = len(output.content) - 1
            stream.push(ThinkingStartEvent(content_index=state.thinking_index, partial=output))
        assert state.thinking_index is not None
        return state.thinking_index

    def _ensure_tool_call_block(
        self,
        stream: EventStream[AssistantMessageEvent, AssistantMessage],
        output: AssistantMessage,
        state: _StreamState,
        tool_call: dict[str, Any],
    ) -> tuple[ToolCall, int]:
        stream_index = tool_call.get("index")
        name = (tool_call.get("function") or {}).get("name") or ""
        block = state.tool_calls_by_index.get(stream_index) if isinstance(stream_index, int) else None
        if block is None and tool_call.get("id"):
            block = state.tool_calls_by_id.get(tool_call["id"])
        if block is None:
            block = ToolCall(id=tool_call.get("id") or "", name=name, arguments={})
            if isinstance(stream_index, int):
                state.tool_calls_by_index[stream_index] = block
            if block.id:
                state.tool_calls_by_id[block.id] = block
            output.content.append(block)
            state.partial_args[id(block)] = ""
            state.tool_indices[id(block)] = len(output.content) - 1
            stream.push(ToolCallStartEvent(content_index=state.tool_indices[id(block)], partial=output))
        else:
            if isinstance(stream_index, int):
                state.tool_calls_by_index[stream_index] = block
            if tool_call.get("id"):
                state.tool_calls_by_id[tool_call["id"]] = block
            if not block.name and name:
                block.name = name
        return block, state.tool_indices[id(block)]

    def _finish_blocks(
        self,
        stream: EventStream[AssistantMessageEvent, AssistantMessage],
        output: AssistantMessage,
        state: _StreamState,
    ) -> None:
        """Close every block, because the protocol has no stop events."""
        for index, block in enumerate(output.content):
            if isinstance(block, TextContent):
                stream.push(TextEndEvent(content_index=index, content=block.text, partial=output))
            elif isinstance(block, ThinkingContent):
                if state.reasoning_details is not None:
                    block.thinking_signature = json.dumps(state.reasoning_details)
                stream.push(ThinkingEndEvent(content_index=index, content=block.thinking, partial=output))
            elif isinstance(block, ToolCall):
                fragment = state.partial_args.pop(id(block), "")
                if fragment:
                    # Malformed JSON is salvaged rather than raised: the model
                    # still gets its (partial) arguments and validation decides.
                    block.arguments = parse_streaming_json(fragment)
                stream.push(ToolCallEndEvent(content_index=index, tool_call=block, partial=output))


async def _read_error_body(response: httpx.Response) -> str:
    try:
        raw = await response.aread()
    except Exception:  # noqa: BLE001
        return "<unreadable body>"
    text = raw.decode("utf-8", errors="replace").strip()
    return text[:MAX_ERROR_BODY_CHARS] or "<empty body>"


def openai_completions_api() -> OpenAiCompletionsProvider:
    """Factory used by the provider registry."""
    return OpenAiCompletionsProvider()


__all__ = [
    "OpenAiCompletionsProvider",
    "SseDataBuffer",
    "build_params",
    "convert_messages",
    "detect_compat",
    "get_compat",
    "map_stop_reason",
    "normalize_tool_call_id",
    "openai_completions_api",
    "parse_chunk_usage",
    "short_hash",
]

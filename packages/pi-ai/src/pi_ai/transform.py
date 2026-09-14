"""Provider-agnostic message normalisation (port of ``api/transform-messages.ts``).

Every adapter runs this before building a request body.  The three jobs it does
are all about keeping a *replayed* conversation valid for a *different* model
than the one that produced it:

* **Image downgrade.** A model that does not declare ``"image"`` in
  ``model.input`` must never receive an image block.  Image blocks become a
  short text placeholder instead, and consecutive images collapse into a single
  placeholder (upstream deduplicates so a screenshot burst does not repeat the
  sentence ten times).
* **Cross-model content normalisation.** ``thinking`` blocks are opaque replay
  data bound to the issuing provider/model: a signature minted by Anthropic is
  meaningless to OpenAI.  Blocks from another model are therefore dropped or
  downgraded to plain text, and ``ToolCall.thought_signature`` (Google's replay
  token) is stripped.
* **Tool-call/tool-result pairing.** Providers reject a ``tool_use`` without a
  matching ``tool_result`` (and vice versa), so orphaned calls get a synthetic
  error result and dangling results are dropped.

Upstream also skips ``stopReason === "error" | "aborted"`` assistant messages:
they may hold half-parsed reasoning or a truncated tool call whose replay makes
some APIs 400, and the model should retry from the last valid state.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .types import (
    AssistantMessage,
    AssistantContentBlock,
    ImageContent,
    Message,
    Model,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

NON_VISION_USER_IMAGE_PLACEHOLDER = "(image omitted: model does not support images)"
NON_VISION_TOOL_IMAGE_PLACEHOLDER = "(tool image omitted: model does not support images)"
#: Text of the synthetic result inserted for a tool call the transcript never answered.
NO_RESULT_PLACEHOLDER = "No result provided"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _replace_images_with_placeholder(
    content: list[TextContent | ImageContent], placeholder: str
) -> list[TextContent]:
    """Swap images for ``placeholder``, collapsing runs into one placeholder.

    ``previous_was_placeholder`` is seeded from an explicit text block equal to
    the placeholder so a hand-written placeholder followed by an image does not
    produce a duplicate sentence.
    """
    result: list[TextContent] = []
    previous_was_placeholder = False

    for block in content:
        if isinstance(block, ImageContent):
            if not previous_was_placeholder:
                result.append(TextContent(text=placeholder))
            previous_was_placeholder = True
            continue

        result.append(block)
        previous_was_placeholder = isinstance(block, TextContent) and block.text == placeholder

    return result


def _downgrade_unsupported_images(messages: list[Message], model: Model) -> list[Message]:
    if "image" in model.input:
        return messages

    downgraded: list[Message] = []
    for message in messages:
        if isinstance(message, UserMessage) and isinstance(message.content, list):
            downgraded.append(
                UserMessage(
                    content=_replace_images_with_placeholder(message.content, NON_VISION_USER_IMAGE_PLACEHOLDER),
                    timestamp=message.timestamp,
                )
            )
        elif isinstance(message, ToolResultMessage):
            downgraded.append(
                ToolResultMessage(
                    tool_call_id=message.tool_call_id,
                    tool_name=message.tool_name,
                    content=_replace_images_with_placeholder(message.content, NON_VISION_TOOL_IMAGE_PLACEHOLDER),
                    is_error=message.is_error,
                    timestamp=message.timestamp,
                    details=message.details,
                    usage=message.usage,
                    added_tool_names=message.added_tool_names,
                )
            )
        else:
            downgraded.append(message)
    return downgraded


def _transform_assistant_content(
    content: list[AssistantContentBlock], is_same_model: bool
) -> list[AssistantContentBlock]:
    transformed: list[AssistantContentBlock] = []

    for block in content:
        if isinstance(block, ThinkingContent):
            # Redacted thinking is opaque encrypted content; only the model that
            # produced it can verify the ciphertext.
            if block.redacted:
                if is_same_model:
                    transformed.append(block)
                continue
            # For the same model keep signed blocks even when the visible text is
            # empty: OpenAI's encrypted reasoning carries its payload in the
            # signature and needs the block replayed verbatim.
            if is_same_model and block.thinking_signature:
                transformed.append(block)
                continue
            if not block.thinking or not block.thinking.strip():
                continue
            if is_same_model:
                transformed.append(block)
                continue
            # Cross-model: the reasoning is not portable, but its text is still
            # useful context, so downgrade rather than discard.
            transformed.append(TextContent(text=block.thinking))
            continue

        if isinstance(block, TextContent):
            if is_same_model:
                transformed.append(block)
            else:
                # Drop the signature: it belongs to the issuing provider.
                transformed.append(TextContent(text=block.text))
            continue

        if isinstance(block, ToolCall):
            thought_signature = block.thought_signature if is_same_model else None
            transformed.append(
                ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=block.arguments,
                    thought_signature=thought_signature,
                    namespace=block.namespace,
                )
            )
            continue

        transformed.append(block)

    return transformed


def transform_messages(messages: list[Message], model: Model) -> list[Message]:
    """Normalise ``messages`` for a request targeting ``model``.

    A new list (and new message objects wherever anything changed) is returned;
    the caller's messages are never mutated, because the session keeps the
    original transcript.
    """
    # Tolerate hand-built histories whose content is None (the type says it
    # cannot be, but old session files and custom tools do it).
    normalised: list[Message] = []
    for message in messages:
        if getattr(message, "content", None) is None:
            if isinstance(message, UserMessage):
                normalised.append(UserMessage(content="", timestamp=message.timestamp))
            elif isinstance(message, AssistantMessage):
                normalised.append(
                    AssistantMessage(
                        content=[],
                        api=message.api,
                        provider=message.provider,
                        model=message.model,
                        usage=message.usage,
                        stop_reason=message.stop_reason,
                        timestamp=message.timestamp,
                    )
                )
            elif isinstance(message, ToolResultMessage):
                normalised.append(
                    ToolResultMessage(
                        tool_call_id=message.tool_call_id,
                        tool_name=message.tool_name,
                        content=[],
                        is_error=message.is_error,
                        timestamp=message.timestamp,
                        details=message.details,
                        usage=message.usage,
                        added_tool_names=message.added_tool_names,
                    )
                )
            else:
                normalised.append(message)
        else:
            normalised.append(message)

    image_aware = _downgrade_unsupported_images(normalised, model)

    # First pass: content portability.
    transformed: list[Message] = []
    for message in image_aware:
        if isinstance(message, AssistantMessage):
            is_same_model = (
                message.provider == model.provider
                and message.api == model.api
                and message.model == model.id
            )
            transformed.append(
                AssistantMessage(
                    content=_transform_assistant_content(message.content, is_same_model),
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
            )
        else:
            transformed.append(message)

    # Every tool call id present in the conversation.  A tool result for an id
    # outside this set is dangling (its call was never recorded) and would make
    # the provider payload invalid.
    known_tool_call_ids: set[str] = set()
    for message in transformed:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolCall):
                    known_tool_call_ids.add(block.id)

    # Second pass: pair tool calls with results, preserving order.
    result: list[Message] = []
    pending_tool_calls: list[ToolCall] = []
    existing_result_ids: set[str] = set()

    def insert_synthetic_results() -> None:
        nonlocal pending_tool_calls, existing_result_ids
        if pending_tool_calls:
            for tool_call in pending_tool_calls:
                if tool_call.id not in existing_result_ids:
                    result.append(
                        ToolResultMessage(
                            tool_call_id=tool_call.id,
                            tool_name=tool_call.name,
                            content=[TextContent(text=NO_RESULT_PLACEHOLDER)],
                            is_error=True,
                            timestamp=_now_ms(),
                        )
                    )
            pending_tool_calls = []
            existing_result_ids = set()

    for message in transformed:
        if isinstance(message, AssistantMessage):
            # A new assistant turn closes the previous tool exchange.
            insert_synthetic_results()
            if message.stop_reason in ("error", "aborted"):
                continue
            tool_calls = [b for b in message.content if isinstance(b, ToolCall)]
            if tool_calls:
                pending_tool_calls = tool_calls
                existing_result_ids = set()
            result.append(message)
        elif isinstance(message, ToolResultMessage):
            if message.tool_call_id in known_tool_call_ids:
                existing_result_ids.add(message.tool_call_id)
                result.append(message)
        elif isinstance(message, UserMessage):
            # A genuine user turn interrupts the tool flow: close it out first so
            # the assistant's calls are not left unanswered.
            insert_synthetic_results()
            result.append(message)
        else:
            result.append(message)

    insert_synthetic_results()
    return result


# --------------------------------------------------------------------------------------
# Shared adapter plumbing: partial-JSON salvage
# --------------------------------------------------------------------------------------
#
# Both streaming adapters accumulate tool-call argument fragments and turn them
# into a ``ToolCall.arguments`` object on every delta.  The fragments are
# routinely incomplete (the model is still writing) and occasionally malformed
# (raw control characters, truncated escapes).  Upstream delegates this to
# ``partial-json`` + its own ``repairJson``; this is the dependency-free
# equivalent, shared here so the adapters never import each other.

_VALID_JSON_ESCAPES = frozenset('"\\/bfnrtu')
_CONTROL_ESCAPES = {"\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape_control_chars(text: str) -> str:
    """Escape raw control characters inside strings and invalid backslash escapes."""
    repaired: list[str] = []
    in_string = False
    index = 0
    length = len(text)

    while index < length:
        char = text[index]
        if not in_string:
            repaired.append(char)
            if char == '"':
                in_string = True
            index += 1
            continue

        if char == '"':
            repaired.append(char)
            in_string = False
            index += 1
            continue

        if char == "\\":
            following = text[index + 1] if index + 1 < length else None
            if following is None:
                repaired.append("\\\\")
                index += 1
                continue
            if following == "u":
                digits = text[index + 2 : index + 6]
                if len(digits) == 4 and all(c in "0123456789abcdefABCDEF" for c in digits):
                    repaired.append(f"\\u{digits}")
                    index += 6
                    continue
            if following in _VALID_JSON_ESCAPES:
                repaired.append(f"\\{following}")
                index += 2
                continue
            repaired.append("\\\\")
            index += 1
            continue

        if ord(char) < 0x20:
            repaired.append(_CONTROL_ESCAPES.get(char) or f"\\u{ord(char):04x}")
        else:
            repaired.append(char)
        index += 1

    return "".join(repaired)


def _close_json(text: str) -> str:
    """Close a truncated JSON prefix: finish the string, then the containers."""
    stack: list[str] = []
    in_string = False
    escaped = False

    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            stack.append("}")
        elif char == "[":
            stack.append("]")
        elif char in "}]" and stack and stack[-1] == char:
            stack.pop()

    repaired = text[:-1] if escaped else text
    if in_string:
        repaired += '"'
    repaired = repaired.rstrip()
    while repaired and repaired[-1] in ",:":
        repaired = repaired[:-1].rstrip()
    return repaired + "".join(reversed(stack))


def _load_json(text: str) -> Any | None:
    """``json.loads`` with a control-character repair pass; ``None`` on failure."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    repaired = _escape_control_chars(text)
    if repaired != text:
        try:
            return json.loads(repaired)
        except ValueError:
            pass
    return None


def parse_streaming_json(text: str | None) -> dict[str, Any]:
    """Best-effort ``dict`` from a possibly incomplete tool-argument fragment.

    Complete JSON wins.  Otherwise the longest prefix that can be closed into
    valid JSON is used (so ``'{"path": "a"'`` yields ``{"path": "a"}``), and a
    final failure yields ``{}`` rather than raising: a model that emits
    malformed arguments must not tear down the stream.
    """
    if not text or not text.strip():
        return {}

    value = _load_json(text)
    if value is None:
        for index in range(len(text) - 1, -1, -1):
            if text[index] not in ',:{["':
                continue
            value = _load_json(_close_json(text[:index]))
            if value is not None:
                break

    return value if isinstance(value, dict) else {}


__all__ = [
    "NO_RESULT_PLACEHOLDER",
    "NON_VISION_TOOL_IMAGE_PLACEHOLDER",
    "NON_VISION_USER_IMAGE_PLACEHOLDER",
    "parse_streaming_json",
    "transform_messages",
]

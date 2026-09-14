"""Wire serialization shared by the ``json`` and ``rpc`` stdout protocols.

Port of ``packages/coding-agent/src/modes/json-event.ts``.

The one non-obvious rule: streaming ``message_update`` events are **delta-only**.
The cumulative assistant snapshot and the live ``partial`` reference are dropped
so the stream stays linear in size, but the cumulative ``usage`` is kept —
clients render live token/cost counters from it, and its size is constant.
``toolcall_start`` is the exception to "deltas are enough": because the partial is
gone, the call's ``id`` and ``toolName`` are injected so a client can correlate
the deltas that follow.
"""

from __future__ import annotations

from typing import Any

from pi_ai import content_to_json, usage_to_json

from ..messages import agent_message_to_json


def message_to_json(message: Any) -> Any:
    """Serialise an agent message, tolerating roles from a newer pi."""
    if message is None:
        return None
    try:
        return agent_message_to_json(message)
    except TypeError:
        # Keep the line parseable rather than failing the whole stream.
        return {"role": getattr(message, "role", None)}


def _content_to_json(content: Any) -> Any:
    """Encode a *list* of content blocks, or pass a plain string through."""
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return content
    return [content_to_json(block) for block in content]


def _block_list_to_json(content: Any) -> list[Any]:
    """Encode a list of content blocks.

    Kept separate from :func:`_content_to_json` on purpose: that helper passes a
    *single* unknown object straight through, which silently leaked live
    ``TextContent`` dataclasses into ``tool_execution_end`` — invisible on the stdio
    path (whose serializer falls back to ``str``) and a hard failure anywhere the
    payload is strictly encoded.
    """
    if not isinstance(content, (list, tuple)):
        return []
    return [content_to_json(block) for block in content]


def _tool_result_to_json(result: Any) -> Any:
    if result is None:
        return None
    return {
        "content": _block_list_to_json(getattr(result, "content", None)),
        "details": getattr(result, "details", None),
        "terminate": getattr(result, "terminate", None),
    }


def assistant_message_event_to_json(event: Any) -> dict[str, Any]:
    """Delta-only projection of one assistant stream event."""
    kind = getattr(event, "type", None)
    payload: dict[str, Any] = {"type": kind}
    content_index = getattr(event, "content_index", None)

    if kind == "toolcall_start":
        # The cumulative partial is dropped, so carry the constant-size identity
        # of the call being started instead.
        index = content_index if content_index is not None else 0
        payload["contentIndex"] = index
        partial = getattr(event, "partial", None)
        content = getattr(partial, "content", None) or []
        block = content[index] if 0 <= index < len(content) else None
        if getattr(block, "type", None) != "toolCall":
            raise ValueError(f"toolcall_start content at index {index} is not a tool call")
        payload["id"] = block.id
        payload["toolName"] = block.name
        return payload

    if kind in ("text_start", "thinking_start") and content_index is not None:
        payload["contentIndex"] = content_index
    elif kind in ("text_delta", "thinking_delta", "toolcall_delta"):
        if content_index is not None:
            payload["contentIndex"] = content_index
        payload["delta"] = getattr(event, "delta", "")
    elif kind in ("text_end", "thinking_end"):
        if content_index is not None:
            payload["contentIndex"] = content_index
        payload["content"] = getattr(event, "content", "")
    elif kind == "toolcall_end":
        if content_index is not None:
            payload["contentIndex"] = content_index
        tool_call = getattr(event, "tool_call", None)
        if tool_call is not None:
            payload["toolCall"] = {
                "type": "toolCall",
                "id": tool_call.id,
                "name": tool_call.name,
                "arguments": tool_call.arguments,
            }
    return payload


def to_json_event(event: Any) -> dict[str, Any]:
    """Project one agent session event into its wire form."""
    kind = getattr(event, "type", None)

    if kind == "message_update":
        message = getattr(event, "message", None)
        if getattr(message, "role", None) != "assistant":
            raise ValueError("message_update message is not an assistant message")
        return {
            "type": "message_update",
            # Cumulative usage stays: constant size, and clients need it live.
            "usage": usage_to_json(getattr(message, "usage", None)) if message is not None else None,
            "assistantMessageEvent": assistant_message_event_to_json(
                getattr(event, "assistant_message_event", None)
            ),
        }

    if kind in ("message_start", "message_end"):
        return {"type": kind, "message": message_to_json(getattr(event, "message", None))}

    if kind == "agent_start" or kind == "turn_start":
        return {"type": kind}

    if kind == "agent_end":
        return {
            "type": "agent_end",
            "messages": [
                message_to_json(message) for message in (getattr(event, "messages", None) or [])
            ],
            "willRetry": bool(getattr(event, "will_retry", False)),
        }

    if kind == "turn_end":
        return {
            "type": "turn_end",
            "message": message_to_json(getattr(event, "message", None)),
            "toolResults": [
                message_to_json(result) for result in (getattr(event, "tool_results", None) or [])
            ],
        }

    if kind == "tool_execution_start":
        return {
            "type": kind,
            "toolCallId": getattr(event, "tool_call_id", ""),
            "toolName": getattr(event, "tool_name", ""),
            "args": getattr(event, "args", None),
        }

    if kind == "tool_execution_update":
        return {
            "type": kind,
            "toolCallId": getattr(event, "tool_call_id", ""),
            "toolName": getattr(event, "tool_name", ""),
            "args": getattr(event, "args", None),
            "partialResult": _tool_result_to_json(getattr(event, "partial_result", None)),
        }

    if kind == "tool_execution_end":
        return {
            "type": kind,
            "toolCallId": getattr(event, "tool_call_id", ""),
            "toolName": getattr(event, "tool_name", ""),
            "result": _tool_result_to_json(getattr(event, "result", None)),
            "isError": bool(getattr(event, "is_error", False)),
        }

    # Unknown event type: keep the type so clients can ignore it safely.
    return {"type": kind}


def dump_line(payload: Any) -> str:
    """Serialise one JSONL record (LF-terminated), without escaping non-ASCII."""
    return json.dumps(payload, ensure_ascii=False, default=str) + "\n"


__all__ = [
    "assistant_message_event_to_json",
    "message_to_json",
    "to_json_event",
]

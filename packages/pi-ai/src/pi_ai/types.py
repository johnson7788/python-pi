"""Wire types for pi-ai.

These dataclasses are the **interop-critical** layer: the JSON shapes produced by
``to_json`` / accepted by ``from_json`` must match the TypeScript implementation
exactly, because session files are shared with ``pi`` itself.

Two rules carried over verbatim from the TypeScript source:

* ``None`` means "field absent" and is dropped on serialisation (the JS
  ``undefined`` semantics).  ``False`` and ``0`` are real values and are kept.
* Timestamps on messages are **Unix milliseconds**; timestamps on session
  entries are ISO-8601 strings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Union

# --------------------------------------------------------------------------------------
# Literals
# --------------------------------------------------------------------------------------

StopReason = Literal["pending", "stop", "length", "toolUse", "error", "aborted", "deferred"]

ThinkingLevel = Literal["minimal", "low", "medium", "high", "xhigh", "max"]
ModelThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]

#: Ordered weakest → strongest; the order *is* the strength ordering.
EXTENDED_THINKING_LEVELS: tuple[ModelThinkingLevel, ...] = (
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

#: ``thinking_level_map[level]`` maps an abstract level to a provider-native value.
#: ``None`` means the level is explicitly unsupported; absent means "provider default".
ThinkingLevelMap = dict[str, Union[str, None]]


# --------------------------------------------------------------------------------------
# Content blocks
# --------------------------------------------------------------------------------------


@dataclass
class TextContent:
    text: str
    text_signature: str | None = None
    type: Literal["text"] = field(default="text", init=False)


@dataclass
class ImageContent:
    data: str  # base64
    mime_type: str
    type: Literal["image"] = field(default="image", init=False)


@dataclass
class ThinkingContent:
    thinking: str
    thinking_signature: str | None = None
    redacted: bool | None = None
    type: Literal["thinking"] = field(default="thinking", init=False)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    thought_signature: str | None = None
    namespace: str | None = None
    type: Literal["toolCall"] = field(default="toolCall", init=False)


ContentBlock = Union[TextContent, ImageContent, ThinkingContent, ToolCall]
AssistantContentBlock = Union[TextContent, ThinkingContent, ToolCall]


# --------------------------------------------------------------------------------------
# Usage / cost
# --------------------------------------------------------------------------------------


@dataclass
class Cost:
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    total: float = 0.0


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cache_write_1h: int | None = None
    reasoning: int | None = None
    total_tokens: int = 0
    cost: Cost = field(default_factory=Cost)


# --------------------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------------------


@dataclass
class UserMessage:
    content: str | list[Union[TextContent, ImageContent]]
    timestamp: int
    role: Literal["user"] = field(default="user", init=False)


@dataclass
class AssistantMessage:
    content: list[AssistantContentBlock]
    api: str
    provider: str
    model: str
    usage: Usage
    stop_reason: StopReason
    timestamp: int
    response_model: str | None = None
    response_id: str | None = None
    provider_thinking_level: str | None = None
    diagnostics: list[dict[str, Any]] | None = None
    deferred: dict[str, Any] | None = None
    error_message: str | None = None
    raw_stop_reason: str | None = None
    end_turn: bool | None = None
    role: Literal["assistant"] = field(default="assistant", init=False)


@dataclass
class ToolResultMessage:
    tool_call_id: str
    tool_name: str
    content: list[Union[TextContent, ImageContent]]
    is_error: bool
    timestamp: int
    details: Any = None
    usage: Usage | None = None
    added_tool_names: list[str] | None = None
    role: Literal["toolResult"] = field(default="toolResult", init=False)


Message = Union[UserMessage, AssistantMessage, ToolResultMessage]


# --------------------------------------------------------------------------------------
# Model descriptor
# --------------------------------------------------------------------------------------


@dataclass
class ModelCostRates:
    """USD per million tokens."""

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0


@dataclass
class ModelCostTier(ModelCostRates):
    input_tokens_above: int = 0


@dataclass
class ModelCost(ModelCostRates):
    tiers: list[ModelCostTier] = field(default_factory=list)


@dataclass
class Model:
    id: str
    name: str
    api: str
    provider: str
    base_url: str
    reasoning: bool
    input: list[str]
    cost: ModelCost
    context_window: int
    max_tokens: int
    thinking_level_map: ThinkingLevelMap | None = None
    sampling_params: dict[str, Any] | None = None
    headers: dict[str, str] | None = None
    compat: dict[str, Any] | None = None


# --------------------------------------------------------------------------------------
# JSON helpers
# --------------------------------------------------------------------------------------


def drop_none(value: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` values, mirroring ``JSON.stringify`` on ``undefined`` fields."""
    return {k: v for k, v in value.items() if v is not None}


def content_to_json(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, TextContent):
        return drop_none(
            {"type": "text", "text": block.text, "textSignature": block.text_signature}
        )
    if isinstance(block, ImageContent):
        return {"type": "image", "data": block.data, "mimeType": block.mime_type}
    if isinstance(block, ThinkingContent):
        return drop_none(
            {
                "type": "thinking",
                "thinking": block.thinking,
                "thinkingSignature": block.thinking_signature,
                "redacted": block.redacted,
            }
        )
    if isinstance(block, ToolCall):
        return drop_none(
            {
                "type": "toolCall",
                "id": block.id,
                "name": block.name,
                "arguments": block.arguments,
                "thoughtSignature": block.thought_signature,
                "namespace": block.namespace,
            }
        )
    raise TypeError(f"Unknown content block: {block!r}")


def content_from_json(data: dict[str, Any]) -> ContentBlock:
    kind = data.get("type")
    if kind == "text":
        return TextContent(text=data["text"], text_signature=data.get("textSignature"))
    if kind == "image":
        return ImageContent(data=data["data"], mime_type=data.get("mimeType", data.get("mediaType", "")))
    if kind == "thinking":
        return ThinkingContent(
            thinking=data["thinking"],
            thinking_signature=data.get("thinkingSignature"),
            redacted=data.get("redacted"),
        )
    if kind == "toolCall":
        return ToolCall(
            id=data["id"],
            name=data["name"],
            arguments=data.get("arguments") or {},
            thought_signature=data.get("thoughtSignature"),
            namespace=data.get("namespace"),
        )
    raise TypeError(f"Unknown content block type: {kind!r}")


def _user_content_to_json(content: str | list[Any]) -> Any:
    if isinstance(content, str):
        return content
    return [content_to_json(b) for b in content]


def _user_content_from_json(content: Any) -> str | list[Any]:
    if isinstance(content, str):
        return content
    return [content_from_json(b) for b in content]


def usage_to_json(usage: Usage) -> dict[str, Any]:
    return drop_none(
        {
            "input": usage.input,
            "output": usage.output,
            "cacheRead": usage.cache_read,
            "cacheWrite": usage.cache_write,
            "cacheWrite1h": usage.cache_write_1h,
            "reasoning": usage.reasoning,
            "totalTokens": usage.total_tokens,
            "cost": {
                "input": usage.cost.input,
                "output": usage.cost.output,
                "cacheRead": usage.cost.cache_read,
                "cacheWrite": usage.cost.cache_write,
                "total": usage.cost.total,
            },
        }
    )


def usage_from_json(data: dict[str, Any] | None) -> Usage:
    data = data or {}
    cost = data.get("cost") or {}
    return Usage(
        input=data.get("input", 0),
        output=data.get("output", 0),
        cache_read=data.get("cacheRead", 0),
        cache_write=data.get("cacheWrite", 0),
        cache_write_1h=data.get("cacheWrite1h"),
        reasoning=data.get("reasoning"),
        total_tokens=data.get("totalTokens", 0),
        cost=Cost(
            input=cost.get("input", 0.0),
            output=cost.get("output", 0.0),
            cache_read=cost.get("cacheRead", 0.0),
            cache_write=cost.get("cacheWrite", 0.0),
            total=cost.get("total", 0.0),
        ),
    )


def message_to_json(message: Message) -> dict[str, Any]:
    """Serialise a base message to its session-file JSON form."""
    if isinstance(message, UserMessage):
        return {
            "role": "user",
            "content": _user_content_to_json(message.content),
            "timestamp": message.timestamp,
        }
    if isinstance(message, AssistantMessage):
        return drop_none(
            {
                "role": "assistant",
                "content": [content_to_json(b) for b in message.content],
                "api": message.api,
                "provider": message.provider,
                "model": message.model,
                "responseModel": message.response_model,
                "responseId": message.response_id,
                "providerThinkingLevel": message.provider_thinking_level,
                "diagnostics": message.diagnostics,
                "usage": usage_to_json(message.usage),
                "stopReason": message.stop_reason,
                "deferred": message.deferred,
                "errorMessage": message.error_message,
                "rawStopReason": message.raw_stop_reason,
                "endTurn": message.end_turn,
                "timestamp": message.timestamp,
            }
        )
    if isinstance(message, ToolResultMessage):
        return drop_none(
            {
                "role": "toolResult",
                "toolCallId": message.tool_call_id,
                "toolName": message.tool_name,
                "content": [content_to_json(b) for b in message.content],
                "details": message.details,
                "usage": usage_to_json(message.usage) if message.usage is not None else None,
                "addedToolNames": message.added_tool_names,
                "isError": message.is_error,
                "timestamp": message.timestamp,
            }
        )
    raise TypeError(f"Unknown message: {message!r}")


def message_from_json(data: dict[str, Any]) -> Message:
    """Parse one of the three base message roles from session JSON."""
    role = data.get("role")
    if role == "user":
        return UserMessage(
            content=_user_content_from_json(data.get("content", "")),
            timestamp=data.get("timestamp", 0),
        )
    if role == "assistant":
        return AssistantMessage(
            content=[content_from_json(b) for b in data.get("content", [])],
            api=data.get("api", ""),
            provider=data.get("provider", ""),
            model=data.get("model", ""),
            response_model=data.get("responseModel"),
            response_id=data.get("responseId"),
            provider_thinking_level=data.get("providerThinkingLevel"),
            diagnostics=data.get("diagnostics"),
            usage=usage_from_json(data.get("usage")),
            stop_reason=data.get("stopReason", "stop"),
            deferred=data.get("deferred"),
            error_message=data.get("errorMessage"),
            raw_stop_reason=data.get("rawStopReason"),
            end_turn=data.get("endTurn"),
            timestamp=data.get("timestamp", 0),
        )
    if role == "toolResult":
        return ToolResultMessage(
            tool_call_id=data.get("toolCallId", ""),
            tool_name=data.get("toolName", ""),
            content=[content_from_json(b) for b in data.get("content", [])],
            details=data.get("details"),
            usage=usage_from_json(data["usage"]) if data.get("usage") else None,
            added_tool_names=data.get("addedToolNames"),
            is_error=bool(data.get("isError", False)),
            timestamp=data.get("timestamp", 0),
        )
    raise TypeError(f"Unknown message role: {role!r}")


__all__ = [
    "EXTENDED_THINKING_LEVELS",
    "AssistantContentBlock",
    "AssistantMessage",
    "ContentBlock",
    "Cost",
    "ImageContent",
    "Message",
    "Model",
    "ModelCost",
    "ModelCostRates",
    "ModelCostTier",
    "ModelThinkingLevel",
    "StopReason",
    "TextContent",
    "ThinkingContent",
    "ThinkingLevel",
    "ThinkingLevelMap",
    "ToolCall",
    "ToolResultMessage",
    "Usage",
    "UserMessage",
    "content_from_json",
    "content_to_json",
    "drop_none",
    "message_from_json",
    "message_to_json",
    "usage_from_json",
    "usage_to_json",
]

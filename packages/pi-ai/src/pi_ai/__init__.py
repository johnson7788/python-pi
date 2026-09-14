"""pi-ai — a unified multi-provider LLM API.

Python port of ``@earendil-works/pi-ai``.  The package is deliberately
dependency-light: dataclasses for the wire types, ``httpx`` for transport.

Typical use::

    from pi_ai import Context, Model, StreamOptions, stream_simple, UserMessage

    message = await stream_simple(model, context, options).result()

Modules whose dependencies may not be installed yet (model catalog, credentials)
are resolved lazily through :pep:`562`.
"""

from __future__ import annotations

from .abort import AbortController, AbortSignal, any_
from .cost import calculate_cost
from .errors import (
    AbortedError,
    AuthenticationError,
    ModelNotFoundError,
    PiAiError,
    ProviderError,
    ToolValidationError,
)
from .events import (
    UPDATE_EVENT_TYPES,
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
from .stream import (
    DEFAULT_THINKING_BUDGETS,
    MIN_ANSWER_TOKENS,
    Context,
    ProviderStreams,
    StreamOptions,
    clamp_thinking_level,
    complete,
    complete_simple,
    create_error_stream,
    create_setup_error_message,
    ensure_builtin_apis,
    get_api,
    get_supported_thinking_levels,
    list_apis,
    register_api,
    stream,
    stream_simple,
)
from .tools import Tool, validate_tool_arguments
from .types import (
    EXTENDED_THINKING_LEVELS,
    AssistantContentBlock,
    AssistantMessage,
    ContentBlock,
    Cost,
    ImageContent,
    Message,
    Model,
    ModelCost,
    ModelCostRates,
    ModelCostTier,
    ModelThinkingLevel,
    StopReason,
    TextContent,
    ThinkingContent,
    ThinkingLevel,
    ThinkingLevelMap,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
    content_from_json,
    content_to_json,
    message_from_json,
    message_to_json,
    usage_from_json,
    usage_to_json,
)

#: Names that live in optional modules, resolved on first attribute access.
_LAZY_EXPORTS = {
    "ApiKeyCredential": "pi_ai.auth",
    "AuthStorage": "pi_ai.auth",
    "Credential": "pi_ai.auth",
    "CredentialStore": "pi_ai.auth",
    "FileCredentialStore": "pi_ai.auth",
    "InMemoryCredentialStore": "pi_ai.auth",
    "OAuthCredential": "pi_ai.auth",
    "ModelRegistry": "pi_ai.models",
    "get_model": "pi_ai.models",
    "load_models_json": "pi_ai.models",
}

__version__ = "0.1.0"


def __getattr__(name: str):
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_name), name)


__all__ = [
    "DEFAULT_THINKING_BUDGETS",
    "EXTENDED_THINKING_LEVELS",
    "MIN_ANSWER_TOKENS",
    "UPDATE_EVENT_TYPES",
    "AbortController",
    "AbortSignal",
    "AbortedError",
    "AssistantContentBlock",
    "AssistantMessage",
    "AssistantMessageEvent",
    "AuthenticationError",
    "ContentBlock",
    "Context",
    "Cost",
    "DoneEvent",
    "ErrorEvent",
    "EventStream",
    "ImageContent",
    "Message",
    "Model",
    "ModelCost",
    "ModelCostRates",
    "ModelCostTier",
    "ModelNotFoundError",
    "ModelThinkingLevel",
    "PiAiError",
    "ProviderError",
    "ProviderStreams",
    "StartEvent",
    "StopReason",
    "StreamOptions",
    "TextContent",
    "TextDeltaEvent",
    "TextEndEvent",
    "TextStartEvent",
    "ThinkingContent",
    "ThinkingDeltaEvent",
    "ThinkingEndEvent",
    "ThinkingLevel",
    "ThinkingLevelMap",
    "ThinkingStartEvent",
    "Tool",
    "ToolCall",
    "ToolCallDeltaEvent",
    "ToolCallEndEvent",
    "ToolCallStartEvent",
    "ToolResultMessage",
    "ToolValidationError",
    "Usage",
    "UserMessage",
    "any_",
    "calculate_cost",
    "clamp_thinking_level",
    "complete",
    "complete_simple",
    "content_from_json",
    "content_to_json",
    "create_assistant_message_event_stream",
    "create_error_stream",
    "create_setup_error_message",
    "ensure_builtin_apis",
    "get_api",
    "get_supported_thinking_levels",
    "list_apis",
    "message_from_json",
    "message_to_json",
    "register_api",
    "stream",
    "stream_simple",
    "usage_from_json",
    "usage_to_json",
    "validate_tool_arguments",
]

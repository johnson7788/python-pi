"""The provider stream contract and the API registry.

A provider adapter is a :class:`ProviderStreams` — a pair of functions keyed by
the ``model.api`` string.  Two **opposite** error contracts must be honoured, and
they are the easiest thing to get wrong in a port:

* :func:`stream_simple` may raise synchronously (e.g. missing credentials).
* :func:`stream` must never raise for setup problems; failures are encoded *into*
  the returned stream as an ``error`` event.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .abort import AbortSignal
from .events import AssistantMessageEvent, EventStream, ErrorEvent, create_assistant_message_event_stream
from .tools import Tool
from .types import (
    EXTENDED_THINKING_LEVELS,
    AssistantMessage,
    Cost,
    Message,
    Model,
    StopReason,
    Usage,
)


# --------------------------------------------------------------------------------------
# Request context and options
# --------------------------------------------------------------------------------------


@dataclass
class Context:
    """Everything needed for one LLM request.  There is no ``system`` role."""

    messages: list[Message]
    system_prompt: str | None = None
    tools: list[Tool] = field(default_factory=list)


@dataclass
class StreamOptions:
    """Provider-agnostic request options.

    ``provider_options`` carries per-API extras (``compat`` overrides, cache
    control, ...) so the common fields stay stable.
    """

    api_key: str | None = None
    signal: AbortSignal | None = None
    headers: dict[str, str] | None = None
    session_id: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    reasoning: str | None = None  # a ModelThinkingLevel
    thinking_budgets: dict[str, int] | None = None
    provider_options: dict[str, Any] = field(default_factory=dict)
    request_timeout: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


#: Default thinking budgets, from ``api/simple-options.ts``.
DEFAULT_THINKING_BUDGETS: dict[str, int] = {
    "minimal": 1024,
    "low": 2048,
    "medium": 8192,
    "high": 16384,
}
MIN_ANSWER_TOKENS = 1024


class ProviderStreams(Protocol):
    """The unit a provider exposes per API string."""

    def stream(
        self, model: Model, context: Context, options: StreamOptions
    ) -> EventStream[AssistantMessageEvent, AssistantMessage]: ...

    def stream_simple(
        self, model: Model, context: Context, options: StreamOptions
    ) -> EventStream[AssistantMessageEvent, AssistantMessage]: ...


# --------------------------------------------------------------------------------------
# Thinking levels
# --------------------------------------------------------------------------------------


def get_supported_thinking_levels(model: Model) -> list[str]:
    """Which abstract thinking levels this model actually accepts.

    ``thinking_level_map`` distinguishes "absent" (provider default) from an
    explicit ``None`` (unsupported), so membership is tested with ``in`` rather
    than a truthiness check.
    """
    if not model.reasoning:
        return ["off"]
    level_map = model.thinking_level_map or {}
    supported: list[str] = []
    for level in EXTENDED_THINKING_LEVELS:
        if level in level_map and level_map[level] is None:
            continue  # explicitly declared unsupported
        if level in ("xhigh", "max") and level not in level_map:
            continue  # these must be explicitly mapped to exist
        supported.append(level)
    return supported


def clamp_thinking_level(model: Model, level: str | None) -> str:
    """Snap a requested level onto the model's supported set.

    Search order is **upward first** (stronger), then downward, then the first
    available level.  Reproducing this direction matters: a request for
    ``"medium"`` on a model that only supports ``high``/``max`` must land on
    ``"high"``, not ``"off"``.
    """
    available = get_supported_thinking_levels(model)
    if not available:
        return "off"
    requested = level or "off"
    if requested in available:
        return requested
    if requested not in EXTENDED_THINKING_LEVELS:
        return available[0]
    start = EXTENDED_THINKING_LEVELS.index(requested)
    for index in range(start, len(EXTENDED_THINKING_LEVELS)):
        if EXTENDED_THINKING_LEVELS[index] in available:
            return EXTENDED_THINKING_LEVELS[index]
    for index in range(start - 1, -1, -1):
        if EXTENDED_THINKING_LEVELS[index] in available:
            return EXTENDED_THINKING_LEVELS[index]
    return available[0]


# --------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------

_API_REGISTRY: dict[str, ProviderStreams] = {}
_BUILTINS_LOADED = False


def register_api(name: str, implementation: ProviderStreams) -> None:
    _API_REGISTRY[name] = implementation


def get_api(name: str) -> ProviderStreams | None:
    return _API_REGISTRY.get(name)


def list_apis() -> list[str]:
    ensure_builtin_apis()
    return sorted(_API_REGISTRY)


def ensure_builtin_apis() -> None:
    """Load the bundled provider adapters on first use.

    Deferred so that ``import pi_ai`` stays cheap and never fails while a
    provider module is missing.
    """
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    from .providers import register_builtin_apis

    register_builtin_apis()


# --------------------------------------------------------------------------------------
# Setup error streams
# --------------------------------------------------------------------------------------


def create_setup_error_message(model: Model, error: BaseException) -> AssistantMessage:
    return AssistantMessage(
        content=[],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=Usage(cost=Cost()),
        stop_reason="error",
        error_message=str(error),
        timestamp=int(time.time() * 1000),
    )


def create_error_stream(model: Model, error: BaseException) -> EventStream[AssistantMessageEvent, AssistantMessage]:
    """A stream that yields exactly one ``error`` event (no ``start``)."""
    stream = create_assistant_message_event_stream()
    message = create_setup_error_message(model, error)
    stream.push(ErrorEvent(reason="error", error=message))
    stream.end(message)
    return stream


def _missing_api_stream(model: Model) -> EventStream[AssistantMessageEvent, AssistantMessage]:
    return create_error_stream(
        model,
        RuntimeError(f'Provider {model.provider!r} has no API implementation for "{model.api}"'),
    )


# --------------------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------------------


def _resolve_options(options: StreamOptions | None) -> StreamOptions:
    return options if options is not None else StreamOptions()


def stream(
    model: Model,
    context: Context,
    options: StreamOptions | None = None,
    *,
    get_api_key: Callable[[str], Any] | None = None,
) -> EventStream[AssistantMessageEvent, AssistantMessage]:
    """Start a raw stream.  Setup failures become ``error`` events, never raises."""
    resolved = _resolve_options(options)
    implementation = get_api(model.api)
    if implementation is None:
        ensure_builtin_apis()
        implementation = get_api(model.api)
    if implementation is None:
        return _missing_api_stream(model)
    try:
        return implementation.stream(model, context, resolved)
    except Exception as error:  # noqa: BLE001 - setup failures are encoded in-band
        return create_error_stream(model, error)


def stream_simple(
    model: Model,
    context: Context,
    options: StreamOptions | None = None,
) -> EventStream[AssistantMessageEvent, AssistantMessage]:
    """Start a stream with thinking levels clamped.

    Unlike :func:`stream`, this **may raise synchronously** (missing auth), which
    is what interactive callers rely on for first-time setup detection.
    """
    resolved = _resolve_options(options)
    implementation = get_api(model.api)
    if implementation is None:
        ensure_builtin_apis()
        implementation = get_api(model.api)
    if implementation is None:
        return _missing_api_stream(model)
    resolved.reasoning = clamp_thinking_level(model, resolved.reasoning)
    return implementation.stream_simple(model, context, resolved)


async def complete(model: Model, context: Context, options: StreamOptions | None = None) -> AssistantMessage:
    return await stream(model, context, options).result()


async def complete_simple(
    model: Model, context: Context, options: StreamOptions | None = None
) -> AssistantMessage:
    return await stream_simple(model, context, options).result()


__all__ = [
    "DEFAULT_THINKING_BUDGETS",
    "MIN_ANSWER_TOKENS",
    "Context",
    "ProviderStreams",
    "StreamOptions",
    "clamp_thinking_level",
    "complete",
    "complete_simple",
    "create_error_stream",
    "create_setup_error_message",
    "get_api",
    "get_supported_thinking_levels",
    "list_apis",
    "register_api",
    "stream",
    "stream_simple",
]

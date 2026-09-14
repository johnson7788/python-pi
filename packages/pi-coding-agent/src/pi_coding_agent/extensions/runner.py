"""Runs loaded extensions and merges their results.

Port of ``packages/coding-agent/src/core/extensions/runner.ts``.

Three rules carry the whole design:

1. **Handlers never break the loop.**  A raising handler is captured as an
   :class:`~.types.ExtensionError` and the next handler still runs.  pi does the
   same; it is what makes a bad third-party extension survivable.
2. **Merge semantics differ per event**, and the differences are load-bearing:
   ``input`` chains transforms and stops on ``handled``; ``tool_call`` stops on the
   first ``block``; ``tool_result`` and ``message_end`` *chain* (each handler sees
   the previous one's output); ``before_agent_start`` chains the system prompt.
3. **Extension order is load order**, and results depend on it.  Discovery order is
   preserved rather than sorted.
"""

from __future__ import annotations

import traceback
from typing import Any, Awaitable, Callable, Sequence

from pi_agent import AgentTool

from .api import ExtensionAPI, ExtensionContext, ExtensionHost
from .types import (
    AgentEndEvent,
    AgentStartEvent,
    BeforeAgentStartEvent,
    BeforeAgentStartResult,
    ExtensionError,
    InputEvent,
    InputEventResult,
    MessageEndEvent,
    MessageEndResult,
    ProviderRegistration,
    RegisteredCommand,
    RegisteredFlag,
    SessionBeforeEventResult,
    SessionBeforeForkEvent,
    SessionBeforeSwitchEvent,
    ToolCallEvent,
    ToolCallEventResult,
    ToolResultEvent,
    ToolResultEventResult,
)

#: Events whose result can veto a pending action; the first `cancel` wins.
_SESSION_BEFORE_EVENTS = ("session_before_switch", "session_before_fork")


class ExtensionRunner:
    """Holds every loaded extension and dispatches events to them."""

    def __init__(
        self,
        extensions: Sequence[ExtensionAPI] = (),
        *,
        host: ExtensionHost | None = None,
    ) -> None:
        self.extensions: list[ExtensionAPI] = list(extensions)
        self._host = host if host is not None else ExtensionHost()
        self._errors: list[ExtensionError] = []
        self._error_listeners: list[Callable[[ExtensionError], Any]] = []
        self._context: ExtensionContext | None = None
        self._bind()

    # -- wiring ------------------------------------------------------------------------

    def _bind(self) -> None:
        for extension in self.extensions:
            extension._host = self._host

    def bind_host(self, host: ExtensionHost) -> None:
        """Attach the live session/runtime.  Re-called after session replacement."""
        self._host = host
        self._context = None
        self._bind()

    @property
    def host(self) -> ExtensionHost:
        return self._host

    def on_error(self, listener: Callable[[ExtensionError], Any]) -> Callable[[], None]:
        self._error_listeners.append(listener)

        def _dispose() -> None:
            try:
                self._error_listeners.remove(listener)
            except ValueError:
                pass

        return _dispose

    def drain_errors(self) -> list[ExtensionError]:
        errors, self._errors = self._errors, []
        return errors

    @property
    def errors(self) -> list[ExtensionError]:
        return list(self._errors)

    def create_context(self) -> ExtensionContext:
        if self._context is None:
            self._context = ExtensionContext(self._host)
        return self._context

    # -- registration accessors --------------------------------------------------------

    def get_tools(self) -> list[AgentTool]:
        tools: list[AgentTool] = []
        for extension in self.extensions:
            tools.extend(extension.tools.values())
        return tools

    def get_commands(self) -> dict[str, RegisteredCommand]:
        commands: dict[str, RegisteredCommand] = {}
        for extension in self.extensions:
            for name, command in extension.commands.items():
                # Duplicate names are suffixed rather than silently clobbered, so a
                # collision stays visible and both commands remain reachable.
                if name in commands:
                    suffix = 2
                    while f"{name}:{suffix}" in commands:
                        suffix += 1
                    commands[f"{name}:{suffix}"] = command
                else:
                    commands[name] = command
        return commands

    def get_flags(self) -> dict[str, RegisteredFlag]:
        flags: dict[str, RegisteredFlag] = {}
        for extension in self.extensions:
            flags.update(extension.flags)
        return flags

    def get_providers(self) -> list[ProviderRegistration]:
        providers: list[ProviderRegistration] = []
        for extension in self.extensions:
            providers.extend(extension.providers)
        return providers

    def has_handler(self, event: str) -> bool:
        return any(extension.handlers.get(event) for extension in self.extensions)

    # -- dispatch ----------------------------------------------------------------------

    async def emit(self, event: Any) -> SessionBeforeEventResult | None:
        """Emit a notification event, or a cancellable ``session_before_*`` event."""
        ctx = self.create_context()
        event_type = getattr(event, "type", "")
        cancellable = event_type in _SESSION_BEFORE_EVENTS or isinstance(
            event, (SessionBeforeSwitchEvent, SessionBeforeForkEvent)
        )
        result: SessionBeforeEventResult | None = None

        for extension in self.extensions:
            for handler in extension.handlers.get(event_type, []):
                try:
                    handler_result = handler(event, ctx)
                    if _is_awaitable(handler_result):
                        handler_result = await handler_result
                except Exception as error:  # noqa: BLE001 - isolated on purpose
                    self._record_error(extension, event_type, error)
                    continue

                if cancellable and handler_result is not None:
                    result = handler_result
                    if getattr(result, "cancel", False):
                        return result
        return result

    async def emit_input(
        self,
        text: str,
        images: Any = None,
        source: str = "user",
        streaming_behavior: str | None = None,
    ) -> InputEventResult:
        ctx = self.create_context()
        current_text = text
        current_images = images
        changed = False

        for extension in self.extensions:
            for handler in extension.handlers.get("input", []):
                event = InputEvent(
                    text=current_text,
                    images=current_images,
                    source=source,
                    streaming_behavior=streaming_behavior,
                )
                try:
                    result = handler(event, ctx)
                    if _is_awaitable(result):
                        result = await result
                except Exception as error:  # noqa: BLE001
                    self._record_error(extension, "input", error)
                    continue

                if result is None:
                    continue
                if getattr(result, "action", None) == "handled":
                    return result
                if getattr(result, "action", None) == "transform":
                    current_text = result.text if result.text is not None else current_text
                    if result.images is not None:
                        current_images = result.images
                    changed = True

        if changed:
            return InputEventResult(action="transform", text=current_text, images=current_images)
        return InputEventResult(action="continue")

    async def emit_before_agent_start(
        self, prompt: str, images: Any, system_prompt: str, system_prompt_options: Any
    ) -> BeforeAgentStartResult | None:
        """Chain the system prompt; accumulate injected messages."""
        ctx = self.create_context()
        current_system_prompt = system_prompt
        messages: list[Any] = []
        modified = False

        for extension in self.extensions:
            for handler in extension.handlers.get("before_agent_start", []):
                event = BeforeAgentStartEvent(
                    prompt=prompt,
                    images=images,
                    system_prompt=current_system_prompt,
                    system_prompt_options=system_prompt_options,
                )
                try:
                    result = handler(event, ctx)
                    if _is_awaitable(result):
                        result = await result
                except Exception as error:  # noqa: BLE001
                    self._record_error(extension, "before_agent_start", error)
                    continue

                if result is None:
                    continue
                if getattr(result, "message", None) is not None:
                    messages.append(result.message)
                injected = getattr(result, "messages", None)
                if injected:
                    messages.extend(injected)
                if getattr(result, "system_prompt", None) is not None:
                    current_system_prompt = result.system_prompt
                    modified = True

        if not modified and not messages:
            return None
        return BeforeAgentStartResult(
            messages=messages,
            system_prompt=current_system_prompt if modified else None,
        )

    async def emit_message_end(self, message: Any) -> Any | None:
        """Chain message replacements; a role change is rejected."""
        ctx = self.create_context()
        current = message
        modified = False

        for extension in self.extensions:
            for handler in extension.handlers.get("message_end", []):
                event = MessageEndEvent(message=current)
                try:
                    result = handler(event, ctx)
                    if _is_awaitable(result):
                        result = await result
                except Exception as error:  # noqa: BLE001
                    self._record_error(extension, "message_end", error)
                    continue

                if result is None or getattr(result, "message", None) is None:
                    continue
                replacement = result.message
                if getattr(replacement, "role", None) != getattr(current, "role", None):
                    self._record_error(
                        extension,
                        "message_end",
                        ValueError("message_end handlers must return a message with the same role"),
                    )
                    continue
                current = replacement
                modified = True

        return current if modified else None

    async def emit_tool_call(self, event: ToolCallEvent) -> ToolCallEventResult | None:
        """First ``block`` wins; args are mutated in place by handlers."""
        ctx = self.create_context()
        result: ToolCallEventResult | None = None

        for extension in self.extensions:
            for handler in extension.handlers.get("tool_call", []):
                try:
                    handler_result = handler(event, ctx)
                    if _is_awaitable(handler_result):
                        handler_result = await handler_result
                except Exception as error:  # noqa: BLE001 - a throwing tool_call handler blocks
                    self._record_error(extension, "tool_call", error)
                    return ToolCallEventResult(block=True, reason=str(error))

                if handler_result is not None:
                    result = handler_result
                    if getattr(result, "block", False):
                        return result
        return result

    async def emit_tool_result(self, event: ToolResultEvent) -> ToolResultEventResult | None:
        """Chain tool-result replacements."""
        ctx = self.create_context()
        current = event
        modified = False

        for extension in self.extensions:
            for handler in extension.handlers.get("tool_result", []):
                try:
                    result = handler(current, ctx)
                    if _is_awaitable(result):
                        result = await result
                except Exception as error:  # noqa: BLE001
                    self._record_error(extension, "tool_result", error)
                    continue

                if result is None:
                    continue
                if getattr(result, "content", None) is not None:
                    current.content = result.content
                    modified = True
                if getattr(result, "details", None) is not None:
                    current.details = result.details
                    modified = True
                if getattr(result, "is_error", None) is not None:
                    current.is_error = result.is_error
                    modified = True

        if not modified:
            return None
        return _as_result(current)

    # -- internals ---------------------------------------------------------------------

    def _record_error(self, extension: ExtensionAPI, event: str, error: BaseException) -> None:
        record = ExtensionError(
            extension_path=extension.path,
            event=event,
            error=str(error),
            stack="".join(traceback.format_exception(type(error), error, error.__traceback__)),
        )
        self._errors.append(record)
        for listener in list(self._error_listeners):
            try:
                listener(record)
            except Exception:  # noqa: BLE001 - error listeners must not cascade
                pass


def _as_result(event: ToolResultEvent) -> ToolResultEventResult:
    return ToolResultEventResult(
        content=event.content, details=event.details, is_error=event.is_error
    )


def _is_awaitable(value: Any) -> bool:
    import inspect

    return inspect.isawaitable(value)


__all__ = ["ExtensionRunner"]

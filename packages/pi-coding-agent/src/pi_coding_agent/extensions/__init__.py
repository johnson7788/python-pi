"""Python-native extension protocol.

The Python analogue of pi's TypeScript extension system.  An extension is a plain
Python module exposing a ``setup(pi)`` factory::

    from pi_coding_agent.extensions import ExtensionAPI

    def setup(pi: ExtensionAPI) -> None:
        pi.on("tool_call", guard)
        pi.register_tool(...)
        pi.register_command("hello", "Say hello", handler)

Where this deviates from pi, and why, is documented in ``docs/EXTENSIONS.md``.
The short version: no cross-language compatibility (pi's extensions are loaded by
``jiti`` into the same process and hand back live TUI components, which cannot
cross a language boundary), and no permission sandbox — matching pi, which runs
extensions with the full permissions of the process.
"""

from __future__ import annotations

from .api import ExtensionAPI, ExtensionContext, ExtensionHost, NullUIContext, UIContext
from .runner import ExtensionRunner
from .types import (
    EMITTED_EVENTS,
    BeforeAgentStartEvent,
    BeforeAgentStartResult,
    CustomMessageSpec,
    ExtensionError,
    ExtensionEvent,
    InputEvent,
    InputEventResult,
    MessageEndEvent,
    MessageEndResult,
    RegisteredCommand,
    RegisteredFlag,
    SessionBeforeEventResult,
    SessionBeforeForkEvent,
    SessionBeforeSwitchEvent,
    SessionInfoChangedEvent,
    SessionShutdownEvent,
    SessionStartEvent,
    ToolCallEvent,
    ToolCallEventResult,
    ToolResultEvent,
    ToolResultEventResult,
)

#: Loading pulls in filesystem and ``importlib.metadata`` machinery; resolve it on
#: first use so importing the protocol types (which the session does on every event)
#: stays cheap.
_LAZY_EXPORTS = {
    "ExtensionDiagnostic": "pi_coding_agent.extensions.loader",
    "ExtensionLoadResult": "pi_coding_agent.extensions.loader",
    "load_extensions": "pi_coding_agent.extensions.loader",
    "resolve_extension_paths": "pi_coding_agent.extensions.loader",
}


def __getattr__(name: str):
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_name), name)

__all__ = [
    "EMITTED_EVENTS",
    "BeforeAgentStartEvent",
    "BeforeAgentStartResult",
    "CustomMessageSpec",
    "ExtensionAPI",
    "ExtensionContext",
    "ExtensionError",
    "ExtensionEvent",
    "ExtensionHost",
    "ExtensionLoadResult",
    "ExtensionRunner",
    "InputEvent",
    "InputEventResult",
    "MessageEndEvent",
    "MessageEndResult",
    "NullUIContext",
    "RegisteredCommand",
    "RegisteredFlag",
    "SessionBeforeEventResult",
    "SessionBeforeForkEvent",
    "SessionBeforeSwitchEvent",
    "SessionInfoChangedEvent",
    "SessionShutdownEvent",
    "SessionStartEvent",
    "ToolCallEvent",
    "ToolCallEventResult",
    "ToolResultEvent",
    "ToolResultEventResult",
    "UIContext",
    "load_extensions",
    "resolve_extension_paths",
]

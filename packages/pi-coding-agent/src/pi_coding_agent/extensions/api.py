"""The object extensions register against, and the context handlers receive.

``ExtensionAPI`` mirrors pi's ``ExtensionAPI``: a per-extension registry that
plugins populate from their ``setup(pi)`` factory.  ``ExtensionContext`` mirrors
pi's ``ExtensionContext``, trimmed to what makes sense headlessly.

Everything a handler does is *awaited but isolated*: a raising handler is captured
as an :class:`~.types.ExtensionError` and never breaks the agent loop.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

from pi_agent import AgentTool, AgentToolResult

from .types import (
    ExtensionHandler,
    ProviderRegistration,
    RegisteredCommand,
    RegisteredFlag,
)


# --------------------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------------------


class UIContext:
    """What an extension can ask the user.

    ``has_ui`` on the context tells an extension whether asking makes sense at all;
    pi's guidance is to check it before prompting.
    """

    def notify(self, message: str, type: str = "info") -> None:  # noqa: A002 - pi's name
        raise NotImplementedError

    def set_status(self, key: str, text: str | None) -> None:
        raise NotImplementedError

    def set_title(self, title: str) -> None:
        raise NotImplementedError

    async def select(self, title: str, options: Sequence[str], *, timeout: float | None = None) -> str | None:
        raise NotImplementedError

    async def confirm(self, title: str, message: str, *, timeout: float | None = None) -> bool:
        raise NotImplementedError

    async def input(
        self, title: str, placeholder: str | None = None, *, timeout: float | None = None
    ) -> str | None:
        raise NotImplementedError

    async def editor(self, title: str, prefill: str | None = None) -> str | None:
        raise NotImplementedError


class NullUIContext(UIContext):
    """Used in modes with no interactive channel (print, json).

    Notifications go to stderr so they are still visible; dialogs decline, which is
    the only safe answer when nobody can answer.
    """

    def __init__(self, stream: Any = None) -> None:
        import sys

        self._stream = stream if stream is not None else sys.stderr

    def notify(self, message: str, type: str = "info") -> None:  # noqa: A002
        print(f"[{type}] {message}", file=self._stream)

    def set_status(self, key: str, text: str | None) -> None:
        del key, text

    def set_title(self, title: str) -> None:
        del title

    async def select(self, title: str, options: Sequence[str], *, timeout: float | None = None) -> str | None:
        del title, options, timeout
        return None

    async def confirm(self, title: str, message: str, *, timeout: float | None = None) -> bool:
        del title, message, timeout
        return False

    async def input(
        self, title: str, placeholder: str | None = None, *, timeout: float | None = None
    ) -> str | None:
        del title, placeholder, timeout
        return None

    async def editor(self, title: str, prefill: str | None = None) -> str | None:
        del title, prefill
        return None


# --------------------------------------------------------------------------------------
# ExtensionAPI
# --------------------------------------------------------------------------------------


class ExtensionAPI:
    """Registration surface handed to one extension's ``setup(pi)`` factory."""

    def __init__(self, path: str = "<inline>") -> None:
        self.path = path
        self.handlers: dict[str, list[ExtensionHandler]] = {}
        self.tools: dict[str, AgentTool] = {}
        self.commands: dict[str, RegisteredCommand] = {}
        self.flags: dict[str, RegisteredFlag] = {}
        self.providers: list[ProviderRegistration] = []
        self._flag_values: dict[str, bool | str | None] = {}
        #: Bound by the runner once a session exists; the runtime helpers below need it.
        self._host: Any = None

    # -- events ------------------------------------------------------------------------

    def on(self, event: str, handler: ExtensionHandler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    # -- tools -------------------------------------------------------------------------

    def register_tool(
        self,
        tool: AgentTool | None = None,
        *,
        name: str | None = None,
        description: str = "",
        parameters: dict[str, Any] | None = None,
        label: str | None = None,
        prompt_snippet: str | None = None,
        prompt_guidelines: list[str] | None = None,
        execution_mode: str = "parallel",
        prepare_arguments: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ):
        """Register a tool.  Usable directly or as a decorator.

        Direct::

            pi.register_tool(AgentTool(name="greet", ..., execute=run))

        Decorator (the function becomes ``execute(tool_call_id, args, signal, on_update)``)::

            @pi.register_tool(name="greet", description="Greet someone")
            async def greet(tool_call_id, args, signal, on_update):
                return AgentToolResult(...)
        """

        def _build(execute: Any) -> AgentTool:
            tool_name = name
            if tool_name is None:
                tool_name = getattr(execute, "__name__", None) or "unnamed_tool"
            return AgentTool(
                name=tool_name,
                description=description or (inspect.getdoc(execute) or "").strip().split("\n")[0],
                parameters=parameters or {"type": "object", "properties": {}},
                execute=_wrap_execute(execute),
                label=label,
                prompt_snippet=prompt_snippet,
                prompt_guidelines=prompt_guidelines,
                execution_mode=execution_mode,  # type: ignore[arg-type]
                prepare_arguments=prepare_arguments,
            )

        if tool is not None:
            self._add_tool(tool)
            return tool

        if name is None and parameters is None:
            # Bare decorator: @pi.register_tool
            def _bare(execute: Any) -> AgentTool:
                built = _build(execute)
                self._add_tool(built)
                return built

            return _bare

        def _decorator(execute: Any) -> Any:
            built = _build(execute)
            self._add_tool(built)
            return execute

        return _decorator

    def _add_tool(self, tool: AgentTool) -> None:
        if not tool.name:
            raise ValueError("Extension tool must have a name")
        self.tools[tool.name] = tool

    # -- commands ----------------------------------------------------------------------

    def register_command(
        self,
        name: str,
        description: str = "",
        handler: Callable[..., Any] | None = None,
    ):
        """Register a ``/name`` command.

        The handler is called as ``handler(args: str, ctx: ExtensionContext)``; a
        1-argument handler is also accepted.
        """

        def _register(fn: Callable[..., Any]) -> Any:
            self.commands[name] = RegisteredCommand(
                name=name, description=description or (inspect.getdoc(fn) or "").strip().split("\n")[0],
                handler=fn, source=self.path,
            )
            return fn

        if handler is not None:
            return _register(handler)
        return _register

    # -- flags / providers -------------------------------------------------------------

    def register_flag(
        self, name: str, description: str = "", default: bool | str | None = None
    ) -> None:
        self.flags[name] = RegisteredFlag(name=name, description=description, default=default)
        self._flag_values.setdefault(name, default)

    def get_flag(self, name: str) -> bool | str | None:
        return self._flag_values.get(name)

    def set_flag_values(self, values: dict[str, bool | str | None]) -> None:
        """Called by the loader once CLI/env values are known."""
        for key, value in values.items():
            if key in self.flags:
                self._flag_values[key] = value

    def register_provider(self, name: str, config: dict[str, Any]) -> None:
        self.providers.append(ProviderRegistration(name=name, config=dict(config), source=self.path))

    # -- session helpers ---------------------------------------------------------------
    #
    # These delegate to the live session, so they are only valid from inside a handler
    # (or after the runner binds a host).  Outside a session they raise a clear error.

    def _require_host(self) -> Any:
        if self._host is None:
            raise RuntimeError(
                "This ExtensionAPI call needs a live session; it is only available from "
                "inside an event handler or command."
            )
        return self._host

    async def send_message(
        self,
        content: Any,
        *,
        custom_type: str | None = None,
        display: bool = True,
        details: Any = None,
        deliver_as: str | None = None,
    ) -> None:
        """Inject a message into the conversation and persist it.

        ``deliver_as`` controls how it reaches the model: ``"steer"`` queues it for
        the current run, ``"followUp"`` waits for the agent to stop, and ``None``
        appends it to the transcript immediately.
        """
        host = self._require_host()
        await host.send_custom_message(
            content,
            custom_type=custom_type or self.path,
            display=display,
            details=details,
            deliver_as=deliver_as,
        )

    async def append_entry(self, custom_type: str, data: Any = None) -> str:
        host = self._require_host()
        return await host.append_custom_entry(custom_type, data)

    async def set_session_name(self, name: str) -> None:
        await self._require_host().set_session_name(name)

    def get_session_name(self) -> str | None:
        return self._require_host().get_session_name()

    async def set_label(self, entry_id: str, label: str | None) -> None:
        await self._require_host().set_label(entry_id, label)

    async def exec(
        self, command: str, args: Sequence[str] | None = None, *, cwd: str | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        """Run a subprocess.  Returns ``{stdout, stderr, code}``."""
        return await self._require_host().exec(command, args or [], cwd=cwd, timeout=timeout)

    def get_active_tools(self) -> list[str]:
        return list(self._require_host().get_active_tools())

    def get_all_tools(self) -> list[AgentTool]:
        return list(self._require_host().get_all_tools())

    async def set_active_tools(self, names: Sequence[str]) -> None:
        await self._require_host().set_active_tools(names)

    def get_commands(self) -> list[dict[str, Any]]:
        return list(self._require_host().get_commands())

    async def set_model(self, model: Any) -> bool:
        return bool(await self._require_host().set_model(model))

    def get_thinking_level(self) -> str:
        return str(self._require_host().get_thinking_level())

    async def set_thinking_level(self, level: str) -> None:
        await self._require_host().set_thinking_level(level)


def _wrap_execute(execute: Any) -> Any:
    """Normalise a decorated function into the ``AgentTool.execute`` signature.

    Accepts ``(tool_call_id, args, signal, on_update)``, ``(args)`` or ``(args, ctx)``
    shapes so simple tools do not need boilerplate.
    """
    if inspect.iscoroutinefunction(execute):
        arity = _arity(execute)
        if arity >= 4:
            return execute

    arity = _arity(execute)

    async def _execute(tool_call_id: str, args: Any, signal: Any, on_update: Any) -> AgentToolResult:
        del tool_call_id, on_update
        if arity == 0:
            result = execute()
        elif arity == 1:
            result = execute(args)
        else:
            result = execute(args, signal)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, AgentToolResult):
            return result
        from pi_ai import TextContent

        return AgentToolResult(content=[TextContent(text=str(result))])

    return _execute


def _arity(fn: Callable[..., Any]) -> int:
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return 1
    count = 0
    for parameter in signature.parameters.values():
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            count += 1
        elif parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return 4
    return count


# --------------------------------------------------------------------------------------
# ExtensionContext
# --------------------------------------------------------------------------------------


@dataclass
class ExtensionHost:
    """Collaborators a context exposes.  Filled in by the session/runtime."""

    cwd: str = ""
    mode: str = "print"
    has_ui: bool = False
    is_project_trusted: bool = False
    ui: UIContext = field(default_factory=NullUIContext)
    signal: Any = None
    session: Any = None
    runtime: Any = None

    async def send_custom_message(self, content, **kwargs) -> None:
        await self.session.send_custom_message(content, **kwargs)

    async def append_custom_entry(self, custom_type, data=None) -> str:
        return await self.session.append_custom_entry(custom_type, data)


class ExtensionContext:
    """What a handler receives: session access plus user interaction."""

    def __init__(self, host: ExtensionHost) -> None:
        self._host = host

    # -- identity ----------------------------------------------------------------------

    @property
    def cwd(self) -> str:
        return self._host.cwd

    @property
    def mode(self) -> str:
        return self._host.mode

    @property
    def has_ui(self) -> bool:
        return self._host.has_ui

    @property
    def ui(self) -> UIContext:
        return self._host.ui

    @property
    def signal(self) -> Any:
        return self._host.signal

    @property
    def session(self) -> Any:
        return self._host.session

    @property
    def runtime(self) -> Any:
        return self._host.runtime

    def is_project_trusted(self) -> bool:
        return self._host.is_project_trusted

    # -- session queries ---------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._host.session.session_id if self._host.session else ""

    @property
    def session_file(self) -> str | None:
        return self._host.session.session_file if self._host.session else None

    @property
    def session_name(self) -> str | None:
        return self._host.session.get_session_name() if self._host.session else None

    @property
    def messages(self) -> list[Any]:
        return list(self._host.session.messages) if self._host.session else []

    @property
    def model(self) -> Any:
        return self._host.session.model if self._host.session else None

    @property
    def thinking_level(self) -> str:
        return self._host.session.thinking_level if self._host.session else "off"

    def is_idle(self) -> bool:
        return bool(self._host.session.is_idle) if self._host.session else True

    def has_pending_messages(self) -> bool:
        if self._host.session is None:
            return False
        return self._host.session.pending_message_count > 0

    def get_system_prompt(self) -> str:
        return self._host.session.get_system_prompt() if self._host.session else ""

    def abort(self) -> None:
        if self._host.session is not None:
            self._host.session.request_abort()

    async def wait_for_idle(self) -> None:
        if self._host.session is not None:
            await self._host.session.wait_for_idle()

    async def compact(self, custom_instructions: str | None = None) -> Any:
        if self._host.session is None:
            raise RuntimeError("No session to compact")
        return await self._host.session.compact(custom_instructions)

    async def new_session(self, **kwargs) -> Any:
        return await self._require_runtime().new_session(**kwargs)

    async def fork(self, entry_id: str, **kwargs) -> Any:
        return await self._require_runtime().fork(entry_id, **kwargs)

    async def switch_session(self, session_path: str, **kwargs) -> Any:
        return await self._require_runtime().switch_session(session_path, **kwargs)

    def _require_runtime(self) -> Any:
        if self._host.runtime is None:
            raise RuntimeError("Session replacement is unavailable in this process")
        return self._host.runtime


__all__ = [
    "ExtensionAPI",
    "ExtensionContext",
    "ExtensionHost",
    "NullUIContext",
    "UIContext",
]

"""Session replacement runtime.

Port of ``packages/coding-agent/src/core/agent-session-runtime.ts``.

An :class:`AgentSession` is bound to one session file for its whole life: its event
subscriptions, its transcript and its cwd-bound services all point at that session.
Replacing it therefore means *rebuilding* the session, not mutating it — which is
what this layer owns:

* :meth:`AgentSessionRuntime.new_session` — a fresh, empty session.
* :meth:`AgentSessionRuntime.switch_session` — a different existing session file.
* :meth:`AgentSessionRuntime.fork` / :meth:`AgentSessionRuntime.clone` — a new
  session file containing only the path up to a chosen entry.

Two consequences are easy to miss and are documented here because callers hit them:

* ``runtime.session`` is a **different object** after a replacement, so anything
  holding the old session (an event subscription, a UI binding) is now attached to
  a dead object.  Register :meth:`set_rebind_session` to be told when to re-attach.
* a failed replacement propagates the error and leaves the runtime pointing at the
  previous session; the caller decides what to show the user.

Replacing a session **mid-turn does not cancel that turn**.  Disposing a session
aborts its active run, so a session left while it is streaming is *retired*
instead: it keeps running in the background, keeps writing its session file, and
is released once it goes idle.  Switching back to that file before it settles
adopts the live session again, so the user keeps watching the same run.

Extension lifecycle events (``session_before_switch`` / ``session_before_fork`` /
``session_shutdown``) are not emitted because this port has no extension system.
The ``{"cancelled": bool}`` return shape is kept so a caller written against pi
still works.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

from .agent_session import AgentSession
from .session_manager import SessionManager


@dataclass
class AgentSessionRuntimeDiagnostic:
    """A non-fatal problem found while building a session."""

    message: str
    severity: str = "warning"


@dataclass
class AgentSessionRuntimeResult:
    """What a runtime factory returns for one session."""

    session: AgentSession
    services: Any = None
    diagnostics: list[AgentSessionRuntimeDiagnostic] = field(default_factory=list)


@dataclass
class _RetiringSession:
    """A replaced session whose in-flight turn is still running."""

    session: AgentSession
    task: asyncio.Task[None]


#: ``(cwd, agent_dir, session_manager, session_start_event) -> result``
CreateAgentSessionRuntimeFactory = Callable[..., Awaitable[AgentSessionRuntimeResult]]

#: How long a session retired mid-turn may keep running before it is released anyway.
_RETIRE_TIMEOUT_SECONDS = 60 * 60


def extract_user_message_text(content: Any) -> str:
    """Text of a user message, used to prefill the editor after a fork."""
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return ""
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


class AgentSessionRuntime:
    """Owns the active session and can replace it."""

    def __init__(
        self,
        session: AgentSession,
        services: Any,
        create_runtime: CreateAgentSessionRuntimeFactory,
        *,
        cwd: str,
        agent_dir: str,
        diagnostics: Sequence[AgentSessionRuntimeDiagnostic] = (),
        extension_runner: Any = None,
    ) -> None:
        self._session = session
        self._services = services
        self._create_runtime = create_runtime
        self._cwd = cwd
        self._agent_dir = agent_dir
        self._diagnostics = list(diagnostics)
        self._extension_runner = extension_runner
        self._rebind_session: Callable[[AgentSession], Awaitable[None]] | None = None
        self._before_session_invalidate: Callable[[], None] | None = None
        self._lock = asyncio.Lock()
        #: Sessions replaced mid-turn stay alive here until their run settles, so
        #: switching away never cancels work the user already started.
        self._retiring: dict[str, _RetiringSession] = {}

    # -- accessors ---------------------------------------------------------------------

    @property
    def session(self) -> AgentSession:
        return self._session

    @property
    def services(self) -> Any:
        return self._services

    @property
    def cwd(self) -> str:
        return self._cwd

    @property
    def agent_dir(self) -> str:
        return self._agent_dir

    @property
    def diagnostics(self) -> list[AgentSessionRuntimeDiagnostic]:
        return list(self._diagnostics)

    def set_rebind_session(self, callback: Callable[[AgentSession], Awaitable[None]] | None) -> None:
        """Called with the new session after every replacement.

        Anything holding a reference to the previous session must re-attach here.
        """
        self._rebind_session = callback

    def set_before_session_invalidate(self, callback: Callable[[], None] | None) -> None:
        """Called after teardown but before the old session is invalidated."""
        self._before_session_invalidate = callback

    def has_retiring_runs(self) -> bool:
        """True while a replaced session is still finishing its last turn.

        Hosts use this to keep a worker alive (no idle eviction) until the work the
        user walked away from has actually finished.
        """
        return any(not entry.task.done() for entry in self._retiring.values())

    # -- replacement -------------------------------------------------------------------

    async def new_session(
        self,
        *,
        parent_session: str | None = None,
        setup: Callable[[SessionManager], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Start a fresh session, keeping the storage mode of the current one."""
        async with self._lock:
            manager = self._session.session_manager
            previous_session_file = self._session.session_file
            if await self._before_replacement(
                "session_before_switch", reason="new", target_session_file=None
            ):
                return {"cancelled": True}

            if manager.is_persisted():
                new_manager = SessionManager.create(
                    self._cwd, session_dir=manager.get_session_dir()
                )
            else:
                new_manager = SessionManager.in_memory(self._cwd)
            if parent_session:
                new_manager.new_session(parent_session=parent_session)

            await self._teardown("new", new_manager.get_session_file())
            await self._apply(await self._build(self._cwd, new_manager))

            if setup is not None:
                await setup(self._session.session_manager)
                self._session.agent.set_messages(
                    self._session.session_manager.build_context().messages
                )
            await self._finish()
            await self._notify_start("new", previous_session_file)
            return {"cancelled": False}

    async def switch_session(
        self,
        session_path: str,
        *,
        cwd_override: str | None = None,
    ) -> dict[str, Any]:
        """Adopt a different session file."""
        async with self._lock:
            previous_session_file = self._session.session_file
            if await self._before_replacement(
                "session_before_switch", reason="resume", target_session_file=session_path
            ):
                return {"cancelled": True}

            resumed = self._take_retiring(session_path)
            if resumed is not None:
                # That conversation is still streaming in the background: take the
                # live session back instead of reopening a half-written file, so the
                # user resumes watching the same run.
                await self._teardown("resume", resumed.session_file)
                self._session = resumed
                await self._finish()
                await self._notify_start("resume", previous_session_file)
                return {"cancelled": False}

            manager = SessionManager.open(session_path, cwd=cwd_override)
            await self._teardown("resume", manager.get_session_file())
            await self._apply(await self._build(manager.get_cwd(), manager))
            await self._finish()
            await self._notify_start("resume", previous_session_file)
            return {"cancelled": False}

    async def fork(
        self,
        entry_id: str,
        *,
        position: str = "before",
    ) -> dict[str, Any]:
        """Branch the session at ``entry_id`` into a new session file.

        ``position="before"`` (the default) forks *before* the given entry, which
        must be a user message; the returned ``text`` is that message's text so a
        caller can prefill its editor.  ``position="at"`` forks at the entry itself.
        """
        if position not in ("before", "at"):
            raise ValueError(f"Invalid fork position: {position!r}")

        async with self._lock:
            if await self._before_replacement(
                "session_before_fork", entry_id=entry_id, position=position
            ):
                return {"cancelled": True, "text": None}

            manager = self._session.session_manager
            entry = manager.get_entry(entry_id)
            if entry is None:
                raise ValueError("Invalid entry ID for forking")

            selected_text: str | None = None
            if position == "at":
                target_leaf_id: str | None = entry.id
            else:
                if getattr(entry, "type", None) != "message" or getattr(
                    getattr(entry, "message", None), "role", None
                ) != "user":
                    raise ValueError("Invalid entry ID for forking")
                target_leaf_id = entry.parent_id
                selected_text = extract_user_message_text(getattr(entry.message, "content", None))

            previous_session_file = self._session.session_file

            if manager.is_persisted():
                if not target_leaf_id:
                    # Forking before the very first entry: nothing to copy.
                    new_manager = SessionManager.create(
                        self._cwd, session_dir=manager.get_session_dir()
                    )
                    new_manager.new_session(parent_session=previous_session_file)
                    await self._teardown("fork", new_manager.get_session_file())
                    await self._apply(await self._build(self._cwd, new_manager))
                    await self._finish()
                    await self._notify_start("fork", previous_session_file)
                    return {"cancelled": False, "text": selected_text}

                if previous_session_file is None or not os.path.exists(previous_session_file):
                    raise ValueError(
                        "This session has not been saved yet. Wait for the first assistant "
                        "response before cloning or forking it."
                    )
                # Re-open so a failure mid-branch cannot leave the live manager
                # half-rewritten (upstream does the same).
                branch_manager = SessionManager.open(previous_session_file, cwd=self._cwd)
                if branch_manager.create_branched_session(target_leaf_id) is None:
                    raise ValueError("Failed to create forked session")
                await self._teardown("fork", branch_manager.get_session_file())
                await self._apply(await self._build(branch_manager.get_cwd(), branch_manager))
                await self._finish()
                await self._notify_start("fork", previous_session_file)
                return {"cancelled": False, "text": selected_text}

            # In-memory sessions have no file to branch into, so the manager is
            # rebound in place.
            await self._teardown("fork", manager.get_session_file())
            if not target_leaf_id:
                manager.new_session(parent_session=previous_session_file)
            else:
                manager.create_branched_session(target_leaf_id)
            await self._apply(await self._build(self._cwd, manager))
            await self._finish()
            await self._notify_start("fork", previous_session_file)
            return {"cancelled": False, "text": selected_text}

    async def clone(self) -> dict[str, Any]:
        """Fork at the current leaf, producing a copy of the whole conversation."""
        leaf_id = self._session.session_manager.get_leaf_id()
        if leaf_id is None:
            raise ValueError("Cannot clone: the session has no entries")
        result = await self.fork(leaf_id, position="at")
        return {"cancelled": result["cancelled"], "text": result.get("text")}

    async def dispose(self) -> None:
        """Release the current session."""
        async with self._lock:
            self._dispose_current()

    # -- internals ---------------------------------------------------------------------

    async def _build(self, cwd: str, manager: SessionManager) -> AgentSessionRuntimeResult:
        result = await self._create_runtime(
            cwd=cwd,
            agent_dir=self._agent_dir,
            session_manager=manager,
            session_start_event={"type": "session_start", "reason": "replacement"},
        )
        if isinstance(result, AgentSession):
            # Tolerate a factory that returns the session directly.
            return AgentSessionRuntimeResult(session=result)
        return result

    async def _apply(self, result: AgentSessionRuntimeResult) -> None:
        self._session = result.session
        self._services = result.services
        self._diagnostics = list(result.diagnostics)

    async def _finish(self) -> None:
        if self._rebind_session is not None:
            await self._rebind_session(self._session)

    async def _before_replacement(self, event_type: str, **fields) -> bool:
        """Ask extensions to veto a pending replacement.  Returns True when cancelled."""
        if self._extension_runner is None:
            return False
        from .extensions.types import SessionBeforeForkEvent, SessionBeforeSwitchEvent

        event = (
            SessionBeforeForkEvent(**fields)
            if event_type == "session_before_fork"
            else SessionBeforeSwitchEvent(**fields)
        )
        result = await self._extension_runner.emit(event)
        return bool(result is not None and getattr(result, "cancel", False))

    async def _notify_start(self, reason: str, previous_session_file: str | None) -> None:
        """Re-point extension contexts at the new session and announce it."""
        if self._extension_runner is None:
            return
        host = self._extension_runner.host
        host.session = self._session
        self._extension_runner.bind_host(host)
        await self._session.emit_session_start(reason, previous_session_file)

    def _dispose_current(self) -> None:
        if self._before_session_invalidate is not None:
            self._before_session_invalidate()
        self._session.dispose()

    async def _teardown(self, reason: str, target_session_file: str | None) -> None:
        del reason, target_session_file  # only meaningful for extension events
        session = self._session
        if getattr(session, "is_streaming", False):
            # The user switched away mid-turn.  Disposing here would abort the run
            # (``AgentSession.dispose`` aborts an active run), so detach the session
            # from this client and let it finish on its own; the turn keeps being
            # written to its session file and is complete on the next visit.
            self._release_listeners()
            self._retire(session)
            return
        self._dispose_current()

    def _release_listeners(self) -> None:
        if self._before_session_invalidate is not None:
            self._before_session_invalidate()

    def _retire(self, session: AgentSession) -> None:
        """Dispose ``session`` once its in-flight turn settles."""
        key = session.session_file or f"memory:{id(session)}"
        existing = self._retiring.get(key)
        if existing is not None and not existing.task.done():
            return

        task = asyncio.ensure_future(self._dispose_when_idle(session))
        self._retiring[key] = _RetiringSession(session, task)

        def _forget(finished: asyncio.Task[None], retiring_key: str = key) -> None:
            current = self._retiring.get(retiring_key)
            if current is not None and current.task is finished:
                self._retiring.pop(retiring_key, None)
            # Surface nothing: a retired session must never break the new one.

        task.add_done_callback(_forget)

    async def _dispose_when_idle(self, session: AgentSession) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _RETIRE_TIMEOUT_SECONDS
        # A cancellation here means the session was adopted back by ``switch_session``
        # (or superseded), so the run must keep going: only an idle session is freed.
        while session.is_streaming and loop.time() < deadline:
            await asyncio.sleep(0.25)
        try:
            session.dispose()
        except Exception:  # noqa: BLE001 - teardown is best effort
            pass

    def _take_retiring(self, session_path: str) -> AgentSession | None:
        """Claim a still-running retired session for ``session_path``, if any."""
        target = os.path.realpath(session_path)
        for key, entry in list(self._retiring.items()):
            if key.startswith("memory:") or os.path.realpath(key) != target:
                continue
            if entry.task.done():
                # It settled (and was disposed) between the click and this check.
                self._retiring.pop(key, None)
                continue
            self._retiring.pop(key, None)
            entry.task.cancel()
            return entry.session
        return None


async def create_agent_session_runtime(
    create_runtime: CreateAgentSessionRuntimeFactory,
    *,
    cwd: str,
    agent_dir: str,
    session_manager: SessionManager,
    extension_runner: Any = None,
) -> AgentSessionRuntime:
    """Build a runtime around an initial session."""
    result = await create_runtime(
        cwd=cwd,
        agent_dir=agent_dir,
        session_manager=session_manager,
        session_start_event={"type": "session_start", "reason": "startup"},
    )
    if isinstance(result, AgentSession):
        result = AgentSessionRuntimeResult(session=result)
    return AgentSessionRuntime(
        result.session,
        result.services,
        create_runtime,
        cwd=cwd,
        agent_dir=agent_dir,
        diagnostics=result.diagnostics,
        extension_runner=extension_runner,
    )


__all__ = [
    "AgentSessionRuntime",
    "AgentSessionRuntimeDiagnostic",
    "AgentSessionRuntimeResult",
    "CreateAgentSessionRuntimeFactory",
    "create_agent_session_runtime",
    "extract_user_message_text",
]

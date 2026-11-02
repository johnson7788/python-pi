"""Background subagent delegation: the ``spawn`` and ``fork`` providers.

Two design points shape this module.

**A child is a real session, not a side channel.**  Spawning reserves a fresh
JSONL session under the parent's session directory (header ``parentSession``
points at the parent), so the child's transcript is durable, listed by the web UI,
and inspectable after the parent has moved on.  Fork adds the parent's completed
turns as a seed; it never shares the parent's in-flight turn.

**The tool returns immediately; the result arrives later.**  A delegation tool call
answers ``started subagent <id>`` as soon as the child is admitted, then this
manager runs the child as an asyncio task.  When it settles, the manager injects a
``custom`` notice into the parent transcript (steered into the running turn when
the parent is streaming, followed by a continuation turn when it is idle).  That is
what keeps a slow child from blocking the parent — and what makes parallel
delegation possible.

Limits are deliberately fail-fast rather than queued: an over-depth or
over-capacity request is refused with a stable code, because a parent waiting for
descendants must never wait on a slot it is itself occupying.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Sequence

from pi_ai import Cost, Usage

from ..session_manager import SessionManager
from .types import (
    DEFAULT_MAX_ACTIVE,
    DEFAULT_MAX_DEPTH,
    MAX_NOTICE_CHARS,
    SubagentError,
    SubagentProvider,
    SubagentRecord,
    SubagentResult,
    SubagentStatus,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..agent_session import AgentSession

#: ``(session_manager, depth, parent_session) -> child session``.  The manager
#: creates the session store; the factory builds the agent on top of it.
ChildFactory = Callable[[SessionManager, int, "AgentSession"], Awaitable["AgentSession"]]


class SubagentManager:
    """Owns every activation under one bootstrapped session tree."""

    def __init__(
        self,
        child_factory: ChildFactory | None = None,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_active: int = DEFAULT_MAX_ACTIVE,
        auto_continue: bool = True,
    ) -> None:
        if max_depth < 0:
            raise ValueError("max_depth must be >= 0")
        if max_active < 1:
            raise ValueError("max_active must be >= 1")
        self._child_factory = child_factory
        self._max_depth = int(max_depth)
        self._max_active = int(max_active)
        self._auto_continue = bool(auto_continue)
        self._records: dict[str, SubagentRecord] = {}
        self._running: dict[str, SubagentRecord] = {}
        #: child session id -> depth of that child
        self._depths: dict[str, int] = {}

    # -- configuration -----------------------------------------------------------------

    def set_child_factory(self, factory: ChildFactory) -> None:
        self._child_factory = factory

    @property
    def max_depth(self) -> int:
        return self._max_depth

    @property
    def max_active(self) -> int:
        return self._max_active

    @property
    def auto_continue(self) -> bool:
        return self._auto_continue

    # -- attachment --------------------------------------------------------------------

    def attach(self, session: "AgentSession", *, depth: int = 0) -> None:
        """Add the delegation tools to ``session`` and register its depth.

        Depth is recorded per session id so a tool closure never has to guess
        whether its caller is a root, a child, or a grandchild.
        """
        self._depths[session.session_id] = int(depth)
        from .tools import create_subagent_tools

        session.add_tools(create_subagent_tools(self, session, depth))
        session.add_dispose_callback(lambda: self._cancel_session(session))
        session.set_background_work(lambda: self._wait_for_children(session))

    def depth_of(self, session: "AgentSession") -> int:
        return self._depths.get(session.session_id, 0)

    # -- admission ---------------------------------------------------------------------

    async def start(
        self,
        provider: SubagentProvider,
        parent: "AgentSession",
        depth: int,
        prompt: str,
        label: str | None = None,
    ) -> SubagentRecord:
        """Admit one delegation and schedule it; returns before the child runs."""
        if self._child_factory is None:
            raise SubagentError("SUBAGENT_UNAVAILABLE", "no child session factory is configured")
        if provider not in ("spawn", "fork"):
            raise SubagentError("SUBAGENT_UNKNOWN_PROVIDER", f"unknown provider {provider!r}")
        text = (prompt or "").strip()
        if not text:
            raise SubagentError("SUBAGENT_EMPTY_PROMPT", "prompt must be a non-empty string")
        if depth >= self._max_depth:
            raise SubagentError(
                "SUBAGENT_DEPTH_LIMIT",
                f"delegation depth limit reached ({self._max_depth}); finish this task directly",
            )
        if len(self._running) >= self._max_active:
            raise SubagentError(
                "SUBAGENT_CAPACITY",
                f"{self._max_active} subagents are already running; wait for one to finish",
            )

        seed = self._seed_messages(parent, provider)
        child_manager = self._new_child_session_manager(parent)
        record = SubagentRecord(
            child_id=child_manager.get_session_id(),
            provider=provider,
            label=_resolve_label(provider, text, label),
            prompt=text,
            depth=depth + 1,
            parent_session_id=parent.session_id,
            session_manager=child_manager,
            started_at=time.time(),
        )
        self._records[record.child_id] = record
        self._running[record.child_id] = record
        record.task = asyncio.create_task(
            self._run(record, parent, seed), name=f"subagent:{record.child_id}"
        )
        return record

    # -- inspection --------------------------------------------------------------------

    def record(self, child_id: str) -> SubagentRecord | None:
        return self._records.get(child_id)

    def records(self) -> list[SubagentRecord]:
        return list(self._records.values())

    def running(self) -> list[SubagentRecord]:
        return list(self._running.values())

    def has_running(self) -> bool:
        return bool(self._running)

    def children_of(self, session_id: str) -> list[SubagentRecord]:
        return [r for r in self._records.values() if r.parent_session_id == session_id]

    async def wait_for(self, child_id: str) -> SubagentRecord | None:
        record = self._records.get(child_id)
        if record is None or record.task is None:
            return record
        await asyncio.gather(record.task, return_exceptions=True)
        return record

    async def wait_all(self) -> None:
        """Wait for every activation currently admitted (and any it spawns)."""
        while self._running:
            tasks = [r.task for r in list(self._running.values()) if r.task is not None]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def dispose_all(self) -> None:
        """Cancel every running child and wait for its teardown."""
        tasks: list[asyncio.Task[Any]] = []
        for record in list(self._running.values()):
            session = record.session
            if session is not None:
                session.request_abort()
            if record.task is not None:
                record.task.cancel()
                tasks.append(record.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- child construction ------------------------------------------------------------

    def _new_child_session_manager(self, parent: "AgentSession") -> SessionManager:
        parent_manager = parent.session_manager
        session_dir = parent_manager.get_session_dir()
        if session_dir:
            return SessionManager.create(
                parent.cwd,
                session_dir=session_dir,
                parent_session=parent.session_id,
            )
        return SessionManager.in_memory(parent.cwd)

    def _seed_messages(self, parent: "AgentSession", provider: SubagentProvider) -> list[Any]:
        """Fork seeds the parent's *completed* turns; spawn seeds nothing.

        The delegating tool call runs inside the parent's open turn, so the seed
        stops at the last user message: the in-flight turn (its prompt, its tool
        call, and partial tool results) is never copied.
        """
        if provider != "fork":
            return []
        messages = list(parent.messages)
        if parent.is_streaming:
            for index in range(len(messages) - 1, -1, -1):
                if getattr(messages[index], "role", None) == "user":
                    return messages[:index]
        return messages

    # -- execution ---------------------------------------------------------------------

    async def _run(self, record: SubagentRecord, parent: "AgentSession", seed: list[Any]) -> None:
        assert record.session_manager is not None
        child: "AgentSession | None" = None
        start_index = 0
        cancelled = False
        try:
            for message in seed:
                await record.session_manager.append_message(message)
            factory = self._child_factory
            assert factory is not None
            child = await factory(record.session_manager, record.depth, parent)
            record.session = child
            self.attach(child, depth=record.depth)
            await child.set_session_name(record.label)
            start_index = len(child.messages)
            await child.prompt(record.prompt)
            # The agent loop swallows cancellation into an aborted message, so a
            # cancelled task can reach this line looking successful.  Ask the task
            # itself: teardown must not turn into a notice.
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise asyncio.CancelledError
            record.result = self._collect(record, child, start_index)
        except asyncio.CancelledError:
            # A cancelled child is the host tearing the tree down, not a result the
            # parent asked for: settle the record but never notify or continue.
            cancelled = True
            record.status = "aborted"
            record.result = self._result(
                record,
                status="aborted",
                text=_last_assistant_text(child) if child is not None else "",
                error="cancelled",
            )
        except Exception as error:  # noqa: BLE001 - a failed child is a settled result
            record.status = "failed"
            record.result = self._result(
                record,
                status="failed",
                text=_last_assistant_text(child) if child is not None else "",
                error=str(error) or error.__class__.__name__,
            )
        finally:
            record.session = None
            if child is not None:
                try:
                    child.dispose()
                except Exception:  # noqa: BLE001 - teardown must not mask the result
                    pass
            if record.result is None:
                record.result = self._result(record, status="failed", error="child produced no result")
            record.finished_at = time.time()
            record.result.finished_at = record.finished_at
            record.result.started_at = record.started_at

        if not cancelled:
            try:
                await self._deliver(record, parent)
            except Exception as error:  # noqa: BLE001 - delivery must not crash the manager
                record.delivery_error = str(error)
        self._running.pop(record.child_id, None)

    def _collect(
        self, record: SubagentRecord, child: "AgentSession", start_index: int
    ) -> SubagentResult:
        produced = list(child.messages[start_index:])
        assistants = [m for m in produced if getattr(m, "role", None) == "assistant"]
        last = assistants[-1] if assistants else None
        stop_reason = getattr(last, "stop_reason", None) if last is not None else None
        text = _last_assistant_text(child)
        error = getattr(last, "error_message", None) if last is not None else None

        if last is None or stop_reason == "error":
            status = "failed"
            error = error or "the subagent produced no assistant message"
        elif stop_reason == "aborted":
            status = "aborted"
        else:
            status = "completed"
        record.status = status
        return self._result(
            record,
            status=status,
            text=text,
            error=error,
            stop_reason=stop_reason,
            usage=_sum_usage(assistants),
        )

    def _result(
        self,
        record: SubagentRecord,
        *,
        status: SubagentStatus,
        text: str = "",
        error: str | None = None,
        stop_reason: str | None = None,
        usage: Usage | None = None,
    ) -> SubagentResult:
        session_file = None
        if record.session_manager is not None:
            session_file = record.session_manager.get_session_file()
        return SubagentResult(
            child_id=record.child_id,
            provider=record.provider,
            label=record.label,
            status=status,
            text=text,
            error=error,
            stop_reason=stop_reason,
            usage=usage,
            session_file=session_file,
            started_at=record.started_at,
            finished_at=time.time(),
        )

    # -- delivery ----------------------------------------------------------------------

    async def _deliver(self, record: SubagentRecord, parent: "AgentSession") -> None:
        result = record.result
        assert result is not None
        text = _notice_text(result)
        details = {
            "childId": result.child_id,
            "provider": result.provider,
            "label": result.label,
            "status": result.status,
            "sessionFile": result.session_file,
            "stopReason": result.stop_reason,
            "durationSeconds": round(result.duration_seconds, 3),
            "usage": _usage_to_json(result.usage),
        }
        if parent.is_streaming:
            # The parent is mid-turn: the notice rides the steering queue and is
            # drained at the next turn boundary.
            await parent.send_custom_message(
                text, custom_type="subagent", deliver_as="steer", details=details
            )
            if self._auto_continue and not parent.is_streaming:
                # Lost the race: the turn ended between the check and the enqueue.
                # ``continue_turn`` drains the steering queue, so the notice is not
                # stranded until the next user prompt.
                try:
                    await parent.continue_turn()
                except RuntimeError:
                    pass
            return
        await parent.send_custom_message(text, custom_type="subagent", details=details)
        if not self._auto_continue:
            return
        try:
            await parent.continue_turn()
        except RuntimeError:
            # A user prompt won the race; the notice is already in the transcript
            # and that running turn will see it (or the next one will).
            return

    # -- teardown ----------------------------------------------------------------------

    def _cancel_session(self, session: "AgentSession") -> None:
        """Cancel every descendant of ``session`` (called by ``AgentSession.dispose``)."""
        for record in self._descendants(session.session_id):
            if record.task is not None and not record.task.done():
                record.task.cancel()

    def _descendants(self, session_id: str) -> list[SubagentRecord]:
        found: list[SubagentRecord] = []
        frontier = [session_id]
        seen: set[str] = set()
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            for record in self._records.values():
                if record.parent_session_id == current:
                    found.append(record)
                    frontier.append(record.child_id)
        return found

    async def _wait_for_children(self, session: "AgentSession") -> bool:
        """Drain hook: ``True`` means at least one child was awaited."""
        children = [r for r in self._descendants(session.session_id) if r.is_running]
        tasks = [r.task for r in children if r.task is not None]
        if not tasks:
            return False
        await asyncio.gather(*tasks, return_exceptions=True)
        return True


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _resolve_label(provider: SubagentProvider, prompt: str, label: str | None) -> str:
    cleaned = (label or "").strip().replace("\n", " ")
    if cleaned:
        return cleaned[:120]
    first_line = prompt.strip().splitlines()[0] if prompt.strip() else "subagent"
    return f"{provider}: {first_line[:80]}"


def _last_assistant_text(session: "AgentSession | None") -> str:
    if session is None:
        return ""
    for message in reversed(session.messages):
        if getattr(message, "role", None) != "assistant":
            continue
        parts = []
        for block in getattr(message, "content", None) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str) and text:
                parts.append(text)
        if parts:
            return "\n".join(parts)
    return ""


def _sum_usage(messages: Sequence[Any]) -> Usage | None:
    total: Usage | None = None
    for message in messages:
        usage = getattr(message, "usage", None)
        if not isinstance(usage, Usage):
            continue
        if total is None:
            total = Usage(cost=Cost())
        total.input += usage.input
        total.output += usage.output
        total.cache_read += usage.cache_read
        total.cache_write += usage.cache_write
        total.total_tokens += usage.total_tokens or (usage.input + usage.output)
        total.cost.input += usage.cost.input
        total.cost.output += usage.cost.output
        total.cost.cache_read += usage.cost.cache_read
        total.cost.cache_write += usage.cost.cache_write
        total.cost.total += usage.cost.total
    return total


def _usage_to_json(usage: Usage | None) -> dict[str, Any] | None:
    if usage is None:
        return None
    return {
        "input": usage.input,
        "output": usage.output,
        "cacheRead": usage.cache_read,
        "cacheWrite": usage.cache_write,
        "totalTokens": usage.total_tokens,
        "cost": {
            "input": usage.cost.input,
            "output": usage.cost.output,
            "cacheRead": usage.cost.cache_read,
            "cacheWrite": usage.cost.cache_write,
            "total": usage.cost.total,
        },
    }


def _notice_text(result: SubagentResult) -> str:
    duration = f"{result.duration_seconds:.1f}s"
    header = f"[subagent {result.provider}] {result.label} — {result.status} in {duration}"
    lines = [header, f"child: {result.child_id}"]
    if result.session_file:
        lines.append(f"session: {result.session_file}")
    if result.usage is not None and result.usage.total_tokens:
        lines.append(f"tokens: {result.usage.total_tokens} (cost ${result.usage.cost.total:.4f})")
    body = result.error if result.status in ("failed", "aborted") and not result.text else result.text
    if result.status in ("failed", "aborted") and result.error:
        lines.append(f"error: {result.error}")
    if body:
        if len(body) > MAX_NOTICE_CHARS:
            body = body[:MAX_NOTICE_CHARS] + "\n… (truncated; read the child session for the full result)"
        lines.append("")
        lines.append(body.strip())
    return "\n".join(lines).strip()


__all__ = ["ChildFactory", "SubagentManager"]

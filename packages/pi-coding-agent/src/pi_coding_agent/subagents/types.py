"""Value types for background subagent delegation.

The vocabulary mirrors the DeepSeek Harness subagent seam, reduced to the two
in-process providers this port ships: ``spawn`` (fresh child) and ``fork`` (child
seeded with the parent's completed turns).  Unlike DSH there is no out-of-process
backend yet, so a record always owns a local :class:`AgentSession`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pi_ai import Usage

if TYPE_CHECKING:  # pragma: no cover - import cycle kept out of runtime
    import asyncio

    from ..agent_session import AgentSession
    from ..session_manager import SessionManager

#: Backends this port implements.  Both run in the parent's process.
SubagentProvider = Literal["spawn", "fork"]

#: Lifecycle of one activation.  ``running`` covers startup and teardown.
SubagentStatus = Literal["running", "completed", "failed", "aborted"]

#: Delegation depth allowed below a root session.  ``1`` means "children may not
#: delegate again"; ``0`` disables delegation entirely.
DEFAULT_MAX_DEPTH = 1

#: Concurrent activations per manager (one manager per bootstrapped session tree).
DEFAULT_MAX_ACTIVE = 8

#: Cap on the result text copied into the parent transcript.  Larger outputs stay
#: in the child session, whose path the notice carries.
MAX_NOTICE_CHARS = 8_000


class SubagentError(Exception):
    """A delegation request the manager refuses before any child exists.

    The ``code`` is a stable, machine-readable token (``SUBAGENT_DEPTH_LIMIT`` …)
    so a client can branch on it without parsing prose.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


@dataclass
class SubagentResult:
    """The settled outcome of one activation."""

    child_id: str
    provider: SubagentProvider
    label: str
    status: SubagentStatus
    #: Final assistant text, or the partial text a failed run produced.
    text: str = ""
    error: str | None = None
    stop_reason: str | None = None
    usage: Usage | None = None
    session_file: str | None = None
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.finished_at - self.started_at)


@dataclass
class SubagentRecord:
    """One managed activation, from admission to settlement."""

    child_id: str
    provider: SubagentProvider
    label: str
    prompt: str
    #: Depth of the *child* (the calling session's depth + 1).
    depth: int
    parent_session_id: str
    status: SubagentStatus = "running"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: SubagentResult | None = None
    #: Set while the child session exists (cleared after teardown).
    session: "AgentSession | None" = None
    session_manager: "SessionManager | None" = None
    task: "asyncio.Task[None] | None" = None
    #: A delivery failure never fails the activation; it is recorded here.
    delivery_error: str | None = None

    @property
    def is_running(self) -> bool:
        return self.status == "running"


__all__ = [
    "DEFAULT_MAX_ACTIVE",
    "DEFAULT_MAX_DEPTH",
    "MAX_NOTICE_CHARS",
    "SubagentError",
    "SubagentProvider",
    "SubagentRecord",
    "SubagentResult",
    "SubagentStatus",
]

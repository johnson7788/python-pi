"""Background subagent delegation (``spawn`` and ``fork``).

The public surface is :class:`SubagentManager` plus its value types.  A host
creates one manager per session tree, gives it a child-session factory, and calls
:meth:`SubagentManager.attach` on every session in that tree (see
``pi_coding_agent.bootstrap``).  Attaching adds the two model-facing tools and the
lifecycle hooks, so callers only wire the factory.
"""

from __future__ import annotations

from .manager import ChildFactory, SubagentManager
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

__all__ = [
    "ChildFactory",
    "DEFAULT_MAX_ACTIVE",
    "DEFAULT_MAX_DEPTH",
    "MAX_NOTICE_CHARS",
    "SubagentError",
    "SubagentManager",
    "SubagentProvider",
    "SubagentRecord",
    "SubagentResult",
    "SubagentStatus",
]

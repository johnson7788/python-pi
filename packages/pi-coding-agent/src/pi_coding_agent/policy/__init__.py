"""First-party tool governance for a shared deployment.

The library ships the *mechanism* here — a pure classifier (:mod:`.rules`) plus the
extension that acts on it (:mod:`.extension`).  A deployment supplies the *policy* by
injecting a :class:`~pi_coding_agent.policy.rules.PolicyConfig` into the session's
extension host, which is why nothing in this package imports a server.

``POLICY_EXTENSION_PATH`` is the file a deployment loads.  It is passed as a *built-in*
extension path, so it is loaded even when user extensions are disabled — which is the
only safe configuration for a multi-tenant server, since a per-user ``extensions/``
directory would otherwise be arbitrary code execution for every colleague.
"""

from __future__ import annotations

import os

from .rules import (
    Decision,
    PolicyConfig,
    Verdict,
    classify_command,
    classify_tool_call,
    escapes_workspace,
    summarize_call,
)

#: The extension module a deployment should load as a built-in extension.
POLICY_EXTENSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extension.py")

__all__ = [
    "POLICY_EXTENSION_PATH",
    "Decision",
    "PolicyConfig",
    "Verdict",
    "classify_command",
    "classify_tool_call",
    "escapes_workspace",
    "summarize_call",
]

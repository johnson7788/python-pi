"""Tool-call policy: what an agent may run when the machine is shared.

This module is pure decision logic — no ``pi`` imports, no I/O — so the rules can be
unit-tested directly and reused by any host.  :mod:`.extension` is the thin adapter that
turns a verdict into a blocked call or a confirmation dialog.

The policy is deliberately *not* a sandbox
-----------------------------------------

A shared deployment that runs agents in the server process has no containment: anything
the process can reach, the agent's shell can reach too.  What the rules below buy is:

* a **hard refusal** list for privilege escalation, host/container control, writes to
  system paths and reads of the deployment's own secrets;
* a **confirmation** step for calls that reach outside the workspace (installs, pushes,
  remote shells, writes to absolute paths elsewhere);
* everything else runs, because the workspace is already per-user and the alternative —
  confirming every ``rm`` and ``mv`` — makes the product unusable and trains people to
  click through dialogs without reading them.

The real boundary is a container, and that is phase two.  Nothing here should be
described to users as making a hostile colleague safe.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping

#: Tools whose argument set always contains a workspace path under ``path``.
PATH_TOOLS = frozenset({"read", "write", "edit", "ls", "grep", "find", "glob"})

#: Argument names that carry a path for those tools.  ``grep``/``find`` also take
#: ``pattern``, which is a regex or glob and never a path, so it is not inspected.
PATH_ARGUMENTS = ("path", "file", "filePath", "file_path", "dir", "directory")

#: Never offered as a choice, in any deployment.
DENY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("privilege escalation", re.compile(r"(^|[;&|]\s*)(sudo|doas)\b")),
    ("switching user", re.compile(r"(^|[;&|]\s*)su\s+[-a-zA-Z]")),
    ("container or init control", re.compile(r"\b(docker|podman|nerdctl|systemctl|service|kubectl|mount|umount)\b")),
    ("scheduled tasks", re.compile(r"\b(crontab|at|batch)\s")),
    ("writes to a system directory", re.compile(r">>?\s*/(etc|usr|bin|sbin|lib|boot|sys|proc|root|var|opt)\b")),
    ("reads the deployment's secrets", re.compile(r"(/etc/shadow|/etc/sudoers|\.pi/agent|/var/lib/pi|PI_API_KEY|api_key)")),
    ("environment dump", re.compile(r"(^|[;&|]\s*)(env|printenv)(\s|$|[;&|])")),
    ("fork bomb", re.compile(r":\s*\(\s*\)\s*\{")),
    ("deleting the filesystem root", re.compile(r"\brm\s+(-[a-zA-Z]+\s+)*/(\s|$|\*)")),
    ("opening a network listener", re.compile(r"\b(nc|ncat|netcat|socat)\s+-[a-zA-Z]*l")),
    ("piping a download into a shell", re.compile(r"\b(curl|wget)\b[^;&|]*\|\s*(sudo\s+)?(ba|z|k)?sh\b")),
    ("history or key material", re.compile(r"(\.ssh/|id_rsa|id_ed25519|\.aws/credentials|\.netrc)")),
)

#: Offered to the driver as a confirmation, because they reach beyond the workspace.
CONFIRM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("installing packages", re.compile(r"\b(pip|pip3|npm|pnpm|yarn|apt|apt-get|yum|dnf|apk|brew|cargo|go)\s+(install|add|get)\b")),
    ("publishing to a remote", re.compile(r"\bgit\s+(push|remote\s+(add|set-url)|fetch|pull)\b")),
    ("remote shell or transfer", re.compile(r"\b(ssh|scp|sftp|rsync)\b")),
    ("changing permissions", re.compile(r"\b(chmod|chown|chgrp)\b")),
    ("starting a service or daemon", re.compile(r"\b(nohup|supervisord|nginx|redis-server|uvicorn)\b")),
    ("touching another user's home", re.compile(r"(~|\$HOME|/home/|/Users/)\S*")),
)

#: Redirection-ish targets to check for leaving the workspace.  Reading ``/usr/bin/python3``
#: is normal and must stay allowed, so only *write* shapes are inspected.
_WRITE_TARGET = re.compile(
    r"(?:>>?|\btee\b|\bcp\b|\bmv\b|\brm\b|\bmkdir\b|\btouch\b|\bln\b|\bdd\s+of=)"
    r"\s*['\"]?(/[^\s'\";|&)>]+)"
)

#: Writing here is not a meaningful escape and is far too common to confirm.
_BENIGN_TARGETS = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/fd/")


class Decision(str, Enum):
    """What the policy says about one tool call."""

    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    reason: str = ""


@dataclass(frozen=True)
class PolicyConfig:
    """The deployment's policy, injected into a session's extension host.

    ``record`` is the audit sink.  It is a plain callable rather than a database handle
    so this module stays free of any storage dependency (and of any import that would
    create a cycle back into the server).
    """

    workspace_root: str
    uid: str = ""
    session_id: str = ""
    #: Tools that may run without asking.  A tool that is in neither set is refused, so a
    #: newly added built-in tool is closed until a deployment deliberately opens it.
    allow_tools: frozenset[str] = field(
        default_factory=lambda: frozenset({"read", "write", "edit", "ls", "grep", "find"})
    )
    #: Tools whose individual calls are classified (``bash``) or always confirmed.
    confirm_tools: frozenset[str] = field(default_factory=lambda: frozenset({"bash"}))
    #: Extra refusal regexes, for a deployment with its own no-go commands.
    extra_deny: tuple[str, ...] = ()
    #: Seconds to wait for a human answer before treating a confirmation as refused.
    confirm_timeout: float = 300.0
    record: Callable[[dict[str, Any]], None] | None = None


def _within(root: str, candidate: str) -> bool:
    return candidate == root or candidate.startswith(root + os.sep)


def escapes_workspace(workspace_root: str, raw_path: str) -> bool:
    """Whether ``raw_path`` resolves outside ``workspace_root``.

    Symlinks are resolved, so a link planted inside the workspace does not count as
    inside it.  A path that does not exist yet is judged by its deepest existing parent.
    """
    text = (raw_path or "").strip()
    if not text:
        return False
    if "\x00" in text:
        return True
    root = os.path.realpath(workspace_root)
    candidate = text if os.path.isabs(text) else os.path.join(root, text)
    subject = candidate if os.path.exists(candidate) else os.path.dirname(candidate) or candidate
    return not _within(root, os.path.realpath(subject))


def _escaping_write_target(command: str, workspace_root: str) -> str | None:
    for match in _WRITE_TARGET.finditer(command):
        target = match.group(1)
        if target.startswith(_BENIGN_TARGETS):
            continue
        if escapes_workspace(workspace_root, target):
            return target
    return None


def classify_command(command: str, workspace_root: str, extra_deny: tuple[str, ...] = ()) -> Verdict:
    """Classify one ``bash`` command line."""
    text = command or ""
    for label, pattern in DENY_PATTERNS:
        if pattern.search(text):
            return Verdict(Decision.DENY, f"refused by policy: {label}")
    for pattern_text in extra_deny:
        if re.search(pattern_text, text):
            return Verdict(Decision.DENY, f"refused by policy: /{pattern_text}/")
    escaping = _escaping_write_target(text, workspace_root)
    if escaping is not None:
        return Verdict(
            Decision.CONFIRM, f"writes outside your workspace: {escaping}"
        )
    for label, pattern in CONFIRM_PATTERNS:
        if pattern.search(text):
            return Verdict(Decision.CONFIRM, f"needs confirmation: {label}")
    return Verdict(Decision.ALLOW)


def classify_tool_call(
    config: PolicyConfig, tool_name: str, args: Mapping[str, Any] | None
) -> Verdict:
    """The policy's verdict for one tool call."""
    name = (tool_name or "").strip()
    if not name:
        return Verdict(Decision.DENY, "refused by policy: unnamed tool")

    if name not in config.allow_tools and name not in config.confirm_tools:
        return Verdict(Decision.DENY, f"refused by policy: tool {name!r} is not available here")

    payload = args if isinstance(args, Mapping) else {}

    if name in PATH_TOOLS:
        for key in PATH_ARGUMENTS:
            value = payload.get(key)
            if isinstance(value, str) and escapes_workspace(config.workspace_root, value):
                return Verdict(
                    Decision.DENY,
                    f"refused by policy: {key} {value!r} is outside your workspace",
                )
        if name in config.confirm_tools:
            return Verdict(Decision.CONFIRM, f"needs confirmation: {name}")
        return Verdict(Decision.ALLOW)

    if name == "bash":
        return classify_command(
            str(payload.get("command") or ""), config.workspace_root, config.extra_deny
        )

    if name in config.confirm_tools:
        return Verdict(Decision.CONFIRM, f"needs confirmation: {name}")
    return Verdict(Decision.ALLOW)


def summarize_call(tool_name: str, args: Mapping[str, Any] | None, *, limit: int = 400) -> str:
    """A short, human-readable description of a call, for the dialog and the audit row."""
    payload = args if isinstance(args, Mapping) else {}
    if tool_name == "bash":
        text = str(payload.get("command") or "")
    elif tool_name == "write":
        text = str(payload.get("path") or "")
    elif tool_name == "edit":
        text = str(payload.get("path") or "")
    else:
        parts = [f"{key}={payload[key]}" for key in sorted(payload) if key in PATH_ARGUMENTS]
        text = ", ".join(parts)
    text = text.strip()
    return text[:limit] + ("…" if len(text) > limit else "")


__all__ = [
    "CONFIRM_PATTERNS",
    "DENY_PATTERNS",
    "PATH_ARGUMENTS",
    "PATH_TOOLS",
    "Decision",
    "PolicyConfig",
    "Verdict",
    "classify_command",
    "classify_tool_call",
    "escapes_workspace",
    "summarize_call",
]

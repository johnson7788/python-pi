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

#: Anything that looks like a path: no whitespace, quotes, or shell punctuation.
_PATH_TOKEN = re.compile(r"[^\s'\";|&()<>]+")

#: Roots the agent may read even though they are outside its workspace, because reading a
#: compiler, a library, a man page or a CA bundle is ordinary work.  The sensitive entries
#: under ``/etc`` (shadow, sudoers) are caught by :data:`DENY_PATTERNS` before this list is
#: consulted.  ``/tmp`` is here because the service unit runs with ``PrivateTmp=yes``,
#: which makes it per-service rather than a shared drop box.
#:
#: Matching is **lexical**, not symlink-resolved, and that is deliberate: on macOS
#: ``realpath("/home/x")`` lands under ``/System/Volumes/Data``, which would silently
#: exempt every other user's home directory from inspection.
SYSTEM_READ_ROOTS = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/opt",
    "/etc",
    "/System",
    "/Library",
    "/dev",
    "/proc/self",
    "/tmp",
)


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
    #: Roots the agent must never touch: the deployment's own data directory (every user's
    #: workspace and transcript) and, if the operator wants, the install itself.  Without
    #: this, ``cat ../../agents/<other-uid>/sessions/*.jsonl`` is an ordinary-looking read.
    protected_roots: tuple[str, ...] = ()
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


def _scan_paths(command: str, config: "PolicyConfig") -> Verdict | None:
    """Classify the path-looking tokens of a command by where they land.

    This is what closes the read side.  A regex over write shapes only catches
    ``> /etc/passwd``; ``cat ../../agents/<other-uid>/sessions/*.jsonl`` has no write shape
    at all and would otherwise run unchallenged.
    """
    root = os.path.realpath(config.workspace_root)
    protected = tuple(os.path.realpath(path) for path in config.protected_roots if path)

    for match in _PATH_TOKEN.finditer(command):
        token = match.group(0)
        looks_absolute = token.startswith("/")
        climbs = any(part == ".." for part in token.split("/"))
        if not looks_absolute and not climbs:
            continue
        lexical = os.path.normpath(token if looks_absolute else os.path.join(root, token))
        resolved = os.path.realpath(lexical)

        if _within(root, resolved):
            continue
        if any(_within(area, resolved) for area in protected):
            return Verdict(
                Decision.DENY,
                f"refused by policy: {token} is part of the deployment's own data",
            )
        if any(_within(os.path.normpath(area), lexical) for area in SYSTEM_READ_ROOTS):
            continue
        return Verdict(Decision.CONFIRM, f"reaches outside your workspace: {token}")
    return None


def classify_command(command: str, workspace_root: str, extra_deny: tuple[str, ...] = ()) -> Verdict:
    """Classify one ``bash`` command line."""
    return classify_command_with(
        command, PolicyConfig(workspace_root=workspace_root, extra_deny=extra_deny)
    )


def classify_command_with(command: str, config: PolicyConfig) -> Verdict:
    """Classify one ``bash`` command line against a full policy."""
    text = command or ""
    for label, pattern in DENY_PATTERNS:
        if pattern.search(text):
            return Verdict(Decision.DENY, f"refused by policy: {label}")
    for pattern_text in config.extra_deny:
        if re.search(pattern_text, text):
            return Verdict(Decision.DENY, f"refused by policy: /{pattern_text}/")

    # Writes first: `echo x > /tmp/f` is a deliberate escape even though /tmp is readable.
    escaping = _escaping_write_target(text, config.workspace_root)
    if escaping is not None:
        return Verdict(Decision.CONFIRM, f"writes outside your workspace: {escaping}")

    scoped = _scan_paths(text, config)
    if scoped is not None:
        return scoped

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
        return classify_command_with(str(payload.get("command") or ""), config)

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

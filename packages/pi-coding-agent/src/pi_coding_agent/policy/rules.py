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
from typing import Any, Callable, Iterable, Mapping

#: Tools whose argument set always contains a workspace path under ``path``.
PATH_TOOLS = frozenset({"read", "write", "edit", "ls", "grep", "find", "glob"})

#: Path tools that may only ever touch the workspace; the rest are read-only and may also
#: read a declared :attr:`PolicyConfig.read_roots` entry.
WRITE_TOOLS = frozenset({"write", "edit"})

#: What may run without asking when a deployment does not widen the set.  Extensions a
#: host loads deliberately add their own tool names to ``allow_tools``.
#:
#: ``ask_user_question`` belongs here unconditionally: it only waits for a human answer,
#: cannot touch files or the network, and denying it silently removes the model's only
#: way to confirm a requirement.  It is also never a confirmation — asking the user to
#: approve "asking the user" is the kind of dialog that makes people click through.
DEFAULT_ALLOW_TOOLS = frozenset(
    {"read", "write", "edit", "ls", "grep", "find", "present", "ask_user_question"}
)

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
    # ``of=`` is its own alternative: ``dd if=/dev/zero of=/dev/sda`` has an argument
    # between ``dd`` and the target, which an anchored ``dd of=`` would miss — and
    # missing it meant a raw device write was read as an ordinary path inspection.
    r"(?:>>?|\btee\b|\bcp\b|\bmv\b|\brm\b|\bmkdir\b|\btouch\b|\bln\b|\bof=)"
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


class Risk(str, Enum):
    """How much an action can change, independent of whether it is permitted.

    The verdict above says *what to do*; the risk says *what it costs if wrong*, and
    is what a session grant is scoped to.  ``DESTRUCTIVE`` is deliberately not
    grantable: "allow this for the session" must never cover an action that cannot be
    undone.
    """

    READ = "read"
    PROBE = "probe"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


_RISK_RANK = {Risk.READ: 1, Risk.PROBE: 2, Risk.WRITE: 3, Risk.DESTRUCTIVE: 4}

#: Command shapes that remove or overwrite data irreversibly.  Kept narrow: a broad
#: list would mark every ``rm`` destructive and make the distinction useless.
DESTRUCTIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("recursive delete", re.compile(r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+")),
    ("disk write", re.compile(r"\bdd\s+.*\bof=/dev/")),
    ("filesystem format", re.compile(r"\bmkfs(\.\w+)?\b")),
    ("history rewrite", re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-[a-zA-Z]*f)")),
    ("database drop", re.compile(r"\b(drop\s+(table|database|schema)|truncate\s+table)\b", re.I)),
    ("broad delete", re.compile(r"\bfind\b.*\s-delete\b")),
)

#: Command shapes that change files but are recoverable.
WRITE_PATTERNS = (
    re.compile(r">\s*[^&|]"),
    re.compile(r"\b(tee|mv|cp|mkdir|rmdir|touch|chmod|chown|sed\s+-i|truncate|ln)\b"),
)


class SessionGrants:
    """Risks the user approved "for this session" on this conversation.

    Grants only ever move *down*: approving a write covers reads, never a
    destructive action.  The store is deliberately in-memory — a grant dies with the
    process, which is the lifetime users mean by "this session".
    """

    def __init__(self) -> None:
        self._granted: set[Risk] = set()

    def grant(self, risk: Risk) -> bool:
        """Record a grant; returns ``False`` for a risk that may not be granted."""
        if risk is Risk.DESTRUCTIVE:
            return False
        self._granted.add(risk)
        return True

    def covers(self, risk: Risk) -> bool:
        return any(_RISK_RANK[risk] <= _RISK_RANK[granted] for granted in self._granted)

    def clear(self) -> None:
        self._granted.clear()

    def snapshot(self) -> tuple[str, ...]:
        return tuple(sorted(risk.value for risk in self._granted))


def risk_of(tool_name: str, args: Mapping[str, Any] | None = None) -> Risk:
    """Classify one call's risk; unknown tools are treated as a probe, never as read."""
    name = (tool_name or "").strip()
    payload = args if isinstance(args, Mapping) else {}
    if name in WRITE_TOOLS:
        return Risk.WRITE
    if name in PATH_TOOLS:
        return Risk.READ
    if name in ("ask_user_question", "present"):
        return Risk.READ
    if name == "bash":
        command = str(payload.get("command") or "")
        if any(pattern.search(command) for _label, pattern in DESTRUCTIVE_PATTERNS):
            return Risk.DESTRUCTIVE
        if any(pattern.search(command) for pattern in WRITE_PATTERNS):
            return Risk.WRITE
        return Risk.PROBE
    return Risk.PROBE


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
    #: A host that loads admin-installed extensions adds their tool names here — otherwise
    #: every plugin tool is refused and the plugin looks silently broken.
    allow_tools: frozenset[str] = field(default_factory=lambda: DEFAULT_ALLOW_TOOLS)
    #: Tools whose individual calls are classified (``bash``) or always confirmed.
    confirm_tools: frozenset[str] = field(default_factory=lambda: frozenset({"bash"}))
    #: Places outside the workspace that **read-only** tools may inspect.  The skill catalog
    #: is the case that matters: a team deployment symlinks ``<agent>/skills`` to the shared
    #: template, which is outside every workspace, so without this the prompt advertises
    #: skills the agent is then refused permission to open.  Writes stay workspace-only.
    read_roots: tuple[str, ...] = ()
    #: Roots the agent must never touch: the deployment's own data directory (every user's
    #: workspace and transcript) and, if the operator wants, the install itself.  Without
    #: this, ``cat ../../agents/<other-uid>/sessions/*.jsonl`` is an ordinary-looking read.
    protected_roots: tuple[str, ...] = ()
    #: Extra refusal regexes, for a deployment with its own no-go commands.
    extra_deny: tuple[str, ...] = ()
    #: Auto-approve calls that would otherwise ask for confirmation.  Hard-deny
    #: patterns still apply; this only removes the interactive prompt.
    auto_approve: bool = False
    #: Seconds to wait for a human answer before treating a confirmation as refused.
    confirm_timeout: float = 300.0
    record: Callable[[dict[str, Any]], None] | None = None
    #: Approvals remembered for the rest of this session.  Empty by default: a
    #: deployment that wants "allow for this session" turns on ``grant_after_confirm``.
    session_grants: SessionGrants = field(default_factory=SessionGrants)
    #: When true, approving a *write* also covers later writes in this session.  The
    #: dialog text must say so; that copy lives in ``policy/extension.py``.
    grant_after_confirm: bool = False

    def offered_tools(self, names: Iterable[str]) -> list[str]:
        """The subset worth offering to the model.

        A tool in neither ``allow_tools`` nor ``confirm_tools`` is refused before it
        runs; advertising it anyway costs tokens and invites a wasted round.
        """
        return [
            name for name in names if name in self.allow_tools or name in self.confirm_tools
        ]


#: Reason prefix of an allow that came from a session grant (not from the rules).
GRANT_REASON_PREFIX = "granted for this session"


def _within(root: str, candidate: str) -> bool:
    return candidate == root or candidate.startswith(root + os.sep)


def _within_read_roots(config: "PolicyConfig", raw_path: str) -> bool:
    """Whether a path resolves inside one of the declared read-only roots."""
    text = (raw_path or "").strip()
    if not text or not config.read_roots:
        return False
    subject = text if os.path.isabs(text) else os.path.join(config.workspace_root, text)
    if not os.path.exists(subject):
        subject = os.path.dirname(subject) or subject
    resolved = os.path.realpath(subject)
    return any(_within(os.path.realpath(root), resolved) for root in config.read_roots if root)


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
    verdict = _classify_without_grants(config, name, payload)
    if verdict.decision is Decision.CONFIRM:
        risk = risk_of(name, payload)
        if config.session_grants.covers(risk):
            return Verdict(Decision.ALLOW, f"{GRANT_REASON_PREFIX}: {risk.value}")
    return verdict


def _classify_without_grants(
    config: PolicyConfig, name: str, payload: Mapping[str, Any]
) -> Verdict:
    """The rules' verdict before any session grant is considered."""
    if name in PATH_TOOLS:
        for key in PATH_ARGUMENTS:
            value = payload.get(key)
            if not isinstance(value, str) or not value:
                continue
            if not escapes_workspace(config.workspace_root, value):
                continue
            # A declared read-only root (the skill catalog) is open to inspection, never to
            # writing: the agent must be able to load a SKILL.md it was told about.
            if name in WRITE_TOOLS or not _within_read_roots(config, value):
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
    "DEFAULT_ALLOW_TOOLS",
    "DENY_PATTERNS",
    "PATH_ARGUMENTS",
    "PATH_TOOLS",
    "WRITE_TOOLS",
    "Decision",
    "PolicyConfig",
    "Verdict",
    "classify_command",
    "classify_tool_call",
    "escapes_workspace",
    "summarize_call",
]

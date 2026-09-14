"""The values the sandbox contract is written in.

Split out from the package ``__init__`` so the two backends can import the contract without
importing each other — and so the contract can be read on its own.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

#: Where the workspace appears inside a sandbox container.
CONTAINER_WORKSPACE = "/workspace"


@dataclass
class ExecResult:
    """What a shell command produced.

    Plain data on purpose: a remote backend has to serialise it, so it cannot carry live
    process handles or the caller's own objects.  ``timed_out``/``aborted`` are flags rather
    than exceptions for the same reason — the local backend has richer exceptions, and a
    remote one can only report.
    """

    output: str
    raw: bytes = b""
    exit_code: int | None = None
    timed_out: bool = False
    aborted: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SandboxScope:
    """Which sandbox a session is entitled to.

    The id is derived from ``(uid, thread)`` rather than generated: the same user returning
    to the same conversation must land in the same container, and two users must never share
    one.  A hash keeps the id opaque and safe to use as a container name.
    """

    uid: str
    thread: str
    #: The workspace as *this process* sees it.
    workspace: str
    #: The workspace as the **provisioner's host** sees it.  They differ when the server
    #: itself runs in a container: a bind mount is resolved by the daemon on the host, not by
    #: the process asking for it.  ``None`` means "this process is already on the host".
    host_workspace: str | None = None

    def sandbox_id(self) -> str:
        digest = hashlib.sha256(f"{self.uid}:{self.thread}".encode("utf-8")).hexdigest()
        return digest[:16]

    def container_cwd(self) -> str:
        return CONTAINER_WORKSPACE


@runtime_checkable
class SandboxBackend(Protocol):
    """Everything a tool needs from the place it runs.

    The method set mirrors the built-in tool surface one-to-one, so a backend that satisfies
    this protocol can serve every tool without the tools knowing where they are.

    ``exec`` takes ``metadata`` (the ``PI_*`` session variables) rather than a prepared
    environment: a caller cannot know what a container's ``PATH`` or ``HOME`` should be, and
    forwarding the host's environment into a container is both broken (host paths) and
    careless (host credentials).  Each backend builds its own.
    """

    name: str

    async def exec(
        self,
        command: str,
        *,
        cwd: str,
        metadata: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        signal: Any = None,
        on_chunk: Callable[[bytes], None] | None = None,
    ) -> ExecResult:
        """Run ``command`` in a shell and collect its output.

        ``signal`` is the session's abort signal (the user pressing stop).  A backend that
        cannot interrupt work already handed off reports ``aborted`` as soon as it notices,
        which is the honest answer rather than pretending the process died.
        """
        ...

    def read_text(self, path: str, *, limit: int | None = None) -> str:
        """Return up to ``limit`` bytes of ``path`` decoded as UTF-8."""
        ...

    def write_bytes(self, path: str, data: bytes) -> None:
        """Create or replace ``path``, creating parents as needed."""
        ...

    def list_dir(self, path: str) -> list[str]:
        """Names directly under ``path``, sorted."""
        ...

    def exists(self, path: str) -> bool:
        """Whether ``path`` is present."""
        ...


def host_path_for(app_path: str, *, app_root: str | None, host_root: str | None) -> str | None:
    """Translate a path from this process's view into the container host's view.

    Returns ``None`` when no mapping is configured, which the provisioner reads as "the path
    is already a host path" — correct when the server runs directly on the host.
    """
    if not host_root or not app_root:
        return None
    resolved_app = os.path.realpath(app_root)
    resolved = os.path.realpath(app_path)
    if resolved == resolved_app or resolved.startswith(resolved_app + os.sep):
        return os.path.join(os.path.realpath(host_root), os.path.relpath(resolved, resolved_app))
    return None


__all__ = [
    "CONTAINER_WORKSPACE",
    "ExecResult",
    "SandboxBackend",
    "SandboxScope",
    "host_path_for",
]

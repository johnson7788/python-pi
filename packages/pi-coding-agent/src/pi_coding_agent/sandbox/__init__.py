"""The execution seam: where a command or a file operation actually happens.

Phase one runs everything in the server process, which is why the security posture is
"narrow what the agent may do" rather than "contain what it can reach" (see
``docs/plans/2026-09-14-team-saas-design.md`` §6 and §12).  This module is the seam that
would let phase two put that boundary somewhere real without touching the agent loop or
the tools' behaviour: the tools ask a backend, and a backend is either this process or a
container somewhere else.

Why a protocol and not just the container:

* **The contract is the deliverable.**  Writing down exactly which operations a remote
  backend must provide — exec, read, write, list, search — is what makes the second
  implementation a wiring job instead of a redesign.  ``ProvisionerBackend`` exists to make
  that failure loud and specific rather than an ``AttributeError`` at runtime.
* **``LocalBackend`` is live code, not a placeholder.**  It implements the protocol by
  delegating to the same helpers the built-in tools already use, so the interface is
  proven to be satisfiable by real behaviour rather than being an aspiration that the first
  implementation discovers is wrong.

Honest status: the tools still run in this process.  ``bash`` calls ``run_command``
directly and the file tools call ``os`` directly, because migrating them one at a time
would leave two execution paths in the most-tested code in the repository.  The seam is
therefore a recorded contract plus a working local implementation, and phase two's job is
to switch the tools onto it in one go.

What *is* enforced today is the operator-facing end: :func:`select_backend` and
:func:`assert_backend_usable` run at startup, so asking for a sandbox that does not exist
yet fails immediately instead of silently running everything in-process — which is the one
outcome a deployment must never get by accident.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

#: The rule phase two fulfils, written down where the next tool author will see it.
BACKEND_RULE = (
    "New tool code must reach the outside world through a SandboxBackend, never by calling "
    "os/subprocess directly. The built-in file tools predate this seam and are migrated in "
    "phase two; see docs/plans/2026-09-14-team-saas-design.md §12."
)


@dataclass
class ExecResult:
    """What a shell command produced.

    Deliberately plain data: a remote backend has to serialise this, so it cannot carry
    live process handles or the caller's own objects.
    """

    output: str
    exit_code: int | None = None
    timed_out: bool = False
    aborted: bool = False
    truncated: bool = False
    full_output_path: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SandboxBackend(Protocol):
    """Everything a tool needs from the place it runs.

    The method set mirrors the built-in tool surface one-to-one on purpose: a backend that
    satisfies this protocol can serve every tool without the tools knowing where they are.
    """

    name: str

    async def exec(
        self,
        command: str,
        *,
        cwd: str,
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_chunk: Callable[[bytes], None] | None = None,
    ) -> ExecResult:
        """Run ``command`` in a shell and collect its output."""
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


class LocalBackend:
    """Runs in this process.  The phase-one behaviour, behind the phase-two interface."""

    name = "local"

    def __init__(self, *, scrub_secrets: bool = False) -> None:
        self.scrub_secrets = scrub_secrets

    async def exec(
        self,
        command: str,
        *,
        cwd: str,
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_chunk: Callable[[bytes], None] | None = None,
    ) -> ExecResult:
        # Imported here rather than at module scope: `tools.bash` imports this module for
        # its type, and a top-level import back would be a cycle.
        from ..tools.bash import CommandAborted, CommandTimedOut, run_command

        resolved = env if env is not None else {}
        try:
            result = await run_command(command, cwd, resolved, timeout_seconds, None, on_chunk)
        except CommandTimedOut as timeout:
            return ExecResult(output=timeout.result.text, exit_code=None, timed_out=True)
        except CommandAborted as abort:
            return ExecResult(output=abort.result.text, exit_code=None, aborted=True)
        return ExecResult(output=result.text, exit_code=result.exit_code)

    def read_text(self, path: str, *, limit: int | None = None) -> str:
        with open(path, "rb") as handle:
            raw = handle.read(limit) if limit is not None else handle.read()
        return raw.decode("utf-8", errors="replace")

    def write_bytes(self, path: str, data: bytes) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)

    def list_dir(self, path: str) -> list[str]:
        return sorted(os.listdir(path))

    def exists(self, path: str) -> bool:
        return os.path.exists(path)


class ProvisionerBackend:
    """Phase two: exec and file access inside a container.

    Not implemented, and it says so rather than half-working.  The design it follows is
    written up in the design document §12 — one sandbox per conversation, keyed by
    ``uid`` and thread, with the deployment's data root mounted read-only and the user's
    workspace mounted read-write.
    """

    name = "provisioner"

    def __init__(self, url: str | None = None) -> None:
        self.url = url

    def _refuse(self) -> None:
        raise NotImplementedError(
            "PI_SANDBOX_BACKEND=provisioner is phase two and not implemented: the tools "
            "still run in this process. See docs/plans/2026-09-14-team-saas-design.md §12."
        )

    async def exec(self, *_args: Any, **_kwargs: Any) -> ExecResult:
        self._refuse()

    def read_text(self, *_args: Any, **_kwargs: Any) -> str:
        self._refuse()

    def write_bytes(self, *_args: Any, **_kwargs: Any) -> None:
        self._refuse()

    def list_dir(self, *_args: Any, **_kwargs: Any) -> list[str]:
        self._refuse()

    def exists(self, *_args: Any, **_kwargs: Any) -> bool:
        self._refuse()


def assert_backend_usable(backend: SandboxBackend) -> None:
    """Fail fast when a deployment asked for isolation that does not exist yet.

    Called at startup on purpose.  An operator who sets ``PI_SANDBOX_BACKEND=provisioner``
    believes commands are contained; discovering otherwise at the first tool call — or
    never — is the failure mode this function exists to remove.
    """
    if isinstance(backend, ProvisionerBackend):
        backend._refuse()


def select_backend(name: str | None = None, *, scrub_secrets: bool = False) -> SandboxBackend:
    """Resolve ``PI_SANDBOX_BACKEND`` into a backend instance.

    Unknown names raise instead of falling back to local: silently running in-process when
    an operator asked for isolation is exactly the failure this seam exists to prevent.
    """
    selected = (name or os.environ.get("PI_SANDBOX_BACKEND") or "local").strip().lower()
    if selected in ("", "local", "process"):
        return LocalBackend(scrub_secrets=scrub_secrets)
    if selected in ("provisioner", "docker", "k8s"):
        return ProvisionerBackend(os.environ.get("PI_SANDBOX_PROVISIONER_URL"))
    raise ValueError(
        f"unknown PI_SANDBOX_BACKEND {selected!r}; expected 'local' or 'provisioner'"
    )


__all__ = [
    "BACKEND_RULE",
    "ExecResult",
    "LocalBackend",
    "ProvisionerBackend",
    "SandboxBackend",
    "assert_backend_usable",
    "select_backend",
]

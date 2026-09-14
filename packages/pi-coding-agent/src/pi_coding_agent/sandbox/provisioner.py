"""Asks the provisioner service for a container, then runs commands inside it.

This is the phase-two backend: the agent's shell no longer runs in the server process, so
``rm -rf /`` costs a container and nothing else, and the container can only see the one
workspace it was given.

Two design points worth stating because they are where sandboxes usually go wrong:

**The container is per conversation, not per request.**  Starting a container per command
would add seconds to every ``ls``; keeping one per user forever would leak a process per
abandoned conversation.  Keying on ``(uid, thread)`` and letting the provisioner reap idle
containers gives the same container back for a conversation and nothing left over for one
that ended.

**The workspace is the only host path that appears.**  It is bind-mounted at
:data:`~.types.CONTAINER_WORKSPACE`, so the container cannot see other users' workspaces, the
session transcripts, the database, or the server's own installation — the paths the policy
extension has to *ask* about are, here, not reachable at all.

File operations still run in this process.  That is deliberate and honest: they are already
confined to the workspace by the same bind mount's host side, and routing every file read
through HTTP would slow the hot path for no additional containment.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from typing import Any, Callable

import httpx

from .types import ExecResult, SandboxScope

#: Substrings that mean "the sandbox no longer exists" in a provisioner/docker error.
_GONE_MARKERS = ("no such container", "404", "not found")

#: How long to wait on the provisioner itself (not the command inside the container).
DEFAULT_REQUEST_TIMEOUT = 30.0

#: The file the mount probe writes and reads back. Hidden and constant so the same name works
#: for the write (host side) and the read (container side).
_PROBE_NAME = ".pi-sandbox-probe"


class ProvisionerError(RuntimeError):
    """The provisioner refused, is unreachable, or is misconfigured."""


def _says_the_container_is_gone(error: Exception) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in _GONE_MARKERS)


class ProvisionerBackend:
    """Exec inside a container managed by the provisioner service."""

    name = "provisioner"

    def __init__(
        self,
        scope: SandboxScope,
        *,
        url: str | None = None,
        token: str | None = None,
        image: str | None = None,
        network: str | None = None,
        run_as: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.scope = scope
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.image = image
        self.network = network
        self.run_as = run_as
        self._client = client
        #: Set once the container is known to exist, so the common path is one request.
        self._ensured = False

    # -- plumbing ----------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self.url:
            raise ProvisionerError("PI_SANDBOX_PROVISIONER_URL is not set")
        if self._client is not None:
            response = await self._client.request(
                method, path, headers=self._headers(), **kwargs
            )
        else:
            async with httpx.AsyncClient(
                base_url=self.url, timeout=DEFAULT_REQUEST_TIMEOUT
            ) as client:
                response = await client.request(method, path, headers=self._headers(), **kwargs)
        if response.status_code == 401:
            raise ProvisionerError("the provisioner rejected the token (check PI_SANDBOX_PROVISIONER_TOKEN)")
        if response.status_code >= 400:
            raise ProvisionerError(
                f"provisioner returned HTTP {response.status_code}: {response.text[:300]}"
            )
        return response

    # -- lifecycle ---------------------------------------------------------------------

    async def ensure(self) -> dict[str, Any]:
        """Create the conversation's container if it does not exist yet.

        Creation is followed by :meth:`_verify_mount`, because the one mistake that matters
        here is silent: the daemon resolves bind mounts on the host, so a wrong host path
        produces a container with an *empty* workspace and every command still "succeeds".
        """
        if self._ensured:
            return {"sandboxId": self.scope.sandbox_id(), "status": "running"}
        workspace = self.scope.host_workspace or self.scope.workspace
        payload: dict[str, Any] = {
            "workspace": workspace,
            # Mount it at the path *this process* uses, not at a fixed /workspace. The agent is
            # told its working directory is the app-side path, and the file tools operate on
            # that path; if the container disagreed, `ls <cwd>` would fail while `read <cwd>`
            # succeeded — one path, two universes, and a model left guessing which is real.
            "containerPath": self.scope.workspace,
            "uid": self.scope.uid,
            "thread": self.scope.thread,
        }
        for key, value in (
            ("image", self.image),
            ("network", self.network),
            ("runAs", self.run_as),
        ):
            if value:
                payload[key] = value
        response = await self._request(
            "POST", f"/sandboxes/{self.scope.sandbox_id()}", json=payload
        )
        self._ensured = True
        await self._verify_mount()
        return response.json()

    async def _verify_mount(self) -> None:
        """Prove the container sees the same directory this process does.

        A marker file is written here (to the workspace as *this* process sees it), read back
        from inside the container, and deleted.  If the two views disagree — the classic
        symptom of a ``PI_SANDBOX_HOST_DATA_ROOT`` that does not match the volume the server
        has mounted — the values differ and the sandbox is discarded rather than used.

        With the workspace mounted at this process's own path, the probe also pins down path
        parity: the container must see the file at the very same string the agent is told is
        its working directory.
        """
        marker = secrets.token_hex(8)
        probe = os.path.join(self.scope.workspace, ".pi-sandbox-probe")
        try:
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write(marker)
        except OSError as error:
            raise ProvisionerError(
                f"cannot write a probe into the workspace {self.scope.workspace!r}: {error}"
            ) from error

        try:
            result = await self._exec_raw(f"cat {_PROBE_NAME}")
            if result.output.strip() != marker:
                await self.release()
                raise ProvisionerError(
                    "the sandbox is not mounting this workspace.\n"
                    f"  The server wrote the probe to {self.scope.workspace!r} and asked the "
                    f"container for it at the same path, and got "
                    f"{result.output.strip()[:80]!r} back.\n"
                    "  The usual cause is PI_SANDBOX_HOST_DATA_ROOT: the daemon resolves bind "
                    "mounts on the *host*, so it must be the host path of the directory the "
                    "server has mounted (see docker-compose.yml)."
                )
        finally:
            try:
                os.unlink(probe)
            except OSError:
                pass

    async def _exec_raw(self, command: str) -> ExecResult:
        """One exec, without the ensure()/verify recursion."""
        response = await self._request(
            "POST",
            f"/sandboxes/{self.scope.sandbox_id()}/exec",
            json={"command": command, "cwd": self.scope.workspace},
        )
        body = response.json()
        output = str(body.get("output") or "")
        return ExecResult(output=output, exit_code=body.get("exitCode"))

    async def release(self) -> None:
        """Drop the container. Called when a session goes away."""
        if not self.url:
            return
        try:
            await self._request("DELETE", f"/sandboxes/{self.scope.sandbox_id()}")
        except ProvisionerError:
            # A container that is already gone, or a provisioner that is down, is not worth
            # failing a user's request over; the provisioner's own reaper is the backstop.
            pass
        finally:
            self._ensured = False

    # -- execution ---------------------------------------------------------------------

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
        """Run ``command`` inside the conversation's container.

        ``cwd`` is translated into the container's view: the workspace maps to
        :data:`CONTAINER_WORKSPACE`, and a path outside it is refused here rather than
        silently run somewhere unexpected.  ``on_chunk`` is accepted and unused — the
        provisioner answers when the command finishes, so there is nothing to stream yet.

        Aborting cancels the *request*; the command itself may keep running until the
        container's own timeout reaps it.  That is stated rather than hidden: reporting
        "aborted" while a process still runs would be the more comfortable lie.
        """
        del on_chunk
        if signal is not None and getattr(signal, "aborted", False):
            return ExecResult(output="", exit_code=None, aborted=True)

        await self.ensure()

        payload: dict[str, Any] = {
            "command": command,
            "cwd": self._container_cwd(cwd),
        }
        if timeout_seconds is not None:
            payload["timeoutSeconds"] = float(timeout_seconds)
        if metadata:
            # Only the PI_* session variables travel; nothing from the host environment.
            payload["env"] = {str(k): str(v) for k, v in metadata.items()}

        try:
            return await self._exec_once(payload, signal)
        except ProvisionerError as error:
            if not _says_the_container_is_gone(error):
                raise
            # The container outlived its use — reaped while idle, removed by hand, or lost
            # with the daemon. Caching "it exists" would turn that into a permanently broken
            # session, so the cache is dropped and one replacement attempt is made.
            self._ensured = False
            await self.ensure()
            return await self._exec_once(payload, signal)

    async def _exec_once(self, payload: dict[str, Any], signal: Any) -> ExecResult:
        request = asyncio.ensure_future(
            self._request("POST", f"/sandboxes/{self.scope.sandbox_id()}/exec", json=payload)
        )
        state = {"aborted": False}
        dispose = None
        if signal is not None and hasattr(signal, "on_abort"):
            def _on_abort() -> None:
                state["aborted"] = True
                request.cancel()

            dispose = signal.on_abort(_on_abort)
        try:
            response = await request
        except asyncio.CancelledError:
            if state["aborted"]:
                return ExecResult(output="", exit_code=None, aborted=True)
            raise
        finally:
            if dispose is not None:
                dispose()

        body = response.json()
        output = str(body.get("output") or "")
        return ExecResult(
            output=output,
            raw=output.encode("utf-8", errors="replace"),
            exit_code=body.get("exitCode"),
            timed_out=bool(body.get("timedOut")),
            meta={"sandboxId": self.scope.sandbox_id()},
        )

    def _container_cwd(self, cwd: str) -> str:
        """The working directory inside the container.

        An identity mapping, because the workspace is mounted at this process's own path: what
        the agent is told, what the file tools use, and what the shell sees are the same
        string. The containment check stays — a command must not be run outside it.
        """
        workspace = self.scope.workspace
        try:
            resolved = os.path.realpath(cwd)
            root = os.path.realpath(workspace)
        except OSError:  # pragma: no cover - realpath does not raise for plain strings
            resolved, root = cwd, workspace
        if resolved == root or resolved.startswith(root + os.sep):
            return cwd
        raise ProvisionerError(
            f"refusing to run outside the workspace: {cwd!r} is not inside {workspace!r}"
        )

    # -- file access -------------------------------------------------------------------
    #
    # These deliberately act on this process's view of the workspace.  The container has the
    # same directory bind-mounted, so reads and writes are consistent without a round trip,
    # and the policy extension already refuses paths outside the workspace.

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


__all__ = ["DEFAULT_REQUEST_TIMEOUT", "ProvisionerBackend", "ProvisionerError"]

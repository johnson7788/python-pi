"""The sandbox provisioner: hands out one container per conversation.

Why this is a separate service rather than code in the server
------------------------------------------------------------

Creating containers needs the Docker socket, and the Docker socket is root on the host.  The
process that runs the agent's code is the one process that must **not** hold it: a sandbox
whose supervisor can be talked into running arbitrary containers is not a sandbox.  So the
only thing on the other side of this HTTP boundary is "create a container with this one
directory mounted, and run this string in it" — and the agent never reaches it.

What the container gets, and why
--------------------------------

* exactly **one** bind mount: the conversation's workspace, at :data:`CONTAINER_WORKSPACE`.
  Not the data root, not the other users' workspaces, not the transcripts or the database.
* no docker socket, no host network, ``cap_drop=ALL``, ``no-new-privileges``, a pid and
  memory cap, and a tmpfs ``/tmp``.
* a **writable, ephemeral** root filesystem.  That is deliberate: an agent that wants to
  ``pip install`` something to try it should be able to, and the result must vanish with the
  container rather than accumulate in an image.  The bind mount is the only thing that
  survives.
* the uid/gid of the server process, so the files it writes into the shared workspace stay
  owned by the server that has to manage them.

Idle containers are reaped.  A server that is killed cannot be trusted to have cleaned up,
so the reaper is the backstop, not the primary mechanism.
"""

from __future__ import annotations

import os
import re
import shlex
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

#: Where the workspace is mounted inside a sandbox container.
CONTAINER_WORKSPACE = "/workspace"

#: Container-name prefix, so `docker ps` groups them and cleanup can be selective.
CONTAINER_PREFIX = "pi-sandbox"

_TOKEN = os.environ.get("SANDBOX_PROVISIONER_TOKEN", "")
_IMAGE = os.environ.get("SANDBOX_IMAGE", "python:3.12-slim")
_NETWORK = os.environ.get("SANDBOX_NETWORK") or "bridge"
_IDLE_TIMEOUT = float(os.environ.get("SANDBOX_IDLE_TIMEOUT_SECONDS", "900"))
_EXEC_TIMEOUT = float(os.environ.get("SANDBOX_EXEC_TIMEOUT_SECONDS", "180"))
_MEMORY_LIMIT = os.environ.get("SANDBOX_MEMORY_LIMIT", "2g")
_PIDS_LIMIT = int(os.environ.get("SANDBOX_PIDS_LIMIT", "512"))
_CPU_LIMIT = float(os.environ.get("SANDBOX_CPUS", "2"))
_HEALTH_TIMEOUT = float(os.environ.get("SANDBOX_HEALTH_TIMEOUT_SECONDS", "60"))

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ProvisionerError(RuntimeError):
    """Local failure: docker unavailable, or the daemon refused."""


@dataclass
class Sandbox:
    """One container, as this process tracks it."""

    sandbox_id: str
    uid: str
    thread: str
    workspace: str
    container_id: str
    created_at: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_used = time.monotonic()


class Registry:
    """Creates, finds and forgets sandboxes.  In memory: containers are the durable state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sandboxes: dict[str, Sandbox] = {}
        self._client: Any = None
        self._docker_error: str | None = None

    # -- docker ------------------------------------------------------------------------

    def client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import docker
        except ImportError as error:  # pragma: no cover - the image installs it
            raise ProvisionerError("the docker SDK is not installed") from error
        try:
            self._client = docker.from_env()
            self._client.ping()
        except Exception as error:  # noqa: BLE001 - any daemon problem is the same to us
            raise ProvisionerError(f"cannot talk to the docker daemon: {error}") from error
        return self._client

    def docker_available(self) -> tuple[bool, str | None]:
        try:
            self.client()
        except ProvisionerError as error:
            return False, str(error)
        return True, None

    # -- naming ------------------------------------------------------------------------

    @staticmethod
    def container_name(sandbox_id: str) -> str:
        return f"{CONTAINER_PREFIX}-{sandbox_id}"

    # -- lifecycle ---------------------------------------------------------------------

    def ensure(self, sandbox_id: str, uid: str, thread: str, workspace: str, **options: Any) -> Sandbox:
        """Return the conversation's container, creating it if the daemon has none.

        Note what is **not** validated here: whether ``workspace`` exists.  This service runs
        in its own container and only talks to the daemon — it has no view of the host
        filesystem, so it cannot tell "that path is not mounted here" from "that path does not
        exist".  A check that rejected both would refuse every legitimate deployment.  The
        client verifies the mount after creation instead (``ProvisionerBackend._verify_mount``),
        which catches the mistake that actually matters: a host path that resolves to a
        different, empty directory.
        """
        _validate_id(sandbox_id, "sandbox id")

        with self._lock:
            existing = self._sandboxes.get(sandbox_id)
            if existing is not None and self._is_running(existing):
                existing.touch()
                return existing

            client = self.client()
            name = self.container_name(sandbox_id)
            container = self._find(client, name)
            if container is None:
                container = self._create(client, name, sandbox_id, uid, workspace, **options)
            sandbox = Sandbox(
                sandbox_id=sandbox_id,
                uid=uid,
                thread=thread,
                workspace=workspace,
                container_id=container.id,
            )
            self._sandboxes[sandbox_id] = sandbox
            return sandbox

    def _find(self, client: Any, name: str) -> Any:
        try:
            from docker.errors import NotFound
        except ImportError:  # pragma: no cover
            NotFound = Exception  # type: ignore[assignment,misc]
        try:
            container = client.containers.get(name)
        except NotFound:
            return None
        if container.status != "running":
            container.start()
        return container

    def _is_running(self, sandbox: Sandbox) -> bool:
        try:
            container = self.client().containers.get(sandbox.container_id)
            container.reload()
            return container.status == "running"
        except Exception:  # noqa: BLE001 - gone means recreate
            return False

    def _create(
        self, client: Any, name: str, sandbox_id: str, uid: str, workspace: str, **options: Any
    ) -> Any:
        image = options.get("image") or _IMAGE
        run_as = options.get("runAs") or ""
        kwargs: dict[str, Any] = {
            "image": image,
            "name": name,
            "command": ["sleep", "infinity"],
            "detach": True,
            "working_dir": CONTAINER_WORKSPACE,
            "volumes": {workspace: {"bind": CONTAINER_WORKSPACE, "mode": "rw"}},
            "tmpfs": {"/tmp": "rw,exec,mode=1777"},
            "labels": {
                "app": CONTAINER_PREFIX,
                "sandbox-id": sandbox_id,
                "uid": uid,
            },
            "network": options.get("network") or _NETWORK,
            "mem_limit": _MEMORY_LIMIT,
            "nano_cpus": int(_CPU_LIMIT * 1_000_000_000),
            "pids_limit": _PIDS_LIMIT,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "environment": {
                # A container-shaped environment. The host's is never forwarded.
                "HOME": CONTAINER_WORKSPACE,
                "PI_CWD": CONTAINER_WORKSPACE,
                "TERM": "dumb",
                "LANG": "C.UTF-8",
                "DEBIAN_FRONTEND": "noninteractive",
            },
        }
        if run_as:
            kwargs["user"] = run_as
        try:
            container = client.containers.run(**kwargs)
        except Exception as error:  # noqa: BLE001 - surface the daemon's own message
            raise ProvisionerError(f"docker refused to create the sandbox: {error}") from error
        self._require_timeout(container, image)
        return container

    @staticmethod
    def _require_timeout(container: Any, image: str) -> None:
        """Fail at creation if the image cannot bound a command's runtime.

        Every exec is wrapped in ``timeout`` so a runaway command is killed rather than
        holding a worker open forever.  An image without coreutils would silently lose that
        bound, so the check is here — once per container, with a message that names the fix —
        instead of being discovered by whoever runs ``yes`` in a loop.
        """
        try:
            exit_code, _ = container.exec_run(["sh", "-lc", "command -v timeout"])
        except Exception:  # noqa: BLE001 - a container that cannot exec is broken anyway
            exit_code = 1
        if exit_code != 0:
            try:
                container.remove(force=True)
            except Exception:  # noqa: BLE001
                pass
            raise ProvisionerError(
                f"the sandbox image {image!r} has no `timeout`; install coreutils "
                "(the provided deploy/sandbox_image/Dockerfile does this already)"
            )

    def exec(self, sandbox_id: str, command: str, cwd: str, timeout_seconds: float | None, env: dict[str, str] | None) -> dict[str, Any]:
        with self._lock:
            sandbox = self._sandboxes.get(sandbox_id)
        if sandbox is None:
            raise ProvisionerError(f"unknown sandbox: {sandbox_id}")
        sandbox.touch()

        if not cwd.startswith(CONTAINER_WORKSPACE):
            # The backend refuses this too; refusing here as well means the provisioner is
            # safe to call directly, not only through a client that behaves.
            raise ProvisionerError(f"cwd must be inside {CONTAINER_WORKSPACE}: {cwd!r}")

        limit = float(timeout_seconds or _EXEC_TIMEOUT)
        wrapped = f"timeout -s KILL {int(limit)} sh -lc {shlex.quote(command)}"
        try:
            container = self.client().containers.get(sandbox.container_id)
            # `timeout` inside the container gives us both a bound and the partial output,
            # which the daemon API alone cannot: exec has no cancellation primitive.
            exit_code, output = container.exec_run(
                ["sh", "-lc", wrapped],
                workdir=cwd,
                environment=dict(env or {}),
                demux=False,
            )
        except Exception as error:  # noqa: BLE001
            raise ProvisionerError(f"exec failed: {error}") from error

        text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output or "")
        # 124 is `timeout`'s own exit code; 137 is SIGKILL, which is how it enforces it.
        timed_out = exit_code in (124, 137)
        return {
            "output": text,
            "exitCode": None if timed_out else exit_code,
            "timedOut": timed_out,
        }

    def delete(self, sandbox_id: str) -> bool:
        with self._lock:
            sandbox = self._sandboxes.pop(sandbox_id, None)
        name = self.container_name(sandbox_id)
        try:
            container = self.client().containers.get(name)
            container.remove(force=True)
        except Exception:  # noqa: BLE001 - already gone is a success
            return sandbox is not None
        return True

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "sandboxId": sandbox.sandbox_id,
                    "uid": sandbox.uid,
                    "thread": sandbox.thread,
                    "idleSeconds": round(time.monotonic() - sandbox.last_used, 1),
                }
                for sandbox in self._sandboxes.values()
            ]

    def reap_idle(self, timeout: float) -> list[str]:
        if timeout <= 0:
            return []
        now = time.monotonic()
        with self._lock:
            stale = [s.sandbox_id for s in self._sandboxes.values() if now - s.last_used > timeout]
        removed = []
        for sandbox_id in stale:
            self.delete(sandbox_id)
            removed.append(sandbox_id)
        return removed


REGISTRY = Registry()


# --------------------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------------------


class CreateSandbox(BaseModel):
    workspace: str = Field(..., description="Host path of the conversation's workspace")
    uid: str = ""
    thread: str = ""
    image: str | None = None
    network: str | None = None
    runAs: str | None = None


class ExecRequest(BaseModel):
    command: str
    cwd: str = CONTAINER_WORKSPACE
    timeoutSeconds: float | None = None
    env: dict[str, str] | None = None


def _validate_id(value: str, label: str) -> str:
    if not _ID_PATTERN.match(value or ""):
        raise HTTPException(status_code=422, detail=f"invalid {label}")
    return value


def require_token(authorization: str | None = Header(default=None)) -> None:
    """Bearer auth.  An unset token is a misconfiguration, not an open door."""
    if not _TOKEN:
        raise HTTPException(
            status_code=503, detail="SANDBOX_PROVISIONER_TOKEN is not set; refusing every request"
        )
    if authorization != f"Bearer {_TOKEN}":
        raise HTTPException(status_code=401, detail="invalid provisioning token")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    import asyncio

    async def _reaper() -> None:
        while True:
            await asyncio.sleep(max(30.0, _IDLE_TIMEOUT / 4))
            try:
                REGISTRY.reap_idle(_IDLE_TIMEOUT)
            except Exception:  # noqa: BLE001 - a failed sweep must not kill the task
                pass

    task = asyncio.create_task(_reaper())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="pi sandbox provisioner", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    available, detail = REGISTRY.docker_available()
    return {
        "status": "ok" if available else "degraded",
        "docker": available,
        "detail": detail,
        "image": _IMAGE,
        "sandboxes": len(REGISTRY.list()),
        "idleTimeoutSeconds": _IDLE_TIMEOUT,
    }


@app.get("/sandboxes", dependencies=[Depends(require_token)])
async def list_sandboxes() -> dict[str, Any]:
    return {"sandboxes": REGISTRY.list()}


@app.post("/sandboxes/{sandbox_id}", dependencies=[Depends(require_token)])
async def create_sandbox(sandbox_id: str, payload: CreateSandbox, request: Request) -> dict[str, Any]:
    _validate_id(sandbox_id, "sandbox id")
    try:
        sandbox = REGISTRY.ensure(
            sandbox_id,
            payload.uid,
            payload.thread,
            payload.workspace,
            image=payload.image,
            network=payload.network,
            runAs=payload.runAs or request.headers.get("x-sandbox-run-as"),
        )
    except ProvisionerError as error:
        # 502: the request was fine, the daemon was not.
        raise HTTPException(status_code=502, detail=str(error)) from None
    return {
        "sandboxId": sandbox.sandbox_id,
        "status": "running",
        "workspace": CONTAINER_WORKSPACE,
    }


@app.post("/sandboxes/{sandbox_id}/exec", dependencies=[Depends(require_token)])
async def exec_command(sandbox_id: str, payload: ExecRequest) -> dict[str, Any]:
    _validate_id(sandbox_id, "sandbox id")
    if not payload.command.strip():
        raise HTTPException(status_code=422, detail="command must not be empty")
    try:
        return REGISTRY.exec(
            sandbox_id, payload.command, payload.cwd, payload.timeoutSeconds, payload.env
        )
    except ProvisionerError as error:
        raise HTTPException(status_code=502, detail=str(error)) from None


@app.delete("/sandboxes/{sandbox_id}", dependencies=[Depends(require_token)])
async def delete_sandbox(sandbox_id: str) -> dict[str, Any]:
    _validate_id(sandbox_id, "sandbox id")
    return {"ok": REGISTRY.delete(sandbox_id)}

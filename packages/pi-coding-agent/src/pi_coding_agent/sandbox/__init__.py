"""Where a command actually happens: this process, or a container per conversation.

Phase one ran everything in the server process, which is why the security posture was
"narrow what the agent may do" rather than "contain what it can reach".  This package adds
the second option.

``LocalBackend``
    Runs in this process.  Still the default: a personal install must not need Docker, and a
    sandbox that is only reachable with Docker would make the common case the untested one.

``ProvisionerBackend``
    Asks a provisioner service (``deploy/sandbox_provisioner``) for a container and runs
    commands inside it.  The container gets one workspace bind-mounted and nothing else: no
    host filesystem, no other user's workspace, no database, no docker socket, dropped
    capabilities.

The contract lives in :mod:`.types`; the two implementations are in :mod:`.local` and
:mod:`.provisioner`.  This module only resolves configuration and checks it.
"""

from __future__ import annotations

import os
from typing import Any

from .local import LocalBackend
from .provisioner import ProvisionerBackend, ProvisionerError
from .types import (
    CONTAINER_WORKSPACE,
    ExecResult,
    SandboxBackend,
    SandboxScope,
    host_path_for,
)

#: The rule every new tool has to follow.
BACKEND_RULE = (
    "New tool code must reach the outside world through a SandboxBackend, never by calling "
    "os/subprocess directly. `bash` is migrated; the file tools are next."
)


def select_backend(
    name: str | None = None,
    *,
    scope: SandboxScope | None = None,
    scrub_secrets: bool = False,
    url: str | None = None,
    token: str | None = None,
    image: str | None = None,
    network: str | None = None,
    run_as: str | None = None,
    client: Any = None,
) -> SandboxBackend:
    """Resolve ``PI_SANDBOX_BACKEND`` into a backend instance.

    Unknown names raise instead of falling back to local: silently running in-process when an
    operator asked for isolation is exactly the failure this seam exists to prevent.
    """
    selected = (name or os.environ.get("PI_SANDBOX_BACKEND") or "local").strip().lower()
    if selected in ("", "local", "process"):
        return LocalBackend(scrub_secrets=scrub_secrets)
    if selected in ("provisioner", "docker", "k8s"):
        if scope is None:
            raise ValueError(
                "the provisioner backend needs a SandboxScope (uid + thread + workspace): "
                "a container belongs to a conversation, not to the process"
            )
        return ProvisionerBackend(
            scope,
            url=url or os.environ.get("PI_SANDBOX_PROVISIONER_URL"),
            token=token or os.environ.get("PI_SANDBOX_PROVISIONER_TOKEN"),
            image=image or os.environ.get("PI_SANDBOX_IMAGE"),
            network=network or os.environ.get("PI_SANDBOX_NETWORK"),
            run_as=run_as or os.environ.get("PI_SANDBOX_RUN_AS"),
            client=client,
        )
    raise ValueError(
        f"unknown PI_SANDBOX_BACKEND {selected!r}; expected 'local' or 'provisioner'"
    )


def assert_backend_usable(backend: SandboxBackend) -> None:
    """Fail fast on a misconfigured backend, at startup rather than at the first command.

    An operator who sets ``PI_SANDBOX_BACKEND=provisioner`` believes commands are contained.
    Discovering a missing URL or token when a user is already waiting on an answer — or never
    — is the failure mode this exists to remove.
    """
    if isinstance(backend, ProvisionerBackend):
        missing = [
            label
            for label, value in (
                ("PI_SANDBOX_PROVISIONER_URL", backend.url),
                ("PI_SANDBOX_PROVISIONER_TOKEN", backend.token),
            )
            if not value
        ]
        if missing:
            raise ProvisionerError(
                "PI_SANDBOX_BACKEND=provisioner needs " + " and ".join(missing) + ". "
                "See docs/DEPLOY.md."
            )


__all__ = [
    "BACKEND_RULE",
    "CONTAINER_WORKSPACE",
    "ExecResult",
    "LocalBackend",
    "ProvisionerBackend",
    "ProvisionerError",
    "SandboxBackend",
    "SandboxScope",
    "assert_backend_usable",
    "host_path_for",
    "select_backend",
]

"""Deployment-level configuration for the team (multi-tenant) server.

``BootstrapOptions`` (in ``pi_coding_agent``) describes *what one agent session needs*;
this module describes *the deployment*: where the database, the per-user workspaces and
the per-user agent directories live, whether requests must authenticate, and the
resource caps that keep one runaway session from taking the process down with it.

Two modes exist on purpose:

``single``
    The original personal tool: no accounts, one shared session, ``cwd`` and
    ``agent_dir`` supplied on the command line.  ``./start.sh`` keeps working
    unchanged while the team surface is built out.

``team``
    Multi-tenant: every request carries an authenticated user, and that user's
    ``cwd``/``agent_dir`` are derived from their ``uid`` instead of the command line.
    This is the mode ``start.sh`` is expected to select once the login page ships.

The paths here are the only place UIDs become filesystem paths, so the validation that
keeps a crafted id from escaping the data root lives here too.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

#: Default location for the database, per-user workspaces and per-user agent dirs.
DEFAULT_DATA_ROOT = "~/.pi/team"

#: Name of the browser session cookie.
COOKIE_NAME = "pi_session"

#: Roles, most privileged last.  ``superadmin`` may change roles and quotas;
#: ``admin`` may create, disable and inspect accounts.
ROLES = ("user", "admin", "superadmin")

_UID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def resolve_data_root(value: str | None) -> str:
    """Expand ``~`` and make ``value`` absolute, defaulting to :data:`DEFAULT_DATA_ROOT`."""
    raw = value or DEFAULT_DATA_ROOT
    return os.path.abspath(os.path.expanduser(raw))


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class ServerConfig:
    """Where the deployment keeps its state, and how strict it is."""

    data_root: str
    auth_mode: str = "single"
    cookie_secure: bool = False
    session_ttl_hours: int = 24 * 14
    admin_username: str = "admin"
    # Team mode never loads per-user extensions: a per-user ``extensions/`` directory
    # would be an arbitrary-code-execution entry point for every colleague (see
    # ``pi_coding_agent.extensions.types``, which runs extensions with the full
    # permissions of the process).  Kept as a flag so it is explicit, not accidental.
    allow_user_extensions: bool = False
    #: Caps that bound the blast radius of a single process holding every session.
    max_sessions_per_user: int = 3
    max_sessions_total: int = 20
    session_idle_minutes: int = 30

    # -- derived paths ---------------------------------------------------------------

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_root, "pi.db")

    @property
    def workspace_root(self) -> str:
        return os.path.join(self.data_root, "workspaces")

    @property
    def agent_root(self) -> str:
        return os.path.join(self.data_root, "agents")

    def workspace_dir(self, uid: str) -> str:
        """The agent's ``cwd`` for one user — this is the tenant boundary."""
        return os.path.join(self.workspace_root, _checked_uid(uid))

    def agent_dir(self, uid: str) -> str:
        """Per-user replacement for ``~/.pi/agent`` (sessions, settings, models)."""
        return os.path.join(self.agent_root, _checked_uid(uid))

    # -- behaviour -------------------------------------------------------------------

    @property
    def requires_auth(self) -> bool:
        return self.auth_mode == "team"

    def ensure_layout(self) -> None:
        """Create the data root, workspace root and agent root."""
        for path in (self.data_root, self.workspace_root, self.agent_root):
            os.makedirs(path, exist_ok=True)

    def ensure_user_layout(self, uid: str) -> tuple[str, str]:
        """Create (and return) one user's workspace and agent dirs."""
        workspace = self.workspace_dir(uid)
        agent = self.agent_dir(uid)
        os.makedirs(workspace, exist_ok=True)
        os.makedirs(agent, exist_ok=True)
        return workspace, agent

    # -- constructors ----------------------------------------------------------------

    @classmethod
    def from_env(cls, **overrides: object) -> "ServerConfig":
        """Build from ``PI_*`` environment variables, with explicit values winning."""
        values: dict[str, object] = {
            "data_root": resolve_data_root(os.environ.get("PI_DATA_ROOT")),
            "auth_mode": (os.environ.get("PI_AUTH_MODE") or "single").strip().lower(),
            "cookie_secure": _bool_env("PI_COOKIE_SECURE"),
            "admin_username": os.environ.get("PI_ADMIN_USERNAME") or "admin",
        }
        ttl = os.environ.get("PI_SESSION_TTL_HOURS")
        if ttl:
            values["session_ttl_hours"] = int(ttl)
        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]


def _checked_uid(uid: str) -> str:
    """Reject anything that is not a plain id, so a uid cannot escape the data root."""
    if not isinstance(uid, str) or not _UID_PATTERN.match(uid):
        raise ValueError(f"unsafe uid: {uid!r}")
    return uid


__all__ = [
    "COOKIE_NAME",
    "DEFAULT_DATA_ROOT",
    "ROLES",
    "ServerConfig",
    "resolve_data_root",
]

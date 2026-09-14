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
import shutil
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


def _secret(name: str) -> str | None:
    """Read a secret from ``NAME`` or from the file named by ``NAME_FILE``.

    The file form exists for ``systemd LoadCredential`` and Docker secrets, which mount
    the value at a path instead of putting it in the environment.  Reading the file
    directly also keeps the key out of ``argv``, where ``ps`` would show it to every
    other account on the machine.
    """
    direct = os.environ.get(name)
    if direct:
        return direct.strip()
    path = os.environ.get(f"{name}_FILE")
    if not path:
        return None
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


@dataclass(frozen=True)
class ServerConfig:
    """Where the deployment keeps its state, and how strict it is."""

    data_root: str
    auth_mode: str = "single"
    cookie_secure: bool = False
    session_ttl_hours: int = 24 * 14
    admin_username: str = "admin"
    #: The shared model credential.  Team mode injects this into every user's session, so
    #: a per-user ``auth.json`` is never needed (and never allowed to shadow it).
    api_key: str | None = None
    #: Model catalog and settings primed into a new user's agent dir.  Without this, a
    #: per-user agent dir would lose the provider overrides that live in
    #: ``~/.pi/agent/models.json`` — the agent would silently use the built-in catalog.
    template_agent_dir: str | None = None
    # Team mode never loads per-user extensions: a per-user ``extensions/`` directory
    # would be an arbitrary-code-execution entry point for every colleague (see
    # ``pi_coding_agent.extensions.types``, which runs extensions with the full
    # permissions of the process).  Kept as a flag so it is explicit, not accidental.
    allow_user_extensions: bool = False
    #: Caps that bound the blast radius of a single process holding every session.
    max_active_users: int = 20
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

    @property
    def template_path(self) -> str:
        return self.template_agent_dir or os.path.join(self.data_root, "template")

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
        """Create (and return) one user's workspace and agent dirs.

        A new agent dir is primed from :attr:`template_path` so provider overrides and
        settings are not silently lost just because the agent dir moved per user.  Only
        missing files are added, so re-running never overwrites a user's own edits.
        """
        workspace = self.workspace_dir(uid)
        agent = self.agent_dir(uid)
        os.makedirs(workspace, exist_ok=True)
        os.makedirs(agent, exist_ok=True)
        self._seed_agent_dir(agent)
        return workspace, agent

    def _seed_agent_dir(self, agent: str) -> None:
        template = self.template_path
        if not os.path.isdir(template):
            return
        for name in ("models.json", "settings.json", "AGENTS.md"):
            source = os.path.join(template, name)
            target = os.path.join(agent, name)
            if os.path.isfile(source) and not os.path.exists(target):
                shutil.copyfile(source, target)
        # `skills/` is linked rather than copied: it is typically large and identical for
        # everyone, and one source of truth means a skill added by an administrator shows
        # up for the whole team.  The trade-off is that a skill directory is shared, so it
        # is not a place for anything user-specific.
        skills_source = os.path.join(template, "skills")
        skills_target = os.path.join(agent, "skills")
        if os.path.isdir(skills_source) and not os.path.exists(skills_target):
            try:
                os.symlink(skills_source, skills_target, target_is_directory=True)
            except OSError:
                pass  # a filesystem without symlinks is not worth failing a login over

    # -- constructors ----------------------------------------------------------------

    @classmethod
    def from_env(cls, **overrides: object) -> "ServerConfig":
        """Build from ``PI_*`` environment variables, with explicit values winning."""
        values: dict[str, object] = {
            "data_root": resolve_data_root(os.environ.get("PI_DATA_ROOT")),
            "auth_mode": (os.environ.get("PI_AUTH_MODE") or "single").strip().lower(),
            "cookie_secure": _bool_env("PI_COOKIE_SECURE"),
            "admin_username": os.environ.get("PI_ADMIN_USERNAME") or "admin",
            "api_key": _secret("PI_API_KEY"),
            "template_agent_dir": os.environ.get("PI_TEMPLATE_AGENT_DIR") or None,
        }
        ttl = os.environ.get("PI_SESSION_TTL_HOURS")
        if ttl:
            values["session_ttl_hours"] = int(ttl)
        cap = os.environ.get("PI_MAX_ACTIVE_USERS")
        if cap:
            values["max_active_users"] = int(cap)
        idle = os.environ.get("PI_SESSION_IDLE_MINUTES")
        if idle:
            values["session_idle_minutes"] = int(idle)
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

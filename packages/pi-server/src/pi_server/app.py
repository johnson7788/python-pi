"""Browser UI server: the pi command protocol over a WebSocket.

Three decisions shape this module.

**The command surface is reused, not reimplemented.**  ``RpcMode`` already owns the
30 commands, event projection and extension dialogs; it just happens to default to
stdio.  Handing it a different ``emit`` and feeding it WebSocket frames turns the
same object into a web server, so the browser speaks exactly the protocol pi's RPC
mode speaks — a client written against one works against the other.

**One worker per user, not one session per process.**  ``SessionRegistry`` starts a
session for a user the first time they need one and releases it when they go idle, so
the single-user behaviour is preserved (a user's tabs all observe the *same* session)
while two users never share anything.  Team mode derives each user's ``cwd`` and
``agent_dir`` from their ``uid``; single mode keeps one worker holding the command-line
options unchanged.

**Events broadcast within a session; dialogs do not.**  All of a user's tabs see the
transcript stream, but a blocking ``confirm``/``select`` goes to the most recently
active tab *of that session* — showing the same modal in three tabs and waiting on
whichever answers first is worse than asking once, and showing it to a different user
would be a bug.  Ids are unique, so a stale reply from another tab is simply ignored.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pi_coding_agent.bootstrap import BootstrapOptions, resolve_project_trust
from pi_coding_agent.modes.jsonl import serialize_json_line
from pi_coding_agent.modes.rpc_mode import model_to_json
from pi_coding_agent.policy import DEFAULT_ALLOW_TOOLS, PolicyConfig
from pi_coding_agent.resource_loader import ResourceLoader
from pi_coding_agent.settings import SettingsManager
from pi_coding_agent.session_manager import get_default_session_dir

from . import apps as app_catalog
from . import market as market_catalog
from .constants import (
    BLOCKING_DIALOG_METHODS,
    LOCAL_UID,
)
from .errors import error, install_error_handlers
from .routers.accounts import register as register_account_routes
from .routers.apps import register as register_app_routes
from .routers.plugins import register as register_plugin_routes
from .routers.workspace import register as register_workspace_routes
from .routers.schedules import register as register_schedule_routes
from .routers.office import register as register_office_routes
from .schemas import (
    SkillToggleRequest,
    MarketRatingRequest,
    FeedbackRequest,
    AppUpsertRequest,
)
from .worker import (
    Connection,
    Hub,
    CapacityError,
    BootstrapError,
    SessionWorker,
    SessionRegistry,
)
from . import plugins as plugin_catalog
from . import schedules as schedule_catalog
from . import auth
from .config import COOKIE_NAME, ServerConfig
from .db import Database, DuplicateUsername, month_start
from .runlog import replay_floor
from .tracing import NullTracer, Tracer
from .workspace import WorkspaceError

__version__ = "0.1.0"



def query_int(value: str | None, default: int = 0) -> int:
    """A non-negative integer query parameter, or ``default`` when absent/invalid.

    Used for the WebSocket's ``?since=`` reconnect offset: a stale bookmark or a
    hand-edited URL must not be able to raise inside the handshake.
    """
    try:
        return max(0, int(value or default))
    except (TypeError, ValueError):
        return default














# --------------------------------------------------------------------------------------
# Static assets
# --------------------------------------------------------------------------------------


def resolve_static_dir() -> Path | None:
    """Where the built SPA lives.

    Checked in order so an installed wheel, a checked-out repo and an explicit
    override all work: ``PI_WEB_DIST`` → packaged ``static/`` → ``<repo>/web/dist``.
    """
    override = os.environ.get("PI_WEB_DIST")
    if override and Path(override).is_dir():
        return Path(override)

    packaged = Path(__file__).parent / "static"
    if (packaged / "index.html").is_file():
        return packaged

    # packages/pi-server/src/pi_server/app.py -> repo root is four levels up.
    repo_web = Path(__file__).resolve().parents[4] / "web" / "dist"
    if (repo_web / "index.html").is_file():
        return repo_web
    return None


# --------------------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------------------






































def create_app(
    options: BootstrapOptions,
    *,
    on_warning: Any = None,
    config: ServerConfig | None = None,
) -> FastAPI:
    server_config = config or ServerConfig.from_env()
    # The credential can arrive as `--api-key` (legacy, visible in `ps`) or through
    # `PI_API_KEY`/`PI_API_KEY_FILE`. Filling it in here is what lets the personal flow stop
    # putting a secret in argv: the environment is readable only by the process owner.
    if server_config.api_key and not options.api_key:
        options = dataclasses.replace(options, api_key=server_config.api_key)
    # A personal install gets its own workspace too.  The old default was the
    # process cwd, which exposed the python-pi checkout (and everything else)
    # through the file browser.  Keep the historical session directory so the
    # conversations created before this move remain visible.
    if not server_config.requires_auth and server_config.single_workspace:
        previous_cwd = options.cwd
        single_cwd = server_config.single_workspace_dir()
        os.makedirs(single_cwd, exist_ok=True)
        if options.session_dir is None:
            session_dir = get_default_session_dir(previous_cwd, options.agent_dir)
        else:
            session_dir = options.session_dir
        options = dataclasses.replace(options, cwd=single_cwd, session_dir=session_dir)
    # Accounts only exist in team mode, so single mode does not create a database (or a
    # data root) behind the user's back — the personal flow stays byte-for-byte what it was.
    database: Database | None = None
    if server_config.requires_auth:
        server_config.ensure_layout()
        database = Database(server_config.db_path)
        database.purge_expired_sessions()

    # Schedules and notifications are useful in the account-less install too.
    # Keep their small SQLite store separate so single mode still has no
    # accounts/login surface and team mode continues to use the main database.
    local_schedule_db: Database | None = None
    if database is None:
        os.makedirs(server_config.data_root, exist_ok=True)
        local_schedule_db = Database(
            os.path.join(server_config.data_root, "local-schedules.db")
        )
        if local_schedule_db.get_user(LOCAL_UID) is None:
            try:
                local_schedule_db.create_user(
                    uid=LOCAL_UID,
                    username="local",
                    password_hash="!",
                    display_name="本地用户",
                )
            except DuplicateUsername:
                pass
    schedule_db = database or local_schedule_db

    def _audit_sink(entry: dict[str, Any]) -> int | None:
        """Write one policy decision to the audit log; never let it break a turn."""
        if database is None:
            return None
        try:
            return database.record_audit(
                uid=str(entry.get("uid") or ""),
                session_id=str(entry.get("session_id") or ""),
                tool_name=str(entry.get("tool_name") or ""),
                tool_call_id=str(entry.get("tool_call_id") or ""),
                decision=str(entry.get("decision") or "unknown"),
                reason=str(entry.get("reason") or ""),
                input_preview=str(entry.get("input_preview") or ""),
            )
        except Exception:  # noqa: BLE001
            return None

    def _audit_finish(row_id: int, is_error: bool, duration_ms: int) -> None:
        if database is None:
            return
        try:
            database.finish_audit(row_id, is_error=is_error, duration_ms=duration_ms)
        except Exception:  # noqa: BLE001
            pass

    def _policy_for(worker: SessionWorker) -> PolicyConfig | None:
        """The governance policy for one worker's session.

        The workspace root is the worker's ``cwd``, which in team mode is derived from the
        uid — so the policy enforces exactly the same boundary the filesystem already does.
        """
        if database is None:
            return None
        return PolicyConfig(
            workspace_root=worker.options.cwd,
            uid=worker.uid,
            # Deny-by-default is about *new built-in* tools; an admin-installed plugin is a
            # deliberate opening, so its tools join the allow set.  Without this every
            # plugin tool is refused and the plugin looks silently broken.
            allow_tools=frozenset(DEFAULT_ALLOW_TOOLS | _registered_tool_names(worker)),
            # The skill catalog lives in the per-user agent dir (symlinked to the shared
            # template), which is outside every workspace.  The prompt advertises those
            # skills, so the read side has to reach them; writes stay workspace-only.
            read_roots=(os.path.join(worker.options.agent_dir or "", "skills"),),
            # Every user's workspace and transcript lives under here, so an agent that can
            # read it can read a colleague's conversations.
            protected_roots=(server_config.data_root,),
            auto_approve=server_config.auto_approve_tools,
            confirm_timeout=server_config.confirm_timeout_seconds,
            grant_after_confirm=server_config.grant_after_confirm,
            record=worker.record_tool_decision,
        )

    def _registered_tool_names(worker: SessionWorker) -> frozenset[str]:
        """Tool names the worker's extensions/plugins registered (best effort)."""
        runner = getattr(getattr(worker, "state", None), "extension_runner", None)
        if runner is None:
            return frozenset()
        try:
            snapshot = runner.status()
        except Exception:  # noqa: BLE001 - a broken runner must not break the session
            return frozenset()
        names: set[str] = set()
        for entry in snapshot.get("extensions") or []:
            names.update(str(name) for name in entry.get("tools") or [] if name)
        return frozenset(names)

    def _usage_sink(entry: dict[str, Any]) -> None:
        if database is None:
            return
        try:
            database.record_usage(**entry)
        except Exception:  # noqa: BLE001 - an accounting failure must not stop a turn
            pass

    #: Commands that make the model produce tokens, so all of them are quota-gated.
    _BILLABLE_COMMANDS = frozenset({"prompt", "steer", "follow_up"})

    def _quota_rejection(uid: str, text: str) -> dict[str, Any] | None:
        """Refuse a billable command *before* it is accepted, or return ``None``.

        Checking at preflight rather than after the model call is the difference between a
        user seeing "out of quota" immediately and a user watching a whole turn run before
        finding out it should not have.
        """
        if database is None:
            return None
        try:
            command = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(command, dict) or command.get("type") not in _BILLABLE_COMMANDS:
            return None
        row = database.get_user(uid)
        if row is None:
            return None
        token_quota = row["monthly_token_quota"]
        cost_quota = row["monthly_cost_quota"]
        if token_quota is None and cost_quota is None:
            return None

        used = database.usage_summary(uid, month_start())
        total_tokens = int(used.get("total_tokens") or 0)
        cost = float(used.get("cost") or 0.0)
        if token_quota is not None and total_tokens >= int(token_quota):
            reason = f"本月的 token 额度已用完（{total_tokens}/{int(token_quota)}），请联系管理员"
        elif cost_quota is not None and cost >= float(cost_quota):
            reason = f"本月的费用额度已用完（{cost:.4f}/{float(cost_quota)}），请联系管理员"
        else:
            return None
        return {
            "type": "response",
            "id": command.get("id"),
            "command": str(command.get("type")),
            "success": False,
            "error": reason,
        }

    def _plugin_paths_for(uid: str, app_id: str) -> tuple[str, ...]:
        """Plugins one worker should load: what the app declares + what the user enabled.

        The app half matters because a declared plugin is the deployment's deliberate
        choice: an app that needs a tool to do its job should not arrive half-working
        because nobody clicked "enable".  Installation is still an administrator action.
        """
        names: set[str] = set()
        found = app_catalog.find_app(app_id)
        if found is not None:
            names.update(found.plugins)
        if database is not None:
            names.update(database.enabled_plugins(uid, app_id))
        return plugin_catalog.entry_paths(server_config.plugin_root, sorted(names))

    # Traces are opt-in: a deployment that does not want another file per day should
    # not pay for it, and the span writer opens nothing until the first span.
    tracing_dir = (
        os.path.join(server_config.data_root, "traces") if server_config.tracing else None
    )
    tracer: Tracer = Tracer(tracing_dir) if tracing_dir else NullTracer()

    registry = SessionRegistry(
        options,
        server_config,
        tracer=tracer,
        on_warning=on_warning,
        on_session_file=(
            (
                lambda uid, workspace, app_id, mode_id, path: database.remember_composition_session(
                    uid, workspace, app_id, mode_id, path
                )
            )
            if database is not None
            else None
        ),
        resume_lookup=(
            (
                lambda uid, workspace, app_id, mode_id: database.recall_composition_session(
                    uid, workspace, app_id, mode_id
                )
            )
            if database is not None
            else None
        ),
        disabled_skills_lookup=(
            (lambda uid, workspace: database.disabled_skills(uid, workspace))
            if database is not None
            else None
        ),
        workspace_path_lookup=(
            (
                lambda uid, workspace: database.get_workspace(uid, workspace)["path"]
                if database.get_workspace(uid, workspace) is not None
                else None
            )
            if database is not None
            else None
        ),
        # Not gated on ``database``: a single-mode deployment has no enable table, but a
        # builtin app that declares a plugin should still get it.
        plugin_paths_lookup=_plugin_paths_for,
        composition_lookup=(lambda app_id, mode_id: app_composition(app_id, mode_id)),
        policy_factory=_policy_for if database is not None else None,
        on_audit=_audit_sink if database is not None else None,
        on_audit_finish=_audit_finish if database is not None else None,
        on_usage=_usage_sink if database is not None else None,
    )

    schedule_runner = (
        schedule_catalog.ScheduleRunner(schedule_db, registry) if schedule_db is not None else None
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        async def _reaper() -> None:
            """Periodically release sessions nobody is using, so idle users cost nothing."""
            interval = max(60.0, server_config.session_idle_minutes * 60 / 4)
            while True:
                await asyncio.sleep(interval)
                try:
                    await registry.evict_idle()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - a failed sweep must not kill the task
                    pass

        reaper = asyncio.create_task(_reaper())
        if schedule_runner is not None:
            await schedule_runner.start()
        try:
            yield
        finally:
            reaper.cancel()
            if schedule_runner is not None:
                await schedule_runner.stop()
            await registry.shutdown()
            if database is not None:
                database.close()
            elif local_schedule_db is not None:
                local_schedule_db.close()

    app = FastAPI(title="python-pi", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.registry = registry
    app.state.server_config = server_config
    app.state.db = database

    # Every AppError becomes one JSON shape here, so no handler chooses a body format.
    install_error_handlers(app)

    @app.exception_handler(BootstrapError)
    async def _bootstrap_failed(_request: Request, failure: BootstrapError) -> JSONResponse:
        # 503, not 500: the server is fine, this session's configuration is not.
        return JSONResponse(
            status_code=503,
            content={"error": "session could not be started", "detail": str(failure)},
        )

    @app.exception_handler(WorkspaceError)
    async def _workspace_failed(_request: Request, failure: WorkspaceError) -> JSONResponse:
        return JSONResponse(status_code=failure.status_code, content={"error": failure.message})

    def require_team_mode() -> Database:
        if database is None:
            raise error(503, "accounts are disabled (auth mode is 'single')")
        return database

    def schedule_uid(request: Request) -> str:
        if database is None:
            return LOCAL_UID
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        return user.uid

    def schedule_store() -> Database:
        if schedule_db is None:
            raise error(503, "scheduler is not configured")
        return schedule_db

    def ensure_default_workspace(uid: str) -> None:
        """Seed the implicit workspace row for an existing or new account.

        New accounts get a directory named after their username.  Existing rows
        keep their path so conversations and files are not moved behind the
        user's back.
        """
        if database is None:
            return
        existing = database.get_workspace(uid, "default")
        if existing is not None:
            os.makedirs(str(existing["path"]), exist_ok=True)
            return
        user = database.get_user(uid)
        username = str(user["username"]) if user is not None else uid
        root = server_config.workspace_dir_for(uid, username)
        os.makedirs(root, exist_ok=True)
        database.ensure_default_workspace(uid, root, f"{username} 的工作空间")

    def resolve_workspace(uid: str, requested: str | None) -> str:
        """Validate a workspace id from a query string, defaulting to ``default``."""
        workspace = (requested or "default").strip()
        if database is None:
            if workspace != "default":
                raise error(404, f"unknown workspace: {workspace}")
            return "default"
        ensure_default_workspace(uid)
        if database.get_workspace(uid, workspace) is None:
            raise error(404, f"unknown workspace: {workspace}")
        return workspace

    def ensure_apps() -> None:
        """Install the built-in catalog once; apps are shared by the whole deployment."""
        if database is None:
            return
        database.seed_team_apps(app_catalog.catalog_json()["apps"])

    def all_apps() -> list[dict[str, Any]]:
        if database is None:
            return app_catalog.catalog_json()["apps"]
        ensure_apps()
        return database.list_team_apps()

    def app_composition(app_id: str, mode_id: str) -> tuple[str, tuple[str, ...] | None]:
        if database is not None:
            composition = database.team_app_composition(app_id, mode_id)
            if composition is not None:
                return composition
        return app_catalog.composition_for(app_id, mode_id)

    def resolve_app_mode(app_id: str | None, mode_id: str | None) -> tuple[str, str]:
        apps = all_apps()
        wanted_app = app_id or app_catalog.DEFAULT_APP_ID
        app = next((item for item in apps if item["id"] == wanted_app), None)
        if app is None:
            raise error(404, f"unknown app: {wanted_app}")
        wanted_mode = mode_id or (app["modes"][0]["id"] if app["modes"] else app_catalog.DEFAULT_MODE_ID)
        mode = next((item for item in app["modes"] if item["id"] == wanted_mode), None)
        if mode is None:
            raise error(404, f"unknown mode: {wanted_app}/{wanted_mode}")
        return str(app["id"]), str(mode["id"])

    def workspace_row(uid: str, workspace: str) -> Any:
        if database is None:
            raise error(503, "workspaces require team mode")
        row = database.get_workspace(uid, workspace)
        if row is None:
            raise error(404, f"unknown workspace: {workspace}")
        return row

    async def worker_for(request: Request) -> SessionWorker:
        """The calling user's live session, started on demand.

        Single mode has exactly one implicit user, so both modes take the same path and
        every endpoint below is written once.
        """
        requested = request.query_params.get("workspace")
        if database is None:
            resolve_workspace(LOCAL_UID, requested)
            app_id, mode_id = resolve_app_mode(
                request.query_params.get("app"), request.query_params.get("mode")
            )
            return await registry.acquire(LOCAL_UID, "default", app_id, mode_id)
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        workspace = resolve_workspace(user.uid, requested)
        app_id, mode_id = resolve_app_mode(
            request.query_params.get("app"), request.query_params.get("mode")
        )
        return await registry.acquire(user.uid, workspace, app_id, mode_id)

    # Fail closed: in team mode everything under /api/ needs a live session, except the
    # two endpoints a browser must reach before it has one.  Enumerating protected paths
    # instead would mean a new endpoint is public until someone remembers to list it.
    _PUBLIC_PATHS = frozenset({"/api/health", "/api/auth/login", "/api/auth/register"})

    @app.middleware("http")
    async def _require_authentication(request: Request, call_next: Any) -> Any:
        if database is None:
            return await call_next(request)
        path = request.url.path
        if not path.startswith("/api/") or path in _PUBLIC_PATHS:
            return await call_next(request)
        if auth.current_user(request) is None:
            return JSONResponse(status_code=401, content={"error": "unauthenticated"})
        return await call_next(request)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "cwd": options.cwd,
            "clients": registry.client_count(),
            "activeUsers": registry.active_users,
            "authMode": server_config.auth_mode,
            # Which permission posture this deployment runs with, so an operator can
            # confirm the env switch took effect without reading the logs.
            "toolPolicy": server_config.tool_policy,
        }

    # Account, quota and audit routes live in their own module (see routers/accounts.py).
    # They receive the two helpers they need explicitly, so the dependency is visible
    # at the call site rather than hidden in a closure.
    register_account_routes(
        app,
        require_team_mode=require_team_mode,
        ensure_default_workspace=ensure_default_workspace,
        server_config=server_config,
    )

    def skill_context(request: Request, workspace: str) -> tuple[str, str, str, ResourceLoader]:
        """Discover skills for the caller without bootstrapping a model session."""
        if database is None:
            resolve_workspace(LOCAL_UID, workspace)
            cwd = options.cwd
            agent_dir = options.agent_dir or ""
            uid = LOCAL_UID
            if not agent_dir:
                from pi_coding_agent.settings import get_agent_dir

                agent_dir = get_agent_dir()
        else:
            user = auth.current_user(request)
            if user is None:
                raise auth.reject_unauthenticated()
            uid = user.uid
            workspace = resolve_workspace(uid, workspace)
            row = workspace_row(uid, workspace)
            cwd = str(row["path"])
            _root, agent_dir = server_config.ensure_user_layout(uid)
        settings = SettingsManager(cwd, agent_dir)
        trusted = resolve_project_trust(cwd, agent_dir, settings, quiet=True)
        loader = ResourceLoader(
            cwd,
            agent_dir,
            project_trusted=trusted,
            # Listing must include disabled skills so the UI can switch them back on;
            # the session loader applies the actual filter.
            include_home_skills=database is None,
        )
        return uid, cwd, agent_dir, loader

    @app.get("/api/skills")
    async def list_skills(
        request: Request, workspace: str = Query("default", description="Workspace id")
    ) -> dict[str, Any]:
        uid, _cwd, _agent_dir, loader = skill_context(request, workspace)
        prefs = database.skill_prefs(uid, workspace) if database is not None else {}
        return {
            "workspace": workspace,
            "skills": [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "enabled": prefs.get(skill.name, True),
                    "disableModelInvocation": skill.disable_model_invocation,
                }
                for skill in loader.skills()
            ],
        }

    @app.patch("/api/skills/{name}")
    async def toggle_skill(
        name: str,
        body: SkillToggleRequest,
        request: Request,
        workspace: str = Query("default", description="Workspace id"),
    ) -> dict[str, Any]:
        if database is None:
            raise error(400, "skill preferences require team mode")
        uid, _cwd, _agent_dir, loader = skill_context(request, workspace)
        if all(skill.name != name for skill in loader.skills()):
            raise error(404, f"unknown skill: {name}")
        database.set_skill_pref(uid, name, body.enabled, workspace)
        disabled = database.disabled_skills(uid, workspace)
        for worker in registry.workers_for(uid, workspace):
            if worker.started:
                worker.controller.session.set_disabled_skills(disabled)
        return {"name": name, "enabled": body.enabled, "workspace": workspace}

    @app.get("/api/models")
    async def models(request: Request) -> dict[str, Any]:
        worker = await worker_for(request)
        return {"models": [model_to_json(model) for model in worker.state.registry.list_models()]}

    # Scheduled tasks and the notification feed (routers/schedules.py).
    register_schedule_routes(
        app,
        schedule_uid=schedule_uid,
        schedule_store=schedule_store,
        require_team_mode=require_team_mode,
        registry=registry,
        resolve_workspace=resolve_workspace,
        resolve_app_mode=resolve_app_mode,
        schedule_runner=schedule_runner,
    )

    # Plugin install, submissions and enablement (routers/plugins.py).
    register_plugin_routes(
        app,
        server_config=server_config,
        require_team_mode=require_team_mode,
        plugin_paths_for=_plugin_paths_for,
        registry=registry,
        database=database,
        resolve_app_mode=resolve_app_mode,
    )

    def _safe_id(raw: Any, fallback: str) -> str:
        value = str(raw or "").strip()
        if value and len(value) <= 40 and all(char.isalnum() or char in "-_" for char in value):
            return value
        return fallback

    _APP_ICONS = frozenset(
        {
            "apps",
            "book",
            "clock",
            "code",
            "file",
            "folder",
            "house",
            "image",
            "palette",
            "plugin",
            "search",
            "sheet",
            "skill",
            "slides",
            "spark",
        }
    )
    _ACCENT = re.compile(r"^#[0-9a-fA-F]{6}$")

    def normalize_app(payload: AppUpsertRequest, app_id: str) -> dict[str, Any]:
        name = payload.name.strip()
        if not 1 <= len(name) <= 40:
            raise error(422, "app name must be 1-40 characters")
        accent = str(payload.accent or "").strip()
        if not _ACCENT.match(accent):
            raise error(422, "accent must look like #3b5fba")
        if len(payload.modes) > 10:
            raise error(422, "an app can have at most 10 modes")

        modes: list[dict[str, Any]] = []
        seen_modes: set[str] = set()
        for mode_index, raw_mode in enumerate(payload.modes):
            mode_id = _safe_id(raw_mode.get("id"), f"mode-{mode_index + 1}")
            if mode_id in seen_modes:
                raise error(422, f"duplicate mode id: {mode_id}")
            seen_modes.add(mode_id)
            mode_name = str(raw_mode.get("name") or mode_id).strip()[:40]
            prompt = str(raw_mode.get("prompt") or "")
            if len(prompt) > 4000:
                raise error(422, f"mode {mode_name!r} prompt is over 4000 characters")
            binding = raw_mode.get("skillBinding", "all")
            if binding != "all":
                if not isinstance(binding, list):
                    raise error(422, "skillBinding must be 'all' or a list")
                binding = list(dict.fromkeys(str(item).strip() for item in binding if str(item).strip()))

            examples_raw = raw_mode.get("examples") or []
            if not isinstance(examples_raw, list):
                raise error(422, "examples must be a list of strings or objects")
            if len(examples_raw) > 6:
                raise error(422, "at most 6 examples per mode")
            examples: list[dict[str, Any]] = []
            seen_examples: set[str] = set()
            for example_index, raw_example in enumerate(examples_raw):
                if isinstance(raw_example, str):
                    prompt = raw_example.strip()[:1500]
                    title = prompt[:80]
                    description = ""
                    image = ""
                    kind = ""
                    fmt = ""
                    tags: list[str] = []
                    example_id = ""
                elif isinstance(raw_example, dict):
                    prompt = str(raw_example.get("prompt") or "").strip()[:1500]
                    title = str(raw_example.get("title") or prompt[:80]).strip()[:80]
                    description = str(raw_example.get("description") or "")[:200]
                    image = str(raw_example.get("image") or "").strip()
                    if image and not re.match(r"^(https?://|/)", image):
                        image = ""
                    kind = str(raw_example.get("kind") or "")[:40]
                    fmt = str(raw_example.get("format") or "")[:40]
                    raw_tags = raw_example.get("tags") or []
                    tags = (
                        [str(tag)[:24] for tag in raw_tags if str(tag).strip()][:6]
                        if isinstance(raw_tags, list)
                        else []
                    )
                    example_id = _safe_id(
                        raw_example.get("id"), f"example-{example_index + 1}"
                    )
                else:
                    raise error(422, "each example must be a string or an object")
                if not prompt:
                    continue
                if example_id in seen_examples:
                    example_id = f"example-{example_index + 1}"
                while example_id in seen_examples:
                    example_id = f"{example_id}-x"
                seen_examples.add(example_id)
                examples.append(
                    {
                        "id": example_id,
                        "prompt": prompt,
                        "title": title or prompt[:80],
                        "description": description,
                        "image": image,
                        "kind": kind,
                        "format": fmt,
                        "tags": tags,
                    }
                )

            placeholder = str(raw_mode.get("placeholder") or "")[:80]
            icon = str(raw_mode.get("icon") or "").strip()
            if icon and icon not in _APP_ICONS:
                icon = ""

            capsules: list[dict[str, Any]] = []
            seen_capsules: set[str] = set()
            raw_capsules = raw_mode.get("capsules") or []
            if not isinstance(raw_capsules, list):
                raise error(422, "capsules must be a list")
            if len(raw_capsules) > 12:
                raise error(422, "at most 12 capsules per mode")
            for capsule_index, raw_capsule in enumerate(raw_capsules):
                if not isinstance(raw_capsule, dict):
                    raise error(422, "each capsule must be an object")
                capsule_id = _safe_id(raw_capsule.get("id"), f"capsule-{capsule_index + 1}")
                if capsule_id in seen_capsules:
                    raise error(422, f"duplicate capsule id: {capsule_id}")
                seen_capsules.add(capsule_id)
                capsule_name = str(raw_capsule.get("name") or capsule_id).strip()[:40]
                if not capsule_name:
                    continue
                capsules.append(
                    {
                        "id": capsule_id,
                        "name": capsule_name,
                        "prompt": str(raw_capsule.get("prompt") or capsule_name)[:500],
                    }
                )
            modes.append(
                {
                    "id": mode_id,
                    "name": mode_name or mode_id,
                    "description": str(raw_mode.get("description") or "")[:200],
                    "prompt": prompt,
                    "skillBinding": binding,
                    "examples": examples,
                    "placeholder": placeholder,
                    "icon": icon,
                    "capsules": capsules,
                }
            )
        if not modes:
            raise error(422, "an app needs at least one mode")
        return {
            "id": app_id,
            "name": name,
            "description": payload.description.strip()[:200],
            "accent": accent,
            "modes": modes,
        }

    # -- message feedback --------------------------------------------------------------

    def checked_session_file(uid: str, session_file: str) -> str:
        agent_dir = os.path.realpath(server_config.agent_dir(uid))
        candidate = os.path.realpath(os.path.abspath(session_file))
        try:
            inside = os.path.commonpath([agent_dir, candidate]) == agent_dir
        except ValueError:
            inside = False
        if not inside:
            raise error(403, "session file is outside the agent directory")
        return candidate

    @app.post("/api/feedback")
    async def submit_feedback(payload: FeedbackRequest, request: Request) -> dict[str, Any]:
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        if payload.rating not in (-1, 1):
            raise error(422, "rating must be -1 or 1")
        session_file = checked_session_file(user.uid, payload.sessionFile)
        db.set_feedback(user.uid, session_file, payload.timestamp, payload.rating, payload.comment)
        return {"timestamp": payload.timestamp, "rating": payload.rating}

    @app.get("/api/feedback")
    async def list_feedback(request: Request, session: str = Query(...)) -> dict[str, Any]:
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        session_file = checked_session_file(user.uid, session)
        return {
            "feedback": [
                {
                    "timestamp": row["message_timestamp"],
                    "rating": row["rating"],
                    "comment": row["comment"],
                }
                for row in db.list_feedback(user.uid, session_file)
            ]
        }

    # -- marketplace -------------------------------------------------------------------

    def market_item_json(item: Any, uid: str) -> dict[str, Any]:
        agent_dir = server_config.agent_dir(uid)
        if item.kind == "skill":
            installed = os.path.isfile(os.path.join(agent_dir, "skills", item.id, "SKILL.md"))
        else:
            installed = any(
                found.id == item.id for found in plugin_catalog.scan(server_config.plugin_root)
            )
        average, count = database.market_rating_summary(item.kind, item.id) if database else (0.0, 0)
        mine = database.my_market_rating(uid, item.kind, item.id) if database else None
        return {
            **market_catalog.item_json(item),
            "installed": installed,
            "rating": {"average": average, "count": count, "mine": mine},
        }

    @app.get("/api/market")
    async def list_market(
        request: Request,
        kind: str = Query("skill"),
        q: str = Query("", description="name/description/tag/author/source"),
        tag: str = Query("", description="exact tag filter"),
        sort: str = Query("rating", description="rating | name | source"),
    ) -> dict[str, Any]:
        require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        if kind not in ("skill", "plugin"):
            raise error(422, "kind must be skill or plugin")
        items = [
            market_item_json(item, user.uid)
            for item in market_catalog.search_items(kind, q, tag)
        ]
        if sort == "name":
            items.sort(key=lambda item: (item["name"], item["id"]))
        elif sort == "source":
            items.sort(key=lambda item: (item["source"], item["name"]))
        else:
            items.sort(
                key=lambda item: (-float(item["rating"]["average"]), item["name"], item["id"])
            )
        tags = sorted(
            {tag_name for item in market_catalog.list_items(kind) for tag_name in item.tags}
        )
        return {
            "kind": kind,
            "items": items,
            "tags": tags,
            "canInstall": True if kind == "skill" else bool(user.is_admin),
        }

    @app.get("/api/market/{kind}/{item_id}")
    async def market_item_detail(kind: str, item_id: str, request: Request) -> dict[str, Any]:
        require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        item = market_catalog.find_item(kind, item_id)
        if item is None:
            raise error(404, "unknown market item")
        return {
            **market_item_json(item, user.uid),
            "readme": market_catalog.readme_for(item),
        }

    @app.post("/api/market/{kind}/{item_id}/rating")
    async def rate_market_item(
        kind: str, item_id: str, payload: MarketRatingRequest, request: Request
    ) -> dict[str, Any]:
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        item = market_catalog.find_item(kind, item_id)
        if item is None:
            raise error(404, "unknown market item")
        if not 1 <= payload.score <= 5:
            raise error(422, "score must be 1-5")
        db.set_market_rating(user.uid, kind, item_id, payload.score)
        average, count = db.market_rating_summary(kind, item_id)
        return {"average": average, "count": count, "mine": payload.score}

    @app.post("/api/market/skills/{item_id}/install")
    async def install_market_skill(item_id: str, request: Request) -> dict[str, Any]:
        require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        item = market_catalog.find_item("skill", item_id)
        if item is None or not item.content:
            raise error(404, "unknown skill")
        _workspace, agent_dir = server_config.ensure_user_layout(user.uid)
        target = os.path.join(agent_dir, "skills", item.id)
        if os.path.exists(os.path.join(target, "SKILL.md")):
            raise error(409, "skill is already installed")
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write(item.content)
        await registry.evict(user.uid)
        return {"id": item.id, "installed": True}

    @app.post("/api/market/plugins/{item_id}/install")
    async def install_market_plugin(item_id: str, request: Request) -> dict[str, Any]:
        require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        if not user.is_admin:
            raise error(403, "only administrators can install plugins")
        item = market_catalog.find_item("plugin", item_id)
        if item is None or not item.package or not os.path.isdir(item.package):
            raise error(404, "plugin package is not bundled")
        os.makedirs(server_config.plugin_root, exist_ok=True)
        target = os.path.join(server_config.plugin_root, item.id)
        if os.path.exists(target):
            raise error(409, "plugin is already installed")
        shutil.copytree(
            item.package,
            target,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
        return {**market_catalog.item_json(item), "installed": True, "enabled": False}

    # -- apps (team-wide; only administrators may change them) -------------------------

    # Team apps (routers/apps.py).
    register_app_routes(
        app,
        database=database,
        server_config=server_config,
        registry=registry,
        require_team_mode=require_team_mode,
        ensure_apps=ensure_apps,
        all_apps=all_apps,
        normalize_app=normalize_app,
        safe_id=_safe_id,
    )

    # -- office editor ----------------------------------------------------------------

    # Office documents and the spreadsheet sidecar (routers/office.py).
    register_office_routes(
        app,
        database=database,
        options=options,
        resolve_workspace=resolve_workspace,
        workspace_row=workspace_row,
    )

    # Workspace files, artifacts, export, session list/state (routers/workspace.py).
    register_workspace_routes(
        app,
        database=database,
        options=options,
        server_config=server_config,
        registry=registry,
        require_team_mode=require_team_mode,
        resolve_workspace=resolve_workspace,
        workspace_row=workspace_row,
        ensure_default_workspace=ensure_default_workspace,
        worker_for=worker_for,
    )

    @app.websocket("/api/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        # The handshake is a same-origin request, so the session cookie is already here;
        # no credential needs to travel in the query string (where it would be logged).
        uid = LOCAL_UID
        requested = websocket.query_params.get("workspace")
        if database is not None:
            user = auth.resolve_session(database, websocket.cookies.get(COOKIE_NAME))
            if user is None:
                await websocket.close(code=4401, reason="unauthenticated")
                return
            uid = user.uid
        try:
            workspace = resolve_workspace(uid, requested)
            app_id, mode_id = resolve_app_mode(
                websocket.query_params.get("app"), websocket.query_params.get("mode")
            )
        except HTTPException:
            await websocket.close(code=4404, reason="unknown workspace or app")
            return
        try:
            worker = await registry.acquire(uid, workspace, app_id, mode_id)
        except CapacityError as error:
            await websocket.close(code=4429, reason=str(error))
            return
        except BootstrapError as error:
            # Say why, rather than dropping the socket with no explanation.
            await websocket.send_text(
                serialize_json_line(
                    {"type": "response", "command": "server", "success": False, "error": str(error)}
                ).rstrip("\n")
            )
            await websocket.close(code=4400, reason="session could not be started")
            return
        connection = worker.hub.add(websocket)
        # A single writer per connection keeps frames from interleaving.
        connection.sender = asyncio.create_task(_pump(connection))
        try:
            await worker.ensure_started()
            # The question broker refuses to ask when no tab can see the card; this
            # counter is what makes that check live instead of optimistic.
            worker.controller.set_client_count(lambda: worker.hub.client_count)
            # Snapshot the position once: ``ready`` and the replay below must agree,
            # or a message persisted between the two would be promised as replayable
            # and then skipped (the tab would lose it).
            stream_head = worker.run_log.head
            persisted = worker.last_persisted_seq
            connection.send(
                {
                    "type": "ready",
                    "version": __version__,
                    "cwd": worker.state.cwd,
                    "warnings": list(worker.warnings),
                    # A reconnecting tab tells the server what it already has, but
                    # this is the authoritative pair: everything at or below
                    # ``lastPersistedSeq`` is in ``get_state``, everything above it
                    # is replayed below.
                    "seq": stream_head,
                    "lastPersistedSeq": persisted,
                }
            )
            connection.send({"type": "response", "command": "get_state", "success": True,
                             "data": worker.state_payload()})
            # Replay *after* get_state: the snapshot is the base a tab renders, and
            # the replayed tail is the in-flight message that has not been
            # persisted.  Nothing else is worth replaying — a finished turn's
            # turn_end/agent_end are already reflected in the snapshot.
            if worker.run_log.has_unpersisted_message(persisted):
                since = query_int(websocket.query_params.get("since"))
                for record in worker.run_log.since(replay_floor(since, persisted)):
                    connection.send(record)
            while True:
                text = await websocket.receive_text()
                worker.touch()
                worker.hub.touch(connection)
                # Quota is enforced here, before the controller acknowledges the command,
                # so a user over budget is told immediately instead of after a full turn.
                rejected = _quota_rejection(uid, text)
                if rejected is not None:
                    connection.send(rejected)
                    continue
                await worker.controller.handle_line(text)
        except WebSocketDisconnect:
            pass
        except Exception as error:  # noqa: BLE001 - one bad frame must not kill the server
            connection.send({"type": "response", "command": "server", "success": False,
                             "error": str(error)})
        finally:
            if connection.sender is not None:
                connection.sender.cancel()
            worker.hub.remove(connection)
            # A closed tab must not keep a question's countdown paused; the host
            # clock takes the wait back so the turn can still finish.
            worker.controller.release_question_claims()

    _mount_spa(app)
    return app


async def _pump(connection: Connection) -> None:
    """Drain one connection's outbound queue onto its socket.

    Records are serialized with the *same* serializer as RPC mode rather than
    ``send_json``: it tolerates values that are not plain JSON (``default=str``), so
    one odd ``details`` payload cannot kill the stream, and it produces records
    byte-identical to the stdio protocol.  A frame that still cannot be encoded is
    skipped rather than allowed to end the connection.
    """
    try:
        while True:
            payload = await connection.queue.get()
            try:
                text = serialize_json_line(payload).rstrip("\n")
            except Exception as error:  # noqa: BLE001 - never lose the stream over one frame
                print(f"pi-py-web: dropped an unencodable frame: {error!r}", file=sys.stderr)
                continue
            await connection.websocket.send_text(text)
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        return
    except Exception as error:  # noqa: BLE001 - report and stop this connection only
        print(f"pi-py-web: connection writer stopped: {error!r}", file=sys.stderr)
        return


def resolve_office_dir() -> Path | None:
    """Where the forked AIOffice docs build lives (``office-dist``)."""
    override = os.environ.get("PI_OFFICE_DIST")
    if override and (Path(override) / "index.html").is_file():
        return Path(override)
    repo = Path(__file__).resolve().parents[4] / "office-dist"
    if (repo / "index.html").is_file():
        return repo
    return None


def _mount_spa(app: FastAPI) -> None:
    """Serve the built SPA, with a history fallback for client-side routes."""
    office_dir = resolve_office_dir()
    if office_dir is not None:
        app.mount("/office", StaticFiles(directory=str(office_dir), html=True), name="office")
    static_dir = resolve_static_dir()
    if static_dir is None:
        @app.get("/")
        async def _missing() -> JSONResponse:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "web UI is not built",
                    "hint": "run `npm install && npm run build` in web/, or set PI_WEB_DIST",
                },
            )

        return

    assets = static_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    index = static_dir / "index.html"

    @app.get("/{path:path}")
    async def spa(path: str) -> Any:
        candidate = (static_dir / path).resolve()
        # Only serve files that really live under the dist directory.
        if path and candidate.is_file() and str(candidate).startswith(str(static_dir.resolve())):
            return FileResponse(candidate)
        return FileResponse(index)


def app_factory_options() -> BootstrapOptions:  # pragma: no cover - convenience
    return BootstrapOptions(cwd=os.getcwd())


__all__ = [
    "BLOCKING_DIALOG_METHODS",
    "CapacityError",
    "Connection",
    "Hub",
    "LOCAL_UID",
    "SessionRegistry",
    "SessionWorker",
    "create_app",
    "resolve_static_dir",
]

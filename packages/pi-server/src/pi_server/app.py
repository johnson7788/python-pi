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
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pi_coding_agent.bootstrap import BootstrapOptions, bootstrap
from pi_coding_agent.modes.jsonl import serialize_json_line
from pi_coding_agent.modes.rpc_mode import RpcMode, model_to_json
from pi_coding_agent.session_manager import SessionManager

from . import auth
from .config import COOKIE_NAME, ServerConfig
from .db import Database, DuplicateUsername

__version__ = "0.1.0"

#: Shortest password the account endpoints will accept.  Length beats composition rules.
_MIN_PASSWORD_LENGTH = 8

#: The single implicit tenant in ``single`` mode.  Team mode never uses it.
LOCAL_UID = "local"

#: Dialog methods that block a handler until a client answers.
BLOCKING_DIALOG_METHODS = frozenset({"select", "confirm", "input", "editor"})


@dataclass
class Connection:
    """One browser tab."""

    websocket: WebSocket
    queue: "asyncio.Queue[dict]" = field(default_factory=asyncio.Queue)
    sender: asyncio.Task | None = None

    def send(self, payload: dict) -> None:
        self.queue.put_nowait(payload)


class Hub:
    """Fan-out for outbound records."""

    def __init__(self) -> None:
        self._clients: list[Connection] = []
        self._last_active: Connection | None = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def add(self, websocket: WebSocket) -> Connection:
        connection = Connection(websocket=websocket)
        self._clients.append(connection)
        self._last_active = connection
        return connection

    def remove(self, connection: Connection) -> None:
        try:
            self._clients.remove(connection)
        except ValueError:
            pass
        if self._last_active is connection:
            self._last_active = self._clients[-1] if self._clients else None

    def touch(self, connection: Connection) -> None:
        self._last_active = connection

    def broadcast(self, payload: dict) -> None:
        for connection in list(self._clients):
            connection.send(payload)

    def send_to_dialog_target(self, payload: dict) -> None:
        """Deliver a blocking dialog to one client, not all of them."""
        target = self._last_active or (self._clients[-1] if self._clients else None)
        if target is None:
            # Nobody can answer; the UI context resolves with safe defaults.
            return
        target.send(payload)

    def emit_routed(self, payload: dict) -> None:
        """Route one outbound record: dialogs to one client, everything else to all."""
        if (
            payload.get("type") == "extension_ui_request"
            and payload.get("method") in BLOCKING_DIALOG_METHODS
        ):
            self.send_to_dialog_target(payload)
            return
        self.broadcast(payload)


class CapacityError(RuntimeError):
    """Raised when the process is already holding as many sessions as it may."""


class BootstrapError(RuntimeError):
    """A session could not be started, for a reason the user can act on.

    ``bootstrap`` reports missing credentials and unselectable models by raising
    ``SystemExit``, which is not an ``Exception``: in a single-user CLI that exits the
    program, but in a server it would tear through the request handler.  It is converted
    here so one user's misconfiguration becomes that user's error message.
    """


class SessionWorker:
    """One user's live session, plus the browser tabs watching it.

    A worker owns exactly what a whole server process used to own: a bootstrapped
    session, the ``RpcMode`` controller driving it, and a :class:`Hub`.  Because the hub
    is per worker, a blocking dialog is offered only to *that user's* tabs — the old
    "most recently active tab anywhere" rule would have shown one colleague's
    confirmation prompt inside another colleague's page.
    """

    def __init__(
        self,
        uid: str,
        options: BootstrapOptions,
        *,
        on_warning: Callable[[str], None] | None = None,
        on_session_file: Callable[[str, str], None] | None = None,
    ) -> None:
        self.uid = uid
        self.options = options
        self.hub = Hub()
        self.warnings: list[str] = []
        self.last_active = time.monotonic()
        self._on_warning = on_warning
        self._on_session_file = on_session_file
        self._state: Any = None
        self._controller: RpcMode | None = None
        self._lock = asyncio.Lock()

    @property
    def started(self) -> bool:
        return self._controller is not None

    @property
    def state(self) -> Any:
        assert self._state is not None, "worker not started"
        return self._state

    @property
    def controller(self) -> RpcMode:
        assert self._controller is not None, "worker not started"
        return self._controller

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_active

    @property
    def busy(self) -> bool:
        """True while a turn is running — an idle-timeout must never interrupt one."""
        if self._controller is None:
            return False
        try:
            session = self._controller.session
        except Exception:  # noqa: BLE001 - a torn-down session is simply not busy
            return False
        return bool(getattr(session, "is_streaming", False))

    def touch(self) -> None:
        self.last_active = time.monotonic()

    def session_file(self) -> str | None:
        if self._controller is None:
            return None
        try:
            return self._controller.session.state().get("session_file")
        except Exception:  # noqa: BLE001
            return None

    async def ensure_started(self) -> None:
        """Bootstrap on first use, so the server binds instantly and idle users cost nothing."""
        if self._controller is not None:
            return
        async with self._lock:
            if self._controller is not None:
                return

            def _warn(message: str) -> None:
                self.warnings.append(message)
                if self._on_warning is not None:
                    self._on_warning(message)

            state = await bootstrap(self.options, on_warning=_warn)
            self._state = state
            self._controller = RpcMode(
                runtime=state.runtime,
                model_registry=state.registry,
                resource_loader=state.loader,
                extension_runner=state.extension_runner,
                emit=self.hub.emit_routed,
                host_mode="web",
            )

    async def shutdown(self, reason: str = "session closed") -> None:
        """Record where the user was, then release the session object."""
        if self._on_session_file is not None:
            path = self.session_file()
            if path:
                self._on_session_file(self.uid, path)
        controller, state = self._controller, self._state
        self._controller, self._state = None, None
        if controller is not None:
            controller.close()
        if state is not None:
            await state.shutdown(reason)

    def state_payload(self) -> dict[str, Any]:
        """The same shape the ``get_state`` command returns."""
        session = self.controller.session
        snapshot = session.state()
        payload: dict[str, Any] = {
            "model": model_to_json(session.model),
            "thinkingLevel": session.thinking_level,
            "isStreaming": session.is_streaming,
            "isCompacting": session.is_compacting,
            "steeringMode": session.steering_mode,
            "followUpMode": session.follow_up_mode,
            "sessionFile": snapshot["session_file"],
            "sessionId": snapshot["session_id"],
            "autoCompactionEnabled": session.auto_compaction_enabled,
            "messageCount": len(session.messages),
            "pendingMessageCount": session.pending_message_count,
            "cwd": self.state.cwd,
        }
        if snapshot["session_name"]:
            payload["sessionName"] = snapshot["session_name"]
        return payload


class SessionRegistry:
    """Owns every live session in this process, one worker per user.

    In team mode each user's ``cwd`` and ``agent_dir`` come from their ``uid``, so a
    session physically cannot see another user's files.  Single mode keeps exactly one
    worker under :data:`LOCAL_UID` with the command-line options untouched, which is how
    the original personal flow keeps behaving the same way it always did.

    Two things bound the shared failure domain: a cap on live workers, and idle
    eviction.  Eviction is lossless because the session file is remembered before the
    session object is released.
    """

    def __init__(
        self,
        base_options: BootstrapOptions,
        config: ServerConfig,
        *,
        on_warning: Callable[[str], None] | None = None,
        on_session_file: Callable[[str, str], None] | None = None,
        resume_lookup: Callable[[str], str | None] | None = None,
    ) -> None:
        self.base_options = base_options
        self.config = config
        self._on_warning = on_warning
        self._on_session_file = on_session_file
        self._resume_lookup = resume_lookup
        self._workers: dict[str, SessionWorker] = {}
        self._lock = asyncio.Lock()

    # -- introspection ---------------------------------------------------------------

    @property
    def active_users(self) -> int:
        return len(self._workers)

    def worker(self, uid: str) -> SessionWorker | None:
        return self._workers.get(uid)

    def client_count(self) -> int:
        return sum(worker.hub.client_count for worker in self._workers.values())

    # -- lifecycle -------------------------------------------------------------------

    def options_for(self, uid: str) -> BootstrapOptions:
        """Per-user session options.  Single mode passes the command line through."""
        if not self.config.requires_auth:
            return self.base_options
        workspace, agent = self.config.ensure_user_layout(uid)
        resume = self._resume_lookup(uid) if self._resume_lookup is not None else None
        if resume and not os.path.isfile(resume):
            resume = None
        return dataclasses.replace(
            self.base_options,
            cwd=workspace,
            agent_dir=agent,
            # The shared credential replaces any per-user `auth.json`, so a user cannot
            # shadow it.  `--resume` is per worker, never inherited from the command line.
            api_key=self.config.api_key or self.base_options.api_key,
            resume=resume,
            extension_paths=(),
            no_extensions=not self.config.allow_user_extensions,
        )

    async def acquire(self, uid: str) -> SessionWorker:
        """Return the user's worker, starting it if needed."""
        async with self._lock:
            existing = self._workers.get(uid)
            if existing is not None:
                existing.touch()
                return existing

            await self._evict_idle_locked()
            if len(self._workers) >= self.config.max_active_users:
                raise CapacityError(
                    f"the server is already running {len(self._workers)} sessions; try again shortly"
                )

            worker = SessionWorker(
                uid,
                self.options_for(uid),
                on_warning=self._on_warning,
                on_session_file=self._on_session_file,
            )
            self._workers[uid] = worker

        # Started outside the registry lock: a bootstrap can take seconds, and holding the
        # lock would stall every other user for its duration.
        try:
            await worker.ensure_started()
        except BaseException as error:
            async with self._lock:
                self._workers.pop(uid, None)
            if isinstance(error, SystemExit):
                raise BootstrapError(str(error)) from error
            raise
        worker.touch()
        return worker

    async def _evict_idle_locked(self) -> None:
        timeout = self.config.session_idle_minutes * 60
        if timeout <= 0:
            return
        # Never evict the last worker: it is the one being asked for right now.
        for uid, worker in list(self._workers.items()):
            if len(self._workers) <= 1:
                break
            if worker.busy or worker.idle_seconds < timeout:
                continue
            self._workers.pop(uid, None)
            await worker.shutdown("idle timeout")

    async def evict_idle(self) -> int:
        """Drop sessions that have gone quiet.  Called periodically by the app."""
        async with self._lock:
            before = len(self._workers)
            await self._evict_idle_locked()
            return before - len(self._workers)

    async def shutdown(self) -> None:
        async with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            try:
                await worker.shutdown("server stopped")
            except Exception:  # noqa: BLE001 - one bad teardown must not block the rest
                pass


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


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChangeRequest(BaseModel):
    currentPassword: str
    newPassword: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    displayName: str = ""
    role: str = "user"
    mustChangePassword: bool = True
    monthlyTokenQuota: int | None = None
    monthlyCostQuota: float | None = None


class UpdateUserRequest(BaseModel):
    """Every field is optional; only what the client sent is applied."""

    displayName: str | None = None
    role: str | None = None
    status: str | None = None
    password: str | None = None
    mustChangePassword: bool | None = None
    monthlyTokenQuota: int | None = None
    monthlyCostQuota: float | None = None


def _user_row_to_json(row: Any) -> dict[str, Any]:
    """The admin-facing shape of an account.  Never includes the password hash."""
    return {
        "uid": row["uid"],
        "username": row["username"],
        "displayName": row["display_name"],
        "role": row["role"],
        "status": row["status"],
        "mustChangePassword": bool(row["must_change_password"]),
        "monthlyTokenQuota": row["monthly_token_quota"],
        "monthlyCostQuota": row["monthly_cost_quota"],
        "createdAt": row["created_at"],
        "lastLoginAt": row["last_login_at"],
    }


def create_app(
    options: BootstrapOptions,
    *,
    on_warning: Any = None,
    config: ServerConfig | None = None,
) -> FastAPI:
    server_config = config or ServerConfig.from_env()
    # Accounts only exist in team mode, so single mode does not create a database (or a
    # data root) behind the user's back — the personal flow stays byte-for-byte what it was.
    database: Database | None = None
    if server_config.requires_auth:
        server_config.ensure_layout()
        database = Database(server_config.db_path)
        database.purge_expired_sessions()

    registry = SessionRegistry(
        options,
        server_config,
        on_warning=on_warning,
        on_session_file=(
            (lambda uid, path: database.remember_session(uid, path))
            if database is not None
            else None
        ),
        resume_lookup=(
            (lambda uid: database.recall_session(uid)) if database is not None else None
        ),
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
        try:
            yield
        finally:
            reaper.cancel()
            await registry.shutdown()
            if database is not None:
                database.close()

    app = FastAPI(title="python-pi", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.registry = registry
    app.state.server_config = server_config
    app.state.db = database

    @app.exception_handler(BootstrapError)
    async def _bootstrap_failed(_request: Request, error: BootstrapError) -> JSONResponse:
        # 503, not 500: the server is fine, this session's configuration is not.
        return JSONResponse(
            status_code=503,
            content={"error": "session could not be started", "detail": str(error)},
        )

    def require_team_mode() -> Database:
        if database is None:
            raise HTTPException(
                status_code=503,
                detail={"error": "accounts are disabled (auth mode is 'single')"},
            )
        return database

    async def worker_for(request: Request) -> SessionWorker:
        """The calling user's live session, started on demand.

        Single mode has exactly one implicit user, so both modes take the same path and
        every endpoint below is written once.
        """
        if database is None:
            return await registry.acquire(LOCAL_UID)
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        return await registry.acquire(user.uid)

    # Fail closed: in team mode everything under /api/ needs a live session, except the
    # two endpoints a browser must reach before it has one.  Enumerating protected paths
    # instead would mean a new endpoint is public until someone remembers to list it.
    _PUBLIC_PATHS = frozenset({"/api/health", "/api/auth/login"})

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
        }

    # -- accounts ----------------------------------------------------------------------

    @app.post("/api/auth/login")
    async def login(payload: LoginRequest, request: Request) -> JSONResponse:
        db = require_team_mode()
        user = auth.authenticate(db, payload.username, payload.password)
        if user is None:
            # One message for every failure mode: never reveal whether a name exists.
            raise HTTPException(status_code=401, detail={"error": "invalid username or password"})
        token = auth.start_session(
            db, user, server_config, user_agent=request.headers.get("user-agent", "")
        )
        response = JSONResponse({"user": user.to_json()})
        auth.set_cookie(response, token, server_config)
        return response

    @app.post("/api/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        db = require_team_mode()
        token = request.cookies.get(COOKIE_NAME)
        if token:
            db.revoke_auth_session(auth.hash_token(token))
        response = JSONResponse({"ok": True})
        auth.clear_cookie(response)
        return response

    @app.get("/api/auth/me")
    async def me(request: Request) -> dict[str, Any]:
        user = auth.current_user(request)
        return {
            "authMode": server_config.auth_mode,
            "user": user.to_json() if user is not None else None,
        }

    @app.post("/api/auth/password")
    async def change_password(
        payload: PasswordChangeRequest,
        user: auth.AuthUser = Depends(auth.get_required_user),
    ) -> JSONResponse:
        """Change your own password.  Other devices are signed out; this one is not."""
        db = require_team_mode()
        row = db.get_user(user.uid)
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "account not found"})
        if not auth.verify_password(payload.currentPassword, row["password_hash"]):
            raise HTTPException(status_code=403, detail={"error": "current password is incorrect"})
        if len(payload.newPassword) < _MIN_PASSWORD_LENGTH:
            raise HTTPException(
                status_code=422,
                detail={"error": f"password must be at least {_MIN_PASSWORD_LENGTH} characters"},
            )
        db.set_password(user.uid, auth.hash_password(payload.newPassword), must_change=False)
        db.revoke_user_sessions(user.uid)
        token = auth.start_session(db, user, server_config)
        refreshed = db.get_user(user.uid)
        response = JSONResponse(
            {"user": _user_row_to_json(refreshed) if refreshed is not None else user.to_json()}
        )
        auth.set_cookie(response, token, server_config)
        return response

    # -- administration ----------------------------------------------------------------

    @app.get("/api/admin/users")
    async def admin_list_users(
        _actor: auth.AuthUser = Depends(auth.get_admin_user),
    ) -> dict[str, Any]:
        db = require_team_mode()
        return {"users": [_user_row_to_json(row) for row in db.list_users()]}

    @app.post("/api/admin/users")
    async def admin_create_user(
        payload: CreateUserRequest,
        actor: auth.AuthUser = Depends(auth.get_admin_user),
    ) -> dict[str, Any]:
        db = require_team_mode()
        auth.validate_role(payload.role)
        if payload.role != "user" and not actor.is_superadmin:
            raise HTTPException(
                status_code=403, detail={"error": "only a superadmin can create administrators"}
            )
        if len(payload.password) < _MIN_PASSWORD_LENGTH:
            raise HTTPException(
                status_code=422,
                detail={"error": f"password must be at least {_MIN_PASSWORD_LENGTH} characters"},
            )
        try:
            uid = db.create_user(
                username=payload.username,
                password_hash=auth.hash_password(payload.password),
                display_name=payload.displayName or payload.username,
                role=payload.role,
                must_change_password=payload.mustChangePassword,
                monthly_token_quota=payload.monthlyTokenQuota,
                monthly_cost_quota=payload.monthlyCostQuota,
            )
        except DuplicateUsername:
            raise HTTPException(
                status_code=409, detail={"error": f"username already exists: {payload.username}"}
            ) from None
        created = db.get_user(uid)
        assert created is not None
        return {"user": _user_row_to_json(created)}

    @app.patch("/api/admin/users/{uid}")
    async def admin_update_user(
        uid: str,
        payload: UpdateUserRequest,
        actor: auth.AuthUser = Depends(auth.get_admin_user),
    ) -> dict[str, Any]:
        db = require_team_mode()
        target = db.get_user(uid)
        if target is None:
            raise HTTPException(status_code=404, detail={"error": "account not found"})

        sent = payload.model_dump(exclude_unset=True)
        if "role" in sent:
            auth.validate_role(sent["role"])
            if not actor.is_superadmin:
                raise HTTPException(
                    status_code=403, detail={"error": "only a superadmin can change roles"}
                )
        if "status" in sent and sent["status"] not in {"active", "disabled"}:
            raise HTTPException(status_code=422, detail={"error": "status must be active or disabled"})
        if uid == actor.uid and ("role" in sent or "status" in sent):
            raise HTTPException(
                status_code=400, detail={"error": "you cannot change your own role or status"}
            )
        if len(sent.get("password") or "") and len(sent["password"]) < _MIN_PASSWORD_LENGTH:
            raise HTTPException(
                status_code=422,
                detail={"error": f"password must be at least {_MIN_PASSWORD_LENGTH} characters"},
            )
        # Never let the last usable administrator lock everyone out.
        removes_admin = sent.get("status") == "disabled" or sent.get("role") == "user"
        if target["role"] in auth.ADMIN_ROLES and target["status"] == "active" and removes_admin:
            if db.count_active_admins() <= 1:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "the last active administrator cannot be demoted or disabled"},
                )

        fields: dict[str, Any] = {}
        if "displayName" in sent:
            fields["display_name"] = sent["displayName"] or target["username"]
        if "role" in sent:
            fields["role"] = sent["role"]
        if "status" in sent:
            fields["status"] = sent["status"]
        if "monthlyTokenQuota" in sent:
            fields["monthly_token_quota"] = sent["monthlyTokenQuota"]
        if "monthlyCostQuota" in sent:
            fields["monthly_cost_quota"] = sent["monthlyCostQuota"]
        if "mustChangePassword" in sent:
            fields["must_change_password"] = bool(sent["mustChangePassword"])
        if "password" in sent:
            fields["password_hash"] = auth.hash_password(sent["password"])
            fields["must_change_password"] = bool(sent.get("mustChangePassword", True))
        if fields:
            db.update_user(uid, **fields)

        # A credential or account change must invalidate existing browser sessions.
        if "password" in sent or sent.get("status") == "disabled":
            db.revoke_user_sessions(uid)

        updated = db.get_user(uid)
        assert updated is not None
        return {"user": _user_row_to_json(updated)}

    @app.get("/api/models")
    async def models(request: Request) -> dict[str, Any]:
        worker = await worker_for(request)
        return {"models": [model_to_json(model) for model in worker.state.registry.list_models()]}

    @app.get("/api/sessions")
    async def sessions(request: Request) -> dict[str, Any]:
        worker = await worker_for(request)
        # `agent_dir` is what keeps this list per-user: the same call in single mode falls
        # back to the default agent dir because the option is None there.
        listed = await SessionManager.list(worker.state.cwd, agent_dir=worker.options.agent_dir)
        return {
            "sessions": [
                {
                    "path": info.path,
                    "id": info.id,
                    "cwd": info.cwd,
                    "name": info.name,
                    "modified": info.modified.isoformat(),
                    "messageCount": info.message_count,
                    "firstMessage": info.first_message,
                }
                for info in listed
            ]
        }

    @app.get("/api/state")
    async def state(request: Request) -> dict[str, Any]:
        worker = await worker_for(request)
        return worker.state_payload()

    @app.websocket("/api/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        # The handshake is a same-origin request, so the session cookie is already here;
        # no credential needs to travel in the query string (where it would be logged).
        uid = LOCAL_UID
        if database is not None:
            user = auth.resolve_session(database, websocket.cookies.get(COOKIE_NAME))
            if user is None:
                await websocket.close(code=4401, reason="unauthenticated")
                return
            uid = user.uid
        try:
            worker = await registry.acquire(uid)
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
            connection.send(
                {
                    "type": "ready",
                    "version": __version__,
                    "cwd": worker.state.cwd,
                    "warnings": list(worker.warnings),
                }
            )
            connection.send({"type": "response", "command": "get_state", "success": True,
                             "data": worker.state_payload()})
            while True:
                text = await websocket.receive_text()
                worker.touch()
                worker.hub.touch(connection)
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


def _mount_spa(app: FastAPI) -> None:
    """Serve the built SPA, with a history fallback for client-side routes."""
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

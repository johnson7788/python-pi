"""Browser UI server: the pi command protocol over a WebSocket.

Three decisions shape this module.

**The command surface is reused, not reimplemented.**  ``RpcMode`` already owns the
30 commands, event projection and extension dialogs; it just happens to default to
stdio.  Handing it a different ``emit`` and feeding it WebSocket frames turns the
same object into a web server, so the browser speaks exactly the protocol pi's RPC
mode speaks — a client written against one works against the other.

**One shared session per server process.**  This is a local, single-user tool:
every tab should observe and drive the *same* conversation, and the REST endpoints
can then report real state instead of a guess.  Per-connection sessions would be
easier but would surprise anyone who opens a second tab.

**Events broadcast; dialogs do not.**  All clients see the transcript stream, but a
blocking ``confirm``/``select`` goes to the most recently active client only —
showing the same modal in three tabs and waiting on whichever answers first is
worse than asking once.  Ids are unique, so a stale reply from another tab is
simply ignored.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


class AgentServer:
    """Owns the single shared session and the controller that drives it."""

    def __init__(self, options: BootstrapOptions, *, on_warning: Any = None) -> None:
        self.options = options
        self.hub = Hub()
        self._on_warning = on_warning
        self._state: Any = None
        self._controller: RpcMode | None = None
        self._lock = asyncio.Lock()
        self.warnings: list[str] = []

    @property
    def state(self) -> Any:
        return self._state

    @property
    def controller(self) -> RpcMode:
        assert self._controller is not None, "server not started"
        return self._controller

    async def ensure_started(self) -> None:
        """Start the shared session on first use (so the server binds instantly)."""
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

    async def shutdown(self) -> None:
        if self._controller is not None:
            self._controller.close()
        if self._state is not None:
            await self._state.shutdown("server stopped")

    # -- payload builders --------------------------------------------------------------

    def state_payload(self) -> dict[str, Any]:
        """The same shape the ``get_state`` command returns."""
        controller = self.controller
        session = controller.session
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
            "cwd": self.state.cwd if self.state else None,
        }
        if snapshot["session_name"]:
            payload["sessionName"] = snapshot["session_name"]
        return payload


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
    server = AgentServer(options, on_warning=on_warning)
    server_config = config or ServerConfig.from_env()
    # Accounts only exist in team mode, so single mode does not create a database (or a
    # data root) behind the user's back — the personal flow stays byte-for-byte what it was.
    database: Database | None = None
    if server_config.requires_auth:
        server_config.ensure_layout()
        database = Database(server_config.db_path)
        database.purge_expired_sessions()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        try:
            yield
        finally:
            await server.shutdown()
            if database is not None:
                database.close()

    app = FastAPI(title="python-pi", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.server = server
    app.state.server_config = server_config
    app.state.db = database

    def require_team_mode() -> Database:
        if database is None:
            raise HTTPException(
                status_code=503,
                detail={"error": "accounts are disabled (auth mode is 'single')"},
            )
        return database

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
            "clients": server.hub.client_count,
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
    async def models() -> dict[str, Any]:
        await server.ensure_started()
        registry = server.state.registry
        return {"models": [model_to_json(model) for model in registry.list_models()]}

    @app.get("/api/sessions")
    async def sessions() -> dict[str, Any]:
        listed = await SessionManager.list(options.cwd)
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
    async def state() -> dict[str, Any]:
        await server.ensure_started()
        return server.state_payload()

    @app.websocket("/api/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        # The handshake is a same-origin request, so the session cookie is already here;
        # no credential needs to travel in the query string (where it would be logged).
        if database is not None:
            user = auth.resolve_session(database, websocket.cookies.get(COOKIE_NAME))
            if user is None:
                await websocket.close(code=4401, reason="unauthenticated")
                return
        connection = server.hub.add(websocket)
        # A single writer per connection keeps frames from interleaving.
        connection.sender = asyncio.create_task(_pump(connection))
        try:
            await server.ensure_started()
            connection.send(
                {
                    "type": "ready",
                    "version": __version__,
                    "cwd": server.state.cwd,
                    "warnings": list(server.warnings),
                }
            )
            connection.send({"type": "response", "command": "get_state", "success": True,
                             "data": server.state_payload()})
            while True:
                text = await websocket.receive_text()
                server.hub.touch(connection)
                await server.controller.handle_line(text)
        except WebSocketDisconnect:
            pass
        except Exception as error:  # noqa: BLE001 - one bad frame must not kill the server
            connection.send({"type": "response", "command": "server", "success": False,
                             "error": str(error)})
        finally:
            if connection.sender is not None:
                connection.sender.cancel()
            server.hub.remove(connection)

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
    "AgentServer",
    "Connection",
    "Hub",
    "create_app",
    "resolve_static_dir",
]

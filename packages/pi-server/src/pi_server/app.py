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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pi_coding_agent.bootstrap import BootstrapOptions, bootstrap
from pi_coding_agent.modes.jsonl import serialize_json_line
from pi_coding_agent.modes.rpc_mode import RpcMode, model_to_json
from pi_coding_agent.session_manager import SessionManager

__version__ = "0.1.0"

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


def create_app(options: BootstrapOptions, *, on_warning: Any = None) -> FastAPI:
    server = AgentServer(options, on_warning=on_warning)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        try:
            yield
        finally:
            await server.shutdown()

    app = FastAPI(title="python-pi", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.server = server

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "cwd": options.cwd,
            "clients": server.hub.client_count,
        }

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

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
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pi_coding_agent.bootstrap import BootstrapOptions, bootstrap
from pi_coding_agent.modes.jsonl import serialize_json_line
from pi_coding_agent.modes.rpc_mode import RpcMode, model_to_json
from pi_coding_agent.policy import POLICY_EXTENSION_PATH, PolicyConfig
from pi_coding_agent.session_manager import SessionManager

from . import auth
from .config import COOKIE_NAME, ServerConfig
from .db import Database, DuplicateUsername, month_start
from .workspace import Workspace, WorkspaceError

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
        policy_factory: Callable[["SessionWorker"], Any] | None = None,
        on_audit: Callable[[dict[str, Any]], int | None] | None = None,
        on_audit_finish: Callable[[int, bool, int], None] | None = None,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.uid = uid
        self.options = options
        self.hub = Hub()
        self.warnings: list[str] = []
        self.last_active = time.monotonic()
        self._on_warning = on_warning
        self._on_session_file = on_session_file
        self._policy_factory = policy_factory
        self._on_audit = on_audit
        self._on_audit_finish = on_audit_finish
        self._on_usage = on_usage
        #: tool_call_id -> (audit row id, monotonic start), so an execution outcome can be
        #: attached to the decision that authorised it.
        self._pending_audit: dict[str, tuple[int, float]] = {}
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
                emit=self._emit,
                host_mode="web",
            )
            # The policy goes in after ``RpcMode`` has bound the host, because binding is
            # what supplies the UI context a confirmation dialog needs.
            if self._policy_factory is not None and state.extension_runner is not None:
                policy = self._policy_factory(self)
                if policy is not None:
                    state.extension_runner.host.policy = policy

    # -- auditing ----------------------------------------------------------------------

    def _emit(self, payload: dict[str, Any]) -> None:
        """Every outbound record, observed for audit and usage, then routed to the tabs."""
        try:
            self._observe(payload)
        except Exception:  # noqa: BLE001 - bookkeeping must not be able to break the stream
            pass
        self.hub.emit_routed(payload)

    def _observe(self, payload: dict[str, Any]) -> None:
        kind = payload.get("type")
        if kind == "tool_execution_end":
            self._observe_tool_end(payload)
        elif kind == "message_end":
            self._observe_message_end(payload)
        elif kind == "response":
            self._observe_response(payload)

    def _record_usage(self, usage: Any, request_kind: str) -> None:
        """Persist one usage payload, if it reports anything at all."""
        if self._on_usage is None or not isinstance(usage, dict):
            return
        input_tokens = int(usage.get("input") or 0)
        output_tokens = int(usage.get("output") or 0)
        cost = float((usage.get("cost") or {}).get("total") or 0.0)
        if not (input_tokens or output_tokens or cost):
            return
        session_id = ""
        provider = model_id = ""
        try:
            session = self.controller.session
            session_id = session.state().get("session_id") or ""
            model = getattr(session, "model", None)
            provider = getattr(model, "provider", "") or ""
            model_id = getattr(model, "id", "") or ""
        except Exception:  # noqa: BLE001 - a torn-down session still gets billed
            pass
        self._on_usage(
            {
                "uid": self.uid,
                "session_id": session_id,
                "provider": provider,
                "model_id": model_id,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": int(usage.get("cacheRead") or 0),
                "cache_write_tokens": int(usage.get("cacheWrite") or 0),
                "cost": cost,
                "request_kind": request_kind,
            }
        )

    def _observe_message_end(self, payload: dict[str, Any]) -> None:
        """Record one model response's usage.

        ``message_end`` fires once per assistant message and carries that message's own
        usage, so this is per-response accounting rather than a running total — which is
        what makes the sum over a month meaningful.  ``turn_end`` carries the same message
        again and is deliberately ignored, or every response would be counted twice.
        """
        message = payload.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return
        self._record_usage(message.get("usage"), "turn")

    def _observe_response(self, payload: dict[str, Any]) -> None:
        """Record context compaction, which never produces a ``message_end``.

        Compaction is a full summarisation pass over the transcript, so it is one of the
        most expensive calls a session makes; leaving it out would understate usage in
        exactly the sessions that use the most tokens.
        """
        if not payload.get("success") or payload.get("command") != "compact":
            return
        data = payload.get("data")
        if isinstance(data, dict):
            self._record_usage(data.get("usage"), "compaction")

    def _observe_tool_end(self, payload: dict[str, Any]) -> None:
        call_id = payload.get("toolCallId") or payload.get("tool_call_id") or ""
        pending = self._pending_audit.pop(str(call_id), None)
        if pending is None or self._on_audit_finish is None:
            return
        row_id, started = pending
        self._on_audit_finish(
            row_id, bool(payload.get("isError")), int((time.monotonic() - started) * 1000)
        )

    def record_tool_decision(self, entry: dict[str, Any]) -> None:
        """The policy extension's audit sink.

        The decision is written as soon as it is made — a blocked call never reaches
        execution, so waiting for an outcome would lose exactly the rows that matter most.
        """
        if self._on_audit is None:
            return
        session_id = ""
        try:
            session_id = self.controller.session.state().get("session_id") or ""
        except Exception:  # noqa: BLE001 - a torn-down session still gets audited
            pass
        row_id = self._on_audit(
            {
                "uid": self.uid,
                "session_id": session_id,
                "tool_name": str(entry.get("tool_name") or ""),
                "tool_call_id": str(entry.get("tool_call_id") or ""),
                "decision": str(entry.get("decision") or ""),
                "reason": str(entry.get("reason") or ""),
                "input_preview": str(entry.get("input_preview") or ""),
            }
        )
        call_id = str(entry.get("tool_call_id") or "")
        if row_id and call_id:
            self._pending_audit[call_id] = (int(row_id), time.monotonic())

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
        policy_factory: Callable[[SessionWorker], Any] | None = None,
        on_audit: Callable[[dict[str, Any]], int | None] | None = None,
        on_audit_finish: Callable[[int, bool, int], None] | None = None,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.base_options = base_options
        self.config = config
        self._on_warning = on_warning
        self._on_session_file = on_session_file
        self._resume_lookup = resume_lookup
        self._policy_factory = policy_factory
        self._on_audit = on_audit
        self._on_audit_finish = on_audit_finish
        self._on_usage = on_usage
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
            # The governance extension is a *built-in* path, so it loads in exactly the
            # configuration that refuses per-user extension code.
            builtin_extension_paths=(POLICY_EXTENSION_PATH,),
            # The process environment holds the shared credential; without this any user
            # could read it back by asking the agent to run `env`.
            scrub_shell_env=True,
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
                policy_factory=self._policy_factory,
                on_audit=self._on_audit,
                on_audit_finish=self._on_audit_finish,
                on_usage=self._on_usage,
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


class WriteFileRequest(BaseModel):
    """The JSON half of ``PUT /api/workspace/file``; raw bytes are the other half."""

    content: str


class CreateDirectoryRequest(BaseModel):
    parentPath: str = "/"
    name: str


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
    # The credential can arrive as `--api-key` (legacy, visible in `ps`) or through
    # `PI_API_KEY`/`PI_API_KEY_FILE`. Filling it in here is what lets the personal flow stop
    # putting a secret in argv: the environment is readable only by the process owner.
    if server_config.api_key and not options.api_key:
        options = dataclasses.replace(options, api_key=server_config.api_key)
    # Accounts only exist in team mode, so single mode does not create a database (or a
    # data root) behind the user's back — the personal flow stays byte-for-byte what it was.
    database: Database | None = None
    if server_config.requires_auth:
        server_config.ensure_layout()
        database = Database(server_config.db_path)
        database.purge_expired_sessions()

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
            # Every user's workspace and transcript lives under here, so an agent that can
            # read it can read a colleague's conversations.
            protected_roots=(server_config.data_root,),
            confirm_timeout=server_config.confirm_timeout_seconds,
            record=worker.record_tool_decision,
        )

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
        policy_factory=_policy_for if database is not None else None,
        on_audit=_audit_sink if database is not None else None,
        on_audit_finish=_audit_finish if database is not None else None,
        on_usage=_usage_sink if database is not None else None,
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

    @app.exception_handler(WorkspaceError)
    async def _workspace_failed(_request: Request, error: WorkspaceError) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content={"error": error.message})

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

    # -- usage and quota ---------------------------------------------------------------

    @app.get("/api/usage/me")
    async def usage_me(
        user: auth.AuthUser = Depends(auth.get_required_user),
    ) -> dict[str, Any]:
        db = require_team_mode()
        since = month_start()
        row = db.get_user(user.uid)
        return {
            "windowStart": since,
            "summary": db.usage_summary(user.uid, since),
            "byDay": db.usage_by_day(user.uid, since),
            "byKind": db.usage_by_kind(user.uid, since),
            "bySession": db.usage_by_session(user.uid, since),
            "quota": {
                "tokens": row["monthly_token_quota"] if row is not None else None,
                "cost": row["monthly_cost_quota"] if row is not None else None,
            },
        }

    @app.get("/api/admin/usage")
    async def admin_usage(
        _actor: auth.AuthUser = Depends(auth.get_admin_user),
    ) -> dict[str, Any]:
        db = require_team_mode()
        since = month_start()
        return {"windowStart": since, "users": db.usage_all_users(since)}

    @app.get("/api/admin/audit")
    async def admin_audit(
        _actor: auth.AuthUser = Depends(auth.get_admin_user),
        uid: str | None = Query(None, description="Restrict to one account"),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        db = require_team_mode()
        return {"records": db.query_audit(uid=uid, limit=limit, offset=offset)}

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

    # -- workspace files ---------------------------------------------------------------

    def workspace_for(request: Request) -> Workspace:
        """The caller's workspace.

        Deliberately does **not** start an agent session: browsing files should not cost a
        bootstrap, and it must keep working while a session is being evicted.
        """
        if database is None:
            return Workspace(options.cwd, create=False)
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        root, _ = server_config.ensure_user_layout(user.uid)
        return Workspace(root)

    @app.get("/api/workspace/tree")
    async def workspace_tree(
        request: Request,
        path: str = Query("/", description="Workspace-relative directory"),
        recursive: bool = Query(False),
        files_only: bool = Query(False),
    ) -> dict[str, Any]:
        return workspace_for(request).tree(path, recursive=recursive, files_only=files_only)

    @app.get("/api/workspace/file")
    async def workspace_read(request: Request, path: str = Query(...)) -> dict[str, Any]:
        return workspace_for(request).read(path)

    @app.put("/api/workspace/file")
    async def workspace_write(request: Request, path: str = Query(...)) -> dict[str, Any]:
        """Write a file from either JSON (text edits) or raw bytes (uploads).

        One endpoint serves both so that a binary upload needs no multipart parser, and so
        the size cap and the path rules are applied exactly once.
        """
        body = await request.body()
        media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if media_type in ("application/json", "text/json"):
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError as error:
                raise HTTPException(
                    status_code=422, detail={"error": f"invalid JSON body: {error}"}
                ) from None
            content = payload.get("content") if isinstance(payload, dict) else None
            if not isinstance(content, str):
                raise HTTPException(
                    status_code=422, detail={"error": "JSON body must be {\"content\": \"...\"}"}
                )
            data = content.encode("utf-8")
        else:
            data = body
        return workspace_for(request).write(path, data)

    @app.delete("/api/workspace/file")
    async def workspace_delete(request: Request, path: str = Query(...)) -> dict[str, Any]:
        return workspace_for(request).delete(path)

    @app.post("/api/workspace/directory")
    async def workspace_mkdir(
        request: Request, payload: CreateDirectoryRequest
    ) -> dict[str, Any]:
        return workspace_for(request).create_directory(payload.parentPath, payload.name)

    @app.get("/api/workspace/download")
    async def workspace_download(request: Request, path: str = Query(...)) -> FileResponse:
        target, filename = workspace_for(request).download_target(path)
        return FileResponse(target, filename=filename)

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

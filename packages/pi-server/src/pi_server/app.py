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
import inspect
import io
import json
import os
import re
import shutil
import time
import tempfile
import base64
import shutil
import secrets
import uuid
import zipfile
from xml.etree import ElementTree
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pi_coding_agent.bootstrap import BootstrapOptions, bootstrap, resolve_project_trust
from pi_coding_agent.modes.jsonl import serialize_json_line
from pi_coding_agent.modes.rpc_mode import RpcMode, model_to_json
from pi_coding_agent.policy import POLICY_EXTENSION_PATH, PolicyConfig
from pi_coding_agent.resource_loader import ResourceLoader
from pi_coding_agent.settings import SettingsManager
from pi_coding_agent.session_manager import SessionManager, get_default_session_dir

from . import apps as app_catalog
from . import market as market_catalog
from . import office as office_store
from . import xlsx_sidecar as xlsx_engine
from . import plugins as plugin_catalog
from . import schedules as schedule_catalog
from . import auth
from .config import COOKIE_NAME, ServerConfig
from .db import Database, DuplicateUsername, DuplicateWorkspace, month_start
from .workspace import Workspace, WorkspaceError

__version__ = "0.1.0"

#: Shortest password the account endpoints will accept.  Length beats composition rules.
_MIN_PASSWORD_LENGTH = 8

#: The single implicit tenant in ``single`` mode.  Team mode never uses it.
LOCAL_UID = "local"

#: A user has at most one live worker, so one sandbox slot per user is enough.  Naming it
#: (rather than hashing something dynamic) is what makes the container id stable across an
#: idle eviction and restart, so a user's installed packages survive a quiet afternoon.
DEFAULT_SANDBOX_SLOT = "session"

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
        workspace: str = "default",
        app_id: str = "general",
        mode_id: str = "office",
        on_warning: Callable[[str], None] | None = None,
        on_session_file: Callable[[str, str, str, str, str], None] | None = None,
        policy_factory: Callable[["SessionWorker"], Any] | None = None,
        on_audit: Callable[[dict[str, Any]], int | None] | None = None,
        on_audit_finish: Callable[[int, bool, int], None] | None = None,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.uid = uid
        self.workspace = workspace
        self.app_id = app_id
        self.mode_id = mode_id
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
        """True while a turn or a delegated child is running.

        An idle timeout must never interrupt a turn, and a background subagent is
        exactly the kind of work a user expects to survive a quiet minute.
        """
        if self._controller is None:
            return False
        manager = getattr(self._state, "subagent_manager", None)
        if manager is not None and manager.has_running():
            return True
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
        """Record where the user was, then release the session object and its sandbox."""
        if self._on_session_file is not None:
            path = self.session_file()
            if path:
                self._on_session_file(self.uid, self.workspace, self.app_id, self.mode_id, path)
        controller, state = self._controller, self._state
        self._controller, self._state = None, None
        if controller is not None:
            controller.close()
        if state is not None:
            await state.shutdown(reason)
        await self._release_sandbox()

    async def _release_sandbox(self) -> None:
        """Drop the user's container when their session goes away.

        Best effort: the provisioner also reaps idle containers, because a server that dies
        cannot be relied on to have cleaned up.
        """
        backend = getattr(self.options, "sandbox_backend", None)
        release = getattr(backend, "release", None)
        if release is None:
            return
        try:
            result = release()
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - a stuck container must not block eviction
            pass

    async def run_prompt(self, text: str) -> str | None:
        """Run one headless turn; returns the session file once it settles."""
        await self.ensure_started()
        if self.busy:
            raise RuntimeError("worker is already running")
        await self.controller.handle_line(
            json.dumps({"type": "prompt", "id": "scheduled", "message": text})
        )
        await self.controller.wait_for_prompt()
        return self.session_file()

    async def run_scheduled_prompt(self, text: str) -> str | None:
        """Start a fresh conversation for one scheduled run, then prompt it.

        A schedule is a task generator, not a command sent to the currently open
        conversation: every run must produce its own session in the schedule history.
        """
        await self.ensure_started()
        if self.busy:
            raise RuntimeError("worker is already running")
        await self.controller.handle_line(
            json.dumps({"type": "new_session", "id": "scheduled-new"})
        )
        await self.controller.handle_line(
            json.dumps({"type": "prompt", "id": "scheduled", "message": text})
        )
        await self.controller.wait_for_prompt()
        return self.session_file()

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
        on_session_file: Callable[[str, str, str, str, str], None] | None = None,
        resume_lookup: Callable[[str, str, str, str], str | None] | None = None,
        disabled_skills_lookup: Callable[[str, str], Sequence[str]] | None = None,
        plugin_paths_lookup: Callable[[str, str], Sequence[str]] | None = None,
        composition_lookup: Callable[[str, str, str], tuple[str, tuple[str, ...] | None]] | None = None,
        workspace_path_lookup: Callable[[str, str], str | None] | None = None,
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
        self._disabled_skills_lookup = disabled_skills_lookup
        self._plugin_paths_lookup = plugin_paths_lookup
        self._composition_lookup = composition_lookup
        self._workspace_path_lookup = workspace_path_lookup
        self._policy_factory = policy_factory
        self._on_audit = on_audit
        self._on_audit_finish = on_audit_finish
        self._on_usage = on_usage
        self._workers: dict[tuple[str, str, str, str], SessionWorker] = {}
        self._lock = asyncio.Lock()

    # -- introspection ---------------------------------------------------------------

    @property
    def active_users(self) -> int:
        return len({uid for uid, _workspace, _app, _mode in self._workers})

    def worker(
        self,
        uid: str,
        workspace: str = "default",
        app_id: str = "general",
        mode_id: str = "office",
    ) -> SessionWorker | None:
        return self._workers.get((uid, workspace, app_id, mode_id))

    def workers_for(self, uid: str, workspace: str | None = None) -> list[SessionWorker]:
        return [
            worker
            for (worker_uid, worker_workspace, _app, _mode), worker in self._workers.items()
            if worker_uid == uid and (workspace is None or worker_workspace == workspace)
        ]

    def client_count(self) -> int:
        return sum(worker.hub.client_count for worker in self._workers.values())

    # -- lifecycle -------------------------------------------------------------------

    def backend_for(self, uid: str, workspace: str, workspace_id: str = "default") -> Any:
        """The execution backend for one user's session.

        The scope is per **user**, not per command: one container serves their conversation
        so a ``ls`` does not pay for a container start, and it is released when their worker
        is evicted.  The id is a function of the uid, so a user who comes back finds the
        same container if the provisioner has not reaped it yet.
        """
        if self.config.sandbox_backend in ("", "local", "process"):
            return None  # LocalBackend is the tool's own default; no need to construct one.
        from pi_coding_agent.sandbox import SandboxScope, host_path_for, select_backend

        host_workspace = host_path_for(
            workspace,
            app_root=self.config.data_root,
            host_root=self.config.host_data_root,
        )
        scope = SandboxScope(
            uid=uid,
            thread=DEFAULT_SANDBOX_SLOT if workspace_id == "default" else f"ws-{workspace_id}",
            workspace=workspace,
            host_workspace=host_workspace,
        )
        return select_backend(
            self.config.sandbox_backend,
            scope=scope,
            url=self.config.provisioner_url,
            token=self.config.provisioner_token,
            image=self.config.sandbox_image,
            network=self.config.sandbox_network,
            run_as=self.config.sandbox_run_as,
        )

    def options_for(
        self,
        uid: str,
        workspace_id: str = "default",
        app_id: str = "general",
        mode_id: str = "office",
    ) -> BootstrapOptions:
        """Per-user, per-workspace session options.  Single mode passes the command line through."""
        if not self.config.requires_auth:
            if workspace_id != "default":
                raise ValueError("workspaces require team mode")
            if self._composition_lookup is not None:
                app_prompt, app_skills = self._composition_lookup(LOCAL_UID, app_id, mode_id)
            else:
                app_prompt, app_skills = app_catalog.composition_for(app_id, mode_id)
            # Sandboxing is orthogonal to tenancy: a single-user install may still want its
            # commands contained, so the backend is attached in both modes.
            if self.config.sandbox_backend in ("", "local", "process"):
                return dataclasses.replace(
                    self.base_options,
                    app_prompt=app_prompt,
                    app_skills=app_skills,
                )
            return dataclasses.replace(
                self.base_options,
                sandbox_backend=self.backend_for(LOCAL_UID, self.base_options.cwd),
                app_prompt=app_prompt,
                app_skills=app_skills,
            )
        workspace, agent = self.config.ensure_user_layout(uid)
        if self._workspace_path_lookup is not None:
            looked_up = self._workspace_path_lookup(uid, workspace_id)
            if looked_up:
                workspace = looked_up
                os.makedirs(workspace, exist_ok=True)
        if workspace_id != "default" and self._workspace_path_lookup is None:
            workspace = self.config.ensure_workspace_path(uid, workspace_id)
        resume = (
            self._resume_lookup(uid, workspace_id, app_id, mode_id)
            if self._resume_lookup is not None
            else None
        )
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
            sandbox_backend=self.backend_for(uid, workspace, workspace_id),
            disabled_skills=(
                tuple(self._disabled_skills_lookup(uid, workspace_id))
                if self._disabled_skills_lookup is not None
                else ()
            ),
            # A tenant's catalog is the deployment template (symlinked into the agent
            # dir), never the server operator's personal ~/.agents/skills.
            include_home_skills=False,
            app_prompt=(
                self._composition_lookup(uid, app_id, mode_id)[0]
                if self._composition_lookup is not None
                else app_catalog.composition_for(app_id, mode_id)[0]
            ),
            app_skills=(
                self._composition_lookup(uid, app_id, mode_id)[1]
                if self._composition_lookup is not None
                else app_catalog.composition_for(app_id, mode_id)[1]
            ),
            plugin_paths=(
                tuple(self._plugin_paths_lookup(uid, app_id))
                if self._plugin_paths_lookup is not None
                else ()
            ),
        )

    async def acquire(
        self,
        uid: str,
        workspace: str = "default",
        app_id: str = "general",
        mode_id: str = "office",
    ) -> SessionWorker:
        """Return the worker for one user/workspace/composition, starting it if needed."""
        key = (uid, workspace, app_id, mode_id)
        async with self._lock:
            existing = self._workers.get(key)
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
                self.options_for(uid, workspace, app_id, mode_id),
                workspace=workspace,
                app_id=app_id,
                mode_id=mode_id,
                on_warning=self._on_warning,
                on_session_file=self._on_session_file,
                policy_factory=self._policy_factory,
                on_audit=self._on_audit,
                on_audit_finish=self._on_audit_finish,
                on_usage=self._on_usage,
            )
            self._workers[key] = worker

        # Started outside the registry lock: a bootstrap can take seconds, and holding the
        # lock would stall every other user for its duration.
        try:
            await worker.ensure_started()
        except BaseException as error:
            async with self._lock:
                self._workers.pop(key, None)
            if isinstance(error, SystemExit):
                raise BootstrapError(str(error)) from error
            raise
        worker.touch()
        return worker

    async def acquire_scheduled(
        self,
        uid: str,
        workspace: str = "default",
        app_id: str = "general",
        mode_id: str = "office",
    ) -> SessionWorker:
        """A disposable worker for one scheduled run.

        The user's interactive worker must not be switched to the scheduled session:
        a background run would otherwise replace the conversation they are reading.
        This worker is not registered, is not connected to any client, and is shut
        down by the schedule runner after its turn.
        """
        worker = SessionWorker(
            uid,
            self.options_for(uid, workspace, app_id, mode_id),
            workspace=workspace,
            app_id=app_id,
            mode_id=mode_id,
            on_warning=self._on_warning,
            # Do not let a background run become the user's interactive resume target.
            on_session_file=None,
            policy_factory=self._policy_factory,
            on_audit=self._on_audit,
            on_audit_finish=self._on_audit_finish,
            on_usage=self._on_usage,
        )
        try:
            await worker.ensure_started()
        except BaseException as error:
            await worker.shutdown("scheduled bootstrap failed")
            if isinstance(error, SystemExit):
                raise BootstrapError(str(error)) from error
            raise
        return worker

    async def _evict_idle_locked(self) -> None:
        timeout = self.config.session_idle_minutes * 60
        if timeout <= 0:
            return
        # Never evict the last worker: it is the one being asked for right now.
        for key, worker in list(self._workers.items()):
            if len(self._workers) <= 1:
                break
            if worker.busy or worker.idle_seconds < timeout:
                continue
            self._workers.pop(key, None)
            await worker.shutdown("idle timeout")

    async def evict_idle(self) -> int:
        """Drop sessions that have gone quiet.  Called periodically by the app."""
        async with self._lock:
            before = len(self._workers)
            await self._evict_idle_locked()
            return before - len(self._workers)

    async def evict_plugin(self, plugin_root: str) -> None:
        """Drop workers that have this plugin loaded, so an uninstall takes effect."""
        prefix = os.path.abspath(plugin_root)
        async with self._lock:
            keys = [
                key
                for key, worker in self._workers.items()
                if any(os.path.abspath(path).startswith(prefix) for path in worker.options.plugin_paths)
            ]
            workers = [self._workers.pop(key) for key in keys]
        for worker in workers:
            try:
                await worker.shutdown("plugin removed")
            except Exception:  # noqa: BLE001
                pass

    async def evict(self, uid: str, app_id: str | None = None) -> None:
        """Drop workers for a user (optionally one app) so the next request reloads config."""
        async with self._lock:
            keys = [
                key
                for key in self._workers
                if key[0] == uid and (app_id is None or key[2] == app_id)
            ]
            workers = [self._workers.pop(key) for key in keys]
        for worker in workers:
            try:
                await worker.shutdown("configuration changed")
            except Exception:  # noqa: BLE001
                pass

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


class RegisterRequest(BaseModel):
    username: str
    password: str
    displayName: str = ""


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


class SkillToggleRequest(BaseModel):
    enabled: bool


class CreateWorkspaceRequest(BaseModel):
    name: str


class PluginToggleRequest(BaseModel):
    enabled: bool
    appId: str | None = None


class CreateScheduleRequest(BaseModel):
    name: str
    prompt: str
    kind: str
    workspace: str = "default"
    appId: str = "general"
    modeId: str = "office"
    runAt: str | None = None
    hour: int | None = None
    minute: int | None = None
    weekday: int | None = None
    intervalMinutes: int | None = None


class UpdateScheduleRequest(BaseModel):
    enabled: bool


class MarketRatingRequest(BaseModel):
    score: int


class FeedbackRequest(BaseModel):
    sessionFile: str
    timestamp: float
    rating: int
    comment: str = ""


class AppUpsertRequest(BaseModel):
    name: str
    description: str = ""
    accent: str = "#3b5fba"
    modes: list[dict[str, Any]] = []


class RenameWorkspaceRequest(BaseModel):
    name: str


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

    sheets_sessions: dict[str, dict[str, Any]] = {}

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
            auto_approve=server_config.auto_approve_tools,
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
        plugin_paths_lookup=(
            (
                lambda uid, app_id: plugin_catalog.entry_paths(
                    server_config.plugin_root, database.enabled_plugins(uid, app_id)
                )
            )
            if database is not None
            else None
        ),
        composition_lookup=(lambda uid, app_id, mode_id: app_composition(uid, app_id, mode_id)),
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

    def schedule_uid(request: Request) -> str:
        if database is None:
            return LOCAL_UID
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        return user.uid

    def schedule_store() -> Database:
        if schedule_db is None:
            raise HTTPException(status_code=503, detail={"error": "scheduler is not configured"})
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
                raise HTTPException(status_code=404, detail={"error": f"unknown workspace: {workspace}"})
            return "default"
        ensure_default_workspace(uid)
        if database.get_workspace(uid, workspace) is None:
            raise HTTPException(status_code=404, detail={"error": f"unknown workspace: {workspace}"})
        return workspace

    def ensure_apps(uid: str) -> None:
        """Seed the built-in catalog into a tenant's editable rows on first use."""
        if database is None:
            return
        database.seed_apps(uid, app_catalog.catalog_json()["apps"])

    def apps_for(uid: str) -> list[dict[str, Any]]:
        ensure_apps(uid)
        if database is None:
            return app_catalog.catalog_json()["apps"]
        return database.list_apps(uid)

    def app_composition(uid: str, app_id: str, mode_id: str) -> tuple[str, tuple[str, ...] | None]:
        if database is not None:
            composition = database.app_composition(uid, app_id, mode_id)
            if composition is not None:
                return composition
        return app_catalog.composition_for(app_id, mode_id)

    def resolve_app_mode(uid: str, app_id: str | None, mode_id: str | None) -> tuple[str, str]:
        apps = apps_for(uid)
        wanted_app = app_id or app_catalog.DEFAULT_APP_ID
        app = next((item for item in apps if item["id"] == wanted_app), None)
        if app is None:
            raise HTTPException(status_code=404, detail={"error": f"unknown app: {wanted_app}"})
        wanted_mode = mode_id or (app["modes"][0]["id"] if app["modes"] else app_catalog.DEFAULT_MODE_ID)
        mode = next((item for item in app["modes"] if item["id"] == wanted_mode), None)
        if mode is None:
            raise HTTPException(
                status_code=404,
                detail={"error": f"unknown mode: {wanted_app}/{wanted_mode}"},
            )
        return str(app["id"]), str(mode["id"])

    def workspace_row(uid: str, workspace: str) -> Any:
        if database is None:
            raise HTTPException(status_code=503, detail={"error": "workspaces require team mode"})
        row = database.get_workspace(uid, workspace)
        if row is None:
            raise HTTPException(status_code=404, detail={"error": f"unknown workspace: {workspace}"})
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
                LOCAL_UID, request.query_params.get("app"), request.query_params.get("mode")
            )
            return await registry.acquire(LOCAL_UID, "default", app_id, mode_id)
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        workspace = resolve_workspace(user.uid, requested)
        app_id, mode_id = resolve_app_mode(
            user.uid, request.query_params.get("app"), request.query_params.get("mode")
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

    @app.post("/api/auth/register", status_code=201)
    async def register(payload: RegisterRequest) -> dict[str, Any]:
        """Open LAN registration: a new member creates their own account.

        Registration is intentionally available only in team mode.  It creates a
        member (never an administrator), and the chosen password is final — the
        user is not forced to change it again.
        """
        db = require_team_mode()
        username = payload.username.strip()
        display_name = payload.displayName.strip() or username
        if not 2 <= len(username) <= 64 or any(char.isspace() for char in username):
            raise HTTPException(status_code=422, detail={"error": "username must be 2-64 characters without spaces"})
        if len(payload.password) < _MIN_PASSWORD_LENGTH:
            raise HTTPException(status_code=422, detail={"error": f"password must be at least {_MIN_PASSWORD_LENGTH} characters"})
        try:
            uid = db.create_user(
                username=username,
                password_hash=auth.hash_password(payload.password),
                display_name=display_name,
                role="user",
                must_change_password=False,
            )
        except DuplicateUsername:
            raise HTTPException(status_code=409, detail={"error": "username already exists"}) from None
        ensure_default_workspace(uid)
        created = db.get_user(uid)
        assert created is not None
        return {"user": _user_row_to_json(created)}

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
            raise HTTPException(
                status_code=400,
                detail={"error": "skill preferences require team mode"},
            )
        uid, _cwd, _agent_dir, loader = skill_context(request, workspace)
        if all(skill.name != name for skill in loader.skills()):
            raise HTTPException(status_code=404, detail={"error": f"unknown skill: {name}"})
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

    # -- notifications ---------------------------------------------------------------

    @staticmethod
    def _notification_json(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "title": row["title"],
            "body": row["body"],
            "link": row["link"],
            "read": bool(row["read"]),
            "createdAt": row["created_at"],
        }

    @app.get("/api/notifications")
    async def list_notifications(request: Request) -> dict[str, Any]:
        db = schedule_store()
        uid = schedule_uid(request)
        return {
            "notifications": [
                _notification_json(row) for row in db.list_notifications(uid)
            ],
            "unread": db.unread_notifications(uid),
        }

    @app.post("/api/notifications/{notification_id}/read")
    async def read_notification(notification_id: str, request: Request) -> dict[str, Any]:
        db = schedule_store()
        uid = schedule_uid(request)
        db.mark_notification(uid, notification_id)
        return {"id": notification_id, "read": True}

    @app.post("/api/notifications/read-all")
    async def read_all_notifications(request: Request) -> dict[str, Any]:
        db = schedule_store()
        db.mark_all_notifications(schedule_uid(request))
        return {"read": True}

    # -- schedules -------------------------------------------------------------------

    @app.get("/api/schedules")
    async def list_schedules(request: Request) -> dict[str, Any]:
        db = schedule_store()
        uid = schedule_uid(request)
        return {
            "schedules": [
                schedule_catalog.schedule_json(row) for row in db.list_schedules(uid)
            ]
        }

    @app.get("/api/schedules/runs")
    async def list_schedule_runs(request: Request, limit: int = 50) -> dict[str, Any]:
        db = schedule_store()
        rows = db.list_user_schedule_runs(schedule_uid(request), limit)
        return {
            "runs": [
                {
                    **schedule_catalog.run_json(row),
                    "scheduleId": row["schedule_id"],
                    "scheduleName": row["schedule_name"],
                    "workspace": row["schedule_workspace"],
                    "appId": row["schedule_app_id"],
                    "modeId": row["schedule_mode_id"],
                }
                for row in rows
            ]
        }

    @app.post("/api/schedules", status_code=201)
    async def create_schedule(payload: CreateScheduleRequest, request: Request) -> dict[str, Any]:
        db = schedule_store()
        uid = schedule_uid(request)
        name = payload.name.strip()
        prompt = payload.prompt.strip()
        if not 1 <= len(name) <= 80 or not prompt:
            raise HTTPException(status_code=422, detail={"error": "name/prompt are required"})
        kind = payload.kind
        if kind not in ("once", "interval", "daily", "weekly"):
            raise HTTPException(status_code=422, detail={"error": f"unknown schedule kind: {kind}"})
        if kind in ("daily", "weekly") and (
            payload.hour is None or payload.minute is None
            or not 0 <= payload.hour <= 23 or not 0 <= payload.minute <= 59
        ):
            raise HTTPException(status_code=422, detail={"error": "hour/minute are required"})
        if kind == "weekly" and (payload.weekday is None or not 0 <= payload.weekday <= 6):
            raise HTTPException(status_code=422, detail={"error": "weekday is required"})
        if kind == "interval" and (
            payload.intervalMinutes is None or not 1 <= payload.intervalMinutes <= 10080
        ):
            raise HTTPException(status_code=422, detail={"error": "intervalMinutes must be 1-10080"})
        workspace = resolve_workspace(uid, payload.workspace)
        app_id, mode_id = resolve_app_mode(uid, payload.appId, payload.modeId)
        if kind == "once" and schedule_catalog.parse_iso(payload.runAt) is None:
            raise HTTPException(status_code=422, detail={"error": "runAt must be ISO-8601"})
        next_at = schedule_catalog.next_run_at(
            kind=kind,
            run_at=payload.runAt,
            hour=payload.hour,
            minute=payload.minute,
            weekday=payload.weekday,
            interval_minutes=payload.intervalMinutes,
        )
        if next_at is None:
            raise HTTPException(status_code=422, detail={"error": "runAt must be in the future"})
        schedule_id = db.create_schedule(
            uid=uid,
            workspace=workspace,
            app_id=app_id,
            mode_id=mode_id,
            name=name,
            prompt=prompt,
            kind=kind,
            run_at=payload.runAt,
            hour=payload.hour,
            minute=payload.minute,
            weekday=payload.weekday,
            interval_minutes=payload.intervalMinutes,
            next_run_at=next_at,
        )
        row = db.get_schedule(uid, schedule_id)
        return schedule_catalog.schedule_json(row)

    @app.patch("/api/schedules/{schedule_id}")
    async def update_schedule(
        schedule_id: str, payload: UpdateScheduleRequest, request: Request
    ) -> dict[str, Any]:
        db = schedule_store()
        uid = schedule_uid(request)
        row = db.get_schedule(uid, schedule_id)
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "unknown schedule"})
        next_at = None
        if payload.enabled:
            next_at = schedule_catalog.next_run_at(
                kind=str(row["kind"]),
                run_at=row["run_at"],
                hour=row["hour"],
                minute=row["minute"],
                weekday=row["weekday"],
                interval_minutes=row["interval_minutes"],
            )
            if next_at is None:
                raise HTTPException(status_code=422, detail={"error": "schedule has no future run"})
        db.set_schedule_enabled(uid, schedule_id, payload.enabled, next_at)
        return schedule_catalog.schedule_json(db.get_schedule(uid, schedule_id))

    @app.delete("/api/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str, request: Request) -> dict[str, Any]:
        db = schedule_store()
        uid = schedule_uid(request)
        if db.get_schedule(uid, schedule_id) is None:
            raise HTTPException(status_code=404, detail={"error": "unknown schedule"})
        db.delete_schedule(uid, schedule_id)
        return {"deleted": schedule_id}

    @app.post("/api/schedules/{schedule_id}/run", status_code=202)
    async def run_schedule(schedule_id: str, request: Request) -> dict[str, Any]:
        db = schedule_store()
        uid = schedule_uid(request)
        row = db.get_schedule(uid, schedule_id)
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "unknown schedule"})
        if schedule_runner is None:
            raise HTTPException(status_code=503, detail={"error": "scheduler is not running"})
        await schedule_runner.run_now(row)
        return {"accepted": True, "id": schedule_id}

    @app.get("/api/schedules/{schedule_id}/runs")
    async def schedule_runs(schedule_id: str, request: Request) -> dict[str, Any]:
        db = schedule_store()
        uid = schedule_uid(request)
        if db.get_schedule(uid, schedule_id) is None:
            raise HTTPException(status_code=404, detail={"error": "unknown schedule"})
        return {
            "runs": [
                schedule_catalog.run_json(row)
                for row in db.list_schedule_runs(schedule_id)
            ]
        }

    def _extract_plugin_zip(data: bytes, staging: str) -> Any:
        """Validate a plugin zip and extract it (safely) into ``staging``."""
        if not data or len(data) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail={"error": "plugin zip must be 1 byte - 10 MB"})
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                total = 0
                for info in archive.infolist():
                    name = info.filename.replace("\\", "/")
                    if name.startswith("/") or ".." in name.split("/"):
                        raise HTTPException(status_code=422, detail={"error": "zip contains an unsafe path"})
                    if (info.external_attr >> 16) & 0o170000 == 0o120000:
                        raise HTTPException(status_code=422, detail={"error": "zip contains a symlink"})
                    total += int(info.file_size)
                    if total > 20 * 1024 * 1024:
                        raise HTTPException(status_code=413, detail={"error": "uncompressed plugin is too large"})
                    target = os.path.join(staging, *name.split("/"))
                    if name.endswith("/"):
                        os.makedirs(target, exist_ok=True)
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with archive.open(info) as source, open(target, "wb") as destination:
                        shutil.copyfileobj(source, destination)
        except zipfile.BadZipFile:
            raise HTTPException(status_code=422, detail={"error": "not a valid zip file"}) from None

        found = plugin_catalog.scan(staging)
        if len(found) != 1:
            top = [item for item in os.listdir(staging) if os.path.isdir(os.path.join(staging, item))]
            if len(top) == 1:
                found = plugin_catalog.scan(os.path.join(staging, top[0]))
        if len(found) != 1:
            raise HTTPException(status_code=422, detail={"error": "zip must contain exactly one plugin.json"})
        return found[0]

    @app.post("/api/plugins/install", status_code=201)
    async def install_plugin(request: Request) -> dict[str, Any]:
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        if not user.is_admin:
            raise HTTPException(status_code=403, detail={"error": "only administrators can install plugins"})
        data = await request.body()
        root = server_config.plugin_root
        os.makedirs(root, exist_ok=True)
        staging = tempfile.mkdtemp(prefix=".install-", dir=root)
        try:
            info = _extract_plugin_zip(data, staging)
            target = os.path.join(root, info.id)
            if os.path.exists(target):
                backup = f"{target}.bak-{uuid.uuid4().hex[:6]}"
                os.rename(target, backup)
                try:
                    shutil.move(info.path, target)
                except Exception:
                    shutil.move(backup, target)
                    raise
                shutil.rmtree(backup, ignore_errors=True)
            else:
                shutil.move(info.path, target)
            return {**info.to_json(), "installed": True, "enabled": False}
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @app.post("/api/plugin-submissions", status_code=201)
    async def submit_plugin(request: Request) -> dict[str, Any]:
        """Any team member may submit; nothing is installed until an admin approves."""
        require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        data = await request.body()
        root = plugin_catalog.submissions_root(server_config.plugin_root)
        os.makedirs(root, exist_ok=True)
        sid = uuid.uuid4().hex[:12]
        staging = tempfile.mkdtemp(prefix=".submit-", dir=root)
        try:
            info = _extract_plugin_zip(data, staging)
            target = os.path.join(root, sid)
            os.makedirs(target)
            shutil.move(info.path, os.path.join(target, "plugin"))
            with open(
                os.path.join(target, plugin_catalog.SUBMISSION_META), "w", encoding="utf-8"
            ) as handle:
                json.dump(
                    {"submitter": user.username, "createdAt": time.time()},
                    handle,
                    ensure_ascii=False,
                )
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return {
            "id": sid,
            "pluginId": info.id,
            "name": info.name,
            "version": info.version,
            "submitted": True,
        }

    @app.get("/api/plugin-submissions")
    async def list_plugin_submissions(request: Request) -> dict[str, Any]:
        require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        if not user.is_admin:
            raise HTTPException(status_code=403, detail={"error": "only administrators can review submissions"})
        root = plugin_catalog.submissions_root(server_config.plugin_root)
        return {
            "submissions": [item.to_json() for item in plugin_catalog.scan_submissions(root)]
        }

    @app.post("/api/plugin-submissions/{submission_id}/approve")
    async def approve_plugin_submission(submission_id: str, request: Request) -> dict[str, Any]:
        require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        if not user.is_admin:
            raise HTTPException(status_code=403, detail={"error": "only administrators can approve submissions"})
        root = plugin_catalog.submissions_root(server_config.plugin_root)
        item = plugin_catalog.find_submission(root, submission_id)
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "unknown submission"})
        plugin_root = server_config.plugin_root
        os.makedirs(plugin_root, exist_ok=True)
        target = os.path.join(plugin_root, item.plugin.id)
        if os.path.exists(target):
            raise HTTPException(
                status_code=409,
                detail={"error": f"plugin {item.plugin.id} is already installed; uninstall it first"},
            )
        shutil.move(item.plugin.path, target)
        shutil.rmtree(item.base, ignore_errors=True)
        return {**item.plugin.to_json(), "installed": True, "enabled": False}

    @app.delete("/api/plugin-submissions/{submission_id}")
    async def reject_plugin_submission(submission_id: str, request: Request) -> dict[str, Any]:
        require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        if not user.is_admin:
            raise HTTPException(status_code=403, detail={"error": "only administrators can reject submissions"})
        root = plugin_catalog.submissions_root(server_config.plugin_root)
        item = plugin_catalog.find_submission(root, submission_id)
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "unknown submission"})
        shutil.rmtree(item.base, ignore_errors=True)
        return {"deleted": submission_id}

    @app.delete("/api/plugins/{plugin_id}")
    async def uninstall_plugin(plugin_id: str, request: Request) -> dict[str, Any]:
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        if not user.is_admin:
            raise HTTPException(status_code=403, detail={"error": "only administrators can uninstall plugins"})
        installed = {item.id: item for item in plugin_catalog.scan(server_config.plugin_root)}
        item = installed.get(plugin_id)
        if item is None:
            raise HTTPException(status_code=404, detail={"error": f"unknown plugin: {plugin_id}"})
        shutil.rmtree(item.path, ignore_errors=True)
        db.delete_plugin_enable(plugin_id)
        await registry.evict_plugin(item.path)
        return {"deleted": plugin_id}

    @app.get("/api/plugins")
    async def list_plugins(request: Request, app: str = Query("general")) -> dict[str, Any]:
        installed = plugin_catalog.scan(server_config.plugin_root)
        can_install = False
        if database is None:
            states: dict[str, bool] = {}
            can_manage = False
        else:
            user = auth.current_user(request)
            if user is None:
                raise auth.reject_unauthenticated()
            app_id, _mode_id = resolve_app_mode(user.uid, app, None)
            states = database.plugin_states(user.uid, app_id)
            can_manage = True
            can_install = bool(user.is_admin)
        return {
            "app": app,
            "canManage": can_manage,
            "canInstall": can_install,
            "plugins": [
                {
                    **info.to_json(),
                    "enabled": states.get(info.id, False),
                }
                for info in installed
            ],
        }

    @app.patch("/api/plugins/{plugin_id}/enable")
    async def toggle_plugin(
        plugin_id: str,
        body: PluginToggleRequest,
        request: Request,
        app: str = Query("general"),
    ) -> dict[str, Any]:
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        app_id, _mode_id = resolve_app_mode(user.uid, body.appId or app, None)
        if all(info.id != plugin_id for info in plugin_catalog.scan(server_config.plugin_root)):
            raise HTTPException(status_code=404, detail={"error": f"unknown plugin: {plugin_id}"})
        db.set_plugin_enabled(user.uid, app_id, plugin_id, body.enabled)
        # The next request/worker for this app reloads the plugin set; running turns are
        # allowed to finish on the old set rather than mutating tools mid-turn.
        await registry.evict(user.uid, app_id)
        return {"id": plugin_id, "enabled": body.enabled, "app": app_id}

    @app.get("/api/plugins/status")
    async def plugin_status(request: Request, app: str = Query("general")) -> dict[str, Any]:
        """What each installed plugin registered in this user's live worker.

        A plugin that failed to import never reaches a worker, so the loader's
        diagnostics are the only place the reason exists.  Without them the UI can
        only say "enabled", which is indistinguishable from "silently broken".
        """
        require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        app_id, _mode_id = resolve_app_mode(user.uid, app, None)

        workers = [
            worker
            for worker in registry.workers_for(user.uid)
            if worker.app_id == app_id and worker.started
        ]
        runner = workers[0].state.extension_runner if workers else None
        snapshot = runner.status() if runner is not None else {"extensions": [], "diagnostics": []}

        def _inside(raw: str, root: str) -> bool:
            try:
                return os.path.realpath(raw).startswith(root + os.sep)
            except (OSError, ValueError):
                return False

        plugins: list[dict[str, Any]] = []
        for info in plugin_catalog.scan(server_config.plugin_root):
            root = os.path.realpath(info.path)
            entries = [entry for entry in snapshot["extensions"] if _inside(entry["path"], root)]
            diagnostics = [
                item for item in snapshot["diagnostics"] if _inside(item["path"], root)
            ]
            plugins.append(
                {
                    "id": info.id,
                    "loaded": bool(entries),
                    "tools": sorted({name for entry in entries for name in entry["tools"]}),
                    "errors": [
                        item["message"] for item in diagnostics if item["severity"] == "error"
                    ],
                    "warnings": [
                        item["message"] for item in diagnostics if item["severity"] != "error"
                    ],
                }
            )
        return {"app": app_id, "runtimeLoaded": runner is not None, "plugins": plugins}

    def _safe_id(raw: Any, fallback: str) -> str:
        value = str(raw or "").strip()
        if value and len(value) <= 40 and all(char.isalnum() or char in "-_" for char in value):
            return value
        return fallback

    def normalize_app(payload: AppUpsertRequest, app_id: str) -> dict[str, Any]:
        name = payload.name.strip()
        if not 1 <= len(name) <= 40:
            raise HTTPException(status_code=422, detail={"error": "app name must be 1-40 characters"})
        modes: list[dict[str, Any]] = []
        seen_modes: set[str] = set()
        for mode_index, raw_mode in enumerate(payload.modes):
            mode_id = _safe_id(raw_mode.get("id"), f"mode-{mode_index + 1}")
            if mode_id in seen_modes:
                raise HTTPException(status_code=422, detail={"error": f"duplicate mode id: {mode_id}"})
            seen_modes.add(mode_id)
            mode_name = str(raw_mode.get("name") or mode_id).strip()[:40]
            prompt = str(raw_mode.get("prompt") or "")
            binding = raw_mode.get("skillBinding", "all")
            if binding != "all":
                if not isinstance(binding, list):
                    raise HTTPException(status_code=422, detail={"error": "skillBinding must be 'all' or a list"})
                binding = [str(item) for item in binding if str(item).strip()]
            capsules: list[dict[str, Any]] = []
            seen_capsules: set[str] = set()
            raw_capsules = raw_mode.get("capsules") or []
            if not isinstance(raw_capsules, list):
                raise HTTPException(status_code=422, detail={"error": "capsules must be a list"})
            for capsule_index, raw_capsule in enumerate(raw_capsules):
                capsule_id = _safe_id(raw_capsule.get("id"), f"capsule-{capsule_index + 1}")
                if capsule_id in seen_capsules:
                    raise HTTPException(status_code=422, detail={"error": f"duplicate capsule id: {capsule_id}"})
                seen_capsules.add(capsule_id)
                capsule_name = str(raw_capsule.get("name") or capsule_id).strip()[:40]
                if not capsule_name:
                    continue
                capsules.append(
                    {
                        "id": capsule_id,
                        "name": capsule_name,
                        "prompt": str(raw_capsule.get("prompt") or capsule_name),
                    }
                )
            modes.append(
                {
                    "id": mode_id,
                    "name": mode_name or mode_id,
                    "description": str(raw_mode.get("description") or "")[:200],
                    "prompt": prompt,
                    "skillBinding": binding,
                    "capsules": capsules,
                }
            )
        if not modes:
            raise HTTPException(status_code=422, detail={"error": "an app needs at least one mode"})
        return {
            "id": app_id,
            "name": name,
            "description": payload.description.strip()[:200],
            "accent": payload.accent if payload.accent.startswith("#") else "#3b5fba",
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
            raise HTTPException(status_code=403, detail={"error": "session file is outside the agent directory"})
        return candidate

    @app.post("/api/feedback")
    async def submit_feedback(payload: FeedbackRequest, request: Request) -> dict[str, Any]:
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        if payload.rating not in (-1, 1):
            raise HTTPException(status_code=422, detail={"error": "rating must be -1 or 1"})
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
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        if kind not in ("skill", "plugin"):
            raise HTTPException(status_code=422, detail={"error": "kind must be skill or plugin"})
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
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        item = market_catalog.find_item(kind, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "unknown market item"})
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
            raise HTTPException(status_code=404, detail={"error": "unknown market item"})
        if not 1 <= payload.score <= 5:
            raise HTTPException(status_code=422, detail={"error": "score must be 1-5"})
        db.set_market_rating(user.uid, kind, item_id, payload.score)
        average, count = db.market_rating_summary(kind, item_id)
        return {"average": average, "count": count, "mine": payload.score}

    @app.post("/api/market/skills/{item_id}/install")
    async def install_market_skill(item_id: str, request: Request) -> dict[str, Any]:
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        item = market_catalog.find_item("skill", item_id)
        if item is None or not item.content:
            raise HTTPException(status_code=404, detail={"error": "unknown skill"})
        _workspace, agent_dir = server_config.ensure_user_layout(user.uid)
        target = os.path.join(agent_dir, "skills", item.id)
        if os.path.exists(os.path.join(target, "SKILL.md")):
            raise HTTPException(status_code=409, detail={"error": "skill is already installed"})
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write(item.content)
        await registry.evict(user.uid)
        return {"id": item.id, "installed": True}

    @app.post("/api/market/plugins/{item_id}/install")
    async def install_market_plugin(item_id: str, request: Request) -> dict[str, Any]:
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        if not user.is_admin:
            raise HTTPException(status_code=403, detail={"error": "only administrators can install plugins"})
        item = market_catalog.find_item("plugin", item_id)
        if item is None or not item.package or not os.path.isdir(item.package):
            raise HTTPException(status_code=404, detail={"error": "plugin package is not bundled"})
        os.makedirs(server_config.plugin_root, exist_ok=True)
        target = os.path.join(server_config.plugin_root, item.id)
        if os.path.exists(target):
            raise HTTPException(status_code=409, detail={"error": "plugin is already installed"})
        shutil.copytree(
            item.package,
            target,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
        return {**market_catalog.item_json(item), "installed": True, "enabled": False}

    @app.get("/api/apps")
    async def list_apps(request: Request) -> dict[str, Any]:
        if database is None:
            catalog = app_catalog.catalog_json()
            return {**catalog, "canManage": False}
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        apps = apps_for(user.uid)
        first = apps[0] if apps else None
        default_app = app_catalog.DEFAULT_APP_ID if any(app["id"] == app_catalog.DEFAULT_APP_ID for app in apps) else (first["id"] if first else "")
        default_mode = ""
        if first is not None:
            chosen = next((app for app in apps if app["id"] == default_app), first)
            default_mode = chosen["modes"][0]["id"] if chosen["modes"] else ""
        return {
            "apps": apps,
            "defaultApp": default_app,
            "defaultMode": default_mode,
            "canManage": True,
        }

    @app.post("/api/apps", status_code=201)
    async def create_app_endpoint(payload: AppUpsertRequest, request: Request) -> dict[str, Any]:
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        app_id = f"app-{uuid.uuid4().hex[:8]}"
        if any(app["id"] == app_id for app in apps_for(user.uid)):
            raise HTTPException(status_code=409, detail={"error": "app id collision, retry"})
        app = normalize_app(payload, app_id)
        db.replace_app(user.uid, app)
        created = next(item for item in apps_for(user.uid) if item["id"] == app_id)
        db.record_app_audit(user.uid, app_id, "create", user.display_name or user.username, None, created)
        return created

    @app.put("/api/apps/{app_id}")
    async def update_app(app_id: str, payload: AppUpsertRequest, request: Request) -> dict[str, Any]:
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        existing = next((item for item in apps_for(user.uid) if item["id"] == app_id), None)
        if existing is None:
            raise HTTPException(status_code=404, detail={"error": f"unknown app: {app_id}"})
        app = normalize_app(payload, app_id)
        db.replace_app(user.uid, app, builtin=bool(existing.get("builtin")))
        updated = next(item for item in apps_for(user.uid) if item["id"] == app_id)
        db.record_app_audit(user.uid, app_id, "update", user.display_name or user.username, existing, updated)
        await registry.evict(user.uid, app_id)
        return updated

    @app.delete("/api/apps/{app_id}")
    async def delete_app(app_id: str, request: Request) -> dict[str, Any]:
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        existing = next((item for item in apps_for(user.uid) if item["id"] == app_id), None)
        if existing is None:
            raise HTTPException(status_code=404, detail={"error": f"unknown app: {app_id}"})
        if existing.get("builtin"):
            raise HTTPException(status_code=400, detail={"error": "built-in apps cannot be deleted"})
        db.delete_app(user.uid, app_id)
        db.record_app_audit(user.uid, app_id, "delete", user.display_name or user.username, existing, None)
        await registry.evict(user.uid, app_id)
        return {"deleted": app_id}

    @app.post("/api/apps/{app_id}/restore")
    async def restore_app(app_id: str, request: Request) -> dict[str, Any]:
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        existing = next((item for item in apps_for(user.uid) if item["id"] == app_id), None)
        if existing is None:
            raise HTTPException(status_code=404, detail={"error": f"unknown app: {app_id}"})
        if not existing.get("builtin"):
            raise HTTPException(status_code=400, detail={"error": "only built-in apps can be restored"})
        default = next(
            (item for item in app_catalog.catalog_json()["apps"] if item["id"] == app_id), None
        )
        if default is None:
            raise HTTPException(status_code=404, detail={"error": f"no built-in default: {app_id}"})
        db.replace_app(user.uid, default, builtin=True)
        restored = next(item for item in apps_for(user.uid) if item["id"] == app_id)
        db.record_app_audit(user.uid, app_id, "restore", user.display_name or user.username, existing, restored)
        await registry.evict(user.uid, app_id)
        return restored

    @app.get("/api/apps/{app_id}/audit")
    async def app_audit(app_id: str, request: Request) -> dict[str, Any]:
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        rows = db.list_app_audit(user.uid, app_id)
        if not rows and all(item["id"] != app_id for item in apps_for(user.uid)):
            raise HTTPException(status_code=404, detail={"error": f"unknown app: {app_id}"})
        return {
            "audit": [
                {
                    "id": row["id"],
                    "action": row["action"],
                    "actor": row["actor"],
                    "createdAt": row["created_at"],
                }
                for row in rows
            ]
        }

    # -- office editor ----------------------------------------------------------------

    def office_target(request: Request, doc_id: str, *, must_exist: bool = True) -> tuple[str, Any, str]:
        try:
            ref = office_store.decode_doc_id(doc_id)
        except office_store.OfficeIdError as error:
            raise HTTPException(status_code=400, detail={"error": str(error)}) from None
        if database is None:
            user_uid = LOCAL_UID
            resolve_workspace(LOCAL_UID, ref.workspace)
            root = options.cwd
        else:
            user = auth.current_user(request)
            if user is None:
                raise auth.reject_unauthenticated()
            user_uid = user.uid
            workspace = resolve_workspace(user.uid, ref.workspace)
            root = str(workspace_row(user.uid, workspace)["path"])
        workspace_obj = Workspace(root, create=False)
        try:
            path = workspace_obj.path_for(ref.path, must_exist=must_exist)
        except Exception as error:  # noqa: BLE001 - workspace errors are handled by the app
            raise HTTPException(status_code=404, detail={"error": str(error)}) from None
        return path, ref, root

    @app.get("/api/office/documents/{doc_id}")
    async def office_metadata(doc_id: str, request: Request) -> dict[str, Any]:
        path, ref, _root = office_target(request, doc_id)
        stat = os.stat(path)
        return {
            "id": doc_id,
            "name": os.path.basename(path),
            "path": ref.path,
            "size": stat.st_size,
            "modified": stat.st_mtime,
            "sha": office_store.sha256_file(path),
        }

    @app.get("/api/office/documents/{doc_id}/blob")
    async def office_blob(doc_id: str, request: Request) -> Response:
        path, _ref, _root = office_target(request, doc_id)
        with open(path, "rb") as handle:
            data = handle.read()
        return Response(content=data, media_type="application/octet-stream")

    @app.put("/api/office/documents/{doc_id}/blob")
    async def office_save(doc_id: str, request: Request) -> dict[str, Any]:
        path, ref, root = office_target(request, doc_id, must_exist=False)
        if database is not None:
            lock = database.office_lock(doc_id)
            supplied = (request.headers.get("x-office-token") or "").strip()
            if lock is not None and float(lock["expires_at"]) > time.time():
                user = auth.current_user(request)
                if user is None or lock["uid"] != user.uid:
                    raise HTTPException(status_code=409, detail={"error": "document is locked"})
        data = await request.body()
        if not data or len(data) > office_store.MAX_OFFICE_BYTES:
            raise HTTPException(status_code=413, detail={"error": "office file must be 1 byte - 50 MB"})
        if os.path.exists(path):
            current = office_store.sha256_file(path)
            expected = (request.headers.get("if-match") or "").strip().strip('"')
            if expected and expected != current:
                raise HTTPException(status_code=409, detail={"error": "document changed on disk"})
            office_store.backup_version(root, doc_id, path)
        office_store.atomic_write(path, data)
        return {
            "id": doc_id,
            "path": ref.path,
            "size": len(data),
            "sha": office_store.sha256_file(path),
        }

    @app.post("/api/office/documents/{doc_id}/lock")
    async def office_lock(doc_id: str, request: Request) -> dict[str, Any]:
        path, _ref, _root = office_target(request, doc_id)
        if database is None:
            return {"token": "single", "expiresAt": time.time() + 3600, "owner": "single"}
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        token = secrets.token_urlsafe(24)
        row = database.acquire_office_lock(doc_id, user.uid, token, ttl_seconds=120)
        if row["uid"] != user.uid:
            raise HTTPException(
                status_code=409,
                detail={"error": "document is being edited by another user", "owner": row["uid"]},
            )
        return {"token": token, "expiresAt": float(row["expires_at"]), "owner": user.uid}

    @app.delete("/api/office/documents/{doc_id}/lock")
    async def office_unlock(doc_id: str, request: Request) -> dict[str, Any]:
        office_target(request, doc_id, must_exist=False)
        if database is None:
            return {"released": True}
        token = (request.headers.get("x-office-token") or "").strip()
        return {"released": database.release_office_lock(doc_id, token)}

    @app.get("/api/office/documents/{doc_id}/versions")
    async def office_versions(doc_id: str, request: Request) -> dict[str, Any]:
        _path, _ref, root = office_target(request, doc_id)
        directory = Path(root) / ".pi" / "versions" / doc_id
        versions: list[dict[str, Any]] = []
        if directory.is_dir():
            for entry in sorted(directory.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
                if entry.is_file():
                    versions.append(
                        {
                            "id": entry.name,
                            "size": entry.stat().st_size,
                            "created": entry.stat().st_mtime,
                        }
                    )
        return {"versions": versions}

    @app.post("/api/office/documents/{doc_id}/versions/{version}/restore")
    async def office_restore(doc_id: str, version: str, request: Request) -> dict[str, Any]:
        path, ref, root = office_target(request, doc_id)
        if database is not None:
            lock = database.office_lock(doc_id)
            supplied = (request.headers.get("x-office-token") or "").strip()
            if lock is not None and float(lock["expires_at"]) > time.time():
                user = auth.current_user(request)
                if user is None or lock["uid"] != user.uid:
                    raise HTTPException(status_code=409, detail={"error": "document is locked"})
        directory = (Path(root) / ".pi" / "versions" / doc_id).resolve()
        candidate = (directory / version).resolve()
        if candidate.parent != directory or not candidate.is_file():
            raise HTTPException(status_code=404, detail={"error": "version not found"})
        office_store.backup_version(root, doc_id, path)
        office_store.atomic_write(path, candidate.read_bytes())
        return {
            "id": doc_id,
            "path": ref.path,
            "sha": office_store.sha256_file(path),
            "size": candidate.stat().st_size,
        }

    # -- xlsx sidecar sessions --------------------------------------------------------

    def sheet_uid(request: Request) -> str:
        if database is None:
            return LOCAL_UID
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        return user.uid

    def sheet_session(request: Request, session_id: str) -> dict[str, Any]:
        session = sheets_sessions.get(session_id)
        if session is None or session["uid"] != sheet_uid(request):
            raise HTTPException(status_code=404, detail={"error": "workbook session not found"})
        return session

    async def sheet_call(command: str, **fields: Any) -> Any:
        try:
            return await xlsx_engine.sidecar().request(command, **fields)
        except xlsx_engine.SidecarError as error:
            raise HTTPException(status_code=503, detail={"error": f"xlsx engine: {error}"}) from None

    @app.post("/api/office/sheets/open")
    async def sheets_open(request: Request) -> dict[str, Any]:
        data = await request.body()
        if not data or len(data) > office_store.MAX_OFFICE_BYTES:
            raise HTTPException(status_code=413, detail={"error": "workbook is empty or too large"})
        work_dir = Path(tempfile.mkdtemp(prefix="pi-xlsx-"))
        path = work_dir / "workbook.xlsx"
        path.write_bytes(data)
        result = await sheet_call("open", path=str(path))
        sheets_sessions[result["sessionId"]] = {
            "sessionId": result["sessionId"],
            "uid": sheet_uid(request),
            "dir": work_dir,
            "path": path,
        }
        return result

    @app.post("/api/office/sheets/read-range")
    async def sheets_read_range(request: Request) -> Any:
        body = await request.json()
        session = sheet_session(request, str(body.get("sessionId") or ""))
        return await sheet_call(
            "read_range",
            sessionId=session["sessionId"],
            sheetId=body.get("sheetId"),
            range=body.get("range"),
        )

    @app.post("/api/office/sheets/read-formulas")
    async def sheets_read_formulas(request: Request) -> Any:
        body = await request.json()
        session = sheet_session(request, str(body.get("sessionId") or ""))
        return await sheet_call(
            "read_formula_cells", sessionId=session["sessionId"], sheetId=body.get("sheetId")
        )

    @app.post("/api/office/sheets/read-media")
    async def sheets_read_media(request: Request) -> Any:
        body = await request.json()
        session = sheet_session(request, str(body.get("sessionId") or ""))
        return await sheet_call(
            "read_media", sessionId=session["sessionId"], visualId=body.get("visualId")
        )

    @app.post("/api/office/sheets/recalc")
    async def sheets_recalc(request: Request) -> Any:
        body = await request.json()
        session = sheet_session(request, str(body.get("sessionId") or ""))
        return await sheet_call(
            "recalc_cells",
            path=str(session["path"]),
            edits=body.get("edits") or [],
            reads=body.get("reads") or [],
        )

    @app.post("/api/office/sheets/manifest")
    async def sheets_manifest(request: Request) -> Any:
        body = await request.json()
        session = sheet_session(request, str(body.get("sessionId") or ""))
        return await sheet_call("archive_manifest", path=str(session["path"]))

    @app.post("/api/office/sheets/read-entries")
    async def sheets_read_entries(request: Request) -> Any:
        body = await request.json()
        session = sheet_session(request, str(body.get("sessionId") or ""))
        entries = body.get("entries") or []
        if not entries:
            return {"entries": []}
        out_dir = Path(tempfile.mkdtemp(prefix="pi-xlsx-read-", dir=session["dir"]))
        try:
            result = await sheet_call(
                "read_entries",
                path=str(session["path"]),
                entries=entries,
                outputDir=str(out_dir),
            )
            return {
                "entries": [
                    {
                        "name": entry["name"],
                        "contentB64": base64.b64encode(Path(entry["path"]).read_bytes()).decode(),
                    }
                    for entry in result.get("entries", [])
                ]
            }
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    @app.post("/api/office/sheets/scan-entries")
    async def sheets_scan_entries(request: Request) -> Any:
        body = await request.json()
        session = sheet_session(request, str(body.get("sessionId") or ""))
        return await sheet_call(
            "scan_entries",
            path=str(session["path"]),
            entries=body.get("entries") or [],
            needle=body.get("needle") or "",
        )

    @app.post("/api/office/sheets/save")
    async def sheets_save(request: Request) -> dict[str, Any]:
        body = await request.json()
        session = sheet_session(request, str(body.get("sessionId") or ""))
        doc_id = str(body.get("docId") or "")
        if not doc_id:
            raise HTTPException(status_code=422, detail={"error": "save requires an existing document"})
        path, _ref, root = office_target(request, doc_id, must_exist=True)
        if database is not None:
            lock = database.office_lock(doc_id)
            if lock is not None and float(lock["expires_at"]) > time.time() and lock["uid"] != sheet_uid(request):
                raise HTTPException(status_code=409, detail={"error": "document is locked"})

        def materialize(items: list[dict[str, Any]], tag: str) -> list[dict[str, str]]:
            output: list[dict[str, str]] = []
            for index, item in enumerate(items):
                target = Path(session["dir"]) / f"{tag}-{index}.bin"
                target.write_bytes(base64.b64decode(item.get("contentB64") or ""))
                output.append({"name": str(item.get("name") or ""), "contentPath": str(target)})
            return output

        replacements = materialize(body.get("replacements") or [], "replace")
        additions = materialize(body.get("additions") or [], "add")
        target = Path(session["dir"]) / "saved.xlsx"
        await sheet_call(
            "save_archive",
            sourcePath=str(session["path"]),
            targetPath=str(target),
            replacements=replacements,
            removals=body.get("removals") or [],
            additions=additions,
        )
        data = target.read_bytes()
        office_store.backup_version(root, doc_id, path)
        office_store.atomic_write(path, data)

        # Reset the sidecar session on the saved file, like AIOffice.
        await sheet_call("close", sessionId=session["sessionId"])
        reopened = await sheet_call("open", path=str(target))
        sheets_sessions.pop(session["sessionId"], None)
        sheets_sessions[reopened["sessionId"]] = {
            "sessionId": reopened["sessionId"],
            "uid": sheet_uid(request),
            "dir": session["dir"],
            "path": target,
        }
        return {
            "docId": doc_id,
            "size": len(data),
            "sha": office_store.sha256_file(path),
            "workbook": reopened,
        }

    @app.post("/api/office/sheets/close")
    async def sheets_close(request: Request) -> dict[str, Any]:
        body = await request.json()
        session = sheets_sessions.pop(str(body.get("sessionId") or ""), None)
        if session is not None:
            try:
                await sheet_call("close", sessionId=body.get("sessionId"))
            except HTTPException:
                pass
            shutil.rmtree(session["dir"], ignore_errors=True)
        return {"closed": True}

    @app.get("/api/workspaces")
    async def list_workspaces(request: Request) -> dict[str, Any]:
        if database is None:
            return {
                "workspaces": [
                    {"id": "default", "name": "默认工作空间", "isDefault": True, "path": options.cwd}
                ],
                "canCreate": False,
            }
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        ensure_default_workspace(user.uid)
        rows = database.list_workspaces(user.uid)
        return {
            "workspaces": [
                {
                    "id": str(row["id"]),
                    "name": str(row["name"]),
                    "isDefault": bool(row["is_default"]),
                    "path": str(row["path"]),
                }
                for row in rows
            ],
            "canCreate": True,
        }

    @app.post("/api/workspaces", status_code=201)
    async def create_workspace(payload: CreateWorkspaceRequest, request: Request) -> dict[str, Any]:
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        name = payload.name.strip()
        if not 1 <= len(name) <= 40:
            raise HTTPException(status_code=422, detail={"error": "workspace name must be 1-40 characters"})
        ensure_default_workspace(user.uid)
        workspace_id = uuid.uuid4().hex[:12]
        path = server_config.ensure_workspace_path(user.uid, workspace_id)
        try:
            db.create_workspace(user.uid, name, path, workspace_id)
        except DuplicateWorkspace:
            raise HTTPException(status_code=409, detail={"error": f"workspace already exists: {name}"}) from None
        return {"id": workspace_id, "name": name, "isDefault": False}

    @app.patch("/api/workspaces/{workspace_id}")
    async def rename_workspace(
        workspace_id: str, payload: RenameWorkspaceRequest, request: Request
    ) -> dict[str, Any]:
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        ensure_default_workspace(user.uid)
        if db.get_workspace(user.uid, workspace_id) is None:
            raise HTTPException(status_code=404, detail={"error": f"unknown workspace: {workspace_id}"})
        name = payload.name.strip()
        if not 1 <= len(name) <= 40:
            raise HTTPException(status_code=422, detail={"error": "workspace name must be 1-40 characters"})
        try:
            db.rename_workspace(user.uid, workspace_id, name)
        except DuplicateWorkspace:
            raise HTTPException(status_code=409, detail={"error": f"workspace already exists: {name}"}) from None
        return {"id": workspace_id, "name": name, "isDefault": workspace_id == "default"}

    @app.get("/api/sessions")
    async def sessions(request: Request) -> dict[str, Any]:
        worker = await worker_for(request)
        # `agent_dir` is what keeps this list per-user: the same call in single mode falls
        # back to the default agent dir because the option is None there.
        listed = await SessionManager.list(
            worker.state.cwd,
            agent_dir=worker.options.agent_dir,
            session_dir=worker.options.session_dir,
        )
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

    _ARTIFACT_EDITABLE = {
        "md", "mdx", "txt", "html", "htm", "csv", "tsv", "json", "yaml", "yml",
    }
    _ARTIFACT_EXTENSIONS = {
        "md": "document",
        "mdx": "document",
        "docx": "document",
        "txt": "document",
        "html": "document",
        "htm": "document",
        "csv": "sheet",
        "tsv": "sheet",
        "xlsx": "sheet",
        "xls": "sheet",
        "pptx": "slides",
        "ppt": "slides",
        "pdf": "document",
        "json": "data",
        "yaml": "data",
        "yml": "data",
        "png": "image",
        "jpg": "image",
        "jpeg": "image",
        "webp": "image",
        "svg": "image",
    }

    # -- workspace files ---------------------------------------------------------------

    def workspace_for(request: Request) -> Workspace:
        """The caller's workspace.

        Deliberately does **not** start an agent session: browsing files should not cost a
        bootstrap, and it must keep working while a session is being evicted.
        """
        if database is None:
            resolve_workspace(LOCAL_UID, request.query_params.get("workspace"))
            return Workspace(options.cwd, create=False)
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        workspace = resolve_workspace(user.uid, request.query_params.get("workspace"))
        row = workspace_row(user.uid, workspace)
        return Workspace(str(row["path"]))

    @app.get("/api/artifacts")
    async def list_artifacts(request: Request) -> dict[str, Any]:
        """Recent workspace files that look like deliverables, newest first."""
        workspace = workspace_for(request)
        tree = workspace.tree("/", recursive=True, files_only=True)
        artifacts: list[dict[str, Any]] = []
        for entry in tree["entries"]:
            if entry.get("hidden"):
                continue
            extension = str(entry.get("name", "")).rsplit(".", 1)[-1].lower()
            kind = _ARTIFACT_EXTENSIONS.get(extension)
            if kind is None:
                continue
            artifacts.append(
                {
                    "path": entry["path"],
                    "name": entry["name"],
                    "kind": kind,
                    "extension": extension,
                    "size": entry["size"],
                    "modified": entry["modified"],
                    "editable": extension in _ARTIFACT_EDITABLE,
                }
            )
        artifacts.sort(key=lambda item: float(item["modified"] or 0), reverse=True)
        return {"artifacts": artifacts[:40]}

    @app.get("/api/artifacts/pptx")
    async def preview_pptx(request: Request, path: str = Query(...)) -> dict[str, Any]:
        """Read-only slide text preview, extracted from the OOXML package.

        Deliberately no python-pptx dependency: the package is a zip of XML, and the
        preview only needs the text runs.  Editing stays a download-and-open workflow.
        """
        workspace = workspace_for(request)
        target, _name = workspace.download_target(path)
        try:
            with zipfile.ZipFile(target) as archive:
                names = [
                    name
                    for name in archive.namelist()
                    if re.fullmatch(r"ppt/slides/slide[0-9]+\.xml", name)
                ]
                names.sort(key=lambda name: int(re.search(r"slide([0-9]+)\.xml$", name).group(1)))
                slides: list[dict[str, Any]] = []
                for index, name in enumerate(names[:100], start=1):
                    root = ElementTree.fromstring(archive.read(name))
                    texts = [
                        node.text
                        for node in root.iter()
                        if node.tag.endswith("}t") and node.text and node.text.strip()
                    ]
                    slides.append({"number": index, "texts": texts[:80]})
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as error:
            raise HTTPException(status_code=422, detail={"error": f"not a readable pptx: {error}"}) from None
        return {"path": path, "slides": slides}

    @app.get("/api/artifacts/xlsx")
    async def preview_xlsx(request: Request, path: str = Query(...)) -> dict[str, Any]:
        """Read-only XLSX preview: sheet names and the first cells of each sheet."""
        workspace = workspace_for(request)
        target, _name = workspace.download_target(path)

        def local(tag: str) -> str:
            return tag.rsplit("}", 1)[-1]

        def column_index(reference: str) -> int:
            letters = "".join(char for char in reference if char.isalpha()).upper()
            index = 0
            for char in letters:
                index = index * 26 + (ord(char) - 64)
            return max(0, index - 1)

        try:
            with zipfile.ZipFile(target) as archive:
                names = set(archive.namelist())
                shared: list[str] = []
                if "xl/sharedStrings.xml" in names:
                    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                    for item in root.iter():
                        if local(item.tag) == "si":
                            shared.append("".join(node.text or "" for node in item.iter() if local(node.tag) == "t"))

                relations: dict[str, str] = {}
                if "xl/_rels/workbook.xml.rels" in names:
                    root = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
                    for item in root.iter():
                        if local(item.tag) == "Relationship":
                            relations[str(item.attrib.get("Id") or "")] = str(item.attrib.get("Target") or "")

                workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
                sheets: list[dict[str, Any]] = []
                for item in workbook.iter():
                    if local(item.tag) != "sheet":
                        continue
                    rel_id = next(
                        (value for key, value in item.attrib.items() if local(key) == "id"), ""
                    )
                    target_name = relations.get(rel_id, "")
                    if not target_name:
                        continue
                    sheet_path = target_name.lstrip("/")
                    if not sheet_path.startswith("xl/"):
                        sheet_path = f"xl/{sheet_path}"
                    if sheet_path not in names:
                        continue
                    sheet_root = ElementTree.fromstring(archive.read(sheet_path))
                    rows: list[list[str]] = []
                    for row in sheet_root.iter():
                        if local(row.tag) != "row":
                            continue
                        cells: dict[int, str] = {}
                        for cell in row:
                            if local(cell.tag) != "c":
                                continue
                            reference = str(cell.attrib.get("r") or "")
                            cell_type = str(cell.attrib.get("t") or "")
                            value = ""
                            if cell_type == "inlineStr":
                                value = "".join(
                                    node.text or "" for node in cell.iter() if local(node.tag) == "t"
                                )
                            else:
                                raw = next(
                                    (node.text or "" for node in cell.iter() if local(node.tag) == "v"),
                                    "",
                                )
                                if cell_type == "s" and raw.isdigit():
                                    index = int(raw)
                                    value = shared[index] if 0 <= index < len(shared) else ""
                                elif cell_type == "b":
                                    value = "TRUE" if raw == "1" else "FALSE"
                                else:
                                    value = raw
                            if value:
                                cells[column_index(reference)] = value
                        if cells:
                            width = min(max(cells) + 1, 30)
                            rows.append([cells.get(index, "") for index in range(width)])
                        if len(rows) >= 200:
                            break
                    sheets.append({"name": str(item.attrib.get("name") or sheet_path), "rows": rows})
                    if len(sheets) >= 8:
                        break
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as error:
            raise HTTPException(status_code=422, detail={"error": f"not a readable xlsx: {error}"}) from None
        return {"path": path, "sheets": sheets}

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
    async def workspace_download(
        request: Request, path: str = Query(...), inline: bool = Query(False)
    ) -> FileResponse:
        target, filename = workspace_for(request).download_target(path)
        return FileResponse(
            target,
            filename=filename,
            content_disposition_type="inline" if inline else "attachment",
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
                uid, websocket.query_params.get("app"), websocket.query_params.get("mode")
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

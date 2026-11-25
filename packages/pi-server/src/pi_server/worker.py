"""Live sessions: one worker per user/composition, and the fan-out to its tabs.

Split out of ``app.py`` so that module is about routes.  Nothing here knows about an
HTTP endpoint: a worker owns a bootstrapped session and a hub, and the router calls
into it.
"""

from __future__ import annotations

from .constants import BLOCKING_DIALOG_METHODS, DEFAULT_SANDBOX_SLOT, LOCAL_UID
import asyncio
import dataclasses
import inspect
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence
from fastapi import WebSocket
from pi_coding_agent.bootstrap import BootstrapOptions, bootstrap
from pi_coding_agent.modes.rpc_mode import RpcMode, model_to_json
from pi_coding_agent.policy import POLICY_EXTENSION_PATH
from . import apps as app_catalog
from .config import ServerConfig
from .runlog import RunLog
from .tracing import EventTracer, NullTracer, Tracer


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
        tracer: Tracer | None = None,
    ) -> None:
        self.uid = uid
        self.workspace = workspace
        self.app_id = app_id
        self.mode_id = mode_id
        self.options = options
        self.hub = Hub()
        #: Everything this session sent its tabs, with sequence numbers, so a tab
        #: that reloads mid-turn can be caught up instead of losing the partial.
        self.run_log = RunLog()
        #: Sequence of the last record whose message the session had already
        #: persisted.  Replay never goes below it: ``get_state`` covers that part.
        self.last_persisted_seq = 0
        self.warnings: list[str] = []
        self.last_active = time.monotonic()
        #: Spans are a diagnostic side channel: a null tracer (the default) accepts
        #: every call and writes nothing.
        self.tracer = tracer or NullTracer()
        self._event_tracer = EventTracer(self.tracer, uid=uid)
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
        runtime = getattr(self._state, "runtime", None)
        if runtime is not None and callable(getattr(runtime, "has_retiring_runs", None)):
            # A conversation the user navigated away from is still finishing; evicting
            # this worker now would abort work they expect to survive the click.
            if runtime.has_retiring_runs():
                return True
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

    def running_session_files(self) -> set[str]:
        """Absolute paths of this worker's conversations that are streaming now.

        Includes runs the user walked away from: the runtime retires them instead
        of cancelling, and the sidebar still has to draw their spinner.
        """
        if self._controller is None or self._state is None:
            return set()
        runtime = getattr(self._state, "runtime", None)
        getter = getattr(runtime, "running_session_files", None)
        if callable(getter):
            try:
                return set(getter())
            except Exception:  # noqa: BLE001 - a torn-down runtime is simply idle
                return set()
        try:
            session = self._controller.session
            if session.is_streaming and session.session_file:
                return {os.path.realpath(session.session_file)}
        except Exception:  # noqa: BLE001
            pass
        return set()

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
                app_id=self.app_id,
                mode_id=self.mode_id,
            )
            # A browser tab is the only possible answerer here. Before the first
            # tab exists the count is zero, so a scheduled run that tries to ask
            # fails fast instead of parking a turn nobody can see.
            self._controller.set_client_count(lambda: self.hub.client_count)
            # A conversation left mid-turn keeps streaming after its events stopped
            # reaching the tabs; this is the one notification that it has finished.
            set_settled = getattr(state.runtime, "set_on_background_settled", None)
            if callable(set_settled):
                set_settled(self._notify_background_settled)
            # The policy goes in after ``RpcMode`` has bound the host, because binding is
            # what supplies the UI context a confirmation dialog needs.
            if self._policy_factory is not None and state.extension_runner is not None:
                policy = self._policy_factory(self)
                if policy is not None:
                    state.extension_runner.host.policy = policy

    # -- auditing ----------------------------------------------------------------------

    def _emit(self, payload: dict[str, Any]) -> None:
        """Every outbound record: stamp it, observe it, then route it to the tabs.

        The sequence number is assigned before anything can fail, so a record that
        is dropped by a broken audit hook still occupies its place in the replay
        order and the client's offset stays consistent.
        """
        self._event_tracer.session_file = self.session_file() or ""
        self._event_tracer.observe(payload)
        seq = self.run_log.append(payload)
        if payload.get("type") == "message_end":
            role = (payload.get("message") or {}).get("role")
            if role in ("user", "assistant", "toolResult"):
                # The session persists on message_end before the record is emitted,
                # so this is exactly the point ``get_state`` is current to.
                self.last_persisted_seq = seq
        try:
            self._observe(payload)
        except Exception:  # noqa: BLE001 - bookkeeping must not be able to break the stream
            pass
        self.hub.emit_routed(payload)

    def _notify_background_settled(self, session_path: str) -> None:
        """Tell the tabs that a conversation abandoned mid-turn has finished.

        The client turns this into the sidebar's green "done, unread" dot. It is an
        extra record on the same socket, not a protocol command, so an older client
        simply ignores it.
        """
        self._emit(
            {
                "type": "session_activity",
                "sessionFile": session_path,
                "running": False,
            }
        )

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
            # Questions that can still be answered: derived from the transcript, so
            # a reconnecting tab restores them without any extra bookkeeping.
            "pendingQuestions": session.question_broker.pending_view(),
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
        composition_lookup: Callable[[str, str], tuple[str, tuple[str, ...] | None]] | None = None,
        workspace_path_lookup: Callable[[str, str], str | None] | None = None,
        policy_factory: Callable[[SessionWorker], Any] | None = None,
        on_audit: Callable[[dict[str, Any]], int | None] | None = None,
        on_audit_finish: Callable[[int, bool, int], None] | None = None,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
        tracer: Tracer | None = None,
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
        self._tracer = tracer
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
                app_prompt, app_skills = self._composition_lookup(app_id, mode_id)
            else:
                app_prompt, app_skills = app_catalog.composition_for(app_id, mode_id)
            # Sandboxing is orthogonal to tenancy: a single-user install may still want its
            # commands contained, so the backend is attached in both modes.
            if self.config.sandbox_backend in ("", "local", "process"):
                return dataclasses.replace(
                    self.base_options,
                    app_prompt=app_prompt,
                    app_skills=app_skills,
                    app_id=app_id,
                    mode_id=mode_id,
                    plugin_paths=(
                        tuple(self._plugin_paths_lookup(uid, app_id))
                        if self._plugin_paths_lookup is not None
                        else ()
                    ),
                )
            return dataclasses.replace(
                self.base_options,
                sandbox_backend=self.backend_for(LOCAL_UID, self.base_options.cwd),
                app_prompt=app_prompt,
                app_skills=app_skills,
                app_id=app_id,
                mode_id=mode_id,
                plugin_paths=(
                    tuple(self._plugin_paths_lookup(uid, app_id))
                    if self._plugin_paths_lookup is not None
                    else ()
                ),
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
                self._composition_lookup(app_id, mode_id)[0]
                if self._composition_lookup is not None
                else app_catalog.composition_for(app_id, mode_id)[0]
            ),
            app_id=app_id,
            mode_id=mode_id,
            app_skills=(
                self._composition_lookup(app_id, mode_id)[1]
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
            users = {key[0] for key in self._workers}
            if uid not in users and len(users) >= self.config.max_active_users:
                raise CapacityError(
                    f"the server is already serving {len(users)} users; try again shortly"
                )
            # A user pins one worker per composition; bound that separately from the
            # deployment-wide user cap, and recycle their least recently used idle one.
            evicted: list[SessionWorker] = []
            mine = [key for key in self._workers if key[0] == uid]
            if len(mine) >= self.config.max_workers_per_user:
                idle = sorted(
                    (
                        (self._workers[key].last_active, key)
                        for key in mine
                        if not self._workers[key].busy
                    ),
                    key=lambda item: item[0],
                )
                if not idle:
                    raise CapacityError(
                        "this account already has the maximum number of active sessions; "
                        "finish one before opening another"
                    )
                evicted.append(self._workers.pop(idle[0][1]))

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

        for stale in evicted:
            try:
                await stale.shutdown("worker limit")
            except Exception:  # noqa: BLE001
                pass

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
        await self._recycle_idle_siblings(uid, workspace, key)
        return worker

    async def _recycle_idle_siblings(
        self, uid: str, workspace: str, keep: tuple[str, str, str, str]
    ) -> None:
        """Drop this user's other compositions in the same workspace.

        Each (app, mode) has its own worker, so switching apps would otherwise pin one
        worker per composition until the 30-minute idle sweep.  A worker is only
        recycled when it is idle *and* has no connected tab: a second browser tab on
        another app keeps its own worker.
        """
        stale: list[SessionWorker] = []
        async with self._lock:
            for key, worker in list(self._workers.items()):
                if key == keep or key[0] != uid or key[1] != workspace:
                    continue
                if worker.busy or worker.hub.client_count > 0:
                    continue
                stale.append(self._workers.pop(key))
        for worker in stale:
            try:
                await worker.shutdown("another composition became active")
            except Exception:  # noqa: BLE001
                pass

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
            tracer=self._tracer,
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

    async def evict_app(self, app_id: str) -> None:
        """Apps are team-wide: drop every user's worker for this app."""
        async with self._lock:
            keys = [key for key in self._workers if key[2] == app_id]
            workers = [self._workers.pop(key) for key in keys]
        for worker in workers:
            try:
                await worker.shutdown("app changed")
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


"""RPC mode: headless operation over LF-delimited JSONL on stdin/stdout.

Port of ``packages/coding-agent/src/modes/rpc/rpc-mode.ts``.

Protocol shape:

* commands arrive on stdin, one JSON object per line;
* every command may carry an ``id``, echoed back on its response so a client can
  correlate concurrent traffic;
* responses are ``{"type":"response","command":...,"success":...}`` with optional
  ``data`` or ``error``;
* agent events are streamed to stdout interleaved with responses, using the same
  projection as JSON mode (:mod:`pi_coding_agent.modes.json_event`).

Two behaviours are easy to get wrong and are preserved deliberately:

* ``prompt`` answers **at preflight**, not on completion — the response means
  "accepted, queued, or handled", and everything after that arrives as events;
* a malformed line produces a ``parse`` error response rather than killing the
  session, and an unknown command type produces an error response too.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, BinaryIO, TextIO

from pi_ai import Model, get_supported_thinking_levels

from ..agent_session import AgentSession
from ..resource_loader import ResourceLoader
from ..session_manager import entry_to_json
from .json_event import to_json_event
from .jsonl import JsonlLineReader, write_json_line

_UNSET = object()

#: Commands this port implements.  Anything else gets an error response that
#: names the command, so a client can degrade instead of hanging.
SUPPORTED_COMMANDS = frozenset(
    {
        "prompt",
        "steer",
        "follow_up",
        "abort",
        "clear_queue",
        "new_session",
        "switch_session",
        "fork",
        "clone",
        "get_state",
        "get_messages",
        "set_model",
        "cycle_model",
        "get_available_models",
        "set_thinking_level",
        "cycle_thinking_level",
        "get_available_thinking_levels",
        "set_steering_mode",
        "set_follow_up_mode",
        "compact",
        "set_auto_compaction",
        "set_auto_retry",
        "abort_retry",
        "get_session_stats",
        "get_entries",
        "get_tree",
        "get_last_assistant_text",
        "get_fork_messages",
        "set_session_name",
        "get_commands",
    }
)

#: Commands pi defines that this port cannot honour, with the reason reported back.
UNSUPPORTED_COMMANDS: dict[str, str] = {
    "export_html": "HTML export is not implemented in this port yet",
    "bash": "user bash execution is not implemented in this port yet",
    "abort_bash": "user bash execution is not implemented in this port yet",
}


# --------------------------------------------------------------------------------------
# Response helpers
# --------------------------------------------------------------------------------------


def success(request_id: str | None, command: str, data: Any = _UNSET) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "response", "command": command, "success": True}
    if request_id is not None:
        payload["id"] = request_id
    if data is not _UNSET:
        payload["data"] = data
    return payload


def failure(request_id: str | None, command: str, message: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "response",
        "command": command,
        "success": False,
        "error": message,
    }
    if request_id is not None:
        payload["id"] = request_id
    return payload


# --------------------------------------------------------------------------------------
# Serialization helpers
# --------------------------------------------------------------------------------------


def cost_to_json(cost: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "input": cost.input,
        "output": cost.output,
        "cacheRead": cost.cache_read,
        "cacheWrite": cost.cache_write,
    }
    if cost.tiers:
        payload["tiers"] = [
            {
                "input": tier.input,
                "output": tier.output,
                "cacheRead": tier.cache_read,
                "cacheWrite": tier.cache_write,
                "inputTokensAbove": tier.input_tokens_above,
            }
            for tier in cost.tiers
        ]
    return payload


def model_to_json(model: Model) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": model.id,
        "name": model.name,
        "api": model.api,
        "provider": model.provider,
        "baseUrl": model.base_url,
        "reasoning": model.reasoning,
        "input": list(model.input),
        "cost": cost_to_json(model.cost),
        "contextWindow": model.context_window,
        "maxTokens": model.max_tokens,
    }
    if model.thinking_level_map:
        payload["thinkingLevelMap"] = dict(model.thinking_level_map)
    return payload


def usage_to_json(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    return {
        "input": usage.input,
        "output": usage.output,
        "cacheRead": usage.cache_read,
        "cacheWrite": usage.cache_write,
        "totalTokens": usage.total_tokens,
        "cost": {
            "input": usage.cost.input,
            "output": usage.cost.output,
            "cacheRead": usage.cost.cache_read,
            "cacheWrite": usage.cost.cache_write,
            "total": usage.cost.total,
        },
    }


def _tree_node_to_json(node: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "entry": entry_to_json(node.entry),
        "children": [_tree_node_to_json(child) for child in node.children],
    }
    if node.label is not None:
        payload["label"] = node.label
    if node.label_timestamp is not None:
        payload["labelTimestamp"] = node.label_timestamp
    return payload


def _text_of(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return ""
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "".join(parts)


# --------------------------------------------------------------------------------------
# The mode
# --------------------------------------------------------------------------------------


class RpcMode:
    """Reads commands, drives one :class:`AgentSession`, writes responses/events."""

    def __init__(
        self,
        session: AgentSession | None = None,
        *,
        runtime: Any = None,
        model_registry: Any = None,
        resource_loader: ResourceLoader | None = None,
        extension_runner: Any = None,
        output: TextIO | None = None,
        emit: Any = None,
        host_mode: str = "rpc",
    ) -> None:
        if session is None and runtime is None:
            raise ValueError("RpcMode needs either a session or an AgentSessionRuntime")

        self._static_session = session
        self._runtime = runtime
        self.model_registry = model_registry
        self.resource_loader = resource_loader
        self._extension_runner = extension_runner
        self.output: TextIO = output if output is not None else sys.stdout
        #: When supplied, outbound records go here instead of stdout.  This is what
        #: lets a WebSocket server reuse this whole command surface.
        self._emit_override = emit
        self._prompt_task: asyncio.Task | None = None
        self._unsubscribe: Any = None
        self._unsubscribe_errors: Any = None
        self._ui: Any = None

        if extension_runner is not None:
            # A transport with a real user on the other end is the only place where
            # `ctx.has_ui` is true and dialogs may block.
            from ..extensions.rpc_ui import RpcUIContext

            self._ui = RpcUIContext(self._emit)
            host = extension_runner.host
            host.ui = self._ui
            host.has_ui = True
            host.mode = host_mode
            extension_runner.bind_host(host)
            self._unsubscribe_errors = extension_runner.on_error(self._on_extension_error)

        self._bind_session()
        if runtime is not None:
            # Replacement swaps the session object; re-attach or events stop flowing.
            runtime.set_rebind_session(self._rebind_session)

    @property
    def session(self) -> AgentSession:
        if self._runtime is not None:
            return self._runtime.session
        assert self._static_session is not None
        return self._static_session

    # -- plumbing ----------------------------------------------------------------------

    def _bind_session(self) -> None:
        self._unsubscribe = self.session.subscribe(self._on_session_event)

    async def _rebind_session(self, session: AgentSession) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
        self._unsubscribe = session.subscribe(self._on_session_event)

    def _emit(self, payload: Any) -> None:
        if self._emit_override is not None:
            self._emit_override(payload)
            return
        write_json_line(self.output, payload)

    def _on_extension_error(self, record: Any) -> None:
        """Surface a failed extension handler to the client (upstream does the same)."""
        self._emit(
            {
                "type": "extension_error",
                "extensionPath": record.extension_path,
                "event": record.event,
                "error": record.error,
            }
        )

    def _on_session_event(self, event: Any) -> None:
        try:
            self._emit(to_json_event(event))
        except Exception as error:  # noqa: BLE001 - never break the stream on one event
            print(f"pi-py: could not serialize event {getattr(event, 'type', '?')}: {error}", file=sys.stderr)

    def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._unsubscribe_errors is not None:
            self._unsubscribe_errors()
            self._unsubscribe_errors = None
        if self._ui is not None:
            # Unblock any handler parked on a dialog; the client is gone.
            self._ui.cancel_all("session ended")

    async def run(self, input_stream: BinaryIO) -> int:
        """Read commands until EOF, then shut down.  (The stdio transport.)"""
        try:
            async for line in JsonlLineReader(input_stream):
                if not line.strip():
                    continue
                await self.handle_line(line)
        finally:
            self.close()
            await self._settle()
        return 0

    async def _settle(self) -> None:
        if self._prompt_task is not None and not self._prompt_task.done():
            self._prompt_task.cancel()
            try:
                await self._prompt_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def handle_line(self, line: str) -> None:
        """Handle one already-framed record.

        Public because the framing is transport-specific: stdio feeds LF-delimited
        lines from :meth:`run`, while a WebSocket server feeds one message per frame.
        """
        try:
            parsed = json.loads(line)
        except Exception as error:  # noqa: BLE001 - a bad line must not kill the session
            self._emit(failure(None, "parse", f"Failed to parse command: {error}"))
            return

        if not isinstance(parsed, dict):
            self._emit(failure(None, "parse", "Command must be a JSON object"))
            return

        # Extension UI replies are not commands: they complete a pending dialog and
        # produce no response of their own.
        if parsed.get("type") == "extension_ui_response":
            if self._ui is not None:
                self._ui.resolve(parsed)
            return

        command_type = parsed.get("type")
        request_id = parsed.get("id") if isinstance(parsed.get("id"), str) else None

        try:
            response = await self._handle_command(parsed)
        except Exception as error:  # noqa: BLE001 - reported as a failed response
            self._emit(failure(request_id, str(command_type), str(error)))
            return

        if response is not None:
            self._emit(response)

    # -- dispatch ----------------------------------------------------------------------

    async def _handle_command(self, command: dict[str, Any]) -> dict[str, Any] | None:
        kind = command.get("type")
        request_id = command.get("id") if isinstance(command.get("id"), str) else None

        if not isinstance(kind, str):
            return failure(request_id, "parse", "Command is missing a string 'type'")

        handler = getattr(self, f"_cmd_{kind}", None)
        if handler is None:
            if kind in UNSUPPORTED_COMMANDS:
                return failure(request_id, kind, f"{kind} is not supported: {UNSUPPORTED_COMMANDS[kind]}")
            return failure(request_id, kind, f"Unknown command: {kind}")

        return await handler(command, request_id)

    # -- prompting ---------------------------------------------------------------------

    async def _cmd_prompt(self, command: dict[str, Any], request_id: str | None):
        message = command.get("message")
        if not isinstance(message, str) or not message:
            return failure(request_id, "prompt", "prompt requires a non-empty string 'message'")

        images = command.get("images")
        behavior = command.get("streamingBehavior")
        if behavior not in (None, "steer", "followUp"):
            return failure(request_id, "prompt", f"Invalid streamingBehavior: {behavior!r}")

        task = asyncio.ensure_future(
            self.session.prompt(message, images=images, streaming_behavior=behavior)
        )
        self._prompt_task = task
        task.add_done_callback(self._consume_prompt_result)

        # Let prompt() run its synchronous preflight (disposed / auth / streaming
        # checks) before acknowledging; the contract is "accepted, queued, or
        # handled", not "finished".
        for _ in range(4):
            if task.done():
                break
            await asyncio.sleep(0)

        if task.done():
            if task.cancelled():
                return failure(request_id, "prompt", "prompt was cancelled")
            error = task.exception()
            if error is not None:
                return failure(request_id, "prompt", str(error))
        return success(request_id, "prompt")

    @staticmethod
    def _consume_prompt_result(task: asyncio.Task) -> None:
        """Retrieve post-acceptance failures so they are never silently dropped."""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            print(f"pi-py: prompt failed: {error}", file=sys.stderr)

    async def _cmd_steer(self, command: dict[str, Any], request_id: str | None):
        message = command.get("message")
        if not isinstance(message, str) or not message:
            return failure(request_id, "steer", "steer requires a non-empty string 'message'")
        await self.session.steer(message, command.get("images"))
        return success(request_id, "steer")

    async def _cmd_follow_up(self, command: dict[str, Any], request_id: str | None):
        message = command.get("message")
        if not isinstance(message, str) or not message:
            return failure(request_id, "follow_up", "follow_up requires a non-empty string 'message'")
        await self.session.follow_up(message, command.get("images"))
        return success(request_id, "follow_up")

    async def _cmd_abort(self, command: dict[str, Any], request_id: str | None):
        await self.session.abort()
        return success(request_id, "abort")

    async def _cmd_clear_queue(self, command: dict[str, Any], request_id: str | None):
        return success(request_id, "clear_queue", self.session.clear_queue())

    # -- session replacement -----------------------------------------------------------

    def _require_runtime(self) -> Any:
        if self._runtime is None:
            raise RuntimeError(
                "session replacement is unavailable: this process did not provide an "
                "AgentSessionRuntime"
            )
        return self._runtime

    async def _cmd_new_session(self, command: dict[str, Any], request_id: str | None):
        runtime = self._require_runtime()
        result = await runtime.new_session(parent_session=command.get("parentSession"))
        return success(request_id, "new_session", {"cancelled": bool(result.get("cancelled"))})

    async def _cmd_switch_session(self, command: dict[str, Any], request_id: str | None):
        path = command.get("sessionPath")
        if not isinstance(path, str) or not path:
            return failure(request_id, "switch_session", "switch_session requires 'sessionPath'")
        runtime = self._require_runtime()
        result = await runtime.switch_session(path)
        return success(request_id, "switch_session", {"cancelled": bool(result.get("cancelled"))})

    async def _cmd_fork(self, command: dict[str, Any], request_id: str | None):
        entry_id = command.get("entryId")
        if not isinstance(entry_id, str) or not entry_id:
            return failure(request_id, "fork", "fork requires 'entryId'")
        runtime = self._require_runtime()
        result = await runtime.fork(entry_id)
        return success(
            request_id,
            "fork",
            {"text": result.get("text") or "", "cancelled": bool(result.get("cancelled"))},
        )

    async def _cmd_clone(self, command: dict[str, Any], request_id: str | None):
        runtime = self._require_runtime()
        result = await runtime.clone()
        return success(request_id, "clone", {"cancelled": bool(result.get("cancelled"))})

    # -- state -------------------------------------------------------------------------

    async def _cmd_get_state(self, command: dict[str, Any], request_id: str | None):
        snapshot = self.session.state()
        data: dict[str, Any] = {
            "model": model_to_json(self.session.model),
            "thinkingLevel": self.session.thinking_level,
            "isStreaming": self.session.is_streaming,
            "isCompacting": self.session.is_compacting,
            "steeringMode": self.session.steering_mode,
            "followUpMode": self.session.follow_up_mode,
            "sessionFile": snapshot["session_file"],
            "sessionId": snapshot["session_id"],
            "autoCompactionEnabled": self.session.auto_compaction_enabled,
            "messageCount": len(self.session.messages),
            "pendingMessageCount": self.session.pending_message_count,
        }
        if snapshot["session_name"]:
            data["sessionName"] = snapshot["session_name"]
        return success(request_id, "get_state", data)

    async def _cmd_get_messages(self, command: dict[str, Any], request_id: str | None):
        from .json_event import message_to_json

        return success(
            request_id,
            "get_messages",
            {"messages": [message_to_json(message) for message in self.session.messages]},
        )

    # -- model -------------------------------------------------------------------------

    async def _available_models(self) -> list[Model]:
        if self.model_registry is None:
            return []
        getter = getattr(self.model_registry, "get_available", None)
        if callable(getter):
            models = await getter()
            return list(models)
        return list(self.model_registry.list_models())

    async def _cmd_get_available_models(self, command: dict[str, Any], request_id: str | None):
        models = await self._available_models()
        return success(request_id, "get_available_models", {"models": [model_to_json(m) for m in models]})

    async def _cmd_set_model(self, command: dict[str, Any], request_id: str | None):
        provider = command.get("provider")
        model_id = command.get("modelId")
        if not isinstance(provider, str) or not isinstance(model_id, str):
            return failure(request_id, "set_model", "set_model requires 'provider' and 'modelId'")
        if self.model_registry is None:
            return failure(request_id, "set_model", "no model registry is configured")

        model = self.model_registry.get(provider, model_id)
        if model is None:
            return failure(request_id, "set_model", f"Model {provider}/{model_id} not found")

        await self.session.set_model(model)
        return success(request_id, "set_model", model_to_json(model))

    async def _cmd_cycle_model(self, command: dict[str, Any], request_id: str | None):
        models = await self._available_models()
        if len(models) < 2:
            return success(request_id, "cycle_model", None)
        current = self.session.model
        index = next(
            (
                i
                for i, model in enumerate(models)
                if model.provider == current.provider and model.id == current.id
            ),
            -1,
        )
        nxt = models[(index + 1) % len(models)]
        await self.session.set_model(nxt)
        return success(
            request_id,
            "cycle_model",
            {
                "model": model_to_json(nxt),
                "thinkingLevel": self.session.thinking_level,
                "isScoped": False,
            },
        )

    # -- thinking ----------------------------------------------------------------------

    async def _cmd_get_available_thinking_levels(
        self, command: dict[str, Any], request_id: str | None
    ):
        return success(
            request_id,
            "get_available_thinking_levels",
            {"levels": get_supported_thinking_levels(self.session.model)},
        )

    async def _cmd_set_thinking_level(self, command: dict[str, Any], request_id: str | None):
        level = command.get("level")
        if not isinstance(level, str):
            return failure(request_id, "set_thinking_level", "set_thinking_level requires 'level'")
        await self.session.set_thinking_level(level)
        return success(request_id, "set_thinking_level")

    async def _cmd_cycle_thinking_level(self, command: dict[str, Any], request_id: str | None):
        levels = get_supported_thinking_levels(self.session.model)
        if len(levels) < 2:
            return success(request_id, "cycle_thinking_level", None)
        current = self.session.thinking_level
        index = levels.index(current) if current in levels else -1
        level = levels[(index + 1) % len(levels)]
        await self.session.set_thinking_level(level)
        return success(request_id, "cycle_thinking_level", {"level": level})

    # -- queue modes -------------------------------------------------------------------

    async def _cmd_set_steering_mode(self, command: dict[str, Any], request_id: str | None):
        try:
            self.session.set_steering_mode(str(command.get("mode")))
        except ValueError as error:
            return failure(request_id, "set_steering_mode", str(error))
        return success(request_id, "set_steering_mode")

    async def _cmd_set_follow_up_mode(self, command: dict[str, Any], request_id: str | None):
        try:
            self.session.set_follow_up_mode(str(command.get("mode")))
        except ValueError as error:
            return failure(request_id, "set_follow_up_mode", str(error))
        return success(request_id, "set_follow_up_mode")

    # -- compaction / retry ------------------------------------------------------------

    async def _cmd_compact(self, command: dict[str, Any], request_id: str | None):
        result = await self.session.compact(command.get("customInstructions"))
        return success(
            request_id,
            "compact",
            {
                "summary": result.summary,
                "firstKeptEntryId": result.first_kept_entry_id,
                "tokensBefore": result.tokens_before,
                "usage": usage_to_json(result.usage),
            },
        )

    async def _cmd_set_auto_compaction(self, command: dict[str, Any], request_id: str | None):
        self.session.set_auto_compaction(bool(command.get("enabled")))
        return success(request_id, "set_auto_compaction")

    async def _cmd_set_auto_retry(self, command: dict[str, Any], request_id: str | None):
        self.session.set_auto_retry(bool(command.get("enabled")))
        return success(request_id, "set_auto_retry")

    async def _cmd_abort_retry(self, command: dict[str, Any], request_id: str | None):
        self.session.abort_retry()
        return success(request_id, "abort_retry")

    # -- session -----------------------------------------------------------------------

    async def _cmd_get_session_stats(self, command: dict[str, Any], request_id: str | None):
        messages = self.session.messages
        tokens = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}
        cost = 0.0
        user_messages = assistant_messages = tool_results = tool_calls = 0

        for message in messages:
            role = getattr(message, "role", None)
            if role == "user":
                user_messages += 1
            elif role == "assistant":
                assistant_messages += 1
                usage = getattr(message, "usage", None)
                if usage is not None:
                    tokens["input"] += usage.input
                    tokens["output"] += usage.output
                    tokens["cacheRead"] += usage.cache_read
                    tokens["cacheWrite"] += usage.cache_write
                    tokens["total"] += usage.total_tokens
                    cost += usage.cost.total
                tool_calls += sum(
                    1 for block in (getattr(message, "content", None) or []) if getattr(block, "type", None) == "toolCall"
                )
            elif role == "toolResult":
                tool_results += 1

        snapshot = self.session.state()
        return success(
            request_id,
            "get_session_stats",
            {
                "sessionFile": snapshot["session_file"],
                "sessionId": snapshot["session_id"],
                "userMessages": user_messages,
                "assistantMessages": assistant_messages,
                "toolCalls": tool_calls,
                "toolResults": tool_results,
                "totalMessages": len(messages),
                "tokens": tokens,
                "cost": cost,
            },
        )

    async def _cmd_get_entries(self, command: dict[str, Any], request_id: str | None):
        manager = self.session.session_manager
        entries = [entry for entry in manager.get_entries() if getattr(entry, "id", None)]
        return success(
            request_id,
            "get_entries",
            {"entries": [entry_to_json(entry) for entry in entries], "leafId": manager.get_leaf_id()},
        )

    async def _cmd_get_tree(self, command: dict[str, Any], request_id: str | None):
        manager = self.session.session_manager
        return success(
            request_id,
            "get_tree",
            {
                "tree": [_tree_node_to_json(node) for node in manager.get_tree()],
                "leafId": manager.get_leaf_id(),
            },
        )

    async def _cmd_get_last_assistant_text(self, command: dict[str, Any], request_id: str | None):
        for message in reversed(self.session.messages):
            if getattr(message, "role", None) == "assistant":
                return success(request_id, "get_last_assistant_text", {"text": _text_of(message)})
        return success(request_id, "get_last_assistant_text", {"text": None})

    async def _cmd_get_fork_messages(self, command: dict[str, Any], request_id: str | None):
        """User messages in the current path, each with the entry it lives in.

        Forking itself is not implemented yet; this is the picker's data source.
        """
        entries = [entry for entry in self.session.session_manager.get_path() if getattr(entry, "id", None)]
        messages: list[dict[str, Any]] = []
        for entry in entries:
            message = getattr(entry, "message", None)
            if getattr(message, "role", None) == "user":
                messages.append({"entryId": entry.id, "text": _text_of(message)})
        return success(request_id, "get_fork_messages", {"messages": messages})

    async def _cmd_set_session_name(self, command: dict[str, Any], request_id: str | None):
        name = command.get("name")
        if not isinstance(name, str):
            return failure(request_id, "set_session_name", "set_session_name requires 'name'")
        await self.session.set_session_name(name)
        return success(request_id, "set_session_name")

    async def _cmd_get_commands(self, command: dict[str, Any], request_id: str | None):
        """Prompt templates, skills and extension commands.

        Built-ins are deliberately absent: pi only exposes those through the
        interactive autocomplete, and RPC clients send them as plain prompts.
        """
        commands: list[dict[str, Any]] = []
        for entry in self.session.get_commands():
            name = entry["name"]
            source = entry.get("source", "prompt")
            scope = "project" if self.resource_loader is not None and self.resource_loader.is_project_trusted else "user"
            commands.append(
                {
                    "name": name,
                    "description": entry.get("description") or None,
                    "source": source,
                    "sourceInfo": {
                        "path": "",
                        "source": source,
                        "scope": scope,
                        "origin": "top-level",
                    },
                }
            )
        return success(request_id, "get_commands", {"commands": commands})


async def run_rpc_mode(
    session: AgentSession | None = None,
    *,
    runtime: Any = None,
    model_registry: Any = None,
    resource_loader: ResourceLoader | None = None,
    extension_runner: Any = None,
    input_stream: BinaryIO | None = None,
    output: TextIO | None = None,
) -> int:
    """Serve a session over the JSONL RPC protocol until stdin closes.

    Pass ``runtime`` instead of ``session`` to enable the session-replacement
    commands (``new_session`` / ``switch_session`` / ``fork`` / ``clone``).
    """
    stream: BinaryIO = input_stream if input_stream is not None else sys.stdin.buffer
    mode = RpcMode(
        session,
        runtime=runtime,
        model_registry=model_registry,
        resource_loader=resource_loader,
        extension_runner=extension_runner,
        output=output,
    )
    return await mode.run(stream)


__all__ = [
    "SUPPORTED_COMMANDS",
    "UNSUPPORTED_COMMANDS",
    "RpcMode",
    "failure",
    "model_to_json",
    "run_rpc_mode",
    "success",
]

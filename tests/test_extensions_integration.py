"""Integration tests for the Python extension protocol.

These drive the real ``AgentSession`` with real extensions, so they pin the parts
that only show up when everything is wired together: tool merging, the two tool
hooks, prompt interception, message replacement, command dispatch, and the promise
that a broken extension cannot take the agent loop down with it.
"""

from __future__ import annotations

import json

import pytest
from pi_ai import (
    AssistantMessage,
    Cost,
    Model,
    ModelCost,
    TextContent,
    ToolCall,
    Usage,
)
from pi_ai.auth import ApiKeyCredential
from pi_ai import AuthStorage, InMemoryCredentialStore
from pi_ai.providers.faux import FauxTurn, register_faux_api
from pi_agent import AgentTool, AgentToolResult
from pi_coding_agent.agent_session import AgentSession
from pi_coding_agent.extensions import (
    BeforeAgentStartResult,
    ExtensionAPI,
    ExtensionHost,
    ExtensionRunner,
    InputEventResult,
    MessageEndResult,
    SessionBeforeEventResult,
    ToolCallEventResult,
    ToolResultEventResult,
)
from pi_coding_agent.session_manager import SessionManager
from pi_coding_agent.tools import create_builtin_tools

FAUX_API = "faux-ext"


def _model() -> Model:
    return Model(
        id="ext-model",
        name="Ext Model",
        api=FAUX_API,
        provider="faux",
        base_url="http://localhost",
        reasoning=False,
        input=["text"],
        cost=ModelCost(),
        context_window=100_000,
        max_tokens=4096,
    )


def _auth() -> AuthStorage:
    store = InMemoryCredentialStore()
    store.set("faux", ApiKeyCredential(key="k"))
    return AuthStorage(store)


def _session_with(tmp_path, turns, *extensions, tools=None, active=()):
    """Build a session wired to a runner, in the same order the CLI does it."""
    register_faux_api(FAUX_API, turns)
    runner = ExtensionRunner(extensions)
    session = AgentSession(
        model=_model(),
        cwd=str(tmp_path),
        session_manager=SessionManager.in_memory(str(tmp_path)),
        auth_storage=_auth(),
        extension_runner=runner,
        tools=tools,
        active_tool_names=active,
    )
    runner.bind_host(ExtensionHost(cwd=str(tmp_path), mode="print", session=session))
    return session, runner


def _assistant(text: str = "", tool_calls=None, stop_reason="stop") -> AssistantMessage:
    content = [TextContent(text=text)] if text else []
    content.extend(tool_calls or [])
    return AssistantMessage(
        content=content,
        api=FAUX_API,
        provider="faux",
        model="ext-model",
        usage=Usage(cost=Cost()),
        stop_reason=stop_reason,
        timestamp=0,
    )


# --------------------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------------------


async def test_extension_tool_is_merged_and_callable(tmp_path):
    api = ExtensionAPI(path="ext.py")
    calls: list[dict] = []

    @api.register_tool(name="greet", description="Greet someone")
    async def greet(args):
        calls.append(args)
        return f"hello {args['name']}"

    session, _ = _session_with(
        tmp_path,
        [
            FauxTurn(
                tool_calls=[ToolCall(id="c1", name="greet", arguments={"name": "ada"})],
                stop_reason="toolUse",
            ),
            _assistant("done"),
        ],
        api,
    )

    assert "greet" in [tool.name for tool in session.get_all_tools()]
    assert "greet" in session.get_active_tools()

    await session.prompt("greet ada")

    assert calls == [{"name": "ada"}]
    tool_result = next(m for m in session.messages if m.role == "toolResult")
    assert tool_result.content[0].text == "hello ada"
    assert tool_result.is_error is False


async def test_tool_call_handler_can_block_a_builtin_tool(tmp_path):
    api = ExtensionAPI(path="guard.py")
    blocked: list[str] = []

    async def guard(event, ctx):
        blocked.append(event.tool_name)
        return ToolCallEventResult(block=True, reason="blocked by policy")

    api.on("tool_call", guard)

    tools = create_builtin_tools(str(tmp_path), ("write",))
    session, _ = _session_with(
        tmp_path,
        [
            FauxTurn(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="write",
                        arguments={"path": "x.txt", "content": "nope"},
                    )
                ],
                stop_reason="toolUse",
            ),
            _assistant("gave up"),
        ],
        api,
        tools=tools,
        active=("write",),
    )

    await session.prompt("write a file")

    assert blocked == ["write"]
    assert not (tmp_path / "x.txt").exists()
    tool_result = next(m for m in session.messages if m.role == "toolResult")
    assert tool_result.is_error is True
    assert "blocked by policy" in tool_result.content[0].text


async def test_tool_call_handler_mutates_arguments_in_place(tmp_path):
    """pi's contract: to change arguments you mutate ``event.input`` in place."""
    api = ExtensionAPI(path="rewrite.py")

    def rewrite(event, ctx):
        event.input["content"] = "rewritten by extension"

    api.on("tool_call", rewrite)

    tools = create_builtin_tools(str(tmp_path), ("write",))
    session, _ = _session_with(
        tmp_path,
        [
            FauxTurn(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="write",
                        arguments={"path": "x.txt", "content": "original"},
                    )
                ],
                stop_reason="toolUse",
            ),
            _assistant("done"),
        ],
        api,
        tools=tools,
        active=("write",),
    )

    await session.prompt("write a file")

    assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "rewritten by extension"


async def test_tool_result_handler_rewrites_content(tmp_path):
    api = ExtensionAPI(path="redact.py")

    def redact(event, ctx):
        text = event.content[0].text
        return ToolResultEventResult(content=[TextContent(text=text.replace("secret", "***"))])

    api.on("tool_result", redact)

    tools = create_builtin_tools(str(tmp_path), ("bash",))
    session, _ = _session_with(
        tmp_path,
        [
            FauxTurn(
                tool_calls=[
                    ToolCall(id="c1", name="bash", arguments={"command": "echo secret"})
                ],
                stop_reason="toolUse",
            ),
            _assistant("done"),
        ],
        api,
        tools=tools,
        active=("bash",),
    )

    await session.prompt("run it")

    tool_result = next(m for m in session.messages if m.role == "toolResult")
    assert "secret" not in tool_result.content[0].text
    assert "***" in tool_result.content[0].text


# --------------------------------------------------------------------------------------
# Prompt interception
# --------------------------------------------------------------------------------------


async def test_input_handler_can_transform_the_prompt(tmp_path):
    api = ExtensionAPI(path="expand.py")

    def expand(event, ctx):
        return InputEventResult(action="transform", text=event.text.replace("XX", "world"))

    api.on("input", expand)

    session, _ = _session_with(tmp_path, [FauxTurn(text="ok")], api)
    await session.prompt("hello XX")

    user_messages = [m for m in session.messages if m.role == "user"]
    assert user_messages[0].content[0].text == "hello world"


async def test_input_handler_can_consume_the_prompt(tmp_path):
    api = ExtensionAPI(path="consume.py")
    handled: list[str] = []

    def consume(event, ctx):
        handled.append(event.text)
        return InputEventResult(action="handled")

    api.on("input", consume)

    session, _ = _session_with(tmp_path, [FauxTurn(text="should not run")], api)
    await session.prompt("/status")

    assert handled == ["/status"]
    assert session.messages == []


async def test_input_handlers_chain_transforms(tmp_path):
    api = ExtensionAPI(path="chain.py")
    api.on("input", lambda event, ctx: InputEventResult(action="transform", text=event.text + "-a"))
    api.on("input", lambda event, ctx: InputEventResult(action="transform", text=event.text + "-b"))

    session, _ = _session_with(tmp_path, [FauxTurn(text="ok")], api)
    await session.prompt("start")

    assert [m for m in session.messages if m.role == "user"][0].content[0].text == "start-a-b"


async def test_before_agent_start_injects_a_message_and_overrides_the_system_prompt(tmp_path):
    api = ExtensionAPI(path="context.py")

    async def inject(event, ctx):
        assert event.system_prompt  # the assembled prompt is visible
        return BeforeAgentStartResult(
            messages=[{"custom_type": "reminder", "content": "remember the thing"}],
            system_prompt="REPLACED PROMPT",
        )

    api.on("before_agent_start", inject)

    session, _ = _session_with(tmp_path, [FauxTurn(text="ok")], api)
    await session.prompt("do it")

    assert session.get_system_prompt() == "REPLACED PROMPT"
    custom = [m for m in session.messages if m.role == "custom"]
    assert custom and custom[0].custom_type == "reminder"


async def test_message_end_handler_can_replace_a_message(tmp_path):
    api = ExtensionAPI(path="strip.py")

    def strip(event, ctx):
        message = event.message
        if getattr(message, "role", None) != "assistant":
            return None
        return MessageEndResult(message=_assistant("redacted answer"))

    api.on("message_end", strip)

    session, _ = _session_with(tmp_path, [FauxTurn(text="original answer")], api)
    await session.prompt("hi")

    assistant = [m for m in session.messages if m.role == "assistant"][-1]
    assert assistant.content[0].text == "redacted answer"
    # The replacement is what got persisted, not the original.
    persisted = [m for m in session.session_manager.get_messages() if m.role == "assistant"]
    assert persisted[-1].content[0].text == "redacted answer"


async def test_message_end_role_change_is_rejected_and_reported(tmp_path):
    api = ExtensionAPI(path="bad.py")

    def bad(event, ctx):
        from pi_ai import UserMessage

        return MessageEndResult(message=UserMessage(content="nope", timestamp=0))

    api.on("message_end", bad)

    session, runner = _session_with(tmp_path, [FauxTurn(text="kept")], api)
    await session.prompt("hi")

    assistant = [m for m in session.messages if m.role == "assistant"][-1]
    assert assistant.content[0].text == "kept"
    errors = runner.drain_errors()
    assert any("same role" in error.error for error in errors)


# --------------------------------------------------------------------------------------
# Commands, session state, error isolation
# --------------------------------------------------------------------------------------


async def test_extension_command_runs_and_bypasses_the_model(tmp_path):
    api = ExtensionAPI(path="cmd.py")
    seen: list[tuple[str, str]] = []

    async def handler(args, ctx):
        seen.append((args, ctx.mode))

    api.register_command("hello", "Say hello", handler)

    session, runner = _session_with(tmp_path, [FauxTurn(text="should not run")], api)
    assert "hello" in runner.get_commands()
    assert any(c["name"] == "hello" and c["source"] == "extension" for c in session.get_commands())

    await session.prompt("/hello world")

    assert seen == [("world", "print")]
    # A command manages its own LLM interaction, so nothing was sent.
    assert session.messages == []


async def test_extension_command_works_as_a_decorator(tmp_path):
    api = ExtensionAPI(path="deco.py")

    @api.register_command("ping", "Ping")
    def ping(args):
        del args
        return None

    session, _ = _session_with(tmp_path, [FauxTurn(text="x")], api)
    await session.prompt("/ping")
    assert session.messages == []


async def test_send_message_and_append_entry_reach_the_session(tmp_path):
    api = ExtensionAPI(path="stateful.py")
    captured: dict[str, object] = {}

    async def remember(event, ctx):
        await ctx.session.send_custom_message("noted", custom_type="note")
        entry_id = await ctx.session.append_custom_entry("note", {"count": 1})
        captured["entry_id"] = entry_id

    api.on("before_agent_start", remember)

    session, _ = _session_with(tmp_path, [FauxTurn(text="ok")], api)
    await session.prompt("go")

    custom = [m for m in session.messages if m.role == "custom"]
    assert custom and custom[0].content == "noted"
    assert captured["entry_id"]

    entries = session.session_manager.get_entries()
    assert any(getattr(entry, "type", None) == "custom" for entry in entries)
    # Custom entries are extension state and must not enter the model context.
    assert all(m.role != "custom" or m.custom_type == "note" for m in session.session_manager.get_messages())


async def test_a_raising_handler_is_captured_and_does_not_break_the_run(tmp_path):
    api = ExtensionAPI(path="broken.py")

    async def boom(event, ctx):
        raise RuntimeError("extension exploded")

    async def fine(event, ctx):
        return InputEventResult(action="transform", text=event.text + " (seen)")

    api.on("input", boom)
    api.on("input", fine)

    session, runner = _session_with(tmp_path, [FauxTurn(text="ok")], api)
    await session.prompt("hello")

    # The run completed and the healthy handler still ran.
    assert [m.role for m in session.messages] == ["user", "assistant"]
    assert session.messages[0].content[0].text == "hello (seen)"
    errors = runner.drain_errors()
    assert [error.event for error in errors] == ["input"]
    assert "extension exploded" in errors[0].error


async def test_session_lifecycle_events_are_emitted(tmp_path):
    api = ExtensionAPI(path="lifecycle.py")
    kinds: list[str] = []
    api.on("session_start", lambda event, ctx: kinds.append(event.type))
    api.on("session_shutdown", lambda event, ctx: kinds.append(event.type))
    api.on("agent_start", lambda event, ctx: kinds.append(event.type))
    api.on("turn_end", lambda event, ctx: kinds.append(event.type))
    api.on("model_select", lambda event, ctx: kinds.append(event.type))

    session, _ = _session_with(tmp_path, [FauxTurn(text="ok")], api)

    await session.emit_session_start("startup")
    await session.prompt("hi")
    await session.set_model(_model())
    await session.emit_session_shutdown("quit")

    assert kinds == [
        "session_start",
        "agent_start",
        "turn_end",
        "model_select",
        "session_shutdown",
    ]


async def test_session_before_fork_can_cancel(tmp_path):
    api = ExtensionAPI(path="veto.py")
    api.on("session_before_fork", lambda event, ctx: SessionBeforeEventResult(cancel=True))

    session, runner = _session_with(tmp_path, [FauxTurn(text="ok")], api)
    from pi_coding_agent.extensions.types import SessionBeforeForkEvent

    result = await runner.emit(SessionBeforeForkEvent(entry_id="abc", position="before"))
    assert result is not None and result.cancel is True


async def test_set_active_tools_swaps_the_tool_list(tmp_path):
    api = ExtensionAPI(path="tools.py")
    api.register_tool(AgentTool(name="extra", description="extra", parameters={"type": "object", "properties": {}}))

    tools = create_builtin_tools(str(tmp_path), ("read", "bash", "edit", "write"))
    session, _ = _session_with(tmp_path, [FauxTurn(text="ok")], api, tools=tools, active=("read", "bash", "edit", "write"))

    assert set(session.get_active_tools()) >= {"read", "bash", "edit", "write", "extra"}

    await session.set_active_tools(["read", "extra"])
    assert session.get_active_tools() == ["read", "extra"]
    assert {tool.name for tool in session.agent.state.tools} == {"read", "extra"}

    with pytest.raises(ValueError, match="Unknown tools"):
        await session.set_active_tools(["nope"])


def test_runner_suffixes_duplicate_command_names():
    first = ExtensionAPI(path="a.py")
    first.register_command("dup", "first", lambda args, ctx: None)
    second = ExtensionAPI(path="b.py")
    second.register_command("dup", "second", lambda args, ctx: None)

    runner = ExtensionRunner([first, second])
    commands = runner.get_commands()
    assert set(commands) == {"dup", "dup:2"}
    assert commands["dup"].description == "first"
    assert commands["dup:2"].description == "second"

"""Tests for :class:`AgentSession`, compaction and the print/json modes.

Everything here is offline: the agent is driven by a scripted ``stream_fn`` (the
same injection point used by ``tests/test_agent_loop.py``, re-implemented here so
the two suites stay independent).  No provider, network or TUI is involved.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from pi_ai import (
    AssistantMessage,
    AuthStorage,
    AuthenticationError,
    Cost,
    DoneEvent,
    ErrorEvent,
    ImageContent,
    InMemoryCredentialStore,
    Model,
    ModelCost,
    StartEvent,
    TextContent,
    TextDeltaEvent,
    ToolCall,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultMessage,
    Usage,
    UserMessage,
    create_assistant_message_event_stream,
)
from pi_agent import AgentTool, AgentToolResult, MessageEndEvent

from pi_coding_agent.agent_session import AgentSession
from pi_coding_agent.compaction import (
    SUMMARIZATION_PROMPT,
    SUMMARIZATION_SYSTEM_PROMPT,
    CompactionSettings,
    compact_session,
    estimate_tokens,
    find_first_kept_entry_id,
    should_compact,
)
from pi_coding_agent.messages import CompactionSummaryMessage, CustomMessage
from pi_coding_agent.modes import run_json_mode, run_print_mode
from pi_coding_agent.resource_loader import ResourceLoader
from pi_coding_agent.session_manager import (
    CompactionEntry,
    CustomMessageEntry,
    ModelChangeEntry,
    SessionManager,
    SessionMessageEntry,
    ThinkingLevelChangeEntry,
)
from pi_coding_agent.settings import ENV_SETTINGS_MAP, SettingsManager
from pi_coding_agent.tools import DEFAULT_ACTIVE_TOOL_NAMES

# --------------------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's real home directory and PI_* settings out of tests."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    for env_name in ENV_SETTINGS_MAP:
        monkeypatch.delenv(env_name, raising=False)


def create_model(*, context_window: int = 100_000, reasoning: bool = False) -> Model:
    return Model(
        id="test-model",
        name="Test Model",
        api="faux",
        provider="test",
        base_url="http://localhost",
        reasoning=reasoning,
        input=["text"],
        cost=ModelCost(),
        context_window=context_window,
        max_tokens=4096,
    )


def create_assistant_message(
    content: list[Any],
    stop_reason: str = "stop",
    *,
    usage: Usage | None = None,
    error_message: str | None = None,
) -> AssistantMessage:
    return AssistantMessage(
        content=content,
        api="faux",
        provider="test",
        model="test-model",
        usage=usage or Usage(cost=Cost()),
        stop_reason=stop_reason,
        error_message=error_message,
        timestamp=int(time.time() * 1000),
    )


def text_message(
    text: str,
    stop_reason: str = "stop",
    *,
    usage: Usage | None = None,
    error_message: str | None = None,
) -> AssistantMessage:
    return create_assistant_message(
        [TextContent(text=text)], stop_reason, usage=usage, error_message=error_message
    )


def scripted_stream_fn(script: list[AssistantMessage], *, max_calls: int = 12, summary: str = "TEST SUMMARY"):
    """Replay one scripted assistant message per call.

    A summarization request (identified by its system prompt) always returns the
    canned ``summary`` so compaction tests do not consume script entries.
    """
    calls = {"n": 0}

    def stream_fn(model: Model, context: Any, options: Any):
        stream = create_assistant_message_event_stream()
        if context.system_prompt == SUMMARIZATION_SYSTEM_PROMPT:
            stream.push(DoneEvent(reason="stop", message=text_message(summary)))
            return stream

        signal = options.signal if options else None
        if signal is not None and signal.aborted:
            aborted = text_message("", "aborted", error_message="Operation aborted")
            stream.push(ErrorEvent(reason="aborted", error=aborted))
            return stream

        index = calls["n"]
        calls["n"] += 1
        assert calls["n"] <= max_calls, (
            f"stream function called {calls['n']} times (limit {max_calls}); "
            "the run is not terminating"
        )
        message = script[min(index, len(script) - 1)]
        stream.push(DoneEvent(reason=message.stop_reason, message=message))
        return stream

    stream_fn.calls = calls  # type: ignore[attr-defined]
    return stream_fn


def streaming_text_stream_fn(text: str, stop_reason: str = "stop", error_message: str | None = None):
    """Emit ``text`` as one text delta, then finish with the given stop reason."""

    def stream_fn(model: Model, context: Any, options: Any):
        stream = create_assistant_message_event_stream()
        partial = create_assistant_message([TextContent(text="")], "pending")
        stream.push(StartEvent(partial=partial))
        stream.push(TextDeltaEvent(content_index=0, delta=text, partial=partial))
        stream.push(
            DoneEvent(
                reason=stop_reason,
                message=text_message(text, stop_reason, error_message=error_message),
            )
        )
        return stream

    return stream_fn


def echo_tool(executed: list[str]) -> AgentTool:
    async def execute(tool_call_id: str, params: dict[str, Any], signal: Any, on_update: Any):
        executed.append(params["value"])
        return AgentToolResult(
            content=[TextContent(text=f"echoed: {params['value']}")], details={"value": params["value"]}
        )

    return AgentTool(
        name="echo",
        label="Echo",
        description="Echo tool",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        execute=execute,
    )


def configured_auth(provider: str = "test") -> AuthStorage:
    storage = AuthStorage(InMemoryCredentialStore(), env={})
    storage.set_api_key(provider, "test-key")
    return storage


def unconfigured_auth() -> AuthStorage:
    return AuthStorage(InMemoryCredentialStore(), env={})


def create_session(
    tmp_path: Path,
    *,
    model: Model | None = None,
    stream_fn: Any = None,
    tools: list[AgentTool] | None = None,
    settings_manager: SettingsManager | None = None,
    resource_loader: ResourceLoader | None = None,
    auth_storage: AuthStorage | None = None,
    session_file: str | None = None,
    **kwargs: Any,
) -> tuple[AgentSession, SessionManager]:
    cwd = tmp_path / "project"
    cwd.mkdir(exist_ok=True)
    if session_file is not None:
        session_manager = SessionManager.open(session_file)
    else:
        session_manager = SessionManager.create(str(cwd), session_dir=str(tmp_path / "sessions"))
    session = AgentSession(
        model or create_model(),
        str(cwd),
        session_manager,
        settings_manager=settings_manager,
        resource_loader=resource_loader,
        auth_storage=auth_storage if auth_storage is not None else configured_auth(),
        stream_fn=stream_fn,
        tools=tools,
        **kwargs,
    )
    return session, session_manager


def user_texts(session: AgentSession) -> list[str]:
    texts: list[str] = []
    for message in session.messages:
        if getattr(message, "role", None) != "user":
            continue
        content = message.content
        if isinstance(content, str):
            texts.append(content)
        else:
            texts.append("".join(block.text for block in content if isinstance(block, TextContent)))
    return texts


def make_settings(tmp_path: Path, overrides: dict[str, Any]) -> SettingsManager:
    return SettingsManager(str(tmp_path / "project"), str(tmp_path / "agent"), cli_overrides=overrides)


# --------------------------------------------------------------------------------------
# Prompting, persistence and reload
# --------------------------------------------------------------------------------------


async def test_prompt_persists_messages_and_survives_reload(tmp_path: Path) -> None:
    session, session_manager = create_session(
        tmp_path, stream_fn=scripted_stream_fn([text_message("hi from model")])
    )

    await session.prompt("hello")

    assert [message.role for message in session.messages] == ["user", "assistant"]
    assert user_texts(session) == ["hello"]

    session_file = session_manager.get_session_file()
    assert session_file is not None and os.path.exists(session_file)

    reopened = SessionManager.open(session_file)
    messages = reopened.get_messages()
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[0].content[0].text == "hello"
    assert messages[1].content[0].text == "hi from model"


async def test_prompt_while_streaming_requires_behavior_and_steers(tmp_path: Path) -> None:
    release = asyncio.Event()
    calls = {"n": 0}

    def stream_fn(model: Model, context: Any, options: Any):
        stream = create_assistant_message_event_stream()
        index = calls["n"]
        calls["n"] += 1

        async def _finish() -> None:
            await release.wait()
            stream.push(DoneEvent(reason="stop", message=text_message(f"reply {index}")))

        asyncio.ensure_future(_finish())
        return stream

    session, _ = create_session(tmp_path, stream_fn=stream_fn)

    task = asyncio.create_task(session.prompt("first"))
    for _ in range(200):
        if session.is_streaming:
            break
        await asyncio.sleep(0.001)
    assert session.is_streaming

    with pytest.raises(RuntimeError):
        await session.prompt("second")

    await session.prompt("second", streaming_behavior="steer")
    assert session.get_steering_messages() == ["second"]

    release.set()
    await asyncio.wait_for(task, timeout=5)

    assert user_texts(session) == ["first", "second"]
    assert session.get_steering_messages() == []


async def test_prompt_without_configured_auth_raises_authentication_error(tmp_path: Path) -> None:
    session, _ = create_session(
        tmp_path,
        stream_fn=scripted_stream_fn([text_message("never sent")]),
        auth_storage=unconfigured_auth(),
    )

    with pytest.raises(AuthenticationError):
        await session.prompt("hello")

    assert session.messages == []


async def test_skill_command_expands_skill_body(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent"
    skill_dir = agent_dir / "skills" / "greet"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: greet\ndescription: Greets the user\n---\nSKILL BODY CONTENT\n",
        encoding="utf-8",
    )
    loader = ResourceLoader(str(tmp_path / "project"), str(agent_dir), project_trusted=False)
    session, _ = create_session(
        tmp_path,
        resource_loader=loader,
        stream_fn=scripted_stream_fn([text_message("ok")]),
    )

    await session.prompt("/skill:greet please")

    text = user_texts(session)[0]
    assert "SKILL BODY CONTENT" in text
    assert '<skill name="greet"' in text
    assert text.endswith("please")


async def test_prompt_template_expands(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent"
    prompts_dir = agent_dir / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "hello.md").write_text("Say hello to $1 and $2\n", encoding="utf-8")
    loader = ResourceLoader(str(tmp_path / "project"), str(agent_dir), project_trusted=False)
    session, _ = create_session(
        tmp_path,
        resource_loader=loader,
        stream_fn=scripted_stream_fn([text_message("ok")]),
    )

    await session.prompt("/hello world moon")

    assert user_texts(session)[0].strip() == "Say hello to world and moon"


# --------------------------------------------------------------------------------------
# Compaction helpers
# --------------------------------------------------------------------------------------


def session_entry(entry_id: str, message: Any) -> SessionMessageEntry:
    return SessionMessageEntry(
        id=entry_id, parent_id=None, timestamp="2024-01-01T00:00:00.000Z", message=message
    )


def test_should_compact_uses_strict_threshold_and_enabled_flag() -> None:
    settings = CompactionSettings(enabled=True, reserve_tokens=100, keep_recent_tokens=20000)
    assert should_compact(901, 1000, settings) is True
    # Exactly at the threshold does NOT compact (upstream uses a strict `>`).
    assert should_compact(900, 1000, settings) is False
    disabled = CompactionSettings(enabled=False, reserve_tokens=100, keep_recent_tokens=20000)
    assert should_compact(100_000, 1000, disabled) is False


def test_estimate_tokens_uses_chars_over_four_and_an_image_budget() -> None:
    assert estimate_tokens(UserMessage(content=[TextContent(text="a" * 400)], timestamp=1)) == 100
    assert estimate_tokens(create_assistant_message([TextContent(text="b" * 8)])) == 2
    image = UserMessage(
        content=[ImageContent(data="AAAA", mime_type="image/png")], timestamp=1
    )
    assert estimate_tokens(image) == 4800 // 4


def test_find_first_kept_entry_id_never_cuts_at_a_tool_result() -> None:
    user = UserMessage(content=[TextContent(text="a" * 400)], timestamp=1)
    tool_call_message = create_assistant_message(
        [ToolCall(id="t1", name="echo", arguments={"value": "x"})], "toolUse"
    )
    tool_result_message = ToolResultMessage(
        tool_call_id="t1",
        tool_name="echo",
        content=[TextContent(text="echoed: x")],
        is_error=False,
        timestamp=2,
    )
    entries = [
        session_entry("a", user),
        session_entry("b", tool_call_message),
        session_entry("c", tool_result_message),
    ]

    # The budget stops inside the assistant turn, so the boundary is the tool
    # call itself and its tool result stays attached.
    assert find_first_kept_entry_id(entries, 4) == "b"
    # The tool result alone cannot be a cut point, so the whole history is kept.
    assert find_first_kept_entry_id(entries, 1) == "a"
    # Plain messages carry no id.
    assert find_first_kept_entry_id([user, tool_call_message], 1) is None


async def test_compact_session_uses_the_injected_stream_fn() -> None:
    entries = [
        session_entry("a", UserMessage(content=[TextContent(text="a" * 400)], timestamp=1)),
        session_entry("b", create_assistant_message([TextContent(text="answer")])),
    ]

    def stream_fn(model: Model, context: Any, options: Any):
        assert context.system_prompt == SUMMARIZATION_SYSTEM_PROMPT
        stream = create_assistant_message_event_stream()
        stream.push(DoneEvent(reason="stop", message=text_message("THE SUMMARY")))
        return stream

    result = await compact_session(
        entries,
        create_model(),
        settings=CompactionSettings(enabled=True, reserve_tokens=100, keep_recent_tokens=1),
        stream_fn=stream_fn,
    )

    assert result.summary == "THE SUMMARY"
    assert result.first_kept_entry_id == "b"
    assert result.tokens_before == estimate_tokens(entries[0].message) + estimate_tokens(entries[1].message)


async def test_compact_session_rejects_a_session_with_nothing_to_summarize() -> None:
    entries = [session_entry("a", UserMessage(content=[TextContent(text="hi")], timestamp=1))]

    def stream_fn(model: Model, context: Any, options: Any):  # pragma: no cover - never called
        raise AssertionError("no summarization call expected")

    with pytest.raises(ValueError):
        await compact_session(
            entries,
            create_model(),
            settings=CompactionSettings(enabled=True, reserve_tokens=100, keep_recent_tokens=1),
            stream_fn=stream_fn,
        )


# --------------------------------------------------------------------------------------
# Compaction during a run
# --------------------------------------------------------------------------------------


async def test_auto_compaction_at_threshold_appends_entry_and_rebuilds_context(tmp_path: Path) -> None:
    executed: list[str] = []
    settings = make_settings(
        tmp_path,
        {
            "compaction": {"enabled": True, "reserveTokens": 50, "keepRecentTokens": 4},
            "retry": {"enabled": False},
        },
    )
    stream_fn = scripted_stream_fn(
        [
            create_assistant_message(
                [ToolCall(id="call-1", name="echo", arguments={"value": "x"})],
                "toolUse",
                usage=Usage(input=100, cost=Cost()),
            ),
            text_message("done"),
        ],
        summary="COMPACTED SUMMARY",
    )
    session, session_manager = create_session(
        tmp_path,
        model=create_model(context_window=120),
        tools=[echo_tool(executed)],
        settings_manager=settings,
        stream_fn=stream_fn,
    )

    # A long first user message guarantees the cut lands on the assistant turn,
    # so the compaction runs from the prepare_next_turn hook before the next call.
    await session.prompt("A" * 400)

    assert executed == ["x"]
    compaction_entries = [entry for entry in session_manager.get_entries() if isinstance(entry, CompactionEntry)]
    assert len(compaction_entries) == 1
    assert compaction_entries[0].summary == "COMPACTED SUMMARY"

    context_messages = session_manager.build_context().messages
    summaries = [message for message in context_messages if isinstance(message, CompactionSummaryMessage)]
    assert [message.summary for message in summaries] == ["COMPACTED SUMMARY"]

    # The rebuilt context is what the agent actually continued with.
    agent_summaries = [message for message in session.messages if isinstance(message, CompactionSummaryMessage)]
    assert [message.summary for message in agent_summaries] == ["COMPACTED SUMMARY"]


async def test_post_run_threshold_compaction_replaces_context(tmp_path: Path) -> None:
    """A single-turn run can still trigger threshold compaction after agent_end."""
    settings = make_settings(
        tmp_path,
        {"compaction": {"enabled": True, "reserveTokens": 50, "keepRecentTokens": 1}},
    )
    stream_fn = scripted_stream_fn(
        [text_message("answer", usage=Usage(input=100, cost=Cost()))], summary="POST SUMMARY"
    )
    session, session_manager = create_session(
        tmp_path,
        model=create_model(context_window=120),
        settings_manager=settings,
        stream_fn=stream_fn,
    )

    await session.prompt("A" * 400)

    compaction_entries = [entry for entry in session_manager.get_entries() if isinstance(entry, CompactionEntry)]
    assert [entry.summary for entry in compaction_entries] == ["POST SUMMARY"]

    agent_summaries = [message for message in session.messages if isinstance(message, CompactionSummaryMessage)]
    assert [message.summary for message in agent_summaries] == ["POST SUMMARY"]
    assert session.messages[-1].role == "assistant"


async def test_manual_compact_appends_entry_and_rebuilds_context(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path, {"compaction": {"enabled": True, "reserveTokens": 50, "keepRecentTokens": 1}}
    )
    stream_fn = scripted_stream_fn([text_message("answer")], summary="MANUAL SUMMARY")
    session, session_manager = create_session(
        tmp_path, settings_manager=settings, stream_fn=stream_fn
    )

    await session.prompt("B" * 400)
    assert len(session_manager.get_entries()) == 2

    result = await session.compact()

    assert result.summary == "MANUAL SUMMARY"
    assert result.tokens_before > 0
    assert any(isinstance(entry, CompactionEntry) for entry in session_manager.get_entries())
    assert any(isinstance(message, CompactionSummaryMessage) for message in session.messages)


async def test_compaction_prompt_is_the_structured_summarization_prompt(tmp_path: Path) -> None:
    """The summary request must carry the module constant, not an ad-hoc prompt."""
    seen_prompts: list[str] = []

    def stream_fn(model: Model, context: Any, options: Any):
        stream = create_assistant_message_event_stream()
        if context.system_prompt == SUMMARIZATION_SYSTEM_PROMPT:
            seen_prompts.append(context.messages[0].content[0].text)
            stream.push(DoneEvent(reason="stop", message=text_message("THE SUMMARY")))
        else:
            stream.push(DoneEvent(reason="stop", message=text_message("answer")))
        return stream

    settings = make_settings(
        tmp_path, {"compaction": {"enabled": True, "reserveTokens": 50, "keepRecentTokens": 1}}
    )
    session, _ = create_session(tmp_path, settings_manager=settings, stream_fn=stream_fn)
    await session.prompt("C" * 400)
    await session.compact()

    assert seen_prompts
    assert SUMMARIZATION_PROMPT in seen_prompts[0]
    assert "<conversation>" in seen_prompts[0]


# --------------------------------------------------------------------------------------
# Retry
# --------------------------------------------------------------------------------------


async def test_retry_after_transient_error_succeeds_without_duplicating(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path, {"retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 1}}
    )
    stream_fn = scripted_stream_fn(
        [
            text_message("", "error", error_message="503 Service Unavailable: overloaded"),
            text_message("recovered"),
        ]
    )
    session, session_manager = create_session(
        tmp_path, settings_manager=settings, stream_fn=stream_fn
    )

    await session.prompt("please retry")

    assert stream_fn.calls["n"] == 2  # type: ignore[attr-defined]
    assert [message.role for message in session.messages] == ["user", "assistant"]
    assert session.messages[-1].content[0].text == "recovered"

    messages = session_manager.build_context().messages
    roles = [message.role for message in messages]
    assert roles == ["user", "assistant", "assistant"]
    assert messages[1].stop_reason == "error"
    assert messages[2].content[0].text == "recovered"
    # The failed attempt is persisted exactly once.
    failed = [
        message
        for message in messages
        if getattr(message, "role", None) == "assistant" and message.stop_reason == "error"
    ]
    assert len(failed) == 1


async def test_non_retryable_error_is_not_retried(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path, {"retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 1}}
    )
    stream_fn = scripted_stream_fn(
        [text_message("", "error", error_message="insufficient_quota: billing hard limit")]
    )
    session, _ = create_session(tmp_path, settings_manager=settings, stream_fn=stream_fn)

    await session.prompt("hello")

    assert stream_fn.calls["n"] == 1  # type: ignore[attr-defined]
    assert session.messages[-1].stop_reason == "error"


# --------------------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------------------


async def test_print_mode_streams_assistant_text(tmp_path: Path) -> None:
    session, _ = create_session(tmp_path, stream_fn=streaming_text_stream_fn("Hello from the model"))
    output = io.StringIO()

    code = await run_print_mode(session, "hi", output=output)

    assert code == 0
    assert output.getvalue().strip() == "Hello from the model"


async def test_print_mode_falls_back_to_final_assistant_text(tmp_path: Path) -> None:
    """A provider without streaming deltas must still print the final answer."""
    session, _ = create_session(tmp_path, stream_fn=scripted_stream_fn([text_message("no deltas here")]))
    output = io.StringIO()

    code = await run_print_mode(session, "hi", output=output)

    assert code == 0
    assert output.getvalue().strip() == "no deltas here"


async def test_print_mode_returns_one_on_error_stop_reason(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    settings = make_settings(tmp_path, {"retry": {"enabled": False}})
    session, _ = create_session(
        tmp_path,
        settings_manager=settings,
        stream_fn=streaming_text_stream_fn("partial output", "error", "boom"),
    )
    output = io.StringIO()

    code = await run_print_mode(session, "hi", output=output)

    assert code == 1
    # Assistant text still went to stdout, the diagnostic to stderr.
    assert output.getvalue().strip() == "partial output"
    assert "boom" in capsys.readouterr().err


def tool_then_text_stream_fn():
    """First call: one tool call.  Later calls: a text response."""
    calls = {"n": 0}

    def stream_fn(model: Model, context: Any, options: Any):
        stream = create_assistant_message_event_stream()
        index = calls["n"]
        calls["n"] += 1
        if index == 0:
            tool_call = ToolCall(id="call-1", name="echo", arguments={"value": "x"})
            partial = create_assistant_message([tool_call], "pending")
            stream.push(StartEvent(partial=partial))
            stream.push(ToolCallStartEvent(content_index=0, partial=partial))
            stream.push(ToolCallEndEvent(content_index=0, tool_call=tool_call, partial=partial))
            stream.push(
                DoneEvent(reason="toolUse", message=create_assistant_message([tool_call], "toolUse"))
            )
            return stream

        partial = create_assistant_message([TextContent(text="")], "pending")
        stream.push(StartEvent(partial=partial))
        stream.push(TextDeltaEvent(content_index=0, delta="all done", partial=partial))
        stream.push(DoneEvent(reason="stop", message=text_message("all done")))
        return stream

    return stream_fn


async def test_json_mode_emits_header_and_delta_only_message_updates(tmp_path: Path) -> None:
    executed: list[str] = []
    session, _ = create_session(
        tmp_path, tools=[echo_tool(executed)], stream_fn=tool_then_text_stream_fn()
    )
    output = io.StringIO()

    code = await run_json_mode(session, "run the tool", output=output)

    assert code == 0
    assert executed == ["x"]

    lines = [line for line in output.getvalue().splitlines() if line]
    payloads = [json.loads(line) for line in lines]

    header = payloads[0]
    assert header["type"] == "session"
    assert header["id"] == session.state()["session_id"]
    assert header["cwd"] == session.cwd
    assert header["model"] == "test-model"

    updates = [payload for payload in payloads if payload["type"] == "message_update"]
    assert updates
    for update in updates:
        # Delta-only: no cumulative message, no live partial snapshot.  The
        # cumulative ``usage`` stays because its size is constant and clients
        # render live token/cost counters from it (upstream json-event.ts).
        assert set(update) == {"type", "usage", "assistantMessageEvent"}
        assert "partial" not in update["assistantMessageEvent"]
        assert {"input", "output", "totalTokens"} <= set(update["usage"])

    text_deltas = [
        update["assistantMessageEvent"]["delta"]
        for update in updates
        if update["assistantMessageEvent"]["type"] == "text_delta"
    ]
    assert "".join(text_deltas) == "all done"

    starts = [
        update["assistantMessageEvent"]
        for update in updates
        if update["assistantMessageEvent"]["type"] == "toolcall_start"
    ]
    assert starts == [{"type": "toolcall_start", "contentIndex": 0, "id": "call-1", "toolName": "echo"}]


async def test_json_mode_returns_one_on_error_stop_reason(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    settings = make_settings(tmp_path, {"retry": {"enabled": False}})
    session, _ = create_session(
        tmp_path,
        settings_manager=settings,
        stream_fn=streaming_text_stream_fn("partial", "error", "kaboom"),
    )
    output = io.StringIO()

    code = await run_json_mode(session, "hi", output=output)

    assert code == 1
    assert "kaboom" in capsys.readouterr().err
    # The stream itself is still valid JSONL.
    for line in output.getvalue().splitlines():
        assert json.loads(line)["type"]


# --------------------------------------------------------------------------------------
# Tools, state and session entries
# --------------------------------------------------------------------------------------


async def test_default_builtin_tools_and_state(tmp_path: Path) -> None:
    session, _ = create_session(tmp_path, stream_fn=scripted_stream_fn([text_message("ok")]))

    assert session.active_tool_names == list(DEFAULT_ACTIVE_TOOL_NAMES)

    state = session.state()
    assert set(state) == {
        "model",
        "thinking_level",
        "is_streaming",
        "session_file",
        "session_id",
        "session_name",
        "message_count",
    }
    assert state["model"] is session.model
    assert state["message_count"] == 0

    await session.prompt("hello")
    assert session.state()["message_count"] == 2


async def test_set_model_and_thinking_level_append_session_entries(tmp_path: Path) -> None:
    session, session_manager = create_session(
        tmp_path, model=create_model(reasoning=True), stream_fn=scripted_stream_fn([text_message("ok")])
    )

    await session.set_thinking_level("high")
    assert session.thinking_level == "high"

    other = create_model(reasoning=True)
    other.id = "other-model"
    await session.set_model(other)

    entries = session_manager.get_entries()
    assert any(
        isinstance(entry, ThinkingLevelChangeEntry) and entry.thinking_level == "high"
        for entry in entries
    )
    assert any(
        isinstance(entry, ModelChangeEntry) and entry.model_id == "other-model" for entry in entries
    )


async def test_custom_message_persists_as_custom_message_entry(tmp_path: Path) -> None:
    session, session_manager = create_session(
        tmp_path, stream_fn=scripted_stream_fn([text_message("ok")])
    )
    custom = CustomMessage(custom_type="test-extension", content="injected", display=True, timestamp=1)

    # There is no public API for injecting runtime messages in v0.1 (the
    # extension system is deferred), so drive the persistence hook directly.
    await session._handle_agent_event(MessageEndEvent(message=custom))

    entries = session_manager.get_entries()
    assert len(entries) == 1
    assert isinstance(entries[0], CustomMessageEntry)
    assert entries[0].custom_type == "test-extension"
    assert entries[0].content == "injected"

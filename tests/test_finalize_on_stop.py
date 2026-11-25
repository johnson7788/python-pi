"""A run that ends for a guard reason still answers.

The failure this prevents: the model stops producing anything (empty reply,
truncation, an endless tool loop), the loop ends, and the user is left looking at
an empty assistant message even though the run had already collected tool results.
The session now writes a final, tool-free answer from that transcript.

The scripted stream makes the distinction visible — the last call the model sees
is the synthesis request, which carries no tools.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from pi_ai import (
    AssistantMessage,
    AuthStorage,
    Cost,
    DoneEvent,
    InMemoryCredentialStore,
    Model,
    ModelCost,
    TextContent,
    ToolCall,
    Usage,
    create_assistant_message_event_stream,
)

from pi_coding_agent.agent_session import AgentSession
from pi_coding_agent.session_manager import SessionManager


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)


def create_model() -> Model:
    return Model(
        id="test-model",
        name="Test Model",
        api="faux",
        provider="test",
        base_url="http://localhost",
        reasoning=False,
        input=["text"],
        cost=ModelCost(),
        context_window=100_000,
        max_tokens=4096,
    )


def assistant(content: list, stop_reason: str = "stop") -> AssistantMessage:
    return AssistantMessage(
        content=content,
        api="faux",
        provider="test",
        model="test-model",
        usage=Usage(cost=Cost()),
        stop_reason=stop_reason,
        timestamp=int(time.time() * 1000),
    )


def text_message(text: str) -> AssistantMessage:
    return assistant([TextContent(text=text)])


def tool_message() -> AssistantMessage:
    return assistant([ToolCall(id="call-1", name="echo", arguments={"value": "x"})], "toolUse")


class ScriptedStream:
    """One scripted message per model call, recording what each call carried."""

    def __init__(self, script: list[AssistantMessage]) -> None:
        self.script = script
        self.calls: list[dict] = []

    def __call__(self, model, context, options):
        self.calls.append({"tools": list(context.tools or []), "messages": list(context.messages)})
        index = len(self.calls) - 1
        message = self.script[min(index, len(self.script) - 1)]
        stream = create_assistant_message_event_stream()
        stream.push(DoneEvent(reason=message.stop_reason, message=message))
        return stream


def create_session(tmp_path: Path, stream_fn, **kwargs) -> tuple[AgentSession, SessionManager]:
    cwd = tmp_path / "project"
    cwd.mkdir(exist_ok=True)
    manager = SessionManager.create(str(cwd), session_dir=str(tmp_path / "sessions"))
    storage = AuthStorage(InMemoryCredentialStore(), env={})
    storage.set_api_key("test", "test-key")
    session = AgentSession(
        create_model(),
        str(cwd),
        manager,
        auth_storage=storage,
        stream_fn=stream_fn,
        tools=[],
        **kwargs,
    )
    return session, manager


def assistant_texts(session: AgentSession) -> list[str]:
    texts: list[str] = []
    for message in session.messages:
        if getattr(message, "role", None) != "assistant":
            continue
        content = getattr(message, "content", None) or []
        texts.append("".join(getattr(block, "text", "") for block in content))
    return texts


async def test_a_hit_iteration_cap_still_produces_an_answer(tmp_path: Path) -> None:
    stream = ScriptedStream([tool_message(), text_message("here is what I found")])
    session, manager = create_session(tmp_path, stream, max_turns=1)

    await session.prompt("keep going forever")
    await session.wait_for_idle()

    # Two model calls: the capped turn, then the synthesis with no tools.
    assert len(stream.calls) == 2
    assert stream.calls[1]["tools"] == [], "the final answer must not be able to start new work"
    assert assistant_texts(session)[-1] == "here is what I found"

    # The synthesis is persisted, not just held in memory: reload the session and
    # look for it in the JSONL the next run would read.
    session_file = manager.get_session_file()
    assert session_file is not None
    assert "here is what I found" in Path(session_file).read_text(encoding="utf-8")


async def test_a_repeated_empty_reply_is_answered_from_the_transcript(tmp_path: Path) -> None:
    stream = ScriptedStream([assistant([]), assistant([]), text_message("best effort answer")])
    session, _manager = create_session(tmp_path, stream)

    await session.prompt("do the thing")
    await session.wait_for_idle()

    assert len(stream.calls) == 3
    assert assistant_texts(session)[-1] == "best effort answer"


async def test_an_empty_synthesis_is_discarded_rather_than_appended(tmp_path: Path) -> None:
    stream = ScriptedStream([assistant([]), assistant([]), assistant([])])
    session, _manager = create_session(tmp_path, stream)

    await session.prompt("do the thing")
    await session.wait_for_idle()

    assert len(assistant_texts(session)) == 2, "no third empty message is appended"
    assert all(text == "" for text in assistant_texts(session))


async def test_an_error_turn_is_not_turned_into_a_synthesized_answer(tmp_path: Path) -> None:
    """Errors belong to the retry path; the guard must not quietly replace them.

    With no retry budget an error ends the turn as-is.  What this pins is that the
    guard machinery stays out of it: no second, tool-free call is made.
    """
    errored = assistant([], "error")
    errored.error_message = "boom"
    stream = ScriptedStream([errored, text_message("synthesized")])
    session, _manager = create_session(tmp_path, stream)

    await session.prompt("do the thing")
    await session.wait_for_idle()

    assert len(stream.calls) == 1
    assert assistant_texts(session) == [""]

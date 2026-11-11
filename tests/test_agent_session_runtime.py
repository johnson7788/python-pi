"""Tests for the session replacement layer.

``AgentSession`` is bound to one session file for its lifetime, so every
replacement has to rebuild it.  These tests pin the observable consequences:
the session object changes, the transcript is rebuilt from the new file, event
subscriptions must be re-attached, and a persisted session must never silently
degrade into an unsaved one.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from pi_ai import Cost, Model, ModelCost, TextContent, Usage
from pi_ai.auth import ApiKeyCredential
from pi_ai import AuthStorage, InMemoryCredentialStore
from pi_ai.providers.faux import FauxTurn, register_faux_api
from pi_coding_agent.agent_session import AgentSession
from pi_coding_agent.agent_session_runtime import (
    AgentSessionRuntime,
    create_agent_session_runtime,
    extract_user_message_text,
)
from pi_coding_agent.session_manager import SessionManager

FAUX_API = "faux-runtime"


def _model() -> Model:
    return Model(
        id="runtime-model",
        name="Runtime Model",
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


async def _make_runtime(tmp_path: Path, turns: list[FauxTurn]) -> AgentSessionRuntime:
    register_faux_api(FAUX_API, turns)
    agent_dir = tmp_path / "agent"

    async def factory(*, cwd, agent_dir, session_manager, session_start_event=None):
        del agent_dir, session_start_event
        return AgentSession(
            model=_model(),
            cwd=cwd,
            session_manager=session_manager,
            auth_storage=_auth(),
            active_tool_names=(),
        )

    manager = SessionManager.create(str(tmp_path), str(agent_dir))
    return await create_agent_session_runtime(
        factory, cwd=str(tmp_path), agent_dir=str(agent_dir), session_manager=manager
    )


def _user_entries(session: AgentSession) -> list:
    return [
        entry
        for entry in session.session_manager.get_path()
        if getattr(entry, "type", None) == "message"
        and getattr(getattr(entry, "message", None), "role", None) == "user"
    ]


def test_extract_user_message_text_handles_both_shapes():
    assert extract_user_message_text("plain") == "plain"
    assert extract_user_message_text([TextContent(text="a"), TextContent(text="b")]) == "ab"
    assert extract_user_message_text(None) == ""


async def test_new_session_replaces_the_session_object_and_clears_history(tmp_path):
    runtime = await _make_runtime(tmp_path, [FauxTurn(text="first"), FauxTurn(text="second")])
    original = runtime.session
    await original.prompt("hello")
    assert [m.role for m in original.messages] == ["user", "assistant"]

    result = await runtime.new_session()

    assert result == {"cancelled": False}
    assert runtime.session is not original
    assert runtime.session.messages == []
    # A persisted session must stay persisted, and get a brand new file.
    assert runtime.session.session_manager.is_persisted()
    assert runtime.session.session_manager.get_session_file() != original.session_manager.get_session_file()
    await runtime.dispose()


async def test_new_session_records_the_parent(tmp_path):
    runtime = await _make_runtime(tmp_path, [FauxTurn(text="one"), FauxTurn(text="two")])
    original_file = runtime.session.session_manager.get_session_file()
    await runtime.session.prompt("hello")

    await runtime.new_session(parent_session=original_file)

    header = runtime.session.session_manager.get_header()
    assert header is not None
    assert header.parent_session == original_file
    await runtime.dispose()


async def test_in_memory_runtime_stays_in_memory(tmp_path):
    register_faux_api(FAUX_API, [FauxTurn(text="x")])
    agent_dir = tmp_path / "agent"

    async def factory(*, cwd, agent_dir, session_manager, session_start_event=None):
        del agent_dir, session_start_event
        return AgentSession(
            model=_model(),
            cwd=cwd,
            session_manager=session_manager,
            auth_storage=_auth(),
            active_tool_names=(),
        )

    runtime = await create_agent_session_runtime(
        factory,
        cwd=str(tmp_path),
        agent_dir=str(agent_dir),
        session_manager=SessionManager.in_memory(str(tmp_path)),
    )

    assert not runtime.session.session_manager.is_persisted()
    await runtime.new_session()
    assert not runtime.session.session_manager.is_persisted()
    assert not (agent_dir / "sessions").exists()
    await runtime.dispose()


async def test_switch_session_adopts_an_existing_file(tmp_path):
    # Build a session file on disk first.
    other = SessionManager.create(str(tmp_path), str(tmp_path / "agent"))
    await other.append_message(
        __import__("pi_ai").UserMessage(content=[TextContent(text="from the other session")], timestamp=0)
    )
    await other.append_message(
        __import__("pi_ai").AssistantMessage(
            content=[TextContent(text="its reply")],
            api=FAUX_API,
            provider="faux",
            model="runtime-model",
            usage=Usage(cost=Cost()),
            stop_reason="stop",
            timestamp=1,
        )
    )
    target_file = other.get_session_file()
    assert target_file is not None and os.path.exists(target_file)

    runtime = await _make_runtime(tmp_path, [FauxTurn(text="unused")])
    before = runtime.session
    result = await runtime.switch_session(target_file)

    assert result == {"cancelled": False}
    assert runtime.session is not before
    # The transcript is rebuilt from the adopted file.
    assert [m.role for m in runtime.session.messages] == ["user", "assistant"]
    assert runtime.session.messages[0].content[0].text == "from the other session"
    assert runtime.session.session_manager.get_session_file() == target_file
    await runtime.dispose()


async def test_rebind_session_is_called_and_events_flow_after_replacement(tmp_path):
    runtime = await _make_runtime(tmp_path, [FauxTurn(text="first"), FauxTurn(text="second")])

    seen: list[tuple[int, str]] = []
    bindings: list[AgentSession] = []

    async def rebind(session: AgentSession) -> None:
        bindings.append(session)
        session.subscribe(lambda event: seen.append((id(session), event.type)))

    runtime.set_rebind_session(rebind)

    original = runtime.session
    await runtime.new_session()
    assert bindings and bindings[-1] is runtime.session
    assert runtime.session is not original

    seen.clear()
    await runtime.session.prompt("after replacement")
    # Events reach the listener attached in the rebind callback.
    assert any(session_id == id(runtime.session) for session_id, _ in seen)
    assert "agent_end" in [kind for _, kind in seen]
    await runtime.dispose()


async def test_fork_before_a_user_message_copies_the_prefix(tmp_path):
    runtime = await _make_runtime(
        tmp_path, [FauxTurn(text="reply one"), FauxTurn(text="reply two"), FauxTurn(text="reply three")]
    )
    session = runtime.session
    await session.prompt("first question")
    await session.prompt("second question")

    users = _user_entries(session)
    assert len(users) == 2
    second = users[1]

    result = await runtime.fork(second.id)

    assert result["cancelled"] is False
    assert result["text"] == "second question"

    forked = runtime.session
    assert forked is not session
    # Only the first exchange (before the second user message) survived.
    assert [m.role for m in forked.messages] == ["user", "assistant"]
    assert forked.messages[0].content[0].text == "first question"

    header = forked.session_manager.get_header()
    assert header is not None
    assert header.parent_session == session.session_manager.get_session_file()
    await runtime.dispose()


async def test_fork_at_an_entry_keeps_it(tmp_path):
    runtime = await _make_runtime(tmp_path, [FauxTurn(text="reply one"), FauxTurn(text="reply two")])
    session = runtime.session
    await session.prompt("first question")

    users = _user_entries(session)
    result = await runtime.fork(users[0].id, position="at")

    assert result["cancelled"] is False
    # "at" keeps the entry itself, so the user message is present.
    assert [m.role for m in runtime.session.messages] == ["user"]
    assert runtime.session.messages[0].content[0].text == "first question"
    await runtime.dispose()


async def test_fork_before_the_first_entry_yields_an_empty_session(tmp_path):
    runtime = await _make_runtime(tmp_path, [FauxTurn(text="reply")])
    session = runtime.session
    await session.prompt("first question")

    users = _user_entries(session)
    result = await runtime.fork(users[0].id)

    assert result["cancelled"] is False
    assert result["text"] == "first question"
    assert runtime.session.messages == []
    header = runtime.session.session_manager.get_header()
    assert header is not None and header.parent_session == session.session_manager.get_session_file()
    await runtime.dispose()


async def test_clone_copies_the_whole_conversation(tmp_path):
    runtime = await _make_runtime(tmp_path, [FauxTurn(text="reply one"), FauxTurn(text="reply two")])
    session = runtime.session
    await session.prompt("first question")

    result = await runtime.clone()

    assert result["cancelled"] is False
    assert [m.role for m in runtime.session.messages] == ["user", "assistant"]
    assert runtime.session.session_manager.get_session_file() != session.session_manager.get_session_file()
    await runtime.dispose()


async def test_fork_rejects_an_unknown_or_wrong_typed_entry(tmp_path):
    runtime = await _make_runtime(tmp_path, [FauxTurn(text="reply")])
    session = runtime.session
    await session.prompt("first question")

    with pytest.raises(ValueError, match="Invalid entry ID"):
        await runtime.fork("does-not-exist")

    # "before" requires a user message; an assistant entry is not a valid anchor.
    assistant_entry = next(
        entry
        for entry in session.session_manager.get_path()
        if getattr(entry, "type", None) == "message"
        and getattr(getattr(entry, "message", None), "role", None) == "assistant"
    )
    with pytest.raises(ValueError, match="Invalid entry ID"):
        await runtime.fork(assistant_entry.id)
    await runtime.dispose()


async def test_clone_on_an_empty_session_is_rejected(tmp_path):
    runtime = await _make_runtime(tmp_path, [FauxTurn(text="x")])
    with pytest.raises(ValueError, match="no entries"):
        await runtime.clone()
    await runtime.dispose()


async def test_fork_rejects_an_invalid_position(tmp_path):
    runtime = await _make_runtime(tmp_path, [FauxTurn(text="x")])
    with pytest.raises(ValueError, match="Invalid fork position"):
        await runtime.fork("whatever", position="sideways")
    await runtime.dispose()


async def test_replacing_a_session_mid_turn_lets_the_run_finish(tmp_path, monkeypatch):
    """Starting another task during a turn must not cancel that turn.

    ``AgentSession.dispose`` aborts an active run, so a replacement while the
    agent is streaming has to retire the old session instead: the user's click
    moves the view, it does not delete the work in flight.
    """
    runtime = await _make_runtime(tmp_path, [FauxTurn(text="finished anyway")])
    retired = runtime.session
    await retired.prompt("hello")

    disposed: list[bool] = []
    retired.add_dispose_callback(lambda: disposed.append(True))

    # No scripted turn lasts long enough to race against, so hold the one flag
    # the replacement layer looks at.
    monkeypatch.setattr(AgentSession, "is_streaming", property(lambda _session: True))

    await runtime.new_session()

    assert runtime.session is not retired, "the client moves to the new session"
    assert runtime.has_retiring_runs() is True, "the abandoned run is still in flight"
    assert disposed == [], "retiring must not dispose, which would abort the run"

    monkeypatch.undo()
    for _ in range(80):
        if not runtime.has_retiring_runs():
            break
        await asyncio.sleep(0.05)
    assert runtime.has_retiring_runs() is False
    assert disposed == [True], "the retired session is released once it is idle"
    await runtime.dispose()


async def test_switching_back_adopts_the_still_running_session(tmp_path, monkeypatch):
    """Returning to a session that is still streaming must not reopen a partial file.

    Two live writers on one session file would interleave entries, so the runtime
    hands the *same* session object back and the run keeps streaming.
    """
    runtime = await _make_runtime(tmp_path, [FauxTurn(text="still going")])
    live = runtime.session
    await live.prompt("hello")
    target = live.session_file
    assert target is not None

    # Only the conversation left behind is "streaming"; the fresh one is empty.
    monkeypatch.setattr(AgentSession, "is_streaming", property(lambda session: session is live))
    await runtime.new_session()
    assert runtime.session is not live
    assert runtime.has_retiring_runs() is True

    await runtime.switch_session(target)

    assert runtime.session is live, "the running session is adopted, not rebuilt"
    assert runtime.has_retiring_runs() is False, "adopting cancels the retirement"

    disposed: list[bool] = []
    live.add_dispose_callback(lambda: disposed.append(True))
    await asyncio.sleep(0.1)
    assert disposed == [], "the adopted session must not be disposed in the background"

    monkeypatch.undo()
    await runtime.dispose()
    assert disposed == [True]


async def test_dispose_releases_the_session(tmp_path):
    runtime = await _make_runtime(tmp_path, [FauxTurn(text="x")])
    session = runtime.session
    await runtime.dispose()
    # A disposed session refuses further prompts rather than failing obscurely.
    with pytest.raises(RuntimeError):
        await session.prompt("too late")

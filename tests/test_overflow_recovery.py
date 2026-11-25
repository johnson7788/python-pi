"""Context-overflow classification and the one compaction it buys.

Two layers again: the classifier is a table of real provider strings, and the
recovery test proves the session compacts once and continues instead of ending the
turn with an error.
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
    ErrorEvent,
    InMemoryCredentialStore,
    Model,
    ModelCost,
    TextContent,
    Usage,
    UserMessage,
    create_assistant_message_event_stream,
)

from pi_coding_agent.agent_session import AgentSession
from pi_coding_agent.overflow import classify_overflow, is_overflow_error
from pi_coding_agent.session_manager import SessionManager
from pi_coding_agent.settings import SettingsManager

# --------------------------------------------------------------------------------------
# The classifier
# --------------------------------------------------------------------------------------

OVERFLOW_MESSAGES = [
    ("prompt is too long: 210000 tokens > 200000 maximum", "Anthropic"),
    ("request_too_large", "Anthropic (HTTP 413)"),
    ("Input is too long for requested model.", "Amazon Bedrock"),
    ("This model's maximum context length is 128000 tokens...", "OpenRouter"),
    ("Range of input length should be [1, 129024]", "DashScope / Qwen"),
    ("exceeded model token limit: 131072", "Kimi"),
    ("context window exceeds limit 200000", "MiniMax"),
    ("the request exceeds the available context size", "llama.cpp"),
    ("400 this model's maximum context length is 8192 tokens", "OpenRouter"),
    ("too many tokens", "generic"),
]


@pytest.mark.parametrize(("text", "provider"), OVERFLOW_MESSAGES)
def test_real_provider_overflow_messages_are_recognised(text: str, provider: str) -> None:
    assert classify_overflow(text) == provider


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        "invalid api key",
        "429 rate limit exceeded, retry later",
        "connection reset by peer",
        "the tool returned 400 bad request",
        "maximum retries reached",
    ],
)
def test_ordinary_errors_are_not_overflow(text: str | None) -> None:
    assert classify_overflow(text) is None


def test_only_error_turns_are_classified() -> None:
    message = AssistantMessage(
        content=[],
        api="faux",
        provider="test",
        model="test-model",
        usage=Usage(cost=Cost()),
        stop_reason="stop",
        error_message="prompt is too long",
        timestamp=0,
    )

    assert is_overflow_error(message) is False


# --------------------------------------------------------------------------------------
# The recovery
# --------------------------------------------------------------------------------------


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
        context_window=120,
        max_tokens=4096,
    )


def assistant(content: list, stop_reason: str = "stop", error: str | None = None) -> AssistantMessage:
    return AssistantMessage(
        content=content,
        api="faux",
        provider="test",
        model="test-model",
        usage=Usage(cost=Cost()),
        stop_reason=stop_reason,
        error_message=error,
        timestamp=int(time.time() * 1000),
    )


class ScriptedStream:
    def __init__(self, script: list) -> None:
        self.script = script
        self.calls: list[dict] = []

    def __call__(self, model_, context, options):
        self.calls.append({"tools": list(context.tools or [])})
        response = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        stream = create_assistant_message_event_stream()
        if isinstance(response, Exception):
            stream.push(ErrorEvent(reason="error", error=assistant([], "error", str(response))))
        else:
            stream.push(DoneEvent(reason=response.stop_reason, message=response))
        return stream


async def make_session(tmp_path: Path, stream, *, context_window: int = 120, history=()):
    cwd = tmp_path / "project"
    cwd.mkdir(exist_ok=True)
    manager = SessionManager.create(str(cwd), session_dir=str(tmp_path / "sessions"))
    settings = SettingsManager(
        str(cwd),
        str(tmp_path / "agent"),
        cli_overrides={
            "compaction": {"enabled": True, "reserveTokens": 20, "keepRecentTokens": 4},
            "retry": {"enabled": False},
        },
    )
    storage = AuthStorage(InMemoryCredentialStore(), env={})
    storage.set_api_key("test", "test-key")
    # Prior turns, so there is something older than the window to compact at all.
    for message in history:
        await manager.append_message(message)
    model = create_model()
    model.context_window = context_window
    session = AgentSession(
        model,
        str(cwd),
        manager,
        settings_manager=settings,
        auth_storage=storage,
        stream_fn=stream,
        tools=[],
    )
    return session, manager


HISTORY = [
    UserMessage(content=[TextContent(text="first question " * 20)], timestamp=1),
    assistant([TextContent(text="first answer")]),
    UserMessage(content=[TextContent(text="second question " * 20)], timestamp=2),
    assistant([TextContent(text="second answer")]),
]


async def test_an_overflow_error_compacts_once_and_continues(tmp_path: Path) -> None:
    overflow = assistant([], "error", "prompt is too long: 210000 tokens > 200000 maximum")
    stream = ScriptedStream(
        [overflow, assistant([TextContent(text="SUMMARY")]), assistant([TextContent(text="the answer")])]
    )
    session, manager = await make_session(tmp_path, stream, history=HISTORY)

    await session.prompt("a long request " * 40)
    await session.wait_for_idle()

    # error -> compaction summary -> continued turn
    assert len(stream.calls) == 3
    assert stream.calls[1]["tools"] == [], "the summarizer must not receive tools"
    assert stream.calls[2]["tools"] == [], "the session's own tools are not used here either"

    entries = manager.get_entries()
    assert any(getattr(entry, "type", None) == "compaction" for entry in entries), (
        "the recovery must actually write a compaction"
    )


async def test_a_second_overflow_error_ends_the_turn_instead_of_looping(tmp_path: Path) -> None:
    overflow = assistant([], "error", "prompt is too long")
    stream = ScriptedStream(
        [
            overflow,
            assistant([TextContent(text="SUMMARY")]),
            overflow,
            assistant([TextContent(text="never reached")]),
        ]
    )
    session, _manager = await make_session(tmp_path, stream, history=HISTORY)

    await session.prompt("a long request " * 40)
    await session.wait_for_idle()

    # One recovery only: error, summary, retried turn, and then the second error
    # ends it — the synthesis path does not apply to errors.
    assert len(stream.calls) == 3

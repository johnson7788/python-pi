"""End-to-end integration tests.

These exercise the whole stack with no network: a registered faux provider drives
the real agent loop, which calls a real built-in tool, whose results are persisted
through the real JSONL session store and reloaded from disk.

The point is to catch integration breakage that per-module unit tests cannot see:
the provider registry lookup, the ``AgentTool`` <-> ``pi_ai.Tool`` bridge, the
``convert_to_llm`` projection, and the session wire format all have to agree.
"""

from __future__ import annotations

import json
import os

import pytest
from pi_ai import (
    AssistantMessage,
    Cost,
    Model,
    ModelCost,
    TextContent,
    ToolCall,
    Usage,
    UserMessage,
)
from pi_ai.providers.faux import FauxTurn, register_faux_api
from pi_agent import Agent
from pi_ai import calculate_cost
from pi_coding_agent.messages import convert_to_llm
from pi_coding_agent.session_manager import SessionManager
from pi_coding_agent.tools import create_builtin_tools

FAUX_API = "faux-integration"


def create_model() -> Model:
    return Model(
        id="integration-model",
        name="Integration Model",
        api=FAUX_API,
        provider="faux",
        base_url="http://localhost",
        reasoning=False,
        input=["text"],
        cost=ModelCost(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75),
        context_window=200_000,
        max_tokens=8192,
    )


def user_message(text: str) -> UserMessage:
    return UserMessage(content=[TextContent(text=text)], timestamp=0)


async def test_full_stack_writes_a_file_and_persists_the_session(tmp_path):
    """faux provider -> agent loop -> real `write` tool -> session JSONL on disk."""
    usage = Usage(input=1200, output=300, cache_read=100, cache_write=50, total_tokens=1650, cost=Cost())
    provider = register_faux_api(
        FAUX_API,
        [
            FauxTurn(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="write",
                        arguments={"path": "hello.txt", "content": "hi there"},
                    )
                ],
                stop_reason="toolUse",
                usage=usage,
            ),
            FauxTurn(text="Done.", stop_reason="stop"),
        ],
    )

    tools = create_builtin_tools(str(tmp_path), ("write",))
    assert [t.name for t in tools] == ["write"]

    agent = Agent(
        model=create_model(),
        system_prompt="You are a coding assistant.",
        tools=tools,
        convert_to_llm=convert_to_llm,  # deliberately synchronous
    )

    await agent.prompt("Please create hello.txt")

    # The real tool ran and wrote the real file.
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hi there"

    # History is what the loop produced, in order.
    assert [m.role for m in agent.state.messages] == [
        "user",
        "assistant",
        "toolResult",
        "assistant",
    ]
    tool_result = agent.state.messages[2]
    assert tool_result.tool_name == "write"
    assert tool_result.is_error is False

    # The provider was consulted exactly twice: once for the tool call, once to finish.
    assert provider.call_count == 2

    # Usage from the faux provider survived into the assistant message, and the
    # provider priced it against the model's rates using the shared cost rule.
    assistant = agent.state.messages[1]
    assert assistant.usage.input == 1200
    assert assistant.usage.cost.input == pytest.approx((3.0 / 1_000_000) * 1200)
    assert assistant.usage.cost.output == pytest.approx((15.0 / 1_000_000) * 300)
    assert assistant.usage.cost.total == pytest.approx(
        assistant.usage.cost.input
        + assistant.usage.cost.output
        + assistant.usage.cost.cache_read
        + assistant.usage.cost.cache_write
    )

    # Same rule, computed independently, to pin the arithmetic itself.
    probe = Usage(input=1200, output=300, cache_read=100, cache_write=50, cost=Cost())
    calculate_cost(create_model(), probe)
    assert probe.cost.total == pytest.approx(assistant.usage.cost.total)

    # --- persistence ---------------------------------------------------------------
    agent_dir = tmp_path / "agent"
    session = SessionManager.create(str(tmp_path), str(agent_dir))
    for message in agent.state.messages:
        await session.append_message(message)

    session_file = session.get_session_file()
    assert session_file is not None and os.path.exists(session_file)

    # Path scheme matches pi: sessions/--<cwd with separators replaced>--/<ts>_<id>.jsonl
    relative = os.path.relpath(session_file, str(agent_dir))
    assert relative.startswith(f"sessions{os.sep}")
    assert relative.endswith(".jsonl")

    lines = (tmp_path / "agent" / relative).read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert header["type"] == "session"
    assert header["version"] == 3
    assert header["cwd"] == str(tmp_path)

    entries = [json.loads(line) for line in lines[1:]]
    assert [e["type"] for e in entries] == ["message", "message", "message", "message"]
    # The tree links parentId -> id, with the first entry rooted at null.
    assert entries[0]["parentId"] is None
    assert entries[1]["parentId"] == entries[0]["id"]
    assert entries[2]["parentId"] == entries[1]["id"]
    assert entries[3]["parentId"] == entries[2]["id"]

    # Message JSON uses pi's camelCase field names.
    assistant_json = entries[1]["message"]
    assert assistant_json["role"] == "assistant"
    assert assistant_json["stopReason"] == "toolUse"
    assert assistant_json["usage"]["totalTokens"] == 1650
    assert assistant_json["usage"]["cacheRead"] == 100

    tool_json = entries[2]["message"]
    assert tool_json["role"] == "toolResult"
    assert tool_json["toolCallId"] == "call_1"
    assert tool_json["toolName"] == "write"
    assert tool_json["isError"] is False


async def test_session_survives_a_reload_from_disk(tmp_path):
    """A session written by the Python port is readable by the Python port again."""
    agent_dir = tmp_path / "agent"
    session = SessionManager.create(str(tmp_path), str(agent_dir))
    await session.append_message(user_message("first question"))
    await session.append_message(
        AssistantMessage(
            content=[TextContent(text="first answer")],
            api=FAUX_API,
            provider="faux",
            model="integration-model",
            usage=Usage(cost=Cost()),
            stop_reason="stop",
            timestamp=1,
        )
    )

    session_file = session.get_session_file()
    assert session_file is not None

    reopened = SessionManager.open(session_file)
    context = reopened.build_context()
    assert [m.role for m in context.messages] == ["user", "assistant"]
    assert context.messages[0].content[0].text == "first question"
    assert context.messages[1].content[0].text == "first answer"
    assert reopened.get_session_id() == session.get_session_id()


async def test_no_session_file_is_created_before_the_first_assistant_message(tmp_path):
    """Upstream quirk we must reproduce: a lone user message does not touch disk.

    ``SessionManager.is_persisted()`` reports whether persistence is *enabled*, not
    whether a file exists, so the assertion has to look at the filesystem.
    """
    agent_dir = tmp_path / "agent"
    session = SessionManager.create(str(tmp_path), str(agent_dir))

    await session.append_message(user_message("just a question"))

    session_file = session.get_session_file()
    assert session_file is not None
    assert not os.path.exists(session_file)

    await session.append_message(
        AssistantMessage(
            content=[TextContent(text="an answer")],
            api=FAUX_API,
            provider="faux",
            model="integration-model",
            usage=Usage(cost=Cost()),
            stop_reason="stop",
            timestamp=2,
        )
    )
    assert os.path.exists(session_file)
    # The deferred entries were flushed wholesale, so nothing is lost.
    lines = open(session_file, encoding="utf-8").read().splitlines()
    assert len(lines) == 3  # header + user + assistant
    assert json.loads(lines[0])["type"] == "session"


async def test_multi_turn_conversation_accumulates_history(tmp_path):
    """Two prompts in one agent session keep a single, ordered transcript."""
    register_faux_api(
        "faux-multi",
        [
            FauxTurn(text="first reply"),
            FauxTurn(text="second reply"),
        ],
    )
    model = create_model()
    model.api = "faux-multi"

    agent = Agent(
        model=model,
        system_prompt="sys",
        tools=[],
        convert_to_llm=convert_to_llm,
    )

    await agent.prompt("one")
    await agent.prompt("two")

    assert [m.role for m in agent.state.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert agent.state.messages[1].content[0].text == "first reply"
    assert agent.state.messages[3].content[0].text == "second reply"

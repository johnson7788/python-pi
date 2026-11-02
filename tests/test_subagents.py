"""Subagent delegation: spawn, fork, limits and failure, all offline.

These tests drive the real :class:`AgentSession` and the real JSONL session store;
only the model is scripted (``FauxProvider``), which is the same injection point
``tests/test_integration.py`` uses.  What they pin down:

* a ``spawn`` child starts empty, a ``fork`` child sees the parent's *completed*
  turns and never the in-flight one;
* the delegation tool returns before the child runs, and the child's result
  arrives later as a persisted ``custom`` notice in the parent;
* depth and capacity limits reject with stable codes instead of queueing;
* a failing child settles as ``failed`` instead of wedging the parent.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

from pi_ai import Model, ModelCost, ToolCall
from pi_ai.providers.faux import FauxTurn, FauxProvider, register_faux_api

from pi_coding_agent.agent_session import AgentSession
from pi_coding_agent.messages import CustomMessage
from pi_coding_agent.session_manager import SessionManager
from pi_coding_agent.subagents import SubagentManager
from pi_coding_agent.tools import create_builtin_tools

FAUX_API = "faux-subagent"


def create_model() -> Model:
    return Model(
        id="subagent-model",
        name="Subagent Model",
        api=FAUX_API,
        provider="faux",
        base_url="http://localhost",
        reasoning=False,
        input=["text"],
        cost=ModelCost(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75),
        context_window=200_000,
        max_tokens=8_192,
    )


def call(name: str, call_id: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def context_text(context: Any) -> str:
    """Flatten an LLM request into text for assertions."""
    parts: list[str] = []
    for message in getattr(context, "messages", None) or []:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            parts.append(content)
            continue
        for block in content or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


async def build_harness(
    tmp_path: Path,
    parent_turns: Sequence[FauxTurn],
    child_scripts: Sequence[Sequence[FauxTurn]],
    *,
    max_depth: int = 1,
    max_active: int = 8,
    auto_continue: bool = False,
    child_gate: asyncio.Event | None = None,
) -> SimpleNamespace:
    model = create_model()
    parent_provider = register_faux_api(FAUX_API, parent_turns)
    parent_requests: list[Any] = []

    def parent_stream(stream_model: Any, context: Any, options: Any) -> Any:
        parent_requests.append(context)
        return parent_provider.stream(stream_model, context, options)

    scripts = list(child_scripts)
    child_requests: list[Any] = []

    async def factory(manager_sm: SessionManager, depth: int, parent: AgentSession) -> AgentSession:
        del depth
        if child_gate is not None:
            await child_gate.wait()
        turns = scripts.pop(0) if scripts else [FauxTurn(text="child done")]
        provider = FauxProvider(turns)

        def child_stream(stream_model: Any, context: Any, options: Any) -> Any:
            child_requests.append(context)
            return provider.stream(stream_model, context, options)

        return AgentSession(
            model=parent.model,
            cwd=parent.cwd,
            session_manager=manager_sm,
            stream_fn=child_stream,
            tools=create_builtin_tools(parent.cwd, ("read",)),
            active_tool_names=("read",),
        )

    manager = SubagentManager(
        factory,
        max_depth=max_depth,
        max_active=max_active,
        auto_continue=auto_continue,
    )
    agent_dir = tmp_path / "agent"
    session_manager = SessionManager.create(str(tmp_path), str(agent_dir))
    parent = AgentSession(
        model=model,
        cwd=str(tmp_path),
        session_manager=session_manager,
        stream_fn=parent_stream,
        tools=create_builtin_tools(str(tmp_path), ("read",)),
        active_tool_names=("read",),
    )
    manager.attach(parent, depth=0)
    return SimpleNamespace(
        tmp_path=tmp_path,
        agent_dir=agent_dir,
        manager=manager,
        parent=parent,
        parent_requests=parent_requests,
        child_requests=child_requests,
    )


def tool_results(session: AgentSession, tool_name: str) -> list[Any]:
    return [
        message
        for message in session.messages
        if getattr(message, "role", None) == "toolResult"
        and getattr(message, "tool_name", None) == tool_name
    ]


def notices(session: AgentSession) -> list[CustomMessage]:
    return [m for m in session.messages if isinstance(m, CustomMessage)]


# --------------------------------------------------------------------------------------
# spawn
# --------------------------------------------------------------------------------------


async def test_spawn_runs_a_child_session_and_delivers_a_persisted_notice(tmp_path: Path):
    harness = await build_harness(
        tmp_path,
        parent_turns=[
            FauxTurn(tool_calls=[call("subagent", "c1", prompt="compute 6*7")], stop_reason="toolUse"),
            FauxTurn(text="noted"),
            FauxTurn(text="the child said 42"),
        ],
        child_scripts=[[FauxTurn(text="42")]],
    )
    parent = harness.parent
    manager = harness.manager

    await parent.prompt("delegate the multiplication")
    await manager.wait_all()

    # The delegation tool answered immediately with a child id.
    [result] = tool_results(parent, "subagent")
    assert result.is_error is False
    assert "started subagent" in context_text_from_blocks(result.content)

    records = manager.records()
    assert len(records) == 1
    record = records[0]
    assert record.provider == "spawn"
    assert record.status == "completed"
    assert record.result is not None
    assert record.result.text == "42"
    assert record.result.usage is not None and record.result.usage.total_tokens >= 0

    # The child is a real session: its own transcript and a file that records the
    # parent session in the header.
    child_file = record.result.session_file
    assert child_file is not None
    header = json.loads(Path(child_file).read_text(encoding="utf-8").splitlines()[0])
    assert header["type"] == "session"
    assert header["parentSession"] == parent.session_id

    # The notice arrived in the parent transcript and was persisted as a custom entry.
    [notice] = notices(parent)
    assert notice.custom_type == "subagent"
    assert "completed" in notice.content and "42" in notice.content
    # A delivered notice is not a queued prompt: the pending counter must be clean
    # even when the notice was steered into a running turn.
    assert parent.pending_message_count == 0
    persisted = [
        entry
        for entry in parent.session_manager.get_entries()
        if getattr(entry, "custom_type", None) == "subagent"
    ]
    assert persisted, "the notice must be durable, not only in memory"

    # A later turn sees the child's answer: the notice is projected into the prompt.
    await parent.prompt("what did it say?")
    assert "42" in context_text(harness.parent_requests[-1])
    assert "subagent spawn" in context_text(harness.parent_requests[-1])


async def test_spawn_notice_auto_continues_the_parent_when_idle(tmp_path: Path):
    # Hold the child until the parent turn has finished, so the notice lands while
    # the parent is idle — that is the branch under test.
    gate = asyncio.Event()
    harness = await build_harness(
        tmp_path,
        parent_turns=[
            FauxTurn(tool_calls=[call("subagent", "c1", prompt="do it")], stop_reason="toolUse"),
            FauxTurn(text="started"),
            FauxTurn(text="child finished"),
        ],
        child_scripts=[[FauxTurn(text="done")]],
        auto_continue=True,
        child_gate=gate,
    )

    await harness.parent.prompt("delegate")
    assert len(harness.parent_requests) == 2
    gate.set()
    await harness.manager.wait_all()
    await harness.parent.wait_for_idle()

    # Three parent requests: the tool call, the acknowledgement, and the
    # continuation triggered by the child's notice.
    assert len(harness.parent_requests) == 3
    assert "subagent spawn" in context_text(harness.parent_requests[-1])
    assert "done" in context_text(harness.parent_requests[-1])
    assistants = [m for m in harness.parent.messages if getattr(m, "role", None) == "assistant"]
    assert any("child finished" in context_text_from_blocks(m.content) for m in assistants)


# --------------------------------------------------------------------------------------
# fork
# --------------------------------------------------------------------------------------


async def test_fork_seeds_completed_turns_but_not_the_in_flight_one(tmp_path: Path):
    harness = await build_harness(
        tmp_path,
        parent_turns=[
            FauxTurn(text="first answer"),
            FauxTurn(tool_calls=[call("subagent_fork", "c1", prompt="now review")], stop_reason="toolUse"),
            FauxTurn(text="ok"),
        ],
        child_scripts=[[FauxTurn(text="reviewed")]],
    )
    parent = harness.parent

    await parent.prompt("first question")
    await parent.prompt("second question")
    await harness.manager.wait_all()

    [record] = harness.manager.records()
    assert record.provider == "fork"
    assert record.status == "completed"

    # The child's first request carries the finished first turn …
    first_child_request = context_text(harness.child_requests[0])
    assert "first question" in first_child_request
    assert "first answer" in first_child_request
    # … and neither the open second turn nor its tool call.
    assert "second question" not in first_child_request
    assert "now review" in first_child_request

    # The seed is durable: the child session starts with the inherited turns.
    seed_roles = [
        getattr(entry.message, "role", None)
        for entry in record.session_manager.get_entries()
        if hasattr(entry, "message") and getattr(entry.message, "role", None) in ("user", "assistant")
    ]
    assert seed_roles[:2] == ["user", "assistant"]


async def test_spawn_does_not_seed_parent_history(tmp_path: Path):
    harness = await build_harness(
        tmp_path,
        parent_turns=[
            FauxTurn(text="first answer"),
            FauxTurn(tool_calls=[call("subagent", "c1", prompt="fresh task")], stop_reason="toolUse"),
            FauxTurn(text="ok"),
        ],
        child_scripts=[[FauxTurn(text="fresh")]],
    )

    await harness.parent.prompt("first question")
    await harness.parent.prompt("second question")
    await harness.manager.wait_all()

    first_child_request = context_text(harness.child_requests[0])
    assert "first question" not in first_child_request
    assert "fresh task" in first_child_request


# --------------------------------------------------------------------------------------
# limits / failure
# --------------------------------------------------------------------------------------


async def test_depth_limit_rejects_a_grandchild_with_a_stable_code(tmp_path: Path):
    harness = await build_harness(
        tmp_path,
        parent_turns=[
            FauxTurn(tool_calls=[call("subagent", "c1", prompt="nested")], stop_reason="toolUse"),
            FauxTurn(text="done"),
        ],
        child_scripts=[
            [
                FauxTurn(tool_calls=[call("subagent", "c2", prompt="too deep")], stop_reason="toolUse"),
                FauxTurn(text="could not delegate"),
            ]
        ],
        max_depth=1,
    )

    await harness.parent.prompt("delegate once")
    await harness.manager.wait_all()

    [record] = harness.manager.records()
    nested = tool_results_in(record, "subagent")
    assert nested, "the child should still see the tool at the depth cap"
    assert nested[0].is_error is True
    assert "SUBAGENT_DEPTH_LIMIT" in context_text_from_blocks(nested[0].content)


async def test_capacity_rejects_a_second_child_instead_of_queueing(tmp_path: Path):
    gate = asyncio.Event()
    harness = await build_harness(
        tmp_path,
        parent_turns=[
            FauxTurn(
                tool_calls=[
                    call("subagent", "c1", prompt="slow task"),
                    call("subagent", "c2", prompt="second task"),
                ],
                stop_reason="toolUse",
            ),
            FauxTurn(text="done"),
        ],
        child_scripts=[[FauxTurn(text="slow")], [FauxTurn(text="second")]],
        max_active=1,
        child_gate=gate,
    )

    await harness.parent.prompt("delegate twice")
    results = tool_results(harness.parent, "subagent")
    assert len(results) == 2
    texts = [context_text_from_blocks(r.content) for r in results]
    assert any("started subagent" in text for text in texts)
    assert any("SUBAGENT_CAPACITY" in text for text in texts)
    assert len(harness.manager.records()) == 1, "the rejected request must not create a child"

    gate.set()
    await harness.manager.wait_all()


async def test_failed_child_settles_as_failed_and_reports_the_error(tmp_path: Path):
    harness = await build_harness(
        tmp_path,
        parent_turns=[
            FauxTurn(tool_calls=[call("subagent", "c1", prompt="break")], stop_reason="toolUse"),
            FauxTurn(text="ok"),
        ],
        child_scripts=[[FauxTurn(text="", stop_reason="error", error_message="upstream exploded")]],
    )

    await harness.parent.prompt("delegate")
    await harness.manager.wait_all()

    [record] = harness.manager.records()
    assert record.status == "failed"
    [notice] = notices(harness.parent)
    assert "failed" in notice.content
    assert "upstream exploded" in notice.content


async def test_steered_notice_is_not_counted_as_a_pending_prompt(tmp_path: Path):
    harness = await build_harness(tmp_path, parent_turns=[FauxTurn(text="ok")], child_scripts=[])
    parent = harness.parent

    await parent.send_custom_message(
        "[subagent spawn] done", custom_type="subagent", deliver_as="steer"
    )
    assert parent.agent.steering_queue.has_items()
    assert parent.pending_message_count == 0


# --------------------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------------------


async def test_tools_and_prompt_snippet_are_visible(tmp_path: Path):
    harness = await build_harness(tmp_path, parent_turns=[FauxTurn(text="ok")], child_scripts=[])
    names = set(harness.parent.get_active_tools())
    assert {"subagent", "subagent_fork"} <= names
    assert "Delegate a self-contained task" in harness.parent.get_system_prompt()
    assert "inherits this conversation" in harness.parent.get_system_prompt()


async def test_disposing_the_parent_cancels_running_children(tmp_path: Path):
    gate = asyncio.Event()
    harness = await build_harness(
        tmp_path,
        parent_turns=[
            FauxTurn(tool_calls=[call("subagent", "c1", prompt="slow")], stop_reason="toolUse"),
            FauxTurn(text="ok"),
        ],
        child_scripts=[[FauxTurn(text="never")]],
        child_gate=gate,
    )

    await harness.parent.prompt("delegate")
    assert harness.manager.has_running()

    harness.parent.dispose()
    for _ in range(100):
        if not harness.manager.has_running():
            break
        await asyncio.sleep(0.01)
    assert harness.manager.has_running() is False
    # A cancelled child is teardown, not a result: no notice is injected.
    assert notices(harness.parent) == []


# --------------------------------------------------------------------------------------
# helpers that need an AgentSession-ish child
# --------------------------------------------------------------------------------------


def tool_results_in(record: Any, tool_name: str) -> list[Any]:
    session = record.session
    # The child session is disposed after settlement; read the transcript from its
    # manager instead.
    entries = record.session_manager.get_entries()
    found: list[Any] = []
    for entry in entries:
        message = getattr(entry, "message", None)
        if getattr(message, "role", None) == "toolResult" and getattr(message, "tool_name", None) == tool_name:
            found.append(message)
    return found


def context_text_from_blocks(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)

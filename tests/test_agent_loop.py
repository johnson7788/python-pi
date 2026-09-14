"""Tests for the agent loop.

The centrepiece is a direct port of upstream's golden event-sequence assertion
(``packages/agent/test/agent-loop.test.ts``): a single tool-calling turn must
emit exactly twelve events in a fixed order.  The loop is driven by a scripted
stream function, so no provider or network is involved.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from pi_ai import (
    AbortController,
    AssistantMessage,
    Context,
    Cost,
    Model,
    ModelCost,
    StreamOptions,
    TextContent,
    ToolCall,
    Usage,
    UserMessage,
    create_assistant_message_event_stream,
)
from pi_agent import (
    Agent,
    AgentContext,
    AgentLoopConfig,
    AgentTool,
    AgentToolResult,
    TurnEndEvent,
    agent_loop,
    run_agent_loop,
)

# --------------------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------------------


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


def create_user_message(text: str) -> UserMessage:
    return UserMessage(content=[TextContent(text=text)], timestamp=int(time.time() * 1000))


def create_assistant_message(
    content: list, stop_reason: str = "stop", usage: Usage | None = None
) -> AssistantMessage:
    return AssistantMessage(
        content=content,
        api="faux",
        provider="test",
        model="test-model",
        usage=usage or Usage(cost=Cost()),
        stop_reason=stop_reason,
        timestamp=int(time.time() * 1000),
    )


def scripted_stream_fn(script, max_calls: int = 10):
    """Build a stream function that replays one assistant message per call.

    ``script`` is a list of ``AssistantMessage``s; only a ``done`` event is
    pushed, exactly like upstream's ``MockAssistantStream``, which forces the
    loop through its "no start event seen" branch.

    Two safety behaviours keep a mis-scripted test from hanging forever:

    * if the abort signal has already fired, an ``aborted`` error event is
      emitted instead of the scripted message — which is what a real provider
      does, and what the loop relies on to terminate;
    * ``max_calls`` turns a runaway loop into an immediate, readable failure.
    """
    from pi_ai import ErrorEvent

    calls = {"n": 0}

    def stream_fn(model: Model, context: Context, options: StreamOptions):
        stream = create_assistant_message_event_stream()
        signal = options.signal if options else None
        index = calls["n"]
        calls["n"] += 1
        assert calls["n"] <= max_calls, (
            f"stream function called {calls['n']} times (limit {max_calls}); "
            "the loop is not terminating"
        )
        if signal is not None and signal.aborted:
            aborted = create_assistant_message([], "aborted")
            aborted.error_message = "Operation aborted"
            stream.push(ErrorEvent(reason="aborted", error=aborted))
            return stream
        message = script[min(index, len(script) - 1)]
        stream.push(_done_event(message))
        return stream

    stream_fn.calls = calls  # type: ignore[attr-defined]
    return stream_fn


def _done_event(message: AssistantMessage):
    from pi_ai import DoneEvent

    return DoneEvent(reason=message.stop_reason, message=message)


async def collect(stream) -> list:
    events = []
    async for event in stream:
        events.append(event)
    return events


def echo_tool(executed: list, *, terminate: bool | None = None, delay: float = 0.0) -> AgentTool:
    async def execute(tool_call_id, params, signal, on_update) -> AgentToolResult:
        if delay:
            await asyncio.sleep(delay)
        value = params["value"]
        executed.append(value)
        return AgentToolResult(
            content=[TextContent(text=f"echoed: {value}")],
            details={"value": value},
            terminate=terminate,
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


async def identity_converter(messages):
    return messages


# --------------------------------------------------------------------------------------
# Golden event sequence
# --------------------------------------------------------------------------------------


async def test_single_tool_turn_emits_the_twelve_event_sequence():
    executed: list[str] = []
    tool = echo_tool(executed)
    context = AgentContext(system_prompt="", messages=[], tools=[tool])

    steering_polls = [0]
    follow_up_polls = [0]
    callback_tool_result_ids: list[str] = []
    callback_context_roles: list[str] = []

    async def get_steering():
        steering_polls[0] += 1
        return []

    async def get_follow_up():
        follow_up_polls[0] += 1
        return [create_user_message("follow up should stay queued")]

    async def should_stop_after_turn(turn):
        assert turn.message.role == "assistant"
        callback_tool_result_ids.extend(r.tool_call_id for r in turn.tool_results)
        callback_context_roles.extend(m.role for m in turn.context.messages)
        return True

    config = AgentLoopConfig(
        model=create_model(),
        convert_to_llm=identity_converter,
        get_steering_messages=get_steering,
        get_follow_up_messages=get_follow_up,
        should_stop_after_turn=should_stop_after_turn,
    )

    stream_fn = scripted_stream_fn(
        [
            create_assistant_message(
                [ToolCall(id="tool-1", name="echo", arguments={"value": "hello"})], "toolUse"
            ),
            create_assistant_message([TextContent(text="should not run")]),
        ]
    )

    stream = agent_loop([create_user_message("echo something")], context, config, None, stream_fn)
    events = await collect(stream)
    messages = await stream.result()

    assert stream_fn.calls["n"] == 1  # type: ignore[attr-defined]
    assert executed == ["hello"]
    assert steering_polls[0] == 1
    assert follow_up_polls[0] == 0
    assert callback_tool_result_ids == ["tool-1"]
    assert callback_context_roles == ["user", "assistant", "toolResult"]
    assert [m.role for m in messages] == ["user", "assistant", "toolResult"]

    assert [e.type for e in events] == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
        "message_start",
        "message_end",
        "tool_execution_start",
        "tool_execution_end",
        "message_start",
        "message_end",
        "turn_end",
        "agent_end",
    ]


# --------------------------------------------------------------------------------------
# Tool execution semantics
# --------------------------------------------------------------------------------------


async def test_length_truncated_message_fails_tool_calls_without_executing():
    executed: list[str] = []
    tool = echo_tool(executed)
    context = AgentContext(system_prompt="", messages=[], tools=[tool])
    config = AgentLoopConfig(model=create_model(), convert_to_llm=identity_converter)

    stream_fn = scripted_stream_fn(
        [
            create_assistant_message(
                [ToolCall(id="tool-1", name="echo", arguments={"value": "hello"})], "length"
            ),
            create_assistant_message([TextContent(text="re-issued")]),
        ]
    )

    stream = agent_loop([create_user_message("echo something")], context, config, None, stream_fn)
    events = await collect(stream)
    messages = await stream.result()

    assert executed == []
    tool_end = next(e for e in events if e.type == "tool_execution_end")
    assert tool_end.is_error is True
    text = next(b for b in tool_end.result.content if b.type == "text")
    assert "output token limit" in text.text
    # The loop continues so the model can re-issue the call.
    assert stream_fn.calls["n"] == 2  # type: ignore[attr-defined]
    assert messages[-1].role == "assistant"


async def test_terminate_stops_the_loop_after_the_batch():
    executed: list[str] = []
    tool = echo_tool(executed, terminate=True)
    context = AgentContext(system_prompt="", messages=[], tools=[tool])
    config = AgentLoopConfig(model=create_model(), convert_to_llm=identity_converter)

    stream_fn = scripted_stream_fn(
        [
            create_assistant_message(
                [ToolCall(id="tool-1", name="echo", arguments={"value": "a"})], "toolUse"
            ),
            create_assistant_message([TextContent(text="should not run")]),
        ]
    )

    stream = agent_loop([create_user_message("go")], context, config, None, stream_fn)
    await collect(stream)

    assert executed == ["a"]
    # has_more_tool_calls is false and no queues are configured, so we stop.
    assert stream_fn.calls["n"] == 1  # type: ignore[attr-defined]


async def test_unknown_tool_becomes_an_error_tool_result():
    context = AgentContext(system_prompt="", messages=[], tools=[])
    config = AgentLoopConfig(model=create_model(), convert_to_llm=identity_converter)

    stream_fn = scripted_stream_fn(
        [
            create_assistant_message(
                [ToolCall(id="t1", name="nope", arguments={})], "toolUse"
            ),
            create_assistant_message([TextContent(text="done")]),
        ]
    )

    stream = agent_loop([create_user_message("go")], context, config, None, stream_fn)
    events = await collect(stream)

    tool_end = next(e for e in events if e.type == "tool_execution_end")
    assert tool_end.is_error is True
    assert "not found" in tool_end.result.content[0].text


async def test_invalid_arguments_become_an_error_tool_result_without_executing():
    executed: list[str] = []
    tool = echo_tool(executed)
    context = AgentContext(system_prompt="", messages=[], tools=[tool])
    config = AgentLoopConfig(model=create_model(), convert_to_llm=identity_converter)

    stream_fn = scripted_stream_fn(
        [
            create_assistant_message(
                [ToolCall(id="t1", name="echo", arguments={})], "toolUse"
            ),
            create_assistant_message([TextContent(text="done")]),
        ]
    )

    stream = agent_loop([create_user_message("go")], context, config, None, stream_fn)
    events = await collect(stream)

    assert executed == []
    tool_end = next(e for e in events if e.type == "tool_execution_end")
    assert tool_end.is_error is True
    assert "value" in tool_end.result.content[0].text


async def test_parallel_results_keep_source_order_while_end_events_follow_completion():
    order: list[str] = []
    started = asyncio.Event()

    async def make(name: str, delay: float) -> AgentTool:
        async def execute(tool_call_id, params, signal, on_update) -> AgentToolResult:
            if name == "slow":
                await started.wait()
            else:
                started.set()
            await asyncio.sleep(delay)
            order.append(name)
            return AgentToolResult(content=[TextContent(text=name)])

        return AgentTool(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            execute=execute,
        )

    slow = await make("slow", 0.05)
    fast = await make("fast", 0.0)
    context = AgentContext(system_prompt="", messages=[], tools=[slow, fast])
    config = AgentLoopConfig(model=create_model(), convert_to_llm=identity_converter)

    stream_fn = scripted_stream_fn(
        [
            create_assistant_message(
                [
                    ToolCall(id="1", name="slow", arguments={}),
                    ToolCall(id="2", name="fast", arguments={}),
                ],
                "toolUse",
            ),
            create_assistant_message([TextContent(text="done")]),
        ]
    )

    stream = agent_loop([create_user_message("go")], context, config, None, stream_fn)
    events = await collect(stream)
    messages = await stream.result()

    # Execution order is completion order...
    assert order == ["fast", "slow"]
    end_ids = [e.tool_call_id for e in events if e.type == "tool_execution_end"]
    assert end_ids == ["2", "1"]
    # ...but the toolResult messages preserve the assistant's source order.
    tool_results = [m for m in messages if m.role == "toolResult"]
    assert [m.tool_call_id for m in tool_results] == ["1", "2"]


async def test_sequential_mode_runs_calls_one_at_a_time_in_source_order():
    order: list[str] = []

    def make(name: str) -> AgentTool:
        async def execute(tool_call_id, params, signal, on_update) -> AgentToolResult:
            order.append(name)
            return AgentToolResult(content=[TextContent(text=name)])

        return AgentTool(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            execute=execute,
        )

    context = AgentContext(system_prompt="", messages=[], tools=[make("a"), make("b")])
    config = AgentLoopConfig(
        model=create_model(), convert_to_llm=identity_converter, tool_execution="sequential"
    )

    stream_fn = scripted_stream_fn(
        [
            create_assistant_message(
                [
                    ToolCall(id="1", name="a", arguments={}),
                    ToolCall(id="2", name="b", arguments={}),
                ],
                "toolUse",
            ),
            create_assistant_message([TextContent(text="done")]),
        ]
    )

    stream = agent_loop([create_user_message("go")], context, config, None, stream_fn)
    await collect(stream)

    assert order == ["a", "b"]


async def test_a_tool_with_sequential_execution_mode_forces_the_whole_batch_sequential():
    order: list[str] = []

    def make(name: str, mode: str = "parallel") -> AgentTool:
        async def execute(tool_call_id, params, signal, on_update) -> AgentToolResult:
            await asyncio.sleep(0)
            order.append(name)
            return AgentToolResult(content=[TextContent(text=name)])

        return AgentTool(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            execute=execute,
            execution_mode=mode,
        )

    context = AgentContext(
        system_prompt="", messages=[], tools=[make("a"), make("b", "sequential")]
    )
    config = AgentLoopConfig(model=create_model(), convert_to_llm=identity_converter)

    stream_fn = scripted_stream_fn(
        [
            create_assistant_message(
                [
                    ToolCall(id="1", name="a", arguments={}),
                    ToolCall(id="2", name="b", arguments={}),
                ],
                "toolUse",
            ),
            create_assistant_message([TextContent(text="done")]),
        ]
    )

    stream = agent_loop([create_user_message("go")], context, config, None, stream_fn)
    await collect(stream)

    assert order == ["a", "b"]


async def test_before_tool_call_can_block():
    executed: list[str] = []
    tool = echo_tool(executed)
    context = AgentContext(system_prompt="", messages=[], tools=[tool])

    from pi_agent import BeforeToolCallResult

    async def before_tool_call(ctx, signal):
        return BeforeToolCallResult(block=True, reason="blocked by policy")

    config = AgentLoopConfig(
        model=create_model(),
        convert_to_llm=identity_converter,
        before_tool_call=before_tool_call,
    )

    stream_fn = scripted_stream_fn(
        [
            create_assistant_message(
                [ToolCall(id="t1", name="echo", arguments={"value": "x"})], "toolUse"
            ),
            create_assistant_message([TextContent(text="done")]),
        ]
    )

    stream = agent_loop([create_user_message("go")], context, config, None, stream_fn)
    events = await collect(stream)

    assert executed == []
    tool_end = next(e for e in events if e.type == "tool_execution_end")
    assert tool_end.is_error is True
    assert "blocked by policy" in tool_end.result.content[0].text


async def test_tool_exception_becomes_an_error_result():
    async def execute(tool_call_id, params, signal, on_update) -> AgentToolResult:
        raise RuntimeError("boom")

    tool = AgentTool(
        name="explode",
        description="explode",
        parameters={"type": "object", "properties": {}},
        execute=execute,
    )
    context = AgentContext(system_prompt="", messages=[], tools=[tool])
    config = AgentLoopConfig(model=create_model(), convert_to_llm=identity_converter)

    stream_fn = scripted_stream_fn(
        [
            create_assistant_message(
                [ToolCall(id="t1", name="explode", arguments={})], "toolUse"
            ),
            create_assistant_message([TextContent(text="done")]),
        ]
    )

    stream = agent_loop([create_user_message("go")], context, config, None, stream_fn)
    events = await collect(stream)

    tool_end = next(e for e in events if e.type == "tool_execution_end")
    assert tool_end.is_error is True
    assert tool_end.result.content[0].text == "boom"


async def test_tool_update_callback_emits_progress_events():
    async def execute(tool_call_id, params, signal, on_update) -> AgentToolResult:
        on_update(AgentToolResult(content=[TextContent(text="50%")]))
        return AgentToolResult(content=[TextContent(text="done")])

    tool = AgentTool(
        name="progress",
        description="progress",
        parameters={"type": "object", "properties": {}},
        execute=execute,
    )
    context = AgentContext(system_prompt="", messages=[], tools=[tool])
    config = AgentLoopConfig(model=create_model(), convert_to_llm=identity_converter)

    stream_fn = scripted_stream_fn(
        [
            create_assistant_message(
                [ToolCall(id="t1", name="progress", arguments={})], "toolUse"
            ),
            create_assistant_message([TextContent(text="done")]),
        ]
    )

    stream = agent_loop([create_user_message("go")], context, config, None, stream_fn)
    events = await collect(stream)

    updates = [e for e in events if e.type == "tool_execution_update"]
    assert len(updates) == 1
    assert updates[0].partial_result.content[0].text == "50%"


# --------------------------------------------------------------------------------------
# Error / abort paths
# --------------------------------------------------------------------------------------


async def test_error_stop_reason_short_circuits_the_loop():
    context = AgentContext(system_prompt="", messages=[], tools=[])
    config = AgentLoopConfig(model=create_model(), convert_to_llm=identity_converter)

    error_message = create_assistant_message([TextContent(text="")], "error")
    error_message.error_message = "provider exploded"
    stream_fn = scripted_stream_fn([error_message])

    stream = agent_loop([create_user_message("go")], context, config, None, stream_fn)
    events = await collect(stream)

    assert [e.type for e in events] == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
        "message_start",
        "message_end",
        "turn_end",
        "agent_end",
    ]
    assert events[-2].tool_results == []


async def test_aborted_signal_prevents_tool_execution():
    """Aborting between the model's tool call and its execution must not run the tool.

    The abort is triggered from ``before_tool_call`` so the assistant message has
    already arrived; a stream-level abort would short-circuit earlier and never
    reach tool execution at all.
    """
    executed: list[str] = []
    tool = echo_tool(executed)
    context = AgentContext(system_prompt="", messages=[], tools=[tool])

    controller = AbortController()

    async def before_tool_call(ctx, signal):
        controller.abort()
        return None

    config = AgentLoopConfig(
        model=create_model(),
        convert_to_llm=identity_converter,
        before_tool_call=before_tool_call,
    )

    stream_fn = scripted_stream_fn(
        [
            create_assistant_message(
                [ToolCall(id="t1", name="echo", arguments={"value": "x"})], "toolUse"
            ),
        ]
    )

    stream = agent_loop(
        [create_user_message("go")], context, config, controller.signal, stream_fn
    )
    events = await collect(stream)

    assert executed == []
    tool_end = next(e for e in events if e.type == "tool_execution_end")
    assert tool_end.is_error is True
    assert "aborted" in tool_end.result.content[0].text.lower()

# --------------------------------------------------------------------------------------
# Steering / follow-up
# --------------------------------------------------------------------------------------


async def test_steering_message_is_injected_before_the_next_llm_call():
    context = AgentContext(system_prompt="", messages=[], tools=[])
    steering = [create_user_message("steer one"), create_user_message("steer two")]

    async def get_steering():
        if steering:
            return [steering.pop(0)]
        return []

    config = AgentLoopConfig(
        model=create_model(), convert_to_llm=identity_converter, get_steering_messages=get_steering
    )

    stream_fn = scripted_stream_fn(
        [
            create_assistant_message([TextContent(text="first")]),
            create_assistant_message([TextContent(text="second")]),
            create_assistant_message([TextContent(text="third")]),
        ]
    )

    stream = agent_loop([create_user_message("go")], context, config, None, stream_fn)
    messages = await stream.result()

    # The loop's very first action is a steering poll ("the user may have typed
    # while waiting"), so the first steering message lands before the first
    # assistant reply rather than after it.
    assert [m.role for m in messages] == [
        "user",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert messages[1].content[0].text == "steer one"
    assert messages[3].content[0].text == "steer two"


async def test_follow_up_message_restarts_the_outer_loop():
    context = AgentContext(system_prompt="", messages=[], tools=[])
    follow_ups = [create_user_message("after you are done")]

    async def get_follow_up():
        if follow_ups:
            return [follow_ups.pop(0)]
        return []

    config = AgentLoopConfig(
        model=create_model(), convert_to_llm=identity_converter, get_follow_up_messages=get_follow_up
    )

    stream_fn = scripted_stream_fn(
        [
            create_assistant_message([TextContent(text="first")]),
            create_assistant_message([TextContent(text="second")]),
        ]
    )

    stream = agent_loop([create_user_message("go")], context, config, None, stream_fn)
    messages = await stream.result()

    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]
    assert stream_fn.calls["n"] == 2  # type: ignore[attr-defined]


async def test_should_stop_after_turn_skips_follow_ups():
    context = AgentContext(system_prompt="", messages=[], tools=[])
    follow_up_calls = [0]

    async def get_follow_up():
        follow_up_calls[0] += 1
        return [create_user_message("should never be delivered")]

    async def should_stop(turn):
        return True

    config = AgentLoopConfig(
        model=create_model(),
        convert_to_llm=identity_converter,
        get_follow_up_messages=get_follow_up,
        should_stop_after_turn=should_stop,
    )

    stream_fn = scripted_stream_fn([create_assistant_message([TextContent(text="only")])])
    stream = agent_loop([create_user_message("go")], context, config, None, stream_fn)
    messages = await stream.result()

    assert follow_up_calls[0] == 0
    assert [m.role for m in messages] == ["user", "assistant"]


async def test_prepare_next_turn_can_swap_model_and_thinking_level():
    from pi_agent import AgentLoopTurnUpdate

    context = AgentContext(system_prompt="", messages=[], tools=[])
    seen_models: list[str] = []

    def make_stream_fn():
        calls = {"n": 0}

        def stream_fn(model: Model, ctx: Context, options: StreamOptions):
            calls["n"] += 1
            seen_models.append(model.id)
            stream = create_assistant_message_event_stream()
            stream.push(_done_event(create_assistant_message([TextContent(text="ok")])))
            return stream

        return stream_fn

    replacement = create_model()
    replacement.id = "second-model"

    async def prepare_next_turn(turn):
        return AgentLoopTurnUpdate(model=replacement, thinking_level="high")

    config = AgentLoopConfig(
        model=create_model(),
        convert_to_llm=identity_converter,
        prepare_next_turn=prepare_next_turn,
        get_steering_messages=lambda: _empty(),
    )

    stream_fn = make_stream_fn()
    stream = agent_loop([create_user_message("go")], context, config, None, stream_fn)
    # One steering round so a second turn actually starts.
    messages = await stream.result()
    assert messages[0].role == "user"
    assert seen_models == ["test-model"]


async def _empty():
    return []


# --------------------------------------------------------------------------------------
# Agent class state reduction
# --------------------------------------------------------------------------------------


async def test_agent_persists_history_from_message_end_events():
    context_tools: list[AgentTool] = []
    agent = Agent(
        model=create_model(),
        system_prompt="sys",
        tools=context_tools,
        convert_to_llm=identity_converter,
        stream_fn=scripted_stream_fn([create_assistant_message([TextContent(text="hi")])]),
    )

    seen = []
    agent.subscribe(lambda event: seen.append(event.type))

    await agent.prompt("hello")

    assert [m.role for m in agent.state.messages] == ["user", "assistant"]
    assert agent.state.is_streaming is False
    assert agent.state.streaming_message is None
    assert "agent_start" in seen and "agent_end" in seen


async def test_agent_marks_error_message_on_failed_turn():
    failing = create_assistant_message([TextContent(text="")], "error")
    failing.error_message = "nope"

    agent = Agent(
        model=create_model(),
        system_prompt="sys",
        tools=[],
        convert_to_llm=identity_converter,
        stream_fn=scripted_stream_fn([failing]),
    )

    await agent.prompt("hello")

    assert agent.state.error_message == "nope"


async def test_agent_rejects_concurrent_prompts():
    started = asyncio.Event()
    release = asyncio.Event()

    def stream_fn(model, context, options):
        stream = create_assistant_message_event_stream()

        async def _produce():
            started.set()
            await release.wait()
            stream.push(_done_event(create_assistant_message([TextContent(text="ok")])))

        asyncio.ensure_future(_produce())
        return stream

    agent = Agent(
        model=create_model(),
        system_prompt="sys",
        tools=[],
        convert_to_llm=identity_converter,
        stream_fn=stream_fn,
    )

    task = asyncio.ensure_future(agent.prompt("hello"))
    await started.wait()
    with pytest.raises(RuntimeError, match="already processing"):
        await agent.prompt("again")

    release.set()
    await task


async def test_agent_abort_marks_run_as_aborted():
    from pi_ai import ErrorEvent

    def stream_fn(model, context, options):
        stream = create_assistant_message_event_stream()
        signal = options.signal

        async def _produce():
            # A real provider's HTTP stream is torn down by the abort signal;
            # emulate that so the loop terminates promptly.
            if signal is not None:
                try:
                    await asyncio.wait_for(signal.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
            aborted = create_assistant_message([], "aborted")
            aborted.error_message = "Operation aborted"
            stream.push(ErrorEvent(reason="aborted", error=aborted))

        asyncio.ensure_future(_produce())
        return stream

    agent = Agent(
        model=create_model(),
        system_prompt="sys",
        tools=[],
        convert_to_llm=identity_converter,
        stream_fn=stream_fn,
    )

    task = asyncio.ensure_future(agent.prompt("hello"))
    await asyncio.sleep(0.01)
    agent.abort()
    await task

    assert agent.state.is_streaming is False
    assert agent.state.messages[-1].stop_reason == "aborted"


async def test_agent_run_failure_is_surfaced_as_synthetic_events():
    async def broken_converter(messages):
        raise RuntimeError("convert failed")

    agent = Agent(
        model=create_model(),
        system_prompt="sys",
        tools=[],
        convert_to_llm=broken_converter,
        stream_fn=scripted_stream_fn([create_assistant_message([TextContent(text="unused")])]),
    )

    await agent.prompt("hello")

    assert agent.state.is_streaming is False
    assert agent.state.error_message == "convert failed"
    assert agent.state.messages[-1].role == "assistant"
    assert agent.state.messages[-1].stop_reason == "error"


async def test_agent_queues_steering_and_follow_up_messages():
    agent = Agent(
        model=create_model(),
        system_prompt="sys",
        tools=[],
        convert_to_llm=identity_converter,
        stream_fn=scripted_stream_fn([create_assistant_message([TextContent(text="ok")])]),
    )

    assert not agent.has_queued_messages()
    agent.steer(create_user_message("steer"))
    assert agent.has_queued_messages()
    assert [m.content[0].text for m in agent.steering_queue.drain()] == ["steer"]

    agent.follow_up(create_user_message("later"))
    assert [m.content[0].text for m in agent.follow_up_queue.drain()] == ["later"]


async def test_run_agent_loop_returns_new_messages_only():
    context = AgentContext(
        system_prompt="",
        messages=[create_user_message("pre-existing")],
        tools=[],
    )
    config = AgentLoopConfig(model=create_model(), convert_to_llm=identity_converter)
    emitted: list = []

    stream_fn = scripted_stream_fn([create_assistant_message([TextContent(text="reply")])])

    collected: list = []

    async def emit(event):
        emitted.append(event)

    await run_agent_loop(
        [create_user_message("new prompt")], context, config, emit, None, stream_fn
    )

    assert [e.type for e in emitted][:4] == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
    ]


async def test_turn_end_event_carries_tool_results():
    executed: list[str] = []
    tool = echo_tool(executed)
    context = AgentContext(system_prompt="", messages=[], tools=[tool])
    config = AgentLoopConfig(model=create_model(), convert_to_llm=identity_converter)

    stream_fn = scripted_stream_fn(
        [
            create_assistant_message(
                [ToolCall(id="t1", name="echo", arguments={"value": "v"})], "toolUse"
            ),
            create_assistant_message([TextContent(text="done")]),
        ]
    )

    stream = agent_loop([create_user_message("go")], context, config, None, stream_fn)
    events = await collect(stream)

    turn_ends = [e for e in events if isinstance(e, TurnEndEvent)]
    assert len(turn_ends) == 2
    assert len(turn_ends[0].tool_results) == 1
    assert turn_ends[1].tool_results == []


# --------------------------------------------------------------------------------------
# Synchronous hooks
# --------------------------------------------------------------------------------------


async def test_synchronous_hooks_are_supported():
    """Every hook is typed ``T | Promise<T>`` upstream, so plain functions must work.

    Requiring coroutines in Python would force every trivial predicate or
    converter to be declared ``async`` for no reason.
    """
    from pi_agent import BeforeToolCallResult

    executed: list[str] = []
    tool = echo_tool(executed)
    context = AgentContext(system_prompt="", messages=[], tools=[tool])

    def sync_convert(messages):
        return messages

    def sync_steering():
        return []

    def sync_follow_up():
        return []

    def sync_before_tool_call(ctx, signal):
        return BeforeToolCallResult(block=False)

    def sync_should_stop(turn):
        return True

    config = AgentLoopConfig(
        model=create_model(),
        convert_to_llm=sync_convert,
        get_steering_messages=sync_steering,
        get_follow_up_messages=sync_follow_up,
        before_tool_call=sync_before_tool_call,
        should_stop_after_turn=sync_should_stop,
    )

    stream_fn = scripted_stream_fn(
        [
            create_assistant_message(
                [ToolCall(id="t1", name="echo", arguments={"value": "sync"})], "toolUse"
            ),
            create_assistant_message([TextContent(text="unused")]),
        ]
    )

    stream = agent_loop([create_user_message("go")], context, config, None, stream_fn)
    messages = await stream.result()

    assert executed == ["sync"]
    assert stream_fn.calls["n"] == 1  # type: ignore[attr-defined]
    assert [m.role for m in messages] == ["user", "assistant", "toolResult"]

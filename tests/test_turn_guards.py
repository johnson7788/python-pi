"""Turn guards: the pure state machine, and the loop integration.

Two layers, on purpose.

The unit tests drive :class:`TurnGuards` with fabricated messages, which is where
the rules (how many empty replies before a stop, which streak resets what) are
pinned down.

The integration tests drive the real loop with a scripted stream, which is where
the *wiring* is pinned down: a guard stop ends the loop without follow-ups, and a
nudge is delivered as a ``<runtime_event>`` user message the model can act on and
a person can recognise as machine text.
"""

from __future__ import annotations

import time

from pi_ai import (
    AssistantMessage,
    Cost,
    DoneEvent,
    Model,
    ModelCost,
    TextContent,
    ThinkingContent,
    ToolCall,
    Usage,
    UserMessage,
    create_assistant_message_event_stream,
)
from pi_agent import (
    AgentContext,
    AgentLoopConfig,
    TurnGuards,
    agent_loop,
    runtime_event,
)

# --------------------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------------------


def model() -> Model:
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


def text_message(text: str, stop_reason: str = "stop") -> AssistantMessage:
    return assistant([TextContent(text=text)], stop_reason)


def tool_message(call_id: str = "call-1", stop_reason: str = "toolUse") -> AssistantMessage:
    return assistant([ToolCall(id=call_id, name="echo", arguments={"value": "x"})], stop_reason)


class FakeToolResult:
    """Only the attribute the guard reads."""

    def __init__(self, is_error: bool) -> None:
        self.is_error = is_error


async def identity_converter(messages):
    return messages


def scripted_stream(script: list[AssistantMessage], counter: dict | None = None):
    """Replay one message per call; the last one repeats if the loop keeps going."""
    calls = counter if counter is not None else {"n": 0}

    def stream_fn(model_: Model, context, options):
        stream = create_assistant_message_event_stream()
        index = calls["n"]
        calls["n"] += 1
        message = script[min(index, len(script) - 1)]
        stream.push(DoneEvent(reason=message.stop_reason, message=message))
        return stream

    stream_fn.calls = calls  # type: ignore[attr-defined]
    return stream_fn


async def collect(stream) -> list:
    return [event async for event in stream]


def text_of(message) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    return "".join(str(getattr(block, "text", "") or "") for block in content or [])


def runtime_events(messages: list) -> list[str]:
    return [text_of(message) for message in messages if "<runtime_event" in text_of(message)]


async def run_turns(guards: TurnGuards, script: list, counter: dict | None = None) -> list:
    config = AgentLoopConfig(
        model=model(),
        convert_to_llm=identity_converter,
        turn_guards=guards,
    )
    context = AgentContext(messages=[], system_prompt="test", tools=[])
    stream = agent_loop(
        [UserMessage(content=[TextContent(text="go")], timestamp=0)],
        context,
        config,
        None,
        scripted_stream(script, counter),
    )
    return await collect(stream)


# --------------------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------------------


def test_the_iteration_cap_stops_the_turn_after_the_configured_count():
    guards = TurnGuards(max_turns=2)

    assert guards.before_turn() is None
    assert guards.before_turn() is None
    decision = guards.before_turn()

    assert decision is not None
    assert decision.action == "stop"
    assert guards.stop_reason == "max_turns"


def test_no_cap_means_no_stop():
    guards = TurnGuards(max_turns=None)
    for _ in range(100):
        assert guards.before_turn() is None
    assert guards.stop_reason is None


def test_an_empty_reply_is_nudged_once_and_then_stops_the_run():
    guards = TurnGuards()
    empty = assistant([])

    nudge = guards.after_turn(empty, [])
    assert nudge is not None and nudge.action == "nudge"
    assert nudge.kind == "empty_response"
    # The nudge is machine text, and says what to do rather than what went wrong.
    assert nudge.text.startswith('<runtime_event type="empty_response">')
    assert "continue the task" in nudge.text.lower()

    stopped = guards.after_turn(empty, [])
    assert stopped is not None and stopped.action == "stop"
    assert guards.stop_reason == "empty_response"


def test_a_reply_with_only_thinking_is_not_empty():
    guards = TurnGuards()
    thinking = assistant([ThinkingContent(thinking="considering the options")])

    assert guards.after_turn(thinking, []) is None
    assert guards.stop_reason is None


def test_a_reply_with_only_a_tool_call_is_not_empty():
    guards = TurnGuards()
    assert guards.after_turn(tool_message(), []) is None


def test_a_verbatim_repeat_is_nudged_once_and_then_stops():
    guards = TurnGuards()
    message = text_message("the same answer")

    assert guards.after_turn(message, []) is None
    nudge = guards.after_turn(message, [])
    assert nudge is not None and nudge.action == "nudge"
    assert nudge.kind == "repeated_response"

    stopped = guards.after_turn(message, [])
    assert stopped is not None and stopped.action == "stop"
    assert guards.stop_reason == "repeated_response"


def test_a_changed_reply_resets_the_repeat_counter():
    guards = TurnGuards(max_repeated_replies=1)
    guards.after_turn(text_message("first"), [])
    assert guards.after_turn(text_message("second"), []) is None
    # "second" is now the baseline, so saying it again is the first repetition.
    nudge = guards.after_turn(text_message("second"), [])
    assert nudge is not None and nudge.kind == "repeated_response"


def test_consecutive_truncation_stops_after_the_streak():
    guards = TurnGuards(max_length_streak=3)
    truncated = text_message("partial", stop_reason="length")

    assert guards.after_turn(truncated, []) is None
    assert guards.after_turn(truncated, []) is None
    decision = guards.after_turn(truncated, [])
    assert decision is not None and decision.action == "stop"
    assert guards.stop_reason == "truncated"


def test_a_successful_turn_breaks_the_truncation_streak():
    guards = TurnGuards(max_length_streak=2)
    guards.after_turn(text_message("partial", stop_reason="length"), [])
    guards.after_turn(text_message("ok"), [])
    assert guards.after_turn(text_message("partial", stop_reason="length"), []) is None


def test_a_tool_failure_streak_nudges_once_then_stops():
    guards = TurnGuards(max_failed_tool_streak=3)
    failures = [FakeToolResult(True), FakeToolResult(True)]

    assert guards.after_turn(tool_message(), failures) is None
    assert guards.after_turn(tool_message(), failures) is None
    nudge = guards.after_turn(tool_message(), failures)
    assert nudge is not None and nudge.action == "nudge"
    assert nudge.kind == "tool_failures"

    stopped = guards.after_turn(tool_message(), failures)
    assert stopped is not None and stopped.action == "stop"
    assert guards.stop_reason == "tool_failures"


def test_one_successful_tool_result_re_arms_the_failure_nudge():
    guards = TurnGuards(max_failed_tool_streak=2)
    failures = [FakeToolResult(True)]

    guards.after_turn(tool_message(), failures)
    guards.after_turn(tool_message(), failures)  # nudge
    guards.after_turn(tool_message(), [FakeToolResult(False)])
    guards.after_turn(tool_message(), failures)  # streak restarts at 1
    nudge = guards.after_turn(tool_message(), failures)  # 2 again
    assert nudge is not None and nudge.action == "nudge"
    assert guards.stop_reason is None


def test_error_and_aborted_turns_are_not_guard_problems():
    guards = TurnGuards()

    assert guards.after_turn(assistant([], "error"), []) is None
    assert guards.after_turn(assistant([], "aborted"), []) is None
    assert guards.stop_reason is None


def test_reset_clears_every_counter_and_the_stop_reason():
    guards = TurnGuards(max_turns=1)
    guards.before_turn()
    guards.before_turn()
    assert guards.stop_reason == "max_turns"

    guards.reset()

    assert guards.stop_reason is None
    assert guards.turns == 0
    assert guards.after_turn(text_message("fresh"), []) is None


def test_runtime_event_keeps_the_body_verbatim_and_trims_it():
    assert runtime_event("x", "  a b\n") == '<runtime_event type="x">a b</runtime_event>'


# --------------------------------------------------------------------------------------
# The wiring
# --------------------------------------------------------------------------------------


async def test_the_loop_stops_at_the_cap_instead_of_spinning_on_tool_calls():
    guards = TurnGuards(max_turns=2)
    counter = {"n": 0}

    events = await run_turns(guards, [tool_message()], counter)

    assert counter["n"] == 2, "the third turn must never reach the model"
    assert guards.stop_reason == "max_turns"
    assert events[-1].type == "agent_end"


async def test_a_nudge_reaches_the_model_as_a_runtime_event_message():
    guards = TurnGuards(max_turns=10)
    counter = {"n": 0}

    events = await run_turns(guards, [assistant([]), text_message("done")], counter)

    assert guards.stop_reason is None
    assert counter["n"] == 2
    nudges = [
        text_of(event.message)
        for event in events
        if event.type == "message_end" and "<runtime_event" in text_of(event.message)
    ]
    assert len(nudges) == 1
    assert nudges[0].startswith('<runtime_event type="empty_response">')
    assert events[-1].type == "agent_end"


async def test_a_second_empty_reply_stops_without_a_third_model_call():
    guards = TurnGuards(max_turns=10)
    counter = {"n": 0}

    await run_turns(guards, [assistant([])], counter)

    assert guards.stop_reason == "empty_response"
    assert counter["n"] == 2

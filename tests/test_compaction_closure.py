"""The parts of compaction that only exist because of an observed failure.

Each test here is one of those failures:

* a summary that forgot the files the run already wrote (so the model rewrites
  them, and a chunked large-file write never terminates);
* a second compaction that summarized a summary and lost the original goal;
* a cut inside a turn that left the kept tail without its request;
* a summarizer request that did not fit the window;
* a summarization failure that left the window uncompacted.

They are grouped in one file because they share the same three-line builders.
"""

from __future__ import annotations

import time
from typing import Any

from pi_ai import (
    AssistantMessage,
    Cost,
    DoneEvent,
    ErrorEvent,
    Model,
    ModelCost,
    TextContent,
    ToolCall,
    Usage,
    UserMessage,
    create_assistant_message_event_stream,
)

from pi_coding_agent.compaction import (
    CompactionSettings,
    compact_session,
    extract_file_ops,
    fit_summarizer_input,
    parse_file_ops,
    serialize_conversation,
)
from pi_coding_agent.messages import create_compaction_summary_message
from pi_coding_agent.session_manager import SessionMessageEntry

# --------------------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------------------


def model(context_window: int = 100_000, max_tokens: int = 4096) -> Model:
    return Model(
        id="test-model",
        name="Test Model",
        api="faux",
        provider="test",
        base_url="http://localhost",
        reasoning=False,
        input=["text"],
        cost=ModelCost(),
        context_window=context_window,
        max_tokens=max_tokens,
    )


def entry(entry_id: str, message: Any) -> SessionMessageEntry:
    return SessionMessageEntry(
        id=entry_id, parent_id=None, timestamp="2024-01-01T00:00:00.000Z", message=message
    )


def user(text: str) -> UserMessage:
    return UserMessage(content=[TextContent(text=text)], timestamp=1)


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


def tool_call_message(name: str, arguments: dict, call_id: str = "call-1") -> AssistantMessage:
    return assistant([ToolCall(id=call_id, name=name, arguments=arguments)], "toolUse")


class RecordingStream:
    """Scripted summarizer: records every prompt it was asked to summarize."""

    def __init__(self, responses: list[AssistantMessage | Exception]) -> None:
        self.responses = responses
        self.prompts: list[str] = []

    def __call__(self, model_: Model, context, options):
        self.prompts.append(context.messages[0].content[0].text)
        index = len(self.prompts) - 1
        response = self.responses[min(index, len(self.responses) - 1)]
        stream = create_assistant_message_event_stream()
        if isinstance(response, Exception):
            stream.push(ErrorEvent(reason="error", error=_error_message(str(response))))
        else:
            stream.push(DoneEvent(reason=response.stop_reason, message=response))
        return stream


def _error_message(text: str) -> AssistantMessage:
    return assistant([], "error")


def settings(keep_recent: int = 1, reserve: int = 100) -> CompactionSettings:
    return CompactionSettings(enabled=True, reserve_tokens=reserve, keep_recent_tokens=keep_recent)


# --------------------------------------------------------------------------------------
# File operations
# --------------------------------------------------------------------------------------


def test_file_ops_come_from_read_write_and_edit_calls():
    messages = [
        tool_call_message("read", {"path": "/work/a.md"}, "c1"),
        tool_call_message("write", {"file_path": "/work/b.html"}, "c2"),
        tool_call_message("edit", {"path": "/work/a.md"}, "c3"),
        tool_call_message("bash", {"command": "ls"}),
    ]

    ops = extract_file_ops(messages)

    assert ops["modified"] == ["/work/b.html", "/work/a.md"]
    # A file that was later edited is reported only as modified.
    assert ops["read"] == []


def test_file_ops_survive_more_than_one_compaction():
    summary = "## Goal\nship it\n\n<modified-files>\n- /work/report.docx\n</modified-files>"
    parsed = parse_file_ops(summary)
    assert parsed == {"read": [], "modified": ["/work/report.docx"]}


async def test_the_summary_carries_the_files_the_run_wrote():
    entries = [
        entry("a", user("write the deck")),
        entry("b", tool_call_message("write", {"path": "/work/deck.html"})),
        entry("c", text_message("wrote it")),
    ]
    stream = RecordingStream([text_message("## Goal\nwrite the deck")])

    result = await compact_session(entries, model(), settings=settings(1), stream_fn=stream)

    assert "<modified-files>" in result.summary
    assert "- /work/deck.html" in result.summary
    assert result.degraded is False


# --------------------------------------------------------------------------------------
# The previous summary
# --------------------------------------------------------------------------------------


async def test_a_previous_summary_is_passed_through_instead_of_summarized_again():
    previous = create_compaction_summary_message("## Goal\nthe original goal", 100, 1)
    entries = [
        entry("a", user("original request")),
        entry("b", text_message("first answer")),
        entry("old-compaction", previous),
        entry("c", user("next request")),
        entry("d", text_message("second answer")),
        entry("e", user("third request")),
        entry("f", text_message("third answer")),
        # Ends on a user message so the cut lands on a turn boundary; this test is
        # about the previous summary, not about split turns.
        entry("g", user("fourth request")),
    ]
    stream = RecordingStream([text_message("## Goal\nthe original goal (updated)")])

    result = await compact_session(entries, model(), settings=settings(1), stream_fn=stream)

    assert len(stream.prompts) == 1
    prompt = stream.prompts[0]
    # The prior summary travelled as a separate block *after* the conversation, not
    # as conversation to summarize again.
    assert "<previous-summary>" in prompt
    assert "the original goal" in prompt
    assert prompt.index("<previous-summary>") > prompt.index("</conversation>")


# --------------------------------------------------------------------------------------
# Split turns
# --------------------------------------------------------------------------------------



async def test_a_cut_inside_a_turn_summarizes_the_opening_on_its_own():
    entries = [
        entry("a", user("older turn")),
        entry("b", text_message("older answer")),
        entry("c", user("the current long turn")),
        entry("d", tool_call_message("read", {"path": "/work/x.md"})),
        entry("e", text_message("step one")),
    ]
    stream = RecordingStream([text_message("HISTORY"), text_message("PREFIX")])

    result = await compact_session(entries, model(), settings=settings(1), stream_fn=stream)

    assert len(stream.prompts) == 2, "history and the split turn are summarized separately"
    assert "opening of a turn" in stream.prompts[1]
    assert "the current long turn" in stream.prompts[1]
    assert "## Current turn so far" in result.summary
    assert "HISTORY" in result.summary and "PREFIX" in result.summary


# --------------------------------------------------------------------------------------
# The summarizer request itself
# --------------------------------------------------------------------------------------


async def test_an_oversized_summary_request_drops_the_oldest_messages_with_a_note():
    big = "x" * 40_000  # ~10k tokens
    entries = [
        entry("a", user(big + " oldest")),
        entry("b", text_message(big + " middle")),
        entry("c", user("recent question")),
        entry("d", text_message("recent answer")),
        entry("e", user("newest")),
    ]
    stream = RecordingStream([text_message("SUMMARY")])

    result = await compact_session(
        entries,
        model(context_window=12_000, max_tokens=1_000),
        settings=settings(1),
        stream_fn=stream,
    )

    assert result.summary == "SUMMARY"
    assert "<note>" in stream.prompts[0]
    assert "not shown" in stream.prompts[0]
    assert "oldest" not in stream.prompts[0], "the oldest messages are the ones dropped"


def test_fit_summarizer_input_keeps_the_newest_messages():
    messages = [user("old " * 8_000), user("recent")]
    fitted, omitted = fit_summarizer_input(messages, model(context_window=8_000, max_tokens=1_000), 1_000)

    assert omitted == 1
    assert "recent" in serialize_conversation(fitted)


# --------------------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------------------


async def test_a_failed_summarizer_degrades_to_a_raw_archive_instead_of_failing():
    entries = [
        entry("a", user("do the thing")),
        entry("b", text_message("did half of it")),
        entry("c", user("keep going")),
    ]
    broken = AssistantMessage(
        content=[TextContent(text="")],
        api="faux",
        provider="test",
        model="test-model",
        usage=Usage(cost=Cost()),
        stop_reason="error",
        error_message="provider exploded",
        timestamp=0,
    )
    stream = RecordingStream([broken])

    result = await compact_session(entries, model(), settings=settings(1), stream_fn=stream)

    assert result.degraded is True
    assert "did half of it" in result.summary
    assert len(stream.prompts) == 2, "one retry before giving up on the summary"


async def test_a_transient_summarizer_failure_is_retried():
    entries = [
        entry("a", user("do the thing")),
        entry("b", text_message("did it")),
        entry("c", user("keep going")),
    ]
    broken = AssistantMessage(
        content=[],
        api="faux",
        provider="test",
        model="test-model",
        usage=Usage(cost=Cost()),
        stop_reason="length",
        error_message="",
        timestamp=0,
    )
    stream = RecordingStream([broken, text_message("SUMMARY ON RETRY")])

    result = await compact_session(entries, model(), settings=settings(1), stream_fn=stream)

    assert result.degraded is False
    assert result.summary == "SUMMARY ON RETRY"
    assert len(stream.prompts) == 2

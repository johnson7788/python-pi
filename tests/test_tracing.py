"""Spans: what gets recorded, what must never be, and that tracing cannot break a turn.

The mapping from outbound records to spans is a pure function of the records, so it is
tested here without a socket.  The redaction cases are the point: tool arguments are
where file bodies, tokens and whole documents would otherwise end up on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pi_server.tracing import EventTracer, NullTracer, Tracer

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def read_spans(directory: Path) -> list[dict]:
    files = sorted(directory.glob("*.jsonl"))
    assert files, "no trace file was written"
    return [json.loads(line) for line in files[-1].read_text(encoding="utf-8").splitlines()]


def tracer_in(tmp_path: Path) -> Tracer:
    return Tracer(str(tmp_path / "traces"))


def run_one_turn(tracer: Tracer) -> None:
    """The record sequence a single tool-calling turn produces."""
    events = EventTracer(tracer, uid="u1", session_file="/sessions/a.jsonl")
    events.observe({"type": "agent_start"})
    events.observe(
        {
            "type": "message_start",
            "message": {"role": "assistant", "model": "m1", "provider": "p1"},
        }
    )
    events.observe(
        {
            "type": "tool_execution_start",
            "toolCallId": "c1",
            "toolName": "bash",
            "args": {"command": "ls -la /workspace", "cwd": "/workspace"},
        }
    )
    events.observe(
        {
            "type": "tool_execution_end",
            "toolCallId": "c1",
            "toolName": "bash",
            "isError": False,
            "result": {"content": [{"type": "text", "text": "file.txt\n"}]},
        }
    )
    events.observe(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "usage": {
                    "input": 1200,
                    "output": 300,
                    "cacheRead": 100,
                    "cost": {"total": 0.0021},
                },
                "stopReason": "stop",
                "content": [{"type": "text", "text": "done"}],
            },
        }
    )
    events.observe({"type": "agent_end"})


# --------------------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------------------


def test_one_turn_produces_a_turn_span_around_its_llm_and_tool_spans(tmp_path: Path) -> None:
    run_one_turn(tracer_in(tmp_path))

    spans = read_spans(tmp_path / "traces")
    kinds = [span["kind"] for span in spans]

    assert kinds.count("turn") == 1
    assert kinds.count("llm") == 1
    assert kinds.count("tool") == 1

    turn = next(span for span in spans if span["kind"] == "turn")
    assert turn["uid"] == "u1"
    assert turn["session_file"] == "/sessions/a.jsonl"
    for span in spans:
        assert span["trace_id"] == turn["trace_id"], "one trace per turn"
        if span["kind"] != "turn":
            assert span["parent_id"] == turn["span_id"]


def test_the_llm_span_carries_usage_and_cost(tmp_path: Path) -> None:
    run_one_turn(tracer_in(tmp_path))

    llm = next(span for span in read_spans(tmp_path / "traces") if span["kind"] == "llm")

    assert llm["input_tokens"] == 1200
    assert llm["output_tokens"] == 300
    assert llm["cost_usd"] == pytest.approx(0.0021)
    assert llm["stop_reason"] == "stop"
    assert llm["model"] == "m1"


def test_a_tool_span_records_outcome_without_copying_the_output(tmp_path: Path) -> None:
    run_one_turn(tracer_in(tmp_path))

    tool = next(span for span in read_spans(tmp_path / "traces") if span["kind"] == "tool")

    assert tool["name"] == "bash"
    assert tool["is_error"] is False
    assert tool["result_chars"] == len("file.txt\n")
    assert tool["result_head"] == "file.txt\\n"
    assert "duration_ms" in tool


# --------------------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------------------


def test_arguments_are_described_and_secrets_never_reach_the_file(tmp_path: Path) -> None:
    tracer = tracer_in(tmp_path)
    events = EventTracer(tracer, uid="u1")
    events.begin_turn()
    events.observe(
        {
            "type": "tool_execution_start",
            "toolCallId": "c9",
            "toolName": "write",
            "args": {
                "path": "/workspace/report.md",
                "content": "PATIENT NAME: Alice\nfull document body",
                "api_token": "sk-live-123",
                "Authorization": "Bearer abc",
                "options": {"deep": True},
            },
        }
    )
    events.end_turn()

    text = (tmp_path / "traces").glob("*.jsonl")
    raw = next(iter(text)).read_text(encoding="utf-8")

    assert "Alice" not in raw
    assert "sk-live-123" not in raw
    assert "Bearer abc" not in raw
    assert "full document body" not in raw

    tool = next(span for span in read_spans(tmp_path / "traces") if span["kind"] == "tool")
    assert tool["args"]["path"] == "/workspace/report.md"
    assert tool["args"]["content"] == "str", "an unsafe value is recorded as its type"
    assert tool["args"]["api_token"] == "<redacted>"
    assert tool["args"]["options"] == "dict"


def test_a_long_safe_value_is_truncated(tmp_path: Path) -> None:
    tracer = tracer_in(tmp_path)
    events = EventTracer(tracer)
    events.begin_turn()
    events.observe(
        {
            "type": "tool_execution_start",
            "toolCallId": "c1",
            "toolName": "bash",
            "args": {"command": "echo " + "x" * 1000},
        }
    )
    events.end_turn()

    tool = next(span for span in read_spans(tmp_path / "traces") if span["kind"] == "tool")
    assert len(tool["args"]["command"]) <= 201
    assert tool["args"]["command"].endswith("…")


# --------------------------------------------------------------------------------------
# Failure containment
# --------------------------------------------------------------------------------------


def test_a_disabled_tracer_writes_nothing(tmp_path: Path) -> None:
    run_one_turn(NullTracer())

    assert list(tmp_path.glob("**/*.jsonl")) == []


def test_an_unwritable_directory_degrades_to_a_no_op(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    tracer = Tracer(str(blocked))

    run_one_turn(tracer)  # must not raise

    assert tracer.enabled is False


def test_a_malformed_record_does_not_raise(tmp_path: Path) -> None:
    events = EventTracer(tracer_in(tmp_path))
    for payload in (
        {},
        {"type": "message_end", "message": None},
        {"type": "tool_execution_end", "toolCallId": None, "result": None},
        {"type": "message_start", "message": "not a dict"},
    ):
        events.observe(payload)

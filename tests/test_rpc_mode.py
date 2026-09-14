"""Tests for RPC mode: command dispatch, responses and LF-only framing.

Everything runs in-process against an ``AgentSession`` driven by a scripted stream
function, plus a dedicated subprocess test that exercises the real ``pi-py
--mode rpc`` process over pipes (the only place the framing and the process
contract are proven end to end).
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from pi_ai import (
    AbortController,
    AssistantMessage,
    Cost,
    Model,
    ModelCost,
    TextContent,
    ToolCall,
    Usage,
    UserMessage,
    create_assistant_message_event_stream,
)
from pi_ai.auth import ApiKeyCredential
from pi_ai import AuthStorage, InMemoryCredentialStore
from pi_ai.providers.faux import FauxTurn, register_faux_api
from pi_coding_agent.agent_session import AgentSession
from pi_coding_agent.messages import convert_to_llm
from pi_coding_agent.modes.jsonl import (
    JsonlLineReader,
    decode_record,
    serialize_json_line,
    split_records,
    strip_record_separator,
)
from pi_coding_agent.modes.rpc_mode import RpcMode, model_to_json, run_rpc_mode
from pi_coding_agent.session_manager import SessionManager

REPO_ROOT = Path(__file__).resolve().parent.parent
FAUX_API = "faux-rpc"


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------


def _model() -> Model:
    return Model(
        id="rpc-model",
        name="RPC Model",
        api=FAUX_API,
        provider="faux",
        base_url="http://localhost",
        reasoning=True,
        thinking_level_map={"off": None, "low": "low", "high": "high"},
        input=["text"],
        cost=ModelCost(input=3.0, output=15.0),
        context_window=100_000,
        max_tokens=4096,
    )


def _auth() -> AuthStorage:
    store = InMemoryCredentialStore()
    store.set("faux", ApiKeyCredential(key="test-key"))
    return AuthStorage(store)


def _session(tmp_path, turns) -> AgentSession:
    register_faux_api(FAUX_API, turns)
    return AgentSession(
        model=_model(),
        cwd=str(tmp_path),
        session_manager=SessionManager.in_memory(str(tmp_path)),
        auth_storage=_auth(),
        active_tool_names=(),
    )


def _assistant(text: str, stop_reason: str = "stop") -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api=FAUX_API,
        provider="faux",
        model="rpc-model",
        usage=Usage(cost=Cost()),
        stop_reason=stop_reason,
        timestamp=0,
    )


def _done(message: AssistantMessage):
    from pi_ai import DoneEvent

    return DoneEvent(reason=message.stop_reason, message=message)


async def _serve(session, commands: list[dict], *, model_registry=None) -> list[dict]:
    """Feed commands through the mode and return every emitted JSON record."""
    raw = "".join(serialize_json_line(command) for command in commands).encode("utf-8")
    output = io.StringIO()
    mode = RpcMode(session, model_registry=model_registry, output=output)
    code = await mode.run(io.BytesIO(raw))
    assert code == 0
    return [json.loads(line) for line in output.getvalue().splitlines() if line]


def _responses(records: list[dict], command: str) -> list[dict]:
    return [r for r in records if r.get("type") == "response" and r.get("command") == command]


# --------------------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------------------


def test_serialize_json_line_is_lf_terminated_and_unescaped():
    line = serialize_json_line({"text": "你好", "sep": "\u2028"})
    assert line.endswith("\n")
    assert line.count("\n") == 1
    # U+2028 stays inside the record: it is valid JSON, not a line break.
    assert "\u2028" in line
    assert "你好" in line  # ensure_ascii=False


def test_split_records_keeps_the_incomplete_tail():
    records, remainder = split_records(b'{"a":1}\n{"b":2}\n{"c"')
    assert records == [b'{"a":1}', b'{"b":2}']
    assert remainder == b'{"c"'


def test_strip_record_separator_handles_crlf():
    assert strip_record_separator('{"a":1}\r') == '{"a":1}'
    assert strip_record_separator('{"a":1}') == '{"a":1}'


def test_decode_record_tolerates_invalid_utf8():
    assert decode_record(b"\xff\xfe{}") == "\ufffd\ufffd{}"


async def test_reader_splits_on_lf_only_and_drops_cr():
    body = '{"a":"\u2028"}\r\n{"b":2}\n{"c":3}'.encode("utf-8")
    lines = [line async for line in JsonlLineReader(io.BytesIO(body))]
    assert lines == ['{"a":"\u2028"}', '{"b":2}', '{"c":3}']


async def test_reader_never_splits_on_u2028_inside_a_string():
    # A naive line reader (or Python's universal newlines) would tear this record.
    payload = {"text": "before\u2028after"}
    body = serialize_json_line(payload).encode("utf-8")
    lines = [line async for line in JsonlLineReader(io.BytesIO(body))]
    assert len(lines) == 1
    assert json.loads(lines[0]) == payload


# --------------------------------------------------------------------------------------
# Command dispatch
# --------------------------------------------------------------------------------------


async def test_get_state_reports_the_session(tmp_path):
    session = _session(tmp_path, [FauxTurn(text="hi")])
    try:
        records = await _serve(session, [{"id": "1", "type": "get_state"}])
        response = _responses(records, "get_state")[0]
        assert response["id"] == "1"
        assert response["success"] is True
        data = response["data"]
        assert data["model"]["id"] == "rpc-model"
        assert data["model"]["contextWindow"] == 100_000
        assert data["isStreaming"] is False
        assert data["isCompacting"] is False
        assert data["steeringMode"] == "one-at-a-time"
        assert data["autoCompactionEnabled"] is True
        assert data["messageCount"] == 0
        assert data["sessionId"]
    finally:
        session.dispose()


async def test_prompt_responds_at_preflight_and_streams_events(tmp_path):
    session = _session(tmp_path, [FauxTurn(text="hello rpc")])
    try:
        records = await _serve(
            session,
            [{"id": "p1", "type": "prompt", "message": "say hi"}, {"id": "s1", "type": "get_state"}],
        )

        prompt_response = _responses(records, "prompt")[0]
        assert prompt_response["id"] == "p1"
        assert prompt_response["success"] is True

        # Events arrive interleaved with the responses.
        types = [record["type"] for record in records]
        assert "agent_start" in types
        assert "message_update" in types
        assert "agent_end" in types

        deltas = "".join(
            record["assistantMessageEvent"]["delta"]
            for record in records
            if record["type"] == "message_update"
            and record["assistantMessageEvent"]["type"] == "text_delta"
        )
        assert deltas == "hello rpc"

        # Every streaming update carries cumulative usage but no live partial.
        for record in records:
            if record["type"] == "message_update":
                assert set(record) == {"type", "usage", "assistantMessageEvent"}
                assert "partial" not in record["assistantMessageEvent"]
    finally:
        session.dispose()


async def test_prompt_without_message_fails(tmp_path):
    session = _session(tmp_path, [FauxTurn(text="x")])
    try:
        records = await _serve(session, [{"id": "bad", "type": "prompt"}])
        response = _responses(records, "prompt")[0]
        assert response["success"] is False
        assert "message" in response["error"]
        assert response["id"] == "bad"
    finally:
        session.dispose()


async def test_concurrent_prompt_without_streaming_behavior_is_rejected(tmp_path):
    """A second prompt while streaming must fail unless it declares queueing."""
    release = asyncio.Event()

    def stream_fn(model, context, options):
        stream = create_assistant_message_event_stream()

        async def _produce():
            await release.wait()
            stream.push(_done(_assistant("finished")))

        asyncio.ensure_future(_produce())
        return stream

    register_faux_api(FAUX_API, [FauxTurn(text="unused")])
    session = AgentSession(
        model=_model(),
        cwd=str(tmp_path),
        session_manager=SessionManager.in_memory(str(tmp_path)),
        auth_storage=_auth(),
        stream_fn=stream_fn,
        active_tool_names=(),
    )
    try:
        output = io.StringIO()
        mode = RpcMode(session, output=output)

        await mode.handle_line(json.dumps({"id": "1", "type": "prompt", "message": "one"}))
        # accepted; the run is now blocked in the provider stream
        await asyncio.sleep(0)
        await mode.handle_line(json.dumps({"id": "2", "type": "prompt", "message": "two"}))

        release.set()
        await session.wait_for_idle()

        responses = [json.loads(line) for line in output.getvalue().splitlines() if line]
        second = [r for r in responses if r.get("id") == "2"]
        assert second and second[0]["success"] is False
        assert "streamingBehavior" in second[0]["error"] or "already processing" in second[0]["error"]
    finally:
        session.dispose()


async def test_unknown_and_unsupported_commands_report_errors(tmp_path):
    session = _session(tmp_path, [FauxTurn(text="x")])
    try:
        records = await _serve(
            session,
            [
                {"id": "u", "type": "not_a_command"},
                {"id": "n", "type": "export_html"},
            ],
        )
        unknown = _responses(records, "not_a_command")[0]
        assert unknown["success"] is False
        assert "Unknown command" in unknown["error"]

        unsupported = _responses(records, "export_html")[0]
        assert unsupported["success"] is False
        assert "not supported" in unsupported["error"]
    finally:
        session.dispose()


async def test_new_session_without_a_runtime_reports_clearly(tmp_path):
    """A plain session has no replacement layer; say so instead of hanging."""
    session = _session(tmp_path, [FauxTurn(text="x")])
    try:
        records = await _serve(session, [{"id": "n", "type": "new_session"}])
        response = _responses(records, "new_session")[0]
        assert response["success"] is False
        assert "AgentSessionRuntime" in response["error"]
    finally:
        session.dispose()


async def test_malformed_line_yields_a_parse_error_and_keeps_going(tmp_path):
    session = _session(tmp_path, [FauxTurn(text="x")])
    try:
        raw = b'{"broken"\n{"id":"ok","type":"get_state"}\n'
        output = io.StringIO()
        mode = RpcMode(session, output=output)
        assert await mode.run(io.BytesIO(raw)) == 0

        records = [json.loads(line) for line in output.getvalue().splitlines() if line]
        assert records[0]["command"] == "parse"
        assert records[0]["success"] is False
        # The session survives a bad line.
        assert _responses(records, "get_state")[0]["success"] is True
    finally:
        session.dispose()


async def test_thinking_level_commands(tmp_path):
    session = _session(tmp_path, [FauxTurn(text="x")])
    try:
        records = await _serve(
            session,
            [
                {"id": "a", "type": "get_available_thinking_levels"},
                {"id": "b", "type": "set_thinking_level", "level": "high"},
                {"id": "c", "type": "cycle_thinking_level"},
                {"id": "d", "type": "get_state"},
            ],
        )
        levels = _responses(records, "get_available_thinking_levels")[0]["data"]["levels"]
        assert "high" in levels and "off" not in levels  # explicitly mapped to null

        assert _responses(records, "set_thinking_level")[0]["success"] is True
        assert _responses(records, "cycle_thinking_level")[0]["data"]["level"] in levels

        state = [r for r in _responses(records, "get_state") if r.get("id") == "d"][0]
        assert state["data"]["thinkingLevel"] in levels
    finally:
        session.dispose()


async def test_queue_mode_and_auto_toggles(tmp_path):
    session = _session(tmp_path, [FauxTurn(text="x")])
    try:
        records = await _serve(
            session,
            [
                {"id": "1", "type": "set_steering_mode", "mode": "all"},
                {"id": "2", "type": "set_follow_up_mode", "mode": "all"},
                {"id": "3", "type": "set_auto_compaction", "enabled": False},
                {"id": "4", "type": "set_auto_retry", "enabled": False},
                {"id": "5", "type": "get_state"},
                {"id": "6", "type": "set_steering_mode", "mode": "nonsense"},
            ],
        )
        state = _responses(records, "get_state")[0]["data"]
        assert state["steeringMode"] == "all"
        assert state["followUpMode"] == "all"
        assert state["autoCompactionEnabled"] is False

        invalid = [r for r in _responses(records, "set_steering_mode") if r.get("id") == "6"][0]
        assert invalid["success"] is False
        assert "Invalid queue mode" in invalid["error"]
    finally:
        session.dispose()


async def test_clear_queue_returns_dropped_messages(tmp_path):
    session = _session(tmp_path, [FauxTurn(text="x")])
    try:
        await session.steer("steer me")
        await session.follow_up("later")
        records = await _serve(session, [{"id": "1", "type": "clear_queue"}])
        data = _responses(records, "clear_queue")[0]["data"]
        assert data["steering"] == ["steer me"]
        assert data["followUp"] == ["later"]
    finally:
        session.dispose()


async def test_get_messages_and_last_assistant_text(tmp_path):
    session = _session(tmp_path, [FauxTurn(text="final answer")])
    try:
        records = await _serve(
            session,
            [
                {"id": "1", "type": "prompt", "message": "hi"},
                {"id": "2", "type": "get_messages"},
                {"id": "3", "type": "get_last_assistant_text"},
            ],
        )
        messages = _responses(records, "get_messages")[0]["data"]["messages"]
        assert [m["role"] for m in messages] == ["user", "assistant"]

        text = _responses(records, "get_last_assistant_text")[0]["data"]["text"]
        assert text == "final answer"
    finally:
        session.dispose()


async def test_session_stats_aggregate_usage(tmp_path):
    usage = Usage(input=100, output=50, total_tokens=150, cost=Cost(input=0.0003, output=0.00075, total=0.00105))
    turn = FauxTurn(text="x", usage=usage)
    session = _session(tmp_path, [turn])
    try:
        records = await _serve(
            session,
            [{"id": "1", "type": "prompt", "message": "hi"}, {"id": "2", "type": "get_session_stats"}],
        )
        stats = _responses(records, "get_session_stats")[0]["data"]
        assert stats["userMessages"] == 1
        assert stats["assistantMessages"] == 1
        assert stats["totalMessages"] == 2
        assert stats["tokens"]["input"] == 100
        assert stats["tokens"]["total"] == 150
        assert stats["cost"] > 0
    finally:
        session.dispose()


async def test_entries_and_tree_are_serialized(tmp_path):
    session = _session(tmp_path, [FauxTurn(text="x")])
    try:
        records = await _serve(
            session,
            [{"id": "1", "type": "prompt", "message": "hi"}, {"id": "2", "type": "get_entries"}, {"id": "3", "type": "get_tree"}],
        )
        entries = _responses(records, "get_entries")[0]["data"]
        assert entries["leafId"]
        assert [e["type"] for e in entries["entries"]] == ["message", "message"]

        tree = _responses(records, "get_tree")[0]["data"]
        assert tree["leafId"] == entries["leafId"]
        assert tree["tree"][0]["entry"]["type"] == "message"
        assert tree["tree"][0]["children"][0]["entry"]["parentId"] == tree["tree"][0]["entry"]["id"]
    finally:
        session.dispose()


async def test_fork_messages_lists_user_entries(tmp_path):
    session = _session(tmp_path, [FauxTurn(text="x")])
    try:
        records = await _serve(
            session,
            [{"id": "1", "type": "prompt", "message": "first"}, {"id": "2", "type": "get_fork_messages"}],
        )
        messages = _responses(records, "get_fork_messages")[0]["data"]["messages"]
        assert len(messages) == 1
        assert messages[0]["text"] == "first"
        assert messages[0]["entryId"]
    finally:
        session.dispose()


async def test_set_session_name_round_trips(tmp_path):
    session = _session(tmp_path, [FauxTurn(text="x")])
    try:
        await _serve(session, [{"id": "1", "type": "set_session_name", "name": "my session"}])
        assert session.session_manager.get_session_name() == "my session"

        records = await _serve(session, [{"id": "2", "type": "get_state"}])
        assert _responses(records, "get_state")[0]["data"]["sessionName"] == "my session"
    finally:
        session.dispose()


async def test_set_model_and_available_models_use_the_registry(tmp_path):
    from pi_ai import ModelRegistry

    store = InMemoryCredentialStore()
    store.set("faux", ApiKeyCredential(key="k"))
    registry = ModelRegistry(AuthStorage(store))
    registry.apply_overlay(
        {
            "faux": {
                "id": "faux",
                "name": "Faux",
                "api": FAUX_API,
                "base_url": "http://localhost",
                "models": [
                    {"id": "alpha", "name": "Alpha", "contextWindow": 1000, "maxTokens": 100},
                    {"id": "beta", "name": "Beta", "contextWindow": 2000, "maxTokens": 200},
                ],
            }
        }
    )

    session = _session(tmp_path, [FauxTurn(text="x")])
    try:
        records = await _serve(
            session,
            [
                {"id": "1", "type": "get_available_models"},
                {"id": "2", "type": "set_model", "provider": "faux", "modelId": "beta"},
                {"id": "3", "type": "set_model", "provider": "faux", "modelId": "nope"},
            ],
            model_registry=registry,
        )
        models = _responses(records, "get_available_models")[0]["data"]["models"]
        ids = {m["id"] for m in models}
        assert {"alpha", "beta"} <= ids

        set_response = [r for r in _responses(records, "set_model") if r.get("id") == "2"][0]
        assert set_response["success"] is True
        assert session.model.id == "beta"

        missing = [r for r in _responses(records, "set_model") if r.get("id") == "3"][0]
        assert missing["success"] is False
        assert "not found" in missing["error"]
    finally:
        session.dispose()


async def test_get_commands_lists_prompt_templates(tmp_path):
    prompts_dir = tmp_path / ".pi" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "review.md").write_text(
        "---\ndescription: Review the code\n---\nReview it.\n", encoding="utf-8"
    )

    from pi_coding_agent.resource_loader import ResourceLoader

    loader = ResourceLoader(str(tmp_path), str(tmp_path / "agent"), project_trusted=True)
    register_faux_api(FAUX_API, [FauxTurn(text="x")])
    session = AgentSession(
        model=_model(),
        cwd=str(tmp_path),
        session_manager=SessionManager.in_memory(str(tmp_path)),
        auth_storage=_auth(),
        resource_loader=loader,
        active_tool_names=(),
    )
    try:
        raw = serialize_json_line({"id": "1", "type": "get_commands"}).encode("utf-8")
        output = io.StringIO()
        mode = RpcMode(session, resource_loader=loader, output=output)
        await mode.run(io.BytesIO(raw))

        records = [json.loads(line) for line in output.getvalue().splitlines() if line]
        commands = _responses(records, "get_commands")[0]["data"]["commands"]
        names = {c["name"] for c in commands}
        assert "review" in names
        review = next(c for c in commands if c["name"] == "review")
        assert review["source"] == "prompt"
        assert review["description"] == "Review the code"
    finally:
        session.dispose()


def test_model_to_json_shape():
    payload = model_to_json(_model())
    assert payload["id"] == "rpc-model"
    assert payload["api"] == FAUX_API
    assert payload["cost"]["input"] == 3.0
    assert payload["thinkingLevelMap"] == {"off": None, "low": "low", "high": "high"}
    assert payload["contextWindow"] == 100_000


async def test_run_rpc_mode_returns_zero_on_eof(tmp_path):
    session = _session(tmp_path, [FauxTurn(text="x")])
    try:
        output = io.StringIO()
        code = await run_rpc_mode(session, input_stream=io.BytesIO(b""), output=output)
        assert code == 0
        assert output.getvalue() == ""
    finally:
        session.dispose()


# --------------------------------------------------------------------------------------
# Wire-format guard
# --------------------------------------------------------------------------------------


def test_every_event_projection_is_strictly_json_serializable():
    """Every outbound record must survive a strict ``json.dumps``.

    This is a real bug class, not a formality: the stdio path serializes with
    ``default=str``, so a live object leaking into a payload degrades into
    ``"<TextContent object at 0x...>"`` instead of failing — while the same payload
    is a hard error over any transport that encodes strictly (WebSocket, JSON API).
    """
    from pi_ai import ImageContent as _ImageContent
    from pi_ai import TextDeltaEvent
    from pi_ai import ToolResultMessage
    from pi_agent import (
        AgentEndEvent,
        AgentStartEvent,
        MessageEndEvent,
        MessageStartEvent,
        MessageUpdateEvent,
        ToolExecutionEndEvent,
        ToolExecutionStartEvent,
        ToolExecutionUpdateEvent,
        TurnEndEvent,
        TurnStartEvent,
        AgentToolResult,
    )
    from pi_coding_agent.messages import CustomMessage
    from pi_coding_agent.modes.json_event import to_json_event

    tool_result = AgentToolResult(
        content=[TextContent(text="wrote the file"), _ImageContent(data="aGk=", mime_type="image/png")],
        details={"path": "x.txt", "bytesWritten": 5},
    )
    tool_result_message = ToolResultMessage(
        tool_call_id="c1",
        tool_name="write",
        content=[TextContent(text="wrote the file")],
        is_error=False,
        timestamp=0,
    )
    assistant = _assistant("hello")
    custom = CustomMessage(custom_type="note", content="noted", display=True, details={"k": 1}, timestamp=0)

    events = [
        AgentStartEvent(),
        TurnStartEvent(),
        MessageStartEvent(message=UserMessage(content=[TextContent(text="hi")], timestamp=0)),
        MessageStartEvent(message=assistant),
        MessageStartEvent(message=tool_result_message),
        MessageStartEvent(message=custom),
        MessageUpdateEvent(message=assistant, assistant_message_event=TextDeltaEvent(
            content_index=0, delta="he", partial=assistant)),
        MessageEndEvent(message=assistant),
        ToolExecutionStartEvent(tool_call_id="c1", tool_name="write", args={"path": "x.txt"}),
        ToolExecutionUpdateEvent(
            tool_call_id="c1", tool_name="write", args={}, partial_result=tool_result
        ),
        ToolExecutionEndEvent(tool_call_id="c1", tool_name="write", result=tool_result, is_error=False),
        TurnEndEvent(message=assistant, tool_results=[tool_result_message]),
        AgentEndEvent(messages=[UserMessage(content=[TextContent(text="hi")], timestamp=0), assistant, tool_result_message]),
        # An event type this port does not model must still encode safely.
        object(),
    ]

    for event in events:
        payload = to_json_event(event)
        encoded = json.dumps(payload)  # strict: no default= fallback
        assert isinstance(json.loads(encoded), dict)

    # The specific leak that motivated this test.
    end = to_json_event(
        ToolExecutionEndEvent(tool_call_id="c1", tool_name="write", result=tool_result, is_error=False)
    )
    assert end["result"]["content"] == [
        {"type": "text", "text": "wrote the file"},
        {"type": "image", "data": "aGk=", "mimeType": "image/png"},
    ]

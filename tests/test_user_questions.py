"""Tests for the ``ask_user_question`` seam.

Everything here is offline: the client is a list, and the "session" is a small
stand-in that records what the late-answer path did.  The properties under test
are the ones that would be expensive to discover in production — validation,
replay from the transcript, the timeout/pending split, and the delegated ban.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pi_ai import (
    AuthStorage,
    InMemoryCredentialStore,
    Model,
    ModelCost,
)

from pi_coding_agent.agent_session import AgentSession
from pi_coding_agent.session_manager import SessionManager
from pi_coding_agent.tools import create_builtin_tools
from pi_coding_agent.tools.ask_user import create_ask_user_tool
from pi_coding_agent.user_questions import (
    QuestionBroker,
    UserQuestionError,
    fold_questions,
    parse_answers,
    parse_questions,
    parse_timeout,
)

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def question_batch() -> list[dict[str, Any]]:
    return [
        {
            "id": "kind",
            "question": "Which report?",
            "header": "Report type",
            "options": [
                {"label": "Progress", "description": "Short, decision first."},
                {"label": "Research", "description": "Longer, tradeoffs."},
            ],
        }
    ]


class FakeSession:
    """Minimal stand-in for the two calls the broker makes outside the tool call."""

    def __init__(self, messages: list[Any] | None = None) -> None:
        self.messages = messages or []
        self.is_streaming = False
        self.sent: list[dict[str, Any]] = []
        self.resumed = 0

    async def send_custom_message(self, content: Any, **kwargs: Any) -> None:
        self.sent.append({"content": content, **kwargs})

    async def continue_turn(self) -> None:
        self.resumed += 1


def call_message(call_id: str = "call_1") -> Any:
    return SimpleNamespace(
        role="assistant",
        content=[
            SimpleNamespace(
                type="toolCall",
                id=call_id,
                name="ask_user_question",
                arguments={"questions": question_batch()},
            )
        ],
    )


def result_message(call_id: str, payload: Any, *, is_error: bool = False) -> Any:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        role="toolResult",
        tool_call_id=call_id,
        is_error=is_error,
        content=[SimpleNamespace(text=text)],
    )


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


def test_question_validation_normalizes_the_wire_shape() -> None:
    questions = parse_questions(question_batch())
    assert questions[0].id == "kind"
    assert questions[0].header == "Report type"
    assert questions[0].options[1].description == "Longer, tradeoffs."
    assert questions[0].to_wire()["options"][0] == {
        "label": "Progress",
        "description": "Short, decision first.",
    }


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (None, "EMPTY_QUESTIONS"),
        ([], "EMPTY_QUESTIONS"),
        ([{"id": "a"}], "BAD_QUESTIONS"),
        ([{"question": "q"}], "BAD_QUESTIONS"),
        ([{"id": "a", "question": "q"}, {"id": "a", "question": "r"}], "BAD_QUESTIONS"),
        (
            [{"id": "a", "question": "q", "options": [{"label": "x"}, {"label": "x"}]}],
            "BAD_QUESTIONS",
        ),
        (
            [{"id": f"q{i}", "question": "q"} for i in range(5)],
            "BAD_QUESTIONS",
        ),
    ],
)
def test_question_validation_rejects_bad_batches(raw: Any, code: str) -> None:
    with pytest.raises(UserQuestionError) as caught:
        parse_questions(raw)
    assert caught.value.code == code


def test_timeout_forms() -> None:
    assert parse_timeout(None, 120.0) == 120.0
    assert parse_timeout(-1) is None
    assert parse_timeout(5) == 5.0
    for bad in (0, -2, 2_147_484, "5", True):
        with pytest.raises(UserQuestionError) as caught:
            parse_timeout(bad)
        assert caught.value.code == "BAD_TIMEOUT"


def test_answers_must_name_every_question_once() -> None:
    questions = parse_questions(question_batch())
    assert parse_answers([{"id": "kind", "selected": ["Progress"]}], questions)[0].selected == (
        "Progress",
    )
    with pytest.raises(UserQuestionError) as missing:
        parse_answers([], questions)
    assert missing.value.code == "BAD_ANSWER"

    with pytest.raises(UserQuestionError):
        parse_answers([{"id": "kind", "selected": ["Other"]}], questions)
    with pytest.raises(UserQuestionError):
        parse_answers([{"id": "kind", "selected": ["Progress", "Research"]}], questions)
    with pytest.raises(UserQuestionError):
        parse_answers([{"id": "nope", "selected": []}], questions)


def test_skip_is_the_empty_answer_and_not_permission() -> None:
    questions = parse_questions(question_batch())
    answer = parse_answers([{"id": "kind", "selected": []}], questions)[0]
    assert answer.skipped is True
    assert answer.to_wire() == {"id": "kind", "selected": []}


# --------------------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------------------


def test_fold_open_continued_and_settled() -> None:
    call = call_message()
    assert fold_questions([call])["active"][0]["state"] == "open"

    continued = fold_questions([call, result_message("call_1", {"pending": True, "callId": "call_1"})])
    assert continued["active"][0]["state"] == "continued"

    settled = fold_questions(
        [call, result_message("call_1", {"answers": [{"id": "kind", "selected": ["Progress"]}]})]
    )
    assert settled["active"] == []
    assert settled["settled"][0]["answers"] == [{"id": "kind", "selected": ["Progress"]}]
    assert settled["settled"][0]["questions"][0]["id"] == "kind"


def test_fold_keeps_an_answerable_question_only_while_it_is() -> None:
    call = call_message()
    failed = fold_questions([call, result_message("call_1", "boom", is_error=True)])
    assert failed == {"active": [], "settled": []}


def test_fold_settles_with_a_late_reply() -> None:
    reply = SimpleNamespace(
        role="custom",
        custom_type="user-question-reply",
        content=[SimpleNamespace(text="{}")],
        details={
            "callId": "call_1",
            "questions": [{"id": "kind", "question": "Which report?"}],
            "answers": [{"id": "kind", "selected": [], "custom": "A memo"}],
        },
    )
    view = fold_questions(
        [call_message(), result_message("call_1", {"pending": True, "callId": "call_1"}), reply]
    )
    assert view["active"] == []
    assert view["settled"][0]["answers"] == [
        {"id": "kind", "selected": [], "custom": "A memo"}
    ]


# --------------------------------------------------------------------------------------
# Broker
# --------------------------------------------------------------------------------------


async def test_blocking_question_round_trip() -> None:
    events: list[dict[str, Any]] = []
    session = FakeSession()
    broker = QuestionBroker()
    broker.attach(emit=events.append, session=lambda: session, client_count=lambda: 1)

    questions = parse_questions(question_batch())
    task = asyncio.create_task(broker.ask("call_1", questions, timeout=10))
    await asyncio.sleep(0)

    request = events[-1]
    assert request["type"] == "user_question_request"
    assert request["callId"] == "call_1"
    assert request["questions"][0]["options"][0]["label"] == "Progress"

    result = await broker.answer("call_1", [{"id": "kind", "selected": ["Progress"]}])
    assert result["outcome"] == "answered"
    assert await task == {"answers": [{"id": "kind", "selected": ["Progress"]}]}
    assert events[-1]["type"] == "user_question_settled"


async def test_timed_question_returns_pending_and_stays_answerable() -> None:
    events: list[dict[str, Any]] = []
    session = FakeSession(messages=[call_message()])
    broker = QuestionBroker()
    broker.attach(emit=events.append, session=lambda: session, client_count=lambda: 1)

    task = asyncio.create_task(broker.ask("call_1", parse_questions(question_batch()), timeout=30))
    await asyncio.sleep(0)
    broker.claim("call_1")
    assert broker.timeout("call_1") is True
    pending = await task
    assert pending["pending"] is True
    assert pending["callId"] == "call_1"

    # The question is gone from the live map but still answerable from the log.
    session.messages.append(result_message("call_1", pending))
    late = await broker.answer("call_1", [{"id": "kind", "selected": ["Research"]}])
    assert late["outcome"] == "answered"
    await asyncio.sleep(0)
    assert session.sent[0]["custom_type"] == "user-question-reply"
    assert session.sent[0]["details"]["answers"] == [{"id": "kind", "selected": ["Research"]}]
    assert session.resumed == 1


async def test_no_client_and_delegated_sessions_cannot_ask() -> None:
    broker = QuestionBroker()
    broker.attach(emit=lambda _payload: None, session=lambda: FakeSession(), client_count=lambda: 0)
    with pytest.raises(UserQuestionError) as unavailable:
        await broker.ask("call_1", parse_questions(question_batch()), timeout=5)
    assert unavailable.value.code == "NO_PROVIDER"

    delegated = QuestionBroker(delegated=True)
    delegated.attach(emit=lambda _payload: None, session=lambda: FakeSession(), client_count=lambda: 1)
    with pytest.raises(UserQuestionError) as refused:
        await delegated.ask("call_2", parse_questions(question_batch()), timeout=5)
    assert refused.value.code == "DELEGATED_CALLER"


async def test_dismissing_a_timed_question_releases_the_model_and_keeps_it_answerable() -> None:
    broker = QuestionBroker()
    broker.attach(emit=lambda _payload: None, session=lambda: FakeSession(), client_count=lambda: 1)
    task = asyncio.create_task(broker.ask("call_1", parse_questions(question_batch()), timeout=30))
    await asyncio.sleep(0)
    # Dismissing is not a refusal: same release as a timeout, question still open.
    assert (await broker.answer("call_1", [], cancelled=True))["outcome"] == "pending"
    assert (await task)["pending"] is True


async def test_pending_view_reports_a_blocking_wait() -> None:
    """A reloaded tab must not invent a deadline for a `timeout: -1` question."""
    session = FakeSession(messages=[call_message()])
    broker = QuestionBroker()
    broker.attach(emit=lambda _payload: None, session=lambda: session, client_count=lambda: 1)
    task = asyncio.create_task(broker.ask("call_1", parse_questions(question_batch()), timeout=None))
    await asyncio.sleep(0)
    view = broker.pending_view()
    assert view[0]["blocking"] is True
    broker.claim("call_1")
    broker.release("call_1")
    await broker.answer("call_1", [], cancelled=True)
    with pytest.raises(UserQuestionError):
        await task


async def test_dismissing_a_blocking_question_aborts_the_call() -> None:
    broker = QuestionBroker()
    broker.attach(emit=lambda _payload: None, session=lambda: FakeSession(), client_count=lambda: 1)
    task = asyncio.create_task(broker.ask("call_1", parse_questions(question_batch()), timeout=None))
    await asyncio.sleep(0)
    assert (await broker.answer("call_1", [], cancelled=True))["outcome"] == "cancelled"
    with pytest.raises(UserQuestionError) as aborted:
        await task
    assert aborted.value.code == "ASK_ABORTED"


async def test_blocking_ceiling_reports_a_failure_not_a_pending_answer() -> None:
    broker = QuestionBroker()
    broker.attach(emit=lambda _payload: None, session=lambda: FakeSession(), client_count=lambda: 1)
    task = asyncio.create_task(broker.ask("call_1", parse_questions(question_batch()), timeout=None))
    await asyncio.sleep(0)
    # Drive the ceiling directly instead of waiting fifteen minutes.
    broker._on_timeout("call_1")  # noqa: SLF001 - the timer path is what is under test
    with pytest.raises(UserQuestionError) as timed_out:
        await task
    assert timed_out.value.code == "ASK_ABORTED"
    assert "timed out" in str(timed_out.value)


# --------------------------------------------------------------------------------------
# Tool
# --------------------------------------------------------------------------------------


async def test_tool_returns_compact_json_and_teaching_errors() -> None:
    broker = QuestionBroker()
    broker.attach(emit=lambda _payload: None, session=lambda: FakeSession(), client_count=lambda: 1)
    tool = create_ask_user_tool(broker)
    assert "skipped" in tool.description
    assert "pending" in tool.description
    assert tool.execution_mode == "sequential"

    async def answer_soon() -> None:
        await asyncio.sleep(0.01)
        for _ in range(100):
            if broker._pending:  # noqa: SLF001 - wait for the call to park
                await broker.answer("call_1", [{"id": "kind", "selected": ["Progress"]}])
                return
            await asyncio.sleep(0.01)

    responder = asyncio.create_task(answer_soon())
    result = await tool.execute(
        "call_1", {"questions": question_batch(), "timeout": 10}, None, None
    )
    await responder
    payload = json.loads(result.content[0].text)
    assert payload["answers"][0]["selected"] == ["Progress"]

    failed = await tool.execute("call_2", {"questions": []}, None, None)
    assert failed.content[0].text.startswith("EMPTY_QUESTIONS:")


# --------------------------------------------------------------------------------------
# Session wiring
# --------------------------------------------------------------------------------------


def _model() -> Model:
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


def _session(tmp_path: Path, **kwargs: Any) -> AgentSession:
    cwd = tmp_path / "project"
    cwd.mkdir(exist_ok=True)
    manager = SessionManager.create(str(cwd), session_dir=str(tmp_path / "sessions"))
    storage = AuthStorage(InMemoryCredentialStore(), env={})
    storage.set_api_key("test", "test-key")
    return AgentSession(_model(), str(cwd), manager, auth_storage=storage, **kwargs)


def test_session_exposes_the_tool_to_the_root_and_not_to_a_child(tmp_path: Path) -> None:
    root = _session(tmp_path)
    assert "ask_user_question" in [tool.name for tool in root.get_all_tools()]
    assert "ask_user_question" in root.get_active_tools()

    child = _session(tmp_path, question_broker=QuestionBroker(delegated=True))
    assert "ask_user_question" not in [tool.name for tool in child.get_all_tools()]
    assert "ask_user_question" not in child.get_active_tools()


def test_default_tool_set_omits_questions_without_a_human_channel(tmp_path: Path) -> None:
    names = [tool.name for tool in create_builtin_tools(str(tmp_path))]
    assert "ask_user_question" not in names
    names_with_broker = [
        tool.name for tool in create_builtin_tools(str(tmp_path), question_broker=QuestionBroker())
    ]
    assert "ask_user_question" in names_with_broker


# --------------------------------------------------------------------------------------
# RPC surface (the server drives exactly these commands)
# --------------------------------------------------------------------------------------


async def test_rpc_commands_drive_a_live_question(tmp_path: Path) -> None:
    from pi_coding_agent.modes.rpc_mode import RpcMode

    session = _session(tmp_path)
    events: list[dict[str, Any]] = []
    mode = RpcMode(session=session, emit=events.append)
    mode.set_client_count(lambda: 1)

    task = asyncio.create_task(
        session.question_broker.ask("call_1", parse_questions(question_batch()), 30)
    )
    await asyncio.sleep(0)

    await mode.handle_line(json.dumps({"type": "claim_question", "callId": "call_1", "id": "c1"}))
    claim = next(event for event in events if event.get("command") == "claim_question")
    assert claim["success"] is True
    assert claim["data"]["remainingMs"] > 0

    await mode.handle_line(
        json.dumps(
            {
                "type": "answer_question",
                "callId": "call_1",
                "id": "c2",
                "answers": [{"id": "kind", "selected": ["Progress"]}],
            }
        )
    )
    answered = next(event for event in events if event.get("command") == "answer_question")
    assert answered["success"] is True
    assert answered["data"]["callId"] == "call_1"
    assert (await task)["answers"][0]["selected"] == ["Progress"]


async def test_rpc_get_state_replays_answerable_questions(tmp_path: Path) -> None:
    from pi_coding_agent.modes.rpc_mode import RpcMode

    session = _session(tmp_path)
    session.messages.extend(
        [
            call_message("call_1"),
            result_message("call_1", {"pending": True, "callId": "call_1"}),
        ]
    )
    events: list[dict[str, Any]] = []
    mode = RpcMode(session=session, emit=events.append)

    await mode.handle_line(json.dumps({"type": "get_state", "id": "s1"}))
    state = next(event for event in events if event.get("command") == "get_state")
    pending = state["data"]["pendingQuestions"]
    assert [item["callId"] for item in pending] == ["call_1"]
    assert pending[0]["state"] == "continued"
    assert pending[0]["questions"][0]["id"] == "kind"


async def test_rpc_unknown_question_fails_with_a_code(tmp_path: Path) -> None:
    from pi_coding_agent.modes.rpc_mode import RpcMode

    events: list[dict[str, Any]] = []
    mode = RpcMode(session=_session(tmp_path), emit=events.append)
    await mode.handle_line(
        json.dumps({"type": "answer_question", "callId": "nope", "id": "c1", "answers": []})
    )
    response = next(event for event in events if event.get("command") == "answer_question")
    assert response["success"] is False
    assert response["code"] == "UNKNOWN_QUESTION"


# --------------------------------------------------------------------------------------
# Full loop: the model calls the tool, the transport answers
# --------------------------------------------------------------------------------------


async def _until(predicate: Any, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was never met")


def _scripted(script: list[Any]) -> Any:
    from pi_ai import DoneEvent, create_assistant_message_event_stream

    calls = {"n": 0}

    def stream_fn(model: Any, context: Any, options: Any):
        stream = create_assistant_message_event_stream()
        index = min(calls["n"], len(script) - 1)
        calls["n"] += 1
        message = script[index]
        stream.push(DoneEvent(reason=message.stop_reason, message=message))
        return stream

    return stream_fn


def _assistant(content: list[Any], reason: str = "stop") -> Any:
    from pi_ai import AssistantMessage, Cost, Usage

    return AssistantMessage(
        content=content,
        api="faux",
        provider="test",
        model="test-model",
        usage=Usage(cost=Cost()),
        stop_reason=reason,
        error_message=None,
        timestamp=1,
    )


async def test_model_tool_call_parks_then_answers_through_the_rpc_surface(tmp_path: Path) -> None:
    from pi_ai import TextContent, ToolCall

    from pi_coding_agent.modes.rpc_mode import RpcMode

    script = [
        _assistant(
            [ToolCall(id="call-1", name="ask_user_question", arguments={"questions": question_batch()})],
            "toolUse",
        ),
        _assistant([TextContent(text="好的，按进度汇报来写。")]),
    ]
    session = _session(tmp_path, stream_fn=_scripted(script))
    events: list[dict[str, Any]] = []
    mode = RpcMode(session=session, emit=events.append)
    mode.set_client_count(lambda: 1)

    task = asyncio.create_task(session.prompt("写个报告"))
    await _until(lambda: any(event.get("type") == "user_question_request" for event in events))
    request = next(event for event in events if event.get("type") == "user_question_request")
    assert request["callId"] == "call-1"

    await mode.handle_line(
        json.dumps(
            {
                "type": "answer_question",
                "callId": "call-1",
                "id": "c1",
                "answers": [{"id": "kind", "selected": ["Progress"]}],
            }
        )
    )
    await asyncio.wait_for(task, timeout=5)

    results = [m for m in session.messages if getattr(m, "role", "") == "toolResult"]
    assert results and results[-1].tool_call_id == "call-1"
    assert json.loads(results[-1].content[0].text)["answers"][0]["selected"] == ["Progress"]
    # Answered, so nothing is left to answer.
    assert session.question_broker.pending_view() == []


async def test_timed_out_question_returns_pending_and_stays_answerable(tmp_path: Path) -> None:
    from pi_ai import TextContent, ToolCall

    from pi_coding_agent.modes.rpc_mode import RpcMode

    batch = question_batch()
    script = [
        _assistant(
            [
                ToolCall(
                    id="call-1",
                    name="ask_user_question",
                    arguments={"questions": batch, "timeout": 1},
                )
            ],
            "toolUse",
        ),
        _assistant([TextContent(text="先按默认继续。")]),
    ]
    session = _session(tmp_path, stream_fn=_scripted(script))
    events: list[dict[str, Any]] = []
    mode = RpcMode(session=session, emit=events.append)
    mode.set_client_count(lambda: 1)

    await asyncio.wait_for(session.prompt("写个报告"), timeout=10)

    results = [m for m in session.messages if getattr(m, "role", "") == "toolResult"]
    payload = json.loads(results[-1].content[0].text)
    assert payload["pending"] is True
    assert payload["callId"] == "call-1"
    # The question is still answerable from the transcript after the timeout.
    pending = session.question_broker.pending_view()
    assert [item["callId"] for item in pending] == ["call-1"]
    assert pending[0]["state"] == "continued"


def test_intent_is_presentation_only_and_must_name_offered_labels() -> None:
    planned = parse_questions(
        [
            {
                "id": "plan",
                "question": "Approve this plan?",
                "detail": "# Plan",
                "options": [{"label": "Approve"}, {"label": "Keep planning"}],
                "intent": {"kind": "plan-review", "approve": "Approve"},
            }
        ]
    )[0]
    assert planned.to_wire()["intent"] == {"kind": "plan-review", "approve": "Approve"}

    with pytest.raises(UserQuestionError) as mismatch:
        parse_questions(
            [
                {
                    "id": "plan",
                    "question": "Approve?",
                    "options": [{"label": "Approve"}],
                    "intent": {"kind": "approval", "approve": "Approve", "reject": "Deny"},
                }
            ]
        )
    assert mismatch.value.code == "BAD_INTENT"

    with pytest.raises(UserQuestionError) as unknown:
        parse_questions(
            [
                {
                    "id": "plan",
                    "question": "Approve?",
                    "options": [{"label": "Approve"}, {"label": "Deny"}],
                    "intent": {"kind": "sign-off", "approve": "Approve"},
                }
            ]
        )
    assert unknown.value.code == "BAD_INTENT"

    # An unknown intent must never make a question unanswerable; the answer
    # encoding is unchanged, which is why the fold keeps the labels.
    assert fold_questions([call_message()])["active"][0]["questions"][0]["options"][0]["label"] == "Progress"

"""A question outlives the process that asked it, without a snapshot machine.

The design decision this pins: pi does not resume a blocked tool call at an index.
A question whose wait expired ends the turn with a ``pending`` result, and the answer
is folded back from the **transcript** — so a reload, a session switch or a server
restart all recover the same state, and there is nothing to keep in sync.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from pi_ai import AssistantMessage, Cost, TextContent, ToolCall, ToolResultMessage, Usage

from pi_coding_agent.session_manager import SessionManager
from pi_coding_agent.user_questions import ASK_TOOL_NAME, fold_questions

QUESTIONS = [
    {
        "id": "q1",
        "question": "用哪个环境？",
        "options": [{"label": "测试"}, {"label": "生产"}],
    }
]


def ask_turn(call_id: str = "call-1") -> AssistantMessage:
    return AssistantMessage(
        content=[
            ToolCall(id=call_id, name=ASK_TOOL_NAME, arguments={"questions": QUESTIONS})
        ],
        api="faux",
        provider="test",
        model="test-model",
        usage=Usage(cost=Cost()),
        stop_reason="toolUse",
        timestamp=int(time.time() * 1000),
    )


def tool_result(call_id: str, payload: dict) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=call_id,
        tool_name=ASK_TOOL_NAME,
        content=[TextContent(text=json.dumps(payload, ensure_ascii=False))],
        is_error=False,
        timestamp=int(time.time() * 1000),
    )


def test_a_pending_result_leaves_the_question_answerable():
    messages = [
        ask_turn(),
        tool_result("call-1", {"pending": True, "callId": "call-1", "timeout": True}),
    ]

    folded = fold_questions(messages)

    assert [entry["callId"] for entry in folded["active"]] == ["call-1"]
    assert folded["active"][0]["state"] == "continued"
    assert folded["active"][0]["questions"][0]["question"] == "用哪个环境？"


def test_a_call_with_no_result_yet_is_open():
    folded = fold_questions([ask_turn()])

    assert folded["active"][0]["state"] == "open"


def test_a_settled_answer_closes_the_question():
    messages = [
        ask_turn(),
        tool_result("call-1", {"answers": [{"id": "q1", "selected": ["测试"]}]}),
    ]

    folded = fold_questions(messages)

    assert folded["active"] == []
    assert folded["settled"][0]["answers"][0]["selected"] == ["测试"]


def test_a_late_reply_custom_message_also_closes_it():
    """The path a late answer takes when the tool call already returned pending."""
    messages = [
        ask_turn(),
        tool_result("call-1", {"pending": True, "callId": "call-1"}),
        _reply_message("call-1", {"answers": [{"id": "q1", "selected": ["生产"]}]}),
    ]

    folded = fold_questions(messages)

    assert folded["active"] == []
    assert folded["settled"][0]["answers"][0]["selected"] == ["生产"]


def test_the_state_survives_a_reload_because_it_is_folded_from_the_file(tmp_path: Path) -> None:
    """The whole suspend/resume claim, end to end through the session file."""
    manager = SessionManager.create(str(tmp_path / "project"), session_dir=str(tmp_path / "s"))
    import asyncio

    asyncio.run(manager.append_message(ask_turn()))
    asyncio.run(
        manager.append_message(
            tool_result("call-1", {"pending": True, "callId": "call-1", "timeout": True})
        )
    )
    session_file = manager.get_session_file()
    assert session_file is not None

    reopened = SessionManager.open(session_file)
    folded = fold_questions(reopened.build_context().messages)

    assert [entry["callId"] for entry in folded["active"]] == ["call-1"]
    assert folded["active"][0]["state"] == "continued"


def _reply_message(call_id: str, details: dict):
    """The custom message the server writes when a late answer arrives.

    Built structurally because the concrete class lives in ``messages.py``; the
    folder only looks at ``role``, ``custom_type`` and ``details``.
    """

    class Reply:
        role = "custom"
        custom_type = "user-question-reply"

        def __init__(self) -> None:
            self.details = {"callId": call_id, **details}
            self.content = ""

    return Reply()

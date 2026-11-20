"""The ``ask_user_question`` tool: let the model stop and ask.

The model-facing half of :mod:`pi_coding_agent.user_questions`.  Everything the
model needs to know about the contract lives in the description, because that
text is the only documentation it reads:

* a skipped question is an explicit answer (empty ``selected``, no ``custom``)
  and is never permission;
* a ``pending`` result means the timeout expired, not that the user declined;
* a recommended option goes first with a ``(Recommended)`` suffix, so no extra
  schema field is needed to express a preference.

When nobody can answer (no browser tab, a scheduled run, a delegated subagent)
the tool fails with a precise code instead of parking the turn: the model is
told to state its assumption and continue, which is the only behaviour that
cannot hang.
"""

from __future__ import annotations

import json
from typing import Any

from pi_agent import AgentTool, AgentToolResult, ToolUpdateCallback
from pi_ai import AbortSignal, TextContent

from ..user_questions import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_OPTIONS,
    MAX_QUESTIONS,
    QuestionBroker,
    UserQuestionError,
    parse_questions,
    parse_timeout,
)
from .path_utils import error_result

DESCRIPTION = (
    "Ask the user a concise question and wait for the answer. "
    "Use it only for user-owned choices or material ambiguity that inspection cannot "
    "resolve — not for facts you can look up yourself, and not to ask permission for "
    "work the user already requested. "
    "Prefer one batch of questions over several calls. "
    "If you recommend an option, put it first and append \" (Recommended)\" to its label. "
    "A returned item with empty 'selected' and no 'custom' means the user skipped that "
    "question: continue without it, do not ask it again, and do not treat the skip as "
    "permission. A 'pending' result means the timeout expired while the question stayed "
    "answerable: continue useful independent work and never treat it as an answer."
)

PROMPT_SNIPPET = (
    "ask_user_question: stop and ask the user when a choice is theirs or when the "
    "request is materially ambiguous; never ask for facts inspection can find."
)

PROMPT_GUIDELINES = [
    "Ask at most one batch of questions per turn, and only when the answer changes what you do next.",
    "Do not ask the user where code lives, how current behaviour works, or to approve steps they already requested.",
    "State your assumption and continue when no user interface can answer; do not repeat an unanswered question.",
]


def create_ask_user_tool(broker: QuestionBroker) -> AgentTool:
    """Build the tool bound to one session's question broker."""

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "description": f"Questions to ask the user, at most {MAX_QUESTIONS}.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Stable id for this question; echoed in the answer.",
                        },
                        "question": {
                            "type": "string",
                            "description": "The specific question to ask, in the user's language.",
                        },
                        "header": {
                            "type": "string",
                            "description": "Short heading for the card, such as \"Report type\" or \"Confirm\".",
                        },
                        "detail": {
                            "type": "string",
                            "description": "Optional supporting text shown under the question (a plan, a diff summary).",
                        },
                        "options": {
                            "type": "array",
                            "description": (
                                "Optional choices. If you recommend one, put it first and "
                                "append \" (Recommended)\" to that label."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "description": "Short user-facing option label; this is the value returned.",
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": f"One sentence explaining the tradeoff (max {MAX_OPTIONS} options).",
                                    },
                                },
                                "required": ["label"],
                            },
                        },
                        "multi_select": {
                            "type": "boolean",
                            "description": "Whether the user may pick more than one option. Defaults to false.",
                        },
                        "intent": {
                            "type": "object",
                            "description": (
                                "Presentation hint for known decision shapes. Omit for a normal "
                                "question: the answer is still an option label either way. "
                                "plan-review needs 'detail' and the 'approve' label; approval "
                                "needs distinct approve/reject labels (optional 'always')."
                            ),
                            "properties": {
                                "kind": {"type": "string", "enum": ["plan-review", "approval"]},
                                "approve": {"type": "string", "description": "Label that grants or approves."},
                                "reject": {"type": "string", "description": "Label that refuses (approval only)."},
                                "always": {"type": "string", "description": "Label for a persistent grant (approval only)."},
                            },
                            "required": ["kind"],
                        },
                    },
                    "required": ["id", "question"],
                },
            },
            "timeout": {
                "type": "integer",
                "description": (
                    f"Seconds to wait before returning pending (default {int(DEFAULT_TIMEOUT_SECONDS)}). "
                    "Use -1 only when you cannot proceed without an answer."
                ),
            },
        },
        "required": ["questions"],
    }

    async def execute(
        tool_call_id: str,
        args: Any,
        signal: AbortSignal | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        del on_update  # A question has no partial output to stream.
        if not isinstance(args, dict):
            return error_result("ask_user_question requires an object argument with 'questions'.")
        try:
            questions = parse_questions(args.get("questions"))
            timeout = parse_timeout(args.get("timeout"), broker.default_timeout)
            result = await broker.ask(tool_call_id, questions, timeout, signal=signal)
        except UserQuestionError as error:
            # The code is part of the contract: the model can tell "you cannot ask
            # here" (stop asking) from "your batch was malformed" (fix it).
            return AgentToolResult(
                content=[TextContent(text=f"{error.code}: {error.message}")],
                details={"error": True, "code": error.code},
            )
        return AgentToolResult(
            content=[TextContent(text=json.dumps(result, ensure_ascii=False))],
            details={"skipped": [answer["id"] for answer in result.get("answers", []) if not answer["selected"] and not answer.get("custom")]},
        )

    return AgentTool(
        name="ask_user_question",
        label="ask_user_question",
        description=DESCRIPTION,
        parameters=parameters,
        execute=execute,
        prompt_snippet=PROMPT_SNIPPET,
        prompt_guidelines=PROMPT_GUIDELINES,
        # Two cards answered in parallel would race for the same composer.
        execution_mode="sequential",
    )


__all__ = ["DESCRIPTION", "PROMPT_GUIDELINES", "PROMPT_SNIPPET", "create_ask_user_tool"]

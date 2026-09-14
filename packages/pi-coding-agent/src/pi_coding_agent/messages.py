"""Extended agent message types for the coding agent.

Port of ``packages/coding-agent/src/core/messages.ts``.  The three base roles
(``user`` / ``assistant`` / ``toolResult``) live in ``pi_ai``; this module adds
the four coding-agent specific roles and the concrete ``AgentMessage`` union.

Interop notes (why this file looks the way it does):

* JSON keys are the upstream camelCase spellings, and ``None`` means "JS
  ``undefined``" -- such keys must be *omitted*, never written as ``null``.
  ``False`` / ``0`` / ``""`` are real values and must be written.
* The key *insertion order* mirrors the upstream object literals so that the
  serialised lines stay as close to what ``JSON.stringify`` emits as possible.
* Everything here is used both for session file JSON and for the LLM
  projection, so nothing may raise on a malformed entry (old/hand-edited
  session files are a supported input).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Union

from pi_ai import (
    AssistantMessage,
    ImageContent,
    Message,
    TextContent,
    ToolResultMessage,
    UserMessage,
    content_from_json,
    content_to_json,
    message_from_json,
    message_to_json,
)

# ``drop_none`` (JS ``undefined``-dropping) is part of the frozen wire helpers but
# is not re-exported from the package root.
from pi_ai.types import drop_none

# --------------------------------------------------------------------------------------
# Rendered prefixes
# --------------------------------------------------------------------------------------

#: Copied verbatim from upstream: these strings are what the model actually sees, so a
#: stray blank line or missing newline changes behaviour (and prompt caching).
COMPACTION_SUMMARY_PREFIX = (
    "The conversation history before this point was compacted into the following summary:\n\n<summary>\n"
)
COMPACTION_SUMMARY_SUFFIX = "\n</summary>"

BRANCH_SUMMARY_PREFIX = (
    "The following is a summary of a branch that this conversation came back from:\n\n<summary>\n"
)
BRANCH_SUMMARY_SUFFIX = "</summary>"

#: Custom message content is either plain text or the same block list a user message uses.
CustomContent = Union[str, list[Union[TextContent, ImageContent]]]


# --------------------------------------------------------------------------------------
# Extended message types
# --------------------------------------------------------------------------------------


@dataclass
class BashExecutionMessage:
    """A ``!`` shell command recorded in the conversation."""

    command: str
    output: str
    exit_code: int | None
    cancelled: bool
    truncated: bool
    timestamp: int
    full_output_path: str | None = None
    #: ``True`` for ``!!`` commands, which never reach the LLM.
    exclude_from_context: bool | None = None
    role: Literal["bashExecution"] = field(default="bashExecution", init=False)


@dataclass
class CustomMessage:
    """An extension-injected message (``sendMessage()`` / ``custom_message`` entries)."""

    custom_type: str
    content: CustomContent
    #: TUI rendering flag: ``True`` renders styled, ``False`` hides it.  It does NOT
    #: affect the LLM context -- verification against upstream ``convertToLlm`` shows
    #: every ``custom`` message is projected regardless of ``display``.
    display: bool
    timestamp: int
    details: Any = None
    role: Literal["custom"] = field(default="custom", init=False)


@dataclass
class BranchSummaryMessage:
    summary: str
    #: Previous leaf whose abandoned path was summarised (``None`` when it was a root).
    from_id: str | None
    timestamp: int
    role: Literal["branchSummary"] = field(default="branchSummary", init=False)


@dataclass
class CompactionSummaryMessage:
    summary: str
    tokens_before: int
    timestamp: int
    role: Literal["compactionSummary"] = field(default="compactionSummary", init=False)


#: The concrete upstream union.  ``pi_agent.AgentMessage`` is ``Any`` because Python has
#: no declaration merging; this alias is the real contract used by the coding agent.
AgentMessage = Union[
    UserMessage,
    AssistantMessage,
    ToolResultMessage,
    BashExecutionMessage,
    CustomMessage,
    BranchSummaryMessage,
    CompactionSummaryMessage,
]

_BASE_MESSAGE_TYPES = (UserMessage, AssistantMessage, ToolResultMessage)


# --------------------------------------------------------------------------------------
# Timestamp helpers
# --------------------------------------------------------------------------------------


def _iso_to_millis(value: str | int | float) -> int:
    """``new Date(value).getTime()`` for the two shapes upstream passes around.

    Session entries carry ISO-8601 strings while messages carry Unix ms; the
    constructors below accept both because the compaction and branch-summary
    code paths call them with each.
    """
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed.astimezone(timezone.utc) - epoch
    # Integer arithmetic avoids the float rounding of ``.timestamp()``.
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


# --------------------------------------------------------------------------------------
# Message constructors (used when projecting session entries into context)
# --------------------------------------------------------------------------------------


def create_branch_summary_message(
    summary: str, from_id: str | None, timestamp: str | int
) -> BranchSummaryMessage:
    return BranchSummaryMessage(summary=summary, from_id=from_id, timestamp=_iso_to_millis(timestamp))


def create_compaction_summary_message(
    summary: str, tokens_before: int, timestamp: str | int
) -> CompactionSummaryMessage:
    return CompactionSummaryMessage(
        summary=summary, tokens_before=tokens_before, timestamp=_iso_to_millis(timestamp)
    )


def create_custom_message(
    custom_type: str,
    content: CustomContent | None,
    display: bool,
    details: Any,
    timestamp: str | int,
) -> CustomMessage:
    return CustomMessage(
        custom_type=custom_type,
        content=content if content is not None else [],
        display=display,
        details=details,
        timestamp=_iso_to_millis(timestamp),
    )


def bash_execution_to_text(message: BashExecutionMessage) -> str:
    """Render a bash execution as the user-role text block upstream produces."""
    text = f"Ran `{message.command}`\n"
    if message.output:
        text += f"```\n{message.output}\n```"
    else:
        text += "(no output)"
    if message.cancelled:
        text += "\n\n(command cancelled)"
    elif message.exit_code is not None and message.exit_code != 0:
        text += f"\n\nCommand exited with code {message.exit_code}"
    if message.truncated and message.full_output_path:
        text += f"\n\n[Output truncated. Full output: {message.full_output_path}]"
    return text


# --------------------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------------------


def _content_to_json(content: CustomContent) -> Any:
    if isinstance(content, str):
        return content
    return [content_to_json(block) for block in content]


def _content_from_json(content: Any) -> CustomContent:
    if isinstance(content, str) or content is None:
        return content if isinstance(content, str) else []
    return [content_from_json(block) for block in content]


def agent_message_to_json(message: AgentMessage) -> dict[str, Any]:
    """Serialise any ``AgentMessage`` into its session-file JSON form."""
    if isinstance(message, _BASE_MESSAGE_TYPES):
        return message_to_json(message)
    if isinstance(message, BashExecutionMessage):
        # Field order follows the upstream object literal in ``recordBashResult``.
        return drop_none(
            {
                "role": "bashExecution",
                "command": message.command,
                "output": message.output,
                "exitCode": message.exit_code,
                "cancelled": message.cancelled,
                "truncated": message.truncated,
                "fullOutputPath": message.full_output_path,
                "timestamp": message.timestamp,
                "excludeFromContext": message.exclude_from_context,
            }
        )
    if isinstance(message, CustomMessage):
        return drop_none(
            {
                "role": "custom",
                "customType": message.custom_type,
                "content": _content_to_json(message.content),
                "display": message.display,
                "details": message.details,
                "timestamp": message.timestamp,
            }
        )
    if isinstance(message, BranchSummaryMessage):
        return {
            "role": "branchSummary",
            "summary": message.summary,
            "fromId": message.from_id,
            "timestamp": message.timestamp,
        }
    if isinstance(message, CompactionSummaryMessage):
        return {
            "role": "compactionSummary",
            "summary": message.summary,
            "tokensBefore": message.tokens_before,
            "timestamp": message.timestamp,
        }
    raise TypeError(f"Unknown agent message: {message!r}")


def agent_message_from_json(data: dict[str, Any]) -> AgentMessage:
    """Parse any ``AgentMessage`` role from session JSON.

    Base roles are delegated to ``pi_ai`` so there is exactly one implementation
    of the wire format for them.
    """
    role = data.get("role")
    if role in ("user", "assistant", "toolResult"):
        return message_from_json(data)
    if role == "bashExecution":
        return BashExecutionMessage(
            command=data.get("command", ""),
            output=data.get("output", ""),
            exit_code=data.get("exitCode"),
            cancelled=bool(data.get("cancelled", False)),
            truncated=bool(data.get("truncated", False)),
            full_output_path=data.get("fullOutputPath"),
            exclude_from_context=data.get("excludeFromContext"),
            timestamp=data.get("timestamp", 0),
        )
    if role == "custom":
        return CustomMessage(
            custom_type=data.get("customType", ""),
            content=_content_from_json(data.get("content", "")),
            display=bool(data.get("display", False)),
            details=data.get("details"),
            timestamp=data.get("timestamp", 0),
        )
    if role == "branchSummary":
        return BranchSummaryMessage(
            summary=data.get("summary", ""),
            from_id=data.get("fromId"),
            timestamp=data.get("timestamp", 0),
        )
    if role == "compactionSummary":
        return CompactionSummaryMessage(
            summary=data.get("summary", ""),
            tokens_before=data.get("tokensBefore", 0),
            timestamp=data.get("timestamp", 0),
        )
    raise TypeError(f"Unknown message role: {role!r}")


# --------------------------------------------------------------------------------------
# LLM projection
# --------------------------------------------------------------------------------------


def _text_user_message(text: str, timestamp: int) -> UserMessage:
    return UserMessage(content=[TextContent(text=text)], timestamp=timestamp)


def _convert_one(message: Any) -> Message | None:
    role = getattr(message, "role", None)
    if role == "bashExecution":
        # ``!!`` commands are stored for the UI but never sent to the model.
        if message.exclude_from_context:
            return None
        return _text_user_message(bash_execution_to_text(message), message.timestamp)
    if role == "custom":
        content = message.content
        if isinstance(content, str):
            content = [TextContent(text=content)]
        elif content is None:
            content = []
        return UserMessage(content=content, timestamp=message.timestamp)
    if role == "branchSummary":
        return _text_user_message(
            BRANCH_SUMMARY_PREFIX + message.summary + BRANCH_SUMMARY_SUFFIX, message.timestamp
        )
    if role == "compactionSummary":
        return _text_user_message(
            COMPACTION_SUMMARY_PREFIX + message.summary + COMPACTION_SUMMARY_SUFFIX,
            message.timestamp,
        )
    if role in ("user", "assistant", "toolResult"):
        return message
    return None


def convert_to_llm(messages: list[Any]) -> list[Message]:
    """Project the extended message union down to ``pi_ai`` messages.

    Mirrors upstream ``convertToLlm`` exactly: ``bashExecution`` (unless it is
    excluded), ``custom``, ``branchSummary`` and ``compactionSummary`` become
    ``user`` messages; everything else passes through unchanged.  ``custom``
    session *entries* never reach this function -- see
    ``session_manager.entry_to_context_messages``.

    This runs before every LLM call and on retry/compaction paths, so it must
    never raise: an unrecognised or malformed message is dropped instead.
    """
    converted: list[Message] = []
    for message in messages:
        try:
            result = _convert_one(message)
        except Exception:  # noqa: BLE001 - defensive by contract: never raise here
            result = None
        if result is not None:
            converted.append(result)
    return converted


__all__ = [
    "BRANCH_SUMMARY_PREFIX",
    "BRANCH_SUMMARY_SUFFIX",
    "COMPACTION_SUMMARY_PREFIX",
    "COMPACTION_SUMMARY_SUFFIX",
    "AgentMessage",
    "BashExecutionMessage",
    "BranchSummaryMessage",
    "CompactionSummaryMessage",
    "CustomContent",
    "CustomMessage",
    "agent_message_from_json",
    "agent_message_to_json",
    "bash_execution_to_text",
    "convert_to_llm",
    "create_branch_summary_message",
    "create_compaction_summary_message",
    "create_custom_message",
]

"""Context compaction for long sessions.

Port of ``packages/coding-agent/src/core/compaction/compaction.ts`` upstream
(1012 lines), reduced to the essential shape the v0.1 subset needs.  Everything
here is a pure function except :func:`compact_session`, which performs the one
summarization LLM call.

What is kept faithful
---------------------
* The default settings (``enabled=True``, ``reserve_tokens=16384``,
  ``keep_recent_tokens=20000``) and the trigger rule
  ``contextTokens > contextWindow - reserveTokens``.
* The token estimate: provider ``usage`` when a valid assistant usage exists,
  otherwise the ``chars / 4`` heuristic (images count as 4800 chars).
* The cut point never lands on a tool result, and leading entries that do not
  contribute to the LLM context may be pulled into the kept region.

What is deliberately deferred (documented, not implemented in v0.1)
-------------------------------------------------------------------
* split-turn summaries (``isSplitTurn`` / ``turnPrefixMessages``) -- a cut that
  lands mid-turn is simply moved back to the enclosing assistant/user entry;
* file-operation tracking (``details.readFiles`` / ``modifiedFiles``) and the
  ``<read-files>`` / ``<modified-files>`` summary suffix;
* the iterative ``UPDATE_SUMMARIZATION_PROMPT`` merge (a previous summary is
  still fed to the model, just without the dedicated update prompt);
* the retry wrapper around the summarization call (the caller retries).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from pi_ai import (
    AssistantMessage,
    Context,
    ImageContent,
    Model,
    StreamOptions,
    TextContent,
    ThinkingContent,
    ToolCall,
    Usage,
    UserMessage,
    stream_simple,
)
from pi_agent import StreamFn

from .messages import convert_to_llm
from .session_manager import entry_to_context_messages

# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------

#: Tokens reserved for the prompt + next response; upstream default.
DEFAULT_COMPACTION_RESERVE_TOKENS = 16384
#: Tokens of recent history kept verbatim after a compaction; upstream default.
DEFAULT_COMPACTION_KEEP_RECENT_TOKENS = 20000

#: Image blocks have no token count available locally; upstream charges them a
#: flat character budget so they cannot make the estimate look free.
_ESTIMATED_IMAGE_CHARS = 4800

#: Tool arguments are serialised without whitespace, matching ``JSON.stringify``.
_JSON_SEPARATORS = (",", ":")


@dataclass(frozen=True)
class CompactionSettings:
    """Resolved compaction policy (mirrors upstream ``CompactionSettings``)."""

    enabled: bool = True
    reserve_tokens: int = DEFAULT_COMPACTION_RESERVE_TOKENS
    keep_recent_tokens: int = DEFAULT_COMPACTION_KEEP_RECENT_TOKENS


#: The upstream defaults, reused when no :class:`SettingsManager` is present.
DEFAULT_COMPACTION_SETTINGS = CompactionSettings()


@dataclass
class CompactionResult:
    """Outcome of one compaction, before the session entry is appended."""

    summary: str
    first_kept_entry_id: str
    tokens_before: int
    usage: Usage | None = None


# --------------------------------------------------------------------------------------
# Summarization prompt
# --------------------------------------------------------------------------------------

#: System prompt for the standalone summarization request.  A dedicated system
#: prompt keeps the conversation out of the "continue this" frame.
SUMMARIZATION_SYSTEM_PROMPT = (
    "You are a context summarization assistant. Your task is to read a conversation "
    "between a user and an AI assistant, then produce a structured summary following "
    "the exact format specified.\n\n"
    "Do NOT continue the conversation. Do NOT respond to any questions in the "
    "conversation. ONLY output the structured summary."
)

#: The summarization instruction.  It must preserve the user's explicit
#: requests, the decisions made, the files read or modified, and the current
#: state of the work, because those are what the next turn needs to continue.
SUMMARIZATION_PROMPT = """The messages above are a conversation to summarize. Create a structured context checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish? Preserve the user's explicit requests verbatim where possible.]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by the user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work and its exact state]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Files
- [Read]: [path] - [why it mattered]
- [Modified]: [path] - [what changed]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, and error messages."""


# --------------------------------------------------------------------------------------
# Token calculation
# --------------------------------------------------------------------------------------


def calculate_context_tokens(usage: Usage) -> int:
    """Context size from provider usage.

    Upstream uses ``totalTokens`` first and then the component sum; the Python
    agent session deliberately charges ``input + cacheRead + cacheWrite`` (the
    output of the previous turn is not part of the next request), so this helper
    exposes exactly that component sum.
    """
    return max(0, usage.input) + max(0, usage.cache_read) + max(0, usage.cache_write)


def _content_chars(content: Any) -> int:
    """Characters in a ``string | TextContent[] | ImageContent[]`` content field."""
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, (list, tuple)):
        return 0
    chars = 0
    for block in content:
        if isinstance(block, TextContent):
            chars += len(block.text)
        elif isinstance(block, ImageContent):
            chars += _ESTIMATED_IMAGE_CHARS
    return chars


def estimate_tokens(message: Any) -> int:
    """Estimate one message's token cost with the upstream ``chars / 4`` rule.

    This is conservative (it overestimates tokens) and is only used when no
    provider usage is available.
    """
    role = getattr(message, "role", None)

    if role == "user":
        return math.ceil(_content_chars(getattr(message, "content", "")) / 4)

    if role == "assistant":
        chars = 0
        for block in getattr(message, "content", []) or []:
            if isinstance(block, TextContent):
                chars += len(block.text)
            elif isinstance(block, ThinkingContent):
                chars += len(block.thinking)
            elif isinstance(block, ToolCall):
                chars += len(block.name) + len(
                    json.dumps(block.arguments, separators=_JSON_SEPARATORS, ensure_ascii=False)
                )
        return math.ceil(chars / 4)

    if role in ("custom", "toolResult"):
        return math.ceil(_content_chars(getattr(message, "content", "")) / 4)

    if role == "bashExecution":
        chars = len(getattr(message, "command", "")) + len(getattr(message, "output", ""))
        return math.ceil(chars / 4)

    if role in ("branchSummary", "compactionSummary"):
        return math.ceil(len(getattr(message, "summary", "")) / 4)

    return 0


def _valid_assistant_usage(message: Any) -> Usage | None:
    """Usage from an assistant message, skipping aborted/error/all-zero ones."""
    if getattr(message, "role", None) != "assistant":
        return None
    if getattr(message, "stop_reason", None) in ("aborted", "error"):
        return None
    usage = getattr(message, "usage", None)
    if not isinstance(usage, Usage):
        return None
    if calculate_context_tokens(usage) <= 0:
        return None
    return usage


def estimate_context_tokens(messages: Sequence[Any]) -> int:
    """Context size for a message list.

    Uses the last valid assistant usage plus an estimate for everything that
    followed it (mirroring upstream ``estimateContextTokens``); falls back to a
    pure ``chars / 4`` estimate when no usage exists at all.
    """
    for index in range(len(messages) - 1, -1, -1):
        usage = _valid_assistant_usage(messages[index])
        if usage is not None:
            trailing = sum(estimate_tokens(message) for message in messages[index + 1 :])
            return calculate_context_tokens(usage) + trailing
    return sum(estimate_tokens(message) for message in messages)


def should_compact(context_tokens: int, context_window: int, settings: CompactionSettings) -> bool:
    """``contextTokens > contextWindow - reserveTokens`` (upstream rule)."""
    if not settings.enabled:
        return False
    return context_tokens > context_window - settings.reserve_tokens


# --------------------------------------------------------------------------------------
# Cut point detection
# --------------------------------------------------------------------------------------


def _project_item(item: Any) -> list[Any]:
    """Context messages contributed by a session entry or a plain message.

    Session entries carry ``message``/``summary`` fields; runtime messages carry
    ``role`` directly.  Anything that contributes nothing to the LLM context
    yields ``[]`` so it can be skipped by the cut-point walk.
    """
    if getattr(item, "role", None) is not None:
        return [item]
    try:
        return entry_to_context_messages(item)
    except Exception:  # noqa: BLE001 - unknown/newer entries contribute nothing
        return []


def _is_valid_cut_point(item: Any) -> bool:
    """Whether history may be split immediately before ``item``.

    A tool result must stay attached to the tool call that produced it, so it is
    never a cut point; metadata-only entries and compaction boundaries are not
    cut points either.
    """
    if getattr(item, "type", None) == "compaction":
        return False
    messages = _project_item(item)
    if not messages:
        return False
    return all(getattr(message, "role", None) != "toolResult" for message in messages)


def _find_cut_index(items: Sequence[Any], keep_recent_tokens: int) -> int:
    """Index of the first entry to keep, walking backwards from the newest.

    Accumulates estimated token sizes until ``keep_recent_tokens`` is reached,
    then snaps the boundary forward to the closest valid cut point so a tool
    result is never orphaned.  Returns ``0`` when no cut point exists (the whole
    history is a candidate for summarization).
    """
    cut_points = [index for index, item in enumerate(items) if _is_valid_cut_point(item)]
    if not cut_points:
        return 0

    cut_index = cut_points[0]
    accumulated = 0
    for index in range(len(items) - 1, -1, -1):
        message_tokens = sum(estimate_tokens(message) for message in _project_item(items[index]))
        if message_tokens == 0:
            continue
        accumulated += message_tokens
        if accumulated >= keep_recent_tokens:
            for candidate in cut_points:
                if candidate >= index:
                    cut_index = candidate
                    break
            break

    # Pull leading metadata entries (model/thinking switches, labels, ...) into
    # the kept region: they do not affect the context but must stay in the tree.
    while cut_index > 0 and not _project_item(items[cut_index - 1]):
        cut_index -= 1
    return cut_index


def find_first_kept_entry_id(items: Sequence[Any], keep_recent_tokens: int) -> str | None:
    """Session entry id of the first kept entry, or ``None`` for plain messages.

    Accepts session entries (preferred, because compaction records the id in the
    session file) or raw messages (useful for estimation-only callers).
    """
    if not items:
        return None
    index = _find_cut_index(items, keep_recent_tokens)
    return getattr(items[index], "id", None) or None


# --------------------------------------------------------------------------------------
# Conversation serialization
# --------------------------------------------------------------------------------------

#: Tool results are truncated in the serialized transcript; summarization does
#: not need the full payload and large outputs would blow the summarizer budget.
_TOOL_RESULT_MAX_CHARS = 2000


def _truncate_for_summary(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n\n[... {len(text) - max_chars} more characters truncated]"


def _content_text(content: Any) -> str:
    """Concatenate the text blocks of an assistant/user content field."""
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return ""
    return "".join(block.text for block in content if isinstance(block, TextContent))


def serialize_conversation(messages: Sequence[Any]) -> str:
    """Render LLM messages as plain text so the model does not continue them."""
    parts: list[str] = []
    for message in messages:
        role = getattr(message, "role", None)
        if role == "user":
            content = _content_text(getattr(message, "content", ""))
            if content:
                parts.append(f"[User]: {content}")
        elif role == "assistant":
            thinking_parts: list[str] = []
            tool_calls: list[str] = []
            content = getattr(message, "content", []) or []
            for block in content:
                if isinstance(block, ThinkingContent):
                    thinking_parts.append(block.thinking)
                elif isinstance(block, ToolCall):
                    args = ", ".join(
                        f"{key}={json.dumps(value, ensure_ascii=False)}"
                        for key, value in (block.arguments or {}).items()
                    )
                    tool_calls.append(f"{block.name}({args})")
            if thinking_parts:
                thinking_text = "\n".join(thinking_parts)
                parts.append(f"[Assistant thinking]: {thinking_text}")
            if any(isinstance(block, TextContent) for block in content):
                parts.append(f"[Assistant]: {_content_text(content)}")
            if tool_calls:
                parts.append(f"[Assistant tool calls]: {'; '.join(tool_calls)}")
        elif role == "toolResult":
            content = _content_text(getattr(message, "content", ""))
            if content:
                parts.append(f"[Tool result]: {_truncate_for_summary(content, _TOOL_RESULT_MAX_CHARS)}")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------------------
# Summarization call
# --------------------------------------------------------------------------------------


def _default_stream_fn(model: Model, context: Context, options: StreamOptions):
    """Default summarization transport: the provider-neutral ``stream_simple``."""
    return stream_simple(model, context, options)


def _summarization_failure(response: AssistantMessage) -> str | None:
    """Reject a summary that must not become a session checkpoint."""
    if response.stop_reason == "error":
        return f"Summarization failed: {response.error_message or 'Unknown error'}"
    if response.stop_reason == "length":
        return "Summarization failed: generation hit the token cap and the summary is incomplete"
    return None


def _build_summarization_prompt(
    messages: Sequence[Any], *, custom_instructions: str | None, previous_summary: str | None
) -> str:
    conversation_text = serialize_conversation(convert_to_llm(list(messages)))
    prompt_text = f"<conversation>\n{conversation_text}\n</conversation>\n\n"
    if previous_summary:
        prompt_text += f"<previous-summary>\n{previous_summary}\n</previous-summary>\n\n"
    prompt_text += SUMMARIZATION_PROMPT
    if custom_instructions:
        prompt_text += f"\n\nAdditional focus: {custom_instructions}"
    return prompt_text


async def _generate_summary(
    messages: Sequence[Any],
    model: Model,
    settings: CompactionSettings,
    *,
    api_key: str | None,
    custom_instructions: str | None,
    previous_summary: str | None,
    stream_fn: StreamFn | None,
    signal: Any,
) -> tuple[str, Usage | None]:
    max_tokens = math.floor(0.8 * settings.reserve_tokens)
    if model.max_tokens > 0:
        max_tokens = min(max_tokens, model.max_tokens)

    prompt_text = _build_summarization_prompt(
        messages,
        custom_instructions=custom_instructions,
        previous_summary=previous_summary,
    )
    context = Context(
        messages=[
            UserMessage(
                content=[TextContent(text=prompt_text)],
                timestamp=int(time.time() * 1000),
            )
        ],
        system_prompt=SUMMARIZATION_SYSTEM_PROMPT,
        tools=[],
    )
    options = StreamOptions(api_key=api_key, signal=signal, max_tokens=max_tokens or None)
    produce: Callable[..., Any] = stream_fn or _default_stream_fn
    response = await produce(model, context, options).result()

    failure = _summarization_failure(response)
    if failure is not None:
        raise RuntimeError(failure)
    if any(isinstance(block, ToolCall) for block in response.content):
        raise RuntimeError("Summarization attempted to call a tool")

    return _content_text(response.content), response.usage


async def compact_session(
    entries: Sequence[Any],
    model: Model,
    *,
    settings: CompactionSettings | None = None,
    stream_fn: StreamFn | None = None,
    api_key: str | None = None,
    custom_instructions: str | None = None,
    previous_summary: str | None = None,
    signal: Any = None,
) -> CompactionResult:
    """Summarize the older part of ``entries`` and return a :class:`CompactionResult`.

    ``entries`` is a session path (root -> leaf).  Everything before the kept
    boundary is summarized; everything from the boundary on is preserved
    verbatim by :func:`pi_coding_agent.session_manager.build_context_entries`.
    ``stream_fn`` lets tests inject a scripted stream instead of a provider.
    """
    resolved_settings = settings or DEFAULT_COMPACTION_SETTINGS
    items = list(entries)
    if not items:
        raise ValueError("Nothing to compact (session too small)")

    cut_index = _find_cut_index(items, resolved_settings.keep_recent_tokens)
    first_kept_entry_id = getattr(items[cut_index], "id", None)
    if not first_kept_entry_id:
        raise ValueError("First kept entry has no id; the session may need migration")

    messages_to_summarize = [message for item in items[:cut_index] for message in _project_item(item)]
    if not messages_to_summarize:
        raise ValueError("Nothing to compact (session too small)")

    all_messages = [message for item in items for message in _project_item(item)]
    tokens_before = estimate_context_tokens(all_messages)

    summary, usage = await _generate_summary(
        messages_to_summarize,
        model,
        resolved_settings,
        api_key=api_key,
        custom_instructions=custom_instructions,
        previous_summary=previous_summary,
        stream_fn=stream_fn,
        signal=signal,
    )

    return CompactionResult(
        summary=summary,
        first_kept_entry_id=first_kept_entry_id,
        tokens_before=tokens_before,
        usage=usage,
    )


__all__ = [
    "DEFAULT_COMPACTION_KEEP_RECENT_TOKENS",
    "DEFAULT_COMPACTION_RESERVE_TOKENS",
    "DEFAULT_COMPACTION_SETTINGS",
    "SUMMARIZATION_PROMPT",
    "SUMMARIZATION_SYSTEM_PROMPT",
    "CompactionResult",
    "CompactionSettings",
    "calculate_context_tokens",
    "compact_session",
    "estimate_context_tokens",
    "estimate_tokens",
    "find_first_kept_entry_id",
    "serialize_conversation",
    "should_compact",
]

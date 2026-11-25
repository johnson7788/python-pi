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

What this module adds beyond the upstream v0.1 shape
--------------------------------------------------
Every one of these exists because the failure it prevents was observed, not
because upstream has it:

* **A cut budget that is a ceiling.** A single tool result can exceed the whole
  keep-recent budget on its own.  Stopping *after* adding the message that
  crosses the budget hands back an oversized "recent" window; the walk stops
  *before* it instead (see :func:`_find_cut_index`).
* **Split-turn summaries.** When the cut lands inside one turn, the discarded
  head of that turn is summarized separately, so the kept tail still carries the
  request that produced it.
* **Mechanical file-operation retention.** A summary is prose, and the paths the
  run wrote are the first detail a summarizer drops.  They are extracted from the
  tool calls and re-attached to every summary, inherited across compactions.
* **A previous summary that survives.** The last compaction's own summary is fed
  back as ``<previous-summary>`` and excluded from the messages being summarized;
  without this every compaction forgets everything but the last window.
* **A summarizer input that fits.** The request itself can overflow the window;
  the oldest messages are dropped with an explicit "do not guess" note.
* **A degraded fallback instead of a failed compaction.** Two attempts, then a
  bounded raw archive: a coarser summary beats an un-compacted window.
"""

from __future__ import annotations

import json
import math
import re
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
    #: Estimated context size after the compaction (summary + kept messages).
    tokens_after: int = 0
    #: At least one summary came from the raw archive because the summarizer
    #: failed.  The context still shrank; its memory is just coarser.
    degraded: bool = False


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


def estimate_tokens(message: Any, scale: float = 1.0) -> int:
    """Estimate one message's token cost with the upstream ``chars / 4`` rule.

    This is conservative (it overestimates tokens) and is only used when no
    provider usage is available.  ``scale`` is the learned ratio between the
    provider's count and this estimate (see :mod:`pi_coding_agent.token_scale`);
    it applies to text only, because the usage it is learned from is text.
    """
    return math.ceil(_raw_estimate_tokens(message) * max(0.0, scale))


def _raw_estimate_tokens(message: Any) -> int:
    """The unscaled ``chars / 4`` estimate for one message."""
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


def estimate_context_tokens(messages: Sequence[Any], scale: float = 1.0) -> int:
    """Context size for a message list.

    Uses the last valid assistant usage plus an estimate for everything that
    followed it (mirroring upstream ``estimateContextTokens``); falls back to a
    pure ``chars / 4`` estimate when no usage exists at all.

    ``scale`` only ever applies to the *estimated* part: the usage figure is already
    in provider tokens.
    """
    for index in range(len(messages) - 1, -1, -1):
        usage = _valid_assistant_usage(messages[index])
        if usage is not None:
            trailing = sum(
                estimate_tokens(message, scale) for message in messages[index + 1 :]
            )
            return calculate_context_tokens(usage) + trailing
    return sum(estimate_tokens(message, scale) for message in messages)


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


def _find_cut_index(items: Sequence[Any], keep_recent_tokens: int, scale: float = 1.0) -> int:
    """Index of the first entry to keep, walking backwards from the newest.

    Accumulates estimated token sizes until ``keep_recent_tokens`` is reached,
    then snaps the boundary forward to the closest valid cut point so a tool
    result is never orphaned.  Returns ``0`` when no cut point exists (the whole
    history is a candidate for summarization).
    """
    cut_points = [index for index, item in enumerate(items) if _is_valid_cut_point(item)]
    if not cut_points:
        return 0

    # ``keep_from`` is the first entry that stays verbatim.  The walk stops
    # *before* a message that would push the kept suffix past the budget, which is
    # the whole point of the ceiling: one knowledge-search-sized tool result is
    # routinely larger than the budget by itself, so "keep the recent 16k" must
    # not hand back 25k.
    keep_from = len(items)
    accumulated = 0
    for index in range(len(items) - 1, -1, -1):
        message_tokens = sum(
            estimate_tokens(message, scale) for message in _project_item(items[index])
        )
        if message_tokens == 0:
            continue
        # Only when something is already kept: a single newest message larger
        # than the budget has to be kept anyway, and any older one would exceed
        # it further.
        if accumulated > 0 and accumulated + message_tokens > keep_recent_tokens:
            keep_from = index + 1
            break
        accumulated += message_tokens
        keep_from = index
        if accumulated >= keep_recent_tokens:
            break

    if accumulated == 0:
        # Nothing measurable (or nothing at all); there is no reason to compact.
        return cut_points[0]

    # Snap forward to the next valid boundary so a tool result is never orphaned.
    # If the only boundary in reach is *inside* the target suffix (the tail is a
    # tool result), keep one turn more than the budget: overshooting by a bounded
    # amount beats either splitting a call from its result or refusing to compact.
    cut_index = cut_points[0]
    for candidate in cut_points:
        if candidate >= keep_from:
            cut_index = candidate
            break
    else:
        for candidate in reversed(cut_points):
            if candidate < keep_from:
                cut_index = candidate
                break

    # Pull leading metadata entries (model/thinking switches, labels, ...) into
    # the kept region: they do not affect the context but must stay in the tree.
    while cut_index > 0 and not _project_item(items[cut_index - 1]):
        cut_index -= 1
    return cut_index


def find_first_kept_entry_id(
    items: Sequence[Any], keep_recent_tokens: int, scale: float = 1.0
) -> str | None:
    """Session entry id of the first kept entry, or ``None`` for plain messages.

    Accepts session entries (preferred, because compaction records the id in the
    session file) or raw messages (useful for estimation-only callers).
    """
    if not items:
        return None
    index = _find_cut_index(items, keep_recent_tokens, scale)
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
# Turn boundaries, file operations, and the summarizer budget
# --------------------------------------------------------------------------------------

_TURN_PREFIX_HEADING = "## Current turn so far"

#: Paths a summary has already been told about, in the machine-readable form this
#: module appends.  Parsing them back is what makes retention transitive: the
#: compaction after the next one inherits the list instead of losing it.
_READ_FILES_BLOCK = re.compile(r"<read-files>\n(.*?)\n</read-files>", re.S)
_MODIFIED_FILES_BLOCK = re.compile(r"<modified-files>\n(.*?)\n</modified-files>", re.S)

#: A session that touched more files than this has its oldest entries dropped
#: rather than growing every subsequent request without bound.
_MAX_TRACKED_FILES = 50

#: Tools whose first path-like argument names a file the run read or changed.
_FILE_TOOLS = {"read": "read", "write": "write", "edit": "write"}
_PATH_ARGUMENTS = ("path", "file", "filePath", "file_path")


def _split_turn(items: Sequence[Any], cut_index: int) -> tuple[int, bool]:
    """Entry index where the turn containing the cut began, and whether it splits.

    A cut is on a turn boundary when the first kept message is a user message.
    Otherwise the kept tail begins mid-turn and lost the request that produced it,
    which is why the discarded head is summarized separately.
    """
    first = _project_item(items[cut_index]) if cut_index < len(items) else []
    if not first or getattr(first[0], "role", None) == "user":
        return 0, False
    for index in range(cut_index - 1, -1, -1):
        messages = _project_item(items[index])
        if any(getattr(message, "role", None) == "user" for message in messages):
            return index, True
    return 0, False


def extract_file_ops(messages: Sequence[Any]) -> dict[str, list[str]]:
    """Paths the messages read and changed, taken from the tool calls themselves.

    Insertion-ordered, deduplicated, and capped: a path is reported once, and a
    path that was written is reported only as modified even if it was also read.
    """
    reads: dict[str, None] = {}
    written: dict[str, None] = {}
    for message in messages:
        for block in getattr(message, "content", None) or []:
            if not isinstance(block, ToolCall):
                continue
            kind = _FILE_TOOLS.get((block.name or "").lower())
            if kind is None:
                continue
            arguments = block.arguments or {}
            path = next(
                (
                    arguments[key]
                    for key in _PATH_ARGUMENTS
                    if isinstance(arguments.get(key), str) and arguments[key].strip()
                ),
                "",
            )
            if not path:
                continue
            path = path.strip()
            if kind == "write":
                written[path] = None
                reads.pop(path, None)
            elif path not in written:
                reads[path] = None
    return {"read": list(reads)[-_MAX_TRACKED_FILES:], "modified": list(written)[-_MAX_TRACKED_FILES:]}


def parse_file_ops(summary: str) -> dict[str, list[str]]:
    """The file lists a previous summary already carries."""
    def paths(pattern: re.Pattern[str]) -> list[str]:
        match = pattern.search(summary or "")
        if match is None:
            return []
        return [line[2:].strip() for line in match.group(1).splitlines() if line.startswith("- ")]

    return {"read": paths(_READ_FILES_BLOCK), "modified": paths(_MODIFIED_FILES_BLOCK)}


def _merge_file_ops(*ops: dict[str, list[str]]) -> dict[str, list[str]]:
    reads = {path: None for op in ops for path in op.get("read", [])}
    writes = {path: None for op in ops for path in op.get("modified", [])}
    for path in writes:
        reads.pop(path, None)
    return {
        "read": list(reads)[-_MAX_TRACKED_FILES:],
        "modified": list(writes)[-_MAX_TRACKED_FILES:],
    }


def render_file_ops(ops: dict[str, list[str]]) -> str:
    """The machine block appended to every summary; empty when nothing was touched."""
    blocks: list[str] = []
    if ops.get("modified"):
        blocks.append("<modified-files>\n" + "\n".join(f"- {p}" for p in ops["modified"]) + "\n</modified-files>")
    if ops.get("read"):
        blocks.append("<read-files>\n" + "\n".join(f"- {p}" for p in ops["read"]) + "\n</read-files>")
    return "\n".join(blocks)


def _summarizer_budget(model: Model, max_tokens: int) -> int:
    """Input characters the summarizer request may carry, with headroom.

    ``chars / 4`` is the same over-estimate the estimator uses, so the budget can
    only be too small — the safe direction for a request that must not overflow.
    """
    window = getattr(model, "context_window", 0) or 0
    if window <= 0:
        return 400_000
    return max(8_000, (window - max_tokens - 1024) * 4)


def fit_summarizer_input(
    messages: Sequence[Any], model: Model, max_tokens: int
) -> tuple[list[Any], int]:
    """Drop the oldest messages until the request fits; returns ``(kept, omitted)``.

    Oldest-first is the only choice that keeps the *end* of the conversation,
    which is where the work in progress actually is.  The omitted count is
    reported to the model as a fact so it does not try to reconstruct the gap.
    """
    budget = _summarizer_budget(model, max_tokens)
    kept_reversed: list[Any] = []
    used = 0
    for message in reversed(list(messages)):
        size = len(serialize_conversation([message]))
        if kept_reversed and used + size > budget:
            break
        kept_reversed.append(message)
        used += size
    kept = list(reversed(kept_reversed))
    return kept, len(messages) - len(kept)


def _archive_summary(messages: Sequence[Any], previous_summary: str, max_tokens: int) -> str:
    """A bounded raw archive used when the summarizer cannot be relied on.

    Keeps the tail of the transcript (the most recent state) plus the head of any
    previous summary (normally the goal and constraints).  It is worse than a real
    summary and much better than leaving the window uncompacted.
    """
    budget = max(2_000, max_tokens * 2)  # characters; a hedge, not a target
    archive = serialize_conversation(list(messages))
    if len(archive) > budget:
        archive = "[... earlier messages omitted ...]\n" + archive[-budget:]
    previous = (previous_summary or "").strip()
    if previous:
        return f"{previous[: budget // 2]}\n\n{archive}"
    return archive


def _compose_summary(history_summary: str, turn_prefix_summary: str) -> str:
    parts = [part.strip() for part in (history_summary, turn_prefix_summary) if part.strip()]
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]}\n\n{_TURN_PREFIX_HEADING}\n{parts[1]}"


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
    messages: Sequence[Any],
    *,
    custom_instructions: str | None,
    previous_summary: str | None,
    omitted: int = 0,
) -> str:
    conversation_text = serialize_conversation(convert_to_llm(list(messages)))
    prompt_text = ""
    if omitted > 0:
        prompt_text += (
            f"<note>{omitted} earlier messages are not shown because they did not fit "
            "the request. Do not guess their content.</note>\n\n"
        )
    prompt_text += f"<conversation>\n{conversation_text}\n</conversation>\n\n"
    if previous_summary:
        prompt_text += f"<previous-summary>\n{previous_summary}\n</previous-summary>\n\n"
    prompt_text += SUMMARIZATION_PROMPT
    if custom_instructions:
        prompt_text += f"\n\nAdditional focus: {custom_instructions}"
    return prompt_text


#: One retry: a second full round-trip is worth it because the failure is usually
#: transient (an empty or truncated completion), and a third attempt means the
#: provider is not going to produce a summary at all.
_SUMMARIZER_ATTEMPTS = 2


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
    max_tokens: int | None = None,
    fallback_to_archive: bool = True,
) -> tuple[str, Usage | None, bool]:
    """Summarize ``messages``; ``(text, usage, degraded)``.

    ``degraded`` means the text came from the raw archive because every attempt
    failed.  It is still returned rather than raised: a coarse summary that shrinks
    the context beats a failed compaction that leaves the window oversized.
    """
    if max_tokens is None:
        max_tokens = math.floor(0.8 * settings.reserve_tokens)
        if model.max_tokens > 0:
            max_tokens = min(max_tokens, model.max_tokens)
    # A summary that is allowed to approach the keep-recent budget reclaims
    # nothing when it is written back, so cap it there as well.
    max_tokens = max(256, min(max_tokens, settings.keep_recent_tokens or max_tokens))

    # The request itself has to fit the window; drop the oldest messages if not.
    fitted, omitted = fit_summarizer_input(messages, model, max_tokens)
    if not fitted:
        return "", None, True

    last_error = "unknown error"
    for _attempt in range(_SUMMARIZER_ATTEMPTS):
        prompt_text = _build_summarization_prompt(
            fitted,
            custom_instructions=custom_instructions,
            previous_summary=previous_summary,
            omitted=omitted,
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
        try:
            response = await produce(model, context, options).result()
            failure = _summarization_failure(response)
            if failure is not None:
                raise RuntimeError(failure)
            if any(isinstance(block, ToolCall) for block in response.content):
                raise RuntimeError("Summarization attempted to call a tool")
            text = _content_text(response.content).strip()
            if not text:
                raise RuntimeError("Summarization returned an empty summary")
            return text, response.usage, False
        except Exception as error:  # noqa: BLE001 - retried, then degraded on purpose
            last_error = str(error)

    if not fallback_to_archive:
        raise RuntimeError(f"Summarization failed: {last_error}")
    return _archive_summary(fitted, previous_summary or "", max_tokens), None, True


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
    scale: float = 1.0,
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

    cut_index = _find_cut_index(items, resolved_settings.keep_recent_tokens, scale)
    if cut_index <= 0:
        # Either everything fits, or the only path forward would split a tool
        # call from its result.  Both mean "do not compact", not "compact badly".
        raise ValueError("Nothing to compact (session too small)")
    first_kept_entry_id = getattr(items[cut_index], "id", None)
    if not first_kept_entry_id:
        raise ValueError("First kept entry has no id; the session may need migration")

    all_messages = [message for item in items for message in _project_item(item)]
    tokens_before = estimate_context_tokens(all_messages, scale)

    # The previous summary is prompt material, not something to summarize again:
    # feeding it back in is what turns successive compactions into a summary of a
    # summary of a summary.  It is excluded below and passed as a separate block.
    previous = previous_summary or ""
    previous_index: int | None = None
    for position in range(cut_index - 1, -1, -1):
        for message in _project_item(items[position]):
            if getattr(message, "role", None) == "compactionSummary":
                previous = str(getattr(message, "summary", "") or "")
                previous_index = position
                break
        if previous_index is not None:
            break

    summarized_items = [
        item for position, item in enumerate(items[:cut_index]) if position != previous_index
    ]

    # A cut inside a turn splits it: the discarded head is summarized on its own
    # so the kept tail still carries the request that produced it.
    prefix_start, is_split = _split_turn(items, cut_index)
    if is_split:
        history_items = [item for position, item in enumerate(summarized_items) if position < prefix_start]
        prefix_items = items[prefix_start:cut_index]
    else:
        history_items = summarized_items
        prefix_items = []

    history_messages = [message for item in history_items for message in _project_item(item)]
    prefix_messages = [message for item in prefix_items for message in _project_item(item)]
    if not history_messages and not prefix_messages:
        raise ValueError("Nothing to compact (session too small)")

    history_summary = ""
    turn_prefix_summary = ""
    usage: Usage | None = None
    degraded = False

    if history_messages:
        history_summary, usage, history_degraded = await _generate_summary(
            history_messages,
            model,
            resolved_settings,
            api_key=api_key,
            custom_instructions=custom_instructions,
            previous_summary=previous,
            stream_fn=stream_fn,
            signal=signal,
        )
        degraded = degraded or history_degraded

    if prefix_messages:
        turn_prefix_summary, prefix_usage, prefix_degraded = await _generate_summary(
            prefix_messages,
            model,
            resolved_settings,
            api_key=api_key,
            custom_instructions=(
                "This is the opening of a turn whose remainder is kept verbatim. "
                "Summarize what the user asked for and what was already done, so the "
                "retained part can continue it."
            ),
            previous_summary=None,
            stream_fn=stream_fn,
            signal=signal,
            max_tokens=max(256, math.floor(0.8 * resolved_settings.reserve_tokens) // 2),
        )
        degraded = degraded or prefix_degraded
        usage = usage or prefix_usage

    summary = _compose_summary(history_summary, turn_prefix_summary)

    # Paths are the first thing a summarizer drops, so they are extracted
    # mechanically and re-attached; the previous block is inherited so the list
    # survives more than one compaction.
    file_ops = _merge_file_ops(
        parse_file_ops(previous),
        extract_file_ops([*history_messages, *prefix_messages]),
    )
    block = render_file_ops(file_ops)
    if block:
        summary = f"{summary.rstrip()}\n\n{block}"

    kept_messages = [message for item in items[cut_index:] for message in _project_item(item)]
    tokens_after = sum(estimate_tokens(message, scale) for message in kept_messages) + math.ceil(
        len(summary) / 4 * scale
    )

    return CompactionResult(
        summary=summary,
        first_kept_entry_id=first_kept_entry_id,
        tokens_before=tokens_before,
        usage=usage,
        tokens_after=tokens_after,
        degraded=degraded,
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

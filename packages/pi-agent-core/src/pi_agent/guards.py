"""Turn guards: the stop conditions that keep a run from spinning.

Why this module exists
----------------------
The bare loop has exactly one exit condition that a model cannot fake — a turn
with no tool calls and no queued message.  Everything else it can do forever: an
empty reply, the same reply twice, a tool that fails identically every round, or
a provider that keeps truncating at the completion cap.  None of those raise, so
the loop happily spends budget repeating them.

The guards below are a small state machine over the turns of **one** run:

* :meth:`TurnGuards.before_turn` enforces the iteration cap.
* :meth:`TurnGuards.after_turn` classifies a finished turn and either **nudges**
  the model with one runtime-event instruction, or **stops** the run.

Two design rules, both learned from watching this fail elsewhere:

1. **A nudge is limited, and it says what to do, not that something went wrong.**
   "Your previous reply was empty" invites an apology; "continue the task and
   write the answer" gets an answer.  The text is wrapped in a
   ``<runtime_event>`` envelope so it is visibly not user speech, and so a later
   reader of the transcript can tell machine text from human text.
2. **Stopping is not an error.**  The loop ends normally and the caller decides
   what to do with the evidence collected so far; this module never raises and
   never invents a message.

Nothing here imports the agent loop, so the rules are unit-testable directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

#: Default cap on ReAct iterations in one user turn.
#:
#: Twenty is comfortably above any real task observed and far below the point
#: where a runaway loop burns a meaningful amount of money.  ``None`` disables
#: the cap (the core library's default, so library users keep upstream
#: behaviour; the coding agent turns it on).
DEFAULT_MAX_TURNS = 20

#: Consecutive rounds a provider may truncate before the run is called off.
DEFAULT_MAX_LENGTH_STREAK = 3

#: Consecutive rounds in which every tool call failed before the model is nudged.
DEFAULT_MAX_FAILED_TOOL_STREAK = 3

GuardAction = Literal["stop", "nudge"]


@dataclass(frozen=True)
class GuardDecision:
    """One verdict from the guard state machine."""

    #: Machine-readable cause, also used as the ``<runtime_event>`` type.
    kind: str
    action: GuardAction
    #: Instruction text for a nudge; a human explanation for a stop.
    text: str


def runtime_event(kind: str, body: str) -> str:
    """Wrap machine-authored text so it cannot be mistaken for user speech.

    The envelope is deliberately boring: a tag, a type, and the instruction.  It
    round-trips through the session file as ordinary text, so no client needs to
    understand it — but a model reading its own history sees the boundary, and a
    person reading a transcript can too.
    """
    return f'<runtime_event type="{kind}">{body.strip()}</runtime_event>'


def _blocks(message: Any) -> list[Any]:
    content = getattr(message, "content", None)
    return list(content) if isinstance(content, list) else []


def _visible_text(message: Any) -> str:
    """The text a user would actually see, ignoring thinking and tool calls."""
    parts: list[str] = []
    for block in _blocks(message):
        if getattr(block, "type", None) == "text":
            parts.append(str(getattr(block, "text", "") or ""))
    return "".join(parts).strip()


def _has_tool_calls(message: Any) -> bool:
    return any(getattr(block, "type", None) == "toolCall" for block in _blocks(message))


def _has_thinking(message: Any) -> bool:
    return any(getattr(block, "type", None) == "thinking" for block in _blocks(message))


@dataclass
class TurnGuards:
    """Stop/nudge decisions for one run.

    One instance per run: :meth:`reset` clears the counters, and the owner calls
    it before each user turn.  ``stop_reason`` is read after the run to decide
    whether the turn ended because of a guard (rather than because the model
    finished).
    """

    max_turns: int | None = DEFAULT_MAX_TURNS
    #: How many empty replies to nudge before stopping (0 stops immediately).
    max_empty_replies: int = 1
    #: How many verbatim repeats to nudge before stopping.
    max_repeated_replies: int = 1
    max_length_streak: int = DEFAULT_MAX_LENGTH_STREAK
    max_failed_tool_streak: int = DEFAULT_MAX_FAILED_TOOL_STREAK

    turns: int = 0
    #: Why the last run stopped early, or ``None`` when the model finished itself.
    stop_reason: str | None = None

    _empty_replies: int = field(default=0, repr=False)
    _repeated_replies: int = field(default=0, repr=False)
    _length_streak: int = field(default=0, repr=False)
    _failed_tool_streak: int = field(default=0, repr=False)
    _tool_failure_nudged: bool = field(default=False, repr=False)
    _last_text: str = field(default="", repr=False)

    def reset(self) -> None:
        self.turns = 0
        self.stop_reason = None
        self._empty_replies = 0
        self._repeated_replies = 0
        self._length_streak = 0
        self._failed_tool_streak = 0
        self._tool_failure_nudged = False
        self._last_text = ""

    def before_turn(self) -> GuardDecision | None:
        """Count one iteration and enforce ``max_turns``."""
        self.turns += 1
        if self.max_turns is not None and self.turns > self.max_turns:
            return self._stop(
                "max_turns",
                f"Stopped after {self.max_turns} model turns without finishing.",
            )
        return None

    def after_turn(self, message: Any, tool_results: list[Any] | None = None) -> GuardDecision | None:
        """Classify one finished turn.

        An ``error``/``aborted`` turn is not a guard problem — the caller's retry
        and abort handling own those — so it passes through untouched.
        """
        stop_reason = getattr(message, "stop_reason", None)
        if stop_reason in ("error", "aborted"):
            return None

        results = list(tool_results or [])

        if stop_reason == "length":
            self._length_streak += 1
            if self._length_streak >= self.max_length_streak:
                return self._stop(
                    "truncated",
                    f"Stopped after {self._length_streak} turns truncated at the token cap.",
                )
        else:
            self._length_streak = 0

        text = _visible_text(message)
        empty = not text and not _has_tool_calls(message) and not _has_thinking(message)

        if empty:
            self._empty_replies += 1
            if self._empty_replies > self.max_empty_replies:
                return self._stop("empty_response", "Stopped after an empty reply with no retry left.")
            return GuardDecision(
                kind="empty_response",
                action="nudge",
                text=runtime_event(
                    "empty_response",
                    "Your previous reply was empty. Continue the task and write the "
                    "answer now. Do not describe this notice.",
                ),
            )

        repeated = bool(text) and stop_reason == "stop" and text == self._last_text
        self._last_text = text or self._last_text
        if repeated:
            self._repeated_replies += 1
            if self._repeated_replies > self.max_repeated_replies:
                return self._stop("repeated_response", "Stopped after the same reply was repeated.")
            return GuardDecision(
                kind="repeated_response",
                action="nudge",
                text=runtime_event(
                    "repeated_response",
                    "You repeated your previous reply verbatim. Take the next concrete "
                    "step instead: call a tool, or produce the final answer.",
                ),
            )

        if results and all(bool(getattr(result, "is_error", False)) for result in results):
            self._failed_tool_streak += 1
            if self._failed_tool_streak >= self.max_failed_tool_streak:
                if self._tool_failure_nudged:
                    return self._stop(
                        "tool_failures",
                        "Stopped after the same tool calls kept failing.",
                    )
                self._tool_failure_nudged = True
                return GuardDecision(
                    kind="tool_failures",
                    action="nudge",
                    text=runtime_event(
                        "tool_failures",
                        f"The last {self._failed_tool_streak} rounds of tool calls all "
                        "failed. Read the error text, change the approach or the "
                        "arguments, and do not repeat the same call.",
                    ),
                )
        elif results:
            self._failed_tool_streak = 0
            self._tool_failure_nudged = False

        return None

    def _stop(self, kind: str, text: str) -> GuardDecision:
        self.stop_reason = kind
        return GuardDecision(kind=kind, action="stop", text=text)

"""Human questions: the model asks, a browser tab answers.

This is the Python half of the ``ask_user_question`` seam.  It exists because a
tool that needs a human decision must be able to *stop* without stopping the
session: the tool call parks on a future, the client renders a card, and the
answer travels back as a tool result.

Four properties are deliberate, and each one is a bug we would otherwise ship:

* **The answer vocabulary is tiny.**  ``{id, selected[], custom?}``.  Options
  carry a user-facing ``label`` that *is* the value, so answers stay readable in
  the transcript, in the session file and in the model's context.  A skipped
  question is an item with an empty ``selected`` and no ``custom``; that is a
  statement about one question, not permission for the tool call.
* **Pending is a state, not a failure.**  A timed question that nobody answered
  before its deadline returns ``{"pending": true, "callId": ...}`` and stays
  answerable.  The late answer arrives as a *new* user message, never as a
  rewritten tool result.
* **The deadline has an owner.**  A connected client claims the wait and counts
  down on its own clock; the host only runs its timer while nobody holds the
  question.  Reading a question is therefore never punished with a timeout, and
  a tab that quietly disappears still releases the turn.
* **What is answerable is derived, not stored.**  :func:`fold_questions` reads
  the live message list, so a reconnect, a reload or a session switch restores
  every open question without a second source of truth.

Delegated sessions cannot ask: a child agent would block with no human attached
to it.  Such a call fails with ``DELEGATED_CALLER`` and the child is expected to
return the unresolved decision in its final result.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

#: Tool name the model calls, shared by the tool definition and the projection.
ASK_TOOL_NAME = "ask_user_question"

#: A question batch is a single decision screen, not a form; four is the cap.
MAX_QUESTIONS = 4
#: More options than this stops being scannable in a narrow chat column.
MAX_OPTIONS = 8
#: Long enough for a real question, short enough that the card stays a card.
MAX_TEXT = 2000

#: Seconds a question waits for an answer before the model is released.
DEFAULT_TIMEOUT_SECONDS = 120.0
#: Hard ceiling for ``timeout: -1`` ("wait indefinitely").  A tool that can block
#: a worker forever is a availability bug, not a feature; the question stays
#: answerable, so the only cost is that the turn moved on.
BLOCKING_CEILING_SECONDS = 900.0
#: Timer slack: how long a claimed question survives a frozen tab.
CLAIM_BACKSTOP_SECONDS = 60.0


class UserQuestionError(Exception):
    """A question failure with a stable code the model can act on."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class QuestionOption:
    """One selectable answer; ``label`` is also the value sent back."""

    label: str
    description: str | None = None

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"label": self.label}
        if self.description is not None:
            payload["description"] = self.description
        return payload


@dataclass(frozen=True)
class Question:
    """One question in a request."""

    id: str
    question: str
    header: str | None = None
    detail: str | None = None
    options: tuple[QuestionOption, ...] = ()
    multi_select: bool = False
    #: Presentation-only hint (``plan-review`` / ``approval``).  It never changes
    #: the answer encoding: the user still returns option labels.
    intent: dict[str, Any] | None = None

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "question": self.question}
        if self.header is not None:
            payload["header"] = self.header
        if self.detail is not None:
            payload["detail"] = self.detail
        if self.options:
            payload["options"] = [option.to_wire() for option in self.options]
        if self.multi_select:
            payload["multiSelect"] = True
        if self.intent is not None:
            payload["intent"] = self.intent
        return payload


@dataclass(frozen=True)
class Answer:
    """One answered (or skipped) question item."""

    id: str
    selected: tuple[str, ...] = ()
    custom: str | None = None

    @property
    def skipped(self) -> bool:
        return not self.selected and not (self.custom or "").strip()

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "selected": list(self.selected)}
        if self.custom is not None:
            payload["custom"] = self.custom
        return payload


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


def _bad(code: str, message: str) -> UserQuestionError:
    return UserQuestionError(message, code)


def _text(value: Any, field_name: str, *, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _bad("BAD_QUESTIONS", f"'{field_name}' must be a non-empty string")
    text = value.strip()
    if len(text) > limit:
        raise _bad("BAD_QUESTIONS", f"'{field_name}' is longer than {limit} characters")
    return text


def parse_questions(raw: Any) -> list[Question]:
    """Validate the model's ``questions`` argument.

    The limits are enforced here rather than in the UI so the model gets a
    precise, retryable error instead of a card it cannot render.
    """

    if not isinstance(raw, list) or not raw:
        raise _bad("EMPTY_QUESTIONS", "ask_user_question requires at least one question")
    if len(raw) > MAX_QUESTIONS:
        raise _bad("BAD_QUESTIONS", f"at most {MAX_QUESTIONS} questions per call")

    questions: list[Question] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise _bad("BAD_QUESTIONS", f"question #{index + 1} must be an object")
        question_id = _text(item.get("id"), f"questions[{index}].id", limit=80)
        if question_id in seen:
            raise _bad("BAD_QUESTIONS", f"question id {question_id!r} is used twice")
        seen.add(question_id)

        options_raw = item.get("options")
        options: list[QuestionOption] = []
        labels: set[str] = set()
        if options_raw is not None:
            if not isinstance(options_raw, list):
                raise _bad("BAD_QUESTIONS", f"questions[{index}].options must be an array")
            if len(options_raw) > MAX_OPTIONS:
                raise _bad("BAD_QUESTIONS", f"at most {MAX_OPTIONS} options per question")
            for option in options_raw:
                if not isinstance(option, dict):
                    raise _bad("BAD_QUESTIONS", f"questions[{index}].options[] must be objects")
                label = _text(option.get("label"), f"questions[{index}].options[].label", limit=120)
                if label in labels:
                    raise _bad("BAD_QUESTIONS", f"option label {label!r} is used twice")
                labels.add(label)
                description = option.get("description")
                if description is not None and not isinstance(description, str):
                    raise _bad("BAD_QUESTIONS", f"questions[{index}].options[].description must be a string")
                options.append(QuestionOption(label=label, description=description))

        header = item.get("header")
        if header is not None and not isinstance(header, str):
            raise _bad("BAD_QUESTIONS", f"questions[{index}].header must be a string")
        detail = item.get("detail")
        if detail is not None and not isinstance(detail, str):
            raise _bad("BAD_QUESTIONS", f"questions[{index}].detail must be a string")

        questions.append(
            Question(
                id=question_id,
                question=_text(item.get("question"), f"questions[{index}].question"),
                header=header.strip() if isinstance(header, str) and header.strip() else None,
                detail=detail,
                options=tuple(options),
                multi_select=bool(item.get("multi_select")),
                intent=_parse_intent(item.get("intent"), options, question_id),
            )
        )
    return questions


def _parse_intent(raw: Any, options: Sequence[QuestionOption], question_id: str) -> dict[str, Any] | None:
    """Validate a presentation hint, which only decorates the option list.

    A UI that does not know the kind renders the generic card, so the hint can
    never make a question unanswerable.  What *is* enforced is the part a UI
    would otherwise have to guess: the named action labels must exist.
    """

    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _bad("BAD_INTENT", f"questions[{question_id}].intent must be an object")
    kind = raw.get("kind")
    if kind not in ("plan-review", "approval"):
        raise _bad("BAD_INTENT", f"question {question_id!r} has an unknown intent kind")
    labels = {option.label for option in options}
    required = ["approve"] if kind == "plan-review" else ["approve", "reject"]
    intent: dict[str, Any] = {"kind": kind}
    for field in required:
        value = raw.get(field)
        if not isinstance(value, str) or value not in labels:
            raise _bad(
                "BAD_INTENT",
                f"question {question_id!r} intent names an option that was not offered: {field}={value!r}",
            )
        intent[field] = value
    always = raw.get("always")
    if always is not None:
        if not isinstance(always, str) or always not in labels:
            raise _bad("BAD_INTENT", f"question {question_id!r} intent names an unknown 'always' label")
        intent["always"] = always
    named = [intent[field] for field in required] + ([intent["always"]] if "always" in intent else [])
    if len(set(named)) != len(named):
        raise _bad("BAD_INTENT", f"question {question_id!r} intent reuses an action label")
    return intent


def questions_from_wire(raw: Any) -> list[Question]:
    """Rebuild question objects from the JSON shape a client or a log carries."""

    if not isinstance(raw, list):
        return []
    rebuilt: list[Question] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        options: list[QuestionOption] = []
        for option in item.get("options") or []:
            if isinstance(option, dict) and isinstance(option.get("label"), str):
                description = option.get("description")
                options.append(
                    QuestionOption(
                        label=option["label"],
                        description=description if isinstance(description, str) else None,
                    )
                )
        rebuilt.append(
            Question(
                id=str(item.get("id", "")),
                question=str(item.get("question", "")),
                header=item.get("header") if isinstance(item.get("header"), str) else None,
                detail=item.get("detail") if isinstance(item.get("detail"), str) else None,
                options=tuple(options),
                multi_select=bool(item.get("multiSelect")),
                intent=item.get("intent") if isinstance(item.get("intent"), dict) else None,
            )
        )
    return rebuilt


def parse_timeout(raw: Any, default: float = DEFAULT_TIMEOUT_SECONDS) -> float | None:
    """Return seconds to wait, or ``None`` for "block until answered".

    ``-1`` means the caller cannot proceed without an answer.  The broker still
    applies a safety ceiling to that form, but it reports a timeout rather than
    pretending the answer might arrive.
    """

    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise _bad("BAD_TIMEOUT", "timeout must be a number of seconds or -1")
    if raw == -1:
        return None
    if raw < 1 or raw > 2_147_483:
        raise _bad("BAD_TIMEOUT", "timeout must be -1 or between 1 and 2147483 seconds")
    return float(raw)


def parse_answers(raw: Any, questions: Sequence[Question]) -> list[Answer]:
    """Validate one answer batch against the questions it claims to answer.

    A batch must name every question exactly once — a partial batch would leave
    the model unable to match answers to questions, and the UI has no reason to
    send one.
    """

    if not isinstance(raw, list):
        raise _bad("BAD_ANSWER", "answers must be an array")
    by_id = {question.id: question for question in questions}
    seen: set[str] = set()
    answers: list[Answer] = []
    for item in raw:
        if not isinstance(item, dict):
            raise _bad("BAD_ANSWER", "each answer must be an object")
        answer_id = item.get("id")
        if not isinstance(answer_id, str) or answer_id not in by_id:
            raise _bad("BAD_ANSWER", f"answer names unknown question {answer_id!r}")
        if answer_id in seen:
            raise _bad("BAD_ANSWER", f"answer for {answer_id!r} appears twice")
        seen.add(answer_id)

        selected_raw = item.get("selected", [])
        if not isinstance(selected_raw, list) or any(not isinstance(value, str) for value in selected_raw):
            raise _bad("BAD_ANSWER", f"answer {answer_id!r} requires a string array 'selected'")
        question = by_id[answer_id]
        offered = {option.label for option in question.options}
        for label in selected_raw:
            if label not in offered:
                raise _bad("BAD_ANSWER", f"answer {answer_id!r} selects an option that was not offered: {label!r}")
        if len(selected_raw) > 1 and not question.multi_select:
            raise _bad("BAD_ANSWER", f"answer {answer_id!r} is single-select but selects {len(selected_raw)} options")

        custom = item.get("custom")
        if custom is not None and not isinstance(custom, str):
            raise _bad("BAD_ANSWER", f"answer {answer_id!r} has a non-string 'custom'")
        if custom is not None:
            custom = custom.strip() or None
        answers.append(Answer(id=answer_id, selected=tuple(selected_raw), custom=custom))

    missing = [question.id for question in questions if question.id not in seen]
    if missing:
        raise _bad("BAD_ANSWER", f"the answer batch is missing question(s): {', '.join(missing)}")
    return answers


# --------------------------------------------------------------------------------------
# Projection: what is answerable right now
# --------------------------------------------------------------------------------------


def _block_text(content: Any) -> str:
    """Extract the first text block from a tool result / custom message."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                return text
    return ""


def _settled_answers(payload: Any) -> list[Answer] | None:
    """Read ``{"answers": [...]}`` out of a tool result or a late reply payload."""

    if not isinstance(payload, dict):
        return None
    raw = payload.get("answers")
    if not isinstance(raw, list):
        return None
    answers: list[Answer] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return None
        selected = item.get("selected")
        if not isinstance(selected, list) or any(not isinstance(value, str) for value in selected):
            return None
        custom = item.get("custom")
        answers.append(
            Answer(
                id=item["id"],
                selected=tuple(selected),
                custom=custom if isinstance(custom, str) else None,
            )
        )
    return answers


def fold_questions(messages: Iterable[Any]) -> dict[str, list[dict[str, Any]]]:
    """Fold a conversation into answerable and settled questions.

    Only the message list is consulted, so the result survives a reload, a
    session switch and a server restart.  A tool call that is still running is
    ``open``; one whose result was the pending payload is ``continued`` and can
    still be answered.  Any other result settles or drops the call, because the
    model already has its outcome.
    """

    active: dict[str, dict[str, Any]] = {}
    settled: list[dict[str, Any]] = []

    for message in messages:
        role = getattr(message, "role", "")
        if role == "assistant":
            content = getattr(message, "content", None)
            if not isinstance(content, list):
                continue
            for block in content:
                if getattr(block, "type", "") != "toolCall":
                    continue
                if getattr(block, "name", "") != ASK_TOOL_NAME:
                    continue
                arguments = getattr(block, "arguments", None)
                if not isinstance(arguments, dict):
                    continue
                try:
                    questions = parse_questions(arguments.get("questions"))
                except UserQuestionError:
                    continue
                active[block.id] = {
                    "callId": block.id,
                    "questions": [question.to_wire() for question in questions],
                    "state": "open",
                }
            continue

        if role == "toolResult":
            call_id = getattr(message, "tool_call_id", "")
            if call_id not in active:
                continue
            text = _block_text(getattr(message, "content", None))
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                payload = None
            if isinstance(payload, dict) and payload.get("pending") is True:
                active[call_id] = {**active[call_id], "state": "continued"}
                continue
            answers = None if getattr(message, "is_error", False) else _settled_answers(payload)
            entry = active.pop(call_id)
            if answers is not None:
                settled.append(
                    {
                        "callId": call_id,
                        "questions": entry["questions"],
                        "answers": [answer.to_wire() for answer in answers],
                    }
                )
            continue

        if role == "custom" and getattr(message, "custom_type", "") == "user-question-reply":
            details = getattr(message, "details", None)
            if not isinstance(details, dict):
                continue
            call_id = details.get("callId")
            if not isinstance(call_id, str) or call_id not in active:
                continue
            entry = active.pop(call_id)
            answers = _settled_answers(details) or []
            settled.append(
                {
                    "callId": call_id,
                    "questions": entry["questions"],
                    "answers": [answer.to_wire() for answer in answers],
                }
            )

    return {"active": list(active.values()), "settled": settled}


# --------------------------------------------------------------------------------------
# Broker
# --------------------------------------------------------------------------------------


@dataclass
class _Pending:
    """One live question call."""

    call_id: str
    questions: list[Question]
    kind: str  # "timed" | "blocking"
    future: asyncio.Future[Any]
    #: Seconds left when nobody holds the question.  Recomputed on claim/release.
    remaining: float
    deadline: float
    timer: asyncio.TimerHandle | None = None
    claims: int = 0
    #: True once the wait was settled (answer, timeout or cancel); guards double finish.
    settled: bool = False


class QuestionBroker:
    """Owns live question waits for one session.

    The broker does not know about WebSockets: it emits plain dicts through the
    ``emit`` callable :meth:`attach` installs, which is the same seam the RPC
    mode uses for every other event.  That keeps the tool testable with a fake
    transport and keeps the server free of question-specific timing policy.
    """

    def __init__(
        self,
        *,
        delegated: bool = False,
        default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._delegated = delegated
        self.default_timeout = default_timeout
        self._emit: Callable[[dict[str, Any]], None] | None = None
        self._session: Callable[[], Any] | None = None
        self._client_count: Callable[[], int] = lambda: 1
        self._pending: dict[str, _Pending] = {}
        self._closed = False

    # -- wiring ------------------------------------------------------------------------

    @property
    def delegated(self) -> bool:
        return self._delegated

    def attach(
        self,
        *,
        emit: Callable[[dict[str, Any]], None] | None = None,
        session: Callable[[], Any] | None = None,
        client_count: Callable[[], int] | None = None,
    ) -> None:
        """Bind the transport and/or the session.  Safe to call repeatedly.

        The two halves are attached by different owners: the session binds at
        construction (so a late answer can be delivered even with no UI), and the
        transport binds when a mode with a client becomes active.
        """

        if emit is not None:
            self._emit = emit
        if session is not None:
            self._session = session
        if client_count is not None:
            self._client_count = client_count

    def detach(self) -> None:
        self._emit = None
        self._session = None

    def has_ui(self) -> bool:
        """True when somebody can actually see a question."""

        return self._emit is not None and self._client_count() > 0

    def close(self) -> None:
        """Release every waiter.  Called when the session is disposed."""

        self._closed = True
        for call_id in list(self._pending):
            self._finish(call_id, "aborted", ASK_ABORTED_MESSAGE)
        self._emit = None
        self._session = None

    # -- asking ------------------------------------------------------------------------

    async def ask(
        self,
        call_id: str,
        questions: Sequence[Question],
        timeout: float | None,
        *,
        signal: Any = None,
    ) -> dict[str, Any]:
        """Wait for an answer batch; return it or a pending marker.

        ``timeout is None`` is the "block until answered" form.  It still has a
        ceiling (see :data:`BLOCKING_CEILING_SECONDS`) so a vanished user cannot
        pin the worker forever; that path raises instead of returning pending,
        because the model asked to *block* and a pending marker would silently
        change the contract.
        """

        if self._closed:
            raise _bad("ASK_ABORTED", "the session is shutting down")
        if self._delegated:
            raise _bad(
                "DELEGATED_CALLER",
                "a delegated subagent cannot ask the user; include the unresolved "
                "question or decision in your final result instead",
            )
        if not self.has_ui():
            raise _bad(
                "NO_PROVIDER",
                "no user interface is connected, so nobody can answer; do not ask "
                "again — state your assumption and continue",
            )
        if call_id in self._pending:
            raise _bad("DUPLICATE_WAIT", "this tool call already has a pending question")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        kind = "blocking" if timeout is None else "timed"
        remaining = BLOCKING_CEILING_SECONDS if timeout is None else float(timeout)
        pending = _Pending(
            call_id=call_id,
            questions=list(questions),
            kind=kind,
            future=future,
            remaining=remaining,
            deadline=time.monotonic() + remaining,
        )
        self._pending[call_id] = pending
        self._schedule(pending)

        dispose = None
        if signal is not None:
            dispose = signal.on_abort(lambda: self._finish(call_id, "aborted", signal.reason or "cancelled"))

        self._send(
            {
                "type": "user_question_request",
                "callId": call_id,
                "questions": [question.to_wire() for question in questions],
                "blocking": kind == "blocking",
                "timeoutMs": int(remaining * 1000),
            }
        )

        try:
            outcome, payload = await future
        finally:
            if dispose is not None:
                dispose()

        if outcome == "answers":
            return {"answers": [answer.to_wire() for answer in payload]}
        if outcome == "pending":
            return {
                "pending": True,
                "callId": call_id,
                "message": (
                    "No answer arrived before the timeout. This is pending, not a skipped "
                    "answer: the question is still answerable and a reply will arrive as a "
                    "user message. Continue useful independent work; do not treat this as permission."
                ),
            }
        raise _bad("ASK_ABORTED", str(payload or ASK_ABORTED_MESSAGE))

    # -- client actions ----------------------------------------------------------------

    def claim(self, call_id: str) -> int | None:
        """Let one client own the countdown; returns remaining milliseconds.

        Claiming freezes the host deadline: the time a human spends reading is
        the client's time, not the question's.  Releasing re-arms the host clock
        with whatever the claiming client had left, which is exactly what its
        card was showing.
        """

        pending = self._pending.get(call_id)
        if pending is None:
            return None
        if pending.claims == 0:
            pending.remaining = max(0.0, pending.deadline - time.monotonic())
            pending.deadline = time.monotonic() + pending.remaining
        pending.claims += 1
        self._cancel_timer(pending)
        # A frozen tab must not hold the turn forever once it owns the timer.
        loop = asyncio.get_running_loop()
        pending.timer = loop.call_later(
            pending.remaining + CLAIM_BACKSTOP_SECONDS, self._on_timeout, call_id
        )
        return int(pending.remaining * 1000 + 0.5)

    def release(self, call_id: str) -> None:
        """Give the wait back to the host clock (tab closed, card dismissed)."""

        pending = self._pending.get(call_id)
        if pending is None or pending.claims <= 0:
            return
        pending.claims -= 1
        if pending.claims > 0:
            return
        self._cancel_timer(pending)
        pending.deadline = time.monotonic() + pending.remaining
        self._schedule(pending)

    def release_all_claims(self) -> None:
        """Every client is gone: restart every uncovered host timer.

        The caller must only invoke this when the *last* tab for the worker has
        gone; releasing one tab's claim must not restart another tab's question.
        """

        for pending in list(self._pending.values()):
            if pending.claims <= 0:
                continue
            pending.claims = 0
            self._cancel_timer(pending)
            pending.deadline = time.monotonic() + pending.remaining
            self._schedule(pending)

    def timeout(self, call_id: str) -> bool:
        """The claiming client's countdown ended."""

        if self._pending.get(call_id) is None:
            return False
        self._on_timeout(call_id)
        return True

    async def answer(self, call_id: str, raw_answers: Any, *, cancelled: bool = False) -> dict[str, Any]:
        """Answer a live question, or deliver a late answer to a continued one."""

        pending = self._pending.get(call_id)
        if pending is not None:
            if cancelled:
                # Dismissing a timed question is not a refusal: the model is
                # released exactly as a timeout would release it, and the question
                # stays answerable.  A blocking question has no such release, so
                # the caller gets an explicit cancellation instead.
                if pending.kind == "timed":
                    self._finish(call_id, "pending")
                    return {"outcome": "pending"}
                self._finish(call_id, "cancelled")
                return {"outcome": "cancelled"}
            answers = parse_answers(raw_answers, pending.questions)
            self._finish(call_id, "answers", answers)
            return {"outcome": "answered", "answers": [answer.to_wire() for answer in answers]}

        # Not live: it may still be `continued` in the transcript, which is what a
        # reloaded tab or a restarted server sees.
        view = self.pending_view()
        entry = next((item for item in view if item["callId"] == call_id), None)
        if entry is None:
            raise _bad("UNKNOWN_QUESTION", "that question is no longer answerable")
        if cancelled:
            return {"outcome": "ignored"}
        questions = questions_from_wire(entry["questions"])
        answers = parse_answers(raw_answers, questions)
        return await self._deliver_late(call_id, entry["questions"], answers)

    # -- projection --------------------------------------------------------------------

    def pending_view(self) -> list[dict[str, Any]]:
        """Every question that can still take an answer, with live timing."""

        session = self._session() if self._session is not None else None
        messages = getattr(session, "messages", None) if session is not None else None
        if not messages:
            return []
        view = fold_questions(messages)["active"]
        for entry in view:
            pending = self._pending.get(entry["callId"])
            if pending is not None:
                entry["remainingMs"] = int(max(0.0, pending.deadline - time.monotonic()) * 1000 + 0.5)
                entry["claimed"] = pending.claims > 0
                # The transcript cannot tell a blocking call from a timed one, but a
                # live wait can: without this a reloaded tab shows a countdown for a
                # question that has no deadline.
                entry["blocking"] = pending.kind == "blocking"
        return view

    # -- internals ---------------------------------------------------------------------

    def _send(self, payload: dict[str, Any]) -> None:
        if self._emit is not None:
            self._emit(payload)

    def _schedule(self, pending: _Pending) -> None:
        if pending.settled or pending.claims > 0:
            return
        delay = max(0.0, pending.deadline - time.monotonic())
        loop = asyncio.get_running_loop()
        pending.timer = loop.call_later(delay, self._on_timeout, pending.call_id)

    def _cancel_timer(self, pending: _Pending) -> None:
        if pending.timer is not None:
            pending.timer.cancel()
            pending.timer = None

    def _on_timeout(self, call_id: str) -> None:
        pending = self._pending.get(call_id)
        if pending is None or pending.settled:
            return
        # Reaching here while claimed means the claiming client's own backstop
        # fired (the tab froze mid-countdown); either way the wait is over.
        if pending.kind == "timed":
            self._finish(call_id, "pending")
        else:
            self._finish(call_id, "aborted", "ask_user_question timed out before the user answered")

    def _finish(self, call_id: str, outcome: str, payload: Any = None) -> None:
        pending = self._pending.pop(call_id, None)
        if pending is None or pending.settled:
            return
        pending.settled = True
        self._cancel_timer(pending)
        if not pending.future.done():
            pending.future.set_result((outcome, payload))
        self._send({"type": "user_question_settled", "callId": call_id, "outcome": outcome})

    async def _deliver_late(
        self, call_id: str, questions: list[dict[str, Any]], answers: Sequence[Answer]
    ) -> dict[str, Any]:
        session = self._session() if self._session is not None else None
        if session is None:
            raise _bad("NO_PROVIDER", "the session is not available to receive the answer")
        payload = {
            "kind": "answer_to_pending_question",
            "tool": ASK_TOOL_NAME,
            "callId": call_id,
            "questions": questions,
            "answers": [answer.to_wire() for answer in answers],
        }
        text = json.dumps(payload, ensure_ascii=False)
        details = {**payload}
        if bool(getattr(session, "is_streaming", False)):
            await session.send_custom_message(
                text, custom_type="user-question-reply", details=details, deliver_as="steer"
            )
            return {"outcome": "answered", "resumed": False}

        await session.send_custom_message(
            text, custom_type="user-question-reply", details=details
        )
        # The answer is in the transcript now; the model should get to react to
        # it without the user having to say "continue".
        asyncio.get_running_loop().create_task(_resume_session(session))
        return {"outcome": "answered", "resumed": True}


ASK_ABORTED_MESSAGE = "ask_user_question was aborted before the user answered"


async def _resume_session(session: Any) -> None:
    """Start one turn from the current transcript, swallowing teardown races."""

    try:
        await session.continue_turn()
    except Exception:  # noqa: BLE001 - a busy or disposed session is not an answer failure
        pass


__all__ = [
    "ASK_ABORTED_MESSAGE",
    "ASK_TOOL_NAME",
    "Answer",
    "BLOCKING_CEILING_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_OPTIONS",
    "MAX_QUESTIONS",
    "Question",
    "QuestionBroker",
    "QuestionOption",
    "UserQuestionError",
    "fold_questions",
    "parse_answers",
    "parse_questions",
    "parse_timeout",
    "questions_from_wire",
]

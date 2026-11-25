"""Structured spans for one server process, written as JSON lines.

Why JSONL and not OpenTelemetry
-------------------------------
The deployment is one process with a local data directory, and the questions asked
of it are "what did this turn do" and "which tool took 40 seconds". OTel would add an
exporter, a collector and a dependency tree for that; a dated file next to the
sessions answers the same questions with ``grep``.

Design rules, learned from tracing agents badly:

* **A span is a finished interval.** ``start``/``end`` are recorded together when the
  span closes, so a crash cannot leave half a line and a reader never has to pair
  events.
* **Arguments are described, not copied.** Tool arguments can carry file bodies,
  secrets and whole documents. Only the key names are recorded, plus a short value
  for the handful of keys that are meaningful and safe (path, command, pattern).
* **Tracing never breaks the run.** Every write is wrapped; a full disk or an
  unwritable directory degrades to a no-op.
* **Off by default.** ``PI_TRACING=1`` turns it on; a deployment that does not want
  the files should not pay for them.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

#: Keys whose value is worth recording verbatim (truncated). Everything else is
#: recorded as a type, because it may be a document or a credential.
SAFE_ARGUMENT_KEYS = frozenset(
    {"path", "file", "filePath", "file_path", "command", "pattern", "query", "url", "name"}
)

#: Values longer than this are cut; a span is an index into the session, not a copy.
MAX_VALUE_CHARS = 200

#: Never recorded, whatever the key name looks like.
SECRET_KEY_MARKERS = ("token", "secret", "password", "api_key", "apikey", "authorization")


def _redact_arguments(arguments: Any) -> dict[str, Any]:
    """A safe description of tool arguments: key names, and values only when safe."""
    if not isinstance(arguments, dict):
        return {"value": type(arguments).__name__}

    described: dict[str, Any] = {}
    for key, value in sorted(arguments.items()):
        lowered = str(key).lower()
        if any(marker in lowered for marker in SECRET_KEY_MARKERS):
            described[str(key)] = "<redacted>"
        elif key in SAFE_ARGUMENT_KEYS and isinstance(value, str):
            described[str(key)] = _shorten(value)
        else:
            described[str(key)] = type(value).__name__
    return described


def _shorten(value: str) -> str:
    text = value.replace("\n", "\\n")
    return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "…"


def _text_of(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return _shorten(content)
    if isinstance(content, (list, tuple)):
        parts = [
            getattr(block, "text", "")
            for block in content
            if getattr(block, "type", None) == "text"
        ]
        return _shorten("".join(str(part) for part in parts))
    return ""


@dataclass
class Span:
    """One completed interval."""

    trace_id: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_id: str | None = None
    kind: str = "span"
    name: str = ""
    start: float = field(default_factory=time.time)
    fields: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "name": self.name,
            "at": round(self.start, 6),
            "duration_ms": round((time.time() - self.start) * 1000, 1),
            **self.fields,
        }


class Tracer:
    """Appends spans to ``<directory>/YYYY-MM-DD.jsonl``; a no-op when disabled."""

    def __init__(self, directory: str | None, *, enabled: bool = True) -> None:
        self.directory = directory
        self.enabled = bool(enabled and directory)
        self._path: str | None = None

    def _file_for_today(self) -> str | None:
        if not self.enabled or not self.directory:
            return None
        day = time.strftime("%Y-%m-%d")
        path = os.path.join(self.directory, f"{day}.jsonl")
        if path != self._path:
            try:
                os.makedirs(self.directory, exist_ok=True)
            except OSError:
                self.enabled = False
                return None
            self._path = path
        return path

    def write(self, span: Span) -> None:
        """Append one span. Never raises: a broken tracer must not break a turn."""
        path = self._file_for_today()
        if path is None:
            return
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(span.to_json(), ensure_ascii=False) + "\n")
        except OSError:
            self.enabled = False


class NullTracer(Tracer):
    """The default: accepts the same calls and writes nothing."""

    def __init__(self) -> None:
        super().__init__(None, enabled=False)


class EventTracer:
    """Turns a session's outbound records into spans.

    Kept separate from the worker so the mapping — which record becomes which span,
    what a tool span contains — is testable without a socket, a model or a session.
    """

    def __init__(self, tracer: Tracer, *, uid: str = "", session_file: str = "") -> None:
        self.tracer = tracer
        self.uid = uid
        self.session_file = session_file
        self._trace_id: str | None = None
        self._turn_start: float | None = None
        self._turn_span_id: str | None = None
        self._tool_spans: dict[str, Span] = {}
        #: One assistant reply streams at a time, so one in-flight llm span is enough.
        self._llm_span: Span | None = None

    # -- lifecycle -------------------------------------------------------------------

    def begin_turn(self) -> None:
        self._trace_id = uuid.uuid4().hex
        self._turn_start = time.time()
        self._turn_span_id = uuid.uuid4().hex[:16]

    def end_turn(self, *, stop_reason: str = "") -> None:
        if self._trace_id is None or self._turn_start is None:
            return
        # A tool that never reported its end (abort, crash, a killed sandbox) is
        # still worth a span: "started and never finished" is the diagnosis.
        for span in self._tool_spans.values():
            span.fields["incomplete"] = True
            self.tracer.write(span)
        self._tool_spans.clear()
        self.tracer.write(
            Span(
                trace_id=self._trace_id,
                span_id=self._turn_span_id or "",
                kind="turn",
                name="prompt",
                start=self._turn_start,
                fields={
                    "uid": self.uid,
                    "session_file": self.session_file,
                    "stop_reason": stop_reason,
                },
            )
        )
        self._trace_id = None
        self._turn_start = None
        self._turn_span_id = None
        self._tool_spans.clear()
        self._llm_span = None

    def _ensure_trace(self) -> str:
        if self._trace_id is None:
            self.begin_turn()
        assert self._trace_id is not None
        return self._trace_id

    # -- records ---------------------------------------------------------------------

    def observe(self, payload: dict[str, Any]) -> None:
        if not self.tracer.enabled:
            return
        kind = payload.get("type")
        try:
            if kind == "agent_start":
                self.begin_turn()
            elif kind == "agent_end":
                self.end_turn()
            elif kind == "message_start":
                self._on_message_start(payload)
            elif kind == "message_end":
                self._on_message_end(payload)
            elif kind == "tool_execution_start":
                self._on_tool_start(payload)
            elif kind == "tool_execution_end":
                self._on_tool_end(payload)
        except Exception:  # noqa: BLE001 - tracing must never affect the turn
            return

    def _on_message_start(self, payload: dict[str, Any]) -> None:
        message = payload.get("message") or {}
        if message.get("role") != "assistant":
            return
        trace_id = self._ensure_trace()
        self._llm_span = Span(
            trace_id=trace_id,
            parent_id=self._turn_span_id,
            kind="llm",
            name="assistant",
            fields={
                "model": message.get("model") or "",
                "provider": message.get("provider") or "",
            },
        )

    def _on_message_end(self, payload: dict[str, Any]) -> None:
        message = payload.get("message") or {}
        role = message.get("role")
        trace_id = self._ensure_trace()
        if role == "assistant":
            span = self._llm_span or Span(
                trace_id=trace_id, parent_id=self._turn_span_id, kind="llm", name="assistant"
            )
            self._llm_span = None
            usage = message.get("usage") or {}
            cost = (usage.get("cost") or {})
            span.fields.update(
                {
                    "stop_reason": message.get("stopReason") or message.get("stop_reason") or "",
                    "input_tokens": int(usage.get("input") or 0),
                    "output_tokens": int(usage.get("output") or 0),
                    "cache_read_tokens": int(usage.get("cacheRead") or 0),
                    "cost_usd": float(cost.get("total") or 0.0),
                    "text_chars": len(_text_of(message)),
                }
            )
            self.tracer.write(span)
            return

        # User and tool-result messages are context, not operations.  Recording them
        # would double every turn's span count for no diagnostic value: the tool
        # spans already carry their outcome, and the session file has the text.

    def _on_tool_start(self, payload: dict[str, Any]) -> None:
        trace_id = self._ensure_trace()
        call_id = str(payload.get("toolCallId") or "")
        span = Span(
            trace_id=trace_id,
            parent_id=self._turn_span_id,
            kind="tool",
            name=str(payload.get("toolName") or "tool"),
            fields={"call_id": call_id, "args": _redact_arguments(payload.get("args"))},
        )
        self._tool_spans[call_id] = span

    def _on_tool_end(self, payload: dict[str, Any]) -> None:
        call_id = str(payload.get("toolCallId") or "")
        span = self._tool_spans.pop(call_id, None)
        if span is None:
            trace_id = self._ensure_trace()
            span = Span(
                trace_id=trace_id,
                parent_id=self._turn_span_id,
                kind="tool",
                name=str(payload.get("toolName") or "tool"),
                fields={"call_id": call_id},
            )
        result = payload.get("result") or {}
        content = result.get("content") if isinstance(result, dict) else None
        text = ""
        if isinstance(content, list):
            text = "".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        span.fields.update(
            {
                "is_error": bool(payload.get("isError")),
                "result_chars": len(text),
                "result_head": _shorten(text[:MAX_VALUE_CHARS]),
            }
        )
        self.tracer.write(span)


__all__ = [
    "EventTracer",
    "NullTracer",
    "Span",
    "Tracer",
    "SECRET_KEY_MARKERS",
    "SAFE_ARGUMENT_KEYS",
]

"""JSONL v3 session store (port of ``core/session-manager.ts``).

Interop is the whole point of this module: the files it writes must be
byte-compatible with the files real ``pi`` writes, and it must read files real
``pi`` wrote.

Layout::

    <agent_dir>/sessions/--<encoded-cwd>--/<iso-ts-with-:-.-as-->_<session-id>.jsonl

Every line is one JSON object; the first line is the session header and does not
participate in the tree.  Every other line is an entry with ``{type,id,parentId,
timestamp}`` where ``id`` is 8 hex characters and ``parentId`` is the previous
leaf (``null`` for a root).

Faithfulness details that are easy to get wrong and are therefore spelled out:

* ``json.dumps(..., separators=(",", ":"), ensure_ascii=False)`` reproduces
  ``JSON.stringify`` output (no spaces, raw UTF-8, only the mandatory escapes),
  so re-serialising a file ``pi`` wrote yields the same bytes.
* Timestamps are ``Date.toISOString()`` shaped: UTC with exactly three
  fractional digits and a trailing ``Z``.
* ``None`` means JS ``undefined`` and the key is *omitted*; ``False``/``0``/``""``
  are real values and are written.
* No file is created until the first **assistant** message is appended.  The
  first write creates the file with ``open(path, "x")`` (exclusive create, so a
  second process cannot silently clobber it) and rewrites the header plus every
  entry seen so far; later writes append one line.
* A v1 (linear) or v2 file is migrated in memory and immediately rewritten at
  v3 -- never at the cost of crashing.
* Cross-process locking (upstream uses ``proper-lockfile``) is out of scope.
  Writes are serialised in-process by an ``asyncio.Lock``, which is why the
  mutating ``append_*`` / ``set_session_name`` methods are coroutines.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, ClassVar, Union

from pi_ai import (
    TextContent,
    Usage,
    content_from_json,
    content_to_json,
    usage_from_json,
    usage_to_json,
)

# ``drop_none`` (JS ``undefined``-dropping) lives in the frozen wire helpers but is
# not re-exported from the package root.
from pi_ai.types import drop_none

from pi_coding_agent.messages import (
    AgentMessage,
    CustomContent,
    agent_message_from_json,
    agent_message_to_json,
    create_branch_summary_message,
    create_compaction_summary_message,
    create_custom_message,
)

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

CURRENT_SESSION_VERSION = 3
#: Overridable by the package's ``piConfig.configDir`` upstream; fixed here.
CONFIG_DIR_NAME = ".pi"
APP_NAME = "pi"
ENV_AGENT_DIR = "PI_CODING_AGENT_DIR"
SESSION_FILE_SUFFIX = ".jsonl"

#: Upstream reads the header through a 1 MiB bounded scan for discovery.
MAX_SESSION_HEADER_SCAN_BYTES = 1024 * 1024

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_SAFE_PATH_RE = re.compile(r"[/\\:]")
_LEADING_SEPARATOR_RE = re.compile(r"^[/\\]")
_NEWLINE_RE = re.compile(r"[\r\n]+")

#: Sentinel distinguishing "leaf not supplied" (use the last entry) from an
#: explicit ``None`` leaf (empty path).  JS makes this distinction with
#: ``undefined`` vs ``null``; Python needs a sentinel.
_UNSET: Any = object()


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def _js_numbers(value: Any) -> Any:
    """Pre-encode numbers the way ``JSON.stringify`` does.

    JS has one number type, so an integral float is written without a trailing
    ``.0`` (``0.0 -> "0"``) and ``NaN``/``Infinity`` become ``null``.  Python
    would otherwise emit ``0.0``/``NaN``, which is valid-ish JSON but not what pi
    writes -- and cost fields are integral floats almost everywhere.
    """
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        if value.is_integer() and abs(value) < 1e21:
            return int(value)
        # Non-integral floats are left to ``json.dumps``.  Both languages use
        # shortest-round-trip formatting, so they agree down to Python's
        # exponential threshold (1e-4 here vs 1e-6 in JS) -- far below any
        # realistic USD cost.
        return value
    if isinstance(value, dict):
        return {key: _js_numbers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_js_numbers(item) for item in value]
    return value


def json_dumps(value: Any) -> str:
    """``JSON.stringify``-equivalent: no spaces, raw UTF-8, JS number formatting."""
    return json.dumps(_js_numbers(value), separators=(",", ":"), ensure_ascii=False)


def now_iso() -> str:
    """``new Date().toISOString()``: UTC, millisecond precision, trailing ``Z``."""
    current = datetime.now(timezone.utc)
    return current.strftime("%Y-%m-%dT%H:%M:%S.") + f"{current.microsecond // 1000:03d}Z"


def resolve_path(path: str, base_dir: str | None = None) -> str:
    """Port of upstream ``resolvePath``: expand ``~`` and make absolute.

    ``os.path.abspath`` mirrors Node's ``path.resolve`` -- it normalises but does
    not follow symlinks, which matters because the encoded session directory
    name is derived from this string and must stay stable.
    """
    expanded = os.path.expanduser(path)
    if base_dir is None:
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(os.path.expanduser(base_dir), expanded))


def get_agent_dir(agent_dir: str | None = None) -> str:
    """``$PI_CODING_AGENT_DIR`` else ``~/.pi/agent``."""
    if agent_dir is not None:
        return resolve_path(agent_dir)
    from_env = os.environ.get(ENV_AGENT_DIR)
    if from_env:
        return resolve_path(from_env)
    return os.path.join(os.path.expanduser("~"), CONFIG_DIR_NAME, "agent")


def session_dir_name(cwd: str) -> str:
    """``--<cwd with leading separator removed and /,\\: replaced by ->--``."""
    resolved = resolve_path(cwd)
    stripped = _LEADING_SEPARATOR_RE.sub("", resolved)
    return f"--{_SAFE_PATH_RE.sub('-', stripped)}--"


def get_default_session_dir(cwd: str, agent_dir: str | None = None) -> str:
    """Default session directory for ``cwd``; created if missing (as upstream does)."""
    directory = os.path.join(get_agent_dir(agent_dir), "sessions", session_dir_name(cwd))
    os.makedirs(directory, exist_ok=True)
    return directory


def session_file_timestamp(timestamp: str) -> str:
    """``toISOString().replace(/[:.]/g, "-")`` used in the file name."""
    return timestamp.replace(":", "-").replace(".", "-")


def assert_valid_session_id(session_id: str) -> None:
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(
            "Session id must be non-empty, contain only alphanumeric characters, "
            "'-', '_', and '.', and start and end with an alphanumeric character"
        )


def create_session_id() -> str:
    """UUIDv7, matching upstream's time-ordered ``uuidv7()``.

    Time ordering keeps file listings and the encoded file timestamp monotone;
    the 8 hex format is irrelevant to pi but the version/variant nibbles are.
    """
    millis = int(time.time() * 1000) & ((1 << 48) - 1)
    value = (
        (millis << 80)
        | (0x7 << 76)
        | (secrets.randbits(12) << 64)
        | (0x2 << 62)
        | secrets.randbits(62)
    )
    return str(uuid.UUID(int=value))


def _to_millis(value: str | int | float) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed.astimezone(timezone.utc) - epoch
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _generate_id(existing: Any) -> str:
    """8 hex chars, collision-checked; full UUID after 100 tries (as upstream)."""
    for _ in range(100):
        candidate = uuid.uuid4().hex[:8]
        if candidate not in existing:
            return candidate
    return str(uuid.uuid4())


# --------------------------------------------------------------------------------------
# Entry types
# --------------------------------------------------------------------------------------


@dataclass
class SessionHeader:
    """First line of the file.  Not part of the tree (no ``id``/``parentId``)."""

    id: str
    timestamp: str
    cwd: str | None = None
    parent_session: str | None = None
    #: Absent in v1 files; always written as ``CURRENT_SESSION_VERSION``.
    version: int | None = CURRENT_SESSION_VERSION
    type: ClassVar[str] = "session"


@dataclass
class SessionEntryBase:
    """Common tree-linking fields of every non-header entry."""

    id: str
    parent_id: str | None
    timestamp: str
    #: Overridden by each concrete entry; a ClassVar so it never becomes a field
    #: (which would break dataclass field ordering).
    type: ClassVar[str] = ""


@dataclass
class SessionMessageEntry(SessionEntryBase):
    message: AgentMessage = None  # type: ignore[assignment]
    type: ClassVar[str] = "message"


@dataclass
class ModelChangeEntry(SessionEntryBase):
    provider: str = ""
    model_id: str = ""
    type: ClassVar[str] = "model_change"


@dataclass
class ThinkingLevelChangeEntry(SessionEntryBase):
    thinking_level: str = ""
    type: ClassVar[str] = "thinking_level_change"


@dataclass
class CompactionEntry(SessionEntryBase):
    summary: str = ""
    first_kept_entry_id: str = ""
    tokens_before: int = 0
    details: Any = None
    usage: Usage | None = None
    #: ``True`` if an extension generated it; ``None``/``False`` if pi did.
    from_hook: bool | None = None
    type: ClassVar[str] = "compaction"


@dataclass
class BranchSummaryEntry(SessionEntryBase):
    from_id: str = ""
    summary: str = ""
    details: Any = None
    usage: Usage | None = None
    from_hook: bool | None = None
    type: ClassVar[str] = "branch_summary"


@dataclass
class CustomEntry(SessionEntryBase):
    """Extension state.  Deliberately NOT part of the LLM context."""

    custom_type: str = ""
    data: Any = None
    type: ClassVar[str] = "custom"


@dataclass
class CustomMessageEntry(SessionEntryBase):
    """Extension-injected message.  DOES participate in the LLM context."""

    custom_type: str = ""
    content: CustomContent = ""
    #: ``True`` = styled in the TUI, ``False`` = hidden.  Never affects the LLM.
    display: bool = False
    details: Any = None
    type: ClassVar[str] = "custom_message"


@dataclass
class LabelEntry(SessionEntryBase):
    target_id: str = ""
    #: ``None`` (JS ``undefined``) clears the label and omits the JSON key.
    label: str | None = None
    type: ClassVar[str] = "label"


@dataclass
class SessionInfoEntry(SessionEntryBase):
    name: str | None = None
    type: ClassVar[str] = "session_info"


@dataclass
class UnknownEntry:
    """An entry type this port does not know yet.

    It is kept verbatim so a session written by a newer ``pi`` can be loaded and
    re-serialised without losing data (and without crashing).
    """

    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def type(self) -> str:
        return str(self.raw.get("type", ""))

    @property
    def id(self) -> str:
        value = self.raw.get("id")
        return value if isinstance(value, str) else ""

    @property
    def parent_id(self) -> str | None:
        value = self.raw.get("parentId")
        return value if isinstance(value, str) else None

    @property
    def timestamp(self) -> str:
        value = self.raw.get("timestamp")
        return value if isinstance(value, str) else ""


SessionEntry = Union[
    SessionMessageEntry,
    ThinkingLevelChangeEntry,
    ModelChangeEntry,
    CompactionEntry,
    BranchSummaryEntry,
    CustomEntry,
    CustomMessageEntry,
    LabelEntry,
    SessionInfoEntry,
    UnknownEntry,
]

FileEntry = Union[SessionHeader, SessionEntry]


# --------------------------------------------------------------------------------------
# Entry <-> JSON
# --------------------------------------------------------------------------------------


def header_to_json(header: SessionHeader) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "session"}
    if header.version is not None:
        result["version"] = header.version
    result["id"] = header.id
    result["timestamp"] = header.timestamp
    if header.cwd is not None:
        result["cwd"] = header.cwd
    if header.parent_session is not None:
        result["parentSession"] = header.parent_session
    return result


def entry_to_json(entry: FileEntry) -> dict[str, Any]:
    """Serialise a header or entry to its exact session-file JSON object.

    Key order follows the upstream object literals; combined with
    :func:`json_dumps` this makes re-serialising an untouched file byte-identical.
    """
    if isinstance(entry, SessionHeader):
        return header_to_json(entry)
    if isinstance(entry, UnknownEntry):
        return dict(entry.raw)

    base = {"type": entry.type, "id": entry.id, "parentId": entry.parent_id}
    timestamp = {"timestamp": entry.timestamp}

    if isinstance(entry, SessionMessageEntry):
        # An unparseable message (unknown role from a newer pi) is kept verbatim
        # rather than crashing the rewrite of an otherwise valid file.
        message = entry.message
        payload = message if isinstance(message, dict) else agent_message_to_json(message)
        return {**base, **timestamp, "message": payload}
    if isinstance(entry, ThinkingLevelChangeEntry):
        return {**base, **timestamp, "thinkingLevel": entry.thinking_level}
    if isinstance(entry, ModelChangeEntry):
        return {**base, **timestamp, "provider": entry.provider, "modelId": entry.model_id}
    if isinstance(entry, CompactionEntry):
        return {
            **base,
            **timestamp,
            **drop_none(
                {
                    "summary": entry.summary,
                    "firstKeptEntryId": entry.first_kept_entry_id,
                    "tokensBefore": entry.tokens_before,
                    "details": entry.details,
                    "usage": usage_to_json(entry.usage) if entry.usage is not None else None,
                    "fromHook": entry.from_hook,
                }
            ),
        }
    if isinstance(entry, BranchSummaryEntry):
        return {
            **base,
            **timestamp,
            **drop_none(
                {
                    "fromId": entry.from_id,
                    "summary": entry.summary,
                    "details": entry.details,
                    "usage": usage_to_json(entry.usage) if entry.usage is not None else None,
                    "fromHook": entry.from_hook,
                }
            ),
        }
    if isinstance(entry, CustomEntry):
        # Upstream builds this literal with the payload first; keep that order.
        result: dict[str, Any] = {"type": "custom", "customType": entry.custom_type}
        if entry.data is not None:
            result["data"] = entry.data
        result.update(base)
        result.update(timestamp)
        return result
    if isinstance(entry, CustomMessageEntry):
        result = {"type": "custom_message", "customType": entry.custom_type}
        result["content"] = (
            entry.content if isinstance(entry.content, str) else _content_list_to_json(entry.content)
        )
        result["display"] = entry.display
        if entry.details is not None:
            result["details"] = entry.details
        result.update(base)
        result.update(timestamp)
        return result
    if isinstance(entry, LabelEntry):
        return {**base, **timestamp, **drop_none({"targetId": entry.target_id, "label": entry.label})}
    if isinstance(entry, SessionInfoEntry):
        return {**base, **timestamp, **drop_none({"name": entry.name})}
    raise TypeError(f"Unknown session entry: {entry!r}")


def _content_list_to_json(content: Any) -> Any:
    if isinstance(content, list):
        return [content_to_json(block) for block in content]
    return content


def _content_from_json(content: Any) -> CustomContent:
    if isinstance(content, str) or content is None:
        return content if isinstance(content, str) else ""
    return [content_from_json(block) for block in content]


def entry_from_json(data: dict[str, Any]) -> FileEntry:
    """Parse one JSON object into the matching dataclass.

    Unknown ``type`` values become :class:`UnknownEntry` rather than an error.
    """
    entry_type = data.get("type")
    parser = _ENTRY_PARSERS.get(entry_type)
    if parser is None:
        return UnknownEntry(raw=dict(data))
    return parser(data)


def _parse_header(data: dict[str, Any]) -> SessionHeader:
    version = data.get("version")
    return SessionHeader(
        id=data.get("id", ""),
        timestamp=data.get("timestamp", ""),
        cwd=data.get("cwd"),
        parent_session=data.get("parentSession"),
        version=version if isinstance(version, int) else None,
    )


def _parse_message_entry(data: dict[str, Any]) -> SessionMessageEntry:
    raw_message = data.get("message")
    if isinstance(raw_message, dict):
        try:
            message: AgentMessage = agent_message_from_json(raw_message)
        except (TypeError, KeyError):
            message = raw_message  # type: ignore[assignment]
    else:
        message = raw_message  # type: ignore[assignment]
    return SessionMessageEntry(
        id=data.get("id", ""),
        parent_id=data.get("parentId"),
        timestamp=data.get("timestamp", ""),
        message=message,
    )


def _parse_thinking_entry(data: dict[str, Any]) -> ThinkingLevelChangeEntry:
    return ThinkingLevelChangeEntry(
        id=data.get("id", ""),
        parent_id=data.get("parentId"),
        timestamp=data.get("timestamp", ""),
        thinking_level=data.get("thinkingLevel", ""),
    )


def _parse_model_entry(data: dict[str, Any]) -> ModelChangeEntry:
    return ModelChangeEntry(
        id=data.get("id", ""),
        parent_id=data.get("parentId"),
        timestamp=data.get("timestamp", ""),
        provider=data.get("provider", ""),
        model_id=data.get("modelId", ""),
    )


def _parse_compaction_entry(data: dict[str, Any]) -> CompactionEntry:
    usage = data.get("usage")
    return CompactionEntry(
        id=data.get("id", ""),
        parent_id=data.get("parentId"),
        timestamp=data.get("timestamp", ""),
        summary=data.get("summary", ""),
        first_kept_entry_id=data.get("firstKeptEntryId", ""),
        tokens_before=data.get("tokensBefore", 0),
        details=data.get("details"),
        usage=usage_from_json(usage) if usage else None,
        from_hook=data.get("fromHook"),
    )


def _parse_branch_summary_entry(data: dict[str, Any]) -> BranchSummaryEntry:
    usage = data.get("usage")
    return BranchSummaryEntry(
        id=data.get("id", ""),
        parent_id=data.get("parentId"),
        timestamp=data.get("timestamp", ""),
        from_id=data.get("fromId", ""),
        summary=data.get("summary", ""),
        details=data.get("details"),
        usage=usage_from_json(usage) if usage else None,
        from_hook=data.get("fromHook"),
    )


def _parse_custom_entry(data: dict[str, Any]) -> CustomEntry:
    return CustomEntry(
        id=data.get("id", ""),
        parent_id=data.get("parentId"),
        timestamp=data.get("timestamp", ""),
        custom_type=data.get("customType", ""),
        data=data.get("data"),
    )


def _parse_custom_message_entry(data: dict[str, Any]) -> CustomMessageEntry:
    return CustomMessageEntry(
        id=data.get("id", ""),
        parent_id=data.get("parentId"),
        timestamp=data.get("timestamp", ""),
        custom_type=data.get("customType", ""),
        content=_content_from_json(data.get("content", "")),
        display=bool(data.get("display", False)),
        details=data.get("details"),
    )


def _parse_label_entry(data: dict[str, Any]) -> LabelEntry:
    return LabelEntry(
        id=data.get("id", ""),
        parent_id=data.get("parentId"),
        timestamp=data.get("timestamp", ""),
        target_id=data.get("targetId", ""),
        label=data.get("label"),
    )


def _parse_session_info_entry(data: dict[str, Any]) -> SessionInfoEntry:
    return SessionInfoEntry(
        id=data.get("id", ""),
        parent_id=data.get("parentId"),
        timestamp=data.get("timestamp", ""),
        name=data.get("name"),
    )


def _rechain_entry(entry: SessionEntry, parent_id: str | None) -> SessionEntry:
    """Return ``entry`` with a new ``parentId`` (upstream's ``{...entry, parentId}``).

    Branch extraction drops label entries and re-parents everything that
    followed them, so the shallow-copy-with-new-parent operation is needed for
    every entry type.  :class:`UnknownEntry` keeps its payload as a raw dict and
    therefore has no ``parentId`` field for ``replace`` to set; the spread is
    emulated on that dict instead.
    """
    if isinstance(entry, UnknownEntry):
        return UnknownEntry(raw={**entry.raw, "parentId": parent_id})
    return replace(entry, parent_id=parent_id)


#: Registry keyed on the entry ``type`` -- the single dispatch point for parsing,
#: so adding an entry type means adding exactly one row here.
_ENTRY_PARSERS: dict[str, Callable[[dict[str, Any]], FileEntry]] = {
    "session": _parse_header,
    "message": _parse_message_entry,
    "thinking_level_change": _parse_thinking_entry,
    "model_change": _parse_model_entry,
    "compaction": _parse_compaction_entry,
    "branch_summary": _parse_branch_summary_entry,
    "custom": _parse_custom_entry,
    "custom_message": _parse_custom_message_entry,
    "label": _parse_label_entry,
    "session_info": _parse_session_info_entry,
}


# --------------------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------------------


def _migrate_v1_to_v2(entries: list[dict[str, Any]]) -> None:
    """Add the ``id``/``parentId`` tree links and ``firstKeptEntryId``.

    Note ``firstKeptEntryIndex`` is an index into the **whole file** including the
    header, which is why the lookup uses ``entries`` directly.
    """
    used: set[str] = set()
    prev_id: str | None = None
    for entry in entries:
        if entry.get("type") == "session":
            entry["version"] = 2
            continue
        entry_id = _generate_id(used)
        used.add(entry_id)
        entry["id"] = entry_id
        entry["parentId"] = prev_id
        prev_id = entry_id
        if entry.get("type") == "compaction":
            index = entry.pop("firstKeptEntryIndex", None)
            if isinstance(index, int) and 0 <= index < len(entries):
                target = entries[index]
                if target.get("type") != "session":
                    entry["firstKeptEntryId"] = target.get("id", "")
            if "firstKeptEntryId" not in entry:
                entry["firstKeptEntryId"] = ""


def _migrate_v2_to_v3(entries: list[dict[str, Any]]) -> None:
    """Rename the ``hookMessage`` message role to ``custom``."""
    for entry in entries:
        if entry.get("type") == "session":
            entry["version"] = 3
            continue
        if entry.get("type") == "message":
            message = entry.get("message")
            if isinstance(message, dict) and message.get("role") == "hookMessage":
                message["role"] = "custom"


def migrate_session_entries(entries: list[dict[str, Any]]) -> bool:
    """Bring raw file entries to ``CURRENT_SESSION_VERSION``; returns whether anything changed.

    A missing ``version`` means v1 (the pre-versioning format).
    """
    header = next((entry for entry in entries if entry.get("type") == "session"), None)
    version = header.get("version", 1) if isinstance(header, dict) else 1
    if not isinstance(version, int) or version >= CURRENT_SESSION_VERSION:
        return False
    if version < 2:
        _migrate_v1_to_v2(entries)
    if version < 3:
        _migrate_v2_to_v3(entries)
    return True


# --------------------------------------------------------------------------------------
# Reading and writing files
# --------------------------------------------------------------------------------------


def parse_session_entries(text: str) -> list[dict[str, Any]]:
    """Parse JSONL text, skipping blank and malformed lines (as upstream)."""
    entries: list[dict[str, Any]] = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def _load_raw_entries(file_path: str) -> list[dict[str, Any]]:
    """Read a session file into raw JSON objects.

    Faithful to upstream ``loadEntriesFromFile``: malformed lines are skipped, a
    first line that is not a valid session header invalidates the whole file, and
    a missing trailing newline is repaired so the next append starts a new line.
    """
    if not os.path.exists(file_path):
        return []
    with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()

    entries = parse_session_entries(text)
    if not entries:
        return []
    header = entries[0]
    if header.get("type") != "session" or not isinstance(header.get("id"), str):
        return []

    if text and not text.endswith("\n"):
        with open(file_path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n")
    return entries


def load_entries_from_file(file_path: str) -> list[FileEntry]:
    """Parsed, **un-migrated** entries of a session file (header included)."""
    return [entry_from_json(data) for data in _load_raw_entries(file_path)]


def read_session_header(file_path: str) -> SessionHeader | None:
    """Read just the header, bounded to 1 MiB for discovery.

    Returns ``None`` when the file is unreadable, has no valid header inside the
    scan window, or is not a pi session.
    """
    try:
        with open(file_path, "rb") as handle:
            chunk = handle.read(MAX_SESSION_HEADER_SCAN_BYTES)
            truncated = handle.read(1) != b""
    except OSError:
        return None

    text = chunk.decode("utf-8", errors="replace")
    if truncated:
        # Only complete lines can be judged inside the scan window.
        text = text.rsplit("\n", 1)[0] + "\n"
    entries = parse_session_entries(text)
    if not entries:
        return None
    # Blank and malformed lines are skipped; the first *parsed* line decides.
    # A non-header there means the file is not a pi session.
    first = entries[0]
    if first.get("type") != "session" or not isinstance(first.get("id"), str):
        return None
    header = entry_from_json(first)
    return header if isinstance(header, SessionHeader) else None


# --------------------------------------------------------------------------------------
# Context building
# --------------------------------------------------------------------------------------


@dataclass
class ModelRef:
    provider: str
    model_id: str


@dataclass
class SessionContext:
    """What the model layer receives: messages plus the resolved session settings."""

    messages: list[AgentMessage]
    thinking_level: str
    model: ModelRef | None


def _entry_index(
    entries: list[SessionEntry], by_id: dict[str, SessionEntry] | None = None
) -> dict[str, SessionEntry]:
    if by_id is not None:
        return by_id
    index: dict[str, SessionEntry] = {}
    for entry in entries:
        if entry.id:
            index[entry.id] = entry
    return index


def build_session_path(
    entries: list[SessionEntry],
    leaf_id: Any = _UNSET,
    by_id: dict[str, SessionEntry] | None = None,
) -> list[SessionEntry]:
    """Root -> leaf path.

    ``leaf_id=None`` yields ``[]`` (the "before any entry" leaf); an unknown id
    falls back to the last entry, matching upstream's ``leaf ??= entries.at(-1)``.
    """
    index = _entry_index(entries, by_id)
    if leaf_id is None:
        return []

    leaf: SessionEntry | None = None
    if leaf_id is not _UNSET:
        leaf = index.get(leaf_id)
    if leaf is None:
        leaf = entries[-1] if entries else None
    if leaf is None:
        return []

    path: list[SessionEntry] = []
    current: SessionEntry | None = leaf
    while current is not None:
        path.append(current)
        parent_id = current.parent_id
        current = index.get(parent_id) if parent_id else None
    path.reverse()
    return path


def get_session_context_settings(path: list[SessionEntry]) -> tuple[str, ModelRef | None]:
    """Latest thinking level and model along ``path``.

    Upstream scans the **full** path, not the compaction-trimmed entry list, so a
    model/level change that was summarised away still applies.
    """
    thinking_level = "off"
    model: ModelRef | None = None
    for entry in path:
        if isinstance(entry, ThinkingLevelChangeEntry):
            thinking_level = entry.thinking_level
        elif isinstance(entry, ModelChangeEntry):
            model = ModelRef(provider=entry.provider, model_id=entry.model_id)
        elif isinstance(entry, SessionMessageEntry) and getattr(entry.message, "role", None) == "assistant":
            message = entry.message
            model = ModelRef(
                provider=getattr(message, "provider", ""), model_id=getattr(message, "model", "")
            )
    return thinking_level, model


def entry_to_context_messages(entry: SessionEntry) -> list[AgentMessage]:
    """Project one entry into runtime messages; ``[]`` for non-context entries."""
    if isinstance(entry, SessionMessageEntry):
        message = entry.message
        role = getattr(message, "role", None)
        if role in ("user", "assistant", "toolResult") and getattr(message, "content", None) is None:
            # Hand-edited/legacy files can carry a null content; the LLM call must
            # still see an array, exactly like upstream's ``{...message, content: []}``.
            if isinstance(message, dict):
                return [{**message, "content": []}]  # type: ignore[list-item]
            return [replace(message, content=[])]
        return [message]
    if isinstance(entry, CustomMessageEntry):
        return [
            create_custom_message(
                entry.custom_type, entry.content, entry.display, entry.details, entry.timestamp
            )
        ]
    if isinstance(entry, BranchSummaryEntry) and entry.summary:
        return [create_branch_summary_message(entry.summary, entry.from_id, entry.timestamp)]
    if isinstance(entry, CompactionEntry):
        return [
            create_compaction_summary_message(entry.summary, entry.tokens_before, entry.timestamp)
        ]
    # ``custom`` entries (extension state) and every metadata entry contribute nothing.
    return []


def build_context_entries(
    entries: list[SessionEntry],
    leaf_id: Any = _UNSET,
    by_id: dict[str, SessionEntry] | None = None,
) -> list[SessionEntry]:
    """Compaction-aware active entry list for the current leaf path.

    Rule (upstream ``buildContextEntries``)::

        [latest compaction]
        + path[firstKeptEntryId .. compactionIndex)
        + path[compactionIndex+1 ..]

    The compaction summary replaces the older, summarised entries; the entries
    from ``firstKeptEntryId`` onward survive verbatim.
    """
    path = build_session_path(entries, leaf_id, by_id)
    compaction: CompactionEntry | None = None
    for entry in path:
        if isinstance(entry, CompactionEntry):
            compaction = entry
    if compaction is None:
        return path

    try:
        compaction_index = next(i for i, entry in enumerate(path) if entry.id == compaction.id)
    except StopIteration:
        return path

    context_entries: list[SessionEntry] = [compaction]
    found_first_kept = False
    for i in range(compaction_index):
        entry = path[i]
        if entry.id == compaction.first_kept_entry_id:
            found_first_kept = True
        if found_first_kept:
            context_entries.append(entry)
    context_entries.extend(path[compaction_index + 1 :])
    return context_entries


def build_session_context(
    entries: list[SessionEntry],
    leaf_id: Any = _UNSET,
    by_id: dict[str, SessionEntry] | None = None,
) -> SessionContext:
    path = build_session_path(entries, leaf_id, by_id)
    thinking_level, model = get_session_context_settings(path)
    messages: list[AgentMessage] = []
    for entry in build_context_entries(entries, leaf_id, by_id):
        messages.extend(entry_to_context_messages(entry))
    return SessionContext(messages=messages, thinking_level=thinking_level, model=model)


def get_latest_compaction_entry(entries: list[SessionEntry]) -> CompactionEntry | None:
    for entry in reversed(entries):
        if isinstance(entry, CompactionEntry):
            return entry
    return None


# --------------------------------------------------------------------------------------
# Session listing
# --------------------------------------------------------------------------------------


@dataclass
class SessionInfo:
    path: str
    id: str
    cwd: str
    created: datetime
    modified: datetime
    message_count: int
    first_message: str
    all_messages_text: str
    name: str | None = None
    parent_session_path: str | None = None


@dataclass
class SessionTreeNode:
    entry: SessionEntry
    children: list[SessionTreeNode] = field(default_factory=list)
    label: str | None = None
    label_timestamp: str | None = None


def _message_text_content(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, TextContent):
            parts.append(block.text)
    return " ".join(parts)


def _build_session_info(file_path: str) -> SessionInfo | None:
    try:
        stat = os.stat(file_path)
    except OSError:
        return None
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
            raw_entries = parse_session_entries(handle.read())
    except OSError:
        return None

    header: dict[str, Any] | None = None
    message_count = 0
    first_message = ""
    all_messages: list[str] = []
    name: str | None = None
    last_activity: int | None = None

    for data in raw_entries:
        if header is None:
            if data.get("type") != "session" or not isinstance(data.get("id"), str):
                return None
            header = data
            continue
        if data.get("type") == "session_info":
            raw_name = data.get("name")
            name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
            continue
        if data.get("type") != "message":
            continue
        message_count += 1
        message = data.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        timestamp = message.get("timestamp")
        if isinstance(timestamp, (int, float)):
            last_activity = max(last_activity or 0, int(timestamp))
        else:
            last_activity = max(last_activity or 0, _to_millis(data.get("timestamp", "")))
        try:
            parsed = agent_message_from_json(message)
        except (TypeError, KeyError):
            continue
        text = _message_text_content(parsed)
        if not text:
            continue
        all_messages.append(text)
        if not first_message and role == "user":
            first_message = text

    if header is None:
        return None

    cwd = header.get("cwd")
    cwd = cwd if isinstance(cwd, str) else ""
    header_time = header.get("timestamp")
    created = _parse_iso(header_time)
    # Prefer the last user/assistant activity time, then the header time, then mtime.
    modified = created if _is_valid_iso(header_time) else datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    if last_activity:
        modified = datetime.fromtimestamp(last_activity / 1000, tz=timezone.utc)

    return SessionInfo(
        path=file_path,
        id=header["id"],
        cwd=cwd,
        name=name,
        parent_session_path=header.get("parentSession"),
        created=created,
        modified=modified,
        message_count=message_count,
        first_message=first_message or "(no messages)",
        all_messages_text=" ".join(all_messages),
    )


def _parse_iso(value: Any) -> datetime:
    if not isinstance(value, str):
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_valid_iso(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------------------
# SessionManager
# --------------------------------------------------------------------------------------


class SessionManager:
    """Append-only conversation tree persisted as JSONL.

    Appending creates a child of the current leaf and advances the leaf;
    :meth:`branch` moves the leaf back so the next append forks the history
    without rewriting anything.
    """

    def __init__(
        self,
        cwd: str,
        session_dir: str,
        session_file: str | None = None,
        persist: bool = True,
        *,
        preloaded_raw: list[dict[str, Any]] | None = None,
        session_id: str | None = None,
        parent_session: str | None = None,
    ) -> None:
        self._cwd = resolve_path(cwd)
        self._session_dir = os.path.abspath(session_dir) if session_dir else ""
        self._persist_enabled = persist
        self._flushed = False
        self._file_entries: list[FileEntry] = []
        self._by_id: dict[str, SessionEntry] = {}
        self._labels_by_id: dict[str, str] = {}
        self._label_timestamps_by_id: dict[str, str] = {}
        self._leaf_id: str | None = None
        self._session_id = session_id or ""
        self._session_file: str | None = None
        #: In-process write serialisation.  Upstream uses ``proper-lockfile`` for
        #: cross-process safety; that is deliberately out of scope here.
        self._lock = asyncio.Lock()

        if persist and self._session_dir:
            os.makedirs(self._session_dir, exist_ok=True)

        if session_file:
            self._set_session_file(session_file, preloaded_raw)
        elif preloaded_raw:
            self._load_raw(preloaded_raw, session_id=session_id, parent_session=parent_session)
        else:
            self.new_session(session_id=session_id, parent_session=parent_session)

    # -- construction ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        cwd: str,
        agent_dir: str | None = None,
        *,
        session_dir: str | None = None,
        session_id: str | None = None,
        parent_session: str | None = None,
    ) -> SessionManager:
        directory = (
            resolve_path(session_dir) if session_dir is not None else get_default_session_dir(cwd, agent_dir)
        )
        return cls(
            cwd,
            directory,
            None,
            True,
            session_id=session_id,
            parent_session=parent_session,
        )

    @classmethod
    def fork_from(
        cls,
        source_path: str,
        target_cwd: str,
        session_dir: str | None = None,
        *,
        session_id: str | None = None,
    ) -> SessionManager:
        """Fork another project's session into ``target_cwd`` as a brand new session.

        The new file carries a fresh session id (and header timestamp) but the
        source's entries are copied *verbatim* -- ids and ``parentId`` links are
        preserved, so the conversation tree is unchanged and only the header
        differs.  ``parentSession`` records where the history came from.

        Entries are copied as their **raw parsed JSON objects** rather than
        through :func:`entry_from_json`/:func:`entry_to_json`, which is what
        upstream does: it keeps the source's key order, any fields this port
        does not model yet, and unknown entry types byte-identical instead of
        normalising them.
        """
        resolved_source = resolve_path(source_path)
        resolved_target = resolve_path(target_cwd)

        source_entries = _load_raw_entries(resolved_source)
        if not source_entries:
            raise ValueError(
                f"Cannot fork: source session file is empty or invalid: {resolved_source}"
            )
        # ``_load_raw_entries`` already rejects a file whose first line is not a
        # session header, so this guard only mirrors upstream's defensive check.
        if not any(entry.get("type") == "session" for entry in source_entries):
            raise ValueError(f"Cannot fork: source session has no header: {resolved_source}")

        directory = (
            resolve_path(session_dir)
            if session_dir is not None
            else get_default_session_dir(resolved_target)
        )
        os.makedirs(directory, exist_ok=True)

        if session_id is not None:
            assert_valid_session_id(session_id)
        new_session_id = session_id or create_session_id()
        timestamp = now_iso()
        new_session_file = os.path.join(
            directory,
            f"{session_file_timestamp(timestamp)}_{new_session_id}{SESSION_FILE_SUFFIX}",
        )

        header = SessionHeader(
            id=new_session_id,
            timestamp=timestamp,
            cwd=resolved_target,
            parent_session=resolved_source,
            version=CURRENT_SESSION_VERSION,
        )
        lines = [json_dumps(header_to_json(header))]
        lines.extend(
            json_dumps(entry) for entry in source_entries if entry.get("type") != "session"
        )
        # Exclusive create, like the rest of the module: the fresh timestamp and
        # session id make an existing path a bug rather than something to clobber.
        with open(new_session_file, "x", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")

        return cls(resolved_target, directory, new_session_file, True)

    @classmethod
    def open(
        cls,
        path: str,
        agent_dir: str | None = None,
        cwd: str | None = None,
    ) -> SessionManager:
        del agent_dir  # kept for API symmetry; upstream derives the dir from the file
        resolved = resolve_path(path)
        header = read_session_header(resolved) if cwd is None and os.path.exists(resolved) else None
        effective_cwd = cwd or (header.cwd if header and header.cwd else None) or os.getcwd()
        directory = os.path.dirname(resolved)
        return cls(effective_cwd, directory, resolved, True)

    @classmethod
    def in_memory(
        cls,
        cwd: str | None = None,
        *,
        entries: list[FileEntry | dict[str, Any]] | None = None,
        session_id: str | None = None,
    ) -> SessionManager:
        raw = [_entry_to_raw(entry) for entry in entries] if entries else None
        return cls(cwd or os.getcwd(), "", None, False, preloaded_raw=raw, session_id=session_id)

    @classmethod
    async def list(
        cls, cwd: str, agent_dir: str | None = None, session_dir: str | None = None
    ) -> list[SessionInfo]:
        directory = (
            resolve_path(session_dir) if session_dir is not None else get_default_session_dir(cwd, agent_dir)
        )
        sessions: list[SessionInfo] = []
        if not os.path.isdir(directory):
            return sessions
        try:
            names = os.listdir(directory)
        except OSError:
            return sessions
        for name in names:
            if not name.endswith(SESSION_FILE_SUFFIX):
                continue
            info = _build_session_info(os.path.join(directory, name))
            if info is not None:
                sessions.append(info)
        sessions.sort(key=lambda info: info.modified, reverse=True)
        return sessions

    # -- state -------------------------------------------------------------------------

    def new_session(self, session_id: str | None = None, parent_session: str | None = None) -> str | None:
        if session_id is not None:
            assert_valid_session_id(session_id)
        self._session_id = session_id or create_session_id()
        timestamp = now_iso()
        self._file_entries = [
            SessionHeader(
                id=self._session_id,
                timestamp=timestamp,
                cwd=self._cwd,
                parent_session=parent_session,
                version=CURRENT_SESSION_VERSION,
            )
        ]
        self._by_id.clear()
        self._labels_by_id.clear()
        self._label_timestamps_by_id.clear()
        self._leaf_id = None
        self._flushed = False
        if self._persist_enabled and self._session_dir:
            file_name = f"{session_file_timestamp(timestamp)}_{self._session_id}{SESSION_FILE_SUFFIX}"
            self._session_file = os.path.join(self._session_dir, file_name)
        return self._session_file

    def _set_session_file(self, session_file: str, preloaded_raw: list[dict[str, Any]] | None) -> None:
        self._session_file = resolve_path(session_file)
        if os.path.exists(self._session_file):
            raw = preloaded_raw if preloaded_raw is not None else _load_raw_entries(self._session_file)
            if not raw:
                # An empty file is initialised with a valid header; a non-empty file
                # that does not parse as a pi session is left untouched and rejected.
                if os.path.getsize(self._session_file) > 0:
                    raise ValueError(
                        f"Session file is not a valid {APP_NAME} session: {self._session_file}"
                    )
                explicit = self._session_file
                self.new_session()
                self._session_file = explicit
                self._rewrite_file()
                self._flushed = True
                return
            self._load_raw(raw)
            self._flushed = True
        else:
            explicit = self._session_file
            self.new_session()
            self._session_file = explicit

    def _load_raw(
        self,
        raw: list[dict[str, Any]],
        *,
        session_id: str | None = None,
        parent_session: str | None = None,
    ) -> None:
        migrated = migrate_session_entries(raw)
        entries = [entry_from_json(data) for data in raw]
        header = next((e for e in entries if isinstance(e, SessionHeader)), None)
        if header is not None:
            self._file_entries = entries
            self._session_id = header.id
            if migrated:
                # Upstream rewrites immediately after a migration so the file on
                # disk is always at the current version.
                self._rewrite_file()
        else:
            self.new_session(session_id=session_id, parent_session=parent_session)
            self._file_entries = self._file_entries + entries
        self._build_index()

    def _build_index(self) -> None:
        self._by_id.clear()
        self._labels_by_id.clear()
        self._label_timestamps_by_id.clear()
        self._leaf_id = None
        for entry in self._file_entries:
            if isinstance(entry, SessionHeader):
                continue
            if entry.id:
                self._by_id[entry.id] = entry
                self._leaf_id = entry.id
            if isinstance(entry, LabelEntry):
                if entry.label:
                    self._labels_by_id[entry.target_id] = entry.label
                    self._label_timestamps_by_id[entry.target_id] = entry.timestamp
                else:
                    self._labels_by_id.pop(entry.target_id, None)
                    self._label_timestamps_by_id.pop(entry.target_id, None)

    # -- persistence -------------------------------------------------------------------

    def _rewrite_file(self) -> None:
        if not self._persist_enabled or not self._session_file:
            return
        with open(self._session_file, "w", encoding="utf-8", newline="\n") as handle:
            for entry in self._file_entries:
                handle.write(json_dumps(entry_to_json(entry)) + "\n")

    def _append_line(self, entry: FileEntry) -> None:
        assert self._session_file is not None
        with open(self._session_file, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json_dumps(entry_to_json(entry)) + "\n")

    def _persist(self, entry: SessionEntry) -> None:
        """Write ``entry``, honouring the "no file before the first assistant" quirk."""
        if not self._persist_enabled or not self._session_file:
            return

        has_assistant = any(
            isinstance(e, SessionMessageEntry) and getattr(e.message, "role", None) == "assistant"
            for e in self._file_entries
        )
        if not has_assistant:
            if self._flushed:
                self._append_line(entry)
            else:
                # Defer: the header plus every deferred entry is written wholesale
                # once an assistant message arrives, so empty sessions leave no file.
                self._flushed = False
            return

        if not self._flushed:
            # ``x`` (exclusive create) fails loudly instead of clobbering a file a
            # concurrent pi process may already own.
            with open(self._session_file, "x", encoding="utf-8", newline="\n") as handle:
                for pending in self._file_entries:
                    handle.write(json_dumps(entry_to_json(pending)) + "\n")
            self._flushed = True
        else:
            self._append_line(entry)

    def _append_entry(self, entry: SessionEntry) -> None:
        self._file_entries.append(entry)
        if entry.id:
            self._by_id[entry.id] = entry
            self._leaf_id = entry.id
        self._persist(entry)

    # -- append API --------------------------------------------------------------------

    async def append_message(self, message: AgentMessage) -> str:
        """Append a message; returns the entry id.

        ``compactionSummary`` / ``branchSummary`` messages are rejected: they must
        be top-level entries (``append_compaction`` / ``append_branch_summary``)
        so the tree walk can find them reliably.
        """
        role = getattr(message, "role", None)
        if role in ("compactionSummary", "branchSummary"):
            raise ValueError(
                f"Cannot append a {role} message directly; use append_compaction() "
                "or append_branch_summary() so it becomes a top-level session entry"
            )
        async with self._lock:
            entry = SessionMessageEntry(
                id=self._next_id(),
                parent_id=self._leaf_id,
                timestamp=now_iso(),
                message=message,
            )
            self._append_entry(entry)
            return entry.id

    async def append_thinking_level_change(self, level: str) -> str:
        async with self._lock:
            entry = ThinkingLevelChangeEntry(
                id=self._next_id(),
                parent_id=self._leaf_id,
                timestamp=now_iso(),
                thinking_level=level,
            )
            self._append_entry(entry)
            return entry.id

    async def append_model_change(self, provider: str, model_id: str) -> str:
        async with self._lock:
            entry = ModelChangeEntry(
                id=self._next_id(),
                parent_id=self._leaf_id,
                timestamp=now_iso(),
                provider=provider,
                model_id=model_id,
            )
            self._append_entry(entry)
            return entry.id

    async def append_compaction(
        self,
        summary: str,
        first_kept_entry_id: str,
        tokens_before: int,
        details: Any = None,
        usage: Usage | None = None,
    ) -> str:
        async with self._lock:
            entry = CompactionEntry(
                id=self._next_id(),
                parent_id=self._leaf_id,
                timestamp=now_iso(),
                summary=summary,
                first_kept_entry_id=first_kept_entry_id,
                tokens_before=tokens_before,
                details=details,
                usage=usage,
            )
            self._append_entry(entry)
            return entry.id

    async def append_branch_summary(
        self,
        from_id: str,
        summary: str,
        details: Any = None,
        usage: Usage | None = None,
    ) -> str:
        async with self._lock:
            entry = BranchSummaryEntry(
                id=self._next_id(),
                parent_id=self._leaf_id,
                timestamp=now_iso(),
                from_id=from_id,
                summary=summary,
                details=details,
                usage=usage,
            )
            self._append_entry(entry)
            return entry.id

    async def append_custom(self, custom_type: str, data: Any = None) -> str:
        async with self._lock:
            entry = CustomEntry(
                id=self._next_id(),
                parent_id=self._leaf_id,
                timestamp=now_iso(),
                custom_type=custom_type,
                data=data,
            )
            self._append_entry(entry)
            return entry.id

    async def append_custom_message(
        self,
        custom_type: str,
        content: CustomContent,
        display: bool,
        details: Any = None,
    ) -> str:
        async with self._lock:
            entry = CustomMessageEntry(
                id=self._next_id(),
                parent_id=self._leaf_id,
                timestamp=now_iso(),
                custom_type=custom_type,
                content=content,
                display=display,
                details=details,
            )
            self._append_entry(entry)
            return entry.id

    async def append_label(self, target_id: str, label: str | None) -> str:
        if target_id not in self._by_id:
            raise ValueError(f"Entry {target_id} not found")
        async with self._lock:
            entry = LabelEntry(
                id=self._next_id(),
                parent_id=self._leaf_id,
                timestamp=now_iso(),
                target_id=target_id,
                label=label,
            )
            self._append_entry(entry)
            if label:
                self._labels_by_id[target_id] = label
                self._label_timestamps_by_id[target_id] = entry.timestamp
            else:
                self._labels_by_id.pop(target_id, None)
                self._label_timestamps_by_id.pop(target_id, None)
            return entry.id

    async def set_session_name(self, name: str) -> str:
        # Newlines would corrupt the one-line-per-entry JSONL view in the TUI.
        sanitized = _NEWLINE_RE.sub(" ", name).strip()
        async with self._lock:
            entry = SessionInfoEntry(
                id=self._next_id(),
                parent_id=self._leaf_id,
                timestamp=now_iso(),
                name=sanitized,
            )
            self._append_entry(entry)
            return entry.id

    def _next_id(self) -> str:
        return _generate_id(self._by_id)

    # -- accessors ---------------------------------------------------------------------

    def get_cwd(self) -> str:
        return self._cwd

    def get_session_dir(self) -> str:
        return self._session_dir

    def get_session_id(self) -> str:
        return self._session_id

    def get_session_file(self) -> str | None:
        return self._session_file

    def is_persisted(self) -> bool:
        return self._persist_enabled

    def get_header(self) -> SessionHeader | None:
        for entry in self._file_entries:
            if isinstance(entry, SessionHeader):
                return entry
        return None

    def get_entries(self) -> list[SessionEntry]:
        return [entry for entry in self._file_entries if not isinstance(entry, SessionHeader)]

    def get_leaf_id(self) -> str | None:
        return self._leaf_id

    def get_leaf_entry(self) -> SessionEntry | None:
        return self._by_id.get(self._leaf_id) if self._leaf_id else None

    def get_entry(self, entry_id: str) -> SessionEntry | None:
        return self._by_id.get(entry_id)

    def get_children(self, parent_id: str) -> list[SessionEntry]:
        return [entry for entry in self._by_id.values() if entry.parent_id == parent_id]

    def get_label(self, entry_id: str) -> str | None:
        return self._labels_by_id.get(entry_id)

    def get_labels(self) -> dict[str, str]:
        return dict(self._labels_by_id)

    def get_path(self, entry_id: str | None = None) -> list[SessionEntry]:
        """Root -> entry path (defaults to the current leaf)."""
        path: list[SessionEntry] = []
        current = self._by_id.get(entry_id) if entry_id else self.get_leaf_entry()
        while current is not None:
            path.append(current)
            current = self._by_id.get(current.parent_id) if current.parent_id else None
        path.reverse()
        return path

    #: Upstream spelling of :meth:`get_path`.
    get_branch = get_path

    def get_tree(self) -> list[SessionTreeNode]:
        node_map: dict[str, SessionTreeNode] = {}
        roots: list[SessionTreeNode] = []
        entries = self.get_entries()
        for entry in entries:
            node_map[entry.id] = SessionTreeNode(
                entry=entry,
                label=self._labels_by_id.get(entry.id),
                label_timestamp=self._label_timestamps_by_id.get(entry.id),
            )
        for entry in entries:
            node = node_map[entry.id]
            if entry.parent_id is None or entry.parent_id == entry.id:
                roots.append(node)
                continue
            parent = node_map.get(entry.parent_id)
            if parent is None:
                roots.append(node)
            else:
                parent.children.append(node)
        stack = list(roots)
        while stack:
            node = stack.pop()
            node.children.sort(key=lambda child: _to_millis(child.entry.timestamp))
            stack.extend(node.children)
        return roots

    def get_session_name(self) -> str | None:
        entries = self.get_entries()
        for entry in reversed(entries):
            if isinstance(entry, SessionInfoEntry):
                return (entry.name or "").strip() or None
        return None

    # -- tree navigation ---------------------------------------------------------------

    def branch(self, branch_from_id: str) -> None:
        if branch_from_id not in self._by_id:
            raise ValueError(f"Entry {branch_from_id} not found")
        self._leaf_id = branch_from_id

    def reset_leaf(self) -> None:
        self._leaf_id = None

    def create_branched_session(self, leaf_id: str) -> str | None:
        """Rewrite the root -> ``leaf_id`` path into a fresh single-path session file.

        The new manager keeps the same cwd but a new session id, and its header
        points back at the old file via ``parentSession``.  Returns the new file
        path, or ``None`` for an in-memory (non-persisted) manager, which instead
        rebinds its entries in place -- upstream's ``createBranchedSession``.

        Label entries cannot simply be filtered out: they are real tree entries,
        so anything appended after a label is a child *of the label*.  Dropping
        one would orphan the rest of the path, so labels are removed while
        remembering, for each, the id of the next retained entry; every retained
        entry is then re-chained onto its predecessor (the first one becomes a
        root) and a compaction's ``firstKeptEntryId`` is translated through that
        map.  Live labels whose target survived are re-appended at the end, so
        the branch does not silently lose them.
        """
        previous_session_file = self._session_file
        # ``get_path`` treats a falsy id as "current leaf"; an empty id is an
        # unknown leaf upstream (``byId.get("")`` misses), so keep it unknown.
        path = [] if leaf_id == "" else self.get_path(leaf_id)
        if not path:
            raise ValueError(f"Entry {leaf_id} not found")

        path_without_labels: list[SessionEntry] = []
        replacement_by_label_id: dict[str, str] = {}
        pending_label_ids: list[str] = []
        path_parent_id: str | None = None
        for entry in path:
            if isinstance(entry, LabelEntry):
                pending_label_ids.append(entry.id)
                continue
            # A label's replacement is the next *retained* entry, chaining every
            # id we walked past onto the same continuation.
            for label_id in pending_label_ids:
                replacement_by_label_id[label_id] = entry.id
            pending_label_ids = []
            if isinstance(entry, CompactionEntry):
                kept = replacement_by_label_id.get(
                    entry.first_kept_entry_id, entry.first_kept_entry_id
                )
                path_without_labels.append(
                    replace(entry, parent_id=path_parent_id, first_kept_entry_id=kept)
                )
            else:
                path_without_labels.append(_rechain_entry(entry, path_parent_id))
            path_parent_id = entry.id

        new_session_id = create_session_id()
        timestamp = now_iso()
        new_session_file = os.path.join(
            self._session_dir,
            f"{session_file_timestamp(timestamp)}_{new_session_id}{SESSION_FILE_SUFFIX}",
        )
        header = SessionHeader(
            id=new_session_id,
            timestamp=timestamp,
            cwd=self._cwd,
            # An unpersisted manager has no file to point at, so the key is omitted.
            parent_session=previous_session_file if self._persist_enabled else None,
            version=CURRENT_SESSION_VERSION,
        )

        path_entry_ids = {entry.id for entry in path_without_labels}
        labels_to_write = [
            (target_id, label, self._label_timestamps_by_id.get(target_id, ""))
            for target_id, label in self._labels_by_id.items()
            if target_id in path_entry_ids
        ]

        # Re-created labels hang off the retained path in label-map order; each
        # new id joins the id set so later labels cannot collide with it.
        parent_id = path_without_labels[-1].id if path_without_labels else None
        if parent_id == "":
            parent_id = None
        label_entries: list[LabelEntry] = []
        for target_id, label, label_timestamp in labels_to_write:
            label_entry = LabelEntry(
                id=_generate_id(path_entry_ids),
                parent_id=parent_id,
                timestamp=label_timestamp,
                target_id=target_id,
                label=label,
            )
            path_entry_ids.add(label_entry.id)
            label_entries.append(label_entry)
            parent_id = label_entry.id

        self._file_entries = [header, *path_without_labels, *label_entries]
        self._session_id = new_session_id

        if not self._persist_enabled:
            self._build_index()
            return None

        self._session_file = new_session_file
        self._build_index()

        # Match ``newSession``: an empty (or still user-only) path leaves no file
        # behind.  ``_persist`` then creates it wholesale on the first assistant
        # response, which is why ``_flushed`` must stay false here.
        has_assistant = any(
            isinstance(entry, SessionMessageEntry)
            and getattr(entry.message, "role", None) == "assistant"
            for entry in self._file_entries
        )
        if has_assistant:
            with open(self._session_file, "x", encoding="utf-8", newline="\n") as handle:
                for pending in self._file_entries:
                    handle.write(json_dumps(entry_to_json(pending)) + "\n")
            self._flushed = True
        else:
            self._flushed = False
        return new_session_file

    # -- context -----------------------------------------------------------------------

    def build_context_entries(self) -> list[SessionEntry]:
        return build_context_entries(self.get_entries(), self._leaf_id, self._by_id)

    def build_context(self) -> SessionContext:
        return build_session_context(self.get_entries(), self._leaf_id, self._by_id)

    def get_messages(self) -> list[AgentMessage]:
        """The projected message list handed to the model."""
        return self.build_context().messages


def _entry_to_raw(entry: FileEntry | dict[str, Any]) -> dict[str, Any]:
    if isinstance(entry, dict):
        return dict(entry)
    return entry_to_json(entry)


__all__ = [
    "APP_NAME",
    "CONFIG_DIR_NAME",
    "CURRENT_SESSION_VERSION",
    "ENV_AGENT_DIR",
    "MAX_SESSION_HEADER_SCAN_BYTES",
    "SESSION_FILE_SUFFIX",
    "BranchSummaryEntry",
    "CompactionEntry",
    "CustomEntry",
    "CustomMessageEntry",
    "FileEntry",
    "LabelEntry",
    "ModelChangeEntry",
    "ModelRef",
    "SessionContext",
    "SessionEntry",
    "SessionHeader",
    "SessionInfo",
    "SessionInfoEntry",
    "SessionManager",
    "SessionMessageEntry",
    "SessionTreeNode",
    "ThinkingLevelChangeEntry",
    "UnknownEntry",
    "assert_valid_session_id",
    "build_context_entries",
    "build_session_context",
    "build_session_path",
    "create_session_id",
    "entry_from_json",
    "entry_to_context_messages",
    "entry_to_json",
    "get_agent_dir",
    "get_default_session_dir",
    "get_latest_compaction_entry",
    "get_session_context_settings",
    "header_to_json",
    "json_dumps",
    "load_entries_from_file",
    "migrate_session_entries",
    "now_iso",
    "parse_session_entries",
    "read_session_header",
    "resolve_path",
    "session_dir_name",
    "session_file_timestamp",
]

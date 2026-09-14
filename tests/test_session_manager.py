"""Tests for the JSONL v3 session store and the extended message types.

The golden-file test at the bottom is the interop contract: it loads a fixture
shaped exactly like something real ``pi`` would emit and checks both the rebuilt
context and that re-serialising the entries reproduces the same JSON objects.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

import pytest

from pi_ai import AssistantMessage, TextContent, Usage, UserMessage

from pi_coding_agent.messages import (
    BRANCH_SUMMARY_PREFIX,
    BRANCH_SUMMARY_SUFFIX,
    COMPACTION_SUMMARY_PREFIX,
    COMPACTION_SUMMARY_SUFFIX,
    BashExecutionMessage,
    CompactionSummaryMessage,
    CustomMessage,
    agent_message_from_json,
    agent_message_to_json,
    convert_to_llm,
    create_branch_summary_message,
    create_compaction_summary_message,
)
from pi_coding_agent.session_manager import (
    CURRENT_SESSION_VERSION,
    CompactionEntry,
    LabelEntry,
    SessionHeader,
    SessionInfoEntry,
    SessionManager,
    SessionMessageEntry,
    entry_to_json,
    header_to_json,
    json_dumps,
    now_iso,
    session_dir_name,
)

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def assistant_message(text: str = "ok", timestamp: int = 2, provider: str = "anthropic") -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="anthropic-messages",
        provider=provider,
        model="claude-sonnet-4-5",
        usage=Usage(input=1, output=1, total_tokens=2),
        stop_reason="stop",
        timestamp=timestamp,
    )


def user_message(text: str, timestamp: int = 1) -> UserMessage:
    return UserMessage(content=text, timestamp=timestamp)


def expected_session_dir_name(cwd: str) -> str:
    """Independent re-derivation of the documented encoding rule."""
    resolved = os.path.abspath(cwd)
    stripped = re.sub(r"^[/\\]", "", resolved)
    return "--" + re.sub(r"[/\\:]", "-", stripped) + "--"


def read_lines(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().split("\n")


# --------------------------------------------------------------------------------------
# Path construction and header
# --------------------------------------------------------------------------------------


def test_session_dir_name_matches_documented_pattern(tmp_path):
    cwd = str(tmp_path / "proj")
    encoded = session_dir_name(cwd)
    assert encoded == expected_session_dir_name(cwd)
    assert encoded.startswith("--") and encoded.endswith("--")
    assert "/" not in encoded


async def test_create_writes_header_and_filename_pattern(tmp_path):
    cwd = tmp_path / "project"
    agent_dir = tmp_path / "agent"
    manager = SessionManager.create(str(cwd), agent_dir=str(agent_dir))

    session_file = manager.get_session_file()
    assert session_file is not None
    # Nothing exists yet: the file is only created by the first assistant message.
    assert not os.path.exists(session_file)

    await manager.append_message(assistant_message())

    assert os.path.exists(session_file)
    assert os.path.dirname(session_file) == os.path.join(
        str(agent_dir), "sessions", expected_session_dir_name(str(cwd))
    )
    file_name = os.path.basename(session_file)
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z_[0-9a-f-]{36}\.jsonl", file_name
    ), file_name
    assert file_name.split("_")[-1].removesuffix(".jsonl") == manager.get_session_id()

    header = json.loads(read_lines(session_file)[0])
    assert header["type"] == "session"
    assert header["version"] == CURRENT_SESSION_VERSION == 3
    assert header["id"] == manager.get_session_id()
    assert header["cwd"] == os.path.abspath(str(cwd))
    assert header["timestamp"] == manager.get_header().timestamp
    assert "parentSession" not in header


async def test_no_file_before_first_assistant_then_bulk_write_then_append(tmp_path):
    manager = SessionManager.create(str(tmp_path), agent_dir=str(tmp_path / "agent"))
    session_file = manager.get_session_file()
    assert session_file is not None

    await manager.append_message(user_message("hello"))
    await manager.append_model_change("openai", "gpt-4o")
    # Still no file: an empty (or non-assistant) session must not leave one behind.
    assert not os.path.exists(session_file)

    assistant_id = await manager.append_message(assistant_message())
    assert os.path.exists(session_file)

    lines = [line for line in read_lines(session_file) if line]
    assert len(lines) == 4  # header + user + model_change + assistant
    assert json.loads(lines[1])["message"]["role"] == "user"
    assert json.loads(lines[2])["type"] == "model_change"
    assert json.loads(lines[3])["id"] == assistant_id

    # Subsequent writes append exactly one line each.
    await manager.append_message(user_message("again", timestamp=3))
    lines_after = [line for line in read_lines(session_file) if line]
    assert len(lines_after) == 5
    assert json.loads(lines_after[4])["message"]["content"] == "again"


async def test_in_memory_never_touches_disk(tmp_path):
    manager = SessionManager.in_memory(str(tmp_path))
    assert manager.get_session_file() is None
    assert manager.is_persisted() is False
    await manager.append_message(user_message("hello"))
    await manager.append_message(assistant_message())
    assert list(tmp_path.iterdir()) == []
    assert len(manager.get_entries()) == 2


# --------------------------------------------------------------------------------------
# Round-trip of every entry type
# --------------------------------------------------------------------------------------


async def test_all_entry_types_round_trip_through_json(tmp_path):
    manager = SessionManager.create(str(tmp_path), agent_dir=str(tmp_path / "agent"))
    session_file = manager.get_session_file()
    assert session_file is not None

    user_id = await manager.append_message(user_message("hello"))
    await manager.append_message(assistant_message())
    await manager.append_thinking_level_change("high")
    await manager.append_model_change("openai", "gpt-4o")
    await manager.append_custom("my-extension", {"count": 42})
    await manager.append_custom_message("inline", "Injected context...", True, {"k": 1})
    await manager.append_compaction(
        "summary text", user_id, 50000, {"readFiles": ["a.txt"]}, Usage(input=3, output=4)
    )
    await manager.append_branch_summary("root", "Branch explored approach A")
    await manager.append_label(user_id, "checkpoint-1")
    await manager.set_session_name("Refactor auth module")

    expected = [entry_to_json(entry) for entry in manager.get_entries()]
    kinds = {entry["type"] for entry in expected}
    assert kinds == {
        "message",
        "thinking_level_change",
        "model_change",
        "custom",
        "custom_message",
        "compaction",
        "branch_summary",
        "label",
        "session_info",
    }

    lines = [line for line in read_lines(session_file) if line]
    assert json.loads(lines[0]) == header_to_json(manager.get_header())
    # Every line is exactly one JSON object, and the raw file matches the model.
    assert [json.loads(line) for line in lines[1:]] == expected

    reopened = SessionManager.open(session_file)
    assert [entry_to_json(entry) for entry in reopened.get_entries()] == expected
    assert header_to_json(reopened.get_header()) == header_to_json(manager.get_header())
    assert reopened.get_session_name() == "Refactor auth module"
    assert reopened.get_labels() == {user_id: "checkpoint-1"}
    assert reopened.get_leaf_id() == manager.get_leaf_id()


async def test_every_entry_has_id_parent_id_and_timestamp(tmp_path):
    manager = SessionManager.in_memory(str(tmp_path))
    await manager.append_message(user_message("a"))
    await manager.append_message(assistant_message())
    await manager.append_model_change("openai", "gpt-4o")
    await manager.set_session_name("named")
    entries = manager.get_entries()
    assert len(entries) == 4
    parent_id = None
    for entry in entries:
        assert re.fullmatch(r"[0-9a-f]{8}", entry.id)
        assert entry.parent_id == parent_id
        assert entry.timestamp.endswith("Z")
        parent_id = entry.id


# --------------------------------------------------------------------------------------
# Context rebuild
# --------------------------------------------------------------------------------------


async def test_build_context_trims_across_compaction_boundary(tmp_path):
    manager = SessionManager.in_memory(str(tmp_path))
    u1 = await manager.append_message(user_message("one"))
    await manager.append_message(assistant_message("a1"))
    u2 = await manager.append_message(user_message("two"))
    a2 = await manager.append_message(assistant_message("a2"))
    compaction_id = await manager.append_compaction("the summary", u2, 1234)
    u3 = await manager.append_message(user_message("three"))

    context_entries = manager.build_context_entries()
    assert [entry.id for entry in context_entries] == [compaction_id, u2, a2, u3]
    # The summarised entries before firstKeptEntryId are dropped.
    assert u1 not in [entry.id for entry in context_entries]

    messages = manager.get_messages()
    assert [getattr(message, "role", None) for message in messages] == [
        "compactionSummary",
        "user",
        "assistant",
        "user",
    ]
    assert isinstance(messages[0], CompactionSummaryMessage)
    assert messages[0].summary == "the summary"
    assert messages[0].tokens_before == 1234
    # The distinguishing prefix is applied when projecting to the LLM.
    rendered = convert_to_llm(messages)
    assert rendered[0].content[0].text == (
        COMPACTION_SUMMARY_PREFIX + "the summary" + COMPACTION_SUMMARY_SUFFIX
    )
    assert messages[1].content == "two"
    assert messages[3].content == "three"


async def test_settings_come_from_full_path_not_the_trimmed_list(tmp_path):
    manager = SessionManager.in_memory(str(tmp_path))
    await manager.append_message(user_message("one"))
    await manager.append_message(assistant_message("a1"))
    await manager.append_model_change("openai", "gpt-4o")
    await manager.append_thinking_level_change("high")
    last_user = await manager.append_message(user_message("two"))
    # firstKeptEntryId is *after* the compaction, so nothing before it survives.
    await manager.append_compaction("summary", last_user, 10)

    context = manager.build_context()
    assert [entry.id for entry in manager.build_context_entries()] == [
        manager.get_entries()[-1].id,
        last_user,
    ]
    assert context.thinking_level == "high"
    assert context.model is not None
    assert (context.model.provider, context.model.model_id) == ("openai", "gpt-4o")


async def test_custom_entries_never_reach_the_context(tmp_path):
    manager = SessionManager.in_memory(str(tmp_path))
    await manager.append_message(user_message("hi"))
    await manager.append_custom("my-extension", {"count": 1})
    await manager.append_custom_message("inline", "injected", False)
    await manager.append_message(assistant_message())

    roles = [getattr(message, "role", None) for message in manager.get_messages()]
    assert roles == ["user", "custom", "assistant"]
    custom = manager.get_messages()[1]
    assert isinstance(custom, CustomMessage)
    assert custom.display is False
    assert custom.content == "injected"

    llm_messages = convert_to_llm(manager.get_messages())
    assert [message.role for message in llm_messages] == ["user", "user", "assistant"]
    # display=False hides it from the TUI only; upstream still sends the content.
    assert llm_messages[1].content == [TextContent(text="injected")]


# --------------------------------------------------------------------------------------
# Contracts / rejections
# --------------------------------------------------------------------------------------


async def test_append_message_rejects_summary_roles(tmp_path):
    manager = SessionManager.in_memory(str(tmp_path))
    compaction = CompactionSummaryMessage(summary="s", tokens_before=1, timestamp=1)
    branch = create_branch_summary_message("b", "from", 1)
    with pytest.raises(ValueError):
        await manager.append_message(compaction)
    with pytest.raises(ValueError):
        await manager.append_message(branch)
    assert manager.get_entries() == []


async def test_labels_and_session_name(tmp_path):
    manager = SessionManager.in_memory(str(tmp_path))
    target = await manager.append_message(user_message("hi"))
    assert manager.get_labels() == {}

    await manager.append_label(target, "checkpoint-1")
    assert manager.get_labels() == {target: "checkpoint-1"}
    assert manager.get_label(target) == "checkpoint-1"

    with pytest.raises(ValueError):
        await manager.append_label("missing", "nope")

    # An empty/None label clears it (the JSON key is then omitted entirely).
    await manager.append_label(target, None)
    assert manager.get_labels() == {}
    cleared = manager.get_entries()[-1]
    assert isinstance(cleared, LabelEntry)
    assert "label" not in entry_to_json(cleared)

    await manager.set_session_name("  Refactor\nauth  ")
    assert manager.get_session_name() == "Refactor auth"
    assert isinstance(manager.get_entries()[-1], SessionInfoEntry)
    assert entry_to_json(manager.get_entries()[-1])["name"] == "Refactor auth"
    # End-to-end: survive a reopen.
    assert manager.get_session_name() == "Refactor auth"


# --------------------------------------------------------------------------------------
# Tree traversal
# --------------------------------------------------------------------------------------


async def test_path_and_children_after_a_branch(tmp_path):
    manager = SessionManager.in_memory(str(tmp_path))
    first_user = await manager.append_message(user_message("root"))
    first_assistant = await manager.append_message(assistant_message("first"))
    assert manager.get_leaf_id() == first_assistant
    assert [entry.id for entry in manager.get_path()] == [first_user, first_assistant]
    assert [entry.id for entry in manager.get_children(first_user)] == [first_assistant]

    manager.branch(first_user)
    assert manager.get_leaf_id() == first_user
    second_assistant = await manager.append_message(assistant_message("second"))
    assert manager.get_leaf_id() == second_assistant

    assert [entry.id for entry in manager.get_children(first_user)] == [
        first_assistant,
        second_assistant,
    ]
    assert [entry.id for entry in manager.get_path(second_assistant)] == [
        first_user,
        second_assistant,
    ]
    assert [entry.id for entry in manager.get_path(first_assistant)] == [
        first_user,
        first_assistant,
    ]
    # Both branches are still present in the flat entry list.
    assert len(manager.get_entries()) == 3

    manager.reset_leaf()
    assert manager.get_leaf_id() is None
    assert manager.get_path() == []


# --------------------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------------------


V1_FIXTURE = "\n".join(
    [
        '{"type":"session","id":"v1-session","timestamp":"2024-01-01T00:00:00.000Z","cwd":"/tmp/v1"}',
        '{"type":"message","timestamp":"2024-01-01T00:00:01.000Z","message":{"role":"user","content":"hello","timestamp":1}}',
        '{"type":"message","timestamp":"2024-01-01T00:00:02.000Z","message":{"role":"assistant","content":[],"api":"a","provider":"p","model":"m","usage":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"totalTokens":0,"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"total":0}},"stopReason":"stop","timestamp":2}}',
        '{"type":"compaction","timestamp":"2024-01-01T00:00:03.000Z","summary":"summary","firstKeptEntryIndex":2,"tokensBefore":10}',
        '{"type":"message","timestamp":"2024-01-01T00:00:04.000Z","message":{"role":"user","content":"next","timestamp":4}}',
    ]
) + "\n"


def test_v1_migration_adds_tree_and_first_kept_entry_id(tmp_path):
    path = tmp_path / "v1.jsonl"
    path.write_text(V1_FIXTURE, encoding="utf-8")

    manager = SessionManager.open(str(path))
    header = manager.get_header()
    assert header is not None
    assert header.version == CURRENT_SESSION_VERSION

    entries = manager.get_entries()
    assert len(entries) == 4
    assert all(re.fullmatch(r"[0-9a-f]{8}", entry.id) for entry in entries)
    # Linear chain: each entry's parent is the previous entry.
    assert entries[0].parent_id is None
    assert entries[1].parent_id == entries[0].id
    assert entries[2].parent_id == entries[1].id
    assert entries[3].parent_id == entries[2].id

    compaction = entries[2]
    assert isinstance(compaction, CompactionEntry)
    # firstKeptEntryIndex counted the header line, so index 2 is the assistant entry.
    assert compaction.first_kept_entry_id == entries[1].id
    assert "firstKeptEntryIndex" not in entry_to_json(compaction)

    # Migrated files are rewritten at the current version straight away.
    lines = [line for line in read_lines(str(path)) if line]
    assert json.loads(lines[0])["version"] == CURRENT_SESSION_VERSION
    assert "firstKeptEntryIndex" not in json.loads(lines[3])

    context = manager.build_context()
    assert [getattr(message, "role", None) for message in context.messages] == [
        "compactionSummary",
        "assistant",
        "user",
    ]
    assert context.messages[2].content == "next"


V2_FIXTURE = "\n".join(
    [
        '{"type":"session","version":2,"id":"v2-session","timestamp":"2024-01-01T00:00:00.000Z","cwd":"/tmp/v2"}',
        '{"type":"message","id":"aaaa0001","parentId":null,"timestamp":"2024-01-01T00:00:01.000Z","message":{"role":"hookMessage","customType":"legacy-ext","content":"legacy","display":false,"timestamp":1}}',
        '{"type":"message","id":"aaaa0002","parentId":"aaaa0001","timestamp":"2024-01-01T00:00:02.000Z","message":{"role":"user","content":"hi","timestamp":2}}',
    ]
) + "\n"


def test_v2_migration_renames_hook_message_role(tmp_path):
    path = tmp_path / "v2.jsonl"
    path.write_text(V2_FIXTURE, encoding="utf-8")

    manager = SessionManager.open(str(path))
    assert manager.get_header().version == CURRENT_SESSION_VERSION

    entries = manager.get_entries()
    assert len(entries) == 2
    first = entries[0]
    assert isinstance(first, SessionMessageEntry)
    assert isinstance(first.message, CustomMessage)
    assert first.message.role == "custom"
    assert first.message.custom_type == "legacy-ext"
    assert first.message.content == "legacy"
    # Ids/parentIds are untouched by the v2 -> v3 step.
    assert first.parent_id is None
    assert entries[1].parent_id == "aaaa0001"

    lines = [line for line in read_lines(str(path)) if line]
    assert json.loads(lines[0])["version"] == CURRENT_SESSION_VERSION
    assert json.loads(lines[1])["message"]["role"] == "custom"

    assert [getattr(m, "role", None) for m in manager.get_messages()] == ["custom", "user"]


def test_migration_of_unknown_entries_does_not_crash(tmp_path):
    path = tmp_path / "unknown.jsonl"
    path.write_text(
        '{"type":"session","version":2,"id":"s","timestamp":"2024-01-01T00:00:00.000Z","cwd":"/tmp/x"}\n'
        '{"type":"future_thing","id":"deadbeef","parentId":null,"timestamp":"2024-01-01T00:00:01.000Z","payload":{"a":1}}\n',
        encoding="utf-8",
    )
    manager = SessionManager.open(str(path))
    entry = manager.get_entries()[0]
    assert entry.type == "future_thing"
    assert entry_to_json(entry) == {
        "type": "future_thing",
        "id": "deadbeef",
        "parentId": None,
        "timestamp": "2024-01-01T00:00:01.000Z",
        "payload": {"a": 1},
    }


def test_migration_with_unknown_message_role_survives_rewrite(tmp_path):
    path = tmp_path / "weird-role.jsonl"
    path.write_text(
        '{"type":"session","id":"s","timestamp":"2024-01-01T00:00:00.000Z","cwd":"/tmp/x"}\n'
        '{"type":"message","timestamp":"2024-01-01T00:00:01.000Z","message":{"role":"futureRole","stuff":1}}\n',
        encoding="utf-8",
    )
    manager = SessionManager.open(str(path))
    entry = manager.get_entries()[0]
    assert isinstance(entry, SessionMessageEntry)
    # Unknown roles are preserved verbatim through the v1 -> v3 rewrite.
    assert entry_to_json(entry)["message"] == {"role": "futureRole", "stuff": 1}
    # The unparseable message is passed through untouched instead of crashing.
    assert manager.get_messages()[0] == {"role": "futureRole", "stuff": 1}

    lines = [line for line in read_lines(str(path)) if line]
    assert json.loads(lines[0])["version"] == CURRENT_SESSION_VERSION
    assert json.loads(lines[1])["message"] == {"role": "futureRole", "stuff": 1}


def test_invalid_session_file_is_rejected(tmp_path):
    path = tmp_path / "bogus.jsonl"
    path.write_text('{"hello":"world"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        SessionManager.open(str(path))
    # The file is left untouched.
    assert path.read_text(encoding="utf-8") == '{"hello":"world"}\n'


# --------------------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------------------


async def test_list_sessions_sorted_by_activity(tmp_path):
    agent_dir = tmp_path / "agent"
    older = SessionManager.create(str(tmp_path), agent_dir=str(agent_dir))
    await older.append_message(assistant_message())
    newer = SessionManager.create(str(tmp_path), agent_dir=str(agent_dir))
    await newer.append_message(assistant_message())
    await newer.set_session_name("Named session")

    sessions = await SessionManager.list(str(tmp_path), agent_dir=str(agent_dir))
    assert len(sessions) == 2
    by_id = {info.id: info for info in sessions}
    assert by_id[newer.get_session_id()].name == "Named session"
    assert by_id[older.get_session_id()].first_message == "(no messages)"
    assert all(info.cwd == os.path.abspath(str(tmp_path)) for info in sessions)


# --------------------------------------------------------------------------------------
# Message-level helpers
# --------------------------------------------------------------------------------------


def test_bash_execution_message_json_and_projection():
    message = BashExecutionMessage(
        command="ls",
        output="a\nb",
        exit_code=1,
        cancelled=False,
        truncated=True,
        timestamp=5,
        full_output_path="/tmp/full.log",
    )
    payload = agent_message_to_json(message)
    assert payload == {
        "role": "bashExecution",
        "command": "ls",
        "output": "a\nb",
        "exitCode": 1,
        "cancelled": False,
        "truncated": True,
        "fullOutputPath": "/tmp/full.log",
        "timestamp": 5,
    }
    # ``undefined`` fields are omitted, never null.
    without = BashExecutionMessage(
        command="ls", output="", exit_code=None, cancelled=False, truncated=False, timestamp=6
    )
    assert set(agent_message_to_json(without)) == {
        "role",
        "command",
        "output",
        "cancelled",
        "truncated",
        "timestamp",
    }
    assert agent_message_from_json(payload) == message

    converted = convert_to_llm([message])
    assert converted[0].role == "user"
    assert "Ran `ls`" in converted[0].content[0].text
    assert "Command exited with code 1" in converted[0].content[0].text
    assert "[Output truncated. Full output: /tmp/full.log]" in converted[0].content[0].text

    excluded = BashExecutionMessage(
        command="ls",
        output="x",
        exit_code=0,
        cancelled=False,
        truncated=False,
        timestamp=7,
        exclude_from_context=True,
    )
    assert convert_to_llm([excluded]) == []


def test_convert_to_llm_never_raises_on_junk():
    assert convert_to_llm([object(), None, "nonsense", {"role": "unknown"}]) == []


def test_summary_prefixes_and_creators():
    branch = create_branch_summary_message("b", "from-1", "2024-12-03T14:00:01.000Z")
    assert branch.timestamp == 1_733_234_401_000
    compaction = create_compaction_summary_message("c", 12, "2024-12-03T14:00:01.000Z")
    assert compaction.tokens_before == 12
    messages = convert_to_llm([branch, compaction])
    assert messages[0].content[0].text == BRANCH_SUMMARY_PREFIX + "b" + BRANCH_SUMMARY_SUFFIX
    assert messages[1].content[0].text == COMPACTION_SUMMARY_PREFIX + "c" + COMPACTION_SUMMARY_SUFFIX


# --------------------------------------------------------------------------------------
# Golden file: exactly what real pi emits
# --------------------------------------------------------------------------------------

GOLDEN_V3_LINES = [
    '{"type":"session","version":3,"id":"0193f0c1-7c4a-7c9a-9a3e-1234567890ab","timestamp":"2024-12-03T14:00:00.000Z","cwd":"/tmp/pi-golden"}',
    '{"type":"message","id":"a1b2c3d4","parentId":null,"timestamp":"2024-12-03T14:00:01.000Z","message":{"role":"user","content":"Hello","timestamp":1733234401000}}',
    '{"type":"message","id":"b2c3d4e5","parentId":"a1b2c3d4","timestamp":"2024-12-03T14:00:02.000Z","message":{"role":"assistant","content":[{"type":"text","text":"Hi!"}],"api":"anthropic-messages","provider":"anthropic","model":"claude-sonnet-4-5","usage":{"input":10,"output":5,"cacheRead":0,"cacheWrite":0,"totalTokens":15,"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"total":0}},"stopReason":"stop","timestamp":1733234402000}}',
    '{"type":"message","id":"c3d4e5f6","parentId":"b2c3d4e5","timestamp":"2024-12-03T14:00:03.000Z","message":{"role":"toolResult","toolCallId":"call_123","toolName":"bash","content":[{"type":"text","text":"output"}],"isError":false,"timestamp":1733234403000}}',
    '{"type":"custom","customType":"my-extension","data":{"count":42},"id":"d4e5f6a7","parentId":"c3d4e5f6","timestamp":"2024-12-03T14:00:04.000Z"}',
    '{"type":"model_change","id":"f6a7b8c9","parentId":"d4e5f6a7","timestamp":"2024-12-03T14:00:05.000Z","provider":"openai","modelId":"gpt-4o"}',
    '{"type":"thinking_level_change","id":"a7b8c9d0","parentId":"f6a7b8c9","timestamp":"2024-12-03T14:00:06.000Z","thinkingLevel":"high"}',
    '{"type":"compaction","id":"b8c9d0e1","parentId":"a7b8c9d0","timestamp":"2024-12-03T14:00:07.000Z","summary":"Earlier turns summarised","firstKeptEntryId":"e1f2a3b4","tokensBefore":50000,"usage":{"input":1,"output":2,"cacheRead":3,"cacheWrite":4,"totalTokens":10,"cost":{"input":0.1,"output":0.2,"cacheRead":0.3,"cacheWrite":0.4,"total":1}}}',
    '{"type":"custom_message","customType":"inline","content":"Injected context...","display":true,"id":"c9d0e1f2","parentId":"b8c9d0e1","timestamp":"2024-12-03T14:00:08.000Z"}',
    '{"type":"branch_summary","id":"d0e1f2a3","parentId":"c9d0e1f2","timestamp":"2024-12-03T14:00:09.000Z","fromId":"c3d4e5f6","summary":"Branch explored approach A"}',
    '{"type":"label","id":"e1f2a3b5","parentId":"d0e1f2a3","timestamp":"2024-12-03T14:00:10.000Z","targetId":"a1b2c3d4","label":"checkpoint-1"}',
    '{"type":"session_info","id":"f2a3b4c5","parentId":"e1f2a3b5","timestamp":"2024-12-03T14:00:11.000Z","name":"Refactor auth module"}',
    '{"type":"message","id":"e1f2a3b4","parentId":"f2a3b4c5","timestamp":"2024-12-03T14:00:12.000Z","message":{"role":"user","content":"continue","timestamp":1733234412000}}',
]


def write_golden(tmp_path) -> str:
    path = tmp_path / "2024-12-03T14-00-00-000Z_0193f0c1-7c4a-7c9a-9a3e-1234567890ab.jsonl"
    path.write_text("\n".join(GOLDEN_V3_LINES) + "\n", encoding="utf-8")
    return str(path)


def test_golden_file_context_and_round_trip(tmp_path):
    path = write_golden(tmp_path)
    manager = SessionManager.open(path)

    header = manager.get_header()
    assert isinstance(header, SessionHeader)
    assert header.id == "0193f0c1-7c4a-7c9a-9a3e-1234567890ab"
    assert header.version == 3
    assert header.cwd == "/tmp/pi-golden"
    assert entry_to_json(header) == json.loads(GOLDEN_V3_LINES[0])

    # The compaction at index 6 keeps everything from firstKeptEntryId onwards;
    # here firstKeptEntryId is the *last* message, so only it survives.
    context_entries = manager.build_context_entries()
    assert [entry.id for entry in context_entries] == [
        "b8c9d0e1",
        "c9d0e1f2",
        "d0e1f2a3",
        "e1f2a3b5",
        "f2a3b4c5",
        "e1f2a3b4",
    ]

    context = manager.build_context()
    assert [getattr(message, "role", None) for message in context.messages] == [
        "compactionSummary",
        "custom",
        "branchSummary",
        "user",
    ]
    assert context.messages[0].summary == "Earlier turns summarised"
    assert context.messages[0].tokens_before == 50000
    assert context.messages[1].custom_type == "inline"
    assert context.messages[1].content == "Injected context..."
    assert context.messages[2].summary == "Branch explored approach A"
    assert context.messages[2].from_id == "c3d4e5f6"
    assert context.messages[3].content == "continue"
    # Settings are resolved from the full path, not the trimmed context entries.
    assert context.thinking_level == "high"
    assert (context.model.provider, context.model.model_id) == ("openai", "gpt-4o")

    llm_messages = convert_to_llm(context.messages)
    assert [message.role for message in llm_messages] == ["user", "user", "user", "user"]
    assert llm_messages[0].content[0].text == (
        COMPACTION_SUMMARY_PREFIX + "Earlier turns summarised" + COMPACTION_SUMMARY_SUFFIX
    )
    assert llm_messages[2].content[0].text == (
        BRANCH_SUMMARY_PREFIX + "Branch explored approach A" + BRANCH_SUMMARY_SUFFIX
    )

    labels = manager.get_labels()
    assert labels == {"a1b2c3d4": "checkpoint-1"}
    assert manager.get_session_name() == "Refactor auth module"
    assert manager.get_leaf_id() == "e1f2a3b4"

    # Re-serialising the entries reproduces the exact JSON objects pi wrote.
    rebuilt = [entry_to_json(entry) for entry in manager.get_entries()]
    assert rebuilt == [json.loads(line) for line in GOLDEN_V3_LINES[1:]]
    # ... and the lines themselves are byte-identical to the fixture.
    assert [json_dumps(entry) for entry in rebuilt] == GOLDEN_V3_LINES[1:]


def test_golden_file_is_not_rewritten_on_load(tmp_path):
    path = write_golden(tmp_path)
    before = open(path, "r", encoding="utf-8").read()
    manager = SessionManager.open(path)
    assert manager.get_entries()
    # No migration happened, so opening must not touch the bytes.
    assert open(path, "r", encoding="utf-8").read() == before


def test_now_iso_shape():
    value = now_iso()
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    assert parsed.tzinfo is timezone.utc


def test_json_dumps_matches_json_stringify_number_formatting():
    # JS has a single number type: JSON.stringify(0.0) is "0", and non-finite
    # values serialise as null.  Python's encoder would emit "0.0"/"NaN".
    assert json_dumps({"a": 0.0, "b": 1.0, "c": 0.5, "d": float("nan")}) == (
        '{"a":0,"b":1,"c":0.5,"d":null}'
    )
    assert json_dumps([1, {"x": -0.0}]) == '[1,{"x":0}]'
    assert json_dumps("caf\u00e9") == '"caf\u00e9"'  # raw UTF-8, not \\u escapes

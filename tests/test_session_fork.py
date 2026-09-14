"""Tests for session extraction: ``create_branched_session`` and ``fork_from``.

Both primitives write a *new* JSONL file whose entries must stay loadable by real
``pi``.  The strongest assertions here are therefore structural (the exact
retained path, re-chained ``parentId`` links, byte-identical lines) rather than
merely "a file appeared".
"""

from __future__ import annotations

import json
import os
import re

import pytest

from pi_ai import AssistantMessage, TextContent, ToolResultMessage, Usage, UserMessage

from pi_coding_agent import session_manager as session_manager_module
from pi_coding_agent.session_manager import (
    CURRENT_SESSION_VERSION,
    SessionManager,
    build_session_context,
    entry_to_json,
    get_default_session_dir,
    json_dumps,
    load_entries_from_file,
    session_dir_name,
)

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def assistant_message(text: str = "ok", timestamp: int = 2) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="anthropic-messages",
        provider="anthropic",
        model="claude-sonnet-4-5",
        usage=Usage(input=1, output=1, total_tokens=2),
        stop_reason="stop",
        timestamp=timestamp,
    )


def user_message(text: str, timestamp: int = 1) -> UserMessage:
    return UserMessage(content=text, timestamp=timestamp)


def tool_result_message(timestamp: int = 3) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id="call_1",
        tool_name="bash",
        content=[TextContent(text="output")],
        is_error=False,
        timestamp=timestamp,
    )


def read_lines(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as handle:
        return [line for line in handle.read().split("\n") if line]


def make_manager(tmp_path, name: str = "project") -> SessionManager:
    return SessionManager.create(str(tmp_path / name), agent_dir=str(tmp_path / "agent"))


# --------------------------------------------------------------------------------------
# create_branched_session: plain path extraction
# --------------------------------------------------------------------------------------


async def test_create_branched_session_from_non_tip_leaf(tmp_path):
    manager = make_manager(tmp_path)
    old_file = manager.get_session_file()
    assert old_file is not None

    root = await manager.append_message(user_message("root"))
    first = await manager.append_message(assistant_message("first"))
    # A side branch that must NOT appear in the extracted file.
    manager.branch(root)
    side = await manager.append_message(assistant_message("side"))
    assert len(manager.get_entries()) == 3

    new_file = manager.create_branched_session(first)
    assert new_file is not None
    assert new_file != old_file
    assert manager.get_session_file() == new_file
    assert manager.get_session_id() != side  # a genuinely new session id

    lines = read_lines(new_file)
    header = json.loads(lines[0])
    assert header["type"] == "session"
    assert header["version"] == CURRENT_SESSION_VERSION
    assert header["id"] == manager.get_session_id()
    assert header["cwd"] == os.path.abspath(str(tmp_path / "project"))
    assert header["parentSession"] == old_file
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z_[0-9a-f-]{36}\.jsonl",
        os.path.basename(new_file),
    )

    entries = [json.loads(line) for line in lines[1:]]
    assert [entry["id"] for entry in entries] == [root, first]
    assert entries[0]["parentId"] is None
    assert entries[1]["parentId"] == root
    assert side not in {entry["id"] for entry in entries}

    # The old file was never touched: header + root + first + side assistant.
    assert len(read_lines(old_file)) == 4


async def test_branched_lines_are_byte_identical_to_entry_json(tmp_path):
    manager = make_manager(tmp_path)
    root = await manager.append_message(user_message("root"))
    first = await manager.append_message(assistant_message("first"))
    await manager.append_message(tool_result_message())
    await manager.append_compaction("summary", root, 10)
    manager.branch(root)
    await manager.append_message(assistant_message("other"))

    new_file = manager.create_branched_session(first)
    assert new_file is not None

    for line, entry in zip(read_lines(new_file), load_entries_from_file(new_file)):
        assert line == json_dumps(entry_to_json(entry))


async def test_append_after_branch_writes_to_the_new_file(tmp_path):
    manager = make_manager(tmp_path)
    old_file = manager.get_session_file()
    assert old_file is not None

    root = await manager.append_message(user_message("root"))
    first = await manager.append_message(assistant_message("first"))
    manager.branch(root)
    await manager.append_message(assistant_message("side"))
    with open(old_file, "r", encoding="utf-8") as handle:
        old_before = handle.read()

    new_file = manager.create_branched_session(first)
    assert new_file is not None
    assert os.path.exists(new_file)

    await manager.append_message(assistant_message("branched"))
    assert len(read_lines(new_file)) == 4  # header + root + first + branched
    with open(old_file, "r", encoding="utf-8") as handle:
        assert handle.read() == old_before
    assert manager.get_session_file() == new_file


async def test_create_branched_session_unknown_leaf_raises(tmp_path):
    manager = make_manager(tmp_path)
    await manager.append_message(user_message("root"))
    await manager.append_message(assistant_message("first"))

    with pytest.raises(ValueError, match=r"Entry nope not found"):
        manager.create_branched_session("nope")
    # An empty id is an unknown leaf too (upstream's ``byId.get("")`` misses).
    with pytest.raises(ValueError, match=r"Entry  not found"):
        manager.create_branched_session("")


async def test_create_branched_session_in_memory_rebinds_in_place(tmp_path):
    manager = SessionManager.in_memory(str(tmp_path))
    root = await manager.append_message(user_message("root"))
    await manager.append_message(assistant_message("first"))
    manager.branch(root)
    await manager.append_message(assistant_message("side"))

    assert manager.create_branched_session(root) is None
    assert manager.get_session_file() is None
    assert [entry.id for entry in manager.get_entries()] == [root]
    assert manager.get_header().parent_session is None
    assert list(tmp_path.iterdir()) == []


async def test_in_memory_branch_translates_a_compaction_boundary_at_a_label(tmp_path):
    """Port of upstream regression #8989: a compaction may cut at a label."""
    manager = SessionManager.in_memory(str(tmp_path))
    old = await manager.append_message(user_message("old"))
    label = await manager.append_label(old, "checkpoint")
    kept = await manager.append_message(user_message("kept"))
    compaction = await manager.append_compaction("summary", label, 100)
    leaf = await manager.append_message(user_message("after"))

    assert manager.create_branched_session(leaf) is None
    assert manager.get_entry(compaction).first_kept_entry_id == kept
    assert [getattr(message, "role", None) for message in manager.build_context().messages] == [
        "compactionSummary",
        "user",
        "user",
    ]


async def test_in_memory_branch_rewires_children_of_removed_labels(tmp_path):
    manager = SessionManager.in_memory(str(tmp_path))
    root = await manager.append_message(user_message("hello"))
    await manager.append_label(root, "checkpoint")
    model = await manager.append_model_change("anthropic", "claude-test")
    leaf = await manager.append_message(user_message("followup"))

    assert manager.create_branched_session(leaf) is None
    assert manager.get_entry(model).parent_id == root
    assert [entry.id for entry in manager.get_entries()][:3] == [root, model, leaf]
    # The surviving label is re-created at the end, chained onto the new tip.
    label_entries = [entry for entry in manager.get_entries() if entry.type == "label"]
    assert [(entry.target_id, entry.parent_id) for entry in label_entries] == [(root, leaf)]


async def test_create_branched_session_defers_file_until_first_assistant(tmp_path):
    """A user-only path leaves no file behind, exactly like ``new_session``."""
    manager = make_manager(tmp_path)
    root = await manager.append_message(user_message("root"))
    await manager.append_message(assistant_message("first"))

    new_file = manager.create_branched_session(root)
    assert new_file is not None
    assert not os.path.exists(new_file)

    await manager.append_message(assistant_message("branched"))
    lines = read_lines(new_file)
    assert len(lines) == 3
    assert json.loads(lines[0])["id"] == manager.get_session_id()
    assert json.loads(lines[1])["id"] == root
    assert json.loads(lines[2])["parentId"] == root


# --------------------------------------------------------------------------------------
# create_branched_session: label re-chaining
# --------------------------------------------------------------------------------------


async def build_labeled_session(manager: SessionManager) -> dict[str, str]:
    """u1,a1,L1,u2,a2,C,u3,a3,L2,u4 plus a side branch carrying a dropped label."""
    ids: dict[str, str] = {}
    ids["u1"] = await manager.append_message(user_message("root"))
    ids["a1"] = await manager.append_message(assistant_message("first"))
    ids["l1"] = await manager.append_label(ids["u1"], "root-label")
    # Appended after the label, so this entry's parentId IS the label entry.
    ids["u2"] = await manager.append_message(user_message("second"))
    ids["a2"] = await manager.append_message(assistant_message("second-reply"))
    # The compaction points *at a label*: that id must be translated on re-chain.
    ids["c"] = await manager.append_compaction(
        "summary text", ids["l1"], 50000, {"readFiles": ["a.txt"]}
    )
    ids["u3"] = await manager.append_message(user_message("third"))
    ids["a3"] = await manager.append_message(assistant_message("third-reply"))
    ids["l2"] = await manager.append_label(ids["a2"], "kept-label")
    ids["u4"] = await manager.append_message(user_message("fourth"))

    # A parallel branch whose label points at an entry that is NOT on the path.
    manager.branch(ids["a1"])
    ids["b1"] = await manager.append_message(assistant_message("side"))
    ids["l3"] = await manager.append_label(ids["b1"], "side-label")
    return ids


async def test_labels_are_dropped_and_the_path_is_rechained(tmp_path):
    manager = make_manager(tmp_path)
    ids = await build_labeled_session(manager)

    new_file = manager.create_branched_session(ids["u4"])
    assert new_file is not None
    entries = [json.loads(line) for line in read_lines(new_file)][1:]
    by_id = {entry["id"]: entry for entry in entries}

    # Exactly the retained path, in path order; the label entries are gone.
    assert [entry["id"] for entry in entries][:8] == [
        ids["u1"], ids["a1"], ids["u2"], ids["a2"], ids["c"], ids["u3"], ids["a3"], ids["u4"]
    ]
    assert ids["l1"] not in by_id and ids["l2"] not in by_id and ids["l3"] not in by_id
    assert ids["b1"] not in by_id

    # Re-chained: each entry is a child of the previous retained entry.
    assert [entry["parentId"] for entry in entries][:8] == [
        None, ids["u1"], ids["a1"], ids["u2"], ids["a2"], ids["c"], ids["u3"], ids["a3"]
    ]
    # u2 used to be a child of L1 and now hangs directly off a1.
    assert by_id[ids["u2"]]["parentId"] == ids["a1"]
    # The compaction's firstKeptEntryId is translated from the label to u2.
    assert by_id[ids["c"]]["firstKeptEntryId"] == ids["u2"]


async def test_surviving_labels_are_recreated_and_dropped_ones_are_not(tmp_path):
    manager = make_manager(tmp_path)
    ids = await build_labeled_session(manager)
    assert manager.get_labels() == {
        ids["u1"]: "root-label",
        ids["a2"]: "kept-label",
        ids["b1"]: "side-label",
    }
    original_label_entries = {
        entry.id: entry for entry in manager.get_entries() if entry.type == "label"
    }

    new_file = manager.create_branched_session(ids["u4"])
    assert new_file is not None

    branched = SessionManager.open(new_file)
    # The side label's target was dropped with its branch, so it is gone.
    assert branched.get_labels() == {ids["u1"]: "root-label", ids["a2"]: "kept-label"}

    label_entries = [entry for entry in branched.get_entries() if entry.type == "label"]
    assert [entry.target_id for entry in label_entries] == [ids["u1"], ids["a2"]]
    assert [entry.label for entry in label_entries] == ["root-label", "kept-label"]
    # Timestamps are preserved from the original label entries.
    assert [entry.timestamp for entry in label_entries] == [
        original_label_entries[ids["l1"]].timestamp,
        original_label_entries[ids["l2"]].timestamp,
    ]
    # Fresh ids, chained after the retained path and onto each other.
    assert all(entry.id not in original_label_entries for entry in label_entries)
    assert label_entries[0].parent_id == ids["u4"]
    assert label_entries[1].parent_id == label_entries[0].id


async def test_branched_context_equals_original_for_the_same_leaf(tmp_path):
    manager = make_manager(tmp_path)
    await manager.append_message(user_message("hello"))
    await manager.append_message(assistant_message("hi"))
    tool = await manager.append_message(tool_result_message())
    await manager.append_message(user_message("and now"))
    await manager.append_message(assistant_message("done"))
    await manager.append_compaction("condensed", tool, 1234)
    leaf = await manager.append_message(assistant_message("after compaction"))
    tail = await manager.append_message(user_message("tail"))

    expected = build_session_context(manager.get_entries(), leaf)
    assert [getattr(message, "role", None) for message in expected.messages] == [
        "compactionSummary",
        "toolResult",
        "user",
        "assistant",
        "assistant",
    ]

    new_file = manager.create_branched_session(leaf)
    assert new_file is not None
    branched = SessionManager.open(new_file)
    assert branched.build_context() == expected
    assert tail not in {entry.id for entry in branched.get_entries()}


async def test_unmapped_compaction_boundary_keeps_its_original_value(tmp_path):
    """A ``firstKeptEntryId`` with no label mapping is copied through unchanged."""
    manager = make_manager(tmp_path)
    await manager.append_message(user_message("root"))
    await manager.append_message(assistant_message("first"))
    compaction = await manager.append_compaction("summary", "deadbeef", 1)
    leaf = await manager.append_message(assistant_message("second"))

    new_file = manager.create_branched_session(leaf)
    assert new_file is not None
    entries = {entry["id"]: entry for entry in map(json.loads, read_lines(new_file)[1:])}
    assert entries[compaction]["firstKeptEntryId"] == "deadbeef"


async def test_branched_context_equals_original_with_labels_on_the_path(tmp_path):
    manager = make_manager(tmp_path)
    ids = await build_labeled_session(manager)
    expected = build_session_context(manager.get_entries(), ids["u3"])

    new_file = manager.create_branched_session(ids["u3"])
    assert new_file is not None
    branched = SessionManager.open(new_file)
    assert branched.build_context() == expected
    assert [getattr(message, "role", None) for message in expected.messages] == [
        "compactionSummary",
        "user",
        "assistant",
        "user",
    ]


# --------------------------------------------------------------------------------------
# fork_from
# --------------------------------------------------------------------------------------


async def test_fork_from_copies_entries_verbatim(tmp_path):
    source = make_manager(tmp_path, "source-project")
    source_file = source.get_session_file()
    assert source_file is not None

    root = await source.append_message(user_message("root"))
    await source.append_message(assistant_message("first"))
    await source.append_message(tool_result_message())
    await source.append_custom("ext", {"n": 1})
    await source.append_label(root, "checkpoint")
    await source.append_compaction("summary", root, 99)
    await source.set_session_name("Source session")

    target_cwd = tmp_path / "target-project"
    fork_dir = tmp_path / "forks"
    forked = SessionManager.fork_from(source_file, str(target_cwd), str(fork_dir))

    new_file = forked.get_session_file()
    assert new_file is not None
    assert os.path.dirname(new_file) == str(fork_dir)
    assert os.path.basename(new_file).endswith(f"_{forked.get_session_id()}.jsonl")
    assert forked.get_cwd() == os.path.abspath(str(target_cwd))

    header = json.loads(read_lines(new_file)[0])
    assert header == {
        "type": "session",
        "version": CURRENT_SESSION_VERSION,
        "id": forked.get_session_id(),
        "timestamp": header["timestamp"],
        "cwd": os.path.abspath(str(target_cwd)),
        "parentSession": os.path.abspath(source_file),
    }
    assert header["id"] != json.loads(read_lines(source_file)[0])["id"]

    # Entries are byte-identical to the source's, ids and parentIds preserved.
    assert read_lines(new_file)[1:] == read_lines(source_file)[1:]
    for line, entry in zip(read_lines(new_file), load_entries_from_file(new_file)):
        assert line == json_dumps(entry_to_json(entry))

    assert forked.build_context() == SessionManager.open(source_file).build_context()
    assert forked.get_labels() == source.get_labels()
    assert forked.get_session_name() == source.get_session_name()
    assert forked.get_header().parent_session == os.path.abspath(source_file)


async def test_fork_from_uses_default_session_dir_for_target_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))
    source = make_manager(tmp_path, "source-project")
    await source.append_message(user_message("root"))
    await source.append_message(assistant_message("first"))
    source_file = source.get_session_file()
    assert source_file is not None

    target_cwd = str(tmp_path / "target-project")
    forked = SessionManager.fork_from(source_file, target_cwd)
    new_file = forked.get_session_file()
    assert new_file is not None
    assert os.path.dirname(new_file) == get_default_session_dir(target_cwd)
    assert os.path.dirname(new_file) == os.path.join(
        str(tmp_path / "agent"), "sessions", session_dir_name(target_cwd)
    )


async def test_fork_from_explicit_session_id_is_validated_and_used(tmp_path):
    source = make_manager(tmp_path)
    await source.append_message(user_message("root"))
    await source.append_message(assistant_message("first"))
    source_file = source.get_session_file()
    assert source_file is not None

    forked = SessionManager.fork_from(
        source_file, str(tmp_path / "target"), str(tmp_path / "forks"), session_id="forked-id-1"
    )
    assert forked.get_session_id() == "forked-id-1"
    assert os.path.basename(forked.get_session_file()).endswith("_forked-id-1.jsonl")

    with pytest.raises(ValueError):
        SessionManager.fork_from(
            source_file, str(tmp_path / "target"), str(tmp_path / "forks"), session_id="bad id!"
        )


async def test_fork_from_missing_or_empty_source_raises(tmp_path):
    target = str(tmp_path / "target")
    missing = str(tmp_path / "missing.jsonl")
    with pytest.raises(ValueError) as excinfo:
        SessionManager.fork_from(missing, target)
    assert str(excinfo.value) == (
        f"Cannot fork: source session file is empty or invalid: {os.path.abspath(missing)}"
    )

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        SessionManager.fork_from(str(empty), target)
    assert str(excinfo.value) == (
        f"Cannot fork: source session file is empty or invalid: {os.path.abspath(str(empty))}"
    )

    garbage = tmp_path / "garbage.jsonl"
    garbage.write_text("not json at all\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"empty or invalid"):
        SessionManager.fork_from(str(garbage), target)


async def test_fork_from_never_clobbers_an_existing_file(tmp_path, monkeypatch):
    """The target is created with ``wx``, so a name collision raises instead."""
    source = make_manager(tmp_path)
    await source.append_message(user_message("root"))
    await source.append_message(assistant_message("first"))
    source_file = source.get_session_file()
    assert source_file is not None

    fork_dir = tmp_path / "forks"
    monkeypatch.setattr(session_manager_module, "now_iso", lambda: "2024-01-01T00:00:00.000Z")
    first = SessionManager.fork_from(
        source_file, str(tmp_path / "target"), str(fork_dir), session_id="dup"
    )
    assert first.get_session_file() == os.path.join(
        str(fork_dir), "2024-01-01T00-00-00-000Z_dup.jsonl"
    )

    with pytest.raises(FileExistsError):
        SessionManager.fork_from(
            source_file, str(tmp_path / "target"), str(fork_dir), session_id="dup"
        )

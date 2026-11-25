"""The command surface is a contract: changing it must be a deliberate edit.

Every RPC/WebSocket client (the browser, an IDE, a test harness) is written against
this set of command names.  A golden file means adding, renaming or retiring one
shows up as a diff in review instead of as a broken client after release.

The wire serialiser is pinned here too, because it is shared by stdio and the
WebSocket: one JSON object per LF-terminated line, non-ASCII kept readable.
"""

from __future__ import annotations

import json
from pathlib import Path

from pi_coding_agent.modes.jsonl import serialize_json_line
from pi_coding_agent.modes.rpc_mode import SUPPORTED_COMMANDS, UNSUPPORTED_COMMANDS

SURFACE = Path(__file__).resolve().parent / "testdata" / "protocol" / "rpc-surface.json"


def test_the_supported_command_set_matches_the_golden_file():
    golden = json.loads(SURFACE.read_text(encoding="utf-8"))

    assert sorted(SUPPORTED_COMMANDS) == golden["supported"]


def test_the_explicitly_unsupported_commands_match_the_golden_file():
    """These are named so a client can tell "not implemented" from "typo"."""
    golden = json.loads(SURFACE.read_text(encoding="utf-8"))

    assert dict(sorted(UNSUPPORTED_COMMANDS.items())) == golden["unsupported"]


def test_no_command_is_both_supported_and_unsupported():
    assert not set(SUPPORTED_COMMANDS) & set(UNSUPPORTED_COMMANDS)


def test_every_unsupported_command_says_why():
    for name, reason in UNSUPPORTED_COMMANDS.items():
        assert reason.strip(), f"{name} has no explanation"


def test_command_names_are_lowercase_and_unprefixed():
    for name in SUPPORTED_COMMANDS:
        assert name == name.lower()
        assert name.isidentifier() or "_" in name


# --------------------------------------------------------------------------------------
# Wire format
# --------------------------------------------------------------------------------------


def test_a_record_is_one_lf_terminated_line_with_readable_non_ascii():
    line = serialize_json_line({"type": "x", "text": "中文", "n": 1})

    assert line.endswith("\n")
    assert line.count("\n") == 1
    assert "中文" in line, "the wire keeps UTF-8 readable rather than \\u-escaping it"
    assert json.loads(line) == {"type": "x", "text": "中文", "n": 1}


def test_a_value_that_is_not_json_does_not_break_the_line():
    class Opaque:
        def __str__(self) -> str:
            return "opaque"

    line = serialize_json_line({"type": "tool_execution_end", "result": Opaque()})

    assert json.loads(line)["result"] == "opaque"


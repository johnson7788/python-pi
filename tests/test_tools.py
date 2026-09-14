"""Tests for the built-in tool set.

Each test drives a real tool factory against a ``tmp_path`` working directory so
the assertions cover the same code path the agent loop uses, rather than a
re-implementation of it.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from pi_ai import ImageContent

from pi_coding_agent.tools.bash import build_shell_env
from pi_coding_agent.tools import (
    BUILTIN_TOOL_NAMES,
    DEFAULT_ACTIVE_TOOL_NAMES,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    FileMutationQueue,
    create_bash_tool,
    create_builtin_tools,
    create_edit_tool,
    create_find_tool,
    create_grep_tool,
    create_ls_tool,
    create_read_tool,
    create_write_tool,
    truncate_head,
    truncate_head_result,
    truncate_line,
    truncate_tail,
    truncate_tail_result,
)

CALL_ID = "call-1"


def text_of(result) -> str:
    """Return the concatenated text blocks of a tool result."""
    return "".join(block.text for block in result.content if hasattr(block, "text"))


# --------------------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------------------


async def test_read_returns_raw_text_without_a_line_number_gutter(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    tool = create_read_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"path": "a.txt"})

    # Upstream returns the file verbatim, including the empty final line that
    # `split("\n")` produces for a trailing newline.
    assert text_of(result) == "one\ntwo\nthree\nfour\n"
    assert result.details is None


async def test_read_preserves_crlf_bytes(tmp_path: Path) -> None:
    (tmp_path / "win.txt").write_bytes(b"one\r\ntwo\r\n")
    tool = create_read_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"path": "win.txt"})

    assert text_of(result) == "one\r\ntwo\r\n"


async def test_read_offset_and_limit_are_one_indexed(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    tool = create_read_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"path": "a.txt", "offset": 2, "limit": 2})

    assert text_of(result) == "two\nthree\n\n[2 more lines in file. Use offset=4 to continue.]"


async def test_read_limit_reaching_end_of_file_has_no_notice(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\ntwo", encoding="utf-8")
    tool = create_read_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"path": "a.txt", "offset": 1, "limit": 2})

    assert text_of(result) == "one\ntwo"
    assert result.details is None


async def test_read_user_limit_stopped_early_notice(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\nb\nc\nd\ne", encoding="utf-8")
    tool = create_read_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"path": "a.txt", "limit": 2})

    assert text_of(result) == "a\nb\n\n[3 more lines in file. Use offset=3 to continue.]"
    assert result.details is None


async def test_read_truncation_notice_for_lines(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("\n".join(str(index) for index in range(2500)), encoding="utf-8")
    tool = create_read_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"path": "big.txt"})

    output = text_of(result)
    assert output.startswith("0\n1\n2\n")
    assert output.endswith("\n\n[Showing lines 1-2000 of 2500. Use offset=2001 to continue.]")
    assert result.details["truncation"]["truncatedBy"] == "lines"


async def test_read_offset_with_limit_reports_remaining_lines(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("\n".join(str(index) for index in range(2500)), encoding="utf-8")
    tool = create_read_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"path": "big.txt", "offset": 2001, "limit": 2})

    assert text_of(result) == "2000\n2001\n\n[498 more lines in file. Use offset=2003 to continue.]"


async def test_read_first_line_over_byte_limit_points_at_bash(tmp_path: Path) -> None:
    (tmp_path / "huge.txt").write_text("z" * (DEFAULT_MAX_BYTES + 1), encoding="utf-8")
    tool = create_read_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"path": "huge.txt"})

    assert text_of(result) == (
        "[Line 1 is 50.0KB, exceeds 50.0KB limit. "
        "Use bash: sed -n '1p' huge.txt | head -c 51200]"
    )
    assert result.details["truncation"]["firstLineExceedsLimit"] is True


async def test_read_offset_beyond_end_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    tool = create_read_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"path": "a.txt", "offset": 50})

    assert result.details == {"error": True}
    assert text_of(result) == "Offset 50 is beyond end of file (3 lines total)"


async def test_read_refuses_binary_files(tmp_path: Path) -> None:
    (tmp_path / "blob.dat").write_bytes(b"\x00\x01\x02binary\x00data")
    tool = create_read_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"path": "blob.dat"})

    assert result.details == {"error": True}
    assert "binary" in text_of(result)


async def test_read_missing_file_is_an_error(tmp_path: Path) -> None:
    result = await create_read_tool(str(tmp_path)).execute(CALL_ID, {"path": "nope.txt"})

    assert result.details == {"error": True}
    assert "Path not found" in text_of(result)


async def test_read_image_returns_image_content(tmp_path: Path) -> None:
    (tmp_path / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    tool = create_read_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"path": "pic.png"})

    assert "Read image file [image/png]" in text_of(result)
    images = [block for block in result.content if isinstance(block, ImageContent)]
    assert len(images) == 1
    assert images[0].mime_type == "image/png"


# --------------------------------------------------------------------------------------
# write / edit
# --------------------------------------------------------------------------------------


async def test_write_creates_parent_directories(tmp_path: Path) -> None:
    tool = create_write_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"path": "deep/nested/file.txt", "content": "hello"})

    assert (tmp_path / "deep" / "nested" / "file.txt").read_text(encoding="utf-8") == "hello"
    assert "Successfully wrote to deep/nested/file.txt" in text_of(result)
    assert result.details["bytesWritten"] == 5


async def test_write_then_edit_round_trip_produces_a_real_diff(tmp_path: Path) -> None:
    write_tool = create_write_tool(str(tmp_path))
    await write_tool.execute(CALL_ID, {"path": "code.py", "content": "def add(a, b):\n    return a + b\n"})

    edit_tool = create_edit_tool(str(tmp_path))
    result = await edit_tool.execute(
        CALL_ID,
        {"path": "code.py", "edits": [{"oldText": "return a + b", "newText": "return a + b  # sum"}]},
    )

    assert "Successfully replaced 1 block(s) in code.py." in text_of(result)
    assert (tmp_path / "code.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b  # sum\n"
    assert "-2     return a + b" in result.details["diff"]
    assert "+2     return a + b  # sum" in result.details["diff"]
    assert result.details["firstChangedLine"] == 2
    assert "-    return a + b\n" in result.details["patch"]
    assert "+    return a + b  # sum\n" in result.details["patch"]
    assert result.details["patch"].startswith("--- code.py\n+++ code.py\n@@ ")


async def test_edit_applies_multiple_disjoint_edits_against_the_original(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
    tool = create_edit_tool(str(tmp_path))

    result = await tool.execute(
        CALL_ID,
        {
            "path": "f.txt",
            "edits": [
                {"oldText": "alpha", "newText": "ALPHA"},
                {"oldText": "gamma", "newText": "GAMMA"},
            ],
        },
    )

    assert result.details.get("error") is None
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "ALPHA\nbeta\nGAMMA\ndelta\n"


async def test_edit_rejects_non_unique_old_text(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("same\nsame\n", encoding="utf-8")
    tool = create_edit_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"path": "f.txt", "edits": [{"oldText": "same", "newText": "other"}]})

    assert result.details == {"error": True}
    assert "occurrences" in text_of(result)
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "same\nsame\n"


async def test_edit_rejects_overlapping_edits(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("abcdefgh\n", encoding="utf-8")
    tool = create_edit_tool(str(tmp_path))

    result = await tool.execute(
        CALL_ID,
        {
            "path": "f.txt",
            "edits": [
                {"oldText": "abcd", "newText": "X"},
                {"oldText": "cdef", "newText": "Y"},
            ],
        },
    )

    assert result.details == {"error": True}
    assert "overlap" in text_of(result)
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "abcdefgh\n"


async def test_edit_reports_missing_old_text(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    tool = create_edit_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"path": "f.txt", "edits": [{"oldText": "absent", "newText": "x"}]})

    assert result.details == {"error": True}
    assert "Could not find the exact text" in text_of(result)


async def test_edit_preserves_bom_and_crlf(tmp_path: Path) -> None:
    target = tmp_path / "win.txt"
    target.write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\nthree\r\n")
    tool = create_edit_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"path": "win.txt", "edits": [{"oldText": "two", "newText": "TWO"}]})

    assert result.details.get("error") is None
    assert target.read_bytes() == b"\xef\xbb\xbfone\r\nTWO\r\nthree\r\n"


async def test_edit_accepts_replace_all_local_uniqueness_via_larger_context(tmp_path: Path) -> None:
    # Two identical short blocks are only editable when the model supplies
    # enough context to make each oldText unique; this documents that the tool
    # refuses the ambiguous call instead of guessing.
    (tmp_path / "f.txt").write_text("x = 1\ny = 1\n", encoding="utf-8")
    tool = create_edit_tool(str(tmp_path))

    ambiguous = await tool.execute(CALL_ID, {"path": "f.txt", "edits": [{"oldText": "= 1", "newText": "= 2"}]})
    assert ambiguous.details == {"error": True}

    unique = await tool.execute(CALL_ID, {"path": "f.txt", "edits": [{"oldText": "x = 1", "newText": "x = 2"}]})
    assert unique.details.get("error") is None
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "x = 2\ny = 1\n"


async def test_edit_rejects_empty_edits_array(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    tool = create_edit_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"path": "f.txt", "edits": []})

    assert result.details == {"error": True}
    assert "at least one replacement" in text_of(result)


async def test_edit_normalizes_a_json_string_edits_argument(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    tool = create_edit_tool(str(tmp_path))
    raw = {"path": "f.txt", "edits": '[{"oldText": "hello", "newText": "bye"}]'}
    args = tool.prepare_arguments(raw) if tool.prepare_arguments else raw

    result = await tool.execute(CALL_ID, args)

    assert result.details.get("error") is None
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "bye\n"


async def test_file_mutation_queue_serializes_same_file(tmp_path: Path) -> None:
    target = tmp_path / "counter.txt"
    target.write_text("0\n", encoding="utf-8")
    queue = FileMutationQueue()
    order: list[str] = []

    async def mutate(tag: str) -> None:
        async with queue.hold(str(target)):
            order.append(f"{tag}-enter")
            await asyncio.sleep(0.01)
            order.append(f"{tag}-exit")

    await asyncio.gather(mutate("a"), mutate("b"))

    assert order in (
        ["a-enter", "a-exit", "b-enter", "b-exit"],
        ["b-enter", "b-exit", "a-enter", "a-exit"],
    )


# --------------------------------------------------------------------------------------
# bash
# --------------------------------------------------------------------------------------


async def test_bash_returns_merged_output(tmp_path: Path) -> None:
    tool = create_bash_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"command": "echo out; echo err 1>&2"})

    assert set(text_of(result).splitlines()) == {"out", "err"}
    assert result.details is None


async def test_bash_injects_pi_cwd(tmp_path: Path) -> None:
    tool = create_bash_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"command": "printf '%s' \"$PI_CWD\""})

    assert text_of(result) == str(tmp_path)


async def test_bash_non_zero_exit_raises_with_output(tmp_path: Path) -> None:
    tool = create_bash_tool(str(tmp_path))

    with pytest.raises(RuntimeError) as excinfo:
        await tool.execute(CALL_ID, {"command": "echo boom; exit 7"})

    message = str(excinfo.value)
    assert "boom" in message
    assert "Command exited with code 7" in message


async def test_bash_timeout_raises(tmp_path: Path) -> None:
    tool = create_bash_tool(str(tmp_path))

    with pytest.raises(RuntimeError) as excinfo:
        await tool.execute(CALL_ID, {"command": "echo starting; sleep 30", "timeout": 0.5})

    message = str(excinfo.value)
    assert "starting" in message
    assert "Command timed out after 0.5 seconds" in message


async def test_bash_rejects_invalid_timeout(tmp_path: Path) -> None:
    tool = create_bash_tool(str(tmp_path))

    with pytest.raises(RuntimeError, match="must be a finite number"):
        await tool.execute(CALL_ID, {"command": "true", "timeout": 0})


async def test_bash_abort_kills_the_command(tmp_path: Path) -> None:
    from pi_ai import AbortController

    tool = create_bash_tool(str(tmp_path))
    controller = AbortController()

    async def abort_soon() -> None:
        await asyncio.sleep(0.05)
        controller.abort()

    aborter = asyncio.create_task(abort_soon())
    with pytest.raises(RuntimeError) as excinfo:
        await tool.execute(CALL_ID, {"command": "echo started; sleep 30"}, signal=controller.signal)
    await aborter

    assert "Command aborted" in str(excinfo.value)


async def test_bash_truncates_tail_and_writes_full_output(tmp_path: Path) -> None:
    tool = create_bash_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"command": "seq 1 3000"})

    output = text_of(result)
    assert output.startswith("1001\n1002")  # 3000 lines minus the 2000 kept
    assert "[Showing lines 1001-3000 of 3000. Full output: " in output
    full_path = result.details["fullOutputPath"]
    assert result.details["truncation"]["truncatedBy"] == "lines"
    assert Path(full_path).read_text(encoding="utf-8").splitlines()[0] == "1"
    assert len(Path(full_path).read_text(encoding="utf-8").splitlines()) == 3000
    os.unlink(full_path)


async def test_bash_empty_output(tmp_path: Path) -> None:
    tool = create_bash_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"command": "true"})

    assert text_of(result) == "(no output)"


async def test_bash_streams_updates(tmp_path: Path) -> None:
    tool = create_bash_tool(str(tmp_path))
    updates: list[str] = []

    await tool.execute(CALL_ID, {"command": "echo first; sleep 0.3; echo second"}, on_update=lambda r: updates.append(text_of(r)))

    assert updates  # at least the initial empty update and a streamed snapshot
    assert any("second" in update for update in updates)


# --------------------------------------------------------------------------------------
# grep
# --------------------------------------------------------------------------------------


async def test_grep_reports_file_line_and_text(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("first\nsecond needle\nthird\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("nothing here\n", encoding="utf-8")
    tool = create_grep_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"pattern": "needle"})

    assert text_of(result) == "a.txt:2: second needle"


async def test_grep_reports_no_matches(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("nothing\n", encoding="utf-8")
    tool = create_grep_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"pattern": "needle"})

    assert text_of(result) == "No matches found"
    assert result.details is None


async def test_grep_limit_truncates_results(tmp_path: Path) -> None:
    (tmp_path / "many.txt").write_text("\n".join("needle" for _ in range(10)) + "\n", encoding="utf-8")
    tool = create_grep_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"pattern": "needle", "limit": 3})

    assert result.details["matchLimitReached"] == 3
    assert text_of(result).count("needle") == 3
    assert "[3 matches limit reached. Use limit=6 for more, or refine pattern]" in text_of(result)


async def test_grep_is_case_sensitive_by_default(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("Needle\n", encoding="utf-8")
    tool = create_grep_tool(str(tmp_path))

    assert text_of(await tool.execute(CALL_ID, {"pattern": "needle"})) == "No matches found"
    insensitive = await tool.execute(CALL_ID, {"pattern": "needle", "caseSensitive": False})
    assert text_of(insensitive) == "a.txt:1: Needle"


async def test_grep_respects_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("ignored.txt\nbuild/\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "gen.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "kept.txt").write_text("needle\n", encoding="utf-8")
    tool = create_grep_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"pattern": "needle"})

    assert text_of(result) == "kept.txt:1: needle"


async def test_grep_context_lines(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("before\nmatch\nafter\n", encoding="utf-8")
    tool = create_grep_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"pattern": "match", "contextLines": 1})

    assert text_of(result) == "a.txt-1- before\na.txt:2: match\na.txt-3- after"


async def test_grep_glob_filters_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")
    tool = create_grep_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"pattern": "needle", "glob": "*.py"})

    assert text_of(result) == "a.py:1: needle"


async def test_grep_truncates_long_lines(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("needle" + "x" * 600 + "\n", encoding="utf-8")
    tool = create_grep_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"pattern": "needle"})

    assert "... [truncated]" in text_of(result)
    assert result.details["linesTruncated"] is True
    assert "Some lines truncated to 500 chars" in text_of(result)


async def test_grep_rejects_invalid_regex(tmp_path: Path) -> None:
    tool = create_grep_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"pattern": "("})

    assert result.details == {"error": True}
    assert "Invalid regex" in text_of(result)


# --------------------------------------------------------------------------------------
# find
# --------------------------------------------------------------------------------------


async def test_find_matches_basenames_at_any_depth(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "sub" / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "sub" / "c.py").write_text("c", encoding="utf-8")
    tool = create_find_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"pattern": "*.txt"})

    assert text_of(result).splitlines() == ["a.txt", "sub/b.txt"]


async def test_find_matches_full_path_patterns(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "b.txt").write_text("b", encoding="utf-8")
    tool = create_find_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"pattern": "sub/*.txt"})

    assert text_of(result).splitlines() == ["sub/b.txt"]


async def test_find_limit_and_no_results(tmp_path: Path) -> None:
    for index in range(5):
        (tmp_path / f"f{index}.txt").write_text("x", encoding="utf-8")
    tool = create_find_tool(str(tmp_path))

    limited = await tool.execute(CALL_ID, {"pattern": "*.txt", "limit": 2})
    assert limited.details["resultLimitReached"] == 2
    assert len(text_of(limited).splitlines()[:2]) == 2

    empty = await tool.execute(CALL_ID, {"pattern": "*.md"})
    assert text_of(empty) == "No files found matching pattern"


async def test_find_respects_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (tmp_path / "keep.txt").write_text("x", encoding="utf-8")
    (tmp_path / "drop.log").write_text("x", encoding="utf-8")
    tool = create_find_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"pattern": "*"})

    # .gitignore is itself not ignored by git, so it is still a result.
    assert text_of(result).splitlines() == [".gitignore", "keep.txt"]


# --------------------------------------------------------------------------------------
# ls
# --------------------------------------------------------------------------------------


async def test_ls_sorts_case_insensitively_and_marks_directories(tmp_path: Path) -> None:
    (tmp_path / "Beta.txt").write_text("x", encoding="utf-8")
    (tmp_path / "alpha.txt").write_text("x", encoding="utf-8")
    (tmp_path / "Zeta").mkdir()
    (tmp_path / ".hidden").write_text("x", encoding="utf-8")
    tool = create_ls_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {})

    assert text_of(result).splitlines() == [".hidden", "alpha.txt", "Beta.txt", "Zeta/"]


async def test_ls_limit_and_errors(tmp_path: Path) -> None:
    for index in range(5):
        (tmp_path / f"f{index}.txt").write_text("x", encoding="utf-8")
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    tool = create_ls_tool(str(tmp_path))

    limited = await tool.execute(CALL_ID, {"limit": 2})
    assert limited.details["entryLimitReached"] == 2
    assert "entries limit reached" in text_of(limited)

    not_a_directory = await tool.execute(CALL_ID, {"path": "file.txt"})
    assert not_a_directory.details == {"error": True}
    assert "Not a directory" in text_of(not_a_directory)

    missing = await tool.execute(CALL_ID, {"path": "nope"})
    assert "Path not found" in text_of(missing)


async def test_ls_empty_directory(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    tool = create_ls_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"path": "empty"})

    assert text_of(result) == "(empty directory)"


# --------------------------------------------------------------------------------------
# truncation
# --------------------------------------------------------------------------------------


def test_truncate_head_keeps_exactly_the_line_budget() -> None:
    content = "\n".join(str(index) for index in range(DEFAULT_MAX_LINES))

    text, truncated = truncate_head(content)

    assert truncated is False
    assert text == content

    over, over_truncated = truncate_head(content + "\n" + str(DEFAULT_MAX_LINES))
    assert over_truncated is True
    assert over.split("\n") == content.split("\n")


def test_truncate_head_reports_the_line_limit() -> None:
    content = "\n".join(str(index) for index in range(DEFAULT_MAX_LINES + 1))

    result = truncate_head_result(content)

    assert result.truncated is True
    assert result.truncated_by == "lines"
    assert result.total_lines == DEFAULT_MAX_LINES + 1
    assert result.output_lines == DEFAULT_MAX_LINES


def test_truncate_head_respects_exactly_the_byte_budget() -> None:
    lines = ["x" * 1000 for _ in range(51)]
    content = "\n".join(lines)
    content = content + "y" * (DEFAULT_MAX_BYTES - len(content))
    assert len(content.encode("utf-8")) == DEFAULT_MAX_BYTES

    text, truncated = truncate_head(content)
    assert truncated is False
    assert text == content

    result = truncate_head_result(content + "y")
    assert result.truncated is True
    assert result.truncated_by == "bytes"
    assert result.output_lines == 50


def test_truncate_head_first_line_over_budget() -> None:
    content = "z" * (DEFAULT_MAX_BYTES + 1)

    result = truncate_head_result(content)

    assert result.content == ""
    assert result.first_line_exceeds_limit is True
    assert result.truncated_by == "bytes"


def test_truncate_tail_keeps_the_end() -> None:
    lines = [str(index) for index in range(DEFAULT_MAX_LINES + 1)]
    content = "\n".join(lines)

    text, truncated = truncate_tail(content)

    assert truncated is True
    assert text.split("\n") == lines[1:]

    exact, exact_truncated = truncate_tail("\n".join(lines[:DEFAULT_MAX_LINES]))
    assert exact_truncated is False
    assert exact == "\n".join(lines[:DEFAULT_MAX_LINES])


def test_truncate_tail_byte_budget() -> None:
    content = "\n".join(["x" * 1000 for _ in range(52)])

    result = truncate_tail_result(content)

    assert result.truncated is True
    assert result.truncated_by == "bytes"
    assert result.output_lines == 51
    assert len(result.content.encode("utf-8")) <= DEFAULT_MAX_BYTES


def test_truncate_tail_partial_line_when_one_line_exceeds_the_budget() -> None:
    content = "a" * 100 + "\n" + "b" * (DEFAULT_MAX_BYTES + 10)

    result = truncate_tail_result(content)

    assert result.truncated is True
    assert result.last_line_partial is True
    assert len(result.content.encode("utf-8")) <= DEFAULT_MAX_BYTES
    assert set(result.content) == {"b"}


def test_truncate_line_boundary() -> None:
    assert truncate_line("a" * 500) == ("a" * 500, False)
    short, was_truncated = truncate_line("a" * 501)
    assert was_truncated is True
    assert short == "a" * 500 + "... [truncated]"


# --------------------------------------------------------------------------------------
# tool set wiring
# --------------------------------------------------------------------------------------


def test_builtin_tool_names_and_defaults(tmp_path: Path) -> None:
    assert BUILTIN_TOOL_NAMES == ("read", "bash", "edit", "write", "grep", "find", "ls")
    assert DEFAULT_ACTIVE_TOOL_NAMES == ("read", "bash", "edit", "write")

    tools = create_builtin_tools(str(tmp_path))
    assert [tool.name for tool in tools] == ["read", "bash", "edit", "write"]

    everything = create_builtin_tools(str(tmp_path), BUILTIN_TOOL_NAMES)
    assert [tool.name for tool in everything] == list(BUILTIN_TOOL_NAMES)

    with pytest.raises(ValueError, match="Unknown built-in tool"):
        create_builtin_tools(str(tmp_path), ["read", "nope"])


def test_every_builtin_tool_exposes_a_schema_and_description(tmp_path: Path) -> None:
    for tool in create_builtin_tools(str(tmp_path), BUILTIN_TOOL_NAMES):
        assert tool.description
        assert tool.parameters["type"] == "object"
        assert callable(tool.execute)


# --------------------------------------------------------------------------------------
# cross-cutting behaviour
# --------------------------------------------------------------------------------------


async def test_edit_matches_typographic_quotes_fuzzily(tmp_path: Path) -> None:
    # A model retyping text from a rendered file emits ASCII quotes; the fuzzy
    # pass accepts that without rewriting the untouched bytes of the file.
    (tmp_path / "f.txt").write_text("say \u2018hello\u2019 now\nkeep me\n", encoding="utf-8")
    tool = create_edit_tool(str(tmp_path))

    result = await tool.execute(
        CALL_ID,
        {"path": "f.txt", "edits": [{"oldText": "say 'hello' now", "newText": "say 'bye' now"}]},
    )

    assert result.details.get("error") is None
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "say 'bye' now\nkeep me\n"


async def test_edit_rejects_a_no_op_replacement(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    tool = create_edit_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"path": "f.txt", "edits": [{"oldText": "hello", "newText": "hello"}]})

    assert result.details == {"error": True}
    assert "No changes made" in text_of(result)


async def test_edit_missing_file_is_an_error_result(tmp_path: Path) -> None:
    result = await create_edit_tool(str(tmp_path)).execute(
        CALL_ID, {"path": "missing.txt", "edits": [{"oldText": "a", "newText": "b"}]}
    )

    assert result.details == {"error": True}
    assert "Could not edit file" in text_of(result)


async def test_read_truncation_notice_matches_upstream_byte_variant(tmp_path: Path) -> None:
    # A file whose content is wider than 50KB but whose lines are short hits the
    # byte budget rather than the line budget; the notice names the limit.
    (tmp_path / "wide.txt").write_text("\n".join("x" * 1000 for _ in range(60)), encoding="utf-8")
    tool = create_read_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"path": "wide.txt"})

    output = text_of(result)
    assert "[Showing lines 1-51 of 60 (50.0KB limit). Use offset=52 to continue.]" in output
    assert result.details["truncation"]["truncatedBy"] == "bytes"


async def test_grep_accepts_a_single_file_search_path(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")
    tool = create_grep_tool(str(tmp_path))

    result = await tool.execute(CALL_ID, {"pattern": "needle", "path": "a.txt"})

    assert text_of(result) == "a.txt:1: needle"


async def test_tool_details_are_json_serializable(tmp_path: Path) -> None:
    """Details are written into session files, so they must survive json.dumps."""
    (tmp_path / "f.txt").write_text("hello\nworld\n", encoding="utf-8")

    results = [
        await create_read_tool(str(tmp_path)).execute(CALL_ID, {"path": "f.txt"}),
        await create_write_tool(str(tmp_path)).execute(CALL_ID, {"path": "new.txt", "content": "x"}),
        await create_edit_tool(str(tmp_path)).execute(
            CALL_ID, {"path": "f.txt", "edits": [{"oldText": "world", "newText": "there"}]}
        ),
        await create_grep_tool(str(tmp_path)).execute(CALL_ID, {"pattern": "hello"}),
        await create_find_tool(str(tmp_path)).execute(CALL_ID, {"pattern": "*.txt"}),
        await create_ls_tool(str(tmp_path)).execute(CALL_ID, {}),
    ]

    for result in results:
        json.dumps(result.details)


# --------------------------------------------------------------------------------------
# Shared-deployment environment scrubbing
# --------------------------------------------------------------------------------------


def test_build_shell_env_keeps_the_parent_environment_by_default(monkeypatch):
    """A personal install must keep seeing its own tokens; this is the old behaviour."""
    monkeypatch.setenv("GITHUB_TOKEN", "personal-token")
    env = build_shell_env("/tmp")
    assert env["GITHUB_TOKEN"] == "personal-token"


def test_build_shell_env_scrubs_credentials_for_a_shared_deployment(monkeypatch):
    monkeypatch.setenv("PI_API_KEY", "shared-company-key")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("DB_PASSWORD", "hunter2")
    monkeypatch.setenv("MY_CREDENTIALS", "creds")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Ada")

    env = build_shell_env("/tmp", scrub_secrets=True)

    for leaked in (
        "PI_API_KEY",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "DB_PASSWORD",
        "MY_CREDENTIALS",
        "XDG_SESSION_TYPE",
    ):
        assert leaked not in env, leaked

    # The rule is per underscore-separated segment, so ordinary variables survive:
    # `GIT_AUTHOR_NAME` contains "AUTH" as a substring but is not a credential.
    assert env["GIT_AUTHOR_NAME"] == "Ada"
    assert env["PATH"] == "/usr/bin"


def test_build_shell_env_still_exposes_the_session(monkeypatch):
    monkeypatch.setenv("PI_API_KEY", "shared-company-key")
    env = build_shell_env("/tmp/work", {"PI_SESSION_ID": "s-1"}, scrub_secrets=True)
    assert env["PI_CWD"] == "/tmp/work"
    assert env["PI_SESSION_ID"] == "s-1"


async def test_a_scrubbed_bash_tool_cannot_read_the_shared_credential(
    tmp_path: Path, monkeypatch
) -> None:
    """The tool-level end of the scrub: ask the shell for the key and get nothing back."""
    monkeypatch.setenv("PI_API_KEY", "shared-company-key")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    tool = create_bash_tool(str(tmp_path), scrub_secrets=True)

    result = await tool.execute(
        CALL_ID, {"command": "printenv PI_API_KEY; printenv GITHUB_TOKEN; echo done"}
    )

    output = text_of(result)
    assert "shared-company-key" not in output
    assert "gh-token" not in output
    assert "done" in output

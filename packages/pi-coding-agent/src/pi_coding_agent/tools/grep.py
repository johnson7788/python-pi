"""The ``grep`` tool: recursive pure-Python content search.

The upstream implementation shells out to ripgrep.  This port cannot depend on
external binaries, so the walk, the ``.gitignore`` handling and the regex
evaluation all live here.  The observable contract is kept: matches are reported
as ``file:line: text`` (context lines as ``file-line- text``), long lines are
truncated to 500 characters, the default result limit is 100 and the total
output is capped at 50KB.
"""

from __future__ import annotations

import os
import posixpath
import re
from pathlib import PurePath
from typing import Any

from pi_agent import AgentTool, AgentToolResult, ToolUpdateCallback
from pi_ai import AbortSignal, TextContent

from .path_utils import matches_glob, resolve_path, walk_files
from .path_utils import error_result as _error_result
from .truncate import DEFAULT_MAX_BYTES, GREP_MAX_LINE_LENGTH, format_size, truncate_head_result, truncate_line

DEFAULT_LIMIT = 100

#: JS ``Number.MAX_SAFE_INTEGER``: "no line limit", only the byte limit applies.
UNLIMITED_LINES = 2**53 - 1

#: Files larger than this are skipped rather than loaded into memory.
MAX_SEARCH_FILE_BYTES = 50 * 1024 * 1024

DESCRIPTION = (
    "Search file contents for a pattern. Returns matching lines with file paths and line numbers. Respects "
    f".gitignore. Output is truncated to {DEFAULT_LIMIT} matches or {DEFAULT_MAX_BYTES // 1024}KB (whichever "
    f"is hit first). Long lines are truncated to {GREP_MAX_LINE_LENGTH} chars."
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "Search pattern (regex or literal string)"},
        "path": {"type": "string", "description": "Directory or file to search (default: current directory)"},
        "glob": {"type": "string", "description": "Filter files by glob pattern, e.g. '*.ts' or '**/*.spec.ts'"},
        "caseSensitive": {"type": "boolean", "description": "Case-sensitive search (default: true)"},
        "literal": {"type": "boolean", "description": "Treat pattern as a literal string (default: false)"},
        "contextLines": {"type": "number", "description": "Lines of context before and after each match (default: 0)"},
        "limit": {"type": "number", "description": "Maximum number of matches to return (default: 100)"},
    },
    "required": ["pattern"],
}


def _is_binary_file(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            return b"\x00" in handle.read(8192)
    except OSError:
        return True


def _read_lines(path: str) -> list[str]:
    with open(path, "rb") as handle:
        data = handle.read()
    text = data.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _format_path(file_path: str, search_path: str, is_directory: bool) -> str:
    """Mirror upstream: relative to the search root for directories, else basename."""
    if is_directory:
        relative = os.path.relpath(file_path, search_path)
        if relative and not relative.startswith(".."):
            return PurePath(relative).as_posix()
    return posixpath.basename(file_path)


async def execute_grep(cwd: str, args: Any, signal: AbortSignal | None = None) -> AgentToolResult:
    """Implementation shared with tests; separated from the tool factory for clarity."""
    if not isinstance(args, dict):
        return _error_result("grep requires an object argument with 'pattern'.")
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or pattern == "":
        return _error_result("grep requires a non-empty 'pattern'.")

    search_path = resolve_path(cwd, args.get("path") or ".")
    if not os.path.exists(search_path):
        return _error_result(f"Path not found: {search_path}")

    is_directory = os.path.isdir(search_path)
    raw_limit = args.get("limit")
    effective_limit = max(1, int(raw_limit)) if isinstance(raw_limit, (int, float)) else DEFAULT_LIMIT
    context_value = args.get("contextLines", args.get("context", 0))
    context_value = max(0, int(context_value)) if isinstance(context_value, (int, float)) else 0
    # ``caseSensitive`` defaults to true; ``ignoreCase`` is accepted for callers
    # that still speak the upstream argument name.
    if "caseSensitive" in args:
        case_sensitive = bool(args.get("caseSensitive"))
    else:
        case_sensitive = not bool(args.get("ignoreCase"))
    literal = bool(args.get("literal"))
    file_glob = args.get("glob") if isinstance(args.get("glob"), str) else None

    try:
        expression = re.escape(pattern) if literal else pattern
        flags = 0 if case_sensitive else re.IGNORECASE
        regex = re.compile(expression, flags)
    except re.error as exc:
        return _error_result(f"Invalid regex pattern: {exc}")

    output_lines: list[str] = []
    match_count = 0
    match_limit_reached = False
    lines_truncated = False

    if is_directory:
        candidates = walk_files(search_path, respect_gitignore=True)
    else:
        candidates = iter([(search_path, posixpath.basename(search_path))])

    for file_path, relative_path in candidates:
        if signal is not None and signal.aborted:
            return _error_result("Operation aborted")
        if file_glob is not None and not matches_glob(file_glob, relative_path):
            continue
        try:
            if os.path.getsize(file_path) > MAX_SEARCH_FILE_BYTES:
                continue
        except OSError:
            continue
        if _is_binary_file(file_path):
            continue

        try:
            lines = _read_lines(file_path)
        except OSError:
            continue
        if not lines:
            continue

        display_path = _format_path(file_path, search_path, is_directory)
        for index, line in enumerate(lines):
            line_number = index + 1
            if not regex.search(line):
                continue

            match_count += 1
            start = max(1, line_number - context_value) if context_value > 0 else line_number
            end = min(len(lines), line_number + context_value) if context_value > 0 else line_number
            for current in range(start, end + 1):
                text = lines[current - 1].replace("\r", "")
                truncated_text, was_truncated = truncate_line(text)
                if was_truncated:
                    lines_truncated = True
                if current == line_number:
                    output_lines.append(f"{display_path}:{current}: {truncated_text}")
                else:
                    output_lines.append(f"{display_path}-{current}- {truncated_text}")

            if match_count >= effective_limit:
                match_limit_reached = True
                break
        if match_limit_reached:
            break

    if match_count == 0:
        return AgentToolResult(content=[TextContent(text="No matches found")], details=None)

    raw_output = "\n".join(output_lines)
    truncation = truncate_head_result(raw_output, max_lines=UNLIMITED_LINES)

    details: dict[str, Any] = {}
    notices: list[str] = []
    if match_limit_reached:
        notices.append(
            f"{effective_limit} matches limit reached. Use limit={effective_limit * 2} for more, or refine pattern"
        )
        details["matchLimitReached"] = effective_limit
    if truncation.truncated:
        notices.append(f"{format_size(DEFAULT_MAX_BYTES)} limit reached")
        details["truncation"] = truncation.to_dict()
    if lines_truncated:
        notices.append(
            f"Some lines truncated to {GREP_MAX_LINE_LENGTH} chars. Use read tool to see full lines"
        )
        details["linesTruncated"] = True

    output = truncation.content
    if notices:
        output += f"\n\n[{'. '.join(notices)}]"
    return AgentToolResult(content=[TextContent(text=output)], details=details or None)


def create_grep_tool(cwd: str) -> AgentTool:
    """Build the ``grep`` tool bound to ``cwd``."""

    async def execute(
        tool_call_id: str,
        args: Any,
        signal: AbortSignal | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        return await execute_grep(cwd, args, signal)

    return AgentTool(
        name="grep",
        label="grep",
        description=DESCRIPTION,
        parameters=_SCHEMA,
        execute=execute,
        prompt_snippet="Search file contents for patterns (respects .gitignore)",
    )


__all__ = ["DEFAULT_LIMIT", "create_grep_tool", "execute_grep"]

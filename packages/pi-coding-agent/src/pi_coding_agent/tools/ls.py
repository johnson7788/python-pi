"""The ``ls`` tool: a single directory level, sorted and annotated.

Directory entries get a trailing ``/`` so the model can tell files from folders
without a second call, and the sort is case-insensitive to match what a shell
user expects from ``ls``.  Hidden files are included because the tool is used to
discover project structure, where ``.gitignore`` and friends matter.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from pi_agent import AgentTool, AgentToolResult, ToolUpdateCallback
from pi_ai import AbortSignal, TextContent

from .path_utils import error_result as _error_result
from .path_utils import resolve_path
from .truncate import DEFAULT_MAX_BYTES, format_size, truncate_head_result

DEFAULT_LIMIT = 500

#: JS ``Number.MAX_SAFE_INTEGER``: "no line limit", only the byte limit applies.
UNLIMITED_LINES = 2**53 - 1

DESCRIPTION = (
    "List directory contents. Returns entries sorted alphabetically, with '/' suffix for directories. Includes "
    f"dotfiles. Output is truncated to {DEFAULT_LIMIT} entries or {DEFAULT_MAX_BYTES // 1024}KB (whichever is "
    "hit first)."
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Directory to list (default: current directory)"},
        "limit": {"type": "number", "description": "Maximum number of entries to return (default: 500)"},
    },
}


def _list_entries(directory: str, limit: int) -> tuple[list[str], bool]:
    """Return formatted entry names and whether the entry limit was reached."""
    names = sorted(os.listdir(directory), key=str.casefold)

    results: list[str] = []
    entry_limit_reached = False
    for name in names:
        if len(results) >= limit:
            entry_limit_reached = True
            break
        full_path = os.path.join(directory, name)
        try:
            is_directory = os.path.isdir(full_path)
        except OSError:
            # Unreadable or broken entry: skip rather than fail the whole listing.
            continue
        results.append(name + ("/" if is_directory else ""))
    return results, entry_limit_reached


async def execute_ls(cwd: str, args: Any, signal: AbortSignal | None = None) -> AgentToolResult:
    """Implementation shared with tests; separated from the tool factory for clarity."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _error_result("ls requires an object argument.")

    directory = resolve_path(cwd, args.get("path") or ".")
    raw_limit = args.get("limit")
    effective_limit = int(raw_limit) if isinstance(raw_limit, (int, float)) else DEFAULT_LIMIT
    effective_limit = max(0, effective_limit)

    if not os.path.exists(directory):
        return _error_result(f"Path not found: {directory}")
    if not os.path.isdir(directory):
        return _error_result(f"Not a directory: {directory}")

    if signal is not None and signal.aborted:
        return _error_result("Operation aborted")

    try:
        results, entry_limit_reached = await asyncio.to_thread(_list_entries, directory, effective_limit)
    except OSError as exc:
        return _error_result(f"Cannot read directory: {exc}")

    if signal is not None and signal.aborted:
        return _error_result("Operation aborted")

    if not results:
        return AgentToolResult(content=[TextContent(text="(empty directory)")], details=None)

    truncation = truncate_head_result("\n".join(results), max_lines=UNLIMITED_LINES)

    details: dict[str, Any] = {}
    notices: list[str] = []
    if entry_limit_reached:
        notices.append(f"{effective_limit} entries limit reached. Use limit={effective_limit * 2} for more")
        details["entryLimitReached"] = effective_limit
    if truncation.truncated:
        notices.append(f"{format_size(DEFAULT_MAX_BYTES)} limit reached")
        details["truncation"] = truncation.to_dict()

    output = truncation.content
    if notices:
        output += f"\n\n[{'. '.join(notices)}]"
    return AgentToolResult(content=[TextContent(text=output)], details=details or None)


def create_ls_tool(cwd: str) -> AgentTool:
    """Build the ``ls`` tool bound to ``cwd``."""

    async def execute(
        tool_call_id: str,
        args: Any,
        signal: AbortSignal | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        return await execute_ls(cwd, args, signal)

    return AgentTool(
        name="ls",
        label="ls",
        description=DESCRIPTION,
        parameters=_SCHEMA,
        execute=execute,
        prompt_snippet="List directory contents",
    )


__all__ = ["DEFAULT_LIMIT", "create_ls_tool", "execute_ls"]

"""The ``find`` tool: recursive glob search in pure Python.

Upstream shells out to ``fd``.  This port walks the tree itself, honours
``.gitignore`` and reproduces fd's two matching modes: a bare pattern matches
the basename at any depth, while a pattern containing ``/`` matches the whole
relative path with an implicit ``**/`` prefix.
"""

from __future__ import annotations

import os
import posixpath
from pathlib import PurePath
from typing import Any

from pi_agent import AgentTool, AgentToolResult, ToolUpdateCallback
from pi_ai import AbortSignal, TextContent

from .path_utils import matches_glob, resolve_path, walk_files
from .path_utils import error_result as _error_result
from .truncate import DEFAULT_MAX_BYTES, format_size, truncate_head_result

DEFAULT_LIMIT = 1000

#: JS ``Number.MAX_SAFE_INTEGER``: "no line limit", only the byte limit applies.
UNLIMITED_LINES = 2**53 - 1

DESCRIPTION = (
    "Search for files by glob pattern. Returns matching file paths relative to the search directory. Respects "
    f".gitignore. Output is truncated to {DEFAULT_LIMIT} results or {DEFAULT_MAX_BYTES // 1024}KB (whichever "
    "is hit first)."
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "Glob pattern to match files, e.g. '*.ts', '**/*.json', or 'src/**/*.spec.ts'",
        },
        "path": {"type": "string", "description": "Directory to search in (default: current directory)"},
        "limit": {"type": "number", "description": "Maximum number of results (default: 1000)"},
    },
    "required": ["pattern"],
}


async def execute_find(cwd: str, args: Any, signal: AbortSignal | None = None) -> AgentToolResult:
    """Implementation shared with tests; separated from the tool factory for clarity."""
    if not isinstance(args, dict):
        return _error_result("find requires an object argument with 'pattern'.")
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or pattern == "":
        return _error_result("find requires a non-empty 'pattern'.")

    search_path = resolve_path(cwd, args.get("path") or ".")
    if not os.path.exists(search_path):
        return _error_result(f"Path not found: {search_path}")

    raw_limit = args.get("limit")
    effective_limit = int(raw_limit) if isinstance(raw_limit, (int, float)) else DEFAULT_LIMIT
    effective_limit = max(1, effective_limit)

    if os.path.isdir(search_path):
        candidates = walk_files(search_path, respect_gitignore=True)
    else:
        candidates = iter([(search_path, posixpath.basename(search_path))])

    results: list[str] = []
    result_limit_reached = False
    for file_path, relative_path in candidates:
        if signal is not None and signal.aborted:
            return _error_result("Operation aborted")
        if not matches_glob(pattern, relative_path):
            continue
        results.append(relative_path)
        if len(results) >= effective_limit:
            result_limit_reached = True
            break

    if not results:
        return AgentToolResult(content=[TextContent(text="No files found matching pattern")], details=None)

    # Sorted output makes the fixed-size truncation stable across runs.
    results.sort()
    truncation = truncate_head_result("\n".join(results), max_lines=UNLIMITED_LINES)

    details: dict[str, Any] = {}
    notices: list[str] = []
    if result_limit_reached:
        notices.append(f"{effective_limit} results limit reached. Use limit={effective_limit * 2} for more, or refine pattern")
        details["resultLimitReached"] = effective_limit
    if truncation.truncated:
        notices.append(f"{format_size(DEFAULT_MAX_BYTES)} limit reached")
        details["truncation"] = truncation.to_dict()

    output = truncation.content
    if notices:
        output += f"\n\n[{'. '.join(notices)}]"
    return AgentToolResult(content=[TextContent(text=output)], details=details or None)


def create_find_tool(cwd: str) -> AgentTool:
    """Build the ``find`` tool bound to ``cwd``."""

    async def execute(
        tool_call_id: str,
        args: Any,
        signal: AbortSignal | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        return await execute_find(cwd, args, signal)

    return AgentTool(
        name="find",
        label="find",
        description=DESCRIPTION,
        parameters=_SCHEMA,
        execute=execute,
        prompt_snippet="Find files by glob pattern (respects .gitignore)",
    )


__all__ = ["DEFAULT_LIMIT", "create_find_tool", "execute_find"]

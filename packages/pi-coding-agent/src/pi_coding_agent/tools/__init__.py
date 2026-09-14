"""Built-in tool set for the coding agent.

Every tool is a factory bound to one working directory (plus, for the mutating
tools, a shared :class:`FileMutationQueue`).  ``create_builtin_tools`` builds the
requested subset and wires one queue through both mutating tools, so an ``edit``
and a ``write`` issued in the same parallel batch cannot interleave on the same
file.
"""

from __future__ import annotations

from typing import Iterable

from pi_agent import AgentTool

from .bash import create_bash_tool
from .edit import create_edit_tool, prepare_edit_arguments
from .find import create_find_tool
from .grep import create_grep_tool
from .ls import create_ls_tool
from .path_utils import error_result, render_path, resolve_path, text_result
from .read import create_read_tool
from .truncate import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    GREP_MAX_LINE_LENGTH,
    TruncationResult,
    format_size,
    truncate_head,
    truncate_head_result,
    truncate_line,
    truncate_tail,
    truncate_tail_result,
)
from .write import FileMutationQueue, create_write_tool, get_file_mutation_queue

#: Every built-in tool this package can build, in prompt order.
BUILTIN_TOOL_NAMES: tuple[str, ...] = ("read", "bash", "edit", "write", "grep", "find", "ls")

#: The subset enabled by default; the search tools are opt-in.
DEFAULT_ACTIVE_TOOL_NAMES: tuple[str, ...] = ("read", "bash", "edit", "write")


def create_builtin_tools(
    cwd: str, names: Iterable[str] | None = None, *, scrub_secrets: bool = False
) -> list[AgentTool]:
    """Build the named built-in tools, defaulting to the active set.

    Ordering follows ``BUILTIN_TOOL_NAMES`` rather than the caller's order so
    prompt output is stable, and unknown names fail loudly instead of silently
    producing a smaller tool set.  ``scrub_secrets`` withholds credential-shaped
    environment variables from shells the agent runs; a shared deployment sets it.
    """
    selected = set(names) if names is not None else set(DEFAULT_ACTIVE_TOOL_NAMES)
    unknown = selected - set(BUILTIN_TOOL_NAMES)
    if unknown:
        raise ValueError(f"Unknown built-in tool(s): {', '.join(sorted(unknown))}")

    ordered = [name for name in BUILTIN_TOOL_NAMES if name in selected]
    # The process-wide queue is shared on purpose: a write from one tool set and
    # an edit from another can still target the same file, and upstream
    # serialises those through one module-level map.
    queue = get_file_mutation_queue()
    tools: list[AgentTool] = []
    for name in ordered:
        if name == "read":
            tools.append(create_read_tool(cwd))
        elif name == "bash":
            tools.append(create_bash_tool(cwd, scrub_secrets=scrub_secrets))
        elif name == "edit":
            tools.append(create_edit_tool(cwd, queue))
        elif name == "write":
            tools.append(create_write_tool(cwd, queue))
        elif name == "grep":
            tools.append(create_grep_tool(cwd))
        elif name == "find":
            tools.append(create_find_tool(cwd))
        else:  # name == "ls"; the set above makes this exhaustive
            tools.append(create_ls_tool(cwd))
    return tools


__all__ = [
    "BUILTIN_TOOL_NAMES",
    "DEFAULT_ACTIVE_TOOL_NAMES",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_LINES",
    "GREP_MAX_LINE_LENGTH",
    "FileMutationQueue",
    "TruncationResult",
    "create_bash_tool",
    "create_builtin_tools",
    "create_edit_tool",
    "create_find_tool",
    "create_grep_tool",
    "create_ls_tool",
    "create_read_tool",
    "create_write_tool",
    "error_result",
    "format_size",
    "get_file_mutation_queue",
    "prepare_edit_arguments",
    "render_path",
    "resolve_path",
    "text_result",
    "truncate_head",
    "truncate_head_result",
    "truncate_line",
    "truncate_tail",
    "truncate_tail_result",
]

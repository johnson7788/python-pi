"""The ``read`` tool: text and image file contents for the model.

Text output is the file's raw content, capped by the shared head truncation,
plus the upstream continuation notices.  No line-number gutter is added: tool
results are part of the interop contract with ``pi`` itself, so the visible text
has to match upstream character for character.  Images are returned as an
``ImageContent`` block; anything without a text or image interpretation is
refused rather than decoded into mojibake, because silently corrupting a binary
file's contents is worse than telling the model to use ``bash``.
"""

from __future__ import annotations

import asyncio
import base64
import os
from typing import Any

from pi_agent import AgentTool, AgentToolResult, ToolUpdateCallback
from pi_ai import AbortSignal, ImageContent, TextContent

from .path_utils import error_result, render_path, resolve_read_path, text_result
from .truncate import DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES, format_size, truncate_head_result, utf8_len

DESCRIPTION = (
    "Read the contents of a file. Supports text files and images (jpg, png, gif, webp, bmp). Images are sent "
    f"as attachments. For text files, output is truncated to {DEFAULT_MAX_LINES} lines or "
    f"{DEFAULT_MAX_BYTES // 1024}KB (whichever is hit first). Use offset/limit for large files. When you need "
    "the full file, continue with offset until complete."
)

#: Extension to MIME type.  Detection is extension based: unlike upstream there
#: is no image library available, so the bytes are forwarded unchanged.
IMAGE_MIME_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

#: How much of a file is inspected for NUL bytes before declaring it binary.
BINARY_SNIFF_BYTES = 8192

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Path to the file to read (relative or absolute)"},
        "offset": {"type": "number", "description": "Line number to start reading from (1-indexed)"},
        "limit": {"type": "number", "description": "Maximum number of lines to read"},
    },
    "required": ["path"],
}


def _image_mime_type(path: str) -> str | None:
    return IMAGE_MIME_TYPES.get(os.path.splitext(path)[1].lower())


def _looks_binary(head: bytes) -> bool:
    return b"\x00" in head


async def execute_read(
    cwd: str,
    args: Any,
    signal: AbortSignal | None = None,
) -> AgentToolResult:
    """Implementation shared with tests; separated from the tool factory for clarity."""
    if not isinstance(args, dict):
        return error_result("read requires an object argument with 'path'.")
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return error_result("read requires a non-empty 'path'.")

    offset = args.get("offset")
    limit = args.get("limit")
    if offset is not None and not isinstance(offset, (int, float)):
        return error_result("read 'offset' must be a number.")
    if limit is not None and not isinstance(limit, (int, float)):
        return error_result("read 'limit' must be a number.")

    absolute_path = resolve_read_path(cwd, path)
    display_path = render_path(absolute_path, cwd)

    if not os.path.exists(absolute_path):
        return error_result(f"Path not found: {absolute_path}")
    if os.path.isdir(absolute_path):
        return error_result(f"Path is a directory, not a file: {absolute_path}")

    try:
        mime_type = _image_mime_type(absolute_path)
        if mime_type is not None:
            data = await asyncio.to_thread(_read_bytes, absolute_path)
            encoded = base64.b64encode(data).decode("ascii")
            return AgentToolResult(
                content=[
                    TextContent(text=f"Read image file [{mime_type}]"),
                    ImageContent(data=encoded, mime_type=mime_type),
                ],
                details=None,
            )

        head = await asyncio.to_thread(_read_head, absolute_path, BINARY_SNIFF_BYTES)
    except OSError as exc:
        return error_result(f"Could not read file: {display_path}. {exc}.")

    if _looks_binary(head):
        return error_result(
            f"Cannot read {display_path}: binary file detected. Use bash for binary data."
        )

    try:
        text = await asyncio.to_thread(_read_text, absolute_path)
    except UnicodeDecodeError:
        return error_result(
            f"Cannot read {display_path}: the file is not valid UTF-8 text (binary file?)."
        )
    except OSError as exc:
        return error_result(f"Could not read file: {display_path}. {exc}.")

    if signal is not None and signal.aborted:
        return error_result("Operation aborted")

    all_lines = text.split("\n")
    total_file_lines = len(all_lines)

    start_line = max(0, int(offset) - 1) if offset else 0
    start_line_display = start_line + 1
    if start_line >= len(all_lines):
        return error_result(f"Offset {int(offset)} is beyond end of file ({len(all_lines)} lines total)")

    user_limited_lines: int | None = None
    if limit is not None:
        end_line = max(start_line, min(start_line + int(limit), len(all_lines)))
        selected_content = "\n".join(all_lines[start_line:end_line])
        user_limited_lines = end_line - start_line
    else:
        selected_content = "\n".join(all_lines[start_line:])

    truncation = truncate_head_result(selected_content)

    details: dict[str, Any] | None = None
    if truncation.first_line_exceeds_limit:
        first_line_size = format_size(utf8_len(all_lines[start_line]))
        output_text = (
            f"[Line {start_line_display} is {first_line_size}, exceeds {format_size(DEFAULT_MAX_BYTES)} limit. "
            f"Use bash: sed -n '{start_line_display}p' {path} | head -c {DEFAULT_MAX_BYTES}]"
        )
        details = {"truncation": truncation.to_dict()}
    elif truncation.truncated:
        end_line_display = start_line_display + truncation.output_lines - 1
        next_offset = end_line_display + 1
        output_text = truncation.content
        if truncation.truncated_by == "lines":
            output_text += (
                f"\n\n[Showing lines {start_line_display}-{end_line_display} of {total_file_lines}. "
                f"Use offset={next_offset} to continue.]"
            )
        else:
            output_text += (
                f"\n\n[Showing lines {start_line_display}-{end_line_display} of {total_file_lines} "
                f"({format_size(DEFAULT_MAX_BYTES)} limit). Use offset={next_offset} to continue.]"
            )
        details = {"truncation": truncation.to_dict()}
    elif user_limited_lines is not None and start_line + user_limited_lines < total_file_lines:
        remaining = total_file_lines - (start_line + user_limited_lines)
        next_offset = start_line + user_limited_lines + 1
        output_text = (
            f"{truncation.content}\n\n[{remaining} more lines in file. Use offset={next_offset} to continue.]"
        )
    else:
        output_text = truncation.content

    return text_result(output_text, details)


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _read_head(path: str, size: int) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(size)


def _read_text(path: str) -> str:
    # Decode the raw bytes instead of using text mode: universal-newline
    # translation would rewrite CRLF to LF and change both the model-visible
    # text and the line lengths upstream reports.
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8")


def create_read_tool(cwd: str) -> AgentTool:
    """Build the ``read`` tool bound to ``cwd``."""

    async def execute(
        tool_call_id: str,
        args: Any,
        signal: AbortSignal | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        return await execute_read(cwd, args, signal)

    return AgentTool(
        name="read",
        label="read",
        description=DESCRIPTION,
        parameters=_SCHEMA,
        execute=execute,
        prompt_snippet="Read file contents",
        prompt_guidelines=["Use read to examine files instead of cat or sed."],
    )


__all__ = ["IMAGE_MIME_TYPES", "create_read_tool", "execute_read"]

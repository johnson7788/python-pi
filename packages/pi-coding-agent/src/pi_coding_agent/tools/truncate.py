"""Bounded output truncation shared by the built-in tools.

Two independent budgets apply and whichever is hit first wins: a line budget
and a byte budget.  Head truncation keeps the beginning of the output and never
returns a partial line, because a half line would invite the model to quote
text that does not exist.  Tail truncation keeps the end of the output (where
errors and final results live) and is the one case where the oldest surviving
line may be cut from the front.

Byte accounting is UTF-8 based to match the upstream TypeScript implementation,
which measures with ``Buffer.byteLength``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Upstream defaults (``truncate.ts``).
DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024  # 50KB
#: Longest single line kept by the grep tool before ``... [truncated]`` is added.
GREP_MAX_LINE_LENGTH = 500


def utf8_len(text: str) -> int:
    """Return the UTF-8 byte length, mirroring ``Buffer.byteLength``."""
    return len(text.encode("utf-8"))


@dataclass
class TruncationResult:
    """Outcome of a truncation pass.

    The field set mirrors upstream so that ``details`` blobs stay compatible
    with the session files written by ``pi`` itself.
    """

    content: str
    truncated: bool
    truncated_by: str | None
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    last_line_partial: bool
    first_line_exceeds_limit: bool
    max_lines: int
    max_bytes: int

    def to_dict(self) -> dict[str, Any]:
        """Render as camelCase JSON, matching the upstream object shape."""
        return {
            "content": self.content,
            "truncated": self.truncated,
            "truncatedBy": self.truncated_by,
            "totalLines": self.total_lines,
            "totalBytes": self.total_bytes,
            "outputLines": self.output_lines,
            "outputBytes": self.output_bytes,
            "lastLinePartial": self.last_line_partial,
            "firstLineExceedsLimit": self.first_line_exceeds_limit,
            "maxLines": self.max_lines,
            "maxBytes": self.max_bytes,
        }


def format_size(num_bytes: int) -> str:
    """Format a byte count the way the model-facing notices spell it."""
    if num_bytes < 1024:
        return f"{num_bytes}B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f}KB"
    return f"{num_bytes / (1024 * 1024):.1f}MB"


def split_lines_for_counting(content: str) -> list[str]:
    """Split content into lines without inventing a trailing empty line.

    ``"a\\n"`` counts as one line, not two, which is what upstream does before
    comparing against the line budget.
    """
    if content == "":
        return []
    lines = content.split("\n")
    if content.endswith("\n"):
        lines.pop()
    return lines


def truncate_string_to_bytes_from_end(text: str, max_bytes: int) -> str:
    """Cut ``text`` from the front so that at most ``max_bytes`` UTF-8 bytes remain.

    The cut point is moved forward until it lands on a character boundary, so a
    multi-byte character is never split into invalid UTF-8.
    """
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    start = len(raw) - max_bytes
    while start < len(raw) and (raw[start] & 0xC0) == 0x80:
        start += 1
    return raw[start:].decode("utf-8", errors="replace")


def truncate_head_result(
    content: str,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    """Keep the first ``max_lines`` lines / ``max_bytes`` bytes of ``content``."""
    total_bytes = utf8_len(content)
    lines = split_lines_for_counting(content)
    total_lines = len(lines)

    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(
            content=content,
            truncated=False,
            truncated_by=None,
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=total_lines,
            output_bytes=total_bytes,
            last_line_partial=False,
            first_line_exceeds_limit=False,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    # A single line larger than the whole budget cannot be shown as a line at
    # all; report that so the caller can point at a bash fallback.
    if lines and utf8_len(lines[0]) > max_bytes:
        return TruncationResult(
            content="",
            truncated=True,
            truncated_by="bytes",
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=0,
            output_bytes=0,
            last_line_partial=False,
            first_line_exceeds_limit=True,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    output_lines: list[str] = []
    output_bytes = 0
    truncated_by = "lines"

    for index, line in enumerate(lines):
        if index >= max_lines:
            break
        line_bytes = utf8_len(line) + (1 if index > 0 else 0)  # +1 for the newline
        if output_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            break
        output_lines.append(line)
        output_bytes += line_bytes

    if len(output_lines) >= max_lines and output_bytes <= max_bytes:
        truncated_by = "lines"

    output_content = "\n".join(output_lines)
    return TruncationResult(
        content=output_content,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(output_lines),
        output_bytes=utf8_len(output_content),
        last_line_partial=False,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_tail_result(
    content: str,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    """Keep the last ``max_lines`` lines / ``max_bytes`` bytes of ``content``."""
    total_bytes = utf8_len(content)
    lines = split_lines_for_counting(content)
    total_lines = len(lines)

    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(
            content=content,
            truncated=False,
            truncated_by=None,
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=total_lines,
            output_bytes=total_bytes,
            last_line_partial=False,
            first_line_exceeds_limit=False,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    output_lines: list[str] = []
    output_bytes = 0
    truncated_by = "lines"
    last_line_partial = False

    for index in range(len(lines) - 1, -1, -1):
        if len(output_lines) >= max_lines:
            break
        line = lines[index]
        line_bytes = utf8_len(line) + (1 if output_lines else 0)  # +1 for the newline
        if output_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            # Nothing fits yet: the final line alone is over budget, so keep the
            # tail of that line rather than returning nothing at all.
            if not output_lines:
                partial = truncate_string_to_bytes_from_end(line, max_bytes)
                output_lines.insert(0, partial)
                output_bytes = utf8_len(partial)
                last_line_partial = True
            break
        output_lines.insert(0, line)
        output_bytes += line_bytes

    if len(output_lines) >= max_lines and output_bytes <= max_bytes:
        truncated_by = "lines"

    output_content = "\n".join(output_lines)
    return TruncationResult(
        content=output_content,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(output_lines),
        output_bytes=utf8_len(output_content),
        last_line_partial=last_line_partial,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_head(
    text: str,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[str, bool]:
    """Convenience wrapper returning ``(content, truncated)``."""
    result = truncate_head_result(text, max_lines=max_lines, max_bytes=max_bytes)
    return result.content, result.truncated


def truncate_tail(
    text: str,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[str, bool]:
    """Convenience wrapper returning ``(content, truncated)``."""
    result = truncate_tail_result(text, max_lines=max_lines, max_bytes=max_bytes)
    return result.content, result.truncated


def truncate_line(line: str, max_chars: int = GREP_MAX_LINE_LENGTH) -> tuple[str, bool]:
    """Truncate one output line, appending ``... [truncated]`` when it was cut."""
    if len(line) <= max_chars:
        return line, False
    return f"{line[:max_chars]}... [truncated]", True


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_LINES",
    "GREP_MAX_LINE_LENGTH",
    "TruncationResult",
    "format_size",
    "split_lines_for_counting",
    "truncate_head",
    "truncate_head_result",
    "truncate_line",
    "truncate_string_to_bytes_from_end",
    "truncate_tail",
    "truncate_tail_result",
    "utf8_len",
]

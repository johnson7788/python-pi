"""The ``edit`` tool: exact-text replacement with multiple disjoint blocks.

All ``oldText`` values are matched against the *original* file, never against a
partially edited view, so one call can express several independent changes
without the model having to reason about offsets shifting.  Matching happens in
LF-normalised space; the BOM and the file's dominant line ending are restored on
write so an edit never rewrites line endings as a side effect.

The matcher first tries an exact substring match and only then a fuzzy
normalisation (NFKC, trailing whitespace, typographic quotes/dashes/spaces).
When any edit needed fuzzy matching the result is re-assembled line by line so
unchanged lines keep their original bytes; unlike upstream, the fuzzy pass is
reported as a success rather than being recorded separately.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from pi_agent import AgentTool, AgentToolResult, ToolUpdateCallback
from pi_ai import AbortSignal

from .path_utils import error_result, render_path, resolve_path, text_result
from .write import FileMutationQueue, get_file_mutation_queue

DESCRIPTION = (
    "Edit a single file using exact text replacement. Every edits[].oldText must match a unique, "
    "non-overlapping region of the original file. If two changes affect the same block or nearby "
    "lines, merge them into one edit instead of emitting overlapping edits. Do not include large "
    "unchanged regions just to connect distant changes."
)

_BOM = "\ufeff"


class EditError(Exception):
    """A user-facing edit failure (message is shown to the model verbatim)."""


# --------------------------------------------------------------------------------------
# Text normalisation
# --------------------------------------------------------------------------------------


def split_bom(raw: str) -> tuple[str, str]:
    """Split a leading UTF-8 BOM from the text it prefixes."""
    if raw.startswith(_BOM):
        return _BOM, raw[len(_BOM) :]
    return "", raw


def detect_line_ending(content: str) -> str:
    """Return the file's dominant line ending, based on the first newline."""
    crlf_index = content.find("\r\n")
    lf_index = content.find("\n")
    if lf_index == -1 or crlf_index == -1:
        return "\n"
    return "\r\n" if crlf_index < lf_index else "\n"


def normalize_to_lf(text: str) -> str:
    """Collapse CRLF and lone CR to LF so matching is line-ending agnostic."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def restore_line_endings(text: str, ending: str) -> str:
    """Re-apply the original line ending to LF-normalised text."""
    return text.replace("\n", "\r\n") if ending == "\r\n" else text


def normalize_for_fuzzy_match(text: str) -> str:
    """Fold the differences models routinely introduce when retyping text.

    NFKC, trailing whitespace, curly quotes, unicode dashes and exotic spaces
    are the transformations upstream applies; anything stricter would silently
    accept genuinely different text.
    """
    folded = unicodedata.normalize("NFKC", text)
    folded = "\n".join(line.rstrip() for line in folded.split("\n"))
    folded = re.sub("[\u2018\u2019\u201a\u201b]", "'", folded)
    folded = re.sub("[\u201c\u201d\u201e\u201f]", '"', folded)
    folded = re.sub("[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]", "-", folded)
    folded = re.sub("[\u00a0\u2002-\u200a\u202f\u205f\u3000]", " ", folded)
    return folded


# --------------------------------------------------------------------------------------
# Matching / applying
# --------------------------------------------------------------------------------------


@dataclass
class FuzzyMatch:
    """Result of looking for one ``oldText``."""

    found: bool
    index: int
    match_length: int
    used_fuzzy_match: bool
    content_for_replacement: str


@dataclass
class MatchedEdit:
    """A located replacement, positioned in the content it was matched in."""

    edit_index: int
    match_index: int
    match_length: int
    new_text: str


@dataclass
class _LineSpan:
    start: int
    end: int


def fuzzy_find_text(content: str, old_text: str) -> FuzzyMatch:
    """Find ``old_text`` in ``content``, exact match first then fuzzy."""
    exact_index = content.find(old_text)
    if exact_index != -1:
        return FuzzyMatch(True, exact_index, len(old_text), False, content)

    fuzzy_content = normalize_for_fuzzy_match(content)
    fuzzy_old_text = normalize_for_fuzzy_match(old_text)
    fuzzy_index = fuzzy_content.find(fuzzy_old_text)
    if fuzzy_index == -1:
        return FuzzyMatch(False, -1, 0, False, content)
    return FuzzyMatch(True, fuzzy_index, len(fuzzy_old_text), True, fuzzy_content)


def count_occurrences(content: str, old_text: str) -> int:
    """Count fuzzy-normalised occurrences of ``old_text``."""
    return normalize_for_fuzzy_match(content).count(normalize_for_fuzzy_match(old_text))


def _split_lines_with_endings(content: str) -> list[str]:
    return re.findall(r"[^\n]*\n|[^\n]+", content)


def _get_line_spans(content: str) -> list[_LineSpan]:
    offset = 0
    spans: list[_LineSpan] = []
    for line in _split_lines_with_endings(content):
        spans.append(_LineSpan(offset, offset + len(line)))
        offset += len(line)
    return spans


def _get_replacement_line_range(spans: list[_LineSpan], replacement: MatchedEdit) -> tuple[int, int]:
    replacement_start = replacement.match_index
    replacement_end = replacement.match_index + replacement.match_length

    start_line = -1
    for index, span in enumerate(spans):
        if span.start <= replacement_start < span.end:
            start_line = index
            break
    if start_line == -1:
        raise EditError("Replacement range is outside the base content.")

    end_line = start_line
    while end_line < len(spans) and spans[end_line].end < replacement_end:
        end_line += 1
    if end_line >= len(spans):
        raise EditError("Replacement range is outside the base content.")
    return start_line, end_line + 1


def _apply_replacements(content: str, replacements: list[MatchedEdit], offset: int = 0) -> str:
    """Apply replacements right-to-left so earlier offsets stay valid."""
    result = content
    for replacement in reversed(replacements):
        index = replacement.match_index - offset
        result = result[:index] + replacement.new_text + result[index + replacement.match_length :]
    return result


def apply_replacements_preserving_unchanged_lines(
    original_content: str,
    base_content: str,
    replacements: list[MatchedEdit],
) -> str:
    """Rebuild ``base_content`` with replacements, copying untouched lines from ``original_content``.

    ``base_content`` is the fuzzy-normalised view of ``original_content``.  Only
    the lines each replacement actually touches are taken from that view, so
    normalisation cannot leak into unrelated lines and duplicate normalised
    lines cannot be aligned to the wrong occurrence.
    """
    original_lines = _split_lines_with_endings(original_content)
    base_lines = _get_line_spans(base_content)
    if len(original_lines) != len(base_lines):
        raise EditError("Cannot preserve unchanged lines because the base content has a different line count.")

    groups: list[dict[str, Any]] = []
    for replacement in sorted(replacements, key=lambda item: item.match_index):
        start_line, end_line = _get_replacement_line_range(base_lines, replacement)
        if groups and start_line < groups[-1]["end_line"]:
            group = groups[-1]
            group["end_line"] = max(group["end_line"], end_line)
            group["replacements"].append(replacement)
            continue
        groups.append({"start_line": start_line, "end_line": end_line, "replacements": [replacement]})

    original_line_index = 0
    result = ""
    for group in groups:
        result += "".join(original_lines[original_line_index : group["start_line"]])
        group_start = base_lines[group["start_line"]].start
        group_end = base_lines[group["end_line"] - 1].end
        result += _apply_replacements(base_content[group_start:group_end], group["replacements"], group_start)
        original_line_index = group["end_line"]
    result += "".join(original_lines[original_line_index:])
    return result


def _not_found_error(path: str, edit_index: int, total_edits: int) -> str:
    if total_edits == 1:
        return (
            f"Could not find the exact text in {path}. The old text must match exactly "
            "including all whitespace and newlines."
        )
    return (
        f"Could not find edits[{edit_index}] in {path}. The oldText must match exactly "
        "including all whitespace and newlines."
    )


def _duplicate_error(path: str, edit_index: int, total_edits: int, occurrences: int) -> str:
    if total_edits == 1:
        return (
            f"Found {occurrences} occurrences of the text in {path}. The text must be unique. "
            "Please provide more context to make it unique."
        )
    return (
        f"Found {occurrences} occurrences of edits[{edit_index}] in {path}. Each oldText must be unique. "
        "Please provide more context to make it unique."
    )


def _empty_old_text_error(path: str, edit_index: int, total_edits: int) -> str:
    if total_edits == 1:
        return f"oldText must not be empty in {path}."
    return f"edits[{edit_index}].oldText must not be empty in {path}."


def _no_change_error(path: str, total_edits: int) -> str:
    if total_edits == 1:
        return (
            f"No changes made to {path}. The replacement produced identical content. This might indicate "
            "an issue with special characters or the text not existing as expected."
        )
    return f"No changes made to {path}. The replacements produced identical content."


def apply_edits_to_normalized_content(
    normalized_content: str,
    edits: list[dict[str, str]],
    path: str,
) -> tuple[str, str]:
    """Apply every edit to the same snapshot; returns ``(base_content, new_content)``."""
    normalized_edits = [
        {"oldText": normalize_to_lf(edit["oldText"]), "newText": normalize_to_lf(edit["newText"])} for edit in edits
    ]

    for index, edit in enumerate(normalized_edits):
        if edit["oldText"] == "":
            raise EditError(_empty_old_text_error(path, index, len(normalized_edits)))

    initial_matches = [fuzzy_find_text(normalized_content, edit["oldText"]) for edit in normalized_edits]
    used_fuzzy_match = any(match.used_fuzzy_match for match in initial_matches)
    replacement_base = normalize_for_fuzzy_match(normalized_content) if used_fuzzy_match else normalized_content

    matched_edits: list[MatchedEdit] = []
    for index, edit in enumerate(normalized_edits):
        match = fuzzy_find_text(replacement_base, edit["oldText"])
        if not match.found:
            raise EditError(_not_found_error(path, index, len(normalized_edits)))

        occurrences = count_occurrences(replacement_base, edit["oldText"])
        if occurrences > 1:
            raise EditError(_duplicate_error(path, index, len(normalized_edits), occurrences))

        matched_edits.append(MatchedEdit(index, match.index, match.match_length, edit["newText"]))

    matched_edits.sort(key=lambda item: item.match_index)
    for previous, current in zip(matched_edits, matched_edits[1:]):
        if previous.match_index + previous.match_length > current.match_index:
            raise EditError(
                f"edits[{previous.edit_index}] and edits[{current.edit_index}] overlap in {path}. "
                "Merge them into one edit or target disjoint regions."
            )

    base_content = normalized_content
    if used_fuzzy_match:
        new_content = apply_replacements_preserving_unchanged_lines(
            normalized_content, replacement_base, matched_edits
        )
    else:
        new_content = _apply_replacements(replacement_base, matched_edits)

    if base_content == new_content:
        raise EditError(_no_change_error(path, len(normalized_edits)))

    return base_content, new_content


# --------------------------------------------------------------------------------------
# Diff rendering
# --------------------------------------------------------------------------------------


def _diff_parts(old_lines: list[str], new_lines: list[str]) -> list[tuple[str, list[str]]]:
    """Group changed lines into ``diffLines``-style parts (equal/added/removed)."""
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    parts: list[tuple[str, list[str]]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            parts.append(("equal", old_lines[i1:i2]))
        elif tag == "delete":
            parts.append(("removed", old_lines[i1:i2]))
        elif tag == "insert":
            parts.append(("added", new_lines[j1:j2]))
        else:  # replace: upstream emits the removals before the additions
            parts.append(("removed", old_lines[i1:i2]))
            parts.append(("added", new_lines[j1:j2]))
    return parts


def generate_diff_string(
    old_content: str,
    new_content: str,
    context_lines: int = 4,
) -> tuple[str, int | None]:
    """Render a compact ``+N line`` / ``-N line`` diff and the first changed line.

    The line number is the line's position in the *new* file for additions and
    in the old file for removals, which is what the TUI and editor integrations
    use for navigation.
    """
    old_lines = old_content.split("\n")
    new_lines = new_content.split("\n")
    counting_old = list(old_lines)
    counting_new = list(new_lines)
    if counting_old and counting_old[-1] == "":
        counting_old.pop()
    if counting_new and counting_new[-1] == "":
        counting_new.pop()

    width = len(str(max(len(old_lines), len(new_lines), 1)))
    parts = _diff_parts(counting_old, counting_new)

    output: list[str] = []
    old_line_num = 1
    new_line_num = 1
    last_was_change = False
    first_changed_line: int | None = None

    for index, (kind, raw) in enumerate(parts):
        if kind in ("added", "removed"):
            if first_changed_line is None:
                first_changed_line = new_line_num
            for line in raw:
                if kind == "added":
                    output.append(f"+{str(new_line_num).rjust(width)} {line}")
                    new_line_num += 1
                else:
                    output.append(f"-{str(old_line_num).rjust(width)} {line}")
                    old_line_num += 1
            last_was_change = True
            continue

        next_is_change = index < len(parts) - 1 and parts[index + 1][0] in ("added", "removed")
        if last_was_change and next_is_change:
            if len(raw) <= context_lines * 2:
                for line in raw:
                    output.append(f" {str(old_line_num).rjust(width)} {line}")
                    old_line_num += 1
                    new_line_num += 1
            else:
                leading = raw[:context_lines]
                trailing = raw[len(raw) - context_lines :]
                skipped = len(raw) - len(leading) - len(trailing)
                for line in leading:
                    output.append(f" {str(old_line_num).rjust(width)} {line}")
                    old_line_num += 1
                    new_line_num += 1
                output.append(f" {' ' * width} ...")
                old_line_num += skipped
                new_line_num += skipped
                for line in trailing:
                    output.append(f" {str(old_line_num).rjust(width)} {line}")
                    old_line_num += 1
                    new_line_num += 1
        elif last_was_change:
            shown = raw[:context_lines]
            skipped = len(raw) - len(shown)
            for line in shown:
                output.append(f" {str(old_line_num).rjust(width)} {line}")
                old_line_num += 1
                new_line_num += 1
            if skipped > 0:
                output.append(f" {' ' * width} ...")
                old_line_num += skipped
                new_line_num += skipped
        elif next_is_change:
            skipped = max(0, len(raw) - context_lines)
            if skipped > 0:
                output.append(f" {' ' * width} ...")
                old_line_num += skipped
                new_line_num += skipped
            for line in raw[skipped:]:
                output.append(f" {str(old_line_num).rjust(width)} {line}")
                old_line_num += 1
                new_line_num += 1
        else:
            old_line_num += len(raw)
            new_line_num += len(raw)

        last_was_change = False

    return "\n".join(output), first_changed_line


def generate_unified_patch(path: str, old_content: str, new_content: str, context_lines: int = 4) -> str:
    """Return a standard unified patch for the change."""
    diff_lines = list(
        difflib.unified_diff(
            old_content.splitlines(),
            new_content.splitlines(),
            fromfile=path,
            tofile=path,
            n=context_lines,
            lineterm="",
        )
    )
    if not diff_lines:
        return ""
    return "\n".join(diff_lines) + "\n"


# --------------------------------------------------------------------------------------
# Tool
# --------------------------------------------------------------------------------------

_EDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Path to the file to edit (relative or absolute)"},
        "edits": {
            "type": "array",
            "description": (
                "One or more targeted replacements. Each edit is matched against the original file, not "
                "incrementally. Do not include overlapping or nested edits. If two changes touch the same "
                "block or nearby lines, merge them into one edit instead."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "oldText": {
                        "type": "string",
                        "description": (
                            "Exact text for one targeted replacement. It must be unique in the original file "
                            "and must not overlap with any other edits[].oldText in the same call."
                        ),
                    },
                    "newText": {"type": "string", "description": "Replacement text for this targeted edit."},
                },
                "required": ["oldText", "newText"],
            },
        },
    },
    "required": ["path", "edits"],
}


def prepare_edit_arguments(args: dict[str, Any]) -> dict[str, Any]:
    """Normalise the shapes models actually send for ``edits``.

    Some models serialise the array as a JSON string, some send a single edit
    object, and older prompts used top-level ``oldText``/``newText``.
    """
    if not isinstance(args, dict):
        return args
    prepared = dict(args)

    edits = prepared.get("edits")
    if isinstance(edits, str):
        try:
            parsed = json.loads(edits)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            prepared["edits"] = parsed
        elif isinstance(parsed, dict) and "oldText" in parsed and "newText" in parsed:
            prepared["edits"] = [parsed]
    elif isinstance(edits, dict) and "oldText" in edits and "newText" in edits:
        prepared["edits"] = [edits]

    legacy_old = prepared.get("oldText")
    legacy_new = prepared.get("newText")
    if isinstance(legacy_old, str) and isinstance(legacy_new, str):
        edits_list = list(prepared["edits"]) if isinstance(prepared.get("edits"), list) else []
        edits_list.append({"oldText": legacy_old, "newText": legacy_new})
        prepared["edits"] = edits_list
        prepared.pop("oldText", None)
        prepared.pop("newText", None)
    return prepared


def _validate_edits(args: dict[str, Any]) -> list[dict[str, str]]:
    edits = args.get("edits")
    if not isinstance(edits, list) or not edits:
        raise EditError("Edit tool input is invalid. edits must contain at least one replacement.")
    normalized: list[dict[str, str]] = []
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise EditError(f"edits[{index}] must be an object with 'oldText' and 'newText'.")
        old_text = edit.get("oldText")
        new_text = edit.get("newText")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            raise EditError(f"edits[{index}] requires string 'oldText' and 'newText'.")
        normalized.append({"oldText": old_text, "newText": new_text})
    return normalized


def _read_text(path: str) -> str:
    # newline="" disables universal-newline translation: the file's original
    # CRLF bytes must survive so the dominant line ending can be detected and
    # restored, otherwise every edit would silently rewrite the file to LF.
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _write_text(path: str, content: str) -> None:
    # newline="" keeps the already-restored line endings byte-exact on write.
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)


async def _apply_and_write(
    absolute_path: str,
    edits: list[dict[str, str]],
    display_path: str,
    signal: AbortSignal | None,
) -> tuple[str, str, str, int | None, int]:
    """Read, edit and write; returns the pieces the caller reports on."""
    if signal is not None and signal.aborted:
        raise EditError("Operation aborted")

    if not os.path.exists(absolute_path):
        raise EditError(f"Could not edit file: {display_path}. No such file or directory.")

    raw_content = await asyncio.to_thread(_read_text, absolute_path)
    if signal is not None and signal.aborted:
        raise EditError("Operation aborted")

    bom, content = split_bom(raw_content)
    original_ending = detect_line_ending(content)
    normalized_content = normalize_to_lf(content)
    base_content, new_content = apply_edits_to_normalized_content(normalized_content, edits, display_path)
    if signal is not None and signal.aborted:
        raise EditError("Operation aborted")

    final_content = bom + restore_line_endings(new_content, original_ending)
    await asyncio.to_thread(_write_text, absolute_path, final_content)
    if signal is not None and signal.aborted:
        raise EditError("Operation aborted")

    diff, first_changed_line = generate_diff_string(base_content, new_content)
    patch = generate_unified_patch(display_path, base_content, new_content)
    return diff, patch, base_content, first_changed_line, len(edits)


def create_edit_tool(cwd: str, mutation_queue: FileMutationQueue | None = None) -> AgentTool:
    """Build the ``edit`` tool bound to ``cwd``."""
    queue = mutation_queue or get_file_mutation_queue()

    async def execute(
        tool_call_id: str,
        args: Any,
        signal: AbortSignal | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        if not isinstance(args, dict):
            return error_result("edit requires an object argument with 'path' and 'edits'.")
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return error_result("edit requires a non-empty 'path'.")
        try:
            edits = _validate_edits(args)
        except EditError as exc:
            return error_result(str(exc))

        absolute_path = resolve_path(cwd, path)
        display_path = render_path(absolute_path, cwd)
        try:
            async with queue.hold(absolute_path):
                diff, patch, _base, first_changed_line, count = await _apply_and_write(
                    absolute_path, edits, display_path, signal
                )
        except EditError as exc:
            return error_result(str(exc))
        except OSError as exc:
            return error_result(f"Could not edit file: {display_path}. {exc}.")
        except UnicodeDecodeError:
            return error_result(f"Could not edit file: {display_path}. The file is not valid UTF-8 text.")

        details: dict[str, Any] = {"diff": diff, "patch": patch}
        if first_changed_line is not None:
            details["firstChangedLine"] = first_changed_line
        return text_result(f"Successfully replaced {count} block(s) in {path}.", details=details)

    return AgentTool(
        name="edit",
        label="edit",
        description=DESCRIPTION,
        parameters=_EDIT_SCHEMA,
        execute=execute,
        prompt_snippet=(
            "Make precise file edits with exact text replacement, including multiple disjoint edits in one call"
        ),
        prompt_guidelines=[
            "Use edit for precise changes (edits[].oldText must match exactly)",
            "When changing multiple separate locations in one file, use one edit call with multiple entries in "
            "edits[] instead of multiple edit calls",
            "Each edits[].oldText is matched against the original file, not after earlier edits are applied. "
            "Do not emit overlapping or nested edits. Merge nearby changes into one edit.",
            "Keep edits[].oldText as small as possible while still being unique in the file. Do not pad with "
            "large unchanged regions.",
        ],
        prepare_arguments=prepare_edit_arguments,
    )


__all__ = [
    "EditError",
    "apply_edits_to_normalized_content",
    "apply_replacements_preserving_unchanged_lines",
    "count_occurrences",
    "create_edit_tool",
    "detect_line_ending",
    "fuzzy_find_text",
    "generate_diff_string",
    "generate_unified_patch",
    "normalize_for_fuzzy_match",
    "normalize_to_lf",
    "prepare_edit_arguments",
    "restore_line_endings",
    "split_bom",
]

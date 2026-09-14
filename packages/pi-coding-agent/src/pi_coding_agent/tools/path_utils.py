"""Path resolution, result helpers and filesystem walking for the built-in tools.

This module is the shared leaf of the ``tools`` package: every tool needs path
normalisation, and every non-shell tool needs to turn a failure into an error
result instead of raising.  Keeping both here avoids a cycle in which
``tools/__init__`` would have to define helpers before importing its own
submodules.
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Iterator

from pi_agent import AgentToolResult
from pi_ai import TextContent

#: Unicode spaces that macOS and copy/paste routinely smuggle into paths.
_UNICODE_SPACES = re.compile("[\u00a0\u2000-\u200a\u202f\u205f\u3000]")
_NARROW_NO_BREAK_SPACE = "\u202f"


# --------------------------------------------------------------------------------------
# Result helpers
# --------------------------------------------------------------------------------------


def error_result(message: str) -> AgentToolResult:
    """Build the standard tool failure result.

    The agent loop only learns that a tool failed from the tool result, so the
    error flag travels in ``details`` alongside the human-readable message.
    """
    return AgentToolResult(content=[TextContent(text=message)], details={"error": True})


def text_result(text: str, details: Any = None) -> AgentToolResult:
    """Build a successful text result."""
    return AgentToolResult(content=[TextContent(text=text)], details=details)


# --------------------------------------------------------------------------------------
# Path resolution
# --------------------------------------------------------------------------------------


def normalize_path(
    path: str,
    *,
    expand_tilde: bool = True,
    strip_at_prefix: bool = True,
    normalize_unicode_spaces: bool = True,
) -> str:
    """Normalise a user-supplied path without touching the filesystem.

    ``@`` prefixes come from CLI ``@file`` references and unicode spaces come
    from copy/paste; both would otherwise produce a path the shell can open but
    the tool cannot.
    """
    normalized = path
    if normalize_unicode_spaces:
        normalized = _UNICODE_SPACES.sub(" ", normalized)
    if strip_at_prefix and normalized.startswith("@"):
        normalized = normalized[1:]
    if expand_tilde:
        if normalized == "~":
            return str(Path.home())
        if normalized.startswith("~/") or normalized.startswith("~\\"):
            return os.path.join(str(Path.home()), normalized[2:])
    return normalized


def resolve_path(cwd: str, path: str) -> str:
    """Resolve ``path`` against ``cwd``, returning a normalised absolute path."""
    normalized = normalize_path(path)
    base = normalize_path(cwd)
    if not os.path.isabs(normalized):
        normalized = os.path.join(base, normalized)
    return os.path.normpath(os.path.abspath(normalized))


def path_exists(path: str) -> bool:
    """Return whether ``path`` exists, treating permission errors as absent."""
    return os.path.exists(path)


def render_path(path: str, cwd: str) -> str:
    """Render ``path`` relative to ``cwd`` with forward slashes.

    Absolute paths are kept when the target lives outside ``cwd`` so the model
    is never shown a ``../..`` chain it cannot act on.
    """
    absolute = resolve_path(cwd, path) if not os.path.isabs(path) else os.path.normpath(path)
    try:
        relative = os.path.relpath(absolute, resolve_path(cwd, "."))
    except ValueError:  # different drive on Windows
        return PurePath(absolute).as_posix()
    if relative == ".." or relative.startswith(".." + os.sep):
        return PurePath(absolute).as_posix()
    return PurePath(relative).as_posix() if relative != "." else "."


def resolve_read_path(cwd: str, path: str) -> str:
    """Resolve a read path, probing macOS filename variants on a miss.

    macOS stores filenames decomposed (NFD) and screenshots use a narrow
    no-break space plus a typographic apostrophe, so a path typed by hand
    frequently does not match the bytes on disk.
    """
    resolved = resolve_path(cwd, path)
    if os.path.exists(resolved):
        return resolved

    candidates = [
        re.sub(r" ([AaPp][Mm])\.", _NARROW_NO_BREAK_SPACE + r"\1.", resolved),
        _normalize_nfd(resolved),
        resolved.replace("'", "\u2019"),
    ]
    candidates.append(_normalize_nfd(candidates[2]))
    for candidate in candidates:
        if candidate != resolved and os.path.exists(candidate):
            return candidate
    return resolved


def _normalize_nfd(path: str) -> str:
    return unicodedata.normalize("NFD", path)


# --------------------------------------------------------------------------------------
# Glob matching
# --------------------------------------------------------------------------------------


def _translate_glob(pattern: str) -> str:
    """Translate a path glob into a regular expression.

    ``**`` crosses directory separators (and ``**/`` may also match nothing),
    while ``*`` and ``?`` stay inside one path segment -- the same split fd and
    ripgrep use.
    """
    out: list[str] = []
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "*":
            end = index
            while end < length and pattern[end] == "*":
                end += 1
            if end - index >= 2 and end < length and pattern[end] == "/":
                out.append("(?:.*/)?")
                index = end + 1
                continue
            out.append(".*" if end - index >= 2 else "[^/]*")
            index = end
            continue
        if char == "?":
            out.append("[^/]")
            index += 1
            continue
        if char == "[":
            end = index + 1
            if end < length and pattern[end] in "!^":
                end += 1
            if end < length and pattern[end] == "]":
                end += 1
            while end < length and pattern[end] != "]":
                end += 1
            if end >= length:
                out.append(re.escape("["))
                index += 1
                continue
            inner = pattern[index + 1 : end]
            if inner.startswith("!"):
                inner = "^" + inner[1:]
            out.append("[" + inner + "]")
            index = end + 1
            continue
        out.append(re.escape(char))
        index += 1
    return "".join(out)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a path glob into an anchored regular expression."""
    return re.compile(_translate_glob(pattern) + r"\Z")


def matches_glob(pattern: str, relative_posix: str) -> bool:
    """Match ``relative_posix`` against ``pattern``.

    A pattern containing ``/`` is matched against the whole relative path with
    an implicit ``**/`` prefix (so ``src/*.py`` matches at any depth, like
    ``fd --full-path``); a bare pattern matches the basename at any depth.
    """
    if "/" in pattern:
        normalized = pattern.lstrip("/")
        if normalized != "**" and not normalized.startswith("**/"):
            normalized = "**/" + normalized
        return glob_to_regex(normalized).match(relative_posix) is not None
    return glob_to_regex(pattern).match(posixpath.basename(relative_posix)) is not None


# --------------------------------------------------------------------------------------
# .gitignore matching (deliberately simple)
# --------------------------------------------------------------------------------------


@dataclass
class IgnoreRule:
    """One parsed ``.gitignore`` line."""

    pattern: str
    negated: bool
    directory_only: bool
    anchored: bool


def parse_gitignore(text: str) -> list[IgnoreRule]:
    """Parse ``.gitignore`` content into rules.

    Only the common syntax is implemented: comments, ``!`` negation, trailing
    ``/`` for directories, leading ``/`` anchoring and ``**`` segments.  Git's
    rule that a directory can never be re-included once excluded is not
    modelled, which is a documented simplification.
    """
    rules: list[IgnoreRule] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # Trailing spaces are ignored unless escaped; a simplification is fine.
        line = line.rstrip()
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        directory_only = line.endswith("/")
        if directory_only:
            line = line[:-1]
        if not line:
            continue
        anchored = line.startswith("/")
        if anchored:
            line = line[1:]
        rules.append(IgnoreRule(pattern=line, negated=negated, directory_only=directory_only, anchored=anchored))
    return rules


def _rule_matches(rule: IgnoreRule, relative_posix: str, is_dir: bool) -> bool:
    if rule.directory_only and not is_dir:
        return False
    if rule.anchored:
        return relative_posix == rule.pattern or relative_posix.startswith(rule.pattern + "/")
    if "/" in rule.pattern:
        if fnmatch.fnmatchcase(relative_posix, rule.pattern):
            return True
        return relative_posix.startswith(rule.pattern.rstrip("/") + "/")
    # A pattern without a separator matches the basename at any depth.
    return fnmatch.fnmatchcase(posixpath.basename(relative_posix), rule.pattern)


class IgnoreMatcher:
    """Stack of ``.gitignore`` rule sets collected while descending a tree."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self._levels: list[tuple[str, list[IgnoreRule]]] = []

    def push(self, directory: str) -> None:
        """Load ``<directory>/.gitignore`` and make its rules active."""
        gitignore = os.path.join(directory, ".gitignore")
        rules: list[IgnoreRule] = []
        try:
            with open(gitignore, "r", encoding="utf-8", errors="replace") as handle:
                rules = parse_gitignore(handle.read())
        except OSError:
            rules = []
        self._levels.append((os.path.abspath(directory), rules))

    def pop(self) -> None:
        if self._levels:
            self._levels.pop()

    def is_ignored(self, path: str, is_dir: bool) -> bool:
        """Return True when the last matching rule (if any) ignores ``path``."""
        absolute = os.path.abspath(path)
        ignored = False
        for directory, rules in self._levels:
            try:
                relative = os.path.relpath(absolute, directory)
            except ValueError:
                continue
            if relative.startswith(".."):
                continue
            relative_posix = PurePath(relative).as_posix()
            for rule in rules:
                if _rule_matches(rule, relative_posix, is_dir):
                    ignored = not rule.negated
        return ignored


def walk_files(
    root: str,
    *,
    respect_gitignore: bool = True,
    skip_git_dir: bool = True,
) -> Iterator[tuple[str, str]]:
    """Yield ``(absolute_path, relative_posix_path)`` for files under ``root``.

    Sorted traversal keeps tool output deterministic, which matters because the
    result is truncated to a fixed number of entries.  Symlinked directories are
    not followed, so a cyclic symlink cannot trap the walk.
    """
    absolute_root = os.path.abspath(root)
    if os.path.isfile(absolute_root):
        yield absolute_root, posixpath.basename(absolute_root)
        return
    if not os.path.isdir(absolute_root):
        return

    matcher = IgnoreMatcher(absolute_root) if respect_gitignore else None

    def recurse(directory: str) -> Iterator[tuple[str, str]]:
        if matcher is not None:
            matcher.push(directory)
        try:
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda entry: entry.name)
            except OSError:
                return
            for entry in entries:
                if skip_git_dir and entry.is_dir(follow_symlinks=False) and entry.name == ".git":
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if matcher is not None and matcher.is_ignored(entry.path, is_dir):
                    continue
                if is_dir:
                    yield from recurse(entry.path)
                else:
                    try:
                        relative = os.path.relpath(entry.path, absolute_root)
                    except ValueError:
                        continue
                    yield entry.path, PurePath(relative).as_posix()
        finally:
            if matcher is not None:
                matcher.pop()

    yield from recurse(absolute_root)


__all__ = [
    "IgnoreMatcher",
    "IgnoreRule",
    "error_result",
    "glob_to_regex",
    "matches_glob",
    "normalize_path",
    "parse_gitignore",
    "path_exists",
    "render_path",
    "resolve_path",
    "resolve_read_path",
    "text_result",
    "walk_files",
]
